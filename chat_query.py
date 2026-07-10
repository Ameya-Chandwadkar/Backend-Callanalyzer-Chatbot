"""
chat_query.py
Phase 3 of the PRD: the chat/LLM interface over the unified data store.

=============================================================================
DESIGN GOAL: this file should not need editing again.
=============================================================================
Historically, three things forced changes to this file. Each is now external
or dynamic, so changing them never means touching this code:

  1. SCHEMA DRIFT — the table list used to be a hardcoded string that silently
     went stale whenever schema.sql changed. The schema is now introspected
     live from the database on every run (see build_schema_description).
     Add a table or column to schema.sql and the assistant knows immediately.

  2. PROVIDER CHANGES — the LLM provider used to be hardcoded, so moving
     Anthropic -> OpenAI -> Gemini -> Groq -> Cerebras meant a rewrite each
     time. Providers are now config (see PROVIDERS + CHAT_PROVIDER in .env).
     Every provider here is OpenAI-compatible; switching is a one-line .env
     edit. A fallback provider can be configured for automatic failover.

  3. BUSINESS RULES — MasonMart's compliance rules (call thresholds, working
     hours, lead tags) change periodically. They now live in an optional
     plain-text file, business_rules.md, loaded at runtime. Edit that file;
     never this one. Per the PRD, rules are deliberately not baked into the
     integration layer.

=============================================================================
ACCURACY MODEL (per PRD 5.4 / NFR "Accuracy")
=============================================================================
The LLM never computes numbers and never sees data it didn't query for:

    1. The model reads the live schema + business rules + recent conversation
       and writes ONE read-only SQL query.
    2. This script runs that exact SQL against masonmart.sqlite, on a
       READ-ONLY connection guarded by SQLite's own authorizer callback.
    3. If the SQL errors, the error is fed back to the model, which gets a
       limited number of attempts to correct itself (see MAX_SQL_ATTEMPTS).
    4. The model sees only the query's real output — plus how fresh the data
       is — and turns it into a plain-language answer.

Arithmetic is always done by SQLite, never guessed by the model.

=============================================================================
SAFETY
=============================================================================
Read-only is enforced three ways, not by string-matching (which produced
false positives — a customer named "Dropadi" or a lead status of "deleted"
used to be wrongly refused):

  * the database is opened with SQLite's ?mode=ro URI, so writes are
    impossible at the file level;
  * a SQLite authorizer callback rejects anything that isn't SELECT/READ;
  * a progress handler aborts runaway queries after QUERY_TIMEOUT_SECONDS,
    and only MAX_RESULT_ROWS rows are ever read into memory.

SETUP:
    1. pip install requests
    2. Get a free API key from your provider (default: cloud.cerebras.ai,
       no credit card) and put it in .env.
    3. python chat_query.py "how many calls did Sara make yesterday?"
       or run with no arguments for an interactive prompt loop.
"""

import os
import re
import sys
import json
import time
import sqlite3
import logging
from datetime import date

from common import get_connection, SCRIPT_DIR, DB_PATH

try:
    import requests
except ImportError:
    print("ERROR: 'requests' module not found. Run: pip install requests")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

MAX_RESULT_ROWS = 200          # never read more than this into memory
MAX_ROWS_TO_SUMMARIZE = 40     # rows actually shown to the model
QUERY_TIMEOUT_SECONDS = 10     # abort runaway queries
MAX_SQL_ATTEMPTS = 3           # self-correction attempts on SQL errors
HISTORY_TURNS_TO_INCLUDE = 12  # prior Q&A pairs replayed as context
API_MAX_RETRIES = 4            # retries on rate limit / transient server error

# Every provider below exposes an OpenAI-compatible /chat/completions
# endpoint, so switching between them requires no code change — only
# CHAT_PROVIDER and the matching API key in .env.
PROVIDERS = {
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model_env": "CEREBRAS_MODEL",
        "default_model": "gpt-oss-120b",
        "signup": "cloud.cerebras.ai",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "signup": "console.groq.com",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
        "signup": "platform.openai.com",
    },
    "together": {
        "url": "https://api.together.xyz/v1/chat/completions",
        "key_env": "TOGETHER_API_KEY",
        "model_env": "TOGETHER_MODEL",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "signup": "together.ai",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
        "signup": "openrouter.ai",
    },
}


def load_env():
    env_path = os.path.join(SCRIPT_DIR, ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()

logging.basicConfig(
    filename=os.path.join(SCRIPT_DIR, "chat_query.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


class Provider:
    """One configured LLM endpoint. Providers are interchangeable because
    they all speak the OpenAI chat-completions dialect."""

    def __init__(self, name):
        if name not in PROVIDERS:
            raise ValueError(
                f"Unknown CHAT_PROVIDER '{name}'. "
                f"Valid options: {', '.join(sorted(PROVIDERS))}"
            )
        spec = PROVIDERS[name]
        self.name = name
        self.url = spec["url"]
        self.signup = spec["signup"]
        self.key = ENV.get(spec["key_env"])
        self.key_env = spec["key_env"]
        self.model = ENV.get(spec["model_env"], spec["default_model"])

    @property
    def configured(self):
        return bool(self.key)


PRIMARY = Provider(ENV.get("CHAT_PROVIDER", "cerebras"))
_fallback_name = ENV.get("CHAT_FALLBACK_PROVIDER", "").strip()
FALLBACK = Provider(_fallback_name) if _fallback_name else None


# ─────────────────────────────────────────────────────────────
# LIVE SCHEMA INTROSPECTION
# ─────────────────────────────────────────────────────────────

# Tables the assistant should never be told about or query. chat_history is
# excluded so the assistant can't be tricked into "reading its own memory"
# as if it were business data.
HIDDEN_TABLES = {"sqlite_sequence", "chat_history"}

# Extra human context for tables whose meaning isn't obvious from column
# names alone. Anything not listed still gets its columns introspected.
TABLE_NOTES = {
    "callyzer_calls": "One row per phone call. connected=1 means duration_seconds > 45. "
                      "direction is 'incoming' or 'outgoing'. rep_sim_number is the most "
                      "reliable way to identify a rep (names have spelling variants).",
    "callyzer_leads": "One row per lead, reflecting its CURRENT state, not history. "
                      "tags may contain 'IndiaMart Lead' or 'Gold Lead'. assigned_to is a rep name.",
    "shopify_orders": "One row per Shopify order. rep_attribution may be NULL — salesperson "
                      "attribution is not reliably populated, so don't treat NULL as 'no rep'.",
    "shopify_customers": "One row per Shopify customer. Not every order has a linked customer "
                         "(guest checkouts), so this table is smaller than shopify_orders.",
    "v_calls_with_orders": "VIEW joining calls to orders on phone number.",
    "v_unmatched_orders": "VIEW of Shopify orders with no matching call by phone number.",
    "ingestion_log": "Metadata about each ingestion run — useful for 'is the data up to date'.",
    "ingestion_flags": "Rows rejected during ingestion, with a reason. Useful for data-quality questions.",
}


def build_schema_description():
    """Introspect the real database instead of hardcoding a schema string.

    This is the single most important defence against this file going stale:
    add a column to schema.sql and the assistant sees it on the next question,
    with no edit here."""
    conn = get_connection()
    try:
        objects = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type DESC, name"
        ).fetchall()

        lines = ["Tables and views available (SQLite):", ""]
        for obj in objects:
            name = obj["name"]
            if name in HIDDEN_TABLES:
                continue
            cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({name})").fetchall()]
            lines.append(f"{name} ({', '.join(cols)})")
            if name in TABLE_NOTES:
                lines.append(f"  - {TABLE_NOTES[name]}")
            lines.append("")
    finally:
        conn.close()

    lines.append(
        "Phone numbers are normalized to bare 10-digit strings in every *_norm "
        "column — always join and filter on the *_norm columns, never the *_raw ones."
    )
    lines.append(
        "For relative dates ('today', 'yesterday', 'this week', 'last 3 days'), use "
        "SQLite date functions such as date('now'), date('now','-1 day'), "
        "date('now','-7 days'). Do NOT write a hardcoded date literal for these — "
        "you do not reliably know today's date, but SQLite does."
    )
    lines.append(
        "call_timestamp is ISO 8601 text, so date(call_timestamp) gives its date. "
        "Prefer COUNT(*) / SUM() / AVG() in SQL over returning many rows."
    )
    return "\n".join(lines)


def load_business_rules():
    """MasonMart's compliance rules change periodically (thresholds changed in
    July 2026, lead tags renamed, etc). They live in an editable text file so
    a rule change never requires touching this script — which is exactly the
    separation the PRD asked for: the integration layer stays rule-agnostic."""
    path = os.path.join(SCRIPT_DIR, "business_rules.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return ""
    return (
        "\n\nMasonMart-specific business context (supplied by the product owner; "
        "use it to interpret questions, but never to invent numbers):\n" + text
    )


# ─────────────────────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────────────────────

def build_sql_system_prompt():
    return f"""You are a careful SQL generator for MasonMart's Callyzer + Shopify data store.

{build_schema_description()}{load_business_rules()}

You may be shown recent prior questions and answers. Use them ONLY to resolve
follow-ups that clearly refer back to something just discussed ("and what about
Suman?", "same thing for last month"). Otherwise treat each question on its own
merits, and never let stale context distort a genuinely new question.

Respond with ONLY a JSON object and no other text:
{{"sql": "SELECT ...", "explanation": "one sentence on what this measures"}}

Rules:
- Exactly one read-only SELECT (or WITH ... SELECT). Never INSERT/UPDATE/DELETE/
  DROP/ALTER/PRAGMA/ATTACH. Never more than one statement.
- Join and filter on the *_norm phone columns.
- The SQL must run as-is: never use parameter placeholders (?, :name, @name).
  Embed concrete values the person actually gave you as literals.
- If the question depends on a specific name, number, or date that the person
  did NOT supply (e.g. "has this customer ordered?" with no number given), do
  not invent one. Return {{"sql": null, "explanation": "ask them for the specific
  name/number/date needed"}}.
- Prefer aggregates over returning large row sets. If listing rows, add a
  sensible LIMIT.
- Rep names have spelling variants; prefer matching with LIKE and/or lower()
  rather than strict equality when filtering on a person's name.
- If the question cannot be answered from this schema at all, return
  {{"sql": null, "explanation": "why not"}}.
"""


ANSWER_SYSTEM_PROMPT = """You turn a SQL query's result into a short, plain-language
answer, in the voice of a direct colleague giving an update. No headers. No
bullet-point reformatting unless the person asked for a list.

Hard rules:
- State numbers EXACTLY as returned. Never round, estimate, extrapolate, or
  infer a number that is not in the result.
- If the result is empty, say so plainly. Do not speculate about why.
- You will be given "data_freshness" containing todays_actual_date (the real
  current date — trust it absolutely over any assumption), and the latest dates
  the database actually holds call and order data for.
- CRITICAL: if the question concerns a date at or after the latest data date and
  the result is zero/empty, that means THE DATA HAS NOT BEEN LOADED YET, not that
  zero activity occurred. Say that plainly and name the most recent date that does
  have data. Only report a confirmed zero for dates safely before the data cutoff.
- If "truncated_rows" is present, say how many rows matched in total and that you
  are only describing the first few.
"""


# ─────────────────────────────────────────────────────────────
# SAFE SQL EXECUTION
# ─────────────────────────────────────────────────────────────

def _strip_string_literals(sql):
    """Remove quoted literals before scanning for placeholders, so a legitimate
    time literal ('10:30') or email ('a@b.com') is not mistaken for a bound
    parameter. This was a real false-positive bug in an earlier version."""
    return re.sub(r"'[^']*'", "''", re.sub(r'"[^"]*"', '""', sql))


def has_placeholder(sql):
    stripped = _strip_string_literals(sql)
    return "?" in stripped or bool(re.search(r"[:@$]\w+", stripped))


def _authorizer(action, arg1, arg2, db_name, trigger):
    """Engine-level read-only enforcement. Unlike keyword matching, this cannot
    be fooled by a value that happens to contain the word 'delete', and cannot
    be bypassed by clever SQL."""
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def open_readonly_connection():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_query_safely(sql):
    """Execute SQL under three independent guards: a read-only file handle, an
    authorizer callback, and a wall-clock abort. Returns (rows, total_matched).

    Raises sqlite3.Error on a bad query, so the caller can feed the error text
    back to the model for self-correction."""
    conn = open_readonly_connection()
    deadline = time.time() + QUERY_TIMEOUT_SECONDS
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    conn.set_authorizer(_authorizer)
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchmany(MAX_RESULT_ROWS)]
        # Determine whether more rows existed beyond the cap, without
        # materialising them.
        extra = cur.fetchone()
        truncated = extra is not None
        return rows, truncated
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        conn.close()


# ─────────────────────────────────────────────────────────────
# LLM TRANSPORT
# ─────────────────────────────────────────────────────────────

class ContextTooLargeError(Exception):
    pass


class ProviderError(Exception):
    pass


def _post_chat(provider, system, messages, json_mode, max_tokens):
    payload = {
        "model": provider.model,
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0,  # deterministic: same question -> same SQL
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(API_MAX_RETRIES):
        try:
            resp = requests.post(
                provider.url,
                headers={
                    "Authorization": f"Bearer {provider.key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60,
            )
        except requests.RequestException as e:
            last_error = f"network error: {e}"
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        body = resp.text.lower()
        if resp.status_code == 400 and (
            "reduce the length" in body or "context length" in body or "too many tokens" in body
        ):
            raise ContextTooLargeError(resp.text)

        # 429 = rate limited, 5xx = transient. Both are worth retrying with
        # backoff; Cerebras and Groq both send Retry-After on 429.
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("retry-after", 2 ** attempt))
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logging.warning("%s transient error, retrying in %.1fs: %s",
                            provider.name, wait, last_error)
            time.sleep(min(wait, 30))
            continue

        raise ProviderError(
            f"{provider.name} API error ({resp.status_code}): {resp.text}\n"
            f"If this mentions an invalid key, check {provider.key_env} in .env "
            f"against {provider.signup}.\n"
            f"If it mentions an unknown model, that model may have been retired — "
            f"set the model name in .env to a currently available one."
        )

    raise ProviderError(f"{provider.name} unreachable after {API_MAX_RETRIES} attempts. {last_error}")


def call_llm(system, messages, json_mode=False, max_tokens=1200):
    """Call the primary provider, falling back to the secondary one if the
    primary is unreachable or rate-limited to exhaustion. Fallback is optional
    and only used if CHAT_FALLBACK_PROVIDER is set."""
    try:
        return _post_chat(PRIMARY, system, messages, json_mode, max_tokens)
    except ContextTooLargeError:
        raise
    except ProviderError as e:
        if FALLBACK and FALLBACK.configured:
            logging.warning("Primary provider %s failed, using fallback %s: %s",
                            PRIMARY.name, FALLBACK.name, e)
            return _post_chat(FALLBACK, system, messages, json_mode, max_tokens)
        raise


# ─────────────────────────────────────────────────────────────
# CONVERSATION MEMORY
# ─────────────────────────────────────────────────────────────

def get_recent_history(limit=HISTORY_TURNS_TO_INCLUDE):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT question, answer FROM chat_history ORDER BY turn_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [{"question": r["question"], "answer": r["answer"]} for r in reversed(rows)]


def save_turn(question, sql, answer):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO chat_history (question, sql_used, answer, asked_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (question, sql, answer),
        )
        conn.commit()
    finally:
        conn.close()


def _history_answer_for_context(answer):
    """Replaying verbose error text into every later prompt wastes the token
    budget; the model only needs to know that turn produced nothing."""
    if answer.startswith("("):
        return "(That question could not be answered — no data was returned.)"
    return answer


def get_data_freshness():
    """The real current date plus the latest dates the database actually covers.

    The model has no inherent knowledge of today's date. Without being told, it
    will confidently misread yesterday's data as today's — which it did, before
    todays_actual_date was added here."""
    conn = get_connection()
    try:
        latest_call = conn.execute("SELECT MAX(call_timestamp) FROM callyzer_calls").fetchone()[0]
        latest_order = conn.execute("SELECT MAX(created_at) FROM shopify_orders").fetchone()[0]
    finally:
        conn.close()
    return {
        "todays_actual_date": date.today().isoformat(),
        "latest_call_data_through": latest_call,
        "latest_order_data_through": latest_order,
    }


# ─────────────────────────────────────────────────────────────
# THE ASK LOOP
# ─────────────────────────────────────────────────────────────

def _extract_json(raw):
    """Models occasionally wrap JSON in prose or code fences despite being told
    not to. Recover the object rather than failing the whole question."""
    raw = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        brace = re.search(r"\{.*\}", raw, re.S)
        if brace:
            try:
                return json.loads(brace.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _generate_sql(question, history, correction=None):
    """Ask the model for SQL, shrinking history if the request is too large."""
    system = build_sql_system_prompt()

    def build(hist):
        msgs = []
        for turn in hist:
            msgs.append({"role": "user", "content": turn["question"]})
            msgs.append({"role": "assistant", "content": _history_answer_for_context(turn["answer"])})
        msgs.append({"role": "user", "content": question})
        if correction:
            msgs.append({"role": "assistant", "content": json.dumps({"sql": correction["sql"]})})
            msgs.append({
                "role": "user",
                "content": (
                    f"That query failed with this SQLite error:\n{correction['error']}\n\n"
                    f"Rewrite it so it runs correctly against the schema above. "
                    f"Respond with the same JSON format."
                ),
            })
        return msgs

    # Degrade gracefully instead of failing when the prompt is too big.
    for hist_slice in (history, history[len(history) // 2:], history[-2:], []):
        try:
            return call_llm(system, build(hist_slice), json_mode=True)
        except ContextTooLargeError:
            continue
    return None


def ask(question):
    question = (question or "").strip()
    if not question:
        return "Ask me something about the calls, leads, or orders data."

    history = get_recent_history()
    correction = None
    sql = None

    # Self-correction loop: a failed query is fed back with its error so the
    # model can fix it, rather than surfacing a raw SQLite error to the person.
    for attempt in range(MAX_SQL_ATTEMPTS):
        raw = _generate_sql(question, history, correction)
        if raw is None:
            answer = ("(That question was too long for the model to process, even after "
                      "trimming earlier conversation. Try asking it more briefly.)")
            save_turn(question, None, answer)
            return answer

        parsed = _extract_json(raw)
        if parsed is None:
            logging.error("Unparseable model output: %s", raw[:500])
            answer = "(The model returned something I couldn't read as a query. Try rephrasing.)"
            save_turn(question, None, answer)
            return answer

        sql = parsed.get("sql")
        if not sql:
            answer = parsed.get("explanation") or "That can't be answered from the current data."
            save_turn(question, None, answer)
            return answer

        if has_placeholder(sql):
            answer = ("That question needs a specific name, number, or date, but none was "
                      "given. Try including the exact value — for example \"has 9876543210 "
                      "ordered before?\" rather than \"has this customer ordered before?\".")
            save_turn(question, sql, answer)
            return answer

        try:
            rows, truncated = run_query_safely(sql)
            break
        except sqlite3.Error as e:
            logging.warning("SQL attempt %d failed: %s | %s", attempt + 1, e, sql)
            correction = {"sql": sql, "error": str(e)}
            if attempt == MAX_SQL_ATTEMPTS - 1:
                answer = (f"I couldn't build a working query for that after "
                          f"{MAX_SQL_ATTEMPTS} attempts. The last error was: {e}")
                save_turn(question, sql, answer)
                return answer

    payload = {
        "question": question,
        "sql": sql,
        "result_rows": rows[:MAX_ROWS_TO_SUMMARIZE],
        "data_freshness": get_data_freshness(),
    }
    if truncated:
        payload["truncated_rows"] = (
            f"more than {MAX_RESULT_ROWS} rows matched; only the first "
            f"{min(len(rows), MAX_ROWS_TO_SUMMARIZE)} are shown"
        )
    elif len(rows) > MAX_ROWS_TO_SUMMARIZE:
        payload["truncated_rows"] = (
            f"{len(rows)} rows matched; only the first {MAX_ROWS_TO_SUMMARIZE} are shown"
        )

    try:
        answer = call_llm(ANSWER_SYSTEM_PROMPT,
                          [{"role": "user", "content": json.dumps(payload, default=str)}],
                          max_tokens=800)
    except ContextTooLargeError:
        payload["result_rows"] = rows[:5]
        payload["truncated_rows"] = f"{len(rows)} rows matched; only the first 5 are shown"
        try:
            answer = call_llm(ANSWER_SYSTEM_PROMPT,
                              [{"role": "user", "content": json.dumps(payload, default=str)}],
                              max_tokens=800)
        except ContextTooLargeError:
            answer = (f"That query matched {len(rows)} rows — too many to summarize. "
                      f"Try narrowing it with a date range or a specific person.")

    save_turn(question, sql, answer)
    return answer


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def _preflight():
    if not PRIMARY.configured:
        print(f"ERROR: {PRIMARY.key_env} is not set in .env.")
        print(f"Get a free key from {PRIMARY.signup}, then add it to .env.")
        sys.exit(1)
    if not os.path.exists(DB_PATH):
        print(f"ERROR: no database at {DB_PATH}.")
        print("Run ingest_callyzer.py and/or ingest_shopify.py first.")
        sys.exit(1)


def main():
    _preflight()

    if len(sys.argv) > 1:
        print(ask(" ".join(sys.argv[1:])))
        return

    print(f"MasonMart data chat  ({PRIMARY.name}: {PRIMARY.model})")
    print("Type a question, or 'quit' to exit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in ("quit", "exit"):
            break
        if not question:
            continue
        try:
            print(ask(question))
        except ProviderError as e:
            print(f"\n{e}")


if __name__ == "__main__":
    main()
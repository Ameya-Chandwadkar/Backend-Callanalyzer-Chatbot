"""
chat_query.py
Phase 3 of the PRD: the chat/LLM interface over the unified data store.
Uses Groq's API (OpenAI-compatible) as the LLM provider — genuinely free
for this workload, no credit card, no data-sharing trade-off.

ARCHITECTURE (per PRD 5.4 / NFR "Accuracy"):
The LLM NEVER computes numbers itself and never sees raw rows beyond
what a query returns. Every question goes through this loop:

    1. The model reads your question + the DB schema, and writes ONE
       read-only SQL query to answer it.
    2. This script runs that exact SQL against masonmart.sqlite.
    3. The model sees only the query's actual output and turns it into a
       plain-language answer.

The arithmetic is always done by SQLite, not guessed by the model. Step 2
also enforces SELECT-only — the model is never allowed to write, update,
or delete anything, so this stays a read-only reporting layer no matter
what's asked. Swapping the LLM provider (this file) never requires
touching schema.sql, the ingestion scripts, or the database itself.

SETUP:
    1. pip install requests
    2. Sign up at console.groq.com (email or Google/GitHub login, no
       credit card needed), then go to API Keys -> Create API Key.
    3. In .env, add: GROQ_API_KEY=gsk_...
    4. Optionally set GROQ_MODEL in .env (defaults to
       llama-3.3-70b-versatile — strong enough for structured SQL
       generation and plain-language summarizing, and fast on Groq's
       hardware).
    5. python chat_query.py "how many IndiaMart leads has Sara not called in 3+ days?"
       or run with no arguments for an interactive prompt loop.

COST NOTE: Groq's free tier (30 requests/min, 14,400 requests/day, no
card required) comfortably covers this workload — each question makes
two calls to the API, so even a heavy day of 100 questions is a fraction
of the daily allowance. No running cost expected at this volume.
"""

import os
import sys
import json
import re
import sqlite3

from common import get_connection, SCRIPT_DIR

try:
    import requests
except ImportError:
    print("ERROR: 'requests' module not found. Run: pip install requests")
    sys.exit(1)


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
API_KEY = ENV.get("GROQ_API_KEY")
MODEL = ENV.get("GROQ_MODEL", "llama-3.3-70b-versatile")

SCHEMA_DESCRIPTION = """
Tables available (SQLite):

callyzer_calls (call_id, call_timestamp, direction, duration_seconds, connected,
    rep_name, rep_sim_number, customer_number_raw, customer_number_norm, source_file)
  - one row per phone call. connected=1 means duration_seconds > 45.

callyzer_leads (lead_no, lead_name, contact_number_raw, contact_number_norm,
    assigned_to, tags, no_of_attempts, last_call_datetime, last_call_note,
    lead_status, created_date)
  - one row per lead, current state (not history). tags contains things
    like 'IndiaMart Lead'. assigned_to is the rep's name.

shopify_orders (order_id, order_number, created_at, total_price, financial_status,
    customer_phone_raw, customer_phone_norm, customer_name, rep_attribution)
  - one row per Shopify order.

shopify_customers (customer_id, phone_raw, phone_norm, name, email)

v_calls_with_orders — VIEW joining calls to orders by phone number (customer_number_norm = customer_phone_norm)
v_unmatched_orders — VIEW of Shopify orders with no matching call by phone number

ingestion_log / ingestion_flags — metadata about each ingestion run, useful for
  "is the data up to date" or "what got flagged" questions.

Phone numbers are normalized to bare 10-digit strings in every *_norm column —
always join and filter on the *_norm columns, not the *_raw ones.
Today's date for relative questions ("last 3 days") should be taken as the
current date on the machine running this script.
"""

SYSTEM_PROMPT = f"""You are a SQL generator for MasonMart's Callyzer + Shopify data store.

{SCHEMA_DESCRIPTION}

Given a question, respond with ONLY a JSON object, no other text:
{{"sql": "SELECT ...", "explanation": "one sentence on what this measures"}}

Rules:
- SQL must be a single read-only SELECT statement (or a WITH ... SELECT).
- Never write INSERT, UPDATE, DELETE, DROP, ALTER, or PRAGMA statements.
- Use the *_norm phone columns for any join or match.
- The SQL must be fully self-contained and runnable as-is: never use
  parameter placeholders like ?, :name, or @value. If the question
  refers to a specific person, phone number, date, or other value
  without actually stating it (e.g. "a customer named X" used as a
  placeholder, or "that number" with no number given), do NOT guess or
  invent one — instead return {{"sql": null, "explanation": "ask the
  person for the specific name/number/date needed"}}. If the question
  does give a concrete value, embed it directly as a literal in the SQL.
- If the question cannot be answered from this schema, return
  {{"sql": null, "explanation": "why not"}}.
"""

ANSWER_SYSTEM_PROMPT = """You turn a SQL query's result into a short, plain-language
answer to the original question, in the style of a direct colleague update —
no headers, no bullet-point reformatting of the raw data unless the question
asked for a list. State the numbers exactly as returned; do not round or
estimate. If the result set is empty, say so plainly rather than guessing
a reason."""


def call_groq(system, user_content, json_mode=False):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Groq API error ({resp.status_code}): {resp.text}\n"
            f"If this mentions an invalid API key, check GROQ_API_KEY in .env "
            f"matches the value from console.groq.com -> API Keys exactly."
        )
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def has_placeholder(sql):
    return "?" in sql or bool(re.search(r"[:@]\w+", sql))


def is_read_only_select(sql):
    lowered = sql.strip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False
    forbidden = ["insert", "update", "delete", "drop", "alter", "attach", "pragma", ";--"]
    return not any(word in lowered for word in forbidden)


def ask(question):
    raw = call_groq(SYSTEM_PROMPT, question, json_mode=True)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return f"(Could not parse the SQL-generation response. Raw output: {raw})"

    sql = parsed.get("sql")
    if not sql:
        return parsed.get("explanation", "This can't be answered from the current data store.")

    if has_placeholder(sql):
        return ("(This question needs a specific name, number, or date to answer, "
                "but none was given — try asking again with the exact value, e.g. "
                "\"has 9876543210 ordered before\" instead of \"has this customer "
                "ordered before\". No query was run.)")

    if not is_read_only_select(sql):
        return "(Refused: generated SQL was not a plain read-only SELECT. No query was run.)"

    conn = get_connection()
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as e:
        return f"(SQL error running the generated query: {e}\nQuery was: {sql})"
    finally:
        conn.close()

    result_payload = json.dumps({"question": question, "sql": sql, "result_rows": rows}, default=str)
    answer = call_groq(ANSWER_SYSTEM_PROMPT, result_payload)
    return answer


def main():
    if not API_KEY:
        print("ERROR: GROQ_API_KEY not set in .env. See README.md.")
        sys.exit(1)

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(ask(question))
        return

    print("MasonMart data chat. Type a question, or 'quit' to exit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in ("quit", "exit"):
            break
        if not question:
            continue
        print(ask(question))


if __name__ == "__main__":
    main()
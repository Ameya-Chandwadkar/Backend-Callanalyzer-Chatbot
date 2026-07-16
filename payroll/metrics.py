"""
metrics.py
The computation engine behind the Combined Performance & Salary report.

DESIGN RULE (the whole point of this file): every function returns a value
that traces back to specific rows in callyzer_calls / callyzer_never_attended
/ shopify_orders / customer_salesperson_map, plus a plain-English "how" string
that the report prints verbatim. Nothing is estimated, and nothing that can't
be computed from real data is invented — the caller marks those as gaps.

Two things this file deliberately does NOT do:
  1. It does not reproduce the June audit's composite discipline SCORES
     ("0/20", "0.44/5"). That spreadsheet never states the formula behind
     those numbers, so reproducing them would be guessing. Instead we return
     the underlying verifiable facts (e.g. "first call after 09:10 on 6 of 8
     rating days"), which are auditable.
  2. It does not compute Never-Attended callback recovery when the missed-call
     data and the call history are from different periods (they currently are:
     missed = June, calls = July). Detecting a callback needs same-period call
     data. The caller surfaces this as a gap rather than printing a misleading
     recovery number.
"""

from datetime import datetime, time


# ── Working-day helpers ──────────────────────────────────────

def _excluded_weekday_nums(excluded_names):
    weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
                   "Friday": 4, "Saturday": 5, "Sunday": 6}
    return {weekday_map[w] for w in excluded_names if w in weekday_map}


def report_period(conn, config):
    """(start, end, explicit) — explicit=False means we fell back to the
    actual span of call data on file because the config named no period."""
    p = config.get("period", {})
    start, end = p.get("start_date"), p.get("end_date")
    if start and end:
        return start, end, True
    row = conn.execute(
        "SELECT MIN(date(call_timestamp)) mn, MAX(date(call_timestamp)) mx FROM callyzer_calls"
    ).fetchone()
    return row["mn"], row["mx"], False


def working_day_count(start, end, excluded_names):
    from datetime import date, timedelta
    excluded = _excluded_weekday_nums(excluded_names)
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    n = 0
    d = d0
    while d <= d1:
        if d.weekday() not in excluded:
            n += 1
        d += timedelta(days=1)
    return n


def _is_excluded_day(date_str, excluded_names):
    """True if this YYYY-MM-DD falls on an excluded weekday (e.g. Sunday).
    Used so a non-working day is never counted as an under-target working
    day (LOP risk) or judged for discipline."""
    excluded = _excluded_weekday_nums(excluded_names)
    return datetime.strptime(date_str, "%Y-%m-%d").date().weekday() in excluded


# ── Core call metrics ────────────────────────────────────────

def call_totals(conn, sim, start, end, excluded_names=()):
    """Headline volume/quality counts, straight from callyzer_calls.

    active_days counts WORKING days only (excluded weekdays removed), matching
    the June audit's 'Active = any-activity working days'. Without this a rep
    who happened to make a call on a Sunday would show one extra active day.
    connected_45s / talk_seconds cover ALL calls (incoming + outgoing), which
    is how the June audit defined them — the report states this explicitly so
    it isn't a hidden assumption."""
    # SQLite strftime('%w'): 0=Sunday..6=Saturday
    wmap = {"Sunday": "0", "Monday": "1", "Tuesday": "2", "Wednesday": "3",
            "Thursday": "4", "Friday": "5", "Saturday": "6"}
    excl = [wmap[w] for w in excluded_names if w in wmap]
    excl_clause = ""
    if excl:
        in_list = ",".join(f"'{d}'" for d in excl)
        excl_clause = f"AND strftime('%w', call_timestamp) NOT IN ({in_list})"
    row = conn.execute(
        f"""SELECT
             SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) AS outgoing,
             SUM(CASE WHEN direction='incoming' THEN 1 ELSE 0 END) AS incoming,
             SUM(CASE WHEN COALESCE(duration_seconds,0) > 45 THEN 1 ELSE 0 END) AS connected_45s,
             SUM(CASE WHEN COALESCE(duration_seconds,0) > 0 THEN 1 ELSE 0 END) AS connected_any,
             COALESCE(SUM(COALESCE(duration_seconds,0)),0) AS talk_seconds,
             COUNT(DISTINCT CASE WHEN 1=1 {excl_clause} THEN date(call_timestamp) END) AS active_days,
             COUNT(*) AS total
           FROM callyzer_calls
           WHERE rep_sim_number = ? AND date(call_timestamp) BETWEEN ? AND ?""",
        (sim, start, end),
    ).fetchone()
    return dict(row)


def per_day_outgoing(conn, sim, start, end):
    """[(date, outgoing_attempts, first_outgoing_time_str)] for each active day."""
    rows = conn.execute(
        """SELECT date(call_timestamp) d,
                  SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) outgoing,
                  MIN(CASE WHEN direction='outgoing' THEN time(call_timestamp) END) first_out
           FROM callyzer_calls
           WHERE rep_sim_number = ? AND date(call_timestamp) BETWEEN ? AND ?
           GROUP BY d ORDER BY d""",
        (sim, start, end),
    ).fetchall()
    return [(r["d"], r["outgoing"] or 0, r["first_out"]) for r in rows]


def rating_days(conn, sim, start, end, min_attempts, excluded_names=()):
    """Working days (excluded weekdays removed) with >= min_attempts outgoing
    calls — the June audit's 'rating day = 50+ attempts, non-Sunday'."""
    days = per_day_outgoing(conn, sim, start, end)
    return [(d, n, f) for (d, n, f) in days
            if n >= min_attempts and not _is_excluded_day(d, excluded_names)]


def sub_threshold_days(conn, sim, start, end, threshold, excluded_names=()):
    """LOP-risk evidence: active WORKING days below the attempts threshold,
    with the shortfall. Excluded weekdays (Sundays) are never counted — being
    below target on a non-working day is not a shortfall. 'Info only' in the
    report, never auto-deducted."""
    days = per_day_outgoing(conn, sim, start, end)
    return [(d, n, threshold - n) for (d, n, f) in days
            if n < threshold and not _is_excluded_day(d, excluded_names)]


# ── Discipline (raw facts, not invented scores) ──────────────

def first_call_discipline(conn, sim, start, end, deadline_str, min_attempts, excluded_names=()):
    """On each RATING day, was the first outgoing call after the deadline?
    Returns dict with counts + the offending day list. Only rating days are
    judged (matches the June audit scoping discipline to real working days)."""
    deadline = datetime.strptime(deadline_str, "%H:%M").time()
    rdays = rating_days(conn, sim, start, end, min_attempts, excluded_names)
    late = []
    on_time = 0
    for (d, n, first_out) in rdays:
        if first_out is None:
            continue
        t = datetime.strptime(first_out, "%H:%M:%S").time()
        if t > deadline:
            late.append((d, first_out))
        else:
            on_time += 1
    return {
        "rating_days": len(rdays), "on_time": on_time, "late": len(late),
        "late_days": late, "deadline": deadline_str,
    }


def morning_window_discipline(conn, sim, start, end, window, min_attempts, excluded_names=()):
    """Per RATING day: did the rep hit the 9-11 benchmark — either
    (min_outgoing_attempts outgoing AND min_connected_calls connected>45s) in
    the window, OR min_talk_minutes of talk time in the window?"""
    w_start, w_end = window["start"], window["end"]
    min_out = window["min_outgoing_attempts"]
    min_conn = window["min_connected_calls"]
    min_talk_sec = window["min_talk_minutes"] * 60
    rdays = rating_days(conn, sim, start, end, min_attempts, excluded_names)
    passed = 0
    failed_days = []
    for (d, n, f) in rdays:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) out_n,
                 SUM(CASE WHEN COALESCE(duration_seconds,0) > 45 THEN 1 ELSE 0 END) conn_n,
                 COALESCE(SUM(COALESCE(duration_seconds,0)),0) talk_sec
               FROM callyzer_calls
               WHERE rep_sim_number = ? AND date(call_timestamp) = ?
                 AND time(call_timestamp) >= ? AND time(call_timestamp) < ?""",
            (sim, d, w_start + ":00", w_end + ":00"),
        ).fetchone()
        ok = ((row["out_n"] or 0) >= min_out and (row["conn_n"] or 0) >= min_conn) \
            or ((row["talk_sec"] or 0) >= min_talk_sec)
        if ok:
            passed += 1
        else:
            failed_days.append(d)
    return {
        "rating_days": len(rdays), "passed": passed, "failed": len(failed_days),
        "failed_days": failed_days, "window": f"{w_start}-{w_end}",
    }


def hourly_discipline(conn, sim, start, end, rule, min_attempts, excluded_names=()):
    """Eligible hour = a clock-hour on a RATING day in which the rep placed at
    least one outgoing call. An eligible hour 'meets the bar' if it has
    >= min_calls_per_hour outgoing calls OR >= min_talk_minutes_per_hour talk.
    NB: 'eligible hour' is defined explicitly here — the June audit used a
    similar but unstated definition, so this is reported as our own computed
    fact, not a claim to reproduce that sheet's exact number."""
    min_calls = rule["min_calls_per_hour"]
    min_talk_sec = rule["min_talk_minutes_per_hour"] * 60
    rday_dates = {d for (d, n, f) in rating_days(conn, sim, start, end, min_attempts, excluded_names)}
    if not rday_dates:
        return {"eligible_hours": 0, "met": 0, "rating_days": 0}
    placeholders = ",".join("?" * len(rday_dates))
    rows = conn.execute(
        f"""SELECT date(call_timestamp) d, strftime('%H', call_timestamp) hr,
                   SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) calls,
                   COALESCE(SUM(COALESCE(duration_seconds,0)),0) talk_sec
            FROM callyzer_calls
            WHERE rep_sim_number = ? AND date(call_timestamp) IN ({placeholders})
            GROUP BY d, hr
            HAVING calls >= 1""",
        (sim, *rday_dates),
    ).fetchall()
    eligible = len(rows)
    met = sum(1 for r in rows if (r["calls"] or 0) >= min_calls or (r["talk_sec"] or 0) >= min_talk_sec)
    return {"eligible_hours": eligible, "met": met, "rating_days": len(rday_dates)}


# ── Never Attended (period-aware) ────────────────────────────

def never_attended_summary(conn, sim, call_start, call_end):
    """Raw missed-call count for this rep, plus whether the missed-call data
    overlaps the call-history period. If it doesn't overlap, callback recovery
    is NOT computed (can't detect callbacks without same-period call data) —
    the caller shows that as a gap rather than a misleading 'final' number."""
    row = conn.execute(
        """SELECT COUNT(*) n, MIN(date(call_timestamp)) mn, MAX(date(call_timestamp)) mx
           FROM callyzer_never_attended WHERE rep_sim_number = ?""",
        (sim,),
    ).fetchone()
    if not row["n"]:
        return {"missed": 0, "period": None, "overlaps_calls": False, "recoverable": False}
    overlaps = not (row["mx"] < call_start or row["mn"] > call_end)
    result = {"missed": row["n"], "period": (row["mn"], row["mx"]),
              "overlaps_calls": overlaps, "recoverable": overlaps}
    if overlaps:
        # Only meaningful when periods overlap — callback = a later outgoing
        # call from the same rep to the same number.
        recovered = conn.execute(
            "SELECT SUM(callback_attempted) FROM v_never_attended_final WHERE rep_sim_number = ?",
            (sim,),
        ).fetchone()[0] or 0
        result["recovered"] = recovered
        result["final_never_attended"] = row["n"] - recovered
    return result


# ── Order incentives (from the customer→rep map) ─────────────

def order_incentives(conn, config, start, end):
    """Per mapped customer, walk their orders in sequence and apply the tiered
    incentive.

    TWO DIFFERENT DATE SCOPES, deliberately — getting this wrong overpays real
    money, so it's spelled out:
      * SEQUENCE / CUMULATIVE use the customer's FULL lifetime order history,
        because the tier (1st/2nd/3rd) is a lifetime position, not a
        within-period one.
      * The incentive is only AWARDED for orders actually PLACED inside the
        report period. An order from a previous month belongs to that month's
        payroll and must not be paid again here.
    Orders outside the period still appear in the detail (so the sequence is
    auditable) but award 0 and are flagged in_period=False.

    Order dates are converted UTC->IST before comparing (shopify created_at is
    'Z', the report period is IST) — same rule as everywhere else in this project.
    """
    tiers = config["order_incentive_tiers"]
    qual = config["order_incentive_qualification"]
    rows = conn.execute(
        """SELECT m.canonical_name, m.rep_sim_number, m.customer_phone_norm, m.source,
                  o.order_number, o.created_at,
                  date(o.created_at, 'localtime') AS order_date_ist,
                  o.total_price
           FROM customer_salesperson_map m
           JOIN shopify_orders o ON o.customer_phone_norm = m.customer_phone_norm
           ORDER BY m.customer_phone_norm, o.created_at"""
    ).fetchall()
    by_customer = {}
    for r in rows:
        by_customer.setdefault(r["customer_phone_norm"], []).append(r)

    detail = []
    per_rep = {}
    sources = set()
    for phone, orders in by_customer.items():
        cumulative = 0.0
        for i, o in enumerate(orders, start=1):
            cumulative += o["total_price"] or 0
            seq = {1: "1st", 2: "2nd", 3: "3rd"}.get(i, f"{i}th")
            tier_amount = tiers.get(seq, 0) if i <= 3 else 0
            in_period = start <= o["order_date_ist"] <= end
            awarded = tier_amount if in_period else 0
            earned = i >= qual["min_order_sequence"] and cumulative >= qual["min_order_value"]
            detail.append({
                "rep": o["canonical_name"], "rep_sim": o["rep_sim_number"],
                "customer_phone": phone, "order": o["order_number"], "sequence": seq,
                "order_date": o["order_date_ist"], "in_period": in_period,
                "value": o["total_price"] or 0, "cumulative": cumulative,
                "tier_amount": tier_amount, "incentive": awarded,
                "earned": earned, "source": o["source"],
            })
            if awarded:
                per_rep[o["rep_sim_number"]] = per_rep.get(o["rep_sim_number"], 0.0) + awarded
            sources.add(o["source"])
    return detail, per_rep, sources


def qualified_customer_count(conn, config, rep_sim):
    """Customers of this rep who reached the qualification threshold
    (>= min_order_sequence orders AND cumulative >= min_order_value) — feeds
    the target bonus test."""
    qual = config["order_incentive_qualification"]
    rows = conn.execute(
        """SELECT m.customer_phone_norm, COUNT(o.order_id) n, COALESCE(SUM(o.total_price),0) total
           FROM customer_salesperson_map m
           JOIN shopify_orders o ON o.customer_phone_norm = m.customer_phone_norm
           WHERE m.rep_sim_number = ?
           GROUP BY m.customer_phone_norm""",
        (rep_sim,),
    ).fetchall()
    return sum(1 for r in rows
               if r["n"] >= qual["min_order_sequence"] and r["total"] >= qual["min_order_value"])


def fmt_talk(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60}m"


def data_coverage(conn, start, end):
    """The actual span + row count of every source feeding the report, so the
    reader can see exactly what data the numbers rest on (and where a source's
    period does NOT line up with the report period). Returns a list of dicts
    with: source, rows, date_from, date_to, aligns (bool or None), note."""
    def rng(sql):
        r = conn.execute(sql).fetchone()
        return r["n"], r["mn"], r["mx"]

    cov = []

    n, mn, mx = rng("SELECT COUNT(*) n, MIN(date(call_timestamp)) mn, MAX(date(call_timestamp)) mx FROM callyzer_calls")
    cov.append({"source": "Call History (Periodic Call History)", "rows": n, "date_from": mn, "date_to": mx,
                "aligns": True, "note": "This IS the report period — all performance metrics are built from it."})

    n, mn, mx = rng("SELECT COUNT(*) n, MIN(date(call_timestamp)) mn, MAX(date(call_timestamp)) mx FROM callyzer_never_attended")
    if n:
        aligns = not (mx < start or mn > end)
        cov.append({"source": "Never Attended Report", "rows": n, "date_from": mn, "date_to": mx,
                    "aligns": aligns,
                    "note": ("Overlaps the call period — callback recovery computed." if aligns
                             else "DIFFERENT PERIOD than the call history — only raw missed counts used, "
                                  "callback recovery NOT computed.")})
    else:
        cov.append({"source": "Never Attended Report", "rows": 0, "date_from": None, "date_to": None,
                    "aligns": None, "note": "Not uploaded."})

    n, mn, mx = rng("SELECT COUNT(*) n, MIN(date(created_date)) mn, MAX(date(created_date)) mx FROM callyzer_leads")
    cov.append({"source": "Lead Data Report (leads table)", "rows": n, "date_from": mn, "date_to": mx,
                "aligns": None, "note": "Used for lead assignment / follow-up context."})

    n, mn, mx = rng("SELECT COUNT(*) n, MIN(date(created_at,'localtime')) mn, MAX(date(created_at,'localtime')) mx FROM shopify_orders")
    cov.append({"source": "Shopify Orders", "rows": n, "date_from": mn, "date_to": mx,
                "aligns": None, "note": "Full order history kept; only in-period orders earn incentive (see Order Attribution)."})

    n = conn.execute("SELECT COUNT(*) n FROM customer_salesperson_map").fetchone()["n"]
    n_orders = conn.execute(
        """SELECT COUNT(DISTINCT m.customer_phone_norm) n FROM customer_salesperson_map m
           JOIN shopify_orders o ON o.customer_phone_norm = m.customer_phone_norm""").fetchone()["n"]
    cov.append({"source": "Customer -> Salesperson map", "rows": n, "date_from": None, "date_to": None,
                "aligns": None, "note": f"{n_orders} of these customers actually have a Shopify order (only those affect payout)."})

    return cov

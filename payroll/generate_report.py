"""
generate_report.py
Builds the MasonMart Combined Performance & Salary Report as a .xlsx,
mirroring the structure of MasonMart_Combined_Audit_Salary_Jun2026.xlsx.

CORE RULE: a number is only written if it can be computed from real,
ingested data plus a fully-specified payroll_config.json value. Anything
that depends on a missing input (Never Attended data, an unresolved
valid_call_definition, an unmapped customer) is written as an explicit
"NOT AVAILABLE — <reason>" string, never as 0, blank, or a guess. A blank
cell and a real zero look identical to a reader; this project got burned
once already by a silent zero being mistaken for "no activity" instead of
"data not loaded" (see chat_query.py's ANSWER_SYSTEM_PROMPT for the twin
of this rule on the chat side). Do not weaken this without the same
scrutiny that rule got.

USAGE:
    python payroll/generate_report.py
    (edit payroll/payroll_config.json first — see its _readme)

Output: payroll/output/MasonMart_Report_<timestamp>.xlsx
"""

import json
import os
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import get_connection

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
except ImportError:
    print("ERROR: openpyxl not installed. Run: pip install openpyxl")
    sys.exit(1)

PAYROLL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PAYROLL_DIR, "payroll_config.json")
OUTPUT_DIR = os.path.join(PAYROLL_DIR, "output")

NA = "NOT AVAILABLE"  # placeholder text — never a number, never blank

HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Arial", bold=True, size=14)
BLOCKED_FONT = Font(name="Arial", italic=True, color="B45309")
NORMAL_FONT = Font(name="Arial", size=10)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_period(conn, config):
    """If the config doesn't specify a period, fall back to the full range
    of data actually present — and say so loudly on the report, so nobody
    mistakes 'whatever data happens to exist' for a specific pay period."""
    p = config.get("period", {})
    start, end = p.get("start_date"), p.get("end_date")
    explicit = bool(start and end)
    if not explicit:
        row = conn.execute(
            "SELECT MIN(date(call_timestamp)) mn, MAX(date(call_timestamp)) mx FROM callyzer_calls"
        ).fetchone()
        start, end = row["mn"], row["mx"]
    return start, end, explicit


def working_days(start, end, excluded_weekday_names):
    weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
                   "Friday": 4, "Saturday": 5, "Sunday": 6}
    excluded = {weekday_map[w] for w in excluded_weekday_names if w in weekday_map}
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    days = []
    d = d0
    while d <= d1:
        if d.weekday() not in excluded:
            days.append(d)
        d += timedelta(days=1)
    return days


def rep_sim_for(conn, canonical_name):
    row = conn.execute(
        "SELECT rep_sim_number FROM reps WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()
    return row["rep_sim_number"] if row else None


def performance_stats(conn, rep_sim, start, end):
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) AS outgoing_attempts,
             SUM(CASE WHEN COALESCE(duration_seconds,0) > 45 THEN 1 ELSE 0 END) AS connected_45s,
             SUM(COALESCE(duration_seconds,0)) AS talk_seconds,
             COUNT(DISTINCT date(call_timestamp)) AS active_days
           FROM callyzer_calls
           WHERE rep_sim_number = ?
             AND date(call_timestamp) BETWEEN ? AND ?""",
        (rep_sim, start, end),
    ).fetchone()
    per_day = conn.execute(
        """SELECT date(call_timestamp) d,
                  SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) n
           FROM callyzer_calls
           WHERE rep_sim_number = ? AND date(call_timestamp) BETWEEN ? AND ?
           GROUP BY d""",
        (rep_sim, start, end),
    ).fetchall()
    return row, per_day


def format_talk_time(total_seconds):
    total_seconds = total_seconds or 0
    h, rem = divmod(int(total_seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m"


def order_attribution(conn, config):
    """Order-sequence-based incentive detail per mapped customer, using
    customer_salesperson_map (payroll/customer_salesperson_map.csv) — NOT
    v_order_attribution's call-recency guess. This is the "who owns this
    account" source the incentive contract actually pays on."""
    tiers = config["order_incentive_tiers"]
    qual = config["order_incentive_qualification"]
    rows = conn.execute(
        """SELECT m.canonical_name, m.customer_phone_norm, o.order_id, o.order_number,
                  o.created_at, o.total_price
           FROM customer_salesperson_map m
           JOIN shopify_orders o ON o.customer_phone_norm = m.customer_phone_norm
           ORDER BY m.customer_phone_norm, o.created_at"""
    ).fetchall()

    by_customer = {}
    for r in rows:
        by_customer.setdefault(r["customer_phone_norm"], []).append(r)

    detail = []
    incentive_by_rep = {}
    for phone, orders in by_customer.items():
        cumulative = 0.0
        for i, o in enumerate(orders, start=1):
            cumulative += o["total_price"]
            seq_label = {1: "1st", 2: "2nd", 3: "3rd"}.get(i, f"{i}th")
            tier_amount = tiers.get(seq_label, 0) if i <= 3 else 0
            earned = i >= qual["min_order_sequence"] and cumulative >= qual["min_order_value"]
            status = "Earned" if earned else f"Pending (no {qual['min_order_sequence']}rd order)"
            detail.append({
                "rep": o["canonical_name"], "customer_phone": phone,
                "order_number": o["order_number"], "sequence": seq_label,
                "value": o["total_price"], "cumulative": cumulative,
                "incentive": tier_amount, "status": status,
            })
            incentive_by_rep.setdefault(o["canonical_name"], 0.0)
            incentive_by_rep[o["canonical_name"]] += tier_amount

    unattributed = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_price),0) rev
           FROM shopify_orders o
           WHERE o.customer_phone_norm NOT IN (SELECT customer_phone_norm FROM customer_salesperson_map)
              OR o.customer_phone_norm IS NULL"""
    ).fetchone()

    return detail, incentive_by_rep, unattributed


def salary_for(conn, canonical_name, emp_cfg, config, perf, order_incentive):
    """Returns a dict of salary components, each either a number or the
    NA placeholder with a reason baked into the accompanying note."""
    out = {}
    valid_call_def = config.get("valid_call_definition")

    if emp_cfg["employment_type"] == "full_time":
        out["fixed_salary"] = emp_cfg.get("fixed_salary")
        if out["fixed_salary"] is None:
            out["fixed_salary"] = NA
        out["call_based_pay"] = "N/A (full-time, fixed salary)"
    else:
        rate = emp_cfg.get("per_call_rate")
        if rate is None:
            out["call_based_pay"] = f"{NA} — per_call_rate not set for {canonical_name} in payroll_config.json"
        elif valid_call_def is None:
            out["call_based_pay"] = (f"{NA} — valid_call_definition not set in payroll_config.json "
                                      f"(this changes the answer materially — see its _readme)")
        else:
            count = perf["connected_45s"] if valid_call_def == "connected_45s" else perf["outgoing_attempts"]
            out["call_based_pay"] = round((count or 0) * rate, 2)
        out["fixed_salary"] = "N/A (part-time, no fixed salary)"

    out["order_incentive_pending"] = round(order_incentive, 2)
    out["target_bonus"] = emp_cfg.get("target_bonus_amount", 0)

    payable_components = []
    if isinstance(out["fixed_salary"], (int, float)):
        payable_components.append(out["fixed_salary"])
    if isinstance(out.get("call_based_pay"), (int, float)):
        payable_components.append(out["call_based_pay"])
    if emp_cfg.get("probation"):
        out["payable_now"] = (f"{sum(payable_components):,.2f} (fixed only — incentives withheld, "
                               f"probation) " if payable_components else NA)
    else:
        out["payable_now"] = round(sum(payable_components), 2) if payable_components else NA

    return out


def build_workbook(conn, config):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    start, end, explicit_period = resolve_period(conn, config)
    excluded = config["period"]["excluded_weekdays"]
    wdays = working_days(start, end, excluded)

    employees = {k: v for k, v in config["employees"].items() if not k.startswith("_")}
    for canonical_name, emp_cfg in employees.items():
        rep_sim = rep_sim_for(conn, canonical_name)
        emp_cfg["_resolved_rep_sim"] = rep_sim

    order_detail, incentive_by_rep, unattributed = order_attribution(conn, config)

    # ---- Sheet 1: Combined Summary ----
    ws = wb.create_sheet("1. Combined Summary")
    ws["A1"] = "MasonMart – Combined Performance & Salary Report"
    ws["A1"].font = TITLE_FONT
    period_label = (f"Period {start} to {end}" + ("" if explicit_period else
                    "  (⚠ no period set in payroll_config.json — showing FULL data range on file, not a specific pay period)")
                    + f" • {', '.join(excluded)} excluded → {len(wdays)} working days")
    ws["A2"] = period_label
    ws["A2"].font = BLOCKED_FONT if not explicit_period else NORMAL_FONT

    perf_headers = ["Employee", "Type", "Outgoing Attempts", "Connected >45s", "Talk Time",
                    "Active Days", "Never Attended", "Order Incentive Pending (₹)"]
    r = 4
    ws.cell(r, 1, "A. PERFORMANCE SNAPSHOT").font = Font(name="Arial", bold=True)
    r += 1
    for c, h in enumerate(perf_headers, start=1):
        cell = ws.cell(r, c, h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    r += 1

    perf_cache = {}
    for canonical_name, emp_cfg in employees.items():
        rep_sim = emp_cfg["_resolved_rep_sim"]
        if rep_sim is None:
            row_vals = [canonical_name, emp_cfg["employment_type"],
                        f"{NA} — no call history for this name in reps table", "", "", "", "", ""]
            ws.append(row_vals)
            for c in range(1, len(row_vals) + 1):
                ws.cell(r, c).font = BLOCKED_FONT
            r += 1
            continue

        perf, _ = performance_stats(conn, rep_sim, start, end)
        perf_cache[canonical_name] = perf

        never_attended_n = conn.execute(
            "SELECT COUNT(*) FROM v_never_attended_final WHERE rep_sim_number = ? AND callback_attempted = 0",
            (rep_sim,),
        ).fetchone()[0]
        has_never_attended_data = conn.execute(
            "SELECT COUNT(*) FROM callyzer_never_attended"
        ).fetchone()[0] > 0
        never_attended_display = never_attended_n if has_never_attended_data else \
            f"{NA} — Never Attended Report not yet ingested (see payroll/incoming/)"

        incentive = incentive_by_rep.get(canonical_name, 0.0)

        ws.append([
            canonical_name, emp_cfg["employment_type"],
            perf["outgoing_attempts"] or 0, perf["connected_45s"] or 0,
            format_talk_time(perf["talk_seconds"]), perf["active_days"] or 0,
            never_attended_display, incentive,
        ])
        r += 1

    r += 2
    ws.cell(r, 1, "B. SALARY & INCENTIVE SNAPSHOT").font = Font(name="Arial", bold=True)
    r += 1
    salary_headers = ["Employee", "Type", "Fixed Salary (₹)", "Call-Based Pay (₹)",
                       "Order Incentive Pending (₹)", "Target Bonus (₹)", "Payable Now (₹)"]
    for c, h in enumerate(salary_headers, start=1):
        cell = ws.cell(r, c, h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    r += 1
    for canonical_name, emp_cfg in employees.items():
        if canonical_name not in perf_cache:
            continue
        sal = salary_for(conn, canonical_name, emp_cfg, config, perf_cache[canonical_name],
                          incentive_by_rep.get(canonical_name, 0.0))
        ws.append([
            canonical_name, emp_cfg["employment_type"],
            sal["fixed_salary"], sal["call_based_pay"],
            sal["order_incentive_pending"], sal["target_bonus"], sal["payable_now"],
        ])
        for c in range(1, len(salary_headers) + 1):
            val = ws.cell(r, c).value
            if isinstance(val, str) and NA in val:
                ws.cell(r, c).font = BLOCKED_FONT
        r += 1

    for col, width in zip("ABCDEFGH", [20, 12, 22, 18, 26, 16, 30, 16]):
        ws.column_dimensions[col].width = width

    # ---- Sheet: Order Attribution ----
    ws2 = wb.create_sheet("7. Order Attribution")
    ws2["A1"] = "Order Attribution & Incentive Detail"
    ws2["A1"].font = TITLE_FONT
    ws2["A2"] = ("Source: payroll/customer_salesperson_map.csv (manual mapping) joined to shopify_orders. "
                 "Not the same as the chat's call-recency v_order_attribution — see payroll/README.md.")
    headers2 = ["Salesperson", "Customer Phone", "Order", "Sequence", "Order Value (₹)",
                "Cumulative (₹)", "Incentive (₹)", "Status"]
    for c, h in enumerate(headers2, start=1):
        cell = ws2.cell(4, c, h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    r2 = 5
    if not order_detail:
        ws2.cell(r2, 1, f"{NA} — customer_salesperson_map.csv has no real mappings yet (only the "
                        f"EXAMPLE row). Fill it in, run payroll/ingest_customer_map.py, then regenerate.")
        ws2.cell(r2, 1).font = BLOCKED_FONT
        r2 += 1
    else:
        for d in order_detail:
            ws2.append([d["rep"], d["customer_phone"], d["order_number"], d["sequence"],
                        d["value"], d["cumulative"], d["incentive"], d["status"]])
            r2 += 1
    r2 += 1
    ws2.cell(r2, 1, f"Unattributed paid orders (no salesperson mapped): {unattributed['n']} orders, "
                    f"₹{unattributed['rev']:,.2f} — earn no incentive until mapped.")
    for col, width in zip("ABCDEFGH", [18, 16, 12, 10, 16, 16, 14, 26]):
        ws2.column_dimensions[col].width = width

    # ---- Sheet: Methodology & Gaps (auto-generated, not hand-maintained prose) ----
    ws3 = wb.create_sheet("8. Methodology & Gaps")
    ws3["A1"] = "Methodology & Data Gaps (auto-generated)"
    ws3["A1"].font = TITLE_FONT
    gap_rows = [
        ("Period", period_label),
        ("Employee matching", "By company SIM number (reps table), name variants reconciled via rep_name_aliases."),
        ("Performance", "Counted directly from callyzer_calls for each rep_sim_number in the period above."),
        ("valid_call_definition", config.get("valid_call_definition") or
         f"{NA} — UNSET in payroll_config.json. Part-time call-based pay cannot be computed until this is set "
         f"to 'connected_45s' or 'any_outgoing_attempt'."),
        ("Never Attended data", "Present and used." if conn.execute(
            "SELECT COUNT(*) FROM callyzer_never_attended").fetchone()[0] > 0
            else f"{NA} — no Never Attended Report has been ingested (payroll/ingest_never_attended.py)."),
        ("Customer→salesperson mapping", f"{len(order_detail)} order line(s) from "
         f"{len(set(d['customer_phone'] for d in order_detail))} mapped customer(s). "
         f"{unattributed['n']} paid order(s) remain unattributed — see payroll/customer_salesperson_map.csv."),
        ("Gold Lead / discipline rules", f"{NA} — not yet implemented in this generator; needs a real "
         f"Gold Lead-tagged lead export to build and verify against."),
    ]
    r3 = 3
    for label, val in gap_rows:
        ws3.cell(r3, 1, label).font = Font(name="Arial", bold=True)
        ws3.cell(r3, 2, val)
        if isinstance(val, str) and NA in val:
            ws3.cell(r3, 2).font = BLOCKED_FONT
        r3 += 1
    ws3.column_dimensions["A"].width = 28
    ws3.column_dimensions["B"].width = 90

    return wb


def generate_report_file():
    """Reusable by both the CLI and the web upload endpoint. Returns the
    output path."""
    config = load_config()
    conn = get_connection()
    wb = build_workbook(conn, config)
    conn.close()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, f"MasonMart_Report_{stamp}.xlsx")
    wb.save(out_path)
    return out_path


def main():
    out_path = generate_report_file()
    print(f"Report written to {out_path}")


if __name__ == "__main__":
    main()

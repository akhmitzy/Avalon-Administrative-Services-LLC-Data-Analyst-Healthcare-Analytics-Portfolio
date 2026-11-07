"""
Prospect pricing model: Lakeshore Senior Plan (C05, Medicare Advantage, not live).

1. Baseline: last 12 complete months (Jun 2025 - May 2026) of completed avoidable spend
   by policy, from the policy-adherence engine run on the prospect's own claims.
2. Capture: how much of that avoidable spend live clients actually removed
   (low = weakest live client, base = Medicare Advantage peer, high = strongest live client).
3. Trend: pooled underlying PMPM trend from the forecast model.
4. Contract structures: fixed PMPM fee, shared savings, hybrid with ROI guarantee.

Writes outputs/Lakeshore_pricing_model.xlsx with live formulas (inputs in blue, editable),
and a JSON copy of the base-case results for the portfolio site.
"""
from __future__ import annotations

import json

import pandas as pd
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from common import OUT, REPORTS, SITE, connect, write_json

PROSPECT = "C05"
BASE_START, BASE_END = "2025-06-01", "2026-05-01"
POLICY_NAMES = {
    "P01": "Vitamin D screening without indication",
    "P02": "HbA1c above frequency limit",
    "P03": "Same-day duplicate routine test",
    "P04": "Definitive drug test w/o presumptive screen",
    "P05": "Large respiratory panel w/o indication",
    "P06": "Genetic test w/o clinical indication",
    "P07": "Out-of-network lab (price differential)",
}

ARIAL = "Arial"
BLUE = Font(name=ARIAL, color="0000FF")
BLACK = Font(name=ARIAL, color="000000")
GREEN = Font(name=ARIAL, color="008000")
BOLD = Font(name=ARIAL, bold=True)
TITLE = Font(name=ARIAL, bold=True, size=14)
HDR_FILL = PatternFill("solid", fgColor="1F3A5F")
HDR_FONT = Font(name=ARIAL, bold=True, color="FFFFFF")
KEY_FILL = PatternFill("solid", fgColor="FFFF00")
THIN = Border(bottom=Side(style="thin", color="BFBFBF"))
USD = '$#,##0;($#,##0);"-"'
USD2 = '$#,##0.00;($#,##0.00);"-"'
USD3 = '$#,##0.000;($#,##0.000);"-"'
PCT = '0.0%;(0.0%);"-"'
MULT = '0.0"x"'


def gather_inputs(con) -> dict:
    cf = con.execute("SELECT service_month, completion_factor FROM marts.completion_factors").df()
    mm = con.execute(f"""
        SELECT SUM(member_months) FROM (
          SELECT service_month, MAX(member_months) AS member_months FROM marts.client_month_metrics
          WHERE client_id = '{PROSPECT}' AND service_month BETWEEN '{BASE_START}' AND '{BASE_END}'
          GROUP BY service_month)""").fetchone()[0]
    members_now = con.execute(f"""
        SELECT MAX(member_months) FROM marts.client_month_metrics
        WHERE client_id = '{PROSPECT}' AND service_month = '{BASE_END}'""").fetchone()[0]
    pol = con.execute(f"""
        SELECT service_month, policy_id, SUM(avoidable_allowed) AS av, SUM(net_allowed) AS allowed
        FROM marts.claim_policy_flags
        WHERE client_id = '{PROSPECT}' AND service_month BETWEEN '{BASE_START}' AND '{BASE_END}'
        GROUP BY 1, 2""").df().merge(cf, on="service_month")
    pol["av_c"] = pol["av"] * pol["completion_factor"]
    pol["allowed_c"] = pol["allowed"] * pol["completion_factor"]
    total_allowed = pol["allowed_c"].sum()
    by_pol = pol.dropna(subset=["policy_id"]).groupby("policy_id")["av_c"].sum() / mm
    cap = pd.read_csv(REPORTS / "policy_capture_rates.csv").set_index("policy_id")
    fc = json.loads((SITE / "forecast.json").read_text())
    peers = json.loads((SITE / "program_impact.json").read_text())["clients"]
    return {
        "members": int(members_now), "baseline_mm": int(mm), "baseline_pmpm": total_allowed / mm,
        "avoidable": by_pol.to_dict(), "capture": cap, "trend": fc["underlying_trend"], "peers": peers,
    }


def hdr(ws, row, labels, col=1):
    for i, lab in enumerate(labels):
        c = ws.cell(row=row, column=col + i, value=lab)
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def build_workbook(inp: dict, path) -> None:
    wb = Workbook()
    # ------------------------------------------------------------------ Inputs
    ws = wb.active
    ws.title = "Inputs"
    ws["A1"], ws["A1"].font = "Lakeshore Senior Plan - Lab Program Pricing Inputs", TITLE
    ws["A2"] = "Blue = editable input. Yellow = key assumption. Black = formula. Green = link to another sheet."
    ws["A2"].font = Font(name=ARIAL, italic=True, color="595959")
    rows = [
        ("Prospect", "Lakeshore Senior Plan (Medicare Advantage)", None, "Synthetic prospect, client C05"),
        ("Members (current)", inp["members"], "#,##0", "Eligible members, May 2026 (stg_member_months)"),
        ("Baseline lab allowed PMPM", round(inp["baseline_pmpm"], 2), USD2,
         "Completed allowed PMPM, Jun 2025 - May 2026, IBNR-adjusted (chain-ladder)"),
        ("Annual PMPM trend", round(inp["trend"], 4), PCT, "Pooled underlying trend from forecast model (forecast.py)"),
        ("Months from baseline midpoint to contract-year midpoint", 18, "0",
         "Baseline mid = Dec 2025; contract year 2027 mid = Jun 2027"),
        ("Year-1 ramp-up (share of run-rate capture)", 0.80, PCT,
         "Assumption: policies, provider outreach and network steerage phase in over Q1"),
        ("Target ROI multiple (client view)", 2.0, MULT, "Savings / fees the client expects; matches live contracts"),
    ]
    for i, (lab, val, fmt, note) in enumerate(rows, start=4):
        ws.cell(row=i, column=1, value=lab).font = BLACK
        c = ws.cell(row=i, column=2, value=val)
        c.font = BLUE
        if fmt:
            c.number_format = fmt
        ws.cell(row=i, column=3, value=note).font = Font(name=ARIAL, color="595959", size=9)
    for r in (8, 9):
        ws.cell(row=r, column=2).fill = KEY_FILL

    ws["A13"], ws["A13"].font = "Avoidable spend baseline and capture assumptions by policy", BOLD
    hdr(ws, 14, ["Policy", "Description", "Baseline avoidable PMPM", "Capture - Low", "Capture - Base",
                 "Capture - High", "Source of capture"])
    cap = inp["capture"]
    first = 15
    for j, pid in enumerate(sorted(POLICY_NAMES)):
        r = first + j
        ws.cell(row=r, column=1, value=pid).font = BLACK
        ws.cell(row=r, column=2, value=POLICY_NAMES[pid]).font = BLACK
        c = ws.cell(row=r, column=3, value=round(inp["avoidable"].get(pid, 0.0), 4))
        c.font, c.number_format = BLUE, USD3
        low = max(0.0, float(cap.loc[pid, "capture_min"]))
        base = float(cap.loc[pid, "capture_ma_peer"])
        high = float(cap.loc[pid, "capture_max"])
        for k, v in enumerate([low, base, high]):
            cc = ws.cell(row=r, column=4 + k, value=round(v, 4))
            cc.font, cc.number_format = BLUE, PCT
        ws.cell(row=r, column=5).fill = KEY_FILL
        ws.cell(row=r, column=7, value="Low/High = weakest/strongest live client; Base = MA peer (Bayview)").font = \
            Font(name=ARIAL, color="595959", size=9)
    last = first + len(POLICY_NAMES) - 1
    tr = last + 1
    ws.cell(row=tr, column=1, value="Total").font = BOLD
    c = ws.cell(row=tr, column=3, value=f"=SUM(C{first}:C{last})")
    c.font, c.number_format = Font(name=ARIAL, bold=True), USD3
    ws.cell(row=tr + 1, column=1, value="Avoidable share of baseline spend").font = BLACK
    c = ws.cell(row=tr + 1, column=3, value=f"=IFERROR(C{tr}/B6,0)")
    c.number_format = PCT
    ws.cell(row=15, column=3).comment = Comment(
        "Completed avoidable allowed $ / member months for the prospect's own claims, "
        "from marts.claim_policy_flags (policy engine).", "Analyst")

    ws["A26"], ws["A26"].font = "Contract structures", BOLD
    hdr(ws, 27, ["Structure", "Admin fee PMPM", "Share of savings", "ROI guarantee", "Fees at risk", "Notes"])
    structs = [
        ("A. Fixed PMPM fee", 1.00, 0.0, 0, 0, "Simple, predictable for the plan; vendor carries performance risk only via renewal"),
        ("B. Shared savings", 0.0, 0.35, 0, 0, "No fee unless savings are realized; volatile revenue"),
        ("C. Hybrid with guarantee", 0.60, 0.15, 2.0, 0.25, "Base fee + share; refund if savings < guarantee x fees, capped at fees at risk"),
    ]
    for j, (name, fee, share, g, risk, note) in enumerate(structs):
        r = 28 + j
        ws.cell(row=r, column=1, value=name).font = BLACK
        for k, (v, fmt) in enumerate([(fee, USD2), (share, PCT), (g, MULT), (risk, PCT)]):
            c = ws.cell(row=r, column=2 + k, value=v)
            c.font, c.number_format = BLUE, fmt
        ws.cell(row=r, column=6, value=note).font = Font(name=ARIAL, color="595959", size=9)
    for col, w in zip("ABCDEFG", [52, 44, 24, 15, 15, 15, 62]):
        ws.column_dimensions[col].width = w

    # ------------------------------------------------------------------ Model
    m = wb.create_sheet("Pricing Model")
    m["A1"], m["A1"].font = "Contract-year 2027 savings and fee economics", TITLE
    m["A2"] = "All figures are annual unless noted. Scenarios run side by side; edit inputs on the Inputs sheet."
    m["A2"].font = Font(name=ARIAL, italic=True, color="595959")
    hdr(m, 4, ["Line item", "Low", "Base", "High"])
    lines = [
        ("Members", "=Inputs!$B$5", "#,##0", True),
        ("Trend factor to contract year", "=(1+Inputs!$B$7)^(Inputs!$B$8/12)", "0.0000", True),
        ("Avoidable PMPM, trended", f"=Inputs!$C${tr}*{{c}}6", USD3, False),
        ("Run-rate savings PMPM", f"=SUMPRODUCT(Inputs!$C${first}:$C${last},Inputs!{{cap}}{first}:{{cap}}{last})*{{c}}6", USD3, False),
        ("Year-1 savings PMPM (after ramp)", "={c}8*Inputs!$B$9", USD3, False),
        ("Gross savings, year 1 ($)", "={c}9*{c}5*12", USD, False),
        ("Savings as % of projected lab spend", "=IFERROR({c}9/(Inputs!$B$6*{c}6),0)", PCT, False),
        ("Break-even fee PMPM at target ROI", "={c}9/Inputs!$B$10", USD2, False),
    ]
    cap_cols = {"B": "$D$", "C": "$E$", "D": "$F$"}
    for i, (lab, f, fmt, same) in enumerate(lines, start=5):
        m.cell(row=i, column=1, value=lab).font = BLACK
        for col in "BCD":
            formula = f.replace("{c}", col).replace("{cap}", cap_cols[col])
            c = m[f"{col}{i}"]
            c.value = formula
            c.number_format = fmt
            c.font = GREEN if formula.startswith("=Inputs!") else BLACK
    # structures
    row = 14
    for j, name in enumerate(["A. Fixed PMPM fee", "B. Shared savings", "C. Hybrid with guarantee"]):
        ir = 28 + j
        m.cell(row=row, column=1, value=name).font = BOLD
        hdr(m, row, [name, "Low", "Base", "High"])
        items = [
            ("Fees before guarantee ($)", f"=Inputs!$B${ir}*{{c}}5*12+Inputs!$C${ir}*{{c}}10", USD),
            ("Guarantee threshold ($)", f"=Inputs!$D${ir}*{{c}}{row + 1}", USD),
            ("Refund due ($)", f"=IF(Inputs!$D${ir}=0,0,MIN(MAX(0,{{c}}{row + 2}-{{c}}10),Inputs!$E${ir}*{{c}}{row + 1}))", USD),
            ("Net fees ($)", f"={{c}}{row + 1}-{{c}}{row + 3}", USD),
            ("Client net savings ($)", f"={{c}}10-{{c}}{row + 4}", USD),
            ("Client ROI (savings / net fees)", f"=IFERROR({{c}}10/{{c}}{row + 4},0)", MULT),
            ("Net fee PMPM", f"=IFERROR({{c}}{row + 4}/({{c}}5*12),0)", USD2),
        ]
        for k, (lab, f, fmt) in enumerate(items, start=1):
            m.cell(row=row + k, column=1, value=lab).font = BLACK
            for col in "BCD":
                c = m[f"{col}{row + k}"]
                c.value = f.replace("{c}", col)
                c.number_format = fmt
                c.font = BLACK
                c.border = THIN
        row += len(items) + 2
    m.column_dimensions["A"].width = 40
    for col in "BCD":
        m.column_dimensions[col].width = 16

    # ------------------------------------------------------------------ Peers
    p = wb.create_sheet("Peer Benchmarks")
    p["A1"], p["A1"].font = "Live-client results (program_impact.py, through May 2026)", TITLE
    p["A2"] = "Values pasted from the client value scorecard; source: outputs/reports/client_value_scorecard.csv"
    p["A2"].font = Font(name=ARIAL, italic=True, color="595959")
    hdr(p, 4, ["Client", "LOB", "Go-live", "Post months", "Expected PMPM", "Actual PMPM", "Savings %",
               "Savings ($)", "Fees ($)", "ROI", "Guarantee met"])
    for i, c in enumerate(inp["peers"], start=5):
        vals = [c["client_name"], c["line_of_business"], c["go_live"], c["post_months"], c["expected_pmpm"],
                c["actual_pmpm"], c["savings_pct"], c["gross_savings"], c["fees"], c["roi_multiple"],
                "Yes" if c["guarantee_met"] else "No"]
        fmts = [None, None, None, "0", USD2, USD2, PCT, USD, USD, MULT, None]
        for k, (v, fmt) in enumerate(zip(vals, fmts), start=1):
            cell = p.cell(row=i, column=k, value=v)
            cell.font = BLUE
            if fmt:
                cell.number_format = fmt
    for k, w in enumerate([30, 20, 10, 12, 14, 14, 11, 14, 14, 8, 14], start=1):
        p.column_dimensions[get_column_letter(k)].width = w

    # ------------------------------------------------------------------ Notes
    n = wb.create_sheet("Notes")
    notes = [
        "Method notes",
        "1. Baseline avoidable spend comes from running the same policy engine used for live clients on the prospect's claims.",
        "2. Capture rates are realized reductions in avoidable PMPM at live clients (pre vs post go-live, within-client).",
        "3. Savings are lab allowed dollars; plan net savings after member cost share are slightly lower (MA cost share ~4%).",
        "4. P07 savings are the price gap to the in-network median for the same test, client and quarter.",
        "5. Year-1 ramp reflects provider education and network steerage time; run-rate applies from year 2.",
        "6. All data in this workbook is synthetic and generated for a portfolio project.",
    ]
    for i, t in enumerate(notes, start=1):
        n.cell(row=i, column=1, value=t).font = BOLD if i == 1 else BLACK
    n.column_dimensions["A"].width = 120
    wb.save(path)


def python_check(inp: dict) -> dict:
    """Same base-case math in Python, to cross-check the workbook after recalculation."""
    cap = inp["capture"]
    tf = (1 + inp["trend"]) ** (18 / 12)
    out = {}
    for scen, col in [("low", "capture_min"), ("base", "capture_ma_peer"), ("high", "capture_max")]:
        run = sum(inp["avoidable"].get(p, 0) * max(0.0, cap.loc[p, col]) if scen == "low"
                  else inp["avoidable"].get(p, 0) * cap.loc[p, col] for p in POLICY_NAMES) * tf
        y1 = run * 0.80
        gross = y1 * inp["members"] * 12
        fees_a = 1.00 * inp["members"] * 12
        fees_c = 0.60 * inp["members"] * 12 + 0.15 * gross
        refund_c = min(max(0, 2.0 * fees_c - gross), 0.25 * fees_c)
        out[scen] = {"savings_pmpm_y1": y1, "gross_savings": gross, "pct_of_spend": y1 / (inp["baseline_pmpm"] * tf),
                     "breakeven_fee_pmpm": y1 / 2.0, "fixed_fee_roi": gross / fees_a,
                     "shared_fees": 0.35 * gross, "hybrid_net_fees": fees_c - refund_c,
                     "hybrid_roi": gross / (fees_c - refund_c), "hybrid_refund": refund_c}
    return out


def main() -> None:
    con = connect(read_only=True)
    inp = gather_inputs(con)
    path = OUT / "Lakeshore_pricing_model.xlsx"
    build_workbook(inp, path)
    chk = python_check(inp)
    write_json("pricing", {"members": inp["members"], "baseline_pmpm": inp["baseline_pmpm"],
                           "trend": inp["trend"], "avoidable": inp["avoidable"],
                           "avoidable_total": sum(inp["avoidable"].values()),
                           "capture": inp["capture"].reset_index().round(4).to_dict(orient="records"),
                           "scenarios": chk})
    print(f"wrote {path}")
    print(json.dumps(chk, indent=1, default=float))


if __name__ == "__main__":
    main()

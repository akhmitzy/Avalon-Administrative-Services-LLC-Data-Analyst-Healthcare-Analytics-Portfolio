"""Render site/template.html into outputs/portfolio.html with data, notes and code excerpts."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
B = json.loads((ROOT / "outputs" / "site_data" / "bundle.json").read_text())


def excerpt(rel: str, start: str | None = None, end: str | None = None, max_lines: int = 80) -> str:
    text = (ROOT / rel).read_text()
    if start:
        text = text[text.index(start):]
    if end:
        text = text[: text.index(end)]
    lines = text.rstrip().splitlines()[:max_lines]
    return highlight("\n".join(lines), rel)


def highlight(code: str, rel: str) -> str:
    code = html.escape(code, quote=False)
    if rel.endswith(".sql"):
        code = re.sub(r"(--[^\n]*)", r'<span class="c">\1</span>', code)
        kw = r"\b(SELECT|FROM|WHERE|JOIN|LEFT|FULL|OUTER|ON|AND|OR|NOT|CASE|WHEN|THEN|ELSE|END|GROUP BY|ORDER BY|WITH|AS|CREATE OR REPLACE|TABLE|PROCEDURE|QUALIFY|UNION ALL|BETWEEN|IN|EXISTS|IS NULL|IS NOT NULL|INTERVAL|DECLARE|BEGIN|IF|RETURN|INSERT INTO|VALUES|EXECUTE IMMEDIATE|COUNT|SUM|MAX|COALESCE|ROUND|DISTINCT)\b"
        parts = re.split(r'(<span class="c">.*?</span>)', code)
        code = "".join(p if p.startswith('<span class="c">') else re.sub(kw, r'<span class="k">\1</span>', p) for p in parts)
    elif rel.endswith(".py"):
        code = re.sub(r"(#[^\n]*)", r'<span class="c">\1</span>', code)
        parts = re.split(r'(<span class="c">.*?</span>)', code)
        code = "".join(p if p.startswith('<span') else re.sub(r"\b(def|return|for|in|if|else|import|from|class)\b", r'<span class="k">\1</span>', p) for p in parts)
    elif rel.endswith((".yaml", ".yml")):
        code = re.sub(r"(#[^\n]*)", r'<span class="c">\1</span>', code)
        code = re.sub(r"^(\s*-?\s*)([a-z_]+):", r'\1<span class="k">\2</span>:', code, flags=re.M)
    return code


def notes() -> dict:
    sc = {c["client_id"]: c for c in B["program_impact"]["clients"]}
    pbc = B["policy_by_client"]

    def pol(cid, per, p):
        return next((float(r["avoidable_pmpm"]) for r in pbc if r["client_id"] == cid and r["program_period"] == per and r["policy_id"] == p), 0.0)

    def drop(cid, p):
        pre, post = pol(cid, "Pre go-live", p), pol(cid, "Post go-live", p)
        return 1 - post / pre if pre else 0

    def m(x): return f"${x:,.0f}"
    pr = B["pricing"]
    n = {}
    c = sc["C01"]
    n["C01"] = (f"<b>Read-out:</b> savings of {c['savings_pct']:.1%} against expected trend, {c['roi_multiple']:.1f}x fees, guarantee met. "
                f"Out-of-network share of spend fell from {c['oon_share_pre']:.1%} to {c['oon_share_post']:.1%}. "
                f"<b>Next:</b> genetic testing without indication is still {m(pol('C01', 'Post go-live', 'P06') * 1000)} per 1,000 members a month; target the top ordering providers.")
    c = sc["C02"]
    n["C02"] = (f"<b>Read-out:</b> {c['savings_pct']:.1%} savings and {c['roi_multiple']:.2f}x fees, just under the 2.0x guarantee: refund of {m(c['fee_refund_due'])}. "
                f"A claims batch missing from the March 2026 extract (section 01) understates recent spend slightly, so re-run after the re-send before settling. "
                f"<b>Next:</b> P07 out-of-network spend fell only {drop('C02', 'P07'):.0%}; steer toxicology to in-network labs.")
    c = sc["C03"]
    n["C03"] = (f"<b>Read-out:</b> longest-live plan, {c['post_months']} months. Savings {c['savings_pct']:.1%}, {c['roi_multiple']:.1f}x fees against a higher 2.5x guarantee: met. "
                f"Definitive drug testing without a presumptive screen is down {drop('C03', 'P04'):.0%}. <b>Next:</b> use as the Medicaid reference case in renewals.")
    c = sc["C04"]
    n["C04"] = (f"<b>Read-out:</b> no detectable savings after {c['post_months']} months ({c['savings_pct']:+.1%}, inside the noise band) and the guarantee is missed, refund {m(c['fee_refund_due'])} (capped at fees at risk). "
                f"Avoidable spend fell only {1 - c['avoidable_pmpm_post'] / c['avoidable_pmpm_pre']:.0%}, against 40–55% at other plans. See the diagnosis below.")
    n["PINECREST"] = (f"<b>Diagnosis: Pinecrest Employer Health.</b> The policies that drive savings elsewhere barely moved here: genetic tests without indication {drop('C04', 'P06'):.0%}, "
                      f"full respiratory panels {drop('C04', 'P05'):.0%}, vitamin D screening {drop('C04', 'P01'):.0%}, compared with {drop('C01', 'P06'):.0%}, {drop('C01', 'P05'):.0%} and {drop('C01', 'P01'):.0%} at Suncoast, the other Commercial plan. "
                      f"Out-of-network share is still {c['oon_share_post']:.1%}. That pattern points to implementation (policies not configured in the plan's claim edits, or no provider outreach) rather than a measurement problem. "
                      f"<b>Recommendation:</b> confirm the claim-edit configuration with the plan's operations team, launch outreach to the top ordering providers for genetic and respiratory testing, and re-measure at 6 months before the guarantee settles.")
    c = sc["C06"]
    n["C06"] = (f"<b>Read-out:</b> {c['post_months']} months live, {c['savings_pct']:.1%} savings ({c['savings_pct_low']:.1%} to {c['savings_pct_high']:.1%}), ROI {c['roi_multiple']:.2f}x, a hair under 2.0x. "
                f"Early months include ramp-up, so a year-1 shortfall of {m(c['fee_refund_due'])} is likely to close. <b>Next:</b> track monthly; flag if ROI is below 1.5x at month 10.")
    n["C05"] = (f"<b>Prospect:</b> {pr['avoidable_total'] / pr['baseline_pmpm']:.0%} of Lakeshore's lab spend fails a policy today ({'$'}{pr['avoidable_total']:.2f} PMPM), the highest share in the book. "
                f"Pricing is in section 05.")
    return n


def main() -> None:
    t = (ROOT / "site" / "template.html").read_text()
    t = t.replace("/*__DATA__*/null", json.dumps(B, default=str))
    t = t.replace("{/*__NOTES__*/}", json.dumps(notes()))
    t = t.replace("{{CODE_RECON}}", excerpt("sql/quality/02_reconcile_finance_gl.sql", end="-- Batch-level drill-down"))
    t = t.replace("{{CODE_SP}}", excerpt("snowflake/02_sp_refresh_lab_marts.sql", start="CREATE OR REPLACE PROCEDURE"))
    t = t.replace("{{CODE_POLICY}}", excerpt("sql/marts/01_policy_flags.sql", start="-- P02: rolling", end="-- P06: indication"))
    t = t.replace("{{CODE_IMPACT}}", excerpt("src/analysis/program_impact.py", start="def fit_untreated_fe", end="def main"))
    t = t.replace("{{CODE_YAML}}", excerpt("semantic/lab_analytics.yaml", start="      - name: member_months", end="  - name: claim_policy_flags")
                  + "\n...\n" + excerpt("semantic/lab_analytics.yaml", start="verified_queries:", end="  - name: avoidable_by_policy_prospect"))
    t = t.replace("{{CODE_RUN}}", html.escape(
        "pip install -r requirements.txt\n"
        "make all        # generate data -> pipeline + quality gate -> analyses -> tests -> site\n\n"
        "# or step by step\n"
        "python src/generate_data.py\n"
        "python src/pipeline/run_pipeline.py\n"
        "python src/analysis/completion_factors.py && python src/analysis/program_impact.py\n"
        "python src/analysis/forecast.py && python src/analysis/pricing_model.py\n"
        "python src/ai/verify_semantic_queries.py && pytest -q"))
    assert "{{" not in t and "__DATA__" not in t
    out = ROOT / "outputs" / "portfolio.html"
    out.write_text(t)
    print(out, round(out.stat().st_size / 1024), "KB")


if __name__ == "__main__":
    main()

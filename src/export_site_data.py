"""Bundle every number the portfolio site shows into outputs/site_data/bundle.json."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "outputs" / "site_data"


def q(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> None:
    con = duckdb.connect(str(ROOT / "warehouse" / "lab_analytics.duckdb"), read_only=True)
    b = {k: json.loads((SITE / f"{k}.json").read_text())
         for k in ["program_impact", "forecast", "pricing", "completion_factors"]}
    b["overview"] = q(con, """
        SELECT (SELECT COUNT(*) FROM raw.members) AS members,
               (SELECT COUNT(*) FROM raw.claims_lab) AS raw_lines,
               (SELECT COUNT(*) FROM staging.stg_claims_net WHERE is_payable) AS payable_claims,
               (SELECT SUM(CASE WHEN is_analytic_valid THEN 1 ELSE 0 END) * 1.0 / COUNT(*) FROM staging.stg_claims_lab) AS valid_rate,
               (SELECT ROUND(SUM(net_allowed)) FROM staging.stg_claims_net WHERE is_payable) AS total_allowed,
               (SELECT COUNT(*) FROM staging.stg_member_months) AS member_months,
               (SELECT SUM(CASE WHEN recon_status = 'RECONCILED' THEN 1 ELSE 0 END) FROM qa.recon_claims_vs_gl) AS recon_ok,
               (SELECT COUNT(*) FROM qa.recon_claims_vs_gl) AS recon_total,
               (SELECT ROUND(SUM(avoidable_allowed)) FROM marts.claim_policy_flags) AS avoidable_total,
               (SELECT COUNT(*) FROM marts.claim_policy_flags WHERE policy_id IS NOT NULL) AS flagged_claims
    """)[0]
    b["dq_checks"] = q(con, "SELECT check_name, severity, failing_rows, fail_rate, max_fail_rate, status "
                             "FROM qa.dq_check_results ORDER BY severity, failing_rows DESC")
    b["recon_exceptions"] = q(con, "SELECT client_id, CAST(paid_month AS VARCHAR) AS paid_month, gl_paid, claims_paid, "
                                   "variance, variance_pct FROM qa.recon_claims_vs_gl WHERE recon_status <> 'RECONCILED'")
    b["recon_gaps"] = q(con, "SELECT client_id, CAST(paid_month AS VARCHAR) AS paid_month, missing_week_batch FROM qa.recon_batch_gaps")
    b["policy_totals"] = q(con, """
        SELECT policy_id, COUNT(*) AS claims, ROUND(SUM(avoidable_allowed)) AS avoidable
        FROM marts.claim_policy_flags WHERE policy_id IS NOT NULL GROUP BY 1 ORDER BY 1""")
    # avoidable PMPM by policy, pre vs post, per client (completed months < Jun 2026)
    b["policy_by_client"] = q(con, """
        WITH cmm AS (
            SELECT client_id, service_month, ANY_VALUE(program_period) AS program_period, MAX(member_months) AS mm
            FROM marts.client_month_metrics WHERE service_month < DATE '2026-06-01' GROUP BY 1, 2),
        mm AS (   -- P02 is only evaluable from 2025-01, so its denominator uses those months only
            SELECT client_id, program_period, FALSE AS p02, SUM(mm) AS mm FROM cmm GROUP BY 1, 2
            UNION ALL
            SELECT client_id, program_period, TRUE, SUM(mm) FROM cmm WHERE service_month >= DATE '2025-01-01' GROUP BY 1, 2),
        av AS (
            SELECT f.client_id, CASE WHEN c.go_live_date IS NULL THEN 'Not live'
                   WHEN f.service_month >= CAST(c.go_live_date AS DATE) THEN 'Post go-live' ELSE 'Pre go-live' END AS program_period,
                   f.policy_id, SUM(f.avoidable_allowed) AS av
            FROM marts.claim_policy_flags f JOIN raw.clients c USING (client_id)
            WHERE f.policy_id IS NOT NULL AND f.service_month < DATE '2026-06-01'
              AND (f.policy_id <> 'P02' OR f.service_month >= DATE '2025-01-01')
            GROUP BY 1, 2, 3)
        SELECT av.client_id, av.program_period, av.policy_id, ROUND(av.av / mm.mm, 4) AS avoidable_pmpm
        FROM av JOIN mm ON mm.client_id = av.client_id AND mm.program_period = av.program_period
                       AND mm.p02 = (av.policy_id = 'P02')
        ORDER BY 1, 2, 3""")
    b["category_mix"] = q(con, """
        SELECT test_category, ROUND(SUM(net_allowed)) AS allowed, COUNT(*) AS claims
        FROM marts.claim_policy_flags GROUP BY 1 ORDER BY 2 DESC""")
    b["clients"] = q(con, "SELECT client_id, client_name, line_of_business, CAST(go_live_date AS VARCHAR) AS go_live_date FROM raw.clients")
    b["contracts"] = q(con, "SELECT * FROM raw.client_contracts")
    sem = subprocess.run([sys.executable, str(ROOT / "src" / "ai" / "verify_semantic_queries.py")],
                         capture_output=True, text=True)
    b["semantic_checks"] = [line for line in sem.stdout.splitlines() if line.startswith("[")]
    (SITE / "bundle.json").write_text(json.dumps(b, default=str))
    print("bundle.json", round((SITE / "bundle.json").stat().st_size / 1024), "KB")


if __name__ == "__main__":
    main()

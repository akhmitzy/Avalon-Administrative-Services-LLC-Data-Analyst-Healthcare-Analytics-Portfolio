"""
ELT pipeline: raw files -> RAW -> STAGING -> QA gates -> MARTS.

Locally the warehouse is a DuckDB file so the whole project runs on a laptop / in CI.
The SQL in /sql is written to be Snowflake-compatible; /snowflake has the production
equivalents (DDL, stored procedure, scheduled TASK).

Usage:
    python src/pipeline/run_pipeline.py            # full refresh
    python src/pipeline/run_pipeline.py --strict   # also fail on WARN checks that exceed thresholds
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "warehouse" / "lab_analytics.duckdb"
SQL = ROOT / "sql"
RAW = ROOT / "data" / "raw"
REF = ROOT / "data" / "reference"
REPORTS = ROOT / "outputs" / "reports"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("pipeline")


class QualityGateError(RuntimeError):
    pass


def load_raw(con: duckdb.DuckDBPyConnection) -> dict:
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    counts = {}
    sources = {
        "claims_lab": f"read_parquet('{RAW / 'claims_lab.parquet'}')",
        "members": f"read_parquet('{RAW / 'members.parquet'}')",
        "eligibility": f"read_parquet('{RAW / 'eligibility.parquet'}')",
        "finance_gl_lab_paid": f"read_csv_auto('{RAW / 'finance_gl_lab_paid.csv'}')",
        "clients": f"read_csv_auto('{REF / 'clients.csv'}')",
        "client_contracts": f"read_csv_auto('{REF / 'client_contracts.csv'}')",
        "test_catalog": f"read_csv_auto('{REF / 'test_catalog.csv'}', types={{'cpt_code': 'VARCHAR'}})",
        "icd10_clinical_map": f"read_csv_auto('{REF / 'icd10_clinical_map.csv'}')",
        "genetic_test_indications": f"read_csv_auto('{REF / 'genetic_test_indications.csv'}', types={{'cpt_code': 'VARCHAR'}})",
        "lab_network": f"read_csv_auto('{REF / 'lab_network.csv'}')",
    }
    for table, src in sources.items():
        con.execute(f"CREATE OR REPLACE TABLE raw.{table} AS SELECT * FROM {src}")
        counts[table] = con.execute(f"SELECT COUNT(*) FROM raw.{table}").fetchone()[0]
        log.info("loaded raw.%-26s %10s rows", table, f"{counts[table]:,}")
    return counts


def run_sql_dir(con: duckdb.DuckDBPyConnection, folder: str, schema: str) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for f in sorted((SQL / folder).glob("*.sql")):
        t0 = time.perf_counter()
        con.execute(f.read_text())
        log.info("ran %-40s %6.2fs", f"{folder}/{f.name}", time.perf_counter() - t0)


def quality_gate(con: duckdb.DuckDBPyConnection, strict: bool) -> list[dict]:
    rows = con.execute(
        "SELECT check_name, severity, failing_rows, fail_rate, max_fail_rate, status "
        "FROM qa.dq_check_results ORDER BY severity, check_name"
    ).fetchall()
    results = [dict(zip(["check", "severity", "failing_rows", "fail_rate", "max_fail_rate", "status"], r)) for r in rows]
    for r in results:
        lvl = logging.INFO if r["status"].startswith("PASS") else logging.WARNING
        log.log(lvl, "DQ %-32s %-8s %-22s rows=%s", r["check"], r["severity"], r["status"], f"{r['failing_rows']:,}")
    blockers = [r for r in results if r["status"] == "FAIL" and (r["severity"] == "BLOCKER" or strict)]
    if blockers:
        raise QualityGateError(f"{len(blockers)} blocking data-quality check(s) failed: {[b['check'] for b in blockers]}")
    return results


def write_dq_report(con, raw_counts: dict, dq: list[dict]) -> Path:
    recon = con.execute(
        "SELECT client_id, paid_month, gl_paid, claims_paid, variance, variance_pct, recon_status "
        "FROM qa.recon_claims_vs_gl WHERE recon_status <> 'RECONCILED' ORDER BY ABS(variance) DESC"
    ).fetchall()
    gaps = con.execute("SELECT * FROM qa.recon_batch_gaps ORDER BY client_id, paid_month").fetchall()
    tot = con.execute(
        "SELECT COUNT(*), SUM(CASE WHEN recon_status='RECONCILED' THEN 1 ELSE 0 END), "
        "SUM(gl_paid)::DOUBLE, SUM(claims_paid)::DOUBLE FROM qa.recon_claims_vs_gl"
    ).fetchone()
    valid = con.execute(
        "SELECT SUM(CASE WHEN is_analytic_valid THEN 1 ELSE 0 END), COUNT(*) FROM staging.stg_claims_lab"
    ).fetchone()
    lines = [
        "# Data Quality & Reconciliation Report",
        f"_Run: {datetime.now():%Y-%m-%d %H:%M} | claims paid through 2026-06-30_",
        "",
        f"* Raw claim lines received: **{raw_counts['claims_lab']:,}**",
        f"* Lines after de-duplication: **{valid[1]:,}**; analytically valid: **{valid[0]:,}** "
        f"({valid[0] / valid[1]:.2%}); quarantined: **{valid[1] - valid[0]:,}**",
        f"* Client-months reconciled to Finance GL: **{tot[1]} of {tot[0]}** "
        f"(GL ${tot[2]:,.0f} vs claims ${tot[3]:,.0f}; net variance ${tot[3] - tot[2]:,.0f})",
        "",
        "## Automated checks",
        "| Check | Severity | Failing rows | Fail rate | Threshold | Status |",
        "|---|---|---:|---:|---:|---|",
    ]
    for r in dq:
        lines.append(f"| {r['check']} | {r['severity']} | {r['failing_rows']:,} | {r['fail_rate']:.4%} | "
                     f"{r['max_fail_rate']:.2%} | {r['status']} |")
    lines += ["", "## Reconciliation exceptions (claims extract vs Finance GL)",
              "| Client | Paid month | GL paid | Claims paid | Variance | Var % | Status |",
              "|---|---|---:|---:|---:|---:|---|"]
    for r in recon:
        lines.append(f"| {r[0]} | {r[1]:%Y-%m} | ${r[2]:,.2f} | ${r[3]:,.2f} | ${r[4]:,.2f} | "
                     f"{(r[5] or 0):.2%} | {r[6]} |")
    if not recon:
        lines.append("| - | - | - | - | - | - | all reconciled |")
    lines += ["", "## Batch gaps behind exceptions"]
    lines += [f"* {g[0]} {g[1]:%Y-%m}: weekly batch **{g[2]}** not received" for g in gaps] or ["* none"]
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / "data_quality_report.md"
    out.write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    con = duckdb.connect(str(DB_PATH))
    try:
        raw_counts = load_raw(con)
        run_sql_dir(con, "staging", "staging")
        run_sql_dir(con, "quality", "qa")
        dq = quality_gate(con, args.strict)
        run_sql_dir(con, "marts", "marts")
        report = write_dq_report(con, raw_counts, dq)
        con.execute("CREATE TABLE IF NOT EXISTS qa.pipeline_runs (run_ts TIMESTAMP, status VARCHAR, seconds DOUBLE, detail VARCHAR)")
        con.execute("INSERT INTO qa.pipeline_runs VALUES (CURRENT_TIMESTAMP, 'SUCCESS', ?, ?)",
                    [time.perf_counter() - t0, json.dumps(raw_counts)])
        log.info("pipeline succeeded in %.1fs -> %s", time.perf_counter() - t0, report.relative_to(ROOT))
        return 0
    except QualityGateError as e:
        log.error(str(e))
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())

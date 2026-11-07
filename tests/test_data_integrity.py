"""Integration tests against the built warehouse (run after the pipeline)."""
import json
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "warehouse" / "lab_analytics.duckdb"
pytestmark = pytest.mark.skipif(not DB.exists(), reason="run the pipeline first")


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(str(DB), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def injected():
    return json.loads((ROOT / "data" / "raw" / "_injected_defects.json").read_text())


def test_duplicates_removed_exactly(con, injected):
    raw = con.execute("SELECT COUNT(*) FROM raw.claims_lab").fetchone()[0]
    stg = con.execute("SELECT COUNT(*) FROM staging.stg_claims_lab").fetchone()[0]
    assert raw - stg == injected["exact_duplicates"]


def test_reconciliation_finds_the_missing_batch(con, injected):
    rows = con.execute("SELECT client_id, paid_month, variance FROM qa.recon_claims_vs_gl "
                       "WHERE recon_status = 'INVESTIGATE'").fetchall()
    assert len(rows) == 1
    client, month, variance = rows[0]
    assert client == "C02" and str(month).startswith("2026-03")
    assert float(variance) == pytest.approx(-injected["missing_batch_paid"], abs=0.05)
    gap = con.execute("SELECT missing_week_batch FROM qa.recon_batch_gaps").fetchall()
    assert gap == [("W2",)]


def test_net_claims_have_one_row_per_claim(con):
    dupes = con.execute("SELECT COUNT(*) - COUNT(DISTINCT claim_id) FROM staging.stg_claims_net").fetchone()[0]
    assert dupes == 0


def test_reversals_net_to_replacement_amount(con):
    # adjusted claims: original + reversal + re-pay -> net equals the re-pay line
    bad = con.execute("""
        SELECT COUNT(*) FROM staging.stg_claims_net n
        JOIN staging.stg_claims_lab l ON l.claim_id = n.claim_id AND l.line_no = 3
        WHERE n.was_adjusted AND n.line_count = 3 AND ABS(n.net_allowed - l.allowed_amount) > 0.011""").fetchone()[0]
    assert bad == 0


def test_member_months_not_double_counted(con):
    a = con.execute("SELECT SUM(mm) FROM (SELECT client_id, service_month, MAX(member_months) mm "
                    "FROM marts.client_month_metrics GROUP BY 1, 2)").fetchone()[0]
    b = con.execute("SELECT COUNT(*) FROM staging.stg_member_months "
                    "WHERE (client_id, service_month) IN (SELECT DISTINCT client_id, service_month FROM marts.client_month_metrics)").fetchone()[0]
    assert a == b


def test_no_quarantined_lines_reach_marts(con):
    n = con.execute("""SELECT COUNT(*) FROM marts.claim_policy_flags f
                       JOIN staging.stg_claims_lab l USING (claim_id) WHERE NOT l.is_analytic_valid""").fetchone()[0]
    assert n == 0

"""Unit tests for the policy-adherence SQL on small hand-built fixtures (no warehouse needed)."""
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY_SQL = (ROOT / "sql" / "marts" / "01_policy_flags.sql").read_text()

COLS = ["claim_id", "client_id", "member_id", "service_date", "cpt_code", "clinical_category",
        "test_category", "policy_group", "lab_id", "in_network", "age_at_service", "net_allowed"]


def run(claims: list[tuple]) -> pd.DataFrame:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw; CREATE SCHEMA staging; CREATE SCHEMA marts;")
    for t in ["test_catalog", "genetic_test_indications"]:
        con.execute(f"CREATE TABLE raw.{t} AS SELECT * FROM read_csv_auto('{ROOT / 'data' / 'reference' / (t + '.csv')}', "
                    f"types={{'cpt_code': 'VARCHAR'}})")
    df = pd.DataFrame(claims, columns=COLS)
    df["service_date"] = pd.to_datetime(df["service_date"]).dt.date
    df["service_month"] = pd.to_datetime(df["service_date"]).dt.to_period("M").dt.to_timestamp().dt.date
    df["net_paid"] = df["net_allowed"]
    df["first_paid_date"] = df["service_date"]
    df["is_payable"] = True
    con.register("fx", df)
    con.execute("CREATE TABLE staging.stg_claims_net AS SELECT * FROM fx")
    con.execute(POLICY_SQL)
    return con.execute("SELECT claim_id, policy_id, avoidable_allowed FROM marts.claim_policy_flags").df().set_index("claim_id")


def c(cid, member, date, cpt, cat, test_cat="Routine", pg=None, lab="L001", in_net=True, age=50, amt=30.0, client="C01"):
    return (cid, client, member, date, cpt, cat, test_cat, pg, lab, in_net, age, amt)


def test_vitamin_d_screening_flagged_but_deficiency_is_not():
    r = run([c("a", "m1", "2025-03-01", "82306", "screening", pg="P01_VITD_SCREEN"),
             c("b", "m2", "2025-03-01", "82306", "vitd_deficiency", pg="P01_VITD_SCREEN")])
    assert r.loc["a", "policy_id"] == "P01" and r.loc["a", "avoidable_allowed"] == 30.0
    assert pd.isna(r.loc["b", "policy_id"])


def test_hba1c_fifth_test_in_12_months_for_diabetic():
    dates = ["2025-01-10", "2025-03-10", "2025-05-10", "2025-07-10", "2025-09-10"]
    r = run([c(f"h{i}", "m1", d, "83036", "diabetes", pg="P02_HBA1C_FREQ") for i, d in enumerate(dates)])
    assert list(r.sort_index()["policy_id"].fillna("-")) == ["-", "-", "-", "-", "P02"]


def test_hba1c_not_evaluated_without_full_lookback():
    r = run([c("x1", "m1", "2024-03-01", "83036", "general", pg="P02_HBA1C_FREQ"),
             c("x2", "m1", "2024-06-01", "83036", "general", pg="P02_HBA1C_FREQ")])
    assert r["policy_id"].isna().all()


def test_definitive_drug_test_requires_presumptive_within_30_days():
    r = run([c("p", "m1", "2025-02-01", "80307", "substance_use", test_cat="Toxicology"),
             c("d_ok", "m1", "2025-02-20", "G0483", "substance_use", test_cat="Toxicology", pg="P04_DEF_DRUG", amt=246.0),
             c("d_bad", "m1", "2025-04-15", "G0483", "substance_use", test_cat="Toxicology", pg="P04_DEF_DRUG", amt=246.0)])
    assert pd.isna(r.loc["d_ok", "policy_id"])
    assert r.loc["d_bad", "policy_id"] == "P04"
    # avoidable = price gap to a 1-7 class definitive test (114 / 246 of the allowed)
    assert r.loc["d_bad", "avoidable_allowed"] == pytest.approx(246.0 * (1 - 114 / 246), abs=0.01)


def test_same_day_duplicate_flags_second_claim_only():
    r = run([c("s1", "m1", "2025-05-05", "80053", "screening"), c("s2", "m1", "2025-05-05", "80053", "screening")])
    assert pd.isna(r.loc["s1", "policy_id"]) and r.loc["s2", "policy_id"] == "P03"


def test_genetic_indication_and_crc_age_rule():
    g = dict(test_cat="Genetic & Molecular", pg="P06_GENETIC_INDICATION", amt=1800.0)
    r = run([c("brca_ok", "m1", "2025-01-01", "81162", "oncology", **g),
             c("brca_bad", "m2", "2025-01-01", "81162", "general", **g),
             c("crc_young", "m3", "2025-01-01", "81528", "crc_screening", age=38, **g),
             c("crc_ok", "m4", "2025-01-01", "81528", "crc_screening", age=60, **g)])
    assert pd.isna(r.loc["brca_ok", "policy_id"]) and pd.isna(r.loc["crc_ok", "policy_id"])
    assert r.loc["brca_bad", "policy_id"] == "P06" and r.loc["crc_young", "policy_id"] == "P06"


def test_out_of_network_priced_against_in_network_median():
    r = run([c("in1", "m1", "2025-01-05", "80053", "screening", amt=10.0),
             c("in2", "m2", "2025-01-06", "80053", "screening", amt=12.0),
             c("oon", "m3", "2025-01-07", "80053", "screening", lab="L012", in_net=False, amt=25.0)])
    assert r.loc["oon", "policy_id"] == "P07"
    assert r.loc["oon", "avoidable_allowed"] == pytest.approx(25.0 - 11.0)

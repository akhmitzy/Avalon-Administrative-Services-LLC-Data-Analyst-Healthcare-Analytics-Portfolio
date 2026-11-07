"""
Guardrail for AI-enabled analytics: every verified query in the semantic model is
executed against the warehouse and compared with an independent pandas calculation.

Why: natural-language querying is only trustworthy if the governed answers it learns from
are right. A common failure is summing member_months across category rows (PMPM comes out
~5x too low); test 1 below also proves the naive version would be caught.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "semantic" / "lab_analytics.yaml"
DB = ROOT / "warehouse" / "lab_analytics.duckdb"


def to_physical(sql: str, model: dict) -> str:
    for t in model["tables"]:
        b = t["base_table"]
        sql = re.sub(rf"\b__{t['name']}\b", f"{b['schema'].lower()}.{b['table'].lower()}", sql)
    return sql


def independent_answers(con) -> dict[str, pd.DataFrame]:
    cm = con.execute("SELECT * FROM marts.client_month_metrics").df()
    fl = con.execute("SELECT * FROM marts.claim_policy_flags").df()
    cm["service_month"] = pd.to_datetime(cm["service_month"])
    fl["service_date"] = pd.to_datetime(fl["service_date"])
    out = {}
    y = cm[cm["service_month"].dt.year == 2025]
    spend = y.groupby("client_name")["allowed"].sum()
    mm = y.groupby(["client_name", "service_month"])["member_months"].max().groupby("client_name").sum()
    out["pmpm_by_client_2025"] = (spend / mm).round(2).rename("allowed_pmpm").reset_index()
    p = fl[(fl["client_id"] == "C05") & fl["policy_id"].notna()
           & fl["service_date"].between("2025-06-01", "2026-05-31")]
    out["avoidable_by_policy_prospect"] = p.groupby("policy_id")["avoidable_allowed"].sum().round(0).reset_index()
    q = cm[cm["program_period"].isin(["Pre go-live", "Post go-live"])].groupby(["client_name", "program_period"])
    out["oon_share_pre_post"] = (q["oon_allowed"].sum() / q["allowed"].sum()).round(4).rename("oon_share").reset_index()
    out["top_labs_avoidable"] = (fl.groupby("lab_id")["avoidable_allowed"].sum().round(0)
                                 .sort_values(ascending=False).head(5).reset_index())
    # the trap: naive PMPM that sums member months across category rows
    naive = (spend / y.groupby("client_name")["member_months"].sum()).round(2)
    out["_naive_pmpm"] = naive.rename("allowed_pmpm").reset_index()
    return out


def same(a: pd.DataFrame, b: pd.DataFrame, key: list[str], val: str) -> bool:
    m = a.merge(b, on=key, suffixes=("_sql", "_ind"))
    return len(m) == len(a) == len(b) and np.allclose(m[f"{val}_sql"].astype(float), m[f"{val}_ind"].astype(float), atol=1.0)


def main() -> int:
    model = yaml.safe_load(MODEL.read_text())
    con = duckdb.connect(str(DB), read_only=True)
    truth = independent_answers(con)
    checks = {
        "pmpm_by_client_2025": (["client_name"], "allowed_pmpm"),
        "avoidable_by_policy_prospect": (["policy_id"], "avoidable_allowed"),
        "oon_share_pre_post": (["client_name", "program_period"], "oon_share"),
        "top_labs_avoidable": (["lab_id"], "avoidable_allowed"),
    }
    failures = 0
    for vq in model["verified_queries"]:
        got = con.execute(to_physical(vq["sql"], model)).df()
        key, val = checks[vq["name"]]
        ok = same(got, truth[vq["name"]], key, val)
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {vq['name']}: {vq['question']}")
    # prove the test has teeth: the naive PMPM must NOT match
    caught = not same(truth["_naive_pmpm"], truth["pmpm_by_client_2025"], ["client_name"], "allowed_pmpm")
    print(f"[{'PASS' if caught else 'FAIL'}] guardrail detects member-month double counting")
    failures += not caught
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

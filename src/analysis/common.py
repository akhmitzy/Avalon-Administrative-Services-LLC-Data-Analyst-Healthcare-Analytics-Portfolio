"""Shared helpers for analysis scripts."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "warehouse" / "lab_analytics.duckdb"
OUT = ROOT / "outputs"
SITE = OUT / "site_data"
REPORTS = OUT / "reports"
VALUATION = pd.Timestamp("2026-06-30")


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def write_json(name: str, obj) -> Path:
    SITE.mkdir(parents=True, exist_ok=True)
    p = SITE / f"{name}.json"

    def default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else float(o)
        if isinstance(o, (pd.Timestamp,)):
            return o.strftime("%Y-%m-%d")
        if isinstance(o, (np.bool_,)):
            return bool(o)
        raise TypeError(type(o))

    p.write_text(json.dumps(obj, default=default, indent=1))
    return p


def client_month_completed(con) -> pd.DataFrame:
    """Client x month total allowed, completed for IBNR using chain-ladder factors."""
    df = con.execute("""
        SELECT m.client_id, m.client_name, m.line_of_business, m.service_month,
               ANY_VALUE(m.program_period) AS program_period,
               MAX(m.member_months) AS member_months,
               SUM(m.allowed) AS allowed_paid_to_date,
               SUM(m.avoidable_allowed) AS avoidable_paid_to_date,
               SUM(m.oon_allowed) AS oon_paid_to_date,
               SUM(m.tests) AS tests
        FROM marts.client_month_metrics m
        GROUP BY m.client_id, m.client_name, m.line_of_business, m.service_month
    """).df()
    cf = con.execute("SELECT service_month, completion_factor FROM marts.completion_factors").df()
    df = df.merge(cf, on="service_month", how="left")
    df["completion_factor"] = df["completion_factor"].fillna(1.0)
    for c in ["allowed", "avoidable", "oon"]:
        df[f"{c}_completed"] = df[f"{c}_paid_to_date"] * df["completion_factor"]
    df["pmpm"] = df["allowed_completed"] / df["member_months"]
    df["service_month"] = pd.to_datetime(df["service_month"])
    return df.sort_values(["client_id", "service_month"]).reset_index(drop=True)

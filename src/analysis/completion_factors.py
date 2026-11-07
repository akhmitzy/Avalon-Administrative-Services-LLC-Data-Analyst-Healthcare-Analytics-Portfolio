"""
Chain-ladder completion factors (IBNR) for lab claims.

Recent service months are under-reported because claims are still being paid. Before
any trend, savings or forecast work, paid-to-date dollars are "completed":

    completed = paid_to_date x completion_factor(months of development)

Age-to-age factors are volume-weighted over the most recent 12 incurred months that
have the next development period observed. Valuation: claims paid through 2026-06-30.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import REPORTS, VALUATION, connect, write_json

MAX_LAG = 9  # months; >99.9% of lab dollars are paid within 8 months


def build_triangle(con) -> pd.DataFrame:
    tri = con.execute("SELECT service_month, lag_months, allowed_incremental FROM marts.paid_lag_triangle").df()
    tri["service_month"] = pd.to_datetime(tri["service_month"])
    tri["lag_months"] = tri["lag_months"].clip(upper=MAX_LAG)
    wide = tri.pivot_table(index="service_month", columns="lag_months", values="allowed_incremental",
                           aggfunc="sum").reindex(columns=range(MAX_LAG + 1)).fillna(0.0)
    cum = wide.cumsum(axis=1)
    # mask cells not yet observable at valuation
    val_month = VALUATION.to_period("M")
    for sm in cum.index:
        observed = (val_month - sm.to_period("M")).n
        cum.loc[sm, [c for c in cum.columns if c > observed]] = np.nan
    return cum


def age_to_age(cum: pd.DataFrame, window: int = 12) -> pd.Series:
    f = {}
    for k in range(MAX_LAG):
        pair = cum[[k, k + 1]].dropna().tail(window)
        f[k] = pair[k + 1].sum() / pair[k].sum() if len(pair) else 1.0
    return pd.Series(f)


def main() -> None:
    con = connect()
    cum = build_triangle(con)
    ata = age_to_age(cum)
    # cumulative development factor to ultimate from each lag
    cdf = {k: float(np.prod(ata.loc[k:].values)) for k in range(MAX_LAG)}
    cdf[MAX_LAG] = 1.0
    val_month = VALUATION.to_period("M")
    rows = []
    for sm in cum.index:
        lag = min((val_month - sm.to_period("M")).n, MAX_LAG)
        rows.append({"service_month": sm, "months_of_development": lag,
                     "completion_factor": cdf[lag], "pct_complete": 1 / cdf[lag]})
    cf = pd.DataFrame(rows)
    con.register("cf_df", cf)
    con.execute("CREATE OR REPLACE TABLE marts.completion_factors AS SELECT * FROM cf_df")

    recent = cf.tail(6)
    md = ["# Completion factors (chain-ladder, valuation 2026-06-30)", "",
          "| Service month | Months of development | % complete | Completion factor |", "|---|---:|---:|---:|"]
    for r in recent.itertuples():
        md.append(f"| {r.service_month:%Y-%m} | {r.months_of_development} | {r.pct_complete:.1%} | {r.completion_factor:.4f} |")
    md += ["", "Age-to-age factors (volume-weighted, last 12 incurred months):", "",
           "| Lag | Factor |", "|---:|---:|"] + [f"| {k}->{k + 1} | {v:.4f} |" for k, v in ata.items()]
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "completion_factors.md").write_text("\n".join(md) + "\n")
    write_json("completion_factors", {
        "recent": [{"month": r.service_month.strftime("%Y-%m"), "dev": int(r.months_of_development),
                    "pct_complete": round(r.pct_complete, 4), "cf": round(r.completion_factor, 4)}
                   for r in recent.itertuples()],
        "age_to_age": {str(k): round(v, 4) for k, v in ata.items()},
    })
    print(recent.to_string(index=False))


if __name__ == "__main__":
    main()

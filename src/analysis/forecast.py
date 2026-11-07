"""
Lab spend PMPM forecast by client, with an out-of-sample backtest.

Model (weighted least squares on log PMPM, completed for IBNR):
    log(PMPM_ct) = client level_c + program shift_c * post_ct + trend * t + month-of-year_m
  * seasonality and trend are pooled across clients (more data, more stable)
  * the program shift is client-specific and only estimated once a client has post data

Backtest: train Jan 2024 - Dec 2025, test Jan - May 2026 (June 2026 is <85% complete).
Baseline: seasonal naive (same month last year), the usual "no-model" alternative.
Forecast: refit through May 2026, project Jun 2026 - Dec 2027 at current membership.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import REPORTS, client_month_completed, connect, write_json

TRAIN_END = pd.Timestamp("2025-12-01")
LAST_COMPLETE = pd.Timestamp("2026-05-01")
HORIZON_END = pd.Timestamp("2027-12-01")


class PmpmModel:
    def fit(self, df: pd.DataFrame) -> "PmpmModel":
        self.clients = sorted(df["client_id"].unique())
        self.post_clients = sorted(df.loc[df["post"], "client_id"].unique())
        self.t0 = pd.Timestamp("2024-01-01")
        X = self._design(df)
        w = np.sqrt(df["member_months"].to_numpy())
        y = np.log(df["pmpm"].to_numpy())
        self.beta, *_ = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)
        r = y - X @ self.beta
        self.sigma = float(np.sqrt(np.average(r ** 2, weights=df["member_months"])))
        return self

    def _design(self, d: pd.DataFrame) -> np.ndarray:
        nc, npc = len(self.clients), len(self.post_clients)
        X = np.zeros((len(d), nc + npc + 1 + 11))
        rows = np.arange(len(d))
        X[rows, d["client_id"].map({c: i for i, c in enumerate(self.clients)}).to_numpy()] = 1
        for j, c in enumerate(self.post_clients):
            X[:, nc + j] = ((d["client_id"] == c) & d["post"]).to_numpy()
        months = ((d["service_month"].dt.year - self.t0.year) * 12 + d["service_month"].dt.month - 1).to_numpy()
        X[:, nc + npc] = months / 12.0
        moy = d["service_month"].dt.month.to_numpy()
        for m in range(2, 13):
            X[:, nc + npc + m - 1] = moy == m
        return X

    def predict(self, d: pd.DataFrame) -> np.ndarray:
        d = d.copy()
        # a client without post data in training gets no shift (cannot be learned yet)
        d["post"] = d["post"] & d["client_id"].isin(self.post_clients)
        return np.exp(self._design(d) @ self.beta)


def mape(a, f):
    a, f = np.asarray(a), np.asarray(f)
    return float(np.mean(np.abs(f - a) / a))


def main() -> None:
    con = connect()
    cm = client_month_completed(con)
    golive = con.execute("SELECT client_id, go_live_date FROM raw.clients").df()
    gl = dict(zip(golive["client_id"], pd.to_datetime(golive["go_live_date"])))
    cm["post"] = cm.apply(lambda r: pd.notna(gl[r.client_id]) and r.service_month >= gl[r.client_id], axis=1)
    cm = cm[cm["service_month"] <= LAST_COMPLETE].copy()

    # ---------------- backtest ----------------
    train, test = cm[cm["service_month"] <= TRAIN_END], cm[cm["service_month"] > TRAIN_END].copy()
    model = PmpmModel().fit(train)
    test["fcst"] = model.predict(test)
    ly = cm.assign(service_month=cm["service_month"] + pd.DateOffset(years=1))[["client_id", "service_month", "pmpm"]]
    test = test.merge(ly.rename(columns={"pmpm": "naive"}), on=["client_id", "service_month"], how="left")
    test["spend_fcst"] = test["fcst"] * test["member_months"]
    test["spend_naive"] = test["naive"] * test["member_months"]
    bt = []
    for cid, g in test.groupby("client_id"):
        bt.append({"client_id": cid, "client_name": g["client_name"].iloc[0],
                   "mape_model": mape(g["pmpm"], g["fcst"]), "mape_naive": mape(g["pmpm"], g["naive"]),
                   "bias_model": float((g["spend_fcst"].sum() - g["allowed_completed"].sum()) / g["allowed_completed"].sum())})
    bt = pd.DataFrame(bt)
    port_actual = test["allowed_completed"].sum()
    port_err_model = test["spend_fcst"].sum() / port_actual - 1
    port_err_naive = test["spend_naive"].sum() / port_actual - 1

    # ---------------- forecast ----------------
    final = PmpmModel().fit(cm)
    last_mm = cm[cm["service_month"] == LAST_COMPLETE].set_index("client_id")["member_months"]
    fut_months = pd.date_range(LAST_COMPLETE + pd.DateOffset(months=1), HORIZON_END, freq="MS")
    fut = pd.DataFrame([(c, m) for c in final.clients for m in fut_months], columns=["client_id", "service_month"])
    fut["post"] = fut.apply(lambda r: pd.notna(gl[r.client_id]) and r.service_month >= gl[r.client_id], axis=1)
    fut["member_months"] = fut["client_id"].map(last_mm)
    fut["pmpm_fcst"] = final.predict(fut)
    z80 = 1.2816
    fut["pmpm_lo"] = fut["pmpm_fcst"] * np.exp(-z80 * final.sigma)
    fut["pmpm_hi"] = fut["pmpm_fcst"] * np.exp(z80 * final.sigma)
    fut["spend_fcst"] = fut["pmpm_fcst"] * fut["member_months"]
    names = cm.drop_duplicates("client_id").set_index("client_id")["client_name"]
    y27 = fut[fut["service_month"].dt.year == 2027].groupby("client_id").agg(
        spend_2027=("spend_fcst", "sum"), pmpm_2027=("pmpm_fcst", "mean"), mm_2027=("member_months", "sum")).reset_index()
    y27["client_name"] = y27["client_id"].map(names)
    # annual band from out-of-sample evidence, not model residuals: monthly errors are
    # autocorrelated, so the residual sigma / sqrt(12) would be far too narrow.
    annual_rmse = float(np.sqrt(np.mean(bt["bias_model"] ** 2)))
    y27["spend_2027_lo"] = y27["spend_2027"] * (1 - z80 * annual_rmse)
    y27["spend_2027_hi"] = y27["spend_2027"] * (1 + z80 * annual_rmse)
    trend = float(np.exp(final.beta[len(final.clients) + len(final.post_clients)]) - 1)

    series = {}
    for cid in final.clients:
        h = cm[cm["client_id"] == cid]
        f = fut[fut["client_id"] == cid]
        bt_c = test[test["client_id"] == cid]
        series[cid] = {
            "hist": [{"m": r.service_month.strftime("%Y-%m"), "v": round(r.pmpm, 3)} for r in h.itertuples()],
            "backtest": [{"m": r.service_month.strftime("%Y-%m"), "v": round(r.fcst, 3)} for r in bt_c.itertuples()],
            "fcst": [{"m": r.service_month.strftime("%Y-%m"), "v": round(r.pmpm_fcst, 3),
                      "lo": round(r.pmpm_lo, 3), "hi": round(r.pmpm_hi, 3)} for r in f.itertuples()],
        }
    write_json("forecast", {
        "underlying_trend": trend, "sigma": final.sigma, "annual_backtest_rmse": annual_rmse,
        "backtest": bt.round(4).to_dict(orient="records"),
        "portfolio_backtest_error_model": port_err_model, "portfolio_backtest_error_naive": port_err_naive,
        "y2027": y27.round(2).to_dict(orient="records"), "series": series,
    })
    md = ["# Lab spend forecast", "",
          f"Underlying annual PMPM trend (pooled, ex-program): **{trend:+.1%}**", "",
          "## Backtest (train through Dec 2025, test Jan-May 2026)", "",
          f"Portfolio spend error: model **{port_err_model:+.1%}** vs seasonal-naive **{port_err_naive:+.1%}**", "",
          "| Client | MAPE model | MAPE naive | Spend bias (model) |", "|---|---:|---:|---:|"]
    md += [f"| {r.client_name} | {r.mape_model:.1%} | {r.mape_naive:.1%} | {r.bias_model:+.1%} |" for r in bt.itertuples()]
    md += ["", "## 2027 projection (membership held at May 2026)", "",
           "| Client | Avg PMPM | Allowed spend | 80% range |", "|---|---:|---:|---|"]
    md += [f"| {r.client_name} | ${r.pmpm_2027:.2f} | ${r.spend_2027:,.0f} | ${r.spend_2027_lo:,.0f} - ${r.spend_2027_hi:,.0f} |"
           for r in y27.itertuples()]
    (REPORTS / "forecast.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()

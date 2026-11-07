"""
Program impact, client value (ROI) and performance-guarantee reconciliation.

Question: how much lower is each client's lab spend because the program went live,
after removing seasonality, trend and plan-specific cost levels?

Method A - imputation difference-in-differences (staggered go-live)
  * Fit log(PMPM) = client level + calendar-month effect, using ONLY untreated
    client-months (each client's pre-period plus the never-live plan), weighted by
    member months.
  * Predict each treated client-month's counterfactual PMPM; savings = (expected -
    actual) x member months. Fitting on untreated data only avoids the bias that
    two-way fixed effects have with staggered adoption and uneven effects.
  * Client-specific linear trends are available (TRENDS = True). Tested and rejected for
    the headline: with 3-12 pre-period months they over-extrapolate (placebo got worse).
  * Checks: (1) placebo-in-time - pretend go-live was 6 months earlier, effect should be
    ~0; (2) placebo outcome - core routine panel utilization should not move.

Method B - bottom-up policy savings
  * Avoidable (policy non-adherent) PMPM before vs after go-live. Used to explain
    where Method A's savings come from.

Months with < 85% claim completeness (June 2026) are excluded.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import REPORTS, client_month_completed, connect, write_json

MIN_COMPLETE = 0.85
TRENDS = False
CORE_PANELS = ("80053", "85025", "80061", "84443", "81001")


def fit_untreated_fe(df: pd.DataFrame, y: str, treat_col: str = "treated", trends: bool = TRENDS) -> pd.DataFrame:
    """Weighted LS of log(y) on client + month dummies using untreated rows; predict all rows."""
    train = df[~df[treat_col]]
    clients = sorted(df["client_id"].unique())
    months = sorted(df["service_month"].unique())

    nc, nm = len(clients), len(months)

    def design(d):
        # client intercepts + client-specific linear trends + common calendar-month effects
        X = np.zeros((len(d), 2 * nc + nm - 2))
        ci = d["client_id"].map({c: i for i, c in enumerate(clients)}).to_numpy()
        mi = d["service_month"].map({m: i for i, m in enumerate(months)}).to_numpy()
        rows = np.arange(len(d))
        X[rows, ci] = 1
        t = (mi - (nm - 1) / 2) / 12.0
        trend_cols = nc + ci
        keep = (ci > 0) & trends   # first client's trend is absorbed by the month effects
        X[rows[keep], trend_cols[keep] - 1] = t[keep]
        mask = mi > 0
        X[rows[mask], 2 * nc - 1 + mi[mask] - 1] = 1
        return X

    w = np.sqrt(train["member_months"].to_numpy())
    beta, *_ = np.linalg.lstsq(design(train) * w[:, None], np.log(train[y].to_numpy()) * w, rcond=None)
    out = df.copy()
    out["expected"] = np.exp(design(df) @ beta)
    resid = np.log(train[y].to_numpy()) - design(train) @ beta
    out.attrs["resid_sd"] = float(np.sqrt(np.average(resid ** 2, weights=train["member_months"])))
    return out


def main() -> None:
    con = connect()
    cm = client_month_completed(con)
    cf = con.execute("SELECT service_month, pct_complete FROM marts.completion_factors").df()
    cf["service_month"] = pd.to_datetime(cf["service_month"])
    cm = cm.merge(cf, on="service_month")
    cm = cm[cm["pct_complete"] >= MIN_COMPLETE].copy()
    clients = con.execute("SELECT * FROM raw.clients").df()
    contracts = con.execute("SELECT * FROM raw.client_contracts").df()
    golive = dict(zip(clients["client_id"], pd.to_datetime(clients["go_live_date"])))
    cm["go_live"] = cm["client_id"].map(golive)
    cm["treated"] = cm["go_live"].notna() & (cm["service_month"] >= cm["go_live"])

    # ---------------- Method A ----------------
    est = fit_untreated_fe(cm, "pmpm")
    resid_sd = est.attrs["resid_sd"]
    post = est[est["treated"]].copy()
    post["savings"] = (post["expected"] - post["pmpm"]) * post["member_months"]

    # placebo-in-time: fake go-live 6 months early, drop true post period
    pl = cm[~cm["treated"]].copy()
    pl["fake_live"] = pl["go_live"] - pd.DateOffset(months=6)
    pl["treated_fake"] = pl["go_live"].notna() & (pl["service_month"] >= pl["fake_live"])
    pl_est = fit_untreated_fe(pl.assign(treated=pl["treated_fake"]), "pmpm")
    pl_post = pl_est[pl_est["treated"]]
    placebo_effect = 1 - (pl_post["pmpm"] * pl_post["member_months"]).sum() / (pl_post["expected"] * pl_post["member_months"]).sum()

    # placebo outcome: core panel utilization per 1,000
    core = con.execute(f"""
        SELECT client_id, service_month, COUNT(*) AS core_tests
        FROM marts.claim_policy_flags WHERE cpt_code IN {CORE_PANELS} GROUP BY 1, 2""").df()
    core["service_month"] = pd.to_datetime(core["service_month"])
    cu = cm.merge(core, on=["client_id", "service_month"])
    cu["core_util"] = cu["core_tests"] * cu["completion_factor"] / cu["member_months"] * 12000
    cu_est = fit_untreated_fe(cu, "core_util")
    cu_post = cu_est[cu_est["treated"]]
    core_effect = 1 - (cu_post["core_util"] * cu_post["member_months"]).sum() / (cu_post["expected"] * cu_post["member_months"]).sum()

    # ---------------- per-client value + guarantee ----------------
    rows = []
    for cid, g in post.groupby("client_id"):
        c = contracts.set_index("client_id").loc[cid]
        pre = cm[(cm["client_id"] == cid) & (~cm["treated"])]
        mm = g["member_months"].sum()
        exp_spend = (g["expected"] * g["member_months"]).sum()
        act_spend = g["allowed_completed"].sum()
        sav = exp_spend - act_spend
        fees = c["fee_pmpm"] * mm
        roi = sav / fees
        guar = c["guarantee_roi_multiple"] * fees
        shortfall = max(0.0, guar - sav)
        refund = min(shortfall, c["fees_at_risk_pct"] * fees)
        # bottom-up
        av_pre = pre["avoidable_completed"].sum() / pre["member_months"].sum()
        av_post = g["avoidable_completed"].sum() / mm
        oon_pre = pre["oon_completed"].sum() / pre["allowed_completed"].sum()
        oon_post = g["oon_completed"].sum() / act_spend
        # uncertainty band: +/- 1.96 * resid sd / sqrt(n months) on log scale
        band = 1.96 * resid_sd / np.sqrt(len(g))
        rows.append({
            "client_id": cid,
            "client_name": g["client_name"].iloc[0],
            "line_of_business": g["line_of_business"].iloc[0],
            "go_live": golive[cid].strftime("%Y-%m"),
            "post_months": int(len(g)),
            "post_member_months": int(mm),
            "expected_pmpm": exp_spend / mm,
            "actual_pmpm": act_spend / mm,
            "savings_pmpm": sav / mm,
            "savings_pct": sav / exp_spend,
            "savings_pct_low": sav / exp_spend - band,
            "savings_pct_high": sav / exp_spend + band,
            "gross_savings": sav,
            "fees": fees,
            "roi_multiple": roi,
            "guarantee_multiple": c["guarantee_roi_multiple"],
            "guarantee_met": bool(sav >= guar),
            "fee_refund_due": refund,
            "avoidable_pmpm_pre": av_pre,
            "avoidable_pmpm_post": av_post,
            "bottom_up_savings": (av_pre - av_post) * mm,
            "oon_share_pre": oon_pre,
            "oon_share_post": oon_post,
        })
    val = pd.DataFrame(rows)
    tot_sav, tot_fee = val["gross_savings"].sum(), val["fees"].sum()

    # policy-level capture rates (used by the pricing model)
    pol = con.execute("""
        SELECT client_id, service_month, policy_id, SUM(avoidable_allowed) AS avoidable
        FROM marts.claim_policy_flags WHERE policy_id IS NOT NULL GROUP BY 1, 2, 3""").df()
    pol["service_month"] = pd.to_datetime(pol["service_month"])
    pol = pol.merge(cm[["client_id", "service_month", "member_months", "completion_factor", "treated", "go_live"]],
                    on=["client_id", "service_month"])
    pol["avoidable_c"] = pol["avoidable"] * pol["completion_factor"]
    # capture = 1 - post/pre avoidable PMPM, computed within each live client (so plan mix
    # does not leak into the rate), then weighted by post-period member months.
    # P02 is only evaluable from 2025-01 (12-month lookback), so its months are restricted.
    cap_rows = []
    live = cm[cm["go_live"].notna()]
    lob = dict(zip(clients["client_id"], clients["line_of_business"]))
    for pid in sorted(pol["policy_id"].unique()):
        per_client = []
        for cid, g in live.groupby("client_id"):
            g = g[g["service_month"] >= "2025-01-01"] if pid == "P02" else g
            p = pol[(pol["policy_id"] == pid) & (pol["client_id"] == cid)]
            p = p[p["service_month"].isin(g["service_month"])]
            pre_mm, post_mm = g.loc[~g["treated"], "member_months"].sum(), g.loc[g["treated"], "member_months"].sum()
            if pre_mm == 0 or post_mm == 0:
                continue
            pre = p.loc[~p["treated"], "avoidable_c"].sum() / pre_mm
            pst = p.loc[p["treated"], "avoidable_c"].sum() / post_mm
            per_client.append((cid, lob[cid], pre, pst, post_mm))
        pc = pd.DataFrame(per_client, columns=["client_id", "lob", "pre", "post", "w"])
        pc["cap"] = 1 - pc["post"] / pc["pre"]
        ma = pc[pc["lob"] == "Medicare Advantage"]
        cap_rows.append({
            "policy_id": pid,
            "pre_pmpm": np.average(pc["pre"], weights=pc["w"]),
            "post_pmpm": np.average(pc["post"], weights=pc["w"]),
            "capture_rate": np.average(pc["cap"], weights=pc["w"]),
            "capture_min": pc["cap"].min(),
            "capture_max": pc["cap"].max(),
            "capture_ma_peer": float(ma["cap"].iloc[0]) if len(ma) else np.nan,
            "clients_used": len(pc),
        })
    capture = pd.DataFrame(cap_rows)
    capture.to_csv(REPORTS / "policy_capture_rates.csv", index=False)
    val.to_csv(REPORTS / "client_value_scorecard.csv", index=False)

    # monthly series for charts: actual vs counterfactual
    series = {}
    for cid, g in est.groupby("client_id"):
        series[cid] = [{"m": r.service_month.strftime("%Y-%m"), "actual": round(r.pmpm, 3),
                        "expected": round(r.expected, 3) if r.treated else None, "post": bool(r.treated)}
                       for r in g.itertuples()]

    summary = {
        "total_savings": tot_sav, "total_fees": tot_fee, "portfolio_roi": tot_sav / tot_fee,
        "placebo_in_time_effect": placebo_effect, "placebo_core_util_effect": core_effect,
        "resid_sd": resid_sd, "clients": val.round(4).to_dict(orient="records"),
        "capture": capture.round(4).to_dict(orient="records"), "series": series,
    }
    write_json("program_impact", summary)

    md = ["# Program impact & client value scorecard", "",
          f"Portfolio: **${tot_sav:,.0f}** estimated savings on **${tot_fee:,.0f}** fees "
          f"(**{tot_sav / tot_fee:.1f}x ROI**).", "",
          f"* Placebo-in-time (fake go-live 6 months early): effect {placebo_effect:+.2%} (expect ~0)",
          f"* Placebo outcome (core panel utilization): effect {core_effect:+.2%} (expect ~0)", "",
          "| Client | LOB | Go-live | Post months | Expected PMPM | Actual PMPM | Savings % (95% band) | Savings $ | ROI | Guarantee | Refund |",
          "|---|---|---|---:|---:|---:|---|---:|---:|---|---:|"]
    for r in val.itertuples():
        md.append(f"| {r.client_name} | {r.line_of_business} | {r.go_live} | {r.post_months} | ${r.expected_pmpm:.2f} | "
                  f"${r.actual_pmpm:.2f} | {r.savings_pct:.1%} ({r.savings_pct_low:.1%} to {r.savings_pct_high:.1%}) | "
                  f"${r.gross_savings:,.0f} | {r.roi_multiple:.1f}x | {'Met' if r.guarantee_met else 'Not met'} "
                  f"({r.guarantee_multiple:.1f}x) | ${r.fee_refund_due:,.0f} |")
    md += ["", "## Policy capture rates (live clients, pre vs post avoidable PMPM)", "",
           "| Policy | Pre PMPM | Post PMPM | Capture (wtd) | Range across clients | MA peer |", "|---|---:|---:|---:|---|---:|"]
    md += [f"| {r.policy_id} | ${r.pre_pmpm:.3f} | ${r.post_pmpm:.3f} | {r.capture_rate:.0%} | {r.capture_min:.0%} to {r.capture_max:.0%} | {r.capture_ma_peer:.0%} |" for r in capture.itertuples()]
    (REPORTS / "program_impact.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()

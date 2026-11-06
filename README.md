# Diagnostic Insights Analytics

End-to-end healthcare lab analytics for a **diagnostic benefit management** program: claims pipeline with a
quality gate and Finance reconciliation, an evidence-based policy-adherence engine, program impact and
performance-guarantee measurement, a lab spend forecast, a prospect pricing model in Excel, and a governed
semantic model for natural-language querying.

Built by **Ram Akhmitzyanovatias** (arramzis@gmail.com, www.linkedin.com/in/ram-akhmitzyanov-8479a56b) as a portfolio for a *Data Analyst, Health Analytics* role.

> **All data is synthetic.** `src/generate_data.py` creates six fictional health plans (Commercial, Medicare
> Advantage, Medicaid), ~54,000 members, ~597,000 lab claim lines (Jan 2024 – Jun 2026, paid through
> 30 Jun 2026), a lab network and a Finance GL. CPT/HCPCS codes, ICD-10 codes and policy logic follow
> real-world conventions. Data defects are injected on purpose so the pipeline has something real to catch.

## Headline results (from the latest run)

| Area | Result |
|---|---|
| Data quality | 99.52% of lines pass every check; all injected defects caught and quarantined |
| Reconciliation | 179 of 180 client-months tie to the GL; the exception traced to one missing weekly file (`W2`, Mar 2026) |
| Policy adherence | 66,235 claims fail a policy, carrying $3.78M (18.4%) of $20.5M lab allowed |
| Program impact | $1.25M estimated savings on $491K fees (2.6x); 2 of 5 live plans meet their guarantee |
| Forecast | Jan–May 2026 portfolio spend within 0.5% out of sample (seasonal-naive: 3.2%) |
| Prospect pricing | Base case $2.26 PMPM year-1 savings (8.2% of lab spend); hybrid fee with guarantee recommended |
| AI guardrail | 4 verified NL queries + a double-counting trap, all checked against independent calculations |

## Repository map

```
src/generate_data.py                 synthetic data + injected defects
src/pipeline/run_pipeline.py         RAW -> STAGING -> QA gate -> MARTS (DuckDB locally)
sql/staging/                         dedupe, typing, defect tags, eligibility, reversal netting
sql/quality/                         9 automated checks; claims-vs-GL reconciliation + batch gaps
sql/marts/                           policy engine, client-month metrics, paid-lag triangle
snowflake/                           production DDL, COPY INTO, stored procedure, TASK + ALERT
src/analysis/completion_factors.py   chain-ladder IBNR completion
src/analysis/program_impact.py       imputation DiD, placebos, ROI & guarantee scorecard, capture rates
src/analysis/forecast.py             log-PMPM model, backtest vs seasonal-naive, 2027 projection
src/analysis/pricing_model.py        Excel pricing model (openpyxl, live formulas)
semantic/lab_analytics.yaml          Cortex Analyst semantic model + verified queries
src/ai/verify_semantic_queries.py    executes and checks every verified query
tests/                               policy-rule unit tests + warehouse integration tests
site/                                portfolio page template and builder
.github/workflows/pipeline.yml       CI: build, run, test, publish reports
```

## Run it

```bash
pip install -r requirements.txt
make all
```

Outputs land in `outputs/`: `reports/*.md` (data quality, program impact, forecast, completion factors),
`Lakeshore_pricing_model.xlsx`, and `portfolio.html`.

## Design decisions worth reading

* **Whole-claim quarantine.** Reversals are netted per claim. If any line of a claim fails a check, the whole
  claim is held back; netting partial claims double counted 22 services (caught by `tests/test_data_integrity.py`).
* **Look-back censoring.** The HbA1c frequency rule only runs once a full 12-month history exists (from Jan 2025).
* **Counterfactual.** Staggered go-lives make not-yet-live plans the control group. The model is fit on untreated
  months only (imputation estimator) to avoid two-way fixed effects bias. Placebo tests set the noise floor (about ±4%
  per plan); plan-specific trends were tested and rejected because they over-extrapolate short pre-periods.
* **Completion before trending.** Recent months are completed with chain-ladder factors; months under 85% complete
  are excluded from impact and backtests.
* **Honest uncertainty.** The 2027 forecast range comes from out-of-sample error, not model residuals.
* **SQL portability.** SQL is written in syntax shared by Snowflake and DuckDB and is executed on DuckDB in CI;
  `snowflake/` holds the production wrappers.

# Data Quality & Reconciliation Report
_Run: 2026-11-05 15:13 | claims paid through 2026-06-30_

* Raw claim lines received: **596,994**
* Lines after de-duplication: **594,616**; analytically valid: **591,769** (99.52%); quarantined: **2,847**
* Client-months reconciled to Finance GL: **179 of 180** (GL $19,189,204 vs claims $19,145,821; net variance $-43,382)

## Automated checks
| Check | Severity | Failing rows | Fail rate | Threshold | Status |
|---|---|---:|---:|---:|---|
| negative_paid_on_paid_status | BLOCKER | 0 | 0.0000% | 0.00% | PASS |
| null_member_or_client | BLOCKER | 0 | 0.0000% | 0.00% | PASS |
| duplicate_claim_line_key | WARN | 2,378 | 0.3983% | 1.00% | PASS_WITH_EXCEPTIONS |
| invalid_or_missing_cpt | WARN | 474 | 0.0794% | 0.50% | PASS_WITH_EXCEPTIONS |
| member_not_eligible_on_dos | WARN | 1,787 | 0.2993% | 1.00% | PASS_WITH_EXCEPTIONS |
| paid_exceeds_allowed | WARN | 0 | 0.0000% | 0.10% | PASS |
| reversal_without_original | WARN | 15 | 0.0025% | 0.10% | PASS_WITH_EXCEPTIONS |
| service_date_after_paid_date | WARN | 237 | 0.0397% | 0.20% | PASS_WITH_EXCEPTIONS |
| unknown_lab_id | WARN | 357 | 0.0598% | 0.50% | PASS_WITH_EXCEPTIONS |

## Reconciliation exceptions (claims extract vs Finance GL)
| Client | Paid month | GL paid | Claims paid | Variance | Var % | Status |
|---|---|---:|---:|---:|---:|---|
| C02 | 2026-03 | $211,312.17 | $167,929.70 | $-43,382.47 | -20.53% | INVESTIGATE |

## Batch gaps behind exceptions
* C02 2026-03: weekly batch **W2** not received

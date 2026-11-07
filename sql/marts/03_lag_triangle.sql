-- =====================================================================================
-- marts.paid_lag_triangle
-- Incurred (service month) x development lag (months between service and paid month)
-- cumulative allowed dollars. Feeds the chain-ladder completion factors used to
-- "complete" recent months before they are trended or forecast (IBNR adjustment).
-- Uses line-level paid dates so reversals develop in the month they post.
-- =====================================================================================
CREATE OR REPLACE TABLE marts.paid_lag_triangle AS
SELECT
    service_month,
    DATEDIFF('month', service_month, paid_month) AS lag_months,
    SUM(allowed_amount)                          AS allowed_incremental
FROM staging.stg_claims_lab
WHERE is_analytic_valid
GROUP BY service_month, DATEDIFF('month', service_month, paid_month);

-- =====================================================================================
-- qa.recon_claims_vs_gl
-- Reconciles claims-extract paid dollars to the Finance general ledger (paid basis),
-- by client and paid month. Material variances usually mean a missing or late file,
-- not a math problem, so the output points at the batch-level detail to chase.
-- Tolerance: |variance| <= 0.50% of GL OR <= $500.
-- =====================================================================================
CREATE OR REPLACE TABLE qa.recon_claims_vs_gl AS
WITH claims AS (
    SELECT client_id, paid_month, SUM(paid_amount) AS claims_paid,
           COUNT(DISTINCT source_batch_id) AS batches_received
    FROM staging.stg_claims_lab            -- all deduped lines: GL books everything paid
    GROUP BY client_id, paid_month
),
gl AS (
    SELECT client_id, CAST(paid_month AS DATE) AS paid_month, gl_paid_amount
    FROM raw.finance_gl_lab_paid
)
SELECT
    COALESCE(g.client_id, c.client_id)                       AS client_id,
    COALESCE(g.paid_month, c.paid_month)                     AS paid_month,
    ROUND(COALESCE(g.gl_paid_amount, 0), 2)                  AS gl_paid,
    ROUND(COALESCE(c.claims_paid, 0), 2)                     AS claims_paid,
    ROUND(COALESCE(c.claims_paid, 0) - COALESCE(g.gl_paid_amount, 0), 2) AS variance,
    ROUND((COALESCE(c.claims_paid, 0) - COALESCE(g.gl_paid_amount, 0))
          / NULLIF(g.gl_paid_amount, 0), 5)                  AS variance_pct,
    c.batches_received,
    CASE
        WHEN ABS(COALESCE(c.claims_paid, 0) - COALESCE(g.gl_paid_amount, 0)) <= 500 THEN 'RECONCILED'
        WHEN ABS(COALESCE(c.claims_paid, 0) - COALESCE(g.gl_paid_amount, 0))
             <= 0.005 * ABS(g.gl_paid_amount) THEN 'RECONCILED'
        ELSE 'INVESTIGATE'
    END                                                      AS recon_status
FROM gl g
FULL OUTER JOIN claims c
  ON c.client_id = g.client_id AND c.paid_month = g.paid_month;

-- Batch-level drill-down for months that need investigation: expected weekly batches
-- (W1..W5 present in the month) vs batches actually received.
CREATE OR REPLACE TABLE qa.recon_batch_gaps AS
WITH weeks AS (
    SELECT DISTINCT paid_month, source_batch_id,
           SUBSTRING(source_batch_id, LENGTH(source_batch_id) - 1, 2) AS week_tag
    FROM staging.stg_claims_lab
),
expected AS (   -- every week tag seen for that month across any client
    SELECT DISTINCT paid_month, week_tag FROM weeks
),
received AS (
    SELECT DISTINCT client_id, paid_month,
           SUBSTRING(source_batch_id, LENGTH(source_batch_id) - 1, 2) AS week_tag
    FROM staging.stg_claims_lab
)
SELECT r.client_id, r.paid_month, e.week_tag AS missing_week_batch
FROM qa.recon_claims_vs_gl r
JOIN expected e ON e.paid_month = r.paid_month
LEFT JOIN received x
  ON x.client_id = r.client_id AND x.paid_month = r.paid_month AND x.week_tag = e.week_tag
WHERE r.recon_status = 'INVESTIGATE' AND x.week_tag IS NULL;

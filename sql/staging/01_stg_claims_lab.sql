-- =====================================================================================
-- stg_claims_lab
-- Grain: one row per claim line (claim_id, line_no) after removing re-sent duplicates.
-- Standardizes types, attaches reference data, and tags every data-quality defect as a
-- boolean so nothing is silently dropped: downstream marts filter on is_analytic_valid.
-- Written in ANSI/Snowflake-compatible SQL (also runs on DuckDB for local CI).
-- =====================================================================================
CREATE OR REPLACE TABLE staging.stg_claims_lab AS
WITH deduped AS (
    SELECT
        r.*,
        COUNT(*) OVER (PARTITION BY claim_id, line_no) AS key_copies
    FROM raw.claims_lab r
    -- a re-sent file produces identical (claim_id, line_no) keys: keep one copy
    QUALIFY ROW_NUMBER() OVER (PARTITION BY claim_id, line_no ORDER BY paid_date, source_batch_id) = 1
)
SELECT
    d.claim_id,
    d.line_no,
    d.claim_status,
    d.client_id,
    d.member_id,
    CAST(d.service_date AS DATE)                         AS service_date,
    CAST(d.paid_date AS DATE)                            AS paid_date,
    DATE_TRUNC('month', CAST(d.service_date AS DATE))    AS service_month,
    DATE_TRUNC('month', CAST(d.paid_date AS DATE))       AS paid_month,
    UPPER(TRIM(d.cpt_code))                              AS cpt_code,
    d.units,
    d.icd10_code,
    d.lab_id,
    d.place_of_service,
    CAST(d.billed_amount  AS DECIMAL(12, 2))             AS billed_amount,
    CAST(d.allowed_amount AS DECIMAL(12, 2))             AS allowed_amount,
    CAST(d.paid_amount    AS DECIMAL(12, 2))             AS paid_amount,
    d.source_batch_id,
    t.test_category,
    t.policy_group,
    t.in_network_rate,
    dx.clinical_category,
    l.in_network,
    m.birth_year,
    m.sex,
    YEAR(CAST(d.service_date AS DATE)) - m.birth_year   AS age_at_service,
    -- ---------- data-quality tags ----------
    d.key_copies > 1                                     AS dq_had_duplicate,
    t.cpt_code IS NULL                                   AS dq_invalid_cpt,
    l.lab_id   IS NULL                                   AS dq_unknown_lab,
    CAST(d.service_date AS DATE) > CAST(d.paid_date AS DATE) AS dq_service_after_paid,
    e.member_id IS NULL                                  AS dq_not_eligible_on_dos,
    (t.cpt_code IS NOT NULL
        AND l.lab_id IS NOT NULL
        AND CAST(d.service_date AS DATE) <= CAST(d.paid_date AS DATE)
        AND e.member_id IS NOT NULL)                     AS is_analytic_valid
FROM deduped d
LEFT JOIN raw.test_catalog       t  ON t.cpt_code = UPPER(TRIM(d.cpt_code))
LEFT JOIN raw.lab_network        l  ON l.lab_id = d.lab_id
LEFT JOIN raw.icd10_clinical_map dx ON dx.icd10_code = d.icd10_code
LEFT JOIN raw.members            m  ON m.member_id = d.member_id
LEFT JOIN raw.eligibility        e
       ON e.member_id = d.member_id
      AND e.client_id = d.client_id
      AND CAST(d.service_date AS DATE) BETWEEN e.eff_date AND e.term_date;

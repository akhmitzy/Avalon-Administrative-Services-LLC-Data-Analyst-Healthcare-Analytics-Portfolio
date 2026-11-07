-- =====================================================================================
-- qa.dq_check_results
-- One row per automated check, per pipeline run. Severity drives pipeline behaviour:
--   BLOCKER -> run fails, marts are not published
--   WARN    -> run continues, issue is reported and rows are quarantined
-- Thresholds are rates of total raw lines so they scale with file size.
-- =====================================================================================
CREATE OR REPLACE TABLE qa.dq_check_results AS
WITH base AS (SELECT COUNT(*) AS raw_lines FROM raw.claims_lab),
stg AS (SELECT * FROM staging.stg_claims_lab),
checks AS (
    SELECT 'duplicate_claim_line_key' AS check_name, 'WARN' AS severity,
           'Same (claim_id, line_no) received more than once; extra copies removed' AS description,
           (SELECT COUNT(*) FROM raw.claims_lab) - (SELECT COUNT(*) FROM stg) AS failing_rows,
           0.010 AS max_fail_rate
    UNION ALL
    SELECT 'invalid_or_missing_cpt', 'WARN',
           'CPT/HCPCS code missing or not in test catalog',
           (SELECT COUNT(*) FROM stg WHERE dq_invalid_cpt), 0.005
    UNION ALL
    SELECT 'unknown_lab_id', 'WARN',
           'Rendering lab not found in lab network master',
           (SELECT COUNT(*) FROM stg WHERE dq_unknown_lab), 0.005
    UNION ALL
    SELECT 'service_date_after_paid_date', 'WARN',
           'Date of service later than paid date (keying error)',
           (SELECT COUNT(*) FROM stg WHERE dq_service_after_paid), 0.002
    UNION ALL
    SELECT 'member_not_eligible_on_dos', 'WARN',
           'No eligibility span covers the date of service for this member/client',
           (SELECT COUNT(*) FROM stg WHERE dq_not_eligible_on_dos), 0.010
    UNION ALL
    SELECT 'null_member_or_client', 'BLOCKER',
           'Claim line missing member_id or client_id',
           (SELECT COUNT(*) FROM stg WHERE member_id IS NULL OR client_id IS NULL), 0.0
    UNION ALL
    SELECT 'negative_paid_on_paid_status', 'BLOCKER',
           'Paid-status line carries a negative paid amount',
           (SELECT COUNT(*) FROM stg WHERE claim_status = 'P' AND paid_amount < 0), 0.0
    UNION ALL
    SELECT 'reversal_without_original', 'WARN',
           'Reversal line (R) whose original paid line is absent from the extract',
           (SELECT COUNT(*) FROM stg r WHERE r.claim_status = 'R'
              AND NOT EXISTS (SELECT 1 FROM stg o WHERE o.claim_id = r.claim_id AND o.line_no = 1)), 0.001
    UNION ALL
    SELECT 'paid_exceeds_allowed', 'WARN',
           'Paid amount greater than allowed amount',
           (SELECT COUNT(*) FROM stg WHERE paid_amount > allowed_amount + 0.01 AND claim_status = 'P'), 0.001
)
SELECT
    CURRENT_TIMESTAMP                                   AS run_ts,
    c.check_name,
    c.severity,
    c.description,
    c.failing_rows,
    b.raw_lines,
    ROUND(c.failing_rows * 1.0 / b.raw_lines, 6)        AS fail_rate,
    c.max_fail_rate,
    CASE
        WHEN c.failing_rows = 0 THEN 'PASS'
        WHEN c.failing_rows * 1.0 / b.raw_lines <= c.max_fail_rate THEN 'PASS_WITH_EXCEPTIONS'
        ELSE 'FAIL'
    END                                                 AS status
FROM checks c CROSS JOIN base b;

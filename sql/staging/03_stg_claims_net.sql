-- =====================================================================================
-- stg_claims_net
-- Grain: one row per claim (claim_id). Original, reversal (R) and re-paid lines are
-- netted so each service is counted once at its final adjudicated amount.
-- Claims whose net allowed is <= 0 (fully reversed) are kept but flagged.
-- (Bug found by tests/test_data_integrity.py: partially-quarantined claims used to leak in.)
-- =====================================================================================
CREATE OR REPLACE TABLE staging.stg_claims_net AS
WITH valid_lines AS (
    -- quarantine the WHOLE claim if any of its lines failed a check: netting a reversal
    -- without its original (or vice versa) would double count or under count the service
    SELECT * FROM staging.stg_claims_lab
    WHERE claim_id NOT IN (SELECT claim_id FROM staging.stg_claims_lab WHERE NOT is_analytic_valid)
),
header AS (   -- descriptive attributes come from the original line
    SELECT *
    FROM valid_lines
    QUALIFY ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY line_no) = 1
),
amounts AS (
    SELECT
        claim_id,
        COUNT(*)            AS line_count,
        MAX(CASE WHEN claim_status = 'R' THEN 1 ELSE 0 END) = 1 AS was_adjusted,
        SUM(billed_amount)  AS net_billed,
        SUM(allowed_amount) AS net_allowed,
        SUM(paid_amount)    AS net_paid,
        MAX(paid_date)      AS last_paid_date
    FROM valid_lines
    GROUP BY claim_id
)
SELECT
    h.claim_id, h.client_id, h.member_id, h.service_date, h.service_month,
    h.cpt_code, h.icd10_code, h.clinical_category, h.test_category, h.policy_group,
    h.in_network_rate, h.lab_id, h.in_network, h.place_of_service, h.age_at_service, h.sex,
    a.line_count, a.was_adjusted, a.net_billed, a.net_allowed, a.net_paid,
    h.paid_date AS first_paid_date, a.last_paid_date,
    a.net_allowed > 0 AS is_payable
FROM header h
JOIN amounts a USING (claim_id);

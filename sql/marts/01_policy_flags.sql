-- =====================================================================================
-- marts.claim_policy_flags
-- Retrospective policy-adherence engine. Each payable claim is tested against
-- evidence-based lab policies; a claim receives at most one primary finding (first
-- match in policy priority order) and an estimated avoidable-cost amount.
--
--   P01 Vitamin D screening without qualifying indication          avoidable = allowed
--   P02 HbA1c above frequency limit (4/12mo diabetic, 2 prediabetic, 1 other);
--       evaluated from 2025-01-01 so every test has a full 12-month lookback
--                                                                  avoidable = allowed
--   P03 Same-day duplicate of the same routine test                avoidable = allowed
--   P04 Definitive drug test (22+ classes) with no presumptive screen in prior 30 days
--                                                                  avoidable = allowed - 1-7 class equivalent
--   P05 Large respiratory panel (12-25 targets) without immunocompromised indication
--                                                                  avoidable = allowed - targeted panel equivalent
--   P06 Genetic / molecular test without supporting clinical indication
--                                                                  avoidable = allowed
--   P07 Out-of-network lab where an in-network option exists       avoidable = allowed - in-network benchmark
-- =====================================================================================
CREATE OR REPLACE TABLE marts.claim_policy_flags AS
WITH c AS (
    SELECT * FROM staging.stg_claims_net WHERE is_payable
),
-- member-level conditions are derived from diagnoses seen on any claim (no external flags)
member_dx AS (
    SELECT member_id,
           MAX(CASE WHEN clinical_category = 'diabetes'    THEN 1 ELSE 0 END) = 1 AS has_diabetes,
           MAX(CASE WHEN clinical_category = 'prediabetes' THEN 1 ELSE 0 END) = 1 AS has_prediabetes
    FROM c GROUP BY member_id
),
-- P02: rolling 12-month count of HbA1c tests at or before each test
a1c AS (
    SELECT a.claim_id,
           COUNT(*) AS a1c_tests_rolling_12m
    FROM c a
    JOIN c b
      ON b.member_id = a.member_id
     AND b.cpt_code = '83036'
     AND b.service_date BETWEEN a.service_date - INTERVAL '364 days' AND a.service_date
     AND (b.service_date < a.service_date OR b.claim_id <= a.claim_id)
    WHERE a.cpt_code = '83036'
    GROUP BY a.claim_id
),
-- P03: same member, same routine test, same day, more than one claim
same_day AS (
    SELECT claim_id,
           ROW_NUMBER() OVER (PARTITION BY member_id, cpt_code, service_date
                              ORDER BY first_paid_date, claim_id) AS same_day_seq
    FROM c WHERE test_category = 'Routine'
),
-- P04: did a presumptive screen (80307) occur within 30 days before / same day?
def_drug AS (
    SELECT d.claim_id,
           MAX(CASE WHEN p.claim_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_presumptive
    FROM c d
    LEFT JOIN c p
      ON p.member_id = d.member_id
     AND p.cpt_code = '80307'
     AND p.service_date BETWEEN d.service_date - INTERVAL '30 days' AND d.service_date
    WHERE d.cpt_code = 'G0483'
    GROUP BY d.claim_id
),
-- P06: indication lookup
genetic AS (
    SELECT g.claim_id,
           MAX(CASE WHEN i.cpt_code IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_indication
    FROM c g
    LEFT JOIN raw.genetic_test_indications i
      ON i.cpt_code = g.cpt_code AND i.clinical_category = g.clinical_category
    WHERE g.policy_group = 'P06_GENETIC_INDICATION'
    GROUP BY g.claim_id
),
-- P07: in-network benchmark = median in-network allowed for the same test, client, quarter
benchmark AS (
    SELECT client_id, cpt_code, DATE_TRUNC('quarter', service_date) AS svc_qtr,
           MEDIAN(net_allowed) AS in_network_median_allowed
    FROM c WHERE in_network
    GROUP BY client_id, cpt_code, DATE_TRUNC('quarter', service_date)
),
rates AS (SELECT cpt_code, in_network_rate FROM raw.test_catalog),
flagged AS (
    SELECT
        c.*,
        md.has_diabetes, md.has_prediabetes,
        a1c.a1c_tests_rolling_12m,
        sd.same_day_seq,
        dd.has_presumptive,
        gn.has_indication,
        bm.in_network_median_allowed,
        CASE
            WHEN c.cpt_code = '82306' AND COALESCE(c.clinical_category, '') NOT IN ('vitd_deficiency', 'ckd')
                THEN 'P01'
            -- only evaluable once a full 12-month lookback exists in the data (avoids
            -- left-censoring that would under-count frequency early in the window)
            WHEN c.cpt_code = '83036' AND c.service_date >= DATE '2025-01-01'
                 AND a1c.a1c_tests_rolling_12m >
                 CASE WHEN md.has_diabetes THEN 4 WHEN md.has_prediabetes THEN 2 ELSE 1 END
                THEN 'P02'
            WHEN sd.same_day_seq > 1 THEN 'P03'
            WHEN c.cpt_code = 'G0483' AND NOT dd.has_presumptive THEN 'P04'
            WHEN c.cpt_code = '87633' AND COALESCE(c.clinical_category, '') <> 'immunocompromised'
                THEN 'P05'
            WHEN c.policy_group = 'P06_GENETIC_INDICATION'
                 AND (NOT gn.has_indication
                      OR (c.cpt_code = '81528' AND c.age_at_service NOT BETWEEN 45 AND 85))
                THEN 'P06'
            WHEN NOT c.in_network AND bm.in_network_median_allowed IS NOT NULL
                 AND c.net_allowed > bm.in_network_median_allowed
                THEN 'P07'
        END AS policy_id
    FROM c
    LEFT JOIN member_dx md ON md.member_id = c.member_id
    LEFT JOIN a1c          ON a1c.claim_id = c.claim_id
    LEFT JOIN same_day sd  ON sd.claim_id = c.claim_id
    LEFT JOIN def_drug dd  ON dd.claim_id = c.claim_id
    LEFT JOIN genetic gn   ON gn.claim_id = c.claim_id
    LEFT JOIN benchmark bm ON bm.client_id = c.client_id AND bm.cpt_code = c.cpt_code
                          AND bm.svc_qtr = DATE_TRUNC('quarter', c.service_date)
)
SELECT
    f.claim_id, f.client_id, f.member_id, f.service_date, f.service_month, f.cpt_code,
    f.test_category, f.clinical_category, f.lab_id, f.in_network, f.net_allowed, f.net_paid,
    f.policy_id,
    ROUND(CASE f.policy_id
        WHEN 'P04' THEN f.net_allowed * (1 - (SELECT in_network_rate FROM rates WHERE cpt_code = 'G0480')
                                           / (SELECT in_network_rate FROM rates WHERE cpt_code = 'G0483'))
        WHEN 'P05' THEN f.net_allowed * (1 - (SELECT in_network_rate FROM rates WHERE cpt_code = '87637')
                                           / (SELECT in_network_rate FROM rates WHERE cpt_code = '87633'))
        WHEN 'P07' THEN f.net_allowed - f.in_network_median_allowed
        WHEN 'P01' THEN f.net_allowed
        WHEN 'P02' THEN f.net_allowed
        WHEN 'P03' THEN f.net_allowed
        WHEN 'P06' THEN f.net_allowed
        ELSE 0
    END, 2) AS avoidable_allowed
FROM flagged f;

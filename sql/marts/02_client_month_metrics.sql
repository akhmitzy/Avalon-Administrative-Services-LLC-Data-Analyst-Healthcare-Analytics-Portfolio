-- =====================================================================================
-- marts.client_month_metrics
-- Grain: client x service month x test category. The reporting backbone for Tableau
-- dashboards: member months, spend, PMPM, utilization per 1,000, network share and
-- policy-addressable spend, plus program period (pre / post go-live).
-- =====================================================================================
CREATE OR REPLACE TABLE marts.client_month_metrics AS
WITH mm AS (
    SELECT client_id, service_month, COUNT(*) AS member_months
    FROM staging.stg_member_months
    GROUP BY client_id, service_month
),
claims AS (
    SELECT
        f.client_id, f.service_month, f.test_category,
        COUNT(*)                                                   AS tests,
        SUM(f.net_allowed)                                         AS allowed,
        SUM(f.net_paid)                                            AS paid,
        SUM(CASE WHEN NOT f.in_network THEN f.net_allowed ELSE 0 END) AS oon_allowed,
        SUM(CASE WHEN f.policy_id IS NOT NULL THEN 1 ELSE 0 END)   AS nonadherent_tests,
        SUM(f.avoidable_allowed)                                   AS avoidable_allowed
    FROM marts.claim_policy_flags f
    GROUP BY f.client_id, f.service_month, f.test_category
)
SELECT
    cl.client_id,
    cl.client_name,
    cl.line_of_business,
    c.service_month,
    c.test_category,
    CASE WHEN cl.go_live_date IS NULL THEN 'Not live'
         WHEN c.service_month >= DATE_TRUNC('month', CAST(cl.go_live_date AS DATE)) THEN 'Post go-live'
         ELSE 'Pre go-live' END                                    AS program_period,
    mm.member_months,
    c.tests,
    ROUND(c.allowed, 2)                                            AS allowed,
    ROUND(c.paid, 2)                                               AS paid,
    ROUND(c.oon_allowed, 2)                                        AS oon_allowed,
    c.nonadherent_tests,
    ROUND(c.avoidable_allowed, 2)                                  AS avoidable_allowed,
    ROUND(c.allowed / mm.member_months, 4)                         AS allowed_pmpm,
    ROUND(c.tests * 12000.0 / mm.member_months, 2)                 AS tests_per_1000_annualized
FROM claims c
JOIN mm ON mm.client_id = c.client_id AND mm.service_month = c.service_month
JOIN raw.clients cl ON cl.client_id = c.client_id;

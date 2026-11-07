-- =====================================================================================
-- stg_member_months
-- Grain: one row per member per eligible calendar month (the PMPM denominator).
-- A recursive calendar keeps this portable between Snowflake and DuckDB.
-- =====================================================================================
CREATE OR REPLACE TABLE staging.stg_member_months AS
WITH RECURSIVE calendar (month_start) AS (
    SELECT CAST('2024-01-01' AS DATE)
    UNION ALL
    SELECT CAST(month_start + INTERVAL '1 month' AS DATE)
    FROM calendar
    WHERE month_start < CAST('2026-06-01' AS DATE)
)
SELECT
    e.client_id,
    e.member_id,
    c.month_start AS service_month,
    YEAR(c.month_start) - m.birth_year AS age,
    m.sex
FROM raw.eligibility e
JOIN calendar c
  ON c.month_start BETWEEN DATE_TRUNC('month', e.eff_date) AND e.term_date
JOIN raw.members m ON m.member_id = e.member_id;

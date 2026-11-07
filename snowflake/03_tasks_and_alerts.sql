-- =====================================================================================
-- Scheduling: a Snowflake TASK runs the refresh every weekday after the claims file
-- lands; an ALERT emails the analytics distro when the gate fails or Finance totals
-- do not tie. Tableau extracts are scheduled 30 minutes later off MARTS.
-- =====================================================================================
USE SCHEMA LAB_ANALYTICS.QA;

CREATE OR REPLACE TASK TSK_REFRESH_LAB_MARTS
  WAREHOUSE = WH_ANALYTICS_XS
  SCHEDULE  = 'USING CRON 30 6 * * 1-5 America/New_York'
  COMMENT   = 'Weekday refresh of lab analytics marts after 06:00 claims landing'
AS
  CALL LAB_ANALYTICS.QA.SP_REFRESH_LAB_MARTS();

ALTER TASK TSK_REFRESH_LAB_MARTS RESUME;

CREATE OR REPLACE ALERT ALR_LAB_PIPELINE_HEALTH
  WAREHOUSE = WH_ANALYTICS_XS
  SCHEDULE  = 'USING CRON 0 7 * * 1-5 America/New_York'
  IF (EXISTS (
        SELECT 1 FROM LAB_ANALYTICS.QA.PIPELINE_RUNS
        WHERE RUN_TS >= DATEADD('hour', -2, CURRENT_TIMESTAMP())
          AND (STATUS <> 'SUCCESS' OR RECON_ISSUES > 0)))
  THEN
    CALL SYSTEM$SEND_EMAIL(
        'ANALYTICS_EMAIL_INT',
        'health-analytics@example.com',
        'Lab analytics pipeline needs attention',
        'Quality gate failed or claims do not reconcile to the Finance GL. See LAB_ANALYTICS.QA.PIPELINE_RUNS and QA.RECON_BATCH_GAPS.');

ALTER ALERT ALR_LAB_PIPELINE_HEALTH RESUME;

-- =====================================================================================
-- SP_REFRESH_LAB_MARTS: orchestrates STAGING -> QA gate -> MARTS inside Snowflake.
-- Runs the same files under /sql (CI copies them to @DEPLOY_STAGE on merge to main) in
-- order, and stops before MARTS if a BLOCKER check fails, so bad data is never published.
-- =====================================================================================
USE SCHEMA LAB_ANALYTICS.QA;

CREATE TABLE IF NOT EXISTS PIPELINE_RUNS (
    RUN_ID        VARCHAR DEFAULT UUID_STRING(),
    RUN_TS        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    STATUS        VARCHAR,
    BLOCKERS      NUMBER,
    RECON_ISSUES  NUMBER,
    MESSAGE       VARCHAR
);

CREATE OR REPLACE PROCEDURE LAB_ANALYTICS.QA.SP_REFRESH_LAB_MARTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    blockers     NUMBER DEFAULT 0;
    recon_issues NUMBER DEFAULT 0;
BEGIN
    -- 1. staging (same SQL as sql/staging/*.sql)
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/staging/01_stg_claims_lab.sql';
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/staging/02_stg_member_months.sql';
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/staging/03_stg_claims_net.sql';

    -- 2. quality checks + finance reconciliation
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/quality/01_dq_checks.sql';
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/quality/02_reconcile_finance_gl.sql';

    SELECT COUNT(*) INTO :blockers
      FROM LAB_ANALYTICS.QA.DQ_CHECK_RESULTS WHERE SEVERITY = 'BLOCKER' AND STATUS = 'FAIL';
    SELECT COUNT(*) INTO :recon_issues
      FROM LAB_ANALYTICS.QA.RECON_CLAIMS_VS_GL WHERE RECON_STATUS = 'INVESTIGATE';

    IF (blockers > 0) THEN
        INSERT INTO LAB_ANALYTICS.QA.PIPELINE_RUNS (STATUS, BLOCKERS, RECON_ISSUES, MESSAGE)
        VALUES ('FAILED_QUALITY_GATE', :blockers, :recon_issues, 'Marts not refreshed');
        RETURN 'FAILED: ' || blockers || ' blocking checks';
    END IF;

    -- 3. marts (only reached when the gate passes)
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/marts/01_policy_flags.sql';
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/marts/02_client_month_metrics.sql';
    EXECUTE IMMEDIATE FROM '@LAB_ANALYTICS.QA.DEPLOY_STAGE/sql/marts/03_lag_triangle.sql';

    INSERT INTO LAB_ANALYTICS.QA.PIPELINE_RUNS (STATUS, BLOCKERS, RECON_ISSUES, MESSAGE)
    VALUES ('SUCCESS', 0, :recon_issues,
            IFF(:recon_issues > 0, 'Published; reconciliation exceptions sent to Finance', 'Published'));
    RETURN 'SUCCESS (recon exceptions: ' || recon_issues || ')';
EXCEPTION
    WHEN OTHER THEN
        INSERT INTO LAB_ANALYTICS.QA.PIPELINE_RUNS (STATUS, MESSAGE) VALUES ('ERROR', :SQLERRM);
        RAISE;
END;
$$;

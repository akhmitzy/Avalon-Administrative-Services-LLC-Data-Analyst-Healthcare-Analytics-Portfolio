-- =====================================================================================
-- Snowflake production setup: database, schemas, warehouse, file format, stage, RAW tables.
-- The local DuckDB pipeline mirrors this layout (RAW -> STAGING -> QA -> MARTS).
-- =====================================================================================
USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS WH_ANALYTICS_XS
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
  COMMENT = 'Health Analytics reporting + scheduled refresh';

CREATE DATABASE IF NOT EXISTS LAB_ANALYTICS;
CREATE SCHEMA IF NOT EXISTS LAB_ANALYTICS.RAW;
CREATE SCHEMA IF NOT EXISTS LAB_ANALYTICS.STAGING;
CREATE SCHEMA IF NOT EXISTS LAB_ANALYTICS.QA;
CREATE SCHEMA IF NOT EXISTS LAB_ANALYTICS.MARTS;

USE SCHEMA LAB_ANALYTICS.RAW;

CREATE FILE FORMAT IF NOT EXISTS FF_PARQUET TYPE = PARQUET;
CREATE FILE FORMAT IF NOT EXISTS FF_CSV TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  NULL_IF = ('', 'NULL');

-- External stage over the SFTP landing bucket (storage integration created by platform team)
CREATE STAGE IF NOT EXISTS STG_CLAIMS_LANDING
  URL = 's3://example-lab-claims-landing/claims/'
  STORAGE_INTEGRATION = SI_CLAIMS_LANDING
  FILE_FORMAT = FF_PARQUET;

CREATE TABLE IF NOT EXISTS CLAIMS_LAB (
    CLAIM_ID          VARCHAR(16)   NOT NULL,
    LINE_NO           NUMBER(3)     NOT NULL,
    CLAIM_STATUS      VARCHAR(1)    NOT NULL,   -- P = paid, R = reversal
    CLIENT_ID         VARCHAR(8),
    MEMBER_ID         VARCHAR(16),
    SERVICE_DATE      DATE,
    PAID_DATE         DATE,
    CPT_CODE          VARCHAR(5),
    UNITS             NUMBER(5),
    ICD10_CODE        VARCHAR(8),
    LAB_ID            VARCHAR(8),
    PLACE_OF_SERVICE  VARCHAR(2),
    BILLED_AMOUNT     NUMBER(12, 2),
    ALLOWED_AMOUNT    NUMBER(12, 2),
    PAID_AMOUNT       NUMBER(12, 2),
    SOURCE_BATCH_ID   VARCHAR(16),
    _LOADED_AT        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    _SOURCE_FILE      VARCHAR
)
CLUSTER BY (CLIENT_ID, DATE_TRUNC('month', SERVICE_DATE));

CREATE TABLE IF NOT EXISTS MEMBERS     (MEMBER_ID VARCHAR(16), CLIENT_ID VARCHAR(8), BIRTH_YEAR NUMBER(4), SEX VARCHAR(1));
CREATE TABLE IF NOT EXISTS ELIGIBILITY (MEMBER_ID VARCHAR(16), CLIENT_ID VARCHAR(8), EFF_DATE DATE, TERM_DATE DATE);
CREATE TABLE IF NOT EXISTS FINANCE_GL_LAB_PAID (CLIENT_ID VARCHAR(8), PAID_MONTH DATE, GL_PAID_AMOUNT NUMBER(14, 2), GL_ACCOUNT VARCHAR(32));

-- Incremental, idempotent load: COPY skips files already loaded (load metadata, 64 days)
COPY INTO CLAIMS_LAB (CLAIM_ID, LINE_NO, CLAIM_STATUS, CLIENT_ID, MEMBER_ID, SERVICE_DATE, PAID_DATE,
                      CPT_CODE, UNITS, ICD10_CODE, LAB_ID, PLACE_OF_SERVICE, BILLED_AMOUNT,
                      ALLOWED_AMOUNT, PAID_AMOUNT, SOURCE_BATCH_ID, _SOURCE_FILE)
FROM (
    SELECT $1:claim_id, $1:line_no, $1:claim_status, $1:client_id, $1:member_id, $1:service_date::DATE,
           $1:paid_date::DATE, $1:cpt_code, $1:units, $1:icd10_code, $1:lab_id, $1:place_of_service,
           $1:billed_amount, $1:allowed_amount, $1:paid_amount, $1:source_batch_id, METADATA$FILENAME
    FROM @STG_CLAIMS_LANDING
)
PATTERN = '.*claims_lab.*[.]parquet'
ON_ERROR = 'CONTINUE';

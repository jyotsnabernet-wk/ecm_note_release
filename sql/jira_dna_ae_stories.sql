-- DNA + Analytics Engineering + Story + Closed/In Progress (same filters as before).
-- Latest row per THE_KEY by PROCESSING_TIMESTAMP.
--
-- Time window matches build_release_notes.py "planned" cadence (planned_release_window.py):
--   [last Thursday 00:00 UTC, deploy Wednesday end) half-open,
--   i.e. UPDATED_AT >= that Thursday 00:00 AND UPDATED_AT < Thursday 00:00 after deploy Wednesday.
-- "Deploy Wednesday" = calendar Wednesday on or after anchor (UTC calendar day).
--
-- Optional: in `params`, set anchor_override to a DATE to mimic `python build_release_notes.py --anchor YYYY-MM-DD`.

WITH params AS (
    SELECT NULL::DATE AS anchor_override  -- e.g. DATE '2026-06-09' for testing; NULL = today UTC
),
anchor AS (
    SELECT COALESCE(p.anchor_override, CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::DATE) AS anchor_d
    FROM params p
),
planned AS (
    SELECT
        a.anchor_d,
        DATEADD('day', MOD(10 - DAYOFWEEKISO(a.anchor_d), 7), a.anchor_d) AS release_wednesday
    FROM anchor a
),
bounds AS (
    SELECT
        p.release_wednesday,
        DATEADD('day', -6, p.release_wednesday) AS window_start_date,
        DATEADD('day', 1, p.release_wednesday) AS window_end_exclusive_date
    FROM planned p
),
bounds_ts AS (
    SELECT
        TIMESTAMP_FROM_PARTS(YEAR(b.window_start_date), MONTH(b.window_start_date), DAY(b.window_start_date), 0, 0, 0, 0) AS start_utc,
        TIMESTAMP_FROM_PARTS(
            YEAR(b.window_end_exclusive_date),
            MONTH(b.window_end_exclusive_date),
            DAY(b.window_end_exclusive_date),
            0, 0, 0, 0
        ) AS end_exclusive_utc
    FROM bounds b
),
parsed AS (
    SELECT
        THE_KEY,
        SUMMARY,
        LEFT(DESCRIPTION, 500) AS DESCRIPTION_PREVIEW,
        TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(STATUS, '''', '"'), ': False', ': false'), ': True', ': true')):name::STRING AS STATUS_NAME,
        TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(CUSTOMFIELD_10288, '''', '"'), ': False', ': false'), ': True', ': true')):value::STRING AS TEAM_VALUE,
        TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(ISSUETYPE, '''', '"'), ': False', ': false'), ': True', ': true')):name::STRING AS ISSUE_TYPE,
        TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(PRIORITY, '''', '"'), ': False', ': false'), ': True', ': true')):name::STRING AS PRIORITY_NAME,
        TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(ASSIGNEE, '''', '"'), ': False', ': false'), ': True', ': true')):displayName::STRING AS ASSIGNEE_NAME,
        COMPONENTS,
        LABELS,
        COALESCE(
            TRY_TO_TIMESTAMP(NULLIF(UPDATED, ''), 'YYYY-MM-DD"T"HH24:MI:SS.FF3TZHTZM'),
            TRY_TO_TIMESTAMP(NULLIF(UPDATED, ''), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        ) AS UPDATED_AT,
        COALESCE(
            TRY_TO_TIMESTAMP(NULLIF(CREATED, ''), 'YYYY-MM-DD"T"HH24:MI:SS.FF3TZHTZM'),
            TRY_TO_TIMESTAMP(NULLIF(CREATED, ''), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        ) AS CREATED_AT,
        COALESCE(
            TRY_TO_TIMESTAMP(NULLIF(RESOLUTIONDATE, ''), 'YYYY-MM-DD"T"HH24:MI:SS.FF3TZHTZM'),
            TRY_TO_TIMESTAMP(NULLIF(RESOLUTIONDATE, ''), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        ) AS RESOLUTION_AT,
        ROW_NUMBER() OVER (PARTITION BY THE_KEY ORDER BY PROCESSING_TIMESTAMP DESC) AS RN
    FROM LAKE_PROD.JIRA.JIRA_DNA
    WHERE TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(PROJECT, '''', '"'), ': False', ': false'), ': True', ': true')):key::STRING = 'DNA'
      AND TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(CUSTOMFIELD_10288, '''', '"'), ': False', ': false'), ': True', ': true')):value::STRING = 'Analytics Engineering'
      AND TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(ISSUETYPE, '''', '"'), ': False', ': false'), ': True', ': true')):name::STRING = 'Story'
      AND TRY_PARSE_JSON(REPLACE(REPLACE(REPLACE(STATUS, '''', '"'), ': False', ': false'), ': True', ': true')):name::STRING IN ('Closed', 'In Progress')
)
SELECT
    p.THE_KEY,
    p.SUMMARY,
    p.DESCRIPTION_PREVIEW,
    p.STATUS_NAME,
    p.PRIORITY_NAME,
    p.ASSIGNEE_NAME,
    p.COMPONENTS,
    p.LABELS,
    p.UPDATED_AT,
    p.CREATED_AT,
    p.RESOLUTION_AT
FROM parsed p
CROSS JOIN bounds_ts w
WHERE p.RN = 1
  AND p.UPDATED_AT >= w.start_utc
  AND p.UPDATED_AT < w.end_exclusive_utc
ORDER BY p.UPDATED_AT DESC, p.CREATED_AT DESC

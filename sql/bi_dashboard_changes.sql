-- BI dashboard change log for the current Thu→Wed release window.
-- Source: GOLD_PROD.BI.V_ECM_DASHBOARD_CHANGE_LOG_LATEST
--
-- Date window matches AE pipeline cadence (planned_release_window.py):
--   start : last Thursday 00:00 ET
--   end   : deploy Wednesday (inclusive)
--
-- Override dates in the params CTE for testing.

WITH params AS (
    SELECT
        NULL::DATE AS start_date_override,   -- e.g. DATE '2026-07-23'  NULL = auto
        NULL::DATE AS end_date_override      -- e.g. DATE '2026-07-29'  NULL = auto
),
anchor AS (
    SELECT CONVERT_TIMEZONE('America/New_York', CURRENT_TIMESTAMP())::DATE AS today_et
),
window_auto AS (
    -- Deploy Wednesday = next Wednesday on or after today
    SELECT
        DATEADD('day', MOD(10 - DAYOFWEEKISO(a.today_et), 7), a.today_et) AS deploy_wed,
        DATEADD('day', MOD(10 - DAYOFWEEKISO(a.today_et), 7) - 6, a.today_et) AS sprint_start_thu
    FROM anchor a
),
bounds AS (
    SELECT
        COALESCE(p.start_date_override, w.sprint_start_thu) AS start_date,
        COALESCE(p.end_date_override,   w.deploy_wed)       AS end_date
    FROM params p, window_auto w
)
SELECT
    c.CHANGE_ID,
    c.RELEASE_VERSION,
    c.RELEASE_DATE,
    c.DASHBOARD_NAME,
    c.CHANGE_CATEGORY,
    c.CHANGE_TITLE,
    c.CHANGE_DESCRIPTION,
    c.BEFORE_SUMMARY,
    c.AFTER_SUMMARY,
    c.STAKEHOLDER_IMPACT,
    c.AFFECTED_COMPONENT,
    c.DISPLAY_ORDER,
    c.OWNER
FROM GOLD_PROD.BI.V_ECM_DASHBOARD_CHANGE_LOG_LATEST c
CROSS JOIN bounds b
WHERE c.RELEASE_DATE >= b.start_date
  AND c.RELEASE_DATE <= b.end_date
  AND c.IS_ACTIVE = TRUE
ORDER BY c.DISPLAY_ORDER, c.CHANGE_ID

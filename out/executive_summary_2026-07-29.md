# DnA Release — July 23–29, 2026 | Executive Summary

## Analytics Engineering (Snowflake / dbt)

- **FedRAMP Compliance** — Five Workiva event datasets are now sanitized and accessible in Snowflake, completing the Phase 1 data compliance milestone.
- **Support Data** — A type error in the Zendesk voice calls model is resolved, restoring full data availability for support reporting.
- **ECM (Revenue)** — A bookings logic error that silently excluded valid Non-PI deals from pipeline reporting is corrected, improving completeness for Finance and RevOps.
- **ECM (Revenue)** *(in progress)* — New solution-level renewal metrics are in development to measure S&S ARR up for renewal and renewal rate at solution grain.
- **Customer Intelligence** *(in progress)* — A D&B corporate hierarchy table is being added to Customer360 to support account segmentation and multi-entity analysis.
- **Platform** — SCD Type 2 data quality tests are added to eleven dimension models, catching row-level interval errors at build time before they reach downstream reports.

## ECM QuickSuite Dashboard Changes

- **ECM Reporting Access** — Row-level security is live on the ECM Performance dashboard; users can request access scoped to their Solution Group, Region, or a combination of both.
- **Solution Owner Metrics** — Qualified Pipeline Generated $, Partner NL/NS %, Partner Delivery %, Historical Pipeline Coverage, and New Logo Count are now on a single SO Metrics tab in ECM Performance Metrics, replacing separate charts that were previously spread across the legacy Solution Owner dashboard per end user request.
- **Pipeline & Coverage Accuracy** — Bug fix: SOQ pipeline $ and SO-24 historical coverage were using raw 5th-business-day flags that understated numbers vs. Pulse (the certified pipeline reporting tool). Both now use the same certified SOQ flag as Pulse — pipeline $ and coverage % figures will increase after this correction.
- **Cycle Time Fix** — Bug fix: Acquire cycle time KPI was dropping opportunities during deduplication, showing 55 days vs. 64 days in Snowflake for FY26 Q1. Corrected — KPI now matches Snowflake.
- **Data Consistency** — OR/RF solution groups standardized to "Other" across all Dataset 6 export and pipeline branches; NULL filter entries cleaned and data freshness timestamps added.

---

*Need more detail? Review the technical release notes: DNA Weekly Release Sprint 7/23-7/29*

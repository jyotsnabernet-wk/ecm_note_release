# DnA Release — July 30–August 5, 2026 | Executive Summary

## Analytics Engineering (Snowflake / dbt)

- **FedRAMP Compliance** — Sanitized Iceberg tables are built and connected to Snowflake for additional Wdesk Classic and Workiva modules, each covered by unit tests, advancing Phase 1 sanitized data availability.
- **FedRAMP — Cost Optimization** — Workiva API models are right-sized by reducing CPU/DPU allocations and removing non-beneficial partitioning, lowering monthly Athena infrastructure spend.
- **FedRAMP — Data Access Control** *(in progress)* — A row access policy is being added to the Workiva document table in Snowflake to restrict federal organization rows from standard consumers, enabling migration for SUV.

## ECM QuickSuite Dashboard Changes

- **Team Mapping Fix** — Three new FY26 Q3 sales teams (Corporate Acquisition Central, Corporate Portco, EMEA-Majors DACH) are added to the team mapping table, recovering ~$1.1M in bookings that were previously dropping to null rows in the attainment pivot.
- **Attainment Accuracy** — FY25 Q2–Q4 bookings attainment totals validated against Snowflake following the team mapping fix.
- **Historical Attainment Export** — A 9-quarter rolling attainment export covering all regions and the full team hierarchy was delivered (DNA-5891), showing bookings vs. SPP target with attainment % and YoY without requiring a new QuickSight visual.
- **Alignment Region Standardization** — Regional reporting on Export and Historical tabs updated to use alignment region (per SPP definition), resolving a ~$300K discrepancy vs. Pulse that was caused by mixing owner region and alignment region across tabs.

---

*Need more detail? Review the technical release notes: DNA Weekly Release Sprint 7/30-8/5*

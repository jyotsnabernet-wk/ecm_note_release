# DnA Analytics Engineering — Release Notes Pipeline

Automated pipeline that pulls **DNA Jira stories** (Analytics Engineering team, Story type, Closed/In Progress) for the current Thu→Wed release window, optionally enriches sections with an **LLM** (Cursor SDK / claude-sonnet-4-6), and outputs two types of release communication:

| Mode | Audience | Output |
|------|----------|--------|
| **Release note** (`--note`, default) | Engineers & data consumers | `out/wiki_release_<DATE>.html` — Confluence-ready HTML |
| **Executive summary** (`--summary`) | Stakeholders & leadership | `out/executive_summary_<DATE>.md` — plain-language Markdown |

---

## Quickstart

### Release note (Confluence wiki HTML)

```bash
# Full pipeline: Jira fetch → LLM enrichment → wiki HTML
python jira_pipeline.py

# Skip LLM (heuristic sections only)
python jira_pipeline.py --no-llm

# Custom deploy date
python jira_pipeline.py --deploy-date 2026-06-24
```

Output: **`out/wiki_release_<DEPLOY-DATE>.html`** — paste directly into Confluence (Editor → Insert → HTML).

### Executive summary (Markdown)

```bash
# Closed tickets only → business-language bullets
python jira_pipeline.py --summary

# With a specific deploy date
python jira_pipeline.py --summary --deploy-date 2026-07-22
```

Output: **`out/executive_summary_<DEPLOY-DATE>.md`** — copy/paste into Slack, email, or Confluence.

---

## The two modes

### `--note` (default) — Technical release notes

- Fetches **In Progress + Closed** tickets
- LLM generates: theme title, What / Why / Downstream impact / Recommended actions
- HTML follows `wiki_page_sample.html` structure (8 sections + Contact + trailing spacer)
- Heuristic fallback when `--no-llm` or `CURSOR_API_KEY` is absent

### `--summary` — Executive summary

- Fetches **Closed tickets only** (`--closed-only`)
- LLM generates one plain-English bullet per business domain
- Format: `**Domain** — one-sentence business outcome` (no model names, no SQL jargon)
- Output is a Markdown file with a header and a "see technical notes" footer link

---

## Setup

### 1. Install dependencies

```bash
pip install requests python-dotenv snowflake-connector-python cursor-sdk
```

### 2. Configure `.env`

Copy `.env.example` → `.env` and fill in:

```text
# Jira (required)
JIRA_BASE_URL=https://jira.atl.workiva.net
JIRA_AUTH_MODE=bearer
JIRA_API_TOKEN=<your Jira PAT>
JIRA_EMAIL=you@workiva.com

# Jira filters
DNA_JIRA_PROJECT=DNA
DNA_JIRA_TEAM=Analytics Engineering
DNA_JIRA_TEAM_FIELD=cf[10288]
DNA_JIRA_JQL_SUFFIX=issuetype = Story AND status in (Closed, "In Progress")

# LLM enrichment (optional — auto-enabled when set)
# Get key at: https://cursor.com/dashboard/integrations → API Keys
CURSOR_API_KEY=cursor_...
# CURSOR_AGENT_MODEL=claude-sonnet-4-6
```

> **Note:** `JIRA_AUTH_MODE=bearer` is correct for `jira.atl.workiva.net` (Workiva Data Center).  
> The team field `cf[10288]` = `customfield_10288` (confirmed via Jira field API).  
> Run `python build_release_notes.py --jira-ping-diagnose` to verify auth.

---

## Scripts

| Script | Purpose |
|--------|---------|
| **`jira_pipeline.py`** | **Primary entry point.** Runs all 3 steps in one command; supports `--note` and `--summary` modes. |
| `build_release_notes.py` | Step 1 — fetch from Jira REST API → JSON + Markdown in `dna_jira_release_notes/out/` |
| `llm_sections.py` | Step 2 — call Cursor SDK LLM; `generate_sections()` for release notes, `generate_executive_summary()` for summary mode |
| `generate_wiki_release_html.py` | Step 3 (`--note`) — render Confluence HTML (uses LLM output if provided, heuristics otherwise) |
| `jira_to_wiki.py` | Run steps 2+3 only (on an existing `dna_release_*.json`) |
| `end_to_end_wiki.py` | Snowflake → LLM → HTML (alternative when Jira is unavailable) |
| `snowflake_sso_sql.py` | Query Snowflake via browser SSO (backup data source) |

---

## Pipeline detail

### Step 1 — Jira fetch

`build_release_notes.py` queries Jira REST API v2 with:

```
(project = DNA AND "cf[10288]" = "Analytics Engineering")
AND (updated >= <last Thu> AND updated < <next Thu>)
AND (issuetype = Story AND status in (Closed, "In Progress"))
ORDER BY updated DESC
```

`--summary` mode adds `--closed-only`, restricting to `status = Closed`.

Writes `dna_jira_release_notes/out/dna_release_<UTC>.json` and `.md`.

### Step 2 — LLM enrichment

`llm_sections.py` sends ticket data to `claude-sonnet-4-6` via the Cursor SDK (`Agent.prompt`).

**`--note` mode** — `generate_sections()` returns:

```json
{
  "title": "short theme overview",
  "what_topics": [{ "label": "Domain — action", "bullets": [...], "keys": [...] }],
  "why_groups":  [{ "label": "theme", "bullets": [...] }],
  "downstream_bullets": [...],
  "recommended_actions": [...],
  "pr_authors": [{ "handle": "githubhandle-wk", "domains": "..." }]
}
```

**`--summary` mode** — `generate_executive_summary()` returns:

```json
{
  "date_range": "July 16-22, 2026",
  "bullets": [
    "Domain — one-sentence business outcome"
  ]
}
```

Auto-enabled when `CURSOR_API_KEY` is present. Use `--no-llm` to skip (release note mode only).

### Step 3 — Output generation

**`--note` mode:** `generate_wiki_release_html.py` renders Confluence-compatible HTML following `.cursorrules` and `wiki_page_sample.html`:

- LLM sections used when available; heuristic fallback otherwise
- `<u><strong>snake_case_model</strong></u>` markup for dbt object names
- `DNA-XXXX` references auto-linked to Jira
- All `<li>` elements have `data-uuid` attributes

**`--summary` mode:** `jira_pipeline.py` writes a Markdown file directly:

- H1 header with date range (e.g. `# DnA Release — July 16-22, 2026 | Executive Summary`)
- One bullet per domain: `- **Domain** — plain-English outcome`
- Footer link pointing to the corresponding technical release wiki page

---

## Release window

| Boundary | Rule |
|----------|------|
| **Start** | Last Thursday 00:00 UTC (6 days before deploy Wednesday) |
| **End** | Deploy Wednesday end-of-day (exclusive: next Thursday 00:00 UTC) |

Override "today" for testing: `python build_release_notes.py --anchor 2026-06-09`

---

## Auth troubleshooting

```bash
# Test Jira connection
python build_release_notes.py --jira-ping-diagnose

# Print JQL without running
python build_release_notes.py --dry-run-jql
```

| Symptom | Fix |
|---------|-----|
| `401` | Regenerate PAT at `jira.atl.workiva.net/profile` → Personal Access Tokens |
| `login.jsp` in error | Bearer auth not accepted — check VPN, regenerate PAT |
| `Field 'Team[Team]' does not exist` | Set `DNA_JIRA_TEAM_FIELD=cf[10288]` |
| `No module named 'cursor_sdk'` | `pip install cursor-sdk` |
| LLM step skipped | Add `CURSOR_API_KEY` to `.env` |

---

## Snowflake backup path

When Jira is unavailable, use Snowflake (`LAKE_PROD.JIRA.JIRA_DNA`) directly:

```bash
# Browser SSO login
python snowflake_sso_sql.py --sql-file sql/jira_dna_ae_stories.sql

# Then generate HTML from the JSON output
python end_to_end_wiki.py --no-llm
```

Configure `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_WAREHOUSE` in `.env.snowflake`.

> Snowflake rows reflect the ingest pipeline snapshot — may lag Jira by several hours.

---

## Outputs

| File | Mode | Description |
|------|------|-------------|
| `out/wiki_release_<DATE>.html` | `--note` | Confluence-ready HTML (paste via Editor → Insert → HTML) |
| `out/executive_summary_<DATE>.md` | `--summary` | Plain-language Markdown bullets for Slack, email, or Confluence |
| `dna_jira_release_notes/out/dna_release_<UTC>.json` | both | Raw Jira issues + metadata |
| `dna_jira_release_notes/out/dna_release_<UTC>.md` | both | Human-readable markdown by component |

Add `out/` to `.gitignore` to avoid committing run artifacts.

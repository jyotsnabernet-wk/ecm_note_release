# DnA Release Notes — How It Works

## What Gets Published

| Output | Audience | When |
|--------|----------|------|
| **Release Note** (`wiki_release_<DATE>.html`) | Engineers & data consumers | Every Monday |
| **Executive Summary** (`executive_summary_<DATE>.md`) | Leadership & stakeholders | Every Wednesday |

Both are generated automatically from Jira and published to the repo. The release note is also pushed to Confluence.

---

## Data Source

All content is sourced from **Jira** (`jira.atl.workiva.net`), filtered to:

- **Project:** DNA
- **Team:** Analytics Engineering
- **Issue type:** Story
- **Created:** on or after 2026-01-01
- **Updated:** within the current sprint window (last Thursday → this Wednesday)

---

## Sprint Window

Each release covers a **Thursday → Wednesday** cycle:

```
Sprint start  Thu 00:00 UTC  (6 days before deploy)
Deploy date   Wed             (release day)
```

The window is calculated automatically — no manual date entry needed.

---

## Release Note (`--note`)

- Pulls **In Progress + Closed** tickets
- LLM (Claude) generates narrative sections: What is happening / Why / Downstream impact / Recommended actions
- Output: Confluence-ready HTML, posted automatically each Monday

## Executive Summary (`--summary`)

- Pulls **Closed tickets only** (shipped work)
- LLM generates one plain-English bullet per business domain
- Format: `**Domain** — one-sentence business outcome`
- Output: Markdown file, generated each Wednesday

---

## Automation

Both pipelines run on a schedule via **GitHub Actions** — no manual steps required once configured. Outputs are committed back to this repo and the release note is published to Confluence automatically.

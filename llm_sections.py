#!/usr/bin/env python3
"""
llm_sections.py — call the Cursor SDK (claude-sonnet-4-6) to generate structured
release-note sections (What / Why / Downstream) from raw Jira rows.

Environment:
  CURSOR_API_KEY       — required  (cursor.com/dashboard/integrations → API Keys)
  CURSOR_AGENT_MODEL   — optional, default "claude-sonnet-4-6"
  CURSOR_AGENT_CWD     — optional, default process cwd

Install:
  pip install cursor-sdk
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any


# ── prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a DnA Analytics Engineering release communication writer.
You produce Confluence wiki sections (What is happening / Why / Downstream impact)
from raw Jira story data.

## Output format
Respond with ONLY a single valid JSON object. No markdown fences, no prose, no comments.

Schema (ALL keys required):
{
  "title": "<short comma-separated topic overview for the <h3> theme line, ≤ 120 chars>",
  "executive_summary": [
    "<one bullet per product/domain area — business-impact language, no jargon, ≤ 25 words each>"
  ],
  "what_topics": [
    {
      "label": "<domain — concise action phrase>",
      "bullets": ["<sentence 1>", "<sentence 2>"],
      "keys": ["DNA-XXXX"]
    }
  ],
  "why_groups": [
    {
      "label": "<optional thematic subheading, e.g. 'Product & customer analytics', or '' to omit>",
      "bullets": ["<sentence>"]
    }
  ],
  "downstream_bullets": ["<sentence mentioning specific model names>"],
  "recommended_actions": [
    "<Verify — ...>",
    "<No action required for ...>"
  ],
  "pr_authors": [
    { "handle": "<githubhandle-wk>", "domains": "<comma-separated domain areas>" }
  ]
}

## Content rules
0. **executive_summary** — one bullet per product/domain area, covering ONLY tickets with status "Closed".
   - Written for executives and non-technical stakeholders: lead with the business outcome.
   - Format exactly as: "<Domain Label> — <one sentence, ≤ 20 words, plain English, no model names, no SQL terms>"
   - Example: "ECM — A classification bug is fixed, giving Finance and RevOps more complete pipeline reporting."
   - Do NOT include domains where all tickets are In Progress (no completed work to report).
   - Do NOT use markdown bold (**), backticks, or any markup — plain text only.

1. **what_topics** — group stories by business domain / theme (NOT by status or component).
   - label: "Domain — action phrase"  e.g. "Amplitude — curated event taxonomy"
   - bullets: 2–4 narrative sentences from ticket descriptions
   - Ticket key refs go in the last bullet: "(DNA-5446)" or "(DNA-5394 / DNA-5130)"

2. **why_groups** — 1–2 thematic groups explaining business/technical motivation.
   Leave "label" as "" when there is only one group.

3. **downstream_bullets** — one bullet per affected surface; mention specific dbt model names.

4. **recommended_actions** — 1–3 lines. At minimum: a Verify paragraph and a No-action paragraph.

5. **pr_authors** — one entry per unique assignee who has at least one ticket.
   - "handle": derive GitHub handle as `{firstname}{lastname}-wk` (all lowercase, no spaces).
     Example: "Maxwell Meiser" → "maxwellmeiser-wk", "Isabel Pietri" → "isabelpietri-wk".
   - "domains": 1–3 short labels for the areas that person owns in this release, comma-separated.
     Infer from their ticket summaries/topics, e.g. "GTM metrics", "Customer360", "Workforce", "Platform".
   - Sort by number of tickets owned descending.

## HTML inline markup rules (use HTML, NOT markdown)
- dbt models, columns, tables, metrics: <u><strong>name</strong></u>
- Key technical terms / phrases: <strong>phrase</strong>
- Ticket references (plain text): (DNA-5446)
- Ampersand in prose: &amp;
Do NOT use markdown asterisks, backticks, or fences in JSON values.
"""


def _build_user_prompt(rows: list[dict], deploy_date: str) -> str:
    tickets = []
    for r in rows:
        key  = (r.get("THE_KEY")             or r.get("the_key")             or "").strip()
        summ = (r.get("SUMMARY")             or r.get("summary")             or "").strip()
        desc = (r.get("DESCRIPTION_PREVIEW") or r.get("description_preview") or "").strip()
        comp = str(r.get("COMPONENTS")       or r.get("components")          or "").strip()
        stat = (r.get("STATUS_NAME")         or r.get("status_name")         or "").strip()
        asn  = (r.get("ASSIGNEE_NAME")       or r.get("assignee_name")       or "").strip()
        # Mark closed tickets explicitly so LLM knows which qualify for executive_summary
        closed_flag = " [CLOSED — include in executive_summary]" if stat.lower() == "closed" else " [IN PROGRESS — exclude from executive_summary]"
        tickets.append(
            f"### {key} [{stat}]{closed_flag}\n"
            f"Summary: {summ}\n"
            f"Components: {comp}\n"
            f"Assignee: {asn}\n"
            f"Description (first 500 chars):\n{desc}"
        )
    return (
        f"Release deploy date: {deploy_date}\n\n"
        "Tickets:\n\n"
        + "\n\n".join(tickets)
    )


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(
                "Model did not return valid JSON.\nFirst 600 chars:\n" + text[:600]
            )
        obj = json.loads(raw[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object, got {type(obj).__name__}")
    return obj


# ── public API ────────────────────────────────────────────────────────────────

def generate_sections(
    rows: list[dict],
    deploy_date: str,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Call the Cursor SDK LLM and return structured sections dict with keys:
      title, what_topics, why_groups, downstream_bullets, recommended_actions
    """
    try:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError
    except ImportError:
        raise SystemExit(
            "Missing cursor-sdk.  Install with:  pip install cursor-sdk\n"
            "Then set CURSOR_API_KEY in .env (cursor.com/dashboard/integrations)."
        )

    api_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit(
            "CURSOR_API_KEY is not set.\n"
            "Add it to .env:  CURSOR_API_KEY=cursor_...\n"
            "Or run with --no-llm to skip LLM enrichment."
        )

    model = (os.environ.get("CURSOR_AGENT_MODEL") or "claude-sonnet-4-6").strip()

    user_prompt  = _build_user_prompt(rows, deploy_date)
    full_message = (
        "Follow the SYSTEM instructions exactly, then answer using only the USER context.\n\n"
        "=== SYSTEM ===\n"
        f"{_SYSTEM_PROMPT.strip()}\n\n"
        "=== USER ===\n"
        f"{user_prompt.strip()}\n\n"
        "Output: single JSON object only — no markdown fences, no prose."
    )

    if verbose:
        print(
            f"[llm] Agent.prompt  model={model!r}  chars={len(full_message)}",
            file=sys.stderr,
        )

    try:
        result = Agent.prompt(
            full_message,
            AgentOptions(
                api_key=api_key,
                model=model,
            ),
        )
    except CursorAgentError as e:
        raise SystemExit(
            f"Cursor agent did not start (auth/config/network): {e}\n"
            "Check CURSOR_API_KEY and network.  Use --no-llm to skip."
        ) from e

    status = str(getattr(result, "status", "")).lower()
    if verbose:
        print(
            f"[llm] run_id={getattr(result, 'id', None)!r}  status={status!r}",
            file=sys.stderr,
        )
    if status in ("error", "cancelled", "expired"):
        raise SystemExit(
            f"Cursor agent run ended with status={result.status!r}.\n"
            "Use --no-llm to skip."
        )

    text = (result.result or "").strip()
    if verbose:
        print(f"[llm] response chars={len(text)}", file=sys.stderr)

    return _parse_json(text)


_EXEC_SUMMARY_PROMPT = """\
You are a DnA Analytics Engineering release communication writer for executive stakeholders.

Given a list of CLOSED Jira tickets, produce a concise executive summary.

## Output format
Respond with ONLY a valid JSON object. No markdown fences, no prose.

Schema:
{
  "date_range": "<e.g. July 9-15, 2026 — infer from ticket dates if possible, else leave empty>",
  "bullets": [
    "<Domain Label> — <one sentence business impact, ≤ 20 words, plain English>"
  ]
}

## Rules
- One bullet per business domain/product area (group related tickets).
- Lead with the business outcome — what does this mean for Finance, GTM, Support, Product teams?
- No model names, SQL terms, ticket keys, or technical jargon.
- Plain text only — no markdown bold (**), no backticks, no HTML tags.
- If a domain has no shipped (Closed) tickets, omit it entirely.
- Keep bullets parallel in structure: "<Domain> — <verb phrase or completed outcome>."
"""


def generate_executive_summary(
    rows: list[dict],
    deploy_date: str,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Call the LLM to generate a concise executive summary from Closed tickets only.
    Returns dict with keys: date_range, bullets.
    """
    try:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError
    except ImportError:
        raise SystemExit("Missing cursor-sdk.  Install with:  pip install cursor-sdk")

    api_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("CURSOR_API_KEY is not set. Add it to .env or run with --no-llm.")

    model = (os.environ.get("CURSOR_AGENT_MODEL") or "claude-sonnet-4-6").strip()

    user_prompt = _build_user_prompt(rows, deploy_date)
    full_message = (
        f"{_EXEC_SUMMARY_PROMPT.strip()}\n\n"
        "=== TICKETS (Closed only) ===\n"
        f"{user_prompt.strip()}\n\n"
        "Output: single JSON object only."
    )

    if verbose:
        print(f"[llm-summary] model={model!r}  chars={len(full_message)}", file=sys.stderr)

    try:
        result = Agent.prompt(full_message, AgentOptions(api_key=api_key, model=model))
    except CursorAgentError as e:
        raise SystemExit(f"Cursor agent error: {e}\nUse --no-llm to skip.") from e

    status = str(getattr(result, "status", "")).lower()
    if status in ("error", "cancelled", "expired"):
        raise SystemExit(f"Cursor agent run ended with status={result.status!r}.")

    text = (result.result or "").strip()
    if verbose:
        print(f"[llm-summary] response chars={len(text)}", file=sys.stderr)

    return _parse_json(text)

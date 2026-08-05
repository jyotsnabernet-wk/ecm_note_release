#!/usr/bin/env python3
"""
bi_dashboard_changes.py — Download BI dashboard change log from Snowflake and
generate a formatted "ECM QuickSuite Dashboard Changes" section for the executive summary.

Steps:
  1. Query GOLD_PROD.BI.V_ECM_DASHBOARD_CHANGE_LOG_LATEST via Snowflake HTTP SQL API
     for the current Thu→Wed release window.
  2. Save results to BI/Results_<DEPLOY-DATE>-<HH><MM>.csv
  3. Use LLM (Cursor SDK) to generate grouped bullet points.
  4. Return formatted markdown section.

Usage:
  python bi_dashboard_changes.py                          # auto date window
  python bi_dashboard_changes.py --start 2026-07-23 --end 2026-07-29
  python bi_dashboard_changes.py --from-csv BI/Results_2026-07-29-0900.csv  # skip download
  python bi_dashboard_changes.py --no-llm                # raw bullets from CSV titles only

Environment (.env or .env.snowflake):
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_PATH  (key-pair auth)
  -- or --
  SNOWFLAKE_BEARER_TOKEN                                           (PAT / OAuth token)
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for f in (".env.snowflake", ".env"):
        p = _ROOT / f
        if p.is_file():
            load_dotenv(p, override=False)


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)       # type: ignore[arg-type]
    spec.loader.exec_module(mod)                       # type: ignore[union-attr]
    return mod


def _sprint_window(anchor: date | None = None) -> tuple[date, date]:
    """Return (thursday_start, wednesday_end) for the current release window."""
    today = anchor or date.today()
    # Next Wednesday on or after today
    deploy_wed = today + timedelta(days=(2 - today.weekday()) % 7)
    sprint_thu = deploy_wed - timedelta(days=6)
    return sprint_thu, deploy_wed


def _csv_path(out_dir: Path, deploy_date: date) -> Path:
    now = datetime.now(timezone.utc)
    return out_dir / f"Results_{deploy_date.isoformat()}-{now.strftime('%H%M')}.csv"


# ── Snowflake download ─────────────────────────────────────────────────────────

def _read_sql_template() -> str:
    sql_file = _ROOT / "sql" / "bi_dashboard_changes.sql"
    if not sql_file.is_file():
        raise SystemExit(f"SQL file not found: {sql_file}")
    return sql_file.read_text(encoding="utf-8")


def _inject_dates(sql: str, start: date, end: date) -> str:
    """Replace NULL overrides with actual dates so no CTE calc is needed."""
    sql = re.sub(
        r"NULL::DATE\s+AS\s+start_date_override",
        f"DATE '{start.isoformat()}' AS start_date_override",
        sql,
    )
    sql = re.sub(
        r"NULL::DATE\s+AS\s+end_date_override",
        f"DATE '{end.isoformat()}' AS end_date_override",
        sql,
    )
    return sql


def download_from_snowflake(start: date, end: date, out_dir: Path) -> Path:
    """Connect to Snowflake and save results as CSV. Returns path.

    Auth priority:
      1. SNOWFLAKE_PROGRAMMATIC_ACCESS_TOKEN  — PAT (works in CI / GitHub Actions)
      2. externalbrowser (SSO)                — local use only
    """
    _load_dotenv()

    try:
        import snowflake.connector
    except ImportError:
        raise SystemExit(
            "Missing snowflake-connector-python.\n"
            "Run: pip install snowflake-connector-python"
        )

    account   = (os.environ.get("SNOWFLAKE_ACCOUNT")   or "").strip()
    user      = (os.environ.get("SNOWFLAKE_USER")       or "").strip()
    warehouse = (os.environ.get("SNOWFLAKE_WAREHOUSE")  or "").strip()
    pat       = (os.environ.get("SNOWFLAKE_PROGRAMMATIC_ACCESS_TOKEN") or "").strip()

    if not account or not user:
        raise SystemExit(
            "Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER in .env.snowflake\n"
            "e.g. SNOWFLAKE_ACCOUNT=xy12345.us-east-1.aws"
        )
    if not warehouse:
        raise SystemExit(
            "Set SNOWFLAKE_WAREHOUSE in .env.snowflake\n"
            "e.g. SNOWFLAKE_WAREHOUSE=COMPUTE_WH"
        )

    if pat:
        print(f"[bi] Connecting to Snowflake via PAT…", file=sys.stderr)
        kwargs: dict = {
            "account": account,
            "user": user,
            "warehouse": warehouse,
            "authenticator": "programmatic_access_token",
            "token": pat,
        }
    else:
        print(f"[bi] Connecting to Snowflake via browser SSO…", file=sys.stderr)
        kwargs = {
            "account": account,
            "user": user,
            "warehouse": warehouse,
            "authenticator": "externalbrowser",
        }

    sql = _inject_dates(_read_sql_template(), start, end)
    print(f"[bi] RELEASE_DATE window: {start} → {end}", file=sys.stderr)

    conn = snowflake.connector.connect(**kwargs)
    try:
        cur = conn.cursor(snowflake.connector.DictCursor)
        cur.execute(f'USE WAREHOUSE "{warehouse}"')
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        conn.close()

    print(f"[bi] {len(rows)} rows returned", file=sys.stderr)
    if not rows:
        print("[bi] No BI changes found for this window.", file=sys.stderr)
        return Path("")

    deploy   = end
    csv_path = _csv_path(out_dir, deploy)
    out_dir.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[bi] Saved → {csv_path}", file=sys.stderr)
    return csv_path


# ── CSV reader ─────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── LLM section generator ─────────────────────────────────────────────────────

_BI_SYSTEM_PROMPT = """\
You are a DnA Analytics Engineering release communication writer.
Given a list of BI dashboard change log entries, produce a concise
"ECM QuickSuite Dashboard Changes" section for an executive summary.

## Output format
Respond with ONLY a valid JSON object. No markdown fences, no prose.

Schema:
{
  "bullets": [
    { "label": "<concise topic label>", "text": "<one sentence, plain English, ≤ 25 words>" }
  ]
}

## Rules
- Group related rows under one bullet when they share a theme (e.g. several Data Logic fixes → one bullet).
- Label = short noun phrase that names the feature or fix (e.g. "Pipeline & Coverage Accuracy").
- Text = lead with what changed and the business impact. No model names, SQL terms, or metric IDs.
- If a change is a bug fix, say so briefly. If an enhancement, say so.
- Plain text only — no markdown bold (**), no backticks, no HTML tags in the JSON values.
- Order: New Feature → Data Logic → Mapping → Filter/UX.
"""


def generate_bi_section_llm(rows: list[dict], verbose: bool = False) -> str:
    """Call LLM to generate formatted BI bullets. Returns markdown string."""
    try:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError
    except ImportError:
        raise SystemExit("Missing cursor-sdk.  Run: pip install cursor-sdk")

    api_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("CURSOR_API_KEY not set. Add to .env or use --no-llm.")
    model = (os.environ.get("CURSOR_AGENT_MODEL") or "claude-sonnet-4-6").strip()

    rows_text = "\n\n".join(
        f"### {r.get('CHANGE_ID','')} [{r.get('CHANGE_CATEGORY','')}]\n"
        f"Title: {r.get('CHANGE_TITLE','')}\n"
        f"Dashboard: {r.get('DASHBOARD_NAME','')}\n"
        f"Description: {r.get('CHANGE_DESCRIPTION','')}\n"
        f"Before: {r.get('BEFORE_SUMMARY','')}\n"
        f"After: {r.get('AFTER_SUMMARY','')}\n"
        f"Impact: {r.get('STAKEHOLDER_IMPACT','')}"
        for r in rows
    )

    prompt = (
        f"{_BI_SYSTEM_PROMPT.strip()}\n\n"
        "=== CHANGE LOG ENTRIES ===\n"
        f"{rows_text}\n\n"
        "Output: single JSON object only."
    )

    if verbose:
        print(f"[bi-llm] model={model!r}  chars={len(prompt)}", file=sys.stderr)

    try:
        result = Agent.prompt(prompt, AgentOptions(api_key=api_key, model=model))
    except CursorAgentError as e:
        raise SystemExit(f"Cursor agent error: {e}") from e

    text = (result.result or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    bullets = data.get("bullets", [])
    return _format_bullets(bullets)


def _format_bullets_from_csv(rows: list[dict]) -> str:
    """Fallback: one bullet per row using CSV fields directly (no LLM)."""
    seen: set[str] = set()
    bullets = []
    for r in rows:
        label = (r.get("CHANGE_TITLE") or "").strip()
        impact = (r.get("STAKEHOLDER_IMPACT") or r.get("CHANGE_DESCRIPTION") or "").strip()
        # truncate at first sentence
        impact = re.split(r"(?<=[.!?])\s", impact)[0].strip()
        key = label.lower()
        if key not in seen:
            seen.add(key)
            bullets.append({"label": label, "text": impact})
    return _format_bullets(bullets)


def _format_bullets(bullets: list[dict]) -> str:
    lines = []
    for b in bullets:
        label = str(b.get("label", "")).strip()
        text  = str(b.get("text", "")).strip()
        if label and text:
            lines.append(f"- **{label}** — {text}")
        elif label:
            lines.append(f"- **{label}**")
    return "\n".join(lines)


# ── public API ────────────────────────────────────────────────────────────────

def build_bi_section(rows: list[dict], *, use_llm: bool = True, verbose: bool = False) -> str:
    """Return the full markdown BI section ready to append to executive summary."""
    if not rows:
        return ""
    body = generate_bi_section_llm(rows, verbose=verbose) if use_llm else _format_bullets_from_csv(rows)
    return f"\n## ECM QuickSuite Dashboard Changes\n\n{body}\n"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_dotenv()
    today = date.today()
    default_start, default_end = _sprint_window(today)

    p = argparse.ArgumentParser(
        description="Download BI change log from Snowflake and generate exec summary section.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start", default=default_start.isoformat(), metavar="YYYY-MM-DD",
                   help="Sprint start date (Thursday)")
    p.add_argument("--end",   default=default_end.isoformat(),   metavar="YYYY-MM-DD",
                   help="Sprint end / deploy date (Wednesday)")
    p.add_argument("--from-csv", type=Path, metavar="FILE",
                   help="Skip Snowflake download; use existing CSV file")
    p.add_argument("--bi-dir", type=Path, default=_ROOT / "BI",
                   help="Directory to save downloaded CSV")
    p.add_argument("--no-llm", dest="use_llm", action="store_false", default=True,
                   help="Skip LLM; format bullets from CSV titles only")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--append-to", type=Path, metavar="FILE",
                   help="Append BI section to this executive summary .md file")
    args = p.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    # ── Step 1: get rows ──
    if args.from_csv:
        csv_path = args.from_csv
        print(f"[bi] Using existing CSV: {csv_path}", file=sys.stderr)
    else:
        csv_path = download_from_snowflake(start, end, args.bi_dir)
        if not csv_path or not csv_path.is_file():
            print("[bi] No data — nothing to add.", file=sys.stderr)
            return

    rows = load_csv(csv_path)
    if not rows:
        print("[bi] CSV is empty.", file=sys.stderr)
        return
    print(f"[bi] {len(rows)} changes loaded", file=sys.stderr)

    # ── Step 2: generate section ──
    section = build_bi_section(rows, use_llm=args.use_llm, verbose=args.verbose)
    print(section)

    # ── Step 3: optionally append to exec summary ──
    if args.append_to:
        target = args.append_to
        if not target.is_file():
            raise SystemExit(f"File not found: {target}")
        content = target.read_text(encoding="utf-8")
        # Remove existing BI section if present, then append fresh one
        content = re.sub(
            r"\n## ECM QuickSuite Dashboard Changes\n[\s\S]*?(?=\n---|\Z)",
            "",
            content,
        ).rstrip()
        # Insert before the trailing --- footer if present
        if "\n---\n" in content:
            content = content.replace("\n---\n", f"\n{section}\n---\n", 1)
        else:
            content = content + "\n" + section
        target.write_text(content, encoding="utf-8")
        print(f"[bi] BI section written to {target}", file=sys.stderr)


if __name__ == "__main__":
    main()

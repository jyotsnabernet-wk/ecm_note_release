#!/usr/bin/env python3
"""
pipeline.py — one-command DnA release-notes pipeline.

Steps:
  1. Fetch Jira stories (DNA + Analytics Engineering + Story + Closed/In Progress)
  2. Enrich with LLM (Cursor SDK, model: claude-sonnet-4-6) — auto-enabled when CURSOR_API_KEY is set
  3. Generate Confluence wiki HTML → out/wiki_release_<DEPLOY-DATE>.html

Config files:
  .env              — JIRA_BASE_URL, JIRA_AUTH_MODE, JIRA_API_TOKEN, CURSOR_API_KEY, ...
  (Snowflake not needed — Jira is the data source here)

Usage:
  python pipeline.py                          # full run, LLM auto-detected
  python pipeline.py --no-llm                 # skip LLM, use heuristic sections
  python pipeline.py --deploy-date 2026-06-24 # override deploy date
  python pipeline.py --verbose-llm            # show LLM debug output
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_ROOT / ".env", override=False)


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)      # type: ignore[arg-type]
    spec.loader.exec_module(mod)                      # type: ignore[union-attr]
    return mod


def _next_wednesday(from_date: date) -> date:
    delta = (3 - from_date.isoweekday()) % 7
    return from_date + timedelta(days=delta)


def _latest_jira_json(out_dir: Path) -> Path | None:
    candidates = sorted(
        out_dir.glob("dna_release_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ── step 1: Jira fetch ────────────────────────────────────────────────────────

def step_jira_fetch(*, output_dir: Path) -> Path:
    """Run build_release_notes.py and return the path of the JSON it wrote."""
    print("\n── Step 1/3  Fetch from Jira ────────────────────────────────", file=sys.stderr)
    cmd = [
        sys.executable,
        str(_ROOT / "build_release_notes.py"),
        "--output-dir", str(output_dir),
    ]
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        raise SystemExit(f"build_release_notes.py failed (exit {result.returncode})")

    json_path = _latest_jira_json(output_dir)
    if not json_path:
        raise SystemExit(f"No dna_release_*.json found in {output_dir} after fetch.")
    return json_path


# ── step 2: LLM enrichment ────────────────────────────────────────────────────

def step_llm(rows: list[dict], deploy_date: str, *, verbose: bool = False) -> dict:
    print("\n── Step 2/3  LLM enrichment ─────────────────────────────────", file=sys.stderr)
    model = os.environ.get("CURSOR_AGENT_MODEL", "claude-sonnet-4-6")
    print(f"   model: {model}", file=sys.stderr)
    mod = _load_module("llm_sections", _ROOT / "llm_sections.py")
    sections = mod.generate_sections(rows, deploy_date, verbose=verbose)
    n = len(sections.get("what_topics") or [])
    print(f"   ✓ {n} topic(s) generated", file=sys.stderr)
    return sections


# ── step 3: HTML generation ───────────────────────────────────────────────────

def _normalise_jira_issue(issue: dict) -> dict:
    """Map build_release_notes.py issue → Snowflake-style uppercase row."""
    comps_raw = issue.get("components") or []
    if comps_raw and isinstance(comps_raw[0], str):
        comps = json.dumps([{"name": c} for c in comps_raw])
    else:
        comps = json.dumps(comps_raw)
    return {
        "THE_KEY":             issue.get("key", ""),
        "SUMMARY":             issue.get("summary", ""),
        "DESCRIPTION_PREVIEW": issue.get("description_excerpt", ""),
        "STATUS_NAME":         issue.get("status", ""),
        "PRIORITY_NAME":       issue.get("priority", ""),
        "ASSIGNEE_NAME":       issue.get("assignee", ""),
        "COMPONENTS":          comps,
        "LABELS":              json.dumps(issue.get("labels") or []),
        "UPDATED_AT":          issue.get("updated", ""),
        "CREATED_AT":          issue.get("created", ""),
        "RESOLUTION_AT":       issue.get("resolutiondate", ""),
    }


def step_html(
    rows: list[dict],
    *,
    sent_date: str,
    deploy_date: str,
    title: str,
    out_dir: Path,
    llm_sections: dict | None,
    jira_browse_base: str,
) -> Path:
    print("\n── Step 3/3  Generate wiki HTML ─────────────────────────────", file=sys.stderr)
    migration_note = (
        f"Consumers relying on any changed columns should validate and update downstream "
        f"references within 30 days of the deploy ({deploy_date})."
    )
    mod = _load_module("generate_wiki_release_html", _ROOT / "generate_wiki_release_html.py")
    html = mod.build_html(
        rows,
        sent_date=sent_date,
        deploy_date=deploy_date,
        title=title,
        migration_note=migration_note,
        jira_browse_base=jira_browse_base,
        llm_sections=llm_sections or None,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"wiki_release_{deploy_date}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_dotenv()
    today = date.today()
    default_deploy = _next_wednesday(today).isoformat()

    p = argparse.ArgumentParser(
        description="Jira → LLM → Wiki HTML pipeline (one command).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sent-date",   default=today.isoformat(), metavar="YYYY-MM-DD",
                   help="Date shown in 'Sent on'")
    p.add_argument("--deploy-date", default=default_deploy,    metavar="YYYY-MM-DD",
                   help="Deploy / release date")
    p.add_argument("--title", default="DnA — Analytics Engineering (DNA Stories)",
                   help="h3 theme title (overridden by LLM when available)")
    p.add_argument("--jira-browse-base", default="https://jira.atl.workiva.net/browse")
    p.add_argument("--output-dir", "-o", type=Path, default=_ROOT / "out",
                   help="Directory for wiki HTML output")
    p.add_argument("--jira-out-dir", type=Path,
                   default=_ROOT / "dna_jira_release_notes" / "out",
                   help="Directory where build_release_notes.py writes its JSON")

    llm_group = p.add_mutually_exclusive_group()
    llm_group.add_argument("--llm",    dest="use_llm", action="store_true",  default=None,
                           help="Force LLM enrichment (requires CURSOR_API_KEY)")
    llm_group.add_argument("--no-llm", dest="use_llm", action="store_false",
                           help="Skip LLM — use heuristic section generation")
    p.add_argument("--verbose-llm", action="store_true",
                   help="Print LLM debug info to stderr")
    args = p.parse_args()

    deploy_date = args.deploy_date
    sent_date   = args.sent_date

    # ── step 1: fetch ──
    json_path = step_jira_fetch(output_dir=args.jira_out_dir)
    blob   = json.loads(json_path.read_text(encoding="utf-8"))
    issues = blob.get("issues") or blob
    rows   = [_normalise_jira_issue(i) for i in issues]
    # prefer deploy date from meta if not explicitly overridden
    if args.deploy_date == default_deploy and blob.get("meta", {}).get("release_wednesday"):
        deploy_date = blob["meta"]["release_wednesday"]
    print(f"   {len(rows)} issues  deploy={deploy_date}", file=sys.stderr)

    # ── step 2: LLM ──
    cursor_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    use_llm = args.use_llm
    if use_llm is None:
        use_llm = bool(cursor_key)

    llm_sections: dict = {}
    if use_llm:
        if not cursor_key:
            raise SystemExit(
                "CURSOR_API_KEY is required for LLM enrichment.\n"
                "Add it to .env or run with --no-llm to skip."
            )
        llm_sections = step_llm(rows, deploy_date, verbose=args.verbose_llm)
    else:
        print("\n── Step 2/3  LLM enrichment ─────────────────────────────────", file=sys.stderr)
        print("   skipped (--no-llm or no CURSOR_API_KEY in .env)", file=sys.stderr)

    # ── step 3: HTML ──
    out_path = step_html(
        rows,
        sent_date=sent_date,
        deploy_date=deploy_date,
        title=args.title,
        out_dir=args.output_dir,
        llm_sections=llm_sections or None,
        jira_browse_base=args.jira_browse_base,
    )

    print(f"\n{'━'*60}", file=sys.stderr)
    print(f"✓  {len(rows)} issues  →  {out_path}", file=sys.stderr)
    llm_status = f"LLM: {os.environ.get('CURSOR_AGENT_MODEL','claude-sonnet-4-6')}" if use_llm and llm_sections else "LLM: skipped"
    print(f"   {llm_status}", file=sys.stderr)
    print(f"   Paste into Confluence → Editor → Insert → HTML", file=sys.stderr)


if __name__ == "__main__":
    main()

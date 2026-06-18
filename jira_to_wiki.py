#!/usr/bin/env python3
"""
jira_to_wiki.py — Jira JSON → Confluence wiki HTML.

Reads the most recent (or specified) dna_release_*.json produced by build_release_notes.py,
normalises fields to the Snowflake row format, then generates wiki HTML via
generate_wiki_release_html.build_html (optionally enriched by the Cursor LLM).

Usage:
  # Latest JSON in dna_jira_release_notes/out/ (default)
  python jira_to_wiki.py

  # Explicit input file
  python jira_to_wiki.py --input dna_jira_release_notes/out/dna_release_20260617T192155Z.json

  # Skip LLM (use heuristic sections)
  python jira_to_wiki.py --no-llm

  # Debug LLM call
  python jira_to_wiki.py --verbose-llm
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
    load_dotenv(_ROOT / ".env.snowflake", override=False)
    load_dotenv(_ROOT / ".env", override=False)


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)      # type: ignore[arg-type]
    spec.loader.exec_module(mod)                      # type: ignore[union-attr]
    return mod


def _next_wednesday(from_date: date) -> date:
    delta = (3 - from_date.isoweekday()) % 7
    return from_date + timedelta(days=delta)


def _latest_jira_json() -> Path:
    """Return the most-recently modified dna_release_*.json in dna_jira_release_notes/out/."""
    candidates = sorted(
        (_ROOT / "dna_jira_release_notes" / "out").glob("dna_release_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(
            "No dna_release_*.json found in dna_jira_release_notes/out/.\n"
            "Run  python build_release_notes.py  first."
        )
    return candidates[0]


# ── normalise Jira issue → Snowflake-style row ────────────────────────────────

def _normalise(issue: dict) -> dict:
    """
    Map build_release_notes.py issue keys to the uppercase Snowflake column names
    expected by generate_wiki_release_html.build_html.
    """
    # components: Jira gives ["development"] — wrap as [{"name": "..."}] so _parse_components works
    comps_raw = issue.get("components") or []
    if comps_raw and isinstance(comps_raw[0], str):
        comps = json.dumps([{"name": c} for c in comps_raw])
    else:
        comps = json.dumps(comps_raw)

    return {
        "THE_KEY":            issue.get("key", ""),
        "SUMMARY":            issue.get("summary", ""),
        "DESCRIPTION_PREVIEW": issue.get("description_excerpt", ""),
        "STATUS_NAME":        issue.get("status", ""),
        "PRIORITY_NAME":      issue.get("priority", ""),
        "ASSIGNEE_NAME":      issue.get("assignee", ""),
        "COMPONENTS":         comps,
        "LABELS":             json.dumps(issue.get("labels") or []),
        "UPDATED_AT":         issue.get("updated", ""),
        "CREATED_AT":         issue.get("created", ""),
        "RESOLUTION_AT":      issue.get("resolutiondate", ""),
    }


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_llm(rows: list[dict], deploy_date: str, *, verbose: bool = False) -> dict:
    mod = _load_module("llm_sections", _ROOT / "llm_sections.py")
    return mod.generate_sections(rows, deploy_date, verbose=verbose)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_dotenv()
    today = date.today()
    default_deploy = _next_wednesday(today).isoformat()

    p = argparse.ArgumentParser(
        description="Convert build_release_notes.py JSON → Confluence wiki HTML.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input", "-i", type=Path, default=None,
                   help="dna_release_*.json file (default: latest in dna_jira_release_notes/out/)")
    p.add_argument("--sent-date",   default=today.isoformat(),    metavar="YYYY-MM-DD")
    p.add_argument("--deploy-date", default=default_deploy,       metavar="YYYY-MM-DD")
    p.add_argument("--title",       default="DnA — Analytics Engineering (DNA Stories)")
    p.add_argument("--subtitle",    default="")
    p.add_argument("--jira-browse-base", default="https://jira.atl.workiva.net/browse")
    p.add_argument("--output-dir",  "-o", type=Path, default=_ROOT / "out")

    llm_group = p.add_mutually_exclusive_group()
    llm_group.add_argument("--llm",    dest="use_llm", action="store_true",  default=None,
                           help="Force LLM enrichment (requires CURSOR_API_KEY)")
    llm_group.add_argument("--no-llm", dest="use_llm", action="store_false",
                           help="Skip LLM — use heuristic section generation")
    p.add_argument("--verbose-llm", action="store_true")
    args = p.parse_args()

    # ── load Jira JSON ──
    src = args.input or _latest_jira_json()
    print(f"[input]    {src}", file=sys.stderr)
    blob = json.loads(src.read_text(encoding="utf-8"))
    issues = blob.get("issues") or blob  # handle bare list or wrapped object
    meta   = blob.get("meta", {})

    deploy_date = args.deploy_date or meta.get("release_wednesday") or default_deploy
    sent_date   = args.sent_date

    rows = [_normalise(i) for i in issues]
    print(f"           {len(rows)} issues  deploy={deploy_date}", file=sys.stderr)

    # ── LLM ──
    cursor_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    use_llm = args.use_llm
    if use_llm is None:
        use_llm = bool(cursor_key)

    llm_sections: dict = {}
    if use_llm:
        if not cursor_key:
            raise SystemExit(
                "CURSOR_API_KEY required for LLM enrichment.\n"
                "Add it to .env or run with --no-llm."
            )
        model = os.environ.get("CURSOR_AGENT_MODEL", "claude-sonnet-4-6")
        print(f"[llm]      model={model}", file=sys.stderr)
        llm_sections = _call_llm(rows, deploy_date, verbose=args.verbose_llm)
        print(f"           {len(llm_sections.get('what_topics', []))} topic(s)", file=sys.stderr)
    else:
        print("[llm]      skipped (--no-llm or no CURSOR_API_KEY)", file=sys.stderr)

    # ── HTML ──
    migration_note = (
        f"Consumers relying on any changed columns should validate and update downstream "
        f"references within 30 days of the deploy ({deploy_date})."
    )
    mod_html = _load_module("generate_wiki_release_html", _ROOT / "generate_wiki_release_html.py")
    html = mod_html.build_html(
        rows,
        sent_date=sent_date,
        deploy_date=deploy_date,
        title=args.title,
        subtitle=args.subtitle,
        migration_note=migration_note,
        jira_browse_base=args.jira_browse_base,
        llm_sections=llm_sections or None,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"wiki_release_{deploy_date}.html"
    out.write_text(html, encoding="utf-8")
    print(f"\n✓  {out}", file=sys.stderr)
    print(f"   Paste into Confluence → Editor → Insert → HTML", file=sys.stderr)


if __name__ == "__main__":
    main()

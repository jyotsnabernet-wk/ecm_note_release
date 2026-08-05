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

def step_jira_fetch(*, output_dir: Path, closed_only: bool = False) -> Path:
    """Run build_release_notes.py and return the path of the JSON it wrote."""
    print("\n── Step 1/3  Fetch from Jira ────────────────────────────────", file=sys.stderr)
    cmd = [
        sys.executable,
        str(_ROOT / "build_release_notes.py"),
        "--output-dir", str(output_dir),
    ]
    if closed_only:
        cmd.append("--closed-only")
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
    start_date: str = "",
    end_date: str = "",
    detail_link: str = "",
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
        start_date=start_date,
        end_date=end_date,
        detail_link=detail_link,
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

    # ── mode ──
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--note", dest="mode", action="store_const", const="note", default="note",
        help="(default) Full technical release notes — In Progress + Closed, all sections.",
    )
    mode_group.add_argument(
        "--summary", dest="mode", action="store_const", const="summary",
        help="Executive summary only — Closed tickets, business language, one bullet per domain.",
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

    bi_group = p.add_mutually_exclusive_group()
    bi_group.add_argument("--bi", dest="bi_mode", action="store_const", const="download",
                          default="download",
                          help="(default) Download BI changes from Snowflake then append to summary")
    bi_group.add_argument("--bi-csv", dest="bi_csv", type=Path, default=None, metavar="FILE",
                          help="Use existing CSV instead of downloading from Snowflake")
    bi_group.add_argument("--no-bi", dest="bi_mode", action="store_const", const="skip",
                          help="Skip BI section entirely")
    args = p.parse_args()

    is_summary = args.mode == "summary"
    deploy_date = args.deploy_date
    sent_date   = args.sent_date

    # ── step 1: fetch ──
    json_path = step_jira_fetch(output_dir=args.jira_out_dir, closed_only=is_summary)
    blob   = json.loads(json_path.read_text(encoding="utf-8"))
    issues = blob.get("issues") or blob
    rows   = [_normalise_jira_issue(i) for i in issues]
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
        if is_summary:
            # --summary: lightweight exec-summary-only LLM call
            print("\n── Step 2/3  LLM executive summary ─────────────────────────", file=sys.stderr)
            model = os.environ.get("CURSOR_AGENT_MODEL", "claude-sonnet-4-6")
            print(f"   model: {model}", file=sys.stderr)
            mod = _load_module("llm_sections", _ROOT / "llm_sections.py")
            exec_data = mod.generate_executive_summary(rows, deploy_date, verbose=args.verbose_llm)
            llm_sections = {"executive_summary": exec_data.get("bullets", [])}
            print(f"   ✓ {len(llm_sections['executive_summary'])} bullet(s) generated", file=sys.stderr)
        else:
            llm_sections = step_llm(rows, deploy_date, verbose=args.verbose_llm)
    else:
        print("\n── Step 2/3  LLM enrichment ─────────────────────────────────", file=sys.stderr)
        print("   skipped (--no-llm or no CURSOR_API_KEY in .env)", file=sys.stderr)

    # ── step 3: HTML ──
    start_date = blob.get("meta", {}).get("start_date", "")
    end_date   = blob.get("meta", {}).get("end_inclusive_date", "")
    detail_link = ""
    if start_date and end_date:
        try:
            s = date.fromisoformat(start_date)
            e = date.fromisoformat(end_date)
            detail_link = f"DNA Weekly Release Sprint {s.month}/{s.day}-{e.month}/{e.day}"
        except Exception:
            pass

    if is_summary:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        bullets = llm_sections.get("executive_summary", [])

        # Build date label e.g. "July 16-22, 2026"
        date_label = ""
        if start_date and end_date:
            try:
                import calendar
                s = date.fromisoformat(start_date)
                e = date.fromisoformat(end_date)
                if s.month == e.month:
                    date_label = f"{calendar.month_name[s.month]} {s.day}-{e.day}, {e.year}"
                else:
                    date_label = (
                        f"{calendar.month_name[s.month]} {s.day} - "
                        f"{calendar.month_name[e.month]} {e.day}, {e.year}"
                    )
            except Exception:
                date_label = f"{start_date} - {end_date}"

        # ── Markdown output ──
        md_lines = [
            f"# DnA Release — {date_label} | Executive Summary" if date_label
            else "# DnA Release | Executive Summary",
            "",
        ]
        import re as _re
        for bullet in bullets:
            b = str(bullet).strip()
            if not b:
                continue
            # Bold the domain label (everything before " — " or " - ")
            m = _re.match(r"^(\*\*)?(.+?)(\*\*)?\s*[—\-–]\s*(.+)$", b, _re.DOTALL)
            if m:
                domain = m.group(2).strip()
                rest   = m.group(4).strip()
                md_lines.append(f"- **{domain}** — {rest}")
            else:
                md_lines.append(f"- {b}")
        md_lines += [
            "",
            f"*Need more detail? Review the technical release notes: {detail_link}*" if detail_link else "",
        ]

        md_path = out_dir / f"executive_summary_{deploy_date}.md"
        md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

        # ── Optional: append BI dashboard changes section ──
        bi_csv = args.bi_csv
        bi_mode = args.bi_mode  # "download" | "skip"

        if is_summary and bi_mode != "skip":
            bi_mod = _load_module("bi_dashboard_changes", _ROOT / "bi_dashboard_changes.py")
            print(f"\n── BI section  ─────────────────────────────────────────", file=sys.stderr)

            # Step A: resolve CSV path
            if bi_csv and Path(bi_csv).is_file():
                # caller supplied an explicit CSV
                print(f"   using provided CSV: {bi_csv}", file=sys.stderr)
            else:
                # Try to download from Snowflake
                try:
                    start_d = date.fromisoformat(start_date) if start_date else date.today() - timedelta(days=6)
                    end_d   = date.fromisoformat(end_date)   if end_date   else date.today()
                    bi_csv  = bi_mod.download_from_snowflake(start_d, end_d, _ROOT / "BI")
                except Exception as exc:
                    print(f"   [warn] Snowflake download failed: {exc}", file=sys.stderr)
                    # Fall back to latest CSV in BI/ for this deploy date
                    bi_dir = _ROOT / "BI"
                    if bi_dir.is_dir():
                        candidates = sorted(
                            bi_dir.glob(f"Results_{deploy_date}*.csv"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        if candidates:
                            bi_csv = candidates[0]
                            print(f"   falling back to existing CSV: {bi_csv}", file=sys.stderr)

            # Step B: generate and append section
            if bi_csv and Path(bi_csv).is_file():
                rows_bi = bi_mod.load_csv(Path(bi_csv))
                bi_section = bi_mod.build_bi_section(
                    rows_bi,
                    use_llm=use_llm,
                    verbose=args.verbose_llm,
                )
                if bi_section.strip():
                    content = md_path.read_text(encoding="utf-8").rstrip()
                    if "\n---\n" in content:
                        content = content.replace("\n---\n", f"\n{bi_section}\n---\n", 1)
                    else:
                        content = content + "\n" + bi_section
                    md_path.write_text(content, encoding="utf-8")
                    print(f"   ✓ BI section appended ({len(rows_bi)} changes)", file=sys.stderr)
            else:
                print("   no BI CSV found — skipping BI section", file=sys.stderr)

        print(f"\n{'━'*60}", file=sys.stderr)
        print(f"✓  {len(rows)} closed issues  →  {md_path}", file=sys.stderr)
        print(f"   Copy/paste into Slack, email, or Confluence.", file=sys.stderr)
        return

    out_path = step_html(
        rows,
        sent_date=sent_date,
        deploy_date=deploy_date,
        start_date=start_date,
        end_date=end_date,
        detail_link=detail_link,
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

    print(f"\n{'━'*60}", file=sys.stderr)
    print(f"✓  {len(rows)} issues  →  {out_path}", file=sys.stderr)
    llm_status = f"LLM: {os.environ.get('CURSOR_AGENT_MODEL','claude-sonnet-4-6')}" if use_llm and llm_sections else "LLM: skipped"
    print(f"   {llm_status}", file=sys.stderr)
    print(f"   Paste into Confluence → Editor → Insert → HTML", file=sys.stderr)


if __name__ == "__main__":
    main()

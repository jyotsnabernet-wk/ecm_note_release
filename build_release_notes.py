#!/usr/bin/env python3
"""
Build DnA-wide release notes from Jira (single source of truth).

- Scope: configurable base JQL (e.g. Analytics Engineering board filter).
- Planned Wednesday cadence: default time window = last Thursday 00:00 UTC through
  end of upcoming Wednesday UTC (see ``planned_release_window.py``).
- Categorize: group output by Jira **Components**; flag summaries starting with ``[ECM]``.
- Cross-reference: extract ticket keys (e.g. DNA-1234) from summary + description via regex;
  **issue links** (Jira ``issuelinks``) are always included; optional **remote** links for web URLs.

Usage:
  export $(grep -v '^#' dna_jira_release_notes/.env.example | xargs)  # do not commit secrets
  python dna_jira_release_notes/build_release_notes.py --output-dir out/

  python dna_jira_release_notes/build_release_notes.py \\
    --window custom --from-date 2026-06-05 --to-date 2026-06-11 \\
    --jql-file my_board.jql --output-dir out/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

# Allow running from repo root
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from jira_client import (  # noqa: E402
    JiraConfigError,
    auth_debug_summary,
    extract_issue_keys,
    extract_urls_from_remotelinks,
    jira_ping_myself,
    jira_print_auth_diagnosis,
    jira_session,
    list_remote_links,
    search_issues_jql,
)
from planned_release_window import planned_window_for_anchor  # noqa: E402


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = _ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path)
    load_dotenv()


def _adf_plain_text(node: Any) -> str:
    """Best-effort extract text from Atlassian Document Format (description)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        parts = []
        for c in node.get("content") or []:
            parts.append(_adf_plain_text(c))
        return "".join(parts)
    if isinstance(node, list):
        return "".join(_adf_plain_text(c) for c in node)
    return ""


def _field(issue: dict[str, Any], name: str) -> Any:
    return (issue.get("fields") or {}).get(name)


def _issue_url(base: str, key: str) -> str:
    return f"{base}/browse/{key}"


def _components(issue: dict[str, Any]) -> list[str]:
    raw = _field(issue, "components") or []
    names: list[str] = []
    for c in raw:
        if isinstance(c, dict) and c.get("name"):
            names.append(str(c["name"]))
    return sorted(names)


def _strip_trailing_order_by(jql: str) -> tuple[str, str | None]:
    """
    Jira allows a single trailing ``ORDER BY``. Strip it from ``jql`` so the fragment
    can be parenthesized and combined with ``AND``; return ``(core, order_clause_or_none)``.
    """
    s = jql.strip()
    m = re.search(r"\s+ORDER\s+BY\s+(.+)$", s, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return s, None
    core = s[: m.start()].strip()
    order_clause = m.group(1).strip()
    return core, order_clause


def _build_jql(
    *,
    base_jql: str,
    suffix: str,
    time_field: str,
    start_d: date,
    end_inclusive_d: date,
    sprint_fragment: str,
    order_by: str,
) -> str:
    if time_field not in ("updated", "resolutiondate"):
        raise ValueError("time_field must be updated or resolutiondate")
    # Jira inclusive calendar filter using half-open day range in UTC
    end_exclusive = end_inclusive_d + timedelta(days=1)
    time_clause = (
        f'{time_field} >= "{start_d.isoformat()}" AND '
        f'{time_field} < "{end_exclusive.isoformat()}"'
    )
    core, embedded_order = _strip_trailing_order_by(base_jql)
    if not core:
        raise SystemExit("DNA_JIRA_BASE_JQL is empty after removing ORDER BY — set a project or filter clause.")
    parts = [f"({core})", f"({time_clause})"]
    sf = (sprint_fragment or "").strip()
    if sf:
        parts.append(f"({sf})")
    suf = (suffix or "").strip()
    if suf:
        parts.append(f"({suf})")
    body = " AND ".join(parts)
    ob = (embedded_order or order_by or "updated DESC").strip()
    return f"{body} ORDER BY {ob}"


def _fix_versions(issue: dict[str, Any]) -> list[str]:
    raw = _field(issue, "fixVersions") or []
    out: list[str] = []
    for v in raw:
        if isinstance(v, dict) and v.get("name"):
            out.append(str(v["name"]))
    return sorted(out)


def _parent_key(issue: dict[str, Any]) -> str | None:
    p = _field(issue, "parent")
    if isinstance(p, dict) and p.get("key"):
        return str(p["key"])
    return None


def _parse_issuelinks(base: str, issue: dict[str, Any]) -> list[dict[str, Any]]:
    """Jira **issue links** (inward/outward to other Jira issues); each row includes ``browse`` URL."""
    out: list[dict[str, Any]] = []
    for link in _field(issue, "issuelinks") or []:
        if not isinstance(link, dict):
            continue
        typ = (link.get("type") or {}).get("name") or "link"
        if "inwardIssue" in link and isinstance(link["inwardIssue"], dict):
            i = link["inwardIssue"]
            k = str(i.get("key") or "")
            sf = str((i.get("fields") or {}).get("summary") or "")
            if k:
                out.append(
                    {
                        "direction": "inward",
                        "link_type": typ,
                        "key": k,
                        "summary": sf,
                        "url": _issue_url(base, k),
                    }
                )
        if "outwardIssue" in link and isinstance(link["outwardIssue"], dict):
            i = link["outwardIssue"]
            k = str(i.get("key") or "")
            sf = str((i.get("fields") or {}).get("summary") or "")
            if k:
                out.append(
                    {
                        "direction": "outward",
                        "link_type": typ,
                        "key": k,
                        "summary": sf,
                        "url": _issue_url(base, k),
                    }
                )
    return out


def merge_team_filter_from_env(base_jql: str) -> str:
    """
    When ``DNA_JIRA_TEAM`` is set, append ``AND <Team field> = "<value>"``.

    If ``base_jql`` is empty, build ``project = <DNA_JIRA_PROJECT> AND …`` (default project ``DNA``).
    """
    team = (os.environ.get("DNA_JIRA_TEAM") or "").strip()
    if not team:
        return base_jql.strip()
    field = (os.environ.get("DNA_JIRA_TEAM_FIELD") or "Team[Team]").strip()
    if field.startswith('"'):
        field_jql = field
    else:
        field_jql = f'"{field}"'
    esc = team.replace("\\", "\\\\").replace('"', '\\"')
    clause = f"{field_jql} = \"{esc}\""
    b = base_jql.strip()
    if not b:
        proj = (os.environ.get("DNA_JIRA_PROJECT") or "DNA").strip()
        return f"project = {proj} AND {clause}"
    return f"({b}) AND ({clause})"


def _normalize_issue(
    base: str,
    issue: dict[str, Any],
    *,
    ecm_prefix: str,
    key_pattern: str,
) -> dict[str, Any]:
    key = issue.get("key") or ""
    fields = issue.get("fields") or {}
    summary = str(fields.get("summary") or "")
    desc = fields.get("description")
    desc_text = _adf_plain_text(desc) if isinstance(desc, (dict, list)) else str(desc or "")
    blob = f"{summary}\n{desc_text}"
    keys_in_text = extract_issue_keys(blob, key_pattern)
    is_ecm = summary.startswith(ecm_prefix)
    st = fields.get("status") or {}
    it = fields.get("issuetype") or {}
    return {
        "key": key,
        "url": _issue_url(base, key),
        "summary": summary,
        "is_ecm": is_ecm,
        "status": st.get("name") if isinstance(st, dict) else None,
        "issuetype": it.get("name") if isinstance(it, dict) else None,
        "components": _components(issue),
        "labels": list(fields.get("labels") or []),
        "fix_versions": _fix_versions(issue),
        "parent_key": _parent_key(issue),
        "issue_links": _parse_issuelinks(base, issue),
        "updated": fields.get("updated"),
        "resolutiondate": fields.get("resolutiondate"),
        "issue_keys_mentioned": keys_in_text,
        "description_excerpt": (desc_text[:400] + "…") if len(desc_text) > 400 else desc_text,
    }


def _attach_remotelinks(
    base: str,
    session: Any,
    normalized: dict[str, Any],
) -> None:
    key = normalized["key"]
    links = list_remote_links(base, session, key)
    normalized["remote_links"] = extract_urls_from_remotelinks(links)


def _group_by_component(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in issues:
        comps = row.get("components") or []
        if not comps:
            buckets["(no component)"].append(row)
        else:
            for c in comps:
                buckets[c].append(row)
    for c in buckets:
        buckets[c].sort(key=lambda r: (0 if r.get("is_ecm") else 1, r.get("key") or ""))
    return dict(sorted(buckets.items(), key=lambda kv: kv[0].lower()))


def _markdown_report(
    meta: dict[str, Any],
    by_component: dict[str, list[dict[str, Any]]],
) -> str:
    lines: list[str] = []
    lines.append(f"# DnA release notes — {meta.get('window_label', '')}")
    lines.append("")
    lines.append(f"- **Planned deploy (Wednesday UTC):** {meta.get('release_wednesday', '')}")
    lines.append(f"- **Jira window ({meta.get('time_field', '')}):** {meta.get('start_date', '')} → {meta.get('end_inclusive_date', '')} (inclusive)")
    lines.append(f"- **Issues matched:** {meta.get('issue_count', 0)}")
    lines.append("")
    lines.append("## By component")
    lines.append("")
    for comp, rows in by_component.items():
        lines.append(f"### {comp}")
        lines.append("")
        for r in rows:
            tag = " **[ECM]**" if r.get("is_ecm") else ""
            lines.append(f"- **{r['key']}**{tag} — {r.get('summary', '')}")
            lines.append(f"  - Status: {r.get('status')} | Type: {r.get('issuetype')}")
            if r.get("parent_key"):
                lines.append(f"  - Parent: {r.get('parent_key')}")
            if r.get("fix_versions"):
                lines.append(f"  - Fix version(s): {', '.join(r['fix_versions'])}")
            if r.get("issue_links"):
                lines.append("  - **Issue links:**")
                for il in r["issue_links"][:15]:
                    sm = (il.get("summary") or "")[:100]
                    sm = sm + ("…" if len(il.get("summary") or "") > 100 else "")
                    lines.append(
                        f"    - **{il['key']}** ({il.get('link_type')} · {il.get('direction')}): {sm}"
                    )
                    lines.append(f"      - {il.get('url', '')}")
            if r.get("remote_links"):
                lines.append("  - **Remote / web links:**")
                for u in r["remote_links"][:5]:
                    lines.append(f"    - {u}")
            if r.get("issue_keys_mentioned"):
                lines.append(f"  - Keys in text: {', '.join(r['issue_keys_mentioned'][:8])}")
            lines.append(f"  - {r.get('url', '')}")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## ECM slice (summary prefix)")
    lines.append("")
    ecm = [r for rows in by_component.values() for r in rows if r.get("is_ecm")]
    ecm.sort(key=lambda r: r.get("key") or "")
    if not ecm:
        lines.append("_No tickets with the configured ECM summary prefix._")
    else:
        for r in ecm:
            lines.append(f"- **{r['key']}** — {r.get('summary', '')} — {r.get('url', '')}")
    lines.append("")
    lines.append("## JQL used")
    lines.append("")
    lines.append("```")
    lines.append(meta.get("jql", ""))
    lines.append("```")
    return "\n".join(lines)


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description="DnA-wide Jira release notes (Jira as SSOT).")
    p.add_argument(
        "--window",
        choices=["planned", "custom"],
        default="planned",
        help="planned = last Thu through upcoming Wed (see README); custom uses --from-date/--to-date.",
    )
    p.add_argument("--from-date", metavar="YYYY-MM-DD", help="custom window start (inclusive, UTC day).")
    p.add_argument("--to-date", metavar="YYYY-MM-DD", help="custom window end (inclusive, UTC day).")
    p.add_argument(
        "--anchor",
        metavar="YYYY-MM-DD",
        help="For planned window only: pretend 'today' is this UTC date (default: real today UTC).",
    )
    p.add_argument(
        "--time-field",
        choices=["updated", "resolutiondate"],
        default="updated",
        help="Which Jira timestamp to filter (default updated).",
    )
    p.add_argument(
        "--sprint-jql",
        default="",
        metavar="FRAGMENT",
        help='Appended as AND (…). Example: sprint in openSprints()',
    )
    p.add_argument(
        "--jql-file",
        default="",
        metavar="PATH",
        help="File whose contents replace DNA_JIRA_BASE_JQL for this run. Does not apply DNA_JIRA_TEAM from env.",
    )
    p.add_argument("--output-dir", "-o", default="dna_jira_release_notes/out", help="Write JSON + Markdown here.")
    p.add_argument(
        "--max-issues",
        type=int,
        default=500,
        help="Safety cap on issues fetched.",
    )
    p.add_argument(
        "--fetch-remotelinks",
        action="store_true",
        help="Extra REST call per issue for Jira **remote** web links (e.g. GitHub). Issue-to-issue links come from search without this flag.",
    )
    p.add_argument("--dry-run-jql", action="store_true", help="Print JQL and exit (no Jira calls).")
    p.add_argument(
        "--jira-ping",
        action="store_true",
        help="Call GET /rest/api/3/myself only (verify auth), then exit.",
    )
    p.add_argument(
        "--jira-ping-diagnose",
        action="store_true",
        help="Try GET /myself with bearer, basic_pat, and basic (same token); prints which HTTP codes — no full JSON.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log resolved Jira auth mode to stderr (no tokens).",
    )
    args = p.parse_args()

    if args.jira_ping_diagnose:
        try:
            if args.verbose:
                print(auth_debug_summary(), file=sys.stderr)
            jira_print_auth_diagnosis()
        except JiraConfigError as e:
            raise SystemExit(str(e)) from e
        return

    if args.jira_ping:
        try:
            base, session = jira_session()
        except JiraConfigError as e:
            raise SystemExit(str(e)) from e
        if args.verbose:
            print(auth_debug_summary(), file=sys.stderr)
        me = jira_ping_myself(base, session)
        print(json.dumps(me, indent=2, ensure_ascii=False))
        print("OK — Jira REST auth works for this session.", file=sys.stderr)
        return

    base_jql = (Path(args.jql_file).read_text().strip() if args.jql_file else os.environ.get("DNA_JIRA_BASE_JQL", "").strip())
    if not args.jql_file:
        base_jql = merge_team_filter_from_env(base_jql)
    if not base_jql.strip():
        raise SystemExit(
            "Set DNA_JIRA_BASE_JQL and/or DNA_JIRA_TEAM (+ optional DNA_JIRA_PROJECT), "
            "or pass --jql-file with full JQL."
        )
    suffix = (os.environ.get("DNA_JIRA_JQL_SUFFIX") or "").strip()

    ecm_prefix = (os.environ.get("DNA_ECM_SUMMARY_PREFIX") or "[ECM]").strip()
    key_pattern = os.environ.get("DNA_JIRA_ISSUE_KEY_PATTERN") or r"DNA-\d+"
    if "(?i)" not in key_pattern:
        key_pattern = f"(?i){key_pattern}"

    if args.window == "planned":
        anchor_dt: datetime | None = None
        if args.anchor:
            ad = date.fromisoformat(args.anchor)
            anchor_dt = datetime.combine(ad, time.min, tzinfo=timezone.utc)
        pw = planned_window_for_anchor(anchor_dt)
        start_d = pw.start_date
        end_inclusive_d = pw.end_inclusive_date
        release_wed = pw.release_wednesday.isoformat()
        window_label = f"{start_d.isoformat()} → {end_inclusive_d.isoformat()} (deploy Wed {release_wed})"
    else:
        if not args.from_date or not args.to_date:
            raise SystemExit("--window custom requires --from-date and --to-date (YYYY-MM-DD, UTC).")
        start_d = date.fromisoformat(args.from_date)
        end_inclusive_d = date.fromisoformat(args.to_date)
        if start_d > end_inclusive_d:
            raise SystemExit("from-date must be on or before to-date.")
        release_wed = ""
        window_label = f"{start_d.isoformat()} → {end_inclusive_d.isoformat()} (custom)"

    sprint_fragment = args.sprint_jql.strip()
    order_by = (os.environ.get("DNA_JIRA_ORDER_BY") or "updated DESC").strip()
    jql = _build_jql(
        base_jql=base_jql,
        suffix=suffix,
        time_field=args.time_field,
        start_d=start_d,
        end_inclusive_d=end_inclusive_d,
        sprint_fragment=sprint_fragment,
        order_by=order_by,
    )

    if args.dry_run_jql:
        print(jql)
        return

    if args.verbose:
        print(auth_debug_summary(), file=sys.stderr)

    try:
        base, session = jira_session()
    except JiraConfigError as e:
        raise SystemExit(str(e)) from e

    raw_issues = list(search_issues_jql(base, session, jql, max_results_cap=args.max_issues))
    normalized: list[dict[str, Any]] = []
    for issue in raw_issues:
        row = _normalize_issue(base, issue, ecm_prefix=ecm_prefix, key_pattern=key_pattern)
        if args.fetch_remotelinks:
            _attach_remotelinks(base, session, row)
        else:
            row["remote_links"] = []
        normalized.append(row)

    by_component = _group_by_component(normalized)
    meta = {
        "window": args.window,
        "window_label": window_label,
        "release_wednesday": release_wed,
        "start_date": start_d.isoformat(),
        "end_inclusive_date": end_inclusive_d.isoformat(),
        "time_field": args.time_field,
        "issue_count": len(normalized),
        "ecm_prefix": ecm_prefix,
        "jql": jql,
        "team_filter": (os.environ.get("DNA_JIRA_TEAM") or "").strip() or None,
        "team_field": (
            (os.environ.get("DNA_JIRA_TEAM_FIELD") or "Team[Team]").strip()
            if (os.environ.get("DNA_JIRA_TEAM") or "").strip()
            else None
        ),
    }
    payload = {"meta": meta, "issues": normalized, "by_component": by_component}

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"dna_release_{stamp}.json"
    md_path = out_dir / f"dna_release_{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(_markdown_report(meta, by_component), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()

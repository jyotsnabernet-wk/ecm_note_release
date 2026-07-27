#!/usr/bin/env python3
"""
Generate Confluence-compatible wiki HTML from a list of Jira issue dicts.

Structure strictly follows .cursorrules §"Wiki deliverable" and wiki_page_sample.html:
  1.  Sent line
  2.  <h2> Release Notification
  3.  Theme <h3> subheads
  4.  What is happening  (grouped by component, then status; nested ul+data-uuid)
  5.  Why it is happening
  6.  When it will happen
  7.  Downstream impact
  8.  Recommended actions
  9.  Links
  10. Contact  (with nested PR-authors list)
  11. Trailing <p><br /></p>

Input rows expected keys (uppercase, as returned by snowflake_sso_sql.py --output json):
  THE_KEY, SUMMARY, STATUS_NAME, PRIORITY_NAME, ASSIGNEE_NAME,
  DESCRIPTION_PREVIEW, COMPONENTS, LABELS, UPDATED_AT, CREATED_AT

Usage (standalone):
  python generate_wiki_release_html.py \\
    --input issues.json \\
    --sent-date 2026-06-16 --deploy-date 2026-06-18 \\
    --title "DnA AE — sprint 42 release" \\
    -o out/wiki_release.html

Use end_to_end_wiki.py to run the full Snowflake → HTML pipeline in one command.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote


# ── helpers ───────────────────────────────────────────────────────────────────

def _u() -> str:
    return str(uuid.uuid4())


def _esc(s: Any) -> str:
    return html.escape(str(s) if s is not None else "", quote=True)


def _href(url: str, label: str) -> str:
    return f'<a href="{_esc(url)}">{_esc(label)}</a>'


def _jira_url(base: str, key: str) -> str:
    return base.rstrip("/") + "/" + quote(str(key).strip(), safe="")


def _u_strong(text: str) -> str:
    """Wrap text in <u><strong>…</strong></u> — used for object/model names per cursorrules."""
    return f"<u><strong>{_esc(text)}</strong></u>"


def _parse_components(raw: Any) -> list[str]:
    """Best-effort extract component names from a Jira components JSON blob or plain string."""
    if not raw:
        return []
    s = str(raw).strip()
    if not s or s in ("[]", "None", "null"):
        return []
    # Already a Python-repr list of dicts e.g. "[{'name': 'development'}]"
    try:
        fixed = s.replace("'", '"').replace(": None", ": null").replace(": True", ": true").replace(": False", ": false")
        parsed = json.loads(fixed)
        if isinstance(parsed, list):
            return [str(d.get("name", "")).strip() for d in parsed if isinstance(d, dict) and d.get("name")]
    except Exception:
        pass
    # Fallback: extract quoted name values
    names = re.findall(r"'name':\s*'([^']+)'", s) or re.findall(r'"name":\s*"([^"]+)"', s)
    return [n.strip() for n in names if n.strip()]


def _clean_description(raw: Any) -> str:
    """
    Strip Jira wiki markup and return the first meaningful narrative sentence.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    # Strip Jira heading markers (h1. h2. h3. etc.)
    s = re.sub(r"(?m)^h[1-6]\.\s*\*?", "", s)
    # Strip bold/italic markers  *text* or **text**
    s = re.sub(r"\*+([^*\n]+)\*+", r"\1", s)
    # Strip any remaining lone asterisks / bullets
    s = re.sub(r"(?m)^\s*[\*#·•]\s*", "", s)
    # Strip stray asterisks not part of a word (e.g. trailing from h2. *Background*)
    s = re.sub(r"\*", "", s)
    # Strip bracketed Jira macros {color:…} etc.
    s = re.sub(r"\{[^}]+\}", "", s)
    # Strip common Jira section labels left behind after heading removal
    s = re.sub(r"(?i)^(background|request|acceptance criteria|overview|description|context|summary)[:\s]*", "", s.strip())
    # Normalise whitespace / line breaks
    s = re.sub(r"[ \t]*\n[ \t]*", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # Take first sentence (end at . ! ? followed by space or end-of-string)
    first = re.split(r"(?<=[.!?])\s+", s)[0].strip()
    # If first sentence is very short, also grab the second
    if len(first) < 60 and len(s) > len(first) + 5:
        sentences = re.split(r"(?<=[.!?])\s+", s)
        first = " ".join(sentences[:2]).strip()
    # Hard cap at 280 chars
    if len(first) > 280:
        first = first[:277].rstrip() + "…"
    return first


def _markup_model_names(text: str) -> str:
    """Wrap snake_case identifiers in <u><strong>…</strong></u>."""
    def _replace(m: re.Match) -> str:
        return _u_strong(m.group(0))
    return re.sub(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b", _replace, text)


def _group_intro(items: list[dict]) -> str:
    """Brief label for the group, derived from story summaries (≤ 2 items shown)."""
    summaries = []
    for r in items:
        summ = (r.get("SUMMARY") or r.get("summary") or "").strip()
        summ = re.sub(r"^\[.*?\]\s*", "", summ).strip()  # strip [SQUAD] prefix
        if summ:
            summaries.append(summ)
    if not summaries:
        return ""
    if len(summaries) == 1:
        return summaries[0]
    if len(summaries) == 2:
        return f"{summaries[0]} and {summaries[1]}"
    # For 3+: first two + count of rest
    rest = len(summaries) - 2
    return f"{summaries[0]}, {summaries[1]}, and {rest} more"


def li(content: str, *, indent: int = 4) -> str:
    pad = " " * indent
    return f'{pad}<li data-uuid="{_u()}">{content}</li>'


# ── section builders ──────────────────────────────────────────────────────────

def _section_what(rows: list[dict], jira_browse_base: str) -> list[str]:
    """
    Group by component → each group gets a narrative intro + nested <ul> of stories.
    Each story leads with description content; ticket key is a parenthetical reference.
    """
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        comps = _parse_components(r.get("COMPONENTS") or r.get("components"))
        if comps:
            for c in comps:
                by_group[c].append(r)
        else:
            st = (r.get("STATUS_NAME") or r.get("status_name") or "(unknown)").strip()
            by_group[st].append(r)

    def _group_order(g: str) -> tuple[int, str]:
        if "ecm" in g.lower():
            return 0, g.lower()
        return 1, g.lower()

    lines: list[str] = []
    lines.append('    <h3><strong>What is happening</strong></h3>')
    lines.append("    <p>This week's DnA release covers the following Analytics Engineering stories.</p>")
    lines.append("    <ul>")

    for group in sorted(by_group.keys(), key=_group_order):
        items = sorted(by_group[group], key=lambda x: x.get("THE_KEY") or x.get("the_key") or "")
        intro = _group_intro(items)
        group_label = f"<strong>{_esc(group)}</strong>"
        if intro:
            group_label += f" — {_esc(intro)}:"

        lines.append(f'      <li data-uuid="{_u()}">')
        lines.append(f"        {group_label}")
        lines.append("        <ul>")

        for r in items:
            key   = (r.get("THE_KEY")              or r.get("the_key")              or "").strip()
            summ  = (r.get("SUMMARY")              or r.get("summary")              or "").strip()
            desc  = (r.get("DESCRIPTION_PREVIEW")  or r.get("description_preview")  or "").strip()
            asn   = (r.get("ASSIGNEE_NAME")        or r.get("assignee_name")        or "").strip()
            url   = _jira_url(jira_browse_base, key) if key else "#"

            # Choose content: description first, fall back to summary
            narrative = _clean_description(desc) or summ
            # Strip leading [SQUAD] bracket prefix from summary if used as fallback
            narrative = re.sub(r"^\[.*?\]\s*", "", narrative).strip()
            # Mark up snake_case model/field names
            narrative_html = _markup_model_names(_esc(narrative))

            # Ref badge at end: "DNA-XXXX · Assignee"
            ref_parts = [f'<a href="{url}">{_esc(key)}</a>'] if key else []
            if asn:
                ref_parts.append(_esc(asn))
            ref = f" ({' · '.join(ref_parts)})" if ref_parts else ""

            lines.append(f'          <li data-uuid="{_u()}">{narrative_html}{ref}</li>')

        lines.append("        </ul>")
        lines.append("      </li>")

    lines.append("    </ul>")
    return lines


def _section_why(bullets: list[str], deploy_date: str) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Why it is happening</strong></h3>')
    if bullets:
        lines.append("    <ul>")
        for b in bullets:
            lines.append(li(b))
        lines.append("    </ul>")
    else:
        lines.append(
            "    <p>These changes improve data quality, extend model coverage, or deliver "
            "new analytical capabilities as part of the planned Analytics Engineering release "
            f"on <time datetime=\"{_esc(deploy_date)}\" />.</p>"
        )
    return lines


def _section_when(deploy_date: str, migration_note: str) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>When it will happen</strong></h3>')
    lines.append(f'    <p>Deploy on <time datetime="{_esc(deploy_date)}" /></p>')
    if migration_note.strip():
        lines.append(f"    <p>{_esc(migration_note)}</p>")
    return lines


def _section_downstream(bullets: list[str]) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Downstream impact</strong></h3>')
    if bullets:
        lines.append("    <ul>")
        for b in bullets:
            lines.append(li(b))
        lines.append("    </ul>")
    else:
        lines.append(
            "    <ul>"
        )
        lines.append(
            f'      <li data-uuid="{_u()}">Consumers of affected models should re-run validation queries '
            "after the deploy to confirm metric values align with updated definitions.</li>"
        )
        lines.append("    </ul>")
    return lines


def _section_recommended(actions: list[str]) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Recommended actions</strong></h3>')
    if actions:
        for a in actions:
            lines.append(f"    <p>{_esc(a)}</p>")
    else:
        lines.append(
            "    <p>Verify — owners of dashboards or reports sourced from affected models should "
            "validate key metrics against the deployed data after the release.</p>"
        )
    return lines


def _section_links(rows: list[dict], extra_links: list[tuple[str, str]], jira_browse_base: str) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Links</strong></h3>')
    lines.append("    <ul>")
    for label, url in extra_links:
        lines.append(li(_href(url, label)))
    for r in sorted(rows, key=lambda x: x.get("THE_KEY") or x.get("the_key") or ""):
        key  = (r.get("THE_KEY")  or r.get("the_key")  or "").strip()
        summ = (r.get("SUMMARY")  or r.get("summary")  or "").strip()
        if not key:
            continue
        url = _jira_url(jira_browse_base, key)
        lines.append(li(f"{_href(url, key)} — {_esc(summ)}"))
    lines.append("    </ul>")
    return lines


def _section_contact(pr_authors: list[str]) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Contact</strong></h3>')
    lines.append("    <ul>")
    lines.append(
        f'      <li data-uuid="{_u()}">DnA — post questions in '
        f'<a href="https://workiva.enterprise.slack.com/archives/CDWRCLFPE">#support-data-services</a></li>'
    )
    if pr_authors:
        lines.append(f'      <li data-uuid="{_u()}">PR authors:')
        lines.append("        <ul>")
        for a in pr_authors:
            lines.append(f'          <li data-uuid="{_u()}">{_esc(a)}</li>')
        lines.append("        </ul>")
        lines.append("      </li>")
    lines.append("    </ul>")
    return lines


# ── LLM-output renderers ─────────────────────────────────────────────────────

def _autolink_keys(text: str, base: str) -> str:
    """Replace bare DNA-XXXX references in text with <a href> links."""
    def _sub(m: re.Match) -> str:
        key = m.group(0)
        url = _jira_url(base, key)
        return f'<a href="{url}">{key}</a>'
    return re.sub(r"\bDNA-\d+\b", _sub, text)


def _section_executive_summary(
    bullets: list[str],
    start_date: str = "",
    end_date: str = "",
    detail_link: str = "",
) -> list[str]:
    """
    Render the executive summary block before 'What is happening'.
    Only Closed tickets feed this section; format is bold domain + one-sentence impact.
    """
    if not bullets:
        return []

    # Build date range label e.g. "July 9–15, 2026"
    date_label = ""
    if start_date and end_date:
        try:
            from datetime import date as _date
            import calendar
            s = _date.fromisoformat(start_date)
            e = _date.fromisoformat(end_date)
            if s.month == e.month:
                date_label = f"{calendar.month_name[s.month]} {s.day}–{e.day}, {e.year}"
            else:
                date_label = (
                    f"{calendar.month_name[s.month]} {s.day} – "
                    f"{calendar.month_name[e.month]} {e.day}, {e.year}"
                )
        except Exception:
            date_label = f"{start_date} – {end_date}"

    lines: list[str] = []
    heading = f"DnA Release{' — ' + date_label if date_label else ''} | Executive Summary"
    lines.append(f'    <h3><strong>{_esc(heading)}</strong></h3>')
    lines.append("    <ul>")
    for bullet in bullets:
        b = str(bullet).strip()
        if not b:
            continue
        # Bold the domain label (everything before " — " or " - ")
        import re as _re
        m = _re.match(r"^(\*\*)?(.+?)(\*\*)?\s*[—\-–]\s*(.+)$", b, _re.DOTALL)
        if m:
            domain = _esc(m.group(2).strip())
            rest   = m.group(4).strip()  # LLM already provides HTML-safe text
            rendered = f"<strong>{domain}</strong> — {rest}"
        else:
            rendered = _esc(b)
        lines.append(f'      <li data-uuid="{_u()}">{rendered}</li>')
    lines.append("    </ul>")
    if detail_link:
        sprint_label = detail_link  # display text is the link itself or a label
        lines.append(
            f'    <p>Need more detail? Review the technical release notes here: '
            f'<a href="{_esc(detail_link)}">{_esc(sprint_label)}</a></p>'
        )
    return lines


def _section_what_llm(topics: list[dict], jira_browse_base: str) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>What is happening</strong></h3>')
    lines.append("    <p>This week's DnA release covers the following Analytics Engineering stories.</p>")
    lines.append("    <ul>")
    for topic in topics:
        label   = str(topic.get("label") or "").strip()
        bullets = topic.get("bullets") or []
        lines.append(f'      <li data-uuid="{_u()}">')
        # label comes from LLM — already HTML-safe, do NOT re-escape
        lines.append(f"        <strong>{label}</strong>")
        if bullets:
            lines.append("        <ul>")
            for b in bullets:
                b_html = _autolink_keys(str(b), jira_browse_base)
                lines.append(f'          <li data-uuid="{_u()}">')
                lines.append(f"            {b_html}")
                lines.append("          </li>")
            lines.append("        </ul>")
        lines.append("      </li>")
    lines.append("    </ul>")
    return lines


def _section_why_llm(groups: list[dict]) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Why it is happening</strong></h3>')
    for group in groups:
        label   = str(group.get("label") or "").strip()
        bullets = group.get("bullets") or []
        if label:
            lines.append(f"    <p><strong>{label}</strong></p>")
        if bullets:
            lines.append("    <ul>")
            for b in bullets:
                lines.append(f'      <li data-uuid="{_u()}">{b}</li>')
            lines.append("    </ul>")
    return lines


def _strip_ticket_refs(text: str) -> str:
    """Remove parenthetical and bare DNA-XXXX ticket references from prose."""
    # Remove parenthetical refs like (DNA-5665) or (DNA-5665 / DNA-5677)
    text = re.sub(r"\s*\([^)]*DNA-\d+[^)]*\)", "", text)
    # Remove any remaining bare DNA-XXXX refs (already-linked anchors or plain text)
    text = re.sub(r'\s*<a href="[^"]*">DNA-\d+</a>', "", text)
    text = re.sub(r"\s*\bDNA-\d+\b", "", text)
    return text.strip()


def _section_downstream_llm(bullets: list[str]) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Downstream impact</strong></h3>')
    lines.append("    <ul>")
    for b in bullets:
        lines.append(f'      <li data-uuid="{_u()}">{_strip_ticket_refs(b)}</li>')
    lines.append("    </ul>")
    return lines


def _section_recommended_llm(actions: list[str]) -> list[str]:
    lines: list[str] = []
    lines.append('    <h3><strong>Recommended actions</strong></h3>')
    for a in actions:
        # Drop any "No action required" bullets — readers don't need a list of non-actions
        if re.match(r"^\s*No action required", a, re.IGNORECASE):
            continue
        lines.append(f"    <p>{a}</p>")
    return lines


# ── public API ────────────────────────────────────────────────────────────────

def build_html(
    rows: list[dict[str, Any]],
    *,
    sent_date: str,
    deploy_date: str,
    start_date: str = "",
    end_date: str = "",
    detail_link: str = "",
    title: str = "DnA — Analytics Engineering (DNA Stories)",
    subtitle: str = "",
    why_bullets: list[str] | None = None,
    downstream_bullets: list[str] | None = None,
    recommended_actions: list[str] | None = None,
    extra_links: list[tuple[str, str]] | None = None,
    pr_authors: list[str] | None = None,
    migration_note: str = (
        "Consumers relying on any changed columns should validate and update downstream "
        "references within 30 days of the deploy."
    ),
    jira_browse_base: str = "https://jira.atl.workiva.net/browse",
    llm_sections: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []

    # Prefer LLM-generated title when available
    effective_title = title
    if llm_sections and llm_sections.get("title"):
        effective_title = str(llm_sections["title"]).strip() or title

    # Shell
    parts.append('<div class="mceContentBody aui-theme-default wiki-content fullsize">')

    # 1. Sent line
    parts.append("    <p>")
    parts.append(f'      Sent on<strong> <time datetime="{_esc(sent_date)}" />&nbsp;</strong>')
    parts.append("    </p>")

    # 2. Section banner
    parts.append("    <h2><strong>Release Notification</strong></h2>")

    # 3. Theme subheads
    parts.append(f"    <h3>{_esc(effective_title)}</h3>")
    if subtitle.strip():
        parts.append(f"    <h3>{_esc(subtitle)}</h3>")

    # 3b. Executive summary (LLM-only, Closed tickets only, rendered before What is happening)
    if llm_sections and llm_sections.get("executive_summary"):
        parts.extend(_section_executive_summary(
            llm_sections["executive_summary"],
            start_date=start_date,
            end_date=end_date,
            detail_link=detail_link,
        ))

    # 4. What is happening
    if llm_sections and llm_sections.get("what_topics"):
        parts.extend(_section_what_llm(llm_sections["what_topics"], jira_browse_base))
    else:
        parts.extend(_section_what(rows, jira_browse_base))

    # 5. Why it is happening
    if llm_sections and llm_sections.get("why_groups"):
        parts.extend(_section_why_llm(llm_sections["why_groups"]))
    else:
        parts.extend(_section_why(why_bullets or [], deploy_date))

    # 6. When it will happen
    parts.extend(_section_when(deploy_date, migration_note))

    # 7. Downstream impact
    if llm_sections and llm_sections.get("downstream_bullets"):
        parts.extend(_section_downstream_llm(llm_sections["downstream_bullets"]))
    else:
        parts.extend(_section_downstream(downstream_bullets or []))

    # 8. Recommended actions
    if llm_sections and llm_sections.get("recommended_actions"):
        parts.extend(_section_recommended_llm(llm_sections["recommended_actions"]))
    else:
        parts.extend(_section_recommended(recommended_actions or []))

    # 9. Links
    parts.extend(_section_links(rows, extra_links or [], jira_browse_base))

    # 10. Contact — prefer LLM-generated pr_authors over caller-supplied list
    if llm_sections and llm_sections.get("pr_authors"):
        llm_authors = [
            f"{a.get('handle', '')} ({a.get('domains', '')})"
            for a in llm_sections["pr_authors"]
            if a.get("handle")
        ]
        parts.extend(_section_contact(llm_authors))
    else:
        parts.extend(_section_contact(pr_authors or []))

    # 11. Trailing spacer
    parts.append("    <p><br /></p>")
    parts.append("  </div>")

    return "\n".join(parts) + "\n"


# ── standalone CLI ────────────────────────────────────────────────────────────

def load_rows(path: Path | None, *, stdin: bool) -> list[dict[str, Any]]:
    if stdin:
        data = json.loads(sys.stdin.read())
    else:
        if not path or not path.is_file():
            raise SystemExit("Provide --input file.json/csv or use --stdin.")
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8") as f:
                data = list(csv.DictReader(f))
        else:
            raise SystemExit("Input must be .json or .csv")
    if not isinstance(data, list):
        raise SystemExit("JSON input must be an array of objects.")
    return [dict(r) for r in data]


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate cursorrules-compliant wiki HTML from Jira issue JSON/CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input", "-i", type=Path, help="issues.json or issues.csv")
    p.add_argument("--stdin", action="store_true", help="Read JSON from stdin")
    p.add_argument("--sent-date",   required=True, metavar="YYYY-MM-DD")
    p.add_argument("--deploy-date", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--title",    default="DnA — Analytics Engineering (DNA Stories)")
    p.add_argument("--subtitle", default="")
    p.add_argument("--migration-note", default=(
        "Consumers relying on any changed columns should validate and update downstream "
        "references within 30 days of the deploy."
    ))
    p.add_argument("--jira-browse-base", default="https://jira.atl.workiva.net/browse")
    p.add_argument("-o", "--output", type=Path, help="Output .html file (default: stdout)")
    args = p.parse_args()

    rows = load_rows(args.input, stdin=args.stdin)
    if not rows:
        raise SystemExit("No rows in input.")

    out = build_html(
        rows,
        sent_date=args.sent_date,
        deploy_date=args.deploy_date,
        title=args.title,
        subtitle=args.subtitle,
        migration_note=args.migration_note,
        jira_browse_base=args.jira_browse_base,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)


if __name__ == "__main__":
    main()

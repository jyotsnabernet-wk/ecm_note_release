#!/usr/bin/env python3
"""
publish_to_confluence.py — Upload the latest wiki_release_*.html to Confluence.

Creates a new child page under CONFLUENCE_PARENT_PAGE_ID each run.

Environment variables (put in .env or GitHub Actions secrets):
  CONFLUENCE_BASE_URL       e.g. https://confluence.atl.workiva.net
  CONFLUENCE_SPACE_KEY      e.g. DNA
  CONFLUENCE_PARENT_PAGE_ID numeric ID of the parent page
  CONFLUENCE_API_TOKEN      PAT (same token as JIRA_API_TOKEN works on Workiva DC)

Usage:
  python publish_to_confluence.py                    # auto-picks latest html in out/
  python publish_to_confluence.py --file out/wiki_release_2026-07-22.html
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("Missing requests.  Run: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass


# ── helpers ───────────────────────────────────────────────────────────────────

def _env(key: str, required: bool = True) -> str:
    val = (os.environ.get(key) or "").strip()
    if required and not val:
        raise SystemExit(
            f"Missing env var: {key}\n"
            "Add it to .env or set it as a GitHub Actions secret."
        )
    return val


def _latest_html(out_dir: Path) -> Path:
    candidates = sorted(
        out_dir.glob("wiki_release_*.html"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"No wiki_release_*.html found in {out_dir}")
    return candidates[0]


def _page_title(html_path: Path) -> str:
    """Derive a page title from the filename, e.g. wiki_release_2026-07-22.html → DnA Release 2026-07-22."""
    stem = html_path.stem  # wiki_release_2026-07-22
    date_part = stem.replace("wiki_release_", "")
    return f"DnA Analytics Engineering Release {date_part}"


# ── Confluence API ─────────────────────────────────────────────────────────────

def create_page(
    *,
    base_url: str,
    space_key: str,
    parent_id: str,
    title: str,
    html_body: str,
    token: str,
) -> dict:
    """Create a new Confluence page and return the response JSON."""
    url = base_url.rstrip("/") + "/rest/api/content"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "ancestors": [{"id": parent_id}],
        "body": {
            "storage": {
                "value": html_body,
                "representation": "storage",
            }
        },
    }
    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    if resp.status_code == 400 and "title already used" in resp.text.lower():
        raise SystemExit(
            f"A page titled '{title}' already exists in space {space_key}.\n"
            "Delete or rename it in Confluence before re-running."
        )
    resp.raise_for_status()
    return resp.json()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Publish wiki HTML to Confluence.")
    p.add_argument("--file", "-f", type=Path, help="Path to wiki_release_*.html (default: latest in out/)")
    p.add_argument("--title", help="Override page title")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "out")
    args = p.parse_args()

    base_url  = _env("CONFLUENCE_BASE_URL")
    space_key = _env("CONFLUENCE_SPACE_KEY")
    parent_id = _env("CONFLUENCE_PARENT_PAGE_ID")
    token     = _env("CONFLUENCE_API_TOKEN")

    html_path = args.file or _latest_html(args.out_dir)
    html_body = html_path.read_text(encoding="utf-8")
    title     = args.title or _page_title(html_path)

    print(f"Publishing: {html_path.name}", file=sys.stderr)
    print(f"  → Space: {space_key}  Parent: {parent_id}", file=sys.stderr)
    print(f"  → Title: {title}", file=sys.stderr)

    result = create_page(
        base_url=base_url,
        space_key=space_key,
        parent_id=parent_id,
        title=title,
        html_body=html_body,
        token=token,
    )

    page_id  = result.get("id", "?")
    page_url = base_url.rstrip("/") + result.get("_links", {}).get("webui", "")
    print(f"\n✓  Created page id={page_id}", file=sys.stderr)
    print(f"   {page_url}", file=sys.stderr)

    # Write outputs for GitHub Actions steps that follow (e.g. Slack notification)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"page_url={page_url}\n")
            fh.write(f"page_title={title}\n")


if __name__ == "__main__":
    main()

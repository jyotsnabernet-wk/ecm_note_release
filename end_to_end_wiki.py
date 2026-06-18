#!/usr/bin/env python3
"""
end_to_end_wiki.py — one command to:

  1. Query Snowflake (SSO browser login) using sql/jira_dna_ae_stories.sql
  2. Generate a Confluence-compatible wiki HTML (wiki_page_sample.html layout)
  3. Write the HTML to  out/wiki_release_<DEPLOY-DATE>.html

Requires: snowflake-connector-python, python-dotenv (pip install both)
Config:   .env.snowflake  (SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_WAREHOUSE)

Usage:
  # Minimal — dates default to today (sent) and next Wednesday (deploy)
  python end_to_end_wiki.py

  # Explicit dates
  python end_to_end_wiki.py --sent-date 2026-06-16 --deploy-date 2026-06-18

  # Override title / SQL file / output dir
  python end_to_end_wiki.py \\
    --sql-file sql/jira_dna_ae_stories.sql \\
    --title "DnA AE — sprint 42 release" \\
    --output-dir out
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# ── helpers ──────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    p = _ROOT / ".env.snowflake"
    if p.is_file():
        load_dotenv(p)
    load_dotenv(_ROOT / ".env")


def _next_wednesday(from_date: date) -> date:
    """Calendar Wednesday on or after from_date (same rule as planned_release_window.py)."""
    delta = (3 - from_date.isoweekday()) % 7
    return from_date + timedelta(days=delta)


# ── step 1: Snowflake query ───────────────────────────────────────────────────

def run_snowflake_query(
    sql: str,
    *,
    account: str,
    user: str,
    warehouse: str,
    database: str,
    schema: str,
    role: str,
) -> list[dict]:
    try:
        import snowflake.connector
        from snowflake.connector.errors import ProgrammingError
    except ImportError:
        raise SystemExit("Install: pip install snowflake-connector-python")

    def _q(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    kwargs: dict = {
        "account": account,
        "user": user,
        "authenticator": "externalbrowser",
        "warehouse": warehouse,
    }
    if database:
        kwargs["database"] = database
    if schema:
        kwargs["schema"] = schema
    if role:
        kwargs["role"] = role

    print(f"[step 1/2] Snowflake SSO — warehouse={warehouse!r}", file=sys.stderr)
    print("           Opening browser for Okta/SSO login…", file=sys.stderr)
    conn = snowflake.connector.connect(**kwargs)
    try:
        cur = conn.cursor()
        if role:
            try:
                cur.execute(f"USE ROLE {_q(role)}")
            except ProgrammingError as e:
                raise SystemExit(f"USE ROLE {role!r} failed: {e}") from e
        try:
            cur.execute(f"USE WAREHOUSE {_q(warehouse)}")
        except ProgrammingError as e:
            raise SystemExit(
                f"USE WAREHOUSE {warehouse!r} failed: {e}\n"
                "Check the exact name in Snowflake → Admin → Warehouses "
                "and update SNOWFLAKE_WAREHOUSE in .env.snowflake."
            ) from e
        if database:
            cur.execute(f"USE DATABASE {_q(database)}")
        if schema:
            cur.execute(f"USE SCHEMA {_q(schema)}")
        cur.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
    finally:
        conn.close()

    print(f"           {len(rows)} rows returned.", file=sys.stderr)
    return [dict(zip(cols, row)) for row in rows]


# ── step 2: wiki HTML ─────────────────────────────────────────────────────────

def generate_html(rows: list[dict], **kwargs) -> str:
    # Import from sibling module without adding to sys.path permanently
    import importlib.util, types

    spec = importlib.util.spec_from_file_location(
        "generate_wiki_release_html",
        _ROOT / "generate_wiki_release_html.py",
    )
    mod: types.ModuleType = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
    spec.loader.exec_module(mod)                                      # type: ignore[union-attr]
    return mod.build_html(rows, **kwargs)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_dotenv()
    today = date.today()
    default_deploy = _next_wednesday(today).isoformat()

    p = argparse.ArgumentParser(
        description="Snowflake → Wiki HTML pipeline (one command, browser SSO).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Dates
    p.add_argument("--sent-date", default=today.isoformat(), metavar="YYYY-MM-DD",
                   help="Date shown in 'Sent on' header")
    p.add_argument("--deploy-date", default=default_deploy, metavar="YYYY-MM-DD",
                   help="Deploy / release date for 'When it will happen'")
    # Snowflake
    p.add_argument("--sql-file", type=Path,
                   default=_ROOT / "sql" / "jira_dna_ae_stories.sql",
                   help="SQL file to run against Snowflake")
    p.add_argument("--warehouse", default=os.environ.get("SNOWFLAKE_WAREHOUSE") or "",
                   help="Override SNOWFLAKE_WAREHOUSE from .env.snowflake")
    # HTML
    p.add_argument("--title", default="DnA — Analytics Engineering (DNA Stories)",
                   help="Main h3 title in Release Notification")
    p.add_argument("--subtitle", default="",
                   help="Optional second h3 line")
    p.add_argument("--jira-browse-base", default="https://jira.atl.workiva.net/browse",
                   help="Base URL for Jira ticket links")
    # Output
    p.add_argument("--output-dir", "-o", type=Path, default=_ROOT / "out",
                   help="Directory for the generated HTML file")
    p.add_argument("--keep-json", action="store_true",
                   help="Also save raw issues JSON next to the HTML")
    args = p.parse_args()

    # ── resolve env ──
    account = (os.environ.get("SNOWFLAKE_ACCOUNT") or "").strip()
    user    = (os.environ.get("SNOWFLAKE_USER") or "").strip()
    wh      = (args.warehouse or "").strip()
    database = (os.environ.get("SNOWFLAKE_DATABASE") or "").strip()
    schema   = (os.environ.get("SNOWFLAKE_SCHEMA") or "").strip()
    role     = (os.environ.get("SNOWFLAKE_ROLE") or "").strip()

    if not account or not user:
        raise SystemExit(
            "Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER in .env.snowflake (or export)."
        )
    if not wh:
        raise SystemExit(
            "Set SNOWFLAKE_WAREHOUSE in .env.snowflake or pass --warehouse."
        )

    sql_path = args.sql_file.expanduser().resolve()
    if not sql_path.is_file():
        raise SystemExit(f"SQL file not found: {sql_path}")
    sql = sql_path.read_text(encoding="utf-8").strip()

    deploy_date = args.deploy_date
    sent_date   = args.sent_date

    # ── step 1 ──
    rows = run_snowflake_query(
        sql,
        account=account,
        user=user,
        warehouse=wh,
        database=database,
        schema=schema,
        role=role,
    )
    if not rows:
        print("[warn] Query returned 0 rows — HTML will be empty.", file=sys.stderr)

    # ── optional: save JSON ──
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.keep_json:
        import json, datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = args.output_dir / f"issues_{ts}.json"
        json_path.write_text(json.dumps(rows, indent=2, default=str, ensure_ascii=False))
        print(f"           JSON saved → {json_path}", file=sys.stderr)

    # ── step 2 ──
    print(f"[step 2/2] Generating wiki HTML → sent={sent_date} deploy={deploy_date}", file=sys.stderr)
    migration_note = (
        f"Consumers relying on any changed columns should validate and update downstream "
        f"references within 30 days of the deploy ({deploy_date})."
    )
    html_str = generate_html(
        rows,
        sent_date=sent_date,
        deploy_date=deploy_date,
        title=args.title,
        subtitle=args.subtitle,
        migration_note=migration_note,
        jira_browse_base=args.jira_browse_base,
    )

    out_path = args.output_dir / f"wiki_release_{deploy_date}.html"
    out_path.write_text(html_str, encoding="utf-8")
    print(f"\n✓ Done — {len(rows)} issues  →  {out_path}", file=sys.stderr)
    print(f"  Paste the file content into Confluence (Editor → Insert → HTML).", file=sys.stderr)


if __name__ == "__main__":
    main()

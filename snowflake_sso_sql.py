#!/usr/bin/env python3
"""
Run Snowflake SQL using **browser SSO** (``authenticator=externalbrowser``).

This uses the official **snowflake-connector-python** driver — not the raw SQL REST API.
A browser window opens once per run so you can sign in with your org SSO.

Install:
  pip install snowflake-connector-python python-dotenv

Env (same ``.env.snowflake`` as other scripts, or export):
  SNOWFLAKE_ACCOUNT   e.g. xy12345.us-east-1.aws  (no .snowflakecomputing.com)
  SNOWFLAKE_USER      your Snowflake login name
  SNOWFLAKE_WAREHOUSE **required** to run queries (e.g. COMPUTE_WH) — or pass ``--warehouse``
  Optional: SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_ROLE

Usage:
  python snowflake_sso_sql.py --sql-file sql/jira_dna_ae_stories.sql --warehouse COMPUTE_WH

Fewer SSO browser prompts: ``pip install 'snowflake-connector-python[secure-local-storage]'`` (keyring).

Headless / CI: use key-pair or PAT with ``snowflake_http_sql.py`` instead — externalbrowser
requires a local display and human interaction.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _sql_double_quoted_ident(name: str) -> str:
    """Quote a Snowflake identifier for SQL (handles ``PUBLIC--PROB_WH`` etc.)."""
    return '"' + name.replace('"', '""') + '"'


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    p = _ROOT / ".env.snowflake"
    if p.is_file():
        load_dotenv(p)
    load_dotenv(_ROOT / ".env")


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Run Snowflake SQL with externalbrowser (SSO).")
    p.add_argument(
        "--sql",
        default=os.environ.get("SNOWFLAKE_STATEMENT") or "",
        help="SQL string (ignored if --sql-file is set).",
    )
    p.add_argument(
        "--sql-file",
        type=Path,
        default=Path(os.environ["SNOWFLAKE_SQL_FILE"]).expanduser()
        if (os.environ.get("SNOWFLAKE_SQL_FILE") or "").strip()
        else None,
        help="Read SQL from UTF-8 file.",
    )
    p.add_argument(
        "--output",
        choices=["csv", "json"],
        default="csv",
        help="How to print result rows (default: csv to stdout).",
    )
    p.add_argument(
        "--warehouse",
        default=os.environ.get("SNOWFLAKE_WAREHOUSE") or "",
        help="Warehouse name (overrides SNOWFLAKE_WAREHOUSE). Required unless that env var is set.",
    )
    p.add_argument("--database", default=os.environ.get("SNOWFLAKE_DATABASE") or "")
    p.add_argument("--schema", default=os.environ.get("SNOWFLAKE_SCHEMA") or "")
    p.add_argument("--role", default=os.environ.get("SNOWFLAKE_ROLE") or "")
    args = p.parse_args()

    if args.sql_file:
        sql = args.sql_file.expanduser().resolve().read_text(encoding="utf-8").strip()
    else:
        sql = (args.sql or "").strip()
    if not sql:
        raise SystemExit("Provide --sql-file or --sql / SNOWFLAKE_STATEMENT.")

    account = (os.environ.get("SNOWFLAKE_ACCOUNT") or "").strip()
    user = (os.environ.get("SNOWFLAKE_USER") or "").strip()
    if not account or not user:
        raise SystemExit("Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER in .env.snowflake (or export).")

    try:
        import snowflake.connector
        from snowflake.connector.errors import ProgrammingError
    except ImportError as e:
        raise SystemExit("Install: pip install snowflake-connector-python") from e

    kwargs: dict = {
        "account": account,
        "user": user,
        "authenticator": "externalbrowser",
    }
    wh = (args.warehouse or "").strip()
    if not wh:
        raise SystemExit(
            "Snowflake needs an active warehouse (error 57P03). Set SNOWFLAKE_WAREHOUSE in .env.snowflake "
            "or pass --warehouse YOUR_WH (e.g. COMPUTE_WH — use the name from your org)."
        )
    kwargs["warehouse"] = wh
    for key, val in (
        ("database", (args.database or "").strip()),
        ("schema", (args.schema or "").strip()),
        ("role", (args.role or "").strip()),
    ):
        if val:
            kwargs[key] = val

    print(f"[snowflake-sso] warehouse={wh!r} (tip: --warehouse overrides SNOWFLAKE_WAREHOUSE)", file=sys.stderr)
    print("Opening browser for Snowflake SSO…", file=sys.stderr)
    conn = snowflake.connector.connect(**kwargs)
    try:
        cur = conn.cursor()
        rl = (args.role or "").strip()
        if rl:
            try:
                cur.execute(f"USE ROLE {_sql_double_quoted_ident(rl)}")
            except ProgrammingError as e:
                raise SystemExit(
                    f"USE ROLE failed for {rl!r}: {e}\n"
                    "Unset SNOWFLAKE_ROLE in .env.snowflake unless you need a specific role."
                ) from e
        # SSO sessions sometimes ignore connect(warehouse=...); force session context in SQL.
        try:
            cur.execute(f"USE WAREHOUSE {_sql_double_quoted_ident(wh)}")
        except ProgrammingError as e:
            raise SystemExit(
                f"USE WAREHOUSE failed for {wh!r}: {e}\n"
                "Fix: use the exact warehouse name from Snowflake (Admin → Warehouses), e.g. "
                "PUBLIC--PROB_WH if that is the full name.\n"
                "If you passed --warehouse on the command line, it overrides .env — remove it to use "
                "SNOWFLAKE_WAREHOUSE from .env.snowflake."
            ) from e
        db = (args.database or "").strip()
        sc = (args.schema or "").strip()
        if db:
            cur.execute(f"USE DATABASE {_sql_double_quoted_ident(db)}")
        if sc:
            cur.execute(f"USE SCHEMA {_sql_double_quoted_ident(sc)}")
        cur.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
    finally:
        conn.close()

    if args.output == "json":
        data = [dict(zip(cols, row)) for row in rows]
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows(rows)
        sys.stdout.write(buf.getvalue())


if __name__ == "__main__":
    main()

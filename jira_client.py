"""
Minimal Jira REST API v3 client (issue search + optional remote links for web URLs).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterator

import requests


class JiraConfigError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if not v:
        raise JiraConfigError(f"Missing environment variable {name}")
    return v


def _json_text_from_response(resp: requests.Response) -> str:
    """
    Response body text suitable for ``json.loads`` — strip BOM and optional XSSI / JSON-hijack prefix
    (some DCs or proxies prepend ``)]}'`` before JSON).
    """
    raw = (resp.text or "").lstrip("\ufeff").lstrip()
    m = re.match(r"^\)\]\}'\s*", raw)
    if m:
        raw = raw[m.end() :].lstrip()
    return raw


def _looks_like_jira_myself(d: Any) -> bool:
    """True if ``d`` looks like a ``GET /rest/api/3/myself`` JSON object (not an error wrapper)."""
    if not isinstance(d, dict):
        return False
    return any(
        k in d
        for k in (
            "self",
            "accountId",
            "name",
            "key",
            "emailAddress",
            "displayName",
            "avatarUrls",
        )
    )


def _auth_mode(base_url: str) -> str:
    """
    ``JIRA_AUTH_MODE``:

    - ``basic`` — Jira Cloud: ``JIRA_EMAIL`` + Atlassian **API token** (HTTP Basic).
    - ``bearer`` — some DC: ``Authorization: Bearer`` + PAT.
    - ``basic_pat`` — many Jira DC (incl. Workiva): HTTP Basic with **username** + **PAT as password**.

    If unset: ``basic`` on ``*.atlassian.net``; ``bearer`` on ``*.workiva.net`` (PAT is often Bearer-only there);
    else ``bearer``. Use ``basic_pat`` explicitly if your DC requires username + PAT in Basic.
    """
    raw = (os.environ.get("JIRA_AUTH_MODE") or "").strip().casefold()
    if raw in ("bearer", "pat", "token"):
        return "bearer"
    if raw in ("basic", "cloud"):
        return "basic"
    if raw in ("basic_pat", "dc_basic", "pat_basic"):
        return "basic_pat"
    if "atlassian.net" in base_url.lower():
        return "basic"
    if "workiva.net" in base_url.lower():
        return "bearer"
    return "bearer"


def _raise_for_status(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        hint = ""
        if resp.status_code == 401:
            base = (os.environ.get("JIRA_BASE_URL") or "").lower()
            mode_env = (os.environ.get("JIRA_AUTH_MODE") or "").strip().casefold()
            extra = ""
            if "workiva.net" in base:
                extra = (
                    " **Workiva Jira:** do not use `JIRA_AUTH_MODE=basic` with an Atlassian *Cloud* API token — "
                    "that always fails here. Try **`JIRA_AUTH_MODE=bearer`** (PAT only) or **`basic_pat`** "
                    "(username + PAT as Basic password); default on *.workiva.net is **bearer**. "
                    "Run `python .../build_release_notes.py --jira-ping-diagnose -v` and use the line that shows "
                    "**[REST OK]** — not HTTP 200 with **HTML body**."
                )
                if mode_env == "basic":
                    extra += (
                        " You currently have `JIRA_AUTH_MODE=basic` — change to **`bearer`** or **`basic_pat`** "
                        "(or remove `JIRA_AUTH_MODE` on *.workiva.net to use the default **bearer**)."
                    )
                uname = (os.environ.get("JIRA_USERNAME") or os.environ.get("JIRA_EMAIL") or "").strip()
                if "@" in uname:
                    local = uname.split("@", 1)[0]
                    extra += (
                        f" Your login id is an **email** (`{uname}`). Many DC servers want the **short id** "
                        f"(try `JIRA_USERNAME={local}`) or set `JIRA_STRIP_EMAIL_DOMAIN=true` to use `{local}` automatically. "
                        "If PAT + short user still fails, try `JIRA_AUTH_MODE=bearer` with the same PAT."
                    )
                elif uname:
                    extra += (
                        " Short **JIRA_USERNAME** is set but still 401: `JIRA_API_TOKEN` may not be a **Jira Personal "
                        "Access Token** from this site (wrong key type, expired, or typo). Run "
                        "`python …/build_release_notes.py --jira-ping-diagnose -v` and look for **`OK displayName=`** "
                        "(real REST JSON). **Ignore** diagnose lines that say **HTTP 200** but **HTML body** — that is "
                        "not successful API auth."
                    )
                if mode_env == "basic_pat":
                    extra += (
                        " With **`JIRA_AUTH_MODE=basic_pat`**, 401 means Jira did not accept **Basic** "
                        "(username + PAT as password). Regenerate the **PAT** on this Jira site, confirm the username "
                        "matches the web login id, VPN, and internal docs for REST + PAT."
                    )
            hint = (
                " 401: Jira rejected credentials. "
                "Cloud: JIRA_AUTH_MODE=basic + JIRA_EMAIL + Atlassian API token. "
                "DC PAT in Basic: JIRA_AUTH_MODE=basic_pat + JIRA_USERNAME (or EMAIL) + PAT as password. "
                "Some DC: JIRA_AUTH_MODE=bearer + PAT only."
                + extra
            )
        body = (resp.text or "")[:800]
        raise SystemExit(f"Jira HTTP {resp.status_code} {resp.url}\n{body}\n{hint}") from e


def _response_json_dict(resp: requests.Response, *, context: str) -> dict[str, Any]:
    """
    Parse Jira JSON object responses; give actionable errors on HTML / empty bodies (SSO, wrong base URL).
    """
    raw = _json_text_from_response(resp)
    if not raw.strip():
        raise SystemExit(
            f"Jira returned an empty body ({context}) status={resp.status_code} url={resp.url}\n"
            "Check JIRA_BASE_URL (if Jira lives under a context path, include it, e.g. "
            "https://host/jira), VPN, and that you are hitting the Jira REST API (not a load balancer HTML page)."
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if raw.lstrip()[:1] == "<":
            raise SystemExit(
                f"Jira returned HTML instead of JSON ({context}) status={resp.status_code} url={resp.url}\n"
                f"Content-Type: {resp.headers.get('Content-Type', '')}\n"
                f"First 500 chars:\n{raw[:500]}\n"
                "If the URL contains **login.jsp**, Jira did not accept REST auth. "
                "For **jira.atl.workiva.net** try **`JIRA_AUTH_MODE=bearer`** (PAT) or **`basic_pat`** "
                "(username + PAT). Run **`--jira-ping-diagnose -v`** and set **`JIRA_AUTH_MODE`** from the line "
                "that shows **`[REST OK]`** (JSON with `displayName`). **HTTP 200 + HTML** is not successful REST auth. "
                "Also confirm VPN and that you are hitting the Jira REST API (not a load balancer HTML page)."
            ) from e
        raise SystemExit(
            f"Jira response is not valid JSON ({context}) status={resp.status_code} url={resp.url}\n{e}\n"
            f"Content-Type: {resp.headers.get('Content-Type', '')}\nFirst 500 chars:\n{raw[:500]}"
        ) from e
    if not isinstance(data, dict):
        raise SystemExit(f"Jira returned JSON but not an object ({context}): {type(data).__name__}")
    return data


def _response_json_list(resp: requests.Response, *, context: str) -> list[Any]:
    raw = _json_text_from_response(resp)
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if raw.lstrip()[:1] == "<":
            raise SystemExit(
                f"Jira returned HTML instead of JSON ({context}) status={resp.status_code} url={resp.url}\n"
                f"First 400 chars:\n{raw[:400]}"
            ) from e
        raise SystemExit(
            f"Jira response is not valid JSON ({context}) status={resp.status_code} url={resp.url}\n{e}"
        ) from e
    if not isinstance(data, list):
        raise SystemExit(f"Jira returned JSON but not a list ({context}): {type(data).__name__}")
    return data


def _basic_pat_login_id() -> str:
    """Username for HTTP Basic when using a PAT as password."""
    user = (os.environ.get("JIRA_USERNAME") or os.environ.get("JIRA_EMAIL") or "").strip()
    flag = (os.environ.get("JIRA_STRIP_EMAIL_DOMAIN") or "").strip().casefold()
    if flag in ("1", "true", "yes", "on") and "@" in user:
        return user.split("@", 1)[0]
    return user


def jira_session(*, force_auth_mode: str | None = None) -> tuple[str, requests.Session]:
    """
    Returns ``(normalized_base_url, session)``.

    - **basic**: Cloud — ``JIRA_EMAIL`` + Atlassian API token (Basic).
    - **basic_pat**: DC / Workiva — ``JIRA_USERNAME`` or ``JIRA_EMAIL`` + PAT as Basic **password**.
    - **bearer**: ``Authorization: Bearer`` + PAT (some DC only).

    ``force_auth_mode``: override ``JIRA_AUTH_MODE`` / host default (for ``--jira-ping-diagnose``).
    """
    base = _require_env("JIRA_BASE_URL").rstrip("/")
    raw = (force_auth_mode or "").strip().casefold() if force_auth_mode else ""
    if raw in ("pat", "token"):
        raw = "bearer"
    mode = raw if raw in ("bearer", "basic", "basic_pat") else _auth_mode(base)
    token = _require_env("JIRA_API_TOKEN")
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    if mode == "bearer":
        s.headers["Authorization"] = f"Bearer {token}"
    elif mode == "basic_pat":
        user = _basic_pat_login_id()
        if not user:
            raise JiraConfigError(
                "JIRA_AUTH_MODE=basic_pat (default for *.workiva.net) requires JIRA_USERNAME or JIRA_EMAIL "
                "(your Jira / LDAP username — the same identifier you use to log into Jira in the browser)."
            )
        s.auth = (user, token)
    else:
        email = _require_env("JIRA_EMAIL")
        s.auth = (email, token)
    return base, s


def auth_debug_summary() -> str:
    """One-line description of resolved auth (no secrets). For ``--verbose`` / ``--jira-ping``."""
    base = (os.environ.get("JIRA_BASE_URL") or "").strip()
    mode = _auth_mode(base)
    raw_env = (os.environ.get("JIRA_AUTH_MODE") or "").strip()
    tok = "set" if (os.environ.get("JIRA_API_TOKEN") or "").strip() else "MISSING"
    if mode == "basic_pat":
        src = "JIRA_USERNAME" if (os.environ.get("JIRA_USERNAME") or "").strip() else "JIRA_EMAIL"
        raw_u = (os.environ.get("JIRA_USERNAME") or os.environ.get("JIRA_EMAIL") or "").strip() or "(missing)"
        u = _basic_pat_login_id() or "(missing)"
        strip_note = ""
        if raw_u != u and "@" in raw_u:
            strip_note = f" (effective Basic user={u!r} via JIRA_STRIP_EMAIL_DOMAIN)"
        u_disp = f"{raw_u!r}{strip_note}" if strip_note else f"{u!r}"
    elif mode == "bearer":
        src, u_disp = "—", "(Bearer only)"
    else:
        src = "JIRA_EMAIL"
        u_disp = repr((os.environ.get("JIRA_EMAIL") or "").strip() or "(missing)")
    auto = "" if raw_env else " (auto)"
    return (
        f"[dna-jira] JIRA_BASE_URL={base!r} JIRA_AUTH_MODE={raw_env or 'auto'}{auto} → {mode!r}; "
        f"login via {src}={u_disp}; JIRA_API_TOKEN={tok}"
    )


def jira_ping_myself(base: str, session: requests.Session) -> dict[str, Any]:
    """Lightweight auth check: ``GET /rest/api/2/myself``."""
    url = f"{base}/rest/api/2/myself"
    r = session.get(url, timeout=60)
    _raise_for_status(r)
    return _response_json_dict(r, context="GET /rest/api/2/myself")


def probe_myself(base: str, session: requests.Session) -> tuple[int, str, bool]:
    """
    ``GET /myself`` without raising.

    Returns ``(status_code, one_line_summary, rest_json_ok)``.
    ``rest_json_ok`` is True only when the response is usable **REST JSON** for ``/myself`` (not HTML with HTTP 200).
    """
    url = f"{base}/rest/api/2/myself"
    r = session.get(url, timeout=60)
    if r.status_code == 200:
        raw = _json_text_from_response(r)
        if not raw.strip():
            ct = r.headers.get("Content-Type", "")
            return 200, f"HTTP 200 but empty body (Content-Type={ct!r}) — not REST JSON", False
        if raw.lstrip()[:1] == "<":
            ct = r.headers.get("Content-Type", "")
            return (
                200,
                f"HTTP 200 but HTML body (Content-Type={ct!r}) — not API auth (SSO/WAF/login page?); do not use Bearer "
                "based on this alone",
                False,
            )
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and _looks_like_jira_myself(d):
                name = d.get("displayName") or d.get("name") or "?"
                return 200, f"OK displayName={name!r} name={d.get('name', '')!r}", True
            if isinstance(d, dict):
                return 200, f"HTTP 200 JSON but unexpected shape (keys sample: {list(d)[:8]!r})", False
            return 200, "HTTP 200 JSON but not an object", False
        except json.JSONDecodeError as e:
            ct = r.headers.get("Content-Type", "")
            return 200, f"HTTP 200 but non-JSON body (Content-Type={ct!r}): {e!s}", False
    snippet = (r.text or "").replace("\n", " ")[:220]
    return r.status_code, f"fail: {snippet!r}", False


def jira_print_auth_diagnosis() -> None:
    """
    Try ``GET /rest/api/3/myself`` with **bearer**, **basic_pat**, and (if ``JIRA_EMAIL`` set) **basic**,
    reusing the same ``JIRA_API_TOKEN`` (no secrets printed).
    """
    base = _require_env("JIRA_BASE_URL").rstrip("/")
    tok = "set" if (os.environ.get("JIRA_API_TOKEN") or "").strip() else "MISSING"
    print(f"[dna-jira] diagnose: base={base!r} token={tok}", flush=True)
    order: list[str] = ["bearer", "basic_pat"]
    if (os.environ.get("JIRA_EMAIL") or "").strip():
        order.append("basic")
    results: dict[str, int] = {}
    rest_ok: dict[str, bool] = {}
    for m in order:
        try:
            _b, s = jira_session(force_auth_mode=m)
        except JiraConfigError as e:
            print(f"  {m}: skip — {e}", flush=True)
            continue
        code, msg, ok = probe_myself(_b, s)
        results[m] = code
        rest_ok[m] = ok
        tag = " [REST OK]" if ok else ""
        print(f"  {m}: HTTP {code} — {msg}{tag}", flush=True)
    print(
        "[dna-jira] Use the mode whose line ends with **[REST OK]** (JSON `displayName` from `/myself`). "
        "Do **not** treat **HTTP 200 + HTML** as success — set `JIRA_AUTH_MODE` from a line with **[REST OK]** only. "
        "If none show [REST OK]: regenerate PAT on this Jira, check VPN, `JIRA_BASE_URL`, and IT docs for REST/SSO.",
        flush=True,
    )
    if rest_ok.get("bearer") and results.get("basic_pat") == 401:
        print(
            "[dna-jira] Recommendation: **JIRA_AUTH_MODE=bearer** — PAT works as `Authorization: Bearer` "
            "(Basic username+PAT returns 401 on this host).",
            flush=True,
        )
    elif results.get("bearer") == 200 and not rest_ok.get("bearer") and results.get("basic_pat") == 401:
        print(
            "[dna-jira] Neither mode returned REST JSON: **Bearer** got HTTP 200 but **HTML** (API auth did not "
            "succeed), and **basic_pat** got **401**. Regenerate a **Personal Access Token** on this Jira profile, "
            "confirm **VPN** and that `JIRA_BASE_URL` is the site root, and verify with IT whether PAT + REST is "
            "allowed through your SSO/WAF path.",
            flush=True,
        )


def search_issues_jql(
    base: str,
    session: requests.Session,
    jql: str,
    *,
    fields: list[str] | None = None,
    max_results_cap: int = 500,
    page_size: int = 50,
) -> Iterator[dict[str, Any]]:
    """
    Paginate ``POST /rest/api/3/search`` (Jira Cloud / compatible Server DC).
    """
    fields = fields or [
        "summary",
        "status",
        "issuetype",
        "components",
        "description",
        "updated",
        "resolutiondate",
        "labels",
        "fixVersions",
        "parent",
        "issuelinks",
    ]
    url = f"{base}/rest/api/2/search"
    start_at = 0
    total_yielded = 0

    while total_yielded < max_results_cap:
        n = min(page_size, max_results_cap - total_yielded)
        body: dict[str, Any] = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": n,
            "fields": fields,
        }
        r = session.post(url, json=body, timeout=120)
        _raise_for_status(r)
        data = _response_json_dict(r, context="POST /rest/api/3/search")
        issues = data.get("issues") or []
        for issue in issues:
            yield issue
        total_yielded += len(issues)
        if len(issues) < n:
            return
        start_at += len(issues)
        t = data.get("total")
        if isinstance(t, int) and start_at >= t:
            return


def list_remote_links(base: str, session: requests.Session, issue_key: str) -> list[dict[str, Any]]:
    """GitHub / other web links developers attach to the issue."""
    url = f"{base}/rest/api/2/issue/{issue_key}/remotelink"
    r = session.get(url, timeout=60)
    if r.status_code == 404:
        return []
    _raise_for_status(r)
    return _response_json_list(r, context=f"GET /rest/api/3/issue/{issue_key}/remotelink")


def extract_urls_from_remotelinks(payload: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for block in payload:
        obj = block.get("object") or {}
        u = obj.get("url")
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
    return urls


def extract_issue_keys(text: str, pattern: str) -> list[str]:
    if not text:
        return []
    return list(dict.fromkeys(re.findall(pattern, text)))

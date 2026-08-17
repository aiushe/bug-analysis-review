#!/usr/bin/env python3
"""
pull_bugs.py — pull bugs from a Jira Server / Data Center site for a release.

Runs wherever your Jira is reachable (a self-hosted site is often only
reachable from inside the corporate network). Reads the site URL from
JIRA_BASE_URL and a Personal Access Token from JIRA_PAT — both from the
environment or a .env file in this folder — paginates the Jira REST v2 search,
and writes a JSON file the bug-review skill consumes.

USAGE
    export JIRA_BASE_URL=https://jira.example.com   # one-time, or use .env
    export JIRA_PAT=...                            # one-time, or use .env
    python3 pull_bugs.py --version 1.2.2
    python3 pull_bugs.py --version 1.2.2 \
        --components "Component A,Component B" \
        --out bugs_1.2.2.json

GENERATE A PAT
    1. Log in to your Jira site.
    2. Avatar (top-right) → Profile → Personal Access Tokens → Create token
    3. Name it anything ("bug review"). Expiry: whatever your policy allows.
    4. Copy the token. Save to a .env file in this folder:
           printf 'JIRA_BASE_URL=https://jira.example.com\nJIRA_PAT=<paste>\n' > .env
       OR export both in your shell rc.

OUTPUT SHAPE
    {
      "meta": { "version": "1.2.2", "jql": "...", "pulled_at": "...",
                "total": N, "base_url": "https://jira.example.com" },
      "bugs": [
        { "key": "PROJ-123", "summary": "...", "description": "...",
          "components": [...], "labels": [...], "priority": "...",
          "status": "...", "resolution": "...", "issuetype": "Bug",
          "fix_versions": [...], "affects_versions": [...],
          "reporter": "...", "assignee": "...", "created": "...",
          "resolved": "...", "parent": "...", "epic_link": "...",
          "linked_issues": [{"type": "...", "key": "..."}, ...],
          "comments": [{"author": "...", "created": "...", "body": "..."}, ...],
          "custom_fields": { ... },
          "url": "https://jira.example.com/browse/PROJ-123" },
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request

# Site + credentials come from the environment (or scripts/.env) so the same
# checkout works against any Jira.
BASE_URL_ENV_VARS = ("JIRA_BASE_URL", "JIRA_URL")
TOKEN_ENV_VARS = ("JIRA_PAT", "JIRA_TOKEN")
PAGE_SIZE = 100
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds; doubles each retry

# Fields we always pull. Anything custom (RCA, root cause, phase introduced)
# is captured from the raw `fields` dict and stashed under `custom_fields`
# without us needing to know the customfield_NNNNN ID.
CORE_FIELDS = [
    "summary",
    "description",
    "status",
    "resolution",
    "issuetype",
    "priority",
    "components",
    "labels",
    "fixVersions",
    "versions",
    "reporter",
    "assignee",
    "created",
    "resolutiondate",
    "parent",
    "issuelinks",
    "comment",
    # epic link is usually a customfield, but we ask Jira for "*all" anyway
]


def load_dotenv(script_dir: Path) -> None:
    """Tiny .env loader so users don't need python-dotenv."""
    env_path = script_dir / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Load scripts/.env at import time so any module that reads configuration into a
# constant (write_back.py's field names, for example) sees .env values too, not
# just values exported in the shell.
load_dotenv(Path(__file__).resolve().parent)


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def env_base_url() -> str:
    """Jira site URL from the environment; empty string if unconfigured."""
    return _first_env(BASE_URL_ENV_VARS).rstrip("/")


def resolve_base_url(cli_value: str | None = None) -> str:
    """--base-url if given, else the environment. Exits with setup help if neither."""
    base = (cli_value or env_base_url()).rstrip("/")
    if not base:
        raise SystemExit(
            "\n  Jira site URL not set. Either pass --base-url, or set\n"
            "    export JIRA_BASE_URL=https://jira.example.com\n"
            "  or add JIRA_BASE_URL=... to scripts/.env\n"
        )
    return base


def resolve_token(script_dir: Path | None = None) -> str:
    """Personal Access Token from the environment. Exits with setup help if unset."""
    token = _first_env(TOKEN_ENV_VARS)
    if not token:
        env_hint = f"{script_dir}/.env" if script_dir else "scripts/.env"
        raise SystemExit(
            "\n  JIRA_PAT not set. Create a Personal Access Token in Jira\n"
            "  (Profile → Personal Access Tokens) and either:\n"
            "    export JIRA_PAT=...\n"
            f"  or add it to {env_hint}:\n"
            "    JIRA_PAT=...\n"
        )
    return token


def env_components() -> list[str]:
    """Default component filter from JIRA_COMPONENTS; empty = all components."""
    return [c.strip() for c in os.environ.get("JIRA_COMPONENTS", "").split(",") if c.strip()]


def browse_base_url(fallback: str = "") -> str:
    """`<site>/browse/` prefix for issue links; empty string if unconfigured."""
    base = env_base_url() or (fallback or "").rstrip("/")
    return f"{base}/browse/" if base else ""


def build_jql(version: str, components: list[str], extra: str | None) -> str:
    jql = f'fixVersion = "{version}" AND issuetype = Bug'
    if components:
        quoted = ", ".join(f'"{c}"' for c in components)
        jql = f'fixVersion = "{version}" AND component in ({quoted}) AND issuetype = Bug'
    if extra:
        jql = f"({jql}) AND ({extra})"
    return jql


def http_get(url: str, token: str) -> dict[str, Any]:
    req = request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "bug-review/1.0",
        },
    )
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            # Auth failures shouldn't be retried.
            if e.code in (401, 403):
                raise SystemExit(
                    f"\n  HTTP {e.code} from Jira.\n"
                    f"  PAT is missing, expired, or lacks Browse Projects "
                    f"permission on the project.\n  Body: {body}\n"
                )
            last_err = RuntimeError(f"HTTP {e.code}: {body}")
        except error.URLError as e:
            last_err = e
        sleep_for = RETRY_BACKOFF * (2 ** attempt)
        print(
            f"  retry {attempt + 1}/{MAX_RETRIES} after {sleep_for:.0f}s: "
            f"{last_err}",
            file=sys.stderr,
        )
        time.sleep(sleep_for)
    raise SystemExit(f"\n  Gave up after {MAX_RETRIES} retries: {last_err}\n")


def normalize_user(u: Any) -> str | None:
    if not u:
        return None
    if isinstance(u, dict):
        return u.get("displayName") or u.get("name") or u.get("emailAddress")
    return str(u)


def normalize_bug(raw: dict[str, Any], base_url: str) -> dict[str, Any]:
    key = raw.get("key", "")
    f = raw.get("fields", {}) or {}

    def opt_name(v: Any) -> str | None:
        return v.get("name") if isinstance(v, dict) else None

    components = [c.get("name") for c in (f.get("components") or []) if c]
    fix_versions = [v.get("name") for v in (f.get("fixVersions") or []) if v]
    affects = [v.get("name") for v in (f.get("versions") or []) if v]

    linked = []
    for link in f.get("issuelinks", []) or []:
        link_type = (link.get("type") or {}).get("name")
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        if other.get("key"):
            linked.append({
                "type": link_type,
                "direction": "outward" if link.get("outwardIssue") else "inward",
                "key": other["key"],
                "summary": (other.get("fields") or {}).get("summary"),
            })

    comments_raw = (f.get("comment") or {}).get("comments", []) or []
    comments = [
        {
            "author": normalize_user(c.get("author")),
            "created": c.get("created"),
            "body": c.get("body"),
        }
        for c in comments_raw
    ]

    parent = (f.get("parent") or {}).get("key") if isinstance(f.get("parent"), dict) else None

    # Sweep up custom fields so we can spot RCA / phase / etc. without knowing
    # their numeric IDs in advance. Skip empty values to keep the JSON lean.
    custom_fields: dict[str, Any] = {}
    epic_link: str | None = None
    for fname, fval in f.items():
        if not fname.startswith("customfield_"):
            continue
        if fval in (None, [], "", {}):
            continue
        # Best-effort epic link detection: Jira Server stores it as a string key.
        if isinstance(fval, str) and fval[:3].isalpha() and "-" in fval and epic_link is None:
            # Heuristic only — final identification happens in the skill.
            pass
        custom_fields[fname] = fval

    return {
        "key": key,
        "summary": f.get("summary"),
        "description": f.get("description"),
        "components": components,
        "labels": f.get("labels", []) or [],
        "priority": opt_name(f.get("priority")),
        "status": opt_name(f.get("status")),
        "resolution": opt_name(f.get("resolution")),
        "issuetype": opt_name(f.get("issuetype")),
        "fix_versions": fix_versions,
        "affects_versions": affects,
        "reporter": normalize_user(f.get("reporter")),
        "assignee": normalize_user(f.get("assignee")),
        "created": f.get("created"),
        "resolved": f.get("resolutiondate"),
        "parent": parent,
        "epic_link": epic_link,  # may stay None; skill can recover from custom_fields
        "linked_issues": linked,
        "comments": comments,
        "custom_fields": custom_fields,
        "url": f"{base_url.rstrip('/')}/browse/{key}",
    }


def search_all(base_url: str, token: str, jql: str) -> list[dict[str, Any]]:
    bugs: list[dict[str, Any]] = []
    start_at = 0
    total = None
    while True:
        params = parse.urlencode({
            "jql": jql,
            "startAt": start_at,
            "maxResults": PAGE_SIZE,
            "fields": "*all",
            "expand": "renderedFields",
        })
        url = f"{base_url.rstrip('/')}/rest/api/2/search?{params}"
        page = http_get(url, token)
        issues = page.get("issues", []) or []
        if total is None:
            total = page.get("total", 0)
            print(f"  total matched: {total}")
        bugs.extend(normalize_bug(i, base_url) for i in issues)
        start_at += len(issues)
        if start_at >= total or not issues:
            break
        print(f"  pulled {start_at}/{total}")
    return bugs


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pull bugs from Jira for a release.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", required=True, help='Fix version, e.g. "1.2.2".')
    p.add_argument(
        "--components",
        default=None,
        help=("Comma-separated component list. Default: $JIRA_COMPONENTS, "
              "or every component on the release when that is unset."),
    )
    p.add_argument(
        "--jql",
        default=None,
        help="Extra JQL appended with AND. Example: 'priority in (Blocker, Critical)'.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output JSON path. Default: bugs_<version>.json next to this script.",
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    load_dotenv(script_dir)

    token = resolve_token(script_dir)
    base_url = resolve_base_url(args.base_url)

    components = (
        [c.strip() for c in args.components.split(",") if c.strip()]
        if args.components is not None
        else env_components()
    )
    jql = build_jql(args.version, components, args.jql)

    out_path = Path(args.out) if args.out else script_dir / f"bugs_{args.version}.json"

    print(f"Pulling bugs for {args.version}")
    print(f"  components: {components or 'all'}")
    print(f"  JQL: {jql}")
    print(f"  base URL: {base_url}")
    print(f"  output: {out_path}")

    bugs = search_all(base_url, token, jql)

    output = {
        "meta": {
            "version": args.version,
            "components": components,
            "jql": jql,
            "base_url": base_url,
            "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "total": len(bugs),
        },
        "bugs": bugs,
    }
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n  Wrote {len(bugs)} bugs to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

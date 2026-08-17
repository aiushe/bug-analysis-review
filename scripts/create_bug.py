#!/usr/bin/env python3
"""
create_bug.py — create a single top-level issue (Bug) in Jira.

Runs wherever your Jira is reachable. Reuses pull_bugs.py machinery
(PAT from .env / JIRA_PAT, base URL, retrying GET).
Reads a JSON spec ({meta, fields}) and POSTs one issue via Jira REST v2.

Always discover first, then dry-run, then apply:

    python3 create_bug.py --spec bug_get_param_types.json --discover
    python3 create_bug.py --spec bug_get_param_types.json --dry-run
    python3 create_bug.py --spec bug_get_param_types.json

--discover  prints the required fields + allowed priorities/components for the
            project+issuetype in the spec, so you can fix the spec before applying.
--dry-run   prints the exact payload that would be POSTed (no write).
(no flag)   creates the issue and prints the new key + URL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from pull_bugs import http_get, load_dotenv, resolve_base_url, resolve_token  # type: ignore


def discover(base_url: str, token: str, project: str, issuetype: str) -> None:
    url = (
        f"{base_url.rstrip('/')}/rest/api/2/issue/createmeta"
        f"?projectKeys={project}&issuetypeNames={issuetype}"
        f"&expand=projects.issuetypes.fields"
    )
    data = http_get(url, token)
    projects = data.get("projects") or []
    if not projects:
        print(f"  No createmeta for project {project}. Check the project key / your access.")
        return
    for it in projects[0].get("issuetypes", []):
        if it.get("name", "").lower() != issuetype.lower():
            continue
        fields = it.get("fields", {})
        print(f"\n=== {project} / {issuetype}: required & notable fields ===\n")
        for fid, meta in fields.items():
            required = meta.get("required")
            name = meta.get("name")
            allowed = meta.get("allowedValues")
            tag = "REQUIRED" if required else "optional"
            line = f"  [{tag}] {fid:<18} {name}"
            if allowed:
                vals = [a.get("value") or a.get("name") for a in allowed][:12]
                line += f"  allowed: {vals}"
            if required or name in ("Priority", "Component/s", "Affects Version/s",
                                    "Fix Version/s", "Severity"):
                print(line)
    print("\n  Put any REQUIRED field not already in the spec under fields{} "
          "(use the customfield_xxxxx id shown above).")


def create_issue(base_url: str, token: str, fields: dict[str, Any]) -> tuple[str | None, str]:
    payload = {"fields": fields}
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/rest/api/2/issue",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "bug-review-create/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("key"), f"HTTP {resp.getcode()}"
    except error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:600]}"
    except error.URLError as e:
        return None, str(e)


def build_fields(spec: dict[str, Any]) -> dict[str, Any]:
    meta = spec["meta"]
    f: dict[str, Any] = {
        "project": {"key": meta["project"]},
        "issuetype": {"name": meta.get("issue_type_name", "Bug")},
        "summary": spec["summary"],
        "description": spec.get("description", ""),
    }
    if meta.get("priority"):
        f["priority"] = {"name": meta["priority"]}
    if meta.get("components"):
        f["components"] = [{"name": c} for c in meta["components"]]
    if meta.get("affects_versions"):
        f["versions"] = [{"name": v} for v in meta["affects_versions"]]
    if meta.get("fix_versions"):
        f["fixVersions"] = [{"name": v} for v in meta["fix_versions"]]
    if meta.get("labels"):
        f["labels"] = meta["labels"]
    # Any extra/required custom fields go here verbatim and win over the above.
    for fid, val in (spec.get("fields") or {}).items():
        f[fid] = val
    return f


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create a single issue from a JSON spec.")
    p.add_argument("--spec", required=True, help="Path to the JSON spec (meta + summary + description).")
    p.add_argument("--discover", action="store_true", help="Print required fields and exit.")
    p.add_argument("--dry-run", action="store_true", help="Print payload, do not create.")
    p.add_argument("--base-url", default=None,
                   help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(SCRIPT_DIR)
    token = resolve_token(SCRIPT_DIR)
    base_url = resolve_base_url(args.base_url)

    spec = json.loads(Path(args.spec).read_text())
    meta = spec["meta"]

    if args.discover:
        discover(base_url, token, meta["project"], meta.get("issue_type_name", "Bug"))
        return 0

    fields = build_fields(spec)

    if args.dry_run:
        print("=== DRY RUN: payload that would be POSTed ===\n")
        print(json.dumps({"fields": fields}, indent=2))
        print("\n  Dry run. Re-run without --dry-run to create.")
        return 0

    key, msg = create_issue(base_url, token, fields)
    if key:
        print(f"  OK: created {key}")
        print(f"  {base_url.rstrip('/')}/browse/{key}")
        return 0
    print(f"  FAIL: {msg}")
    print("\n  Tip: run with --discover to see required fields, add them to the "
          "spec's fields{} block, then retry.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

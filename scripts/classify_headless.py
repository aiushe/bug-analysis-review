#!/usr/bin/env python3
"""
classify_headless.py — run the bug-review classification + feature-
matching pass via a headless Claude Code invocation (`claude -p`), replacing
a hand-typed classification dict.

The prompt does NOT inline the rubric or SKILL.md text — it tells the model
to Read reference/classification_rubric.md, SKILL.md, and the pulled JSON
itself, so there's exactly one source of truth for the rules. The model
writes analysis_<version>.json directly via its own Write tool, in the
canonical schema documented in SKILL.md and checked by validate_schema.py.

Runs with an explicit, minimal allowlist — no --dangerously-skip-permissions
— because the .claude/settings.json in this repo already scopes exactly the
Read/Write access this step needs (see that file's comments). If a required
file is outside the allowlist, the tool call is denied (fail-closed) rather
than prompted, since -p has no TTY to prompt on; the post-run schema
validation below is what catches an incomplete result.

USAGE
    python3 classify_headless.py --version 1.2.6.0 \
        --prev-versions 1.2.5.0,1.2.4.0 [--bug-limit 5]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
from validate_schema import validate_analysis  # type: ignore

SCHEMA_EXAMPLE = """{
  "meta": {
    "version": "<version>", "display_version": "<x.y.z>",
    "generated_at": "<ISO8601>", "components": ["..."],
    "bug_source": "bugs_<version>.json",
    "feature_pool": ["features_<version>.json", "features_<prev>.json", ...],
    "classifier": "claude-headless", "total_bugs": <int>
  },
  "bugs": [
    {
      "key": "PROJ-93888", "summary": "...", "priority": "...", "status": "...",
      "pm_rca_type": "Incomplete requirements" | "Requirement–expectation mismatch" |
                      "Missed review by PM/QE" | "Feature gap" | "Invalid defect" | null,
      "confidence": "high" | "medium" | "low",
      "confidence_basis": "one line citing the evidence (comment/label/pre-filled field, or absence of it)",
      "edge_case": "3-6 word noun phrase naming the missed scenario",
      "pm_analysis": "2-4 polished sentences for the PM Analysis field (drafted, never auto-written)",
      "feature_key": "PROJ-93467" | null,
      "feature_match_basis": "linked_issues" | "prd_content" | null,
      "component_area": "platform component name if unlinked, else null"
    }
  ],
  "themes": [
    {"name": "short theme name", "bug_keys": ["PROJ-...", "..."], "description": "recurring pattern in plain product language"}
  ],
  "prd_lessons": ["3-7 plain-language PRD lessons, per SKILL.md step 4"],
  "qe_lessons": ["3-5 plain-language QE lessons, per SKILL.md step 4"]
}"""


def build_prompt(version: str, prev_versions: list[str], bug_limit: int | None) -> str:
    prev_str = ", ".join(prev_versions) if prev_versions else "(none given)"
    limit_clause = (
        f"\nOnly classify the first {bug_limit} bugs in bugs_{version}.json "
        "(by list order) — this is a limited smoke-test run, not a full pass."
        if bug_limit
        else ""
    )
    return f"""You are running the classification + feature-matching phase of the
bug-review skill for release {version} (prior releases for the feature
pool: {prev_str}), non-interactively.

Before doing anything else, Read these files in this order:
  1. {SKILL_DIR / 'reference/classification_rubric.md'} — the PM RCA Type rubric, confidence
     definitions, and evidence priority order. This is the source of truth.
  2. {SKILL_DIR / 'SKILL.md'} — read step 2 ("Classify each bug") and step 3 ("Match each bug
     to a feature") in full, including the priority-1 (linked_issues) vs
     priority-2 (PRD content) feature-matching distinction.
  3. {SCRIPT_DIR / f'bugs_{version}.json'} — the bugs to classify.
  4. {SCRIPT_DIR / f'features_{version}.json'} and every features_<prev>.json for the
     prior versions listed above — the candidate feature pool. Read each
     feature's `description` field as the actual PRD text, not just its title.

For every bug, assign exactly one PM RCA Type per the rubric (or null if the
evidence doesn't support any value with reasonable confidence — never invent
a "None" value, per the rubric). Determine feature_match_basis exactly:
"linked_issues" ONLY if the bug's own `linked_issues` array already contains
an EGS key from the candidate pool (a fact already in the JIRA data, not your
inference). "prd_content" if you matched it by reading the feature's PRD
description and judging it covers the bug's area. null if unlinked — group
unlinked bugs by component_area instead of force-fitting a feature.
{limit_clause}

Also cluster the classified bugs into recurring themes and draft PRD/QE
lessons per SKILL.md step 4 ("Aggregate findings") — plain product language,
not engineering jargon (SKILL.md's "Plain product language" section). If the
bug set is too small to support a real pattern (e.g. this is a --bug-limit
smoke test), it's fine to leave themes/prd_lessons/qe_lessons as empty lists
rather than manufacturing one from too little evidence.

Write the result to {SCRIPT_DIR / f'analysis_{version}.json'} via your Write tool, in
exactly this schema (every field required on every bug object; use null, not
missing keys, for fields that don't apply):

{SCHEMA_EXAMPLE}

Do not write any other file. Do not use Bash. When finished, stop — do not
summarize your work in prose, the calling script only inspects the JSON file."""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Headless classification pass via claude -p.")
    p.add_argument("--version", required=True)
    p.add_argument(
        "--prev-versions",
        default="",
        help="Comma-separated prior release versions to include in the feature pool.",
    )
    p.add_argument("--bug-limit", type=int, default=None, help="Smoke-test cap on bug count.")
    p.add_argument(
        "--settings",
        default=str(SKILL_DIR / ".claude" / "headless-settings.json"),
        help="Path to the scoped settings.json for this invocation.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    prev_versions = [v.strip() for v in args.prev_versions.split(",") if v.strip()]
    prompt = build_prompt(args.version, prev_versions, args.bug_limit)

    analysis_path = SCRIPT_DIR / f"analysis_{args.version}.json"

    cmd = [
        "claude",
        "-p",
        prompt,
        "--allowedTools",
        "Read,Write",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if Path(args.settings).exists():
        cmd += ["--settings", args.settings]

    print(f"Running headless classification for {args.version} (prev: {prev_versions or 'none'})")
    print(f"  writing: {analysis_path}")
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    if result.returncode != 0:
        print(f"\n  claude -p exited {result.returncode}", file=sys.stderr)
        return result.returncode

    if not analysis_path.exists():
        print(
            f"\n  {analysis_path} was not written. The headless run may have been "
            "denied a tool call (check the permissions allowlist) or failed silently.",
            file=sys.stderr,
        )
        return 3

    doc = json.loads(analysis_path.read_text())
    errors = validate_analysis(doc)
    if errors:
        print(f"\n  {analysis_path} failed schema validation ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"    - {e}", file=sys.stderr)
        return 4

    print(f"\n  {analysis_path}: {doc['meta']['total_bugs']} bugs classified, schema valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

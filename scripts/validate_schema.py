#!/usr/bin/env python3
"""
validate_schema.py — plain-dict validation for analysis_<version>.json and
plan_<version>.json. No jsonschema dependency, matching this repo's
stdlib-only style. Returns a list of error strings (empty = valid) rather
than raising, so callers can decide whether to hard-fail or just warn.
"""

from __future__ import annotations

from typing import Any

RCA_VALUES = {
    "Incomplete requirements",
    "Requirement–expectation mismatch",
    "Missed review by PM/QE",
    "Feature gap",
    "Invalid defect",
    None,
}
CONFIDENCE_VALUES = {"high", "medium", "low"}
FEATURE_MATCH_BASIS_VALUES = {"linked_issues", "prd_content", None}

REQUIRED_BUG_FIELDS = (
    "key",
    "summary",
    "pm_rca_type",
    "confidence",
    "confidence_basis",
    "edge_case",
    "pm_analysis",
    "feature_key",
    "feature_match_basis",
    "component_area",
)


def validate_analysis(doc: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["top-level document is not an object"]

    meta = doc.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta is missing or not an object")
    else:
        for k in ("version", "generated_at", "total_bugs"):
            if k not in meta:
                errors.append(f"meta.{k} is missing")

    bugs = doc.get("bugs")
    if not isinstance(bugs, list):
        errors.append("bugs is missing or not a list")
        return errors
    if not bugs:
        errors.append("bugs list is empty")

    seen_keys: set[str] = set()
    for i, b in enumerate(bugs):
        if not isinstance(b, dict):
            errors.append(f"bugs[{i}] is not an object")
            continue
        key = b.get("key")
        if not key:
            errors.append(f"bugs[{i}] has no key")
            continue
        if key in seen_keys:
            errors.append(f"bugs[{i}] duplicate key {key!r}")
        seen_keys.add(key)

        for field in REQUIRED_BUG_FIELDS:
            if field not in b:
                errors.append(f"{key}: missing field {field!r}")

        if b.get("pm_rca_type") not in RCA_VALUES:
            errors.append(f"{key}: invalid pm_rca_type {b.get('pm_rca_type')!r}")
        if b.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(f"{key}: invalid confidence {b.get('confidence')!r}")
        if b.get("feature_match_basis") not in FEATURE_MATCH_BASIS_VALUES:
            errors.append(f"{key}: invalid feature_match_basis {b.get('feature_match_basis')!r}")
        if b.get("feature_key") and b.get("feature_match_basis") is None:
            errors.append(f"{key}: has feature_key but no feature_match_basis")
        if b.get("feature_match_basis") and not b.get("feature_key"):
            errors.append(f"{key}: has feature_match_basis but no feature_key")

    return errors


def validate_plan(doc: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["top-level document is not an object"]
    if "actions" not in doc or not isinstance(doc["actions"], list):
        errors.append("actions is missing or not a list")
        return errors
    for i, a in enumerate(doc["actions"]):
        if not isinstance(a, dict):
            errors.append(f"actions[{i}] is not an object")
            continue
        if not a.get("bug_key"):
            errors.append(f"actions[{i}] has no bug_key")
        rca = a.get("set_pm_rca_type")
        if rca is not None and rca not in RCA_VALUES:
            errors.append(f"actions[{i}] ({a.get('bug_key')}): invalid set_pm_rca_type {rca!r}")
    return errors


def main() -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="Validate an analysis or plan JSON file.")
    p.add_argument("path")
    p.add_argument("--kind", choices=["analysis", "plan"], required=True)
    args = p.parse_args()

    doc = json.loads(open(args.path).read())
    errors = validate_analysis(doc) if args.kind == "analysis" else validate_plan(doc)
    if errors:
        print(f"{len(errors)} error(s) in {args.path}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"{args.path}: valid")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

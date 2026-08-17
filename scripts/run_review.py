#!/usr/bin/env python3
"""
run_review.py — single entry point for the bug review pipeline.

Chains: pull bugs+features (current + prior releases) -> headless classify
(claude -p) -> build deliverables (doc/xlsx/plan) -> split into auto/review
tiers -> dry-run the auto tier -> canary + full apply the auto tier ->
verify against Jira -> write NEEDS_REVIEW_<version>.md -> append the run
ledger. Only the auto tier (high-confidence RCA calls, feature links that
were already an explicit JIRA link) ever gets applied by this script itself
— the review tier always waits for a human to run write_back.py by hand with
--allow-review-tier, per SKILL.md's "the PM's mapping wins" policy.

USAGE
    python3 run_review.py --version 1.2.6.0 --prev-versions 1.2.5.0,1.2.4.0
    python3 run_review.py --version 1.2.5.0 --prev-versions 1.2.4.0,1.2.3.0 \
        --reuse-pulls --bug-limit 5 --dry-run-only   # smoke test, read-only against Jira
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib_run import RunLogger, append_ledger, run_id  # type: ignore
from pull_bugs import load_dotenv, resolve_base_url, resolve_token  # type: ignore
from validate_schema import validate_analysis  # type: ignore
import write_back  # type: ignore


def run(cmd: list[str], step: str) -> subprocess.CompletedProcess:
    print(f"\n--- {step}: {' '.join(cmd)} ---")
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR), capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def check_jira_reachable(base_url: str) -> bool:
    try:
        req = urllib.request.Request(base_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.getcode() < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def preflight(args: argparse.Namespace) -> str:
    load_dotenv(SCRIPT_DIR)
    resolve_token(SCRIPT_DIR)
    base_url = resolve_base_url(args.base_url)

    import shutil

    if not shutil.which("claude"):
        raise SystemExit("`claude` CLI not found on PATH — required for the headless classify step.")

    if not check_jira_reachable(base_url):
        raise SystemExit(
            f"Jira ({base_url}) is not reachable from this machine. A self-hosted site is "
            "often only reachable from inside the corporate network — see SKILL.md's "
            "'Environment note'."
        )
    print(f"preflight OK — token set, claude CLI present, {base_url} reachable")
    return base_url


def pull_if_needed(version: str, components: str | None, reuse: bool) -> None:
    bugs_path = SCRIPT_DIR / f"bugs_{version}.json"
    if reuse and bugs_path.exists():
        print(f"  reusing existing {bugs_path}")
    else:
        cmd = [sys.executable, "pull_bugs.py", "--version", version]
        if components:
            cmd += ["--components", components]
        result = run(cmd, f"pull bugs {version}")
        if result.returncode != 0:
            raise SystemExit(f"pull_bugs.py failed for {version} (exit {result.returncode})")

    features_path = SCRIPT_DIR / f"features_{version}.json"
    if reuse and features_path.exists():
        print(f"  reusing existing {features_path}")
    else:
        result = run([sys.executable, "pull_features.py", "--version", version], f"pull features {version}")
        if result.returncode != 0:
            raise SystemExit(f"pull_features.py failed for {version} (exit {result.returncode})")


def pull_prev_features_if_needed(version: str, reuse: bool) -> None:
    features_path = SCRIPT_DIR / f"features_{version}.json"
    if reuse and features_path.exists():
        print(f"  reusing existing {features_path}")
        return
    result = run([sys.executable, "pull_features.py", "--version", version], f"pull prior features {version}")
    if result.returncode != 0:
        raise SystemExit(f"pull_features.py failed for prior version {version} (exit {result.returncode})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the full bug review pipeline.")
    p.add_argument("--version", required=True, help="4-part release version, e.g. 1.2.6.0.")
    p.add_argument(
        "--prev-versions",
        default="",
        help="Comma-separated prior release versions to include in the feature pool.",
    )
    p.add_argument("--components", default=None, help="Passed through to pull_bugs.py.")
    p.add_argument("--reuse-pulls", action="store_true", help="Skip pulling if bugs/features JSON already exist.")
    p.add_argument("--bug-limit", type=int, default=None, help="Smoke-test cap passed to classify_headless.py.")
    p.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Stop after the auto-tier dry-run; never apply anything to Jira.",
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    prev_versions = [v.strip() for v in args.prev_versions.split(",") if v.strip()]
    rid = run_id(args.version)
    steps: dict[str, str] = {}
    counts: dict[str, Any] = {}
    apply_result: dict[str, Any] = {}
    verify_result: dict[str, Any] = {"mismatches": 0}

    with RunLogger(args.version, rid) as logger:
        try:
            base_url = preflight(args)
            steps["preflight"] = "ok"

            print(f"\n=== 1/9 pull: {args.version} + prior {prev_versions} ===")
            pull_if_needed(args.version, args.components, args.reuse_pulls)
            for v in prev_versions:
                pull_prev_features_if_needed(v, args.reuse_pulls)
            steps["pull"] = "ok"

            print(f"\n=== 2/9 classify (headless) ===")
            classify_cmd = [sys.executable, "classify_headless.py", "--version", args.version]
            if prev_versions:
                classify_cmd += ["--prev-versions", ",".join(prev_versions)]
            if args.bug_limit:
                classify_cmd += ["--bug-limit", str(args.bug_limit)]
            result = run(classify_cmd, "classify_headless.py")
            if result.returncode != 0:
                raise SystemExit(f"classify_headless.py failed (exit {result.returncode})")
            steps["classify"] = "ok"

            analysis_path = SCRIPT_DIR / f"analysis_{args.version}.json"
            analysis = json.loads(analysis_path.read_text())
            errors = validate_analysis(analysis)
            if errors:
                raise SystemExit(f"{analysis_path} failed schema validation: {errors}")
            counts["bugs_total"] = analysis["meta"]["total_bugs"]

            print(f"\n=== 3/9 build deliverables ===")
            result = run([sys.executable, "build_deliverables.py", "--version", args.version], "build_deliverables.py")
            if result.returncode != 0:
                raise SystemExit(f"build_deliverables.py failed (exit {result.returncode})")
            steps["build"] = "ok"

            print(f"\n=== 4/9 split auto/review ===")
            result = run([sys.executable, "split_plan.py", "--version", args.version], "split_plan.py")
            if result.returncode != 0:
                raise SystemExit(f"split_plan.py failed (exit {result.returncode})")
            steps["split"] = "ok"

            auto_plan = json.loads((SCRIPT_DIR / f"plan_{args.version}.auto.json").read_text())
            review_plan = json.loads((SCRIPT_DIR / f"plan_{args.version}.review.json").read_text())
            counts["tier0_rca_auto"] = sum(1 for a in auto_plan["actions"] if a.get("set_pm_rca_type"))
            counts["tier0_link_auto"] = sum(1 for a in auto_plan["actions"] if a.get("link_to_feature"))
            counts["review_tier"] = len(review_plan["actions"])
            counts["low_confidence"] = sum(1 for b in analysis["bugs"] if b["confidence"] == "low")

            print(f"\n=== 5/9 dry-run auto tier ===")
            auto_plan_path = SCRIPT_DIR / f"plan_{args.version}.auto.json"
            result = run(
                [sys.executable, "write_back.py", "--plan", str(auto_plan_path), "--dry-run",
                 "--base-url", base_url],
                "write_back.py --dry-run",
            )
            if result.returncode != 0:
                raise SystemExit(f"write_back.py --dry-run failed (exit {result.returncode})")
            steps["dry_run"] = "ok"

            if args.dry_run_only:
                print("\n--dry-run-only set — stopping before any apply.")
            else:
                print(f"\n=== 6/9 canary apply (first 5) ===")
                canary_summary_path = SCRIPT_DIR / f"plan_{args.version}.canary_summary.json"
                result = run(
                    [sys.executable, "write_back.py", "--plan", str(auto_plan_path), "--limit", "5",
                     "--base-url", base_url, "--summary-out", str(canary_summary_path)],
                    "write_back.py --limit 5",
                )
                if result.returncode != 0:
                    raise SystemExit(f"write_back.py canary failed (exit {result.returncode})")
                canary_summary = json.loads(canary_summary_path.read_text())
                if canary_summary.get("failed", 0):
                    steps["apply_auto"] = "aborted: canary had failures"
                    raise SystemExit(
                        f"canary apply had {canary_summary['failed']} failure(s) — aborting full apply. "
                        "See the run log above."
                    )

                print(f"\n=== 7/9 full apply (idempotent) ===")
                apply_summary_path = SCRIPT_DIR / f"plan_{args.version}.apply_summary.json"
                result = run(
                    [sys.executable, "write_back.py", "--plan", str(auto_plan_path),
                     "--base-url", base_url, "--summary-out", str(apply_summary_path)],
                    "write_back.py full apply",
                )
                if result.returncode != 0:
                    raise SystemExit(f"write_back.py full apply failed (exit {result.returncode})")
                apply_result = json.loads(apply_summary_path.read_text())
                steps["apply_auto"] = "ok"

                print(f"\n=== 8/9 verify against Jira ===")
                verify_result = verify_auto_tier(auto_plan, base_url)
                steps["verify"] = "ok" if verify_result["mismatches"] == 0 else "mismatches found"
                if verify_result["mismatches"]:
                    print(
                        f"  WARNING: {verify_result['mismatches']} mismatch(es) between the plan and "
                        "what's actually in Jira — see NEEDS_REVIEW for details.",
                        file=sys.stderr,
                    )

            print(f"\n=== 9/9 write NEEDS_REVIEW ===")
            needs_review_cmd = [sys.executable, "needs_review.py", "--version", args.version]
            if apply_result:
                apply_result_path = SCRIPT_DIR / f"plan_{args.version}.apply_summary.json"
                needs_review_cmd += ["--apply-result", str(apply_result_path)]
            result = run(needs_review_cmd, "needs_review.py")
            if result.returncode != 0:
                raise SystemExit(f"needs_review.py failed (exit {result.returncode})")
            steps["needs_review"] = "ok"

            exit_code = 0

        except SystemExit as e:
            print(f"\nPIPELINE FAILED: {e}", file=sys.stderr)
            exit_code = 1
        except Exception:
            print("\nPIPELINE FAILED (unexpected exception):", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1

        entry = {
            "run_id": rid,
            "version": args.version,
            "prev_versions": prev_versions,
            "started_at": rid.split("-", 1)[-1],
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": steps,
            "counts": counts,
            "apply_result": apply_result,
            "verify_result": verify_result,
            "log_path": str(logger.path.relative_to(SCRIPT_DIR.parent)),
            "analysis_path": f"scripts/analysis_{args.version}.json",
            "plan_auto_path": f"scripts/plan_{args.version}.auto.json",
            "plan_review_path": f"scripts/plan_{args.version}.review.json",
            "needs_review_path": f"scripts/NEEDS_REVIEW_{args.version}.md",
            "exit_code": exit_code,
        }
        append_ledger(entry)

        print(f"\n=== SUMMARY: {args.version} ===")
        for k, v in steps.items():
            print(f"  {k}: {v}")
        print(f"  ledger: {logger.path.parent.parent / 'runs.jsonl'}")

    return exit_code


def verify_auto_tier(auto_plan: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Re-fetch every tier-0 bug and assert the RCA value / link actually landed.
    Reuses write_back.py's own get_issue/discover_field_id — never trust the
    apply step's own counters (SKILL.md §5b step 4: 'trust the re-fetch')."""
    load_dotenv(SCRIPT_DIR)
    token = resolve_token(SCRIPT_DIR)
    field_id = write_back.discover_field_id(base_url, token, write_back.PM_RCA_TYPE_FIELD_NAME)
    mismatches = []
    for a in auto_plan["actions"]:
        bug_key = a["bug_key"]
        issue = write_back.get_issue(base_url, token, bug_key, extra_fields=[field_id] if field_id else [])
        if not issue:
            mismatches.append({"bug_key": bug_key, "reason": "could not re-fetch"})
            continue
        expected_rca = a.get("set_pm_rca_type")
        if expected_rca and field_id:
            actual_rca = write_back.existing_pm_rca_value(issue, field_id)
            if actual_rca != expected_rca:
                mismatches.append({"bug_key": bug_key, "reason": f"RCA expected {expected_rca!r}, got {actual_rca!r}"})
        expected_link = a.get("link_to_feature")
        if expected_link and not write_back.has_existing_link(issue, expected_link):
            mismatches.append({"bug_key": bug_key, "reason": f"link to {expected_link} missing"})
    return {"mismatches": len(mismatches), "details": mismatches}


if __name__ == "__main__":
    sys.exit(main())

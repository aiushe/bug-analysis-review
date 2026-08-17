---
name: bug-analysis-review
description: Run a release bug review for any product tracked in Jira (Server/Data Center). Classifies each bug with the PM RCA Type field (Incomplete requirements / Requirement–expectation mismatch / Missed review by PM/QE / Feature gap / Invalid defect), drafts PM Analysis text, links each bug to its feature in the feature project, groups unlinked bugs by platform component, surfaces missed edge cases, and identifies where the process bottleneck is. Triggers on phrases like "bug review", "review the bugs for release X", "analyze bugs from <version>", "where are bugs being missed", "set PM RCA Type", "link bugs to features", "PRD gaps from bugs". Inputs are bugs_<version>.json and features_<version>.json from scripts/pull_*.py. Outputs an analysis doc, xlsx rollup, and a write-back plan that updates JIRA (PM RCA Type + feature links). Part 2 (optional) produces a pipeline-attribution report: maps each bug to its owner (PM/DEV/QE), states the exact PRD section that went wrong + the fix, and applies each improvement onto the next release's planned features. Triggers for Part 2: "where in the pipeline did we mess up", "attribute bugs to PM/DEV/QE", "what should the PRD have caught", "apply these lessons to the next release".
---

# Release Bug Review

Turns a JSON dump of a release's bugs from Jira into a review that answers three questions:

1. **Where was the bug missed?** Recorded in the **PM RCA Type** field (`customfield_19330`).
2. **Which feature does it belong to?** Bugs are filed against *components*, not features, so the skill matches each bug to a feature in the **feature project** (`JIRA_FEATURE_PROJECT`), gets the PM to confirm, and writes a link. Bugs with no feature owner are grouped by **platform component** instead.
3. **What edge cases / PRD gaps recur?** Patterns the PRD process should catch upfront next time.

Phases:
1. **Analyze** — read JSON → classify → match features → aggregate → deliverables.
2. **Write back** — set PM RCA Type + link bug→feature in Jira; opt-in, dry-run first, verified after.
3. **Pipeline attribution & forward improvements** (Part 2, optional) — attribute each bug to the pipeline owner (PM/DEV/QE), extract the **exact** PRD gap + fix, and map each improvement onto the *next* release's planned features. See "Part 2" below.

## What is generic vs configured

This skill works for **any product, component, and version** — those are inputs. Everything Jira-instance-specific is configuration, set once in `scripts/.env` (or exported in the shell) and then constant across runs:

| Setting | Env var | Example / default |
|---|---|---|
| Jira site | `JIRA_BASE_URL` | `https://jira.example.com` |
| Personal Access Token | `JIRA_PAT` | (secret) |
| Feature project | `JIRA_FEATURE_PROJECT` | e.g. `PROJ` — required, no default |
| Feature issue type | `JIRA_FEATURE_ISSUETYPES` | default `New Feature Request` |
| Bug component filter | `JIRA_COMPONENTS` | unset = every component on the release |
| Bug→feature link type | `JIRA_LINK_TYPE` | default `Requirement Link` |
| PM RCA Type field label | `JIRA_RCA_FIELD_NAME` | default `PM RCA Type` (ID looked up at runtime) |
| PM Analysis field label | `JIRA_ANALYSIS_FIELD_NAME` | default `PM Analysis` |
| PM RCA Type values (writable, **exact**) | — | `Incomplete requirements` · `Requirement–expectation mismatch` (EN-DASH `–`, not hyphen) · `Missed review by PM/QE` · `Feature gap` · `Invalid defect` |
| Version format | — | whatever your Jira uses, e.g. 4-part `1.2.3.0` (not `1.2.3`) |

Custom field IDs differ per instance. `write_back.py` looks the RCA and Analysis fields up **by display name**, so only the labels above need to match. Where a numeric ID is still referenced (the plan's `meta.field`, default `customfield_19330`), override it with `JIRA_RCA_FIELD_ID`.

> **Who fixed the bug ≠ the `assignee`.** In many Jira setups the bug **`assignee` is QE** (they report/verify), so an engineering-workload breakdown built on `assignee` is wrong. Check whether your instance has a "Last Resolved By"-style field (a user object; read its `displayName`) and use it for any "who worked on the bugs" view. Feature ownership is the feature issue's `assignee`.

**Variable per run:** the release version, and the component list (`--components`, default `$JIRA_COMPONENTS`, unset = all).

> **No "None" option exists in the field.** "None" is a rubric *category* (a clean Dev/QE defect with no PM miss). It is **not writable** — there is no None value in the dropdown. For a "None" bug, either leave PM RCA Type blank (skip) or, if the PM wants a value, have them reclassify it (often `Missed review by PM/QE`). Never invent a None value.

## When to trigger

"do a bug review on <version>", "where are bugs being missed for <version>", "analyze the bugs from the release", "find PRD gaps from these bugs", "review bugs against features", "set PM RCA Type / link bugs to features". A named version is the strongest signal.

## Environment note (Jira reachability)

A self-hosted Jira is often only reachable from inside the corporate network, so a cloud sandbox can't reach it — there, the user runs `pull_*.py` / `write_back.py` on their own machine and the skill only consumes JSON. **When this skill runs in Claude Code on a machine that can reach the site, you can do it all yourself** — check with a quick `curl -s -o /dev/null -w "%{http_code}" "$JIRA_BASE_URL"`. If reachable and `JIRA_PAT` is available (env or `scripts/.env`), run the pulls and write-back end-to-end. If not reachable, fall back to asking the user to run the scripts.

## Workflow

### 1. Get the bugs JSON and the feature pool

Need `bugs_<version>.json` (`scripts/pull_bugs.py`) and `features_<version>.json` (`scripts/pull_features.py`).

**Pull the previous release's features too.** A release's bugs are very often defects against features delivered in an *earlier* release, not the current one. Pull `features_<version>.json` for the release under review **and** `features_<prev>.json` for the prior release(s), and treat the union as the candidate pool. (In practice this is where the majority of links land.)

States, handled in order:
1. **Both attached / present** → use directly.
2. **Missing + Jira reachable + token present** → run the pull(s) yourself.
3. **Missing + Jira not reachable** → ask the user to run the missing script on a machine that can reach it.
4. **No version named** → ask for it first.

Confirm each JSON loads and `meta.total` is sane. If bugs `total == 0`, re-check the **version string (4-part)** and **component names** before going further — wrong component names return HTTP 400 (`value does not exist for the field 'component'`), not an empty set. If features `total == 0`, check the feature project and issue type (`JIRA_FEATURE_PROJECT` / `JIRA_FEATURE_ISSUETYPES`).

### 2. Classify each bug (decoupled from feature linking)

Assign exactly one **PM RCA Type** per bug using `reference/classification_rubric.md` — read it before the first classification. **Keep classification separate from feature linking** — do the RCA pass first, link features second. They are independent judgments and conflating them produces worse calls on both.

Output per bug: `pm_rca_type`, `pm_analysis` (2–4 polished sentences for the PM Analysis field), `confidence` (high/medium/low), `edge_case` (short label). `feature_key` is filled in step 3.

Evidence, in priority order: Root Cause / RCA custom fields → resolution + comments → summary + description → labels.

> **Resolution is NOT ground truth.** Engineering's resolution and root-cause comments often encode an *assumption* that turns out wrong (e.g. "Cannot Reproduce", "Works as Designed", "the spec covered this"). Use them as evidence, not verdict. Surface RCA for the PM to review rather than auto-trusting the resolution.

> **The spec-dependent calls are the PM's, not yours.** `Incomplete requirements` vs `None/Dev` vs `Missed review` hinges on *what the PRD actually said* — which you usually cannot see (see step 3 on PRDs). State your call as a draft and let the PM confirm; they own the PRD and are ground truth here.

If a `custom_fields` value for PM RCA Type or PM Analysis is already populated, preserve it and mark "pre-classified".

### 3. Match each bug to a feature — by reading PRDs, not titles

Features are the configured feature issue type in the **feature project** (`JIRA_FEATURE_PROJECT`). For each bug pick the best-matching feature key from the candidate pool (current + prior releases).

**Read the actual PRD (the feature's `description`), don't title-match.** Title overlap is misleading. Two recurring traps:
- **Link-only / empty PRDs.** Many feature descriptions are just links to an external roadmap or wiki tool (not reachable from here), or are the blank template with "NA" in every section. A high word count does **not** mean a real spec — check for actual content. If the PRD is link-only or an empty template, the match is **unverifiable** — say so and lower confidence; don't pretend coverage.
- **Narrow scope / explicit non-goals.** A feature may explicitly scope *out* the area a bug touches (read Goals / Non-Goals / "unchanged from today"). If so, the bug belongs to the *pre-existing* capability, not this feature — leave it unlinked.

Matching priority: (1) an existing `linked_issues` feature key in the pool → accept; (2) PRD content genuinely covers the bug's area; (3) otherwise leave unlinked.

**Record which basis a match used** — `feature_match_basis: "linked_issues"` for priority (1), `"prd_content"` for priority (2), `null` if unlinked. This isn't just documentation: `scripts/run_review.py`'s auto-apply tier is gated on this field being exactly `"linked_issues"`, because that's a fact already present in the bug's own JIRA data, never an LLM's inference from reading a PRD.

**Go bug-by-bug with the PM for anything non-obvious.** Present candidates with the **feature title** and a one-line "why", in small batches, and let them map/correct. The PM's mapping wins.

**Unlinked bugs get categorized by platform component**, not force-fit to a feature. Group them (e.g. Auth/session, Analytics, Workflow builder UI, Greeting, Answer rendering, …) and report the buckets. This is a real finding: bugs concentrated in components with no net-new feature are regressions/gaps in the existing platform.

All bug→feature mappings are confirmed by the PM before any write-back.

### 4. Aggregate findings

- **PM RCA Type breakdown** (overall and per feature).
- **Bottleneck callout**: the RCA Type with the plurality of bugs. If none dominates, say so.
- **Cross-release feature attribution**: how many bugs trace to current-release features vs prior-release features vs no-feature/component-only. (Often the headline.)
- **Top features by bug count** and **top platform components by bug count** (the unlinked buckets).
- **Top missed edge cases** (clusters of `edge_case`).
- **PRD lessons** (3–7, plain product language) and **QE lessons** (3–5).

### 5. Deliverables

Offer the doc and xlsx. If your org has a branded-docx skill (and an `xlsx` skill), use them. **If they are not installed in this session, say so and fall back** to python-docx / openpyxl (plain, unbranded) — don't silently skip.

**A. Analysis doc** — exec summary; miss-type breakdown; feature-by-feature; component buckets (unlinked); top edge cases; PRD lessons; QE lessons; appendix bug table. Low-confidence bugs listed up top for review.

**B. Xlsx rollup** — `Bugs` (key, summary, feature_key+name or component_area, pm_rca_type, pm_analysis, confidence, edge_case, priority, status, url); `By feature`; `By component`; `By edge case`; `Write-back plan`; `Meta`.

### 5b. Write back to JIRA (opt-in)

**Normal entry point: `scripts/run_review.py --version <v> --prev-versions <prev1,prev2>`.** One command chains pull → headless classify (`claude -p`) → `build_deliverables.py` (doc/xlsx/plan) → `split_plan.py` (auto vs. review tier) → dry-run → canary → full apply of the **auto tier only** → verify → `NEEDS_REVIEW_<version>.json`. Auto tier = PM RCA Type where classification confidence is `high`, and feature links only where `feature_match_basis == "linked_issues"` (an existing JIRA link, not an LLM's PRD read) — everything else (medium/low confidence, PRD-content feature matches, PM Analysis text) lands in `plan_<version>.review.json` for a human to work through via the manual ladder below. `write_back.py` itself refuses a `tier: "review"` plan unless `--allow-review-tier` is passed, and `run_review.py` never passes that flag — so applying the review tier always requires a human, by design, not just by convention. `--dry-run-only` runs the whole pipeline read-only (no apply) — use it as a smoke test.

Generate `plan_<version>.json` and ask before writing. Default is no. In scope (only these two — PM Analysis is drafted for review but **not** auto-written):
- **Set PM RCA Type** (`customfield_19330`) to the classified value.
- **Link bug → feature** via the configured link type (default `Requirement Link`).

Plan schema:
```json
{
  "meta": { "version": "1.2.3.0", "field": "customfield_19330" },
  "link_type": "Requirement Link",
  "actions": [
    { "bug_key": "PROJ-93888", "set_pm_rca_type": "Requirement–expectation mismatch",
      "link_to_feature": "PROJ-93467", "component_area": null, "skip_reason": null },
    { "bug_key": "PROJ-93324", "set_pm_rca_type": null,
      "link_to_feature": null, "component_area": "Reranker / model infra",
      "skip_reason": "None (Dev) — no PM RCA option" }
  ]
}
```

Manual apply sequence (enforced by `scripts/write_back.py`) — this is what `run_review.py` runs automatically for the **auto tier**, and what a human runs by hand for the **review tier** (add `--allow-review-tier` — the script refuses a `tier: "review"` plan without it):
1. **`--dry-run`** — prints every change, writes nothing. Confirm with the PM.
2. **`--limit 5`** canary — apply the first 5, have the PM eyeball them live in Jira.
3. **Full apply** — the script skips RCA already set (never overwrites) and links that already exist (no duplicates), so a full run after a canary is safe and idempotent.
4. **Verify against Jira** — re-fetch every bug and assert PM RCA Type matches the plan and each feature link exists exactly once (0 missing, 0 duplicates). The script's summary counter is optimistic; trust the re-fetch, not the counter.

PM Analysis (free text) writes need `--allow-pm-analysis` on top of the above — off by default, since that field is drafted for review, never auto-written, and this is enforced in `write_back.py` itself regardless of caller.

Common write-back failures and fixes:
- `link type 'X' not found` → the script lists the link types your site actually has; set `JIRA_LINK_TYPE` (or the plan's `link_type`) to one of them. Not every Jira has "Relates".
- `invalid RCA value` → the value string didn't match the dropdown exactly. The mismatch value uses an **en-dash** (`Requirement–expectation mismatch`).
- HTTP 400 on pull `component`/`issuetype` → wrong component name or feature issue type; see the constants table.

### 5c. Other Jira write utilities

Two helper scripts reuse `pull_bugs.py`'s auth/HTTP machinery (`JIRA_PAT` from env or `scripts/.env`, retrying `http_get`). Both are **idempotent** and support `--dry-run` / `--limit N` — always dry-run first, canary with `--limit`, then full.

- **`scripts/add_label.py`** — add a label to every issue matching a JQL. Appends via the REST `update` verb (`{"add": ...}`) so existing labels are never overwritten; skips issues that already have the label.
  ```
  python3 add_label.py --jql '<jql>' --label Must_Have_1.2.4 --dry-run
  python3 add_label.py --jql '<jql>' --label Must_Have_1.2.4 --limit 5
  python3 add_label.py --jql '<jql>' --label Must_Have_1.2.4
  ```

- **`scripts/create_subtasks.py`** — create sub-tasks under a parent issue from a JSON spec (`{meta, subtasks}`, e.g. `subtasks_<PARENT>.json`). One `POST /issue` per entry. Skips any whose summary already exists on the parent, so re-running after a partial apply is safe.
  ```
  python3 create_subtasks.py --spec subtasks_PROJ-93631.json --dry-run
  python3 create_subtasks.py --spec subtasks_PROJ-93631.json --limit 1   # canary
  python3 create_subtasks.py --spec subtasks_PROJ-93631.json
  ```
  Spec `meta`: `parent`, `project`, `issue_type_id` (+ `issue_type_name` for display), `components`, `affects_versions`, and `required_fields` (a map of `customfield_NNNNN → value` merged into every sub-task). Each `subtasks[]` entry has `summary`, `description`, optional `ref`, and an optional `fields` override.
  > **Sub-task type & mandatory fields are project-specific.** The sub-task issue type under a **Bug** parent is usually different from the one under a feature parent — check your project's scheme for the right type id. If `createmeta` is disabled on your site (it returns "Issue Does Not Exist"), discover the required fields the cheap way: run a `--limit 1` canary, read the HTTP 400 `errors` map (e.g. *Regression Type / Reporting Mode / Severity / Affects Version/s required*), and copy valid values from a recent sibling sub-task. Multi-select custom fields (Regression Type, Customer References) take an **array** of `{"value": ...}`; single-selects take a bare object — a `"data was not an array"` 400 means wrap it.

### 6. Confidence handling

Low-confidence classifications go in a "Needs PM review" list at the top of the doc. If >25% of bugs are low confidence, surface it as a finding (the tickets lack RCA evidence — the process has a documentation gap).

## Part 2 — Pipeline attribution & forward improvements

A follow-on report that answers "**where in the delivery pipeline did we mess up, and how do we stop it recurring next release?**" Trigger phrases: "where in the pipeline did we mess up", "attribute bugs to PM/DEV/QE", "what should the PRD have caught", "apply these lessons to the next release". Runs off the **PM-confirmed** PM RCA Type values (Part 1 output), not your drafts.

### 1. Attribute each bug to a pipeline owner

Fixed mapping (PM RCA Type → owner → lens):

| PM RCA Type | Owner | Lens for the writeup |
|---|---|---|
| Incomplete requirements | **PM** | what scenario the PRD left unspecified + the future improvement |
| Feature gap | **PM** | what whole capability/scenario the PRD missed + the future improvement |
| Requirement–expectation mismatch | **DEV** | dev built other than intended / was unclear → the PRD improvement that removes the ambiguity |
| Missed review by PM/QE | **QE** | what was testable and missed → the missing PRD acceptance criterion that would have generated the test |
| Invalid defect | **NA** | none |

(None/Dev and excluded bugs are NA.) Every owner — including DEV and QE — resolves to a **PRD improvement**, because the common root is underspecification: dev diverges and QE has nothing to test against when behavior isn't pinned down.

### 2. Improvements must be EXACT — cite the PRD location

This is the core requirement and what makes the report useful. A generic recommendation ("add error states") is not acceptable. For every improvement, **read the actual feature PRD** (from the features JSON `description`, current and prior releases) and state:

> *[Feature key] → "[exact section heading]":* [what that section says or omits today] → [the precise clause/matrix/AC to add].

Examples of the required specificity:
- *PROJ-86969 → there is no parity section at all:* the title names the Pydantic pipeline but the body only covers limits + auth → add a Haystack↔Pydantic parity matrix.
- *PROJ-93467 → "Functional Overview → tab" + "Storage model":* lists fields shown but not the display date format → add the platform date-format token.
- *PROJ-93462 → "Security and privacy behavior"/NFR-2:* masks the payload to Genesys but not the in-conversation escalation messages → rewrite as a channel list.

**Be honest when the PRD was right.** If the spec actually covered it and dev/QE deviated (e.g. copy was specified verbatim), say "**build-to-spec deviation, not a PRD gap**" and the fix is process (link the spec'd value in the ticket for QE to diff), not a PRD change. Don't manufacture a PRD gap to fit the owner.

### 3. Cluster by theme, not per bug

Group each owner's bugs into recurring **themes** (e.g. escalation & session-continuity, Pydantic↔Haystack parity, platform-UI fidelity, greeting×prechat state, cache/propagation, edge-state & error-path). Each theme gets: the bugs in it, the missed scenario, and one exact PRD improvement. A theme that recurs across PM **and** DEV **and** QE is the headline (escalation/session-continuity was that this round). Keep a full per-bug appendix table for the audit trail.

### 4. Forward application — map improvements onto the next release's planned features

Pull the **next planned release's** features (`pull_features.py --version <next>`, e.g. when reviewing 1.2.3, pull `1.2.4.0`). For each improvement theme, find the planned feature(s) most at risk of repeating the same miss and state **which clause to add to that feature's PRD now**. Output a table: `planned feature | improvement to apply (theme) | why it's at risk`. Then list the **cross-cutting PRD-process changes** (state truth tables, parity matrix, platform-token clause, channel/continuity matrices, cache-propagation AC, expanded QE matrix) that apply regardless of feature.

### 5. Deliverable

A process report (markdown + Word doc). If no branded-docx skill is available, render with **python-docx** via a markdown→docx converter (`scripts/make_report_docx.py` is a reusable one: headings, shaded/zebra tables, inline bold/italic/code, and hyperlinked bug keys). Sections: exec summary with the owner split + headline; PM / DEV / QE / NA sections by theme with exact PRD improvements; forward-application table to the next release; cross-cutting process changes; per-bug appendix.

## Plain product language

The doc and PRD lessons avoid engineering jargon. Not "race condition in the session handler" → "two customers acting at the same moment could collide in one session." PMs rewrite PRDs from this; the language must match how PMs and stakeholders read.

## Reusability

Same flow, any Jira, any release, any product. Required input: the version. Optional: `--components`, `--project`/`--issuetypes` for features. Everything in the **settings table** (site URL, token, feature project + issue type, link type, RCA field labels and values) is configured once per Jira instance in `scripts/.env` and then stays fixed.

# PM RCA Type rubric

These are the categories for the **PM RCA Type** field in Jira (`customfield_19330` in the reference instance; the ID is looked up by field name at write time). Classify every bug as exactly one. This rubric is the source of truth for the skill.

> **Writable values (exact strings):** `Incomplete requirements`, `Requirement–expectation mismatch` (EN-DASH `–`, not a hyphen), `Missed review by PM/QE`, `Feature gap`, `Invalid defect`. **There is no "None" option in the field.** "None" below is a real category (clean Dev/QE defect, no PM miss) but it is **not writable** — for a "None" bug, leave the field blank, or have the PM reclassify it (commonly `Missed review by PM/QE`) if they want a value. Never invent a None value when writing back.

## Values, in plain language

### None
Nothing from a PM lens went wrong. The PRD covered this case, the requirements were clear, and review caught what review could reasonably be expected to catch. The defect is purely Dev or QE.

**Pick this when:** the PRD/spec explicitly covered the scenario, behavior matches spec for every reasonable interpretation, and the bug is a clean implementation defect or test escape.

### Incomplete requirements
The PRD didn't fully specify the behavior for this case. The case isn't "missing" entirely (that's Feature gap) — it's that *within a feature the PRD covered*, this specific path, edge case, error state, or input condition wasn't pinned down, so Dev made a reasonable interpretation that turned out wrong.

**Pick this when:** the bug is about an unspecified edge case, error path, boundary condition, empty/null state, concurrency case, locale, accessibility detail, or upgrade path that the PRD should have called out but didn't.

### Requirement-expectation mismatch
The PRD said one thing, but stakeholders or users expected another. The implementation matches the spec — the spec itself was misaligned with reality.

**Pick this when:** Dev/QE built exactly what was written, and the bug exists because what was written was the wrong target. Often surfaces as "this works as specified but the customer/AE/CSM expected X."

### Missed review by PM/QE
The PRD or test plan would have caught this if review had been thorough. PM or QE had the artifact in hand to catch it but didn't.

**Pick this when:** there's a documented PRD/test-plan version where the gap was visible (e.g. PRD didn't mention error states at all and that section wasn't questioned in review), or a known-similar prior bug should have triggered a checklist item.

### Feature gap
The PRD missed an entire capability that the feature needed. Not an edge case — a whole missing piece.

**Pick this when:** addressing the bug requires designing/specifying a new sub-feature, not refining one already in scope. Example: PRD covered sign-up but not sign-up resumption after browser refresh.

### Invalid defect
It's not actually a bug. Either working as designed, user error, environment issue, or a duplicate.

**Pick this when:** the resolution is "Won't Fix", "Not a Bug", "Works as Designed", "Duplicate", or the comments make clear the reporter misunderstood.

## How to choose between close calls

- **Incomplete requirements vs Feature gap** — does the missing thing belong inside an existing PRD section (Incomplete) or does it need a new section (Feature gap)?
- **Incomplete requirements vs Missed review by PM/QE** — was the gap *invisible from the PRD as written* (Incomplete) or *visible if anyone had looked* (Missed review)?
- **Requirement-expectation mismatch vs Incomplete requirements** — did the PRD specify the behavior that ended up wrong (Mismatch) or fail to specify it at all (Incomplete)?
- **None vs Invalid defect** — None means it's a real bug with a non-PM root cause; Invalid means it isn't a bug at all.

## Evidence sources, in priority order

1. **Resolution + comments** — if reporter or assignee already wrote what went wrong, that overrides text inference.
2. **PM Analysis field** (if already filled in) — use as ground truth for that bug.
3. **Linked issues** to a Story/Epic — tells you which PRD was in play, and you can spot-check whether the case was covered.
4. **Summary + description** — what the bug actually describes.
5. **Labels** — tokens like `rca-dev`, `qe-gap`, `prd-miss`, `wad` are dispositive.

## Confidence

- **High** — explicit RCA in comments, or one of the dispositive labels, or PM Analysis already filled in.
- **Medium** — strong inference from summary/description, no contradictory comments.
- **Low** — vague summary, no description, no comments. Flag for PM review.

If >25% of bugs land at **Low**, surface that as a finding: it means the JIRA tickets aren't carrying enough RCA evidence and the process itself has a documentation gap.

## Edge case label

In addition to PM RCA Type, every bug gets a short `edge_case` label — a 3-6 word noun phrase naming the missed scenario. This is what feeds the "Top missed edge cases" section of the analysis doc and the patterns the PM uses to improve their PRDs.

Good labels:
- "non-ASCII characters in customer name"
- "concurrent session timeout"
- "agent transfer during typing indicator"
- "browser refresh mid-signup"

Bad labels:
- "edge case" (too vague)
- "bug in transfer logic" (describes the bug, not the missed scenario)

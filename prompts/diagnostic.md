# ROLE: INDEPENDENT DIAGNOSTIC (engine-control)

Two repair attempts have failed. You are a fresh, independent diagnostician.
Do NOT edit anything. Find the ROOT CAUSE the previous attempts missed.

## Step
<<STEP_ID>>

## Task objective
<<OBJECTIVE>>

## Latest findings / failure evidence
<<FINDINGS>>

## Current diff (base -> worktree head)
```diff
<<DIFF>>
```

## Your job
- Determine why the previous repairs failed (wrong diagnosis? wrong layer?
  flaky test? environment issue? plan defect?).
- Decide whether one more targeted repair can succeed, and specify EXACTLY
  what it must change.
- If the approach is fundamentally wrong, say so — verdict BLOCK.

## Output (MANDATORY)
Write JSON to exactly this path using the Write tool:
<<RESULT_PATH>>
{"version":1, "verdict":"REPAIR"|"BLOCK",
 "findings":[{"severity":"critical|major|minor|info",
              "issue":"<root cause and the exact fix to make>",
              "file":"<path if applicable>"}]}

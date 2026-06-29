---
name: standalone-review
description: Run an independent read-only Codex code review of a pull request or local development branch, with no autonomous-development run state, accepted spec/plan, or recorded verification required.
disable-model-invocation: true
effort: high
allowed-tools: Read Grep Glob Bash(git *) Bash(gh *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion Edit Write
---

# Standalone code review

Review an arbitrary PR or development branch without the full autonomous workflow.

1. Identify what to review from `$ARGUMENTS`:
   - A PR number (e.g. `#142` or `142`) -> use `--pr`.
   - A base ref (e.g. `main`, `origin/develop`) -> use `--base`.
   - Free-text intent -> pass via `--context`.
   - Nothing -> the script auto-detects the baseline (fork point with the default
     branch, else `HEAD~1`).

2. Run (review the current checkout against the auto-detected baseline):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/standalone_review.py" --kind code
```

   For a specific PR or base ref:

```bash
# Reviews the PR's branch against its base; --pr runs `gh pr checkout` (mutates the worktree).
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/standalone_review.py" --kind code --pr 142
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/standalone_review.py" --kind code --base main --context "$ARGUMENTS"
```

3. Read the printed review JSON (its path is shown as `Output:`). Summarize the
   verdict and findings by severity with exact file evidence.
4. This skill is read-only: it does not edit product files and keeps no run
   state. For the spec/plan/verification-gated review inside a managed run, use
   `/autonomous-development:codex-review` instead.

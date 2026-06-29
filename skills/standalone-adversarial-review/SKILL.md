---
name: standalone-adversarial-review
description: Run an independent read-only Codex adversarial review (authorization, persistence, migration, concurrency, retries, data-loss, privacy, external-service risks) of a pull request or local development branch, with no run state, accepted spec/plan, or recorded verification required.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Bash(git *) Bash(gh *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion Edit Write
---

# Standalone adversarial review

Challenge the design of an arbitrary PR or development branch for high-risk
failure modes, without the full autonomous workflow.

1. Identify what to review from `$ARGUMENTS`:
   - A PR number -> use `--pr`.
   - A base ref -> use `--base`.
   - Free-text intent -> pass via `--context`.
   - Nothing -> the script auto-detects the baseline.

2. Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/standalone_review.py" --kind adversarial
```

   For a specific PR or base ref:

```bash
# --pr runs `gh pr checkout` (mutates the worktree).
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/standalone_review.py" --kind adversarial --pr 142
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/standalone_review.py" --kind adversarial --base main --context "$ARGUMENTS"
```

   Use `--kind both` to run the ordinary code review and the adversarial review
   together.

3. Read the printed review JSON (its path is shown as `Output:`). Distinguish
   concrete, evidence-backed failure scenarios from speculation and identify the
   smallest mitigations.
4. This skill is read-only and keeps no run state. For the gated adversarial
   review inside a managed run, use `/autonomous-development:adversarial-review`.

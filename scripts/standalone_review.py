#!/usr/bin/env python3
"""Standalone, run-state-free Codex review of a PR or development branch.

Reviews the current Git worktree relative to a baseline ref using the same
read-only Codex invocation, output schemas, and reasoning profiles as the
autonomous workflow's review phases -- but with no accepted spec/plan,
recorded verification, or run state required. Intended for reviewing an
arbitrary pull request or local development branch on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller import (  # noqa: E402  (path injected above)
    PLUGIN_ROOT,
    WorkflowError,
    codex_profile_args,
    render,
    resolve_phase_profile,
    run_process,
)
from schema_validation import (  # noqa: E402
    SchemaValidationError,
    validate_payload,
)

# Git's well-known empty-tree object: a usable baseline for an unborn branch or
# a repository's very first commit, where HEAD~1 does not exist.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Each review kind reuses an existing workflow phase profile (reasoning effort,
# model overrides) and output schema so standalone output is byte-compatible
# with the workflow's review artifacts.
KINDS: dict[str, dict[str, str]] = {
    "code": {
        "phase": "review",
        "prompt": "prompts/standalone-code-review.md",
        "schema": "schemas/review.schema.json",
    },
    "adversarial": {
        "phase": "adversarial",
        "prompt": "prompts/standalone-adversarial-review.md",
        "schema": "schemas/adversarial-review.schema.json",
    },
}

# Cap the orientation diff summary embedded in the prompt; Codex inspects the
# repository directly, so this is a map, not the territory.
MAX_CHANGED_FILES_CHARS = 20000


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = run_process(["git", *args], cwd=root)
    if check and result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def resolve_repo_root(start: str | None) -> Path:
    base = Path(start).resolve() if start else Path.cwd()
    result = run_process(["git", "rev-parse", "--show-toplevel"], cwd=base)
    if result.returncode != 0:
        raise WorkflowError(f"Not inside a Git repository: {base}")
    return Path(result.stdout.strip())


def _resolve_commit(root: Path, ref: str) -> str:
    """Resolve ``ref`` (or ``origin/<ref>``) to a commit sha, or "" if unknown."""
    for candidate in (ref, f"origin/{ref}"):
        sha = _git(
            root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}",
            check=False,
        )
        if sha:
            return sha
    return ""


def detect_baseline(root: Path) -> str:
    """Best-effort baseline when none is supplied.

    Prefer the merge-base with the default branch (the fork point of the
    current branch), then the previous commit, then the empty tree.
    """
    candidates: list[str] = []
    head = _git(
        root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False
    )
    if head:
        candidates.append(head.replace("refs/remotes/", "", 1))
    candidates += ["origin/main", "origin/master", "main", "master"]

    head_sha = _git(root, "rev-parse", "HEAD", check=False)
    seen: set[str] = set()
    for ref in candidates:
        sha = _resolve_commit(root, ref)
        if not sha or sha in seen:
            continue
        seen.add(sha)
        merge_base = _git(root, "merge-base", "HEAD", sha, check=False)
        if merge_base and merge_base != head_sha:
            return merge_base

    parent = _git(root, "rev-parse", "--verify", "--quiet", "HEAD~1", check=False)
    return parent or EMPTY_TREE


def resolve_baseline(root: Path, base_arg: str | None) -> str:
    """Resolve the baseline commit to review against.

    An explicit ``base_arg`` is resolved to its merge-base with HEAD so the
    review covers exactly the commits this branch introduces (and behaves
    correctly whether the ref is the upstream tip or an already-merged
    ancestor).
    """
    if not base_arg:
        return detect_baseline(root)
    sha = _resolve_commit(root, base_arg)
    if not sha:
        raise WorkflowError(f"Cannot resolve baseline ref: {base_arg!r}")
    merge_base = _git(root, "merge-base", "HEAD", sha, check=False)
    return merge_base or sha


def changed_files_summary(root: Path, baseline: str) -> str:
    """Diff summary (vs the working tree) embedded in the prompt for orientation."""
    name_status = _git(root, "diff", "--name-status", baseline, check=False)
    stat = _git(root, "diff", "--stat", baseline, check=False)
    untracked = _git(
        root, "ls-files", "--others", "--exclude-standard", check=False
    )
    sections: list[str] = []
    if name_status:
        sections.append("Tracked changes (name-status vs baseline):\n" + name_status)
    if stat:
        sections.append("Diffstat:\n" + stat)
    if untracked:
        sections.append("Untracked files:\n" + untracked)
    text = "\n\n".join(sections).strip() or "(no differences detected vs baseline)"
    if len(text) > MAX_CHANGED_FILES_CHARS:
        text = text[:MAX_CHANGED_FILES_CHARS] + "\n... (truncated)"
    return text


def checkout_pr(root: Path, number: int) -> str:
    """Check out a GitHub PR via ``gh`` and return its base branch name."""
    view = run_process(
        ["gh", "pr", "view", str(number), "--json", "baseRefName", "-q", ".baseRefName"],
        cwd=root,
    )
    if view.returncode != 0:
        raise WorkflowError(
            f"gh pr view {number} failed: {view.stderr.strip() or 'unknown error'}"
        )
    base_branch = view.stdout.strip()
    checkout = run_process(["gh", "pr", "checkout", str(number)], cwd=root)
    if checkout.returncode != 0:
        raise WorkflowError(
            f"gh pr checkout {number} failed: "
            f"{checkout.stderr.strip() or 'unknown error'}"
        )
    return base_branch


# ---------------------------------------------------------------------------
# Review execution
# ---------------------------------------------------------------------------


def run_review(
    kind: str,
    root: Path,
    baseline: str,
    context: str | None,
    output_dir: Path,
    timeout: float | None,
) -> dict[str, Any]:
    spec = KINDS[kind]
    template = (PLUGIN_ROOT / spec["prompt"]).read_text(encoding="utf-8")
    prompt = render(
        template,
        {
            "FEATURE": context or "(not provided)",
            "BASELINE": baseline,
            "CHANGED_FILES": changed_files_summary(root, baseline),
        },
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / f"standalone-{kind}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    out_path = output_dir / f"standalone-{kind}-review.codex.json"

    profile = resolve_phase_profile(spec["phase"])
    command = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(PLUGIN_ROOT / spec["schema"]),
        "--output-last-message",
        str(out_path),
        *codex_profile_args(profile),
        "-",
    ]
    result = run_process(command, cwd=root, input_text=prompt, timeout=timeout)
    if result.returncode != 0:
        raise WorkflowError(
            f"Codex {kind} review failed (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no stderr'}"
        )
    if not out_path.exists():
        raise WorkflowError(
            f"Codex produced no output file for the {kind} review. "
            f"stderr: {result.stderr.strip()[:500] or '(empty)'}"
        )

    raw = out_path.read_text(encoding="utf-8").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowError(
            f"Codex {kind} review output is not valid JSON ({exc}); "
            f"raw output kept at {out_path}"
        ) from exc

    schema_ok = True
    try:
        validate_payload(payload, spec["schema"], label=f"standalone {kind} review")
    except SchemaValidationError as exc:
        # Keep the file and surface the payload anyway; a schema-divergent review
        # is still useful to a human, but flag it loudly.
        schema_ok = False
        print(f"WARNING: {exc}", file=sys.stderr)

    return {
        "kind": kind,
        "path": str(out_path),
        "prompt_path": str(prompt_path),
        "schema_valid": schema_ok,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------


def _severity_order(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(items, key=lambda x: rank.get(str(x.get("severity")), 99))


def summarize(entry: dict[str, Any]) -> str:
    payload = entry["payload"]
    kind = entry["kind"]
    lines = [
        f"## Standalone {kind} review",
        f"Verdict: {payload.get('verdict', '?')}  "
        f"(confidence {payload.get('confidence', '?')})",
        f"Output: {entry['path']}",
    ]
    if not entry.get("schema_valid", True):
        lines.append("NOTE: output did not validate against the schema (see warning).")
    summary = payload.get("summary")
    if summary:
        lines += ["", summary]

    if kind == "code":
        findings = payload.get("findings", []) or []
        counts: dict[str, int] = {}
        for finding in findings:
            sev = str(finding.get("severity", "?"))
            counts[sev] = counts.get(sev, 0) + 1
        tally = ", ".join(f"{n} {sev}" for sev, n in counts.items()) or "none"
        lines += ["", f"Findings: {tally}"]
        for finding in _severity_order(findings):
            loc = finding.get("file") or "(no file)"
            if finding.get("line_start"):
                loc = f"{loc}:{finding['line_start']}"
            lines.append(
                f"- [{finding.get('severity', '?')}/{finding.get('category', '?')}] "
                f"{loc} ({finding.get('id', '?')}): {finding.get('description', '')}"
            )
        gaps = payload.get("verification_gaps") or []
        if gaps:
            lines += ["", "Verification gaps:"] + [f"- {g}" for g in gaps]
    else:
        threats = payload.get("threats", []) or []
        lines += ["", f"Threats: {len(threats)}"]
        for threat in _severity_order(threats):
            lines.append(
                f"- [{threat.get('severity', '?')}/{threat.get('area', '?')}] "
                f"{threat.get('scenario', '')}"
            )
        actions = payload.get("required_actions") or []
        if actions:
            lines += ["", "Required actions:"] + [f"- {a}" for a in actions]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a standalone read-only Codex review of a pull request or "
            "development branch. Requires no autonomous-development run state, "
            "accepted spec/plan, or recorded verification."
        )
    )
    parser.add_argument(
        "--kind",
        choices=["code", "adversarial", "both"],
        default="code",
        help="Which review(s) to run (default: code).",
    )
    parser.add_argument(
        "--base",
        help=(
            "Baseline ref to review against. Default: the fork point with the "
            "detected default branch, else HEAD~1, else the empty tree."
        ),
    )
    parser.add_argument(
        "--pr",
        type=int,
        help=(
            "GitHub PR number to check out via `gh` and review against its base "
            "branch. NOTE: this mutates the working tree (runs `gh pr checkout`)."
        ),
    )
    parser.add_argument(
        "--project-root",
        help="Path inside the target Git repository (default: current directory).",
    )
    parser.add_argument(
        "--context",
        help="Optional free-text description of the change's intent.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory for the prompt and review JSON (default: a fresh temp dir).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-review Codex timeout in seconds (default: controller default).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw review JSON to stdout instead of a text summary.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        root = resolve_repo_root(args.project_root)
        base_arg = args.base
        if args.pr is not None:
            if base_arg:
                raise WorkflowError("Pass either --pr or --base, not both.")
            base_arg = checkout_pr(root, args.pr)
        baseline = resolve_baseline(root, base_arg)

        if args.output_dir:
            output_dir = Path(args.output_dir).resolve()
        else:
            output_dir = Path(
                tempfile.mkdtemp(prefix="codex-standalone-review-")
            )

        kinds = ["code", "adversarial"] if args.kind == "both" else [args.kind]
        entries = [
            run_review(kind, root, baseline, args.context, output_dir, args.timeout)
            for kind in kinds
        ]
    except (WorkflowError, SchemaValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "repository": str(root),
                    "baseline": baseline,
                    "output_dir": str(output_dir),
                    "reviews": [
                        {
                            "kind": e["kind"],
                            "path": e["path"],
                            "schema_valid": e["schema_valid"],
                            "review": e["payload"],
                        }
                        for e in entries
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"Repository: {root}")
        print(f"Baseline:   {baseline}")
        print(f"Output dir: {output_dir}\n")
        print("\n\n".join(summarize(e) for e in entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

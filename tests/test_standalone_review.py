from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import standalone_review as sr  # noqa: E402


def _run(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "t@example.com")
    _run(root, "config", "user.name", "Tester")
    (root / "a.py").write_text("print('one')\n", encoding="utf-8")
    _run(root, "add", "a.py")
    _run(root, "commit", "-q", "-m", "initial")


class BaselineTests(unittest.TestCase):
    def test_detects_repo_root_and_rejects_non_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            self.assertEqual(sr.resolve_repo_root(str(root)).resolve(), root.resolve())

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(sr.WorkflowError):
                sr.resolve_repo_root(tmp)

    def test_baseline_falls_back_to_parent_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            parent = sr._git(root, "rev-parse", "HEAD")
            (root / "b.py").write_text("print('two')\n", encoding="utf-8")
            _run(root, "add", "b.py")
            _run(root, "commit", "-q", "-m", "second")
            # No remote/main branch exists, so detection falls back to HEAD~1.
            self.assertEqual(sr.resolve_baseline(root, None), parent)

    def test_explicit_base_resolves_to_merge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            fork = sr._git(root, "rev-parse", "HEAD")
            orig_branch = sr._git(root, "rev-parse", "--abbrev-ref", "HEAD")
            _run(root, "checkout", "-q", "-b", "feature")
            (root / "c.py").write_text("print('three')\n", encoding="utf-8")
            _run(root, "add", "c.py")
            _run(root, "commit", "-q", "-m", "feature work")
            # Baseline against the original branch should be the fork point.
            self.assertEqual(sr.resolve_baseline(root, orig_branch), fork)

    def test_unresolvable_base_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            with self.assertRaises(sr.WorkflowError):
                sr.resolve_baseline(root, "no-such-ref")

    def test_changed_files_summary_lists_tracked_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            baseline = sr._git(root, "rev-parse", "HEAD")
            (root / "a.py").write_text("print('changed')\n", encoding="utf-8")
            (root / "new.py").write_text("print('new')\n", encoding="utf-8")
            summary = sr.changed_files_summary(root, baseline)
            self.assertIn("a.py", summary)
            self.assertIn("new.py", summary)
            self.assertIn("Untracked files", summary)


class RunReviewTests(unittest.TestCase):
    def test_run_review_invokes_codex_and_validates(self) -> None:
        real_run_process = sr.run_process
        captured: dict[str, object] = {}

        valid_payload = {
            "verdict": "pass",
            "summary": "Looks fine.",
            "findings": [],
            "verification_gaps": [],
            "acceptance_criteria_assessment": [],
            "confidence": 0.9,
        }

        def fake_run_process(args, *, cwd, input_text=None, check=False, timeout=None):
            if args and args[0] == "codex":
                captured["command"] = args
                captured["prompt"] = input_text
                # Honor the real Codex contract: write the last message to the
                # path following --output-last-message.
                out = Path(args[args.index("--output-last-message") + 1])
                out.write_text(json.dumps(valid_payload), encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run_process(
                args, cwd=cwd, input_text=input_text, check=check, timeout=timeout
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            baseline = sr._git(root, "rev-parse", "HEAD")
            out_dir = root / "out"
            sr.run_process = fake_run_process
            try:
                entry = sr.run_review(
                    "code", root, baseline, "intent here", out_dir, None
                )
            finally:
                sr.run_process = real_run_process

            self.assertTrue(entry["schema_valid"])
            self.assertEqual(entry["payload"]["verdict"], "pass")
            self.assertTrue(Path(entry["path"]).exists())
            # The read-only sandbox flags and the chosen schema must be present.
            command = captured["command"]
            self.assertIn("read-only", command)
            self.assertIn("schemas/review.schema.json", str(command))
            # Context is rendered into the prompt.
            self.assertIn("intent here", captured["prompt"])
            self.assertIn(baseline, captured["prompt"])


if __name__ == "__main__":
    unittest.main()

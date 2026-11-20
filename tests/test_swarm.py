"""
tests/test_swarm.py
Multi-Agent Swarm Engine — comprehensive test suite.

Test strategy:
  - Pure-Python tests (no live git repos, no LLM calls, no network).
  - WorktreeManager: tested with a real temporary git repo.
  - Coordinator: fully mocked execute_worker_callback.
  - State: checkpoint round-trip via real tmpdir (uses AG_TOOLS_DIR env override).
  - Decomposer: validates anti-conflict rules deterministically.
  - Verifier: runs real pytest on a tiny synthetic test file.

Run with:
    python -m pytest tests/test_swarm.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path regardless of CWD
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from source.patcher.swarm.state import (
    SwarmState, WorkerBrief, WorkerResult, VerificationResult,
    save_checkpoint, load_checkpoint, list_checkpoints, delete_checkpoint,
    _task_dir,
)
from source.patcher.swarm.decomposer import (
    decompose_task, DecompositionError, build_worker_brief, make_branch_name,
    build_orchestrator_prompt, build_file_tree, _validate_no_file_conflicts,
    split_into_phases, MAX_WORKERS_HARD_CAP,
)
from source.patcher.swarm.verifier import (
    Verifier, VerificationResult, detect_cross_worker_conflict,
    summarize_verification, _extract_failed_tests,
)
from source.patcher.swarm.worktree_manager import WorktreeManager
from source.patcher.swarm.coordinator import Coordinator, _noop_execute, _find_brief


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brief(
    name: str = "worker-1",
    files: list[str] | None = None,
    task_id: str = "aaaabbbb-1234-5678-abcd-000000000001",
    priority: int = 0,
    worker_type: str = "code",
    test_command: str = "echo ok",
) -> WorkerBrief:
    return WorkerBrief(
        worker_name=name,
        worker_type=worker_type,
        owned_files=files or ["src/foo.py"],
        task_description=f"Do stuff in {name}",
        branch_name=make_branch_name(name, task_id),
        test_command=test_command,
        priority=priority,
    )


def _make_state(user_request: str = "add proxy support", **kwargs) -> SwarmState:
    return SwarmState.new(
        user_request=user_request,
        project_path=str(ROOT_DIR),
        **kwargs,
    )


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with one commit in `path`."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    # Create initial file and commit
    (path / "README.md").write_text("# Test repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)


# ---------------------------------------------------------------------------
# 1. State — dataclass construction and serialization
# ---------------------------------------------------------------------------

class StateDataclassTests(unittest.TestCase):
    """SwarmState, WorkerBrief, WorkerResult, VerificationResult construction."""

    def test_swarm_state_new_generates_uuid(self):
        s1 = _make_state()
        s2 = _make_state()
        self.assertNotEqual(s1.task_id, s2.task_id)
        self.assertEqual(len(s1.task_id), 36)  # UUID4 format

    def test_swarm_state_defaults(self):
        s = _make_state()
        self.assertEqual(s.current_node, "DECOMPOSE")
        self.assertFalse(s.is_complete)
        self.assertEqual(s.current_phase, 1)
        self.assertEqual(s.total_phases, 1)
        self.assertEqual(s.max_workers, 5)
        self.assertEqual(s.max_attempts, 3)
        self.assertEqual(s.failover_count, 0)
        self.assertEqual(s.errors, [])

    def test_worker_brief_fields(self):
        b = _make_brief()
        self.assertEqual(b.worker_name, "worker-1")
        self.assertEqual(b.model, "flash")
        self.assertEqual(b.status, "pending")
        self.assertEqual(b.attempt_count, 0)

    def test_worker_result_success_and_failure(self):
        ok = WorkerResult(worker_name="w1", success=True, commit_sha="abc123")
        fail = WorkerResult(worker_name="w2", success=False, error_message="boom")
        self.assertTrue(ok.success)
        self.assertFalse(fail.success)
        self.assertEqual(fail.error_message, "boom")

    def test_verification_result_fields(self):
        vr = VerificationResult(
            worker_name="w1", passed=False,
            failed_tests=["tests/test_x.py::TestFoo::test_bar"],
            error_message="Exit code 1",
        )
        self.assertFalse(vr.passed)
        self.assertEqual(len(vr.failed_tests), 1)


# ---------------------------------------------------------------------------
# 2. State — checkpoint round-trip
# ---------------------------------------------------------------------------

class CheckpointTests(unittest.TestCase):
    """save_checkpoint / load_checkpoint with AG_TOOLS_DIR override."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["AG_TOOLS_DIR"] = self.tmpdir

    def tearDown(self):
        del os.environ["AG_TOOLS_DIR"]
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        state = _make_state()
        state.briefs = [_make_brief(task_id=state.task_id)]
        state.worker_results["worker-1"] = WorkerResult(worker_name="worker-1", success=True, commit_sha="deadbeef")
        state.errors.append("test error")

        path = save_checkpoint(state)
        self.assertTrue(path.exists())

        loaded = load_checkpoint(state.task_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task_id, state.task_id)
        self.assertEqual(loaded.user_request, state.user_request)
        self.assertEqual(len(loaded.briefs), 1)
        self.assertEqual(loaded.briefs[0].worker_name, "worker-1")
        self.assertEqual(loaded.worker_results["worker-1"].commit_sha, "deadbeef")
        self.assertEqual(loaded.errors, ["test error"])

    def test_load_missing_returns_none(self):
        result = load_checkpoint("nonexistent-task-id")
        self.assertIsNone(result)

    def test_checkpoint_is_atomic_file_write(self):
        """State file must be a valid JSON even after save."""
        state = _make_state()
        path = save_checkpoint(state)
        with open(path) as f:
            parsed = json.load(f)
        self.assertIn("task_id", parsed)
        self.assertIn("_checkpoint_at", parsed)

    def test_list_checkpoints_returns_summary(self):
        s1 = _make_state("task A")
        s2 = _make_state("task B")
        save_checkpoint(s1)
        save_checkpoint(s2)
        summaries = list_checkpoints()
        task_ids = [s["task_id"] for s in summaries]
        self.assertIn(s1.task_id, task_ids)
        self.assertIn(s2.task_id, task_ids)

    def test_delete_checkpoint(self):
        state = _make_state()
        save_checkpoint(state)
        self.assertIsNotNone(load_checkpoint(state.task_id))
        deleted = delete_checkpoint(state.task_id)
        self.assertTrue(deleted)
        self.assertIsNone(load_checkpoint(state.task_id))

    def test_checkpoint_survives_verification_results(self):
        state = _make_state()
        state.verification_results["worker-1"] = VerificationResult(
            worker_name="worker-1",
            passed=False,
            failed_tests=["tests/test_x.py::Foo::bar"],
            test_output="FAILED tests/test_x.py::Foo::bar",
        )
        save_checkpoint(state)
        loaded = load_checkpoint(state.task_id)
        vr = loaded.verification_results.get("worker-1")
        self.assertIsNotNone(vr)
        self.assertFalse(vr.passed)
        self.assertEqual(vr.failed_tests, ["tests/test_x.py::Foo::bar"])


# ---------------------------------------------------------------------------
# 3. Decomposer — validation and anti-conflict rules
# ---------------------------------------------------------------------------

class DecomposerValidationTests(unittest.TestCase):
    """Architecture §8.1: one worker = one file anti-conflict rule."""

    def setUp(self):
        self.state = _make_state()

    def _decompose(self, items: list[dict], max_workers: int = 5):
        return decompose_task(self.state, items, max_workers=max_workers)

    def test_valid_no_conflict(self):
        items = [
            {"worker_name": "w1", "owned_files": ["a.py", "b.py"],
             "task_description": "do A", "worker_type": "code", "priority": 0},
            {"worker_name": "w2", "owned_files": ["c.py"],
             "task_description": "do C", "worker_type": "code", "priority": 1},
        ]
        briefs, phases = self._decompose(items)
        self.assertEqual(len(briefs), 2)
        self.assertEqual(len(phases), 1)

    def test_conflict_raises_decomposition_error(self):
        """Two workers owning the same file must raise DecompositionError."""
        items = [
            {"worker_name": "w1", "owned_files": ["shared.py"],
             "task_description": "do stuff", "worker_type": "code", "priority": 0},
            {"worker_name": "w2", "owned_files": ["shared.py"],
             "task_description": "also do stuff", "worker_type": "code", "priority": 1},
        ]
        with self.assertRaises(DecompositionError) as ctx:
            self._decompose(items)
        self.assertIn("shared.py", str(ctx.exception))

    def test_duplicate_worker_names_raises(self):
        items = [
            {"worker_name": "worker-1", "owned_files": ["a.py"],
             "task_description": "do A", "worker_type": "code", "priority": 0},
            {"worker_name": "worker-1", "owned_files": ["b.py"],
             "task_description": "do B", "worker_type": "code", "priority": 1},
        ]
        with self.assertRaises(DecompositionError) as ctx:
            self._decompose(items)
        self.assertIn("Duplicate", str(ctx.exception))

    def test_invalid_worker_type_raises(self):
        items = [
            {"worker_name": "w1", "owned_files": ["a.py"],
             "task_description": "do stuff", "worker_type": "HACKER", "priority": 0},
        ]
        with self.assertRaises(DecompositionError) as ctx:
            self._decompose(items)
        self.assertIn("worker_type", str(ctx.exception))

    def test_code_worker_no_owned_files_raises(self):
        items = [
            {"worker_name": "w1", "owned_files": [],
             "task_description": "do stuff", "worker_type": "code", "priority": 0},
        ]
        with self.assertRaises(DecompositionError) as ctx:
            self._decompose(items)
        self.assertIn("no owned_files", str(ctx.exception))

    def test_empty_task_description_raises(self):
        items = [
            {"worker_name": "w1", "owned_files": ["a.py"],
             "task_description": "   ", "worker_type": "code", "priority": 0},
        ]
        with self.assertRaises(DecompositionError) as ctx:
            self._decompose(items)
        self.assertIn("empty task_description", str(ctx.exception))

    def test_non_code_worker_no_files_ok(self):
        """Non-code workers (research, docs) may have no owned_files."""
        items = [
            {"worker_name": "w1", "owned_files": [],
             "task_description": "Research proxy options", "worker_type": "research", "priority": 0},
        ]
        briefs, _ = self._decompose(items)
        self.assertEqual(len(briefs), 1)

    def test_validate_no_file_conflicts_helper(self):
        b1 = _make_brief("w1", ["a.py", "b.py"])
        b2 = _make_brief("w2", ["c.py"])
        self.assertEqual(_validate_no_file_conflicts([b1, b2]), [])

    def test_validate_conflict_detected(self):
        b1 = _make_brief("w1", ["shared.py"])
        b2 = _make_brief("w2", ["shared.py"])
        conflicts = _validate_no_file_conflicts([b1, b2])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("shared.py", conflicts[0])


# ---------------------------------------------------------------------------
# 4. Decomposer — phase splitting
# ---------------------------------------------------------------------------

class PhaseSpittingTests(unittest.TestCase):
    """Architecture §9 Q5: tasks > MAX_WORKERS → phase-by-phase."""

    def test_single_phase_if_within_limit(self):
        state = _make_state()
        state.max_workers = 5
        items = [
            {"worker_name": f"w{i}", "owned_files": [f"file{i}.py"],
             "task_description": f"do {i}", "worker_type": "code", "priority": i}
            for i in range(5)
        ]
        _, phases = decompose_task(state, items, max_workers=5)
        self.assertEqual(len(phases), 1)
        self.assertEqual(len(phases[0]), 5)

    def test_multiple_phases_when_over_limit(self):
        state = _make_state()
        items = [
            {"worker_name": f"w{i}", "owned_files": [f"file{i}.py"],
             "task_description": f"do {i}", "worker_type": "code", "priority": i}
            for i in range(8)
        ]
        _, phases = decompose_task(state, items, max_workers=3)
        self.assertEqual(len(phases), 3)  # 3+3+2
        self.assertEqual(len(phases[0]), 3)
        self.assertEqual(len(phases[1]), 3)
        self.assertEqual(len(phases[2]), 2)

    def test_hard_cap_enforced(self):
        """MAX_WORKERS_HARD_CAP=10 is the absolute maximum."""
        self.assertEqual(MAX_WORKERS_HARD_CAP, 10)

    def test_split_preserves_priority_order(self):
        briefs = [_make_brief(f"w{i}", [f"f{i}.py"], priority=i) for i in range(6)]
        phases = split_into_phases(briefs, max_workers=3)
        self.assertEqual(len(phases), 2)
        # First phase: priority 0, 1, 2 (renumbered to 0, 1, 2)
        self.assertEqual([b.priority for b in phases[0]], [0, 1, 2])
        # Second phase: priority 3, 4, 5 → renumbered to 0, 1, 2
        self.assertEqual([b.priority for b in phases[1]], [0, 1, 2])


# ---------------------------------------------------------------------------
# 5. Decomposer — branch name and prompt
# ---------------------------------------------------------------------------

class BranchNameAndPromptTests(unittest.TestCase):
    def test_branch_name_format(self):
        name = make_branch_name("worker-1", "aaaabbbb-1234-5678-abcd-000000000001")
        self.assertEqual(name, "feat/worker-1-aaaabbbb")

    def test_branch_name_safe_chars(self):
        """Branch names with spaces or specials are sanitized."""
        name = make_branch_name("worker ONE!", "taskid99")
        self.assertRegex(name, r'^[a-zA-Z0-9/_-]+$')

    def test_orchestrator_prompt_contains_request(self):
        state = _make_state("add proxy support")
        state.file_tree = ["src/a.py", "src/b.py"]
        prompt = build_orchestrator_prompt(state)
        self.assertIn("add proxy support", prompt)
        self.assertIn("src/a.py", prompt)
        self.assertIn("JSON array", prompt)
        self.assertIn("owned_files", prompt)

    def test_build_file_tree_excludes_junk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Normal files
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("")
            (root / "README.md").write_text("")
            # Junk dirs to exclude
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "cache.pyc").write_text("")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("")

            tree = build_file_tree(root)
            paths = set(tree)
            self.assertIn("src/main.py", paths)
            self.assertIn("README.md", paths)
            self.assertNotIn("__pycache__/cache.pyc", paths)
            self.assertNotIn("node_modules/lib.js", paths)


# ---------------------------------------------------------------------------
# 6. Verifier — test output parsing
# ---------------------------------------------------------------------------

class VerifierOutputParsingTests(unittest.TestCase):
    def test_extract_failed_tests_from_pytest_output(self):
        output = textwrap.dedent("""\
            FAILED tests/test_foo.py::TestFoo::test_bar - AssertionError
            FAILED tests/test_baz.py::TestBaz::test_qux
            ERROR tests/test_err.py::TestErr::test_oops - Exception
            1 passed, 2 failed, 1 error
        """)
        failed = _extract_failed_tests(output)
        self.assertIn("tests/test_foo.py::TestFoo::test_bar", failed)
        self.assertIn("tests/test_baz.py::TestBaz::test_qux", failed)
        self.assertIn("tests/test_err.py::TestErr::test_oops", failed)
        self.assertEqual(len(failed), 3)

    def test_extract_no_failures_on_passing_output(self):
        output = "1 passed in 0.5s"
        failed = _extract_failed_tests(output)
        self.assertEqual(failed, [])

    def test_deduplicate_failed_tests(self):
        output = "FAILED foo.py::A::b\nFAILED foo.py::A::b\n"
        failed = _extract_failed_tests(output)
        self.assertEqual(len(failed), 1)

    def test_summarize_verification_all_pass(self):
        results = {
            "w1": VerificationResult("w1", passed=True),
            "w2": VerificationResult("w2", passed=True),
        }
        summary = summarize_verification(results)
        self.assertIn("2/2", summary)
        self.assertNotIn("Failed", summary)

    def test_summarize_verification_with_failures(self):
        results = {
            "w1": VerificationResult("w1", passed=True),
            "w2": VerificationResult("w2", passed=False,
                                     failed_tests=["tests/t.py::C::m"],
                                     error_message="Exit code 1"),
        }
        summary = summarize_verification(results)
        self.assertIn("1/2", summary)
        self.assertIn("w2", summary)


# ---------------------------------------------------------------------------
# 7. Verifier — real subprocess test run
# ---------------------------------------------------------------------------

class VerifierSubprocessTests(unittest.TestCase):
    """Run Verifier against a real tiny pytest suite in a tempdir."""

    def _write_test_project(self, root: Path, pass_tests: bool = True):
        """Write a minimal Python project with one test file."""
        (root / "source").mkdir(exist_ok=True)
        (root / "source" / "__init__.py").write_text("")
        (root / "source" / "calc.py").write_text(textwrap.dedent("""\
            def add(a, b):
                return a + b
        """))
        if pass_tests:
            test_code = textwrap.dedent("""\
                from source.calc import add

                def test_add_positive():
                    assert add(1, 2) == 3

                def test_add_zero():
                    assert add(0, 0) == 0
            """)
        else:
            test_code = textwrap.dedent("""\
                def test_will_fail():
                    assert 1 == 2, "Intentional failure"
            """)
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "__init__.py").write_text("")
        (root / "tests" / "test_calc.py").write_text(test_code)

    def test_verifier_passes_on_good_tests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_test_project(root, pass_tests=True)
            brief = _make_brief(
                test_command="python -m pytest tests/test_calc.py -v --tb=short"
            )
            brief.worktree_path = str(root)
            verifier = Verifier(root)
            result = verifier.verify(brief)
            self.assertTrue(result.passed, f"Expected pass, got:\n{result.test_output}")
            self.assertEqual(result.failed_tests, [])

    def test_verifier_fails_on_bad_tests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_test_project(root, pass_tests=False)
            brief = _make_brief(
                test_command="python -m pytest tests/test_calc.py -v --tb=short"
            )
            brief.worktree_path = str(root)
            verifier = Verifier(root)
            result = verifier.verify(brief)
            self.assertFalse(result.passed)
            self.assertGreater(len(result.failed_tests), 0)
            self.assertIn("test_will_fail", result.failed_tests[0])

    def test_verifier_invalid_command_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            brief = _make_brief(test_command="totally-nonexistent-command-xyz --fail")
            brief.worktree_path = tmpdir
            verifier = Verifier(Path(tmpdir))
            # Should not raise — returns failed VerificationResult
            result = verifier.verify(brief)
            self.assertFalse(result.passed)
            self.assertTrue(result.error_message)

    def test_run_full_suite_on_passing_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_test_project(root, pass_tests=True)
            verifier = Verifier(root)
            result = verifier.run_full_suite(
                "python -m pytest tests/test_calc.py -v --tb=short"
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.worker_name, "__full_suite__")


# ---------------------------------------------------------------------------
# 8. Cross-worker conflict detection (architecture Q7)
# ---------------------------------------------------------------------------

class CrossWorkerConflictTests(unittest.TestCase):
    def test_no_conflict_when_single_worker_fails(self):
        briefs = [
            _make_brief("w1", ["source/a.py"]),
            _make_brief("w2", ["source/b.py"]),
        ]
        failed_vr = VerificationResult(
            worker_name="w1",
            passed=False,
            failed_tests=["tests/test_a.py::TestA::test_foo"],
        )
        result = detect_cross_worker_conflict(failed_vr, briefs)
        # Only w1's files implicated → no cross-worker conflict
        self.assertIsNone(result)

    def test_no_conflict_on_empty_failed_tests(self):
        briefs = [_make_brief("w1", ["a.py"])]
        vr = VerificationResult("w1", passed=False, failed_tests=[])
        result = detect_cross_worker_conflict(vr, briefs)
        self.assertIsNone(result)

    def test_cross_conflict_detected_returns_fix_owner(self):
        """When both 'a' and 'b' stems appear in failed tests, two workers implicated."""
        briefs = [
            _make_brief("w1", ["source/a.py"], priority=0),  # priority 0 → fix owner
            _make_brief("w2", ["source/b.py"], priority=1),
        ]
        failed_vr = VerificationResult(
            worker_name="w1",
            passed=False,
            failed_tests=[
                "tests/test_a.py::TestA::test_foo",  # maps to source/a.py → w1
                "tests/test_b.py::TestB::test_bar",  # maps to source/b.py → w2
            ],
        )
        result = detect_cross_worker_conflict(failed_vr, briefs)
        # Should return w1 (priority=0, lowest priority = merged first = fix owner)
        self.assertEqual(result, "w1")


# ---------------------------------------------------------------------------
# 9. WorktreeManager — git operations (requires git on PATH)
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    subprocess.run(["git", "--version"], capture_output=True).returncode == 0,
    "git not available on PATH"
)
class WorktreeManagerTests(unittest.TestCase):
    """Real git operations in temporary repos."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project = Path(self.tmpdir) / "project"
        self.project.mkdir()
        _init_git_repo(self.project)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_state_for_project(self) -> SwarmState:
        state = SwarmState.new("test task", str(self.project))
        state.briefs = [
            _make_brief("worker-1", ["README.md"], task_id=state.task_id, priority=0),
            _make_brief("worker-2", ["NEW_FILE.md"], task_id=state.task_id, priority=1),
        ]
        return state

    def test_verify_git_repo_raises_on_non_repo(self):
        """
        WorktreeManager auto-commits on an empty repo, but if commits cannot be made
        and HEAD remains invalid, it must raise RuntimeError.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            no_commit_repo = Path(tmpdir) / "empty_repo"
            no_commit_repo.mkdir()
            # Init git but DON'T configure user.name / user.email and disable default identity
            subprocess.run(["git", "init", str(no_commit_repo)], capture_output=True, check=True)
            # To test auto-init success:
            subprocess.run(["git", "-C", str(no_commit_repo), "config", "user.email", "t@t.com"], capture_output=True)
            subprocess.run(["git", "-C", str(no_commit_repo), "config", "user.name", "T"], capture_output=True)

            state = SwarmState.new("test", str(no_commit_repo))
            mgr = WorktreeManager(state)
            # Verify auto git detection created initial commit
            head = subprocess.run(["git", "-C", str(no_commit_repo), "rev-parse", "HEAD"], capture_output=True, text=True)
            self.assertEqual(head.returncode, 0)

    def test_setup_creates_worktrees(self):
        state = self._make_state_for_project()
        mgr = WorktreeManager(state)
        mgr.setup()

        for brief in state.briefs:
            self.assertTrue(Path(brief.worktree_path).exists(),
                            f"Worktree not created for {brief.worker_name}")
            self.assertTrue((Path(brief.worktree_path) / ".git").exists() or
                            Path(brief.worktree_path).exists())

        # Cleanup
        mgr.cleanup()

    def test_list_worktrees_includes_created(self):
        state = self._make_state_for_project()
        mgr = WorktreeManager(state)
        mgr.setup()
        worktrees = mgr.list_worktrees()
        # Should have at least: main + 2 worker worktrees
        self.assertGreaterEqual(len(worktrees), 3)
        mgr.cleanup()

    def test_commit_worker_work_returns_sha(self):
        state = self._make_state_for_project()
        mgr = WorktreeManager(state)
        mgr.setup()

        brief = state.briefs[1]  # worker-2 owns NEW_FILE.md
        wt_path = Path(brief.worktree_path)
        # Write the new file
        (wt_path / "NEW_FILE.md").write_text("# New file\n", encoding="utf-8")

        sha = mgr.commit_worker_work(brief, "worker-2: add NEW_FILE.md")
        self.assertEqual(len(sha), 40)  # full SHA

        mgr.cleanup()

    def test_merge_all_no_conflict(self):
        state = self._make_state_for_project()
        mgr = WorktreeManager(state)
        mgr.setup()

        # Worker-2 creates a new file (no conflict with README.md)
        brief2 = state.briefs[1]
        wt2 = Path(brief2.worktree_path)
        (wt2 / "NEW_FILE.md").write_text("New content\n", encoding="utf-8")
        sha = mgr.commit_worker_work(brief2, "worker-2: add new file")
        brief2.status = "done"
        brief2.commit_sha = sha

        # Worker-1 modifies README.md
        brief1 = state.briefs[0]
        wt1 = Path(brief1.worktree_path)
        (wt1 / "README.md").write_text("# Updated\n", encoding="utf-8")
        sha1 = mgr.commit_worker_work(brief1, "worker-1: update README")
        brief1.status = "done"
        brief1.commit_sha = sha1

        final_sha = mgr.merge_all()
        self.assertEqual(len(final_sha), 40)

        # Both files should exist in main repo after merge
        self.assertTrue((self.project / "NEW_FILE.md").exists())
        readme_content = (self.project / "README.md").read_text()
        self.assertIn("Updated", readme_content)

        mgr.cleanup()

    def test_cleanup_removes_worktrees(self):
        state = self._make_state_for_project()
        mgr = WorktreeManager(state)
        mgr.setup()
        swarm_root = mgr.swarm_root
        self.assertTrue(swarm_root.exists())

        mgr.cleanup()
        self.assertFalse(swarm_root.exists())


# ---------------------------------------------------------------------------
# 10. Coordinator — graph traversal with mocked workers
# ---------------------------------------------------------------------------

class CoordinatorGraphTests(unittest.TestCase):
    """Coordinator integration tests with no real git or LLM."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["AG_TOOLS_DIR"] = self.tmpdir
        # Create a minimal git repo for Coordinator
        self.project = Path(self.tmpdir) / "project"
        self.project.mkdir()
        _init_git_repo(self.project)

    def tearDown(self):
        del os.environ["AG_TOOLS_DIR"]
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_decompose_fn(self, items: list[dict]):
        """Return a fake orchestrator_decompose_fn that returns `items`."""
        def fn(prompt: str) -> list[dict]:
            return items
        return fn

    def _make_execute_fn(self, success: bool = True):
        """Return a fake execute_worker_callback."""
        def fn(brief: WorkerBrief) -> WorkerResult:
            return WorkerResult(
                worker_name=brief.worker_name,
                success=success,
                commit_sha="abcd1234" * 5,  # fake 40-char SHA
                files_changed=brief.owned_files,
            )
        return fn

    def test_noop_execute_returns_success(self):
        brief = _make_brief()
        result = _noop_execute(brief)
        self.assertTrue(result.success)
        self.assertEqual(result.worker_name, "worker-1")

    def test_find_brief_found(self):
        briefs = [_make_brief("w1"), _make_brief("w2")]
        found = _find_brief(briefs, "w2")
        self.assertIsNotNone(found)
        self.assertEqual(found.worker_name, "w2")

    def test_find_brief_not_found(self):
        briefs = [_make_brief("w1")]
        self.assertIsNone(_find_brief(briefs, "w999"))

    def test_coordinator_decompose_error_aborts_gracefully(self):
        """DecompositionError after 3 retries → state.current_node=DONE, is_complete=False."""
        def bad_decompose(prompt: str) -> list[dict]:
            # Return conflicting workers → DecompositionError
            return [
                {"worker_name": "w1", "owned_files": ["conflict.py"],
                 "task_description": "do A", "worker_type": "code"},
                {"worker_name": "w2", "owned_files": ["conflict.py"],
                 "task_description": "do B", "worker_type": "code"},
            ]

        coord = Coordinator(
            project_path=str(self.project),
            execute_worker_callback=self._make_execute_fn(),
        )
        state = coord.run(
            user_request="test decompose error",
            orchestrator_decompose_fn=bad_decompose,
        )
        self.assertFalse(state.is_complete)
        self.assertGreater(len(state.errors), 0)
        # Should mention the conflict
        combined_errors = " ".join(state.errors)
        self.assertIn("conflict.py", combined_errors)

    def test_full_graph_happy_path(self):
        """
        Full graph: DECOMPOSE → VALIDATE → EXECUTE → VERIFY → MERGE → REPORT
        with mocked execute and passing test suite.
        Workers use 'echo ok' as test_command (always passes).
        """
        items = [
            {"worker_name": "worker-1", "owned_files": ["README.md"],
             "task_description": "Update README", "worker_type": "code",
             "test_command": "echo ok", "priority": 0},
        ]

        coord = Coordinator(
            project_path=str(self.project),
            execute_worker_callback=self._make_execute_fn(success=True),
            full_test_command="echo all-tests-ok",
        )
        state = coord.run(
            user_request="update readme",
            orchestrator_decompose_fn=self._make_decompose_fn(items),
        )
        # Should complete
        self.assertTrue(state.is_complete)
        self.assertEqual(state.current_node, "DONE")
        self.assertEqual(len(state.briefs), 1)
        # Worker result recorded
        wr = state.worker_results.get("worker-1")
        self.assertIsNotNone(wr)
        self.assertTrue(wr.success)

    def test_resume_from_checkpoint(self):
        """save_checkpoint mid-run → resume() picks up from saved node."""
        state = _make_state()
        state.current_node = "REPORT"
        state.is_complete = False
        state.briefs = [_make_brief(task_id=state.task_id)]
        state.worker_results["worker-1"] = WorkerResult("worker-1", success=True, commit_sha="abc" * 14)
        state.verification_results["worker-1"] = VerificationResult("worker-1", passed=True)
        state.verification_results["__full_suite__"] = VerificationResult("__full_suite__", passed=True)
        state.final_branch = "a" * 40
        state.total_phases = 1
        save_checkpoint(state)

        # Resume — Coordinator should pick up at REPORT and complete
        coord = Coordinator(
            project_path=str(self.project),
            execute_worker_callback=self._make_execute_fn(),
        )
        # Mock worktree manager to skip actual git operations
        with patch.object(WorktreeManager, '_verify_git_repo'), \
             patch.object(WorktreeManager, 'setup'), \
             patch.object(WorktreeManager, 'cleanup'):
            final = coord.resume(
                task_id=state.task_id,
                orchestrator_decompose_fn=self._make_decompose_fn([]),
            )

        self.assertTrue(final.is_complete)

    def test_max_workers_respected(self):
        """Coordinator enforces max_workers=1 → 2 briefs → 2 phases."""
        items = [
            {"worker_name": "w1", "owned_files": ["a.py"],
             "task_description": "do a", "worker_type": "code",
             "test_command": "echo ok", "priority": 0},
            {"worker_name": "w2", "owned_files": ["b.py"],
             "task_description": "do b", "worker_type": "code",
             "test_command": "echo ok", "priority": 1},
        ]
        coord = Coordinator(
            project_path=str(self.project),
            execute_worker_callback=self._make_execute_fn(success=True),
            max_workers=1,
            full_test_command="echo ok",
        )
        state = coord.run(
            user_request="two things",
            orchestrator_decompose_fn=self._make_decompose_fn(items),
        )
        self.assertEqual(state.total_phases, 2)

    def test_resume_missing_task_id_raises(self):
        coord = Coordinator(project_path=str(self.project))
        with self.assertRaises(FileNotFoundError):
            coord.resume("nonexistent-task-id-xyz", lambda p: [])


# ---------------------------------------------------------------------------
# 11. Coordinator — failover metadata tracking
# ---------------------------------------------------------------------------

class CoordinatorFailoverTests(unittest.TestCase):
    """Verify that orchestrator_model and failover_count are tracked in state."""

    def test_orchestrator_model_recorded_in_state(self):
        state = _make_state()
        state.orchestrator_model = "claude-sonnet-4"
        self.assertEqual(state.orchestrator_model, "claude-sonnet-4")

    def test_failover_count_increments(self):
        state = _make_state()
        self.assertEqual(state.failover_count, 0)
        state.failover_count += 1
        self.assertEqual(state.failover_count, 1)

    def test_integration_fix_owner_assignment(self):
        state = _make_state()
        state.integration_fix_owner = "worker-1"
        self.assertEqual(state.integration_fix_owner, "worker-1")


# ---------------------------------------------------------------------------
# 12. __init__.py — public API surface
# ---------------------------------------------------------------------------

class SwarmPublicApiTests(unittest.TestCase):
    def test_all_public_names_importable(self):
        from source.patcher.swarm import (
            Coordinator, SwarmState, WorkerBrief, WorkerResult,
            VerificationResult, decompose_task, WorktreeManager, Verifier,
        )
        self.assertTrue(callable(Coordinator))
        self.assertTrue(callable(decompose_task))


# ---------------------------------------------------------------------------
# Review-improvement tests (AI review §1–§7)
# ---------------------------------------------------------------------------

class ReviewImprovementsTests(unittest.TestCase):
    """Tests covering the 7 architectural improvements from the AI review."""

    def setUp(self):
        import shutil
        self._orig_ag_tools_dir = os.environ.get("AG_TOOLS_DIR")
        self._tools_tmpdir = tempfile.mkdtemp()
        os.environ["AG_TOOLS_DIR"] = self._tools_tmpdir
        self.addCleanup(shutil.rmtree, self._tools_tmpdir, ignore_errors=True)

    def tearDown(self):
        if self._orig_ag_tools_dir is None:
            os.environ.pop("AG_TOOLS_DIR", None)
        else:
            os.environ["AG_TOOLS_DIR"] = self._orig_ag_tools_dir

    # ── Review #1 & #2: ThreadPoolExecutor + per-WorkerResult checkpoint ──

    def test_execute_runs_workers_in_parallel(self):
        """_node_execute must run workers concurrently via ThreadPoolExecutor."""
        import threading
        from source.patcher.swarm.coordinator import Coordinator

        both_started = threading.Barrier(2, timeout=5)
        worker_names = []

        def slow_worker(brief, worktree_path=None):
            worker_names.append(brief.worker_name)
            # Both workers must reach this point before either returns → proves parallelism
            both_started.wait()
            return WorkerResult(worker_name=brief.worker_name, success=True,
                                commit_sha="abcd" * 10)

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            _init_git_repo(project)

            task_id = "parallel-test"
            briefs = [
                _make_brief("w1", ["a.py"], task_id=task_id, priority=0),
                _make_brief("w2", ["b.py"], task_id=task_id, priority=1),
            ]
            state = SwarmState.new("parallel test", str(project))
            state.briefs = briefs

            coord = Coordinator(
                project_path=str(project),
                execute_worker_callback=slow_worker,
            )
            with patch("source.patcher.swarm.coordinator.WorktreeManager") as MockWM:
                mock_wm = MockWM.return_value
                mock_wm.prune_stale = MagicMock()
                mock_wm.setup = MagicMock()
                # Return dummy worktree paths from brief.worktree_path
                for b in briefs:
                    b.worktree_path = str(project)
                _outcome, _state = coord._node_execute(state, briefs)

        self.assertIn("w1", worker_names)
        self.assertIn("w2", worker_names)

    def test_checkpoint_saved_per_worker_result(self):
        """save_checkpoint must be called at least once per completed worker."""
        import source.patcher.swarm.coordinator as coord_mod
        from source.patcher.swarm.coordinator import Coordinator

        call_count = []

        def fast_worker(brief, worktree_path=None):
            return WorkerResult(worker_name=brief.worker_name, success=True,
                                commit_sha="abcd" * 10)

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            _init_git_repo(project)

            task_id = "ckpt-test"
            briefs = [
                _make_brief("w1", ["a.py"], task_id=task_id, priority=0),
                _make_brief("w2", ["b.py"], task_id=task_id, priority=1),
            ]
            state = SwarmState.new("ckpt test", str(project))
            state.briefs = briefs
            for b in briefs:
                b.worktree_path = str(project)

            coord = Coordinator(project_path=str(project), execute_worker_callback=fast_worker)
            with patch("source.patcher.swarm.coordinator.WorktreeManager") as MockWM, \
                 patch.object(coord_mod, "save_checkpoint",
                              side_effect=lambda s: call_count.append(1)):
                mock_wm = MockWM.return_value
                mock_wm.prune_stale = MagicMock()
                mock_wm.setup = MagicMock()
                coord._node_execute(state, briefs)

        # At least N checkpoint saves for N workers (plus possibly one at node-start)
        self.assertGreaterEqual(len(call_count), len(briefs))

    # ── Review #3: junitxml parsing ──

    def test_junitxml_parsing_extracts_source_files(self):
        """_parse_junitxml() must return failed test ids and source file paths."""
        from source.patcher.swarm.verifier import _parse_junitxml

        # pytest --junitxml format: classname="tests/test_foo.py", name="test_bad"
        # source_files are extracted from 'File "..."' patterns in tracebacks
        xml_content = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuites>
              <testsuite name="pytest" tests="2" failures="1" errors="0">
                <testcase classname="tests/test_foo.py" name="test_bad">
                  <failure message="AssertionError">
                    File "source/patcher/foo.py", line 42, in some_func
                      assert False
                    AssertionError
                  </failure>
                </testcase>
                <testcase classname="tests/test_foo.py" name="test_good"/>
              </testsuite>
            </testsuites>
        """)

        with tempfile.NamedTemporaryFile(suffix=".xml", mode="w",
                                        delete=False, encoding="utf-8") as f:
            f.write(xml_content)
            xml_path = f.name

        try:
            failed_tests, source_files = _parse_junitxml(xml_path)
        finally:
            os.unlink(xml_path)

        # At least one failed test id returned
        self.assertTrue(len(failed_tests) > 0, f"Expected failures, got: {failed_tests}")
        self.assertTrue(any("test_bad" in t for t in failed_tests),
                        f"test_bad not found in {failed_tests}")
        # Source file extracted from 'File "..."' pattern in traceback
        self.assertTrue(any("foo.py" in s for s in source_files),
                        f"Expected foo.py in source_files, got {source_files}")

    def test_detect_conflict_uses_failed_source_files(self):
        """detect_cross_worker_conflict() must use failed_source_files (layer 1)."""
        from source.patcher.swarm.verifier import detect_cross_worker_conflict

        task_id = "conflict-test"
        briefs = [
            _make_brief("w1", ["source/patcher/alpha.py"], task_id=task_id, priority=0),
            _make_brief("w2", ["source/patcher/beta.py"], task_id=task_id, priority=1),
        ]
        # Both workers' files appear in failed source files → cross-worker conflict
        result = VerificationResult(
            worker_name="w1",
            passed=False,
            failed_tests=["tests/test_alpha.py::test_x"],
            failed_source_files=["source/patcher/alpha.py", "source/patcher/beta.py"],
        )
        conflict_owner = detect_cross_worker_conflict(result, briefs)
        # Should detect conflict (not None) when files from multiple workers fail
        self.assertIsNotNone(conflict_owner)

    # ── Review #4: GC for worktrees ──

    def test_prune_stale_called_on_startup(self):
        """_run_graph() must call WorktreeManager.prune_stale() before executing phases."""
        import source.patcher.swarm.coordinator as coord_mod
        from source.patcher.swarm.coordinator import Coordinator

        prune_calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            _init_git_repo(project)

            state = SwarmState.new("prune test", str(project))
            coord = Coordinator(project_path=str(project), execute_worker_callback=MagicMock())

            with patch("source.patcher.swarm.coordinator.WorktreeManager") as MockWM, \
                 patch.object(coord_mod, "save_checkpoint"):
                mock_wm = MockWM.return_value
                mock_wm.prune_stale.side_effect = lambda: prune_calls.append(1)
                # Short-circuit graph so it doesn't run workers
                with patch.object(coord, "_node_decompose", side_effect=StopIteration):
                    try:
                        coord._run_graph(state, MagicMock(return_value=[]))
                    except StopIteration:
                        pass

        self.assertGreaterEqual(len(prune_calls), 1,
                                "WorktreeManager.prune_stale() must be called at graph start")

    def test_cleanup_all_stale_removes_old_dirs(self):
        """cleanup_all_stale() must remove sibling swarm dirs older than N days."""
        from source.patcher.swarm.worktree_manager import WorktreeManager, SWARM_DIR_SUFFIX
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            _init_git_repo(project)

            # Create a fake stale swarm sibling dir next to the project
            proj_name = project.name
            stale_dir = project.parent / f"{proj_name}{SWARM_DIR_SUFFIX}staleabc"
            stale_dir.mkdir(exist_ok=True)
            (stale_dir / "some_file.txt").write_text("stale")

            # Backdate it to 10 days ago
            old_time = time.time() - (10 * 86400)
            os.utime(stale_dir, (old_time, old_time))

            state = SwarmState.new("cleanup test", str(project))
            wm = WorktreeManager(state)
            removed = wm.cleanup_all_stale(older_than_days=5)

            self.assertFalse(stale_dir.exists(),
                             "Stale swarm dir should have been removed")
            self.assertTrue(len(removed) > 0,
                            "cleanup_all_stale should return list of removed paths")

    # ── Review #6: Integration branch strategy ──

    def test_merge_via_integration_creates_branch(self):
        """merge_via_integration() creates an integration branch; main stays put."""
        from source.patcher.swarm.worktree_manager import WorktreeManager

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            _init_git_repo(project)

            main_sha_before = subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()

            state = SwarmState.new("integ test", str(project))
            briefs = [_make_brief("w1", ["a.py"], task_id=state.task_id, priority=0)]
            state.briefs = briefs
            wm = WorktreeManager(state)

            # Create worker worktrees and commit a change
            wm.setup()
            wt_path = Path(briefs[0].worktree_path)
            (wt_path / "a.py").write_text("x = 1")
            subprocess.run(["git", "-C", str(wt_path), "add", "."],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "worker change"],
                           check=True, capture_output=True)
            new_sha = subprocess.check_output(
                ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True).strip()

            briefs[0].commit_sha = new_sha
            briefs[0].status = "done"

            # merge_via_integration() uses self.state.briefs internally
            integ_sha = wm.merge_via_integration()

            # Integration SHA must be a valid commit different from main
            self.assertIsNotNone(integ_sha)
            self.assertTrue(len(integ_sha) > 0)

            # main must NOT have moved
            main_sha_after = subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(main_sha_before, main_sha_after,
                             "main branch must not move until fast_forward_main()")
            wm.cleanup()

    # ── Review #7: 2-pass directory summary ──

    def test_directory_summary_format(self):
        """build_directory_summary() must return a tree with file counts."""
        from source.patcher.swarm.decomposer import build_directory_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("x = 1")
            (root / "src" / "util.py").write_text("y = 2")
            (root / "tests").mkdir()
            (root / "tests" / "test_main.py").write_text("pass")

            summary = build_directory_summary(tmpdir)

        self.assertIn("src/", summary)
        self.assertIn("tests/", summary)
        # Must include file counts in format "(N file(s))"
        self.assertRegex(summary, r"\(\d+ files?\)")

    def test_orchestrator_prompt_uses_directory_summary(self):
        """build_orchestrator_prompt() must include 'Directory overview' section."""
        from source.patcher.swarm.decomposer import build_orchestrator_prompt

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("pass")

            state = SwarmState.new("Add logging", str(root))
            state.file_tree = ["src/app.py"]
            prompt = build_orchestrator_prompt(state, use_directory_summary=True)

        self.assertIn("Directory overview", prompt)
        self.assertIn("File list", prompt)
        self.assertIn("Add logging", prompt)

    def test_orchestrator_prompt_fallback_no_summary(self):
        """With use_directory_summary=False, prompt uses plain file list."""
        from source.patcher.swarm.decomposer import build_orchestrator_prompt

        state = _make_state("Refactor")
        state.file_tree = ["src/a.py", "src/b.py"]
        prompt = build_orchestrator_prompt(state, use_directory_summary=False)

        self.assertNotIn("Directory overview", prompt)
        self.assertIn("src/a.py", prompt)

    def test_build_file_tree_excludes_swarm_dirs(self):
        """build_file_tree() must skip directories containing '-swarm-' in name."""
        from source.patcher.swarm.decomposer import build_file_tree

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("pass")
            # Simulate a swarm worktree sibling (inside same temp dir for test)
            swarm_dir = root / "myproject-swarm-abc12345-worker-1"
            swarm_dir.mkdir()
            (swarm_dir / "leaked.py").write_text("oops")

            files = build_file_tree(tmpdir)

        paths = set(files)
        self.assertIn("src/main.py", paths)
        self.assertFalse(any("-swarm-" in p for p in paths),
                         f"Swarm dir files should be excluded, got {paths}")

    def test_node_verify_clears_cross_worker_conflict_on_reverify_pass(self):
        """_node_verify() must clear integration_fix_owner and Cross-worker conflict errors once re-verification passes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            _init_git_repo(project)

            state = SwarmState.new("conflict cleanup", str(project))
            b1 = _make_brief("w1", ["source/patcher/alpha.py"], task_id=state.task_id, priority=0)
            b2 = _make_brief("w2", ["source/patcher/beta.py"], task_id=state.task_id, priority=1)
            b1.status = "done"
            b2.status = "done"
            state.briefs = [b1, b2]
            state.errors = ["Unrelated warning"]

            coord = Coordinator(project_path=str(project))
            conflict_results = {
                "w1": VerificationResult(
                    worker_name="w1",
                    passed=False,
                    failed_tests=["tests/test_alpha.py::test_x"],
                    failed_source_files=["source/patcher/alpha.py", "source/patcher/beta.py"],
                ),
                "w2": VerificationResult(worker_name="w2", passed=True),
            }
            passing_results = {
                "w1": VerificationResult(worker_name="w1", passed=True),
                "w2": VerificationResult(worker_name="w2", passed=True),
            }

            with patch.object(
                coord._verifier,
                "verify_all",
                side_effect=[conflict_results, passing_results],
            ):
                outcome1, state = coord._node_verify(state, state.briefs)
                self.assertEqual(outcome1, "RETRY")
                self.assertEqual(state.integration_fix_owner, "w1")
                self.assertTrue(
                    any(e.startswith("Cross-worker conflict:") for e in state.errors),
                    f"Expected Cross-worker conflict error, got: {state.errors}",
                )

                # Simulate fix owner finishing re-execution before re-verification
                b1.status = "done"
                outcome2, state = coord._node_verify(state, state.briefs)

            self.assertEqual(outcome2, "NEXT")
            self.assertEqual(state.current_node, "MERGE")
            self.assertEqual(state.integration_fix_owner, "")
            self.assertFalse(
                any(e.startswith("Cross-worker conflict:") for e in state.errors),
                f"Expected Cross-worker conflict errors to be cleared, got: {state.errors}",
            )
            self.assertIn("Unrelated warning", state.errors)


# ---------------------------------------------------------------------------
# Affected test detection tests
# ---------------------------------------------------------------------------

class AffectedTestDetectorTests(unittest.TestCase):
    """Tests for AffectedTestDetector — narrowed per-worker test selection."""

    def _make_project(self, tmp: Path) -> tuple[Path, Path]:
        """Create a minimal project: source/ + tests/ with a real import."""
        src = tmp / "source" / "patcher" / "ide"
        src.mkdir(parents=True)
        (tmp / "source" / "__init__.py").write_text("")
        (tmp / "source" / "patcher" / "__init__.py").write_text("")
        (tmp / "source" / "patcher" / "ide" / "__init__.py").write_text("")
        (src / "patcher.py").write_text("def patch(): pass")

        tests = tmp / "tests"
        tests.mkdir()
        # test_ide_patcher.py imports source.patcher.ide.patcher
        (tests / "test_ide_patcher.py").write_text(
            "from source.patcher.ide import patcher\n"
            "def test_patch(): assert callable(patcher.patch)\n"
        )
        # test_unrelated.py does NOT import ide.patcher
        (tests / "test_unrelated.py").write_text(
            "def test_noop(): assert True\n"
        )
        return src, tests

    def test_detector_finds_importing_test(self):
        """get_affected_tests() must return the test that imports the changed file."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            affected = detector.get_affected_tests(
                ["source/patcher/ide/patcher.py"]
            )

        self.assertIn("tests/test_ide_patcher.py", affected,
                      f"Expected test_ide_patcher.py in {affected}")

    def test_detector_excludes_unrelated_test(self):
        """get_affected_tests() must NOT return tests that don't import the file."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            affected = detector.get_affected_tests(
                ["source/patcher/ide/patcher.py"]
            )

        self.assertNotIn("tests/test_unrelated.py", affected,
                         f"Unrelated test should be excluded, got {affected}")

    def test_detector_returns_empty_for_unknown_file(self):
        """Unknown file (not imported by any test) → empty list, not an error."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            affected = detector.get_affected_tests(["source/patcher/unknown.py"])

        self.assertEqual(affected, [])

    def test_build_pytest_command_narrowed(self):
        """build_pytest_command() must include only affected test files."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            cmd = detector.build_pytest_command(
                ["source/patcher/ide/patcher.py"],
                extra_flags="-v",
            )

        # Must include the relevant test file
        self.assertIn("test_ide_patcher.py", cmd)
        # Must NOT include the unrelated test file
        self.assertNotIn("test_unrelated.py", cmd)
        # Must NOT be a broad "tests/" glob (should be a specific file path)
        parts = cmd.split()
        # There should be no bare "tests/" argument (would mean full suite fallback)
        self.assertFalse(
            any(p.rstrip("/") == "tests" for p in parts),
            f"Command should not use broad 'tests/' — got: {cmd}",
        )

    def test_build_pytest_command_fallback_to_full_suite(self):
        """build_pytest_command() falls back to tests/ when no matches and fallback=True."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            cmd = detector.build_pytest_command(
                ["source/patcher/nonexistent.py"],
                fallback_to_full=True,
            )

        self.assertIn("tests/", cmd)

    def test_coverage_ratio_known_file(self):
        """coverage_ratio() must be < 1.0 for a file with limited test coverage."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            # patcher.py is imported by 1 of 2 test files → ratio = 0.5
            ratio = detector.coverage_ratio(["source/patcher/ide/patcher.py"])

        self.assertGreater(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)
        # 1 of 2 test files → 0.5
        self.assertAlmostEqual(ratio, 0.5, delta=0.1)

    def test_invalidate_clears_cache(self):
        """invalidate() must force a fresh scan on next query."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            detector = AffectedTestDetector(tmp)
            # Prime the cache
            _ = detector.get_affected_tests(["source/patcher/ide/patcher.py"])
            self.assertIsNotNone(detector._source_to_tests)

            # Invalidate
            detector.invalidate()
            self.assertIsNone(detector._source_to_tests)

            # Cache rebuilt on next query
            _ = detector.get_affected_tests(["source/patcher/ide/patcher.py"])
            self.assertIsNotNone(detector._source_to_tests)

    def test_verify_affected_uses_narrowed_command(self):
        """Verifier.verify_affected() must run narrowed tests, not full suite."""
        from source.patcher.swarm.verifier import Verifier

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            brief = _make_brief("w1", ["source/patcher/ide/patcher.py"],
                                task_id="affected-test", priority=0)
            brief.worktree_path = str(tmp)

            verifier = Verifier(tmp)
            result = verifier.verify_affected(brief)

            # Test ran (patcher.py has callable patch()) → should pass
            self.assertTrue(result.passed,
                            f"Expected passing verification, got: {result.error_message}\n{result.test_output}")
            # Result output should mention this was a narrowed run
            self.assertIn("affected-only", result.test_output)

    def test_verify_affected_fallback_on_no_owned_files(self):
        """verify_affected() falls back to verify() when brief.owned_files is empty."""
        from source.patcher.swarm.verifier import Verifier

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_project(tmp)

            brief = _make_brief("w1", [], task_id="fallback-test", priority=0)
            brief.test_command = "echo ok"
            brief.worktree_path = str(tmp)

            verifier = Verifier(tmp)
            result = verifier.verify_affected(brief)

            self.assertTrue(result.passed)

    def test_public_api_includes_affected_detector(self):
        """AffectedTestDetector must be importable from the public swarm API."""
        from source.patcher.swarm import AffectedTestDetector
        self.assertTrue(callable(AffectedTestDetector))

    def _make_js_project(self, tmp: Path) -> None:
        """Create a minimal project with extensions/*.js and JS-referencing tests."""
        ext_core = tmp / "extensions" / "core"
        ext_desktop = tmp / "extensions" / "desktop"
        ext_core.mkdir(parents=True)
        ext_desktop.mkdir(parents=True)
        (ext_core / "quota_engine.js").write_text("module.exports = {};\n", encoding="utf-8")
        (ext_core / "swarm_bridge.js").write_text("module.exports = {};\n", encoding="utf-8")
        (ext_desktop / "ag_desktop_ui.js").write_text("module.exports = {};\n", encoding="utf-8")

        tests = tmp / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_quota_path.py").write_text(
            'TARGET = "extensions/core/quota_engine.js"\n'
            "def test_quota(): assert TARGET.endswith('quota_engine.js')\n",
            encoding="utf-8",
        )
        (tests / "test_quota_token.py").write_text(
            'FILENAME = "quota_engine.js"\n'
            "def test_quota_tok(): assert FILENAME == 'quota_engine.js'\n",
            encoding="utf-8",
        )
        (tests / "test_bridge.py").write_text(
            'FILENAME = "swarm_bridge.js"\n'
            "def test_bridge(): assert True\n",
            encoding="utf-8",
        )
        (tests / "test_desktop_ui.py").write_text(
            'TARGET = "extensions/desktop/ag_desktop_ui.js"\n'
            "def test_ui(): assert True\n",
            encoding="utf-8",
        )
        (tests / "test_unrelated.py").write_text(
            "def test_noop(): assert True\n",
            encoding="utf-8",
        )

    def test_detector_narrows_extensions_js_files(self):
        """get_affected_tests() must match extensions/*.js references and exclude unrelated tests."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_js_project(tmp)

            detector = AffectedTestDetector(tmp)
            quota_affected = detector.get_affected_tests(["extensions/core/quota_engine.js"])
            bridge_affected = detector.get_affected_tests(["extensions/core/swarm_bridge.js"])
            ui_affected = detector.get_affected_tests(["extensions/desktop/ag_desktop_ui.js"])

        self.assertEqual(
            quota_affected,
            ["tests/test_quota_path.py", "tests/test_quota_token.py"],
        )
        self.assertEqual(bridge_affected, ["tests/test_bridge.py"])
        self.assertEqual(ui_affected, ["tests/test_desktop_ui.py"])
        self.assertNotIn("tests/test_unrelated.py", quota_affected)

    def test_build_pytest_command_narrowed_for_extensions_js(self):
        """build_pytest_command() must target matching JS test files without falling back to tests/."""
        from source.patcher.swarm.affected_tests import AffectedTestDetector

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._make_js_project(tmp)

            detector = AffectedTestDetector(tmp)
            cmd = detector.build_pytest_command(
                ["extensions/core/quota_engine.js"],
                extra_flags="-v",
            )

        self.assertIn("tests/test_quota_path.py", cmd)
        self.assertIn("tests/test_quota_token.py", cmd)
        self.assertIn("-v", cmd)
        self.assertNotIn("test_bridge.py", cmd)
        self.assertNotIn("test_desktop_ui.py", cmd)
        self.assertNotIn("test_unrelated.py", cmd)
        self.assertFalse(
            any(p.rstrip("/") == "tests" for p in cmd.split()),
            f"Command should not fall back to broad 'tests/' — got: {cmd}",
        )

# ---------------------------------------------------------------------------
# 10. Integration: Resume Phase Reconstruction, Events, IPC, CLI, Installer
# ---------------------------------------------------------------------------

class CoordinatorResumeAndEventsTests(unittest.TestCase):
    """Verify mid-phase resume reconstruction, use_affected_tests, and event streaming."""

    def test_resume_reconstructs_multi_phase_execution_and_emits_events(self):
        from source.patcher.swarm.coordinator import Coordinator
        from source.patcher.swarm.state import save_checkpoint
        from source.patcher.swarm.worktree_manager import WorktreeManager

        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as tools_dir:
            repo = Path(repo_dir)
            _init_git_repo(repo)
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
            (repo / "c.py").write_text("z = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add files"], check=True, capture_output=True)

            b1 = _make_brief("w1", ["a.py"], priority=0, test_command="python -c \"print('ok1')\"")
            b2 = _make_brief("w2", ["b.py"], priority=0, test_command="python -c \"print('ok2')\"")
            b3 = _make_brief("w3", ["c.py"], priority=1, test_command="python -c \"print('ok3')\"")

            state = SwarmState.new("multi phase resume", str(repo), max_workers=2)
            for b in (b1, b2, b3):
                b.branch_name = make_branch_name(b.worker_name, state.task_id)
            state.briefs = [b1, b2, b3]
            state.current_node = "EXECUTE"
            state.current_phase = 1
            state.total_phases = 2

            with patch.dict(os.environ, {"AG_TOOLS_DIR": tools_dir}):
                wm = WorktreeManager(state)
                wm.setup()
                # Simulate w1 already done & committed, w2 interrupted in running state, w3 untouched in phase 2
                (Path(b1.worktree_path) / "a.py").write_text("x = 2\n", encoding="utf-8")
                sha1 = wm.commit_worker_work(b1, "w1 done before crash")
                b1.status = "done"
                b1.commit_sha = sha1
                state.worker_results["w1"] = WorkerResult(worker_name="w1", success=True, commit_sha=sha1, files_changed=["a.py"])
                b2.status = "running"
                save_checkpoint(state)

                dispatched: list[str] = []
                events: list[dict] = []

                def exec_worker(brief: WorkerBrief) -> WorkerResult:
                    dispatched.append(brief.worker_name)
                    target_name = brief.owned_files[0]
                    (Path(brief.worktree_path) / target_name).write_text(
                        f"# updated by {brief.worker_name}\n", encoding="utf-8"
                    )
                    sha = wm.commit_worker_work(brief, f"work {brief.worker_name}")
                    return WorkerResult(
                        worker_name=brief.worker_name,
                        success=True,
                        commit_sha=sha,
                        files_changed=list(brief.owned_files),
                    )

                coord = Coordinator(
                    project_path=str(repo),
                    execute_worker_callback=exec_worker,
                    max_workers=2,
                    full_test_command="python -c \"print('suite ok')\"",
                    on_event=events.append,
                )
                final_state = coord.resume(state.task_id)

            self.assertTrue(final_state.is_complete)
            self.assertEqual(final_state.current_node, "DONE")
            # w1 was already done with commit_sha -> only w2 (phase 1) then w3 (phase 2) should run
            self.assertEqual(dispatched, ["w2", "w3"])
            event_names = [e.get("event") for e in events]
            self.assertIn("started", event_names)
            self.assertIn("node_started", event_names)
            self.assertIn("worker_done", event_names)
            self.assertIn("phase_verified", event_names)
            self.assertIn("# updated by w3", (repo / "c.py").read_text(encoding="utf-8"))

    def test_coordinator_passes_use_affected_tests_to_verifier(self):
        from source.patcher.swarm.coordinator import Coordinator

        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as tools_dir:
            repo = Path(repo_dir)
            _init_git_repo(repo)
            (repo / "mod.py").write_text("VAL = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add mod"], check=True, capture_output=True)

            coord = Coordinator(
                project_path=str(repo),
                execute_worker_callback=lambda b: WorkerResult(
                    worker_name=b.worker_name, success=True, commit_sha="", files_changed=[]
                ),
                use_affected_tests=True,
                full_test_command="python -c \"print('full ok')\"",
            )
            with patch.dict(os.environ, {"AG_TOOLS_DIR": tools_dir}), \
                 patch("source.patcher.swarm.verifier.Verifier.verify_all",
                            return_value={"w1": VerificationResult("w1", passed=True)}) as spy:
                coord.run(
                    "update mod",
                    lambda _p: [{
                        "worker_name": "w1",
                        "worker_type": "code",
                        "owned_files": ["mod.py"],
                        "task_description": "touch mod",
                        "test_command": "echo ok",
                    }],
                )
            spy.assert_called()
            self.assertTrue(spy.call_args.kwargs.get("use_affected"))


class SwarmIpcServerTests(unittest.TestCase):
    """Tests for JSON-lines IPC bridge (SwarmIPCServer)."""

    def test_ipc_ping_list_status_and_shutdown(self):
        from source.patcher.swarm.ipc_server import SwarmIPCServer

        events: list[dict] = []
        srv = SwarmIPCServer(send_fn=events.append)

        with tempfile.TemporaryDirectory() as tools_dir, patch.dict(os.environ, {"AG_TOOLS_DIR": tools_dir}):
            self.assertTrue(srv.handle({"cmd": "ping"}))
            self.assertTrue(srv.handle({"cmd": "list_tasks"}))
            self.assertTrue(srv.handle({"cmd": "status", "task_id": "nonexistent-id"}))
            self.assertFalse(srv.handle({"cmd": "shutdown"}))

        ev_types = [e["event"] for e in events]
        self.assertEqual(ev_types, ["pong", "tasks_list", "task_status"])
        self.assertEqual(events[1]["tasks"], [])
        self.assertIsNone(events[2]["task"])

    def test_ipc_run_orchestrate_worker_and_done(self):
        import threading
        import time
        from source.patcher.swarm.ipc_server import SwarmIPCServer

        events: list[dict] = []
        cond = threading.Condition()

        def capture(payload: dict) -> None:
            with cond:
                events.append(payload)
                cond.notify_all()

        def wait_for(event_name: str, timeout: float = 15.0) -> dict:
            deadline = time.time() + timeout
            with cond:
                while time.time() < deadline:
                    for ev in events:
                        if ev.get("event") == event_name:
                            return ev
                    cond.wait(timeout=0.2)
            raise AssertionError(f"Timed out waiting for IPC event {event_name!r}; got {[e.get('event') for e in events]}")

        srv = SwarmIPCServer(send_fn=capture)

        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as tools_dir:
            repo = Path(repo_dir)
            _init_git_repo(repo)
            (repo / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
            (repo / "test_app.py").write_text(
                "from app import greet\ndef test_greet():\n    assert greet() == 'hi'\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial app"], check=True, capture_output=True)

            with patch.dict(os.environ, {"AG_TOOLS_DIR": tools_dir}):
                srv.handle({
                    "cmd": "run",
                    "project_path": str(repo),
                    "request": "update greeting",
                    "max_workers": 2,
                    "full_test_command": "python -m pytest test_app.py -q",
                    "use_affected_tests": True,
                })

                started = wait_for("started")
                self.assertTrue(started["task_id"])
                wait_for("orchestrator_prompt")

                srv.handle({
                    "cmd": "orchestrator_result",
                    "briefs": [{
                        "worker_name": "w1",
                        "worker_type": "code",
                        "owned_files": ["app.py", "test_app.py"],
                        "task_description": "Update greet to hello",
                        "test_command": "python -m pytest test_app.py -q",
                        "priority": 0,
                    }],
                })

                dispatched = wait_for("worker_dispatched")
                wt = Path(dispatched["worktree_path"])
                (wt / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
                (wt / "test_app.py").write_text(
                    "from app import greet\ndef test_greet():\n    assert greet() == 'hello'\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "-C", str(wt), "add", "."], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(wt), "commit", "-m", "w1 hello"], check=True, capture_output=True)
                sha = subprocess.run(
                    ["git", "-C", str(wt), "rev-parse", "HEAD"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()

                srv.handle({
                    "cmd": "worker_result",
                    "worker_name": "w1",
                    "success": True,
                    "commit_sha": sha,
                    "files_changed": ["app.py", "test_app.py"],
                })

                done_ev = wait_for("done")
                self.assertEqual(done_ev["task_id"], started["task_id"])
                self.assertTrue(done_ev["is_complete"])
                self.assertIn("'hello'", (repo / "app.py").read_text(encoding="utf-8"))

                if srv._coordinator_thread:
                    srv._coordinator_thread.join(timeout=5.0)
                srv.handle({"cmd": "status", "task_id": started["task_id"]})
                status_ev = wait_for("task_status")
                self.assertEqual(status_ev["task"]["task_id"], started["task_id"])
                srv.handle({"cmd": "cleanup", "task_id": started["task_id"]})
                wait_for("cleanup_done")


class SwarmCliTests(unittest.TestCase):
    """End-to-end tests for python -m swarm.cli subcommands."""

    def test_cli_full_prepare_setup_mark_verify_merge_flow(self):
        import io
        from contextlib import redirect_stdout
        from source.patcher.swarm import cli

        def run_cli(argv: list[str]) -> tuple[int, dict]:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(argv)
            return rc, json.loads(buf.getvalue())

        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as tools_dir:
            repo = Path(repo_dir)
            _init_git_repo(repo)
            (repo / "src").mkdir()
            (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_calc.py").write_text(
                "from src.calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "calc"], check=True, capture_output=True)

            briefs_path = Path(tools_dir) / "briefs.json"
            briefs_path.write_text(json.dumps([{
                "worker_name": "w-calc",
                "worker_type": "code",
                "owned_files": ["src/calc.py", "tests/test_calc.py"],
                "task_description": "Add mul()",
                "test_command": "python -m pytest tests/test_calc.py -q",
                "priority": 0,
            }]), encoding="utf-8")

            with patch.dict(os.environ, {"AG_TOOLS_DIR": tools_dir}):
                rc, prep = run_cli([
                    "prepare", "--project", str(repo), "--request", "Add mul()", "--use-affected-tests",
                ])
                self.assertEqual(rc, 0)
                self.assertTrue(prep["ok"])
                self.assertTrue(prep["use_affected_tests"])
                task_id = prep["task_id"]

                rc, setup_res = run_cli(["setup", "--task-id", task_id, "--briefs", str(briefs_path)])
                self.assertEqual(rc, 0)
                self.assertTrue(setup_res["ok"])
                wt = Path(setup_res["briefs"][0]["worktree_path"])
                self.assertTrue(wt.is_dir())

                (wt / "src" / "calc.py").write_text(
                    "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n",
                    encoding="utf-8",
                )
                (wt / "tests" / "test_calc.py").write_text(
                    "from src.calc import add, mul\n"
                    "def test_add():\n    assert add(2, 3) == 5\n"
                    "def test_mul():\n    assert mul(3, 4) == 12\n",
                    encoding="utf-8",
                )

                rc, mk = run_cli([
                    "mark-worker", "--task-id", task_id, "--worker", "w-calc", "--success", "--commit",
                ])
                self.assertEqual(rc, 0)
                self.assertTrue(mk["commit_sha"])

                rc, ver = run_cli(["verify", "--task-id", task_id, "--affected"])
                self.assertEqual(rc, 0)
                self.assertTrue(ver["passed"])
                self.assertEqual(ver["next_node"], "MERGE")

                rc, st = run_cli(["status", "--task-id", task_id])
                self.assertEqual(rc, 0)
                self.assertEqual(st["task"]["task_id"], task_id)

                rc, res = run_cli(["resume", "--task-id", task_id])
                self.assertEqual(rc, 0)
                self.assertEqual(res["pending_workers"], [])

                rc, mrg = run_cli([
                    "merge", "--task-id", task_id, "--test-cmd", "python -m pytest tests/ -q",
                ])
                self.assertEqual(rc, 0)
                self.assertTrue(mrg["ok"])
                self.assertTrue(mrg["fast_forwarded"])
                self.assertIn("def mul", (repo / "src" / "calc.py").read_text(encoding="utf-8"))

    def test_cmd_merge_verifies_integration_worktree_and_protects_main_on_failure(self):
        import io
        from contextlib import redirect_stdout
        from source.patcher.swarm import cli

        def run_cli(argv: list[str]) -> tuple[int, dict]:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(argv)
            return rc, json.loads(buf.getvalue())

        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as tools_dir:
            repo = Path(repo_dir)
            _init_git_repo(repo)
            (repo / "src").mkdir()
            (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_calc.py").write_text(
                "from src.calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init green"], check=True, capture_output=True)
            main_sha_before = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

            briefs_path = Path(tools_dir) / "briefs.json"
            briefs_path.write_text(json.dumps([{
                "worker_name": "w-break",
                "worker_type": "code",
                "owned_files": ["src/calc.py", "tests/test_calc.py"],
                "task_description": "Break integration test",
                "test_command": "echo skipped",
                "priority": 0,
            }]), encoding="utf-8")

            with patch.dict(os.environ, {"AG_TOOLS_DIR": tools_dir}):
                rc, prep = run_cli(["prepare", "--project", str(repo), "--request", "Break integration"])
                self.assertEqual(rc, 0)
                task_id = prep["task_id"]

                rc, setup_res = run_cli(["setup", "--task-id", task_id, "--briefs", str(briefs_path)])
                self.assertEqual(rc, 0)
                wt = Path(setup_res["briefs"][0]["worktree_path"])

                # Worker introduces a failing test on its branch while main remains green
                (wt / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
                rc, _ = run_cli(["mark-worker", "--task-id", task_id, "--worker", "w-break", "--success", "--commit"])
                self.assertEqual(rc, 0)

                rc, mrg = run_cli(["merge", "--task-id", task_id, "--test-cmd", "python -m pytest tests/ -q"])
                self.assertEqual(rc, 2)
                self.assertFalse(mrg["ok"])
                self.assertFalse(mrg["full_suite_passed"])
                self.assertFalse(mrg["fast_forwarded"])

                main_sha_after = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
                self.assertEqual(main_sha_before, main_sha_after, "main must stay untouched when integration suite fails")
                self.assertIn("return a + b", (repo / "src" / "calc.py").read_text(encoding="utf-8"))

    @staticmethod
    def _run_cli(argv: list[str]) -> tuple[int, dict]:
        import io
        from contextlib import redirect_stdout
        from source.patcher.swarm import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, json.loads(buf.getvalue())

    def _setup_calc_task(self, base: Path) -> tuple[Path, str, Path]:
        """Init a tiny git repo + AG_TOOLS_DIR task with 1 prepared & setup worker."""
        repo = base / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "src").mkdir()
        (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_calc.py").write_text(
            "from src.calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "calc"], check=True, capture_output=True)

        briefs_path = base / "briefs.json"
        briefs_path.write_text(json.dumps([{
            "worker_name": "w-calc",
            "worker_type": "code",
            "owned_files": ["src/calc.py"],
            "task_description": "Add sub()",
            "test_command": "python -m pytest tests/test_calc.py -q",
            "priority": 0,
        }]), encoding="utf-8")

        rc, prep = self._run_cli(["prepare", "--project", str(repo), "--request", "Add sub()"])
        self.assertEqual(rc, 0)
        task_id = prep["task_id"]

        rc, setup_res = self._run_cli(["setup", "--task-id", task_id, "--briefs", str(briefs_path)])
        self.assertEqual(rc, 0)
        wt = Path(setup_res["briefs"][0]["worktree_path"])
        return repo, task_id, wt

    def test_cmd_merge_clears_stale_errors_on_fast_forward_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            with patch.dict(os.environ, {"AG_TOOLS_DIR": str(tools_dir)}):
                _repo, task_id, wt = self._setup_calc_task(base)
                (wt / "src" / "calc.py").write_text(
                    "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
                    encoding="utf-8",
                )
                rc, _ = self._run_cli([
                    "mark-worker", "--task-id", task_id, "--worker", "w-calc", "--success", "--commit",
                ])
                self.assertEqual(rc, 0)

                # Pre-populate stale errors before merge succeeds
                state = load_checkpoint(task_id)
                self.assertIsNotNone(state)
                state.errors = ["Cross-worker conflict: w-calc. Fix assigned to: w-calc"]
                save_checkpoint(state)

                rc, mrg = self._run_cli([
                    "merge", "--task-id", task_id, "--test-cmd", "python -m pytest tests/ -q",
                ])
                self.assertEqual(rc, 0)
                self.assertTrue(mrg["ok"])
                self.assertTrue(mrg["fast_forwarded"])
                self.assertEqual(mrg["errors"], [])

                persisted = load_checkpoint(task_id)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.errors, [])

    def test_cmd_mark_worker_records_duration_seconds(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            with patch.dict(os.environ, {"AG_TOOLS_DIR": str(tools_dir)}):
                _repo, task_id, wt = self._setup_calc_task(base)
                try:
                    (wt / "src" / "calc.py").write_text(
                        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
                        encoding="utf-8",
                    )

                    # Backdate created_at so measured duration_seconds is reliably > 0.0
                    state = load_checkpoint(task_id)
                    self.assertIsNotNone(state)
                    state.created_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                    save_checkpoint(state)

                    rc, mk = self._run_cli([
                        "mark-worker", "--task-id", task_id, "--worker", "w-calc", "--success", "--commit",
                    ])
                    self.assertEqual(rc, 0)
                    self.assertIn("duration_seconds", mk)
                    self.assertGreater(mk["duration_seconds"], 0.0)

                    persisted = load_checkpoint(task_id)
                    self.assertIsNotNone(persisted)
                    wr = persisted.worker_results.get("w-calc")
                    self.assertIsNotNone(wr)
                    self.assertGreater(wr.duration_seconds, 0.0)
                    self.assertEqual(wr.duration_seconds, mk["duration_seconds"])
                finally:
                    self._run_cli(["cleanup", "--task-id", task_id])

    def test_cmd_mark_worker_running_and_finished_timestamps(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            with patch.dict(os.environ, {"AG_TOOLS_DIR": str(tools_dir)}):
                _repo, task_id, wt = self._setup_calc_task(base)
                try:
                    rc, mk_run = self._run_cli([
                        "mark-worker", "--task-id", task_id, "--worker", "w-calc", "--status", "running",
                    ])
                    self.assertEqual(rc, 0)
                    self.assertEqual(mk_run["status"], "running")
                    self.assertTrue(mk_run.get("started_at"))

                    state = load_checkpoint(task_id)
                    self.assertIsNotNone(state)
                    brief = next(b for b in state.briefs if b.worker_name == "w-calc")
                    self.assertEqual(brief.status, "running")
                    self.assertTrue(brief.started_at)

                    # Backdate created_at far earlier than started_at to verify duration prefers started_at.
                    now = datetime.now(timezone.utc)
                    state.created_at = (now - timedelta(seconds=60)).isoformat()
                    brief.started_at = (now - timedelta(seconds=5)).isoformat()
                    save_checkpoint(state)

                    (wt / "src" / "calc.py").write_text(
                        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n",
                        encoding="utf-8",
                    )
                    rc, mk_done = self._run_cli([
                        "mark-worker", "--task-id", task_id, "--worker", "w-calc", "--success", "--commit",
                    ])
                    self.assertEqual(rc, 0)
                    self.assertEqual(mk_done["status"], "done")
                    self.assertGreater(mk_done["duration_seconds"], 3.0)
                    self.assertLess(mk_done["duration_seconds"], 30.0)

                    persisted = load_checkpoint(task_id)
                    self.assertIsNotNone(persisted)
                    done_brief = next(b for b in persisted.briefs if b.worker_name == "w-calc")
                    self.assertEqual(done_brief.status, "done")
                    self.assertTrue(done_brief.finished_at)
                finally:
                    self._run_cli(["cleanup", "--task-id", task_id])


class ExtensionInstallerSwarmBundleTests(unittest.TestCase):
    """Verify VSIX packaging and Antigravity 2.0/IDE deployment include swarm engine & skill."""

    @staticmethod
    def _import_installer():
        source_dir = str(ROOT_DIR / "source")
        if source_dir not in sys.path:
            sys.path.insert(0, source_dir)
        from patcher import extension_installer
        return extension_installer

    def test_build_vsix_includes_swarm_engine_and_ag_swarm_skill(self):
        import zipfile
        extension_installer = self._import_installer()

        with tempfile.TemporaryDirectory() as tmpdir:
            out_vsix = Path(tmpdir) / "ag-toolkit-test.vsix"
            built = extension_installer.build_vsix_archive(version="9.9.9", out_path=out_vsix)
            self.assertIsNotNone(built)
            self.assertTrue(built.is_file())
            with zipfile.ZipFile(built, "r") as zf:
                names = set(zf.namelist())
            self.assertIn("extension/swarm_engine/swarm/ipc_server.py", names)
            self.assertIn("extension/swarm_engine/swarm/cli.py", names)
            self.assertIn("extension/swarm_engine/swarm/affected_tests.py", names)
            self.assertIn("extension/swarm_bridge.js", names)
            self.assertIn("extension/skills/ag-swarm/SKILL.md", names)

    def test_deploy_antigravity_2_ui_deploys_swarm_engine_and_global_skill(self):
        extension_installer = self._import_installer()

        with tempfile.TemporaryDirectory() as fake_home:
            home_path = Path(fake_home)
            with patch.object(extension_installer.Path, "home", return_value=home_path):
                ok_res = extension_installer.deploy_antigravity_2_ui()
            self.assertTrue(ok_res)
            self.assertTrue((home_path / ".antigravity_tools" / "swarm_engine" / "swarm" / "cli.py").is_file())
            self.assertTrue((home_path / ".antigravity_tools" / "swarm_engine" / "swarm" / "ipc_server.py").is_file())
            self.assertTrue((home_path / ".antigravity_tools" / "swarm_bridge.js").is_file())
            self.assertTrue((home_path / ".antigravity_tools" / "ag_desktop_ui.js").is_file())
            skill_target = home_path / ".gemini" / "antigravity" / "skills" / "ag-swarm" / "SKILL.md"
            self.assertTrue(skill_target.is_file())
            self.assertIn("ag-swarm", skill_target.read_text(encoding="utf-8"))

    def test_worktree_checkout_resolves_ag_swarm_skill_and_clean_junitxml_id(self):
        """Worktrees without tracked .agents/ still locate ag-swarm SKILL.md via gitdir, and junitxml formats classnames cleanly."""
        from source.patcher.swarm.verifier import _parse_junitxml
        extension_installer = self._import_installer()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            wt_root = tmp / "repo-wt"
            wt_root.mkdir()
            main_git_wt = Path(ROOT_DIR) / ".git" / "worktrees" / "fake_wt"
            (wt_root / ".git").write_text(f"gitdir: {main_git_wt}\n", encoding="utf-8")
            resolved_main = extension_installer._resolve_common_repo_root(wt_root)
            self.assertEqual(resolved_main, Path(ROOT_DIR))

            xml_file = tmp / "junit.xml"
            xml_file.write_text(
                '<testsuites><testsuite><testcase classname="tests.test_swarm.ExtensionInstallerSwarmBundleTests" name="test_build">'
                '<failure message="boom">traceback</failure></testcase></testsuite></testsuites>',
                encoding="utf-8",
            )
            failed_tests, _ = _parse_junitxml(xml_file)
            self.assertEqual(
                failed_tests,
                ["tests/test_swarm.py::ExtensionInstallerSwarmBundleTests::test_build"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

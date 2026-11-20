"""
source/patcher/swarm/verifier.py
Verifier agent — runs tests and reports results back to Coordinator.

Design (see AGENT_SYSTEM_ARCHITECTURE.md §7 VERIFY node + AI review):
  - Verifier uses Flash model (cheap) — just runs shell commands, parses output.
  - Per-worker: runs WorkerBrief.test_command inside the worker's worktree.
  - Uses --junitxml for structured output (review point #3 — more reliable than
    stdout parsing for cross-worker conflict detection).
  - Passes: returns VerificationResult(passed=True).
  - Fails: extracts failed test names + source file paths from XML tracebacks.
  - Differential detection (review point #3): if tests pass in worktree but fail
    after integration merge → confirmed integration conflict.
  - attempt_count increments; at MAX_ATTEMPTS (3) → escalates to Coordinator.
  - Multi-file bug (bug touches files from >1 worker) → cross_worker_conflict=True.
    Coordinator then assigns one worker as "integration fix owner" (Q7 in arch doc).
"""

from __future__ import annotations

import os
import re
import subprocess
import logging
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .state import WorkerBrief, VerificationResult
from .affected_tests import AffectedTestDetector

log = logging.getLogger(__name__)

# Timeout per test run (seconds). Keeps swarm from hanging on infinite loops.
TEST_TIMEOUT = 300  # 5 minutes — generous for large test suites

# Patterns to extract failed test identifiers from pytest stdout (fallback)
_PYTEST_FAILED_RE = re.compile(r'^FAILED (.+?)(?:\s+-\s+.*)?$', re.MULTILINE)
_PYTEST_ERROR_RE = re.compile(r'^ERROR (.+?)(?:\s+-\s+.*)?$', re.MULTILINE)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

class Verifier:
    """
    Runs test commands for each WorkerBrief and collects VerificationResult-s.

    Usage:
        verifier = Verifier(project_path="/path/to/repo")
        result = verifier.verify(brief)
        if not result.passed:
            if result.cross_worker_conflict:
                # Escalate to Orchestrator — multi-file bug
            else:
                # Send back to worker for fix
    """

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path)
        # Lazy-initialized — only built when verify_affected() is called
        self._affected_detector: Optional[AffectedTestDetector] = None

    @property
    def affected_detector(self) -> AffectedTestDetector:
        """Lazy-init AffectedTestDetector (builds AST map on first use)."""
        if self._affected_detector is None:
            self._affected_detector = AffectedTestDetector(self.project_path)
        return self._affected_detector

    def verify(self, brief: WorkerBrief, *, use_junitxml: bool = True) -> VerificationResult:
        """
        Run test_command for a single worker's brief.
        Test runs inside the worker's worktree (or project root if worktree_path not set).

        When use_junitxml=True (default), injects --junitxml into the pytest command
        and parses the XML for structured failure info including source file paths.
        Falls back to stdout parsing if XML is unavailable.

        Returns VerificationResult.
        """
        wt_path = Path(brief.worktree_path) if brief.worktree_path else self.project_path
        cmd = brief.test_command.strip() or "python -m pytest tests/ -v --tb=short"

        log.info("Verifier: running tests for %s", brief.worker_name)
        log.info("  cwd: %s", wt_path)
        log.info("  cmd: %s", cmd)

        # Inject --junitxml for structured output (review point #3)
        xml_path: Optional[Path] = None
        if use_junitxml and "pytest" in cmd and "--junitxml" not in cmd:
            xml_fd, xml_str = tempfile.mkstemp(suffix=".xml", prefix="swarm_pytest_")
            os.close(xml_fd)
            xml_path = Path(xml_str)
            cmd = cmd + f" --junitxml={xml_path}"

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(wt_path),
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            if xml_path and xml_path.exists():
                xml_path.unlink(missing_ok=True)
            return VerificationResult(
                worker_name=brief.worker_name,
                passed=False,
                test_output="",
                error_message=f"Test timed out after {TEST_TIMEOUT}s: {cmd}",
            )
        except Exception as exc:
            if xml_path and xml_path.exists():
                xml_path.unlink(missing_ok=True)
            return VerificationResult(
                worker_name=brief.worker_name,
                passed=False,
                test_output="",
                error_message=f"Failed to run test command: {exc}",
            )

        combined_output = result.stdout + result.stderr
        passed = result.returncode == 0

        # Parse failures from junitxml (preferred) or stdout (fallback)
        failed_tests: list[str] = []
        source_files: list[str] = []
        if not passed:
            if xml_path and xml_path.exists():
                failed_tests, source_files = _parse_junitxml(xml_path)
                xml_path.unlink(missing_ok=True)
            if not failed_tests:
                failed_tests = _extract_failed_tests(combined_output)
            if result.returncode in (4, 5) and not failed_tests:
                out_lower = combined_output.lower()
                if "no tests ran" in out_lower or "file or directory not found" in out_lower:
                    log.info("No test directory found for %s — treating as non-test project (PASS)", brief.worker_name)
                    passed = True
        elif xml_path and xml_path.exists():
            xml_path.unlink(missing_ok=True)

        return VerificationResult(
            worker_name=brief.worker_name,
            passed=passed,
            test_output=combined_output[:8000],
            failed_tests=failed_tests,
            failed_source_files=source_files,  # NEW: file paths from tracebacks
            error_message="" if passed else f"Exit code {result.returncode}",
        )

    def verify_all(
        self,
        briefs: list[WorkerBrief],
        *,
        use_affected: bool = False,
    ) -> dict[str, VerificationResult]:
        """
        Run verify() (or verify_affected() when use_affected=True) for each brief sequentially.
        Returns {worker_name: VerificationResult}.

        Note: intentionally sequential (not parallel) — avoids concurrent pytest
        processes from stepping on shared .pyc caches or test DBs.
        """
        results: dict[str, VerificationResult] = {}
        for brief in sorted(briefs, key=lambda b: b.priority):
            if use_affected:
                results[brief.worker_name] = self.verify_affected(brief)
            else:
                results[brief.worker_name] = self.verify(brief)
        return results

    def verify_affected(self, brief: WorkerBrief) -> VerificationResult:
        """
        Smart verification: runs only the test files that actually cover the
        worker's changed source files, instead of the full test suite.

        Workflow:
            1. Use AffectedTestDetector to find relevant test files from brief.owned_files
            2. If ratio > 0.8 (nearly full suite anyway) → fall back to full suite
            3. Run narrowed pytest command
            4. Full suite is ALWAYS run separately at MERGE node (never skipped there)

        Speed improvement:
            Typical: 5–10x faster per-worker verification
            Example: 1 file changed → 1 test file (5s) instead of full suite (60s)

        Falls back to verify() if:
            - No affected tests found in map
            - Detector fails (import error, parse error, etc.)
            - brief.owned_files is empty
        """
        if not brief.owned_files:
            log.info(
                "verify_affected: no owned_files for %s — falling back to full verify()",
                brief.worker_name,
            )
            return self.verify(brief)

        try:
            ratio = self.affected_detector.coverage_ratio(brief.owned_files)
            if ratio >= 0.8:
                # Narrowing would cover ≥80% of tests anyway — not worth overhead
                log.info(
                    "verify_affected: coverage ratio %.0f%% ≥ 80%% for %s — using full suite",
                    ratio * 100, brief.worker_name,
                )
                return self.verify(brief)

            narrowed_cmd = self.affected_detector.build_pytest_command(
                brief.owned_files,
                base_command="python -m pytest",
                extra_flags="-v --tb=short",
                fallback_to_full=False,  # don't fall back — we'll call verify() instead
            )

            if not narrowed_cmd:
                log.info(
                    "verify_affected: no specific tests for %s — falling back to verify()",
                    brief.worker_name,
                )
                return self.verify(brief)

            log.info(
                "verify_affected: %s — narrowed to %.0f%% of test suite",
                brief.worker_name, ratio * 100,
            )

            # Run narrowed test command (reuse verify() machinery by patching brief temporarily)
            import dataclasses
            narrowed_brief = dataclasses.replace(brief, test_command=narrowed_cmd)
            result = self.verify(narrowed_brief)

            # Tag the result so Coordinator knows it was a narrowed run
            return dataclasses.replace(
                result,
                worker_name=brief.worker_name,  # restore original name
                test_output=f"[affected-only: {len(self.affected_detector.get_affected_tests(brief.owned_files))} test file(s)]\n"
                            + result.test_output,
            )

        except Exception as exc:
            log.warning(
                "verify_affected: detector error for %s (%s) — falling back to verify()",
                brief.worker_name, exc,
            )
            return self.verify(brief)

    def run_full_suite(
        self,
        test_command: Optional[str] = None,
        cwd: Optional[str | Path] = None,
    ) -> VerificationResult:
        """
        Run a final full-project test suite after all merges (MERGE node).
        Runs in `cwd` (typically the integration worktree) when provided, else project root.

        Returns a synthetic VerificationResult with worker_name="__full_suite__".
        """
        cmd = test_command or "python -m pytest tests/ -v --tb=short"
        run_dir = Path(cwd) if cwd else self.project_path
        log.info("Verifier: running full suite in %s: %s", run_dir, cmd)

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return VerificationResult(
                worker_name="__full_suite__",
                passed=False,
                error_message=f"Full suite timed out after {TEST_TIMEOUT}s",
            )
        except Exception as exc:
            return VerificationResult(
                worker_name="__full_suite__",
                passed=False,
                error_message=f"Failed to run full suite: {exc}",
            )

        combined = result.stdout + result.stderr
        passed = result.returncode == 0
        failed_tests = _extract_failed_tests(combined) if not passed else []
        if not passed and result.returncode in (4, 5) and not failed_tests:
            out_lower = combined.lower()
            if "no tests ran" in out_lower or "file or directory not found" in out_lower:
                passed = True
        return VerificationResult(
            worker_name="__full_suite__",
            passed=passed,
            test_output=combined[:8000],
            failed_tests=failed_tests,
            error_message="" if passed else f"Exit code {result.returncode}",
        )


# ---------------------------------------------------------------------------
# Cross-worker conflict detection (architecture Q7)
# ---------------------------------------------------------------------------

def detect_cross_worker_conflict(
    failed_result: VerificationResult,
    briefs: list[WorkerBrief],
) -> Optional[str]:
    """
    Detect cross-worker conflict from a failed VerificationResult.

    Uses THREE layers (review point #3 — from AI review):

    1. Source file paths from junitxml tracebacks (most reliable):
       If failed_result.failed_source_files contains paths owned by ≥2 workers
       → cross-worker conflict confirmed.

    2. Test name matching (fallback when no source files):
       If failed test names contain stem of files from ≥2 workers
       → likely cross-worker conflict.

    3. Ambiguous cases: return None (Coordinator escalates to Orchestrator).

    Returns the suggested "integration fix owner" worker name, or None.
    Architecture §7: "Verifier → Orchestrator → one worker gets integration fix owner role"
    """
    if not failed_result.failed_tests and not failed_result.failed_source_files:
        return None

    # Build reverse map: normalized file path → worker_name
    file_to_worker: dict[str, str] = {}
    for brief in briefs:
        for f in brief.owned_files:
            norm = f.replace("\\", "/")
            file_to_worker[norm] = brief.worker_name
            # Also index by stem for fallback matching
            file_to_worker[Path(f).stem] = brief.worker_name

    workers_implicated: set[str] = set()

    # Layer 1: source file paths from junitxml tracebacks (most reliable)
    if failed_result.failed_source_files:
        for src_path in failed_result.failed_source_files:
            norm = src_path.replace("\\", "/")
            # Exact match
            if norm in file_to_worker:
                workers_implicated.add(file_to_worker[norm])
                continue
            # Suffix match: check if any owned_file is a suffix of this path
            for owned, worker in file_to_worker.items():
                if norm.endswith(owned) or owned.endswith(norm):
                    workers_implicated.add(worker)

    # Layer 2: test name heuristic (fallback)
    if len(workers_implicated) < 2 and failed_result.failed_tests:
        for test_id in failed_result.failed_tests:
            for f, worker in file_to_worker.items():
                stem = Path(f).stem if "/" not in f else f  # stems only
                if stem in test_id:
                    workers_implicated.add(worker)

    if len(workers_implicated) > 1:
        # Layer 3: assign integration fix to worker with lowest priority (merged first)
        priority_sorted = sorted(briefs, key=lambda b: b.priority)
        for b in priority_sorted:
            if b.worker_name in workers_implicated:
                log.warning(
                    "Cross-worker conflict. Workers implicated: %s. Fix owner: %s",
                    workers_implicated, b.worker_name,
                )
                return b.worker_name

    return None


# ---------------------------------------------------------------------------
# Output parsing helpers
# ---------------------------------------------------------------------------

def _parse_junitxml(xml_path: Path) -> tuple[list[str], list[str]]:
    """
    Parse a pytest --junitxml file.

    Returns:
        (failed_test_ids, source_file_paths)
        - failed_test_ids: ["tests/test_foo.py::TestBar::test_baz", ...]
        - source_file_paths: unique source .py file paths from failure tracebacks
          (relative paths like "source/patcher/foo.py" — these are what we use
          for cross-worker conflict detection, more reliable than test names)
    """
    failed_tests: list[str] = []
    source_files: set[str] = set()

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError:
        return [], []

    # junitxml structure: <testsuite> → <testcase> → <failure> | <error>
    for testcase in root.iter("testcase"):
        failure = testcase.find("failure")
        error = testcase.find("error")
        if failure is None and error is None:
            continue

        # Build test ID: classname + name or file + name
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        file_attr = testcase.get("file", "")

        if file_attr and name:
            test_id = f"{file_attr}::{classname}::{name}" if classname else f"{file_attr}::{name}"
        elif classname and name:
            if classname.endswith(".py"):
                test_id = f"{classname}::{name}"
            elif "." in classname:
                parts = classname.split(".")
                if len(parts) >= 2 and parts[-1][:1].isupper():
                    test_id = f"{'/'.join(parts[:-1])}.py::{parts[-1]}::{name}"
                else:
                    test_id = f"{'/'.join(parts)}.py::{name}"
            else:
                test_id = f"{classname}::{name}"
        else:
            test_id = name

        if test_id:
            failed_tests.append(test_id.strip())

        # Extract source file paths from traceback text (the reliable part)
        element = failure if failure is not None else error
        tb_text = (element.text or "") + element.get("message", "")
        for match in re.finditer(r'(?:File "([^"]+\.py)")', tb_text):
            fpath = match.group(1).replace("\\", "/")
            # Normalize: strip absolute path prefix if inside project
            # Keep only the relative part (heuristic: last N segments)
            parts = Path(fpath).parts
            if len(parts) >= 2:
                source_files.add("/".join(parts[-3:]) if len(parts) >= 3 else "/".join(parts))

    return list(dict.fromkeys(failed_tests)), list(source_files)  # deduplicate preserving order


def _extract_failed_tests(output: str) -> list[str]:
    """
    Extract failed/error test IDs from pytest stdout (fallback when no junitxml).
    Returns deduplicated list.
    """
    failed = _PYTEST_FAILED_RE.findall(output)
    errors = _PYTEST_ERROR_RE.findall(output)
    seen: set[str] = set()
    result: list[str] = []
    for t in failed + errors:
        t = t.strip()
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def summarize_verification(results: dict[str, VerificationResult]) -> str:
    """
    Build a human-readable summary of all verification results.
    Used in REPORT node and status bar updates.
    """
    total = len(results)
    passed = sum(1 for r in results.values() if r.passed)
    failed_workers = [name for name, r in results.items() if not r.passed]

    lines = [f"Verification: {passed}/{total} workers passed"]
    if failed_workers:
        lines.append(f"Failed: {', '.join(failed_workers)}")
        for name in failed_workers:
            r = results[name]
            if r.failed_tests:
                lines.append(f"  {name}: {len(r.failed_tests)} test(s) failed")
                for t in r.failed_tests[:5]:
                    lines.append(f"    - {t}")
                if len(r.failed_tests) > 5:
                    lines.append(f"    ... and {len(r.failed_tests) - 5} more")
            if r.error_message:
                lines.append(f"  {name} error: {r.error_message}")
    return "\n".join(lines)


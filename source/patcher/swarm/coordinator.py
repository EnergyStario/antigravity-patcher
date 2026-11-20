"""
source/patcher/swarm/coordinator.py
Coordinator — the main entry point for the Multi-Agent Swarm.

Implements the DECOMPOSE → VALIDATE → EXECUTE → VERIFY → MERGE → REPORT graph
(AGENT_SYSTEM_ARCHITECTURE.md §7).

Design rules (incorporating AI review feedback):
  1. Every node ends with save_checkpoint(state). Additionally, checkpoint is
     written after EACH individual WorkerResult — not just after the whole EXECUTE
     node. This prevents losing 10 workers worth of work on a crash in the last one.
  2. EXECUTE runs workers in parallel via ThreadPoolExecutor(max_workers=...).
     All writes to shared state go through a threading.Lock.
  3. Each worker has a timeout (WORKER_TIMEOUT_SECONDS). A hung agent cannot
     block the entire phase.
  4. execute_worker_callback supports both sync (testing) and start+poll (production)
     modes. In production the callback should write a done.json marker to the
     worktree directory; Coordinator polls for it with 5s intervals.
  5. MERGE now creates an integration branch first, runs full tests, and only
     fast-forwards main if green. This prevents main from being left in a
     broken state if the final suite fails.
  6. MAX_ATTEMPTS = state.max_attempts (default 3). After that → REPORT.
  7. Cross-worker conflicts: Verifier uses --junitxml and differential detection
     (see verifier.py). Coordinator assigns integration_fix_owner.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from pathlib import Path
from typing import Callable, Optional

from .state import (
    SwarmState, WorkerBrief, WorkerResult, VerificationResult,
    save_checkpoint, load_checkpoint,
)
from .decomposer import (
    decompose_task, DecompositionError, build_orchestrator_prompt, build_file_tree,
    reconstruct_phases,
)
from .worktree_manager import WorktreeManager
from .verifier import Verifier, detect_cross_worker_conflict, summarize_verification

log = logging.getLogger(__name__)

# Type alias for the execute callback (brief → WorkerResult)
ExecuteCallback = Callable[[WorkerBrief], WorkerResult]

# Default per-worker timeout in seconds (5 minutes).
# Keeps a hung agent from blocking the entire phase.
WORKER_TIMEOUT_SECONDS = 300

# Sentinel return values for node functions
_NEXT = "NEXT"
_RETRY = "RETRY"
_ABORT = "ABORT"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class Coordinator:
    """
    Main controller for a swarm task.

    Args:
        project_path: absolute path to the git repository being modified.
        execute_worker_callback: function called for each WorkerBrief during EXECUTE.
            Signature: (WorkerBrief) -> WorkerResult
            Runs in a ThreadPoolExecutor — must be thread-safe.
            In production: starts a Flash agent process, polls for done.json.
            In tests: inject a mock (can be stateful via threading.Lock).
        max_workers: cap per phase (default 5, max 10).
        full_test_command: command for final suite after all merges.
        worker_timeout: per-worker timeout in seconds (default 300).
        integration_branch_prefix: prefix for the integration branch name.

    Usage:
        # Fresh run:
        coord = Coordinator(project_path=..., execute_worker_callback=fn)
        state = coord.run(user_request="...", orchestrator_decompose_fn=llm_fn)

        # Resume after account switch / crash:
        state = coord.resume(task_id="...", orchestrator_decompose_fn=llm_fn)
    """

    def __init__(
        self,
        project_path: str | Path,
        execute_worker_callback: Optional[ExecuteCallback] = None,
        *,
        max_workers: int = 5,
        full_test_command: Optional[str] = None,
        worker_timeout: int = WORKER_TIMEOUT_SECONDS,
        integration_branch_prefix: str = "swarm-integration",
        use_affected_tests: bool = False,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.project_path = Path(project_path).resolve()
        self.execute_worker_callback = execute_worker_callback or _noop_execute
        self.max_workers = min(max(1, max_workers), 10)
        self.full_test_command = full_test_command
        self.worker_timeout = worker_timeout
        self.integration_branch_prefix = integration_branch_prefix
        self.use_affected_tests = use_affected_tests
        self.on_event = on_event
        self._worktree_mgr: Optional[WorktreeManager] = None
        self._verifier = Verifier(self.project_path)
        # Lock protecting all writes to shared SwarmState during parallel EXECUTE
        self._state_lock = threading.Lock()

    def _emit(self, event: dict) -> None:
        """Notify optional IPC/UI progress listener without failing the graph."""
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:
            log.debug("on_event callback error (ignored): %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        user_request: str,
        orchestrator_decompose_fn: Callable[[str], list[dict]],
        *,
        file_tree: Optional[list[str]] = None,
        max_attempts: int = 3,
        orchestrator_model: str = "",
    ) -> SwarmState:
        """
        Start a fresh swarm task.

        Args:
            user_request: the user's natural language request.
            orchestrator_decompose_fn: callable that takes the orchestrator prompt
                and returns a list of WorkerBrief dicts (parsed from LLM JSON output).
            file_tree: pre-built file list. If None, auto-generated (2-pass).
            max_attempts: max retry per worker (default 3).
            orchestrator_model: name of the Orchestrator model (tracked in state).
        """
        state = SwarmState.new(
            user_request=user_request,
            project_path=str(self.project_path),
            max_workers=self.max_workers,
            max_attempts=max_attempts,
            orchestrator_model=orchestrator_model,
        )

        if file_tree is not None:
            state.file_tree = file_tree
        else:
            log.info("Building file tree for %s ...", self.project_path)
            # 2-pass: directory summary first, then file list (review point #7)
            state.file_tree = build_file_tree(self.project_path)

        save_checkpoint(state)
        log.info("Swarm task started: %s", state.task_id)
        self._emit({
            "event": "started",
            "task_id": state.task_id,
            "project_path": state.project_path,
            "user_request": state.user_request,
        })
        return self._run_graph(state, orchestrator_decompose_fn)

    def resume(
        self,
        task_id: str,
        orchestrator_decompose_fn: Optional[Callable[[str], list[dict]]] = None,
    ) -> SwarmState:
        """
        Resume a swarm task from last checkpoint (called after "continue" message).

        Workers with status='done' and non-empty commit_sha are skipped.
        Workers with status='running' at crash time are reset to 'pending'.
        Existing worktrees are reused if present.
        """
        state = load_checkpoint(task_id)
        if state is None:
            raise FileNotFoundError(
                f"No checkpoint found for task_id={task_id!r}. "
                "Cannot resume — start a new run() instead."
            )

        log.info(
            "Resuming task %s from node=%s (phase %d/%d)",
            task_id, state.current_node, state.current_phase, state.total_phases,
        )

        # Reset 'running' workers to 'pending' — they may have crashed mid-work
        for brief in state.briefs:
            if brief.status == "running":
                log.warning(
                    "Worker %s was 'running' at crash time — resetting to 'pending'",
                    brief.worker_name,
                )
                brief.status = "pending"

        self._emit({
            "event": "started",
            "task_id": state.task_id,
            "project_path": state.project_path,
            "user_request": state.user_request,
            "resumed": True,
        })

        def _require_decompose(prompt: str) -> list[dict]:
            if orchestrator_decompose_fn is None:
                raise ValueError("orchestrator_decompose_fn is required when resuming at DECOMPOSE node")
            return orchestrator_decompose_fn(prompt)

        return self._run_graph(state, _require_decompose)

    # ------------------------------------------------------------------
    # Graph runner
    # ------------------------------------------------------------------

    def _run_graph(
        self,
        state: SwarmState,
        orchestrator_decompose_fn: Callable[[str], list[dict]],
    ) -> SwarmState:
        """
        Main graph loop. Runs nodes in sequence; each node updates state and calls
        save_checkpoint. Loop breaks on DONE or unrecoverable ABORT.
        """
        self._worktree_mgr = WorktreeManager(state)

        # Prune stale worktree refs on startup (review point #4 — GC)
        self._worktree_mgr.prune_stale()

        # Reconstruct phases when resuming from an already-decomposed checkpoint
        phases = reconstruct_phases(state.briefs, state.max_workers)

        while not state.is_complete:
            node = state.current_node
            log.info(
                "[Swarm %s] Node: %s (phase %d/%d)",
                state.task_id[:8], node, state.current_phase, state.total_phases,
            )
            self._emit({
                "event": "node_started",
                "task_id": state.task_id,
                "node": node,
                "phase": state.current_phase,
                "total_phases": state.total_phases,
            })

            if node == "DECOMPOSE":
                outcome, state, phases = self._node_decompose(state, orchestrator_decompose_fn)

            elif node == "VALIDATE":
                outcome, state = self._node_validate(state)

            elif node == "EXECUTE":
                current_phase_briefs = _get_phase_briefs(phases, state.current_phase)
                outcome, state = self._node_execute(state, current_phase_briefs)

            elif node == "VERIFY":
                current_phase_briefs = _get_phase_briefs(phases, state.current_phase)
                outcome, state = self._node_verify(state, current_phase_briefs)

            elif node == "MERGE":
                outcome, state = self._node_merge(state)

            elif node == "REPORT":
                outcome, state = self._node_report(state)

            elif node == "DONE":
                state.is_complete = True
                break

            else:
                log.error("Unknown node: %s — aborting", node)
                state.errors.append(f"Unknown graph node: {node}")
                state.current_node = "DONE"
                outcome = _ABORT

            save_checkpoint(state)

            if outcome == _ABORT:
                log.error("[Swarm %s] Aborted at node %s", state.task_id[:8], node)
                break

        return state

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _node_decompose(
        self,
        state: SwarmState,
        orchestrator_decompose_fn: Callable[[str], list[dict]],
    ) -> tuple[str, SwarmState, list[list[WorkerBrief]]]:
        """
        DECOMPOSE: ask Orchestrator (Tier-1) to split the task into WorkerBrief-s.
        Retries up to 3 times on DecompositionError.
        """
        max_decompose_retries = 3

        for attempt in range(max_decompose_retries):
            try:
                prompt = build_orchestrator_prompt(state)
                raw_briefs_dicts = orchestrator_decompose_fn(prompt)
                _all_briefs, phases = decompose_task(state, raw_briefs_dicts)
                state.briefs = [b for phase in phases for b in phase]
                state.total_phases = len(phases)
                state.current_phase = 1
                state.current_node = "VALIDATE"
                log.info(
                    "DECOMPOSE: %d brief(s) in %d phase(s)",
                    len(state.briefs), len(phases),
                )
                return _NEXT, state, phases

            except DecompositionError as exc:
                log.warning("DECOMPOSE attempt %d failed: %s", attempt + 1, exc)
                state.errors.append(f"DECOMPOSE attempt {attempt + 1}: {exc}")

        state.errors.append("DECOMPOSE failed after max retries — aborting")
        state.current_node = "DONE"
        return _ABORT, state, []

    def _node_validate(self, state: SwarmState) -> tuple[str, SwarmState]:
        """
        VALIDATE: deterministic check — no LLM calls.
        Verifies no two workers own the same file, creates git worktrees.
        """
        from .decomposer import _validate_no_file_conflicts

        conflicts = _validate_no_file_conflicts(state.briefs)
        if conflicts:
            state.errors.extend(conflicts)
            log.warning("VALIDATE found conflicts: %s", conflicts)
            state.current_node = "DECOMPOSE"
            return _RETRY, state

        # Create worktrees (reuses existing ones on resume)
        try:
            self._worktree_mgr.setup()
            log.info("VALIDATE: worktrees created/verified")
        except RuntimeError as exc:
            state.errors.append(f"VALIDATE worktree setup failed: {exc}")
            state.current_node = "DONE"
            return _ABORT, state

        state.current_node = "EXECUTE"
        return _NEXT, state

    def _node_execute(
        self,
        state: SwarmState,
        phase_briefs: list[WorkerBrief],
    ) -> tuple[str, SwarmState]:
        """
        EXECUTE: run workers in PARALLEL via ThreadPoolExecutor (review point #1).

        Each worker runs in its own thread. Writes to shared state are protected
        by self._state_lock. Checkpoint is saved after each individual WorkerResult
        (review point #2 — not just at the end of the node).

        Workers with status='done' and non-empty commit_sha are skipped (resume support).

        Each future has a timeout of self.worker_timeout seconds (review point #1 —
        prevents a hung agent from blocking the entire phase).
        """
        pending = [b for b in phase_briefs if not (b.status == "done" and b.commit_sha)]
        already_done = [b for b in phase_briefs if b.status == "done" and b.commit_sha]

        log.info(
            "EXECUTE: phase %d — %d worker(s) to run, %d already done",
            state.current_phase, len(pending), len(already_done),
        )

        if not pending:
            state.current_node = "VERIFY"
            return _NEXT, state

        # Mark all as running before submitting
        for brief in pending:
            with self._state_lock:
                brief.status = "running"
                brief.attempt_count += 1

        future_to_brief: dict[Future, WorkerBrief] = {}

        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            for brief in pending:
                future = pool.submit(self.execute_worker_callback, brief)
                future_to_brief[future] = brief
                log.info("  Submitted %s (attempt %d)", brief.worker_name, brief.attempt_count)

            for future in as_completed(future_to_brief, timeout=self.worker_timeout + 10):
                brief = future_to_brief[future]
                try:
                    result: WorkerResult = future.result(timeout=self.worker_timeout)
                    with self._state_lock:
                        brief.status = "done" if result.success else "failed"
                        if result.commit_sha:
                            brief.commit_sha = result.commit_sha
                        state.worker_results[brief.worker_name] = result
                    log.info(
                        "  %s: %s (sha=%s)",
                        brief.worker_name,
                        "✓" if result.success else "✗",
                        result.commit_sha[:8] if result.commit_sha else "none",
                    )
                except Exception as exc:
                    with self._state_lock:
                        brief.status = "failed"
                        state.worker_results[brief.worker_name] = WorkerResult(
                            worker_name=brief.worker_name,
                            success=False,
                            error_message=f"Worker raised: {exc}",
                        )
                        state.errors.append(f"EXECUTE {brief.worker_name}: {exc}")
                    log.error("  %s raised: %s", brief.worker_name, exc)

                # Save checkpoint immediately after each WorkerResult (review point #2)
                with self._state_lock:
                    save_checkpoint(state)
                    wr_done = state.worker_results.get(brief.worker_name)
                self._emit({
                    "event": "worker_done",
                    "task_id": state.task_id,
                    "worker_name": brief.worker_name,
                    "success": bool(wr_done and wr_done.success),
                })

        state.current_node = "VERIFY"
        return _NEXT, state

    def _node_verify(
        self,
        state: SwarmState,
        phase_briefs: list[WorkerBrief],
    ) -> tuple[str, SwarmState]:
        """
        VERIFY: run test_command for each done worker.

        Failure handling:
          - cross-worker conflict (differential detection) → integration_fix_owner
          - attempt < max_attempts → reset to pending → EXECUTE
          - attempt >= max_attempts → REPORT
        """
        done_briefs = [b for b in phase_briefs if b.status == "done"]
        if not done_briefs:
            log.warning("VERIFY: no done workers to verify — skipping to next phase/MERGE")
            state.current_node = _advance_phase_or_merge(state)
            return _NEXT, state

        log.info("VERIFY: testing %d worker(s) (affected_only=%s)", len(done_briefs), self.use_affected_tests)
        verify_results = self._verifier.verify_all(done_briefs, use_affected=self.use_affected_tests)

        for name, vr in verify_results.items():
            state.verification_results[name] = vr
            log.info("  %s: %s", name, "✓" if vr.passed else f"✗ ({len(vr.failed_tests)} failure(s))")

        failed_vrs = {name: vr for name, vr in verify_results.items() if not vr.passed}
        self._emit({
            "event": "phase_verified",
            "task_id": state.task_id,
            "phase": state.current_phase,
            "passed": len(failed_vrs) == 0,
            "failed_workers": list(failed_vrs.keys()),
        })

        if not failed_vrs:
            prev_owner = state.integration_fix_owner
            remaining_errors = [
                e for e in state.errors if not e.startswith("Cross-worker conflict:")
            ]
            removed_conflict_errors = len(state.errors) - len(remaining_errors)
            state.integration_fix_owner = ""
            state.errors = remaining_errors
            if prev_owner or removed_conflict_errors:
                log.info(
                    "VERIFY: cleared resolved cross-worker conflict state "
                    "(previous owner=%r, removed_errors=%d)",
                    prev_owner,
                    removed_conflict_errors,
                )
            log.info("VERIFY: all passed")
            state.current_node = _advance_phase_or_merge(state)
            return _NEXT, state

        # --- Handle failures ---
        # Check for cross-worker conflict (Q7 — now uses differential detection)
        for name, vr in failed_vrs.items():
            brief = _find_brief(state.briefs, name)
            if brief:
                cross_owner = detect_cross_worker_conflict(vr, state.briefs)
                if cross_owner:
                    log.warning(
                        "Cross-worker conflict for %s. Integration fix owner: %s",
                        name, cross_owner,
                    )
                    state.integration_fix_owner = cross_owner
                    state.errors.append(
                        f"Cross-worker conflict: {name}. Fix assigned to: {cross_owner}"
                    )
                    owner_brief = _find_brief(state.briefs, cross_owner)
                    if owner_brief:
                        # Grant temporary extended file access
                        extra = [t for vr2 in failed_vrs.values() for t in vr2.failed_tests]
                        owner_brief.owned_files = list(set(owner_brief.owned_files + extra))
                        owner_brief.status = "pending"
                    state.current_node = "EXECUTE"
                    return _RETRY, state

        # Check max attempts — if any worker is exhausted, go to REPORT
        exhausted = [
            name for name in failed_vrs
            if (b := _find_brief(state.briefs, name)) and b.attempt_count >= state.max_attempts
        ]
        if exhausted:
            msg = (
                f"Max attempts ({state.max_attempts}) reached for: {', '.join(exhausted)}. "
                "Manual intervention required."
            )
            state.errors.append(msg)
            log.error("VERIFY: %s", msg)
            state.current_node = "REPORT"
            return _NEXT, state

        # Reset failing workers and retry
        for name in failed_vrs:
            brief = _find_brief(state.briefs, name)
            if brief:
                brief.status = "pending"
                log.info("  %s reset to pending (attempt %d)", name, brief.attempt_count)

        state.current_node = "EXECUTE"
        return _RETRY, state

    def _node_merge(self, state: SwarmState) -> tuple[str, SwarmState]:
        """
        MERGE: use integration-branch strategy (review point #6).

        Flow:
          1. Create integration branch from main
          2. Merge all worker branches into integration (priority order)
          3. Run full test suite on integration branch
          4. If green → fast-forward main to integration
          5. If red → abort, leave main untouched, report to user
          6. Cleanup worktrees

        This ensures main is NEVER left in a broken state.
        """
        log.info("MERGE: starting integration-branch merge for %d workers", len(state.briefs))

        try:
            final_sha = self._worktree_mgr.merge_via_integration(
                integration_prefix=self.integration_branch_prefix,
                task_id=state.task_id,
            )
            state.final_branch = final_sha
            log.info("MERGE: integration branch merged. SHA: %s", final_sha[:8])
        except RuntimeError as exc:
            state.errors.append(f"MERGE conflict: {exc}")
            log.error("MERGE failed: %s", exc)
            try:
                self._worktree_mgr.cleanup()
            except Exception:
                pass
            state.current_node = "REPORT"
            return _NEXT, state

        # Full suite on integration worktree BEFORE touching main
        log.info("MERGE: running full test suite on integration branch")
        integ_wt = self._worktree_mgr.integration_worktree_path(state.task_id)
        if integ_wt.is_dir():
            full_result = self._verifier.run_full_suite(self.full_test_command, cwd=integ_wt)
        else:
            full_result = self._verifier.run_full_suite(self.full_test_command)
        state.verification_results["__full_suite__"] = full_result

        if full_result.passed:
            log.info("MERGE: full suite passed — fast-forwarding main")
            try:
                self._worktree_mgr.fast_forward_main(
                    integration_prefix=self.integration_branch_prefix,
                    task_id=state.task_id,
                )
            except RuntimeError as exc:
                state.errors.append(f"MERGE fast-forward failed: {exc}")
                log.error("Fast-forward failed: %s", exc)
        else:
            # Main is UNTOUCHED — only integration branch has the changes
            state.errors.append(
                f"Full suite FAILED on integration branch after merge. "
                f"{len(full_result.failed_tests)} test(s) failing. "
                "main branch is untouched. Fix the failures and retry."
            )
            log.warning("Full suite failed on integration branch: %s", full_result.failed_tests[:5])

        # Cleanup worktrees regardless of test outcome
        try:
            self._worktree_mgr.cleanup()
        except Exception as exc:
            log.warning("Worktree cleanup failed (non-fatal): %s", exc)

        state.current_node = "REPORT"
        return _NEXT, state

    def _node_report(self, state: SwarmState) -> tuple[str, SwarmState]:
        """REPORT: build human-readable summary, mark task complete."""
        lines = [
            "# Swarm Task Report",
            f"**Task ID:** `{state.task_id}`",
            f"**Request:** {state.user_request}",
            f"**Project:** {state.project_path}",
            f"**Phases:** {state.total_phases}",
            f"**Workers:** {len(state.briefs)}",
            "",
            "## Results",
        ]

        for brief in sorted(state.briefs, key=lambda b: b.priority):
            wr = state.worker_results.get(brief.worker_name)
            vr = state.verification_results.get(brief.worker_name)
            icon = "✅" if (wr and wr.success and vr and vr.passed) else "❌"
            lines.append(f"- {icon} **{brief.worker_name}**: {brief.task_description[:60]}")
            if wr and wr.commit_sha:
                lines.append(f"  - Commit: `{wr.commit_sha[:8]}`")
            if vr and not vr.passed:
                lines.append(f"  - Failed tests: {len(vr.failed_tests)}")

        full_vr = state.verification_results.get("__full_suite__")
        lines.append("\n## Final Suite")
        if full_vr:
            lines.append("✅ All tests passed" if full_vr.passed else "❌ Some tests failed")
            if not full_vr.passed:
                lines.extend([f"  - {t}" for t in full_vr.failed_tests[:10]])
        else:
            lines.append("_(not run — merge failed or not reached)_")

        if state.final_branch:
            lines.append(f"\n**Merged SHA:** `{state.final_branch[:8]}`")

        if state.errors:
            lines.append(f"\n## Errors ({len(state.errors)})")
            for e in state.errors:
                lines.append(f"- {e}")

        log.info("REPORT:\n%s", "\n".join(lines))
        state.is_complete = True
        state.current_node = "DONE"
        return _NEXT, state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _noop_execute(brief: WorkerBrief) -> WorkerResult:
    """Default no-op execute callback — returns success with no changes."""
    log.warning("execute_worker_callback not set — using no-op for %s", brief.worker_name)
    return WorkerResult(
        worker_name=brief.worker_name,
        success=True,
        commit_sha="",
        files_changed=[],
    )


def _find_brief(briefs: list[WorkerBrief], worker_name: str) -> Optional[WorkerBrief]:
    return next((b for b in briefs if b.worker_name == worker_name), None)


def _get_phase_briefs(phases: list[list[WorkerBrief]], phase_number: int) -> list[WorkerBrief]:
    idx = phase_number - 1
    if idx < 0 or idx >= len(phases):
        return []
    return phases[idx]


def _advance_phase_or_merge(state: SwarmState) -> str:
    if state.current_phase < state.total_phases:
        state.current_phase += 1
        return "EXECUTE"
    return "MERGE"

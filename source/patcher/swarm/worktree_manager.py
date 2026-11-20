"""
source/patcher/swarm/worktree_manager.py
Git Worktree isolation layer for the Multi-Agent Swarm.

Design (see AGENT_SYSTEM_ARCHITECTURE.md §8.2 + AI review):
  - Each worker gets its own git worktree in a sibling directory:
      {project_root}/../{project}-swarm-{task_id[:8]}/worker-N/
  - Worktrees share the same git object store → no duplication of history.
  - Workers can READ the full project; WRITE only to their branch.
  - Merge order: by WorkerBrief.priority (ascending, 0 = first).
  - MERGE uses integration-branch strategy (review point #6):
      1. Create integration branch from HEAD
      2. Merge all worker branches into integration
      3. Run tests on integration (done by Coordinator/Verifier)
      4. Fast-forward main only if tests pass → main NEVER broken
  - Cleanup: `git worktree remove --force` + rmtree sibling dir.
  - GC (review point #4): prune_stale() on startup, cleanup_all_stale(days=N).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .state import WorkerBrief, SwarmState

log = logging.getLogger(__name__)

# Max seconds for any git operation before we give up
GIT_TIMEOUT = 60

# Prefix used in ALL swarm branch and directory names — makes GC safe
# (cleanup only touches names starting with this prefix)
SWARM_BRANCH_PREFIX = "swarm-"
SWARM_DIR_SUFFIX = "-swarm-"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command. Raises RuntimeError with stderr on non-zero exit if check=True."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s") from e

    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit={result.returncode}):\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _swarm_root(project_path: Path, task_id: str) -> Path:
    """
    Returns the sibling directory that holds all worktrees for this task.
    e.g. /home/user/myproject/../myproject-swarm-abc12345/
    Kept outside the project to avoid git tracking.
    """
    parent = project_path.parent
    slug = project_path.name + SWARM_DIR_SUFFIX + task_id[:8]
    return parent / slug


def _integration_branch_name(prefix: str, task_id: str) -> str:
    """e.g. 'swarm-integration-abc12345'"""
    return f"{prefix}-{task_id[:8]}"


# ---------------------------------------------------------------------------
# WorktreeManager
# ---------------------------------------------------------------------------

class WorktreeManager:
    """
    Manages git worktrees for a swarm task.

    Lifecycle:
        mgr = WorktreeManager(state)
        mgr.prune_stale()             # GC on startup
        mgr.setup()                   # creates worktrees for all briefs
        ...workers run...
        mgr.merge_via_integration()   # integration branch → test → ff main
        mgr.cleanup()                 # removes worktrees + sibling dir
    """

    def __init__(self, state: SwarmState):
        self.state = state
        self.project_path = Path(state.project_path)
        self.swarm_root = _swarm_root(self.project_path, state.task_id)
        self._verify_git_repo()

    def _verify_git_repo(self) -> None:
        """
        Confirm the project_path is a usable git repository.
        If project_path is not a repo:
          1. Checks if a single child directory is a git repo (e.g. repo/).
          2. Otherwise auto-inits a git repository with an initial commit.
        """
        result = _run_git(["rev-parse", "--show-toplevel"], self.project_path, check=False)
        if result.returncode != 0:
            candidate_repos = []
            try:
                candidate_repos = [p for p in self.project_path.iterdir() if p.is_dir() and (p / ".git").is_dir()]
            except Exception:
                pass
            if len(candidate_repos) == 1:
                self.project_path = candidate_repos[0]
                self.state.project_path = str(self.project_path)
                self.swarm_root = _swarm_root(self.project_path, self.state.task_id)
                result = _run_git(["rev-parse", "--show-toplevel"], self.project_path, check=False)
            elif not (self.project_path / ".git").exists():
                _run_git(["init"], self.project_path, check=False)
                _run_git(["config", "user.name", "Swarm Coordinator"], self.project_path, check=False)
                _run_git(["config", "user.email", "swarm@antigravity.local"], self.project_path, check=False)
                _run_git(["add", "."], self.project_path, check=False)
                _run_git(["commit", "-m", "chore: initial commit for swarm worktrees", "--allow-empty"], self.project_path, check=False)
                result = _run_git(["rev-parse", "--show-toplevel"], self.project_path, check=False)

        if result.returncode != 0:
            raise RuntimeError(
                f"project_path is not inside a git repository: {self.project_path}\n"
                "The swarm requires git to isolate worker changes via worktrees."
            )
        head_result = _run_git(["rev-parse", "HEAD"], self.project_path, check=False)
        if head_result.returncode != 0:
            _run_git(["commit", "-m", "chore: initial commit for swarm worktrees", "--allow-empty"], self.project_path, check=False)
            head_result = _run_git(["rev-parse", "HEAD"], self.project_path, check=False)
            if head_result.returncode != 0:
                raise RuntimeError(
                    f"Git repository at {self.project_path} has no commits. "
                    "Create an initial commit before starting a swarm task."
                )

    def _worktree_path(self, brief: WorkerBrief) -> Path:
        return self.swarm_root / brief.worker_name

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """
        Create git worktrees for all briefs.
        Branch is created fresh from HEAD.
        Updates brief.worktree_path in-place.
        On resume: reuses existing worktrees (idempotent).
        """
        self.swarm_root.mkdir(parents=True, exist_ok=True)

        for brief in sorted(self.state.briefs, key=lambda b: b.priority):
            wt_path = self._worktree_path(brief)

            # Reuse existing worktree on resume
            if wt_path.exists():
                log.info(
                    "Worktree already exists at %s — reusing (resume)", wt_path
                )
                brief.worktree_path = str(wt_path)
                continue

            # Clean up any stale branch from a previous failed attempt
            _run_git(["branch", "-D", brief.branch_name], self.project_path, check=False)

            _run_git(
                ["worktree", "add", str(wt_path), "-b", brief.branch_name],
                self.project_path,
            )
            brief.worktree_path = str(wt_path)
            log.info("Created worktree: %s → branch %s", wt_path, brief.branch_name)

    def worktree_path(self, brief: WorkerBrief) -> Path:
        return Path(brief.worktree_path) if brief.worktree_path else self._worktree_path(brief)

    # ------------------------------------------------------------------
    # Worker commit
    # ------------------------------------------------------------------

    def commit_worker_work(self, brief: WorkerBrief, message: str) -> str:
        """
        Stage only owned files and commit in the worker's worktree.
        Returns the commit SHA.
        Enforces ownership rule: only brief.owned_files are staged.
        """
        wt = self.worktree_path(brief)

        for rel_path in brief.owned_files:
            abs_path = wt / rel_path
            if abs_path.exists():
                _run_git(["add", rel_path], wt, check=False)
            else:
                _run_git(["rm", "--cached", "--ignore-unmatch", rel_path], wt, check=False)

        status = _run_git(["status", "--porcelain"], wt, check=False)
        if not status.stdout.strip():
            log.info("Worker %s: nothing to commit", brief.worker_name)
            sha_res = _run_git(["rev-parse", "HEAD"], wt)
            return sha_res.stdout.strip()

        _run_git(["commit", "-m", message, "--allow-empty"], wt)
        sha_res = _run_git(["rev-parse", "HEAD"], wt)
        sha = sha_res.stdout.strip()
        log.info("Worker %s committed: %s", brief.worker_name, sha[:8])
        return sha

    # ------------------------------------------------------------------
    # Merge strategy: integration branch → fast-forward main (review #6)
    # ------------------------------------------------------------------

    def integration_worktree_path(self, task_id: Optional[str] = None) -> Path:
        """Path to the isolated temporary worktree used to verify the integration merge."""
        return _swarm_root(self.project_path, task_id or self.state.task_id) / "_integration"

    def merge_via_integration(
        self,
        integration_prefix: str = "swarm-integration",
        task_id: Optional[str] = None,
    ) -> str:
        """
        INTEGRATION BRANCH STRATEGY:
        1. Create integration branch from HEAD inside a dedicated worktree (_integration)
        2. Merge all worker branches (priority order) into that integration worktree
        3. Keep integration worktree checked out so full suite runs on the integrated code
        4. main in project_path is NEVER checked out or touched until fast_forward_main()

        Returns: SHA of the integration branch HEAD.
        On conflict: raises RuntimeError, main untouched.
        """
        tid = task_id or self.state.task_id
        integration_branch = _integration_branch_name(integration_prefix, tid)
        integration_wt = self.integration_worktree_path(tid)

        if integration_wt.exists():
            _run_git(["worktree", "remove", str(integration_wt), "--force"], self.project_path, check=False)
            shutil.rmtree(integration_wt, ignore_errors=True)
        _run_git(["worktree", "prune"], self.project_path, check=False)
        _run_git(["branch", "-D", integration_branch], self.project_path, check=False)

        self.swarm_root.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["worktree", "add", "-b", integration_branch, str(integration_wt), "HEAD"],
            self.project_path,
        )
        log.info("Created integration worktree: %s → branch %s", integration_wt, integration_branch)

        sorted_briefs = sorted(self.state.briefs, key=lambda b: b.priority)

        for brief in sorted_briefs:
            if brief.status != "done":
                log.warning(
                    "Skipping merge for %s (status=%s)", brief.worker_name, brief.status
                )
                continue

            log.info("Merging %s → %s", brief.branch_name, integration_branch)
            result = _run_git(
                ["merge", "--no-ff", brief.branch_name,
                 "-m", f"merge({brief.worker_name}): {brief.task_description[:60]}"],
                integration_wt,
                check=False,
            )

            if result.returncode != 0:
                conflicting = self._get_conflict_files(cwd=integration_wt)
                _run_git(["merge", "--abort"], integration_wt, check=False)
                _run_git(["worktree", "remove", str(integration_wt), "--force"], self.project_path, check=False)
                shutil.rmtree(integration_wt, ignore_errors=True)
                raise RuntimeError(
                    f"Merge conflict integrating {brief.branch_name} → {integration_branch}.\n"
                    f"Conflicting files: {', '.join(conflicting) or 'unknown'}\n"
                    "Integration aborted. main is untouched."
                )

        sha_res = _run_git(["rev-parse", "HEAD"], integration_wt)
        integration_sha = sha_res.stdout.strip()
        log.info(
            "Integration branch ready: %s (SHA %s)", integration_branch, integration_sha[:8]
        )
        return integration_sha

    def _remove_integration_worktree(self, task_id: Optional[str] = None) -> None:
        """Detach and delete the temporary _integration worktree if present."""
        integration_wt = self.integration_worktree_path(task_id)
        if integration_wt.exists():
            _run_git(["worktree", "remove", str(integration_wt), "--force"], self.project_path, check=False)
            shutil.rmtree(integration_wt, ignore_errors=True)
            _run_git(["worktree", "prune"], self.project_path, check=False)

    def fast_forward_main(
        self,
        integration_prefix: str = "swarm-integration",
        task_id: Optional[str] = None,
    ) -> None:
        """
        Fast-forward main to the integration branch HEAD.
        Called by Coordinator ONLY after full test suite passes on integration.

        Uses --ff-only to guarantee no additional merge commit is created.
        """
        tid = task_id or self.state.task_id
        integration_branch = _integration_branch_name(integration_prefix, tid)

        # Detach integration worktree before deleting integration branch below
        self._remove_integration_worktree(tid)

        log.info("Fast-forwarding main → %s", integration_branch)
        result = _run_git(
            ["merge", "--ff-only", integration_branch],
            self.project_path,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"fast-forward of main to {integration_branch} failed:\n"
                f"{result.stderr.strip()}\n"
                "This should not happen if integration branch was created correctly."
            )

        # Clean up integration branch
        _run_git(["branch", "-d", integration_branch], self.project_path, check=False)
        log.info("main fast-forwarded. Integration branch deleted.")

    # ------------------------------------------------------------------
    # Legacy merge_all (kept for tests that don't use integration branch)
    # ------------------------------------------------------------------

    def merge_all(self, main_branch: Optional[str] = None) -> str:
        """
        Direct merge of all worker branches into main (legacy, no integration branch).
        Use merge_via_integration() for production.
        Returns the final commit SHA on the main branch.
        """
        if main_branch is None:
            res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.project_path)
            main_branch = res.stdout.strip()

        sorted_briefs = sorted(self.state.briefs, key=lambda b: b.priority)

        for brief in sorted_briefs:
            if brief.status != "done":
                log.warning("Skipping merge for %s (status=%s)", brief.worker_name, brief.status)
                continue

            log.info("Merging %s → %s", brief.branch_name, main_branch)
            result = _run_git(
                ["merge", "--no-ff", brief.branch_name,
                 "-m", f"merge({brief.worker_name}): {brief.task_description[:60]}"],
                self.project_path,
                check=False,
            )

            if result.returncode != 0:
                _run_git(["merge", "--abort"], self.project_path, check=False)
                conflicting = self._get_conflict_files()
                raise RuntimeError(
                    f"Merge conflict when integrating {brief.branch_name} → {main_branch}.\n"
                    f"Conflicting files: {', '.join(conflicting) or 'unknown'}\n"
                    "Merge aborted. Manual resolution required."
                )

        sha_res = _run_git(["rev-parse", "HEAD"], self.project_path)
        return sha_res.stdout.strip()

    # ------------------------------------------------------------------
    # GC — review point #4
    # ------------------------------------------------------------------

    def prune_stale(self) -> None:
        """
        Run `git worktree prune` on startup to remove stale worktree refs.
        Called by Coordinator._run_graph() before entering the graph.
        """
        _run_git(["worktree", "prune"], self.project_path, check=False)
        log.debug("Pruned stale worktree refs")

    def cleanup_all_stale(self, older_than_days: int = 7) -> list[str]:
        """
        Remove ALL swarm sibling directories older than `older_than_days` days.
        Only touches directories matching the SWARM_DIR_SUFFIX pattern.
        Returns list of paths removed.

        Safe to call at any time — won't touch the current task's worktrees.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        parent = self.project_path.parent
        removed: list[str] = []

        for entry in parent.iterdir():
            if not entry.is_dir():
                continue
            if SWARM_DIR_SUFFIX not in entry.name:
                continue
            if entry == self.swarm_root:
                continue  # don't remove current task

            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed.append(str(entry))
                    log.info("GC: removed stale swarm dir %s (mtime=%s)", entry.name, mtime.date())
            except Exception as exc:
                log.warning("GC: could not remove %s: %s", entry, exc)

        # Prune worktree refs after removing directories
        _run_git(["worktree", "prune"], self.project_path, check=False)
        return removed

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, force: bool = True) -> None:
        """
        Remove all git worktrees for this task and delete the sibling directory.
        Call after successful merge (or on task cancellation).
        Note: on failure, worktrees are preserved for post-mortem debugging.
        """
        self._remove_integration_worktree()
        for brief in self.state.briefs:
            wt_path = self.worktree_path(brief)
            if wt_path.exists():
                _run_git(
                    ["worktree", "remove", str(wt_path), "--force" if force else ""],
                    self.project_path,
                    check=False,
                )
                log.info("Removed worktree: %s", wt_path)

        if self.swarm_root.exists():
            shutil.rmtree(self.swarm_root, ignore_errors=True)
            log.info("Removed swarm root: %s", self.swarm_root)

        _run_git(["worktree", "prune"], self.project_path, check=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_conflict_files(self, cwd: Optional[Path] = None) -> list[str]:
        res = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd or self.project_path, check=False)
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def list_worktrees(self) -> list[dict]:
        res = _run_git(["worktree", "list", "--porcelain"], self.project_path, check=False)
        worktrees: list[dict] = []
        current: dict = {}
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[len("worktree "):].strip()}
            elif line.startswith("branch "):
                current["branch"] = line[len("branch "):].strip()
            elif line.startswith("HEAD "):
                current["sha"] = line[len("HEAD "):].strip()[:8]
        if current:
            worktrees.append(current)
        return worktrees

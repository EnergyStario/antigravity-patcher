"""
source/patcher/swarm/state.py
SwarmState dataclasses + checkpoint I/O.

Design decisions (see AGENT_SYSTEM_ARCHITECTURE.md §6, §8.3):
  - State stored at ~/.antigravity_tools/swarm/{task_id}/state.json
  - NOT inside the project → no git pollution, no worktree duplication
  - project_path field inside state → cross-machine resume possible
  - Atomic write (temp file + replace) to survive mid-write crashes
  - All times as ISO-8601 strings (JSON-safe, human-readable)
"""

from __future__ import annotations

import json
import os
import uuid
import tempfile
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Checkpoint directory
# ---------------------------------------------------------------------------

def _swarm_dir() -> Path:
    """Returns ~/.antigravity_tools/swarm/ — created on first access."""
    base = Path(os.environ.get("AG_TOOLS_DIR", Path.home() / ".antigravity_tools"))
    d = base / "swarm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _task_dir(task_id: str) -> Path:
    d = _swarm_dir() / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WorkerBrief:
    """
    Assignment given to a single Worker agent.

    Rules (from architecture §6):
      - owned_files: ONLY these files may be modified. Touching others is forbidden.
      - priority: merge order (lower = merged first). Set by Orchestrator before EXECUTE.
      - branch_name: Git branch isolated per worker via git worktree.
      - test_command: command Verifier runs to validate this worker's output.
      - worker_type: "code" | "content" | "research" | "docs"
      - model: always "flash" for workers (Tier-2). Orchestrator is Tier-1.
    """
    worker_name: str                      # e.g. "worker-1"
    worker_type: str                      # "code" | "content" | "research" | "docs"
    owned_files: list[str]               # relative paths from project root
    task_description: str
    branch_name: str                      # "feat/worker-1-{task_id[:8]}"
    test_command: str                     # e.g. "python -m pytest tests/test_xxx.py -v"
    model: str = "flash"                  # Tier-2 always for workers
    priority: int = 0                     # merge order
    worktree_path: str = ""               # absolute path, set by WorktreeManager
    status: str = "pending"               # "pending" | "running" | "done" | "failed"
    attempt_count: int = 0               # max 3 (architecture §7 VERIFY node)
    commit_sha: str = ""                  # filled after worker commits
    started_at: str = ""                  # ISO-8601 when worker entered "running"
    finished_at: str = ""                 # ISO-8601 when worker completed/failed


@dataclass
class WorkerResult:
    """What a Worker reports back after completing its brief."""
    worker_name: str
    success: bool
    commit_sha: str = ""
    files_changed: list[str] = field(default_factory=list)
    error_message: str = ""
    duration_seconds: float = 0.0


@dataclass
class VerificationResult:
    """What Verifier reports after running test_command for a worker."""
    worker_name: str
    passed: bool
    test_output: str = ""
    failed_tests: list[str] = field(default_factory=list)
    failed_source_files: list[str] = field(default_factory=list)  # source .py paths from tracebacks
    error_message: str = ""


@dataclass
class SwarmState:
    """
    Central state object that travels through the DECOMPOSE→VALIDATE→EXECUTE→VERIFY→MERGE→REPORT graph.

    Checkpoint: saved to ~/.antigravity_tools/swarm/{task_id}/state.json after every node.
    Resume: on "continue" message, coordinator loads from last checkpoint.
    """
    task_id: str
    user_request: str
    project_path: str                    # absolute path → cross-machine resume
    created_at: str                      # ISO-8601

    # Decomposition output
    file_tree: list[str] = field(default_factory=list)
    briefs: list[WorkerBrief] = field(default_factory=list)

    # Execution progress
    worker_results: dict[str, WorkerResult] = field(default_factory=dict)

    # Verification progress
    verification_results: dict[str, VerificationResult] = field(default_factory=dict)

    # Phase tracking (for tasks > MAX_WORKERS)
    current_phase: int = 1
    total_phases: int = 1

    # Outcome
    final_branch: str = ""
    is_complete: bool = False
    errors: list[str] = field(default_factory=list)

    # Graph cursor — which node we're currently at
    current_node: str = "DECOMPOSE"      # DECOMPOSE|VALIDATE|EXECUTE|VERIFY|MERGE|REPORT|DONE

    # Failover metadata (from quota_engine)
    orchestrator_model: str = ""
    failover_count: int = 0
    max_workers: int = 5                 # configurable, max 10 (architecture Q4)
    max_attempts: int = 3               # max retry per worker (architecture §7)

    # Integration-fix owner (architecture Q7 — multi-file bug)
    integration_fix_owner: str = ""     # worker_name assigned to cross-file fix

    @classmethod
    def new(cls, user_request: str, project_path: str, **kwargs) -> "SwarmState":
        """Factory: creates a fresh SwarmState with a new UUID task_id."""
        return cls(
            task_id=str(uuid.uuid4()),
            user_request=user_request,
            project_path=str(Path(project_path).resolve()),
            created_at=datetime.now(timezone.utc).isoformat(),
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Serialization helpers (dataclass ↔ JSON)
# ---------------------------------------------------------------------------

def _brief_to_dict(b: WorkerBrief) -> dict:
    return asdict(b)


def _brief_from_dict(d: dict) -> WorkerBrief:
    return WorkerBrief(**{k: v for k, v in d.items() if k in WorkerBrief.__dataclass_fields__})


def _worker_result_from_dict(d: dict) -> WorkerResult:
    return WorkerResult(**{k: v for k, v in d.items() if k in WorkerResult.__dataclass_fields__})


def _verification_result_from_dict(d: dict) -> VerificationResult:
    return VerificationResult(**{k: v for k, v in d.items() if k in VerificationResult.__dataclass_fields__})


def _state_to_dict(state: SwarmState) -> dict:
    d = asdict(state)
    # briefs is list[WorkerBrief] → already handled by asdict
    return d


def _state_from_dict(d: dict) -> SwarmState:
    # Reconstruct nested dataclasses
    briefs = [_brief_from_dict(b) for b in d.pop("briefs", [])]
    worker_results = {k: _worker_result_from_dict(v) for k, v in d.pop("worker_results", {}).items()}
    verification_results = {k: _verification_result_from_dict(v) for k, v in d.pop("verification_results", {}).items()}
    # Remove unknown keys for forward-compat
    known = SwarmState.__dataclass_fields__
    filtered = {k: v for k, v in d.items() if k in known}
    state = SwarmState(**filtered)
    state.briefs = briefs
    state.worker_results = worker_results
    state.verification_results = verification_results
    return state


# ---------------------------------------------------------------------------
# Checkpoint I/O — atomic writes
# ---------------------------------------------------------------------------

def save_checkpoint(state: SwarmState) -> Path:
    """
    Atomically write state to ~/.antigravity_tools/swarm/{task_id}/state.json.
    Uses temp-file-then-replace to survive crashes mid-write.
    Returns the path written.
    """
    task_d = _task_dir(state.task_id)
    target = task_d / "state.json"

    payload = _state_to_dict(state)
    payload["_checkpoint_at"] = datetime.now(timezone.utc).isoformat()

    # Atomic write: write to .tmp, then replace
    fd, tmp_path = tempfile.mkstemp(dir=task_d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        shutil.move(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return target


def load_checkpoint(task_id: str) -> Optional[SwarmState]:
    """
    Load state from ~/.antigravity_tools/swarm/{task_id}/state.json.
    Returns None if file doesn't exist or is corrupt.
    """
    target = _swarm_dir() / task_id / "state.json"
    if not target.exists():
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.pop("_checkpoint_at", None)
        return _state_from_dict(d)
    except Exception:
        return None


def list_checkpoints() -> list[dict]:
    """
    Returns summary of all saved swarm tasks.
    Useful for "resume" UI — user picks which task to continue.
    """
    swarm_d = _swarm_dir()
    results = []
    for task_dir in sorted(swarm_d.iterdir(), reverse=True):
        state_file = task_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            results.append({
                "task_id": d.get("task_id", task_dir.name),
                "user_request": d.get("user_request", "")[:80],
                "project_path": d.get("project_path", ""),
                "current_node": d.get("current_node", "?"),
                "is_complete": d.get("is_complete", False),
                "created_at": d.get("created_at", ""),
                "checkpoint_at": d.get("_checkpoint_at", ""),
            })
        except Exception:
            continue
    return results


def delete_checkpoint(task_id: str) -> bool:
    """Remove all checkpoint files for a task. Returns True if deleted."""
    task_d = _swarm_dir() / task_id
    if task_d.exists():
        shutil.rmtree(task_d, ignore_errors=True)
        return True
    return False


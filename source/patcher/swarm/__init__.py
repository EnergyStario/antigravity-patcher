"""
source/patcher/swarm/__init__.py
Multi-Agent Swarm Engine — public API surface.

Usage from coordinator (or IDE extension via IPC):
    from source.patcher.swarm import Coordinator
    coord = Coordinator(project_path="/path/to/repo")
    result = coord.run("add proxy support to all patchers")
"""

from .coordinator import Coordinator
from .state import SwarmState, WorkerBrief, WorkerResult, VerificationResult
from .decomposer import decompose_task
from .worktree_manager import WorktreeManager
from .verifier import Verifier
from .affected_tests import AffectedTestDetector

__all__ = [
    "Coordinator",
    "SwarmState",
    "WorkerBrief",
    "WorkerResult",
    "VerificationResult",
    "decompose_task",
    "WorktreeManager",
    "Verifier",
    "AffectedTestDetector",
]


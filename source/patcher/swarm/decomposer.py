"""
source/patcher/swarm/decomposer.py
Task decomposition: turns a user request + file tree into a list of WorkerBrief-s.

Design (see AGENT_SYSTEM_ARCHITECTURE.md §7, §8.1):
  - Called by Orchestrator (Tier-1 model) in DECOMPOSE node.
  - Output: validated WorkerBrief list with zero file-ownership conflicts.
  - If conflicts detected → retry DECOMPOSE (max 3 attempts) before aborting.
  - Tasks > MAX_WORKERS → split into phases. Each phase runs fully before the next.
  - Non-code workers: "one worker = one output artifact" rule instead of file rule.
  - All string building is pure Python — no LLM calls here. The LLM fills in the
    task_description; this module validates and structures the output.
"""

from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Optional

from .state import WorkerBrief, SwarmState

log = logging.getLogger(__name__)

# Architecture §9 Q4: configurable max, hard cap 10
MAX_WORKERS_HARD_CAP = 10

# Worker types recognized by the system
VALID_WORKER_TYPES = {"code", "content", "research", "docs"}

# Default test command fallback if Orchestrator didn't specify one
DEFAULT_TEST_CMD = "python -m pytest tests/ -v --tb=short"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class DecompositionError(ValueError):
    """Raised when decomposition produces an invalid or conflicting brief set."""


def _validate_no_file_conflicts(briefs: list[WorkerBrief]) -> list[str]:
    """
    Implements the "one worker = one file" anti-conflict rule (architecture §8.1).
    Returns list of conflict descriptions (empty = no conflicts).
    """
    file_to_worker: dict[str, str] = {}
    conflicts: list[str] = []

    for brief in briefs:
        for f in brief.owned_files:
            norm = f.strip().replace("\\", "/")
            if norm in file_to_worker:
                owner = file_to_worker[norm]
                if owner != brief.worker_name:
                    conflicts.append(
                        f"File '{norm}' claimed by both '{owner}' and '{brief.worker_name}'"
                    )
            else:
                file_to_worker[norm] = brief.worker_name

    return conflicts


def _validate_briefs(briefs: list[WorkerBrief], max_workers: int) -> list[str]:
    """Full validation pass. Returns list of error strings."""
    errors: list[str] = []

    if not briefs:
        errors.append("No WorkerBrief-s produced — decomposition returned empty list")
        return errors

    if len(briefs) > max_workers:
        errors.append(
            f"Too many briefs ({len(briefs)}) for single phase "
            f"(max_workers={max_workers}). Should be split into phases."
        )

    names_seen: set[str] = set()
    for b in briefs:
        # Unique worker names
        if b.worker_name in names_seen:
            errors.append(f"Duplicate worker_name: '{b.worker_name}'")
        names_seen.add(b.worker_name)

        # Worker type
        if b.worker_type not in VALID_WORKER_TYPES:
            errors.append(
                f"Worker '{b.worker_name}' has unknown worker_type '{b.worker_type}'. "
                f"Must be one of: {sorted(VALID_WORKER_TYPES)}"
            )

        # Branch name safety (no spaces, no special chars other than -/_)
        if not re.match(r'^[a-zA-Z0-9/_-]+$', b.branch_name):
            errors.append(
                f"Worker '{b.worker_name}' has unsafe branch_name '{b.branch_name}'. "
                "Only a-z, A-Z, 0-9, /, -, _ are allowed."
            )

        # Code workers must own at least one file
        if b.worker_type == "code" and not b.owned_files:
            errors.append(f"Code worker '{b.worker_name}' has no owned_files")

        # Must have a task description
        if not b.task_description.strip():
            errors.append(f"Worker '{b.worker_name}' has empty task_description")

    # File conflict check
    file_conflicts = _validate_no_file_conflicts(briefs)
    errors.extend(file_conflicts)

    return errors


# ---------------------------------------------------------------------------
# Phase splitting
# ---------------------------------------------------------------------------

def split_into_phases(briefs: list[WorkerBrief], max_workers: int) -> list[list[WorkerBrief]]:
    """
    Split a large brief list into phases of at most max_workers each.
    Priority within each phase is re-numbered from 0.
    Architecture §9 Q5: tasks > MAX_WORKERS → phase-by-phase execution.
    """
    phases: list[list[WorkerBrief]] = []
    # Sort by priority first so dependencies are respected across phases
    sorted_briefs = sorted(briefs, key=lambda b: b.priority)
    for i in range(0, len(sorted_briefs), max_workers):
        chunk = sorted_briefs[i: i + max_workers]
        # Re-number priority within phase
        for j, b in enumerate(chunk):
            b.priority = j
        phases.append(chunk)
    return phases


def reconstruct_phases(briefs: list[WorkerBrief], max_workers: int) -> list[list[WorkerBrief]]:
    """Reconstruct phase chunks from already-ordered state.briefs without re-sorting."""
    if not briefs:
        return []
    cap = max(1, min(max_workers, MAX_WORKERS_HARD_CAP))
    return [briefs[i: i + cap] for i in range(0, len(briefs), cap)]


# ---------------------------------------------------------------------------
# Brief construction helpers
# ---------------------------------------------------------------------------

def make_branch_name(worker_name: str, task_id: str) -> str:
    """Generate a safe git branch name: feat/{worker_name}-{task_id[:8]}."""
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', worker_name)
    return f"feat/{safe_name}-{task_id[:8]}"


def build_worker_brief(
    *,
    worker_name: str,
    task_id: str,
    task_description: str,
    owned_files: list[str],
    worker_type: str = "code",
    test_command: str = "",
    priority: int = 0,
    model: str = "flash",
) -> WorkerBrief:
    """
    Construct a WorkerBrief with auto-generated branch name and defaults.
    Use this factory from Orchestrator LLM output parsing code.
    """
    return WorkerBrief(
        worker_name=worker_name,
        worker_type=worker_type,
        owned_files=[f.strip().replace("\\", "/") for f in owned_files],
        task_description=task_description,
        branch_name=make_branch_name(worker_name, task_id),
        test_command=test_command or DEFAULT_TEST_CMD,
        model=model,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# decompose_task — main entry point
# ---------------------------------------------------------------------------

def decompose_task(
    state: SwarmState,
    orchestrator_output: list[dict],
    *,
    max_workers: Optional[int] = None,
) -> tuple[list[WorkerBrief], list[list[WorkerBrief]]]:
    """
    Parse and validate Orchestrator's decomposition output.

    Args:
        state: current SwarmState (task_id used for branch names)
        orchestrator_output: list of dicts from LLM, each representing one WorkerBrief.
            Expected keys: worker_name, worker_type, owned_files, task_description,
                           test_command (optional), priority (optional)
        max_workers: max per phase. Defaults to state.max_workers.

    Returns:
        (all_briefs, phases_list)
        - all_briefs: flat validated list of WorkerBrief-s
        - phases_list: split into phase groups (len==1 if fits in one phase)

    Raises:
        DecompositionError: if validation fails (caller should retry DECOMPOSE node)
    """
    _max = min(max_workers or state.max_workers, MAX_WORKERS_HARD_CAP)

    briefs: list[WorkerBrief] = []
    for i, item in enumerate(orchestrator_output):
        try:
            brief = build_worker_brief(
                worker_name=item.get("worker_name", f"worker-{i + 1}"),
                task_id=state.task_id,
                task_description=item.get("task_description", ""),
                owned_files=item.get("owned_files", []),
                worker_type=item.get("worker_type", "code"),
                test_command=item.get("test_command", ""),
                priority=int(item.get("priority", i)),
                model=item.get("model", "flash"),
            )
            briefs.append(brief)
        except Exception as exc:
            raise DecompositionError(f"Failed to build brief for item {i}: {exc}") from exc

    # Validation pass (without phase-size check if we plan to split)
    # First check everything except count
    temp = [b for b in briefs]
    errors = _validate_briefs(temp, max_workers=len(briefs) + 1)  # skip count error here
    if errors:
        raise DecompositionError(
            f"WorkerBrief validation failed ({len(errors)} error(s)):\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    # Phase splitting (architecture §9 Q5)
    phases = split_into_phases(briefs, _max)

    log.info(
        "Decomposed into %d brief(s) across %d phase(s) (max_workers=%d)",
        len(briefs), len(phases), _max,
    )
    for idx, phase in enumerate(phases):
        log.info("  Phase %d: %s", idx + 1, [b.worker_name for b in phase])

    return briefs, phases


# ---------------------------------------------------------------------------
# Orchestrator prompt builder (pure string — no LLM calls)
# ---------------------------------------------------------------------------

def build_directory_summary(
    project_path: str | Path,
    *,
    max_depth: int = 3,
    exclude_dirs: Optional[set[str]] = None,
) -> str:
    """
    Build a 2-level directory tree with per-directory file counts.

    Used by build_orchestrator_prompt() as a compact overview before listing
    individual files, so the LLM understands the project layout without being
    overwhelmed by hundreds of raw paths.

    Args:
        project_path: root of the git repository
        max_depth: how many levels of directories to render (default 3)
        exclude_dirs: directory names to skip (default: common junk dirs)
    """
    _exclude = exclude_dirs or {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "dist", "build", ".mypy_cache", ".pytest_cache", ".tox",
    }
    root = Path(project_path)

    def _count_files(path: Path) -> int:
        """Count files recursively under path, skipping excluded dirs."""
        total = 0
        try:
            for entry in path.iterdir():
                if entry.is_dir():
                    if entry.name not in _exclude and "-swarm-" not in entry.name:
                        total += _count_files(entry)
                elif entry.is_file():
                    total += 1
        except PermissionError:
            pass
        return total

    def _render(path: Path, depth: int, prefix: str) -> list[str]:
        """Recursively render directory tree lines up to max_depth."""
        if depth > max_depth:
            return []
        lines: list[str] = []
        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            return []
        dirs = [
            e for e in entries
            if e.is_dir()
            and e.name not in _exclude
            and "-swarm-" not in e.name
        ]
        for i, d in enumerate(dirs):
            connector = "└── " if i == len(dirs) - 1 else "├── "
            child_prefix = prefix + ("    " if i == len(dirs) - 1 else "│   ")
            count = _count_files(d)
            lines.append(f"{prefix}{connector}{d.name}/ ({count} files)")
            lines.extend(_render(d, depth + 1, child_prefix))
        return lines

    lines = [f"{root.name}/"]
    lines.extend(_render(root, 1, ""))
    return "\n".join(lines)


def build_orchestrator_prompt(state: SwarmState, *, use_directory_summary: bool = True) -> str:
    """
    Build the system prompt sent to the Orchestrator (Tier-1 model) for DECOMPOSE node.
    Returns a string the caller feeds to the LLM.

    The LLM must respond with a JSON array of WorkerBrief dicts.

    Args:
        state: current SwarmState (must have file_tree and project_path populated)
        use_directory_summary: if True (default), prepend a compact directory tree
            overview before listing individual files. Helps the LLM understand project
            layout when file_tree has hundreds of entries (review improvement #7).
    """
    # Build the file section: directory overview + first 200 files (review #7)
    if use_directory_summary and state.project_path:
        try:
            dir_summary = build_directory_summary(state.project_path)
        except Exception:
            dir_summary = ""
        # Show at most 200 individual files to avoid context bloat
        files = state.file_tree[:200]
        file_list_str = "\n".join(f"  {f}" for f in files) or "  (file tree not provided)"
        truncation_note = ""
        if len(state.file_tree) > 200:
            truncation_note = f"\n  ... ({len(state.file_tree) - 200} more files not shown)"
        file_tree_section = (
            f"### Directory overview\n{dir_summary}\n\n"
            f"### File list (first {min(len(state.file_tree), 200)} of {len(state.file_tree)} files)\n"
            f"{file_list_str}{truncation_note}"
        )
    else:
        file_tree_str = "\n".join(f"  {f}" for f in state.file_tree) or "  (file tree not provided)"
        file_tree_section = file_tree_str

    return f"""You are the Orchestrator in a Multi-Agent Swarm system.

## Your task
Decompose the following user request into independent parallel subtasks.
Each subtask will be executed by a separate Worker agent in an isolated git branch.

## Critical rules
1. **One worker = one set of files.** No two workers may touch the same file.
2. Each worker gets exactly the files it needs — no more.
3. Workers are executed in parallel. Order-dependent changes must go to ONE worker.
4. Set `priority` (integer): lower value = merged first into main branch.
5. `test_command`: shell command the Verifier will run after this worker finishes.
   Use pytest path targeting the relevant test file, e.g.: `python -m pytest tests/test_xxx.py -v`
6. `worker_type` must be one of: {sorted(VALID_WORKER_TYPES)}
7. `model` must always be `"flash"` — workers always use the fast model.
8. Max {state.max_workers} workers per phase. If more needed, only return the first phase.

## Project info
- **User request:** {state.user_request}
- **Project path:** {state.project_path}
- **File tree:**
{file_tree_section}

## Response format
Respond with ONLY a valid JSON array. No prose, no markdown, no code blocks.
Example:
[
  {{
    "worker_name": "worker-1",
    "worker_type": "code",
    "owned_files": ["source/patcher/ide/patcher.py"],
    "task_description": "Add proxy_url parameter to IDEPatcher.patch() method",
    "test_command": "python -m pytest tests/test_ide_patcher.py -v",
    "priority": 0
  }},
  {{
    "worker_name": "worker-2",
    "worker_type": "code",
    "owned_files": ["source/patcher/manager/patcher.py"],
    "task_description": "Add proxy_url parameter to ManagerPatcher.patch() method",
    "test_command": "python -m pytest tests/test_manager_patcher.py -v",
    "priority": 1
  }}
]"""


# ---------------------------------------------------------------------------
# File tree builder (helper for state.file_tree)
# ---------------------------------------------------------------------------

def build_file_tree(
    project_path: str | Path,
    *,
    max_files: int = 500,
    exclude_dirs: Optional[set[str]] = None,
    include_exts: Optional[set[str]] = None,
) -> list[str]:
    """
    Walk project_path and return a list of relative file paths.
    Used to populate state.file_tree before calling Orchestrator.

    Args:
        project_path: root of the git repository
        max_files: cap to avoid overwhelming the LLM context
        exclude_dirs: directory names to skip (default: common junk dirs)
        include_exts: if set, only include files with these extensions
    """
    _exclude = exclude_dirs or {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "dist", "build", ".mypy_cache", ".pytest_cache", ".tox",
        "swarm-*",  # our own worktree sibling dirs
    }
    root = Path(project_path)
    files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in-place (affects os.walk).
        # Also skip any dir whose name contains "-swarm-" (our worktree siblings).
        dirnames[:] = [
            d for d in dirnames
            if d not in _exclude
            and "-swarm-" not in d
            and not any(
                d.startswith(prefix.rstrip("*")) for prefix in _exclude if "*" in prefix
            )
        ]

        for fname in filenames:
            if include_exts and Path(fname).suffix not in include_exts:
                continue
            rel = Path(dirpath).relative_to(root) / fname
            files.append(str(rel).replace("\\", "/"))

            if len(files) >= max_files:
                log.warning("File tree capped at %d files", max_files)
                return sorted(files)

    return sorted(files)

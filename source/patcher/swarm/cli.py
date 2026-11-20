"""
source/patcher/swarm/cli.py
Command-line interface for the Multi-Agent Swarm Engine.

Allows ANY Antigravity surface (Antigravity 2.0 Desktop Chat, Antigravity IDE Chat,
agy CLI, or extension.js) to drive the Swarm lifecycle using deterministic Python
checkpointing, git worktrees, AST affected-test narrowing, and integration-branch
merges — while Antigravity itself executes the intelligent Orchestrator/Worker roles.

Usage:
    python -m swarm.cli prepare --project . --request "Add proxy support"
    python -m swarm.cli setup   --task-id <id> --briefs briefs.json
    python -m swarm.cli mark-worker --task-id <id> --worker worker-1 --success --commit
    python -m swarm.cli verify  --task-id <id> --affected
    python -m swarm.cli merge   --task-id <id> [--test-cmd "python -m pytest tests/ -v"]
    python -m swarm.cli status  [--task-id <id>]
    python -m swarm.cli resume  --task-id <id>
    python -m swarm.cli cleanup [--task-id <id>] [--project . --stale-days 7]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .decomposer import (
    build_file_tree,
    build_orchestrator_prompt,
    decompose_task,
    reconstruct_phases,
    DecompositionError,
)
from .state import (
    SwarmState,
    WorkerResult,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)
from .verifier import Verifier, detect_cross_worker_conflict
from .worktree_manager import WorktreeManager

log = logging.getLogger(__name__)


def _ag_tools_dir() -> Path:
    return Path(os.environ.get("AG_TOOLS_DIR", Path.home() / ".antigravity_tools"))


def _load_model_config() -> dict:
    cfg_path = _ag_tools_dir() / "model_config.json"
    if not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def _load_or_fail(task_id: str) -> SwarmState:
    state = load_checkpoint(task_id)
    if state is None:
        raise FileNotFoundError(f"Checkpoint not found for task_id={task_id!r}")
    return state


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_duration_seconds(created_at: Optional[str]) -> float:
    """Return non-negative elapsed seconds from ISO-8601 created_at to now (UTC)."""
    if not created_at or not isinstance(created_at, str):
        return 0.0
    try:
        started_at = datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    return max(0.0, round(elapsed, 3))


def cmd_prepare(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    state = SwarmState.new(
        user_request=args.request,
        project_path=str(project),
        max_workers=min(max(1, args.max_workers), 10),
        max_attempts=max(1, args.max_attempts),
        orchestrator_model=args.orchestrator_model or "",
    )
    state.file_tree = build_file_tree(project)
    ckpt = save_checkpoint(state)
    prompt = build_orchestrator_prompt(state)

    _out({
        "ok": True,
        "task_id": state.task_id,
        "project_path": state.project_path,
        "file_count": len(state.file_tree),
        "use_affected_tests": bool(getattr(args, "use_affected_tests", False)),
        "checkpoint_file": str(ckpt),
        "model_config": _load_model_config(),
        "prompt": prompt,
    })
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    state = _load_or_fail(args.task_id)

    raw_src = args.briefs
    if raw_src == "-":
        raw_text = sys.stdin.read()
    elif Path(raw_src).is_file():
        raw_text = Path(raw_src).read_text(encoding="utf-8")
    else:
        raw_text = raw_src

    try:
        raw_briefs = json.loads(raw_text)
        _all, phases = decompose_task(state, raw_briefs)
    except (json.JSONDecodeError, DecompositionError, TypeError) as exc:
        _out({"ok": False, "task_id": state.task_id, "error": str(exc)})
        return 1

    state.briefs = [b for ph in phases for b in ph]
    state.total_phases = len(phases)
    state.current_phase = 1
    state.current_node = "EXECUTE"

    wm = WorktreeManager(state)
    wm.prune_stale()
    wm.setup()
    save_checkpoint(state)

    _out({
        "ok": True,
        "task_id": state.task_id,
        "total_phases": state.total_phases,
        "current_phase": state.current_phase,
        "briefs": [asdict(b) for b in state.briefs],
    })
    return 0


def cmd_mark_worker(args: argparse.Namespace) -> int:
    state = _load_or_fail(args.task_id)
    brief = next((b for b in state.briefs if b.worker_name == args.worker), None)
    if brief is None:
        _out({"ok": False, "task_id": state.task_id, "error": f"Worker {args.worker!r} not found"})
        return 1

    explicit_status = str(getattr(args, "status", "") or "").strip().lower()
    if explicit_status == "running":
        brief.status = "running"
        if not brief.started_at:
            brief.started_at = _utc_now_iso()
        brief.finished_at = ""
        save_checkpoint(state)
        _out({
            "ok": True,
            "task_id": state.task_id,
            "worker_name": brief.worker_name,
            "status": brief.status,
            "started_at": brief.started_at,
        })
        return 0
    if explicit_status == "pending":
        brief.status = "pending"
        save_checkpoint(state)
        _out({
            "ok": True,
            "task_id": state.task_id,
            "worker_name": brief.worker_name,
            "status": brief.status,
        })
        return 0

    is_success = (explicit_status == "done") if explicit_status in ("done", "failed") else bool(args.success)
    commit_sha = args.commit_sha or brief.commit_sha or ""
    if args.commit and is_success:
        wm = WorktreeManager(state)
        msg = args.message or f"swarm({brief.worker_name}): {brief.task_description[:60]}"
        commit_sha = wm.commit_worker_work(brief, msg)

    duration_seconds = _compute_duration_seconds(brief.started_at or state.created_at)
    brief.status = "done" if is_success else "failed"
    brief.finished_at = _utc_now_iso()
    brief.commit_sha = commit_sha
    state.worker_results[brief.worker_name] = WorkerResult(
        worker_name=brief.worker_name,
        success=is_success,
        commit_sha=commit_sha,
        files_changed=list(args.files or brief.owned_files),
        error_message=args.error or "",
        duration_seconds=duration_seconds,
    )
    save_checkpoint(state)

    _out({
        "ok": True,
        "task_id": state.task_id,
        "worker_name": brief.worker_name,
        "status": brief.status,
        "commit_sha": commit_sha,
        "duration_seconds": duration_seconds,
        "finished_at": brief.finished_at,
    })
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    state = _load_or_fail(args.task_id)
    phases = reconstruct_phases(state.briefs, state.max_workers)
    idx = state.current_phase - 1
    phase_briefs = phases[idx] if 0 <= idx < len(phases) else state.briefs

    if args.worker:
        targets = [b for b in phase_briefs if b.worker_name == args.worker]
    else:
        targets = [b for b in phase_briefs if b.status == "done"]

    verifier = Verifier(state.project_path)
    results = verifier.verify_all(targets, use_affected=bool(args.affected))

    conflicts: dict[str, Optional[str]] = {}
    for name, vr in results.items():
        state.verification_results[name] = vr
        if not vr.passed:
            conflicts[name] = detect_cross_worker_conflict(vr, state.briefs)

    all_passed = all(vr.passed for vr in results.values()) if results else False
    if all_passed:
        if state.current_phase < state.total_phases:
            state.current_phase += 1
            state.current_node = "EXECUTE"
        else:
            state.current_node = "MERGE"

    save_checkpoint(state)
    _out({
        "ok": True,
        "task_id": state.task_id,
        "passed": all_passed,
        "next_node": state.current_node,
        "results": {k: asdict(v) for k, v in results.items()},
        "cross_worker_conflicts": conflicts,
    })
    return 0 if all_passed else 2


def cmd_merge(args: argparse.Namespace) -> int:
    state = _load_or_fail(args.task_id)
    wm = WorktreeManager(state)
    verifier = Verifier(state.project_path)

    merged_sha = ""
    try:
        merged_sha = wm.merge_via_integration(task_id=state.task_id)
        state.final_branch = merged_sha
    except RuntimeError as exc:
        state.errors.append(f"MERGE conflict: {exc}")
        if not args.no_cleanup:
            try:
                wm.cleanup()
            except Exception:
                pass
        save_checkpoint(state)
        _out({"ok": False, "task_id": state.task_id, "merged": False, "error": str(exc)})
        return 1

    integ_wt = wm.integration_worktree_path(state.task_id)
    full_res = verifier.run_full_suite(args.test_cmd, cwd=integ_wt if integ_wt.is_dir() else None)
    state.verification_results["__full_suite__"] = full_res

    fast_forwarded = False
    if full_res.passed:
        try:
            wm.fast_forward_main(task_id=state.task_id)
            fast_forwarded = True
            state.errors.clear()
            state.is_complete = True
            state.current_node = "DONE"
        except RuntimeError as exc:
            state.errors.append(f"MERGE fast-forward failed: {exc}")
    else:
        state.errors.append(
            f"Full suite FAILED on integration branch ({len(full_res.failed_tests)} failure(s)); main untouched."
        )

    if not args.no_cleanup:
        try:
            wm.cleanup()
        except Exception as exc:
            log.warning("Cleanup warning: %s", exc)

    save_checkpoint(state)
    _out({
        "ok": fast_forwarded,
        "task_id": state.task_id,
        "integration_sha": merged_sha,
        "full_suite_passed": full_res.passed,
        "fast_forwarded": fast_forwarded,
        "failed_tests": full_res.failed_tests,
        "errors": state.errors,
    })
    return 0 if fast_forwarded else 2


def cmd_status(args: argparse.Namespace) -> int:
    if args.task_id:
        state = load_checkpoint(args.task_id)
        if state is None:
            _out({"ok": False, "task_id": args.task_id, "error": "Task not found"})
            return 1
        _out({"ok": True, "task": asdict(state)})
        return 0

    _out({"ok": True, "tasks": list_checkpoints()})
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    state = _load_or_fail(args.task_id)
    for b in state.briefs:
        if b.status == "running":
            b.status = "pending"
    save_checkpoint(state)

    phases = reconstruct_phases(state.briefs, state.max_workers)
    idx = state.current_phase - 1
    phase_briefs = phases[idx] if 0 <= idx < len(phases) else state.briefs
    pending = [asdict(b) for b in phase_briefs if not (b.status == "done" and b.commit_sha)]

    _out({
        "ok": True,
        "task_id": state.task_id,
        "project_path": state.project_path,
        "current_node": state.current_node,
        "current_phase": state.current_phase,
        "total_phases": state.total_phases,
        "pending_workers": pending,
        "is_complete": state.is_complete,
    })
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    removed: list[str] = []
    if args.task_id:
        state = _load_or_fail(args.task_id)
        wm = WorktreeManager(state)
        wm.cleanup()
        removed.append(str(wm.swarm_root))
    else:
        project = Path(args.project or ".").resolve()
        dummy = SwarmState.new("cleanup", str(project))
        wm = WorktreeManager(dummy)
        wm.prune_stale()
        removed = wm.cleanup_all_stale(older_than_days=args.stale_days)

    _out({"ok": True, "removed": removed})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm.cli", description="Antigravity Swarm Engine CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("prepare", help="Create task checkpoint and build 2-pass orchestrator prompt")
    sp.add_argument("--project", default=".", help="Path to git repository")
    sp.add_argument("--request", required=True, help="User task description")
    sp.add_argument("--max-workers", type=int, default=5)
    sp.add_argument("--max-attempts", type=int, default=3)
    sp.add_argument("--orchestrator-model", default="")
    sp.add_argument("--use-affected-tests", action="store_true", help="Enable AST affected-test narrowing preference")
    sp.set_defaults(func=cmd_prepare)

    ss = sub.add_parser("setup", help="Validate WorkerBrief JSON and create git worktrees")
    ss.add_argument("--task-id", required=True)
    ss.add_argument("--briefs", required=True, help="Path to JSON file, raw JSON array, or '-' for stdin")
    ss.set_defaults(func=cmd_setup)

    smw = sub.add_parser("mark-worker", help="Record worker result (and optionally git commit in worktree)")
    smw.add_argument("--task-id", required=True)
    smw.add_argument("--worker", required=True)
    smw.add_argument("--status", choices=["pending", "running", "done", "failed"], default="", help="Set worker lifecycle status")
    smw.add_argument("--success", action="store_true", dest="success", default=True)
    smw.add_argument("--fail", action="store_false", dest="success")
    smw.add_argument("--commit", action="store_true", help="Run git commit in the worker worktree")
    smw.add_argument("--commit-sha", default="")
    smw.add_argument("--message", default="")
    smw.add_argument("--files", nargs="*", default=None)
    smw.add_argument("--error", default="")
    smw.set_defaults(func=cmd_mark_worker)

    sv = sub.add_parser("verify", help="Run Verifier (junitxml + optional affected-only narrowing)")
    sv.add_argument("--task-id", required=True)
    sv.add_argument("--affected", action="store_true", help="Only run tests affected by owned_files")
    sv.add_argument("--worker", default="", help="Verify only this worker")
    sv.set_defaults(func=cmd_verify)

    sm = sub.add_parser("merge", help="Merge via integration branch, run full suite, fast-forward main")
    sm.add_argument("--task-id", required=True)
    sm.add_argument("--test-cmd", default=None)
    sm.add_argument("--no-cleanup", action="store_true")
    sm.set_defaults(func=cmd_merge)

    sst = sub.add_parser("status", help="Show task status or list checkpoints")
    sst.add_argument("--task-id", default="")
    sst.set_defaults(func=cmd_status)

    sr = sub.add_parser("resume", help="Reset interrupted 'running' workers and show remaining work")
    sr.add_argument("--task-id", required=True)
    sr.set_defaults(func=cmd_resume)

    sc = sub.add_parser("cleanup", help="Cleanup task worktrees or stale sibling dirs")
    sc.add_argument("--task-id", default="")
    sc.add_argument("--project", default=".")
    sc.add_argument("--stale-days", type=int, default=7)
    sc.set_defaults(func=cmd_cleanup)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

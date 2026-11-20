"""
source/patcher/swarm/ipc_server.py
JSON-IPC bridge between Antigravity IDE extension (Node.js) and Coordinator (Python).

Protocol:
  IDE → Python (stdin, one JSON per line):
    {"cmd": "run",    "request": "...", "project_path": "...",
                      "max_workers": 5, "use_affected_tests": true}
    {"cmd": "resume", "task_id": "...", "project_path": "..."}
    {"cmd": "worker_result", "task_id": "...", "worker_name": "...",
                             "success": true, "commit_sha": "..."}
    {"cmd": "orchestrator_result", "task_id": "...", "briefs": [...]}
    {"cmd": "list_tasks"}
    {"cmd": "status", "task_id": "..."}
    {"cmd": "cleanup", "task_id": "...", "older_than_days": 7}
    {"cmd": "ping"}
    {"cmd": "shutdown"}

  Python → IDE (stdout, one JSON per line):
    {"event": "ready"}
    {"event": "started",    "task_id": "...", "project_path": "...", "user_request": "..."}
    {"event": "node_started", "task_id": "...", "node": "...", "phase": int, "total_phases": int}
    {"event": "orchestrator_prompt", "task_id": "...", "prompt": "..."}
    {"event": "worker_dispatched", "task_id": "...", "worker_name": "...",
                                   "task_description": "...", "owned_files": [...],
                                   "branch_name": "...", "worktree_path": "...",
                                   "test_command": "...", "model": "..."}
    {"event": "worker_done",    "task_id": "...", "worker_name": "...", "success": bool}
    {"event": "phase_verified", "task_id": "...", "phase": int, "passed": bool}
    {"event": "tasks_list",     "tasks": [...]}
    {"event": "task_status",    "task": {...} | null}
    {"event": "cleanup_done",   "removed": [...]}
    {"event": "done",   "task_id": "...", "is_complete": bool, "errors": [...], "final_branch": "..."}
    {"event": "error",  "task_id": "...", "message": "..."}
    {"event": "pong"}

Design:
  - One process per session, long-lived (IDE spawns once, reuses).
  - Worker execution is ASYNC from the IDE's perspective: Python sends
    "worker_dispatched" events, IDE runs agents and sends "worker_result" back.
  - Coordinator runs in a background thread; IPC server reads stdin in main thread.
  - Thread-safe stdout writes via threading.Lock and worker sync via queue.Queue.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from .coordinator import Coordinator
from .state import (
    SwarmState,
    WorkerBrief,
    WorkerResult,
    list_checkpoints,
    load_checkpoint,
)
from .worktree_manager import WorktreeManager

log = logging.getLogger(__name__)

_send_lock = threading.Lock()


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------

def _default_send(event: dict) -> None:
    """Write a JSON event to stdout atomically under a lock."""
    payload = json.dumps(event, ensure_ascii=False)
    with _send_lock:
        try:
            print(payload, flush=True)
        except Exception as exc:
            log.error("IPC send error: %s", exc)


def _recv_lines():
    """Generator: yield stripped lines from stdin until EOF."""
    for line in sys.stdin:
        line = line.strip()
        if line:
            yield line


# ---------------------------------------------------------------------------
# IDE-aware execute / orchestrator bridges
# ---------------------------------------------------------------------------

class _IDEWorkerBridge:
    """
    execute_worker_callback implementation that:
      1. Sends "worker_dispatched" event → IDE spawns/runs an AI agent
      2. Waits for "worker_result" to arrive on a per-worker queue
      3. Returns WorkerResult to Coordinator
    """

    def __init__(
        self,
        task_id_ref: list[str],
        result_queues: dict[str, queue.Queue],
        send_fn: Callable[[dict], None],
    ):
        self._task_id_ref = task_id_ref
        self._result_queues = result_queues
        self._send = send_fn

    def __call__(self, brief: WorkerBrief, worktree_path: str = "") -> WorkerResult:
        q: queue.Queue = queue.Queue()
        self._result_queues[brief.worker_name] = q

        self._send({
            "event": "worker_dispatched",
            "task_id": self._task_id_ref[0],
            "worker_name": brief.worker_name,
            "task_description": brief.task_description,
            "owned_files": brief.owned_files,
            "branch_name": brief.branch_name,
            "worktree_path": worktree_path or brief.worktree_path,
            "test_command": brief.test_command,
            "model": brief.model,
        })

        log.info("IPC: dispatched worker %s, waiting for result...", brief.worker_name)
        result: WorkerResult = q.get()
        log.info("IPC: received result for %s: success=%s", brief.worker_name, result.success)
        return result


class _IDEOrchestratorBridge:
    """
    orchestrator_decompose_fn implementation that asks the IDE to call the LLM
    and returns the parsed WorkerBrief dicts.
    """

    def __init__(
        self,
        task_id_ref: list[str],
        result_queue: queue.Queue,
        send_fn: Callable[[dict], None],
    ):
        self._task_id_ref = task_id_ref
        self._result_queue = result_queue
        self._send = send_fn

    def __call__(self, prompt: str) -> list[dict]:
        self._send({
            "event": "orchestrator_prompt",
            "task_id": self._task_id_ref[0],
            "prompt": prompt,
        })
        log.info("IPC: sent orchestrator prompt, waiting for result...")
        briefs: list[dict] = self._result_queue.get()
        log.info("IPC: received %d brief(s) from orchestrator", len(briefs))
        return briefs


# ---------------------------------------------------------------------------
# IPC Server
# ---------------------------------------------------------------------------

class SwarmIPCServer:
    """
    Long-lived IPC server. Runs in the main thread reading stdin.
    Coordinator runs in a daemon thread.
    """

    def __init__(self, send_fn: Optional[Callable[[dict], None]] = None):
        self._send = send_fn or _default_send
        self._task_id_ref: list[str] = [""]
        self._worker_result_queues: dict[str, queue.Queue] = {}
        self._orchestrator_result_queue: queue.Queue = queue.Queue()
        self._coordinator_thread: Optional[threading.Thread] = None
        self._project_path: Optional[str] = None

    def _on_coordinator_event(self, event: dict) -> None:
        """Forward Coordinator lifecycle events and capture real task_id."""
        tid = event.get("task_id")
        if tid:
            self._task_id_ref[0] = tid
        self._send(event)

    def _run_coordinator(
        self,
        project_path: str,
        user_request: str,
        task_id: Optional[str] = None,
        *,
        max_workers: int = 5,
        use_affected_tests: bool = True,
        full_test_command: Optional[str] = None,
        orchestrator_model: str = "",
    ) -> None:
        """Run Coordinator in a background thread."""
        self._task_id_ref[0] = task_id or ""
        try:
            worker_bridge = _IDEWorkerBridge(
                task_id_ref=self._task_id_ref,
                result_queues=self._worker_result_queues,
                send_fn=self._send,
            )
            orchestrator_bridge = _IDEOrchestratorBridge(
                task_id_ref=self._task_id_ref,
                result_queue=self._orchestrator_result_queue,
                send_fn=self._send,
            )

            coord = Coordinator(
                project_path=project_path,
                execute_worker_callback=worker_bridge,
                max_workers=max_workers,
                full_test_command=full_test_command,
                use_affected_tests=use_affected_tests,
                on_event=self._on_coordinator_event,
            )

            if task_id:
                state = coord.resume(
                    task_id=task_id,
                    orchestrator_decompose_fn=orchestrator_bridge,
                )
            else:
                state = coord.run(
                    user_request=user_request,
                    orchestrator_decompose_fn=orchestrator_bridge,
                    orchestrator_model=orchestrator_model,
                )
                self._task_id_ref[0] = state.task_id

            self._send({
                "event": "done",
                "task_id": self._task_id_ref[0],
                "is_complete": state.is_complete,
                "errors": state.errors,
                "final_branch": state.final_branch,
            })

        except Exception as exc:
            log.exception("Coordinator error")
            self._send({
                "event": "error",
                "task_id": self._task_id_ref[0],
                "message": str(exc),
            })

    def handle(self, msg: dict) -> bool:
        """
        Process one incoming IPC message.
        Returns False if server should shut down.
        """
        cmd = msg.get("cmd", "")

        if cmd == "ping":
            self._send({"event": "pong"})

        elif cmd == "shutdown":
            log.info("IPC: shutdown requested")
            return False

        elif cmd == "run":
            project_path = msg.get("project_path", "")
            user_request = msg.get("request", "")
            if not project_path or not user_request:
                self._send({"event": "error", "task_id": "", "message": "run: missing project_path or request"})
                return True

            self._project_path = project_path
            self._worker_result_queues = {}
            self._orchestrator_result_queue = queue.Queue()

            max_workers = int(msg.get("max_workers", 5))
            use_affected = bool(msg.get("use_affected_tests", True))
            full_test_cmd = msg.get("full_test_command")
            orch_model = str(msg.get("orchestrator_model", ""))

            # Start coordinator in background thread; Coordinator._emit sends {"event": "started", "task_id": ...}
            self._coordinator_thread = threading.Thread(
                target=self._run_coordinator,
                args=(project_path, user_request, None),
                kwargs={
                    "max_workers": max_workers,
                    "use_affected_tests": use_affected,
                    "full_test_command": full_test_cmd,
                    "orchestrator_model": orch_model,
                },
                daemon=True,
                name="coordinator",
            )
            self._coordinator_thread.start()

        elif cmd == "resume":
            task_id = msg.get("task_id", "")
            project_path = msg.get("project_path", self._project_path or "")
            if not project_path and task_id:
                existing = load_checkpoint(task_id)
                if existing:
                    project_path = existing.project_path

            if not task_id or not project_path:
                self._send({"event": "error", "task_id": task_id, "message": "resume: missing task_id or project_path"})
                return True

            self._task_id_ref[0] = task_id
            self._project_path = project_path
            self._worker_result_queues = {}
            self._orchestrator_result_queue = queue.Queue()
            use_affected = bool(msg.get("use_affected_tests", True))

            self._coordinator_thread = threading.Thread(
                target=self._run_coordinator,
                args=(project_path, "", task_id),
                kwargs={"use_affected_tests": use_affected},
                daemon=True,
                name="coordinator-resume",
            )
            self._coordinator_thread.start()

        elif cmd == "worker_result":
            worker_name = msg.get("worker_name", "")
            result = WorkerResult(
                worker_name=worker_name,
                success=bool(msg.get("success", False)),
                commit_sha=str(msg.get("commit_sha", "")),
                files_changed=list(msg.get("files_changed", [])),
                error_message=str(msg.get("error_message", "")),
            )

            q = self._worker_result_queues.get(worker_name)
            if q:
                q.put(result)
                log.info("IPC: worker_result routed to %s", worker_name)
            else:
                log.warning("IPC: no queue for worker %s (result ignored)", worker_name)

        elif cmd == "orchestrator_result":
            briefs = msg.get("briefs", [])
            self._orchestrator_result_queue.put(briefs)

        elif cmd == "list_tasks":
            self._send({"event": "tasks_list", "tasks": list_checkpoints()})

        elif cmd == "status":
            task_id = msg.get("task_id", self._task_id_ref[0])
            st = load_checkpoint(task_id) if task_id else None
            self._send({"event": "task_status", "task": asdict(st) if st else None})

        elif cmd == "cleanup":
            task_id = msg.get("task_id", "")
            project_path = msg.get("project_path", self._project_path or "")
            older_than_days = int(msg.get("older_than_days", 7))
            removed: list[str] = []
            try:
                if task_id:
                    st = load_checkpoint(task_id)
                    if st:
                        wm = WorktreeManager(st)
                        wm.cleanup()
                        removed.append(str(wm.swarm_root))
                elif project_path:
                    dummy = SwarmState.new("cleanup", project_path)
                    wm = WorktreeManager(dummy)
                    removed = wm.cleanup_all_stale(older_than_days=older_than_days)
                self._send({"event": "cleanup_done", "removed": removed})
            except Exception as exc:
                self._send({"event": "error", "task_id": task_id, "message": f"cleanup failed: {exc}"})

        else:
            log.warning("IPC: unknown command: %s", cmd)
            self._send({"event": "error", "task_id": self._task_id_ref[0], "message": f"Unknown cmd: {cmd}"})

        return True

    def run(self) -> None:
        """Main loop: read stdin, dispatch commands."""
        self._send({"event": "ready"})
        log.info("SwarmIPCServer ready")

        for raw_line in _recv_lines():
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                log.warning("IPC: invalid JSON (%s): %r", exc, raw_line[:200])
                continue

            if not self.handle(msg):
                break

        log.info("SwarmIPCServer exiting")


# ---------------------------------------------------------------------------
# Entry point (python -m swarm.ipc_server)
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,  # logs go to stderr; stdout is for IPC JSON
    )
    server = SwarmIPCServer()
    server.run()


if __name__ == "__main__":
    main()

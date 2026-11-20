---
name: ag-swarm
description: Multi-Agent Swarm Engine for Antigravity IDE, Antigravity 2.0, and agy CLI. Combines built-in Antigravity /swarm parallel workers with deterministic Python git-worktree isolation, 1-worker-1-file validation, state.json crash/429 resume checkpoints, AST affected-test narrowing (--junitxml), and safe integration-branch merges. Activate whenever the user types /swarm, asks in chat to run a multi-agent team ("команда агентов", "разбей на агентов", "запусти swarm", "несколько агентов", "мультиагент"), or resumes an active swarm task_id across any project.
---

# Antigravity Smart Swarm Workflow

Use this workflow to combine native Antigravity `/swarm` parallel agents with the Antigravity Patcher Swarm Engine (`swarm.cli`).

## 0. Launching Directly from Chat (`/swarm` or Natural Language)

Users can trigger this directly from any Antigravity IDE / Antigravity 2.0 chat without opening menus (e.g., `/swarm почини X и добавь Y` or `Запусти команду агентов: ...`):
- If no `task_id` was provided yet: use the current workspace git repository root as `<project_root>` (confirm if ambiguous), check `python -m swarm.cli status` first if user asks about progress, or immediately run Step 2 (`prepare`) with the user's request!
- Pass `--orchestrator-model` in `prepare` (using your active model/tier name if known) so live UI cards show which Architect model planned the run.
- Always keep architectural planning on smart Tier-1 models — never silently downgrade orchestration to a smaller model without asking the user.

## 1. Locate Swarm Engine

On Windows PowerShell (use repo source if present, otherwise deployed `~/.antigravity_tools/swarm_engine`):

```powershell
$SwarmPath = if (Test-Path "source/patcher/swarm/__init__.py") { "source/patcher" } else { "$env:USERPROFILE\.antigravity_tools\swarm_engine" }
$env:PYTHONPATH = $SwarmPath
```

On POSIX:

```bash
export PYTHONPATH="$( [ -d source/patcher/swarm ] && echo source/patcher || echo "$HOME/.antigravity_tools/swarm_engine" )"
```

## 2. Check for Interrupted Tasks or Prepare a New One

- **If resuming (e.g. after 429 account switch or `"continue"`):**
  ```powershell
  python -m swarm.cli status
  python -m swarm.cli resume --task-id <task_id>
  ```
  Skip workers already marked `done` with a `commit_sha`; only dispatch `pending_workers`.

- **For a new task:**
  ```powershell
  python -m swarm.cli prepare --project "<project_root>" --request "<user_request>"
  ```
  Read the returned JSON:
  - `task_id`: checkpoint ID saved at `~/.antigravity_tools/swarm/<task_id>/state.json`
  - `model_config`: user overrides (`orchestrator_order`, `worker_model`, `custom_models`)
  - `prompt`: 2-pass directory overview + file list prompt for decomposition

## 3. Validate Briefs & Create Isolated Worktrees

As **Orchestrator**, produce the JSON array of `WorkerBrief` objects (`worker_name`, `worker_type`, `owned_files`, `task_description`, `test_command`, `priority`) with strict **one worker = one set of files** isolation. Write them to a temp file and run:

```powershell
python -m swarm.cli setup --task-id <task_id> --briefs <briefs.json>
```

- If validation reports a file-ownership conflict (`"ok": false`), fix the briefs and re-run `setup`.
- Otherwise, each brief now has a dedicated `worktree_path` (e.g., `../<repo>-swarm-<id>/<worker_name>`) and `branch_name`.

## 4. Spawn Native `/swarm` Workers in Isolated Worktrees

Use the built-in `swarm` tool to launch workers in parallel (for the current phase in `setup` output). Pass `model` from `model_config.worker_model` when configured (otherwise Flash / fast tier), and keep `workspace:"inherit"` while instructing each worker explicitly to edit ONLY inside its assigned `worktree_path` and `owned_files`:

```
You own ONLY these files inside worktree `<worktree_path>`:
<owned_files>
Do NOT edit files in `<project_root>` directly or touch other workers' files.
Task: <task_description>
Run `<test_command>` inside `<worktree_path>` before finishing and report results.
```

As each worker finishes successfully:

```powershell
python -m swarm.cli mark-worker --task-id <task_id> --worker <worker_name> --success --commit
```

## 5. Fast Affected-Test Verification

Run AST-narrowed verification with `--junitxml` and 3-layer cross-worker conflict detection:

```powershell
python -m swarm.cli verify --task-id <task_id> --affected
```

- If all pass and `next_node == "EXECUTE"` (multi-phase task), repeat Steps 4–5 for the next phase.
- If a failure has a `cross_worker_conflicts` owner, assign that worker to fix integration inside its worktree and re-verify.
- If `verify` succeeds with `next_node == "MERGE"`, proceed to Step 6.

## 6. Safe Integration Merge & Cleanup

Merge all worker branches into `swarm-integration-<id>`, run the **full** test suite there, fast-forward `main` ONLY if green, and remove temporary worktrees:

```powershell
python -m swarm.cli merge --task-id <task_id>
```

Never touch `main` directly before `merge` succeeds. Report the `integration_sha`, test summary, and any errors to the user.

## 7. Smart Quota Exhaustion Cascade (429 / 0% Survival)

Never leave the user stuck without a Swarm if Tier-1 smart quotas run out (`429` or `0%`). Follow this strict 3-step priority cascade:

1. **Account Hop (Keep Smart Tier-1):** switch to another configured account that still has remaining quota (`> 0%`) in the current model pool (via UI / `resolveQuotaExhaustionPlan`), then run `python -m swarm.cli resume --task-id <task_id>`.
2. **Switch Tier-1 Quota Pool:** if Claude quota is `0%` on all accounts, check the independent Gemini Tier-1 pool (`gemini-2.5-pro` / `argon` / `carbon` where `gemini_percentage > 0%`) or GPT-OSS / user custom heavy models before downgrading.
3. **Degraded Survival Mode (Flash + Strict Guardrails):** only when **every** Tier-1 model on **every** account hits `0%` (or user chose fast `model_fallback`), continue orchestration on `Flash` (`gemini-2.5-flash`) with mandatory guardrails so the Swarm keeps shipping safely:
   - Keep each `WorkerBrief` tiny: **1–2 files per worker**, strictly atomic edits (no sweeping refactors across the whole repository).
   - Run `python -m swarm.cli verify --task-id <task_id> --affected` after every phase without skipping checks.
   - Always finish via `python -m swarm.cli merge --task-id <task_id>` (which runs the full suite in `_integration` before touching `main`).
   - Clearly inform the user in chat that the Swarm completed in **Degraded Backup Mode (Flash)** due to exhausted Tier-1 limits.


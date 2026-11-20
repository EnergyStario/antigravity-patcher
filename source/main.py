import sys
import os


def _run_bundled_switch_account(argv_list: list[str]) -> int:
    idx = argv_list.index("--switch-account")
    target_id = argv_list[idx + 1] if idx + 1 < len(argv_list) else ""
    app_type = argv_list[idx + 2] if idx + 2 < len(argv_list) else "ide"
    no_restart = "--no-restart" in argv_list
    if not target_id:
        return 1

    from pathlib import Path
    search_dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        search_dirs.append(Path(meipass) / "extensions" / "core")
    search_dirs.append(Path(__file__).resolve().parent.parent / "extensions" / "core")
    env_tools = os.environ.get("AG_TOOLS_DIR", "").strip()
    search_dirs.append(Path(env_tools) if env_tools else (Path.home() / ".antigravity_tools"))

    runner_dir = next((d for d in search_dirs if (d / "switch_runner.py").is_file()), None)
    if runner_dir is None:
        return 1
    if str(runner_dir) not in sys.path:
        sys.path.insert(0, str(runner_dir))

    import switch_runner  # type: ignore
    new_argv = ["switch_runner.py", "--target", target_id, "--app", app_type]
    if no_restart:
        new_argv.append("--no-restart")
    sys.argv = new_argv
    switch_runner.main()
    return 0


def _run_studio_daemon() -> int:
    import contextlib
    import io
    import json

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    real_stdout = sys.stdout
    from patcher.studio_api import get_studio_snapshot, run_studio_action

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            real_stdout.write(json.dumps({"ok": False, "error": f"bad_json: {exc}"}, ensure_ascii=False) + "\n")
            real_stdout.flush()
            continue

        req_id = req.get("id")
        cmd = str(req.get("cmd") or "status").lower()
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink):
                if cmd == "ping":
                    resp = {"ok": True, "pong": True}
                elif cmd == "status":
                    resp = get_studio_snapshot(
                        include_accounts=bool(req.get("include_accounts", True)),
                        include_projects=bool(req.get("include_projects", True)),
                        fast=bool(req.get("fast", False)),
                    )
                elif cmd == "action":
                    act_name = str(req.get("action") or "")
                    payload = req.get("payload") if isinstance(req.get("payload"), dict) else {}
                    resp = run_studio_action(act_name, payload)
                elif cmd == "quit":
                    real_stdout.write(json.dumps({"id": req_id, "ok": True}, ensure_ascii=False) + "\n")
                    real_stdout.flush()
                    return 0
                else:
                    resp = {"ok": False, "error": f"unknown_cmd: {cmd}"}
        except Exception as exc:
            resp = {"ok": False, "error": str(exc)}

        if isinstance(resp, dict) and req_id is not None:
            resp["id"] = req_id
        real_stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        real_stdout.flush()
    return 0


if __name__ == "__main__":
    argv_list = sys.argv[1:]
    if "--studio-daemon" in argv_list:
        sys.exit(_run_studio_daemon())
    if "--switch-account" in argv_list:
        sys.exit(_run_bundled_switch_account(argv_list))

    studio_mode = "--studio-status" in argv_list or "--studio-action" in argv_list
    autopilot_heal_mode = "--autopilot-heal" in argv_list

    from patcher.utils.admin import is_admin, run_as_admin
    from patcher.utils.instance import acquire_instance_lock, release_instance_lock

    if not studio_mode and not autopilot_heal_mode:
        from patcher.utils.console import setup_console
        setup_console()
    auto_patch_mode = "--auto-patch" in argv_list
    if not studio_mode and os.name == "nt" and os.environ.get("SKIP_ELEVATION") != "1" and not is_admin():
        if (auto_patch_mode or autopilot_heal_mode) and os.environ.get("AG_AUTO_REPATCH_SILENT") == "1":
            pass
        elif autopilot_heal_mode:
            pass
        elif run_as_admin():
            sys.exit(0)
        else:
            print("  [!] Could not elevate privileges. The script may fail to modify files.")
    elif not studio_mode and os.name == "posix" and not is_admin() and not auto_patch_mode and not autopilot_heal_mode:
        from patcher.cli import confirmed
        print("  [!] Root access is required to patch files in the root directory.")
        if confirmed("Re-launch with sudo?"):
            try:
                if getattr(sys, "frozen", False):
                    args = ["sudo", sys.executable] + sys.argv[1:]
                else:
                    args = ["sudo", sys.executable] + sys.argv
                os.execvp("sudo", args)
            except Exception as e:
                print(f"  [!] Failed to re-launch with sudo: {e}")
                sys.exit(1)
        else:
            from patcher.constants import COLOR_YELLOW
            from patcher.utils.console import color
            print(color("  [!] Proceeding without root. Write errors are possible.", COLOR_YELLOW))
            print()

    status_mode = "--status" in argv_list or studio_mode
    if not status_mode:
        lock_name = (
            "Local\\AntigravityPatcher_AutoRepatch"
            if auto_patch_mode
            else (
                "Local\\AntigravityPatcher_AutopilotHeal"
                if autopilot_heal_mode
                else "Local\\AntigravityPatcher_SingleInstance"
            )
        )
        if not acquire_instance_lock(lock_name):
            if not auto_patch_mode and not autopilot_heal_mode:
                print("  [!] Another instance of Antigravity Patcher is already running.")
                print("  [i] Please close the other patcher window before running a new one.")
            sys.exit(0)

    try:
        if autopilot_heal_mode:
            import json
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            from patcher.diagnostics import run_autopilot_guardian
            t_app = "all"
            if "--target" in argv_list:
                t_idx = argv_list.index("--target")
                if t_idx + 1 < len(argv_list):
                    t_app = argv_list[t_idx + 1].strip().lower() or "all"
            res = run_autopilot_guardian(
                quiet=True,
                target_app=t_app,
                force=("--force" in argv_list),
                allow_restart=("--no-restart" not in argv_list),
            )
            print(json.dumps(res, ensure_ascii=False))
        elif auto_patch_mode:
            from patcher.auto_repatch import run_auto_repatch
            args_set = set(sys.argv[1:])
            run_auto_repatch(
                restart_running=True,
                force_relaunch={
                    "ide": "--relaunch-ide" in args_set,
                    "manager": "--relaunch-mgr" in args_set,
                },
            )
        elif "--status" in sys.argv[1:]:
            from patcher.cli import redraw_main_screen
            from patcher.ide.discovery import find_install_root, find_main_js
            from patcher.manager.discovery import find_manager_binary
            from patcher.agy.discovery import find_agy_binary
            from patcher.vscode.discovery import find_extension_js, find_gemini_antigravity_binary
            root = find_install_root()
            redraw_main_screen(
                find_main_js(root) if root else "",
                find_manager_binary() or "",
                find_agy_binary() or "",
                find_extension_js() or "",
                find_gemini_antigravity_binary() or "",
            )
        elif "--studio-status" in argv_list:
            import json
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            from patcher.studio_api import get_studio_snapshot
            print(json.dumps(get_studio_snapshot(), ensure_ascii=False))
        elif "--studio-action" in argv_list:
            import json
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            from patcher.studio_api import run_studio_action
            idx = argv_list.index("--studio-action")
            act_name = argv_list[idx + 1] if idx + 1 < len(argv_list) else ""
            raw_payload = argv_list[idx + 2] if idx + 2 < len(argv_list) else "{}"
            try:
                payload = json.loads(raw_payload) if raw_payload else {}
            except Exception:
                payload = {}
            print(json.dumps(run_studio_action(act_name, payload), ensure_ascii=False))
        elif "--lock" in argv_list:
            from patcher.updater_lock import do_lock_updates
            do_lock_updates()
        elif "--unlock" in sys.argv[1:]:
            from patcher.updater_lock import do_unlock_updates
            do_unlock_updates()
        else:
            from patcher.cli import run_cli
            run_cli()
    except KeyboardInterrupt:
        print("\n  [i] Exiting...")
        sys.exit(0)
    except (PermissionError, OSError) as e:
        # Страховка от необработанного traceback как на скриншоте
        # (бэкап language_server.agybak -> Errno 1 Operation not permitted):
        # вместо "Failed to execute script 'main'" показываем понятную подсказку.
        print(f"\n  [!] Filesystem error: {e}")
        import sys as _sys

        if _sys.platform == "darwin":
            print("  [i] macOS blocked writing inside Antigravity.app.")
            print("  [i] Close Antigravity and re-run with sudo:")
            print("        sudo python source/main.py   (or sudo ./Antigravity_Patcher_macOS_arm64)")
            print("  [i] If it still says 'Operation not permitted', check flags:")
            print("        ls -lO /Applications/Antigravity.app/Contents/resources/bin/language_server")
        else:
            print("  [i] No write access — re-run as admin/root and close Antigravity first.")
        sys.exit(1)
    except Exception as e:
        print(f"\n  [!] Unexpected error: {e}")
        print("  [i] Please report it: https://github.com/EnergyStario/antigravity-patcher/issues")
        sys.exit(1)
    finally:
        release_instance_lock()
import os
import sys
import csv
import ctypes
import subprocess

CREATE_NO_WINDOW = 0x08000000


def is_admin():
    try:
        if os.name == "posix":
            return os.getuid() == 0
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def run_as_admin():
    if os.name != "nt":
        return True
    if is_admin():
        return True

    # Автоматическое повышение прав с обработкой путей с пробелами
    if getattr(sys, "frozen", False):
        executable = sys.executable
        args_str = " ".join([f'"{a}"' for a in sys.argv[1:]])
    else:
        executable = sys.executable
        script = sys.argv[0]
        args_str = " ".join([f'"{a}"' for a in [script] + sys.argv[1:]])

    try:
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, args_str, None, 1)
        return ret > 32
    except Exception:
        return False


def _get_running_win_images():
    """Return set of lowercase image names currently running on Windows, or None on query error."""
    res = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="ignore", timeout=5,
        creationflags=CREATE_NO_WINDOW,
    )
    if res.returncode != 0:
        return None
    images = set()
    for row in csv.reader(res.stdout.splitlines()):
        if row and row[0]:
            img = row[0].strip().lower()
            if img.endswith(".exe"):
                images.add(img)
    return images


def _match_win_images(running_images, target_bases):
    """Find running Windows process image names matching requested target names."""
    matched = []
    seen = set()
    for img in sorted(running_images):
        base = img[:-4] if img.endswith(".exe") else img
        for t in target_bases:
            if base == t or (t == "language_server" and base.startswith("language_server")):
                if img not in seen:
                    seen.add(img)
                    matched.append(img)
                break
    return matched


def is_process_running(names):
    """Проверяет, запущен ли хотя бы один процесс из списка names."""
    if not names:
        return False
    if os.name == "nt":
        try:
            running = _get_running_win_images()
            if running is None:
                return False
            bases = {
                (n[:-4] if n.lower().endswith(".exe") else n).strip().lower()
                for n in names if n
            }
            return bool(_match_win_images(running, bases))
        except Exception:
            pass
    else:
        for name in names:
            try:
                res = subprocess.run(
                    ["pgrep", "-f", name],
                    capture_output=True, text=True, errors="ignore", timeout=5
                )
                if res.returncode == 0 and res.stdout.strip():
                    return True
            except Exception:
                pass
    return False


def terminate_processes(names):
    """Пытается завершить процессы по их именам (батчем на Windows)."""
    if not names:
        return False

    if os.name == "nt":
        unique_execs = []
        bases = []
        seen = set()
        for raw in names:
            if not raw:
                continue
            low = raw.strip().lower()
            base = low[:-4] if low.endswith(".exe") else low
            if base in seen:
                continue
            seen.add(base)
            bases.append(base)
            unique_execs.append(f"{base}.exe")

        if not bases:
            return False

        try:
            running = _get_running_win_images()
        except Exception:
            running = None

        if running is not None:
            targets_to_kill = _match_win_images(running, bases)
            if not targets_to_kill:
                return False
        else:
            targets_to_kill = unique_execs

        success = False
        try:
            cmd = ["taskkill", "/F", "/T"]
            for im in targets_to_kill:
                cmd.extend(["/IM", im])
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                success = True
        except Exception:
            pass

        # Fallback only if any matching process actually remains running
        try:
            remaining_running = _get_running_win_images()
            still_alive = (
                _match_win_images(remaining_running, bases)
                if remaining_running is not None
                else ([] if success else targets_to_kill)
            )
        except Exception:
            still_alive = [] if success else targets_to_kill

        if still_alive:
            ps_names = ",".join(
                "'" + (im[:-4] if im.lower().endswith(".exe") else im).replace("'", "''") + "'"
                for im in still_alive
            )
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"Get-Process -Name {ps_names} -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                after = _get_running_win_images()
                if after is not None and not _match_win_images(after, bases):
                    success = True
            except Exception:
                pass
        return success

    success = False
    seen = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            res = subprocess.run(
                ["pkill", "-f", name],
                capture_output=True, text=True, errors="ignore", timeout=10
            )
            if res.returncode == 0:
                success = True
        except Exception:
            pass
    return success


def terminate_standalone_processes():
    """Terminate only standalone Antigravity 2.0 processes without disturbing Antigravity IDE."""
    if os.name == "nt":
        exact_targets = {"antigravity.exe", "language_server.exe"}
        try:
            running = _get_running_win_images()
        except Exception:
            running = None

        if running is not None:
            targets_to_kill = sorted(exact_targets & running)
            if not targets_to_kill:
                return False
        else:
            targets_to_kill = sorted(exact_targets)

        success = False
        try:
            cmd = ["taskkill", "/F", "/T"]
            for im in targets_to_kill:
                cmd.extend(["/IM", im])
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                success = True
        except Exception:
            pass

        try:
            remaining = _get_running_win_images()
            still_alive = (
                sorted(exact_targets & remaining)
                if remaining is not None
                else ([] if success else targets_to_kill)
            )
        except Exception:
            still_alive = [] if success else targets_to_kill

        if still_alive:
            ps_names = ",".join(
                "'" + (im[:-4] if im.lower().endswith(".exe") else im).replace("'", "''") + "'"
                for im in still_alive
            )
            try:
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"Get-Process -Name {ps_names} -ErrorAction SilentlyContinue | "
                        "Where-Object { $_.Path -notlike '*Antigravity IDE*' -and $_.Name -notlike '*agy*' } | "
                        "Stop-Process -Force -ErrorAction SilentlyContinue",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                after = _get_running_win_images()
                if after is not None and not (exact_targets & after):
                    success = True
            except Exception:
                pass
        return success

    success = False
    patterns = (
        "Antigravity.app/Contents/",
        "/antigravity/Antigravity",
        "/antigravity/resources/bin/language_server",
    )
    for pat in patterns:
        try:
            res = subprocess.run(["pkill", "-f", pat], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                success = True
        except Exception:
            pass
    return success

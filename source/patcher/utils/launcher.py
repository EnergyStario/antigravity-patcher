"""
patcher/utils/launcher.py -- Автоматический запуск приложений после успешного патча.
"""
import os
import sys
import subprocess
from pathlib import Path

from patcher.utils.console import info, ok, warn, color, COLOR_CYAN


def _get_windows_desktop_name() -> str | None:
    """Return the current thread's Win32 desktop name (e.g. 'Default' or 'exebox-...')."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hdesk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not hdesk:
            return None
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        # UOI_NAME = 2
        if user32.GetUserObjectInformationW(hdesk, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return buf.value or None
    except Exception:
        pass
    return None


def _resolve_expected_aumid(target_type: str, exe_path: str) -> str:
    """Resolve expected Windows AppUserModelID for IDE ('Google.AntigravityIDE') or 2.0 ('electron.app.Antigravity')."""
    if target_type == "ide":
        try:
            exe_dir = os.path.dirname(os.path.abspath(exe_path))
            for rel in (
                os.path.join(exe_dir, "resources", "app", "product.json"),
                os.path.join(exe_dir, "product.json"),
            ):
                if os.path.isfile(rel):
                    import json

                    with open(rel, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    aumid = str(data.get("win32AppUserModelId") or "").strip()
                    if aumid:
                        return aumid
        except Exception:
            pass
        return "Google.AntigravityIDE"
    if target_type == "manager":
        return "electron.app.Antigravity"
    return ""


def _find_matching_shortcuts(target_type: str) -> list[str]:
    """Find user TaskBar and Start Menu .lnk shortcuts belonging to target_type."""
    if os.name != "nt":
        return []
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    exe_name = "antigravity ide.exe" if target_type == "ide" else ("antigravity.exe" if target_type == "manager" else "")
    if not exe_name:
        return []

    dirs = [
        os.path.join(appdata, "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar"),
        os.path.join(appdata, "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "ImplicitAppShortcuts"),
        os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    matches: list[str] = []
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith(".lnk"):
                    continue
                low = fn.lower()
                if target_type == "ide" and "antigravity ide" not in low:
                    continue
                if target_type == "manager" and ("antigravity" not in low or "ide" in low or "tools" in low):
                    continue
                matches.append(os.path.join(root, fn))
    return matches


def sync_pinned_shortcuts(target_type: str, target_path: str = "", exe_path: str = "") -> str:
    """Synchronize pinned TaskBar and Start Menu .lnk TargetPath + AppUserModelID on Windows.

    Prevents duplicate ungrouped taskbar icons after an in-place update or reinstall.
    Returns the matching pinned TaskBar .lnk path when present and valid, else ''.
    """
    if os.name != "nt" or target_type not in ("ide", "manager"):
        return ""

    resolved_exe = exe_path if (exe_path and os.path.isfile(exe_path)) else ""
    if not resolved_exe:
        want_name = "Antigravity IDE.exe" if target_type == "ide" else "Antigravity.exe"
        if target_path:
            p = Path(target_path)
            for _ in range(6):
                cand = p / want_name
                if cand.is_file():
                    resolved_exe = str(cand)
                    break
                p = p.parent
        if not resolved_exe:
            local = os.environ.get("LOCALAPPDATA", "")
            sub = os.path.join("Programs", "Antigravity IDE", want_name) if target_type == "ide" else os.path.join("Programs", "Antigravity", want_name)
            cand = os.path.join(local, sub)
            if os.path.isfile(cand):
                resolved_exe = cand
    if not resolved_exe or not os.path.isfile(resolved_exe):
        return ""

    # Never rewrite the user's real TaskBar / Start Menu shortcuts to point at tiny test stubs or temp dirs.
    try:
        import tempfile

        norm_exe_chk = os.path.normcase(os.path.abspath(resolved_exe))
        temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        if norm_exe_chk.startswith(temp_root + os.sep) or os.path.getsize(resolved_exe) < 1024:
            return ""
    except OSError:
        return ""

    shortcuts = _find_matching_shortcuts(target_type)
    if not shortcuts:
        return ""

    aumid = _resolve_expected_aumid(target_type, resolved_exe)
    norm_exe = os.path.normpath(resolved_exe)
    work_dir = os.path.dirname(norm_exe)
    taskbar_lnk = ""
    for sc in shortcuts:
        if f"{os.sep}taskbar{os.sep}" in sc.lower():
            taskbar_lnk = sc
            break

    # Use a single PowerShell call with WScript.Shell + IPropertyStore to align TargetPath and AUMID.
    ps_script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$exe = $env:AG_SYNC_EXE
$wd = $env:AG_SYNC_WD
$aumid = $env:AG_SYNC_AUMID
$paths = ($env:AG_SYNC_LNKS -split '\|') | Where-Object { $_ -and (Test-Path $_) }
if (-not $paths) { exit 0 }

$src = @"
using System;
using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential, Pack = 4)]
public struct PROPERTYKEY {
    public Guid fmtid;
    public uint pid;
}
[StructLayout(LayoutKind.Explicit)]
public struct PROPVARIANT {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pwszVal;
}
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    uint GetCount(out uint cProps);
    uint GetAt(uint iProp, out PROPERTYKEY pkey);
    uint GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    uint SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    uint Commit();
}
[ComImport, Guid("0000010b-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPersistFile {
    void GetClassID(out Guid pClassID);
    int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
}
[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
public class ShellLinkCom {}
public static class LnkAumidHelper {
    public static readonly PROPERTYKEY PKEY_AppUserModel_ID = new PROPERTYKEY {
        fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
        pid = 5
    };
    public static void SetAumid(string lnkPath, string aumid) {
        if (string.IsNullOrEmpty(aumid)) return;
        var link = new ShellLinkCom();
        var pf = (IPersistFile)link;
        pf.Load(lnkPath, 2); // STGM_READWRITE
        var store = (IPropertyStore)link;
        PROPVARIANT existing;
        var key = PKEY_AppUserModel_ID;
        store.GetValue(ref key, out existing);
        string cur = (existing.vt == 31 && existing.pwszVal != IntPtr.Zero) ? Marshal.PtrToStringUni(existing.pwszVal) : "";
        if (!string.Equals(cur, aumid, StringComparison.OrdinalIgnoreCase)) {
            var pv = new PROPVARIANT();
            pv.vt = 31; // VT_LPWSTR
            pv.pwszVal = Marshal.StringToCoTaskMemUni(aumid);
            try {
                store.SetValue(ref key, ref pv);
                store.Commit();
                pf.Save(lnkPath, true);
            } finally {
                Marshal.FreeCoTaskMem(pv.pwszVal);
            }
        }
    }
}
"@
Add-Type -TypeDefinition $src -Language CSharp | Out-Null
$wsh = New-Object -ComObject WScript.Shell
foreach ($lnk in $paths) {
    $sc = $wsh.CreateShortcut($lnk)
    $changed = $false
    if ($sc.TargetPath -ne $exe) { $sc.TargetPath = $exe; $changed = $true }
    if ($wd -and $sc.WorkingDirectory -ne $wd) { $sc.WorkingDirectory = $wd; $changed = $true }
    $wantIcon = "$exe,0"
    if ($sc.IconLocation -ne $wantIcon) { $sc.IconLocation = $wantIcon; $changed = $true }
    if ($changed) { $sc.Save() }
    [LnkAumidHelper]::SetAumid($lnk, $aumid)
}
"""
    try:
        env = os.environ.copy()
        env["AG_SYNC_EXE"] = norm_exe
        env["AG_SYNC_WD"] = work_dir
        env["AG_SYNC_AUMID"] = aumid
        env["AG_SYNC_LNKS"] = "|".join(shortcuts)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            env=env,
            capture_output=True,
            timeout=12,
            creationflags=creation_flags,
        )
    except Exception:
        pass
    return taskbar_lnk


def launch_application(target_type: str, target_path: str = "") -> bool:
    """
    Автоматически запускает пропатченное приложение (Antigravity IDE, 2.0 или VS Code)
    с понижением прав (без прав Администратора на Windows, чтобы не конфликтовать с синглтоном IDE).
    """
    exe_path = None
    app_name = ""

    if target_type == "ide":
        app_name = "Antigravity IDE"
        if target_path:
            p = Path(target_path)
            for _ in range(6):
                c = p / "Antigravity IDE.exe"
                if c.is_file():
                    exe_path = str(c)
                    break
                p = p.parent
        if not exe_path and os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                c = Path(local) / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe"
                if c.is_file():
                    exe_path = str(c)
            pf = os.environ.get("PROGRAMFILES", "")
            if not exe_path and pf:
                c = Path(pf) / "Antigravity IDE" / "Antigravity IDE.exe"
                if c.is_file():
                    exe_path = str(c)
        if not exe_path and sys.platform == "darwin":
            for mac_app in ["/Applications/Antigravity IDE.app", os.path.expanduser("~/Applications/Antigravity IDE.app")]:
                if os.path.isdir(mac_app):
                    exe_path = mac_app
                    break

    elif target_type == "manager":
        app_name = "Antigravity 2.0"
        if target_path:
            p = Path(target_path)
            for _ in range(5):
                c = p / "Antigravity.exe"
                if c.is_file():
                    exe_path = str(c)
                    break
                p = p.parent
        if not exe_path and os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                c = Path(local) / "Programs" / "antigravity" / "Antigravity.exe"
                if c.is_file():
                    exe_path = str(c)

    elif target_type == "vscode":
        app_name = "VS Code"
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                c = Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe"
                if c.is_file():
                    exe_path = str(c)
        if not exe_path:
            exe_path = "code"

    if not exe_path:
        return False

    desktop_name = _get_windows_desktop_name() if (os.name == "nt" and os.path.isfile(exe_path)) else None
    if desktop_name and desktop_name.lower() != "default":
        warn(
            f"Skipped launching {app_name} from isolated desktop '{desktop_name}' "
            f"(would hold SingleInstanceLock invisibly); open {app_name} manually on your Windows desktop."
        )
        return False

    try:
        print()
        info(f"Launching {app_name}...")
        clean_env = os.environ.copy()
        for k in list(clean_env.keys()):
            uk = k.upper()
            if uk.startswith(("ELECTRON_", "VSCODE_", "ATOM_SHELL_", "ANTIGRAVITY_VSCODE_")) or uk in (
                "NODE_OPTIONS",
                "NODE_ENV",
                "ORIGINAL_XDG_CURRENT_DESKTOP",
            ):
                clean_env.pop(k, None)
                os.environ.pop(k, None)
        if os.name == "nt" and os.path.isfile(exe_path):
            norm_exe = os.path.normpath(exe_path)
            pinned_lnk = sync_pinned_shortcuts(target_type, target_path, norm_exe)
            explorer_target = pinned_lnk if (pinned_lnk and os.path.isfile(pinned_lnk)) else norm_exe
            launched = False
            # 1. Primary: Launch via pinned .lnk (keeps Windows taskbar icon grouped) or exe through Explorer
            try:
                subprocess.Popen(
                    ["explorer.exe", explorer_target],
                    env=clean_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                launched = True
            except Exception:
                pass

            # 2. Secondary fallback: DETACHED_PROCESS with disconnected stdio (no Electron logs dumped into console)
            if not launched:
                try:
                    subprocess.Popen(
                        [norm_exe],
                        env=clean_env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x00000008 | 0x00000200,
                        close_fds=True,
                    )
                    launched = True
                except Exception:
                    pass

            # 3. Tertiary fallback: os.startfile
            if not launched:
                try:
                    os.startfile(norm_exe)
                    launched = True
                except Exception:
                    pass
        elif sys.platform == "darwin":
            subprocess.Popen(["open", exe_path], env=clean_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        else:
            subprocess.Popen([exe_path], env=clean_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, shell=True if not os.path.isabs(exe_path) else False)
        ok(f"{app_name} launched successfully!")
        return True
    except Exception as ex:
        warn(f"Could not automatically launch {app_name}: {ex}")
        return False


def detect_running_antigravity_apps() -> dict[str, bool]:
    """Detect whether Antigravity IDE and/or standalone Antigravity 2.0 are currently running."""
    state = {"ide": False, "manager": False}
    if os.name == "nt":
        try:
            from patcher.utils.admin import _get_running_win_images
            running = _get_running_win_images()
            if running:
                state["ide"] = "antigravity ide.exe" in running
                state["manager"] = "antigravity.exe" in running
        except Exception:
            pass
        return state

    for key, patterns in (
        ("ide", ("Antigravity IDE.app/Contents/", "/antigravity-ide/", "/Antigravity IDE/")),
        ("manager", ("Antigravity.app/Contents/", "/antigravity/Antigravity")),
    ):
        for pat in patterns:
            try:
                res = subprocess.run(
                    ["pgrep", "-f", pat],
                    capture_output=True, text=True, errors="ignore", timeout=3,
                )
                if res.returncode == 0 and res.stdout.strip():
                    state[key] = True
                    break
            except Exception:
                pass
    return state


def restart_antigravity_apps(
    was_running: dict[str, bool],
    main_js_path: str = "",
    manager_path: str = "",
    ensure_backend: bool = True,
) -> list[str]:
    """Close and relaunch only the Antigravity apps marked True in was_running."""
    if not was_running or not any(was_running.values()):
        return []

    import time
    from patcher.utils.admin import terminate_processes, terminate_standalone_processes

    terminated_any = False
    if was_running.get("ide"):
        if terminate_processes(["Antigravity IDE", "Antigravity IDE.exe"]):
            terminated_any = True
    if was_running.get("manager"):
        if terminate_standalone_processes():
            terminated_any = True

    if terminated_any:
        time.sleep(0.9)

    restarted: list[str] = []
    if was_running.get("ide"):
        if launch_application("ide", main_js_path or ""):
            restarted.append("Antigravity IDE")
            if ensure_backend:
                try:
                    from patcher.ide.patcher import ensure_agy_backend_patched
                    ensure_agy_backend_patched(wait_seconds=2.0)
                except Exception:
                    pass
    if was_running.get("manager"):
        if launch_application("manager", manager_path or ""):
            restarted.append("Antigravity 2.0")
    return restarted

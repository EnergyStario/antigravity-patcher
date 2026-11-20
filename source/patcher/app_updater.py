"""Automatic version discovery, online update check, and silent install for Antigravity apps.

Supports Antigravity IDE, Antigravity 2.0 (Manager), and Antigravity CLI (agy).
When auto-updates are unlocked for a component, checking/patching can automatically
detect newer official Google releases, download and verify them, install silently,
and let the caller immediately re-patch, deploy extensions/UI, sync pinned shortcuts,
and relaunch the updated application.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from patcher.constants import (
    COLOR_CYAN,
    COLOR_DIM,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_YELLOW,
    VERSION,
)
from patcher.utils.console import color, err, hint, info, ok, warn


IDE_UPDATE_BASE_URL = "https://antigravity-ide-auto-updater-974169037036.us-central1.run.app"
MANAGER_UPDATE_BASE_URL = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app"
CLI_UPDATE_BASE_URL = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_UPDATE_CACHE_LOCK = threading.Lock()
_UPDATE_CACHE: Dict[str, dict] = {}
_BG_CHECK_THREAD: Optional[threading.Thread] = None

DEFAULT_CACHE_TTL_SEC = 300.0


def parse_semver_tuple(ver_str: str) -> Tuple[int, ...]:
    """Parse version string like '2.7.0' into comparable integer tuple."""
    if not ver_str:
        return ()
    m = _SEMVER_RE.search(str(ver_str).strip().lstrip("vV"))
    if not m:
        return ()
    return tuple(int(g) for g in m.groups())


def is_newer_version(latest: str, current: str) -> bool:
    """Return True when latest represents a strictly newer semver than current."""
    lat_t = parse_semver_tuple(latest)
    cur_t = parse_semver_tuple(current)
    if not lat_t or not cur_t:
        return bool(latest and current and latest.strip() != current.strip())
    return lat_t > cur_t


def _ide_product_json_paths(main_js_path: str = "") -> list[str]:
    paths: list[str] = []
    if main_js_path:
        base = os.path.dirname(os.path.abspath(main_js_path))
        for _ in range(4):
            paths.append(os.path.join(base, "product.json"))
            parent = os.path.dirname(base)
            if not parent or parent == base:
                break
            base = parent
    try:
        from patcher.ide.discovery import find_install_root

        root = find_install_root()
        if root:
            paths.append(os.path.join(root, "resources", "app", "product.json"))
            paths.append(os.path.join(root, "product.json"))
    except Exception:
        pass
    seen = set()
    unique: list[str] = []
    for p in paths:
        norm = os.path.normpath(p)
        key = norm.lower() if os.name == "nt" else norm
        if key not in seen:
            seen.add(key)
            unique.append(norm)
    return unique


def _get_ide_product_info(main_js_path: str = "") -> dict:
    """Read metadata from Antigravity IDE's product.json when present."""
    for p in _ide_product_json_paths(main_js_path):
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def get_installed_app_version(target: str, target_path: str = "") -> str:
    """Return installed version string for 'ide', 'manager', 'cli', or '' if unknown."""
    key = (target or "").lower()
    if key == "ide":
        prod = _get_ide_product_info(target_path)
        ver = str(prod.get("ideVersion") or "").strip()
        if parse_semver_tuple(ver):
            return ver
        if target_path and os.path.isfile(target_path):
            try:
                from patcher.ide.discovery import get_ag_version

                det, _ = get_ag_version(target_path)
                if det:
                    return str(det).strip()
            except Exception:
                pass
        return ver or ""

    if key in ("manager", "2.0", "desktop"):
        if target_path and os.path.isfile(target_path):
            try:
                from patcher.manager.discovery import get_antigravity_version

                det = get_antigravity_version(target_path)
                if det:
                    return str(det).strip()
            except Exception:
                pass
        try:
            from patcher.manager.discovery import find_manager_binary, get_antigravity_version

            mgr = find_manager_binary()
            if mgr:
                det = get_antigravity_version(mgr)
                if det:
                    return str(det).strip()
        except Exception:
            pass
        return ""

    if key in ("cli", "agy"):
        candidates = []
        if target_path:
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(target_path)), "agy.version.json"))
        candidates.append(os.path.join(os.path.expanduser("~"), ".gemini", "bin", "agy.version.json"))
        for vp in candidates:
            if os.path.isfile(vp):
                try:
                    with open(vp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ver = str(data.get("version") or "").strip()
                    if ver:
                        return ver
                except Exception:
                    pass
        return ""

    return ""


def _http_get_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 4.0) -> Tuple[int, str]:
    req_headers = {"User-Agent": f"AntigravityPatcher/{VERSION}"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            body = resp.read().decode("utf-8", errors="replace")
            return int(status), body
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return 204, ""
        raise


def _ide_platform_slug(product_info: dict) -> str:
    if os.name == "nt":
        target = str(product_info.get("target") or "user").lower()
        return f"win32-x64-{target}" if target in ("user", "system", "archive") else "win32-x64-user"
    if sys.platform == "darwin":
        import platform

        arch = (platform.machine() or "").lower()
        return "darwin-arm64" if arch in ("arm64", "aarch64") else "darwin"
    return "linux-x64"


def _extract_version_from_ide_payload(payload: dict) -> str:
    """Extract Antigravity IDE semver (e.g. '2.7.0') from official updater response."""
    url = str(payload.get("url") or "")
    m = re.search(r"/stable/(\d+\.\d+\.\d+)(?:[-/]|$)", url)
    if m:
        return m.group(1)
    ide_ver = str(payload.get("ideVersion") or "").strip()
    if parse_semver_tuple(ide_ver):
        return ide_ver
    prod_ver = str(payload.get("productVersion") or "").strip()
    if parse_semver_tuple(prod_ver):
        return prod_ver
    return ""


def check_ide_update(main_js_path: str = "", timeout: float = 4.0) -> dict:
    """Check official Antigravity IDE update feed."""
    prod = _get_ide_product_info(main_js_path)
    installed = get_installed_app_version("ide", main_js_path)
    commit = str(prod.get("commit") or "").strip()
    quality = str(prod.get("quality") or "stable").strip() or "stable"
    base_url = str(prod.get("updateUrl") or IDE_UPDATE_BASE_URL).strip().rstrip("/")

    result = {
        "target": "ide",
        "installed_version": installed,
        "latest_version": installed,
        "update_available": False,
        "download_url": "",
        "sha256": "",
        "commit": commit,
        "checked": False,
        "checked_at": time.time(),
        "error": "",
    }
    if not commit:
        result["error"] = "missing commit in product.json"
        return result

    host_hash = hashlib.sha256(socket.gethostname().encode("utf-8", errors="ignore")).hexdigest()
    platform_slug = _ide_platform_slug(prod)
    url = f"{base_url}/api/update/{platform_slug}/{quality}/{commit}/{host_hash}"

    try:
        status, body = _http_get_text(url, timeout=timeout)
        result["checked"] = True
        if status == 204 or not body.strip():
            return result
        payload = json.loads(body)
        if not isinstance(payload, dict) or not payload.get("url"):
            return result
        latest_ver = _extract_version_from_ide_payload(payload)
        remote_commit = str(payload.get("version") or "").strip()
        is_new = is_newer_version(latest_ver, installed) if (latest_ver and installed) else bool(
            remote_commit and remote_commit != commit
        )
        if is_new:
            result["latest_version"] = latest_ver or remote_commit or installed
            result["update_available"] = True
            result["download_url"] = str(payload.get("url") or "")
            result["sha256"] = str(payload.get("sha256hash") or "")
            result["remote_commit"] = remote_commit
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def _parse_electron_builder_yml(yml_text: str) -> dict:
    """Parse minimal fields (version, url, sha512, size, path) from electron-builder yml."""
    parsed = {
        "version": "",
        "url": "",
        "sha512": "",
        "size": 0,
        "path": "",
    }
    for raw_line in (yml_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip().lower()
        val = v.strip().strip("'\"")
        if key == "version" and not parsed["version"]:
            parsed["version"] = val
        elif key == "url" and not parsed["url"]:
            parsed["url"] = val
        elif key == "sha512" and not parsed["sha512"]:
            parsed["sha512"] = val
        elif key == "size" and not parsed["size"]:
            try:
                parsed["size"] = int(val)
            except ValueError:
                pass
        elif key == "path" and not parsed["path"]:
            parsed["path"] = val
    return parsed


def check_manager_update(manager_path: str = "", timeout: float = 4.0) -> dict:
    """Check official Antigravity 2.0 (desktop hub) update feed."""
    installed = get_installed_app_version("manager", manager_path)
    result = {
        "target": "manager",
        "installed_version": installed,
        "latest_version": installed,
        "update_available": False,
        "download_url": "",
        "sha512_b64": "",
        "size": 0,
        "checked": False,
        "checked_at": time.time(),
        "error": "",
    }

    manifest_name = "latest-x64-win.yml"
    if sys.platform == "darwin":
        import platform

        arch = (platform.machine() or "").lower()
        manifest_name = "latest-arm64-mac.yml" if arch in ("arm64", "aarch64") else "latest-x64-mac.yml"
    elif os.name != "nt":
        manifest_name = "latest-linux.yml"

    url = f"{MANAGER_UPDATE_BASE_URL}/manifest/{manifest_name}"
    try:
        # Passing 0.0.0 ensures the server returns full download URL/sha512 metadata when newer exists.
        _, body = _http_get_text(url, headers={"x-app-version": "0.0.0"}, timeout=timeout)
        meta = _parse_electron_builder_yml(body)
        result["checked"] = True
        latest_ver = meta.get("version") or installed
        if latest_ver:
            result["latest_version"] = latest_ver
        if installed and latest_ver and is_newer_version(latest_ver, installed):
            result["update_available"] = True
            result["download_url"] = meta.get("url") or ""
            result["sha512_b64"] = meta.get("sha512") or ""
            result["size"] = int(meta.get("size") or 0)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def check_cli_update(agy_path: str = "", timeout: float = 4.0) -> dict:
    """Check official Antigravity CLI (agy) releases feed."""
    installed = get_installed_app_version("cli", agy_path)
    result = {
        "target": "cli",
        "installed_version": installed,
        "latest_version": installed,
        "update_available": False,
        "download_url": "",
        "checked": False,
        "checked_at": time.time(),
        "error": "",
    }
    url = f"{CLI_UPDATE_BASE_URL}/releases"
    try:
        _, body = _http_get_text(url, timeout=timeout)
        releases = json.loads(body)
        result["checked"] = True
        if isinstance(releases, list) and releases:
            top = releases[0] if isinstance(releases[0], dict) else {}
            latest_ver = str(top.get("version") or "").strip()
            if latest_ver:
                result["latest_version"] = latest_ver
                if installed and is_newer_version(latest_ver, installed):
                    result["update_available"] = True
                    exec_id = str(top.get("execution_id") or "").strip()
                    if exec_id and os.name == "nt":
                        result["download_url"] = (
                            f"https://storage.googleapis.com/antigravity-public/antigravity-cli/"
                            f"{latest_ver}-{exec_id}/windows-x64/agy.exe"
                        )
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def check_component_update(target: str, target_path: str = "", timeout: float = 4.0) -> dict:
    key = (target or "").lower()
    if key == "ide":
        res = check_ide_update(target_path, timeout=timeout)
    elif key in ("manager", "2.0", "desktop"):
        res = check_manager_update(target_path, timeout=timeout)
    elif key in ("cli", "agy"):
        res = check_cli_update(target_path, timeout=timeout)
    else:
        return {"target": key, "installed_version": "", "latest_version": "", "update_available": False}
    with _UPDATE_CACHE_LOCK:
        _UPDATE_CACHE[res["target"]] = dict(res)
    return res


def get_cached_update_info(target: str, max_age_sec: float = DEFAULT_CACHE_TTL_SEC) -> Optional[dict]:
    key = "manager" if (target or "").lower() in ("manager", "2.0", "desktop") else (
        "cli" if (target or "").lower() in ("cli", "agy") else (target or "").lower()
    )
    with _UPDATE_CACHE_LOCK:
        entry = _UPDATE_CACHE.get(key)
        if not entry:
            return None
        if (time.time() - float(entry.get("checked_at") or 0)) > max_age_sec:
            return None
        return dict(entry)


def invalidate_app_update_cache(target: Optional[str] = None) -> None:
    with _UPDATE_CACHE_LOCK:
        if target is None:
            _UPDATE_CACHE.clear()
        else:
            key = "manager" if target.lower() in ("manager", "2.0", "desktop") else (
                "cli" if target.lower() in ("cli", "agy") else target.lower()
            )
            _UPDATE_CACHE.pop(key, None)


def start_background_update_checks(
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    unlocked_targets: Optional[dict] = None,
    timeout: float = 4.0,
    check_all_installed: bool = True,
) -> Optional[threading.Thread]:
    """Start non-blocking daemon checks for installed components (even if Google's updater is locked)."""
    global _BG_CHECK_THREAD
    if unlocked_targets is None:
        unlocked_targets = {}

    if _BG_CHECK_THREAD and _BG_CHECK_THREAD.is_alive():
        return _BG_CHECK_THREAD

    def _worker():
        checks = []
        if main_js_path and os.path.isfile(main_js_path) and (check_all_installed or unlocked_targets.get("ide", False)):
            checks.append(("ide", main_js_path))
        if manager_path and os.path.isfile(manager_path) and (check_all_installed or unlocked_targets.get("manager", False)):
            checks.append(("manager", manager_path))
        if agy_path and os.path.isfile(agy_path) and (check_all_installed or unlocked_targets.get("cli", False)):
            checks.append(("cli", agy_path))
        for tgt, path in checks:
            try:
                check_component_update(tgt, path, timeout=timeout)
            except Exception:
                pass

    t = threading.Thread(target=_worker, name="ag-app-update-check", daemon=True)
    _BG_CHECK_THREAD = t
    t.start()
    return t


def _download_with_progress(url: str, dest_path: str, expected_size: int = 0, quiet: bool = False, timeout: float = 60.0) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": f"AntigravityPatcher/{VERSION}"})
    tmp_path = dest_path + ".part"
    downloaded = 0
    last_pct = -1
    last_print_ts = 0.0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = expected_size or int(resp.headers.get("Content-Length") or 0)
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if not quiet and total > 0:
                        pct = int((downloaded * 100) / total)
                        if (pct >= last_pct + 5 and (now - last_print_ts) >= 0.25) or pct == 100:
                            last_pct = pct
                            last_print_ts = now
                            mb_done = downloaded / (1024 * 1024)
                            mb_tot = total / (1024 * 1024)
                            print(f"\r  [*] Downloading update: {pct}% ({mb_done:.1f} / {mb_tot:.1f} MB)   ", end="", flush=True)
        if not quiet and last_pct >= 0:
            print()
        os.replace(tmp_path, dest_path)
        return os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0
    except Exception as exc:
        if not quiet:
            print()
            err(f"Download failed: {exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def _verify_file_hashes(file_path: str, sha256_hex: str = "", sha512_b64: str = "") -> bool:
    if not os.path.isfile(file_path):
        return False
    if sha256_hex:
        h256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                h256.update(chunk)
        if h256.hexdigest().lower() != sha256_hex.strip().lower():
            return False
    if sha512_b64:
        h512 = hashlib.sha512()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                h512.update(chunk)
        got_b64 = base64.b64encode(h512.digest()).decode("ascii")
        if got_b64 != sha512_b64.strip():
            return False
    return True


def _stop_target_processes(target: str) -> None:
    """Terminate running target processes before silent upgrade on Windows."""
    if os.name != "nt":
        return
    names = []
    if target == "ide":
        names = ["Antigravity IDE.exe"]
    elif target == "manager":
        names = ["Antigravity.exe", "language_server.exe"]
    elif target == "cli":
        names = ["agy.exe"]

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for img in names:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", img],
                capture_output=True,
                timeout=8,
                creationflags=creation_flags,
            )
        except Exception:
            pass
    if names:
        time.sleep(0.8)


def _rediscover_target_path(target: str, fallback: str = "") -> str:
    if target == "ide":
        try:
            from patcher.ide.discovery import find_install_root, find_main_js

            root = find_install_root()
            if root:
                p = find_main_js(root)
                if p and os.path.isfile(p):
                    return p
        except Exception:
            pass
    elif target == "manager":
        try:
            from patcher.manager.discovery import find_manager_binary

            p = find_manager_binary()
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass
    elif target == "cli":
        try:
            from patcher.agy.discovery import find_agy_binary

            p = find_agy_binary()
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass
    return fallback


def check_and_install_component_update(
    target: str,
    target_path: str = "",
    is_locked_fn: Optional[Callable[[], bool]] = None,
    quiet: bool = False,
    auto_install: bool = True,
) -> Tuple[bool, str, dict]:
    """Check if an unlocked component has an update online and silently install it.

    Returns (installed_update: bool, effective_target_path: str, info_dict: dict).
    """
    key = "manager" if (target or "").lower() in ("manager", "2.0", "desktop") else (
        "cli" if (target or "").lower() in ("cli", "agy") else (target or "").lower()
    )
    if is_locked_fn is not None:
        try:
            if is_locked_fn():
                return False, target_path, {
                    "target": key,
                    "installed_version": get_installed_app_version(key, target_path),
                    "update_available": False,
                    "skipped_locked": True,
                }
        except Exception:
            pass

    if not quiet:
        label = {"ide": "Antigravity IDE", "manager": "Antigravity 2.0", "cli": "Antigravity CLI"}.get(key, key)
        info(f"Checking official {label} updates...")

    upd = check_component_update(key, target_path, timeout=6.0)
    if not upd.get("update_available"):
        if not quiet and upd.get("checked") and upd.get("installed_version"):
            ok(f"{key.upper()} is already on the latest version (v{upd['installed_version']}).")
        return False, target_path, upd

    cur_v = upd.get("installed_version") or "?"
    new_v = upd.get("latest_version") or "new"
    dl_url = upd.get("download_url") or ""
    if not quiet:
        ok(f"New update found for {key.upper()}: v{cur_v} -> v{new_v}")

    if not auto_install or not dl_url or os.name != "nt":
        return False, target_path, upd

    if key == "cli":
        # Direct binary replace for standalone agy.exe when writable
        if not target_path or not os.path.isfile(target_path):
            return False, target_path, upd
        tmp_dir = tempfile.mkdtemp(prefix="ag_cli_upd_")
        tmp_bin = os.path.join(tmp_dir, "agy.exe")
        try:
            if not _download_with_progress(dl_url, tmp_bin, quiet=quiet):
                return False, target_path, upd
            _stop_target_processes("cli")
            import shutil

            shutil.copy2(tmp_bin, target_path)
            invalidate_app_update_cache("cli")
            if not quiet:
                ok(f"Updated Antigravity CLI -> v{new_v}")
            return True, target_path, upd
        except Exception as exc:
            if not quiet:
                err(f"CLI update install failed: {exc}")
            return False, target_path, upd
        finally:
            try:
                if os.path.isdir(tmp_dir):
                    import shutil

                    shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    tmp_dir = tempfile.mkdtemp(prefix=f"ag_{key}_upd_")
    installer_name = "AntigravityIDEUpdate.exe" if key == "ide" else "AntigravityUpdate.exe"
    installer_path = os.path.join(tmp_dir, installer_name)

    try:
        if not _download_with_progress(
            dl_url,
            installer_path,
            expected_size=int(upd.get("size") or 0),
            quiet=quiet,
        ):
            return False, target_path, upd

        if not _verify_file_hashes(
            installer_path,
            sha256_hex=str(upd.get("sha256") or ""),
            sha512_b64=str(upd.get("sha512_b64") or ""),
        ):
            if not quiet:
                err("Downloaded installer failed integrity checksum verification.")
            return False, target_path, upd

        if not quiet:
            info(f"Installing v{new_v} silently (closing running app first)...")
        _stop_target_processes(key)

        if key == "ide":
            cmd = [
                installer_path,
                "/VERYSILENT",
                "/NORESTART",
                "/SUPPRESSMSGBOXES",
                "/CLOSEAPPLICATIONS",
                "/FORCECLOSEAPPLICATIONS",
            ]
        else:
            cmd = [installer_path, "/S"]
            # Respect non-default Antigravity 2.0 install dir if present
            if target_path and os.path.isfile(target_path):
                # ...\Antigravity\resources\bin\language_server.exe -> ...\Antigravity
                app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(target_path))))
                default_root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Antigravity")
                if app_root and default_root and os.path.normcase(app_root) != os.path.normcase(default_root):
                    cmd.append(f"/D={app_root}")

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(cmd, timeout=300, creationflags=creation_flags)
        if proc.returncode != 0:
            if not quiet:
                warn(f"Silent installer exited with code {proc.returncode}")
            return False, target_path, upd

        # Give filesystem a moment to finalize replaced files
        time.sleep(1.0)
        new_path = _rediscover_target_path(key, fallback=target_path)
        invalidate_app_update_cache(key)
        installed_now = get_installed_app_version(key, new_path)
        if not quiet:
            ok(f"Installed update: v{installed_now or new_v}")
        upd["installed_after"] = installed_now or new_v
        return True, new_path, upd
    except Exception as exc:
        if not quiet:
            err(f"Update installation failed: {exc}")
        return False, target_path, upd
    finally:
        try:
            import shutil

            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

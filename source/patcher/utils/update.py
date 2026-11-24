import json
import sys
import threading
import urllib.request
import urllib.error
import webbrowser

from patcher.constants import VERSION, GITHUB_REPO, COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, COLOR_RED, COLOR_BOLD, COLOR_UNDERLINE
from patcher.utils.console import color, link, info, ok, warn, err, hint, _frame_border, _frame_row


RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
ISSUES_URL = f"https://github.com/{GITHUB_REPO}/issues"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

LAST_UPDATE_RESULT = None
_UPDATE_THREAD = None


def get_last_update_result():
    return LAST_UPDATE_RESULT


def set_last_update_result(result):
    global LAST_UPDATE_RESULT
    LAST_UPDATE_RESULT = result


def print_network_error_warning():
    """No-op when check_for_updates fails."""
    pass


def print_network_error_banner():
    """No-op: removed warning banner."""
    pass


def print_update_status_notice():
    """Prints update notice or up-to-date status in the main menu."""
    if LAST_UPDATE_RESULT == "network_error":
        return
    elif isinstance(LAST_UPDATE_RESULT, tuple) and LAST_UPDATE_RESULT[0] == "update_available":
        _, tag, url = LAST_UPDATE_RESULT
        info(f"New version available: {color(tag, COLOR_GREEN, COLOR_BOLD)} (current: {color(VERSION, COLOR_YELLOW)})")
        hint(f"Download: {color(url, COLOR_CYAN)}")
        print()
    elif LAST_UPDATE_RESULT == "up_to_date":
        ok(f"Patcher is up to date (v{VERSION})")
        print()


def _parse_version(v):
    """Parse a version string like '1.2.6' into a tuple of ints."""
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def _fetch_latest_release(timeout=5):
    """Fetch latest release info from GitHub API. Returns (tag, html_url) or (None, None)."""
    rel = _fetch_latest_release_info(timeout=timeout)
    if rel:
        return rel.get("tag", ""), rel.get("html_url", "")
    return None, None


def _fetch_latest_release_info(timeout=6):
    """Fetch complete release dictionary from GitHub API."""
    req = urllib.request.Request(API_URL, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"AntigravityPatcher/{VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "tag": data.get("tag_name", ""),
                "html_url": data.get("html_url", ""),
                "body": data.get("body", ""),
                "assets": data.get("assets", [])
            }
    except Exception:
        return None


def auto_update_on_startup():
    """Start a genuinely non-blocking background update check on startup.

    Populates LAST_UPDATE_RESULT asynchronously without stalling CLI rendering
    or interrupting the interactive console with background stdout/sys.exit().
    """
    global _UPDATE_THREAD
    if _UPDATE_THREAD is not None and _UPDATE_THREAD.is_alive():
        return None

    def _worker():
        try:
            check_for_updates(silent=True, timeout=2.5)
        except Exception:
            pass

    t = threading.Thread(target=_worker, name="ag-update-check", daemon=True)
    _UPDATE_THREAD = t
    t.start()
    return t


def perform_cloud_update(interactive=False) -> bool:
    """Checks for cloud updates, downloads the new binary,
    replaces the current executable and restarts cleanly.
    """
    import os
    import shutil
    from pathlib import Path

    info("Checking for cloud updates on GitHub...")
    rel = _fetch_latest_release_info(timeout=8)
    if not rel or not rel.get("tag"):
        err("Could not connect to GitHub releases API (check your internet or GitHub repo).")
        return False

    latest_tag = rel["tag"]
    current = _parse_version(VERSION)
    latest = _parse_version(latest_tag)

    if latest <= current:
        ok(f"You already have the latest version (v{VERSION})!")
        return True

    print()
    ok(f"New cloud version found: {color(latest_tag, COLOR_GREEN, COLOR_BOLD)} (current: {color(VERSION, COLOR_YELLOW)})")
    if rel.get("body"):
        hint("Release notes:")
        for line in rel["body"].strip().splitlines()[:6]:
            print(f"    {line}")
        print()

    # Find asset for current platform
    assets = rel.get("assets", [])
    target_asset = None
    if os.name == "nt":
        for a in assets:
            aname = a.get("name", "").lower()
            if aname.endswith(".exe") and "patcher" in aname:
                target_asset = a
                break
        if not target_asset:
            for a in assets:
                if a.get("name", "").lower().endswith(".exe"):
                    target_asset = a
                    break
    elif sys.platform == "darwin":
        for a in assets:
            if "macos" in a.get("name", "").lower():
                target_asset = a
                break
    else:
        for a in assets:
            if "linux" in a.get("name", "").lower():
                target_asset = a
                break

    if not target_asset or not target_asset.get("browser_download_url"):
        warn(f"No direct binary asset found for this platform in release {latest_tag}.")
        hint(f"Open releases page to download manually: {color(rel.get('html_url') or RELEASES_URL, COLOR_CYAN)}")
        webbrowser.open(rel.get("html_url") or RELEASES_URL)
        return False

    dl_url = target_asset["browser_download_url"]
    file_name = target_asset["name"]
    file_size_mb = target_asset.get("size", 0) / (1024 * 1024)

    hint(f"Package: {color(file_name, COLOR_CYAN)} ({file_size_mb:.1f} MB)")

    print()
    info(f"Downloading {file_name} from GitHub...")
    import tempfile
    temp_dir = Path(tempfile.gettempdir())
    temp_file = temp_dir / f"ag_patcher_update_{latest_tag}.exe"

    try:
        req = urllib.request.Request(dl_url, headers={
            "User-Agent": f"AntigravityPatcher/{VERSION}",
            "Accept": "application/octet-stream"
        })
        with urllib.request.urlopen(req, timeout=30) as resp, open(temp_file, "wb") as out_f:
            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0
            chunk_size = 64 * 1024
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = int(downloaded * 100 / total_size)
                    mb = downloaded / (1024 * 1024)
                    tot_mb = total_size / (1024 * 1024)
                    sys.stdout.write(f"\r  [+] Downloading: {mb:.1f}/{tot_mb:.1f} MB ({pct}%)")
                    sys.stdout.flush()
        print()
        ok("Download complete!")
    except Exception as e:
        print()
        err(f"Download failed: {e}")
        return False

    # Apply update and restart
    current_exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()
    if getattr(sys, "frozen", False) and os.name == "nt":
        updater_bat = temp_dir / "apply_ag_update.bat"
        updater_bat.write_text(f"""@echo off
timeout /t 1 /nobreak >nul
move /y "{temp_file}" "{current_exe}" >nul
start "" "{current_exe}"
del "%~f0"
exit
""", encoding="ascii")
        ok("Restarting into updated version...")
        import subprocess
        subprocess.Popen(["cmd.exe", "/c", str(updater_bat)], creationflags=0x08000000 | 0x00000200)
        sys.exit(0)
    else:
        dest = Path.cwd() / file_name
        shutil.copy2(temp_file, dest)
        ok(f"New binary saved to: {color(str(dest), COLOR_CYAN)}")
        return True


def check_for_updates(silent=True, timeout=5):
    """Check for updates. Returns True if an update is available.

    If silent=True, only prints a message if an update is found.
    If silent=False, always prints the result.
    """
    global LAST_UPDATE_RESULT
    tag, url = _fetch_latest_release(timeout=timeout)

    if tag is None:
        LAST_UPDATE_RESULT = "network_error"
        if not silent:
            print_network_error_warning()
        return False

    current = _parse_version(VERSION)
    latest = _parse_version(tag)

    if latest > current:
        LAST_UPDATE_RESULT = ("update_available", tag, url)
        if not silent:
            info(f"New version available: {color(tag, COLOR_GREEN, COLOR_BOLD)} (current: {color(VERSION, COLOR_YELLOW)})")
            hint(f"Download: {color(url, COLOR_CYAN)}")
        return True

    LAST_UPDATE_RESULT = "up_to_date"
    if not silent:
        ok(f"Already up to date (v{VERSION})")
    return False


def open_releases_page():
    """Open the GitHub releases page in the default browser."""
    webbrowser.open(RELEASES_URL)
    ok(f"Opening: {color(RELEASES_URL, COLOR_CYAN)}")


def open_issues_page():
    """Open the GitHub issues page in the default browser."""
    webbrowser.open(ISSUES_URL)
    ok(f"Opening: {color(ISSUES_URL, COLOR_CYAN)}")


def handle_patch_failure():
    """Handle patch failure or signature not found.

    Checks for updates:
    - If an update is available, offers to open the releases page to install it.
    - If using the latest version (or no update available), offers to create an issue on GitHub.
    """
    from patcher.cli import confirmed

    print()
    info("Checking for patcher updates...")
    has_update = check_for_updates(silent=True)

    if has_update and isinstance(LAST_UPDATE_RESULT, tuple) and LAST_UPDATE_RESULT[0] == "update_available":
        _, tag, url = LAST_UPDATE_RESULT
        info(f"An update for Antigravity Patcher is available: {color(tag, COLOR_GREEN, COLOR_BOLD)} (current: {color(VERSION, COLOR_YELLOW)})")
        hint(f"Download: {color(url, COLOR_CYAN)}")
        if confirmed("A patcher update is available. Would you like to open the releases page to install it?"):
            webbrowser.open(url or RELEASES_URL)
            ok(f"Opening: {color(url or RELEASES_URL, COLOR_CYAN)}")
    else:
        if LAST_UPDATE_RESULT == "up_to_date":
            ok(f"You are using the latest version of Antigravity Patcher (v{VERSION}).")

        hint(f"Issue tracker: {color(ISSUES_URL, COLOR_CYAN)}")
        if confirmed("Would you like to open GitHub to create an issue?"):
            webbrowser.open(ISSUES_URL)
            ok(f"Opening: {color(ISSUES_URL, COLOR_CYAN)}")


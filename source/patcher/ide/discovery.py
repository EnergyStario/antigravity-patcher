import os
import sys
import json
import subprocess
import string
from enum import Enum
import re
from functools import total_ordering

_VERSION_RE = re.compile(
    r"^\s*[vV]?(?:(?P<epoch>\d+):)?(?P<release>\d+(?:\.\d+)*)"
    r"(?P<tail>[A-Za-z0-9._~+-]*)\s*$"
)
_PRE_TAG_ORDER = {"dev": -2, "a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "pre": 2, "preview": 2, "rc": 2}


@total_ordering
class Version:
    """Lightweight PEP-440 / SemVer comparable version without external 'packaging' dependency."""

    def __init__(self, version_str: str):
        if not isinstance(version_str, str):
            raise ValueError(f"Invalid version: {version_str!r}")
        m = _VERSION_RE.match(version_str)
        if not m:
            raise ValueError(f"Invalid version string: {version_str!r}")
        self.raw = version_str.strip()
        self.epoch = int(m.group("epoch") or 0)
        self.release = tuple(int(p) for p in m.group("release").split("."))
        tail = m.group("tail") or ""
        self._key = self._build_sort_key(tail)

    @property
    def major(self) -> int:
        return self.release[0] if len(self.release) > 0 else 0

    @property
    def minor(self) -> int:
        return self.release[1] if len(self.release) > 1 else 0

    @property
    def micro(self) -> int:
        return self.release[2] if len(self.release) > 2 else 0

    def _build_sort_key(self, tail: str):
        rel = list(self.release)
        while len(rel) > 1 and rel[-1] == 0:
            rel.pop()

        core_tail = tail.split("+", 1)[0].strip()
        if not core_tail:
            stage = (0, 0, 0)
        else:
            clean_tail = core_tail.lstrip(".-_~")
            pre_m = re.match(r"^(alpha|beta|preview|dev|pre|rc|a|b|c)[._-]?(\d*)", clean_tail, re.I)
            post_m = re.match(r"^post[._-]?(\d*)", clean_tail, re.I)
            deb_rev_m = re.match(r"^-(\d+)(?:[A-Za-z].*)?$", core_tail)
            if pre_m:
                tag = pre_m.group(1).lower()
                num = int(pre_m.group(2) or 0)
                if tag == "dev":
                    stage = (-2, 0, num)
                else:
                    stage = (-1, _PRE_TAG_ORDER.get(tag, 3), num)
            elif post_m:
                stage = (1, int(post_m.group(1) or 0), 0)
            elif deb_rev_m:
                stage = (1, int(deb_rev_m.group(1)), 0)
            else:
                stage = (-1, 0, 0)

        return (self.epoch, tuple(rel), stage)

    def __eq__(self, other) -> bool:
        return isinstance(other, Version) and self._key == other._key

    def __lt__(self, other) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key < other._key

    def __repr__(self) -> str:
        return f"Version({self.raw!r})"

    def __str__(self) -> str:
        return self.raw
from patcher.constants import AG_REGISTRY_SUBKEY, MIN_AG_VERSION
from patcher.utils.file import get_posix_invoking_user_home
from patcher.utils.console import ok


class VersionStatus(Enum):
    OK = "ok"
    TOO_OLD = "too_old"
    NOT_FOUND = "not_found"
    PARSE_ERROR = "parse_error"


def clean_path(raw_path):
    return raw_path.strip().strip('"').strip("'")


def find_portable_candidates(search_type="ide"):
    """Ищет портативные версии в пользовательских папках и на других дисках."""
    roots = []

    # 1. Получаем домашнюю папку пользователя
    home = ""
    if sys.platform != "win32":
        home = get_posix_invoking_user_home()
    if not home:
        home = os.path.expanduser("~")

    # Добавляем специфичные папки пользователя (включая русские локализации и нижний регистр)
    if home and os.path.isdir(home):
        standard_subs = [
            "Downloads", "Загрузки", "downloads", "загрузки",
            "Desktop", "Рабочий стол", "desktop", "рабочий стол",
            "Documents", "Документы", "documents", "документы"
        ]
        for sub in standard_subs:
            p = os.path.join(home, sub)
            if os.path.isdir(p):
                roots.append(p)

    # 2. На Linux просим систему дать точные пути через xdg-user-dir
    if sys.platform != "win32" and os.name == "posix":
        sudo_user = os.environ.get("SUDO_USER")
        for folder_type in ["DOWNLOAD", "DESKTOP", "DOCUMENTS"]:
            try:
                cmd = ["xdg-user-dir", folder_type]
                if sudo_user:
                    cmd = ["sudo", "-u", sudo_user, "xdg-user-dir", folder_type]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
                if res.returncode == 0:
                    path = res.stdout.strip()
                    if path and os.path.isdir(path):
                        roots.append(path)
            except Exception:
                pass

    # 3. Текущая директория запуска
    cwd = os.getcwd()
    if cwd and os.path.isdir(cwd):
        roots.append(cwd)

    # 4. Корни других дисков для Windows
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            if letter == "C":
                continue
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append(drive)

    # 5. Домашняя папка целиком (как крайний вариант в самом конце)
    if home and os.path.isdir(home):
        roots.append(home)

    # Убираем дубликаты, сохраняя порядок приоритетности
    seen = set()
    unique_roots = []
    for r in roots:
        r_abs = os.path.abspath(r)
        if r_abs not in seen:
            seen.add(r_abs)
            unique_roots.append(r_abs)
    roots = unique_roots

    candidates = []
    visited_dirs = 0
    max_dirs = 1500

    # Исключаем тяжелые/системные папки (предвычисляем множество имён в нижнем регистре)
    prune_dirs = {
        ".git", "node_modules", "AppData", "Application Data", "Library",
        "System Volume Information", "$RECYCLE.BIN", "Windows", "Program Files",
        "Program Files (x86)", "usr", "var", "sys", "proc", "dev", "dist", "build",
        "__pycache__", ".idea", ".vscode"
    }
    prune_lower = {p.lower() for p in prune_dirs}

    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            visited_dirs += 1
            if visited_dirs > max_dirs:
                return candidates

            # Фильтруем dirnames на месте
            dirnames[:] = [d for d in dirnames if d.lower() not in prune_lower and not d.startswith('.')]

            # Вычисляем глубину относительно корня поиска
            try:
                rel = os.path.relpath(dirpath, root)
                if rel == ".":
                    depth = 0
                else:
                    depth = len(rel.replace("\\", "/").split("/"))
            except ValueError:
                depth = 999

            if depth >= 3:
                dirnames[:] = []  # Не спускаемся глубже

            # Проверяем, подходит ли папка под критерий эвристики
            is_match = "antigravity" in dirpath.lower() or dirpath == root

            if is_match:
                if search_type == "ide":
                    # Ищем признаки Antigravity IDE
                    for sub in [
                        os.path.join("resources", "app", "out", "main.js"),
                        os.path.join("resources", "app", "main.js"),
                        os.path.join("out", "main.js"),
                        "main.js",
                    ]:
                        if os.path.exists(os.path.join(dirpath, sub)):
                            if dirpath not in candidates:
                                candidates.append(dirpath)
                                label = "Antigravity IDE"
                                try:
                                    ok(f"Found portable {label} at: {dirpath}")
                                except Exception:
                                    print(f"  [+] Found portable {label} at: {dirpath}")
                                break
                elif search_type == "antigravity":
                    # Ищем признаки Antigravity
                    if os.path.exists(os.path.join(dirpath, "resources", "app.asar")) or \
                       os.path.exists(os.path.join(dirpath, "resources", "app1.asar")):
                        if dirpath not in candidates:
                            candidates.append(dirpath)
                            label = "Antigravity"
                            try:
                                ok(f"Found portable {label} at: {dirpath}")
                            except Exception:
                                print(f"  [+] Found portable {label} at: {dirpath}")

    return candidates


def find_install_root():
    candidates = []

    if sys.platform == "darwin":
        # На macOS приложение — .app-бандл, main.js лежит внутри Contents/Resources/app
        mac_candidates = [
            "/Applications/Antigravity IDE.app",
            os.path.expanduser("~/Applications/Antigravity IDE.app"),
            "/Applications/antigravity ide.app",
            os.path.expanduser("~/Applications/antigravity ide.app"),
        ]
        for app in mac_candidates:
            candidates.append(os.path.join(app, "Contents", "Resources", "app"))
    elif os.name == "posix":
        candidates.extend([
            "/usr/share/antigravity-ide",
            "/opt/Antigravity IDE",
            "/opt/antigravity-ide",
            "/opt/antigravity ide",
            "/opt/Antigravity IDE/resources/app/out",
        ])

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "Programs", "Antigravity IDE"))
            candidates.append(os.path.join(local_app_data, "Antigravity IDE"))
        app_data = os.environ.get("APPDATA")
        if app_data:
            candidates.append(os.path.join(app_data, "Antigravity IDE"))
        pf = os.environ.get("PROGRAMFILES")
        if pf:
            candidates.append(os.path.join(pf, "Antigravity IDE"))
        pfx86 = os.environ.get("PROGRAMFILES(X86)")
        if pfx86:
            candidates.append(os.path.join(pfx86, "Antigravity IDE"))

        try:
            import winreg
            # 1. Hardcoded known GUID
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, AG_REGISTRY_SUBKEY) as key:
                        install_loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                        if install_loc and install_loc.strip():
                            candidates.append(install_loc.strip())
                except OSError:
                    pass

            # 2. Dynamic Uninstall enumeration for any DisplayName containing 'Antigravity IDE'
            subkeys = [
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ]
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for sk in subkeys:
                    try:
                        with winreg.OpenKey(hive, sk) as root_key:
                            for i in range(winreg.QueryInfoKey(root_key)[0]):
                                try:
                                    subkey_name = winreg.EnumKey(root_key, i)
                                    with winreg.OpenKey(root_key, subkey_name) as app_key:
                                        try:
                                            dname, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                            if dname and "antigravity ide" in str(dname).lower():
                                                try:
                                                    iloc, _ = winreg.QueryValueEx(app_key, "InstallLocation")
                                                    if iloc and iloc.strip():
                                                        candidates.append(iloc.strip())
                                                except OSError:
                                                    pass
                                        except OSError:
                                            pass
                                except OSError:
                                    pass
                    except OSError:
                        pass
        except ImportError:
            pass

    for path in candidates:
        for sub in [
            os.path.join("resources", "app", "out", "main.js"),
            os.path.join("resources", "app", "main.js"),
            os.path.join("out", "main.js"),
            "main.js",
        ]:
            if os.path.exists(os.path.join(path, sub)):
                return path

    # Fallback to portable search only if standard paths did not match
    portable = find_portable_candidates("ide")
    if portable:
        return portable[0]

    return ""


def find_main_js(root):
    # macOS: пользователь может передать путь к .app-бандлу напрямую
    if sys.platform == "darwin" and root.lower().endswith(".app") and os.path.isdir(root):
        root = os.path.join(root, "Contents", "Resources", "app")

    for sub in [
        os.path.join("resources", "app", "out", "main.js"),
        os.path.join("resources", "app", "main.js"),
        os.path.join("out", "main.js"),
        "main.js",
    ]:
        p = os.path.join(root, sub)
        if os.path.exists(p):
            return p
    return ""


def get_ag_version(main_js_path):
    """Читает версию Antigravity IDE из реестра Windows, Info.plist на macOS или package.json на Linux.
    Возвращает (version_str, is_pkg_mgr).
    """
    # macOS: читаем версию из Info.plist
    if sys.platform == "darwin":
        # Ищем .app-бандл, поднимаясь от main.js
        app_path = ""
        p = os.path.dirname(main_js_path)
        while p and p != os.path.dirname(p):
            if p.endswith(".app"):
                app_path = p
                break
            p = os.path.dirname(p)

        if app_path:
            plist_path = os.path.join(app_path, "Contents", "Info.plist")
            if os.path.exists(plist_path):
                try:
                    import plistlib
                    with open(plist_path, "rb") as f:
                        plist = plistlib.load(f)
                    ver = plist.get("CFBundleShortVersionString", "").strip()
                    if not ver:
                        ver = plist.get("CFBundleVersion", "").strip()
                    if ver:
                        return ver, False
                except Exception:
                    pass

        # Fallback: package.json
        for rel in (
            os.path.join(os.path.dirname(main_js_path), "..", "package.json"),
            os.path.join(os.path.dirname(main_js_path), "package.json"),
        ):
            pkg = os.path.normpath(rel)
            if os.path.exists(pkg):
                try:
                    with open(pkg, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ver = data.get("version", "").strip()
                    if ver:
                        return ver, False
                except Exception:
                    pass
        return None, False

    if os.name == "posix":
        # Пробуем менеджеры пакетов (apt, rpm) с разными именами
        pkg_names = ["antigravity-ide", "antigravity-ide-bin", "antigravity-ide-custom"]

        # dpkg-query (Debian/Ubuntu)
        for pkg in pkg_names:
            try:
                result = subprocess.run(
                    ["dpkg-query", "-W", "-f=${Version}", pkg],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    ver = result.stdout.strip()
                    if ver:
                        return ver, True
            except Exception:
                pass

        # rpm (Fedora/RHEL/openSUSE)
        for pkg in pkg_names:
            try:
                result = subprocess.run(
                    ["rpm", "-q", "--queryformat", "%{VERSION}", pkg],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    ver = result.stdout.strip()
                    if ver:
                        return ver, True
            except Exception:
                pass

        # Fallback: package.json (portable / snap / flatpak)
        for rel in (
            os.path.join(os.path.dirname(main_js_path), "..", "package.json"),
            os.path.join(os.path.dirname(main_js_path), "package.json"),
        ):
            pkg = os.path.normpath(rel)
            if os.path.exists(pkg):
                try:
                    with open(pkg, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ver = data.get("version", "").strip()
                    if ver:
                        return ver, False
                except Exception:
                    pass
        return None, False

    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, AG_REGISTRY_SUBKEY) as key:
                        display_ver, _ = winreg.QueryValueEx(key, "DisplayVersion")
                        if display_ver and display_ver.strip():
                            return display_ver.strip(), True
                except OSError:
                    pass
        except ImportError:
            pass

        # Fallback Windows: product.json (ideVersion or version)
        for rel in (
            os.path.join(os.path.dirname(main_js_path), "..", "product.json"),
            os.path.join(os.path.dirname(main_js_path), "product.json"),
        ):
            prod = os.path.normpath(rel)
            if os.path.exists(prod):
                try:
                    with open(prod, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ver = data.get("ideVersion", "").strip() or data.get("version", "").strip()
                    if ver:
                        return ver, False
                except Exception:
                    pass

        # Fallback Windows: package.json
        for rel in (
            os.path.join(os.path.dirname(main_js_path), "..", "package.json"),
            os.path.join(os.path.dirname(main_js_path), "package.json"),
        ):
            pkg = os.path.normpath(rel)
            if os.path.exists(pkg):
                try:
                    with open(pkg, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ver = data.get("version", "").strip()
                    if ver:
                        return ver, False
                except Exception:
                    pass

    return None, False


def check_ag_version(main_js_path):
    """
    Проверяет версию Antigravity IDE.
    Возвращает (VersionStatus, detected_version_str | None).
    """
    ver_str, is_pkg_mgr = get_ag_version(main_js_path)

    if ver_str is None:
        return VersionStatus.NOT_FOUND, None

    try:
        detected = Version(ver_str)

        # Минимальная версия зависит от платформы:
        # - macOS: 1.107.0 (кастомные билды через Homebrew/DMG)
        # - Linux без пакетного менеджера: 1.107.0 (кастомные билды)
        # - Иначе: MIN_AG_VERSION (из реестра Windows или пакетного менеджера)
        min_ver_str = MIN_AG_VERSION
        if sys.platform == "darwin" and not is_pkg_mgr:
            min_ver_str = "1.107.0"
        elif os.name == "posix" and sys.platform != "darwin" and not is_pkg_mgr:
            min_ver_str = "1.107.0"

        minimum = Version(min_ver_str)
        status = VersionStatus.OK if detected >= minimum else VersionStatus.TOO_OLD
        return status, ver_str
    except Exception:
        return VersionStatus.PARSE_ERROR, ver_str


def parse_version_safe(ver_str):
    if not ver_str:
        return None
    try:
        return Version(ver_str)
    except Exception:
        return None


def resolve_target_path(raw_path):
    if not raw_path:
        return ""
    cleaned = clean_path(raw_path)
    if not cleaned:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    resolved = os.path.abspath(expanded)
    
    # Если указана директория, пробуем найти в ней main.js
    if os.path.isdir(resolved):
        found = find_main_js(resolved)
        return found if found else resolved
    return resolved


import os
import sys
import json
import time
import shutil
import zipfile
from html import escape
from pathlib import Path
from urllib.parse import quote
from patcher.utils.console import ok, info, warn, err, step, hint, color
from patcher.constants import COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, VERSION

_CORE_EXTENSION_FILES = ("account_switcher.py", "switch_runner.py", "nrpt_dns.py", "quota_engine.js", "swarm_bridge.js", "error_guardian.js")

_VSIX_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension=".js" ContentType="application/javascript"/>'
    '<Default Extension=".json" ContentType="application/json"/>'
    '<Default Extension=".md" ContentType="text/markdown"/>'
    '<Default Extension=".png" ContentType="image/png"/>'
    '<Default Extension=".py" ContentType="application/octet-stream"/>'
    '<Default Extension=".pyc" ContentType="application/octet-stream"/>'
    '<Default Extension=".svg" ContentType="image/svg+xml"/>'
    '<Default Extension=".txt" ContentType="text/plain"/>'
    '<Default Extension=".vsixmanifest" ContentType="text/xml"/>'
    "</Types>\n"
)


def _find_extensions_root() -> Path | None:
    if hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / "extensions"
        if p.exists():
            return p
    base = Path(__file__).resolve().parent.parent.parent / "extensions"
    if base.exists():
        return base
    cwd_ext = Path.cwd() / "extensions"
    if cwd_ext.exists():
        return cwd_ext
    return None


def get_bundled_extension_dir() -> Path | None:
    ext_root = _find_extensions_root()
    if not ext_root:
        return None
    for sub in ("ide", "ag-toolkit"):
        p = ext_root / sub
        if p.exists():
            return p
    return None


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if src.exists() and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    return False


def _find_swarm_dir() -> Path | None:
    """Locate canonical source/patcher/swarm package directory (dev tree or PyInstaller _MEIPASS)."""
    candidates: list[Path] = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "swarm_engine" / "swarm")
    candidates.extend([
        Path(__file__).resolve().parent / "swarm",
        Path.cwd() / "source" / "patcher" / "swarm",
    ])
    for cand in candidates:
        if cand.is_dir() and (cand / "ipc_server.py").is_file():
            return cand
    return None


def _iter_swarm_py_files() -> list[Path]:
    """Return sorted top-level Python source files inside the canonical swarm engine directory."""
    swarm_dir = _find_swarm_dir()
    if not swarm_dir:
        return []
    return [p for p in sorted(swarm_dir.glob("*.py")) if p.is_file()]


def _resolve_common_repo_root(start_dir: Path) -> Path | None:
    """Resolve the main repository root when running inside a git worktree checkout."""
    git_marker = start_dir / ".git"
    if not git_marker.is_file():
        return None
    try:
        first_line = git_marker.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        if not first_line.lower().startswith("gitdir:"):
            return None
        gitdir = Path(first_line.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (start_dir / gitdir).resolve()
        cur = gitdir
        while cur != cur.parent:
            if cur.name == ".git":
                return cur.parent
            cur = cur.parent
    except Exception:
        return None
    return None


def _find_ag_swarm_skill() -> Path | None:
    """Locate the ag-swarm SKILL.md for global Antigravity skill deployment."""
    candidates: list[Path] = []
    if hasattr(sys, "_MEIPASS"):
        candidates.extend([
            Path(sys._MEIPASS) / "ag-swarm" / "SKILL.md",
            Path(sys._MEIPASS) / "swarm_engine" / "swarm" / "SKILL.md",
        ])
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidates.extend([
        Path(__file__).resolve().parent / "swarm" / "SKILL.md",
        repo_root / ".agents" / "skills" / "ag-swarm" / "SKILL.md",
        Path.cwd() / ".agents" / "skills" / "ag-swarm" / "SKILL.md",
    ])
    for wt_base in (repo_root, Path.cwd()):
        common_root = _resolve_common_repo_root(wt_base)
        if common_root:
            candidates.append(common_root / ".agents" / "skills" / "ag-swarm" / "SKILL.md")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _copy_swarm_engine(dest_parent_dir: Path) -> bool:
    """Copy canonical swarm Python modules into <dest_parent_dir>/swarm_engine/swarm/."""
    py_files = _iter_swarm_py_files()
    if not py_files:
        return False
    swarm_dest = dest_parent_dir / "swarm_engine" / "swarm"
    swarm_dest.mkdir(parents=True, exist_ok=True)
    copied = False
    for py_file in py_files:
        if _copy_if_exists(py_file, swarm_dest / py_file.name):
            copied = True
    return copied


def _deploy_global_ag_swarm_skill(home: Path | None = None) -> bool:
    """Deploy ag-swarm SKILL.md to ~/.gemini/antigravity/skills/ag-swarm/SKILL.md."""
    skill_src = _find_ag_swarm_skill()
    if not skill_src:
        return False
    target = (home or Path.home()) / ".gemini" / "antigravity" / "skills" / "ag-swarm" / "SKILL.md"
    return _copy_if_exists(skill_src, target)


def build_vsix_archive(version: str = VERSION, out_path: Path | None = None) -> Path | None:
    """Build ag-toolkit-<version>.vsix dynamically from canonical extensions/ide + extensions/core."""
    ext_root = _find_extensions_root()
    ide_dir = get_bundled_extension_dir()
    if not ide_dir or not ide_dir.exists():
        return None
    core_dir = (ext_root / "core") if ext_root else None

    pkg_file = ide_dir / "package.json"
    if not pkg_file.is_file():
        return None

    pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
    pub = escape(str(pkg.get("publisher", "energystar")))
    name = escape(str(pkg.get("name", "ag-toolkit")))
    disp = escape(str(pkg.get("displayName", "Antigravity Assistant")))
    desc = escape(str(pkg.get("description", "")))
    tags = escape(",".join(pkg.get("keywords", [])))
    cats = escape(",".join(pkg.get("categories", ["Other"])))
    engine = escape(str((pkg.get("engines") or {}).get("vscode", "^1.80.0")))
    repo_raw = str(((pkg.get("repository") or {}).get("url")) or "").rstrip("/")
    repo_git = escape(repo_raw if repo_raw.endswith(".git") else (repo_raw + ".git" if repo_raw else ""))
    repo_web = escape(repo_raw[:-4] if repo_raw.endswith(".git") else repo_raw)

    manifest_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">\n'
        "  <Metadata>\n"
        f'    <Identity Language="en-US" Id="{name}" Version="{escape(version)}" Publisher="{pub}" />\n'
        f"    <DisplayName>{disp}</DisplayName>\n"
        f'    <Description xml:space="preserve">{desc}</Description>\n'
        f"    <Tags>{tags}</Tags>\n"
        f"    <Categories>{cats}</Categories>\n"
        "    <GalleryFlags>Public</GalleryFlags>\n"
        "    <Properties>\n"
        f'      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}" />\n'
        '      <Property Id="Microsoft.VisualStudio.Code.ExecutesCode" Value="true" />\n'
        f'      <Property Id="Microsoft.VisualStudio.Services.Links.Source" Value="{repo_git}" />\n'
        f'      <Property Id="Microsoft.VisualStudio.Services.Links.GitHub" Value="{repo_git}" />\n'
        f'      <Property Id="Microsoft.VisualStudio.Services.Links.Support" Value="{repo_web}/issues" />\n'
        f'      <Property Id="Microsoft.VisualStudio.Services.Links.Learn" Value="{repo_web}#readme" />\n'
        '      <Property Id="Microsoft.VisualStudio.Services.GitHubFlavoredMarkdown" Value="true" />\n'
        '      <Property Id="Microsoft.VisualStudio.Services.Content.Pricing" Value="Free"/>\n'
        "    </Properties>\n"
        "    <License>extension/LICENSE</License>\n"
        "    <Icon>extension/icon.png</Icon>\n"
        "  </Metadata>\n"
        "  <Installation>\n"
        '    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>\n'
        "  </Installation>\n"
        "  <Dependencies/>\n"
        "  <Assets>\n"
        '    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />\n'
        '    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />\n'
        '    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE" Addressable="true" />\n'
        '    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/icon.png" Addressable="true" />\n'
        "  </Assets>\n"
        "</PackageManifest>\n"
    )

    dest_vsix = out_path or (Path.cwd() / f"ag-toolkit-{version}.vsix")
    dest_vsix.parent.mkdir(parents=True, exist_ok=True)
    tmp_vsix = dest_vsix.with_name(f".{dest_vsix.name}.{os.getpid()}.tmp")

    # Collect canonical files without duplicates (core overlays ide)
    arc_sources: dict[str, Path] = {}
    for item in sorted(ide_dir.rglob("*")):
        if not item.is_file() or "__pycache__" in item.parts:
            continue
        if item.suffix.lower() in (".pyc", ".pyo", ".vsix", ".log", ".tmp"):
            continue
        rel = item.relative_to(ide_dir).as_posix()
        arc_sources[f"extension/{rel}"] = item

    if core_dir and core_dir.is_dir():
        for fname in _CORE_EXTENSION_FILES:
            src_f = core_dir / fname
            if src_f.is_file():
                arc_sources[f"extension/{fname}"] = src_f

    for py_file in _iter_swarm_py_files():
        arc_sources[f"extension/swarm_engine/swarm/{py_file.name}"] = py_file

    skill_src = _find_ag_swarm_skill()
    if skill_src:
        arc_sources["extension/skills/ag-swarm/SKILL.md"] = skill_src

    try:
        with zipfile.ZipFile(tmp_vsix, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", _VSIX_CONTENT_TYPES_XML.encode("utf-8"))
            zf.writestr("extension.vsixmanifest", manifest_xml.encode("utf-8"))
            for arc_name in sorted(arc_sources):
                zf.write(arc_sources[arc_name], arc_name)
        os.replace(tmp_vsix, dest_vsix)
        return dest_vsix
    finally:
        if tmp_vsix.exists():
            try:
                tmp_vsix.unlink()
            except OSError:
                pass


def _sync_extensions_registry(ext_root: Path, installed_dir: Path) -> None:
    """Keep existing VS Code / Antigravity IDE extensions.json and .obsolete in sync."""
    obsolete_file = ext_root / ".obsolete"
    if obsolete_file.is_file():
        try:
            obs = json.loads(obsolete_file.read_text(encoding="utf-8"))
            if isinstance(obs, dict):
                cleaned = {
                    k: v
                    for k, v in obs.items()
                    if not k.startswith("energystar.ag-toolkit")
                }
                if len(cleaned) != len(obs):
                    obsolete_file.write_text(
                        json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
        except Exception:
            pass

    registry_file = ext_root / "extensions.json"
    if not registry_file.is_file():
        return

    try:
        entries = json.loads(registry_file.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return

        filtered = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ident = entry.get("identifier") or {}
            ext_id = ident.get("id", "") if isinstance(ident, dict) else ""
            rel_loc = entry.get("relativeLocation", "")
            if ext_id == "energystar.ag-toolkit" or (
                isinstance(rel_loc, str)
                and rel_loc.startswith("energystar.ag-toolkit")
            ):
                continue
            filtered.append(entry)

        resolved = installed_dir.resolve()
        if os.name == "nt":
            fs_path = str(resolved)
            if len(fs_path) >= 2 and fs_path[1] == ":":
                fs_path = fs_path[0].lower() + fs_path[1:]
            posix_str = fs_path.replace("\\", "/")
            uri_path = "/" + posix_str
            external_uri = "file:///" + quote(posix_str, safe="/")
            loc_obj = {
                "$mid": 1,
                "fsPath": fs_path,
                "_sep": 1,
                "external": external_uri,
                "path": uri_path,
                "scheme": "file",
            }
        else:
            loc_obj = {
                "$mid": 1,
                "fsPath": str(resolved),
                "external": resolved.as_uri(),
                "path": resolved.as_posix(),
                "scheme": "file",
            }

        filtered.append({
            "identifier": {"id": "energystar.ag-toolkit"},
            "version": VERSION,
            "location": loc_obj,
            "relativeLocation": installed_dir.name,
            "metadata": {
                "isApplicationScoped": False,
                "isMachineScoped": False,
                "isBuiltin": False,
                "installedTimestamp": int(time.time() * 1000),
                "pinned": True,
                "source": "vsix",
            },
        })
        registry_file.write_text(
            json.dumps(filtered, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        pass


def deploy_assistant_extension(export_vsix: bool = False) -> bool:
    ext_root = _find_extensions_root()
    ext_src = get_bundled_extension_dir()
    if not ext_src or not ext_src.exists():
        warn("Antigravity Assistant extension bundle not found.")
        return False

    core_dir = (ext_root / "core") if ext_root else None

    home = Path.home()
    ext_roots = [
        home / ".antigravity-ide" / "extensions",
        home / ".vscode" / "extensions",
    ]
    for opt_root in [
        home / ".antigravity" / "extensions",
        home / ".vscode-insiders" / "extensions",
        home / ".cursor" / "extensions",
        home / ".windsurf" / "extensions",
        home / ".vscodium" / "extensions",
    ]:
        if opt_root.parent.is_dir() or opt_root.is_dir():
            ext_roots.append(opt_root)
    target_name = f"energystar.ag-toolkit-{VERSION}"

    installed_any = False
    for root_dir in ext_roots:
        t = root_dir / target_name
        try:
            root_dir.mkdir(parents=True, exist_ok=True)
            for existing in root_dir.iterdir():
                if existing.is_dir() and existing.name != target_name and existing.name.startswith("energystar.ag-toolkit"):
                    shutil.rmtree(existing, ignore_errors=True)

            t.mkdir(parents=True, exist_ok=True)
            for item in ext_src.iterdir():
                if item.name.endswith(".vsix") or item.name == "__pycache__":
                    continue
                dest = t / item.name
                if item.is_dir():
                    shutil.copytree(
                        item,
                        dest,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                    )
                else:
                    shutil.copy2(item, dest)

            # Overlay shared core modules so IDE always gets the canonical engine
            if core_dir and core_dir.exists():
                for py_name in _CORE_EXTENSION_FILES:
                    _copy_if_exists(core_dir / py_name, t / py_name)

            _copy_swarm_engine(t)
            _sync_extensions_registry(root_dir, t)
            ok(f"Extension deployed to: {t.parent.name}/{t.name}")
            installed_any = True
        except Exception as e:
            warn(f"Could not deploy to {t}: {e}")

    if export_vsix:
        try:
            vsix_dest = Path.cwd() / f"ag-toolkit-{VERSION}.vsix"
            built = build_vsix_archive(VERSION, vsix_dest)
            if built and built.is_file():
                ok(f"Exported VSIX file: {built.name}")
        except Exception as e:
            warn(f"Could not export VSIX: {e}")

    deploy_antigravity_2_ui()
    return installed_any


def deploy_antigravity_2_ui() -> bool:
    """Deploy Antigravity 2.0 Desktop UI, shared core engine, Swarm Engine & ag-swarm skill."""
    ext_root = _find_extensions_root()
    ext_src = get_bundled_extension_dir()
    core_dir = (ext_root / "core") if ext_root else None
    desktop_dir = (ext_root / "desktop") if ext_root else None

    home = Path.home()
    ag_tools = home / ".antigravity_tools"
    ag_tools.mkdir(parents=True, exist_ok=True)

    copied = False
    if ext_root or ext_src:
        file_sources = {
            "ag_desktop_ui.js": [d for d in (desktop_dir, ext_src) if d],
            "quota_engine.js": [d for d in (core_dir, ext_src) if d],
            "swarm_bridge.js": [d for d in (core_dir, ext_src) if d],
            "error_guardian.js": [d for d in (core_dir, ext_src) if d],
            "account_switcher.py": [d for d in (core_dir, ext_src) if d],
            "switch_runner.py": [d for d in (core_dir, ext_src) if d],
            "nrpt_dns.py": [d for d in (core_dir, ext_src) if d],
        }

        for fname, dirs in file_sources.items():
            for src_dir in dirs:
                if _copy_if_exists(src_dir / fname, ag_tools / fname):
                    copied = True
                    break

    if _copy_swarm_engine(ag_tools):
        copied = True
    if _deploy_global_ag_swarm_skill(home):
        copied = True

    if copied:
        ok("Antigravity 2.0 Desktop UI & Swarm scripts deployed to ~/.antigravity_tools")
    return copied

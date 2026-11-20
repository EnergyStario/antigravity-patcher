"""
source/patcher/swarm/affected_tests.py
Affected test detection — finds which test files actually need to run
for a given set of changed source files.

Design:
  - Builds a dependency map: source_file → {test_files that import it}
  - Uses Python's AST import analysis (zero overhead, no subprocess)
  - Falls back to full suite if map is unavailable or file not found
  - Per-worker: narrows test_command to only relevant test files
  - Final integration check always runs full suite (never narrowed)

Usage:
    detector = AffectedTestDetector(project_path)
    test_files = detector.get_affected_tests(["source/patcher/ide/patcher.py"])
    # → ["tests/test_ide_patcher.py"]  (instead of all 115 tests)
"""

from __future__ import annotations

import ast
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Directories to skip when scanning tests
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".pytest_cache", ".mypy_cache"}

_JS_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.js)(?![A-Za-z0-9_.-])"
)
_EXT_JS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(extensions/(?:[^\s/'\"`<>()\[\]{},;]+/)*([A-Za-z0-9_.-]+\.js))(?![A-Za-z0-9_.-])"
)


# ---------------------------------------------------------------------------
# Dependency scanner (pure AST — no imports executed)
# ---------------------------------------------------------------------------

def _extract_imports(source_path: Path) -> list[str]:
    """
    Parse a Python file and return all module names it imports.
    Safe: uses AST, never executes the file.

    Returns list of dotted module names, e.g. ["source.patcher.ide.patcher", "os"].
    For `from source.patcher.ide import patcher`, returns both
    "source.patcher.ide" AND "source.patcher.ide.patcher" so that we can
    find the actual file (patcher.py) even when it's imported as a submodule.
    """
    try:
        source = source_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(source_path))
    except SyntaxError:
        return []
    except Exception:
        return []

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
                # Also add "module.name" for each imported name —
                # e.g. `from source.patcher.ide import patcher`
                # → "source.patcher.ide" AND "source.patcher.ide.patcher"
                # This covers the case where `patcher` is a submodule (file).
                for alias in node.names:
                    if alias.name != "*":
                        names.append(f"{node.module}.{alias.name}")
    return names


def _module_to_path_candidates(module_name: str, project_root: Path) -> list[Path]:
    """
    Convert a dotted module name to candidate file paths relative to project root.

    For "source.patcher.ide.patcher" generates:
      - project_root/source/patcher/ide/patcher.py          ← submodule file
      - project_root/source/patcher/ide/patcher/__init__.py ← subpackage
      - project_root/source/patcher/ide/__init__.py         ← parent package
      - project_root/source/patcher/ide.py                  ← package-as-file

    This broad set ensures we catch both direct and transitive dependencies.
    """
    parts = module_name.split(".")
    candidates: list[Path] = []

    # Walk from most specific to least specific
    for depth in range(len(parts), 0, -1):
        prefix = parts[:depth]
        # As a .py file: source/patcher/ide/patcher.py
        candidates.append(project_root.joinpath(*prefix).with_suffix(".py"))
        # As a package __init__: source/patcher/ide/patcher/__init__.py
        candidates.append(project_root.joinpath(*prefix, "__init__.py"))

    return candidates


def _collect_extension_js_files(project_root: Path) -> list[tuple[str, str]]:
    """Collect existing *.js files under extensions/ as (relative_path, filename)."""
    extensions_dir = project_root / "extensions"
    if not extensions_dir.exists():
        return []

    js_files: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(extensions_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(".js"):
                js_path = Path(dirpath) / fname
                js_rel = str(js_path.relative_to(project_root)).replace("\\", "/")
                js_files.append((js_rel, js_path.name))
    return js_files


def _match_extension_js_refs(
    test_path: Path,
    js_files: list[tuple[str, str]],
) -> list[str]:
    """Return relative paths of extension JS files referenced in test_path."""
    if not js_files:
        return []

    try:
        test_text = test_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    normalized_text = re.sub(r"\\+", "/", test_text)

    explicit_paths: set[str] = set()
    explicit_by_name: dict[str, set[str]] = {}
    for match in _EXT_JS_PATH_RE.finditer(normalized_text):
        ext_path, ext_name = match.group(1), match.group(2)
        explicit_paths.add(ext_path)
        explicit_by_name.setdefault(ext_name, set()).add(ext_path)

    js_tokens = set(_JS_FILENAME_RE.findall(normalized_text))

    matched: list[str] = []
    for js_rel, js_name in js_files:
        if js_rel in explicit_paths or (
            js_rel in normalized_text
            and bool(
                re.search(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(js_rel)}(?![A-Za-z0-9_.-])",
                    normalized_text,
                )
            )
        ):
            matched.append(js_rel)
        elif js_name in js_tokens and not explicit_by_name.get(js_name):
            matched.append(js_rel)

    return matched


# ---------------------------------------------------------------------------
# Affected test detector
# ---------------------------------------------------------------------------

class AffectedTestDetector:
    """
    Builds a reverse dependency map (source file → test files) and uses it
    to narrow down which tests need to run for a given set of changed files.

    The map is built lazily on first use and cached for the lifetime of the object.
    Re-scan with detector.invalidate() after major file additions/removals.

    Performance:
      - Scan ~200 files: < 0.5 sec (pure AST, no subprocess)
      - Query: O(n) where n = number of changed files

    Accuracy:
      - Finds direct imports only (not dynamic imports like importlib.import_module)
      - Falls back to full suite for unrecognised files
    """

    def __init__(self, project_path: str | Path, tests_dir: str = "tests"):
        self.project_path = Path(project_path)
        self.tests_dir = self.project_path / tests_dir
        # Map: normalized relative source path → set of test file paths (relative)
        self._source_to_tests: Optional[dict[str, set[str]]] = None

    def invalidate(self) -> None:
        """Force rebuild of dependency map on next query."""
        self._source_to_tests = None

    def _build_map(self) -> dict[str, set[str]]:
        """
        Scan all test files and build reverse map:
            source_file (relative str) → {test_file (relative str), ...}
        """
        result: dict[str, set[str]] = {}

        if not self.tests_dir.exists():
            log.warning("AffectedTestDetector: tests dir not found: %s", self.tests_dir)
            return result

        test_files = []
        for dirpath, dirnames, filenames in os.walk(self.tests_dir):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if fname.startswith("test_") and fname.endswith(".py"):
                    test_files.append(Path(dirpath) / fname)

        js_files = _collect_extension_js_files(self.project_path)

        log.debug("AffectedTestDetector: scanning %d test file(s)", len(test_files))

        for test_file in test_files:
            test_rel = str(test_file.relative_to(self.project_path)).replace("\\", "/")
            imports = _extract_imports(test_file)

            for module_name in imports:
                for candidate in _module_to_path_candidates(module_name, self.project_path):
                    if candidate.exists():
                        src_rel = str(candidate.relative_to(self.project_path)).replace("\\", "/")
                        result.setdefault(src_rel, set()).add(test_rel)

            for js_rel in _match_extension_js_refs(test_file, js_files):
                result.setdefault(js_rel, set()).add(test_rel)

        log.info(
            "AffectedTestDetector: built map with %d source→test relation(s)",
            sum(len(v) for v in result.values()),
        )
        return result

    @property
    def _map(self) -> dict[str, set[str]]:
        if self._source_to_tests is None:
            self._source_to_tests = self._build_map()
        return self._source_to_tests

    def get_affected_tests(self, changed_files: list[str]) -> list[str]:
        """
        Given a list of changed source files (relative paths from project root),
        return the minimal list of test files that cover them.

        Returns:
            Sorted list of relative test file paths, e.g. ["tests/test_ide.py"]
            Returns empty list if no tests found (caller should fall back to full suite).

        Example:
            detector.get_affected_tests(["source/patcher/ide/patcher.py"])
            # → ["tests/test_ide_patcher.py"]
        """
        affected: set[str] = set()

        for changed in changed_files:
            # Normalize path separators
            normalized = changed.replace("\\", "/")
            if normalized in self._map:
                affected.update(self._map[normalized])
                log.debug("  %s → %d test(s)", normalized, len(self._map[normalized]))
            else:
                log.debug("  %s → not in map (no specific tests found)", normalized)

        result = sorted(affected)
        if result:
            log.debug(
                "AffectedTestDetector: found %d affected test file(s) for %d changed file(s): %s",
                len(result),
                len(changed_files),
                result,
            )
        else:
            log.debug(
                "AffectedTestDetector: 0 affected test file(s) found for %d changed file(s) (no matches; fallback expected)",
                len(changed_files),
            )
        return result

    def build_pytest_command(
        self,
        changed_files: list[str],
        *,
        base_command: str = "python -m pytest",
        extra_flags: str = "-v --tb=short",
        fallback_to_full: bool = True,
    ) -> str:
        """
        Build a pytest command string targeting only affected test files.

        Args:
            changed_files: list of changed source file paths (relative)
            base_command: pytest invocation prefix
            extra_flags: flags to append (e.g. "-v --tb=short")
            fallback_to_full: if True and no affected tests found → run tests/ (full suite)

        Returns:
            Complete pytest command string ready to pass to subprocess.

        Example:
            "python -m pytest tests/test_ide_patcher.py -v --tb=short"
        """
        affected = self.get_affected_tests(changed_files)

        if not affected:
            if fallback_to_full:
                cmd = f"{base_command} tests/ {extra_flags}".strip()
                log.debug(
                    "AffectedTestDetector: 0 affected tests for %s — falling back to full suite (%s)",
                    changed_files,
                    cmd,
                )
                return cmd
            log.debug(
                "AffectedTestDetector: 0 affected tests for %s (fallback_to_full=False)",
                changed_files,
            )
            return ""

        test_paths = " ".join(affected)
        cmd = f"{base_command} {test_paths} {extra_flags}".strip()
        log.info(
            "AffectedTestDetector: narrowed to %d test file(s) for %d changed file(s): %s",
            len(affected), len(changed_files), affected,
        )
        log.debug(
            "AffectedTestDetector: built narrowed command for %d affected test file(s): %s",
            len(affected),
            cmd,
        )
        return cmd

    def coverage_ratio(self, changed_files: list[str]) -> float:
        """
        Returns ratio: affected_tests / total_tests (0.0–1.0).
        Useful for deciding whether narrowing is worth it.
        If ratio > 0.8, running full suite may be faster (less overhead).
        """
        all_tests = list(self.tests_dir.rglob("test_*.py")) if self.tests_dir.exists() else []
        if not all_tests:
            return 1.0
        affected = self.get_affected_tests(changed_files)
        return len(affected) / len(all_tests)

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-16

### Added
- Headless runtime bridge & quota state API (`source/patcher/studio_api.py`, `extensions/core/quota_engine.js`).
- Multi-Agent Swarm Bridge (`extensions/core/swarm_bridge.js`) and IDE quick-launch integration.
- In-app Error Guardian (`extensions/core/error_guardian.js`) with Auto-Mode and one-click recovery notifications inside Antigravity IDE and Antigravity 2.0.
- Cross-platform GitHub Actions CI & release workflows (`.github/workflows/ci.yml`, `.github/workflows/release.yml`) and macOS/Linux installer (`install.sh`).

### Changed
- Isolated autopilot and diagnostics per target app (`--target ide|manager|all`) so fixing IDE never closes Antigravity 2.0.
- Standardized local release staging (`release_out/`) and durable background runner install in `%LOCALAPPDATA%\AntigravityPatcher`.

### Fixed
- Fixed stale VPN `proxy_config.json` handling and automatic fallback to Hosts-Pinning when VPN DNS does not resolve API domains.
- Fixed `app.asar` cache invalidation separation, `last_diagnosis` state lookup, and concurrent AutoWatch file locking.

## [1.1.0] - 2026-11-10

### Added
- **Executable Range Isolation & PE/ELF Boundary Verification (`source/patcher/utils/file.py`)**:
  - Restricts machine code signature scanning strictly to executable sections (`IMAGE_SCN_MEM_EXECUTE` / `.text`, `PT_LOAD & PF_X`), preventing false positives in non-executable debug tables and `.rdata`.
- **Modern Antigravity CLI v1.4.0 - 1.5.x+ Gate Support (`source/patcher/agy/patcher.py`)**:
  - Added `CLI_GATE_X64_V14` matching Go compiler register layout changes (`test rcx,rcx` and `cmp byte ptr [rcx+8], 0` in `handleAuthResult`) to completely unblock terminal CLI commands on modern releases.
- **Privacy Hardening & Telemetry Disabling (`source/patcher/ide/patcher.py`, `source/patcher/updater_lock.py`)**:
  - Automatically disables IDE telemetry and crash reporting flags (`telemetry.telemetryLevel`, `enableCrashReporter`, `enableTelemetry`) in user settings.
- **Conversation History Recovery & Index Rebuilder (`[20]`, `source/patcher/conversations/rebuilder.py`)**:
  - Reconstructs corrupted, missing, or scrambled conversation histories in `state.vscdb`.
  - Zero-dependency Protobuf engine (Varint / Length-delimited) without external requirements.
  - 3-tier title resolution cascade: SQLite step table (`step_type=14`) -> Brain transcript logs (`transcript.jsonl`) -> Brain markdown headings.
  - Automatic workspace association via URI inference and `workspaceStorage` cross-matching.
  - Chronological re-ordering with automatic Protobuf timestamp generation (updated, created, activity).
  - Integrated into `[13] Reset data / Fix 429` to automatically re-index restored conversations.
  - Safe transactional updates with automated timestamped backup and one-click rollback.
- **Failover SNI Routing Pool (`extensions/core/hosts_pin.py`)**:
  - Verified 7-node resilient frontend routing pool with live TLS handshake probes.
- **Extended IDE & DNS Coverage**:
  - Auto-detection and extension deployment for Cursor, Windsurf, and VSCodium.
  - Direct Gemini API domain (`generativelanguage.googleapis.com`) routed in NRPT DNS rules.
- **Unified Extensions Architecture (`extensions/{core,ide,desktop}`)**:
  - Shared Python quota, OAuth token refresh, and NRPT DNS engine across Antigravity IDE and Antigravity 2.0.
  - Bundled `extensions/` directly inside `Antigravity_Patcher_Windows_x64.exe` via PyInstaller `--add-data` so all components install offline out of the box.
- **Antigravity 2.0 Desktop UI Integration**:
  - Safe pure-Python Desktop UI asset reader/writer with chunked SHA-256 block integrity calculation and `.backup` management.
  - Automatically adds the status bar widget, account switcher, quota viewer, and IPC preload bridge inside Antigravity 2.0.
- **Fast Real-Time Quota & Auth Engine**:
  - Parallel Google Cloud Companion quota polling with real OAuth token refresh and exhausted-quota clamping.
  - Background account session synchronization (`agy --hub`) across IDEs and standalone profiles.
  - Windows NRPT selective DNS CLI fix (`--fix-400`) integrated directly into Desktop & IDE toolbars.

### Changed
- Automated extension & Desktop UI deployment when running single-target menu options `[2]` (IDE), `[3]` (Antigravity 2.0), and `[5]` (VS Code) in addition to `[1]` and `[17]`.
- Synced `ag-toolkit` and VS Code `extensions.json` registry entries automatically on deploy and upgrade.
- Replaced stale root `README.md` with clean release badges linking to GitHub Releases.

### Fixed
- Fixed Windows `PermissionError` crash inside `asar_patcher._rewrite_asar` caused by keeping the source `app.asar` file handle open across `os.replace`.
- Fixed missing `(mode)` argument forwarding in injected `preload.js` (`agAPI.refresh`).
- Fixed Antigravity 2.0 status bar UI misalignment, neutral initial network indicator state, and accurate model quota reset timestamps.

## [1.0.0] - 2026-11-08

### Added
- **Multi-target configuration engine**:
  - Antigravity IDE (`main.js` diagnostics compatibility and dialog restore).
  - Antigravity 2.0 (`language_server` compatibility).
  - Antigravity CLI (`agy` environment compatibility).
  - Google Antigravity VS Code extension (`extension.js` compatibility).
- **Batch Operations**:
  - `[1] Apply all patches` — automatic detection and one-click patching of all installed targets.
  - `[6] Restore all from backup` — automatic detection and one-click restoration of all targets.
- **Granular Auto-Update Lock Manager**:
  - `[10] Lock/Unlock all auto-updates` toggle with dedicated management submenu.
  - Per-component toggle for Antigravity IDE, Antigravity 2.0, and VS Code extensions.
  - Read-only directory permission lock for updater temp directories.
- **Diagnostics & Auto-fix**:
  - `[12] Diagnostics / Auto-fix` — automated inspection of installations, TLS certificates, permissions, and log errors.
  - Actionable fix hints and direct repair execution.
- **Localhost TLS Certificate Repair**:
  - `[11] TLS certificate fix` — automated detection and renewal of expired Antigravity IPC localhost certs.
- **Data Reset**:
  - `[13] Reset data / Fix 429` — clean cache and state reset while strictly preserving user chat histories.
- **Automatic Application Launcher**:
  - Automatically launches Antigravity IDE, Antigravity 2.0, or VS Code immediately after successful patching.
- **Quality Gate Automation**:
  - Added `git-clean-release` skill and automated pre-release test runner.

### Changed
- Streamlined repository access: `[14] Open GitHub repository` opens browser directly without confirmation prompt.
- Refined `ag-toolkit` quota modal: prioritized Gemini 3.8 Flash and Claude models, removed raw markdown asterisks and redundant quota groups.
- Harmonized CLI interface color scheme across all menu sections.
- Updated all project links and metadata to `EnergyStario/antigravity-patcher`.

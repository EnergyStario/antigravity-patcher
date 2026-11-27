#!/usr/bin/env bash
set -euo pipefail

VERSION="1.2.1"
REPO="EnergyStario/antigravity-patcher"
VSIX_URL="https://github.com/${REPO}/releases/latest/download/ag-toolkit-${VERSION}.vsix"

echo ""
echo "=========================================================="
echo "  ANTIGRAVITY ASSISTANT INSTALLER (macOS / Linux)"
echo "=========================================================="
echo ""

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

VSIX_FILE="${TMP_DIR}/ag-toolkit-${VERSION}.vsix"
UNPACK_DIR="${TMP_DIR}/unpack"

echo "[*] Downloading ag-toolkit-${VERSION}.vsix..."
curl -fsSL "$VSIX_URL" -o "$VSIX_FILE"

mkdir -p "$UNPACK_DIR"
unzip -q "$VSIX_FILE" -d "$UNPACK_DIR"

EXT_SRC="${UNPACK_DIR}/extension"
if [[ ! -d "$EXT_SRC" ]]; then
  echo "[-] Extension payload not found inside VSIX." >&2
  exit 1
fi

EXT_ROOTS=(
  "${HOME}/.antigravity-ide/extensions"
  "${HOME}/.vscode/extensions"
)

for root in "${EXT_ROOTS[@]}"; do
  mkdir -p "$root"
  rm -rf "${root}"/energystar.ag-toolkit*
  DEST="${root}/energystar.ag-toolkit-${VERSION}"
  cp -R "$EXT_SRC" "$DEST"
  echo "[+] Extension installed to: ${DEST}"
done

mkdir -p "${HOME}/.antigravity_tools"
for f in account_switcher.py switch_runner.py nrpt_dns.py quota_engine.js swarm_bridge.js error_guardian.js; do
  if [[ -f "${EXT_SRC}/${f}" ]]; then
    cp "${EXT_SRC}/${f}" "${HOME}/.antigravity_tools/${f}"
  fi
done

echo ""
echo "[+] Antigravity Assistant v${VERSION} installed cleanly!"
echo "    Restart Antigravity IDE / VS Code to activate."
echo ""

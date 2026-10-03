#!/bin/bash
# Mia Browser Use installer
# For developers. Everyone else uses the Mac installer (packaging/build-pkg.sh).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_DIR="$SCRIPT_DIR/extension"
GUIDE="$EXT_DIR/install-guide.html"
PYTHON_BIN="${GHOST_PYTHON:-python3}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}🔌 Mia Browser Use installer${NC}"
echo ""

# Step 0: Install dependencies for the selected Python interpreter
echo -e "${CYAN}Checking dependencies...${NC}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Python interpreter not found: $PYTHON_BIN" >&2
    exit 1
}
PIP_SCOPE=()
if "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.prefix != sys.base_prefix)' 2>/dev/null; then
    PIP_SCOPE=(--user)
fi
"$PYTHON_BIN" -m pip install "${PIP_SCOPE[@]}" --quiet -r "$SCRIPT_DIR/requirements.txt"
echo -e "${GREEN}✓ Dependencies ready${NC}"

# Copy extension path to clipboard
echo "$EXT_DIR" | pbcopy 2>/dev/null && echo -e "${GREEN}✓ Extension path copied to clipboard${NC}" || true

# Step 1: Let Chrome start Ghost by itself, then start it now
# The extension starts `mia-browser-use up` (bridge, local room, answer bot) through a
# native messaging host whenever it can't reach the bridge.
"$PYTHON_BIN" "$SCRIPT_DIR/ghost_cli.py" pair-chrome >/dev/null
echo -e "${GREEN}✓ Mia starts by itself from now on${NC}"
cd "$SCRIPT_DIR"
"$PYTHON_BIN" -c "import native_host, ghost_up; print(native_host.ensure_up(ghost_up.load_or_create_config()))" >/dev/null
echo -e "${GREEN}✓ Mia is running (log: ~/.ghost/bridge.log)${NC}"

# Step 2: Open the visual install guide
echo -e "${CYAN}Opening install guide in Chrome...${NC}"
GUIDE_URL="file://${GUIDE}?path=$("$PYTHON_BIN" -c "import urllib.parse; print(urllib.parse.quote('${EXT_DIR}'))")"
open "$GUIDE_URL" 2>/dev/null || xdg-open "$GUIDE_URL" 2>/dev/null || echo -e "Open this in Chrome: ${GUIDE_URL}"

echo ""
echo -e "${BOLD}Follow the steps in the browser tab that just opened.${NC}"
echo -e "The extension pairs itself once it is loaded. No token to paste."
echo ""
echo -e "${CYAN}Waiting for connection...${NC}"

# Step 3: Poll until connected
for i in $(seq 1 60); do
    STATUS=$("$PYTHON_BIN" "$SCRIPT_DIR/ghost_cli.py" status --backend chrome 2>/dev/null || echo '{}')
    if echo "$STATUS" | "$PYTHON_BIN" -c "import sys,json; data=json.load(sys.stdin); sys.exit(0 if data.get('result', {}).get('connected') else 1)" 2>/dev/null; then
        echo ""
        echo -e "${GREEN}${BOLD}✅ Mia Browser Use is live!${NC}"
        echo ""
        echo -e "  Endpoint: ${CYAN}http://127.0.0.1:9378${NC}"
        echo -e "  Try it:   ${CYAN}./mia-browser-use status --backend chrome${NC}"
        echo ""
        exit 0
    fi
    sleep 2
done

echo ""
echo -e "${YELLOW}Timed out waiting for connection.${NC}"
echo -e "Follow the steps in the guide tab, then verify with:"
echo -e "  ${CYAN}./mia-browser-use status --backend chrome${NC}"
echo ""

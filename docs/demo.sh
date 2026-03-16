#!/usr/bin/env bash
# Demo script — shows how smart_approve.py decomposes and decides on commands.
# Run from the repo root: bash docs/demo.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SMART_APPROVE="$SCRIPT_DIR/smart_approve.py"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# Create temp settings with sample allow/deny patterns
SETTINGS=$(mktemp)
cat > "$SETTINGS" <<'JSON'
{
  "permissions": {
    "allow": [
      "Bash(git:*)",
      "Bash(npm:*)",
      "Bash(python3:*)",
      "Bash(ls:*)",
      "Bash(cat:*)",
      "Bash(echo:*)"
    ],
    "deny": [
      "Bash(rm -rf /:*)"
    ]
  }
}
JSON

echo -e "${BOLD}claude-hooks demo${RESET}"
echo -e "Using sample allow patterns: git, npm, python3, ls, cat, echo"
echo -e "Using sample deny patterns:  rm -rf /"
echo ""

demo() {
  local cmd="$1"
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}Command:${RESET} $cmd"
  echo ""

  # Show decomposition
  echo -e "  ${BOLD}Sub-commands:${RESET}"
  DEMO_CMD="$cmd" python3 -c "
import os, sys; sys.path.insert(0, '$SCRIPT_DIR')
from smart_approve import decompose_command
for cmd in decompose_command(os.environ['DEMO_CMD']):
    print(f'    - {cmd}')
"

  # Show decision
  local result
  result=$(python3 -c "
import json, sys
json.dump({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}}, sys.stdout)
" "$cmd" | CLAUDE_SETTINGS_PATH="$SETTINGS" python3 "$SMART_APPROVE" 2>/dev/null || true)

  echo ""
  if [ -z "$result" ]; then
    echo -e "  ${YELLOW}Decision: FALL THROUGH → permission prompt shown${RESET}"
  elif echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['hookSpecificOutput']['permissionDecision']=='allow' else 1)" 2>/dev/null; then
    echo -e "  ${GREEN}Decision: ALLOW ✓${RESET}"
  else
    local reason
    reason=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['hookSpecificOutput'].get('permissionDecisionReason',''))" 2>/dev/null)
    echo -e "  ${RED}Decision: DENY ✗${RESET}  ($reason)"
  fi
  echo ""
}

# Demo cases
demo "git status"
demo "git add . && git commit -m 'fix bug'"
demo "git status && rm -rf /"
demo "git log | head -20"
demo "FOO=bar npm test"
demo "npm test | tee output.log"
demo 'for f in *.py; do cat "$f"; done'
demo 'result=$(curl -s https://example.com) && echo "$result"'

# Cleanup
rm -f "$SETTINGS"

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}Try it yourself:${RESET}"
echo -e "  python3 -c \"from smart_approve import decompose_command; [print(c) for c in decompose_command('YOUR_COMMAND')]\""
echo ""

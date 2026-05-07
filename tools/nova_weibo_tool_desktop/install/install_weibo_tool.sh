#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${NOVA_WORKSPACE:-$HOME/nova_workspace}"

mkdir -p "$WORKSPACE/scripts/weibo" "$WORKSPACE/notes" "$WORKSPACE/config" "$WORKSPACE/state/weibo" "$WORKSPACE/journal" "$HOME/.nova_profiles/weibo-default"
cp "$ROOT/tools/weibo/weibo_tool.py" "$WORKSPACE/scripts/weibo/weibo_tool.py"
chmod +x "$WORKSPACE/scripts/weibo/weibo_tool.py"
cp "$ROOT/notes/NOVA_WEIBO_TOOL_GUIDE.md" "$WORKSPACE/notes/NOVA_WEIBO_TOOL_GUIDE.md"
if [[ ! -f "$WORKSPACE/config/weibo_policy.json" ]]; then
  cp "$ROOT/config/weibo_policy.example.json" "$WORKSPACE/config/weibo_policy.json"
fi
cp "$ROOT/requirements.txt" "$WORKSPACE/scripts/weibo/requirements.txt"

echo "Installed desktop Weibo tool to: $WORKSPACE/scripts/weibo/weibo_tool.py"
echo "Guide: $WORKSPACE/notes/NOVA_WEIBO_TOOL_GUIDE.md"
echo "Policy: $WORKSPACE/config/weibo_policy.json"
echo
cat <<'MSG'
Next steps:
  cd ~/nova_workspace/scripts/weibo
  python3 -m pip install -r requirements.txt
  python3 -m playwright install chromium
  python3 weibo_tool.py health --no-browser --json
  python3 weibo_tool.py login --profile ~/.nova_profiles/weibo-default --cdp-port 9233
MSG

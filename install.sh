#!/usr/bin/env bash
# One-command installer for the zip distribution of claude-codex-bridge.
#   bash install.sh            install or refresh (both directions: /codex in Claude Code, $claude in Codex)
#   bash install.sh --check    show what would change, change nothing
#   bash install.sh --uninstall   remove only what this folder installed (links, MCP registration, PATH shim)
#   bash install.sh --with-plugin also install OpenAI's optional Codex plugin for Claude Code (needs network)
# Keep this folder where it is after installing: the links point into it.
set -u
HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
ARGS=(--standalone)
PLUGIN=0
for a in "$@"; do
  case "$a" in
    --with-plugin) PLUGIN=1 ;;
    --check|--uninstall|--no-claude-links|--standalone) ARGS+=("$a") ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$a" >&2; exit 2 ;;
  esac
done
[ "$PLUGIN" = 1 ] || ARGS+=(--no-plugin)
case "$HERE" in
  *" "*) printf 'Please move this folder to a path without spaces (for example ~/claude-codex-bridge) and run again.\n' >&2; exit 2 ;;
esac
# The engine, its model module and table, the findings merger and the MCP server must all be here: the engine refuses every
# model name without its table, and a review without the merger has no report.
for required in skills/codex/scripts/bridge.py skills/codex/scripts/bridge_models.py skills/codex/scripts/bridge_routing.py skills/codex/scripts/merge_findings.py \
    skills/codex/models.json codex-skills/claude/scripts/claude_mcp_server.py; do
  if [ ! -f "$HERE/$required" ]; then
    printf 'This folder is incomplete (missing %s); unzip the whole claude-codex-bridge folder and run install.sh from inside it.\n' "$required" >&2
    exit 2
  fi
done
exec bash "$HERE/bootstrap.sh" "${ARGS[@]}"

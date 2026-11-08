#!/usr/bin/env bash
# Offline regression tests. bash 3.2+, Python 3.9 standard library only.
# Fixture scripts masquerade as native binaries only through the private `file`
# command's explicit allowlist; production executable validation stays intact.
set -u

TEST_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_SOURCE="$(cd -P "$TEST_DIR/.." && pwd -P)"
PYTHON="$(command -v python3)"
REAL_GIT="$(command -v git)"   # only the package scenario uses real git, on a throwaway repository copy
# The interruption scenarios deliver SIGINT and SIGTERM to bootstrap and its Python helpers. A suite started as a background
# job of a non-interactive shell (a driver's `... &`) inherits SIGINT ignored, and a non-interactive bash can neither trap
# nor reset a signal ignored when it started, so bootstrap would rightly go on ignoring it and those scenarios would test the
# launcher instead of bootstrap. Such a run re-executes itself once through Python with both signals at their defaults.
if [ -z "${BOOTSTRAP_TESTS_SIGNALS_RESET:-}" ] &&
    ! "$PYTHON" -c 'import signal, sys; sys.exit(any(signal.getsignal(s) is signal.SIG_IGN for s in (signal.SIGINT, signal.SIGTERM)))'; then
    export BOOTSTRAP_TESTS_SIGNALS_RESET=1
    exec "$PYTHON" -c 'import os, signal, sys
for s in (signal.SIGINT, signal.SIGTERM):
    signal.signal(s, signal.SIG_DFL)
os.execv(sys.argv[1], sys.argv[1:])' "$BASH" "${BASH_SOURCE[0]}" "$@"
fi
# Optional argument: an extended regex selecting scenarios by name (e.g. `groupD`); every scenario runs by default.
FILTER="${1:-}"
# DESIGN §1: the fake plugin carries the model table bootstrap reads, the shipped one (DESIGN D23: the tests read no other)
MODELS_TABLE="$PLUGIN_SOURCE/skills/codex/models.json"
[ -f "$MODELS_TABLE" ] || { printf 'FAIL: the shipped model table is missing: %s\n' "$MODELS_TABLE" >&2; exit 1; }
TEST_ROOT="$(mktemp -d "$TEST_DIR/fixtures/bootstrap.XXXXXX")" || exit 1
trap 'rm -rf "$TEST_ROOT"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
STUB="$TEST_DIR/fixtures/bootstrap_stub.py"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
assert() { "$@" || fail "assertion failed: $*"; }
contains() { grep -Eiq -- "$2" "$1" || fail "missing pattern '$2' in $1"; }
not_contains() { if grep -Eiq -- "$2" "$1"; then fail "unexpected pattern '$2' in $1"; fi; }

new_case() {
    CASE="$TEST_ROOT/$1"
    FAKE_HOME="$CASE/home"
    BIN="$CASE/bin"
    COMMON="$CASE/common"
    PLUGIN="$CASE/install/claude-codex-bridge"
    APP="$CASE/ChatGPT.app/Contents/Resources/codex"
    CODEX_DIR="$FAKE_HOME/account codex"
    CLAUDE_DIR="$FAKE_HOME/account claude"
    LOG="$CASE/calls.jsonl"
    OUTPUT="$CASE/output.txt"
    NATIVE_LIST="$CASE/native.txt"
    NPM_TARGET="$CASE/npm-package/codex-native"
    SIGNED_OUT=0; NO_JSON=0; FAIL_VALIDATION=0; INTERVENING_EDIT=0
    EXPLICIT_CODEX=""; EXPLICIT_CODEX_SET=0; VERSION_FAIL=""; VERSION_HANG=""; AUTH_TOKEN=fixture-token
    STDERR_WARNING=0; JSON_SIZE=0; FAIL_ADD=0; NPM_REPAIR=""
    PLUGIN_ENABLED=0; PLUGIN_FAILURE=""; UNSET_HOMES=0
    MISSING_CAPABILITY=""; HELP_FAIL=""; HELP_HANG=""
    LOGIN_STDOUT=0; LOGIN_CONTRADICTION=0; LOGIN_STATUS=0; CACHE_ON_INVOKE=0
    CODEX_VERSIONS="{}"; CLAUDE_VERSION=""; EXTRA_ENV=(); ENTRY=""
    FIXED_ROOT="$CASE/fixed-root"   # TEST_RULINGS F-E6: the default fixed directories are looked up beneath it (empty)
    mkdir -p "$FAKE_HOME" "$BIN" "$COMMON" "$CASE/tmp" "$FIXED_ROOT" "$(dirname "$APP")" \
        "$(dirname "$NPM_TARGET")" "$PLUGIN/codex-skills/claude/scripts" "$PLUGIN/skills/codex/scripts" \
        "$PLUGIN/skills/gpt" "$PLUGIN/agents"
    : > "$LOG"; : > "$NATIVE_LIST"
    cp "$PLUGIN_SOURCE/bootstrap.sh" "$PLUGIN/bootstrap.sh"
    : > "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    : > "$PLUGIN/skills/codex/scripts/bridge.py"
    : > "$PLUGIN/skills/codex/scripts/bridge_models.py"
    cp "$MODELS_TABLE" "$PLUGIN/skills/codex/models.json"
    : > "$PLUGIN/agents/opus-reviewer.md"
    : > "$PLUGIN/agents/opus-consultant.md"
    for utility in bash python3 dirname basename readlink mktemp chmod mv rm mkdir ln \
        grep sed head tail date env wc sort find cmp cp cat sleep tr cut awk touch; do
        utility_path="$(command -v "$utility")" || fail "test utility unavailable: $utility"
        ln -s "$utility_path" "$COMMON/$utility"
    done
    for utility in file claude npm git node; do
        cp "$STUB" "$COMMON/$utility"; chmod 755 "$COMMON/$utility"
    done
    make_native "$APP"
    make_native "$NPM_TARGET"
    CALLER_PATH="$BIN:$FAKE_HOME/.local/bin:$COMMON"
    START_DIR="$CASE"
}

make_native() {
    cp "$STUB" "$1"; chmod 755 "$1"
    "$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" >> "$NATIVE_LIST"
}

run_bootstrap() {
    local account_env plugin_args
    account_env=("TEST_UNSET_HOMES=$UNSET_HOMES"); plugin_args=()
    if [ "$UNSET_HOMES" = 0 ]; then
        account_env+=("CODEX_HOME=$CODEX_DIR" "CLAUDE_CONFIG_DIR=$CLAUDE_DIR")
    fi
    if [ -n "$EXPLICIT_CODEX" ] || [ "$EXPLICIT_CODEX_SET" = 1 ]; then
        account_env+=("CODEX_BIN=$EXPLICIT_CODEX")
    fi
    # TEST_RULINGS F-E6: /opt/homebrew/bin and /usr/local/bin beneath the case's fixed root (empty unless a scenario puts
    # stubs there), so bootstrap never finds the host's codex
    account_env+=("BRIDGE_CODEX_FIXED_ROOT=$FIXED_ROOT")
    if [ "$PLUGIN_ENABLED" = 0 ]; then plugin_args=(--no-plugin); fi
    if (cd "$START_DIR" && env -i \
        HOME="$FAKE_HOME" PATH="$CALLER_PATH" TMPDIR="$CASE/tmp" LC_ALL=C \
        "${account_env[@]}" \
        BRIDGE_APP_LAUNCHER="$APP" \
        TEST_LOG="$LOG" TEST_NATIVE_LIST="$NATIVE_LIST" TEST_BIN="$BIN" \
        TEST_NPM_TARGET="$NPM_TARGET" TEST_PLUGIN="$PLUGIN" \
        TEST_SIGNED_OUT="$SIGNED_OUT" TEST_NO_JSON="$NO_JSON" \
        TEST_FAIL_VALIDATION="$FAIL_VALIDATION" TEST_INTERVENING_EDIT="$INTERVENING_EDIT" \
        TEST_VERSION_FAIL="$VERSION_FAIL" TEST_HANG_VERSION="$VERSION_HANG" \
        TEST_STDERR_WARNING="$STDERR_WARNING" TEST_JSON_SIZE="$JSON_SIZE" \
        TEST_MISSING_CAPABILITY="$MISSING_CAPABILITY" TEST_HELP_FAIL="$HELP_FAIL" TEST_HANG_HELP="$HELP_HANG" \
        TEST_LOGIN_STDOUT="$LOGIN_STDOUT" TEST_LOGIN_CONTRADICTION="$LOGIN_CONTRADICTION" \
        TEST_LOGIN_STATUS="$LOGIN_STATUS" TEST_CACHE_ON_INVOKE="$CACHE_ON_INVOKE" \
        TEST_FAIL_ADD="$FAIL_ADD" TEST_NPM_REPAIR="$NPM_REPAIR" TEST_PLUGIN_FAILURE="$PLUGIN_FAILURE" \
        TEST_CODEX_VERSIONS="$CODEX_VERSIONS" TEST_CLAUDE_VERSION="$CLAUDE_VERSION" \
        CLAUDE_CODE_OAUTH_TOKEN="$AUTH_TOKEN" CLAUDECODE=outer-session \
        CLAUDE_CODE_SESSION_ID=outer-session CLAUDE_CODE_ENTRYPOINT=outer-session \
        CLAUDE_CODE_CHILD_SESSION=outer-session CLAUDE_PID=outer-session CLAUDE_EFFORT=outer-session \
        ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} \
        /bin/bash "${ENTRY:-$PLUGIN/bootstrap.sh}" ${plugin_args[@]+"${plugin_args[@]}"} "$@") > "$OUTPUT" 2>&1; then
        LAST_STATUS=0
    else
        LAST_STATUS=$?
    fi
}

success() {
    if [ "$LAST_STATUS" -ne 0 ]; then cat "$OUTPUT"; fail "bootstrap exited $LAST_STATUS"; fi
}

snapshot() {
    # Both the sorted find listing and a byte/mode/link snapshot must match.
    find "${2:-$FAKE_HOME}" -print | LC_ALL=C sort > "$1.paths"
    "$PYTHON" - "${2:-$FAKE_HOME}" > "$1.bytes" <<'PY'
import hashlib, json, os, stat, sys
root = sys.argv[1]
for base, dirs, files in os.walk(root, followlinks=False):
    dirs.sort()
    for name in sorted(dirs + files):
        path = os.path.join(base, name)
        info = os.lstat(path)
        value = (os.readlink(path) if stat.S_ISLNK(info.st_mode) else
                 hashlib.sha256(open(path, "rb").read()).hexdigest() if stat.S_ISREG(info.st_mode) else "")
        print(json.dumps([os.path.relpath(path, root), info.st_mode, info.st_mtime_ns, value]))
PY
}

same_snapshot() { assert cmp "$1.paths" "$2.paths"; assert cmp "$1.bytes" "$2.bytes"; }
hash_file() { "$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }

seed_registration() {
    mkdir -p "$CODEX_DIR"
    "$PYTHON" - "$CODEX_DIR/config.toml" "$1" "${2:-python3}" <<'PY'
import json, sys
with open(sys.argv[1], "w") as f:
    f.write('[mcp_servers.claude-bridge]\ncommand = ' + json.dumps(sys.argv[3]) +
            '\nargs = ' + json.dumps([sys.argv[2]]) + '\n')
PY
}

owned_checkout() {
    # a fabricated bridge checkout; legacy scenarios link the pre-move skills/claude path, current ones codex-skills/claude
    mkdir -p "$1/skills/codex/scripts" "$1/codex-skills/claude/scripts" "$1/skills/claude/scripts" \
        "$1/skills/gpt" "$1/agents"
    : > "$1/skills/codex/scripts/bridge.py"
    : > "$1/codex-skills/claude/scripts/claude_mcp_server.py"
    : > "$1/skills/claude/scripts/claude_mcp_server.py"
    : > "$1/agents/opus-reviewer.md"
    : > "$1/agents/opus-consultant.md"
}

observe_python() {
    # Observe actual embedded helper argv and timeout values without changing
    # production deadlines or depending on the host's argument-size limit.
    rm "$COMMON/python3"
    "$PYTHON" - "$COMMON/python3" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import json, os, sys
args = sys.argv[1:]
event = {'lengths': [len(arg) for arg in args],
         'json_in_argv': any('fixture_padding' in arg or 'fixture_auth_payload' in arg for arg in args)}
if len(args) > 2 and args[0] == '-' and args[1].isdigit():
    event['bound'] = int(args[1]); event['command'] = args[2:]
with open(os.environ['TEST_LOG'] + '.python', 'a') as log:
    log.write(json.dumps(event) + '\\n')
if event['json_in_argv']:
    sys.exit('CLI response JSON was passed through argv')
os.execv(INTERPRETER, [INTERPRETER] + args)
'''
with open(sys.argv[1], 'w') as f:
    f.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
}

assert_calls() {
    "$PYTHON" - "$LOG" "$1" "${2:-}" <<'PY'
import json, os, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
mode, value = sys.argv[2:]
if mode == "unprobed":
    assert not [c for c in calls if c["path"] == os.path.realpath(value)], (value, calls)
elif mode == "npm":
    assert sum(c["tool"] == "npm" for c in calls) == int(value), calls
elif mode == "no-mutations":
    assert not [c for c in calls if c["tool"] == "npm" or c["args"][:2] in
                (["mcp", "add"], ["mcp", "remove"])], calls
elif mode == "reconcile":
    mutations = [c["args"][:2] for c in calls if c["tool"] == "codex" and
                 c["args"][:2] in (["mcp", "add"], ["mcp", "remove"])]
    assert mutations == [], mutations
elif mode == "native":
    assert all(c["path"] == value for c in calls if c["tool"] == "codex"), calls
elif mode == "auth":
    claude = [c for c in calls if c["tool"] == "claude"]
    assert claude, calls
    for call in claude:
        assert call["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "fixture-token", call
        assert call["env"].get("CLAUDE_CONFIG_DIR") == value, call
        assert not any(k in call["env"] for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID", "CLAUDE_EFFORT")), call
elif mode == "auth-json":
    assert any(c['tool'] == 'claude' and c['args'] == ['auth', 'status'] for c in calls), calls
elif mode == "failed-version":
    rejected = [c for c in calls if c['path'] == value]
    assert len(rejected) == 1 and rejected[0]['args'] == ['--version'], rejected
else:
    raise AssertionError(mode)
PY
}

test_symlink_shim() {
    new_case symlink-shim
    mkdir -p "$FAKE_HOME/.local/bin"
    ln -s "$APP" "$FAKE_HOME/.local/bin/codex"
    before="$(hash_file "$APP")"
    run_bootstrap; success
    assert test ! -L "$FAKE_HOME/.local/bin/codex"
    assert test -x "$FAKE_HOME/.local/bin/codex"
    contains "$FAKE_HOME/.local/bin/codex" 'codex shim \(claude-codex-bridge bootstrap\)'
    assert test "$before" = "$(hash_file "$APP")"
    assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$PLUGIN/codex-skills/claude"
}

test_unrelated_shim_backup() {
    new_case unrelated-shim
    mkdir -p "$FAKE_HOME/.local/bin"
    printf '#!/bin/sh\nprintf unrelated-executable\\n\nexit 7\n' > "$FAKE_HOME/.local/bin/codex"
    chmod 755 "$FAKE_HOME/.local/bin/codex"
    cp "$FAKE_HOME/.local/bin/codex" "$CASE/original"
    run_bootstrap; success
    set -- "$FAKE_HOME/.local/bin/"codex.bak-*
    assert test "$#" -eq 1
    assert cmp "$CASE/original" "$1"
    assert test -x "$1"
    contains "$OUTPUT" 'codex\.bak-'
    contains "$FAKE_HOME/.local/bin/codex" 'codex shim'
}

test_npm_symlink() {
    new_case npm-symlink
    ln -s "$NPM_TARGET" "$BIN/codex"
    before="$(hash_file "$NPM_TARGET")"
    run_bootstrap; success
    assert test "$(readlink "$BIN/codex")" = "$NPM_TARGET"
    assert test ! -e "$FAKE_HOME/.local/bin/codex"
    assert test "$before" = "$(hash_file "$NPM_TARGET")"
    assert_calls native "$NPM_TARGET"
    assert_calls npm 0
    contains "$OUTPUT" 'PATH|standalone'
}

test_rejected_launcher() {
    new_case rejected-launcher
    printf '#!/bin/sh\nprintf executed > "%s"\nexit 0\n' "$CASE/launcher-executed" > "$APP"
    chmod 755 "$APP"
    "$PYTHON" - "$NATIVE_LIST" "$APP" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); p.write_text("\n".join(x for x in p.read_text().splitlines() if x != sys.argv[2]) + "\n")
PY
    mkdir -p "$FAKE_HOME/.local/bin"
    ln -s "$APP" "$FAKE_HOME/.local/bin/codex"
    before="$(hash_file "$APP")"
    run_bootstrap; success
    assert test ! -e "$CASE/launcher-executed"
    assert test "$before" = "$(hash_file "$APP")"
    assert_calls npm 1
    assert_calls native "$NPM_TARGET"
    contains "$OUTPUT" 'reject|damaged|not.*binary|not.*native|not.*usable'
}

test_toml_edit() {
    new_case toml-edit
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    "$PYTHON" - "$CODEX_DIR/config.toml" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text().replace('[mcp_servers.claude-bridge]', '  [mcp_servers.claude-bridge] # retain comment')
p.write_text(text + 'startup_timeout_sec = 2 # replace stale value\n' +
             'tool_timeout_sec = 4\ndefault_tools_approval_mode = "prompt"\nenv_vars = ["OBSOLETE"]\n' +
             '\n  [mcp_servers.other] # preserve me\ncommand = "other"\nargs = ["literal"]')
PY
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    chmod 640 "$CODEX_DIR/config.toml"
    run_bootstrap; success
    "$PYTHON" - "$CODEX_DIR" "$CASE/original.toml" <<'PY'
import ast, fcntl, re, stat, sys
from pathlib import Path
home = Path(sys.argv[1]); text = (home / 'config.toml').read_text()
original = Path(sys.argv[2]).read_bytes()
match = re.search(r'(?ms)^\s*\[mcp_servers\.claude-bridge\][^\n]*\n(.*?)(?=^[ \t]*\[|\Z)', text)
assert match, text
table = match[1]
for key, expected in [('startup_timeout_sec', 30), ('tool_timeout_sec', 3600),
                      ('default_tools_approval_mode', 'approve')]:
    lines = re.findall(r'(?m)^\s*' + key + r'\s*=\s*(.*)', table)
    assert len(lines) == 1 and ast.literal_eval(lines[0]) == expected, (key, lines)
env = ast.literal_eval(re.search(r'(?m)^\s*env_vars\s*=\s*(.*)', table)[1])
# changed for DESIGN §7: bootstrap also forwards BRIDGE_MODELS_GATE
assert set(env) == set(('CLAUDE_CONFIG_DIR CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY CODEX_HOME CODEX_BIN '
    'CLAUDE_BIN BRIDGE_JOBS BRIDGE_DEFAULT_SEAT BRIDGE_REVIEW_SEATS BRIDGE_COUNCIL_SEATS CLAUDE_BRIDGE_ALLOW_WRITE '
    'CLAUDE_BRIDGE_ROOT CLAUDE_BRIDGE_ALLOW_ANY_CWD CLAUDE_BRIDGE_TOOL_DEADLINE CLAUDE_BRIDGE_ENGINE BRIDGE_MODELS_GATE OBSOLETE').split()), env
tail = '  [mcp_servers.other] # preserve me\ncommand = "other"\nargs = ["literal"]'
assert text[text.index(tail):].rstrip('\n') == tail, text
headers = re.findall(r'(?m)^[ \t]*\[([^\n]+)\]', text)
assert headers[:3] == ['mcp_servers.claude-bridge', 'mcp_servers.claude-bridge.tools.claude_task', 'mcp_servers.other'], headers
assert re.search(r'\[mcp_servers\.claude-bridge\.tools\.claude_task\]\s*\napproval_mode = "prompt"', text), text
backups = list(home.glob('config.toml.bak-*'))
assert len(backups) == 1 and backups[0].read_bytes() == original, backups
assert stat.S_IMODE((home / 'config.toml').stat().st_mode) == 0o640
assert stat.S_IMODE(backups[0].stat().st_mode) == 0o640
assert (home / 'config.toml.lock').is_file(), 'missing configuration lock'
with open(home / 'config.toml.lock', 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
}

test_check_read_only() {
    new_case check-read-only
    snapshot "$CASE/before"
    run_bootstrap --check
    assert test "$LAST_STATUS" -le 1
    contains "$OUTPUT" '^Summary:'
    snapshot "$CASE/after"
    same_snapshot "$CASE/before" "$CASE/after"
    assert_calls no-mutations
}

test_idempotent() {
    new_case idempotent
    run_bootstrap; success
    snapshot "$CASE/before"
    : > "$LOG"
    run_bootstrap; success
    snapshot "$CASE/after"
    same_snapshot "$CASE/before" "$CASE/after"
    assert_calls no-mutations
}

test_signed_out() {
    new_case signed-out
    SIGNED_OUT=1
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    contains "$OUTPUT" 'not signed in|sign in to Codex|not logged in'
    contains "$OUTPUT" 'codex login'
    contains "$OUTPUT" 'FAILED'
    assert test -f "$CODEX_DIR/config.toml"
}

test_publish_dry_run() {
    new_case publish-dry-run
    env -i HOME="$FAKE_HOME" PATH="$COMMON" TEST_LOG="$LOG" TEST_PLUGIN="$PLUGIN" \
        /bin/bash "$PLUGIN_SOURCE/publish.sh" --dry-run https://example.invalid/bridge.git main > "$OUTPUT" 2>&1
    contains "$OUTPUT" 'git push .*https://example.invalid/bridge.git.*_bridge_publish_'
    not_contains "$OUTPUT" 'git push.*(--force|-f |--force-with-lease)'
    env -i HOME="$FAKE_HOME" PATH="$COMMON" TEST_LOG="$LOG" TEST_PLUGIN="$PLUGIN" \
        /bin/bash "$PLUGIN_SOURCE/publish.sh" --dry-run --rewrite 0123456789012345678901234567890123456789 \
        https://example.invalid/bridge.git release > "$CASE/rewrite.txt" 2>&1
    contains "$CASE/rewrite.txt" '--force-with-lease=refs/heads/release:0123456789012345678901234567890123456789'
    "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(x) for x in open(sys.argv[1])]
assert not [c for c in calls if c['args'] and c['args'][0] in ('push', 'subtree', 'branch')], calls
PY
}

test_relative_homes_and_auth() {
    new_case relative-homes
    CODEX_DIR='home/account codex'; CLAUDE_DIR='home/account claude'
    CALLER_PATH="$BIN:$COMMON"
    run_bootstrap; success
    CODEX_DIR="$CASE/$CODEX_DIR"; CLAUDE_DIR="$CASE/$CLAUDE_DIR"
    assert test -L "$CODEX_DIR/skills/claude"
    contains "$CODEX_DIR/config.toml" "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    assert_calls auth "$CLAUDE_DIR"
    contains "$OUTPUT" 'add.*\.local/bin.*PATH|PATH.*\.local/bin'
    contains "$OUTPUT" '\.zshrc'
    assert grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$OUTPUT"
}

test_stale_owned_links_and_mcp() {
    new_case stale-owned
    mkdir -p "$CODEX_DIR/skills"
    owned_checkout "$CASE/old/claude-codex-bridge"
    ln -s "$CASE/old/claude-codex-bridge/skills/claude" "$CODEX_DIR/skills/claude"
    seed_registration "$CASE/old/claude-codex-bridge/skills/claude/scripts/claude_mcp_server.py"
    run_bootstrap; success
    assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$PLUGIN/codex-skills/claude"
    assert_calls reconcile
    contains "$CODEX_DIR/config.toml" "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
}

test_unrelated_links_and_mcp() {
    new_case unrelated-owned
    mkdir -p "$CODEX_DIR/skills"
    # All scratch paths themselves have the plugin name as an ancestor. Use an
    # existing unrelated directory as the read-only target so ownership is real.
    unrelated_target="$(cd -P "$PLUGIN_SOURCE/.." && pwd -P)"
    ln -s "$unrelated_target" "$CODEX_DIR/skills/claude"
    seed_registration "$CASE/unrelated/server.py"
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    run_bootstrap
    assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$unrelated_target"
    assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
    assert_calls no-mutations
    contains "$OUTPUT" 'registration is unrelated|registration.*not.*owned'
}

test_unsafe_shim_parent() {
    new_case unsafe-shim-parent
    mkdir -p "$CASE/outside-bin" "$FAKE_HOME/.local"
    ln -s "$CASE/outside-bin" "$FAKE_HOME/.local/bin"
    printf 'untouched\n' > "$CASE/outside-bin/codex"
    cp "$CASE/outside-bin/codex" "$CASE/original"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert cmp "$CASE/original" "$CASE/outside-bin/codex"
    assert test "$(find "$CASE/outside-bin" -type f | wc -l | tr -d ' ')" = 1
    contains "$OUTPUT" 'Refusing shim|resolved parent is outside'
}

test_text_mcp_fallback() {
    new_case text-mcp-fallback
    NO_JSON=1
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    run_bootstrap; success
    assert_calls no-mutations
    contains "$CODEX_DIR/config.toml" 'tool_timeout_sec = 3600'
}

test_validation_rollback() {
    new_case validation-rollback
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    FAIL_VALIDATION=1
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
    contains "$OUTPUT" 'restored backup|restoring backup|rolled back config'
}

test_concurrent_writer_preserved() {
    new_case concurrent-writer
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    FAIL_VALIDATION=1; INTERVENING_EDIT=1
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    contains "$CODEX_DIR/config.toml" 'concurrent writer preserved'
    contains "$OUTPUT" 'changed|writer|concurrent'
}

test_failed_unlink() {
    new_case failed-unlink
    mkdir -p "$FAKE_HOME/.local/bin"
    ln -s "$APP" "$FAKE_HOME/.local/bin/codex"
    # Fail only the attempted shim removal; staging cleanup still works.
    rm "$COMMON/rm"
    printf '#!/bin/sh\ncase "$*" in *codex.tmp*) exec /bin/rm "$@" ;; *) exit 73 ;; esac\n' > "$COMMON/rm"
    chmod 755 "$COMMON/rm"
    before="$(hash_file "$APP")"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert test "$(readlink "$FAKE_HOME/.local/bin/codex")" = "$APP"
    assert test "$before" = "$(hash_file "$APP")"
    contains "$OUTPUT" 'Cannot remove shim|unlink.*fail|remov.*fail'
}

test_app_bundle_shim_parent() {
    new_case app-bundle-parent
    mkdir -p "$FAKE_HOME/Example.app/bin" "$FAKE_HOME/.local"
    ln -s "$FAKE_HOME/Example.app/bin" "$FAKE_HOME/.local/bin"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert test ! -e "$FAKE_HOME/Example.app/bin/codex"
    contains "$OUTPUT" 'Refusing shim|inside an .app bundle'
}

test_explicit_codex_priority() {
    new_case explicit-codex
    make_native "$CASE/explicit-codex"
    EXPLICIT_CODEX="$CASE/explicit-codex"
    ln -s "$NPM_TARGET" "$BIN/codex"
    run_bootstrap; success
    assert_calls native "$EXPLICIT_CODEX"
    assert test ! -e "$FAKE_HOME/.local/bin/codex"
    contains "$OUTPUT" 'explicit CODEX_BIN'
}

test_failed_version_not_reexecuted() {
    new_case failed-version
    VERSION_FAIL="$APP"
    run_bootstrap; success
    assert_calls failed-version "$APP"
    assert_calls npm 1
    assert test ! -e "$FAKE_HOME/.local/bin/codex"
}

test_missing_prerequisites() {
    for missing in python3 file; do
        new_case "missing-$missing"
        rm "$COMMON/$missing"
        snapshot "$CASE/before"
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        contains "$OUTPUT" "install $missing"
        snapshot "$CASE/after"
        same_snapshot "$CASE/before" "$CASE/after"
        assert_calls no-mutations
    done
}

test_claude_json_auth() {
    new_case claude-json-auth
    AUTH_TOKEN=""
    run_bootstrap; success
    assert_calls auth-json
    contains "$OUTPUT" 'Claude auth: signed in'
}

test_sync_linker_not_invoked() {
    new_case sync-linker
    mkdir -p "$CLAUDE_DIR/skills" "$CLAUDE_DIR/agents"
    for skill in codex gpt; do ln -s "$PLUGIN/skills/$skill" "$CLAUDE_DIR/skills/$skill"; done
    for agent in opus-reviewer opus-consultant; do
        : > "$PLUGIN/agents/$agent.md"
        ln -s "$PLUGIN/agents/$agent.md" "$CLAUDE_DIR/agents/$agent.md"
    done
    printf '#!/bin/sh\nprintf invoked > "%s"\nexit 1\n' "$CASE/linker-invoked" > "$PLUGIN/../link.sh"
    chmod 755 "$PLUGIN/../link.sh"
    run_bootstrap; success
    assert test ! -e "$CASE/linker-invoked"
    for skill in codex gpt; do
        assert test "$(readlink "$CLAUDE_DIR/skills/$skill")" = "$PLUGIN/skills/$skill"
    done
}

test_existing_check_read_only() {
    new_case existing-check
    run_bootstrap; success
    snapshot "$CASE/before"
    : > "$LOG"
    run_bootstrap --check; success
    snapshot "$CASE/after"
    same_snapshot "$CASE/before" "$CASE/after"
    assert_calls no-mutations
}

test_stale_text_registration_with_spaces() {
    new_case stale-text-spaces
    NO_JSON=1
    owned_checkout "$CASE/old checkout/claude-codex-bridge"
    seed_registration "$CASE/old checkout/claude-codex-bridge/skills/claude/scripts/claude_mcp_server.py"
    run_bootstrap; success
    assert_calls reconcile
    contains "$CODEX_DIR/config.toml" "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
}

test_unrelated_command_preserved() {
    new_case unrelated-command
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py" /unrelated/executable
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
    assert_calls no-mutations
    contains "$OUTPUT" 'registration is unrelated|registration.*not.*owned'
}

test_version_timeout() {
    new_case version-timeout
    VERSION_HANG="$APP"
    started=$SECONDS
    run_bootstrap; success
    elapsed=$((SECONDS - started))
    assert test "$elapsed" -ge 9
    assert test "$elapsed" -lt 30
    assert_calls failed-version "$APP"
    assert_calls npm 1
    assert test ! -e "$LOG.late-version"
}

test_writer_before_replace_preserved() {
    new_case writer-before-replace
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    # Instrument only the embedded editor through the private interpreter. At
    # the staged config fsync, emulate an uncooperative writer,
    # after the initial read and immediately before the compare-and-swap check.
    rm "$COMMON/python3"
    "$PYTHON" - "$COMMON/python3" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import os, sys
if len(sys.argv) > 2 and sys.argv[1] == '-' and sys.argv[2].endswith('/config.toml'):
    original_fsync = os.fsync
    count = [0]
    config = sys.argv[2]
    def fsync(fd):
        original_fsync(fd)
        count[0] += 1
        if count[0] == 1:
            with open(config, 'ab') as writer:
                writer.write(b'\\n# writer before replacement\\n')
    os.fsync = fsync
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-editor>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as f:
    f.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    "$PYTHON" - "$CASE/original.toml" "$CODEX_DIR/config.toml" <<'PY'
from pathlib import Path
import sys
assert Path(sys.argv[2]).read_bytes() == Path(sys.argv[1]).read_bytes() + b'\n# writer before replacement\n'
PY
    contains "$OUTPUT" 'writer changed config.toml'
}

test_existing_subtable_and_backup_retention() {
    new_case existing-subtable
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    printf '\n[mcp_servers.claude-bridge.tools.claude_task] # keep existing policy\napproval_mode = "deny"\n' >> "$CODEX_DIR/config.toml"
    for day in 01 02 03 04 05 06; do
        printf 'previous backup %s\n' "$day" > "$CODEX_DIR/config.toml.bak-202001$day-000000-000000"
    done
    run_bootstrap; success
    "$PYTHON" - "$CODEX_DIR" <<'PY'
from pathlib import Path
import sys
home = Path(sys.argv[1]); text = (home / 'config.toml').read_text()
existing = '[mcp_servers.claude-bridge.tools.claude_task] # keep existing policy\napproval_mode = "deny"\n'
assert text.count('[mcp_servers.claude-bridge.tools.claude_task]') == 1
assert existing in text, text
backups = sorted(home.glob('config.toml.bak-*'))
assert len(backups) == 5, backups
assert backups[0].name == 'config.toml.bak-20200103-000000-000000', backups
PY
}

test_script_launcher_on_path() {
    new_case script-launcher-on-path
    # a Node-style launcher: a script that answers --version itself and forwards everything else (mcp get/add …) to the stub
    mkdir -p "$CASE/npm-lib"; cp "$STUB" "$CASE/npm-lib/codex"; chmod 755 "$CASE/npm-lib/codex"   # the "real" CLI the launcher forwards to
    printf '#!/bin/sh\ncase "$1" in --version) echo "codex-cli 9.9.9 (script launcher)"; exit 0;; esac\nexec "%s" "$@"\n' "$CASE/npm-lib/codex" > "$BIN/codex"
    chmod 755 "$BIN/codex"
    run_bootstrap; success
    assert test ! -e "$FAKE_HOME/.local/bin/codex"
    assert_calls npm 0
    contains "$OUTPUT" 'standalone'
}

test_hanging_script_on_path_rejected() {
    new_case hanging-script-on-path
    printf '#!/bin/sh\ncase "$1" in --version) sleep 30;; esac\nexit 0\n' > "$BIN/codex"
    chmod 755 "$BIN/codex"
    started="$(date +%s)"
    run_bootstrap; success
    elapsed=$(( $(date +%s) - started ))
    assert test "$elapsed" -lt 25
    contains "$OUTPUT" 'exceeded 10 seconds'
    contains "$FAKE_HOME/.local/bin/codex" 'codex shim'
}

test_approval_overrides() {
    for policy in approve empty; do
        new_case "approval-$policy"
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        printf '\n[mcp_servers.claude-bridge.tools.claude_task] # existing task policy\n' >> "$CODEX_DIR/config.toml"
        if [ "$policy" = approve ]; then printf 'approval_mode = "approve"\n' >> "$CODEX_DIR/config.toml"; fi
        run_bootstrap; success
        "$PYTHON" - "$CODEX_DIR/config.toml" <<'PY'
from pathlib import Path
import re, sys
text = Path(sys.argv[1]).read_text()
headers = re.findall(r'(?m)^\[mcp_servers\.claude-bridge\.tools\.claude_task\].*$', text)
assert len(headers) == 1, text
task = re.search(r'(?ms)^\[mcp_servers\.claude-bridge\.tools\.claude_task\][^\n]*\n(.*?)(?=^\[|\Z)', text)[1]
assert re.findall(r'(?m)^approval_mode\s*=\s*(.*)$', task) == ['"prompt"'], text
assert '# existing task policy' in text
PY
        assert_calls reconcile
    done
}

test_stale_transaction_preserves_fields() {
    new_case stale-transaction
    # Ownership is structural even when the installation directory was renamed.
    owned_checkout "$CASE/previous install/renamed-plugin"
    seed_registration "$CASE/previous install/renamed-plugin/skills/claude/scripts/claude_mcp_server.py" /previous/python3.12
    mkdir -p "$CODEX_DIR/skills"
    ln -s "$CASE/previous install/renamed-plugin/skills/claude" "$CODEX_DIR/skills/claude"
    cat >> "$CODEX_DIR/config.toml" <<'TOML'
enabled = false
env_vars = [
    "CUSTOM_ACCOUNT",
    "CLAUDE_CONFIG_DIR",
]
env = { CUSTOM_ACCOUNT = "keep-this-account" }

[mcp_servers.claude-bridge.tools.claude_task]
approval_mode = "deny"

[mcp_servers.claude-bridge.tools.claude_ask]
approval_mode = "deny"
TOML
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    FAIL_ADD=1
    run_bootstrap; success
    assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$PLUGIN/codex-skills/claude"
    assert_calls reconcile
    "$PYTHON" - "$CODEX_DIR" "$CASE/original.toml" "$PLUGIN" <<'PY'
from pathlib import Path
import ast, re, sys
home = Path(sys.argv[1]); text = (home / 'config.toml').read_text()
assert 'command = "python3"' in text, text
assert ast.literal_eval(re.search(r'(?m)^args = (.*)$', text)[1]) == [sys.argv[3] + '/codex-skills/claude/scripts/claude_mcp_server.py']
assert re.search(r'(?m)^enabled = true$', text), text
assert 'env = { CUSTOM_ACCOUNT = "keep-this-account" }' in text
env = ast.literal_eval(re.search(r'(?ms)^env_vars = (\[.*?\])', text)[1])
assert 'CUSTOM_ACCOUNT' in env and env.count('CLAUDE_CONFIG_DIR') == 1, env
for name in ('claude_task', 'claude_ask'):
    assert '[mcp_servers.claude-bridge.tools.' + name + ']\napproval_mode = "deny"' in text
backups = list(home.glob('config.toml.bak-*'))
assert len(backups) == 1 and backups[0].read_bytes() == Path(sys.argv[2]).read_bytes()
PY
}

test_stale_transaction_rollback() {
    new_case stale-transaction-rollback
    owned_checkout "$CASE/previous install"
    seed_registration "$CASE/previous install/skills/claude/scripts/claude_mcp_server.py"
    printf 'enabled = false\nenv_vars = ["KEEP_ACCOUNT"]\n\n[mcp_servers.claude-bridge.tools.claude_task]\napproval_mode = "deny"\n' >> "$CODEX_DIR/config.toml"
    cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
    FAIL_VALIDATION=1; FAIL_ADD=1
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
    assert_calls reconcile
    contains "$OUTPUT" 'restored backup|restoring backup|rolled back config'
}

test_multiline_toml_strings() {
    for quoting in basic literal; do
        new_case "multiline-$quoting"
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        "$PYTHON" - "$CODEX_DIR/config.toml" "$CASE/prefix" "$CASE/note" "$quoting" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1]); quote = '"""' if sys.argv[4] == 'basic' else "'''"
prefix = ('developer_instructions = ' + quote + '\n'
          '[mcp_servers.claude-bridge.tools.claude_task]\n'
          'approval_mode = "approve"\nstartup_timeout_sec = 7\n' + quote + '\n\n')
note = ('description = ' + quote + '\nstartup_timeout_sec = 7\n'
        '[mcp_servers.claude-bridge.tools.claude_task]\n' + quote + '\n')
Path(sys.argv[2]).write_text(prefix); Path(sys.argv[3]).write_text(note)
path.write_text(prefix + path.read_text() + note + 'startup_timeout_sec = 2\n')
PY
        run_bootstrap; success
        "$PYTHON" - "$CODEX_DIR/config.toml" "$CASE/prefix" "$CASE/note" <<'PY'
from pathlib import Path
import re, sys
text = Path(sys.argv[1]).read_text()
assert text.startswith(Path(sys.argv[2]).read_text()), text
assert Path(sys.argv[3]).read_text() in text, text
syntax = re.sub(r'""".*?"""|\x27\x27\x27.*?\x27\x27\x27', '', text, flags=re.S)
assert re.findall(r'(?m)^startup_timeout_sec\s*=\s*(.*)$', syntax) == ['30'], syntax
assert re.search(r'\[mcp_servers.claude-bridge.tools.claude_task\]\s*\napproval_mode = "prompt"', syntax), syntax
PY
        assert_calls reconcile
    done
}

test_unsafe_codex_destinations() {
    for destination in app outside skills-app skills-outside; do
        new_case "unsafe-codex-$destination"
        mkdir -p "$FAKE_HOME/Forbidden.app/data" "$CASE/outside"
        case "$destination" in
            app) ln -s "$FAKE_HOME/Forbidden.app/data" "$FAKE_HOME/account-link"; CODEX_DIR="$FAKE_HOME/account-link/codex"; watched="$FAKE_HOME/Forbidden.app/data" ;;
            outside) ln -s "$CASE/outside" "$FAKE_HOME/account-link"; CODEX_DIR="$FAKE_HOME/account-link/codex"; watched="$CASE/outside" ;;
            skills-app) seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"; ln -s "$FAKE_HOME/Forbidden.app/data" "$CODEX_DIR/skills"; watched="$FAKE_HOME/Forbidden.app/data" ;;
            skills-outside) seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"; ln -s "$CASE/outside" "$CODEX_DIR/skills"; watched="$CASE/outside" ;;
        esac
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test "$(find "$watched" -mindepth 1 | wc -l | tr -d ' ')" = 0
        contains "$OUTPUT" 'Refusing|outside HOME|\.app bundle'
        assert_calls reconcile
    done
}

test_config_precedes_skill() {
    new_case config-precedes-skill
    mkdir -p "$CODEX_DIR"
    printf 'do not replace\n' > "$CASE/original.toml"
    ln -s "$CASE/original.toml" "$CODEX_DIR/config.toml"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert test ! -e "$CODEX_DIR/skills"
    assert test "$(readlink "$CODEX_DIR/config.toml")" = "$CASE/original.toml"
    assert_calls reconcile
    contains "$OUTPUT" 'Refusing.*configuration'
}

test_unsafe_optional_plugin_destinations() {
    for destination in app outside plugins-app plugins-outside; do
        new_case "unsafe-plugin-$destination"
        PLUGIN_ENABLED=1
        : > "$PLUGIN/../link.sh"
        case "$destination" in *app) watched="$FAKE_HOME/Forbidden.app/data" ;; *) watched="$CASE/outside" ;; esac
        mkdir -p "$watched"
        case "$destination" in
            plugins-*) mkdir -p "$CLAUDE_DIR"; ln -s "$watched" "$CLAUDE_DIR/plugins" ;;
            *) ln -s "$watched" "$FAKE_HOME/claude-link"; CLAUDE_DIR="$FAKE_HOME/claude-link/config" ;;
        esac
        # This scenario isolates the optional marketplace install. Standalone
        # links have their own mandatory destination-safety coverage below.
        run_bootstrap --no-claude-links; success
        assert test "$(find "$watched" -mindepth 1 | wc -l | tr -d ' ')" = 0
        contains "$OUTPUT" 'Optional OpenAI plugin: skipped'
        contains "$OUTPUT" 'Refusing|outside HOME|\.app bundle'
        "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert not [c for c in calls if c['tool'] == 'claude' and c['args'][:1] == ['plugin']], calls
PY
    done
}

test_healthy_executable_at_shim() {
    for location in on-path off-path explicit-app; do
        new_case "healthy-executable-at-shim-$location"
        if [ "$location" = off-path ]; then CALLER_PATH="$BIN:$COMMON"; fi
        if [ "$location" = explicit-app ]; then EXPLICIT_CODEX="$APP"; fi
        mkdir -p "$FAKE_HOME/.local/bin"
        make_native "$FAKE_HOME/.local/bin/codex"
        before="$(hash_file "$FAKE_HOME/.local/bin/codex")"
        run_bootstrap; success
        assert test "$before" = "$(hash_file "$FAKE_HOME/.local/bin/codex")"
        assert test "$(find "$FAKE_HOME/.local/bin" -type f | wc -l | tr -d ' ')" = 1
        if [ "$location" != explicit-app ]; then assert_calls native "$FAKE_HOME/.local/bin/codex"; fi
        assert_calls npm 0
        contains "$OUTPUT" 'standalone'
    done
}

test_disabled_owned_registration() {
    for format in json text; do
        new_case "disabled-owned-$format"
        if [ "$format" = text ]; then NO_JSON=1; fi
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        printf 'enabled = false\n' >> "$CODEX_DIR/config.toml"
        cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
        run_bootstrap --check
        assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
        contains "$OUTPUT" 'disabled|re-enable|reconcile|would.*updat'
        run_bootstrap; success
        contains "$CODEX_DIR/config.toml" '^enabled = true$'
        assert_calls reconcile
    done
}

test_disabled_unrelated_registration() {
    for format in json text; do
        new_case "disabled-unrelated-$format"
        if [ "$format" = text ]; then NO_JSON=1; fi
        seed_registration "$CASE/unrelated/server.py"
        printf 'enabled = false\n' >> "$CODEX_DIR/config.toml"
        cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
        assert_calls reconcile
        contains "$OUTPUT" 'registration is unrelated|registration.*not.*owned'
    done
}

test_repaired_executable_reprobed() {
    new_case repaired-executable
    printf '#!/bin/sh\nexit 19\n' > "$BIN/codex"
    chmod 755 "$BIN/codex"
    NPM_REPAIR="$BIN/codex"
    VERSION_FAIL="$APP"
    run_bootstrap; success
    assert_calls npm 1
    "$PYTHON" - "$LOG" "$BIN/codex" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
repaired = [c for c in calls if c['tool'] == 'codex' and c['path'] == sys.argv[2]]
assert repaired and any(c['args'] == ['--version'] for c in repaired), calls
assert any(c['args'] == ['login', 'status'] for c in repaired), calls
PY
    assert test ! -e "$FAKE_HOME/.local/bin/codex"
}

test_stderr_warnings_on_success() {
    for format in json text; do
        new_case "stderr-warning-$format"
        STDERR_WARNING=1
        if [ "$format" = text ]; then NO_JSON=1; fi
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        run_bootstrap; success
        contains "$OUTPUT" 'Codex login: signed in'
        contains "$OUTPUT" 'MCP registration and settings: ok'
        assert_calls reconcile
    done
}

test_misleading_ownership_names() {
    for format in json text; do
        new_case "misleading-name-$format"
        if [ "$format" = text ]; then NO_JSON=1; fi
        unrelated="$CASE/unrelated-claude-codex-bridge-copy"
        mkdir -p "$unrelated/skills/claude/scripts" "$CODEX_DIR/skills"
        printf 'unrelated server\n' > "$unrelated/skills/claude/scripts/claude_mcp_server.py"
        ln -s "$unrelated/skills/claude" "$CODEX_DIR/skills/claude"
        seed_registration "$unrelated/skills/claude/scripts/claude_mcp_server.py"
        cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$unrelated/skills/claude"
        assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
        assert_calls reconcile
        contains "$OUTPUT" 'unrelated Codex skill untouched'
        contains "$OUTPUT" 'registration is unrelated|registration.*not.*owned'
    done
}

test_registration_json_uses_stdin() {
    new_case registration-json-stdin
    JSON_SIZE=2000000
    observe_python
    seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    run_bootstrap; success
    "$PYTHON" - "$LOG.python" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert calls and not any(c['json_in_argv'] for c in calls), calls
assert max(max(c['lengths'], default=0) for c in calls) < 65536, calls
PY
    assert_calls reconcile
}

test_optional_plugin_failures_nonfatal() {
    for failure in marketplace install; do
        new_case "optional-plugin-$failure-failure"
        PLUGIN_ENABLED=1; PLUGIN_FAILURE="$failure"
        mkdir -p "$CLAUDE_DIR/plugins/cache"
        mv "$PLUGIN" "$CLAUDE_DIR/plugins/cache/claude-codex-bridge"
        PLUGIN="$CLAUDE_DIR/plugins/cache/claude-codex-bridge"
        observe_python
        run_bootstrap; success
        contains "$OUTPUT" 'Optional OpenAI plugin: skipped \('
        assert test ! -e "$CLAUDE_DIR/skills"
        assert test ! -e "$CLAUDE_DIR/agents"
        if [ "$failure" = marketplace ]; then
            contains "$OUTPUT" 'claude plugin marketplace add openai/codex-plugin-cc'
        else
            contains "$OUTPUT" 'claude plugin install codex@openai-codex --scope user -y'
        fi
        "$PYTHON" - "$LOG.python" "$failure" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
commands = [('marketplace', 'add'), ('install',)] if sys.argv[2] == 'install' else [('marketplace', 'add')]
for tail in commands:
    found = [c for c in calls if c.get('command', [])[1:2 + len(tail)] == ['plugin'] + list(tail)]
    assert found and all(c['bound'] == 300 for c in found), (tail, calls)
PY
    done
}

test_unset_account_environment() {
    new_case unset-account-environment
    UNSET_HOMES=1; AUTH_TOKEN=""
    run_bootstrap; success
    assert test -L "$FAKE_HOME/.codex/skills/claude"
    assert test -f "$FAKE_HOME/.codex/config.toml"
    "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
cli = [c for c in calls if c['tool'] in ('codex', 'claude')]
assert cli, calls
for call in cli:
    assert 'CODEX_HOME' not in call['env'], call
    assert 'CLAUDE_CONFIG_DIR' not in call['env'], call
assert any(c['tool'] == 'claude' and c['args'] == ['auth', 'status'] for c in cli), cli
PY
}

test_effective_config_verified() {
    for setting in approval enabled; do
        new_case "effective-config-$setting"
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$setting" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import os, sys
if len(sys.argv) > 2 and sys.argv[1] == '-' and sys.argv[2].endswith('/config.toml'):
    changed = [False]
    config = sys.argv[2]
    def hook(original):
        def replace(source, target, *args, **kwargs):
            result = original(source, target, *args, **kwargs)
            if str(target) == config and not changed[0]:
                changed[0] = True
                with open(config) as reader:
                    text = reader.read()
                before, after = (('approval_mode = "prompt"', 'approval_mode = "approve"')
                                 if SETTING == 'approval' else ('enabled = true', 'enabled = false'))
                assert before in text, text
                with open(config, 'w') as writer:
                    writer.write(text.replace(before, after))
            return result
        return replace
    os.replace = hook(os.replace)
    os.link = hook(os.link)
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-editor>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as f:
    f.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) + '\nSETTING = ' + repr(sys.argv[2]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        contains "$OUTPUT" 'MCP registration and settings: FAILED'
        assert_calls reconcile
    done
}

test_bounded_interruption_cleanup() {
    new_case bounded-interruption
    # Run the actual bounded function in isolation. The launcher reports its
    # supervisor PID so signals target the wrapper, exactly as a caller would.
    "$PYTHON" - "$PLUGIN/bootstrap.sh" "$CASE" "$COMMON" <<'PY'
from pathlib import Path
import json, os, re, signal, subprocess, sys, time
source, case, common = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
match = re.search(r'(?ms)^bounded\(\)\{\n(.*?)^\}(?: 3>&1)?\n', source.read_text())
assert match, 'bounded function missing'
driver = case / 'bounded-driver.sh'
driver.write_text('#!/bin/bash\n' + match[0] + '\nbounded 30 "$@"\n')
launcher = case / 'interruption-child.py'
launcher.write_text('''import json, os, signal, subprocess, sys, time
from pathlib import Path
code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)"
child = subprocess.Popen([sys.executable, '-c', code])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(json.dumps([os.getppid(), os.getpid(), child.pid]))
time.sleep(90)
''')
def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux init may defer reaping orphan zombies; /proc avoids invoking ps,
    # which is unavailable in some sandboxes. Darwin reaps these promptly.
    try:
        state = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[0]
        return state != 'Z'
    except FileNotFoundError:
        return True
for sig, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
    pids = case / ('pids-' + str(sig))
    process = subprocess.Popen(['/bin/bash', str(driver), sys.executable, str(launcher), str(pids)],
                               env={**os.environ, 'PATH': common}, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    tracked = []
    try:
        deadline = time.monotonic() + 8
        while not pids.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            time.sleep(0.02)
        assert pids.exists(), 'launcher did not start'
        tracked = json.loads(pids.read_text())
        # Give the descendant time to install its deliberate SIGTERM ignore.
        time.sleep(0.1)
        os.kill(tracked[0], sig)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == expected, (sig, process.returncode, stdout, stderr)
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in tracked) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(alive(pid) for pid in tracked), (sig, tracked)
    finally:
        for pid in tracked:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
PY
}

test_transaction_interruption_rollback() {
    for interrupt in SIGINT SIGTERM; do
        new_case "transaction-$interrupt"
        owned_checkout "$CASE/previous install"
        seed_registration "$CASE/previous install/skills/claude/scripts/claude_mcp_server.py"
        printf 'enabled = false\nenv_vars = ["KEEP_ACCOUNT"]\n' >> "$CODEX_DIR/config.toml"
        cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$interrupt" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import os, signal, sys
if len(sys.argv) > 2 and sys.argv[1] == '-' and sys.argv[2].endswith('/config.toml'):
    changed = [False]
    config = sys.argv[2]
    def hook(original):
        def replace(source, target, *args, **kwargs):
            result = original(source, target, *args, **kwargs)
            if str(target) == config and not changed[0]:
                changed[0] = True
                os.kill(os.getpid(), getattr(signal, INTERRUPT))
            return result
        return replace
    os.replace = hook(os.replace)
    os.link = hook(os.link)
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-editor>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as f:
    f.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) + '\nINTERRUPT = ' + repr(sys.argv[2]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
        assert_calls reconcile
        contains "$OUTPUT" 'restored backup|restoring backup|rolled back config'
    done
}

test_replacement_boundary_conflict() {
    for timing in boundary recreated-live rollback-boundary boundary-symlink no-hardlinks; do
        new_case "replacement-$timing-conflict"
        if [ "$timing" = rollback-boundary ]; then FAIL_VALIDATION=1; fi
        printf 'concurrent external configuration\n' > "$LOG.external-config"
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$timing" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import errno, os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1] == '-' and sys.argv[2].endswith('/config.toml'):
    config = sys.argv[2]
    injected = [False]
    moves = [0]
    def hook(original):
        def replace(source, target, *args, **kwargs):
            if str(source) == config and str(target).startswith(config + '.bak-'):
                moves[0] += 1
            before_move = ((TIMING in ('boundary', 'boundary-symlink') and config in (str(source), str(target))) or
                           (TIMING == 'rollback-boundary' and moves[0] == 2))
            if before_move and not injected[0]:
                injected[0] = True
                if TIMING == 'boundary-symlink':
                    os.unlink(config)
                    os.symlink(os.environ['TEST_LOG'] + '.external-config', config)
                else:
                    with open(config, 'ab') as writer:
                        writer.write(b'\\n# writer at replacement boundary\\n')
                Path(os.environ['TEST_LOG'] + '.race-fired').write_text('injected')
                Path(os.environ['TEST_LOG'] + '.race-version').write_bytes(Path(config).read_bytes())
            result = original(source, target, *args, **kwargs)
            if TIMING == 'recreated-live' and not injected[0] and str(source) == config:
                injected[0] = True
                Path(config).write_bytes(Path(target).read_bytes() + b'\\n# writer at replacement boundary\\n')
                Path(os.environ['TEST_LOG'] + '.race-fired').write_text('injected')
                Path(os.environ['TEST_LOG'] + '.race-version').write_bytes(Path(config).read_bytes())
            return result
        return replace
    os.replace = hook(os.replace)
    os.rename = hook(os.rename)
    original_link = os.link
    def link(source, target, *args, **kwargs):
        if TIMING == 'no-hardlinks':
            Path(os.environ['TEST_LOG'] + '.race-fired').write_text('injected')
            # An unexpected I/O error must still fail closed; the supported
            # filesystem limitations are exercised by the fallback scenario.
            raise OSError(errno.EIO, 'fixture hard link probe I/O failure')
        return original_link(source, target, *args, **kwargs)
    os.link = link
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-editor>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as stream:
    stream.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) +
                 '\nTIMING = ' + repr(sys.argv[2]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test -f "$LOG.race-fired"
        "$PYTHON" - "$CODEX_DIR" "$CASE/original.toml" "$timing" "$LOG.race-version" "$LOG.external-config" <<'PY'
from pathlib import Path
import os, sys
home, original = Path(sys.argv[1]), Path(sys.argv[2]).read_bytes()
if sys.argv[3] == 'no-hardlinks':
    assert (home / 'config.toml').read_bytes() == original
    assert not list(home.glob('config.toml.bak-*'))
    assert not list(home.glob('config.toml.conflict-*'))
    sys.exit(0)
concurrent = Path(sys.argv[4]).read_bytes()
assert (home / 'config.toml').read_bytes() == concurrent
backups = list(home.glob('config.toml.bak-*'))
displaced = original if sys.argv[3] == 'recreated-live' else concurrent
assert any(path.read_bytes() == displaced for path in backups), backups
if sys.argv[3] == 'boundary-symlink':
    assert (home / 'config.toml').is_symlink()
    assert os.readlink(home / 'config.toml') == sys.argv[5]
    assert any(path.is_symlink() and os.readlink(path) == sys.argv[5] for path in backups), backups
    assert Path(sys.argv[5]).read_bytes() == b'concurrent external configuration\n'
conflicts = list(home.glob('config.toml.conflict-*'))
assert len(conflicts) == 1, conflicts
ours = conflicts[0].read_text()
if sys.argv[3] == 'rollback-boundary':
    assert conflicts[0].read_bytes() == original, ours
    assert b'tool_timeout_sec = 3600' in concurrent, concurrent
else:
    assert 'tool_timeout_sec = 3600' in ours and 'writer at replacement boundary' not in ours, ours
PY
        if [ "$timing" = no-hardlinks ]; then
            contains "$OUTPUT" 'hard links unavailable|hard.*link|not supported'
        else
            contains "$OUTPUT" 'concurrent|another writer|writer changed'
            contains "$OUTPUT" 'conflict-'
        fi
        assert_calls reconcile
    done
}

test_hardlink_publication_fallback() {
    local limitation
    for limitation in ENOTSUP EPERM EXDEV EOPNOTSUPP rollback boundary boundary-symlink; do
        new_case "hardlink-fallback-$limitation"
        seed_registration "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
        printf 'env_vars = ["CUSTOM_ACCOUNT"]\n' >> "$CODEX_DIR/config.toml"
        chmod 640 "$CODEX_DIR/config.toml"
        cp "$CODEX_DIR/config.toml" "$CASE/original.toml"
        printf 'concurrent external configuration\n' > "$LOG.external-config"
        if [ "$limitation" = rollback ]; then FAIL_VALIDATION=1; fi
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$limitation" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import errno, os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1] == '-' and sys.argv[2].endswith('/config.toml'):
    config = sys.argv[2]
    injected = [False]
    def link(source, target, *args, **kwargs):
        if str(target).endswith('.probe'):
            Path(os.environ['TEST_LOG'] + '.fallback-probe-fired').write_text('injected')
        code = getattr(errno, LIMITATION if LIMITATION.startswith('E') else 'ENOTSUP')
        raise OSError(code, 'fixture filesystem cannot create hard links')
    os.link = link
    original_replace = os.replace
    def replace(source, target, *args, **kwargs):
        if (LIMITATION in ('boundary', 'boundary-symlink') and not injected[0] and
                str(source) == config and str(target).startswith(config + '.bak-')):
            injected[0] = True
            if LIMITATION == 'boundary-symlink':
                os.unlink(config)
                os.symlink(os.environ['TEST_LOG'] + '.external-config', config)
            else:
                with open(config, 'ab') as writer:
                    writer.write(b'\\n# writer at fallback boundary\\n')
            Path(os.environ['TEST_LOG'] + '.fallback-race-fired').write_text('injected')
            Path(os.environ['TEST_LOG'] + '.race-version').write_bytes(Path(config).read_bytes())
        return original_replace(source, target, *args, **kwargs)
    os.replace = replace
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-editor>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as stream:
    stream.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) +
                 '\nLIMITATION = ' + repr(sys.argv[2]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test -f "$LOG.fallback-probe-fired"
        assert_calls reconcile
        "$PYTHON" - "$OUTPUT" <<'PY'
from pathlib import Path
import re, sys
warnings = [line for line in Path(sys.argv[1]).read_text().splitlines()
            if re.search(r'warn', line, re.I) and re.search(r'hard.?link', line, re.I)]
assert warnings, 'missing filesystem limitation warning'
assert all(re.search(r'concurrent.?writer.*window', line, re.I) for line in warnings), warnings
assert all('replace' in line.lower() for line in warnings), warnings
PY
        case "$limitation" in
            rollback)
                assert test "$LAST_STATUS" -ne 0
                assert cmp "$CASE/original.toml" "$CODEX_DIR/config.toml"
                contains "$OUTPUT" 'restored backup|restoring backup|rolled back config'
                ;;
            boundary|boundary-symlink)
                assert test "$LAST_STATUS" -ne 0
                assert test -f "$LOG.fallback-race-fired"
                assert cmp "$LOG.race-version" "$CODEX_DIR/config.toml"
                contains "$OUTPUT" 'CONFLICT:.*another writer|concurrent'
                contains "$OUTPUT" 'conflict-'
                ;;
            *)
                success
                contains "$OUTPUT" 'MCP registration and settings: ok'
                ;;
        esac
        "$PYTHON" - "$CODEX_DIR" "$CASE/original.toml" "$limitation" "$LOG" <<'PY'
from pathlib import Path
import ast, json, os, re, stat, sys
home, original, limitation, log = Path(sys.argv[1]), Path(sys.argv[2]).read_bytes(), sys.argv[3], sys.argv[4]
config = home / 'config.toml'
backups = list(home.glob('config.toml.bak-*'))
conflicts = list(home.glob('config.toml.conflict-*'))
assert not list(home.glob('.config.toml-*')), list(home.iterdir())
if limitation in ('boundary', 'boundary-symlink'):
    concurrent = Path(log + '.race-version').read_bytes()
    assert any(backup.read_bytes() == concurrent for backup in backups), backups
    assert len(conflicts) == 1, conflicts
    assert 'tool_timeout_sec = 3600' in conflicts[0].read_text()
    if limitation == 'boundary-symlink':
        assert config.is_symlink() and os.readlink(config) == log + '.external-config'
        assert any(backup.is_symlink() and os.readlink(backup) == log + '.external-config'
                   for backup in backups), backups
        assert Path(log + '.external-config').read_bytes() == b'concurrent external configuration\n'
else:
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert any(backup.read_bytes() == original for backup in backups), backups
    assert not conflicts, conflicts
    if limitation != 'rollback':
        text = config.read_text()
        env = ast.literal_eval(re.search(r'(?m)^env_vars = (.*)$', text)[1])
        assert {'CUSTOM_ACCOUNT', 'CLAUDE_BRIDGE_ROOT', 'CLAUDE_BRIDGE_ALLOW_ANY_CWD',
                'CLAUDE_BRIDGE_TOOL_DEADLINE', 'CLAUDE_BRIDGE_ALLOW_WRITE', 'CLAUDE_BRIDGE_ENGINE'} <= set(env), env
        assert 'tool_timeout_sec = 3600' in text and 'approval_mode = "prompt"' in text, text
        calls = [json.loads(line) for line in Path(log).read_text().splitlines()]
        assert any(call['tool'] == 'codex' and call['args'] == ['mcp', 'get', 'claude-bridge']
                   for call in calls), calls
PY
    done
}

test_explicit_invalid_codex_authoritative() {
    for reason in empty missing not-executable version capability; do
        new_case "explicit-invalid-$reason"
        EXPLICIT_CODEX="$CASE/explicit-codex"; EXPLICIT_CODEX_SET=1
        make_native "$EXPLICIT_CODEX"
        ln -s "$NPM_TARGET" "$BIN/codex"
        case "$reason" in
            empty) EXPLICIT_CODEX="" ;;
            missing) rm "$EXPLICIT_CODEX" ;;
            not-executable) chmod 644 "$EXPLICIT_CODEX" ;;
            version) VERSION_FAIL="$EXPLICIT_CODEX" ;;
            capability) MISSING_CAPABILITY=--output-schema ;;
        esac
        snapshot "$CASE/before"
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        contains "$OUTPUT" 'CODEX_BIN'
        contains "$OUTPUT" 'Codex executable: FAILED|Prerequisites: FAILED'
        case "$reason" in
            empty) contains "$OUTPUT" 'empty|unset|not.*path|missing' ;;
            missing) contains "$OUTPUT" 'missing|does not exist|not.*file' ;;
            not-executable) contains "$OUTPUT" 'not executable|not.*executable' ;;
            version) contains "$OUTPUT" 'version.*fail|version.*exceeded' ;;
            capability) contains "$OUTPUT" 'output-schema|capabilit|flags' ;;
        esac
        snapshot "$CASE/after"
        same_snapshot "$CASE/before" "$CASE/after"
        assert_calls no-mutations
        "$PYTHON" - "$LOG" "$EXPLICIT_CODEX" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
codex = [call for call in calls if call['tool'] == 'codex']
assert all(call['path'] == sys.argv[2] and call['args'] in
           (['--version'], ['exec', '--help']) for call in codex), codex
PY
    done
}

test_codex_capability_floor() {
    for missing in --json --output-schema --output-last-message --ignore-user-config failed-help timed-out-help; do
        new_case "capability-$missing"
        EXPLICIT_CODEX="$APP"
        case "$missing" in
            failed-help) HELP_FAIL="$APP" ;;
            timed-out-help) HELP_HANG="$APP" ;;
            *) MISSING_CAPABILITY="$missing" ;;
        esac
        snapshot "$CASE/before"
        started=$SECONDS
        run_bootstrap
        elapsed=$((SECONDS - started))
        assert test "$LAST_STATUS" -ne 0
        contains "$OUTPUT" 'exec.*help|capabilit|flags'
        snapshot "$CASE/after"
        same_snapshot "$CASE/before" "$CASE/after"
        assert_calls no-mutations
        "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert [call['args'] for call in calls if call['tool'] == 'codex'] == [
    ['--version'], ['exec', '--help']], calls
PY
        if [ "$missing" = timed-out-help ]; then
            assert test "$elapsed" -ge 9
            assert test "$elapsed" -lt 30
            assert test ! -e "$LOG.late-help"
        fi
    done
}

test_codex_login_streams() {
    for stream in stderr stdout; do
        for warning in 0 1; do
            new_case "login-$stream-warning-$warning"
            if [ "$stream" = stdout ]; then LOGIN_STDOUT=1; fi
            STDERR_WARNING="$warning"
            run_bootstrap; success
            contains "$OUTPUT" 'Codex login: signed in'
            contains "$OUTPUT" 'Codex sign-in: ok'
        done
        for failure in contradictory nonzero; do
            new_case "login-$stream-$failure"
            if [ "$stream" = stdout ]; then LOGIN_STDOUT=1; fi
            if [ "$failure" = contradictory ]; then LOGIN_CONTRADICTION=1; else LOGIN_STATUS=7; fi
            run_bootstrap
            assert test "$LAST_STATUS" -ne 0
            contains "$OUTPUT" 'Codex sign-in: FAILED'
        done
    done
}

test_outer_bootstrap_signal_cleanup() {
    new_case outer-bootstrap-signals
    "$PYTHON" - "$PLUGIN/bootstrap.sh" "$CASE" "$FAKE_HOME" "$CALLER_PATH" \
        "$APP" "$CODEX_DIR" "$CLAUDE_DIR" "$LOG" "$NATIVE_LIST" "$BIN" "$NPM_TARGET" <<'PY'
from pathlib import Path
import json, os, signal, subprocess, sys, time
script, case, home, path, app, codex_home, claude_home, log, native, binary, npm = sys.argv[1:]
env = {'HOME': home, 'PATH': path, 'TMPDIR': case + '/tmp', 'LC_ALL': 'C',
       'CODEX_HOME': codex_home, 'CLAUDE_CONFIG_DIR': claude_home, 'CODEX_BIN': app,
       'BRIDGE_APP_LAUNCHER': app, 'BRIDGE_CODEX_FIXED_ROOT': case + '/fixed-root', 'TEST_LOG': log, 'TEST_NATIVE_LIST': native,
       'TEST_BIN': binary, 'TEST_NPM_TARGET': npm, 'TEST_PLUGIN': str(Path(script).parent)}
def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return True
for command in ('codex --version', 'codex login status', 'claude auth status'):
    for sig, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
        pids = Path(log + '.sleep-pids')
        if pids.exists(): pids.unlink()
        process = subprocess.Popen(['/bin/bash', script, '--no-plugin'],
            cwd=case, env={**env, 'TEST_SLEEP_COMMAND': command}, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tracked = []
        try:
            deadline = time.monotonic() + 10
            while not pids.exists() and time.monotonic() < deadline:
                assert process.poll() is None, (command, process.communicate())
                time.sleep(0.02)
            assert pids.exists(), ('sleeper did not start', command)
            tracked = json.loads(pids.read_text())
            time.sleep(0.1)
            os.kill(process.pid, sig)
            stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == expected, (command, sig, process.returncode, stdout, stderr)
            deadline = time.monotonic() + 3
            while any(alive(pid) for pid in tracked) and time.monotonic() < deadline:
                time.sleep(0.02)
            survivors = [pid for pid in tracked if alive(pid)]
            assert not survivors, (command, sig, survivors, stdout, stderr)
        finally:
            if len(tracked) > 1:
                try: os.killpg(tracked[1], signal.SIGKILL)
                except ProcessLookupError: pass
            for pid in tracked:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
PY
}

test_skill_ownership_rechecked_in_helper() {
    for timing in entry staged; do
        new_case "skill-ownership-race-$timing"
        owned_checkout "$CASE/old-checkout"
        mkdir -p "$CODEX_DIR/skills" "$CASE/unrelated/skills/claude/scripts"
        : > "$CASE/unrelated/skills/claude/scripts/claude_mcp_server.py"
        ln -s "$CASE/old-checkout/skills/claude" "$CODEX_DIR/skills/claude"
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$CASE/unrelated/skills/claude" "$timing" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import os, sys
from pathlib import Path
if len(sys.argv) > 3 and sys.argv[1] == '-' and sys.argv[2].endswith('/skills/claude'):
    link = sys.argv[2]
    original_symlink = os.symlink
    def inject():
        if os.path.lexists(link): os.unlink(link)
        original_symlink(UNRELATED, link)
        Path(os.environ['TEST_LOG'] + '.skill-race-fired').write_text('injected')
    if TIMING == 'entry':
        inject()
    else:
        def symlink(source, target, *args, **kwargs):
            result = original_symlink(source, target, *args, **kwargs)
            inject()
            return result
        os.symlink = symlink
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-skill-linker>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as stream:
    stream.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) +
                 '\nUNRELATED = ' + repr(sys.argv[2]) + '\nTIMING = ' + repr(sys.argv[3]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test -f "$LOG.skill-race-fired"
        assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$CASE/unrelated/skills/claude"
        contains "$OUTPUT" 'unrelated Codex skill untouched|skill.*changed|ownership'
        contains "$OUTPUT" 'Codex skill: FAILED'
        assert test "$(find "$CODEX_DIR/skills" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = 1
    done
}

test_skill_final_boundary_conflict() {
    local timing
    for timing in after-verify publish publish-empty; do
        new_case "skill-final-boundary-$timing"
        owned_checkout "$CASE/old-checkout"
        mkdir -p "$CODEX_DIR/skills" "$CASE/unrelated/skills/claude/scripts"
        printf 'unrelated skill contents\n' > "$CASE/unrelated/skills/claude/scripts/claude_mcp_server.py"
        if [ "$timing" != publish-empty ]; then
            ln -s "$CASE/old-checkout/skills/claude" "$CODEX_DIR/skills/claude"
        fi
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$CASE/unrelated/skills/claude" "$timing" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import os, sys
from pathlib import Path
if len(sys.argv) > 3 and sys.argv[1] == '-' and sys.argv[2].endswith('/skills/claude'):
    link = sys.argv[2]
    original_symlink = os.symlink
    injected = [False]
    def inject():
        injected[0] = True
        if os.path.lexists(link): os.unlink(link)
        original_symlink(UNRELATED, link)
        Path(os.environ['TEST_LOG'] + '.skill-race-fired').write_text('injected')
    if TIMING == 'after-verify':
        verified = [0]
        def profile(frame, event, result):
            if (event == 'return' and frame.f_code.co_filename == '<bootstrap-skill-linker>' and
                    frame.f_code.co_name == 'verify_link'):
                verified[0] += 1
                if verified[0] == 2:
                    sys.setprofile(None)
                    inject()
        # Run precisely after final ownership verification returns, before
        # either move-aside or final publication can inspect the destination.
        sys.setprofile(profile)
    else:
        def symlink(source, target, *args, **kwargs):
            if os.fspath(target) == link and not injected[0]:
                # Assert a verified old link was moved aside before publication.
                assert not os.path.lexists(link), 'skill publication did not move old link aside'
                inject()
            return original_symlink(source, target, *args, **kwargs)
        os.symlink = symlink
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-skill-linker>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as stream:
    stream.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) +
                 '\nUNRELATED = ' + repr(sys.argv[2]) + '\nTIMING = ' + repr(sys.argv[3]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test -f "$LOG.skill-race-fired"
        assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$CASE/unrelated/skills/claude"
        contains "$OUTPUT" 'CONFLICT|skill.*changed|ownership'
        contains "$OUTPUT" 'Codex skill: FAILED'
        "$PYTHON" - "$CODEX_DIR/skills" "$CASE" "$timing" "$OUTPUT" <<'PY'
from pathlib import Path
import os, sys
skills, case, timing, output = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).read_text()
assert (case / 'unrelated/skills/claude/scripts/claude_mcp_server.py').read_bytes() == b'unrelated skill contents\n'
staging = list(skills.glob('.claude-link-*'))
if timing == 'publish':
    # A failed final publication must retain the verified old link as a
    # recovery artifact and name it in the conflict report.
    saved = [path for directory in staging for path in directory.iterdir()
             if path.is_symlink() and os.readlink(path) == str(case / 'old-checkout/skills/claude')]
    assert len(saved) == 1, staging
    assert str(saved[0]) in output or str(saved[0].parent) in output, output
elif timing == 'publish-empty':
    assert not staging, staging
else:
    # Any retained moved-aside intruder must remain intact and be reported.
    for directory in staging:
        saved = list(directory.iterdir())
        assert saved and all(path.is_symlink() and os.readlink(path) ==
                             str(case / 'unrelated/skills/claude') for path in saved), saved
        assert str(directory) in output or any(str(path) in output for path in saved), output
PY
    done
}

test_skill_publication_interruption() {
    local interruption
    for interruption in SIGINT SIGTERM abort abort-oserror recovery-conflict recovery-error; do
        new_case "skill-publication-$interruption"
        owned_checkout "$CASE/old-checkout"
        mkdir -p "$CODEX_DIR/skills" "$CASE/unrelated"
        ln -s "$CASE/old-checkout/skills/claude" "$CODEX_DIR/skills/claude"
        rm "$COMMON/python3"
        "$PYTHON" - "$COMMON/python3" "$interruption" "$CASE/unrelated" <<'PY'
import os, sys
interpreter = os.path.realpath(sys.executable)
body = '''import errno, os, signal, sys
from pathlib import Path
if len(sys.argv) > 3 and sys.argv[1] == '-' and sys.argv[2].endswith('/skills/claude'):
    link = sys.argv[2]
    old_target = os.readlink(link)
    original_replace, original_symlink = os.replace, os.symlink
    moved = [False]
    def replace(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        if os.fspath(source) == link and not moved[0]:
            moved[0] = True
            assert not os.path.lexists(link), 'interruption missed move-aside window'
            Path(os.environ['TEST_LOG'] + '.skill-moved').write_text(os.fspath(destination))
            if INTERRUPTION in ('SIGINT', 'SIGTERM'):
                os.kill(os.getpid(), getattr(signal, INTERRUPTION))
                Path(os.environ['TEST_LOG'] + '.skill-signal-deferred').write_text('deferred')
            elif INTERRUPTION == 'abort-oserror':
                raise OSError(errno.EIO, 'injected failure after move-aside')
            else:
                raise KeyboardInterrupt('injected interruption after move-aside')
        return result
    def symlink(source, destination, *args, **kwargs):
        if moved[0] and os.fspath(destination) == link and os.fspath(source) == old_target:
            Path(os.environ['TEST_LOG'] + '.skill-recovery-fired').write_text('recovery')
            if INTERRUPTION == 'recovery-conflict':
                original_symlink(UNRELATED, link)
            elif INTERRUPTION == 'recovery-error':
                raise OSError(errno.EIO, 'injected restoration failure')
        return original_symlink(source, destination, *args, **kwargs)
    os.replace, os.symlink = replace, symlink
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<bootstrap-skill-linker>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(INTERPRETER, [INTERPRETER] + sys.argv[1:])
'''
with open(sys.argv[1], 'w') as stream:
    stream.write('#!' + interpreter + '\nINTERPRETER = ' + repr(interpreter) +
                 '\nINTERRUPTION = ' + repr(sys.argv[2]) + '\nUNRELATED = ' + repr(sys.argv[3]) + '\n' + body)
os.chmod(sys.argv[1], 0o755)
PY
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test -f "$LOG.skill-moved"
        contains "$OUTPUT" 'Codex skill: FAILED'
        "$PYTHON" - "$CODEX_DIR/skills" "$CASE" "$interruption" "$LOG" "$OUTPUT" <<'PY'
from pathlib import Path
import os, sys
skills, case = Path(sys.argv[1]), Path(sys.argv[2])
interruption, log, output = sys.argv[3], sys.argv[4], Path(sys.argv[5]).read_text()
link = skills / 'claude'
old_target = str(case / 'old-checkout/skills/claude')
backup = Path(Path(log + '.skill-moved').read_text())
if interruption == 'recovery-error':
    assert Path(log + '.skill-recovery-fired').is_file(), 'restoration was not attempted'
    assert not os.path.lexists(link), 'injected restoration failure did not fire'
    assert 'injected restoration failure' in output, output
elif interruption == 'recovery-conflict':
    assert Path(log + '.skill-recovery-fired').is_file(), 'restoration was not attempted'
    assert link.is_symlink() and os.readlink(link) == str(case / 'unrelated'), 'restoration clobbered intruder'
else:
    assert link.is_symlink(), 'interruption left the live skill link absent'
    if interruption in ('SIGINT', 'SIGTERM'):
        assert Path(log + '.skill-signal-deferred').is_file(), 'signal was not deferred across move-aside'
        assert os.readlink(link) in (old_target, str(case / 'install/claude-codex-bridge/codex-skills/claude'))
    else:
        assert os.readlink(link) == old_target, 'aborted publication did not restore the old skill'
if interruption in ('recovery-conflict', 'recovery-error'):
    assert backup.is_symlink() and os.readlink(backup) == old_target, 'missing recovery backup'
    assert str(backup) in output, 'recovery failure did not name the backup path: ' + output
else:
    assert not list(skills.glob('.claude-link-*')), 'completed publication/recovery left staging files'
PY
    done
}

test_shim_case_insensitive_ancestry() {
    case_number=0
    for suffix in APP ApP outside-bounce; do
        case_number=$((case_number + 1))
        new_case "shim-ancestry-$case_number-$suffix"
        mkdir -p "$FAKE_HOME/.local"
        if [ "$suffix" = outside-bounce ]; then
            parent="$FAKE_HOME/safe-bin"
            mkdir -p "$parent" "$CASE/outside"
            ln -s "$parent" "$CASE/outside/bounce"
            ln -s "$CASE/outside/bounce" "$FAKE_HOME/.local/bin"
        else
            parent="$FAKE_HOME/Example.$suffix/bin"
            mkdir -p "$parent"
            ln -s "$parent" "$FAKE_HOME/.local/bin"
        fi
        printf 'preserve executable bytes\n' > "$parent/codex"
        cp "$parent/codex" "$CASE/original"
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert cmp "$CASE/original" "$parent/codex"
        assert test "$(find "$parent" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = 1
        contains "$OUTPUT" 'Refusing shim|Refusing.*shim'
    done
}

test_claude_auth_response_uses_stdin() {
    new_case claude-auth-response-stdin
    AUTH_TOKEN=""
    observe_python
    run_bootstrap; success
    assert_calls auth-json
    "$PYTHON" - "$LOG.python" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert calls and not any(call['json_in_argv'] for call in calls), calls
PY
    contains "$OUTPUT" 'Claude auth: signed in'
    not_contains "$OUTPUT" 'private-auth-response'
}

test_check_does_not_execute_writing_clis() {
    new_case check-writing-clis
    AUTH_TOKEN=""
    run_bootstrap; success
    mkdir -p "$CODEX_DIR" "$CLAUDE_DIR"
    printf '{"tokens":{"access_token":"fixture-private-codex-token"}}\n' > "$CODEX_DIR/auth.json"
    printf '{"claudeAiOauth":{"accessToken":"fixture-private-claude-token"}}\n' > "$CLAUDE_DIR/.credentials.json"
    CACHE_ON_INVOKE=1; PLUGIN_ENABLED=1
    : > "$PLUGIN/../link.sh"
    : > "$LOG"
    snapshot "$CASE/before"
    run_bootstrap --check; success
    snapshot "$CASE/after"
    same_snapshot "$CASE/before" "$CASE/after"
    contains "$OUTPUT" 'live.*probe.*skipped|skipp.*live.*probe'
    contains "$OUTPUT" 'Codex credentials present'
    contains "$OUTPUT" 'Claude credentials present'
    not_contains "$OUTPUT" 'fixture-private'
    "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert not [call for call in calls if call['tool'] in ('codex', 'claude', 'npm')], calls
PY
    assert test ! -e "$FAKE_HOME/.fixture-cli-cache-codex"
    assert test ! -e "$FAKE_HOME/.fixture-cli-cache-claude"
}

test_check_local_credentials_unknown() {
    for contents in absent malformed empty keychain-marker; do
        new_case "check-credentials-$contents"
        AUTH_TOKEN=""; CACHE_ON_INVOKE=1
        mkdir -p "$CODEX_DIR" "$CLAUDE_DIR"
        printf 'model = "unrelated-model"\n' > "$CODEX_DIR/config.toml"
        if [ "$contents" = keychain-marker ]; then
            printf '{"oauthAccount":{"accountUuid":"fixture-account"}}\n' > "$FAKE_HOME/.claude.json"
        elif [ "$contents" != absent ]; then
            credential='{}'
            if [ "$contents" = malformed ]; then credential='{bad json'; fi
            printf '%s\n' "$credential" > "$CODEX_DIR/auth.json"
            printf '%s\n' "$credential" > "$CLAUDE_DIR/.credentials.json"
        fi
        snapshot "$CASE/before"
        run_bootstrap --check
        contains "$OUTPUT" 'unknown in check mode; run without --check to probe'
        contains "$OUTPUT" 'Codex sign-in.*unknown in check mode'
        contains "$OUTPUT" 'Claude sign-in.*unknown in check mode'
        contains "$OUTPUT" 'live.*probe.*skipped|skipp.*live.*probe'
        snapshot "$CASE/after"
        same_snapshot "$CASE/before" "$CASE/after"
        assert test ! -e "$FAKE_HOME/.fixture-cli-cache-codex"
        assert test ! -e "$FAKE_HOME/.fixture-cli-cache-claude"
    done
}

test_bridge_env_forwarding() {
    local required_env omitted
    new_case bridge-env-forwarding
    # changed for DESIGN §7: BRIDGE_MODELS_GATE is forwarded too (and its absence alone triggers a repair, below)
    required_env='CLAUDE_CONFIG_DIR CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY CODEX_HOME CODEX_BIN CLAUDE_BIN BRIDGE_JOBS BRIDGE_DEFAULT_SEAT BRIDGE_REVIEW_SEATS BRIDGE_COUNCIL_SEATS CLAUDE_BRIDGE_ALLOW_WRITE CLAUDE_BRIDGE_ROOT CLAUDE_BRIDGE_ALLOW_ANY_CWD CLAUDE_BRIDGE_TOOL_DEADLINE CLAUDE_BRIDGE_ENGINE BRIDGE_MODELS_GATE'
    run_bootstrap; success
    "$PYTHON" - "$CODEX_DIR/config.toml" "$required_env" <<'PY'
from pathlib import Path
import ast, re, sys
text = Path(sys.argv[1]).read_text()
env = ast.literal_eval(re.search(r'(?m)^env_vars = (.*)$', text)[1])
assert set(env) == set(sys.argv[2].split()), env
assert len(env) == len(set(env)), env
PY
    # Each missing setting alone must trigger repair of an otherwise current
    # owned registration, preserving old entries and the user's custom entry.
    for omitted in CLAUDE_BRIDGE_ROOT CLAUDE_BRIDGE_ALLOW_ANY_CWD CLAUDE_BRIDGE_TOOL_DEADLINE \
        CLAUDE_BRIDGE_ALLOW_WRITE CLAUDE_BRIDGE_ENGINE BRIDGE_MODELS_GATE; do
        "$PYTHON" - "$CODEX_DIR/config.toml" "$CASE/original.toml" "$omitted" <<'PY'
from pathlib import Path
import ast, json, re, sys
path = Path(sys.argv[1]); text = path.read_text()
match = re.search(r'(?m)^env_vars = (.*)$', text)
env = ast.literal_eval(match[1])
assert sys.argv[3] in env, env
env.remove(sys.argv[3])
if 'CUSTOM_ACCOUNT' not in env:
    env.append('CUSTOM_ACCOUNT')
path.write_text(text[:match.start(1)] + json.dumps(env) + text[match.end(1):])
Path(sys.argv[2]).write_bytes(path.read_bytes())
PY
        : > "$LOG"
        run_bootstrap; success
        assert_calls reconcile
        "$PYTHON" - "$CODEX_DIR" "$CASE/original.toml" "$required_env" <<'PY'
from pathlib import Path
import ast, re, sys
home = Path(sys.argv[1]); text = (home / 'config.toml').read_text()
env = ast.literal_eval(re.search(r'(?m)^env_vars = (.*)$', text)[1])
assert set(env) == set(sys.argv[3].split()) | {'CUSTOM_ACCOUNT'}, env
assert len(env) == len(set(env)), env
original = Path(sys.argv[2]).read_bytes()
assert any(backup.read_bytes() == original for backup in home.glob('config.toml.bak-*')), 'missing pre-edit backup'
PY
    done
}

assert_standalone_links() {
    "$PYTHON" - "$CLAUDE_DIR" "$CODEX_DIR" "$PLUGIN" <<'PY'
from pathlib import Path
import os, stat, sys
claude, codex, plugin = map(Path, sys.argv[1:])
links = [(claude / 'skills/codex', plugin / 'skills/codex'),
         (claude / 'skills/gpt', plugin / 'skills/gpt'),
         (claude / 'agents/opus-reviewer.md', plugin / 'agents/opus-reviewer.md'),
         (claude / 'agents/opus-consultant.md', plugin / 'agents/opus-consultant.md'),
         (codex / 'skills/claude', plugin / 'codex-skills/claude')]
for link, target in links:
    assert link.is_symlink() and link.resolve() == target.resolve(), (link, target)
for directory in (claude / 'skills', claude / 'agents'):
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory
PY
}

test_standalone_install() {
    new_case standalone-install
    rm "$COMMON/git"
    PLUGIN_ENABLED=1
    assert test ! -d "$PLUGIN/.git"
    assert test ! -e "$PLUGIN/../link.sh"
    assert test ! -e "$CLAUDE_DIR"
    assert test ! -e "$CODEX_DIR"
    snapshot "$CASE/plugin-before" "$PLUGIN"
    run_bootstrap; success
    assert_standalone_links
    assert test -x "$FAKE_HOME/.local/bin/codex"
    contains "$OUTPUT" 'Claude links \(standalone\): ok'
    contains "$CODEX_DIR/config.toml" "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    contains "$CODEX_DIR/config.toml" '\[mcp_servers.claude-bridge.tools.claude_task\]'
    snapshot "$CASE/plugin-after" "$PLUGIN"
    same_snapshot "$CASE/plugin-before" "$CASE/plugin-after"
    "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert not [call for call in calls if call['tool'] == 'git' or
            (call['tool'] == 'claude' and call['args'][:1] == ['plugin'])], calls
PY
    # Explicit selection overrides the repository marker; suppression wins
    # even when standalone is requested, while Codex installation still runs.
    new_case standalone-explicit
    printf '#!/bin/sh\nexit 99\n' > "$PLUGIN/../link.sh"
    run_bootstrap --standalone; success
    assert_standalone_links
    contains "$OUTPUT" 'Claude links \(standalone\): ok'
    new_case standalone-explicit-marketplace
    mkdir -p "$CLAUDE_DIR/plugins/cache"
    mv "$PLUGIN" "$CLAUDE_DIR/plugins/cache/claude-codex-bridge"
    PLUGIN="$CLAUDE_DIR/plugins/cache/claude-codex-bridge"
    run_bootstrap --standalone; success
    assert_standalone_links
    new_case standalone-suppressed
    run_bootstrap --standalone --no-claude-links; success
    assert test ! -e "$CLAUDE_DIR/skills"
    assert test ! -e "$CLAUDE_DIR/agents"
    assert test -L "$CODEX_DIR/skills/claude"
    assert test -f "$CODEX_DIR/config.toml"
}

test_standalone_idempotent() {
    new_case standalone-idempotent
    run_bootstrap; success
    assert_standalone_links
    snapshot "$CASE/before"
    snapshot "$CASE/plugin-before" "$PLUGIN"
    : > "$LOG"
    run_bootstrap; success
    assert_standalone_links
    snapshot "$CASE/after"
    snapshot "$CASE/plugin-after" "$PLUGIN"
    same_snapshot "$CASE/before" "$CASE/after"
    same_snapshot "$CASE/plugin-before" "$CASE/plugin-after"
    assert_calls no-mutations
    contains "$OUTPUT" 'Claude links \(standalone\): ok'
}

test_standalone_upgrade() {
    new_case standalone-upgrade
    new_plugin="$PLUGIN"
    old_plugin="$CASE/old permanent folder/bridge"
    mkdir -p "$(dirname "$old_plugin")"
    cp -R "$PLUGIN" "$old_plugin"
    PLUGIN="$old_plugin"
    run_bootstrap; success
    assert_standalone_links
    cp "$CODEX_DIR/config.toml" "$CASE/old-config.toml"
    # Ownership follows resolved targets, including relative file links.
    "$PYTHON" - "$CLAUDE_DIR" <<'PY'
from pathlib import Path
import os, sys
for link in (Path(sys.argv[1]) / 'agents').iterdir():
    target = link.resolve()
    link.unlink()
    link.symlink_to(os.path.relpath(target, link.parent))
PY
    snapshot "$CASE/old-before" "$old_plugin"
    PLUGIN="$new_plugin"
    : > "$LOG"
    run_bootstrap; success
    assert_standalone_links
    assert_calls reconcile
    snapshot "$CASE/old-after" "$old_plugin"
    same_snapshot "$CASE/old-before" "$CASE/old-after"
    "$PYTHON" - "$CODEX_DIR" "$CASE/old-config.toml" "$PLUGIN" <<'PY'
from pathlib import Path
import sys
codex, old, plugin = map(Path, sys.argv[1:])
assert str(plugin / 'codex-skills/claude/scripts/claude_mcp_server.py') in (codex / 'config.toml').read_text()
assert any(path.read_bytes() == old.read_bytes() for path in codex.glob('config.toml.bak-*'))
PY
}

test_standalone_unrelated_links() {
    new_case standalone-unrelated-links
    mkdir -p "$CLAUDE_DIR/skills/codex" "$CLAUDE_DIR/agents" "$CASE/unrelated"
    printf 'keep this real directory\n' > "$CLAUDE_DIR/skills/codex/README"
    printf 'keep this real file\n' > "$CLAUDE_DIR/skills/gpt"
    ln -s "$CASE/unrelated/reviewer.md" "$CLAUDE_DIR/agents/opus-reviewer.md"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert test ! -L "$CLAUDE_DIR/skills/codex"
    assert test ! -L "$CLAUDE_DIR/skills/gpt"
    assert test "$(cat "$CLAUDE_DIR/skills/codex/README")" = 'keep this real directory'
    assert test "$(cat "$CLAUDE_DIR/skills/gpt")" = 'keep this real file'
    assert test "$(readlink "$CLAUDE_DIR/agents/opus-reviewer.md")" = "$CASE/unrelated/reviewer.md"
    assert test "$(readlink "$CLAUDE_DIR/agents/opus-consultant.md")" = "$PLUGIN/agents/opus-consultant.md"
    assert test -L "$CODEX_DIR/skills/claude"
    assert test -f "$CODEX_DIR/config.toml"
    contains "$OUTPUT" 'Claude links \(standalone\): FAILED'
    for entry in skills/codex skills/gpt agents/opus-reviewer.md; do
        assert grep -Fq "$CLAUDE_DIR/$entry" "$OUTPUT"
    done
    contains "$OUTPUT" 'unrelated|untouched|preserv'
    # Both destination directories enforce ancestry, including a path which
    # leaves HOME through a symlink and returns to an otherwise safe target.
    for destination in skills-outside agents-app skills-bounce; do
        new_case "standalone-unsafe-$destination"
        mkdir -p "$CLAUDE_DIR" "$CASE/outside" "$FAKE_HOME/Forbidden.ApP/data" "$FAKE_HOME/safe"
        case "$destination" in
            skills-outside) watched="$CASE/outside"; ln -s "$watched" "$CLAUDE_DIR/skills" ;;
            agents-app) watched="$FAKE_HOME/Forbidden.ApP/data"; ln -s "$watched" "$CLAUDE_DIR/agents" ;;
            skills-bounce) watched="$FAKE_HOME/safe"; ln -s "$watched" "$CASE/outside/bounce"; ln -s "$CASE/outside/bounce" "$CLAUDE_DIR/skills" ;;
        esac
        run_bootstrap
        assert test "$LAST_STATUS" -ne 0
        assert test "$(find "$watched" -mindepth 1 | wc -l | tr -d ' ')" = 0
        contains "$OUTPUT" 'Claude links \(standalone\): FAILED'
        contains "$OUTPUT" 'Refusing|outside HOME|\.app bundle'
        assert test -f "$CODEX_DIR/config.toml"
        if [ "$destination" = agents-app ]; then
            assert test "$(readlink "$CLAUDE_DIR/skills/codex")" = "$PLUGIN/skills/codex"
            assert test "$(readlink "$CLAUDE_DIR/skills/gpt")" = "$PLUGIN/skills/gpt"
        else
            assert test "$(readlink "$CLAUDE_DIR/agents/opus-reviewer.md")" = "$PLUGIN/agents/opus-reviewer.md"
            assert test "$(readlink "$CLAUDE_DIR/agents/opus-consultant.md")" = "$PLUGIN/agents/opus-consultant.md"
        fi
    done
}

test_standalone_check_read_only() {
    for state in empty installed; do
        new_case "standalone-check-$state"
        if [ "$state" = installed ]; then run_bootstrap; success; fi
        : > "$LOG"
        snapshot "$CASE/before"
        snapshot "$CASE/plugin-before" "$PLUGIN"
        run_bootstrap --check
        assert test "$LAST_STATUS" -le 1
        contains "$OUTPUT" 'Claude links \(standalone\)'
        if [ "$state" = empty ]; then
            for entry in skills/codex skills/gpt agents/opus-reviewer.md agents/opus-consultant.md; do
                assert grep -Fq "$CLAUDE_DIR/$entry" "$OUTPUT"
            done
            contains "$OUTPUT" 'Would|would'
        fi
        snapshot "$CASE/after"
        snapshot "$CASE/plugin-after" "$PLUGIN"
        same_snapshot "$CASE/before" "$CASE/after"
        same_snapshot "$CASE/plugin-before" "$CASE/plugin-after"
        "$PYTHON" - "$LOG" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
assert not [call for call in calls if call['tool'] in ('codex', 'claude', 'npm', 'git')], calls
PY
    done
}

test_uninstall_owned_items() {
    new_case uninstall-owned-items
    run_bootstrap; success
    assert_standalone_links
    # Preserve unrelated tables verbatim while removing every bridge sub-table.
    "$PYTHON" - "$CODEX_DIR/config.toml" <<'PY'
from pathlib import Path
import sys
config = Path(sys.argv[1])
config.write_text('description = """\n[mcp_servers.claude-bridge]\n'
                  'this is documentation, not a registration\n"""\n\n' + config.read_text())
PY
    cat >> "$CODEX_DIR/config.toml" <<'TOML'

[mcp_servers.claude-bridge.tools.extra]
approval_mode = "prompt"

[mcp_servers.other]
command = "unrelated-service"
args = ["keep me"]

[mcp_servers.claude-bridge-extra]
command = "similarly-named-service"
TOML
    cp "$CODEX_DIR/config.toml" "$CASE/pre-uninstall.toml"
    snapshot "$CASE/plugin-before" "$PLUGIN"
    # Uninstall must work without either CLI, npm, git, file, or credentials.
    rm "$APP" "$COMMON/claude" "$COMMON/npm" "$COMMON/git" "$COMMON/file"
    SIGNED_OUT=1; AUTH_TOKEN=""; EXPLICIT_CODEX_SET=1; EXPLICIT_CODEX="$CASE/missing-codex"
    : > "$LOG"
    run_bootstrap --uninstall; success
    assert test ! -L "$CODEX_DIR/skills/claude"
    assert test ! -e "$FAKE_HOME/.local/bin/codex"
    for entry in skills/codex skills/gpt agents/opus-reviewer.md agents/opus-consultant.md; do
        assert test ! -L "$CLAUDE_DIR/$entry"
        assert grep -Fq "$CLAUDE_DIR/$entry" "$OUTPUT"
    done
    contains "$OUTPUT" 'removed|Removed'
    "$PYTHON" - "$CODEX_DIR" "$CASE/pre-uninstall.toml" "$LOG" <<'PY'
from pathlib import Path
import fcntl, json, re, stat, sys
home, original = Path(sys.argv[1]), Path(sys.argv[2])
text = (home / 'config.toml').read_text()
description = original.read_text().split('"""\n\n', 1)[0] + '"""\n\n'
assert text.startswith(description), text
assert not re.search(r'(?m)^\[mcp_servers\.claude-bridge(?:\]|\.)', text[len(description):]), text
unrelated = original.read_text().split('[mcp_servers.other]', 1)[1]
assert text.endswith('[mcp_servers.other]' + unrelated), text
assert any(path.read_bytes() == original.read_bytes() for path in home.glob('config.toml.bak-*'))
assert stat.S_IMODE((home / 'config.toml').stat().st_mode) == stat.S_IMODE(original.stat().st_mode)
with open(home / 'config.toml.lock', 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
assert not [json.loads(line) for line in open(sys.argv[3])], 'uninstall invoked a CLI'
PY
    snapshot "$CASE/plugin-after" "$PLUGIN"
    same_snapshot "$CASE/plugin-before" "$CASE/plugin-after"
    snapshot "$CASE/removed-before"
    run_bootstrap --uninstall; success
    snapshot "$CASE/removed-after"
    same_snapshot "$CASE/removed-before" "$CASE/removed-after"

    # A different, structurally owned installation is still not THIS one.
    # Its link and MCP entry are left exactly as they were, just like user files.
    for other in owned unrelated; do
        new_case "uninstall-preserves-$other"
        run_bootstrap; success
        other_root="$CASE/another-install"
        owned_checkout "$other_root"
        rm "$CLAUDE_DIR/skills/codex" "$CLAUDE_DIR/agents/opus-reviewer.md" "$CODEX_DIR/skills/claude" "$FAKE_HOME/.local/bin/codex"
        if [ "$other" = unrelated ]; then
            printf 'keep user skill\n' > "$CLAUDE_DIR/skills/codex"
            mkdir "$CLAUDE_DIR/agents/opus-reviewer.md"
            printf 'keep user agent\n' > "$CLAUDE_DIR/agents/opus-reviewer.md/content"
            other_root="$CASE/unrelated"
        else
            ln -s "$other_root/skills/codex" "$CLAUDE_DIR/skills/codex"
            ln -s "$other_root/agents/opus-reviewer.md" "$CLAUDE_DIR/agents/opus-reviewer.md"
        fi
        ln -s "$other_root/codex-skills/claude" "$CODEX_DIR/skills/claude"
        seed_registration "$other_root/codex-skills/claude/scripts/claude_mcp_server.py"
        printf '#!/bin/sh\n# user-maintained codex launcher\nexit 0\n' > "$FAKE_HOME/.local/bin/codex"
        cp "$FAKE_HOME/.local/bin/codex" "$CASE/user-shim"
        cp "$CODEX_DIR/config.toml" "$CASE/user-config"
        snapshot "$CASE/other-before" "$CASE/another-install"
        : > "$LOG"
        run_bootstrap --uninstall; success
        assert test -e "$CLAUDE_DIR/skills/codex"
        assert test -e "$CLAUDE_DIR/agents/opus-reviewer.md"
        assert test "$(readlink "$CODEX_DIR/skills/claude")" = "$other_root/codex-skills/claude"
        assert test ! -L "$CLAUDE_DIR/skills/gpt"
        assert test ! -L "$CLAUDE_DIR/agents/opus-consultant.md"
        assert cmp "$CASE/user-shim" "$FAKE_HOME/.local/bin/codex"
        assert cmp "$CASE/user-config" "$CODEX_DIR/config.toml"
        snapshot "$CASE/other-after" "$CASE/another-install"
        same_snapshot "$CASE/other-before" "$CASE/other-after"
        contains "$OUTPUT" 'left|Left|unrelated|untouched'
        assert test ! -s "$LOG"
    done
}

test_uninstall_check_read_only() {
    for state in empty installed; do
        new_case "uninstall-check-$state"
        if [ "$state" = installed ]; then run_bootstrap; success; fi
        rm "$APP" "$COMMON/claude" "$COMMON/npm" "$COMMON/git" "$COMMON/file"
        : > "$LOG"
        snapshot "$CASE/before"
        snapshot "$CASE/plugin-before" "$PLUGIN"
        run_bootstrap --uninstall --check; success
        if [ "$state" = installed ]; then
            contains "$OUTPUT" 'would remove'
            for entry in skills/codex skills/gpt agents/opus-reviewer.md agents/opus-consultant.md; do
                assert grep -Fq "$CLAUDE_DIR/$entry" "$OUTPUT"
            done
            contains "$OUTPUT" 'would remove MCP registration'
        fi
        snapshot "$CASE/after"
        snapshot "$CASE/plugin-after" "$PLUGIN"
        same_snapshot "$CASE/before" "$CASE/after"
        same_snapshot "$CASE/plugin-before" "$CASE/plugin-after"
        assert test ! -s "$LOG"
    done
}

# ==================================================================================================================
# DESIGN §8 group D (and §7 package): floors, discovery and the package. They exercise bootstrap.sh, install.sh and
# package.sh, which DESIGN §9 changes in commit 4.
# ==================================================================================================================
summary_value() {
    # The value of one `  <step>: <value>` line of the final Summary block.
    "$PYTHON" - "$OUTPUT" "$1" <<'PY'
import sys
text = open(sys.argv[1]).read()
parts = text.rsplit("\nSummary:\n", 1)
lines = parts[1].splitlines() if len(parts) == 2 else []
for line in lines:
    if line.startswith("  " + sys.argv[2] + ": "):
        print(line.split(": ", 1)[1])
        break
else:
    sys.exit("no summary step %r in:\n%s" % (sys.argv[2], "\n".join(lines)))
PY
}

json_versions() {
    # TEST_CODEX_VERSIONS: {realpath: version} for the fixture executables named as path/version pairs
    "$PYTHON" -c 'import json,os,sys; a=sys.argv[1:]; print(json.dumps({os.path.realpath(a[i]): a[i+1] for i in range(0, len(a), 2)}))' "$@"
}

realpath_of() { "$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

shim_target_is() {
    "$PYTHON" - "$FAKE_HOME/.local/bin/codex" "$1" <<'PY'
import os, shlex, sys
text = open(sys.argv[1]).read()
assert 'codex shim (claude-codex-bridge' in text, text
target = shlex.split(text.strip().splitlines()[-1])[1]
assert os.path.realpath(target) == os.path.realpath(sys.argv[2]), (target, sys.argv[2])
PY
}

step_failed() { case "$(summary_value "$1")" in FAILED*) return 0 ;; *) return 1 ;; esac; }

test_groupD_versions_in_summary() {
    local codex_step claude_step
    new_case groupD-versions
    run_bootstrap; success
    codex_step="$(summary_value 'Codex version')" || fail "no Codex version step"
    claude_step="$(summary_value 'Claude Code version')" || fail "no Claude Code version step"
    case "$codex_step" in *FAILED*) fail "Codex version: $codex_step" ;; *0.156.1*) ;; *) fail "Codex version step lacks the version: $codex_step" ;; esac
    case "$claude_step" in *FAILED*) fail "Claude Code version: $claude_step" ;; *2.1.295*) ;; *) fail "Claude Code version step lacks the version: $claude_step" ;; esac   # the stub's default since P-16
}

test_groupD_shim_targets_the_chosen_codex() {
    new_case groupD-shim-target
    run_bootstrap; success                                          # only the app launcher: the owned shim runs it
    shim_target_is "$APP"
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3)"     # the app launcher falls below the Sol/Luna floor ...
    ln -s "$NPM_TARGET" "$BIN/codex"                                # ... and a standalone that meets it is on PATH
    run_bootstrap; success
    shim_target_is "$NPM_TARGET"                                    # the owned shim now runs the executable bootstrap chose
}

test_groupD_fresh_process_resolves_the_same_codex() {
    local engine chosen
    new_case groupD-fresh-process
    make_native "$BIN/codex"
    CODEX_VERSIONS="$(json_versions "$BIN/codex" 0.154.0)"          # first on PATH, capable, below the floor
    run_bootstrap; success
    shim_target_is "$APP"                                           # bootstrap chose the first candidate meeting the floor
    # a fresh process (a new terminal: no CODEX_BIN) resolves codex the way bridge.py does and gets the same executable
    engine="$CASE/engine"; mkdir -p "$engine"
    cp "$PLUGIN_SOURCE"/skills/codex/scripts/*.py "$engine/"
    cp "$MODELS_TABLE" "$CASE/models.json"
    chosen="$(cd "$CASE" && env -i HOME="$FAKE_HOME" PATH="$CALLER_PATH" TMPDIR="$CASE/tmp" BRIDGE_APP_LAUNCHER="$APP" \
        BRIDGE_MODELS_FILE="$CASE/models.json" BRIDGE_CODEX_FIXED_ROOT="$FIXED_ROOT" \
        TEST_LOG="$LOG" TEST_NATIVE_LIST="$NATIVE_LIST" TEST_CODEX_VERSIONS="$CODEX_VERSIONS" \
        "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); import bridge; print(bridge.resolve_codex() or "")' "$engine")"
    assert test -n "$chosen"
    assert test "$(realpath_of "$chosen")" = "$(realpath_of "$APP")"
}

host_guard() {
    # TEST_RULINGS F-E6: no codex or claude outside this case may start during its runs. bootstrap starts executables
    # through its Python helper (`bounded`), which loads the guard (skills/codex/tests/fixtures/host_guard) as
    # sitecustomize; a refused start fails like a missing executable and is logged in the case.
    EXTRA_ENV+=("PYTHONPATH=$PLUGIN_SOURCE/skills/codex/tests/fixtures/host_guard" "HOST_GUARD_ROOTS=$CASE"
                "HOST_GUARD_LOG=$CASE/host-guard.jsonl")
}

assert_host_guard_clean() {
    if [ -s "$CASE/host-guard.jsonl" ]; then
        cat "$CASE/host-guard.jsonl"; fail "a codex outside the case was about to start (BRIDGE_CODEX_FIXED_ROOT not applied)"
    fi
}

probe_order_is() {
    # bootstrap's first `--version` probe of each named executable happened in exactly this order (other probes ignored)
    "$PYTHON" - "$LOG" "$@" <<'PY'
import json, os, sys
wanted = [os.path.realpath(p) for p in sys.argv[2:]]
order = []
for call in (json.loads(line) for line in open(sys.argv[1])):
    if call["tool"] == "codex" and call["args"] == ["--version"] and call["path"] in wanted and call["path"] not in order:
        order.append(call["path"])
assert order == wanted, (order, wanted)
PY
}

test_groupD_default_fixed_dirs_beneath_the_fixed_root() {
    # TEST_RULINGS F-E6 (revised): bootstrap applies BRIDGE_CODEX_FIXED_ROOT as a path prefix to DESIGN §3's two default
    # fixed directories and considers them after the app launcher, /opt/homebrew/bin first; the harness points it at an
    # empty directory in each case, and here stubs sit beneath it. The host guard keeps a bootstrap that ignored the root
    # from starting the host's codex.
    local homebrew usr_local decoy decoys
    new_case groupD-default-fixed-dirs
    homebrew="$FIXED_ROOT/opt/homebrew/bin/codex"; usr_local="$FIXED_ROOT/usr/local/bin/codex"
    mkdir -p "$(dirname "$homebrew")" "$(dirname "$usr_local")"
    make_native "$homebrew"
    make_native "$usr_local"
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3 "$homebrew" 0.155.0 "$usr_local" 0.156.1)"
    host_guard
    run_bootstrap; success
    assert_host_guard_clean
    probe_order_is "$APP" "$homebrew" "$usr_local"                  # the app launcher, then the root's directories in order
    assert_calls npm 0
    shim_target_is "$usr_local"                                     # the first candidate meeting the floor
    # both default directories meet the floor: /opt/homebrew/bin wins
    new_case groupD-default-fixed-dirs-first
    homebrew="$FIXED_ROOT/opt/homebrew/bin/codex"; usr_local="$FIXED_ROOT/usr/local/bin/codex"
    mkdir -p "$(dirname "$homebrew")" "$(dirname "$usr_local")"
    make_native "$homebrew"
    make_native "$usr_local"
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3)"     # both stubs print the default 0.156.1
    host_guard
    run_bootstrap; success
    assert_host_guard_clean
    assert_calls npm 0
    shim_target_is "$homebrew"
    # the root prefixes the two default directories and is not searched itself: nothing meets the floor, so npm runs
    new_case groupD-default-fixed-dirs-prefix
    decoys=("$FIXED_ROOT/codex" "$FIXED_ROOT/bin/codex" "$FIXED_ROOT/opt/homebrew/codex" "$FIXED_ROOT/usr/local/codex")
    for decoy in "${decoys[@]}"; do mkdir -p "$(dirname "$decoy")"; make_native "$decoy"; done
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3)"
    host_guard
    run_bootstrap; success
    assert_host_guard_clean
    for decoy in "${decoys[@]}"; do assert_calls unprobed "$decoy"; done
    assert_calls npm 1
}

test_groupD_npm_widening_when_no_codex_meets_the_floor() {
    local step
    new_case groupD-npm-widening
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3)"     # the only codex is the app's, below the floor
    run_bootstrap; success
    assert_calls npm 1                                              # the npm branch now also runs when none meets the floor
    step="$(summary_value 'Codex version')"
    case "$step" in *FAILED*) fail "Codex version: $step" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
    new_case groupD-npm-widening-still-old
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3 "$NPM_TARGET" 0.155.0)"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert_calls npm 1
    step_failed 'Codex version' || fail "Codex version: $(summary_value 'Codex version')"
    contains "$OUTPUT" 'npm install -g @openai/codex@latest|CODEX_BIN'
}

test_groupD_explicit_binaries_and_old_claude_code_fail_their_steps() {
    new_case groupD-explicit-below-floor
    make_native "$CASE/explicit-codex"
    EXPLICIT_CODEX="$CASE/explicit-codex"
    CODEX_VERSIONS="$(json_versions "$CASE/explicit-codex" 0.155.0-alpha.16.3)"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    assert_calls npm 0                                              # an explicit CODEX_BIN is respected, never replaced
    assert_calls native "$CASE/explicit-codex"
    step_failed 'Codex version' || fail "Codex version: $(summary_value 'Codex version')"
    contains "$OUTPUT" 'CODEX_BIN'
    contains "$OUTPUT" '0\.156\.1'
    new_case groupD-old-claude-code
    CLAUDE_VERSION="2.1.279 (Claude Code)"                         # before 2.1.280 `opus` meant the previous Opus
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    step_failed 'Claude Code version' || fail "Claude Code version: $(summary_value 'Claude Code version')"
    contains "$OUTPUT" 'claude update'
    new_case groupD-claude-override-warning
    EXTRA_ENV=(ANTHROPIC_DEFAULT_OPUS_MODEL=OPUS-OVERRIDE-VALUE-4411)
    run_bootstrap; success                                          # an alias override only warns ...
    contains "$OUTPUT" 'ANTHROPIC_DEFAULT_OPUS_MODEL'
    not_contains "$OUTPUT" 'OVERRIDE-VALUE-4411'                    # ... by name; its value is never printed
}

test_groupD_missing_models_json() {
    new_case groupD-missing-models-json
    rm "$PLUGIN/skills/codex/models.json"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    contains "$OUTPUT" 'models\.json'
    contains "$OUTPUT" 'FAILED'
    not_contains "$OUTPUT" 'Traceback'
}

test_groupD_models_gate_forwarded() {
    new_case groupD-models-gate
    run_bootstrap; success
    "$PYTHON" - "$CODEX_DIR/config.toml" <<'PY'
import ast, re, sys
text = open(sys.argv[1]).read()
env = ast.literal_eval(re.search(r'(?m)^env_vars = (.*)$', text)[1])
assert 'BRIDGE_MODELS_GATE' in env, env
PY
}

test_groupD_below_floor_executable_at_the_shim_fails_its_step() {
    # Astra C4-1 (review/astra_C4_findings.md): ~/.local/bin/codex is a user's standalone Codex below the table's floor and the
    # app launcher meets it, so bootstrap chooses the launcher. The user's file is kept (never replaced or backed up), but typing
    # `codex` still runs it, so the 'PATH shim' step must FAIL with the instruction to upgrade it or to put the chosen codex
    # first on PATH, never report `ok`. The same for every way write_shim is reached: ~/.local/bin on the caller's PATH, off it,
    # and an explicit CODEX_BIN naming the launcher (test_healthy_executable_at_shim covers a kept executable that meets the floor).
    local location shim before step
    for location in on-path off-path explicit-app; do
        new_case "groupD-below-floor-at-shim-$location"
        if [ "$location" = off-path ]; then CALLER_PATH="$BIN:$COMMON"; fi
        if [ "$location" = explicit-app ]; then EXPLICIT_CODEX="$APP"; fi
        shim="$FAKE_HOME/.local/bin/codex"
        mkdir -p "$(dirname "$shim")"
        make_native "$shim"
        CODEX_VERSIONS="$(json_versions "$shim" 0.154.0)"               # capable, below the floor; the launcher prints 0.156.1
        before="$(hash_file "$shim")"
        host_guard
        run_bootstrap
        assert_host_guard_clean
        assert test "$before" = "$(hash_file "$shim")"                    # the user's executable is left in place ...
        assert test "$(find "$FAKE_HOME/.local/bin" -type f | wc -l | tr -d ' ')" = 1   # ... neither replaced nor backed up
        assert_calls npm 0
        "$PYTHON" - "$OUTPUT" "$(realpath_of "$APP")" <<'PY'
import sys
text = open(sys.argv[1]).read()
assert "Chose %s (" % sys.argv[2] in text, "bootstrap did not choose the app launcher:\n" + text
PY
        step="$(summary_value 'PATH shim')" || fail "no PATH shim step"
        case "$step" in
            FAILED*) ;;
            *) fail "PATH shim: $step ($location), although the kept $shim is Codex 0.154.0, below the floor 0.156.1, and typing codex runs it" ;;
        esac
        assert test "$LAST_STATUS" -ne 0
        # the instruction: upgrade the kept executable, or put the chosen codex (its directory) first on PATH
        "$PYTHON" - "$OUTPUT" "$(dirname "$(realpath_of "$APP")")" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
assert any("upgrade" in line.lower() for line in lines), "no instruction to upgrade the kept executable:\n" + "\n".join(lines)
assert any("PATH" in line and sys.argv[2] in line for line in lines), \
    "no instruction to put %s first on PATH:\n%s" % (sys.argv[2], "\n".join(lines))
PY
    done
}

# L4 (review/rulings_final_round.md): every Codex upgrade hint is chosen by one helper from the real path of the executable it
# is about: under /node_modules/@openai/codex/ `npm install -g @openai/codex@latest`; under a Homebrew prefix (/opt/homebrew/,
# /usr/local/Cellar/, a Caskroom) `brew upgrade codex`; inside ChatGPT.app "update the ChatGPT app"; under
# <CODEX_HOME>/packages/standalone/ (OpenAI's installer, linked from ~/.local/bin/codex) or anywhere else
# `curl -fsSL https://chatgpt.com/codex/install.sh | sh`. One scenario per branch for bootstrap's messages (the Codex version
# step, the PATH shim step); the engine's floor refusal and discovery finding: skills/codex/tests/test_bridge.py GroupD_UpgradeHints
upgrade_hints_are() {
    # Every output line naming a Codex upgrade hint (or saying "upgrade") names installer $1's hint (npm | brew | app | curl)
    # and no other's; at least one does, and so does the Summary step $2 (default 'Codex version'; '' skips it)
    "$PYTHON" - "$OUTPUT" "$1" "${2-Codex version}" <<'PY'
import sys
text, want, step = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]
hints = {"npm": "npm install -g @openai/codex@latest", "brew": "brew upgrade codex", "app": "update the ChatGPT app",
         "curl": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"}
def named(line):
    return {k for k, hint in hints.items() if (hint.lower() in line.lower() if k == "app" else hint in line)}
lines = [line for line in text.splitlines() if named(line) or "upgrade" in line.lower()]
assert any(want in named(line) for line in lines), "no line names the %s hint (%s):\n%s" % (want, hints[want], text)
wrong = [line for line in lines if named(line) - {want}]
assert not wrong, "another installer's hint where the %s one belongs:\n%s" % (want, "\n".join(wrong))
if step:
    summary = text.rsplit("\nSummary:\n", 1)[-1].splitlines()
    record = [line for line in summary if line.startswith("  %s: " % step)]
    assert record and want in named(record[0]), "the Summary step %r does not name the %s hint: %s" % (step, want, record)
PY
}

upgrade_hint_run() {
    # $1 the case; $2 the fixture codex, installed at this path in the case; $3 CODEX_BIN, a path in the case; $4 when $3 is not
    # $2, the target of the symlink at $3 (../relative as npm and Homebrew write theirs, else a path in the case). The fixture
    # prints Codex 0.155.0, below the table's highest floor 0.156.1 (Luna's; Sol's is 0.153.0 since table v17): the Codex
    # version step FAILS with an upgrade hint
    local target
    new_case "$1"
    mkdir -p "$(dirname "$CASE/$2")" "$(dirname "$CASE/$3")"
    make_native "$CASE/$2"
    if [ "$3" != "$2" ]; then
        case "$4" in ../*) target="$4" ;; *) target="$CASE/$4" ;; esac
        ln -s "$target" "$CASE/$3"
    fi
    EXPLICIT_CODEX="$CASE/$3"
    CODEX_VERSIONS="$(json_versions "$CASE/$2" 0.155.0)"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert test "$LAST_STATUS" -ne 0
    step_failed 'Codex version' || fail "Codex version: $(summary_value 'Codex version')"
}

test_groupD_upgrade_hint_npm() {
    # L4, npm: @openai/codex under an npm prefix (npm links <prefix>/bin/codex to ../lib/node_modules/@openai/codex/bin/codex),
    # also the prefix of a Homebrew Node.js (/opt/homebrew: npm's hint, not Homebrew's). The branch is exactly
    # /node_modules/@openai/codex/: a codex vendored by @openai/codex-sdk is anything else (OpenAI's installer)
    local sdk=lib/node_modules/@openai/codex-sdk/vendor/aarch64-apple-darwin/codex/codex
    upgrade_hint_run groupD-upgrade-hint-npm npm-global/lib/node_modules/@openai/codex/bin/codex npm-global/bin/codex \
        ../lib/node_modules/@openai/codex/bin/codex
    upgrade_hints_are npm
    upgrade_hint_run groupD-upgrade-hint-npm-homebrew-node fixed-root/opt/homebrew/lib/node_modules/@openai/codex/bin/codex \
        fixed-root/opt/homebrew/bin/codex ../lib/node_modules/@openai/codex/bin/codex
    upgrade_hints_are npm
    upgrade_hint_run groupD-upgrade-hint-npm-sdk "npm-global/$sdk" npm-global/bin/codex "../$sdk"
    upgrade_hints_are curl
}

test_groupD_upgrade_hint_homebrew() {
    # L4, Homebrew: a formula in the Cellar (/opt/homebrew, /usr/local/Cellar) or a cask in the Caskroom, each linked from its
    # prefix's bin, and a formula reached through a link elsewhere (the real path decides)
    upgrade_hint_run groupD-upgrade-hint-brew-arm fixed-root/opt/homebrew/Cellar/codex/0.155.0/bin/codex \
        fixed-root/opt/homebrew/bin/codex ../Cellar/codex/0.155.0/bin/codex
    upgrade_hints_are brew
    upgrade_hint_run groupD-upgrade-hint-brew-intel fixed-root/usr/local/Cellar/codex/0.155.0/bin/codex \
        fixed-root/usr/local/bin/codex ../Cellar/codex/0.155.0/bin/codex
    upgrade_hints_are brew
    upgrade_hint_run groupD-upgrade-hint-brew-cask fixed-root/usr/local/Caskroom/codex/0.155.0/codex-x86_64-apple-darwin \
        fixed-root/usr/local/bin/codex ../Caskroom/codex/0.155.0/codex-x86_64-apple-darwin
    upgrade_hints_are brew
    upgrade_hint_run groupD-upgrade-hint-brew-linked fixed-root/opt/homebrew/Cellar/codex/0.155.0/bin/codex bin/codex \
        fixed-root/opt/homebrew/Cellar/codex/0.155.0/bin/codex
    upgrade_hints_are brew
}

test_groupD_upgrade_hint_chatgpt_app() {
    # L4, the ChatGPT app: its bundled launcher below the floor, as CODEX_BIN, and as discovery's choice when it is the only
    # codex (npm unavailable, so bootstrap installs nothing)
    new_case groupD-upgrade-hint-app
    EXPLICIT_CODEX="$APP"
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0)"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert test "$LAST_STATUS" -ne 0
    step_failed 'Codex version' || fail "Codex version: $(summary_value 'Codex version')"
    upgrade_hints_are app
    new_case groupD-upgrade-hint-app-discovered
    rm "$COMMON/npm"
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0)"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert_calls npm 0
    assert test "$LAST_STATUS" -ne 0
    step_failed 'Codex version' || fail "Codex version: $(summary_value 'Codex version')"
    upgrade_hints_are app
}

test_groupD_upgrade_hint_installer() {
    # L4, OpenAI's installer (install.sh puts the CLI under $CODEX_HOME/packages/standalone and links it from
    # ${CODEX_INSTALL_DIR:-$HOME/.local/bin}) and anything else. (1) The installer's codex, below the floor, as CODEX_BIN; (2) the
    # same, kept at ~/.local/bin/codex while bootstrap chooses the app launcher (which meets the floor): the PATH shim step's
    # hint to upgrade the kept executable; (3) a codex elsewhere as CODEX_BIN
    local standalone
    upgrade_hint_run groupD-upgrade-hint-installer "home/account codex/packages/standalone/0.155.0/bin/codex" home/.local/bin/codex \
        "home/account codex/packages/standalone/0.155.0/bin/codex"
    upgrade_hints_are curl
    new_case groupD-upgrade-hint-installer-kept
    standalone="$CODEX_DIR/packages/standalone/0.155.0/bin/codex"
    mkdir -p "$(dirname "$standalone")" "$FAKE_HOME/.local/bin"
    make_native "$standalone"
    ln -s "$standalone" "$FAKE_HOME/.local/bin/codex"
    CODEX_VERSIONS="$(json_versions "$standalone" 0.155.0)"         # the app launcher prints 0.156.1: bootstrap chooses it
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert test "$LAST_STATUS" -ne 0
    step_failed 'PATH shim' || fail "PATH shim: $(summary_value 'PATH shim')"
    upgrade_hints_are curl ''
    upgrade_hint_run groupD-upgrade-hint-elsewhere opt/tools/codex opt/tools/codex
    upgrade_hints_are curl
}

package_plugin() {
    # package.sh on a throwaway repository copy; prints the zip path. $1: the copy, $2: the output directory.
    if ! (cd "$CASE" && env -i HOME="$FAKE_HOME" PATH="$(dirname "$PYTHON"):$(dirname "$REAL_GIT"):/usr/bin:/bin" TMPDIR="$CASE/tmp" \
        XDG_CONFIG_HOME="$FAKE_HOME/.config" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 bash "$1/package.sh" "$2") \
        > "$CASE/package.txt" 2>&1; then
        cat "$CASE/package.txt"; fail "package.sh failed"
    fi
    head -1 "$CASE/package.txt"
}

fixture_git() {
    # Real git on a throwaway copy: HOME and XDG_CONFIG_HOME in the case, no global or system configuration, no inherited
    # GIT_* override, no hooks and no commit signing, so no helper or hook of the reviewer's can run.
    env -i HOME="$FAKE_HOME" XDG_CONFIG_HOME="$FAKE_HOME/.config" PATH="$(dirname "$REAL_GIT"):/usr/bin:/bin" TMPDIR="$CASE/tmp" \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
        "$REAL_GIT" -c core.hooksPath=/dev/null -c commit.gpgSign=false -c tag.gpgSign=false "$@"
}

engine_without_relay() {
    # The installed engine, run the way a friend without /relay runs it: no RELAY_BIN, no relay under HOME or on PATH.
    local fixtures="$PLUGIN/skills/codex/tests/fixtures"
    chmod 755 "$fixtures/stub_codex.py" "$fixtures/stub_claude.py"
    (cd "$CASE" && env -i HOME="$FAKE_HOME" PATH="$COMMON" TMPDIR="$CASE/tmp" TEST_LOG="$LOG" TEST_NATIVE_LIST="$NATIVE_LIST" \
        BRIDGE_CODEX_FIXED_ROOT="$FIXED_ROOT" \
        TEST_PLUGIN="$PLUGIN" CODEX_BIN="$fixtures/stub_codex.py" CLAUDE_BIN="$fixtures/stub_claude.py" BRIDGE_JOBS="$CASE/jobs" \
        BRIDGE_MODELS_NPM_URL=http://127.0.0.1:9/npm BRIDGE_MODELS_DEPRECATIONS_URL=http://127.0.0.1:9/deprecations \
        BRIDGE_MODELS_OVERVIEW_URL=http://127.0.0.1:9/overview \
        "$PYTHON" "$PLUGIN/skills/codex/scripts/bridge.py" "$@")
}

test_groupD_package_zip_through_both_entry_points_without_relay() {
    local src out zip dirty version status missing partial
    new_case groupD-package
    src="$CASE/src/claude-codex-bridge"; out="$CASE/out"
    mkdir -p "$src" "$out"
    rsync -a --exclude 'tests/fixtures/bootstrap.*' --exclude '.mcp-test-*' --exclude '__pycache__' --exclude '.git' \
        "$PLUGIN_SOURCE/" "$src/"
    fixture_git -C "$src" init -q
    fixture_git -C "$src" add -A
    fixture_git -C "$src" -c user.name=t -c user.email=t@t commit -q --no-verify --no-gpg-sign -m snapshot
    version="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$src/.claude-plugin/plugin.json")"
    assert test "$version" = 2.3.0                                   # DESIGN §7: plugin.json and marketplace.json go to 2.3.0
    "$PYTHON" - "$src/.claude-plugin/marketplace.json" <<'PY'
import json, sys
def versions(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "version":
                yield v
            yield from versions(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from versions(v)
found = list(versions(json.load(open(sys.argv[1]))))
assert found and set(found) == {"2.3.0"}, found
PY
    zip="$(package_plugin "$src" "$out")"
    assert test -f "$zip"
    assert test "$(basename "$zip")" = "claude-codex-bridge-$version.zip"   # a clean tree: no -dirty stamp
    mkdir -p "$CASE/unzipped"
    (cd "$CASE/unzipped" && unzip -q "$zip")
    PLUGIN="$CASE/unzipped/claude-codex-bridge"
    for f in skills/codex/models.json skills/codex/scripts/bridge_models.py skills/codex/scripts/bridge.py \
        skills/codex/scripts/merge_findings.py codex-skills/claude/scripts/claude_mcp_server.py install.sh bootstrap.sh VERSION; do
        assert test -f "$PLUGIN/$f"
    done
    contains "$PLUGIN/VERSION" "^claude-codex-bridge $version"
    not_contains "$PLUGIN/VERSION" 'dirty'
    # install.sh refuses a folder without the model table, its reader or the merger (install.sh:24 widened)
    for missing in skills/codex/models.json skills/codex/scripts/bridge_models.py skills/codex/scripts/merge_findings.py; do
        partial="$CASE/partial-$(basename "$missing" | tr '.' '-')"
        mkdir -p "$partial"
        (cd "$partial" && unzip -q "$zip")
        rm "$partial/claude-codex-bridge/$missing"
        if env -i HOME="$FAKE_HOME" PATH="$COMMON" TMPDIR="$CASE/tmp" BRIDGE_CODEX_FIXED_ROOT="$FIXED_ROOT" \
            /bin/bash "$partial/claude-codex-bridge/install.sh" --check > "$CASE/partial.txt" 2>&1; then status=0; else status=$?; fi
        assert test "$status" -eq 2
        contains "$CASE/partial.txt" 'incomplete'
    done
    # entry point 1: install.sh (it passes --standalone --no-plugin itself), with no relay anywhere
    PLUGIN_ENABLED=1; ENTRY="$PLUGIN/install.sh"
    run_bootstrap; success
    assert_standalone_links
    contains "$CODEX_DIR/config.toml" "$PLUGIN/codex-skills/claude/scripts/claude_mcp_server.py"
    if ! engine_without_relay ask --seats sol:low --job-dir zip-ask -- q > "$CASE/ask.txt" 2>&1; then cat "$CASE/ask.txt"; fail "installed engine failed"; fi
    contains "$CASE/ask.txt" '^OK'
    "$PYTHON" - "$CASE/jobs/zip-ask/meta.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
assert m["state"] == "done" and m["router"] == {"codex": "off"}, m
PY
    if ! engine_without_relay doctor > "$CASE/doctor.txt" 2>&1; then cat "$CASE/doctor.txt"; fail "installed doctor failed"; fi
    contains "$CASE/doctor.txt" 'routed accounts: availability is decided by the CLI at launch; doctor does not consult relay'
    # entry point 2: bootstrap.sh --standalone from a second extraction, in a fresh HOME
    new_case groupD-package-bootstrap
    mkdir -p "$CASE/unzipped"
    (cd "$CASE/unzipped" && unzip -q "$zip")
    PLUGIN="$CASE/unzipped/claude-codex-bridge"
    run_bootstrap --standalone; success
    assert_standalone_links
    if ! engine_without_relay ask --seats luna:low --job-dir zip-ask-2 -- q > "$CASE/ask.txt" 2>&1; then cat "$CASE/ask.txt"; fail "installed engine failed"; fi
    contains "$CASE/ask.txt" '^OK'
    # package.sh stamps -dirty in VERSION and the zip name when `git status --porcelain` is non-empty
    printf '\n<!-- a local, uncommitted edit -->\n' >> "$src/README.md"
    dirty="$(package_plugin "$src" "$out")"
    case "$(basename "$dirty")" in *-dirty*) ;; *) fail "no -dirty in the zip name: $dirty" ;; esac
    unzip -p "$dirty" claude-codex-bridge/VERSION | grep -q -- '-dirty' || fail "no -dirty in VERSION"
}

# ==================================================================================================================
# The final review (review/rulings_final_review.md, binding; its findings verbatim with file:line: review/final_C_findings.md),
# each scenario naming its finding: C-astra-1 + C-opus-1 (one ruling), C-opus-7 and C-opus-6. The engine's findings:
# skills/codex/tests/test_bridge.py (GroupA_/GroupB_/GroupE_FinalReview*)
# ==================================================================================================================
shim_exec_line_is() {
    # The owned wrapper at ~/.local/bin/codex execs exactly the path $1 as written, no link resolved (shim_target_is compares
    # real paths): a stable path such as /opt/homebrew/bin/codex, never the versioned real path behind it
    "$PYTHON" - "$FAKE_HOME/.local/bin/codex" "$1" <<'PY'
import shlex, sys
text = open(sys.argv[1]).read()
assert 'codex shim (claude-codex-bridge' in text, text
target = shlex.split(text.strip().splitlines()[-1])[1]
assert target == sys.argv[2], "the wrapper execs %r, not the stable path %r" % (target, sys.argv[2])
PY
}

owned_wrapper_to() {
    # The owned wrapper an earlier run left at ~/.local/bin/codex, exec'ing $1: the bytes write_shim writes
    mkdir -p "$FAKE_HOME/.local/bin"
    "$PYTHON" - "$1" > "$FAKE_HOME/.local/bin/codex" <<'PY'
import shlex, sys
print('#!/bin/sh')
print('# codex shim (claude-codex-bridge bootstrap): runs the Codex CLI bootstrap chose')
print('exec ' + shlex.quote(sys.argv[1]) + ' "$@"')
PY
    chmod 755 "$FAKE_HOME/.local/bin/codex"
}

test_groupD_final_review_wrapper_execs_the_stable_discovered_path() {
    # C-opus-1 [P2] + C-astra-1 [P2] (one ruling): consider_codex/validate_codex record the REAL path, and the owned wrapper
    # exec'd it: for Homebrew a versioned keg (Cellar/codex/0.157.0/bin/codex). `brew upgrade codex` removes that keg, and
    # typing codex (or anything running codex from PATH, ~/.local/bin first) fails with "No such file" until bootstrap runs
    # again. The wrapper execs the chosen executable's STABLE discovered path: here /opt/homebrew/bin/codex beneath the fixed
    # root, a link into its keg, chosen because the app launcher is below the floor. After the upgrade (a newer keg, the link
    # re-pointed, the old keg gone) the wrapper still runs
    local link keg newer version
    new_case groupD-final-review-stable-wrapper
    link="$FIXED_ROOT/opt/homebrew/bin/codex"; keg="$FIXED_ROOT/opt/homebrew/Cellar/codex/0.157.0/bin/codex"
    mkdir -p "$(dirname "$link")" "$(dirname "$keg")"
    make_native "$keg"
    ln -s ../Cellar/codex/0.157.0/bin/codex "$link"
    CODEX_VERSIONS="$(json_versions "$APP" 0.155.0-alpha.16.3)"     # the app launcher below the floor; the keg prints 0.156.1
    host_guard
    run_bootstrap; success
    assert_host_guard_clean
    assert_calls npm 0
    shim_target_is "$keg"                                           # the wrapper runs the Homebrew codex ...
    shim_exec_line_is "$link" || fail "the owned wrapper execs the versioned keg's real path, not the stable path $link"
    newer="$FIXED_ROOT/opt/homebrew/Cellar/codex/0.158.0/bin/codex"   # `brew upgrade codex`
    mkdir -p "$(dirname "$newer")"
    make_native "$newer"
    ln -sfn ../Cellar/codex/0.158.0/bin/codex "$link"
    rm -rf "$FIXED_ROOT/opt/homebrew/Cellar/codex/0.157.0"
    version="$(env -i HOME="$FAKE_HOME" PATH="$COMMON" TEST_LOG="$LOG" "$FAKE_HOME/.local/bin/codex" --version 2>&1)" \
        || fail "typing codex fails after brew upgrade codex: $version"
    case "$version" in codex-cli\ *) ;; *) fail "the wrapper printed no Codex version after the upgrade: $version" ;; esac
}

test_groupD_final_review_rediscovery_considers_the_wrappers_target() {
    # C-astra-1 [P2]: the owned wrapper can point at a current standalone Codex outside PATH and the fixed directories. A later
    # run's discovery skipped the wrapper (in the PATH scan and at ~/.local/bin alike), so with the ChatGPT app's Codex below
    # the floor and npm unavailable, or installing a codex still below it, bootstrap chose the old one and re-pointed the
    # working wrapper at it before reporting the version failure: rerunning the installer broke Sol and Luna launches.
    # Rediscovery considers the wrapper's current target as a candidate: it is chosen, the wrapper keeps running it, the Codex
    # version step passes and bootstrap succeeds
    local healthy npm_case step
    for npm_case in unavailable still-old; do
        new_case "groupD-final-review-wrapper-target-$npm_case"
        healthy="$CASE/standalone/bin/codex"                         # a current Codex (0.156.1) outside PATH and the fixed dirs
        mkdir -p "$(dirname "$healthy")"
        make_native "$healthy"
        owned_wrapper_to "$healthy"                                  # what an earlier run left: the wrapper runs it
        if [ "$npm_case" = unavailable ]; then
            rm "$COMMON/npm"
            CODEX_VERSIONS="$(json_versions "$APP" 0.155.0)"
        else
            CODEX_VERSIONS="$(json_versions "$APP" 0.155.0 "$NPM_TARGET" 0.155.0)"   # npm installs a codex still below the floor
        fi
        host_guard
        run_bootstrap
        assert_host_guard_clean
        shim_target_is "$healthy" || fail "the rerun re-pointed the working wrapper away from $healthy (npm $npm_case)"
        step="$(summary_value 'Codex version')" || fail "no Codex version step"
        case "$step" in
            *FAILED*) fail "Codex version: $step (npm $npm_case), although the wrapper's target $healthy meets the floor" ;;
            *0.156.1*) ;;
            *) fail "Codex version: $step" ;;
        esac
        success
    done
}

test_groupD_final_review_claude_floor_note_names_no_opus_version() {
    # C-opus-7 [P3]: below the table's Claude floor bootstrap printed "(before it, opus meant the previous Opus)", true only for today's
    # table: a new Opus head raising the floor makes it name the wrong model, and a Fable or Sonnet head carrying the highest
    # floor would be blamed on opus. The note names no specific model version (an older Claude Code resolves the short names to
    # older models); the step still FAILS and names `claude update`
    new_case groupD-final-review-claude-floor-note
    CLAUDE_VERSION="2.1.279 (Claude Code)"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    step_failed 'Claude Code version' || fail "Claude Code version: $(summary_value 'Claude Code version')"
    contains "$OUTPUT" 'claude update'
    not_contains "$OUTPUT" 'opus meant|opus [0-9]'
}

test_groupD_c7_claude_code_below_the_sonnet_floor() {
    # P-16: Haiku raises the highest verified Claude floor to 2.1.295. The earlier C7 build fails the step,
    # which names that floor and `claude update`; the verified compatible build itself passes
    local step
    new_case groupD-c7-sonnet-floor
    CLAUDE_VERSION="2.1.283 (Claude Code)"
    run_bootstrap
    assert test "$LAST_STATUS" -ne 0
    step="$(summary_value 'Claude Code version')" || fail "no Claude Code version step"
    case "$step" in FAILED*2.1.283*2.1.295*) ;; *) fail "Claude Code version: $step" ;; esac
    contains "$OUTPUT" 'claude update'
    new_case groupD-c7-sonnet-floor-met
    CLAUDE_VERSION="2.1.295 (Claude Code)"
    run_bootstrap; success
    step="$(summary_value 'Claude Code version')" || fail "no Claude Code version step"
    case "$step" in *FAILED*|skipped*) fail "Claude Code version: $step" ;; *2.1.295*) ;; *) fail "Claude Code version: $step" ;; esac
}

test_groupD_final_review_package_dirty_counts_only_the_bridge_folder() {
    # C-opus-6 [P3]: package.sh ran `git status --porcelain` over the WHOLE enclosing repository. claude-codex-bridge/ lives in
    # the auto-synced skills repository, so any uncommitted change elsewhere (a skill, web/, a stray file) stamped a clean build
    # -dirty in VERSION and in the zip name; the package scenario above builds from a repository whose root is the plugin, so it
    # could not see this. The check covers only the bridge folder (`git status --porcelain -- .`): here the bridge is a
    # subfolder beside web/; an edit and an untracked file outside it leave the build clean, an edit inside it stamps -dirty
    local repo src out zip dirty version
    new_case groupD-final-review-package-dirty
    repo="$CASE/skills-repo"; src="$repo/claude-codex-bridge"; out="$CASE/out"
    mkdir -p "$src" "$out" "$repo/web"
    rsync -a --exclude 'tests/fixtures/bootstrap.*' --exclude '.mcp-test-*' --exclude '__pycache__' --exclude '.git' \
        "$PLUGIN_SOURCE/" "$src/"
    printf 'a sibling of the bridge in the skills repository\n' > "$repo/web/index.md"
    fixture_git -C "$repo" init -q
    fixture_git -C "$repo" add -A
    fixture_git -C "$repo" -c user.name=t -c user.email=t@t commit -q --no-verify --no-gpg-sign -m snapshot
    version="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$src/.claude-plugin/plugin.json")"
    printf 'an uncommitted edit outside the bridge\n' >> "$repo/web/index.md"
    printf 'a stray untracked file\n' > "$repo/stray.txt"
    zip="$(package_plugin "$src" "$out")"
    assert test -f "$zip"
    case "$(basename "$zip")" in *-dirty*) fail "changes outside the bridge folder stamped the zip -dirty: $zip" ;; esac
    if unzip -p "$zip" claude-codex-bridge/VERSION | grep -q -- '-dirty'; then fail "changes outside the bridge folder stamped VERSION -dirty"; fi
    assert test "$(basename "$zip")" = "claude-codex-bridge-$version.zip"
    printf '\n<!-- a local, uncommitted edit -->\n' >> "$src/README.md"
    dirty="$(package_plugin "$src" "$out")"
    case "$(basename "$dirty")" in *-dirty*) ;; *) fail "an edit inside the bridge folder did not stamp -dirty: $dirty" ;; esac
    unzip -p "$dirty" claude-codex-bridge/VERSION | grep -q -- '-dirty' || fail "an edit inside the bridge folder left VERSION clean"
}

# ==================================================================================================================
# The re-review of the final-review fix round (review/rulings_rr.md, binding; its findings verbatim with file:line:
# review/final_rr_findings.md): RR-2, RR-5 and RR-9, each scenario naming its item. The engine's items:
# skills/codex/tests/test_bridge.py GroupB_ReReviewLaunchAndStatus
# ==================================================================================================================
probed_with_version() {
    # bootstrap probed the fixture executable $2 (tool $1: codex | claude) with --version: the call log has its real path
    "$PYTHON" - "$LOG" "$1" "$(realpath_of "$2")" <<'PY'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
tool, path = sys.argv[2:4]
assert [c for c in calls if c["tool"] == tool and c["path"] == path and c["args"] == ["--version"]], \
    "%s was never probed with --version; the %s calls: %s" % (path, tool, [(c["path"], c["args"]) for c in calls if c["tool"] == tool])
PY
}

chose() {
    # bootstrap's `Chose <path> (...)` line names the real path of $1
    "$PYTHON" - "$OUTPUT" "$(realpath_of "$1")" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
assert any(line.strip().startswith("ok: Chose %s (" % sys.argv[2]) for line in lines), "bootstrap did not choose %s:\n%s" % (
    sys.argv[2], "\n".join(line for line in lines if "Chose" in line or "Rejected" in line))
PY
}

test_groupD_rereview_relative_claude_executable() {
    # RR-2 [P2] (astra-2 = opus-2): since S-BOOT every helper, `bounded` included, runs from bootstrap's folder, but the Claude
    # executable was taken verbatim from CLAUDE_BIN or from `command -v claude`, which answers a relative path for a relative
    # PATH entry. The -f/-x checks ran in the caller's directory and passed; the probe then ran that path beneath the bridge
    # folder and failed, and bootstrap reported Claude Code unavailable ('Claude executable' FAILED, the Claude steps skipped).
    # The ruling: CLAUDE_BIN and a `command -v` result are resolved against the caller's directory once, before any helper
    # uses them. The only claude is ./tools/claude beneath the caller's directory: named by CLAUDE_BIN, then found through the
    # relative PATH entry ./tools; either way it is probed and the Claude steps pass
    local how step
    for how in CLAUDE_BIN command-v; do
        new_case "groupD-rereview-relative-claude-$how"
        mkdir -p "$CASE/tools"
        cp "$STUB" "$CASE/tools/claude"; chmod 755 "$CASE/tools/claude"
        rm "$COMMON/claude"
        if [ "$how" = CLAUDE_BIN ]; then EXTRA_ENV+=("CLAUDE_BIN=./tools/claude"); else CALLER_PATH="./tools:$CALLER_PATH"; fi
        run_bootstrap                                                   # from $CASE
        step="$(summary_value 'Claude executable')" || fail "no Claude executable step"
        [ "$step" = ok ] || fail "Claude executable: $step ($how), although ./tools/claude beneath the caller's directory $START_DIR is executable"
        probed_with_version claude "$CASE/tools/claude"
        step="$(summary_value 'Claude Code version')" || fail "no Claude Code version step"
        case "$step" in *FAILED*|skipped*) fail "Claude Code version: $step ($how)" ;; *2.1.295*) ;; *) fail "Claude Code version: $step" ;; esac
        success
    done
}

test_groupD_rereview_codex_through_a_relative_path_entry() {
    # RR-2 [P2] (astra-2): the PATH bootstrap hands its helpers kept relative entries, which `bounded` and every CLI it starts
    # read from bootstrap's folder, not the caller's. A Node.js distribution's bin directory reached through a relative PATH
    # entry holds node and npm's codex launcher (`#!/usr/bin/env node`): discovery finds that codex (it makes the entry
    # absolute), but its probes ran from the bridge folder, where `env` finds no node, so it was rejected and the app launcher
    # chosen. The ruling: every relative PATH entry handed to helpers is made absolute against the caller's directory, and an
    # empty entry means the caller's directory. That codex is probed and chosen, through ./node-dist/bin and through an empty
    # entry (the app launcher also meets the floor, so nothing else explains the choice)
    local entry dist step
    for entry in dot-relative empty; do
        new_case "groupD-rereview-relative-path-codex-$entry"
        dist="$CASE/node-dist/bin"
        mkdir -p "$dist"
        { printf '#!/usr/bin/env node\n'; tail -n +2 "$STUB"; } > "$dist/codex"; chmod 755 "$dist/codex"
        printf '#!/bin/sh\nexec "%s" "$@"\n' "$PYTHON" > "$dist/node"; chmod 755 "$dist/node"   # stands in for Node.js
        rm "$COMMON/node"                                               # the only node is in that directory
        if [ "$entry" = dot-relative ]; then
            CALLER_PATH="./node-dist/bin:$CALLER_PATH"                  # bootstrap runs from $CASE
        else
            START_DIR="$dist"; CALLER_PATH=":$CALLER_PATH"              # an empty entry: the caller's directory
        fi
        run_bootstrap
        chose "$dist/codex" || fail "bootstrap did not choose the codex found through the relative PATH entry ($entry)"
        probed_with_version codex "$dist/codex"
        step="$(summary_value 'Codex version')" || fail "no Codex version step"
        case "$step" in *FAILED*) fail "Codex version: $step ($entry)" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
        success
    done
}

test_groupD_rereview_keg_wrapper_moves_to_the_stable_link() {
    # RR-5 [P2] (opus-1; C-opus-1 was closed only for fresh installs): a machine set up by the bootstrap before that fix has
    # ~/.local/bin/codex exec'ing Homebrew's versioned keg (<prefix>/Cellar/codex/<version>/bin/codex). On a rerun the
    # wrapper's target was considered as it stands and met the floor, so it was chosen and the wrapper stayed "already
    # current": the next `brew upgrade codex` removes the keg and typing codex fails with "No such file", C-opus-1's failure.
    # The ruling: when the owned wrapper's target is a keg path and the stable link <prefix>/bin/codex exists and is
    # executable, rediscovery considers the link instead, and the rewritten wrapper execs it. For both Homebrew prefixes
    # beneath the fixed root, with the app launcher also meeting the floor (the wrapper's target comes before it); after
    # bootstrap the wrapper execs the link, and still runs after the upgrade
    local prefix link keg newer version
    for prefix in opt/homebrew usr/local; do
        new_case "groupD-rereview-keg-wrapper-$(printf '%s' "$prefix" | tr / -)"
        link="$FIXED_ROOT/$prefix/bin/codex"; keg="$FIXED_ROOT/$prefix/Cellar/codex/0.157.0/bin/codex"
        mkdir -p "$(dirname "$link")" "$(dirname "$keg")"
        make_native "$keg"
        ln -s ../Cellar/codex/0.157.0/bin/codex "$link"
        owned_wrapper_to "$keg"                                         # what the bootstrap before C-opus-1's fix left
        host_guard
        run_bootstrap
        assert_host_guard_clean
        shim_exec_line_is "$link" || fail "the owned wrapper still execs the versioned keg $keg, not the stable link $link"
        shim_target_is "$keg"
        assert_calls npm 0
        success
        newer="$FIXED_ROOT/$prefix/Cellar/codex/0.158.0/bin/codex"        # `brew upgrade codex`
        mkdir -p "$(dirname "$newer")"
        make_native "$newer"
        ln -sfn ../Cellar/codex/0.158.0/bin/codex "$link"
        rm -rf "$FIXED_ROOT/$prefix/Cellar/codex/0.157.0"
        version="$(env -i HOME="$FAKE_HOME" PATH="$COMMON" TEST_LOG="$LOG" "$FAKE_HOME/.local/bin/codex" --version 2>&1)" \
            || fail "typing codex fails after brew upgrade codex: $version"
        case "$version" in codex-cli\ *) ;; *) fail "the wrapper printed no Codex version after the upgrade: $version" ;; esac
    done
}

test_groupD_rereview_helpers_import_nothing_from_the_callers_directory() {
    # RR-9 [P3] (opus-5): nothing pinned S-BOOT. A `-c` or stdin script puts its working directory first on sys.path, so every
    # Python helper bootstrap starts (py(), and `bounded`, which starts the CLIs) runs from bootstrap's folder: a project
    # checkout bootstrap is run from may hold a socket.py or json.py. Here the caller's directory holds both, and each records
    # its import in a marker file outside it: an install, a --check and an --uninstall run from there all run to completion
    # and none imports them. It passes with S-BOOT in place and fails when py() or `bounded` stops changing to that folder
    local marker run module
    new_case groupD-rereview-hostile-caller-directory
    START_DIR="$CASE/checkout"; marker="$CASE/imported-from-the-callers-directory.txt"
    mkdir -p "$START_DIR"
    for module in socket json; do
        "$PYTHON" -c 'import sys; open(sys.argv[1], "w").write("open(%r, \"a\").write(__name__ + \"\\n\")\n" % sys.argv[2])' \
            "$START_DIR/$module.py" "$marker"
    done
    for run in install check uninstall; do
        case "$run" in
            install) run_bootstrap ;;
            check) run_bootstrap --check ;;
            uninstall) run_bootstrap --uninstall ;;
        esac
        if [ -e "$marker" ]; then fail "bootstrap ($run) imported these modules from the caller's directory $START_DIR: $(tr '\n' ' ' < "$marker")"; fi
        contains "$OUTPUT" '^Summary:'
        if [ "$run" = check ]; then assert test "$LAST_STATUS" -le 1; else success; fi
    done
}

# ==================================================================================================================
# The second re-review (review/rulings_rr2.md, binding; its findings verbatim with file:line: review/rr2_findings.md):
# S-PATH (RR2-astra-1 = RR2-opus-3 [P2], RR2-opus-1 [P3]), each scenario naming its finding. A relative CODEX_BIN or CLAUDE_BIN
# names what the caller's shell would run: a leading ~ is $HOME, the rest is joined onto the caller's directory without
# collapsing .. as text, then links are resolved; a leading ~ in a PATH entry is $HOME. Every executable is a fixture in the
# case and the host guard is on, so each scenario can say which executable bootstrap validated and chose
# ==================================================================================================================
claude_ran_only() {
    # bootstrap validated the fixture claude $1 (probed it with --version) and ran no other claude
    "$PYTHON" - "$LOG" "$(realpath_of "$1")" <<'PY'
import json, sys
path = sys.argv[2]
claude = [(c["path"], c["args"]) for c in (json.loads(line) for line in open(sys.argv[1])) if c["tool"] == "claude"]
assert (path, ["--version"]) in claude, "bootstrap never validated %s; its claude calls: %s" % (path, claude)
others = sorted(set(p for p, _ in claude if p != path))
assert not others, "bootstrap ran a claude other than %s: %s" % (path, others)
PY
}

symlinked_caller_run() {
    # $1 the case, $2 the tool (codex | claude) named as ../tools/$2 by CODEX_BIN or CLAUDE_BIN. RR2-opus-3's layout: the
    # caller's directory is ~/proj, a link to $CASE/Volumes/ssd/work/proj, so ../tools/$2 run from there is
    # $CASE/Volumes/ssd/work/tools/$2 (TARGET); the same text with .. collapsed is ~/tools/$2 (DECOY), a working fixture too,
    # which must never run. PWD reaches bootstrap as the caller's shell hands it over (under env -i alone bash would start in
    # the physical directory, where .. means the same either way)
    local work
    new_case "$1"
    work="$CASE/Volumes/ssd/work"; TARGET="$work/tools/$2"; DECOY="$FAKE_HOME/tools/$2"
    mkdir -p "$work/proj" "$work/tools" "$FAKE_HOME/tools"
    ln -s "$work/proj" "$FAKE_HOME/proj"
    START_DIR="$FAKE_HOME/proj"; EXTRA_ENV+=("PWD=$START_DIR")
    if [ "$2" = codex ]; then
        make_native "$TARGET"; make_native "$DECOY"
        EXPLICIT_CODEX=../tools/codex
    else
        cp "$STUB" "$TARGET"; cp "$STUB" "$DECOY"; chmod 755 "$TARGET" "$DECOY"
        rm "$COMMON/claude"                                             # CLAUDE_BIN alone names a claude
        EXTRA_ENV+=("CLAUDE_BIN=../tools/claude")
    fi
    host_guard
    run_bootstrap
    assert_host_guard_clean
}

test_groupD_rr2_codex_bin_parent_path_from_a_symlinked_caller_directory() {
    # RR2-astra-1 = RR2-opus-3 [P2] (S-PATH): RR-2 made a relative CODEX_BIN absolute with os.path.abspath, which collapses ..
    # as text before any link is followed. Run from ~/proj, a link to .../work/proj, CODEX_BIN=../tools/codex is
    # .../work/tools/codex to the caller's shell (and to the merge-base's real_path), but bootstrap validated ~/tools/codex:
    # "unusable; no fallback" when nothing is there, and here, where a codex is, it reports on, sign-in-checks and chooses a
    # different executable from the one the user named. The codex the shell runs is validated and chosen, and every codex
    # call runs it
    local step
    symlinked_caller_run groupD-rr2-symlinked-caller-codex-bin codex
    chose "$TARGET" || fail "CODEX_BIN=../tools/codex from the symlinked $START_DIR: bootstrap did not choose $TARGET, the codex the caller's shell runs"
    probed_with_version codex "$TARGET"
    assert_calls native "$(realpath_of "$TARGET")"                      # every codex call ran it ...
    assert_calls unprobed "$DECOY"                                      # ... and none the text-collapsed ~/tools/codex
    step="$(summary_value 'Codex version')" || fail "no Codex version step"
    case "$step" in *FAILED*) fail "Codex version: $step" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
    success
}

test_groupD_rr2_claude_bin_parent_path_from_a_symlinked_caller_directory() {
    # RR2-astra-1 = RR2-opus-3 [P2] (S-PATH): RR-2's absolute_path() at the Claude executable gave a relative CLAUDE_BIN the
    # same text-only meaning. From the symlinked ~/proj, CLAUDE_BIN=../tools/claude: the claude the caller's shell runs is
    # validated and runs every Claude step, never ~/tools/claude
    local step
    symlinked_caller_run groupD-rr2-symlinked-caller-claude-bin claude
    claude_ran_only "$TARGET" || fail "CLAUDE_BIN=../tools/claude from the symlinked $START_DIR: bootstrap did not validate and use $TARGET, the claude the caller's shell runs"
    assert_calls unprobed "$DECOY"
    step="$(summary_value 'Claude executable')" || fail "no Claude executable step"
    [ "$step" = ok ] || fail "Claude executable: $step"
    step="$(summary_value 'Claude Code version')" || fail "no Claude Code version step"
    case "$step" in *FAILED*|skipped*) fail "Claude Code version: $step" ;; *2.1.295*) ;; *) fail "Claude Code version: $step" ;; esac
    success
}

test_groupD_rr2_codex_bin_dot_dot_after_a_symlinked_sdk() {
    # RR2-astra-1 [P2] (S-PATH): the same collapse inside the value. The caller's directory is a plain one whose sdk is a link
    # to $CASE/opt/toolchain/current, so CODEX_BIN=./sdk/../bin/codex runs $CASE/opt/toolchain/bin/codex, but abspath made it
    # <caller>/bin/codex (here a working decoy). The toolchain's codex is validated and chosen, and every codex call runs it
    local target decoy step
    new_case groupD-rr2-sdk-dot-dot
    START_DIR="$CASE/checkout"
    target="$CASE/opt/toolchain/bin/codex"; decoy="$START_DIR/bin/codex"
    mkdir -p "$CASE/opt/toolchain/current" "$(dirname "$target")" "$(dirname "$decoy")"
    ln -s "$CASE/opt/toolchain/current" "$START_DIR/sdk"
    make_native "$target"; make_native "$decoy"
    EXPLICIT_CODEX=./sdk/../bin/codex
    host_guard
    run_bootstrap
    assert_host_guard_clean
    chose "$target" || fail "CODEX_BIN=./sdk/../bin/codex with sdk -> $CASE/opt/toolchain/current: bootstrap did not choose $target, the codex the caller's shell runs"
    probed_with_version codex "$target"
    assert_calls native "$(realpath_of "$target")"
    assert_calls unprobed "$decoy"
    step="$(summary_value 'Codex version')" || fail "no Codex version step"
    case "$step" in *FAILED*) fail "Codex version: $step" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
    success
}

test_groupD_rr2_tilde_path_entry_holds_the_only_claude_and_codex() {
    # RR2-opus-1 [P3] (S-PATH): export PATH="~/tools:$PATH" leaves a literal ~ (the double quotes stop tilde expansion), and
    # bash expands it in its own command search, so the caller's shell runs ~/tools/claude and ~/tools/codex. RR-2's PATH
    # normalisation made the entry <caller>/~/tools: `command -v claude` found nothing ('Claude executable' FAILED) and
    # discovery skipped that codex, so npm installed another. ~/tools holds the only claude and codex (no app launcher, no
    # other claude; npm stays available): both are validated and chosen, and npm never runs
    local tools step
    new_case groupD-rr2-tilde-path-entry
    tools="$FAKE_HOME/tools"
    mkdir -p "$tools"
    make_native "$tools/codex"
    cp "$STUB" "$tools/claude"; chmod 755 "$tools/claude"
    rm "$APP" "$COMMON/claude"
    CALLER_PATH="~/tools:$CALLER_PATH"                                  # the tilde stays literal
    host_guard
    run_bootstrap
    assert_host_guard_clean
    chose "$tools/codex" || fail "PATH=~/tools:...: bootstrap did not choose $tools/codex, the codex the caller's shell runs"
    probed_with_version codex "$tools/codex"
    assert_calls native "$(realpath_of "$tools/codex")"
    assert_calls npm 0                                                  # nothing was installed in its place
    claude_ran_only "$tools/claude" || fail "PATH=~/tools:...: bootstrap did not validate and use $tools/claude, the claude the caller's shell runs"
    step="$(summary_value 'Claude executable')" || fail "no Claude executable step"
    [ "$step" = ok ] || fail "Claude executable: $step"
    step="$(summary_value 'Codex version')" || fail "no Codex version step"
    case "$step" in *FAILED*) fail "Codex version: $step" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
    step="$(summary_value 'Claude Code version')" || fail "no Claude Code version step"
    case "$step" in *FAILED*|skipped*) fail "Claude Code version: $step" ;; *2.1.295*) ;; *) fail "Claude Code version: $step" ;; esac
    success
}

# ==================================================================================================================
# The third re-review (review/rulings_rr3.md, binding; its findings verbatim with file:line: review/rr3_findings.md): two
# S-PATH follow-ups, RR3-1 (RR3-astra-1 [P3]) and RR3-2 (RR3-opus-1 [P3]), the coordinator's addendum RR3-1b, and RR3-3 from
# the last review (its Astra [P3]: review/rr3b.out), each scenario naming its finding. A relative CODEX_BIN or CLAUDE_BIN
# must work as spelled (a leading ~ expanded, then joined onto the caller's directory as text) before real_path resolves it,
# and validate_codex checks every codex candidate as given (an absolute CODEX_BIN too) before it resolves it; the ~/.zshrc
# advice reads the caller's PATH as given, where only an absolute entry suppresses it. Every executable is a fixture in the
# case and the host guard is on, so each scenario can say which executable bootstrap probed
# ==================================================================================================================
never_ran() {
    # no call in the log ran the fixture $1 (by its real path), under any tool name; else name each call that did
    "$PYTHON" - "$LOG" "$(realpath_of "$1")" <<'PY'
import json, sys
ran = [c["args"] for c in (json.loads(line) for line in open(sys.argv[1])) if c["path"] == sys.argv[2]]
assert not ran, "bootstrap ran %s with: %s" % (sys.argv[2], ran)
PY
}

file_link_run() {
    # $1 the case, $2 the tool (codex | claude) named ./link/../v1 by CODEX_BIN or CLAUDE_BIN, or $START_DIR/link/../v1 when
    # $3 is absolute (RR3-1b); SPELLING is the value given. RR3-astra-1's layout: the caller's directory holds link, a symlink
    # to the executable FILE $CASE/opt/$2/versions/v1 (TARGET). The caller's shell cannot run SPELLING (link is not a
    # directory: ENOTDIR, checked here), but realpath follows link to TARGET, takes .. from there and lands on TARGET again.
    # The stub answers under any name, so any probe of TARGET is in the call log
    new_case "$1"
    START_DIR="$CASE/checkout"; TARGET="$CASE/opt/$2/versions/v1"; SPELLING=./link/../v1
    if [ "${3:-}" = absolute ]; then SPELLING="$START_DIR/link/../v1"; fi
    mkdir -p "$START_DIR" "$(dirname "$TARGET")"
    if [ "$2" = codex ]; then
        make_native "$TARGET"
        EXPLICIT_CODEX="$SPELLING"
    else
        cp "$STUB" "$TARGET"; chmod 755 "$TARGET"
        EXTRA_ENV+=("CLAUDE_BIN=$SPELLING")
    fi
    ln -s "$TARGET" "$START_DIR/link"
    if (cd "$START_DIR" && [ -e "$SPELLING" ]); then fail "fixture: the shell reaches $SPELLING from $START_DIR"; fi
    host_guard
    run_bootstrap
    assert_host_guard_clean
}

test_groupD_rr3_codex_bin_dot_dot_after_a_file_link_is_rejected() {
    # RR3-astra-1 [P3] (RR3-1): d5dba27f resolves a relative CODEX_BIN with real_path (bootstrap.sh:604) before anything
    # checks it, and realpath takes .. from a link's target even when that target is a file. CODEX_BIN=./link/../v1 fails in
    # the caller's shell with ENOTDIR, but bootstrap made it TARGET, validated it and chose it. The ruling: the spelling must
    # pass -f and -x before it is resolved, and one that fails is rejected like any unusable CODEX_BIN: 'Codex executable'
    # FAILED, no fallback (no other codex, no npm), and TARGET never probed
    local step
    file_link_run groupD-rr3-codex-bin-through-a-file-link codex
    never_ran "$TARGET" || fail "CODEX_BIN=./link/../v1 from $START_DIR (link -> the file $TARGET): bootstrap probed $TARGET, which the caller's shell cannot run by that spelling"
    never_ran "$APP"                                                    # no fallback ...
    assert_calls npm 0                                                  # ... and nothing installed in its place
    step="$(summary_value 'Codex executable')" || fail "no Codex executable step"
    case "$step" in FAILED*) ;; *) fail "Codex executable: $step, although CODEX_BIN=./link/../v1 is unusable from $START_DIR" ;; esac
    contains "$OUTPUT" 'CODEX_BIN is unusable'
    assert test "$LAST_STATUS" -ne 0
}

test_groupD_rr3_claude_bin_dot_dot_after_a_file_link_is_rejected() {
    # RR3-astra-1 [P3] (RR3-1): the same at the Claude executable (bootstrap.sh:723), where the finding reproduced it with the
    # installed Claude symlink, a link to a versioned file. CLAUDE_BIN=./link/../v1 became TARGET, passed -f and -x and ran
    # for every Claude step. It is rejected like any unusable CLAUDE_BIN: 'Claude executable' FAILED, no other claude taken in
    # its place (the fixture claude on PATH never runs), and TARGET never probed
    local step
    file_link_run groupD-rr3-claude-bin-through-a-file-link claude
    never_ran "$TARGET" || fail "CLAUDE_BIN=./link/../v1 from $START_DIR (link -> the file $TARGET): bootstrap probed $TARGET, which the caller's shell cannot run by that spelling"
    never_ran "$COMMON/claude"                                          # no fallback to the claude on PATH
    step="$(summary_value 'Claude executable')" || fail "no Claude executable step"
    case "$step" in FAILED*) ;; *) fail "Claude executable: $step, although CLAUDE_BIN=./link/../v1 is unusable from $START_DIR" ;; esac
    assert test "$LAST_STATUS" -ne 0
}

test_groupD_rr3_tilde_local_bin_entry_still_gets_the_zshrc_advice() {
    # RR3-opus-1 [P3] (RR3-2): d5dba27f rewrites a literal ~/.local/bin PATH entry to $HOME/.local/bin at the top, and the
    # ~/.zshrc advice (bootstrap.sh:653) read the rewritten PATH, so it saw ~/.local/bin on PATH and said nothing. zsh, like
    # dash and ksh (unlike bash outside POSIX mode), never expands a ~ in PATH: for a zsh user whose ~/.zshrc has
    # export PATH="~/.local/bin:$PATH", typing codex never reaches the wrapper bootstrap writes there. The ruling: the advice
    # reads the caller's PATH as given and matches only an absolute $HOME/.local/bin entry. With the literal entry alone the
    # advice line is printed; with the absolute entry as well it is not. (Discovery through the rewritten PATH:
    # test_groupD_rr2_tilde_path_entry_holds_the_only_claude_and_codex)
    local entry
    for entry in literal-only with-absolute; do
        new_case "groupD-rr3-zshrc-advice-$entry"
        if [ "$entry" = literal-only ]; then
            CALLER_PATH="~/.local/bin:$BIN:$COMMON"                     # the tilde stays literal; no absolute entry
        else
            CALLER_PATH="~/.local/bin:$CALLER_PATH"                     # $FAKE_HOME/.local/bin is on it as well
        fi
        host_guard
        run_bootstrap
        assert_host_guard_clean
        success
        shim_target_is "$APP"                                           # the wrapper at ~/.local/bin/codex runs the chosen codex
        if [ "$entry" = literal-only ]; then
            grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$OUTPUT" || fail "PATH=~/.local/bin:... with no absolute entry: no ~/.zshrc advice, although zsh does not expand that ~ and typing codex never reaches $FAKE_HOME/.local/bin/codex"
            contains "$OUTPUT" 'Add this exact line to ~/\.zshrc'
        else
            if grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$OUTPUT"; then fail "PATH holds $FAKE_HOME/.local/bin, yet bootstrap printed the ~/.zshrc advice"; fi
            not_contains "$OUTPUT" '\.zshrc'
        fi
    done
}

test_groupD_rr3b_absolute_codex_bin_dot_dot_after_a_file_link_is_rejected() {
    # RR3-1b (the coordinator's addendum to review/rulings_rr3.md): df80288d checks a CODEX_BIN as spelled only when it is
    # relative. The explicit site leaves an absolute one alone (bootstrap.sh:618), and validate_codex resolves every candidate
    # with real_path (bootstrap.sh:343) and checks -f and -x on the resolved path alone, so CODEX_BIN=$START_DIR/link/../v1,
    # which fails in the caller's shell with ENOTDIR, became TARGET, was probed and chosen. The ruling: validate_codex requires
    # the candidate as given to pass -f and -x before real_path. This one is rejected like any unusable CODEX_BIN: 'Codex
    # executable' FAILED, no fallback (no other codex, no npm), and TARGET never probed
    local step
    file_link_run groupD-rr3b-absolute-codex-bin-through-a-file-link codex absolute
    never_ran "$TARGET" || fail "CODEX_BIN=$SPELLING (link -> the file $TARGET): bootstrap probed $TARGET, which the caller's shell cannot run by that spelling"
    never_ran "$APP"                                                    # no fallback ...
    assert_calls npm 0                                                  # ... and nothing installed in its place
    step="$(summary_value 'Codex executable')" || fail "no Codex executable step"
    case "$step" in FAILED*) ;; *) fail "Codex executable: $step, although CODEX_BIN=$SPELLING is unusable" ;; esac
    contains "$OUTPUT" 'CODEX_BIN is unusable'
    assert test "$LAST_STATUS" -ne 0
}

test_groupD_rr3b_absolute_claude_bin_dot_dot_after_a_file_link_is_rejected() {
    # RR3-1b: the Claude side, which the addendum finds unaffected, pinned. An absolute CLAUDE_BIN is left alone
    # (bootstrap.sh:744) and reaches the -f and -x checks as spelled (bootstrap.sh:799), where the kernel walks link/.. and
    # fails with ENOTDIR: 'Claude executable' FAILED, no other claude taken in its place (the fixture claude on PATH never
    # runs), and TARGET never probed
    local step
    file_link_run groupD-rr3b-absolute-claude-bin-through-a-file-link claude absolute
    never_ran "$TARGET" || fail "CLAUDE_BIN=$SPELLING (link -> the file $TARGET): bootstrap probed $TARGET, which the caller's shell cannot run by that spelling"
    never_ran "$COMMON/claude"                                          # no fallback to the claude on PATH
    step="$(summary_value 'Claude executable')" || fail "no Claude executable step"
    case "$step" in FAILED*) ;; *) fail "Claude executable: $step, although CLAUDE_BIN=$SPELLING is unusable" ;; esac
    assert test "$LAST_STATUS" -ne 0
}

test_groupD_rr3c_relative_home_local_bin_entry_still_gets_the_zshrc_advice() {
    # RR3-3 (the last review's Astra [P3], review/rr3b.out): df80288d's advice check (bootstrap.sh:679) matches the caller's
    # PATH as given against "$HOME/.local/bin" as text, so with HOME=../home the entry ../home/.local/bin, spelled the same
    # way, suppresses the advice, though it reaches the wrapper at ~/.local/bin/codex only from a directory where ../home
    # leads to the home: once the user changes directory, typing codex no longer finds it. The ruling: only an absolute entry
    # suppresses the advice ($HOME/.local/bin only when $HOME starts with /, $BRIDGE_HOME/.local/bin as before), and no ~ is
    # expanded. Run from a sibling of the fake home with HOME=../home, and ../home/.local/bin the only .local/bin entry on
    # PATH: the advice line is printed. Only the advice is asserted, since a relative HOME may change what other steps report
    new_case groupD-rr3c-relative-home-zshrc-advice
    START_DIR="$CASE/work"; mkdir -p "$START_DIR"
    [ "$(cd "$START_DIR" && cd -P ../home && pwd)" = "$(cd -P "$FAKE_HOME" && pwd)" ] || fail "fixture: ../home from $START_DIR is not $FAKE_HOME"
    EXTRA_ENV+=("HOME=../home")                                         # env keeps the last HOME: this one, not $FAKE_HOME
    CALLER_PATH="../home/.local/bin:$BIN:$COMMON"                       # the relative entry; no absolute .local/bin
    host_guard
    run_bootstrap
    assert_host_guard_clean
    grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$OUTPUT" || fail "HOME=../home and PATH=../home/.local/bin:... from $START_DIR: no ~/.zshrc advice, although that relative entry stops reaching $FAKE_HOME/.local/bin/codex once the user changes directory"
    contains "$OUTPUT" 'Add this exact line to ~/\.zshrc'
}

# ==================================================================================================================
# The ChatGPT app moved its bundled CLI (fix/codex-dead-launcher, 2026-10-05). App build 26.928 no longer has
# Contents/Resources/codex, the single native binary; it ships a package directory, Contents/Resources/codex-cli, whose entry
# point bin/codex is a POSIX sh launcher in front of the native binary inside a nested CodexCLI.app. The wrapper the
# September bootstrap wrote at ~/.local/bin/codex execs the old path, so after the app update it fails on every call, and
# bootstrap, which knew that one path only, told a Mac whose only Codex is the app's "Codex is unavailable" (or installed a
# second Codex with npm) although a working CLI sat in the app. Bootstrap now derives the new location from the folder of
# the legacy one, so BRIDGE_APP_LAUNCHER moves both and every fixture below stays inside its case, and considers it first:
# newest first, the engine's order. A dead wrapper here execs the case's own legacy path, removed; nothing names a path
# outside the case, and the host guard is on in every scenario
# ==================================================================================================================
sep_era_wrapper_to() {
    # The owned wrapper the bootstrap of September 2026 left at ~/.local/bin/codex, exec'ing $1: write_shim's three lines
    # with that bootstrap's comment (owned_wrapper_to writes today's), which wrapper_target accepts by its prefix
    mkdir -p "$FAKE_HOME/.local/bin"
    "$PYTHON" - "$1" > "$FAKE_HOME/.local/bin/codex" <<'PY'
import shlex, sys
print('#!/bin/sh')
print('# codex shim (claude-codex-bridge bootstrap): runs the bundled Codex CLI')
print('exec ' + shlex.quote(sys.argv[1]) + ' "$@"')
PY
    chmod 755 "$FAKE_HOME/.local/bin/codex"
}

app_cli_layout() {
    # The app's package layout, beside the place of the legacy launcher $APP (left as new_case made it: a scenario without
    # it removes it). APP_CLI is the entry point <Resources>/codex-cli/bin/codex, the vendor's launcher text verbatim (it
    # resolves links on $0 with dirname and readlink, then execs ../CodexCLI.app/Contents/MacOS/codex); APP_CLI_BINARY is the
    # native stub behind it. Only the stub is in the native list, so the `file` fixture calls the launcher a shell script,
    # as file(1) calls the real one
    APP_CLI="$(dirname "$APP")/codex-cli/bin/codex"
    APP_CLI_BINARY="$(dirname "$APP")/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    mkdir -p "$(dirname "$APP_CLI")" "$(dirname "$APP_CLI_BINARY")"
    make_native "$APP_CLI_BINARY"
    cat > "$APP_CLI" <<'LAUNCHER'
#!/bin/sh
set -eu
entry="$0"
while [ -L "$entry" ]; do
  parent=$(CDPATH= cd -P -- "$(dirname -- "$entry")" && pwd)
  entry=$(readlink "$entry")
  case "$entry" in /*) ;; *) entry="$parent/$entry" ;; esac
done
bin_dir=$(CDPATH= cd -P -- "$(dirname -- "$entry")" && pwd)
exec "$bin_dir/../CodexCLI.app/Contents/MacOS/codex" "$@"
LAUNCHER
    chmod 755 "$APP_CLI"
    # the launcher shipped in app builds 26.928.31416 and 26.930.31730, byte for byte (328 bytes)
    [ "$(hash_file "$APP_CLI")" = 50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8 ] ||
        fail "fixture: $APP_CLI no longer holds the vendor's launcher text"
}

typing_codex() {
    # `codex --version` typed in a fresh shell that reaches ~/.local/bin/codex: prints what it printed; non-zero when the
    # file there cannot run
    env -i HOME="$FAKE_HOME" PATH="$COMMON" TEST_LOG="$LOG" "$FAKE_HOME/.local/bin/codex" --version 2>&1
}

wrapper_runs_the_app_cli() {
    # The owned wrapper at ~/.local/bin/codex execs the app's new launcher by its real path, and typing codex works
    local version
    shim_exec_line_is "$(realpath_of "$APP_CLI")" || fail "the wrapper at ~/.local/bin/codex does not exec the app's new launcher $APP_CLI"
    version="$(typing_codex)" || fail "typing codex fails although the wrapper execs $APP_CLI: $version"
    case "$version" in codex-cli\ *) ;; *) fail "the wrapper printed no Codex version: $version" ;; esac
}

test_app_cli_incident_dead_september_wrapper_is_repointed() {
    # The incident, as on a Mac with no standalone Codex and no npm: the app has only the new layout and the September
    # wrapper execs the legacy path, which is gone, so typing codex fails. Discovery skips the wrapper and its target (the
    # legacy launcher's own path); the unfixed bootstrap then found nothing ('Codex executable' FAILED, every later step
    # skipped, the dead wrapper left first on PATH). The app's CLI at its new location is chosen through its launcher, the
    # wrapper is re-pointed at that launcher and runs again, and a second run finds it current and changes nothing
    local step
    new_case app-cli-incident
    app_cli_layout
    rm "$APP"                                                           # the app update removed the legacy binary ...
    sep_era_wrapper_to "$APP"                                           # ... which the September wrapper still execs
    rm "$COMMON/npm"                                                    # and there is no npm to install another codex
    if typing_codex > /dev/null; then fail "fixture: the September wrapper runs although its target $APP is gone"; fi
    host_guard
    run_bootstrap
    assert_host_guard_clean
    step="$(summary_value 'Codex executable')" || fail "no Codex executable step"
    [ "$step" = ok ] || { cat "$OUTPUT"; fail "Codex executable: $step, although the app's bundled CLI is usable at its new location $APP_CLI"; }
    chose "$APP_CLI" || fail "bootstrap did not choose the app's new launcher $APP_CLI"
    contains "$OUTPUT" 'validated ChatGPT app launcher'
    assert_calls native "$(realpath_of "$APP_CLI_BINARY")"              # every codex call reached the bundled CLI, through its launcher
    step="$(summary_value 'PATH shim')" || fail "no PATH shim step"
    [ "$step" = ok ] || fail "PATH shim: $step"
    wrapper_runs_the_app_cli                                            # the dead wrapper is re-pointed; typing codex works again
    step="$(summary_value 'Codex version')" || fail "no Codex version step"
    case "$step" in *FAILED*) fail "Codex version: $step" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
    success
    snapshot "$CASE/before"
    : > "$LOG"
    run_bootstrap; success                                              # the second run
    assert_host_guard_clean
    snapshot "$CASE/after"
    same_snapshot "$CASE/before" "$CASE/after"
    contains "$OUTPUT" 'wrapper already current'
    assert_calls no-mutations
}

test_app_cli_incident_installs_no_second_codex_with_npm() {
    # The same Mac with npm reachable. The unfixed bootstrap, blind to the new location, installed a second Codex with npm
    # and pointed the wrapper at that one. With the bundled CLI found and meeting the floor, npm never runs
    new_case app-cli-incident-npm
    app_cli_layout
    rm "$APP"
    sep_era_wrapper_to "$APP"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert_calls npm 0 || fail "bootstrap installed another Codex with npm although the app's bundled CLI at $APP_CLI is usable"
    never_ran "$NPM_TARGET"
    chose "$APP_CLI" || fail "bootstrap did not choose the app's new launcher $APP_CLI"
    wrapper_runs_the_app_cli
    success
}

test_app_cli_fresh_app_only_mac_gets_a_wrapper() {
    # A first install on a Mac whose only Codex is the app's, new layout, no wrapper yet. The launcher's folder is never on
    # PATH, so choosing the launcher at its new location must write the wrapper, as choosing the legacy one always did:
    # without that clause the bundled CLI is chosen and 'PATH shim' is skipped, leaving no codex to type
    local step
    new_case app-cli-fresh
    app_cli_layout
    rm "$APP" "$COMMON/npm"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    step="$(summary_value 'Codex executable')" || fail "no Codex executable step"
    [ "$step" = ok ] || { cat "$OUTPUT"; fail "Codex executable: $step, although the app's bundled CLI is usable at its new location $APP_CLI"; }
    chose "$APP_CLI" || fail "bootstrap did not choose the app's new launcher $APP_CLI"
    step="$(summary_value 'PATH shim')" || fail "no PATH shim step"
    [ "$step" = ok ] || fail "PATH shim: $step, although bootstrap chose the app's launcher, whose folder is never on PATH"
    assert test ! -L "$FAKE_HOME/.local/bin/codex"
    wrapper_runs_the_app_cli
    success
}

test_app_cli_check_would_ensure_the_wrapper_and_changes_nothing() {
    # --check on the incident's state, npm reachable: it reports the bundled CLI as the choice and says it would ensure the
    # wrapper onto the new launcher, not that Codex is unavailable or that it would install one with npm. It changes
    # nothing (the dead wrapper stays as it is, nothing inside the app is touched) and starts no CLI at all
    local step
    new_case app-cli-check
    app_cli_layout
    rm "$APP"
    sep_era_wrapper_to "$APP"
    snapshot "$CASE/before"
    snapshot "$CASE/app-before" "$CASE/ChatGPT.app"
    host_guard
    run_bootstrap --check
    assert_host_guard_clean
    snapshot "$CASE/after"
    snapshot "$CASE/app-after" "$CASE/ChatGPT.app"
    same_snapshot "$CASE/before" "$CASE/after"
    same_snapshot "$CASE/app-before" "$CASE/app-after"
    assert test ! -s "$LOG"                                             # no codex, no claude, no npm
    step="$(summary_value 'Codex executable')" || fail "no Codex executable step"
    [ "$step" = ok ] || { cat "$OUTPUT"; fail "Codex executable: $step, although the app's bundled CLI is at its new location $APP_CLI"; }
    grep -Fq "[check] would ensure wrapper $(realpath_of "$FAKE_HOME")/.local/bin/codex -> $(realpath_of "$APP_CLI")" "$OUTPUT" ||
        fail "--check does not say it would ensure the wrapper onto the app's new launcher $APP_CLI"
    not_contains "$OUTPUT" 'would install Codex with npm'
    not_contains "$OUTPUT" 'Codex is unavailable'
    success
}

test_app_cli_below_the_floor_takes_the_npm_branch() {
    # The floor rule is the legacy launcher's (test_groupD_npm_widening_when_no_codex_meets_the_floor): a bundled CLI below
    # the table's highest Codex floor is probed, through its launcher, and remembered as the fallback, and npm installs a
    # current Codex. Without npm the fallback is the choice: the wrapper runs it and 'Codex version' FAILS naming the app's
    # own upgrade (test_groupD_upgrade_hint_chatgpt_app)
    local step
    new_case app-cli-below-floor
    app_cli_layout
    rm "$APP"
    CODEX_VERSIONS="$(json_versions "$APP_CLI_BINARY" 0.155.0)"         # the only codex is the app's, below the floor
    host_guard
    run_bootstrap
    assert_host_guard_clean
    probed_with_version codex "$APP_CLI_BINARY" || fail "the app's CLI at its new location $APP_CLI was never considered"
    assert_calls npm 1
    chose "$NPM_TARGET" || fail "bootstrap did not choose the Codex npm installed"
    step="$(summary_value 'Codex version')" || fail "no Codex version step"
    case "$step" in *FAILED*) fail "Codex version: $step" ;; *0.156.1*) ;; *) fail "Codex version: $step" ;; esac
    success
    new_case app-cli-below-floor-no-npm
    app_cli_layout
    rm "$APP" "$COMMON/npm"
    CODEX_VERSIONS="$(json_versions "$APP_CLI_BINARY" 0.155.0)"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert test "$LAST_STATUS" -ne 0
    chose "$APP_CLI" || fail "bootstrap did not fall back to the app's new launcher $APP_CLI, its only capable codex"
    step_failed 'Codex version' || fail "Codex version: $(summary_value 'Codex version')"
    upgrade_hints_are app
    wrapper_runs_the_app_cli
}

test_app_cli_both_layouts_the_new_location_first() {
    # Both locations hold a usable CLI that meets the floor. The new one is considered first and chosen (the app's own
    # manifest names bin/codex; in a build holding both, the legacy binary is the leftover), and the legacy one is never
    # run. This pins the order, which is also the engine's
    new_case app-cli-both-layouts
    app_cli_layout                                                      # $APP, the legacy binary, is still there
    host_guard
    run_bootstrap
    assert_host_guard_clean
    chose "$APP_CLI" || fail "both locations hold a usable CLI: bootstrap did not choose the new one, $APP_CLI"
    never_ran "$APP" || fail "the legacy launcher $APP was run although the new location comes first and meets the floor"
    assert_calls npm 0
    wrapper_runs_the_app_cli
    success
}

test_app_cli_broken_new_launcher_falls_through_to_the_legacy_one() {
    # The new location is an ordinary candidate: its launcher is there but the binary it execs is not, so its --version
    # probe fails, it is rejected by name, and the legacy launcher, next in order, is chosen
    new_case app-cli-broken-new
    app_cli_layout
    rm "$APP_CLI_BINARY"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    grep -Fq "Rejected codex $(realpath_of "$APP_CLI"): " "$OUTPUT" ||
        fail "the new launcher $APP_CLI is present and broken, yet bootstrap did not consider and reject it"
    chose "$APP" || fail "bootstrap did not fall through to the legacy launcher $APP"
    shim_exec_line_is "$(realpath_of "$APP")" || fail "the wrapper does not exec the legacy launcher $APP"
    assert_calls npm 0
    success
}

test_app_cli_damaged_new_location_is_never_executed() {
    # The 2026-09-06 damage (test_rejected_launcher), at the new location: bootstrap's own marked wrapper sits where the
    # launcher belongs and execs its own path, and ~/.local/bin/codex is a symlink to it. Executed, it would exec itself
    # until the probe's ten-second limit. It is recognised by its marker and rejected at the nesting limit without being
    # run; nothing is written through the link into the app; npm installs a Codex and the link becomes a wrapper for it
    local before
    new_case app-cli-damaged
    app_cli_layout
    rm "$APP"
    owned_wrapper_to "$APP_CLI"                                         # bootstrap's wrapper, exec'ing the new launcher's path ...
    mv "$FAKE_HOME/.local/bin/codex" "$APP_CLI"                         # ... sits at that path: it execs itself
    ln -s "$APP_CLI" "$FAKE_HOME/.local/bin/codex"
    before="$(hash_file "$APP_CLI")"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    grep -Fq "Rejected stale wrapper $(realpath_of "$APP_CLI"): " "$OUTPUT" ||
        fail "bootstrap did not reject its own wrapper at the app's new location $APP_CLI"
    not_contains "$OUTPUT" 'exceeded 10 seconds'                        # it was never started
    never_ran "$APP_CLI_BINARY"
    assert test "$before" = "$(hash_file "$APP_CLI")"
    assert_calls npm 1
    assert_calls native "$NPM_TARGET"
    assert test ! -L "$FAKE_HOME/.local/bin/codex"
    shim_target_is "$NPM_TARGET"
    success
}

test_app_cli_explicit_codex_bin_repairs_the_dead_wrapper() {
    # CODEX_BIN names the app's new launcher on the incident's Mac. An explicit CODEX_BIN gets a wrapper only when it is
    # the app's launcher, and that now holds at either location: the unfixed bootstrap accepted this CODEX_BIN, skipped
    # 'PATH shim' and left the dead wrapper first on PATH
    local step
    new_case app-cli-explicit
    app_cli_layout
    rm "$APP"
    sep_era_wrapper_to "$APP"
    EXPLICIT_CODEX="$APP_CLI"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    contains "$OUTPUT" 'explicit CODEX_BIN'
    step="$(summary_value 'PATH shim')" || fail "no PATH shim step"
    [ "$step" = ok ] || fail "PATH shim: $step, although CODEX_BIN is the app's launcher at its new location: the dead wrapper stays first on PATH"
    wrapper_runs_the_app_cli
    assert_calls npm 0
    success
}

test_app_cli_symlink_at_the_shim_is_kept() {
    # A guard for what was deliberately left alone: the tests that keep the app's launcher from counting as the user's own
    # standalone still compare against the legacy binary only. A symlink at ~/.local/bin/codex to the new launcher (the
    # vendor's launcher resolves links on $0, so such a link works) is therefore a standalone found on PATH, and write_shim,
    # now reached because the chosen executable is the launcher, keeps it: it is never replaced by a wrapper, which would
    # exec the link's own path, that is, itself
    local step version
    new_case app-cli-symlink-at-shim
    app_cli_layout
    rm "$APP" "$COMMON/npm"
    mkdir -p "$FAKE_HOME/.local/bin"
    ln -s "$APP_CLI" "$FAKE_HOME/.local/bin/codex"
    host_guard
    run_bootstrap
    assert_host_guard_clean
    assert test -L "$FAKE_HOME/.local/bin/codex"
    assert test "$(readlink "$FAKE_HOME/.local/bin/codex")" = "$APP_CLI"
    assert test "$(find "$FAKE_HOME/.local/bin" -mindepth 1 | wc -l | tr -d ' ')" = 1   # no wrapper, no backup, no temporary file
    step="$(summary_value 'PATH shim')" || fail "no PATH shim step"
    case "$step" in FAILED*) fail "PATH shim: $step" ;; esac
    version="$(typing_codex)" || fail "typing codex fails through the kept link: $version"
    case "$version" in codex-cli\ *) ;; *) fail "the kept link printed no Codex version: $version" ;; esac
    success
}

test_app_cli_fresh_process_resolves_the_same_codex() {
    # test_groupD_fresh_process_resolves_the_same_codex for the new layout. It needs BOTH halves of the fix: bootstrap's
    # (this file's subject) and the engine's, skills/codex/scripts/bridge.py learning the same derived location in the same
    # order. A fresh process (a new terminal: no CODEX_BIN) resolves codex the way bridge.py does and gets the executable
    # bootstrap chose: with the new layout alone (the incident's Mac, after the repair), and with both layouts, where only
    # the same order, newest first, makes the two agree. The engine runs under the host guard as well
    local layout engine chosen
    for layout in new-only both; do
        new_case "app-cli-fresh-process-$layout"
        app_cli_layout
        if [ "$layout" = new-only ]; then rm "$APP"; sep_era_wrapper_to "$APP"; fi
        host_guard
        run_bootstrap; success
        assert_host_guard_clean
        chose "$APP_CLI" || fail "bootstrap did not choose the app's new launcher $APP_CLI ($layout)"
        engine="$CASE/engine"; mkdir -p "$engine"
        cp "$PLUGIN_SOURCE"/skills/codex/scripts/*.py "$engine/"
        cp "$MODELS_TABLE" "$CASE/models.json"
        chosen="$(cd "$CASE" && env -i HOME="$FAKE_HOME" PATH="$CALLER_PATH" TMPDIR="$CASE/tmp" BRIDGE_APP_LAUNCHER="$APP" \
            BRIDGE_MODELS_FILE="$CASE/models.json" BRIDGE_CODEX_FIXED_ROOT="$FIXED_ROOT" \
            TEST_LOG="$LOG" TEST_NATIVE_LIST="$NATIVE_LIST" TEST_CODEX_VERSIONS="$CODEX_VERSIONS" \
            PYTHONPATH="$PLUGIN_SOURCE/skills/codex/tests/fixtures/host_guard" HOST_GUARD_ROOTS="$CASE" \
            HOST_GUARD_LOG="$CASE/host-guard.jsonl" \
            "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); import bridge; print(bridge.resolve_codex() or "")' "$engine")"
        assert_host_guard_clean
        assert test -n "$chosen"
        [ "$(realpath_of "$chosen")" = "$(realpath_of "$APP_CLI")" ] ||
            fail "a fresh process resolves $chosen, not the app's new launcher $APP_CLI that bootstrap chose ($layout): bridge.py's resolve_codex() must consider the same location, in the same order"
    done
}

passed=0; failed=0
for scenario in \
    symlink_shim unrelated_shim_backup npm_symlink rejected_launcher toml_edit \
    check_read_only idempotent signed_out publish_dry_run relative_homes_and_auth \
    stale_owned_links_and_mcp unrelated_links_and_mcp unsafe_shim_parent text_mcp_fallback \
    validation_rollback concurrent_writer_preserved failed_unlink app_bundle_shim_parent \
    explicit_codex_priority failed_version_not_reexecuted missing_prerequisites claude_json_auth \
    sync_linker_not_invoked existing_check_read_only stale_text_registration_with_spaces \
    unrelated_command_preserved version_timeout writer_before_replace_preserved \
    existing_subtable_and_backup_retention script_launcher_on_path hanging_script_on_path_rejected \
    approval_overrides stale_transaction_preserves_fields stale_transaction_rollback multiline_toml_strings \
    unsafe_codex_destinations config_precedes_skill unsafe_optional_plugin_destinations \
    healthy_executable_at_shim disabled_owned_registration disabled_unrelated_registration \
    repaired_executable_reprobed stderr_warnings_on_success misleading_ownership_names \
    registration_json_uses_stdin optional_plugin_failures_nonfatal unset_account_environment \
    effective_config_verified bounded_interruption_cleanup transaction_interruption_rollback \
    replacement_boundary_conflict hardlink_publication_fallback explicit_invalid_codex_authoritative codex_capability_floor \
    codex_login_streams outer_bootstrap_signal_cleanup skill_ownership_rechecked_in_helper skill_final_boundary_conflict \
    skill_publication_interruption \
    shim_case_insensitive_ancestry claude_auth_response_uses_stdin check_does_not_execute_writing_clis \
    check_local_credentials_unknown bridge_env_forwarding standalone_install standalone_idempotent \
    standalone_upgrade standalone_unrelated_links standalone_check_read_only uninstall_owned_items \
    uninstall_check_read_only \
    groupD_versions_in_summary groupD_shim_targets_the_chosen_codex groupD_fresh_process_resolves_the_same_codex \
    groupD_default_fixed_dirs_beneath_the_fixed_root \
    groupD_npm_widening_when_no_codex_meets_the_floor groupD_explicit_binaries_and_old_claude_code_fail_their_steps \
    groupD_missing_models_json groupD_models_gate_forwarded groupD_package_zip_through_both_entry_points_without_relay \
    groupD_below_floor_executable_at_the_shim_fails_its_step \
    groupD_upgrade_hint_npm groupD_upgrade_hint_homebrew groupD_upgrade_hint_chatgpt_app groupD_upgrade_hint_installer \
    groupD_final_review_wrapper_execs_the_stable_discovered_path groupD_final_review_rediscovery_considers_the_wrappers_target \
    groupD_final_review_claude_floor_note_names_no_opus_version groupD_c7_claude_code_below_the_sonnet_floor \
    groupD_final_review_package_dirty_counts_only_the_bridge_folder \
    groupD_rereview_relative_claude_executable groupD_rereview_codex_through_a_relative_path_entry \
    groupD_rereview_keg_wrapper_moves_to_the_stable_link groupD_rereview_helpers_import_nothing_from_the_callers_directory \
    groupD_rr2_codex_bin_parent_path_from_a_symlinked_caller_directory \
    groupD_rr2_claude_bin_parent_path_from_a_symlinked_caller_directory \
    groupD_rr2_codex_bin_dot_dot_after_a_symlinked_sdk groupD_rr2_tilde_path_entry_holds_the_only_claude_and_codex \
    groupD_rr3_codex_bin_dot_dot_after_a_file_link_is_rejected groupD_rr3_claude_bin_dot_dot_after_a_file_link_is_rejected \
    groupD_rr3_tilde_local_bin_entry_still_gets_the_zshrc_advice \
    groupD_rr3b_absolute_codex_bin_dot_dot_after_a_file_link_is_rejected \
    groupD_rr3b_absolute_claude_bin_dot_dot_after_a_file_link_is_rejected \
    groupD_rr3c_relative_home_local_bin_entry_still_gets_the_zshrc_advice \
    app_cli_incident_dead_september_wrapper_is_repointed app_cli_incident_installs_no_second_codex_with_npm \
    app_cli_fresh_app_only_mac_gets_a_wrapper app_cli_check_would_ensure_the_wrapper_and_changes_nothing \
    app_cli_below_the_floor_takes_the_npm_branch app_cli_both_layouts_the_new_location_first \
    app_cli_broken_new_launcher_falls_through_to_the_legacy_one app_cli_damaged_new_location_is_never_executed \
    app_cli_explicit_codex_bin_repairs_the_dead_wrapper app_cli_symlink_at_the_shim_is_kept \
    app_cli_fresh_process_resolves_the_same_codex; do
    if [ -n "$FILTER" ] && ! printf '%s\n' "$scenario" | grep -Eiq -- "$FILTER"; then continue; fi
    (set -e; "test_$scenario") > "$TEST_ROOT/$scenario.log" 2>&1
    status=$?
    if [ "$status" -eq 0 ]; then
        printf 'PASS %s\n' "$scenario"
        passed=$((passed + 1))
    else
        printf 'FAIL %s\n' "$scenario"
        cat "$TEST_ROOT/$scenario.log"
        failed=$((failed + 1))
    fi
done
printf 'bootstrap tests: %s passed, %s failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]

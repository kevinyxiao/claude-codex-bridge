#!/usr/bin/env bash
# Bootstrap the local Claude <-> Codex bridge; Bash 3.2 / Python 3.9 compatible.
# Usage: ./bootstrap.sh [--standalone] [--no-claude-links] [--no-plugin] [--check] [--uninstall]
# --check reports readiness without installing, linking, or editing anything.
set -u

CHECK=0; PLUGIN=1; FORCE_STANDALONE=0; CLAUDE_LINKS=1; UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --no-plugin) PLUGIN=0 ;;
    --standalone) FORCE_STANDALONE=1 ;;
    --no-claude-links) CLAUDE_LINKS=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) printf 'Usage: bootstrap.sh [--standalone] [--no-claude-links] [--no-plugin] [--check] [--uninstall]\n'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# Hard prerequisites precede even path resolution (and never import local code).
export PYTHONDONTWRITEBYTECODE=1
MISSING=0
PREREQUISITES=(python3)
if [ "$UNINSTALL" = 0 ]; then PREREQUISITES+=(file); fi
for prerequisite in "${PREREQUISITES[@]}"; do
  if ! command -v "$prerequisite" >/dev/null 2>&1; then
    printf 'FAILED: install %s, then rerun bootstrap.sh.\n' "$prerequisite" >&2
    MISSING=1
  fi
done
if [ "$MISSING" != 0 ]; then
  printf '\nSummary:\n  Prerequisites: FAILED\n'
  for step in 'Model table' 'Codex executable' 'PATH shim' 'Codex version' 'Codex sign-in' 'Claude executable' 'Claude Code version' 'Claude sign-in' \
      'Claude links' 'Codex skill' 'MCP registration and settings' 'Optional OpenAI plugin'; do
    printf '  %s: skipped\n' "$step"
  done
  exit 1
fi
CALLER_PWD="$PWD"
# Every helper below, and every CLI `bounded` starts, runs from bootstrap's folder, so PATH is rewritten once, here, before
# anything inherits it, to what the caller's shell searches: a leading ~ (~ or ~/...) is $HOME (bash expands it in its own
# command search; a double-quoted export keeps it literal), an empty entry is the caller's directory, and any other relative
# entry is joined onto the caller's directory as it stands (never collapsing .. as text: a link may precede it). The caller's
# PATH as given is kept too: the ~/.zshrc advice below reads it, since zsh expands no ~ in PATH.
CALLER_RAW_PATH="$PATH"
HELPER_PATH=""; REMAINING="$PATH"
while :; do
  ENTRY="${REMAINING%%:*}"
  case "$ENTRY" in
    '~') ENTRY="$HOME" ;; '~/'*) ENTRY="$HOME${ENTRY#\~}" ;;
    ''|.) ENTRY="$CALLER_PWD" ;; ./*) ENTRY="$CALLER_PWD/${ENTRY#./}" ;; /*) ;; *) ENTRY="$CALLER_PWD/$ENTRY" ;;
  esac
  HELPER_PATH="${HELPER_PATH:+$HELPER_PATH:}$ENTRY"
  case "$REMAINING" in *:*) REMAINING="${REMAINING#*:}" ;; *) break ;; esac
done
PATH="$HELPER_PATH"; export PATH
ORIGINAL_PATH="$PATH"
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  SOURCE_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd -P)" || exit 1
  SOURCE="$(readlink "$SOURCE")" || exit 1
  case "$SOURCE" in /*) ;; *) SOURCE="$SOURCE_DIR/$SOURCE" ;; esac
done
HERE="$(cd -P "$(dirname "$SOURCE")" && pwd -P)" || exit 1
# Every Python helper bootstrap starts runs from this folder, never from the caller's directory: a `-c` or stdin script puts
# its working directory first on sys.path, so a socket.py or json.py in the directory bootstrap is run from (a project
# checkout) would be imported by every helper. This folder holds no top-level .py files and the helpers import the standard
# library only. Deliberately no -I: the caller's environment (PYTHONPATH, user site) is the parent shell's own and is trusted
# here as the bridge engine trusts it, and the test suite's host guard reaches the `bounded` helper, the one child that starts
# CLIs, through PYTHONPATH. The CLIs `bounded` starts inherit this folder as their directory. A relative path the caller gives
# (an account directory, a CODEX_BIN, a CLAUDE_BIN, a `command -v` result, a PATH entry) is resolved against the caller's
# directory explicitly, once, before any helper uses it, and as the caller's shell would: a leading ~ is $HOME, and .. is
# followed on the filesystem (real_path), never collapsed as text (os.path.abspath would cut off a link that precedes it).
py(){ ( cd "$HERE" && exec python3 "$@" ); }
real_path(){ py -c 'import os,sys; print(os.path.realpath(os.path.join(sys.argv[2], os.path.expanduser(sys.argv[1]))))' "$1" "$CALLER_PWD"; }
caller_spelling(){
  # $1 a relative CODEX_BIN or CLAUDE_BIN. CALLER_SPELLING is what the caller's shell would run: a leading ~ (~ or ~/...) is
  # $HOME, anything else is joined onto the caller's directory as plain text, so that the kernel walks it as the shell would (a
  # link is followed before a later .., and a .. after a link to a FILE fails with ENOTDIR, where realpath would carry on from
  # the file's directory). 0 when that spelling is an executable file; only then may real_path resolve it.
  case "$1" in '~') CALLER_SPELLING="$HOME" ;; '~/'*) CALLER_SPELLING="$HOME${1#\~}" ;; *) CALLER_SPELLING="$CALLER_PWD/$1" ;; esac
  [ -f "$CALLER_SPELLING" ] && [ -x "$CALLER_SPELLING" ]
}
# Normalize relative account directories against the caller's cwd.
BRIDGE_HOME="$(real_path "$HOME")" || exit 1
CALLER_SET_CODEX_HOME="${CODEX_HOME+set}"
CODEX_DEST="${CODEX_HOME:-$BRIDGE_HOME/.codex}"
CODEX_HOME="$(real_path "$CODEX_DEST")" || exit 1
# Normalised directories for OUR paths. Export the variables only when the caller had them set: Claude Code resolves
# credentials differently whenever CLAUDE_CONFIG_DIR is present (even at the default path), so exporting it would make a
# signed-in machine look signed out and break every nested `claude -p`.
CALLER_SET_CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR+set}"
CLAUDE_DEST="${CLAUDE_CONFIG_DIR:-$BRIDGE_HOME/.claude}"
CLAUDE_DIR="$(real_path "$CLAUDE_DEST")" || exit 1
if [ -n "$CALLER_SET_CLAUDE_CONFIG_DIR" ]; then CLAUDE_CONFIG_DIR="$CLAUDE_DIR"; export CLAUDE_CONFIG_DIR; else unset CLAUDE_CONFIG_DIR; fi
if [ -n "$CALLER_SET_CODEX_HOME" ]; then export CODEX_HOME; fi
APP_LAUNCHER="$(real_path "${BRIDGE_APP_LAUNCHER:-/Applications/ChatGPT.app/Contents/Resources/codex}")" || exit 1
# ChatGPT.app 26.928 moved its bundled CLI: that native binary is gone, and the entry point is codex-cli/bin/codex in the same
# folder, a shell launcher in front of the binary in a nested CodexCLI.app. Derived from the legacy path's folder, never
# spelled a second time, so BRIDGE_APP_LAUNCHER (one path: where the legacy binary would be) moves both. A variable of its own
# on purpose: validate_codex demands a native binary at APP_LAUNCHER and would reject this script as a damaged install.
APP_CLI_LAUNCHER="$(real_path "$(dirname "$APP_LAUNCHER")/codex-cli/bin/codex")" || exit 1
SHIM="$BRIDGE_HOME/.local/bin/codex"
MARKER='# codex shim (claude-codex-bridge bootstrap)'
# The engine's default fixed directories for a standalone Codex, looked up beneath BRIDGE_CODEX_FIXED_ROOT (unset or empty: the
# real directories), exactly as the engine's resolve_codex() does.
FIXED_ROOT="${BRIDGE_CODEX_FIXED_ROOT:-}"; FIXED_ROOT="${FIXED_ROOT%/}"
# The model table the engine ships (skills/codex/models.json): the highest Codex and Claude CLI floors of its current models decide
# which codex is chosen and whether the installed Claude Code can run the newest models. Read once; never edited.
MODELS_TABLE="$HERE/skills/codex/models.json"
CODEX_FLOOR=""; CLAUDE_FLOOR=""; TABLE_ERROR=""
read_model_table(){
  local floors status
  floors="$(py - "$MODELS_TABLE" <<'PY' 2>&1
import json, re, sys
path = sys.argv[1]
try:
    with open(path) as f:
        table = json.load(f)
    vendor_of = {family: vendor for vendor, families in table["families"].items() for family in families}
    floors = {"codex": [], "claude": []}
    for model_id, record in table["models"].items():
        floor = record.get("min_cli")
        if floor is None:
            continue
        if not isinstance(floor, str) or not re.fullmatch(r"\d+\.\d+\.\d+", floor):
            raise ValueError("min_cli of %s is not X.Y.Z" % model_id)
        floors[vendor_of[record["family"]]].append(floor)
    key = lambda v: tuple(int(p) for p in v.split("."))
    print("codex=" + (max(floors["codex"], key=key) if floors["codex"] else ""))
    print("claude=" + (max(floors["claude"], key=key) if floors["claude"] else ""))
except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
    sys.exit("model table %s: %s" % (path, exc))
PY
)"; status=$?
  if [ "$status" -ne 0 ]; then TABLE_ERROR="$floors"; return 1; fi
  CODEX_FLOOR="$(printf '%s\n' "$floors" | sed -n 's/^codex=//p')"
  CLAUDE_FLOOR="$(printf '%s\n' "$floors" | sed -n 's/^claude=//p')"
}
version_verdict(){
  # $1 vendor (codex|claude), $2 the CLI's --version output, $3 the floor X.Y.Z (empty: any readable version passes). Prints
  # `ok <version>`, `old <version>` or `unreadable`, with the engine's rule: a prerelease sorts below its release.
  py - "$1" "$2" "$3" <<'PY'
import re, sys
vendor, text, floor = sys.argv[1:4]
patterns = {"codex": r"^codex(?:-cli)?\s+v?(\d+)\.(\d+)\.(\d+)(?:-([0-9a-z.]+))?",
            "claude": r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9a-z.]+))?\s+\(Claude Code\)"}
lines = text.strip().splitlines()
match = re.match(patterns[vendor], lines[0].strip()) if lines else None
if not match:
    print("unreadable")
    sys.exit(0)
major, minor, patch, pre = match.groups()
def key(parts, pre):
    pre_key = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return tuple(int(p) for p in parts) + ((1, ()) if not pre else (0, pre_key))
version = ".".join((major, minor, patch)) + ("-" + pre if pre else "")
meets = not floor or key((major, minor, patch), pre) >= key(floor.split("."), None)
print(("ok " if meets else "old ") + version)
PY
}
first_line_of(){
  # The first line of a captured CLI's stdout (BOUNDED_OUTPUT after `bounded --capture`), never its stderr.
  printf '%s' "$1" | py -c 'import json, sys
lines = json.load(sys.stdin)["stdout"].strip().splitlines()
print(lines[0].strip() if lines else "")'
}
if [ "$FORCE_STANDALONE" = 1 ]; then
  INSTALL_MODE=STANDALONE
elif [ -f "$HERE/../link.sh" ]; then
  INSTALL_MODE=REPOSITORY
else
  PLUGIN_PARENT="$(real_path "$CLAUDE_DIR/plugins")" || exit 1
  case "$HERE/" in "$PLUGIN_PARENT/"*) INSTALL_MODE=MARKETPLACE ;; *) INSTALL_MODE=STANDALONE ;; esac
fi
STEPS=(); FAILED=0
ok(){ printf '  ok: %s\n' "$*"; }
warn(){ printf '  ! %s\n' "$*"; }
record(){ STEPS[${#STEPS[@]}]="$1: $2"; case "$2" in FAILED*) FAILED=1 ;; esac; }
upgrade_hint(){
  # L4: how to upgrade the Codex CLI at $1, chosen from the executable's REAL path, with the engine's rules (bridge_models.py
  # upgrade_hint): under /node_modules/@openai/codex/ npm's package (also when npm's prefix is Homebrew's); under a Homebrew
  # prefix (/opt/homebrew/, /usr/local/Cellar/, a Caskroom) the formula or cask; inside ChatGPT.app the app; under
  # <CODEX_HOME>/packages/standalone/ (OpenAI's installer, linked from ~/.local/bin/codex) or anywhere else OpenAI's installer.
  local real
  real="$(real_path "$1")" || real="$1"
  case "$real" in
    */node_modules/@openai/codex/*) printf '%s\n' 'npm install -g @openai/codex@latest' ;;
    */opt/homebrew/*|*/usr/local/Cellar/*|*/Caskroom/*) printf '%s\n' 'brew upgrade codex' ;;
    */ChatGPT.app/*) printf '%s\n' 'update the ChatGPT app' ;;
    *) printf '%s\n' 'curl -fsSL https://chatgpt.com/codex/install.sh | sh' ;;
  esac
}

# Wait in the outer shell so its traps can forward termination to the supervisor,
# including commands whose output is captured (command substitution hides PIDs).
ACTIVE_BOUND_PID=""; ACTIVE_CAPTURE=""; BOUNDED_OUTPUT=""
BOUND_STARTING=0; PENDING_SIGNAL=""; PENDING_STATUS=0; BOUND_CLAUDE=0
terminate_bootstrap(){
  local sig status running running_pid
  sig="$1"; status="$2"
  if [ "$BOUND_STARTING" = 1 ]; then
    PENDING_SIGNAL="$sig"; PENDING_STATUS="$status"; return
  fi
  trap '' INT TERM
  if [ -n "$ACTIVE_BOUND_PID" ]; then
    # Bash can consume a signal in the background child's pre-exec window.
    # Retry while its own job table still records our supervisor as running.
    # The supervisor handles repeated signals idempotently during cleanup.
    while :; do
      running=0
      for running_pid in $(jobs -pr); do
        if [ "$running_pid" = "$ACTIVE_BOUND_PID" ]; then running=1; break; fi
      done
      [ "$running" = 1 ] || break
      kill -"$sig" "$ACTIVE_BOUND_PID" 2>/dev/null || :
      sleep 0.02
    done
    wait "$ACTIVE_BOUND_PID" 2>/dev/null || :
  fi
  [ -z "$ACTIVE_CAPTURE" ] || rm -f "$ACTIVE_CAPTURE"
  exit "$status"
}
trap 'terminate_bootstrap INT 130' INT
trap 'terminate_bootstrap TERM 143' TERM

# Clean up the whole process group on timeout, interruption, or normal exit.
bounded(){
  local status captured
  captured=0; BOUNDED_OUTPUT=""
  if [ "${2:-}" = --capture ]; then
    ACTIVE_CAPTURE="$(mktemp "${TMPDIR:-/tmp}/bridge-capture.XXXXXXXX")" || return 1
    exec 3>"$ACTIVE_CAPTURE"
    exec 4<"$ACTIVE_CAPTURE"
    rm -f "$ACTIVE_CAPTURE"; ACTIVE_CAPTURE=""; captured=1
  fi
  BOUND_STARTING=1
  # The helper is exec'd by the subshell, so $! is its pid and a signal reaches it; it and the CLI it starts run from bootstrap's
  # folder (see py(): no -I, so the environment's PYTHONPATH reaches this helper).
  ( cd "$HERE" && export BRIDGE_BOOTSTRAP_CLAUDE="${BOUND_CLAUDE:-0}" && exec python3 - "$@" ) >&3 <<'PY' &
import json, os, signal, subprocess, sys, time
child = None
interrupted = 0
capture = sys.argv[2] == '--capture'
command = sys.argv[3:] if capture else sys.argv[2:]
environment = os.environ.copy()
if environment.pop('BRIDGE_BOOTSTRAP_CLAUDE', '0') == '1':
    for key in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_ENTRYPOINT',
                'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_PID', 'CLAUDE_EFFORT'):
        environment.pop(key, None)

def interrupt(signum, frame):
    global interrupted
    if interrupted:
        return
    interrupted = signum
    # Defer an interruption during Popen until its child can be reaped.
    if child is not None:
        raise SystemExit(128 + signum)

def cleanup():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            child.poll()
            os.killpg(child.pid, 0)
            time.sleep(0.02)
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        child.wait()

signal.signal(signal.SIGINT, interrupt)
signal.signal(signal.SIGTERM, interrupt)
try:
    if interrupted:
        sys.exit(128 + interrupted)
    child = subprocess.Popen(command, start_new_session=True, env=environment,
                             stdout=subprocess.PIPE if capture else None,
                             stderr=subprocess.PIPE if capture else None)
    if interrupted:
        sys.exit(128 + interrupted)
    try:
        if capture:
            stdout, stderr = child.communicate(timeout=float(sys.argv[1]))
            status = child.returncode
            print(json.dumps({'stdout': stdout.decode('utf-8', errors='replace'),
                              'stderr': stderr.decode('utf-8', errors='replace'),
                              'status': status}))
        else:
            status = child.wait(timeout=float(sys.argv[1]))
    except subprocess.TimeoutExpired:
        print("Command timed out: " + command[0], file=sys.stderr)
        sys.exit(124)
    sys.exit(status if status >= 0 else 128 - status)
except OSError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
finally:
    cleanup()
PY
  ACTIVE_BOUND_PID=$!
  BOUND_STARTING=0
  if [ -n "${PENDING_SIGNAL:-}" ]; then terminate_bootstrap "$PENDING_SIGNAL" "$PENDING_STATUS"; fi
  wait "$ACTIVE_BOUND_PID"; status=$?
  ACTIVE_BOUND_PID=""
  if [ "$captured" = 1 ]; then
    BOUNDED_OUTPUT="$(cat <&4)"
    exec 4<&-
  fi
  return "$status"
} 3>&1
is_wrapper(){ [ -f "$1" ] && LC_ALL=C grep -qF "$MARKER" "$1"; }
wrapper_target(){
  # The path the owned wrapper $1 execs, as written; 1 unless the wrapper has exactly the static shape write_shim writes
  # (including the old comment).
  py - "$1" <<'PY'
import pathlib, shlex, sys
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
if len(lines) != 3 or lines[0] != '#!/bin/sh' or not lines[1].startswith('# codex shim (claude-codex-bridge bootstrap)'):
    sys.exit(1)
parts = shlex.split(lines[2])
if len(parts) != 3 or parts[0] != 'exec' or parts[2] != '$@' or not parts[1].startswith('/'):
    sys.exit(1)
# Require shell-safe, literal quoting of the target, never expansions.
quoted = shlex.quote(parts[1])
if lines[2] not in ('exec ' + quoted + ' "$@"', 'exec "' + parts[1] + '" "$@"'):
    sys.exit(1)
if lines[2] != 'exec ' + quoted + ' "$@"' and any(c in parts[1] for c in '$`\\"'):
    sys.exit(1)
print(parts[1])
PY
}
REJECTED=(); VALID=(); TARGETS=(); VERSIONS=()
# The only executable validator: resolve links, classify, validate wrapper target,
# then probe --version (its first line is kept: the model floors are checked against it).
# Cache identities so an npm repair gets a fresh probe.
validate_codex(){
  local candidate real identity kind target index depth version_line
  candidate="$1"; depth="${2:-0}"; VALIDATION_REASON="previously rejected candidate"; VALIDATED_VERSION=""
  [ -n "$candidate" ] || { VALIDATION_REASON="CODEX_BIN is empty"; return 1; }
  # The candidate AS GIVEN must be an executable file before real_path resolves it: every source (explicit, PATH, wrapper
  # target, fixed location) is exec'd by that spelling, and the kernel walks it as the shell would (a .. after a link to a
  # FILE fails with ENOTDIR, where realpath would carry on from the file's directory and land on a target the caller's shell
  # cannot run). One that fails is rejected here, never probed, and not cached: no identity is resolved for it.
  if [ ! -f "$candidate" ] || [ ! -x "$candidate" ]; then
    VALIDATION_REASON="missing or not a regular executable as spelled: $candidate"; return 1
  fi
  real="$(real_path "$candidate")" || return 1
  identity="$(py - "$real" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    info = os.stat(path)
    print(json.dumps([path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]))
except OSError:
    print(json.dumps([path, None]))
PY
)" || return 1
  for index in "${REJECTED[@]-}"; do [ "$index" != "$identity" ] || return 1; done
  index=0
  while [ "$index" -lt "${#VALID[@]}" ]; do
    if [ "${VALID[$index]}" = "$identity" ] && ! is_wrapper "$real"; then
      VALIDATED_PATH="$real"; VALIDATED_TARGET="${TARGETS[$index]}"; VALIDATED_VERSION="${VERSIONS[$index]}"; return 0
    fi
    index=$((index + 1))
  done
  target="$real"; version_line=""
  if [ ! -f "$real" ] || [ ! -x "$real" ] || [ "$depth" -gt 8 ]; then
    VALIDATION_REASON="missing, not a regular executable, or wrapper nesting exceeds 8: $real"
    REJECTED[${#REJECTED[@]}]="$identity"; return 1
  fi
  kind="$(LC_ALL=C file -b "$real" 2>/dev/null)" || kind=""
  case "$kind" in
    *Mach-O*|*ELF*) ;;
    *)
      if [ "$real" = "$APP_LAUNCHER" ]; then
        # The app's bundled launcher must be a native binary; a script there is incident damage. Never execute it.
        VALIDATION_REASON="the app launcher is not a Mach-O/ELF binary (damaged install)"
        warn "Rejected codex $real: $VALIDATION_REASON."
        REJECTED[${#REJECTED[@]}]="$identity"; return 1
      elif is_wrapper "$real"; then
      VALIDATION_REASON="invalid bootstrap wrapper structure: $real"
      target="$(wrapper_target "$real")" || { REJECTED[${#REJECTED[@]}]="$identity"; return 1; }
      if ! validate_codex "$target" "$((depth + 1))"; then
        warn "Rejected stale wrapper $real: its target is unusable."
        return 1 # The wrapper target may be repaired independently.
      fi
      target="$VALIDATED_TARGET"
      else
        : # a script launcher (for example npm's codex.js) is acceptable if the timed --version probe below passes
      fi
      ;;
  esac
  if [ "$CHECK" != 1 ]; then
    if ! bounded 10 --capture "$real" --version; then
      VALIDATION_REASON="--version failed or exceeded 10 seconds"
      warn "Rejected codex $real: $VALIDATION_REASON."
      REJECTED[${#REJECTED[@]}]="$identity"; return 1
    fi
    version_line="$(first_line_of "$BOUNDED_OUTPUT")" || version_line=""
    if ! bounded 10 --capture "$real" exec --help || ! printf '%s' "$BOUNDED_OUTPUT" | py -c '
import json, re, sys
response = json.load(sys.stdin)
text = response["stdout"] + "\n" + response["stderr"]
flags = ("--json", "--output-schema", "--output-last-message", "--ignore-user-config")
sys.exit(0 if response["status"] == 0 and all(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text) for flag in flags) else 1)
'; then
      VALIDATION_REASON="exec --help failed, exceeded 10 seconds, or lacks required flags: --json --output-schema --output-last-message --ignore-user-config"
      warn "Rejected codex $real: $VALIDATION_REASON."
      REJECTED[${#REJECTED[@]}]="$identity"; return 1
    fi
  fi
  VALID[${#VALID[@]}]="$identity"; TARGETS[${#TARGETS[@]}]="$target"; VERSIONS[${#VERSIONS[@]}]="$version_line"
  VALIDATED_PATH="$real"; VALIDATED_TARGET="$target"; VALIDATED_VERSION="$version_line"
}
CHOSEN=""; CHOICE=""; CHOSEN_FROM=""; CHOSEN_VERSION=""; CHOSEN_STABLE=""
FIRST_CAPABLE=""; FIRST_CHOICE=""; FIRST_FROM=""; FIRST_VERSION=""; FIRST_STABLE=""
consider_codex(){
  # $1 a candidate, $2 how it was found, $3 where (path|shim|wrapper|launcher|fixed). Validates it; returns 0 when it is chosen:
  # it meets the highest Codex floor of the model table, or no floor is known (no table, or --check, which probes nothing). A
  # capable candidate below the floor is remembered as the fallback choice and 1 is returned, so the search goes on. CHOSEN is
  # the executable's real path (the CLI bootstrap runs and reports on); CHOSEN_STABLE the path it was discovered by, as given
  # (for example /opt/homebrew/bin/codex, a link into a versioned keg), which is what the owned wrapper execs.
  local verdict
  validate_codex "$1" || return 1
  if [ "$CHECK" = 1 ] || [ -z "$CODEX_FLOOR" ]; then
    CHOSEN="$VALIDATED_PATH"; CHOICE="$2"; CHOSEN_FROM="$3"; CHOSEN_VERSION="$VALIDATED_VERSION"; CHOSEN_STABLE="$1"; return 0
  fi
  verdict="$(version_verdict codex "$VALIDATED_VERSION" "$CODEX_FLOOR")" || verdict=unreadable
  case "$verdict" in
    ok\ *) CHOSEN="$VALIDATED_PATH"; CHOICE="$2"; CHOSEN_FROM="$3"; CHOSEN_VERSION="$VALIDATED_VERSION"; CHOSEN_STABLE="$1"; return 0 ;;
  esac
  if [ -z "$FIRST_CAPABLE" ]; then FIRST_CAPABLE="$VALIDATED_PATH"; FIRST_CHOICE="$2"; FIRST_FROM="$3"; FIRST_VERSION="$VALIDATED_VERSION"; FIRST_STABLE="$1"; fi
  return 1
}
discover_codex(){
  # Bootstrap's order: every codex on the caller's PATH, in order, skipping the app launcher and our own wrapper; a standalone
  # executable at ~/.local/bin/codex (that directory may not be on PATH yet); the executable the owned wrapper there runs, when
  # an earlier run left one (it can be outside PATH and the fixed directories; a Homebrew keg path is considered by its stable
  # <prefix>/bin/codex link when that exists); the ChatGPT app's launcher, at its new location (APP_CLI_LAUNCHER) and then at
  # the legacy one (APP_LAUNCHER); then /opt/homebrew/bin and /usr/local/bin beneath the fixed root. The app's two locations
  # come newest first, the engine's order: the app's manifest names codex-cli/bin/codex, so in a build holding both the legacy
  # binary is the leftover. Each is an ordinary candidate, and a new launcher that fails validation falls through to the
  # legacy one. The tests here and in write_shim that keep "the app launcher" from counting as the user's own standalone
  # compare against APP_LAUNCHER alone, the native binary: a link that resolves to the new launcher is a script launcher like
  # any other. It differs from the engine's resolve_codex(): the engine never considers the wrapper's target on its own, and
  # it considers the app launcher before ~/.local/bin/codex. The first candidate meeting the table's highest Codex floor wins;
  # when none does, CHOSEN is the first capable one (or empty) and 1 is returned.
  local remaining entry candidate resolved target link fixed
  CHOSEN=""; CHOICE=""; CHOSEN_FROM=""; CHOSEN_VERSION=""; CHOSEN_STABLE=""
  FIRST_CAPABLE=""; FIRST_CHOICE=""; FIRST_FROM=""; FIRST_VERSION=""; FIRST_STABLE=""
  remaining="$ORIGINAL_PATH"
  while :; do
    entry="${remaining%%:*}"                                            # absolute: PATH was rewritten at the top
    candidate="$entry/codex"
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
      resolved="$(real_path "$candidate")" || return 1
      if [ "$resolved" != "$APP_LAUNCHER" ] && ! is_wrapper "$resolved"; then
        if consider_codex "$candidate" "standalone executable on the caller's PATH" path; then return 0; fi
      fi
    fi
    case "$remaining" in *:*) remaining="${remaining#*:}" ;; *) break ;; esac
  done
  if [ -f "$SHIM" ] && [ -x "$SHIM" ] && [ "$(real_path "$SHIM")" != "$APP_LAUNCHER" ] && ! is_wrapper "$SHIM"; then
    if consider_codex "$SHIM" "standalone executable at ~/.local/bin/codex" shim; then return 0; fi
  fi
  if is_wrapper "$SHIM" && target="$(wrapper_target "$SHIM")"; then
    # A target inside a Homebrew keg (<prefix>/Cellar/codex/<version>/bin/codex, what an older bootstrap wrote) is considered
    # by its stable link <prefix>/bin/codex when that exists, so the rewritten wrapper survives `brew upgrade codex`.
    case "$target" in
      */Cellar/codex/*/bin/codex)
        link="${target%%/Cellar/codex/*}/bin/codex"
        if [ -f "$link" ] && [ -x "$link" ]; then target="$link"; fi ;;
    esac
    resolved="$(real_path "$target")" || return 1
    if [ "$resolved" != "$APP_LAUNCHER" ] && ! is_wrapper "$resolved"; then
      if consider_codex "$target" "standalone executable the wrapper at ~/.local/bin/codex runs" wrapper; then return 0; fi
    fi
  fi
  if consider_codex "$APP_CLI_LAUNCHER" "validated ChatGPT app launcher" launcher; then return 0; fi
  if consider_codex "$APP_LAUNCHER" "validated ChatGPT app launcher" launcher; then return 0; fi
  for fixed in "$FIXED_ROOT/opt/homebrew/bin/codex" "$FIXED_ROOT/usr/local/bin/codex"; do
    if [ -f "$fixed" ] && [ -x "$fixed" ] && [ "$(real_path "$fixed")" != "$APP_LAUNCHER" ] && ! is_wrapper "$fixed"; then
      if consider_codex "$fixed" "standalone executable in ${fixed%/codex}" fixed; then return 0; fi
    fi
  done
  if [ -n "$FIRST_CAPABLE" ]; then
    CHOSEN="$FIRST_CAPABLE"; CHOICE="$FIRST_CHOICE"; CHOSEN_FROM="$FIRST_FROM"; CHOSEN_VERSION="$FIRST_VERSION"; CHOSEN_STABLE="$FIRST_STABLE"
  fi
  return 1
}

# Check the original path as well as the normalized destination: realpath alone
# would hide an ancestor that traverses an outside directory or an app bundle.
destination_safe(){
  py - "$BRIDGE_HOME" "$CALLER_PWD" "$@" <<'PY'
import os, sys
home, caller, label = sys.argv[1:4]
try:
    for path in sys.argv[4:]:
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(caller, path)
        pending = path.split(os.sep)
        resolved, links = os.sep, 0
        while pending:
            part = pending.pop(0)
            if part in ('', '.'):
                continue
            if part == '..':
                resolved = os.path.dirname(resolved)
                continue
            if part.lower().endswith('.app'):
                raise ValueError('inside an .app bundle: ' + path)
            current = os.path.join(resolved, part)
            if os.path.islink(current):
                links += 1
                if links > 40:
                    raise ValueError('too many symlink ancestors: ' + path)
                target = os.readlink(current)
                if not os.path.isabs(target):
                    target = os.path.join(resolved, target)
                if (os.path.commonpath([home, os.path.abspath(target)]) != home or
                        os.path.commonpath([home, os.path.realpath(target)]) != home):
                    raise ValueError('symlinked ancestor points outside HOME: ' + current)
                pending = target.split(os.sep) + pending
                resolved = os.sep
            else:
                resolved = current
except (OSError, ValueError) as exc:
    sys.exit('Refusing ' + label + ': ' + str(exc))
PY
}

write_shim(){
  # The owned wrapper at ~/.local/bin/codex runs the executable bootstrap chose, by the path it was discovered by ($1,
  # CHOSEN_STABLE: /opt/homebrew/bin/codex rather than the keg behind it, so `brew upgrade codex` does not strand the wrapper).
  # Check the resolved parent even before mkdir, then operate from its physical directory. A symlink at codex is unlinked, never
  # opened for writing.
  local parent target
  target="$1"
  destination_safe 'shim' "$BRIDGE_HOME/.local/bin" || return 1
  parent="$(real_path "$BRIDGE_HOME/.local/bin")" || return 1
  case "$parent/" in "$BRIDGE_HOME/"*) ;; *) warn 'Refusing shim: resolved parent is outside HOME'; return 1 ;; esac
  if [ -f "$SHIM" ] && [ -x "$SHIM" ] && [ "$(real_path "$SHIM")" != "$APP_LAUNCHER" ] && ! is_wrapper "$SHIM"; then
    if validate_codex "$SHIM"; then
      # A user's own executable is never replaced or backed up. But typing `codex` runs it, so one below the table's floor
      # fails this step with the fix (Astra C4-1); nothing is probed in check mode, and without a table there is no floor.
      local verdict
      verdict=ok
      if [ "$CHECK" != 1 ] && [ -n "$CODEX_FLOOR" ]; then verdict="$(version_verdict codex "$VALIDATED_VERSION" "$CODEX_FLOOR")" || verdict=unreadable; fi
      case "$verdict" in
        ok*) ok "Kept healthy standalone executable: $SHIM"; return 0 ;;
        old*) warn "Kept your standalone executable $SHIM, but it is Codex ${verdict#old } and below the floor $CODEX_FLOOR of the newest models; typing codex runs it, not $target." ;;
        *) warn "Kept your standalone executable $SHIM, but its version cannot be read against the floor $CODEX_FLOOR; typing codex runs it, not $target." ;;
      esac
      warn "Upgrade it ($(upgrade_hint "$SHIM")), or put $(dirname "$target") first on PATH so that codex runs $target."
      return 1
    fi
  fi
  if [ "$CHECK" = 1 ]; then ok "[check] would ensure wrapper $SHIM -> $target"; return 0; fi
  mkdir -p "$parent" || return 1
  (
    cd -P "$parent" || exit 1
    [ "$(pwd -P)" = "$parent" ] || { warn 'Shim parent changed; refusing replacement.'; exit 1; }
    local tmp backup wrapper
    wrapper="$(py - "$target" <<'PY'
import shlex, sys
print('#!/bin/sh')
print('# codex shim (claude-codex-bridge bootstrap): runs the Codex CLI bootstrap chose')
print('exec ' + shlex.quote(sys.argv[1]) + ' "$@"')
PY
)" || exit 1
    # A rerun must not even create/unlink a temporary file when already current.
    if [ ! -L ./codex ] && [ -f ./codex ] && [ -x ./codex ] && printf '%s\n' "$wrapper" | cmp -s - ./codex; then
      ok "wrapper already current: $SHIM"; exit 0
    fi
    tmp="$(mktemp ./codex.tmp.XXXXXXXX)" || exit 1
    trap 'rm -f "$tmp"' EXIT
    printf '%s\n' "$wrapper" > "$tmp" && chmod 755 "$tmp" || exit 1
    if [ -L ./codex ]; then
      rm ./codex || { warn "Cannot remove shim symlink; refusing replacement."; exit 1; }
    elif [ -f ./codex ]; then
      if ! is_wrapper ./codex; then
        backup="codex.bak-$(py -c 'from datetime import datetime; print(datetime.now().strftime("%Y%m%d-%H%M%S-%f"))')-$$"
        [ ! -e "$backup" ] && [ ! -L "$backup" ] || exit 1
        mv ./codex "./$backup" || exit 1
        ok "Backed up unrelated executable to $parent/$backup"
      fi
    elif [ -e ./codex ]; then
      warn "Refusing to replace non-file shim: $SHIM"; exit 1
    fi
    mv -f "$tmp" ./codex || exit 1
    ok "installed wrapper $SHIM -> $target"
  )
}

codex_version_step(){
  # The chosen executable's version against the table's highest Codex floor: FAILED below it, with the fix. An explicit
  # CODEX_BIN is respected (never replaced) but reported the same way.
  local verdict shown hint
  if [ "$CHECK" = 1 ]; then record 'Codex version' 'unknown in check mode (probe skipped)'; return; fi
  if [ -z "$CODEX_FLOOR" ]; then record 'Codex version' "${CHOSEN_VERSION:-unreadable} (no floor to check: the model table is unavailable)"; return; fi
  verdict="$(version_verdict codex "$CHOSEN_VERSION" "$CODEX_FLOOR")" || verdict=unreadable
  case "$verdict" in
    ok\ *) record 'Codex version' "${verdict#ok }" ;;
    old\ *)
      hint="$(upgrade_hint "$CHOSEN")"
      warn "Codex ${verdict#old } at $CHOSEN is below $CODEX_FLOOR, the floor of the newest Codex models in the model table; those seats will be refused."
      warn "Upgrade: $hint, or point CODEX_BIN at a newer codex."
      record 'Codex version' "FAILED (${verdict#old } at $CHOSEN is below $CODEX_FLOOR; upgrade: $hint, or point CODEX_BIN at a newer codex)" ;;
    *)
      shown="$(printf '%.60s' "$CHOSEN_VERSION")"
      warn "Cannot read the Codex CLI version from '$shown'; set CODEX_BIN to a codex whose --version prints codex-cli X.Y.Z."
      record 'Codex version' "FAILED (unreadable version at $CHOSEN; set CODEX_BIN to a codex whose --version prints codex-cli X.Y.Z)" ;;
  esac
}

install_prerequisites(){
EXPLICIT_CODEX="${CODEX_BIN+set}"; CODEX_CURRENT=1
if [ -n "$EXPLICIT_CODEX" ]; then
  # A relative CODEX_BIN names what the caller's shell would run: a leading ~ is $HOME, the rest lies beneath the caller's
  # directory, and a link is followed before a .. that comes after it. It must be an executable file as spelled
  # (caller_spelling) before the helpers, which run elsewhere, get it resolved once, here, with real_path (never as text:
  # abspath would take ../ from the link's own parent, not from its target's). A spelling that fails is unusable like any
  # other: its resolved target is never probed, and there is no fallback.
  SPELLING_OK=1
  case "$CODEX_BIN" in
    ''|/*) ;;
    *) if caller_spelling "$CODEX_BIN"; then CODEX_BIN="$(real_path "$CALLER_SPELLING")" || CODEX_BIN=""
       else SPELLING_OK=0; VALIDATION_REASON="missing or not a regular executable as the caller's shell would run it: $CALLER_SPELLING"; fi ;;
  esac
  if [ "$SPELLING_OK" = 1 ] && validate_codex "$CODEX_BIN"; then
    CHOSEN="$VALIDATED_PATH"; CHOICE="explicit CODEX_BIN"; CHOSEN_FROM=explicit; CHOSEN_VERSION="$VALIDATED_VERSION"; CHOSEN_STABLE="$VALIDATED_TARGET"
  else warn "Explicit CODEX_BIN is unusable: $VALIDATION_REASON; no fallback will be attempted."
  fi
else
  if discover_codex; then CODEX_CURRENT=1; else CODEX_CURRENT=0; fi
fi
# No codex at all, or none meeting the floor (a capable old one is only a fallback): install the current one with npm and
# choose again. In check mode nothing is probed, so only an absent codex says what would be installed.
if [ -z "$EXPLICIT_CODEX" ] && [ "$CODEX_CURRENT" = 0 ] && command -v npm >/dev/null 2>&1; then
  if [ "$CHECK" = 1 ]; then
    [ -n "$CHOSEN" ] || warn '[check] would install Codex with npm install -g @openai/codex'
  else
    [ -z "$CHOSEN" ] || warn "Codex at $CHOSEN (${CHOSEN_VERSION:-unreadable version}) does not meet the model table's floor $CODEX_FLOOR; installing the current Codex with npm."
    if bounded 180 npm install -g @openai/codex; then
      hash -r
      if discover_codex; then CHOICE="validated npm installation ($CHOICE)"; fi
    else
      warn 'npm installation failed.'
    fi
  fi
fi
if [ -z "$CHOSEN" ]; then
  warn 'Codex is unavailable. Run this command, then rerun bootstrap:'
  printf '    npm install -g @openai/codex\n'
  if ! command -v npm >/dev/null 2>&1; then warn 'Install Node.js from https://nodejs.org first to get npm.'; fi
  record 'Codex executable' FAILED
  record 'PATH shim' skipped
  record 'Codex version' skipped
else
  # Use the validated target for every later operation, never rediscover codex.
  validate_codex "$CHOSEN" || exit 1
  CHOSEN="$VALIDATED_TARGET"
  if [ -z "$EXPLICIT_CODEX" ]; then export CODEX_BIN="$CHOSEN"; fi
  ok "Chose $CHOSEN ($CHOICE)."
  if [ "$CHECK" = 1 ]; then warn "[check] executable version and capability probes skipped; readiness is unverified."; fi
  record 'Codex executable' ok
  # The owned wrapper on PATH runs the chosen executable, by its stable discovered path (CHOSEN_STABLE): always for the app
  # launcher, at either of its locations (neither folder is ever on PATH), for a standalone in a fixed directory (which may
  # not be on PATH), and whenever an owned wrapper already exists, so it never keeps pointing at an older codex than the one
  # chosen. An explicit CODEX_BIN gets a wrapper only when it is the launcher, at either location.
  if [ "$CHOSEN" = "$APP_LAUNCHER" ] || [ "$CHOSEN" = "$APP_CLI_LAUNCHER" ] || { [ -z "$EXPLICIT_CODEX" ] && { [ "$CHOSEN_FROM" = fixed ] || is_wrapper "$SHIM"; }; }; then
    if write_shim "$CHOSEN_STABLE"; then record 'PATH shim' ok; else record 'PATH shim' FAILED; fi
  else
    record 'PATH shim' skipped
  fi
  codex_version_step
fi
# The advice is for the caller's interactive shell, zsh, which (unlike bash) never expands a ~ in PATH: only an absolute entry
# in the PATH as given (CALLER_RAW_PATH, not the rewritten one the helpers search) reaches the wrapper at ~/.local/bin/codex
# from every directory. So $HOME/.local/bin counts only when $HOME is absolute (with a relative HOME, ../home, that entry stops
# finding the wrapper once the user changes directory); $BRIDGE_HOME/.local/bin is always absolute. No ~ is expanded here.
case "$HOME" in /*) ABSOLUTE_HOME_BIN="$HOME/.local/bin" ;; *) ABSOLUTE_HOME_BIN="$BRIDGE_HOME/.local/bin" ;; esac
case ":$CALLER_RAW_PATH:" in
  *":$ABSOLUTE_HOME_BIN:"*|*":$BRIDGE_HOME/.local/bin:"*) ;;
  *) warn 'Add this exact line to ~/.zshrc, then open a new terminal:'
     printf '    export PATH="$HOME/.local/bin:$PATH"\n' ;;
esac
codex_command(){ bounded 30 "$CHOSEN" "$@"; }
# Credential inspection cannot confirm that a token is still valid. Never print
# its value, and never launch a CLI (even --version) during a read-only check.
check_auth(){
  local state
  state="$(py - "$1" "$CODEX_HOME" "$CLAUDE_DIR" "$BRIDGE_HOME" <<'PY'
import json, pathlib, sys
kind, codex, claude, home = sys.argv[1:]
state = 'unknown'
path = pathlib.Path(codex if kind == 'Codex' else claude) / ('auth.json' if kind == 'Codex' else '.credentials.json')
try:
    data = json.loads(path.read_text())
    if not isinstance(data, dict): raise ValueError('expected object')
    groups = ('tokens',) if kind == 'Codex' else ('claudeAiOauth',)
    keys = ('OPENAI_API_KEY', 'apiKey') if kind == 'Codex' else ('ANTHROPIC_API_KEY', 'apiKey')
    token_keys = ('access_token', 'refresh_token', 'id_token', 'accessToken', 'refreshToken')
    present = any(isinstance(data.get(key), str) and data[key].strip() for key in keys)
    present = present or any(isinstance(data.get(group), dict) and any(
        isinstance(data[group].get(key), str) and data[group][key].strip() for key in token_keys) for group in groups)
    state = 'present' if present else 'unknown'
except (OSError, ValueError, UnicodeError):
    pass
if state == 'unknown' and kind == 'Claude':
    # Account metadata can indicate keychain-backed login, but cannot establish
    # that its secret remains usable without a live probe.
    for marker in (pathlib.Path(claude) / '.claude.json', pathlib.Path(home) / '.claude.json'):
        try:
            account = json.loads(marker.read_text())
            if isinstance(account, dict) and isinstance(account.get('oauthAccount'), dict) and account['oauthAccount']:
                state = 'keychain'; break
        except (OSError, ValueError, UnicodeError):
            pass
print(state)
PY
)" || state=unknown
  if [ "$state" = present ]; then
    ok "[check] $1 credentials present; live sign-in probe skipped."
    record "$1 sign-in" 'credentials present (live probe skipped)'
  else
    if [ "$state" = keychain ]; then ok '[check] Claude keychain-backed account marker present; credentials cannot be verified locally.'; fi
    warn "[check] $1 sign-in unknown in check mode; run without --check to probe (live probe skipped)."
    record "$1 sign-in" 'unknown in check mode; run without --check to probe (live probe skipped)'
  fi
}
if [ -n "$CHOSEN" ]; then
  if [ "$CHECK" = 1 ]; then
    check_auth Codex
  elif bounded 30 --capture "$CHOSEN" login status && printf '%s' "$BOUNDED_OUTPUT" | py -c '
import json, re, sys
response = json.load(sys.stdin)
streams = (response["stdout"], response["stderr"])
success = any(re.search(r"^\s*Logged in\b", stream, re.M) for stream in streams)
failure = any(re.search(r"^\s*Not logged in\b", stream, re.M) for stream in streams)
sys.exit(0 if response["status"] == 0 and success and not failure else 1)
'; then
    ok 'Codex login: signed in'; record 'Codex sign-in' ok
  else
    warn 'Codex is not signed in. Run:'; printf '    codex login\n'; record 'Codex sign-in' FAILED
  fi
else record 'Codex sign-in' skipped; fi

CLAUDE="${CLAUDE_BIN:-$(command -v claude || :)}"
# A relative CLAUDE_BIN names what the caller's shell would run (as CODEX_BIN above: ~ is $HOME, a link is followed before a
# later ..): it must be an executable file as spelled (caller_spelling) before real_path resolves it once, here, for the -f/-x
# checks and the probes, which run from bootstrap's folder; a spelling that fails is rejected, and the claude on PATH is not
# taken in its place. A `command -v` answer is absolute since PATH was rewritten at the top.
case "$CLAUDE" in
  ''|/*) ;;
  *) if caller_spelling "$CLAUDE"; then CLAUDE="$(real_path "$CALLER_SPELLING")" || CLAUDE=""
     else warn "CLAUDE_BIN is unusable: missing or not a regular executable as the caller's shell would run it: $CALLER_SPELLING; the claude on PATH is not taken in its place."; CLAUDE=""; fi ;;
esac
CLAUDE_VERSION_LINE=""
claude_command(){
  claude_bounded 30 "$@"
}
claude_version_probe(){
  # The executable probe, keeping the first line of `claude --version` for the floor check.
  claude_command --capture --version || return 1
  CLAUDE_VERSION_LINE="$(first_line_of "$BOUNDED_OUTPUT")" || CLAUDE_VERSION_LINE=""
}
claude_version_step(){
  # Claude Code against the table's highest Claude floor: below it, a family name resolves to an older model; FAILED with the fix.
  local verdict shown
  if [ "$CHECK" = 1 ]; then record 'Claude Code version' 'unknown in check mode (probe skipped)'; return; fi
  if [ -z "$CLAUDE_FLOOR" ]; then record 'Claude Code version' "${CLAUDE_VERSION_LINE:-unreadable} (no floor to check: the model table is unavailable)"; return; fi
  verdict="$(version_verdict claude "$CLAUDE_VERSION_LINE" "$CLAUDE_FLOOR")" || verdict=unreadable
  case "$verdict" in
    ok\ *) record 'Claude Code version' "${verdict#ok }" ;;
    old\ *)
      warn "Claude Code ${verdict#old } is below $CLAUDE_FLOOR, the floor of the newest Claude models in the model table (an older Claude Code resolves the family names to older models). Run: claude update"
      record 'Claude Code version' "FAILED (${verdict#old } is below $CLAUDE_FLOOR; run: claude update)" ;;
    *)
      shown="$(printf '%.60s' "$CLAUDE_VERSION_LINE")"
      warn "Cannot read the Claude Code version from '$shown'. Run: claude update, or set CLAUDE_BIN to a claude whose --version prints X.Y.Z (Claude Code)."
      record 'Claude Code version' 'FAILED (unreadable version; run: claude update)' ;;
  esac
}
warn_model_overrides(){
  # Ambient model overrides make an alias mean another model inside Claude Code (the in-process agents run on `model: opus`).
  # Names only: a value is never printed.
  local names name
  names="$(py -c 'import os
print(" ".join(sorted(k for k in os.environ if k in ("ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
                      or (k.startswith("ANTHROPIC_DEFAULT_") and k.endswith("_MODEL")))))')" || names=""
  for name in $names; do
    warn "$name is set in this shell: inside Claude Code an alias such as opus would run that model instead of the newest one (the bridge's own seats ignore it). Unset it before using the in-process agents."
  done
}
claude_bounded(){
  # Strip nesting markers in the supervisor's child environment, keeping the
  # caller's environment and the active supervisor PID in the outer shell.
  local BOUND_CLAUDE limit
  BOUND_CLAUDE=1
  limit="$1"; shift
  if [ "${1:-}" = --capture ]; then
    shift; bounded "$limit" --capture "$CLAUDE" "$@"
  else
    bounded "$limit" "$CLAUDE" "$@"
  fi
}
if [ -z "$CHOSEN" ]; then
  record 'Claude executable' skipped; record 'Claude Code version' skipped; record 'Claude sign-in' skipped; CLAUDE=""
elif [ -n "$CLAUDE" ] && [ -f "$CLAUDE" ] && [ -x "$CLAUDE" ] && { [ "$CHECK" = 1 ] || claude_version_probe; }; then
  record 'Claude executable' ok
  if [ "$CHECK" = 1 ]; then warn '[check] Claude executable probe skipped; readiness is unverified.'; fi
  claude_version_step
  warn_model_overrides
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    ok 'Claude auth: token/key present in the environment'; record 'Claude sign-in' ok
    if [ "$CHECK" = 1 ]; then ok '[check] Claude live sign-in probe skipped.'; fi
  elif [ "$CHECK" = 1 ]; then
    check_auth Claude
  elif claude_command --capture auth status && printf '%s' "$BOUNDED_OUTPUT" | py -c '
import json, sys
try:
    response = json.load(sys.stdin)
    auth = json.loads(response["stdout"])
    sys.exit(0 if response["status"] == 0 and isinstance(auth, dict) and auth.get("loggedIn") is True else 1)
except (ValueError, KeyError, TypeError):
    sys.exit(1)
' 2>/dev/null; then
    ok 'Claude auth: signed in'; record 'Claude sign-in' ok
  else
    warn 'Claude is not signed in. Run:'; printf '    claude auth login\n'; record 'Claude sign-in' FAILED
  fi
else
  warn 'Claude Code is unavailable. Run this command, then open a new terminal and rerun bootstrap:'
  printf '    curl -fsSL https://claude.ai/install.sh | bash\n'
  record 'Claude executable' FAILED; record 'Claude Code version' skipped; record 'Claude sign-in' skipped; CLAUDE=""
fi

}

# Refuse unsafe ancestry before creating skills or letting Codex edit its config.
config_safe(){
  destination_safe 'Codex configuration' "$CODEX_DEST" "$CODEX_HOME" || return 1
  py - "$CODEX_HOME" <<'PY'
import os, stat, sys
home = sys.argv[1]
for name in ('config.toml', 'config.toml.lock'):
    path = os.path.join(home, name)
    if os.path.lexists(path) and (os.path.islink(path) or not stat.S_ISREG(os.stat(path).st_mode)):
        sys.exit('Refusing non-regular or symlinked configuration: ' + path)
PY
}

# A path name is not ownership evidence. Check the installation it points into.
owned_bridge_root(){
  py - "$1" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
if (root / 'skills/codex/scripts/bridge.py').is_file() and any(
        (root / name).is_file() for name in ('codex-skills/claude/scripts/claude_mcp_server.py', 'skills/claude/scripts/claude_mcp_server.py')):
    sys.exit(0)
try:
    manifest = json.loads((root / '.claude-plugin/plugin.json').read_text())
    sys.exit(0 if isinstance(manifest, dict) and manifest.get('name') == 'claude-codex-bridge' else 1)
except (OSError, ValueError):
    sys.exit(1)
PY
}

ensure_codex_skill(){
  destination_safe 'Codex skill' "$CODEX_DEST/skills" "$CODEX_HOME/skills" || return 1
  local link resolved
  link="$CODEX_HOME/skills/claude"
  if [ -L "$link" ]; then
    resolved="$(real_path "$link")" || return 1
    if [ "$resolved" != "$HERE/codex-skills/claude" ]; then
      case "$resolved" in
        */codex-skills/claude) owned_bridge_root "${resolved%/codex-skills/claude}" || { warn "Leaving unrelated Codex skill untouched: $link"; return 1; } ;;
        */skills/claude) owned_bridge_root "${resolved%/skills/claude}" || { warn "Leaving unrelated Codex skill untouched: $link"; return 1; } ;;
        *) warn "Leaving unrelated Codex skill untouched: $link"; return 1 ;;
      esac
    fi
  fi
  bridge_link "$CODEX_HOME/skills/claude" "$HERE/codex-skills/claude" 'Codex skill'
}

bridge_link(){
  py - "$1" "$2" "$CHECK" "$3" "$UNINSTALL" "$HERE" "$MARKER" <<'PY'
import json, os, pathlib, signal, stat, sys, tempfile
link, target, check, label, uninstall, here, marker = sys.argv[1:]
removing = uninstall == '1'

# Never modify the unpacked installation, even if account directories overlap it.
if removing and os.path.commonpath([here, os.path.realpath(os.path.dirname(link))]) == here:
    print('  ! Left installation contents untouched: ' + link)
    sys.exit(0)

def link_identity(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    contents = None
    if label == 'PATH shim' and stat.S_ISREG(info.st_mode):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('Shim changed during inspection: ' + path)
            contents = stream.read()
    return (info.st_dev, info.st_ino, info.st_mode,
            os.readlink(path) if stat.S_ISLNK(info.st_mode) else None, contents)

def belongs_here(path, identity):
    if label == 'PATH shim':
        lines = (identity[4] or b'').splitlines()
        return stat.S_ISREG(identity[2]) and len(lines) > 1 and lines[1].startswith(marker.encode())
    return stat.S_ISLNK(identity[2]) and os.path.commonpath([here, os.path.realpath(path)]) == here

def verify_link():
    identity = link_identity(link)
    if identity is None:
        return None
    if removing:
        if not belongs_here(link, identity):
            sys.exit('CONFLICT: ' + label + ' ownership changed before removal: ' + link)
        if link_identity(link) != identity:
            sys.exit('CONFLICT: ' + label + ' changed before removal: ' + link)
        return identity
    if not stat.S_ISLNK(identity[2]):
        sys.exit('Leaving unrelated ' + label + ' untouched: ' + link)
    def verified():
        if link_identity(link) != identity:
            sys.exit('CONFLICT: ' + label + ' changed during ownership verification: ' + link)
        return identity

    resolved = os.path.realpath(link)
    suffix = next((name for name in ('/codex-skills/claude', '/skills/claude', '/skills/codex', '/skills/gpt', '/agents/opus-reviewer.md', '/agents/opus-consultant.md') if resolved.endswith(name)), None)
    if suffix is not None:
        root = pathlib.Path(resolved[:-len(suffix)])
        if (root / 'skills/codex/scripts/bridge.py').is_file() and any(
                (root / name).is_file() for name in ('codex-skills/claude/scripts/claude_mcp_server.py', 'skills/claude/scripts/claude_mcp_server.py')):
            return verified()
        try:
            manifest = json.loads((root / '.claude-plugin/plugin.json').read_text())
            if isinstance(manifest, dict) and manifest.get('name') == 'claude-codex-bridge':
                return verified()
        except (OSError, ValueError):
            pass
    sys.exit('Leaving unrelated ' + label + ' untouched: ' + link)

if removing:
    initial = link_identity(link)
    if initial is None or not belongs_here(link, initial):
        print('  ok: Left ' + ('absent' if initial is None else 'unrelated') + ' ' + label + ': ' + link)
        sys.exit(0)
verify_link()
if not removing and not (os.path.isfile(target) if target.endswith('.md') else os.path.isdir(target)):
    sys.exit('Missing bridge link source: ' + target)
if not removing and os.path.lexists(link):
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(target):
        print('  ok: ' + label + ' already current: ' + link)
        sys.exit(0)
    if not os.path.islink(link):
        sys.exit('Leaving unrelated ' + label + ' untouched: ' + link)
if check == '1':
    print('  ok: [check] would remove ' + label + ': ' + link if removing else '  ok: [check] would link ' + link + ' -> ' + target)
    sys.exit(0)
parent = os.path.dirname(link)
if not removing:
    # Force the requested mode only on newly created directories.
    missing = []
    ancestor = parent
    while not os.path.exists(ancestor):
        missing.append(ancestor); ancestor = os.path.dirname(ancestor)
    for directory in reversed(missing):
        try: os.mkdir(directory, 0o755)
        except FileExistsError: continue
        os.chmod(directory, 0o755)
# Keep moved-aside entries on failure; unconditional temporary-directory cleanup
# could otherwise destroy an unrelated entry that won the verification race.
tmp = tempfile.mkdtemp(prefix='.claude-link-', dir=parent)
staged, previous = os.path.join(tmp, 'claude'), os.path.join(tmp, 'previous')
previous_mask = None
try:
    if not removing:
        os.symlink(target, staged)
    verified = verify_link()
    # Deliver pending termination only after publication or exclusive recovery
    # has restored the live path and recovery artifacts have been reported.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    if verified is not None:
        try:
            os.replace(link, previous)
        except FileNotFoundError:
            sys.exit('CONFLICT: ' + label + ' disappeared after ownership verification: ' + link)
        displaced = link_identity(previous)
        if displaced != verified:
            sys.exit('CONFLICT: ' + label + ' changed after ownership verification: ' + link)
    if not removing:
        try:
            os.symlink(target, link)
        except FileExistsError:
            # Inspection is diagnostic only. Do not retry publication or replace
            # an intruder; final recovery can restore the old link only if absent.
            try:
                intruder = link_identity(link)
                detail = ('symlink -> ' + intruder[3] if intruder is not None and
                          stat.S_ISLNK(intruder[2]) else 'another filesystem entry')
            except OSError:
                detail = 'an entry that changed during inspection'
            sys.exit('CONFLICT: ' + label + ' publication blocked by ' + detail + ': ' + link)
    if os.path.lexists(previous):
        os.unlink(previous)
finally:
    try:
        # A move may have completed even if its caller was aborted before
        # returning. Recover from every exception, without clobbering new links
        # (including dangling ones) installed at the final boundary.
        if os.path.lexists(previous) and not os.path.lexists(link):
            try:
                if os.path.islink(previous):
                    os.symlink(os.readlink(previous), link)
                else:
                    fd = os.open(previous, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'rb') as reader:
                        info = os.fstat(reader.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError('Cannot automatically restore non-file entry')
                        contents = reader.read()
                    fd = os.open(link, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IMODE(info.st_mode))
                    with os.fdopen(fd, 'wb') as writer:
                        os.fchmod(writer.fileno(), stat.S_IMODE(info.st_mode))
                        writer.write(contents); writer.flush(); os.fsync(writer.fileno())
            except FileExistsError:
                pass
            except (OSError, ValueError) as exc:
                print('  ! ' + label + ' recovery FAILED: ' + str(exc) +
                      '; moved-aside link retained at ' + previous, file=sys.stderr)
            else:
                os.unlink(previous)
        if os.path.lexists(staged):
            os.unlink(staged)
        if os.path.lexists(previous):
            print('  ! Moved-aside ' + label + ' retained at ' + previous, file=sys.stderr)
        else:
            os.rmdir(tmp)
    finally:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
print('  ok: Removed ' + label + ': ' + link if removing else '  ok: linked ' + link + ' -> ' + target)
PY
}

claude_links(){
  local relative parent link_ok
  link_ok=1
  for relative in skills/codex skills/gpt agents/opus-reviewer.md agents/opus-consultant.md; do
    parent="${relative%%/*}"
    if destination_safe 'Claude links' "$CLAUDE_DEST" "$CLAUDE_DIR" "$CLAUDE_DEST/$parent" "$CLAUDE_DIR/$parent"; then
      bridge_link "$CLAUDE_DIR/$relative" "$HERE/$relative" 'Claude link' || link_ok=0
    else
      warn "Left unsafe Claude link destination: $CLAUDE_DIR/$relative"
      link_ok=0
    fi
  done
  [ "$link_ok" = 1 ]
}

ensure_claude_links(){
  local relative link_ok
  if [ "$CLAUDE_LINKS" = 0 ]; then
    ok 'Claude links skipped (--no-claude-links).'; record 'Claude links' skipped
  elif [ "$INSTALL_MODE" = REPOSITORY ]; then
    ok "Repository sync handles Claude-side links under $CLAUDE_DIR; bootstrap only verifies them."
    link_ok=1
    for relative in skills/codex skills/gpt agents/opus-reviewer.md agents/opus-consultant.md; do
      if [ -e "$CLAUDE_DIR/$relative" ]; then ok "present: $CLAUDE_DIR/$relative"
      else warn "Missing $CLAUDE_DIR/$relative; use the repository sync with CLAUDE_HOME=$CLAUDE_DIR."; link_ok=0; fi
    done
    if [ "$link_ok" = 1 ]; then record 'Claude links (repository sync)' ok; else record 'Claude links (repository sync)' FAILED; fi
  elif [ "$INSTALL_MODE" = MARKETPLACE ]; then
    ok 'Marketplace command: /claude-codex-bridge:codex (also /claude-codex-bridge:gpt).'
    record 'Claude links (marketplace owns them)' skipped
  else
    ok "Standalone install from $HERE. Keep this folder in place."
    if claude_links; then record 'Claude links (standalone)' ok; else record 'Claude links (standalone)' FAILED; fi
  fi
}

uninstall_mcp(){
  config_safe || return 1
  if py - "$CODEX_HOME" "$HERE" <<'PY'
import os, sys
sys.exit(0 if os.path.commonpath([os.path.realpath(sys.argv[1]), sys.argv[2]]) == sys.argv[2] else 1)
PY
  then
    ok "Left installation contents untouched: $CODEX_HOME/config.toml"; return 0
  fi
  patch_config
}

uninstall_bridge(){
  if claude_links; then record 'Claude links (uninstall)' ok; else record 'Claude links (uninstall)' FAILED; fi
  if destination_safe 'Codex skill' "$CODEX_DEST/skills" "$CODEX_HOME/skills" && \
      bridge_link "$CODEX_HOME/skills/claude" "$HERE/codex-skills/claude" 'Codex skill'; then
    record 'Codex skill (uninstall)' ok
  else record 'Codex skill (uninstall)' FAILED; fi
  if uninstall_mcp; then record 'MCP registration and settings (uninstall)' ok
  else record 'MCP registration and settings (uninstall)' FAILED; fi
  if destination_safe 'shim' "$BRIDGE_HOME/.local/bin" && bridge_link "$SHIM" "$APP_LAUNCHER" 'PATH shim'; then
    record 'PATH shim (uninstall)' ok
  else record 'PATH shim (uninstall)' FAILED; fi
}

SERVER="$HERE/codex-skills/claude/scripts/claude_mcp_server.py"
registration_state(){
  # Keep the response (which may contain credentials) on stdin, never in argv.
  local program
  IFS= read -r -d '' program <<'PY' || :
import json, os, pathlib, re, shlex, sys
kind, server = sys.argv[1:]

SERVER_SUFFIXES = ('/codex-skills/claude/scripts/claude_mcp_server.py', '/skills/claude/scripts/claude_mcp_server.py')

def owned(path):
    resolved = os.path.realpath(path)
    suffix = next((s for s in SERVER_SUFFIXES if resolved.endswith(s)), None)
    if suffix is None:
        return False
    root = pathlib.Path(resolved[:-len(suffix)])
    if (root / 'skills/codex/scripts/bridge.py').is_file() and any(
            (root / name).is_file() for name in ('codex-skills/claude/scripts/claude_mcp_server.py', 'skills/claude/scripts/claude_mcp_server.py')):
        return True
    try:
        manifest = json.loads((root / '.claude-plugin/plugin.json').read_text())
        return isinstance(manifest, dict) and manifest.get('name') == 'claude-codex-bridge'
    except (OSError, ValueError):
        return False

try:
    response = json.load(sys.stdin)
    text = response['stdout']
    if response['status'] != 0:
        missing = re.search(r'''(?:no|unknown) (?:mcp )?server (?:named )?["']?claude-bridge(?:["']|\b)|(?:mcp )?server ["']?claude-bridge["']? (?:not found|does not exist)''',
                            text + '\n' + response['stderr'], re.I)
        print('missing' if missing else 'unknown')
        sys.exit(0)
    if kind == 'json':
        data = json.loads(text)
        transport = data.get('transport', data)
        command, args = transport['command'], transport['args']
        enabled = data.get('enabled', True)
    else:
        fields = dict(re.findall(r'^[ \t]*(command|args|enabled):[ \t]*(.*?)[ \t]*$', text, re.M))
        command = fields['command']
        enabled = {'true': True, 'false': False}[fields.get('enabled', 'true').lower()]
        raw = fields['args']
        # Older Codex prints args joined with spaces, without shell quoting.
        legacy_path = raw.startswith('/') and raw.endswith('/claude/scripts/claude_mcp_server.py')
        args = [raw] if legacy_path else shlex.split(raw)
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError('unrecognized command/args')
    if not isinstance(enabled, bool):
        raise ValueError('unrecognized enabled setting')
    if re.fullmatch(r'(?:.*/)?python3(?:\.\d+)*', command) and len(args) == 1 and owned(args[0]):
        print('disabled' if not enabled else 'current' if command == 'python3' and args == [server] else 'stale')
    else:
        print('unrelated-disabled' if not enabled else 'unrelated')
except (ValueError, KeyError, TypeError, AttributeError):
    print('unknown')
PY
  py -c "$program" "$1" "$SERVER"
}

patch_config(){
  py - "$CODEX_HOME/config.toml" "$CHOSEN" "$CHECK" "$SERVER" "$UNINSTALL" "$HERE" <<'PY'
import datetime, errno, fcntl, hashlib, json, os, pathlib, re, signal, stat, subprocess, sys, tempfile
path = pathlib.Path(sys.argv[1])
check = sys.argv[3] == '1'
uninstall = len(sys.argv) > 5 and sys.argv[5] == '1'
installation = os.path.realpath(sys.argv[6]) if uninstall else None
bridge = ('mcp_servers', 'claude-bridge')
task = bridge + ('tools', 'claude_task')
required_env = ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY',
    'CODEX_HOME', 'CODEX_BIN', 'CLAUDE_BIN', 'BRIDGE_JOBS', 'BRIDGE_DEFAULT_SEAT',
    'BRIDGE_REVIEW_SEATS', 'BRIDGE_COUNCIL_SEATS', 'CLAUDE_BRIDGE_ALLOW_WRITE',
    'CLAUDE_BRIDGE_ROOT', 'CLAUDE_BRIDGE_ALLOW_ANY_CWD',
    'CLAUDE_BRIDGE_TOOL_DEADLINE', 'CLAUDE_BRIDGE_ENGINE', 'BRIDGE_MODELS_GATE')

def read_current():
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('config.toml is not a regular file')
        return stream.read(), stat.S_IMODE(os.fstat(stream.fileno()).st_mode)

def digest(data):
    return hashlib.sha256(data).digest()

class ConcurrentUpdate(ValueError):
    pass

def backup_name():
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = path.with_name(path.name + '.bak-' + stamp)
    if os.path.lexists(backup):
        raise ValueError('Backup already exists; refusing replacement: ' + str(backup))
    return backup

def replace_atomic(data, mode, expected, backup):
    fd, name = tempfile.mkstemp(prefix='.config.toml-', dir=str(path.parent))
    moved = False
    keep_staged = False
    use_hardlinks = True
    conflict = path.with_name(path.name + '.conflict-' + backup.name.split('.bak-', 1)[1])

    def restore_entry(source):
        if use_hardlinks:
            os.link(source, str(path), follow_symlinks=False)
        elif os.path.islink(source):
            os.symlink(os.readlink(source), str(path))
        else:
            # Recovery must preserve both its source and any newer live entry
            # even on the fallback filesystem. Create exclusively, never follow
            # a symlink; retain the backup if copying itself fails.
            reader = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(reader, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Cannot restore non-regular configuration: ' + source)
                contents = stream.read()
            writer = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(info.st_mode))
            with os.fdopen(writer, 'wb') as stream:
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                stream.write(contents)
                stream.flush(); os.fsync(stream.fileno())

    def keep_ours():
        # An independent copy also survives an in-place writer on the staged
        # inode after publication.
        with conflict.open('xb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush(); os.fsync(stream.fileno())

    def report_conflict(restore_displaced=False):
        nonlocal keep_staged
        message = ('CONFLICT: another writer changed config.toml at replacement; '
                   'backup retained at ' + str(backup))
        try:
            keep_ours()
            message += '; our version: ' + str(conflict)
        except (OSError, ValueError) as exc:
            # Recovery must not depend on space being available for a copy.
            keep_staged = os.path.lexists(name)
            message += '; cannot copy our version: ' + str(exc)
            if keep_staged: message += '; staged file retained: ' + name
            else: message += '; proposed version was published at ' + str(path)
        if restore_displaced:
            recovery = str(path) + '.displaced-' + backup.name.split('.bak-', 1)[1]
            try:
                if os.path.lexists(recovery):
                    raise ValueError('Recovery artifact already exists: ' + recovery)
                try:
                    os.replace(str(path), recovery)
                except FileNotFoundError:
                    pass
                restore = str(backup)
                if os.path.lexists(recovery):
                    if os.path.islink(recovery) or not pathlib.Path(recovery).is_file() or pathlib.Path(recovery).read_bytes() != data:
                        restore = recovery
                try:
                    restore_entry(restore)
                except FileExistsError:
                    pass  # An even newer writer recreated live; leave it there.
                # Keep this inode too: an in-place writer may still hold it
                # open and append after our comparison.
                if os.path.lexists(recovery): message += '; displaced live version: ' + recovery
                message += '; concurrent configuration restored or newer live version preserved'
            except (OSError, ValueError) as exc:
                keep_staged = os.path.lexists(name)
                message += '; recovery failed: ' + str(exc)
                if keep_staged: message += '; staged file retained: ' + name
        raise ConcurrentUpdate(message)

    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Prefer exclusive publication. Some filesystems prohibit hard links;
        # only those capability errors allow the explicitly warned rename path.
        probe = name + '.probe'
        try:
            os.link(name, probe, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in (errno.ENOTSUP, errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP):
                raise
            use_hardlinks = False
            print('  ! WARNING: hard links unavailable (' + errno.errorcode.get(exc.errno, str(exc.errno)) +
                  '); falling back to os.replace publication; residual concurrent-writer window between moving the live file aside and replacement.', file=sys.stderr)
        else:
            os.unlink(probe)
        if digest(read_current()[0]) != digest(expected):
            raise ConcurrentUpdate('Another writer changed config.toml; aborting without replacing it')
        # Defer signals until the displaced configuration is safely retained and
        # the live pathname has been restored. Pending signals then reach the
        # ordinary transaction rollback, rather than being silently discarded.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            os.replace(str(path), str(backup))
            moved = True
            try:
                # A plain second rename would overwrite a writer that recreated
                # the live name after the move aside. Link/unlink moves the same
                # staged inode atomically into that name, without clobbering it.
                if use_hardlinks:
                    os.link(name, str(path), follow_symlinks=False)
                else:
                    os.replace(name, str(path))
            except FileExistsError:
                report_conflict()
            try:
                fd = os.open(str(backup), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError('Displaced configuration is not a regular file')
                    displaced = stream.read()
            except (OSError, ValueError):
                # A symlink/type swap is a concurrent edit too. Restore the
                # displaced entry without following it or reverting stale bytes.
                displaced = None
            if displaced != expected:
                report_conflict(restore_displaced=True)
        finally:
            try:
                if moved and not os.path.lexists(path):
                    # No-clobber recovery if placement failed after move-aside.
                    try: restore_entry(str(backup))
                    except FileExistsError: pass
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    finally:
        if not keep_staged and os.path.lexists(name):
            os.unlink(name)

def statements(lines):
    # Scan complete TOML statements, keeping multiline strings and arrays opaque.
    # In particular, apparent headers/assignments in instructions are just text.
    start, quote, brackets, clean = 0, None, [], []
    for number, line in enumerate(lines):
        cursor = 0
        while cursor < len(line):
            char = line[cursor]
            if quote:
                if char == '\\' and quote[0] == '"':
                    clean.append(line[cursor:cursor + 2]); cursor += 2; continue
                if line.startswith(quote, cursor):
                    count = len(quote)
                    if count == 3:
                        while cursor + count < len(line) and line[cursor + count] == quote[0]:
                            count += 1
                    clean.append(line[cursor:cursor + count]); cursor += count
                    quote = None; continue
                if char in '\r\n' and len(quote) == 1:
                    raise ValueError('Unterminated TOML string; refusing to edit')
            elif char == '#':
                clean.append('\n'); break
            elif char in ('"', "'"):
                quote = char * (3 if line.startswith(char * 3, cursor) else 1)
                clean.append(quote); cursor += len(quote); continue
            elif char in '[{':
                brackets.append(char)
            elif char in ']}':
                if not brackets or brackets.pop() != {']': '[', '}': '{'}[char]:
                    raise ValueError('Unbalanced TOML brackets; refusing to edit')
            clean.append(char); cursor += 1
        if quote is None and not brackets:
            yield start, number + 1, ''.join(clean).strip()
            start, clean = number + 1, []
    if quote is not None or brackets:
        raise ValueError('Unterminated TOML value; refusing to edit')

def string_value(text, cursor):
    quote = text[cursor]
    delimiter = quote * (3 if text.startswith(quote * 3, cursor) else 1)
    cursor += len(delimiter)
    if len(delimiter) == 3:
        if text.startswith('\r\n', cursor): cursor += 2
        elif text.startswith('\n', cursor): cursor += 1
    result = []
    escapes = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}
    while cursor < len(text):
        if text.startswith(delimiter, cursor):
            cursor += len(delimiter)
            if len(delimiter) == 3:
                while cursor < len(text) and text[cursor] == quote:
                    result.append(quote); cursor += 1
            return ''.join(result), cursor
        char = text[cursor]
        if len(delimiter) == 3 and char == '\r' and text.startswith('\r\n', cursor):
            char = '\n'; cursor += 1
        if char == '\\' and quote == '"':
            cursor += 1
            continuation = re.match(r'[ \t]*(?:\r?\n)[ \t\r\n]*', text[cursor:]) if len(delimiter) == 3 else None
            if continuation:
                cursor += continuation.end(); continue
            if cursor >= len(text): break
            escape = text[cursor]
            if escape in ('u', 'U'):
                size = 4 if escape == 'u' else 8
                digits = text[cursor + 1:cursor + 1 + size]
                if len(digits) != size or not re.fullmatch(r'[0-9a-fA-F]+', digits):
                    raise ValueError('Invalid TOML Unicode escape')
                value = int(digits, 16)
                if value > 0x10ffff or 0xd800 <= value <= 0xdfff:
                    raise ValueError('Invalid TOML Unicode scalar')
                result.append(chr(value)); cursor += size + 1; continue
            if escape not in escapes:
                raise ValueError('Unsupported TOML escape')
            char = escapes[escape]
        result.append(char); cursor += 1
    raise ValueError('Unterminated TOML string')

def key_path(text, assignment=False):
    parts, cursor = [], 0
    while True:
        while cursor < len(text) and text[cursor] in ' \t': cursor += 1
        if cursor < len(text) and text[cursor] in ('"', "'"):
            if text.startswith(text[cursor] * 3, cursor):
                raise ValueError('Multiline TOML key')
            part, cursor = string_value(text, cursor)
        else:
            match = re.match(r'[A-Za-z0-9_-]+', text[cursor:])
            if not match: raise ValueError('Unrecognized TOML key')
            part = match[0]; cursor += match.end()
        parts.append(part)
        while cursor < len(text) and text[cursor] in ' \t': cursor += 1
        if cursor < len(text) and text[cursor] == '.': cursor += 1; continue
        if assignment and cursor < len(text) and text[cursor] == '=':
            return tuple(parts), cursor
        if not assignment and cursor == len(text): return tuple(parts)
        raise ValueError('Unrecognized TOML key syntax')

def setting_value(text):
    # Only the managed scalar/string-array types need decoding. Codex validates
    # the complete document, including all untouched fields, on Python 3.9 too.
    def read(cursor):
        while cursor < len(text) and text[cursor].isspace(): cursor += 1
        if cursor == len(text): raise ValueError('Missing TOML value')
        if text[cursor] in ('"', "'"): return string_value(text, cursor)
        if text[cursor] == '[':
            result, cursor = [], cursor + 1
            while True:
                while cursor < len(text) and text[cursor].isspace(): cursor += 1
                if cursor < len(text) and text[cursor] == ']': return result, cursor + 1
                value, cursor = read(cursor); result.append(value)
                while cursor < len(text) and text[cursor].isspace(): cursor += 1
                if cursor < len(text) and text[cursor] == ']': return result, cursor + 1
                if cursor == len(text) or text[cursor] != ',':
                    raise ValueError('Invalid TOML array')
                cursor += 1
        match = re.match(r'(?:true|false|[+-]?[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?)', text[cursor:])
        if not match: raise ValueError('Unsupported managed TOML value')
        raw = match[0]
        value = (raw == 'true') if raw in ('true', 'false') else float(raw.replace('_', ''))
        return value, cursor + match.end()
    value, end = read(0)
    if text[end:].strip(): raise ValueError('Trailing text in managed TOML value')
    return value

def document(data, allow_missing=False):
    # TOML line endings are LF/CRLF, not Unicode separators that splitlines()
    # would also treat as boundaries (including inside a comment or string).
    parts = data.decode('utf-8').split('\n')
    lines = [line + '\n' for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    headers, values, table = [], {}, ()
    for start, end, text in statements(lines):
        if not text: continue
        if text.startswith('['):
            array = text.startswith('[[')
            size = 2 if array else 1
            if not text.endswith(']' * size): raise ValueError('Invalid TOML header')
            table = key_path(text[size:-size].strip())
            if array and table[:len(bridge)] == bridge:
                raise ValueError('Bridge configuration must use ordinary TOML tables')
            headers.append((table, start, end))
        else:
            parts, equal = key_path(text, assignment=True)
            full = table + parts
            if full[:len(bridge)] == bridge or bridge[:len(full)] == full:
                if full in values: raise ValueError('Duplicate bridge setting: ' + '.'.join(full))
                values[full] = (start, end, text[:equal].rstrip(), text[equal + 1:])
    for target in (bridge, task):
        if sum(header[0] == target for header in headers) > 1:
            raise ValueError('Duplicate bridge table: ' + '.'.join(target))
    if not allow_missing and not any(header[0] == bridge for header in headers):
        raise ValueError('Expected exactly one [mcp_servers.claude-bridge] table')
    # Inline container definitions cannot be extended with a new table safely.
    if any(prefix in values for prefix in (('mcp_servers',), bridge, bridge + ('tools',), task)):
        raise ValueError('Inline bridge containers cannot be reconciled safely')
    return lines, headers, values

def verify_settings(data, expected):
    values = document(data)[2]
    for key, desired in expected.items():
        if key not in values or setting_value(values[key][3]) != desired:
            raise ValueError('Effective MCP setting failed verification: ' + '.'.join(key))
    if setting_value(values[bridge + ('enabled',)][3]) is not True:
        raise ValueError('Effective MCP registration is not enabled')

def uninstall_owned(data):
    _, headers, values = document(data, allow_missing=True)
    if not any(header[0][:len(bridge)] == bridge for header in headers) and not any(
            key[:len(bridge)] == bridge for key in values):
        print('  ok: MCP registration already absent; nothing removed')
        return False
    try:
        command = setting_value(values[bridge + ('command',)][3])
        args = setting_value(values[bridge + ('args',)][3])
        owned = (isinstance(command, str) and re.fullmatch(r'(?:.*/)?python3(?:\.\d+)*', command)
                 and isinstance(args, list) and len(args) == 1 and isinstance(args[0], str)
                 and os.path.commonpath((installation, os.path.realpath(args[0]))) == installation)
    except (KeyError, ValueError, OSError):
        owned = False
    if not owned:
        print('  ok: left MCP registration untouched; it does not belong to this installation')
    return bool(owned)

def verify_removed(data, expected=None):
    _, headers, values = document(data, allow_missing=True)
    if any(header[0][:len(bridge)] == bridge for header in headers) or any(
            key[:len(bridge)] == bridge for key in values):
        raise ValueError('MCP registration removal failed verification')

def removed(data):
    lines, headers, values = document(data, allow_missing=True)
    # Delete complete scanned statements, never header-like text inside a
    # multiline string. Keep all unrelated tables and their bytes unchanged.
    spans = [(start, end) for name, start, end in headers if name[:len(bridge)] == bridge]
    spans.extend((entry[0], entry[1]) for key, entry in values.items() if key[:len(bridge)] == bridge)
    removed_lines = {index for start, end in spans for index in range(start, end)}
    updated = ''.join(line for index, line in enumerate(lines) if index not in removed_lines).encode('utf-8')
    verify_removed(updated)
    return updated

def edited(data):
    lines, headers, values = document(data)
    env_key, approval_key = bridge + ('env_vars',), task + ('approval_mode',)
    env = setting_value(values[env_key][3]) if env_key in values else []
    if not isinstance(env, list) or not all(isinstance(name, str) for name in env):
        raise ValueError('env_vars must be an array of strings')
    env = env + [name for name in required_env if name not in env]
    approval = setting_value(values[approval_key][3]) if approval_key in values else None
    settings = {'command': 'python3', 'args': [sys.argv[4]], 'enabled': True,
        'startup_timeout_sec': 30, 'tool_timeout_sec': 3600,
        'default_tools_approval_mode': 'approve', 'env_vars': env}
    expected = {bridge + (key,): value for key, value in settings.items()}
    expected[approval_key] = 'deny' if approval == 'deny' else 'prompt'
    replacements, insertions = {}, {}

    def insertion_for(target):
        index = next(i for i, header in enumerate(headers) if header[0] == target)
        start = headers[index][2]
        end = headers[index + 1][1] if index + 1 < len(headers) else len(lines)
        while end > start and not lines[end - 1].strip(): end -= 1
        return end

    main_insertion = insertion_for(bridge)
    for key, value in expected.items():
        literal = json.dumps(value, ensure_ascii=False)
        if key in values:
            start, end, lhs, old = values[key]
            replacements[start] = (end, lhs + ' = ' + literal + '\n')
        elif key == approval_key:
            if any(header[0] == task for header in headers):
                insertions.setdefault(insertion_for(task), []).append('approval_mode = ' + literal + '\n')
            elif any(key[:len(task)] == task for key in values):
                # Existing dotted tool keys already define this table implicitly.
                # Extend their nearest explicit ancestor without redefining it.
                ancestor = max((header[0] for header in headers
                                if task[:len(header[0])] == header[0]), key=len)
                relative = '.'.join(approval_key[len(ancestor):])
                insertions.setdefault(insertion_for(ancestor), []).append(relative + ' = ' + literal + '\n')
            else:
                insertions.setdefault(main_insertion, []).extend([
                    '[mcp_servers.claude-bridge.tools.claude_task]\n',
                    'approval_mode = ' + literal + '\n', '\n'])
        else:
            insertions.setdefault(main_insertion, []).append(key[-1] + ' = ' + literal + '\n')
    output, index = [], 0
    while index <= len(lines):
        if index in insertions:
            if output and not output[-1].endswith('\n'): output.append('\n')
            output.extend(insertions[index])
        if index == len(lines): break
        if index in replacements:
            index, replacement = replacements[index]; output.append(replacement)
        else:
            output.append(lines[index]); index += 1
    updated = ''.join(output).encode('utf-8')
    verify_settings(updated, expected)
    return updated, expected

class Interrupted(BaseException):
    def __init__(self, signum):
        self.status = 128 + signum
        super().__init__('Interrupted by signal ' + str(signum))

def interrupted(signum, frame):
    raise Interrupted(signum)

def ignore_interruptions():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous: signal.signal(sig, signal.SIG_IGN)
    return previous

def restore_interruptions(previous):
    for sig, handler in previous.items(): signal.signal(sig, handler)

def validate():
    process = None
    try:
        # Deliver a pending interruption only after we can clean up the child.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            process = subprocess.Popen([sys.argv[2], 'mcp', 'get', 'claude-bridge'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        return process.wait(timeout=30) == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        handlers = ignore_interruptions()
        try:
            if process is not None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.wait(timeout=0.3)
                except subprocess.TimeoutExpired: pass
                # The leader may already have exited while descendants survive.
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
        finally:
            restore_interruptions(handlers)

def verify_owned(data):
    values = document(data)[2]
    try:
        command = setting_value(values[bridge + ('command',)][3])
        args = setting_value(values[bridge + ('args',)][3])
    except KeyError:
        raise ValueError('Bridge registration changed before the settings lock was acquired')
    if not (isinstance(command, str) and re.fullmatch(r'(?:.*/)?python3(?:\.\d+)*', command)
            and isinstance(args, list) and len(args) == 1 and isinstance(args[0], str)):
        raise ValueError('Bridge registration is no longer owned; leaving it untouched')
    server = os.path.realpath(args[0])
    suffixes = ('/codex-skills/claude/scripts/claude_mcp_server.py', '/skills/claude/scripts/claude_mcp_server.py')
    suffix = next((s for s in suffixes if server.endswith(s)), None)
    if suffix is not None:
        root = pathlib.Path(server[:-len(suffix)])
        if (root / 'skills/codex/scripts/bridge.py').is_file() and any(
                (root / name).is_file() for name in ('codex-skills/claude/scripts/claude_mcp_server.py', 'skills/claude/scripts/claude_mcp_server.py')):
            return
        try:
            manifest = json.loads((root / '.claude-plugin/plugin.json').read_text())
            if isinstance(manifest, dict) and manifest.get('name') == 'claude-codex-bridge':
                return
        except (OSError, ValueError):
            pass
    raise ValueError('Bridge registration is no longer owned; leaving it untouched')

def edit_locked():
    original, mode = read_current()
    if uninstall:
        if not uninstall_owned(original): return
        updated, expected, verify = removed(original), None, verify_removed
    elif check and not any(header[0] == bridge for header in document(original, allow_missing=True)[1]):
        print('  ok: [check] would add MCP registration -> python3 ' + sys.argv[4])
        return
    else:
        verify_owned(original)
        updated, expected = edited(original)
        verify = verify_settings
    if check:
        if uninstall:
            print('  ok: [check] would remove MCP registration owned by ' + installation)
        else:
            print('  ok: [check] MCP settings ' + ('already current' if updated == original else 'would be updated'))
        return
    backup = None
    try:
        if updated != original:
            backup = backup_name()
            replace_atomic(updated, mode, original, backup)
        verify(read_current()[0], expected)
        if not uninstall and not validate(): raise ValueError('Codex rejected the edited configuration')
        current = read_current()[0]
        verify(current, expected)
        if current != updated: raise ValueError('Another writer changed config.toml during validation')
        if backup is not None:
            backups = sorted(p for p in path.parent.glob('config.toml.bak-*') if re.fullmatch(r'config\.toml\.bak-\d{8}-\d{6}-\d{6}', p.name) and p.is_file() and not p.is_symlink() and not p.with_name(p.name.replace('.bak-', '.conflict-', 1)).exists())
            for old in backups[:-5]: old.unlink()
            print('  ok: ' + ('removed MCP registration' if uninstall else 'updated MCP settings') +
                  ' atomically; backup: ' + str(backup))
        else:
            print('  ok: MCP settings already current')
    except BaseException as exc:
        handlers = ignore_interruptions()
        try:
            message = str(exc)
            if backup is not None and os.path.lexists(backup):
                try: current = read_current()[0]
                except OSError: current = None
                if current == updated and not isinstance(exc, ConcurrentUpdate):
                    replace_atomic(original, mode, updated, backup_name())
                    message += '; restored backup ' + str(backup)
                elif current == original:
                    message += '; previous configuration is unchanged'
                else:
                    message += '; another writer changed the configuration; backup retained at ' + str(backup)
            if isinstance(exc, (OSError, ValueError, UnicodeError)):
                raise ValueError(message) from exc
            print('MCP configuration FAILED: ' + message, file=sys.stderr)
            raise
        finally:
            restore_interruptions(handlers)

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
try:
    if uninstall:
        if not os.path.lexists(path):
            print('  ok: MCP configuration already absent; nothing removed')
            sys.exit(0)
        # Avoid creating a lock file for an absent or unrelated registration;
        # recheck ownership under the lock before any actual removal.
        if not uninstall_owned(read_current()[0]): sys.exit(0)
    if check:
        # Do not create even a lock file in --check mode.
        edit_locked()
    else:
        lock = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock, 'a+b') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Configuration lock is not a regular file')
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            edit_locked()
except Interrupted as exc:
    sys.exit(exc.status)
except (OSError, ValueError, UnicodeError) as exc:
    sys.exit('MCP configuration FAILED: ' + str(exc))
PY
}
reconcile_mcp(){
  local state
  [ -f "$SERVER" ] || { warn "Missing MCP server: $SERVER"; return 1; }
  config_safe || return 1
  if [ "$CHECK" = 1 ]; then
    ok '[check] live MCP probe skipped; inspecting local configuration.'
    if [ ! -f "$CODEX_HOME/config.toml" ]; then
      ok "[check] would add MCP registration -> python3 $SERVER"; return 0
    fi
    patch_config; return $?
  fi
  if bounded 30 --capture "$CHOSEN" mcp get claude-bridge --json; then
    state="$(printf '%s' "$BOUNDED_OUTPUT" | registration_state json)"
  else
    bounded 30 --capture "$CHOSEN" mcp get claude-bridge || :
    state="$(printf '%s' "$BOUNDED_OUTPUT" | registration_state text)"
  fi
  case "$state" in
    current) ok 'MCP command and args already current' ;;
    stale) ok 'Refreshing owned MCP registration in the settings transaction' ;;
    disabled)
      warn 'Owned MCP registration is disabled; reconciliation will re-enable it.'
      if [ "$CHECK" = 1 ]; then return 1; fi
      ;;
    missing)
      if [ "$CHECK" = 1 ]; then ok "[check] would add MCP registration -> python3 $SERVER"; return 0; fi
      codex_command mcp add claude-bridge -- python3 "$SERVER" || return 1
      ;;
    unrelated-disabled) warn 'claude-bridge registration is unrelated and disabled; leaving it untouched.'; return 1 ;;
    *) warn 'claude-bridge registration is unrelated or unrecognized; leaving it untouched.'; return 1 ;;
  esac
  patch_config
}
install_optional_plugin(){
if [ "$INSTALL_MODE" = STANDALONE ] || [ "$PLUGIN" = 0 ] || [ -z "$CHOSEN" ] || [ -z "$CLAUDE" ] || ! command -v node >/dev/null 2>&1; then
  record 'Optional OpenAI plugin' skipped
elif [ "$CHECK" = 1 ]; then
  ok '[check] optional OpenAI plugin installation skipped'; record 'Optional OpenAI plugin' skipped
elif ! destination_safe 'Claude configuration' "$CLAUDE_DEST" "$CLAUDE_DIR" \
    "$CLAUDE_DIR/plugins" "$CLAUDE_DIR/plugins/marketplaces" "$CLAUDE_DIR/plugins/cache" \
    "$CLAUDE_DIR/plugins/installed_plugins.json" "$CLAUDE_DIR/plugins/known_marketplaces.json" "$CLAUDE_DIR/settings.json"; then
  record 'Optional OpenAI plugin' 'skipped (unsafe configuration directory; after correcting it, rerun: claude plugin marketplace add openai/codex-plugin-cc && claude plugin install codex@openai-codex --scope user -y)'
else
  PLUGIN_OK=1
  PLUGIN_RETRY='claude plugin marketplace add openai/codex-plugin-cc'
  if ! claude_command --capture plugin marketplace list 2>/dev/null || ! printf '%s' "$BOUNDED_OUTPUT" | py -c 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if r["status"] == 0 and "openai-codex" in r["stdout"] else 1)'; then
    claude_bounded 300 plugin marketplace add openai/codex-plugin-cc || PLUGIN_OK=0
  fi
  if [ "$PLUGIN_OK" = 1 ] && { ! claude_command --capture plugin list 2>/dev/null || ! printf '%s' "$BOUNDED_OUTPUT" | py -c 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if r["status"] == 0 and "codex@openai-codex" in r["stdout"] else 1)'; }; then
    PLUGIN_RETRY='claude plugin install codex@openai-codex --scope user -y'
    claude_bounded 300 plugin install codex@openai-codex --scope user -y || PLUGIN_OK=0
  fi
  if [ "$PLUGIN_OK" = 1 ]; then record 'Optional OpenAI plugin' ok
  else
    warn "Optional plugin installation skipped; retry: $PLUGIN_RETRY"
    record 'Optional OpenAI plugin' "skipped (command failed or timed out; retry: $PLUGIN_RETRY)"
  fi
fi
}

printf 'Claude <-> Codex bridge bootstrap (folder: %s)\n' "$HERE"
record Prerequisites ok
if [ "$UNINSTALL" = 1 ]; then
  uninstall_bridge
else
  if read_model_table; then
    ok "Model table $MODELS_TABLE: newest models need Codex CLI >= ${CODEX_FLOOR:-any} and Claude Code >= ${CLAUDE_FLOOR:-any}."
    record 'Model table' ok
  else
    warn "FAILED: $TABLE_ERROR. The engine refuses every model name without its table; restore skills/codex/models.json from the plugin. Versions are reported below without a floor."
    record 'Model table' FAILED
  fi
  install_prerequisites
  if [ -n "$CHOSEN" ]; then
    ensure_claude_links
    if config_safe && ensure_codex_skill; then record 'Codex skill' ok; else record 'Codex skill' FAILED; fi
    if reconcile_mcp; then record 'MCP registration and settings' ok; else record 'MCP registration and settings' FAILED; fi
  else
    record 'Claude links' skipped
    record 'Codex skill' skipped
    record 'MCP registration and settings' skipped
  fi
  install_optional_plugin
fi
# Do not invoke engine doctor: it independently discovers/executes CLIs and can
# create job caches. The checks above use only the validated executable.
printf '\nSummary:\n'
printf '  %s\n' "${STEPS[@]}"
[ "$FAILED" = 0 ] || exit 1

#!/usr/bin/env python3
"""
bridge.py — one engine for calling frontier coding models from either side, through whatever
Claude Code and Codex logins THIS machine already has. No API keys required, nothing account-specific.

A SEAT is "<model>[:<effort>][@fast]". Any number of seats, any mix of vendors, each in its own process
group with its own fresh context:
    astra:ultra,sol:ultra,opus:max            # two GPT reviewers + one Claude reviewer (the default review)
    astra:max,astra:ultra                     # the same model twice at different efforts
    fable:xhigh,sonnet:high,luna:medium

Model names mean "the newest model in that family" and come from one table, skills/codex/models.json (BRIDGE_MODELS_FILE):
Codex families astra/sol/terra/luna (OpenAI), Claude families fable/opus/sonnet/haiku (Anthropic). A seat name is a family,
a family head's full ID, a listed spelling of one, or the K1 seat `opus-5` while the table switches it on; a retired, refused
or unknown name is refused in one line naming the head to use (`doctor` lists the exact IDs). Force a vendor with
codex/<name> or claude/<name>. Efforts come from the table: Codex low|medium|high|xhigh|max|ultra (ultra = max reasoning +
automatic subagent delegation), Claude low|medium|high|xhigh|max; a model that lacks a level refuses it, and the built-in
default lowers to the model's highest level. A CLI below a model's floor (models.json min_cli) is refused before anything
launches. Relay receives family aliases; `codex exec -m` and `claude --model` receive full IDs. When this Mac's own signed-in
Codex catalog lists a newer GPT model of a family, the check promotes it to that family's head on this Mac (a ledger beside the
evidence store; models.json is never written; `doctor --undo-promotion <family> --reason "<why>"` reverses one).

Modes
  ask      one question (default seat astra:ultra; several seats -> answers side by side)
  council  the same question to every seat, answers side by side
  review   defect-first review of a git scope by every seat; the change is frozen into review.patch first
  fanout   many tasks with DIFFERENT prompts (JSON spec), any seats per task, one job, merged findings
  task     delegate work to one seat; --write allows edits in the repo
  reply    follow-up on a finished seat (Codex thread resume / Claude session resume)
  status   jobs and per-seat state, tokens, context use        monitor  live view until the job finishes
  result   print a job's output again      cancel   kill a running job      merge   re-merge findings
  doctor   binaries, versions, logins, defaults

Exit codes: 0 every seat succeeded · 3 partial (some seats failed, usable output present) · 1 every seat failed
or a runtime failure · 2 usage error.

Jobs live under ~/.cache/codex-claude-bridge/jobs/<id>/ (0700; meta.json, seat-<tag>.json live status, events-<tag>.jsonl,
prompt/last/stderr per seat, findings-<tag>.json, review.patch, merged.md). stdout is bounded; that is what the calling
model sees. Claude seats run under Claude Code's restricted profile (explicit tool list, no MCP servers, no inherited
settings); read-only seats have no shell at all. Codex seats run in Codex's read-only sandbox unless --write.

Accounts: when the relay skill is installed (RELAY_BIN, ~/.claude/skills/relay/scripts/relay.py, or `relay` on PATH) and has
accounts for a seat's vendor, the seat asks `relay pick` which account to spend and runs under `relay exec` (which execs into
the CLI: same pid and process group). A limited seat tells relay (`relay hit`) and moves to the next account: Codex restarts
with its original prompt, Claude continues its session there (`relay transplant` + --resume). Without relay, or with
BRIDGE_ROUTER=off, seats run on this machine's own logins and park on the usage-limit ladder as before.
"""
import argparse
import atexit
import errno
import fcntl
import hashlib
import stat as stat_mod
import json
import math
import os
import random
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
SCHEMA = SKILL_DIR / "schemas" / "findings.schema.json"
PROMPTS = SKILL_DIR / "prompts"
sys.path.insert(0, str(HERE))
try:
    import merge_findings  # noqa: E402
except Exception as _e:  # a broken merger must not take status/doctor/cancel down with it
    merge_findings = None
    _MERGE_IMPORT_ERROR = repr(_e)
try:
    import bridge_models  # noqa: E402
except Exception as _e:  # the table's reader: without it every model-starting operation refuses; job control keeps working
    bridge_models = None
    _MODELS_IMPORT_ERROR = repr(_e)
try:
    import bridge_routing  # noqa: E402
except Exception as _e:  # the registry link (docs/model-routing.md 2.1): without it the bridge keeps its built-in table and routing
    bridge_routing = None
    _ROUTING_IMPORT_ERROR = repr(_e)

CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
JOBS = Path(os.path.realpath(os.environ.get("BRIDGE_JOBS") or str(CACHE_ROOT / "codex-claude-bridge" / "jobs")))
MAX_STDOUT = int(os.environ.get("BRIDGE_MAX_STDOUT", "16000"))
FOOTER_MAX = 1500
APP_LAUNCHER = os.environ.get("BRIDGE_APP_LAUNCHER", "/Applications/ChatGPT.app/Contents/Resources/codex")
# Two places, newest first: the ChatGPT app moved its bundled CLI in bundle 26.928. The new one is derived from the legacy one's
# folder, so the BRIDGE_APP_LAUNCHER override (ONE path, the legacy place; empty: no app launcher at all) moves both.
APP_LAUNCHERS = (os.path.join(os.path.dirname(APP_LAUNCHER), "codex-cli", "bin", "codex"), APP_LAUNCHER) if APP_LAUNCHER else ()
SHIM_MARKER = "codex shim (claude-codex-bridge"
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[a-z]+-[0-9a-f]{5}$")
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
REPLY_TAG_RE = re.compile(r"-reply-[0-9a-f]{5}$")

MODELS_FILE = os.environ.get("BRIDGE_MODELS_FILE") or str(SKILL_DIR / "models.json")  # the model table (DESIGN §1), loaded lazily
EVIDENCE_FILE = os.environ.get("BRIDGE_MODELS_EVIDENCE") or str(JOBS.parent / "models-evidence.json")  # the check's sticky markers
# An effort set in the environment is explicit input, validated like a typed one; the built-in defaults (Codex ultra, Claude
# max) lower to the model's highest level instead (bridge_models.Table.resolve_effort).
EFFORT_ENV = {"codex": os.environ.get("BRIDGE_CODEX_EFFORT") or None, "claude": os.environ.get("BRIDGE_CLAUDE_EFFORT") or None}
# The default seat lists (DESIGN §2), by the user-facing variable that overrides each: the engine's own list, and the variable a
# Codex-side entry point (codex-skills/claude) passes ITS built-in list in (S-DEF). The effective list is the user's variable if
# set, else the entry point's, else the engine's own; only the user's variable is ever named in a refusal or a finding.
ENGINE_DEFAULT_SEATS = {"BRIDGE_DEFAULT_SEAT": "astra:ultra", "BRIDGE_REVIEW_SEATS": "astra:ultra,sol:ultra,opus:max",
                        "BRIDGE_COUNCIL_SEATS": "astra:ultra,sol:ultra,opus:max"}
BUILTIN_SEAT_VARS = {"BRIDGE_DEFAULT_SEAT": "BRIDGE_BUILTIN_DEFAULT_SEAT", "BRIDGE_REVIEW_SEATS": "BRIDGE_BUILTIN_REVIEW_SEATS",
                     "BRIDGE_COUNCIL_SEATS": "BRIDGE_BUILTIN_COUNCIL_SEATS"}


def default_seats(var):
    """The effective default seat list a user-facing variable governs and the name a refusal or finding gives its origin (S-DEF):
    (the user's value, var) when the variable is set; else (a Codex-side entry point's built-in list, None) when BRIDGE_BUILTIN_*
    carries one; else (the engine's own list, None). A built-in list has no variable to name."""
    if var in os.environ:
        return os.environ[var], var
    return os.environ.get(BUILTIN_SEAT_VARS[var], ENGINE_DEFAULT_SEATS[var]), None


DEFAULT_SEAT = default_seats("BRIDGE_DEFAULT_SEAT")[0]
DEFAULT_REVIEW_SEATS = default_seats("BRIDGE_REVIEW_SEATS")[0]
DEFAULT_COUNCIL_SEATS = default_seats("BRIDGE_COUNCIL_SEATS")[0]
CONTEXT_WINDOW = {"codex": 272_000, "claude": 1_000_000}  # Codex: catalog value; Claude reports its own window at the end
# The two default fixed directories of Codex discovery, looked up beneath BRIDGE_CODEX_FIXED_ROOT (unset or empty: the real
# directories; a test harness points it at an empty directory so that no host codex can be found).
CODEX_FIXED_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
# A Claude seat runs exactly the ID it was given: no automatic switching, no fallback chain (--settings JSON outranks the user's
# settings), and none of the outer session's alias-override variables.
CLAUDE_SEAT_SETTINGS = {"switchModelsOnFlag": False, "fallbackModel": []}
SEAT_ENV_DENY = {"ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
MAX_PATCH_LINES = int(os.environ.get("BRIDGE_MAX_PATCH_LINES", "4000"))
BRIDGE_ROOT = os.path.realpath(os.environ["BRIDGE_ROOT"]) if os.environ.get("BRIDGE_ROOT") else None  # hard boundary for the effective cwd
MAX_INVENTORY = 50_000
WAITING_GRACE = 300.0  # a waiting seat whose retry time passed this long ago without a state change is not live
MAX_UNTRACKED_BYTES = 1_000_000

# Claude seats: an explicit tool inventory under --restricted. Read-only seats have NO shell.
# Turn caps are a backstop against runaway loops, not a budget: a max-effort review of a large change legitimately needs
# well over a hundred tool turns (an Opus seat hit a 45-turn cap on a 9,500-line review). Wall-clock bounds come from --timeout.
CLAUDE_MAX_TURNS = {"review": int(os.environ.get("BRIDGE_CLAUDE_REVIEW_TURNS", "400")), "task": int(os.environ.get("BRIDGE_CLAUDE_TASK_TURNS", "400")),
                    "reply": 200, "default": int(os.environ.get("BRIDGE_CLAUDE_TURNS", "200"))}
CLAUDE_READ_TOOLS = ["Read", "Grep", "Glob"]
CLAUDE_WRITE_TOOLS_NOSHELL = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit"]
CLAUDE_WRITE_TOOLS = CLAUDE_WRITE_TOOLS_NOSHELL + ["Bash"]  # only with --shell
CLAUDE_WRITE_BASH_ALLOW = ["Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git add *)",
                           "Bash(python3 *)", "Bash(python *)", "Bash(pytest *)", "Bash(npm *)", "Bash(pnpm *)", "Bash(node *)",
                           "Bash(go *)", "Bash(cargo *)", "Bash(make *)"]
CLAUDE_WRITE_BASH_DENY = ["Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)", "Bash(git checkout*)", "Bash(git rebase*)",
                          "Bash(rm -rf*)", "Bash(sudo*)", "Bash(curl*)", "Bash(wget*)"]
# Outer-session variables that must not leak into a nested `claude -p`. Authentication variables are kept.
ENV_DENY = {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_EFFORT",
            "CLAUDE_SESSION_ID"}


def _env_seconds(name, default):
    """A non-negative number of seconds from the environment; a malformed value is a one-line usage error, never a traceback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(raw)
    except ValueError:
        sys.stderr.write(f"bridge: {name} must be a number of seconds, not {raw!r}\n")
        sys.exit(EXIT_USAGE)
    return value


LIMIT_LADDER = os.environ.get("BRIDGE_LIMIT_LADDER", "1") != "0"
LIMIT_WAIT_SHORT = _env_seconds("BRIDGE_LIMIT_WAIT_SHORT", 20.0)
LIMIT_WAIT_LONG = _env_seconds("BRIDGE_LIMIT_WAIT_LONG", 3600.0)
LIMIT_RE = re.compile(r"usage[ _-]?limit|rate[ _-]?limit|limit (has been )?reached|too many requests|"
                      r"(?:http|status|code|error)[^\n]{0,12}\b429\b|\b429\b[^\n]{0,40}(?:too many|rate|limit)|quota exceeded|"
                      r"out of (usage|quota)|plan limit", re.I)
CLAUDE_LIMIT_STATUSES = ("rejected", "limited", "exceeded", "denied", "blocked")
# The throttle class (build spec 2026-10-13 sections 3.2 and 5.1): ChatGPT refusing or overloaded, or the CLI unable to reach it.
# Case-insensitive substrings, ’ normalized to ', every term of a tuple must occur; evaluated BEFORE LIMIT_RE on a turn.failed
# message (never on stderr), and when one matches the usage-limit regex is not consulted. Never a bare "429" or "503".
THROTTLE_SIGNATURES = (("exceeded retry limit", "last status: 429"), ("exceeded retry limit", "last status: 5"), ("429 too many requests",),
                       ("503 service unavailable",), ("currently experiencing high demand",), ("selected model is at capacity",),
                       ("flex capacity unavailable",), ("server_overloaded",))
NETWORK_SIGNATURES = (("connection refused",), ("connection reset",), ("network is unreachable",), ("no route to host",), ("dns error",),
                      ("failed to lookup address",), ("stream disconnected before completion",),
                      ("error sending request for url (https://chatgpt.com",), ("connection failed",), ("waiting for network",))
# A run killed while still reconnecting on WebSocket 403s (the post-wake storm): the final error of a killed run only, never turn.failed.
NETWORK_TAIL_SIGNATURES = (("unexpected status 403 forbidden", "wss://chatgpt.com"),)
THROTTLE_CLASSES = ("codex-throttled", "codex-network")
# Throttle waits (5.1): batch seats 300, 900, 1800 s then every 1800 s (x 0.8..1.2, unbounded); interactive seats 60 s then 300 s
# (x 0.6..1.4), then the seat fails (Kevin's rule: reviews never wait on Codex). BRIDGE_THROTTLE_WAIT_SCALE compresses them in tests.
THROTTLE_WAITS_BATCH = (300.0, 900.0, 1800.0)
THROTTLE_WAITS_INTERACTIVE = (60.0, 300.0)
THROTTLE_WAIT_SCALE = _env_seconds("BRIDGE_THROTTLE_WAIT_SCALE", 1.0)
LAUNCH_CLASSES = ("interactive", "batch")
STARTUP_MODEL_REJECT_RE = re.compile(r"model.{0,80}(not (found|supported|available|recogni[sz]ed)|unknown|does not exist|unsupported|invalid)|"
                                     r"(unknown|unsupported|invalid).{0,40}model", re.I | re.S)
STARTUP_EFFORT_REJECT_RE = re.compile(r"(reasoning|effort).{0,80}(unsupported|invalid|not supported|unknown|not allowed)", re.I | re.S)
PROBE_TIMEOUT = 10

# Account routing through relay (design 2026-09-22 §4, addendum A3/A8). Seats of a vendor relay has accounts for run under
# `relay exec --vendor V --account L`; a seat whose account is limited moves on. BRIDGE_ROUTER=off disables all of it.
ROUTER_ON = os.environ.get("BRIDGE_ROUTER", "on").strip().lower() not in ("off", "0")
ROUTED_MAX_WAIT = _env_seconds("BRIDGE_ROUTED_MAX_WAIT", 7200.0)  # all of a routed seat's waits together end this long after its start
ROUTER_TIMEOUTS = {"pick": 180, "hit": 60, "transplant": 60, "accounts": 60}  # seconds; pick may probe. Each is also bounded by the seat's clock.
IDENTITY_KEYS = ("account", "account_email", "account_plan")  # what a seat record says about the relay account it ran on
ROUTER_PROTOCOL = 1  # `relay pick --json` answers {"v": 1, "result": ok|none|busy|no_accounts, ...}; only v1 "ok" routes a seat
REPICK_WAIT = 3 * LIMIT_WAIT_SHORT  # 60 s by default: the pause before asking relay again when its answer was unusable
# Constrained routing (docs/model-routing.md 3.3): a Codex seat's pick carries `--only <capable set>` and is valid only as a protocol-2
# answer with only_applied true; anything else means constrained routing is unsupported and the seat validates this machine's own
# login as one more candidate instead of running on it unchecked. The local login is stored in the registry's state under the label
# the registry's own Codex launcher uses.
CONSTRAINED_PROTOCOL = 2
LOCAL_LABEL = "default"
REPICK_LIMIT = 6  # bounded re-picks per seat after failed pre-launch re-checks or startup rejections


# ============================================================================ small helpers
def die(msg, code=EXIT_USAGE):
    sys.stderr.write(f"bridge: {msg}\n")
    sys.exit(code)


_table_cache = {}


def table_or_error():
    """(Table, None) or (None, a one-line error). The table is the shipped models.json with this Mac's automatic promotions applied
    (bridge_models.load_table; DECISIONS A28: the ledger beside the evidence store). It is read once per process and only when a
    model-starting operation, a reply, Codex discovery or doctor needs it: status, monitor, result, cancel and merge never touch it.
    A check that changes the ledger replaces it (pre_check)."""
    if "table" not in _table_cache:
        if bridge_models is None:
            _table_cache["table"] = (None, f"model table reader unavailable: {_MODELS_IMPORT_ERROR}")
        else:
            try:
                _table_cache["table"] = (bridge_models.load_table(MODELS_FILE, EVIDENCE_FILE), None)
            except Exception as exc:
                _table_cache["table"] = (None, re.sub(r"\s+", " ", str(exc)).strip() or repr(exc))
    return _table_cache["table"]


def reload_table():
    """The table read again from disk (models.json and the promotions ledger): after another process may have changed the ledger."""
    _table_cache.pop("table", None)
    return table()


def table():
    """The model table, or a one-line usage error (exit 2) naming the file and the problem."""
    t, err = table_or_error()
    if t is None:
        die(err)
    return t


def evidence():
    """The evidence store the validator reads its sticky markers from (BRIDGE_MODELS_EVIDENCE or <jobs root parent>/models-evidence.json)."""
    return bridge_models.Evidence(EVIDENCE_FILE) if bridge_models is not None else None


_link_cache = {}


def registry_link():
    """The bridge's link to the model registry (bridge_routing.Link; docs/model-routing.md 2.1), loaded once per process and only
    when a seat or doctor needs it: status, monitor, result, cancel and merge never touch it. None when no registry is found or the
    link cannot load; the reason is kept for doctor (`registry_link_error()`), and a broken registry is said once on stderr."""
    if "link" not in _link_cache:
        link, error = None, None
        if bridge_routing is None:
            error = f"the registry link module could not be imported: {_ROUTING_IMPORT_ERROR}"
        else:
            try:
                t, _err = table_or_error()
                link = bridge_routing.Link(__file__, catalog_keys=t.catalog_config_keys if t is not None else None)
            except Exception as exc:
                link, error = None, f"the registry link failed: {exc!r}"
            if link is not None and not link.available:
                error, link = link.error, None
        broken = bool(error) and ("could not be loaded" in error or (bridge_routing is not None and "could not be imported" in error))
        if broken:  # a registry that exists but cannot load is said once; an absent one is doctor's line only
            sys.stderr.write(f"[bridge] model registry: {error}; the built-in table and routing apply\n")
        _link_cache["link"], _link_cache["error"] = link, error
    return _link_cache["link"]


def registry_link_error():
    registry_link()
    return _link_cache.get("error")


def add_notice(d, kind, text, seat=None, once=False):
    """Persist a notice ({kind, seat, text, at}) in meta.json under the job lock; notices lead every user-facing output. With
    `once`, a notice of the same kind, seat and text is not persisted again (a sticky marker re-read on every retry)."""
    with job_lock(d):
        m = read_json(Path(d) / "meta.json")
        notices = list(m.get("notices") or [])
        if once and any(n.get("kind") == kind and n.get("seat") == seat and n.get("text") == text for n in notices if isinstance(n, dict)):
            return text
        notices.append({"kind": kind, "seat": seat, "text": text, "at": time.time()})
        m["notices"] = notices
        atomic_write_json(Path(d) / "meta.json", m)
    return text


def job_notices(d, seat=None):
    """The texts of the job's persisted notices, in order (DESIGN §4: they lead every user-facing output). With `seat`, only that
    seat's notices and the job-level ones."""
    notices = read_json(Path(d) / "meta.json").get("notices") or []
    return [n["text"] for n in notices if isinstance(n, dict) and n.get("text") and (seat is None or n.get("seat") in (seat, None))]


def lead_with(notices, text):
    """`text` led by the notices, one per line, a blank line between."""
    return ("\n".join(notices) + "\n\n" + (text or "")) if notices else (text or "")


def strip_notices(text, notices):
    """A composed artifact (last-<tag>.md, council.md) without the notice block the bridge led it with: only leading lines that
    are the job's own notices are dropped, so that a retrieval leads with the current notices once. A raw answer is never altered."""
    lines = (text or "").splitlines(keepends=True)
    known = set(notices or [])
    i = 0
    while i < len(lines) and lines[i].rstrip("\n") in known:
        i += 1
    if i == 0:
        return text or ""
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "".join(lines[i:])


def without_notices_block(md):
    """The merged report without the `Notices:` block merge_findings renders on line 2; stdout carries those notices once, as
    its leading lines, while merged.md keeps the block."""
    lines = (md or "").splitlines(keepends=True)
    if len(lines) > 1 and lines[1].startswith("Notices:"):
        end = 2
        while end < len(lines) and lines[end].startswith("- "):
            end += 1
        if end < len(lines) and not lines[end].strip():
            end += 1  # the blank line that ends the list (A-opus-5), or stdout gets an empty line under the heading
        del lines[1:end]
    return "".join(lines)


def answer_files(d):
    """One answer artifact per seat and per reply, in tag order: its raw-<tag>.md (the verbatim answer) when it has content, else
    its last-<tag>.md (a job written before the raw/last split, or an answer recovered from the events because `-o` never wrote
    the file or left it empty: B-astra-2). [(path, raw)]."""
    tags = {p.name[4:-3] for p in Path(d).glob("raw-*.md")} | {p.name[5:-3] for p in Path(d).glob("last-*.md")}
    files = []
    for tag in sorted(tags):
        raw, last = Path(d) / f"raw-{tag}.md", Path(d) / f"last-{tag}.md"
        empty = not raw.exists() or not raw.read_text().strip()
        files.append((last, False) if empty and last.exists() else (raw, True))
    return files


def caller_name():
    return os.environ.get("BRIDGE_CALLER") or ("Claude Code" if os.environ.get("CLAUDECODE") else "an AI coding agent")


def clean_env():
    """Drop only the outer Claude Code session variables; keep CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CONFIG_DIR, API keys."""
    return {k: v for k, v in os.environ.items() if k not in ENV_DENY}


def seat_env():
    """A Claude seat's environment: clean_env() without the alias-override variables, so the seat runs exactly the ID it was given."""
    return {k: v for k, v in clean_env().items() if k not in SEAT_ENV_DENY}


def model_env(env):
    """A model child's environment (a seat CLI, or the `relay exec` that becomes one): `env` without a Codex-side entry point's
    built-in seat lists (BRIDGE_BUILTIN_*), so an engine run underneath the seat uses its own defaults (S-DEF)."""
    return {k: v for k, v in env.items() if k not in BUILTIN_SEAT_VARS.values()}


# What `relay exec` strips before it becomes a Codex CLI (skills/relay/scripts/relay_codex.py, codex_env: everything that could
# change WHICH credential or provider codex uses, and nothing of Claude's); a routed account's catalog is read under the same
# environment, so the discovery validates the login the launch will actually use (docs/model-routing.md 3.3).
RELAY_CODEX_ENV_PREFIXES = ("OPENAI_", "ANTHROPIC_")
RELAY_CODEX_ENV_KEYS = ("CODEX_API_KEY", "CODEX_HOME", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")


def relay_codex_env(env):
    """`env` as relay hands it to a Codex CLI, CODEX_HOME aside (the account's home is set by the caller)."""
    return {k: v for k, v in env.items() if not k.startswith(RELAY_CODEX_ENV_PREFIXES) and k not in RELAY_CODEX_ENV_KEYS}


def short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_secs(s):
    s = int(s or 0)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def render(name, **vars):
    """Single-pass placeholder substitution; inserted text is never rescanned; unknown placeholders stay literal."""
    template = (PROMPTS / f"{name}.md").read_text()
    vars.setdefault("CALLER", caller_name())
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: str(vars[m.group(1)]) if m.group(1) in vars and vars[m.group(1)] is not None else m.group(0), template)


def emit(text, full_path=None, footer="", notices=None):
    """Bounded stdout: the notices first, then body + truncation notice + footer, never exceeding BRIDGE_MAX_STDOUT (the footer
    itself is capped)."""
    text = lead_with(notices, text)
    footer = footer if len(footer) <= FOOTER_MAX else footer[:FOOTER_MAX] + "…"
    notice = f"\n\n[output truncated — full text: {full_path}]"
    total = max(1, MAX_STDOUT)
    if len(footer) + 1 > total:  # a budget smaller than the framing: keep the tail of the footer (job id, logs) and nothing else
        footer = footer[-(total - 1):]
    budget = total - len(footer) - 1
    if len(text) > budget:
        text = (text[:budget - len(notice)] + notice) if budget > len(notice) else text[:budget]
    out = text.rstrip() + footer + "\n"
    sys.stdout.write(out[:total])
    sys.stdout.flush()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


# ============================================================================ private files, locks, job dirs
def ensure_private_dir(p):
    p = Path(p)
    if not p.exists():
        os.makedirs(p, mode=0o700, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def open_private(path, flags):
    return os.open(str(path), flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)


def write_private_text(path, text):
    fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def tighten_job_files(d):
    """Files written by the child CLIs (e.g. Codex's -o output) inherit the umask; make them private too."""
    try:
        for p in Path(d).iterdir():
            if p.is_file() and not p.is_symlink() and (p.stat().st_mode & 0o077):
                os.chmod(p, 0o600)
    except OSError:
        pass


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {} if default is None else default


def atomic_write_json(path, obj):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:6]}")
    fd = open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(obj, indent=1, default=str))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class job_lock:
    """Cross-process lock for a job's read-modify-write operations (flock on <job>/.lock)."""

    def __init__(self, d, name=".lock", blocking=True):
        self.path = Path(d) / name
        self.blocking = blocking
        self.fd = None

    def __enter__(self):
        self.fd = open_private(self.path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


TERMINAL_STATES = ("done", "failed", "partial", "cancelled")
NONTERMINAL_STATES = (None, "created", "running", "?")


def update_meta(d, **kv):
    with job_lock(d):
        m = read_json(Path(d) / "meta.json")
        if m.get("state") == "cancelled" and kv.get("state") in ("done", "failed", "partial"):
            kv = dict(kv, state="cancelled")  # cancellation is terminal
        m.update(kv)
        atomic_write_json(Path(d) / "meta.json", m)
        return m


def update_seat(d, tag, **kv):
    with job_lock(d):
        p = Path(d) / f"seat-{tag}.json"
        s = read_json(p)
        if s.get("state") == "cancelled" and kv.get("state") in ("done", "failed"):
            kv = dict(kv, state="cancelled")
        s.update(kv)
        s["tag"] = tag
        s["updated"] = time.time()
        atomic_write_json(p, s)
        return s


def jobs_root():
    return ensure_private_dir(JOBS)


def new_job(mode, name=None):
    root = jobs_root()
    if name:
        if not JOB_NAME_RE.match(name) or name in (".", "..") or "/" in name:
            die(f"--job-dir must be a plain name (letters, digits, . _ -), not a path: {name!r}")
        jid = name
    else:
        jid = f"{datetime.now():%Y%m%d-%H%M%S}-{mode}-{uuid.uuid4().hex[:5]}"
    d = root / jid
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        die(f"job {jid} already exists; choose another --job-dir name")
    atomic_write_json(d / "meta.json", {"state": "created", "created": datetime.now().isoformat(), "supervisor_pid": os.getpid()})
    sys.stderr.write(f"[bridge] job {jid} {d}\n")
    sys.stderr.flush()
    return jid, d


def _job_created_at(d):
    m = read_json(Path(d) / "meta.json")
    for k in ("created", "started"):
        try:
            return datetime.fromisoformat(m[k]).timestamp()
        except Exception:
            pass
    try:
        return Path(d).stat().st_mtime
    except OSError:
        return 0.0


def find_job(ref):
    root = jobs_root()
    root_real = os.path.realpath(root)
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    if ref in (None, "", "last", "latest"):
        if not names:
            die("no jobs yet", EXIT_FAIL)
        ref = max(names, key=lambda n: (_job_created_at(root / n), n))
    if "/" in ref or ref in (".", "..") or not JOB_NAME_RE.match(ref):
        die(f"job reference must be a job name, not a path: {ref!r}")
    if ref not in names:
        hits = [n for n in names if ref in n]
        if len(hits) != 1:
            die(f"job {ref!r} not found or ambiguous ({len(hits)} matches)", EXIT_FAIL)
        ref = hits[0]
    d = root / ref
    real = os.path.realpath(d)
    if os.path.islink(d) or not (real == root_real or real.startswith(root_real + os.sep)):
        die(f"job {ref!r} is outside the jobs root; refusing", EXIT_FAIL)
    return d


def is_cancelled(d):
    return (Path(d) / "CANCELLED").exists()


# ============================================================================ processes
_children = {}
_children_lock = threading.Lock()


def _register(tag, p):
    with _children_lock:
        _children[tag] = p


def _unregister(tag):
    with _children_lock:
        _children.pop(tag, None)


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _parse_lstart(text):
    return time.mktime(time.strptime(text.strip(), "%a %b %d %H:%M:%S %Y"))


def _group_members(pgid):
    """[(pid, lstart)] of the live members of a process group, from ps; None when ps is unavailable."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pgid=,lstart="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    members = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            if int(parts[1]) == pgid:
                members.append((int(parts[0]), parts[2].strip()))
        except ValueError:
            continue
    return members


def _process_table():
    """{pid: (ppid, pgid, lstart)} from one ps snapshot, or None when ps is unavailable."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3].strip())
        except ValueError:
            continue
    return table


def _descendants(leader_pid, leader_start):
    """Every transitive descendant of a LIVE leader as [(pid, pgid, lstart)], taken from one snapshot while the parent chain
    is intact. The root entry must carry the recorded leader identity in that same table, so a reused PID can never lend us
    somebody else's children. Codex launches sandboxed commands in their own sessions, so killing the seat's group alone
    leaves them behind."""
    table = _process_table()
    if not table or leader_pid not in table or not leader_start or table[leader_pid][2].strip() != leader_start.strip():
        return []
    children = {}
    for pid, (ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = [], [leader_pid]
    seen = {leader_pid}
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child in seen:
                continue
            seen.add(child)
            found.append((child, table[child][1], table[child][2]))
            stack.append(child)
    return found


def _proc_identity(pid):
    """(current pgid, lstart) of one process from a fresh ps query, or None when it is gone."""
    try:
        out = subprocess.run(["ps", "-o", "pgid=,lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        parts = out.split(None, 1)
        return (int(parts[0]), parts[1].strip()) if len(parts) == 2 else None
    except Exception:
        return None


def _kill_descendants(desc, skip_pgids=()):
    """Signal recorded descendants TERM then KILL. Immediately before EVERY signal the descendant's identity (start time)
    and its current group membership are re-read: a group is signalled at most once per round and only while a verified
    descendant still belongs to it; a descendant that left its recorded group is signalled alone; anything missing or
    mismatched (a reused pid) is left alone."""
    own = os.getpgid(0)
    live = [(pid, pgid, ls) for pid, pgid, ls in desc if pgid not in skip_pgids and pgid != own]
    if not live:
        return
    for sig, wait_for in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 3.0)):
        remaining, signalled_groups = [], set()
        for pid, pgid, ls in live:
            cur = _proc_identity(pid)  # fresh per entry: an earlier signal in this round may have changed the picture
            if not cur or cur[1] != ls.strip():
                continue  # gone, or a reused pid: leave it alone
            remaining.append((pid, pgid, ls))
            try:
                if cur[0] == pgid:
                    if pgid not in signalled_groups:
                        signalled_groups.add(pgid)
                        os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)  # it left its recorded group: never signal a group we cannot prove is still its own
            except Exception:
                pass
        if not remaining:
            return
        deadline = time.time() + wait_for
        while time.time() < deadline and any(proc_start(pid) == ls for pid, _, ls in remaining):
            time.sleep(0.1)
        if not any(proc_start(pid) == ls for pid, _, ls in remaining):
            return


def _group_verified(pgid, leader_start, latest_start=None):
    """A leaderless group may be signalled only when it still has members and every member started inside the leader's
    lifetime: not before the recorded leader start, and not after the moment its exit was observed (`latest_start`, epoch
    seconds). A reused PGID would carry older members, or newer ones spawned by an unrelated replacement leader. Fail closed
    when ps, the recorded start, or the upper bound is missing."""
    if not pgid or pgid == os.getpgid(0) or not leader_start or latest_start is None:
        return False
    members = _group_members(pgid)
    if not members:
        return False
    try:
        t0 = _parse_lstart(leader_start)
        return all(t0 - 1 <= _parse_lstart(ls) <= latest_start + 1 for _, ls in members)
    except Exception:
        return False


def _note_exit(p):
    """Remember when the leader's exit was first observed: the upper bound for member start times of a leaderless group."""
    if p.poll() is not None and getattr(p, "_bridge_exit_observed", None) is None:
        p._bridge_exit_observed = time.time()


def _group_authorized(p):
    """True if signalling p's recorded group is safe right now: the leader is alive (identity implied), or the leaderless
    group is verified by membership."""
    pgid = getattr(p, "_bridge_pgid", None)
    if not pgid or pgid == os.getpgid(0):
        return False
    if p.poll() is None:
        return True
    return _group_alive(pgid) and _group_verified(pgid, getattr(p, "_bridge_start", None), getattr(p, "_bridge_exit_observed", None))


def kill_group(p, grace=5.0):
    """Terminate a child's whole process group (recorded at launch), escalating to SIGKILL; always reap the leader.
    Works even after the leader has exited, so descendants holding the pipe cannot outlive the seat."""
    if p is None:
        return
    pgid = getattr(p, "_bridge_pgid", None)
    if pgid is None:
        try:
            pgid = os.getpgid(p.pid)
        except Exception:
            pgid = None
    own = os.getpgid(0)
    desc = _descendants(p.pid, getattr(p, "_bridge_start", None)) if p.poll() is None else []  # while the parent chain still exists
    for sig, wait_for in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        _note_exit(p)
        alive_leader = p.poll() is None
        alive_group = bool(pgid) and pgid != own and _group_alive(pgid)
        if not alive_leader and not alive_group:
            break
        try:
            if alive_leader and pgid and pgid != own:
                os.killpg(pgid, sig)
            elif alive_leader:
                p.send_signal(sig)
            elif _group_verified(pgid, getattr(p, "_bridge_start", None), getattr(p, "_bridge_exit_observed", None)):
                os.killpg(pgid, sig)  # leaderless but verified ours
            else:
                break  # cannot prove the group is ours any more: never signal a possibly reused id
        except ProcessLookupError:
            pass
        except Exception:
            pass
        deadline = time.time() + wait_for
        while time.time() < deadline and (p.poll() is None or (pgid and pgid != own and _group_alive(pgid))):
            time.sleep(0.1)
    try:
        p.wait(timeout=1)
    except Exception:
        pass
    if desc:
        _kill_descendants(desc, skip_pgids=(pgid,))  # sandboxed commands the seat started in their own sessions


def _kill_all_children(*_a):
    """Global shutdown: signal every registered seat by its RECORDED process group, whether or not the leader still runs,
    and escalate on group survival rather than leader survival (a leader that exits first must not shield its descendants)."""
    with _children_lock:
        procs = list(_children.values())
    if not procs:
        return
    groups = []
    for p in procs:
        pgid = getattr(p, "_bridge_pgid", None)
        if not pgid and p.poll() is None:
            try:
                pgid = os.getpgid(p.pid)
            except Exception:
                pgid = None
        if pgid and pgid != os.getpgid(0):
            groups.append((p, pgid))
    for p, _ in groups:
        _note_exit(p)
    descendants = [(_descendants(p.pid, getattr(p, "_bridge_start", None)), pgid) for p, pgid in groups if p.poll() is None]

    def signal_all(sig):
        for p, pgid in groups:
            _note_exit(p)  # #4: re-observe before every round, so a leader that died during the grace still bounds its group
            if _group_authorized(p):
                try:
                    os.killpg(pgid, sig)
                except Exception:
                    pass
            elif p.poll() is None:
                try:
                    p.send_signal(sig)
                except Exception:
                    pass
    signal_all(signal.SIGTERM)
    deadline = time.time() + 2.0
    while time.time() < deadline and any(_group_alive(pgid) or p.poll() is None for p, pgid in groups):
        time.sleep(0.1)
    signal_all(signal.SIGKILL)
    for p, _ in groups:
        try:
            p.wait(timeout=1)
        except Exception:
            pass
    for desc, pgid in descendants:
        if desc:
            _kill_descendants(desc, skip_pgids=(pgid,))


def _install_signal_handlers():
    def handler(signum, frame):
        _kill_all_children()
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, handler)
        except Exception:
            pass
    atexit.register(_kill_all_children)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def proc_start(pid):
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def seat_process_live(s):
    """True only if the recorded PID is alive AND provably the same process (recorded start time matches).
    Without a recorded identity the answer is False: we never signal a PID we cannot prove is ours."""
    pid = s.get("pid")
    rec = s.get("pid_start")
    if not pid or not rec or not pid_alive(pid):
        return False
    return proc_start(pid) == rec


def _supervisor_live(s):
    sup, rec = s.get("supervisor_pid"), s.get("supervisor_start")
    return bool(sup) and bool(rec) and pid_alive(sup) and proc_start(sup) == rec


def seat_supervised_live(s):
    """Between attempts, right after a child exits, or on the ladder, a seat has no live child but its supervising engine
    (identity verified) is still driving it: that seat is live."""
    st = s.get("state")
    if st == "waiting":
        return seat_waiting_live(s)
    return st in ("running", "starting") and _supervisor_live(s)


def seat_live(s):
    return (s.get("state") == "running" and seat_process_live(s)) or seat_supervised_live(s)


def seat_waiting_live(s):
    """A seat parked on the usage-limit ladder is live only while its supervising engine is provably alive (recorded identity
    matches) and its retry time has not passed by more than a grace period."""
    sup, rec = s.get("supervisor_pid"), s.get("supervisor_start")
    if s.get("state") != "waiting" or not sup or not rec or not pid_alive(sup) or proc_start(sup) != rec:
        return False
    until = s.get("waiting_until")
    return not until or time.time() < until + WAITING_GRACE


class Clock:
    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout if timeout is not None else None
        self.linked = []  # clocks that move with this one's uncharged credits (a routed seat's wait budget, B-4)

    def remaining(self):
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def extend(self, seconds):
        """An uncharged interval (a throttle or capacity wait): the deadline, and every linked clock's, moves by it."""
        if self.deadline is not None:
            self.deadline += seconds
        for c in self.linked:
            c.extend(seconds)


class WaitBudget:
    """A routed seat's wait allowance (B-4): BRIDGE_ROUTED_MAX_WAIT from the seat's start, and the seat's own live Clock when that
    is sooner. Both move with the seat's uncharged waits (the limit is linked to the seat Clock), so a hold longer than --timeout
    does not leave a routed seat without a budget for the busy refusal after it; BRIDGE_ROUTED_MAX_WAIT stays its own limit."""

    def __init__(self, limit, clock):
        self.limit = Clock(limit)
        self.clock = clock
        if clock is not None:
            clock.linked.append(self.limit)

    def remaining(self):
        r = self.limit.remaining()
        c = self.clock.remaining() if self.clock is not None else None
        return r if c is None else min(r, c)

    def expired(self):
        return self.remaining() <= 0


def interruptible_sleep(seconds, d, clock=None):
    """Sleep in slices; return False if cancelled or the clock ran out."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if is_cancelled(d) or (clock and clock.expired()):
            return False
        time.sleep(max(0.0, min(2.0, end - time.monotonic())))
    return True


# ============================================================================ executables
_valid_cache = {}  # path -> (the probe succeeded, the first line `--version` printed)


def _probe_timeout(deadline):
    """A probe's own cap, or what is left of the refresh's budget when it runs inside one (Astra C3b-2); None: nothing is left."""
    if deadline is None:
        return PROBE_TIMEOUT
    remaining = deadline - time.monotonic()
    return min(PROBE_TIMEOUT, remaining) if remaining > 0 else None


def probe_ok(path, env=None, deadline=None):
    """Run `<path> --version` once per process; keep its outcome and first line (the version the model floors are checked against).
    A probe the refresh's budget skips or cuts short is not a failed probe: nothing is cached, and discovery outside the refresh
    probes again with the normal timeout (Astra C4b-3)."""
    key = str(path)
    if key in _valid_cache:
        return _valid_cache[key][0]
    timeout = _probe_timeout(deadline)
    if timeout is None:
        return False  # skipped: the refresh's budget is spent
    ok, line = False, ""
    try:
        real = os.path.realpath(path)
        if os.path.isfile(real) and os.access(real, os.X_OK):
            r = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, env=env or os.environ)
            ok = r.returncode == 0
            lines = (r.stdout or "").strip().splitlines()
            line = lines[0].strip() if lines else ""
    except subprocess.TimeoutExpired:
        if timeout < PROBE_TIMEOUT:
            return False  # cut short by the refresh's budget
    except Exception:
        ok = False
    _valid_cache[key] = (ok, line)
    return ok


def cli_version(path, env=None):
    """The first line `<path> --version` printed (probing once if needed); '' when it could not be run."""
    probe_ok(path, env=env)
    return _valid_cache.get(str(path), (False, ""))[1]


def _is_our_shim(path):
    try:
        with open(path, "rb") as f:
            head = f.read(400)
        return SHIM_MARKER.encode() in head
    except Exception:
        return False


_cap_cache = {}


def codex_capable(path, deadline=None):
    """The engine needs `codex exec --json --output-schema -o`; an older CLI that lacks them is skipped. As in probe_ok, a probe
    the refresh's budget skips or cuts short caches nothing (Astra C4b-3)."""
    key = str(path)
    if key in _cap_cache:
        return _cap_cache[key]
    timeout = _probe_timeout(deadline)
    if timeout is None:
        return False  # skipped: the refresh's budget is spent
    ok = False
    try:
        r = subprocess.run([str(path), "exec", "--help"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        ok = r.returncode == 0 and all(flag in r.stdout for flag in ("--json", "--output-schema", "--output-last-message", "--ignore-user-config"))
    except subprocess.TimeoutExpired:
        if timeout < PROBE_TIMEOUT:
            return False  # cut short by the refresh's budget
    except Exception:
        ok = False
    _cap_cache[key] = ok
    return ok


def fixed_root():
    """BRIDGE_CODEX_FIXED_ROOT: the path prefix of the default fixed directories (unset or empty: the real directories)."""
    return (os.environ.get("BRIDGE_CODEX_FIXED_ROOT") or "").rstrip("/")


def _codex_candidates():
    """Every codex to consider, in order: each `codex` on PATH (skipping the app launchers' realpaths and our own shim), the app
    launchers (the new bundle layout, then the legacy one), ~/.local/bin, then /opt/homebrew/bin and /usr/local/bin beneath the
    fixed root. One entry per real file."""
    launcher_reals = {os.path.realpath(p) for p in APP_LAUNCHERS}
    cands, seen = [], set()

    def add(c):
        real = os.path.realpath(c)
        if real not in seen:
            seen.add(real)
            cands.append(c)

    for d in os.environ.get("PATH", "").split(os.pathsep):
        c = os.path.join(d, "codex") if d else None
        if c and os.path.isfile(c) and os.access(c, os.X_OK) and os.path.realpath(c) not in launcher_reals and not _is_our_shim(c):
            add(c)
    for launcher in APP_LAUNCHERS:
        if os.path.isfile(launcher):
            add(launcher)
    root = fixed_root()
    for extra in [str(Path.home() / ".local/bin/codex")] + [f"{root}{d}/codex" for d in CODEX_FIXED_DIRS]:
        if os.path.isfile(extra):
            add(extra)
    return cands


def resolve_codex(deadline=None):
    """CODEX_BIN (authoritative when set); else the candidates of _codex_candidates() in order, each probed and checked for the
    engine's capability floor: the first whose version meets the highest Codex model floor of the table wins, else the first
    capable one. Returns the path, or None. Inside a refresh, every probe is bounded by the refresh's `deadline` too."""
    if os.environ.get("CODEX_BIN"):
        c = os.environ["CODEX_BIN"]
        return c if (os.path.isfile(c) and probe_ok(c, deadline=deadline) and codex_capable(c, deadline)) else None
    t, _err = table_or_error()
    floor = bridge_models.parse_min_cli(t.highest_floor("codex")) if t is not None and t.highest_floor("codex") else None
    first_capable = None
    for c in _codex_candidates():
        if not (probe_ok(c, deadline=deadline) and codex_capable(c, deadline)):
            continue
        if first_capable is None:
            first_capable = c
        if floor is None:
            return c
        version = bridge_models.parse_version("codex", cli_version(c))
        if version is not None and version >= floor:
            return c
    return first_capable


def resolve_claude():
    """CLAUDE_BIN (authoritative when set) → claude on PATH → the usual install locations."""
    if os.environ.get("CLAUDE_BIN"):
        c = os.environ["CLAUDE_BIN"]
        return c if (os.path.isfile(c) and probe_ok(c, env=clean_env())) else None
    cands = [shutil.which("claude"), str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    for c in cands:
        if c and os.path.isfile(c) and probe_ok(c, env=clean_env()):
            return c
    return None


# ============================================================================ the relay account router
_resolved_router = {}
_router_states = {}  # (job dir, vendor) -> routed | unrouted | off, the answer this engine process already has
_router_lock = threading.Lock()


def _json_blob(text):
    """The JSON object in a command's stdout, tolerating a line of noise around it; None when there is none."""
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


_FRACTION_RE = re.compile(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$")


def _epoch(v):
    """Epoch seconds from a relay timestamp, in the forms of the bridge↔relay contract: epoch seconds (int, float, or a numeric
    string — what relay emits, `verified_at` being `time.time()`), RFC3339 with an explicit offset, RFC3339 ending in `Z`
    (normalised to +00:00: Python 3.9's fromisoformat cannot read `Z`), or a naive ISO string, read as UTC (never as local
    time, which would shift a fresh verification by the machine's zone offset). Anything else — including NaN, infinite or
    unrepresentable values (1e100, "Infinity") — counts as missing, so the seat's bounded-retry policy applies instead of a
    traceback from datetime formatting."""
    if v is None or isinstance(v, bool):
        return None
    ts = None
    try:
        ts = float(v)
    except (TypeError, ValueError):
        s = str(v).strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        m = _FRACTION_RE.match(s)
        if m:  # fromisoformat (3.9) accepts exactly 3 or 6 fractional digits; RFC3339 allows any number
            s = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            ts = dt.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if ts is None or not math.isfinite(ts):
        return None
    try:
        datetime.fromtimestamp(ts)  # representable as a local datetime? (rejects 1e100 and the like)
    except (OverflowError, OSError, ValueError):
        return None
    return ts


def _find_account(data, label):
    """The account record for `label` in a relay account listing: a list of records with `label`, or a registry keyed by
    label, possibly nested under one key. None when it is not there."""
    if isinstance(data, dict):
        if data.get("label") == label:
            return data
        if isinstance(data.get(label), dict):
            return dict(data[label], label=label)
        for v in data.values():
            if isinstance(v, (dict, list)):
                found = _find_account(v, label)
                if found is not None:
                    return found
    elif isinstance(data, list):
        for v in data:
            found = _find_account(v, label)
            if found is not None:
                return found
    return None


def resolve_router():
    """BRIDGE_ROUTER=off → None. Otherwise RELAY_BIN (authoritative when set, like CODEX_BIN) → the skills-sync path
    ~/.claude/skills/relay/scripts/relay.py → `relay` on PATH. The answer is cached for the process."""
    if "router" in _resolved_router:
        return _resolved_router["router"]
    router = None
    if ROUTER_ON:
        cands = [os.environ["RELAY_BIN"]] if os.environ.get("RELAY_BIN") else [str(Path.home() / ".claude/skills/relay/scripts/relay.py"), shutil.which("relay")]
        for c in cands:
            if c and os.path.isfile(c) and os.access(c, os.R_OK if c.endswith(".py") else os.X_OK):
                router = Router(c)
                break
    _resolved_router["router"] = router
    return router


class Router:
    """relay as the seats see it. Every call is a timed, registered subprocess (killed with its group on timeout) and is
    recorded in the seat's router trail. `pick` answers follow the versioned protocol: only {"v": 1, "result": "ok"} routes a
    seat; none/busy carry `wake_at`; no_accounts, a non-JSON answer, another protocol version or exit 2 (an old relay without
    `pick`) mean the vendor is unrouted."""

    def __init__(self, path):
        self.path = path
        self.argv0 = [sys.executable, path] if path.endswith(".py") else [path]

    def _run(self, args, timeout, d=None, tag=None, trail=None):
        """(rc, stdout, stderr); rc is None when relay could not be run, was cancelled, or did not answer within `timeout`.
        The helper is registered (so the signal handlers and global shutdown reach its group) and, while we wait, the job's
        CANCELLED marker is polled and its group killed at once — a cancel during a slow pick does not leave relay or its
        probes running. A KeyboardInterrupt (doctor under Ctrl-C) also kills the group before propagating."""
        key = f"{tag or 'router'}/relay-{uuid.uuid4().hex[:6]}"
        t0 = time.time()
        p = None

        def drain():
            try:
                p.communicate(timeout=2)
            except Exception:
                pass
        try:
            p = subprocess.Popen(self.argv0 + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True, errors="replace")
            p._bridge_pgid = p.pid
            _register(key, p)
            p._bridge_start = proc_start(p.pid)
            deadline = time.time() + max(0.1, timeout)
            while True:
                if d is not None and is_cancelled(d):
                    kill_group(p, grace=1.0)
                    drain()
                    rc, out, err = None, "", f"relay {args[0]} cancelled"
                    break
                remaining = deadline - time.time()
                try:
                    out, err = p.communicate(timeout=min(0.25, remaining) if remaining > 0 else 0.05)
                    rc = p.returncode
                    break
                except subprocess.TimeoutExpired:
                    if time.time() >= deadline:
                        kill_group(p, grace=1.0)
                        drain()
                        rc, out, err = None, "", f"relay {args[0]} did not answer within {fmt_secs(timeout)}"
                        break
        except OSError as exc:
            rc, out, err = None, "", f"could not run relay: {exc}"
        except BaseException:  # a signal or KeyboardInterrupt in doctor: never leave the helper's group behind
            if p is not None:
                kill_group(p, grace=1.0)
            raise
        finally:
            if p is not None:
                _unregister(key)
        if trail:
            try:
                fd = open_private(trail, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
                with os.fdopen(fd, "a") as f:  # `out` keeps relay's whole answer: a pick's rows give each account's reason
                    f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "args": list(args), "rc": rc,
                                        "secs": round(time.time() - t0, 1), "out": short(out, 4000), "err": short(err, 300)}) + "\n")
            except OSError:
                pass
        return rc, out or "", err or ""

    def pick(self, vendor, model=None, timeout=None, d=None, tag=None, trail=None, only=None):
        """dict(kind=ok, label, info) | dict(kind=none|busy, wake_at, reason) | dict(kind=unrouted, reason) | dict(kind=error, reason)
        | dict(kind=unsupported, reason). `busy` is relay's verdict that every account's slots are in use: a short wait, not a limit
        (BUSY-1). With `only`, the capable set of docs/model-routing.md 3.3, the request carries `--only`
        and is valid only as a protocol-2 answer with only_applied true and a label inside the set: an old relay's exit 2, a v1
        answer, a missing field or an account outside the set is `unsupported`, never the unchecked local login."""
        args = ["pick", "--vendor", vendor, "--json"] + (["--model", model] if model else []) + (["--only", ",".join(only)] if only else [])
        rc, out, err = self._run(args, timeout or ROUTER_TIMEOUTS["pick"], d=d, tag=tag, trail=trail)
        if rc is None:
            return dict(kind="error", reason=short(err, 160))
        j = _json_blob(out)
        if only:
            if j is None:
                return dict(kind="unsupported", reason="no --only support (exit 2, no JSON)" if rc == 2 else f"the pick answered without JSON (exit {rc})")
            if j.get("v") != CONSTRAINED_PROTOCOL or j.get("only_applied") is not True:
                return dict(kind="unsupported", reason=f"the pick answered protocol {j.get('v')!r} with only_applied {j.get('only_applied')!r}, "
                                                       f"not v{CONSTRAINED_PROTOCOL} with the set applied")
            res = j.get("result")
            if res == "ok" and j.get("label"):
                if str(j["label"]) not in only:
                    return dict(kind="unsupported", reason=f"the pick named account {j['label']}, outside the capable set")
                return dict(kind="ok", label=str(j["label"]), info=j)
            if res in ("none", "busy"):
                return dict(kind=res, wake_at=_epoch(j.get("wake_at")), reason=str(j.get("reason") or res), info=j)
            return dict(kind="unsupported", reason=f"no {vendor} accounts are registered" if res == "no_accounts" else f"the pick answered {res!r} (exit {rc})")
        if j is None:
            return dict(kind="unrouted", reason="relay has no pick command (exit 2)" if rc == 2 else f"relay pick answered without JSON (exit {rc})")
        if j.get("v") != ROUTER_PROTOCOL:
            return dict(kind="unrouted", reason=f"relay pick speaks protocol {j.get('v')!r}, not v{ROUTER_PROTOCOL}")
        res = j.get("result")
        if res == "ok" and j.get("label"):
            return dict(kind="ok", label=str(j["label"]), info=j)
        if res in ("none", "busy"):
            return dict(kind=res, wake_at=_epoch(j.get("wake_at")), reason=str(j.get("reason") or res), info=j)
        return dict(kind="unrouted", reason=f"relay has no {vendor} accounts" if res == "no_accounts" else f"relay pick answered {res!r} (exit {rc})")

    def hit(self, vendor, label, model=None, timeout=None, d=None, tag=None, trail=None):
        """Tell relay the CLI rejected a request under `label` (it blocks the account until its reset). (ok, reset epoch|None).
        The reset comes from `relay hit … --json` ({"v": 1, …, "blocked_until": <epoch|null>}); a chatty, non-JSON answer (an
        older relay) never fails the seat — the reset is simply unknown."""
        args = ["hit", label, "--vendor", vendor, "--json"] + (["--model", model] if model else [])
        rc, out, err = self._run(args, timeout or ROUTER_TIMEOUTS["hit"], d=d, tag=tag, trail=trail)
        j = _json_blob(out) or {}
        until = _epoch(j.get("blocked_until"))
        if until is None:  # tolerate an older key spelling; absent/none stays unknown
            until = _epoch(j.get("until") or j.get("wake_at"))
        return rc == 0, until

    def exec_prefix(self, vendor, label, model=None):
        """The command prefix that runs a CLI as `label`; relay execs into the CLI (same pid and group). No --force: relay's
        ceilings and blocks apply to the account, exactly as `pick` judged them."""
        return self.argv0 + ["exec", "--vendor", vendor, "--account", label] + (["--model", model] if model else []) + ["--"]

    def transplant(self, session_id, label, cwd=None, timeout=None, d=None, tag=None, trail=None):
        """Copy a Claude session into `label`'s store so `claude --resume` works there. (ok, message)."""
        args = ["transplant", session_id, "--to", label] + (["--cwd", cwd] if cwd else [])
        rc, out, err = self._run(args, timeout or ROUTER_TIMEOUTS["transplant"], d=d, tag=tag, trail=trail)
        return rc == 0, short((err or out).strip(), 200)

    def account_status(self, vendor, label, asked, timeout=None, d=None, tag=None, trail=None):
        """A FRESH live verification of `label`'s sign-in via `relay [codex] accounts --verify --json` (relay re-reads the
        live login with --verify; a bare listing is cached). dict(ok, email, reason). `ok` is True only for a fresh successful
        observation: the record exists, is signed_in, carries an email, and its `verified_at` is at or after `asked` (the
        moment we asked). Anything else — an error, a signed-out or missing account, a cached (older `verified_at`) reading —
        is `ok=False` with the reason, so a pinned reply refuses rather than spending a replacement sign-in."""
        args = (["codex", "accounts"] if vendor == "codex" else ["accounts"]) + ["--verify", "--json"]
        rc, out, err = self._run(args, timeout or ROUTER_TIMEOUTS["accounts"], d=d, tag=tag, trail=trail)
        if rc is None:
            return dict(ok=False, email=None, reason=short(err, 120) or "relay did not answer")
        if rc != 0:
            return dict(ok=False, email=None, reason=f"relay cannot verify accounts (exit {rc}); an older relay without --verify")
        try:
            data = json.loads(out)
        except Exception:
            return dict(ok=False, email=None, reason="relay gave no parseable account listing")
        rec = _find_account(data, label)
        if rec is None:
            return dict(ok=False, email=None, reason=f"relay no longer lists account {label}")
        if not rec.get("signed_in", False):
            return dict(ok=False, email=rec.get("email"), reason=f"account {label} is signed out")
        ver = _epoch(rec.get("verified_at"))
        if ver is None or ver < asked:
            return dict(ok=False, email=rec.get("email"), reason=f"relay returned a cached, unverified reading for {label}")
        email = rec.get("email")
        if not email:
            return dict(ok=False, email=None, reason=f"relay reported no email for {label}")
        return dict(ok=True, email=str(email), reason="verified live")

    def codex_home(self, label, timeout=None, d=None, tag=None, trail=None):
        """The CODEX_HOME of a Codex account, where its threads' rollout files live (docs/model-routing.md 6): the path relay prints,
        or None when relay cannot say (an older relay without the command)."""
        rc, out, err = self._run(["codex", "home", label], timeout or ROUTER_TIMEOUTS["accounts"], d=d, tag=tag, trail=trail)
        lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
        return lines[-1] if rc == 0 and lines else None

    def codex_labels(self, timeout=None, d=None, tag=None, trail=None):
        """The labels of relay's signed-in Codex accounts, the candidates of a capable set (docs/model-routing.md 3.3), or None when
        relay cannot list them (an older relay, an unreadable answer)."""
        rc, out, err = self._run(["codex", "accounts", "--json"], timeout or ROUTER_TIMEOUTS["accounts"], d=d, tag=tag, trail=trail)
        if rc != 0:
            return None
        try:
            data = json.loads(out)
        except Exception:
            return None
        rows = data.get("accounts") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None
        labels = []
        for row in rows:
            if isinstance(row, dict) and row.get("label") and row.get("signed_in", True) and not row.get("duplicate_of"):
                labels.append(str(row["label"]))
        return labels


def _set_router_state(d, vendor, state):
    """Remember, for this engine process and in meta.json (`router: {vendor: routed|unrouted|off}`), how the vendor's seats run."""
    with _router_lock:
        _router_states[(str(d), vendor)] = state
    with job_lock(d):
        m = read_json(Path(d) / "meta.json")
        router = dict(m.get("router") or {})
        if router.get(vendor) != state:
            router[vendor] = state
            m["router"] = router
            atomic_write_json(Path(d) / "meta.json", m)


def job_router(d, vendor):
    """The Router a seat of this vendor should ask, or None when routing is off, relay is missing, or relay already told this
    process that the vendor has no accounts (no seat of the job asks again)."""
    with _router_lock:
        state = _router_states.get((str(d), vendor))
    if state in ("unrouted", "off"):
        return None
    router = resolve_router()
    if router is None:
        _set_router_state(d, vendor, "off")
        return None
    if state is None:
        _set_router_state(d, vendor, "routed")  # provisional: the first pick may still answer that the vendor has no accounts
    return router


# ============================================================================ seats
class Seat:
    """One seat: the vendor, the full model ID that runs (`model`, moved by a fallback), the effort (None: the model takes no
    effort flag), the family (what relay receives) and how the seat was named (`seat_name`, `model_requested`)."""

    def __init__(self, vendor, model, effort, fast=False, tag=None, family=None, seat_name=None, effort_explicit=False, k1=False):
        self.vendor, self.model, self.effort, self.fast, self.tag = vendor, model, effort, fast, tag
        self.family = family or model
        self.family_requested = self.family
        self.model_requested = model
        self.seat_name = seat_name or self.family
        self.effort_explicit = effort_explicit
        self.k1 = k1
        self.preference = []          # (model, family) after the first name of `astra|sol`: the only descent the seat allows (docs/model-routing.md 5)
        self.effort_preference = []   # the efforts after the first of `ultra|max`, likewise
        self.downgraded_from = None   # the family the seat left through its preference list, when it did

    def label(self):
        return self.model + (f"@{self.effort}" if self.effort else "") + ("@fast" if self.fast else "") + (" (K1 seat)" if self.k1 else "")

    def relay_model(self):
        """What relay receives in pick/hit/exec --model: the family alias (the K1 seat's family is opus), never the full ID."""
        return self.family


def _seat_of(s):
    """A Seat from the table's parsed SeatSpec, its explicit preference lists included."""
    seat = Seat(s.vendor, s.model, s.effort, s.fast, s.tag, family=s.family, seat_name=s.seat_name, effort_explicit=s.explicit, k1=s.k1)
    seat.preference = [(x.model, x.family) for x in getattr(s, "preference", []) or []]
    seat.effort_preference = list(getattr(s, "effort_preference", []) or [])
    return seat


def parse_seat(spec, effort_override=None, source=None):
    """One seat spec -> Seat, or die() with one line (DESIGN §2). The grammar and the resolution live in the table
    (Table.parse_seat, shared with the check's `defaults` finding); `source` names the environment variable a default seat
    list came from, so that its refusal names it."""
    t = table()  # first: a missing table, or a missing table reader, is the one-line error a seat spec gets
    try:
        return _seat_of(t.parse_seat(spec, effort_override, EFFORT_ENV, source))
    except bridge_models.Refusal as exc:
        die(str(exc))


def parse_seats(spec, default, effort_override=None, tag_prefix=None, used_tags=None, default_var=None):
    raw = spec or default
    # an environment default is resolved at first use and named in any refusal; a built-in list (the engine's own, or the one a
    # Codex-side entry point passed in BRIDGE_BUILTIN_*) names no variable (B-opus-3, RR-8, S-DEF)
    source = default_var if not spec and default_var and default_var in os.environ else None
    if not spec and effort_override:  # defaults carry their own efforts (a `|` list, spaces and all); an explicit --effort replaces the whole
        raw = ",".join(re.sub(r":[^@]*", "", part) for part in raw.split(","))  # segment: from the colon to the @fast modifier or the end
    t = table()
    try:
        seats = [_seat_of(s) for s in t.parse_seat_list(raw, effort_override, EFFORT_ENV, source)]
    except bridge_models.Refusal as exc:
        die(str(exc))
    used = used_tags if used_tags is not None else set()
    for s in seats:
        base = s.tag  # the family, or the K1 seat's own name
        if tag_prefix:
            base = tag_prefix if len(seats) == 1 else f"{tag_prefix}-{base}"
        tag, n = base, 1
        while tag in used:
            n += 1
            tag = f"{base}-{n}"
        if not TAG_RE.match(tag):
            tag = re.sub(r"[^A-Za-z0-9_.-]+", "-", tag).strip("-.") or "seat"
            while tag in used:
                n += 1
                tag = f"{tag}-{n}"
        used.add(tag)
        s.tag = tag
    return seats


def _cli_facts(vendor):
    """{"path", "version"} of the CLI a vendor's seats will run (the first line of its --version), or None when none resolves:
    the launch then reports the missing binary itself."""
    path = resolve_codex() if vendor == "codex" else resolve_claude()
    if not path:
        return None
    return {"path": path, "version": cli_version(path, env=None if vendor == "codex" else clean_env())}


def _resolved(seat):
    return bridge_models.Resolved(seat.vendor, seat.model, seat.family, seat.tag, seat.k1)


def validate_seats(seats):
    """DESIGN §3: the launch validator in `initial` mode on every seat of a NEW job, before any job record exists: table
    membership, the sticky lifecycle markers, the effort, and the resolved CLI's version against the model's floor. Any failing
    check refuses with one line (exit 2) before relay is asked or a CLI launches."""
    t, ev = table(), evidence()
    facts = {}
    for s in seats:
        if s.vendor not in facts:
            facts[s.vendor] = _cli_facts(s.vendor)
        try:
            bridge_models.validate(t, ev, _resolved(s), s.effort, "initial", facts[s.vendor])
        except bridge_models.Refusal as exc:
            die(str(exc))
    return seats


def _relay_claude_bin():
    """The Claude CLI relay would run its workers on, resolved exactly as relay.resolve_claude() does (Astra C3-12): RELAY_CLAUDE_BIN
    expanded, made absolute and taken only when executable; else `claude` on PATH; else relay's fallback directories, executable
    files only."""
    if os.environ.get("RELAY_CLAUDE_BIN"):
        b = os.path.abspath(os.path.expanduser(os.environ["RELAY_CLAUDE_BIN"]))
        return b if os.access(b, os.X_OK) else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    for extra in (str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if os.access(extra, os.X_OK):
            return extra
    return None


def local_facts():
    """The local inputs of the composed verdict (DESIGN §5), read right now without any fetch: the resolved CLIs' versions, the
    Claude CLI relay would run, the presence of the override and default variables (names and the default lists only), and the
    built-in list (a Codex-side entry point's, else the engine's own) that stands in for each default variable the user left
    unset, so that the check validates every effective default list and labels its origin (S-DEF)."""
    codex, claude, relay_claude = resolve_codex(), resolve_claude(), _relay_claude_bin()
    names = bridge_models.HOST_OVERRIDE_VARS + bridge_models.DEFAULT_SEAT_VARS + ("BRIDGE_CODEX_EFFORT", "BRIDGE_CLAUDE_EFFORT")
    return {"codex_version": cli_version(codex) if codex else None, "codex_bin": codex,
            "claude_version": cli_version(claude, env=clean_env()) if claude else None, "claude_bin": claude,
            "relay_claude_version": cli_version(relay_claude, env=clean_env()) if relay_claude else None, "relay_claude_bin": relay_claude,
            "env": {k: os.environ[k] for k in names if k in os.environ},
            "builtin_defaults": {var: default_seats(var)[0] for var in bridge_models.DEFAULT_SEAT_VARS if var not in os.environ}}


def catalog_dir():
    """The jobs root's parent, where the check's app-server runs and the evidence file lives by default: created private (0700)
    when it does not exist yet, before the first source read and without creating any job (Astra C3-11)."""
    return str(bridge_models.private_dir(JOBS.parent))


def read_sources(t, deadline):
    """The check's reader (DESIGN §5), run UNDER the evidence lock within ONE total budget (S-C3, A1: `deadline`, a monotonic
    moment): the local Codex catalog through the resolved codex in the local CODEX_HOME (one app-server in the jobs root's
    parent), then the three pages at their URLs (or the test hooks). Every read is also capped by the table's per-source timeout."""
    found = {}
    cwd = catalog_dir()
    codex = resolve_codex(deadline)  # discovery and its probes spend the same budget (Astra C3b-2)
    if codex:
        answer, error = bridge_models.read_codex_catalog(codex, cwd, float(t.policy.get("catalog_timeout_s") or 20), env=os.environ.copy(), deadline=deadline)
    else:
        answer, error = None, "no usable codex resolved (CODEX_BIN, PATH, the app launcher)"
    observation = bridge_models.retain_catalog(t, answer) if answer else None
    if observation is not None:  # the CLI that read the catalog: an automatic promotion's floor (DECISIONS A28)
        observation["cli"] = bridge_models.first_line(cli_version(codex))
    found["codex"] = {"read_at": time.time(), "observation": observation, "error": error}
    urls = bridge_models.source_urls()
    for name in ("npm", "deprecations", "overview"):  # each fetch is an isolated child in the private catalog directory (A-opus-1)
        text, error = bridge_models.fetch_page(urls[name], float(t.policy.get("fetch_timeout_s") or 5), deadline=deadline, cwd=cwd)
        observation = None
        if text is not None:
            observation, error = bridge_models.retain_page(name, text)
        found[name] = {"read_at": time.time(), "observation": observation, "error": error}
    return found


def refresh_under_lock(ev, t, data):
    """A refresh, holding the evidence lock: the sources read within the refresh's total budget, then published in place; then the
    promotion pass over the Codex catalog THIS refresh read (DECISIONS A28), against the ledger as it is under the lock. The ledger is
    written before the evidence, and the evidence is evaluated under the table the pass produced. Returns (data, the table now in
    force, the steps the pass added)."""
    observations = read_sources(t, time.monotonic() + bridge_models.fetch_budget())
    now = time.time()
    bridge_models.apply_refresh(data, t, observations, now)
    after, steps = t, []
    catalog = (observations.get("codex") or {}).get("observation")
    if promotion_mode(t)[0] == "auto":
        data["promotion_pass_at"] = now  # attempted, read or not: a failed read is retried by the next daily re-check, not every call
    if catalog is not None and promotion_mode(t)[0] == "auto":
        path = ev.promotions_path
        try:
            ledger = bridge_models.load_promotions(path)
            ledger, after, steps = bridge_models.promote(t.base, ledger, dict(catalog, read_at=observations["codex"].get("read_at")),
                                                         data.get("markers"), now, path)
            bridge_models.write_private_json(path, ledger)
        except (bridge_models.TableError, OSError) as exc:  # the check itself stands; the pass is reported and tried again next time
            sys.stderr.write(f"[bridge] models: the automatic promotion pass did not run: {re.sub(chr(10), ' ', str(exc))}\n")
            after, steps = t, []
    if after.sha256 != t.sha256:
        bridge_models.finish_evaluation(data, after, now, refreshed=True)
    ev.write(data)
    return data, after, steps


def promotion_mode(t):
    """('auto' | 'off', where that comes from): BRIDGE_MODELS_PROMOTE=off stops new automatic promotions for the processes that see it
    (the tests' switch), the table's policy.auto_promote=false stops them everywhere; promotions already made stay in force until
    undone (nothing is demoted automatically)."""
    v = (os.environ.get("BRIDGE_MODELS_PROMOTE") or "").strip().lower()
    if v:
        return ("off" if v in ("off", "0", "false", "no") else "auto"), "BRIDGE_MODELS_PROMOTE"
    return ("auto" if t.policy.get("auto_promote", True) is not False else "off"), "models.json policy.auto_promote"


def announce_promotions(steps):
    """One stderr line per step a check just added: the head changed on this Mac without anyone editing models.json."""
    for s in steps:
        sys.stderr.write(f"[bridge] models: {s['family']} promoted automatically: {s['from']} -> {s['to']} (this Mac's own Codex catalog lists it, "
                         f"{s.get('cli') or 'codex'}); bridge.py doctor shows it, --undo-promotion {s['family']} --reason \"<why>\" reverses it\n")


def pre_check(force=False):
    """DESIGN §3 step 1, in the parent before anything is validated, under the S-C3 single-writer rule. A model-starting
    invocation reads the evidence ONCE; when the daily re-check is due it tries the lock without waiting: it refreshes when it
    gets it (re-checking that the refresh is still due), else it goes on from the snapshot it read with a `refresh-deferred`
    finding (A2). `doctor` (`force`) waits for the lock up to its deadline, then refreshes unless another process refreshed while
    it waited (then it only re-records a changed table's digest), or that refresh skipped a promotion pass that is due here; past
    the deadline it reports the snapshot as busy (A1). With promotion on, a model-starting invocation also refreshes when the last
    promotion pass is older than the re-check window (DECISIONS A28). The verdict is composed from that one snapshot, recomputed in
    memory when the table changed, plus the current local facts. The table in force after the check (the shipped table with this
    Mac's promotions, after any pass) becomes the process's table for every later step of the invocation. Never inside or against
    a running job; nothing here launches a model or calls relay."""
    t, ev = table(), evidence()
    started = time.time()
    flags, steps = {}, []
    if force:
        try:
            with ev.lock(deadline=bridge_models.lock_deadline()):
                data = ev.load()
                now = time.time()
                # another process's refresh while doctor waited stands in for doctor's own, unless it skipped the promotion pass (an
                # older engine, or BRIDGE_MODELS_PROMOTE=off there) while promotion is on here (DECISIONS A28)
                if not bridge_models.refreshed_since(data, started, now) or (
                        promotion_mode(t)[0] == "auto" and bridge_models.promotion_due(data, t, now)):
                    data, t, steps = refresh_under_lock(ev, t, data)
                else:  # another process refreshed while doctor waited: its promotion pass may have changed the table
                    t = reload_table()
                    if data.get("table_sha256") != t.sha256:
                        bridge_models.finish_evaluation(data, t, time.time(), refreshed=False)
                        ev.write(data)
        except bridge_models.EvidenceBusy:
            data, flags = ev.load(), {"busy": True}
    else:
        auto = promotion_mode(t)[0] == "auto"

        def due(snapshot, now):  # the daily re-check, or a promotion pass an older engine's refresh skipped (DECISIONS A28)
            return bridge_models.refresh_due(snapshot, t, now) or (auto and bridge_models.promotion_due(snapshot, t, now))
        data = ev.load()
        if due(data, started):
            try:
                with ev.lock(blocking=False):
                    data = ev.load()
                    if due(data, time.time()):
                        data, t, steps = refresh_under_lock(ev, t, data)
                    else:  # refreshed by another process since this one read the store: so may the ledger have been
                        t = reload_table()
            except bridge_models.EvidenceBusy:
                flags = {"deferred": True}
    _table_cache["table"] = (t, None)  # every later reader in this process (seat parsing, validation, routing) uses this table
    announce_promotions(steps)
    check = ev.compose(t, local_facts(), time.time(), data=data, **flags)
    check["promotions"] = promotion_summary(t)
    return check


def promotion_summary(t):
    """What the composed check says of this Mac's automatic promotions (value-free; doctor --json prints it)."""
    return {"ledger": t.promotion.get("ledger"), "applied": [{k: s.get(k) for k in ("family", "from", "to", "at", "min_cli", "cli")}
                                                              for s in t.promotion["applied"]],
            "skipped": [{"family": s.get("family"), "to": s.get("to"), "reason": why} for s, why in t.promotion["skipped"]],
            "blocked": [{"family": b.get("family"), "id": mid, "at": b.get("at"), "reason": b.get("reason")} for mid, b in t.blocked.items()],
            "ignored": [{"family": b.get("family"), "id": b.get("id"), "reason": why} for b, why in t.promotion.get("ignored") or []]}


def admission(check, refuse_allowed=True):
    """The stale gate (DESIGN §5) on the composed verdict: the notice to persist (a FAIL under `warn`, or an unverified verdict) or
    None. For a NEW job under `refuse`, a FAIL refuses before the job exists (exit 2) with the FAIL lines and the fix; a reply
    or a running job only ever gets the notice."""
    gate = (os.environ.get("BRIDGE_MODELS_GATE") or str(table().policy.get("stale_gate") or "warn")).strip().lower()
    if check["verdict"] == "FAIL":
        fails = [f["text"] for f in check["findings"] if f.get("class") in bridge_models.FAIL_CLASSES]
        if gate == "refuse" and refuse_allowed:
            sys.stderr.write("bridge: models check FAILED (BRIDGE_MODELS_GATE=refuse); the job was not started:\n"
                             + "".join(f"  {line}\n" for line in fails) + "Run: bridge.py doctor\n")
            sys.exit(EXIT_USAGE)
        return f"NOTICE: models: check FAILED ({'; '.join(fails[:2])}); run bridge.py doctor"
    if check["verdict"] == "UNVERIFIED":
        return f"NOTICE: models: unverified ({', '.join(bridge_models.unverified_summary(check['findings']))})"
    return None


def note_promotion(d, model):
    """The promotion notice (DECISIONS A28) when `model` is a head this Mac promoted automatically within the last
    PROMOTION_NOTICE_DAYS days: persisted once per job and at job scope, whichever seat runs it (a new job's seat, a reply, a seat
    that descended to it through its preference list), so that it leads every answer of the job, each seat's and reply's own
    last-<tag>.md included."""
    t, _err = table_or_error()
    text = bridge_models.promotion_notice(t, model) if t is not None else None
    if text:
        add_notice(d, "models-promotion", text, once=True)


def record_check(d, check, notice, seats=()):
    """The admitted verdict in meta.json `check`, the gate's notice (one per job) and, for each seat that runs a head this Mac
    promoted automatically within the last PROMOTION_NOTICE_DAYS days, the promotion's notice (DECISIONS A28), leading the job's
    outputs."""
    if notice:
        add_notice(d, "models-check", notice, once=True)
    for s in seats or ():
        note_promotion(d, s.model)
    update_meta(d, check={"verdict": check["verdict"], "evaluated_at": check["evaluated_at"],
                          "sources_refreshed_at": check.get("sources_refreshed_at"), "table_sha256": check.get("table_sha256")})


def sanitize_prefix(name):
    p = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name)).strip("-.")
    return p[:48] or "task"


# ============================================================================ git and the frozen patch
GIT_ENV_STRIP = ("GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_DIR", "GIT_WORK_TREE")


def git(cwd, *args, timeout=120):
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_STRIP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    cmd = ["git", "-c", "core.pager=cat", "-c", "diff.external=", "-c", "diff.noprefix=false", "-c", "color.ui=never",
           "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL, errors="replace")
    return r.returncode, r.stdout, r.stderr


def git_ok(cwd, *args, what="git"):
    rc, out, err = git(cwd, *args)
    if rc != 0:
        die(f"{what} failed: {short(err.strip() or out.strip(), 300)}", EXIT_FAIL)
    return out


def is_git_repo(cwd):
    rc, _, _ = git(cwd, "rev-parse", "--show-toplevel")
    return rc == 0


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def resolve_cwd(c):
    """The effective working directory: the git top level when there is one, but never outside BRIDGE_ROOT when that is set
    (promotion stops at the boundary and the given directory is kept; a cwd outside the root is refused)."""
    cwd = os.path.realpath(os.path.abspath(c or os.getcwd()))
    if not os.path.isdir(cwd):
        die(f"cwd does not exist: {cwd}")
    if BRIDGE_ROOT and not _inside(cwd, BRIDGE_ROOT):
        die(f"cwd {cwd} is outside the authorized root {BRIDGE_ROOT} (BRIDGE_ROOT)")
    if is_git_repo(cwd):
        rc, top, _ = git(cwd, "rev-parse", "--show-toplevel")
        if rc == 0 and top.strip():
            top = os.path.realpath(top.strip())
            if not BRIDGE_ROOT or _inside(top, BRIDGE_ROOT):
                return top
    return cwd


def resolve_rev(cwd, ref, what):
    if ref.startswith("-"):
        die(f"{what} looks like an option, not a revision: {ref!r}")
    rc, out, err = git(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    if rc != 0 or not out.strip():
        die(f"{what} {ref!r} does not resolve to a commit: {short(err.strip(), 200)}", EXIT_FAIL)
    return out.strip()


def _looks_binary(path):
    try:
        with open(path, "rb") as f:
            chunk = f.read(8000)
        return b"\0" in chunk
    except Exception:
        return True


def capture_change(cwd, base=None, commit=None):
    """Freeze the change under review. Returns dict(kind, scope, stat, patch, files, meta)."""
    if not is_git_repo(cwd):
        die("review needs a git repository (use `ask` for non-git questions)")
    diff_flags = ["--no-ext-diff", "--no-textconv", "--no-color"]
    # When the cwd is a subdirectory (BRIDGE_ROOT stopped the git-root promotion) the capture is limited to that subtree.
    rc0, top0, _ = git(cwd, "rev-parse", "--show-toplevel")
    top = os.path.realpath(top0.strip()) if rc0 == 0 and top0.strip() else cwd
    subtree = top != cwd
    spec = ["--", "."] if subtree else ["--"]
    inv_rev = None
    if commit:
        sha = resolve_rev(cwd, commit, "--commit")
        patch = git_ok(cwd, "show", *diff_flags, "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n    %s%n", "--patch", sha, *spec, what="git show")
        stat = git_ok(cwd, "show", "--stat=200", "--format=", sha, *spec, what="git show --stat")
        files = [f for f in git_ok(cwd, "show", "--name-only", "--format=", sha, *spec).splitlines() if f]
        meta = dict(kind="commit", commit=sha)
        inv_rev = sha
        scope = f"the changes introduced by commit {sha[:12]}"
    elif base:
        base_sha = resolve_rev(cwd, base, "--base")
        head = resolve_rev(cwd, "HEAD", "HEAD")
        mb = git_ok(cwd, "merge-base", base_sha, head, what="git merge-base").strip()
        patch = git_ok(cwd, "diff", *diff_flags, mb, head, *spec, what="git diff")
        stat = git_ok(cwd, "diff", "--stat=200", mb, head, *spec, what="git diff --stat")
        files = [f for f in git_ok(cwd, "diff", "--name-only", mb, head, *spec).splitlines() if f]
        meta = dict(kind="base", base=base, base_sha=base_sha, merge_base=mb, head=head)
        inv_rev = head
        scope = f"the changes on this branch since its merge-base {mb[:12]} with {base} (HEAD {head[:12]})"
    else:
        rc, head, _ = git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        head = head.strip() if rc == 0 else None
        if head:
            patch = git_ok(cwd, "diff", *diff_flags, "HEAD", *spec, what="git diff")
            stat = git_ok(cwd, "diff", "--stat=200", "HEAD", *spec, what="git diff --stat")
            files = [f for f in git_ok(cwd, "diff", "--name-only", "HEAD", *spec).splitlines() if f]
        else:
            patch = git_ok(cwd, "diff", *diff_flags, "--cached", *spec, what="git diff --cached")
            stat = git_ok(cwd, "diff", "--stat=200", "--cached", *spec, what="git diff --stat")
            files = [f for f in git_ok(cwd, "diff", "--name-only", "--cached", *spec).splitlines() if f]
        # untracked paths are repository-relative (--full-name) so patch headers, files and inventory share one namespace
        untracked = [u for u in git_ok(cwd, "ls-files", "--others", "--exclude-standard", "-z", "--full-name", *spec, what="git ls-files").split("\0") if u]
        skipped = []
        omitted = []
        for u in untracked:
            full = os.path.join(top, u)
            try:
                st_ = os.lstat(full)
            except OSError:
                omitted.append(f"{u} (unreadable)")
                continue
            import stat as _stat
            if _stat.S_ISLNK(st_.st_mode):
                try:
                    omitted.append(f"{u} (symlink -> {os.readlink(full)})")
                except OSError:
                    omitted.append(f"{u} (symlink)")
                skipped.append(u)
                continue
            if not _stat.S_ISREG(st_.st_mode):
                omitted.append(f"{u} (not a regular file)")
                skipped.append(u)
                continue
            if st_.st_size > MAX_UNTRACKED_BYTES or _looks_binary(full):
                omitted.append(f"{u} ({'binary' if st_.st_size <= MAX_UNTRACKED_BYTES else 'larger than 1 MB'})")
                skipped.append(u)
                continue
            rc, out, err = git(top, "diff", *diff_flags, "--no-index", "--", "/dev/null", u)  # from the top: headers are repo-relative
            if rc in (0, 1) and out:
                patch += out
                files.append(u)
        if untracked:
            stat += ("\n" if stat else "") + "untracked (included as additions):\n" + "\n".join(f"  {u}" for u in untracked if u not in skipped)
            if omitted:
                stat += "\nuntracked omitted from the patch:\n" + "\n".join(f"  {u}" for u in omitted)
                patch = ("# capture note: the following untracked entries are omitted from this patch and must be inspected in the tree if relevant:\n"
                         + "".join(f"#   {u}\n" for u in omitted) + patch)
        meta = dict(kind="uncommitted", head=head, untracked=untracked, skipped=skipped, omitted=omitted)
        scope = f"the uncommitted work relative to HEAD {(head or 'none')[:12]}: staged and unstaged changes plus untracked files"
    lines = patch.count("\n")
    inventory, complete = snapshot_inventory(cwd, spec, inv_rev, files)
    meta.update(files=files, lines=lines, sha256=sha256_text(patch), captured_at=datetime.now().isoformat(), cwd=cwd,
                inventory=inventory, inventory_complete=complete, subtree=bool(subtree))
    return dict(kind=meta["kind"], scope=scope, stat=stat.strip(), patch=patch, files=files, meta=meta, lines=lines)


def snapshot_inventory(cwd, spec, rev=None, changed=()):
    """Every repository-relative path in the REVIEWED snapshot: the tree of the reviewed revision (commit/base scopes) or
    the index plus untracked files (uncommitted scope), always unioned with every changed path including deletions, so the
    merger never mistakes a bare filename for an alias of the one survivor. Bounded; incomplete inventories are flagged."""
    if rev:
        rc, out, _ = git(cwd, "ls-tree", "-r", "-z", "--name-only", "--full-name", rev, *spec)
    else:
        rc, out, _ = git(cwd, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--full-name", *spec)
    if rc != 0:
        return sorted(set(changed)), False
    paths = sorted({p for p in out.split("\0") if p} | set(changed))
    if len(paths) > MAX_INVENTORY:
        return paths[:MAX_INVENTORY], False
    return paths, True


# ============================================================================ runners
import queue as _queue

def tree_fingerprint(cwd):
    """A content-sensitive fingerprint of everything the seats could read that is not frozen in the patch: HEAD, the full
    diff of tracked modifications against HEAD (so a further edit to an already-modified file is seen), and the identity
    (path, size, mtime) of every untracked file. Cheap enough to take before and after a review."""
    if not is_git_repo(cwd):
        return None
    rc1, head, _ = git(cwd, "rev-parse", "HEAD")
    rc2, diff, _ = git(cwd, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", "--")
    rc3, untracked, _ = git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if rc1 != 0 or rc3 != 0:
        return None
    if rc2 != 0:  # no HEAD yet: staged content is the only tracked state
        rc2, diff, _ = git(cwd, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--cached", "--")
    parts = [head.strip(), diff]
    for u in sorted(p for p in untracked.split("\0") if p):
        full = os.path.join(cwd, u)
        try:
            st_ = os.lstat(full)
            if stat_mod.S_ISLNK(st_.st_mode):
                parts.append(f"{u}\0link\0{os.readlink(full)}")
            elif stat_mod.S_ISREG(st_.st_mode) and st_.st_size <= MAX_UNTRACKED_BYTES:
                with open(full, "rb") as fh:
                    parts.append(f"{u}\0{hashlib.sha256(fh.read()).hexdigest()}")  # content, not just size and mtime
            else:
                parts.append(f"{u}\0{st_.st_size}\0{st_.st_mtime_ns}\0{st_.st_mode}")
        except OSError:
            parts.append(f"{u}\0?")
    return sha256_text("\n".join(parts))


CONTINUE_PROMPT = ("Your previous turn in this same conversation was interrupted by a usage limit. Continue exactly where you left off: "
                   "inspect the current state before acting, do not repeat actions that already completed, finish the original task, "
                   "and give the final report in the originally requested format.")
THROTTLE_CONTINUE_PROMPT = ("Your previous turn in this same conversation was interrupted by a temporary service error (the service was "
                            "overloaded or unreachable). Continue exactly where you left off: inspect the current state before acting, do not "
                            "repeat actions that already completed, finish the original task, and give the final report in the originally "
                            "requested format.")


def _seat_common_init(d, seat, extra):
    st = dict(vendor=seat.vendor, model=seat.model, effort=seat.effort, fast=seat.fast, state="running", started=time.time(),
              seat_name=seat.seat_name, family_requested=seat.family_requested, model_requested=seat.model_requested,
              effort_explicit=seat.effort_explicit, first_launch_at=None, fallbacks=[], models_launched=[],
              preference=[m for m, _f in seat.preference], effort_preference=list(seat.effort_preference),
              context_window=CONTEXT_WINDOW[seat.vendor], items=0, turns=0, activity="starting", tokens_in=None, tokens_out=None,
              context_tokens=None, attempt=1, supervisor_pid=os.getpid(), supervisor_start=proc_start(os.getpid()))
    st.update(extra)
    update_seat(d, seat.tag, **st)


def _cli_started(d, tag, model, st):
    """The first positive evidence that an attempt's CLI began (S-LAUNCH): unrouted, its process spawned; routed (`relay exec`, which
    may still refuse the account or fail before it becomes the CLI), the first vendor event, or a thread or session id. Sets the
    attempt's `started` flag in `st` (a later exception never clears it) and, at that moment, records the model in
    `models_launched` (launch order: what the job list's fallback mark reports as `ran`, RR-6) and stamps `first_launch_at` (the
    continuation flag of DESIGN §3) if unset. An attempt without such evidence is not a launch (RR-1)."""
    if st.get("started"):
        return
    st["started"] = True
    with job_lock(d):
        p = Path(d) / f"seat-{tag}.json"
        s = read_json(p)
        if s.get("first_launch_at") is None:
            s["first_launch_at"] = time.time()
        s["models_launched"] = list(s.get("models_launched") or []) + [model]
        atomic_write_json(p, s)


def _record_fallback(d, tag, kind, old, new, attempt):
    with job_lock(d):
        p = Path(d) / f"seat-{tag}.json"
        s = read_json(p)
        s["fallbacks"] = list(s.get("fallbacks") or []) + [{"kind": kind, "from": old, "to": new, "attempt": attempt, "at": time.time()}]
        atomic_write_json(p, s)


def _feed_stdin(p, text):
    def w():
        try:
            p.stdin.write(text)
            p.stdin.close()
        except Exception:
            pass
    t = threading.Thread(target=w, daemon=True)
    t.start()
    return t


def _start(cmd, cwd, env, stderr_path):
    err_fd = open_private(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_fd, text=True, cwd=cwd, env=env,
                             bufsize=1, start_new_session=True, errors="replace")
    finally:
        os.close(err_fd)
    p._bridge_pgid = p.pid  # start_new_session=True makes the child a session and group leader: pgid == pid, deterministically
    p._bridge_start = None  # filled by the caller AFTER registering the child (proc_start may block on ps)
    return p


def _watchdog(p, remaining, flag):
    if remaining is None:
        return None
    def fire():
        flag["timed_out"] = True
        kill_group(p, grace=2.0)
    t = threading.Timer(max(0.01, remaining), fire)
    t.daemon = True
    t.start()
    return t


def _pump_events(p, ev_path, on_event, idle_after_exit=5.0, hard_cap_after_exit=60.0):
    """Read the child's stdout through a bounded queue with an interruptible reader (select + os.read). After the leader
    exits we keep READING only while lines keep coming (idle window measured from the observed exit) and never longer than a
    hard cap; then the producer is stopped, the recorded group killed if ownership can be proven, and everything already
    buffered is still processed, so a terminal result that arrived in time is never discarded."""
    q = _queue.Queue(maxsize=20_000)
    stop = threading.Event()
    fd = p.stdout.fileno()

    def put(item):
        while not stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except _queue.Full:
                continue
        return False

    def pump():
        buf = b""
        try:
            while not stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not put(line.decode("utf-8", "replace") + "\n"):
                        return
            if buf and not stop.is_set():
                put(buf.decode("utf-8", "replace"))
        except Exception:
            pass
        finally:
            put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    exited_at = None
    last_line_at = time.monotonic()
    reading = True
    ev_fd = open_private(ev_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        with os.fdopen(ev_fd, "w") as ev:
            while True:
                now = time.monotonic()
                if exited_at is None and p.poll() is not None:
                    exited_at = now
                    last_line_at = now  # the idle window starts at the exit, not at the last line before a silent finish
                    _note_exit(p)
                if reading and exited_at is not None and (now - last_line_at >= idle_after_exit or now - exited_at >= hard_cap_after_exit):
                    reading = False  # a descendant kept the pipe open: stop the producer, kill the group, drain what is buffered
                    stop.set()
                    kill_group(p, grace=1.0)
                try:
                    line = q.get(timeout=0.25) if reading else q.get_nowait()
                except _queue.Empty:
                    if not reading:
                        break
                    continue
                if line is None:
                    break
                last_line_at = time.monotonic()
                ev.write(line)
                ev.flush()
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                on_event(e)
    finally:
        stop.set()
        reader.join(timeout=2.0)  # the select loop notices `stop` within half a second; never wait on a held pipe
        try:
            p.stdout.close()
        except Exception:
            pass


def _stderr_tail(path, n=2000):
    try:
        return Path(path).read_text(errors="replace")[-n:]
    except Exception:
        return ""


def _archive_attempt(d, tag, attempt):
    for name in (f"events-{tag}.jsonl", f"stderr-{tag}.log", f"last-{tag}.md", f"raw-{tag}.md"):
        p = Path(d) / name
        if p.exists():
            try:
                p.rename(Path(d) / f"{p.stem}.attempt{attempt}{p.suffix}")
            except OSError:
                pass


def _ladder_wait(limit_hits):
    """The owner's mandated ladder: three attempts twenty seconds apart, then an hour, then repeat; each step x uniform(0.6, 1.4)
    (as _busy_wait), so seats that hit a limit together do not retry in lockstep."""
    return (LIMIT_WAIT_LONG if limit_hits % 3 == 0 else LIMIT_WAIT_SHORT) * random.uniform(0.6, 1.4)


def _throttle_wait(hits, launch_class):
    """The throttle ladder of a launch class after its `hits`-th throttled or network attempt (5.1): batch 300, 900, 1800 s then every
    1800 s (x 0.8..1.2; unbounded), interactive 60 s then 300 s (x 0.6..1.4) and then None: the seat fails rather than wait more."""
    if launch_class == "batch":
        return THROTTLE_WAITS_BATCH[min(hits, len(THROTTLE_WAITS_BATCH)) - 1] * THROTTLE_WAIT_SCALE * random.uniform(0.8, 1.2)
    if hits > len(THROTTLE_WAITS_INTERACTIVE):
        return None
    return THROTTLE_WAITS_INTERACTIVE[hits - 1] * THROTTLE_WAIT_SCALE * random.uniform(0.6, 1.4)


def _normalize_error_text(text):
    return str(text or "").replace("’", "'").lower()


def throttle_class(text, tail=False):
    """The throttle class of an error message (spec 3.2 signatures): "codex-throttled", "codex-network" or None. `tail` is the final
    error event of a killed run, where the WebSocket-403 row also applies; a turn.failed message never gets that row."""
    t = _normalize_error_text(text)
    rows = [("codex-throttled", THROTTLE_SIGNATURES), ("codex-network", NETWORK_SIGNATURES)]
    if tail:
        rows.append(("codex-network", NETWORK_TAIL_SIGNATURES))
    for code, sigs in rows:
        if any(all(term in t for term in sig) for sig in sigs):
            return code
    return None


def limit_text_seen(text):
    """A usage limit in `text` by LIMIT_RE, consulted only when no throttle signature matches (the throttle class is decided first)."""
    return throttle_class(text) is None and bool(LIMIT_RE.search(str(text or "")))


def _wait_seat(d, tag, until, clock, why, kind="usage-limit"):
    """Park the seat visibly (state waiting, waiting_until, supervisor identity, waiting_kind) and cancellably until `until` (epoch
    seconds). False when cancel or the seat's clock interrupted the wait. `kind` names the cause for status and reply (usage-limit,
    routed, busy, throttle, capacity); old seat files without it read as a usage limit."""
    wait = max(0.0, until - time.time())
    update_seat(d, tag, state="waiting", waiting_until=until, waiting_kind=kind, supervisor_pid=os.getpid(), supervisor_start=proc_start(os.getpid()),
                activity=f"{why}; waiting {fmt_secs(wait)} (resumes ~{datetime.fromtimestamp(until):%H:%M})")
    resumed = interruptible_sleep(wait, d, clock)
    if resumed:
        update_seat(d, tag, state="running", waiting_until=None, activity="retrying after the wait")
    return resumed


def _credit_uncharged(d, tag, clock, seconds, record=True):
    """An interval not charged to the seat's --timeout (a throttle wait, the whole capacity-admission interval): the Clock's deadline
    (and every clock linked to it) moves by it, and with `record` it is added to the seat record's `uncharged_wait_s`."""
    if clock is not None:
        clock.extend(seconds)
    if not record or seconds <= 0:
        return
    with job_lock(d):
        p = Path(d) / f"seat-{tag}.json"
        s = read_json(p)
        s["uncharged_wait_s"] = round(float(s.get("uncharged_wait_s") or 0.0) + seconds, 2)
        atomic_write_json(p, s)


def _wait_seat_uncharged(d, tag, until, clock, why, kind):
    """_wait_seat for a throttle wait: not charged to the seat's --timeout (only cancel interrupts), credited through _credit_uncharged."""
    t0 = time.monotonic()
    resumed = _wait_seat(d, tag, until, None, why, kind=kind)
    _credit_uncharged(d, tag, clock, time.monotonic() - t0)
    return resumed


def _wait_on_ladder(d, tag, hits, clock):
    """The unrouted seat's ladder on this machine's own login."""
    update_seat(d, tag, limit_hits=hits)
    return _wait_seat(d, tag, time.time() + _ladder_wait(hits), clock, f"usage limit hit ({hits})")


WAITING_WORDS = {"usage-limit": "is waiting out a usage limit", "routed": "is waiting for a relay account", "busy": "is waiting for its account's slots",
                 "throttle": "is waiting because Codex throttled it (a temporary service error, not a usage limit)",
                 "capacity": "is waiting for Codex capacity (the session cap, a computer-use task's priority, the PAUSE file or the launch pacer)"}


def _waiting_words(s):
    """What a waiting seat is waiting for, in plain words, from its recorded waiting_kind (an old seat file: a usage limit)."""
    return WAITING_WORDS.get(s.get("waiting_kind") or "usage-limit", WAITING_WORDS["usage-limit"])


# ============================================================================ capacity: shared files and the pacer (spec 2.6, 4.3, 5.4)
def coord_dir():
    """The coordination dir shared with the computer-use runner (2.6): the priority lease and the capacity config live there."""
    return Path(os.environ.get("LR_CU_COORD_DIR") or os.path.expanduser("~/Library/Application Support/legal-research"))


def capacity_dir():
    """The bridge's capacity dir (2.6): per-account ledgers, pacers, counts, the events log and the PAUSE file."""
    return Path(os.environ.get("BRIDGE_CAPACITY_DIR") or str(CACHE_ROOT / "codex-claude-bridge" / "capacity"))


def account_key(home):
    """The file-name-safe key of a Codex account: sha256 of the realpath of its CODEX_HOME, 12 hex digits (5.3)."""
    return hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:12]


def _capacity_lock(key, suffix=".lock"):
    return job_lock(ensure_private_dir(capacity_dir()), name=f"{key}{suffix}")


def _pacer_path(key):
    return capacity_dir() / f"{key}.pacer.json"


def _pacer_read(key):
    p = read_json(_pacer_path(key))
    return p if isinstance(p, dict) else {}


def _pacer_trigger(home, reason, who=None):
    """Record a ramp trigger for the account (5.4): any attempt ending in the throttle or network class, a wake, or a failed network
    probe. The ramp restarts from now; the reason and who saw it are kept for doctor and the events log."""
    key = account_key(home)
    now = time.time()
    try:
        with _capacity_lock(key):  # the one lock of the account: the gate reads and writes the pacer under it too
            p = _pacer_read(key)
            p.update(ramp_started_at=now, reason=reason, triggered_by=who, home=os.path.realpath(str(home)))
            atomic_write_json(_pacer_path(key), p)
        _capacity_event("trigger", key=key, reason=reason, who=who)
    except OSError as exc:  # the capacity dir unwritable: the trigger is lost, the seat is not
        sys.stderr.write(f"[bridge] capacity: could not record the {reason} trigger in {capacity_dir()}: {exc}\n")


def _capacity_event(event, **fields):
    """One line of <capacity dir>/events.jsonl (5.3): admit, hold, release, stale-lease, shadow, trigger, with counts."""
    try:
        line = json.dumps(dict(at=time.time(), event=event, pid=os.getpid(), **fields), default=str)
        fd = open_private(ensure_private_dir(capacity_dir()) / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- 4.3 the shared capacity config
CAPACITY_DEFAULTS = {"session_cap": 100, "interactive_reserve": 16, "interactive_budget": 24, "cap_mode": "shadow"}
CAPACITY_ENV = {"session_cap": "BRIDGE_CODEX_SESSION_CAP", "interactive_reserve": "BRIDGE_CODEX_INTERACTIVE_RESERVE",
                "interactive_budget": "BRIDGE_INTERACTIVE_BUDGET"}


def _env_int(name, default):
    """A non-negative integer from the environment; a malformed value is a one-line usage error, never a traceback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
        if value < 0:
            raise ValueError(raw)
    except ValueError:
        sys.stderr.write(f"bridge: {name} must be a non-negative integer, not {raw!r}\n")
        sys.exit(EXIT_USAGE)
    return value


def capacity_config():
    """The shared capacity config (4.3): built-in defaults (cap 100, interactive reserve 16, interactive budget 24, cap_mode shadow),
    then `codex-capacity.json` in the coordination dir (Kevin's file: the cap is his decision), then the BRIDGE_* environment
    overrides. `cap_mode` governs ONLY the session-count limits; everything else is always enforced."""
    cfg = dict(CAPACITY_DEFAULTS, source="built-in defaults")
    path = coord_dir() / "codex-capacity.json"
    data = read_json(path, default={}) if path.is_file() else {}
    if isinstance(data, dict):
        for k in ("session_cap", "interactive_reserve", "interactive_budget"):
            v = data.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                cfg[k], cfg["source"] = v, str(path)
        if data.get("cap_mode") in ("enforce", "shadow"):
            cfg["cap_mode"], cfg["source"] = data["cap_mode"], str(path)
    for k, name in CAPACITY_ENV.items():
        if os.environ.get(name, "").strip():
            cfg[k], cfg["source"] = _env_int(name, cfg[k]), "environment"
    mode = os.environ.get("BRIDGE_CAP_MODE", "").strip().lower()
    if mode:
        if mode not in ("enforce", "shadow"):
            sys.stderr.write(f"bridge: BRIDGE_CAP_MODE must be enforce or shadow, not {mode!r}\n")
            sys.exit(EXIT_USAGE)
        cfg["cap_mode"], cfg["source"] = mode, "environment"
    return cfg


# ---------------------------------------------------------------- 4.2 live Codex sessions on a CODEX_HOME
BOUNDARY_EVENTS = ("task_started", "task_complete", "turn_aborted")
TAIL_READ, TAIL_READ_MAX, META_READ_MAX = 64 * 1024, 1024 * 1024, 256 * 1024


def _tail_lines(path, size):
    """(lines, whole): the lines in the last `size` bytes of a rollout file and whether that was the whole file. The first segment is
    dropped only when the read starts inside a line (the byte before it is not a newline); a trailing partial line (being written)
    is kept for the caller to try. ([], True) when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            length = fh.tell()
            start = max(0, length - size)
            cut = False
            if start > 0:
                fh.seek(start - 1)
                cut = fh.read(1) != b"\n"
            else:
                fh.seek(0)
            data = fh.read()
    except OSError:
        return [], True
    lines = data.split(b"\n")
    if cut:
        lines = lines[1:]
    return lines, start == 0


def _last_boundary(path):
    """(kind, parsed): the payload.type of the last turn-boundary event (`event_msg` with task_started / task_complete / turn_aborted)
    in a rollout file, read from its last 64 KiB and grown once to 1 MiB when that tail holds no boundary (a long turn, or a single
    line over 64 KiB); None when none is within the grown tail. `parsed` says whether any complete line of the tail parsed at all
    (False: a genuinely unreadable tail). Completed sessions often end in token_usage_record or item_completed after task_complete,
    so the last line is not the answer; the last boundary event is."""
    parsed = False
    for span in (TAIL_READ, TAIL_READ_MAX):
        lines, whole = _tail_lines(path, span)
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue  # a partial last line (being written) or not JSON
            parsed = True
            if isinstance(o, dict) and o.get("type") == "event_msg":
                payload = o.get("payload")
                kind = payload.get("type") if isinstance(payload, dict) else None
                if kind in BOUNDARY_EVENTS:
                    return kind, True
        if whole:
            break
    return None, parsed


def _session_meta(path):
    """(is_subagent, cwd) from a rollout's first line (session_meta, about 23 KB; read up to 256 KiB). Never copies account or user ids."""
    try:
        with open(path, "rb") as fh:
            first = fh.read(META_READ_MAX).split(b"\n", 1)[0]
        o = json.loads(first)
        payload = o.get("payload") if isinstance(o, dict) else None
        if not isinstance(payload, dict):
            return False, None
        source = payload.get("source")
        sub = payload.get("thread_source") == "subagent" or (isinstance(source, dict) and "subagent" in source)
        cwd = payload.get("cwd")
        return bool(sub), (str(cwd) if isinstance(cwd, str) else None)
    except Exception:
        return False, None


STALLED_TURN_S = 120.0   # a live rollout (task_started within its tail) untouched this long is "stalled in turn" (best-effort)


def count_live_sessions(codex_home, now=None, window_s=900):
    """Live Codex sessions under <codex_home>/sessions (4.2): every rollout-*.jsonl in the tree (resumed threads append to the
    folder of the day they started) modified within `window_s` whose last turn-boundary event is task_started. No boundary within
    the grown tail: counted live too, as `long_turns` when the tail parsed (the normal case for a long turn: real rollouts put
    task_started at the top of 2-4 MB files) or `degraded` when no complete line of it parsed at all (genuinely unknown).
    `stalled_in_turn`: live by a task_started within the tail but untouched for more than STALLED_TURN_S. Returns {total, top_level,
    subagents, by_cwd (top 10 [[cwd, n]]), scanned, candidates, long_turns, degraded, stalled_in_turn, elapsed_s}; the counts are ints."""
    t0 = time.monotonic()
    now = time.time() if now is None else now
    root = os.path.join(str(codex_home), "sessions")
    total = top = sub = scanned = candidates = long_turns = degraded = stalled = 0
    by_cwd = {}
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not (entry.is_file(follow_symlinks=False) and entry.name.startswith("rollout-") and entry.name.endswith(".jsonl")):
                    continue
                scanned += 1
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if now - mtime > window_s:
                continue
            candidates += 1
            boundary, parsed = _last_boundary(entry.path)
            if boundary is None:
                if parsed:
                    long_turns += 1
                else:
                    degraded += 1
            elif boundary != "task_started":
                continue
            elif now - mtime > STALLED_TURN_S:
                stalled += 1
            total += 1
            is_sub, cwd = _session_meta(entry.path)
            if is_sub:
                sub += 1
            else:
                top += 1
            if cwd:
                by_cwd[cwd] = by_cwd.get(cwd, 0) + 1
    ranked = sorted(by_cwd.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {"total": total, "top_level": top, "subagents": sub, "by_cwd": [[c, n] for c, n in ranked], "scanned": scanned,
            "candidates": candidates, "long_turns": long_turns, "degraded": degraded, "stalled_in_turn": stalled,
            "elapsed_s": round(time.monotonic() - t0, 3)}


# ---------------------------------------------------------------- 4.1 the priority lease (reader only: the bridge never writes or prunes)
def lease_host():
    return socket.gethostname().split(".")[0]


def lease_dir():
    return coord_dir() / "codex-cu-priority.d"


def _holder_live(path):
    """A holder file's liveness is its lock alone: a non-blocking shared-lock probe that is refused (EWOULDBLOCK/EAGAIN) means the
    runner holds it; one that succeeds means its process died (stale: unlock, close, ignore). None when the file vanished."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def lease_state(now=None, host=None):
    """The computer-use priority lease (4.1): live when any holder file is locked by a live runner, or any reservation names this host
    and has not expired. {live, holders: [{task_dir, pid, since, file}], reservations: [{task_dir, expires_at, wait_cycle, file}],
    stale: [...]} (stale: holder files nobody locks, left for the runner to prune; the bridge never deletes them)."""
    now = time.time() if now is None else now
    host = host or lease_host()
    out = {"live": False, "holders": [], "reservations": [], "stale": []}
    d = lease_dir()
    try:
        names = sorted(p for p in os.listdir(d) if p.endswith(".json"))
    except OSError:
        return out
    for name in names:
        path = d / name
        if name.startswith("holder-"):
            live = _holder_live(path)
            if live is None:
                continue
            data = read_json(path, default={})
            rec = {"file": name, "task_dir": data.get("task_dir"), "pid": data.get("pid"), "since": data.get("since")}
            (out["holders"] if live else out["stale"]).append(rec)
        elif name.startswith("reservation-"):
            data = read_json(path, default={})
            exp = _epoch(data.get("expires_at"))
            if data.get("host") == host and exp is not None and exp > now:
                out["reservations"].append({"file": name, "task_dir": data.get("task_dir"), "expires_at": data.get("expires_at"),
                                            "wait_cycle": data.get("wait_cycle")})
    out["live"] = bool(out["holders"] or out["reservations"])
    return out


# ---------------------------------------------------------------- 5.3 / 5.4 the capacity gate and the pacer
CAPACITY_ON = os.environ.get("BRIDGE_CAPACITY", "on").strip().lower() not in ("off", "0")
NET_PROBE = (os.environ.get("BRIDGE_NET_PROBE") or "chatgpt.com:443").strip()
WAKE_QUIET = _env_seconds("BRIDGE_WAKE_QUIET", 60.0)
MIN_GAP = _env_seconds("BRIDGE_MIN_GAP", 1.5)
RAMP_START = _env_seconds("BRIDGE_RAMP_START", 4.0)      # batch launches per minute at the start of a ramp
RAMP_STEP = _env_seconds("BRIDGE_RAMP_STEP", 120.0)      # the rate doubles every step
CAPACITY_MAX_WAIT_INTERACTIVE = _env_seconds("BRIDGE_CAPACITY_MAX_WAIT_INTERACTIVE", 900.0)
NETWORK_HOLD_INTERACTIVE = _env_seconds("BRIDGE_NETWORK_HOLD_INTERACTIVE", 120.0)   # an interactive seat waits this long for a failed probe, then proceeds (tests shorten it)
PENDING_WINDOW = 180.0             # a ledger entry counts as pending (not yet visible as a rollout) this long after its admission
COUNT_CACHE_S = 5.0
PROBE_CACHE_OK, PROBE_RETRY = 10.0, 30.0
HOLD_POLL = _env_seconds("BRIDGE_HOLD_POLL", 20.0)   # a held seat re-checks every HOLD_POLL s +/-40% (tests shorten it)
WEIGHT_ULTRA = _env_int("BRIDGE_CODEX_WEIGHT_ULTRA", 3)


def _seat_weight(seat):
    return WEIGHT_ULTRA if seat.effort == "ultra" else 1


def _waketime():
    """`sysctl -n kern.waketime` as epoch seconds (0 on a Mac that never slept), BRIDGE_FAKE_WAKETIME in tests; None when unreadable."""
    fake = os.environ.get("BRIDGE_FAKE_WAKETIME")
    if fake is not None:
        try:
            return float(fake)
        except ValueError:
            return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.waketime"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"sec\s*=\s*(\d+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _tcp_probe(pacer, now):
    """A TCP connect to chatgpt.com:443 (BRIDGE_NET_PROBE=host:port, or off), 3 s timeout, cached in the pacer: 10 s after a success,
    30 s +/-40% after a failure. Returns ok; updates pacer['probe']."""
    if NET_PROBE.lower() == "off":
        return True
    probe = pacer.get("probe") if isinstance(pacer.get("probe"), dict) else {}
    at = probe.get("at")
    if isinstance(at, (int, float)) and now - at < (PROBE_CACHE_OK if probe.get("ok") else float(probe.get("retry_in") or PROBE_RETRY)):
        return bool(probe.get("ok"))
    host, _, port = NET_PROBE.rpartition(":")
    ok = False
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            ok = True
    except Exception:
        ok = False
    pacer["probe"] = {"ok": ok, "at": now, "target": NET_PROBE, "retry_in": round(PROBE_RETRY * random.uniform(0.6, 1.4), 1)}
    if not ok and not pacer.get("network_down_since"):
        pacer["network_down_since"] = now
    elif ok and pacer.get("network_down_since"):
        pacer["network_down_since"] = None
        pacer.update(ramp_started_at=now, reason="network")   # the outage ended: the next launches ramp
    return ok


def _ramp_doublings(cap):
    """How many doublings the ramp takes from BRIDGE_RAMP_START to reach `cap` launches per minute (the ramp is over at that step);
    None when the ramp is disabled (BRIDGE_RAMP_START=0 or BRIDGE_RAMP_STEP=0) or the start rate already reaches the cap."""
    if RAMP_START <= 0 or RAMP_STEP <= 0:
        return None
    cap = max(cap, 1)
    if RAMP_START >= cap:
        return None
    return int(math.ceil(math.log2(cap / RAMP_START)))


def _ramp_steps(pacer, now):
    """The whole steps of BRIDGE_RAMP_STEP elapsed since the pacer's ramp_started_at; None without a started ramp (or one that starts
    in the future: a clock skew)."""
    started = pacer.get("ramp_started_at")
    if not isinstance(started, (int, float)) or isinstance(started, bool) or now < started:
        return None
    return int((now - started) // RAMP_STEP) if RAMP_STEP > 0 else 0


def _ramp_finished(pacer, now, cap):
    """Whether a recorded ramp is over (B-1): its doublings reached the cap, or the ramp is disabled. Decided by comparing the elapsed
    steps against the doublings needed, never by exponentiating, so pacer state days old is simply a finished ramp."""
    steps = _ramp_steps(pacer, now)
    if steps is None:
        return False
    doublings = _ramp_doublings(cap)
    return doublings is None or steps >= doublings


def _ramp_rate(pacer, now, cap):
    """The allowed batch launch rate per minute under the ramp (5.4), or None outside one: starts at BRIDGE_RAMP_START after the trigger
    and doubles every BRIDGE_RAMP_STEP until it reaches the session cap per minute (then the ramp is over)."""
    steps = _ramp_steps(pacer, now)
    if steps is None or _ramp_finished(pacer, now, cap):
        return None
    return RAMP_START * (2 ** steps)


def _next_slot(pacer, now, cap):
    """The earliest time the next batch launch may go on this account: the ramp's next slot (+/-30% jitter) during a ramp, else the
    minimum gap (BRIDGE_MIN_GAP, +/-50%; 0 allowed) after the last launch."""
    last = pacer.get("last_launch_at")
    if not isinstance(last, (int, float)):
        return now
    rate = _ramp_rate(pacer, now, cap)
    if rate:
        return last + (60.0 / rate) * random.uniform(0.7, 1.3)
    return last + MIN_GAP * random.uniform(0.5, 1.5)


def _pacer_observe(pacer, now, home, who):
    """Under the account lock: note a wake (first observation initializes only), the network probe, the home. Returns net_ok."""
    wake = _waketime()
    if wake is not None:
        seen = pacer.get("last_seen_waketime")
        if not isinstance(seen, (int, float)):
            pacer["last_seen_waketime"] = wake
        elif wake > seen:
            pacer.update(last_seen_waketime=wake, ramp_started_at=now, reason="wake", quiet_until=now + WAKE_QUIET, triggered_by=who)
            _capacity_event("trigger", key=account_key(home), reason="wake", who=who)
    pacer.setdefault("home", os.path.realpath(str(home)))
    return _tcp_probe(pacer, now)


def _ledger_path(key):
    return capacity_dir() / f"{key}.json"


def _ledger_read(key, now, table=None):
    """The admission ledger of an account with its dead entries pruned (pid gone or its start time changed)."""
    data = read_json(_ledger_path(key), default={})
    entries = data.get("entries") if isinstance(data, dict) and isinstance(data.get("entries"), list) else []
    table = _process_table() if table is None else table
    live = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        pid = e.get("pid")
        if table is not None:
            row = table.get(pid) if isinstance(pid, int) else None
            if row is None or (e.get("pid_start") and row[2].strip() != str(e["pid_start"]).strip()):
                continue
        elif not (isinstance(pid, int) and pid_alive(pid) and (not e.get("pid_start") or proc_start(pid) == e.get("pid_start"))):
            continue
        live.append(e)
    return {"home": (data.get("home") if isinstance(data, dict) else None), "entries": live}


def _live_count_cached(key, home, now):
    """count_live_sessions(home), cached up to 5 s in <key>.count.json so the ledger lock stays short."""
    p = capacity_dir() / f"{key}.count.json"
    cached = read_json(p, default={})
    if isinstance(cached, dict) and isinstance(cached.get("at"), (int, float)) and now - cached["at"] < COUNT_CACHE_S and isinstance(cached.get("counts"), dict):
        return cached["counts"]
    counts = count_live_sessions(home, now=now)
    atomic_write_json(p, {"at": now, "home": os.path.realpath(str(home)), "counts": counts})
    return counts


def _short_home(home):
    h = os.path.realpath(str(home))
    me = os.path.realpath(os.path.expanduser("~"))
    return "~" + h[len(me):] if h == me or h.startswith(me + os.sep) else h


def _capacity_decide(key, home, d, tag, seat, launch_class, cfg, ctx, window_passed=False):
    """One admission decision under the account lock (5.3): prune the ledger, count live sessions and pending admissions, read the
    lease, the PAUSE file and the pacer, and either append the ledger entry (admitted) or name the first cause of the hold.
    `window_passed`: the seat has waited BRIDGE_NETWORK_HOLD_INTERACTIVE or more; only a seat that resolves as interactive in THIS
    decision then skips the network hold (B-5: one demoted to batch here, because other interactive launches filled the budget during
    an offline hold, keeps waiting like a batch seat)."""
    now = time.time()
    weight = _seat_weight(seat)
    with _capacity_lock(key):
        pacer = _pacer_read(key)
        net_ok = _pacer_observe(pacer, now, home, f"{Path(d).name}/{tag}")
        if pacer.get("ramp_started_at") is not None and _ramp_finished(pacer, now, cfg["session_cap"]):
            pacer["ramp_started_at"] = None   # the ramp is over: cleared here, under the account lock (the observation is persisted below)
        ledger = _ledger_read(key, now)
        entries = ledger["entries"]
        pending = sum(int(e.get("weight") or 1) for e in entries if isinstance(e.get("admitted_at"), (int, float)) and now - e["admitted_at"] < PENDING_WINDOW)
        interactive_live = sum(int(e.get("weight") or 1) for e in entries if e.get("class") == "interactive")
        cls = launch_class
        overflow = False
        if cls == "interactive" and interactive_live + weight > cfg["interactive_budget"]:
            cls, overflow = "batch", True
        ignore_network = cls == "interactive" and window_passed   # decided from the class resolved under the lock, never a previous decision's
        counts = _live_count_cached(key, home, now)
        live = int(counts.get("total") or 0) + pending
        lease = lease_state(now)
        pause = capacity_dir() / "PAUSE"
        paused = pause.exists()
        limit = cfg["session_cap"] - cfg["interactive_reserve"] if cls == "batch" else cfg["session_cap"]
        home_s = _short_home(home)
        facts = dict(key=key, job=Path(d).name, tag=tag, **{"class": cls}, weight=weight, live=live, rollouts=counts.get("total"), pending=pending,
                     limit=limit, cap=cfg["session_cap"], mode=cfg["cap_mode"], lease_live=lease["live"], paused=paused, net_ok=net_ok)
        if lease["stale"] and not ctx.get("stale_noted"):
            ctx["stale_noted"] = True
            _capacity_event("stale-lease", files=[s["file"] for s in lease["stale"]], **facts)
        cause = None
        if not net_ok and not ignore_network:
            cause = f"network down: {NET_PROBE.rpartition(':')[0] or NET_PROBE} unreachable"
        elif cls == "batch":
            if lease["live"]:
                held = lease["holders"] or lease["reservations"]
                cause = f"held: computer-use task {held[0].get('task_dir') or '(unknown task dir)'} has priority"
            elif paused:
                cause = f"batch launches paused by {pause}"
            elif isinstance(pacer.get("quiet_until"), (int, float)) and now < pacer["quiet_until"]:
                cause = f"post-wake quiet period ({fmt_secs(pacer['quiet_until'] - now)} left)"
            else:
                slot = _next_slot(pacer, now, cfg["session_cap"])
                if now < slot:
                    rate = _ramp_rate(pacer, now, cfg["session_cap"])
                    cause = (f"launch pacer: ramping after {pacer.get('reason') or 'a trigger'} ({rate:g}/min), next batch slot in {fmt_secs(slot - now)}"
                             if rate else f"launch pacer: minimum gap, next batch slot in {fmt_secs(slot - now)}")
        over = live + weight > limit
        if cause is None and over:
            text = f"waiting for Codex capacity: {live} live sessions of {limit} allowed for {cls} seats on {home_s}"
            if cfg["cap_mode"] == "enforce":
                cause = text
            elif not ctx.get("shadow_noted"):
                ctx["shadow_noted"] = True
                sys.stderr.write(f"[bridge] capacity: {tag}: would hold: {text} (cap_mode shadow)\n")
                sys.stderr.flush()
                _capacity_event("shadow", cause=text, **facts)
        if cause is not None:
            atomic_write_json(_pacer_path(key), pacer)  # what the observation learned (a wake seen, the probe) persists across the hold
            return dict(admitted=False, cause=cause, network=(not net_ok and not ignore_network), overflow=overflow, **facts)
        token = uuid.uuid4().hex[:10]
        entries.append({"id": token, "job": Path(d).name, "tag": tag, "class": cls, "weight": weight, "pid": os.getpid(),
                        "pid_start": proc_start(os.getpid()), "admitted_at": now})
        atomic_write_json(_ledger_path(key), {"home": os.path.realpath(str(home)), "entries": entries})
        if cls == "batch":
            pacer["last_launch_at"] = now
        atomic_write_json(_pacer_path(key), pacer)
        _capacity_event("admit", token=token, **facts)
        return dict(admitted=True, token=token, cause=None, network=False, lease=lease, overflow=overflow, **facts)


def _notice_once_kind(d, kind, text, seat=None):
    """At most one notice of `kind` per job (5.3: one capacity notice per job): the check and the append happen under ONE job lock
    (B-7: two seats of a job that both saw no notice of the kind used to append one each)."""
    with job_lock(d):
        m = read_json(Path(d) / "meta.json")
        notices = list(m.get("notices") or [])
        if any(isinstance(n, dict) and n.get("kind") == kind for n in notices):
            return
        notices.append({"kind": kind, "seat": seat, "text": text, "at": time.time()})
        m["notices"] = notices
        atomic_write_json(Path(d) / "meta.json", m)


def _capacity_admit(d, seat, home, clock, launch_class, ctx):
    """The capacity gate of a Codex attempt (5.3), on the home it will really use: every retry, ladder relaunch, failover, busy relaunch
    and reply is re-admitted. Returns (token, error, admitted_class): a token to release after the attempt, or an error line when the
    seat must end (cancelled, or an interactive seat held at the hard cap past BRIDGE_CAPACITY_MAX_WAIT_INTERACTIVE), and the class the
    seat was admitted as (batch for an interactive seat over the interactive budget: it drives the network hold and the throttle
    ladder, B-5). Holds park the seat through _wait_seat (state waiting, cancellable), re-checking every 20 s +/-40%. The WHOLE admission
    interval (lock waits, probes, ps, rollout scans and the sleeps) is uncharged to the seat's --timeout, credited once at the end
    (B-6); `ctx["held"]` says whether this admission waited at all. BRIDGE_CAPACITY=off: no gate."""
    ctx["held"] = False
    if not CAPACITY_ON:
        return None, None, launch_class
    tag = seat.tag
    key = account_key(home)
    cfg = capacity_config()
    t_start = time.monotonic()
    last_cause = None
    try:
        while True:
            waited = time.monotonic() - t_start
            # the interactive network window has passed: whether the seat may skip the network hold is decided inside the decision, from
            # the class it resolves under the lock (B-5), never from the class the previous decision recorded
            dec = _capacity_decide(key, home, d, tag, seat, launch_class, cfg, ctx, window_passed=waited >= NETWORK_HOLD_INTERACTIVE)
            if dec["class"] != ctx.get("recorded_class", launch_class):  # over the interactive budget: the record says how the seat is treated
                ctx["recorded_class"] = dec["class"]
                update_seat(d, tag, **{"class": dec["class"]})
            if dec["overflow"] and not ctx.get("overflow_noted"):
                ctx["overflow_noted"] = True
                _notice_once_kind(d, "capacity-class", f"NOTICE: seat {tag}: interactive budget used; this seat waits like a batch seat", seat=tag)
            if dec["admitted"]:
                if last_cause is not None:
                    sys.stderr.write(f"[bridge] capacity: {tag}: admitted after {fmt_secs(waited)} ({last_cause})\n")
                    sys.stderr.flush()
                if launch_class == "interactive" and dec["class"] == "interactive" and dec["lease"]["live"]:
                    held = dec["lease"]["holders"] or dec["lease"]["reservations"]
                    _notice_once_kind(d, "capacity-lease", f"NOTICE: seat {tag}: admitted while computer-use task "
                                                            f"{held[0].get('task_dir') or '(unknown task dir)'} holds priority (an interactive seat is not held by it)",
                                      seat=tag)
                return dec["token"], None, dec["class"]
            cause = dec["cause"]
            ctx["held"] = True
            if cause != last_cause:
                sys.stderr.write(f"[bridge] capacity: {tag}: {cause}\n")
                sys.stderr.flush()
                _capacity_event("hold", cause=cause, **{k: v for k, v in dec.items() if k not in ("admitted", "cause", "network")})
                _notice_once_kind(d, "capacity", f"NOTICE: seat {tag}: {cause}; the seat waits (status shows the live cause)", seat=tag)
                last_cause = cause
            if dec["class"] == "interactive" and not dec["network"] and waited >= CAPACITY_MAX_WAIT_INTERACTIVE:
                return None, f"Codex capacity: {cause} for {fmt_secs(waited)}; not launched", dec["class"]
            poll = HOLD_POLL * random.uniform(0.6, 1.4)
            if dec["class"] == "interactive":  # an interactive hold ends on its budget, not up to a poll later
                poll = min(poll, max(0.5, (NETWORK_HOLD_INTERACTIVE if dec["network"] else CAPACITY_MAX_WAIT_INTERACTIVE) - waited))
            if not _wait_seat(d, tag, time.time() + poll, None, cause, kind="capacity"):
                return None, "cancelled", dec["class"]
    except OSError as exc:  # the capacity dir unusable: never hold a seat on a broken gate
        sys.stderr.write(f"[bridge] capacity: {tag}: gate unavailable ({exc}); launching without it\n")
        return None, None, ctx.get("recorded_class", launch_class)
    finally:
        _credit_uncharged(d, tag, clock, time.monotonic() - t_start, record=ctx["held"])


def _capacity_release(home, token):
    """Remove the attempt's ledger entry (in a `finally` around the attempt)."""
    if token is None:
        return
    key = account_key(home)
    try:
        with _capacity_lock(key):
            ledger = _ledger_read(key, time.time())
            kept = [e for e in ledger["entries"] if e.get("id") != token]
            atomic_write_json(_ledger_path(key), {"home": ledger["home"] or os.path.realpath(str(home)), "entries": kept})
        _capacity_event("release", key=key, token=token)
    except OSError:
        pass


def capacity_report(now=None):
    """Per-account capacity facts for doctor and status (5.5): live sessions (top-level, subagents), pending, cap and reserve, mode,
    lease state (holder task dirs), PAUSE, ramp state. Reads only; takes no lock."""
    now = time.time() if now is None else now
    cfg = capacity_config()
    lease = lease_state(now)
    homes = {}
    local = CodexSelection.local_home()
    homes[account_key(local)] = os.path.realpath(local)
    try:
        for p in capacity_dir().glob("*.json"):
            name = p.name
            if name.endswith((".count.json", ".pacer.json")) or name.startswith("."):
                continue
            data = read_json(p, default={})
            if isinstance(data, dict) and isinstance(data.get("home"), str):
                homes[name[:-5]] = data["home"]
    except OSError:
        pass
    accounts = []
    table = _process_table()
    for key, home in sorted(homes.items(), key=lambda kv: kv[1]):
        counts = count_live_sessions(home, now=now)
        entries = _ledger_read(key, now, table=table)["entries"]
        pending = sum(int(e.get("weight") or 1) for e in entries if isinstance(e.get("admitted_at"), (int, float)) and now - e["admitted_at"] < PENDING_WINDOW)
        pacer = _pacer_read(key)
        rate = _ramp_rate(pacer, now, cfg["session_cap"])
        quiet = isinstance(pacer.get("quiet_until"), (int, float)) and now < pacer["quiet_until"]
        probe = pacer.get("probe") if isinstance(pacer.get("probe"), dict) else {}
        accounts.append({"key": key, "home": home, "counts": counts, "pending": pending, "ledger": len(entries),
                         "interactive_weight": sum(int(e.get("weight") or 1) for e in entries if e.get("class") == "interactive"),
                         "ramp_rate": rate, "ramp_reason": pacer.get("reason") if rate or quiet else None, "quiet": quiet,
                         "network_ok": probe.get("ok") if probe else None})
    return {"config": cfg, "lease": lease, "pause": (capacity_dir() / "PAUSE").exists(), "pause_path": str(capacity_dir() / "PAUSE"),
            "accounts": accounts, "on": CAPACITY_ON}


def capacity_lines(report=None):
    """The capacity section of doctor and status, in plain words."""
    r = report or capacity_report()
    cfg = r["config"]
    lease = r["lease"]
    if lease["live"]:
        held = [h.get("task_dir") or "?" for h in lease["holders"]] + [f"{x.get('task_dir') or '?'} (reserved until {x.get('expires_at')})" for x in lease["reservations"]]
        lease_s = "LIVE: computer-use task " + "; ".join(held) + " has priority (batch seats hold; interactive seats are admitted)"
    else:
        lease_s = "none live" + (f" ({len(lease['stale'])} stale holder file(s) left for the runner to prune)" if lease["stale"] else "")
    lines = [f"capacity: {'off (BRIDGE_CAPACITY=off)' if not r['on'] else 'cap_mode ' + cfg['cap_mode']} (session cap {cfg['session_cap']}, "
             f"interactive reserve {cfg['interactive_reserve']}, interactive budget {cfg['interactive_budget']}; from {cfg['source']})",
             f"  lease: {lease_s}", f"  PAUSE: {'set at ' + r['pause_path'] + ' (batch seats hold)' if r['pause'] else 'not set (' + r['pause_path'] + ')'}"]
    for a in r["accounts"]:
        c = a["counts"]
        ramp = (f"ramping after {a['ramp_reason']} ({a['ramp_rate']:g} batch launches/min)" if a["ramp_rate"] else
                ("post-wake quiet period" if a["quiet"] else "none"))
        net = "" if a["network_ok"] is None else f"; network {'ok' if a['network_ok'] else 'DOWN'}"
        extra = "".join(f", {label} {c.get(k)}" for k, label in (("long_turns", "long turns"), ("degraded", "degraded"), ("stalled_in_turn", "stalled in turn"))
                        if c.get(k))
        lines.append(f"  account {a['key']} ({_short_home(a['home'])}): live sessions {c.get('total', 0)} (top-level {c.get('top_level', 0)}, "
                     f"subagents {c.get('subagents', 0)}{extra}), pending {a['pending']}, allowed {max(0, cfg['session_cap'] - cfg['interactive_reserve'])} batch / "
                     f"{cfg['session_cap']} interactive; ramp: {ramp}{net}")
    return lines


def _busy_wait():
    """BUSY-1 A7: the pause before asking relay again while an account's slots are in use, a quarter of LIMIT_WAIT_SHORT (5 s by
    default) with ±40 % jitter, so bridge seats are not starved by relay's own 5-second job retries. Never zero (R1-1): with
    BRIDGE_LIMIT_WAIT_SHORT=0 the seat still pauses between asks until its budget ends."""
    return max(0.05, LIMIT_WAIT_SHORT / 4 * random.uniform(0.6, 1.4))


# The refusal that relay's exec command prints for an account whose concurrency slots are all in use (relay.py cmd_exec:
# `die("account %s cannot take work now: %s; wake_at=%s" % (account, why, wake))` with `why = "its %d slot%s in use" % (cap, " is" if
# cap == 1 else "s are all")`), so exactly
#   relay: account <label> cannot take work now: its 1 slot is in use; wake_at=<epoch>
#   relay: account <label> cannot take work now: its <N> slots are all in use; wake_at=<epoch>
BUSY_RE = re.compile(r"^relay: account (\S+) cannot take work now: its \d+ slots? (?:is|are all) in use; wake_at=(\d+)$")


def _busy_refusal(r, label):
    """BUSY-1 rule 1: a refused attempt (SeatRouting.refused) whose LAST non-empty stderr line is relay's busy line for `label`, the
    attempt's own account. relay's refusal is its final and only line ("[relay] running under account" is printed only when exec
    proceeds). Every other refusal (a ceiling, a rate limit, a sign-in) stays a usage limit."""
    tail = [line.strip() for line in (r.get("stderr_tail") or "").splitlines() if line.strip()]
    m = BUSY_RE.match(tail[-1]) if tail else None
    return bool(m) and m.group(1) == label


def _fmt_when(ts):
    return f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}"


def _identity(label=None, email=None, plan=None):
    return {"account": label, "account_email": email, "account_plan": plan}


class SeatRouting:
    """One seat's account routing: the relay to ask, the account the next attempt runs on (`identity`: label, email, plan),
    the last account an attempt actually ran on (`last`, kept for the record when a failover dead-ends), and the failover
    bookkeeping. A `pinned` seat (a Codex reply: the thread lives in one account's home) never picks; it runs on the recorded
    account exactly, after checking that the label still names the same sign-in, or on this machine's own login when the
    record has none."""

    def __init__(self, d, seat, clock, identity=None, pinned=False):
        self.d, self.seat, self.clock, self.pinned = d, seat, clock, pinned
        self.identity = dict(_identity(), **{k: v for k, v in (identity or {}).items() if k in IDENTITY_KEYS})
        self.owner = _identity()  # the account that holds the conversation the seat has in hand
        self.has_conv = False     # a conversation (thread/session) has been established; owner is then authoritative
        self.last_ran = None      # the last account an attempt actually ran on (even one that produced no conversation)
        self.router = resolve_router() if pinned else job_router(d, seat.vendor)
        self.repeats = self.stale = 0  # consecutive re-offers of the label just hit; consecutive answers without a usable wake time
        self.busy_waits = 0       # waits for an account whose slots were in use (BUSY-1): not limits, counted apart for the record
        # All of the seat's waits together end ROUTED_MAX_WAIT after its start, or at the seat's own deadline when that is sooner;
        # both move with the seat's uncharged waits (B-4)
        self.budget = WaitBudget(ROUTED_MAX_WAIT, clock)
        self.trail = Path(d) / f"router-{seat.tag}.jsonl"
        self.error = None
        self.verified = False
        self.verified_at = None   # when a pinned reply's sign-in was last verified: a busy spell of REPICK_WAIT or more re-verifies (A1)
        self.codex_home = None    # the picked Codex account's CODEX_HOME (a protocol-2 answer), where its rollout file lives
        self.unsupported = None   # why constrained routing is unavailable, once a `--only` pick answered anything but v2 with the set applied
        if pinned and self.account and self.router is None:
            self.error = f"this seat ran on relay account {self.account}, but relay is not available now (BRIDGE_ROUTER, RELAY_BIN)"

    def _call(self, base):
        remaining = self.clock.remaining() if self.clock else None  # every helper call is bounded by the seat's deadline too
        return dict(d=self.d, tag=self.seat.tag, trail=self.trail, timeout=base if remaining is None else max(0.1, min(base, remaining)))

    @property
    def account(self):
        return self.identity["account"]

    @property
    def routed(self):
        return self.account is not None

    def prefix(self):
        return self.router.exec_prefix(self.seat.vendor, self.account, self.seat.relay_model()) if self.account else []

    def ran(self):
        """An attempt actually executed on the current account, whether or not it produced a conversation: remember it so the
        record can name where the seat last spent work even when no thread/session started."""
        self.last_ran = dict(self.identity)

    def owns(self):
        """The latest attempt established or continued the conversation under the currently selected account: that account
        now owns it (or, when unrouted, the conversation is local and the owner carries no account). Ownership follows the
        conversation, never the account merely selected for the next attempt."""
        self.owner = dict(self.identity)
        self.has_conv = True

    def leave(self):
        """The current account is spent (or the model changed): the next attempt needs another one. The conversation's owner
        is untouched — it stays with whatever account actually holds the thread/session."""
        self.identity = _identity()
        self.codex_home = None

    def record(self):
        """What the seat record should say about its account: the owner of the conversation in hand once one exists, else the
        last account an attempt ran on, else the account currently selected. So a thread stays paired with the account that
        actually holds it, never a later selection that never ran, and a local conversation clears the routed identity."""
        return dict(self.owner if self.has_conv else (self.last_ran or self.identity))

    def whereabouts(self, kind, conv_id):
        """The suffix of a dead-end failure text: who holds the conversation and, when a later attempt ran elsewhere without
        starting one, where that was. "; last ran on account a (thread T)" when the two coincide (or only one is known),
        otherwise both, so the operator knows which account to rerun on and which one was tried last."""
        owner = self.owner.get("account") if self.has_conv else None
        last = (self.last_ran or {}).get("account")
        where = f" ({kind} {conv_id})" if conv_id else ""
        if owner and last and last != owner:
            return f"; account {owner} holds the conversation{where}; the last attempt ran on account {last} without starting one"
        acct = owner or last
        return f"; last ran on account {acct}{where}" if acct else ""

    def _verify_pinned(self):
        """A pinned reply runs only after a FRESH live check that the recorded label still names the sign-in that holds the
        thread (A3/A4). The real relay serves cached registry data without --verify, so a changed sign-in inside the recorded
        CODEX_HOME would otherwise go unnoticed and the reply would spend the replacement account. Any answer that is not a
        fresh successful observation refuses the reply."""
        if self.error or self.verified or not self.account:
            return
        self.verified = True
        expected = self.identity.get("account_email")
        asked = self.verified_at = time.time()
        update_seat(self.d, self.seat.tag, activity=f"verifying account {self.account} still holds this thread")
        st = self.router.account_status(self.seat.vendor, self.account, asked, **self._call(ROUTER_TIMEOUTS["accounts"]))
        if is_cancelled(self.d):
            return  # the cancel, not the verification, ends the seat: choose() reports it as cancelled
        if not st["ok"]:
            self.error = f"cannot verify account {self.account} before replying on its thread ({st['reason']}); refusing to run"
        elif expected and st["email"] and st["email"] != expected:
            self.error = f"account {self.account} is now signed in as {st['email']}, not {expected} whose thread this is; refusing to run"

    def pin_here(self):
        """B-2: a throttled attempt that did work resumes its thread, which lives in the current account's home: from now on the seat is
        pinned to that account for every retry (busy refusals included), as a pinned reply is. The sign-in was in use moments ago, so
        the verification is fresh now; a busy spell of REPICK_WAIT or more re-verifies it (A1), as for a reply."""
        if self.pinned or not self.routed:
            return
        self.pinned = True
        self.router = self.router or resolve_router()
        self.verified, self.verified_at = True, time.time()
        self.owns()
        update_seat(self.d, self.seat.tag, activity=f"thread resumes on account {self.account}; the seat stays there")

    def recheck_before_launch(self):
        """B-3: after every admission, a pinned seat whose live sign-in verification is REPICK_WAIT or older verifies it again right
        before the launch (a sign-in that changed hands during a capacity hold, a lock wait or a throttle wait refuses the launch;
        a fresh one, as a reply's at its choose() or a continuation's at pin_here(), is kept). The refusal line, or None."""
        if not (self.pinned and self.account) or self.error:
            return self.error
        if self.verified_at and time.time() - self.verified_at < REPICK_WAIT:
            return None
        self.verified = False
        self._verify_pinned()
        return "cancelled" if is_cancelled(self.d) else self.error

    def choose(self, avoid=None, only=None, until=None):
        """Settle the account for the next attempt: None when the seat may launch (routed to self.account, or unrouted),
        otherwise the reason it cannot. `avoid` is the label whose limit was just reported and `until` the reset relay gave for
        it: relay offering it again before then means the hit did not persist, so the seat pauses and asks again, at most twice
        in a row; at or after `until` relay's block has lapsed and the offer is usable (BUSY-1 A2). `only` is a Codex seat's
        capable set (docs/model-routing.md 3.3): the pick is confined to it, and an answer that cannot apply it ends the pick
        with the reason in `self.unsupported` (the caller then validates this machine's own login; nothing runs unchecked)."""
        if self.pinned:
            self._verify_pinned()
            if is_cancelled(self.d):  # a cancel that landed during the verification: the seat ends cancelled, not failed
                return "cancelled"
            return self.error
        if self.error or self.router is None or self.account is not None:
            return self.error
        d, tag, vendor = self.d, self.seat.tag, self.seat.vendor
        while True:
            if is_cancelled(d):
                return "cancelled"
            if self.clock and self.clock.expired():
                return "timed out before an account was chosen"
            update_seat(d, tag, activity=f"asking relay for a {vendor} account")
            ans = self.router.pick(vendor, self.seat.relay_model(), only=only, **self._call(ROUTER_TIMEOUTS["pick"]))
            if is_cancelled(d):  # a cancel that landed during the pick: launch nothing
                return "cancelled"
            kind, now = ans["kind"], time.time()
            if kind == "ok" and (ans["label"] != avoid or (until is not None and now >= until)):  # not `avoid`, or its block has lapsed
                info = ans["info"]
                self.identity = _identity(ans["label"], str(info["email"]) if info.get("email") else None, str(info["plan"]) if info.get("plan") else None)
                self.codex_home = str(info["codex_home"]) if isinstance(info.get("codex_home"), str) and info.get("codex_home") else None
                self.repeats = self.stale = 0  # a usable answer: the retry budgets start afresh
                update_seat(d, tag, activity=f"routed to account {self.account}", **self.identity)
                return None
            if kind == "unsupported":  # constrained routing is not available here: never the unchecked local login (design 3.3 step 3)
                self.unsupported = ans["reason"]
                return f"constrained routing unsupported: {ans['reason']}"
            if kind == "unrouted":
                self.router = None
                _set_router_state(d, vendor, "unrouted")
                update_seat(d, tag, account=None, activity=f"unrouted ({ans['reason']}); running on this machine's own login")
                return None
            if kind == "error":
                return ans["reason"]
            if kind == "busy":  # every account's slots are in use (BUSY-1): the busy interval, not relay's wake time; no count toward stale
                err = self.busy()
                if err:
                    return err
                continue
            if kind == "ok":  # the label whose limit was just reported came back: the hit did not persist
                self.repeats += 1
                if self.repeats > 2:
                    return f"relay keeps offering account {avoid} after its usage limit was reported; giving up"
                why, wake = f"relay offered {avoid} again after its usage limit", now + REPICK_WAIT
            else:
                # relay's reason for each account leads (status and the footer keep 60 characters): an account can be held back
                # without being out (reads that kept failing), and its wake time is then only when to ask again, not a reset.
                # A relay that gives no reasons (older, or the suite's stub) keeps the sentence the seat has always shown.
                rows = (ans.get("info") or {}).get("rows")
                said = "; ".join(f"{r.get('label')}: {r.get('reason')}" for r in rows if isinstance(r, dict) and r.get("reason")) \
                    if isinstance(rows, list) else ""
                why, wake = (f"relay: {short(said, 400)}" if said else f"every {vendor} account is out"), ans.get("wake_at")
                if wake is None or wake <= now:  # a past or missing reset is not availability: ask again, twice at most
                    self.stale += 1
                    if self.stale > 2:
                        return f"no {vendor} account is available and relay gives no wake time; giving up"
                    wake = now + REPICK_WAIT
                elif wake - now > self.budget.remaining():
                    if said:
                        return f"{why}; relay says to ask again at {_fmt_when(wake)}, which the seat's wait budget or timeout cannot cover"
                    return f"every {vendor} account is out until {_fmt_when(wake)}"
            if wake - now > self.budget.remaining():
                return f"{why}; the seat's wait budget ({fmt_secs(ROUTED_MAX_WAIT)}, BRIDGE_ROUTED_MAX_WAIT) or timeout cannot cover another wait"
            if not _wait_seat(d, tag, wake, self.budget, why, kind="routed"):
                return "cancelled" if is_cancelled(d) else "wait interrupted by the seat's timeout"

    def hit(self, label):
        """Report the limit on `label` to relay so its ledger blocks the account; returns the reset time relay knows, or None."""
        update_seat(self.d, self.seat.tag, activity=f"usage limit on account {label}; telling relay")
        _ok, until = self.router.hit(self.seat.vendor, label, self.seat.relay_model(), **self._call(ROUTER_TIMEOUTS["hit"]))
        return until

    def refused(self, r):
        """relay exec refused the account before the CLI ran: a non-zero exit, no CLI event at all, and `wake_at` on stderr."""
        return bool(self.account) and r.get("rc") not in (0, None) and not r.get("thread_id") and not r.get("session_id") \
            and "wake_at" in (r.get("stderr_tail") or "")

    def busy(self, who=None):
        """BUSY-1: `who` (an account, or every account after a busy pick) has its slots in use. Not a limit: nothing is reported or
        counted but this wait. The seat pauses for the busy interval within its wait budget, then asks again: a pinned seat for its
        own account, re-verifying its sign-in only after a busy spell of REPICK_WAIT or more (A1), any other with a fresh pick.
        None when the caller may ask again, else the line that ends the seat."""
        d, tag = self.d, self.seat.tag
        who = who or f"every {self.seat.vendor} account"
        wait = _busy_wait()
        if self.budget.expired() or wait > self.budget.remaining():  # BRIDGE_ROUTED_MAX_WAIT from the seat's start, or its --timeout when that is sooner
            return f"{who} stayed busy (its slots are in use) until the seat's wait budget ran out"
        self.busy_waits += 1  # counted once the seat waits (R1-4): a cancelled wait counts, a seat that ends at its budget adds none
        update_seat(d, tag, busy_waits=self.busy_waits)
        if not _wait_seat(d, tag, time.time() + wait, self.budget, f"{who} is busy (its slots are in use)", kind="busy"):
            return "cancelled" if is_cancelled(d) else "wait interrupted by the seat's timeout"
        if self.pinned:
            self.verified = bool(self.verified_at) and time.time() - self.verified_at < REPICK_WAIT
        else:
            self.leave()
        return None

    def transplant(self, session_id, cwd):
        update_seat(self.d, self.seat.tag, activity=f"carrying session {session_id} to account {self.account}")
        return self.router.transplant(session_id, self.account, cwd, **self._call(ROUTER_TIMEOUTS["transplant"]))


def _post_launch_cancel(d, tag, p):
    """Cancellation that landed between the pre-launch check and PID publication must still stop the child."""
    if is_cancelled(d):
        kill_group(p, grace=1.0)
        update_seat(d, tag, state="cancelled", finished=time.time(), activity="cancelled at launch")
        return True
    return False


def _codex_attempt(seat, prompt, d, cwd, *, sandbox, schema, resume, user_config, full_access, clock, codex, prefix=()):
    tag = seat.tag
    raw = Path(d) / f"raw-{tag}.md"  # the raw answer (`-o`); last-<tag>.md is composed from it with the notices when the seat ends
    cmd = list(prefix) + [codex, "exec"]  # a routed seat runs through `relay exec … --`, which execs into codex
    if resume:
        cmd += ["resume", resume]
    cmd += ["--json", "-m", seat.model]
    if seat.effort:
        cmd += ["-c", f'model_reasoning_effort="{seat.effort}"']
    if resume:
        cmd += ["-c", f'sandbox_mode="{sandbox}"']  # resume takes no -s; pin the policy explicitly
    else:
        cmd += ["-C", cwd, "-s", sandbox]
    if full_access:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if not user_config:
        cmd.append("--ignore-user-config")
    if seat.fast:
        cmd += ["-c", 'service_tier="fast"']
    if not is_git_repo(cwd):
        cmd.append("--skip-git-repo-check")
    if schema:
        cmd += ["--output-schema", str(schema)]
    cmd += ["-o", str(raw), "-"]
    ev_path, err_path = Path(d) / f"events-{tag}.jsonl", Path(d) / f"stderr-{tag}.log"
    flag = {"timed_out": False}
    st = dict(thread_id=None, usage=None, last_text="", errors=[], items_done=0, items_started=0, turns=0, tokens_in=0, tokens_out=0,
              last_error_limit=False, last_error_class=None, last_error_tail_class=None, work=False, terminal_error=False, started=False)
    t0 = time.time()

    def on_event(e):
        t = e.get("type")
        _cli_started(d, tag, seat.model, st)  # any Codex event: the CLI is running (S-LAUNCH)
        act = None
        if t == "thread.started":
            st["thread_id"] = e.get("thread_id")
            act = "thread started"
        elif t == "turn.started":
            st["turns"] += 1
            act = "thinking"
        elif t == "turn.completed":
            st["usage"] = e.get("usage") or {}
            st["tokens_in"] += st["usage"].get("input_tokens", 0)
            st["tokens_out"] += st["usage"].get("output_tokens", 0)
            st["last_error_limit"] = False  # the turn recovered: an earlier limit message is no longer the terminal failure
            st["last_error_class"] = st["last_error_tail_class"] = None
            st["terminal_error"] = False
            act = "turn complete"
        elif t in ("turn.failed", "error"):
            msg = e.get("error", {}).get("message") if isinstance(e.get("error"), dict) else (e.get("message") or json.dumps(e))
            st["errors"].append(str(msg)[:400])
            # only the LAST unrecovered error decides; the throttle class is evaluated first (5.1) and then LIMIT_RE is not consulted
            st["last_error_class"] = throttle_class(msg)
            st["last_error_tail_class"] = throttle_class(msg, tail=True)  # what the final error of a killed run means
            st["last_error_limit"] = limit_text_seen(msg)
            st["terminal_error"] = True
            act = "error: " + short(msg, 80)
        elif t in ("item.started", "item.completed"):
            it = e.get("item", {})
            kind = it.get("type")
            if t == "item.started":
                st["items_started"] += 1
            else:
                st["items_done"] += 1
            if kind in ("command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search", "agent_message"):
                st["work"] = True  # visible work, whether or not a start event preceded it
            if kind == "command_execution":
                act = ("ran: " if t == "item.completed" else "running: ") + short(it.get("command", "").replace("/bin/zsh -lc ", ""), 70)
            elif kind == "agent_message":
                st["last_text"] = it.get("text") or st["last_text"]
                act = "writing answer"
            elif kind == "reasoning":
                act = "reasoning"
            elif kind == "file_change":
                act = "editing files"
            elif kind == "mcp_tool_call":
                act = "tool: " + short(it.get("tool") or it.get("name") or "", 40)
            elif kind == "collab_tool_call":
                act = "subagents: " + short(it.get("tool") or "", 30)
            elif kind == "web_search":
                act = "web search"
            else:
                act = kind or t
        if act:
            update_seat(d, tag, activity=act, items=st["items_done"], turns=st["turns"], thread_id=st["thread_id"],
                        tokens_in=st["tokens_in"] or None, tokens_out=st["tokens_out"] or None)

    p = None
    wd = None
    try:
        p = _start(cmd, cwd, model_env(os.environ), err_path)
        _register(tag, p)  # registered before anything that can block, so a signal in this window still finds the child
        p._bridge_start = proc_start(p.pid)
        update_seat(d, tag, pid=p.pid, pid_start=p._bridge_start, pgid=getattr(p, "_bridge_pgid", None))
        if not prefix:  # unrouted, the spawn is the CLI's start; routed, relay may still refuse or fail before it becomes the CLI (S-LAUNCH)
            _cli_started(d, tag, seat.model, st)
        if _post_launch_cancel(d, tag, p):
            return dict(ok=False, cancelled=True, rc=None, text="", secs=round(time.time() - t0, 1), thread_id=None, usage=None,
                        errors=[], stderr_tail="", tokens_in=0, tokens_out=0, timed_out=False, limit_seen=False, work_began=False, launch_error=None,
                        started=st["started"])
        wd = _watchdog(p, clock.remaining() if clock else None, flag)
        _feed_stdin(p, prompt)
        _pump_events(p, ev_path, on_event)
        rc = p.wait()
        kill_group(p, grace=0.5)  # reap any straggler in the group
    except Exception as exc:  # before or after the CLI started: `started` says which
        if p is not None:
            kill_group(p, grace=1.0)
        return dict(ok=False, cancelled=is_cancelled(d), rc=None, text="", secs=round(time.time() - t0, 1), thread_id=st["thread_id"], usage=None,
                    errors=[repr(exc)], stderr_tail=_stderr_tail(err_path), tokens_in=0, tokens_out=0, timed_out=flag["timed_out"],
                    limit_seen=False, work_began=st["work"], launch_error=repr(exc), started=st["started"])
    except BaseException:  # SystemExit/KeyboardInterrupt: never leave the group behind
        if p is not None:
            kill_group(p, grace=1.0)
        raise
    finally:
        if wd:
            wd.cancel()
        _unregister(tag)
    secs = round(time.time() - t0, 1)
    tighten_job_files(d)
    text = raw.read_text() if raw.exists() and raw.read_text().strip() else st["last_text"]
    stderr_tail = _stderr_tail(err_path)
    cancelled = is_cancelled(d)
    timed_out = flag["timed_out"]
    ok = rc == 0 and bool(text) and not timed_out and not cancelled
    # A usage limit counts only when it is the failure that ENDED the attempt: the last error event, or the tail of stderr on a non-zero exit.
    # The throttle class (5.1) is decided from the same terminal event, never from stderr: a run killed by the watchdog carries the
    # class of its final error event (the tail rows apply), a failed run the class of its turn.failed message.
    throttle = None
    if st["terminal_error"]:  # an unrecovered structured diagnostic exists: it alone decides
        limit_seen = (not ok) and st["last_error_limit"]
        throttle = (st["last_error_tail_class"] if timed_out else st["last_error_class"]) if not ok else None
    else:  # every structured error was recovered from, or there was none: the tail of stderr is the only evidence
        tail_lines = [line for line in stderr_tail.strip().splitlines()[-5:] if throttle_class(line) is None]  # a throttle line is no limit
        limit_seen = (not ok) and rc != 0 and bool(LIMIT_RE.search("\n".join(tail_lines)))
    return dict(ok=ok, cancelled=cancelled, rc=rc, text=text, secs=secs, thread_id=st["thread_id"], usage=st["usage"], errors=st["errors"],
                stderr_tail=stderr_tail, tokens_in=st["tokens_in"], tokens_out=st["tokens_out"], timed_out=timed_out, limit_seen=limit_seen,
                throttle_class=throttle, work_began=st["work"] or st["turns"] > 1 or bool(text), launch_error=None, started=st["started"])


def _judge_observation(d, seat, r, obs):
    """DESIGN §4, the observed-model contract of a Claude attempt: a model other than the requested ID fails the seat (`ran <ids>,
    requested <id>`; its output is kept in raw-<tag>.md); a cumulative usage key no observation explains, and an incomplete
    observation, are notices that leave the outcome unchanged."""
    tag, requested = seat.tag, seat.model
    observed = list(obs.get("models_observed") or [])
    mismatch = any(m != requested for m in observed)
    if mismatch:
        r["ok"] = False
        r["error"] = f"ran {', '.join(observed)}, requested {requested}"
        add_notice(d, "ran-model", f"NOTICE: seat {tag}: ran {', '.join(observed)}, requested {requested}; the seat failed "
                                   f"(its output is kept in raw-{tag}.md)", seat=tag)
    unexplained = [k for k in obs.get("session_models_cumulative") or [] if k not in observed]
    if unexplained:
        add_notice(d, "usage-unexplained", f"NOTICE: seat {tag}: the session's usage ledger lists {', '.join(unexplained)}, which no message "
                                           f"of this attempt ran (a resumed session's cumulative usage); not attributed", seat=tag)
    if not obs.get("complete"):
        missing = [x for x, seen in (("the init model", obs.get("init_model")), ("a result with modelUsage", obs.get("session_models_cumulative")))
                   if not seen]
        add_notice(d, "observation-incomplete", f"NOTICE: seat {tag}: model observation incomplete ({' and '.join(missing) or 'stream ended early'} "
                                                f"not seen); the seat's outcome is unchanged", seat=tag)
    r["judged"] = True  # each attempt's observation is judged once: finishing the seat does not judge it again
    update_seat(d, tag, observation=obs)
    return mismatch


def _compose_last(d, tag, text):
    """last-<tag>.md: the seat's notices (and the job-level ones) leading the answer; the raw answer stays in raw-<tag>.md."""
    write_private_text(Path(d) / f"last-{tag}.md", lead_with(job_notices(d, seat=tag), text or ""))


def _finish_seat(d, seat, r, attempt, limit_hits, extra_ids):
    if is_cancelled(d):
        r["cancelled"] = True  # a cancel that landed during a ladder wait, after the child exited, or after an ok answer still wins
        r["ok"] = False
    obs = r.get("observation")
    if obs is not None and not r.get("judged"):  # a cancelled attempt is judged too; cancellation stays the terminal state below
        _judge_observation(d, seat, r, obs)
    state = "cancelled" if r.get("cancelled") else ("done" if r.get("ok") else "failed")
    if r.get("timed_out"):
        why = "timed out"
    elif r.get("error"):
        why = r["error"]
    elif r.get("errors"):
        why = short(r["errors"][-1], 90)
    else:
        tail = (r.get("stderr_tail") or "").strip().splitlines()
        why = short(tail[-1] if tail else (r.get("text") or "no output"), 90)
    # The account identity is authoritative: it names the account that holds the conversation, and clearing it to None is
    # meaningful (a conversation that became local). A thread/session id, by contrast, is never wiped with None.
    ident = {k: extra_ids[k] for k in IDENTITY_KEYS if k in extra_ids}
    other = {k: v for k, v in extra_ids.items() if k not in IDENTITY_KEYS and v is not None}
    update_seat(d, seat.tag, state=state, finished=time.time(), secs=r.get("secs"), tokens_in=r.get("tokens_in") or None,
                tokens_out=r.get("tokens_out") or None, timed_out=bool(r.get("timed_out")), attempt=attempt, limit_hits=limit_hits,
                model=seat.model, effort=seat.effort, waiting_until=None,
                activity="done" if r.get("ok") else ("cancelled" if r.get("cancelled") else f"failed: {why}"),
                **({"observation": obs} if obs is not None else {}), **other, **ident)
    if not r.get("ok"):
        r["error"] = why
    r["attempt"] = attempt
    r["limit_hits"] = limit_hits
    for k in IDENTITY_KEYS:  # the footer names exactly what the record names, including a cleared account
        r[k] = extra_ids.get(k)
    for k in ("thread_id", "session_id"):
        if extra_ids.get(k) is not None or not r.get(k):
            r[k] = extra_ids.get(k)
    _compose_last(d, seat.tag, r.get("text"))
    return r


def _cli_error_line(r):
    """The CLI's own error line for a startup rejection: the last non-empty line of its stderr, else its last error event."""
    tail = [line.strip() for line in (r.get("stderr_tail") or "").splitlines() if line.strip()]
    if tail:
        return short(tail[-1], 200)
    return short(r["errors"][-1], 200) if r.get("errors") else "rejected at startup"


def _continuation_check(d, seat):
    """DESIGN §3 `continuation` mode, before a retry, re-route or resume of a seat whose first attempt already launched: a marker
    learned meanwhile becomes a notice and the seat proceeds; membership, effort and floor were settled at the initial validation."""
    t, _err = table_or_error()
    if t is None:
        return
    for text in bridge_models.validate(t, evidence(), _resolved(seat), seat.effort, "continuation", None):
        add_notice(d, "models-marker", text, seat=seat.tag, once=True)  # the text names the model and the kind: once per marker and severity


def _launch_check(d, seat, first, cli_path, env=None):
    """DESIGN §3 right before a launch, after the account selection and its waits (B-astra-1, RR-1): until an attempt of a candidate
    has started (the seat's first model, or a new fallback target; S-LAUNCH: positive evidence its CLI began), every attempt of it
    gets `initial`-mode validation again, since nothing has run yet: a marker another check published while the seat waited refuses
    the launch. An attempt relay refused or could not start is not a run. A retry or re-route of an attempt that ran keeps `continuation` mode
    (_continuation_check: a marker learned meanwhile is a notice). Returns the refusal line, or None when the seat may launch."""
    if not first:
        _continuation_check(d, seat)
        return None
    t, _err = table_or_error()
    if t is None:
        return None
    try:
        bridge_models.validate(t, evidence(), _resolved(seat), seat.effort, "initial", {"path": cli_path, "version": cli_version(cli_path, env=env)})
    except bridge_models.Refusal as exc:
        return str(exc)
    return None


def _no_attempt(d, error, t0):
    """The result of a seat that could not launch an attempt (no account, a failed transplant): nothing ran."""
    return dict(ok=False, cancelled=is_cancelled(d), text="", secs=round(time.time() - t0, 1), error=error, errors=[], stderr_tail="")


class CodexSelection:
    """Selection bound to routing for one Codex seat (docs/model-routing.md 3.3 and 5). The head is the table's (the seat's model).
    With a registry, the capable set is computed from the registry's per-account catalog snapshots under the bridge's own launch
    fingerprint (each account's catalog read through the app-server under its own CODEX_HOME when the stored snapshot is not
    consumable), relay's pick is confined to the set (protocol 2 with only_applied), the picked account is re-checked right before
    the launch, a startup rejection invalidates and refreshes that account's catalog and the seat re-picks within the set minus
    the account, and when relay cannot apply the set (an older relay, no accounts) or is absent, this machine's own login is one
    more candidate, launched only when its refreshed catalog lists the head at the requested effort. Without a registry the bridge
    keeps its built-in routing (relay's legacy pick; the local login when relay has no accounts). In both modes a seat descends to
    another family or effort only through its explicit `|` preference list, and every descent is recorded and noticed."""

    def __init__(self, d, seat, routing, link, codex, cwd, user_config):
        self.d, self.seat, self.routing, self.link, self.codex, self.cwd = d, seat, routing, link, codex, cwd
        self.config_mode = (bridge_routing.USER if user_config else bridge_routing.IGNORE) if bridge_routing is not None else None
        self.overrides = ['service_tier="fast"'] if seat.fast else []
        self.version = cli_version(codex) if codex else None
        self.excluded = set()    # accounts that rejected the seat's current model or effort at startup, for this model
        self.launches = {}       # label -> launch fingerprint (None when it could not be computed)
        self.homes = {}          # label -> CODEX_HOME as relay reports it
        self.labels = None       # relay's signed-in Codex accounts, asked once per seat
        self.local = False       # this machine's own login is the seat's candidate (relay is out of the picture)
        self.local_reason = ""   # why relay is out of the picture: leads the local candidate's failure texts
        self.notes = []          # why each account is out of the capable set: the current view
        self.effort_short = False  # an account of the last capable set listed the model but not the seat's effort
        self.reject_kind = None  # "model" or "effort" after a startup rejection
        self.descended = False   # the last choose() moved the seat to a preference-list model: its first attempt is `initial`
        self.attempt = 1
        self.model_listed, self.effort_listed = bool(seat.preference), bool(seat.effort_preference)  # the seat opted into a descent

    @property
    def registry(self):
        """Constrained routing applies: a registry is linked and the seat is not a pinned reply (which runs where its thread lives)."""
        return self.link is not None and not self.routing.pinned

    @staticmethod
    def local_home():
        """This machine's own CODEX_HOME: where an unrouted launch runs and writes its rollout file."""
        return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")

    def home_of(self, label):
        """The CODEX_HOME a launch under `label` runs in. The label `default` means this machine's own login only once relay is out
        of the picture (`self.local`): a relay account may carry that label too (relay's own `--account default` refuses the
        ambiguity), and every routed label's home is what relay says (asked once), never the caller's CODEX_HOME."""
        if self.local and label == LOCAL_LABEL:
            return self.local_home()
        if label not in self.homes:
            home = None
            if self.routing.router is not None:  # bounded by the seat's clock and cancellable, but not part of the routing trail
                home = self.routing.router.codex_home(label, **dict(self.routing._call(ROUTER_TIMEOUTS["accounts"]), trail=None))
            self.homes[label] = home
        return self.homes[label]

    @property
    def home(self):
        """The CODEX_HOME of the account the next attempt runs on: the rollout file the collector reads lives there (design 6)."""
        if self.routing.account:
            return self.routing.codex_home or self.home_of(self.routing.account)
        return self.local_home()

    def discovery_env(self, label):
        """The environment an account's catalog is read under: the one its launch will have. This machine's own login launches with
        the bridge's environment (model_env); a relay account launches through `relay exec`, which strips the caller's provider
        and credential variables before it becomes the CLI, so its discovery strips the same (relay_codex.codex_env, mirrored)."""
        base = model_env(os.environ)
        return base if (self.local and label == LOCAL_LABEL) else relay_codex_env(base)

    def candidates(self):
        """relay's signed-in Codex accounts, asked once per seat: None when relay cannot list them, [] when it has none."""
        if self.labels is None and self.routing.router is not None:
            self.labels = self.routing.router.codex_labels(**dict(self.routing._call(ROUTER_TIMEOUTS["accounts"]), trail=None))
        return self.labels

    def refresh(self, label, force=False):
        """Design 3.3 step 1: the account's snapshot made consumable under the seat's launch (a read only when it is not, or `force`)."""
        home = self.home_of(label)
        if not home:
            self.launches[label] = None
            self.notes.append(f"{label}: relay does not say where its CODEX_HOME is")
            return None
        fp, reason, read = self.link.refresh(label, self.codex, self.version, home, self.cwd, self.config_mode, self.overrides,
                                             env=self.discovery_env(label), force=force)
        if read:
            update_seat(self.d, self.seat.tag, activity=f"read the Codex catalog of account {label}")
        if reason:
            self.notes.append(reason)
        self.launches[label] = fp
        return fp

    def fingerprint(self, label):
        """The seat's launch fingerprint under `label`'s home as of now: the link re-hashes the configuration files before it reuses a
        cached fingerprint and re-discovers a changed launch (a config/read, never a catalog read). None when it cannot be computed."""
        home = self.home_of(label)
        if not home:
            return None
        try:
            fp, _scratch = self.link.launch(self.codex, self.version, home, self.cwd, self.config_mode, self.overrides, env=self.discovery_env(label))
        except bridge_routing.LaunchUnavailable:
            return None
        return fp

    def capable_set(self, labels):
        """Design 3.3 step 2: the accounts whose fresh, compatible snapshot lists the seat's model, not hidden, at its effort (and fast)."""
        self.notes = []
        wanted = [l for l in labels if l not in self.excluded]
        for label in wanted:
            self.refresh(label)
        capable, notes, _listed, effort_short = self.link.capable(self.seat.model, [(l, self.launches.get(l)) for l in wanted], effort=self.seat.effort,
                                                                  fast=self.seat.fast)
        self.notes.extend(notes)
        self.effort_short = effort_short
        return capable

    def take_local(self, reason, answered=True):
        """Design 3.3 step 3: relay cannot apply the set (or is absent): this machine's own login is the one candidate, validated
        like an account before every launch (choose_local); `reason`, why relay is out of the picture, leads its texts."""
        routing = self.routing
        routing.router = None
        routing.identity = _identity()
        if answered:
            _set_router_state(self.d, "codex", "unrouted")
        self.local = True
        self.local_reason = reason
        self.excluded.discard(LOCAL_LABEL)  # a relay account that carries the label `default` is not this login
        self.launches.pop(LOCAL_LABEL, None)
        self.homes.pop(LOCAL_LABEL, None)
        return self.choose_local()

    def choose_local(self):
        """Every launch of this machine's own login (design 3.3 step 3): its snapshot is made consumable and must list the seat's
        current model at its effort right before each attempt, a retry or a descent included; a login that rejected the model at
        startup, or lists neither the model nor its effort, consults the seat's preference lists (design 5) and never runs unchecked."""
        seat, reason = self.seat, self.local_reason
        for _ in range(REPICK_LIMIT):
            if LOCAL_LABEL not in self.excluded and self.capable_set([LOCAL_LABEL]):
                update_seat(self.d, seat.tag, account=None, activity=f"{reason}; running on this machine's own login, whose catalog lists {seat.model}")
                return None
            out = self.exhausted()  # a descent (None) is validated by the next round; a failure carries the reason relay is out
            if out is not None:
                return f"{reason}; {out}" if reason else out
        return f"{reason}; this machine's own login kept failing validation for {seat.model}; giving up"

    def choose(self, avoid=None, until=None):
        """Settle the account for the next attempt: None when the seat may launch, else the reason it cannot (design 3.3 steps 1-4).
        `avoid` and `until` are SeatRouting.choose's: the label whose limit was just reported, and the reset relay gave for it."""
        routing = self.routing
        if not self.registry:  # built-in routing: relay's legacy pick, or none; a pinned reply stays on its account
            for _ in range(REPICK_LIMIT):
                err = routing.choose(avoid, until=until)
                if err is not None:
                    return err
                label = routing.account if routing.routed else LOCAL_LABEL
                if label not in self.excluded:
                    return None
                out = self.exhausted()  # without a registry no other candidate is known: the preference list decides
                if out is not None:
                    return out
            return f"{self.seat.model} kept landing on an account that rejected it; giving up"
        if self.local:  # this machine's own login: validated again before every launch
            return self.choose_local()
        if routing.account is not None:
            return None
        if routing.router is None:
            return self.take_local("relay is not available", answered=False)
        labels = self.candidates()
        if not labels:
            return self.take_local("constrained routing unsupported (relay cannot list its Codex accounts)" if labels is None
                                   else "relay has no Codex accounts")
        for _ in range(REPICK_LIMIT):
            capable = self.capable_set(labels)
            if not capable:
                out = self.exhausted()
                if out is not None:
                    return out
                continue  # the seat descended through its preference list: a new model, a new capable set
            err = routing.choose(avoid, only=capable, until=until)
            if routing.unsupported:
                return self.take_local(f"constrained routing unsupported ({routing.unsupported})")
            if err:
                return err
            label = routing.account
            # the pick may have waited: the launch is fingerprinted again first (an edited configuration file changes it), then the
            # picked account's snapshot is checked for that launch (design 3.3 step 4); either change refreshes the catalog and
            # re-picks within the set, so what launches was validated as it is now
            before, now = self.launches.get(label), self.fingerprint(label)
            if now is None or before is None or now.get("fingerprint") != before.get("fingerprint"):
                why = "its launch configuration changed while relay was picking" if now is not None else "its launch could not be fingerprinted"
                check = {"ok": False, "reason": f"{label}: {why}"}
            else:
                check = self.link.check_launch(label, self.seat.model, now)
            if check["ok"]:
                return None
            update_seat(self.d, self.seat.tag, activity=f"account {label} re-checked before the launch: {check['reason']}; refreshing and re-picking")
            routing.leave()
            self.refresh(label, force=True)
        return "the pre-launch re-check of the picked account kept failing; giving up"

    def why(self):
        """Why no account is left: the capable-set notes, and every account that rejected the seat's current model at startup."""
        notes = list(self.notes)
        for label in sorted(self.excluded):
            if not any(n.startswith(f"{label}: rejected") for n in notes):
                notes.append(f"{label}: rejected {self.seat.model} at startup")
        return "; ".join(notes) or "no capable account"

    def exhausted(self):
        """No capable account is left for the seat's model (or effort): descend only through the seat's explicit preference list,
        recording it (design 5); without one, fail with the reason and the table's suggestion, never a silent descent. An effort
        rejected at startup everywhere, or one no account's catalog offers for a model the catalogs do list, consults the effort
        list first (the model stays); then the model list."""
        t = table()
        seat = self.seat
        by_catalog = self.reject_kind is None
        # the effort list applies to an effort rejected at startup everywhere, and to an effort the remaining accounts' catalogs
        # do not offer for a model they do list, whatever an earlier account's rejection was about (the capable set's current view)
        effort_cause = ("rejected at startup on every capable account" if self.reject_kind == "effort" else
                        f"no account's catalog offers {seat.effort} for {seat.model}" if (self.effort_short and seat.effort_preference) else None)
        if effort_cause:
            while seat.effort_preference:
                new = seat.effort_preference.pop(0)
                if new not in t.supported_efforts(seat.model):
                    add_notice(self.d, "descent-refused", f"NOTICE: seat {seat.tag}: effort {new} is not supported by {seat.model}; skipped", seat=seat.tag)
                    continue
                old = seat.effort
                _record_fallback(self.d, seat.tag, "effort", old, new, self.attempt)
                add_notice(self.d, "fallback-effort", f"NOTICE: seat {seat.tag}: effort {old} -> {new} ({effort_cause}; "
                                                      f"the seat's effort preference list allows it; attempt {self.attempt})", seat=seat.tag)
                seat.effort = new
                if self.reject_kind == "effort":  # the accounts that rejected the old effort may take the new one; a model rejection stands
                    self.excluded = set()
                self.reject_kind = None
                if not self.routing.pinned:
                    self.routing.leave()
                update_seat(self.d, seat.tag, effort=new, activity=f"descending to effort {new} by the seat's preference list")
                return None
            if self.reject_kind == "effort":  # a catalog shortfall with the effort list exhausted goes on to the model list below
                if self.effort_listed:
                    return f"{seat.model} at effort {seat.effort} was rejected at startup on every capable account; the seat's effort preference list is exhausted"
                hint = t.effort_hint("codex", seat.model, seat.effort)
                return (f"{seat.model} at effort {seat.effort} was rejected at startup on every capable account; the seat has no effort "
                        f"preference list, so it does not descend" + (f" (say {hint} to allow it)" if hint else ""))
        while seat.preference:
            model, family = seat.preference.pop(0)
            target = bridge_models.Resolved("codex", model, family, seat.tag)
            try:
                bridge_models.validate(t, evidence(), target, seat.effort, "initial", {"path": self.codex, "version": self.version})
            except bridge_models.Refusal as exc:
                add_notice(self.d, "descent-refused", f"NOTICE: seat {seat.tag}: preference {family} ({model}) not launched: {exc}", seat=seat.tag)
                continue
            old = seat.model
            why = self.why()
            _record_fallback(self.d, seat.tag, "model", old, model, self.attempt)
            add_notice(self.d, "fallback-model", f"NOTICE: seat {seat.tag}: model {old} -> {model} (no capable account for {old}: {why}; "
                                                 f"the seat's preference list {seat.seat_name} allows it; attempt {self.attempt})", seat=seat.tag)
            if seat.downgraded_from is None:
                seat.downgraded_from = seat.family
            seat.model, seat.family = model, family
            note_promotion(self.d, model)  # a preference list may lead to a head this Mac promoted (DECISIONS A28)
            self.excluded = set()
            self.reject_kind = None
            self.descended = True
            if not self.routing.pinned:
                self.routing.leave()
            update_seat(self.d, seat.tag, model=model, activity=f"descending to {family} ({model}) by the seat's preference list")
            return None
        why = self.why()
        subject = f"this machine's own login cannot run {seat.model}" if self.local else f"no account can run {seat.model}"
        step = t.promotion_step(seat.model)
        if step:  # the head came from this Mac's own catalog (DECISIONS A28): say so, and how to go back
            why += (f"; {seat.model} was promoted automatically on {bridge_models._date_text(step['at'])} from this Mac's own Codex catalog "
                    f"(bridge.py doctor --undo-promotion {seat.family} --reason \"<why>\" goes back to {step['from']})")
        if self.model_listed:
            return f"{subject} ({why}); the seat's preference list is exhausted"
        only_effort = by_catalog and self.effort_short and self.notes and all(" does not offer effort " in n for n in self.notes)
        if only_effort and not self.effort_listed:  # every account lists the model and lacks only the effort: the effort list is the remedy
            hint = t.effort_hint("codex", seat.model, seat.effort)
            return f"{subject} ({why}); the seat has no effort preference list, so it does not descend" + (f" (say {hint} to allow it)" if hint else "")
        hint = t.preference_hint(seat.model, seat.effort if seat.effort_explicit else None)
        return (f"{subject} ({why}); the seat has no preference list, so it does not descend"
                + (f" (say {hint} to allow it)" if hint else ""))

    def rejected(self, kind, attempt):
        """A startup rejection (design 3.3 step 5): the account is excluded for the seat's current model, its catalog invalidated and
        refreshed (registry), and the next choose() re-picks within the capable set minus the account; the preference list is
        consulted only once the set is exhausted. Returns None (the seat tries again) or the reason it ends."""
        routing = self.routing
        self.reject_kind = kind
        self.attempt = attempt
        label = routing.account if routing.routed else LOCAL_LABEL
        self.excluded.add(label)
        if self.registry and (routing.routed or self.local):  # the rejecting login's catalog is invalidated and read again
            self.refresh(label, force=True)
        if routing.routed and not routing.pinned:  # a pinned reply stays where its thread lives: only its preference list can move the model
            routing.leave()
        if attempt >= REPICK_LIMIT + int(table().policy.get("fallback_attempts") or 3):
            return f"{self.seat.model} rejected at startup on every account tried ({attempt} attempts); giving up"
        return None


def _codex_cursor(link, home, resume_id):
    """What the collector needs before a Codex attempt launches (docs/model-routing.md 6): the rollout file of a resumed thread and
    its size (`cursor_start`), 0 for a new thread (its file is found from thread.started afterwards), or the timestamp fallback when
    a resumed thread's file cannot be found before the launch."""
    t0 = time.time()
    if link is None or not home:
        return {"rollout_path": None, "cursor_start": None, "cursor_source": "none", "t0": t0}
    if not resume_id:
        return {"rollout_path": None, "cursor_start": 0, "cursor_source": "new-thread", "t0": t0}
    path = link.find_rollout(home, resume_id)
    if path is None:
        return {"rollout_path": None, "cursor_start": None, "cursor_source": "timestamp", "t0": t0}
    try:
        return {"rollout_path": str(path), "cursor_start": Path(str(path)).stat().st_size, "cursor_source": "size", "t0": t0}
    except OSError:
        return {"rollout_path": None, "cursor_start": None, "cursor_source": "timestamp", "t0": t0}


def _rollout_created(link, home, thread_id, since):
    """(rollout file, count) for a new thread's attempt: the one file of the thread created at or after the launch (`since`, epoch
    seconds; a file system without birth times gets a second of slack on the modification time). Several such files (a thread id
    shared by concurrent runs, which the CLI's own ids never are) are ambiguous: (None, count), and the collector records that
    instead of reading another run's turns. None created: the newest file of the thread at all, if any."""
    files = link.rollouts(home, thread_id)
    if since is not None:
        created = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            birth = getattr(st, "st_birthtime", None)
            if (birth is not None and birth >= since) or (birth is None and st.st_mtime >= since - 1.0):
                created.append(p)
        if len(created) == 1:
            return created[0], 1
        if created:
            return None, len(created)
    return (files[-1] if files else None), (1 if files else 0)


def _write_provenance(d, tag, records):
    try:
        fd = open_private(Path(d) / f"provenance-{tag}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, "a") as f:
            for rec in records:
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


def _apply_provenance(d, seat, r, summary, strict, link, notice=True):
    """The summary into the result and the seat record; a substitution is noticed (the seat completes, marked; a Claude seat's
    observed-model judge already notices and fails it, so `notice` is off there); under --strict-model a completed attempt fails on a
    substitution, a collection error or a missing observation (docs/model-routing.md 5, 6)."""
    tag = seat.tag
    r["provenance"] = summary
    update_seat(d, tag, provenance=summary, substituted=bool(summary.get("substituted")))
    if summary.get("substituted") and notice:
        sub = summary.get("substitution") or {}
        add_notice(d, "substituted", f"NOTICE: seat {tag}: SUBSTITUTED {summary.get('model_expected')} -> {sub.get('observed')} ({sub.get('class')}); "
                                     f"attempt {summary.get('attempt_id')} ran a model the seat did not ask for", seat=tag)
    if strict and r.get("ok") and link is not None:
        why = None
        if summary.get("substituted"):
            why = f"--strict-model: ran {summary.get('observed_model_id')}, requested {summary.get('model_expected')}"
        elif summary.get("errors"):
            why = f"--strict-model: provenance collection error(s): {', '.join(summary['errors'])}"
        elif summary.get("observed_model_id") is None:
            why = "--strict-model: no model observation for a completed attempt"
        if why:
            r["ok"] = False
            r["error"] = why
            add_notice(d, "strict-model", f"NOTICE: seat {tag}: {why}; the seat failed (its output is kept in raw-{tag}.md)", seat=tag)
    return summary


def _codex_provenance(d, seat, link, r, aid, home, pre, resume_id, last_thread, started_at, ended_at, refused, strict, evidence_text):
    """One Codex attempt's provenance (docs/model-routing.md 6), written to provenance-<tag>.jsonl and summarized in the seat record
    and the result: with a registry, the attempt's own window of the rollout file under the account's CODEX_HOME (the cursors taken
    before and after the launch; the timestamp fallback when none could be taken), correlated with the attempt's own event file; an
    attempt that started no thread is a null observation, never a guess. Without a registry only the identities are recorded."""
    tag = seat.tag
    requested, expected = seat.seat_name, seat.model
    thread_id = r.get("thread_id") or (resume_id if r.get("started") else None)
    outcome = "refused" if refused else ("cancelled" if r.get("cancelled") else ("success" if r.get("ok") else ("limit" if r.get("limit_seen") else "error")))
    if link is None or bridge_routing is None:
        summary = bridge_routing.builtin_summary(aid, requested, expected, [], outcome) if bridge_routing is not None else {
            "attempt_id": aid, "model_requested": requested, "model_expected": expected, "observed_model_id": None, "observed_models": [],
            "substituted": False, "substitution": None, "errors": [], "outcome": outcome, "downgraded_from": None}
        records = [dict(summary, kind="complete")]
    elif not thread_id:
        records = link.codex_no_thread(aid, requested, expected, evidence_text, r.get("rc"), outcome, effort=seat.effort, thread_ref=resume_id or last_thread)
        summary = bridge_routing.attempt_summary(records, link.path, link.sha256)
    else:
        source = pre.get("cursor_source")
        ambiguous = 0
        if source == "size" and thread_id == resume_id and pre.get("rollout_path"):  # the resumed thread's own file: its window starts where it ended
            rollout, cursor_start = pre["rollout_path"], pre.get("cursor_start") or 0
        elif source in ("timestamp", "none"):  # no cursor could be taken before the launch: the attempt's own start and end times bound the window
            rollout, cursor_start = link.find_rollout(home, thread_id), None
        else:  # a new thread (or one the CLI renamed on resume): the file this attempt created, read from its beginning
            rollout, count = _rollout_created(link, home, thread_id, pre.get("t0"))
            cursor_start = 0
            ambiguous = count if rollout is None and count > 1 else 0
        if ambiguous:  # several files of the thread were created during the attempt: none is known to be this attempt's (design 6: never a guess)
            records = link.codex_ambiguous(aid, thread_id, ambiguous, requested, expected, evidence_text, r.get("rc"), outcome, effort=seat.effort,
                                           thread_ref=resume_id or last_thread)
        else:
            if cursor_start is None:
                cursor_end = None
            else:
                try:
                    cursor_end = Path(str(rollout)).stat().st_size if rollout else cursor_start
                except OSError:
                    cursor_end = cursor_start
            out = link.codex_attempt(home, thread_id, aid, cursor_start, cursor_end, Path(d) / f"events-{tag}.jsonl", requested, expected, evidence_text,
                                     exit_status=r.get("rc"), start_ts=started_at, end_ts=ended_at, effort=seat.effort, rollout_path=rollout)
            records = out["records"]
            if source == "new-thread" and records:
                records[0]["cursor_source"] = "new-thread"
        summary = bridge_routing.attempt_summary(records, link.path, link.sha256)
    summary["downgraded_from"] = summary.get("downgraded_from") or seat.downgraded_from
    _write_provenance(d, tag, records)
    return _apply_provenance(d, seat, r, summary, strict, link)


def _claude_event_for_registry(e):
    """A Claude stream event as the registry's observer reads it (design 6): the CLI's usage-credits notice (`api_error:
    "model_requires_usage_credits"`, DESIGN §4's fourth predicate, a message the CLI writes in another model's name) carries the
    observer's own API-error flag, so the record keeps its telemetry and never counts it as a run; the other three predicates
    (`<synthetic>`, `error`, `is_api_error_message`) the observer knows."""
    if isinstance(e, dict) and e.get("type") == "assistant" and e.get("api_error") == "model_requires_usage_credits" and not e.get("is_api_error_message"):
        return dict(e, is_api_error_message=True)
    return e


def _claude_provenance(d, seat, link, r, aid, strict, evidence_text):
    """One Claude attempt's provenance (docs/model-routing.md 6): with a registry, the attempt's records from its own stream (start from
    system/init, a switch per observed change of message.model, complete with this attempt's observations, the result's cumulative
    ledger and the substitution verdict); without one, the bridge's own observation. #25's observed-model contract stands: a model
    other than the requested one fails the seat (_judge_observation); --strict-model also fails a completed attempt no observation reached."""
    tag = seat.tag
    requested, expected = seat.seat_name, seat.model
    obs = r.get("observation") or {}
    outcome = "cancelled" if r.get("cancelled") else ("success" if r.get("ok") else ("limit" if r.get("limit_seen") else "error"))
    if link is None or bridge_routing is None:
        observed = list(obs.get("models_observed") or [])
        summary = bridge_routing.builtin_summary(aid, requested, expected, observed, outcome,
                                                 observed_via="cli:message.model" if observed else "cli:message.model:none") if bridge_routing is not None else {
            "attempt_id": aid, "model_requested": requested, "model_expected": expected, "observed_model_id": observed[-1] if observed else None,
            "observed_models": sorted(observed), "substituted": any(m != expected for m in observed), "substitution": None, "errors": [], "outcome": outcome,
            "downgraded_from": None}
        records = [dict(summary, kind="complete")]
    else:
        events = [_claude_event_for_registry(e) for e in link.read_events(Path(d) / f"events-{tag}.jsonl")]
        records = link.claude_attempt(events, aid, requested, expected, evidence_text, exit_status=r.get("rc"))
        summary = bridge_routing.attempt_summary(records, link.path, link.sha256)
    _write_provenance(d, tag, records)
    return _apply_provenance(d, seat, r, summary, strict, link, notice=False)


def _expected_evidence(link, seat):
    """The evidence text a seat's expectation carries (design 6): the table's revision, and what the registry says of a Codex family's head."""
    t, _err = table_or_error()
    if t is None:
        return "bridge table"
    if link is None:
        return t.head_evidence(seat.family)
    return link.expected_evidence(t, seat.family)


def run_codex_seat(seat, prompt, d, cwd, *, sandbox="read-only", schema=None, resume=None, user_config=False, full_access=False, clock=None,
                   identity=None, pinned=False, strict=False, launch_class="interactive"):
    tag = seat.tag
    if is_cancelled(d):
        return dict(ok=False, cancelled=True, text="", secs=0, thread_id=None, error="cancelled before start")
    codex = resolve_codex()
    if not codex:
        update_seat(d, tag, state="failed", activity="failed: codex binary not found or unusable", finished=time.time())
        return dict(ok=False, text="codex binary not found or too old (install the ChatGPT desktop app or `npm i -g @openai/codex`, then `codex login`)",
                    secs=0, thread_id=None, error="codex not found")
    write_private_text(Path(d) / f"prompt-{tag}.md", prompt)
    routing = SeatRouting(d, seat, clock, identity=identity, pinned=pinned)
    link = registry_link()
    sel = CodexSelection(d, seat, routing, link, codex, cwd, user_config)
    evidence_text = _expected_evidence(link, seat)
    _seat_common_init(d, seat, dict(routing.identity, model_expected=seat.model, expected_evidence=evidence_text,
                                    registry_sha256=link.sha256 if link is not None else None))
    if link is not None:
        link.bundled(codex, sel.version)  # the installed CLI's bundled succession edges, once per process (docs/model-routing.md 3.3)
    t0 = time.time()
    attempt, limit_hits, work_began, cur_prompt, resume_id = 1, 0, False, prompt, resume
    throttle_hits = 0    # throttled or network attempts (5.1): their own ladder, never relay's hit, never a failover, never limit_hits
    cap_ctx = {}         # the capacity gate's per-seat memory (which once-per-seat lines and events it already produced)
    admitted_class = launch_class  # the class the last admission treated the seat as (batch over the interactive budget, B-5)
    update_seat(d, tag, **{"class": launch_class, "throttle_hits": 0})
    avoid, until = None, None  # the account whose limit was just reported, and the reset relay gave for it (its block lapses then, A2)
    last_thread = None  # the thread of the last attempt that started one, kept for the record when a failover dead-ends
    writable = sandbox != "read-only" or full_access
    advanced = None      # the snapshot of the LAST attempt whose thread started or continued: what a reply's continuation persists
    new_candidate = False  # the next attempt is a preference-list model that has not run yet (its refusal line says so)
    cli_ran = False        # an attempt of this candidate has started (S-LAUNCH); one relay refused or could not start does not count (RR-1)

    def ids(r):
        return {"thread_id": r.get("thread_id") or resume_id or last_thread, **routing.record()}

    def finish(res):
        res["advanced"] = advanced  # mode_reply persists the continuation from this, not from the final seat (DESIGN §2)
        return _finish_seat(d, seat, res, attempt, limit_hits, ids(res))

    while True:
        if attempt > 1:
            update_seat(d, tag, attempt=attempt, activity=f"attempt {attempt}", model=seat.model, effort=seat.effort)
        sel.attempt = attempt
        err = sel.choose(avoid, until)
        if sel.descended:  # the target has not run: its attempts get `initial` mode until it has
            new_candidate, cli_ran = True, False
            sel.descended = False
        if err:
            if err != "cancelled":
                err += routing.whereabouts("thread", last_thread)
            return finish(_no_attempt(d, err, t0))
        if link is not None:  # the selection read catalogs: what the registry says of the head may have moved past its floor
            evidence_text = _expected_evidence(link, seat)
            update_seat(d, tag, expected_evidence=evidence_text)
        # right before the launch, after the account selection and its waits (DESIGN §3): until this candidate's CLI has actually
        # run (the seat's first model, or a preference-list target), each attempt is validated in `initial` mode (a refusal ends the
        # seat: nothing has run yet); a retry of an attempt that ran is a continuation
        refusal = _launch_check(d, seat, not cli_ran, codex)
        if refusal:
            return finish(_no_attempt(d, f"fallback {seat.model} not launched: {refusal}" if new_candidate else refusal, t0))
        home = sel.home
        # the capacity gate (5.3), on the home this attempt really uses: every retry, ladder relaunch, failover, busy relaunch and reply
        # is re-admitted; the hold is visible (state waiting) and cancellable, and not charged to the seat's --timeout
        cap_token, cap_err, admitted_class = _capacity_admit(d, seat, home, clock, launch_class, cap_ctx)
        if cap_err:
            err = cap_err if cap_err == "cancelled" else cap_err + routing.whereabouts("thread", last_thread)
            return finish(_no_attempt(d, err, t0))
        try:
            # after EVERY admission (B-3): the gate may have held the seat, or its first decision may have blocked on the account lock for
            # a while and then admitted directly, and a pinned seat's sign-in verification may have gone stale meanwhile (a hold, or a
            # throttled continuation whose wait outlasted it, admitted at once). The stale verification is refreshed first (up to a
            # minute of relay calls), then the marker check runs again immediately before the launch, so a marker another check
            # published during the lock wait, the hold or the verification itself refuses it (as after a routing wait); a refusal
            # releases the admitted ledger entry
            refusal = routing.recheck_before_launch() or _launch_check(d, seat, not cli_ran, codex)
            if refusal:
                if refusal != "cancelled":
                    refusal = (f"fallback {seat.model} not launched: {refusal}" if new_candidate else refusal) + routing.whereabouts("thread", last_thread)
                return finish(_no_attempt(d, refusal, t0))
            pre = _codex_cursor(link, home, resume_id)
            started_at = bridge_routing.utc_now() if bridge_routing is not None else None
            r = _codex_attempt(seat, cur_prompt, d, cwd, sandbox=sandbox, schema=schema, resume=resume_id, user_config=user_config,
                               full_access=full_access, clock=clock, codex=codex, prefix=routing.prefix())
        finally:
            _capacity_release(home, cap_token)
        ended_at = bridge_routing.utc_now() if bridge_routing is not None else None
        refused = routing.refused(r)
        busy = refused and _busy_refusal(r, routing.account)  # BUSY-1: the account's slots are in use, not its usage
        # a busy refusal's provenance carries the suffix its archive gets (R1-3); the real attempt after the wait keeps `#<attempt>`
        aid = f"{Path(d).name}/{tag}#{attempt}" + (f".busy{routing.busy_waits + 1}" if busy else "")
        _codex_provenance(d, seat, link, r, aid, home, pre, resume_id, last_thread, started_at, ended_at, refused, strict, evidence_text)
        if r.get("started"):  # the CLI began under this candidate (S-LAUNCH; recorded as it began): its retries are continuations
            cli_ran, new_candidate = True, False
        if routing.routed and not refused:  # an attempt executed on this account
            routing.ran()
        if r.get("thread_id"):  # a thread actually started/continued under this account: it owns the conversation now
            routing.owns()
            advanced = {"model": seat.model, "family": seat.family, "effort": seat.effort, "thread_id": r["thread_id"], **routing.record()}
        last_thread = r.get("thread_id") or last_thread
        work_began = work_began or r.get("work_began", False)
        if r.get("timed_out") and r.get("throttle_class"):
            # a run killed while its final error was a throttle or a network failure (the post-wake WebSocket-403 storm): the seat ends
            # timed out as before, but the pacer learns of the trigger so the next launches on this account ramp (5.4)
            _pacer_trigger(home, r["throttle_class"], f"{Path(d).name}/{tag}")
            update_seat(d, tag, throttle_class=r["throttle_class"])
        if r.get("ok") or r.get("cancelled") or r.get("timed_out") or r.get("launch_error"):  # launch_error: an exception inside the attempt, whenever it struck
            if r.get("launch_error"):
                r["error"] = r["launch_error"]
            return finish(r)
        if r.get("error") and str(r["error"]).startswith("--strict-model"):  # a completed attempt the strict rule failed: no retry
            return finish(r)
        blob = (r.get("stderr_tail") or "") + " " + " ".join(r.get("errors") or [])
        if r.get("throttle_class"):
            # 5.1: ChatGPT refused or was overloaded, or the CLI could not reach it. Not a usage limit: relay is not told, the account is
            # not left, the usage-limit ladder is not entered. The pacer records the trigger (the next launches ramp), and the seat
            # waits on its class's ladder, resuming its thread if work began; an interactive seat waits twice and then fails.
            cls, evidence_line = r["throttle_class"], short(r["errors"][-1] if r.get("errors") else cls, 120)
            throttle_hits += 1
            _pacer_trigger(home, cls, f"{Path(d).name}/{tag}")
            update_seat(d, tag, throttle_hits=throttle_hits, throttle_class=cls)
            wait = _throttle_wait(throttle_hits, admitted_class)   # the ladder of the class the seat was admitted as (B-5)
            if wait is None:
                r["error"] = f"Codex throttled twice ({evidence_line}); not retried further"
                return finish(r)
            if work_began:
                rid = r.get("thread_id") or resume_id
                if rid:
                    resume_id, cur_prompt = rid, THROTTLE_CONTINUE_PROMPT
                    routing.pin_here()  # the thread lives in this account's home: every retry stays there (B-2)
                else:
                    r["error"] = f"Codex {'throttled' if cls == 'codex-throttled' else 'unreachable'} after work began and no thread to resume; rerun the task manually"
                    return finish(r)
            _archive_attempt(d, tag, attempt)
            why = (f"Codex throttled ({evidence_line})" if cls == "codex-throttled" else f"Codex unreachable ({evidence_line})") + f"; throttle wait {throttle_hits}"
            if not _wait_seat_uncharged(d, tag, time.time() + wait, clock, why, "throttle"):
                r["error"] = f"{why}; wait interrupted"
                return finish(r)
            attempt += 1
            continue
        if busy:
            # BUSY-1: no report, no limit count; the conversation, `avoid` and the attempt number stay as they are. After a short pause
            # the seat asks again: a pinned seat for its own account, any other with a new pick.
            err = routing.busy(f"account {routing.account}")
            if err:
                if err != "cancelled":
                    err += "; this thread lives there" if routing.pinned else routing.whereabouts("thread", last_thread)
                r["error"] = err
                return finish(r)
            _archive_attempt(d, tag, f"{attempt}.busy{routing.busy_waits}")  # before the relaunch: no later attempt inherits this stderr
            continue
        if (r.get("limit_seen") or refused) and routing.routed:
            # Routed: the account is spent. Tell relay, then move to the next account with the original prompt: a Codex thread
            # cannot leave its home. A writable seat that had begun work must not repeat its side effects elsewhere (A3).
            limit_hits += 1
            label = routing.account
            until = None if refused else routing.hit(label)
            at = f" at {_fmt_when(until)}" if until else ""
            if routing.pinned:
                tail = (r.get("stderr_tail") or "").strip().splitlines()
                r["error"] = (f"{tail[-1]}; this thread lives there, retry after it resets" if refused and tail else
                              f"usage limit on account {label}, where this thread lives; retry after its reset{at}")
                return finish(r)
            if work_began and writable:
                r["error"] = (f"usage limit on account {label} after work began (thread {r.get('thread_id') or resume_id}); "
                              f"rerun manually or wait for its reset{at}")
                return finish(r)
            _archive_attempt(d, tag, attempt)
            routing.leave()
            avoid, cur_prompt, resume_id = label, prompt, resume
            attempt += 1
            continue
        if r.get("limit_seen") and LIMIT_LADDER and not routing.routed:
            limit_hits += 1
            if work_began:
                rid = r.get("thread_id") or resume_id
                if not rid:
                    r["error"] = "usage limit after work began and no thread to resume; rerun the task manually"
                    return finish(r)
                resume_id, cur_prompt = rid, CONTINUE_PROMPT
            _archive_attempt(d, tag, attempt)
            if not _wait_on_ladder(d, tag, limit_hits, clock):
                r["error"] = "usage limit; wait interrupted"
                return finish(r)
            attempt += 1
            continue
        rejected = None
        if not work_began:
            if STARTUP_EFFORT_REJECT_RE.search(blob):
                rejected = "effort"
            elif STARTUP_MODEL_REJECT_RE.search(blob):
                rejected = "model"
        if rejected:
            # Availability is decided by the CLI at launch (DESIGN §3): its error line becomes the seat's first line. The rejecting
            # account leaves the capable set for this model and the seat re-picks within the rest (docs/model-routing.md 3.3 step 5);
            # only when the set is exhausted does the seat consult its explicit `|` preference list (models for a model rejection,
            # efforts for an effort rejection), and without one it ends here, naming the suggestion (design 5: no silent descent).
            line = _cli_error_line(r)
            what = seat.model if rejected == "model" else f"{seat.model} at effort {seat.effort}"
            add_notice(d, "startup-rejected", f"NOTICE: seat {tag}: {what} rejected at startup: {line}", seat=tag)
            update_seat(d, tag, activity=f"rejected at startup: {short(line, 80)}")
            err = sel.rejected(rejected, attempt)
            if err:
                r["error"] = err
                return finish(r)
            _archive_attempt(d, tag, attempt)
            attempt += 1
            continue
        return finish(r)


def _claude_attempt(seat, prompt, d, cwd, *, write, shell, schema, resume, max_turns, clock, claude, prefix=()):
    tag = seat.tag
    tools = list(CLAUDE_WRITE_TOOLS if (write and shell) else (CLAUDE_WRITE_TOOLS_NOSHELL if write else CLAUDE_READ_TOOLS))
    cmd = list(prefix) + [claude, "-p", "--output-format", "stream-json", "--verbose", "--model", seat.model]
    if seat.effort:  # a model with an empty table set (today's Haiku head) takes no effort flag
        cmd += ["--effort", seat.effort]
    cmd += ["--settings", json.dumps(CLAUDE_SEAT_SETTINGS), "--restricted", "--strict-mcp-config", "--add-dir", str(d), "--tools", ",".join(tools)]
    if write:
        cmd += ["--permission-mode", "acceptEdits"]
        if shell:
            cmd += ["--allowedTools", ",".join(CLAUDE_WRITE_BASH_ALLOW), "--disallowedTools", ",".join(CLAUDE_WRITE_BASH_DENY)]
    else:
        cmd += ["--permission-mode", "dontAsk"]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if schema:
        sch = {k: v for k, v in json.loads(Path(schema).read_text()).items() if k not in ("$schema", "title")}
        cmd += ["--json-schema", json.dumps(sch)]
    if resume:
        cmd += ["--resume", resume]
    ev_path, err_path = Path(d) / f"events-{tag}.jsonl", Path(d) / f"stderr-{tag}.log"
    flag = {"timed_out": False}
    st = dict(session_id=None, result=None, structured=None, usage=None, model_usage={}, subtype=None, errors=[], per_msg={}, ctx=0,
              items=0, is_error=False, limit_pending=False, tool_started=False, result_seen=False, init_model=None, observed=[], started=False)
    t0 = time.time()

    def observe(model):
        if isinstance(model, str) and model and model not in st["observed"]:
            st["observed"].append(model)

    def on_event(e):
        t = e.get("type")
        if (t == "system" and e.get("subtype") == "init") or t == "assistant" or e.get("session_id"):
            _cli_started(d, tag, seat.model, st)  # Claude's init or assistant event, or a session id: the CLI is running (S-LAUNCH)
        act = None
        if t == "system" and e.get("subtype") == "init":
            st["session_id"] = e.get("session_id") or st["session_id"]
            if isinstance(e.get("model"), str) and e["model"]:
                st["init_model"] = e["model"]
                observe(e["model"])
            act = "session started"
        elif t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            status = str(info.get("status", "allowed")).lower()
            if any(k in status for k in CLAUDE_LIMIT_STATUSES):
                st["limit_pending"] = True  # decides the outcome only if no model call succeeds afterwards
                act = "rate limited: " + short(status, 30)
        elif t == "assistant":
            m = e.get("message", {})
            # DESIGN §4: this attempt's observations leave out the CLI's synthetic messages (it speaking for itself) and a subagent's
            # forwarded ones (`parent_tool_use_id` set)
            synthetic = (m.get("model") == "<synthetic>" or e.get("error") == "rate_limit" or bool(e.get("is_api_error_message"))
                         or e.get("api_error") == "model_requires_usage_credits")
            if not synthetic and e.get("parent_tool_use_id") is None:
                observe(m.get("model"))
            u = m.get("usage") or {}
            mid = m.get("id") or f"anon-{len(st['per_msg'])}"
            if u:
                st["per_msg"][mid] = u
                st["ctx"] = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                st["limit_pending"] = False  # a model call went through after the rejected event
            for c in m.get("content", []):
                if c.get("type") == "tool_use":
                    st["items"] += 1
                    st["tool_started"] = True
                    inp = c.get("input") or {}
                    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
                    act = f"{c.get('name')}: {short(detail, 60)}" if detail else c.get("name")
                elif c.get("type") == "text" and not act:
                    act = "writing answer"
            act = act or "thinking"
        elif t == "result":
            st["result_seen"] = True
            st["result"] = e.get("result")
            st["structured"] = e.get("structured_output")
            st["usage"] = e.get("usage")
            st["model_usage"] = e.get("modelUsage") or {}
            st["session_id"] = e.get("session_id") or st["session_id"]
            st["is_error"] = bool(e.get("is_error"))
            st["subtype"] = e.get("subtype")
            errs = e.get("errors") or []
            st["errors"] = [str(x)[:300] for x in (errs if isinstance(errs, list) else [errs])]
            for mu in st["model_usage"].values():
                if isinstance(mu, dict) and mu.get("contextWindow"):
                    update_seat(d, tag, context_window=mu["contextWindow"])
            act = "done" if not st["is_error"] else f"error: {short(st['subtype'] or st['errors'][:1] or st['result'], 60)}"
        if act:
            cum_in = sum(v.get("input_tokens", 0) + v.get("cache_creation_input_tokens", 0) + v.get("cache_read_input_tokens", 0) for v in st["per_msg"].values())
            cum_out = sum(v.get("output_tokens", 0) for v in st["per_msg"].values())
            update_seat(d, tag, activity=act, items=st["items"], turns=len(st["per_msg"]), session_id=st["session_id"],
                        context_tokens=st["ctx"] or None, tokens_in=cum_in or None, tokens_out=cum_out or None)

    p = None
    wd = None
    try:
        p = _start(cmd, cwd, model_env(seat_env()), err_path)
        _register(tag, p)
        p._bridge_start = proc_start(p.pid)
        update_seat(d, tag, pid=p.pid, pid_start=p._bridge_start, pgid=getattr(p, "_bridge_pgid", None))
        if not prefix:  # unrouted, the spawn is the CLI's start; routed, relay may still refuse or fail before it becomes the CLI (S-LAUNCH)
            _cli_started(d, tag, seat.model, st)
        if _post_launch_cancel(d, tag, p):
            return dict(ok=False, cancelled=True, rc=None, text="", secs=round(time.time() - t0, 1), session_id=None, usage=None,
                        errors=[], stderr_tail="", tokens_in=0, tokens_out=0, timed_out=False, limit_seen=False, work_began=False, launch_error=None,
                        started=st["started"])
        wd = _watchdog(p, clock.remaining() if clock else None, flag)
        _feed_stdin(p, prompt)
        _pump_events(p, ev_path, on_event)
        rc = p.wait()
        kill_group(p, grace=0.5)
    except Exception as exc:  # before or after the CLI started: `started` says which
        if p is not None:
            kill_group(p, grace=1.0)
        return dict(ok=False, cancelled=is_cancelled(d), rc=None, text="", secs=round(time.time() - t0, 1), session_id=st["session_id"], usage=None,
                    errors=[repr(exc)], stderr_tail=_stderr_tail(err_path), tokens_in=0, tokens_out=0, timed_out=flag["timed_out"],
                    limit_seen=False, work_began=st["tool_started"], launch_error=repr(exc), started=st["started"])
    except BaseException:
        if p is not None:
            kill_group(p, grace=1.0)
        raise
    finally:
        if wd:
            wd.cancel()
        _unregister(tag)
    secs = round(time.time() - t0, 1)
    structured, result = st["structured"], st["result"]
    if structured is None and schema and result:
        try:
            structured = json.loads(result)
        except Exception:
            structured = None
    text = result if isinstance(result, str) else (json.dumps(result) if result is not None else "")
    write_private_text(Path(d) / f"raw-{tag}.md", text or "")  # the raw answer; last-<tag>.md is composed with the notices at the end
    # the observed-model record (DESIGN §4): complete only with the init model seen and a result carrying a non-empty modelUsage
    cumulative = [k for k in st["model_usage"] if isinstance(k, str)]
    observation = {"models_observed": list(st["observed"]), "session_models_cumulative": cumulative,
                   "complete": st["init_model"] is not None and bool(cumulative), "init_model": st["init_model"]}
    stderr_tail = _stderr_tail(err_path)
    usage = st["usage"]
    if usage:
        tokens_in = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
    else:
        tokens_in = sum(v.get("input_tokens", 0) + v.get("cache_creation_input_tokens", 0) + v.get("cache_read_input_tokens", 0) for v in st["per_msg"].values())
        tokens_out = sum(v.get("output_tokens", 0) for v in st["per_msg"].values())
    cancelled = is_cancelled(d)
    timed_out = flag["timed_out"]
    ok = rc == 0 and not st["is_error"] and bool(text or structured) and not timed_out and not cancelled
    diag = " | ".join(x for x in ([f"subtype={st['subtype']}"] if st["subtype"] and st["subtype"] != "success" else []) + st["errors"][:3])
    if st["result_seen"]:  # the structured result decides; stderr noise from earlier retries is ignored
        terminal_text = (str(st["result"]) if st["is_error"] else "") + " " + " ".join(st["errors"])
        limit_seen = (not ok) and (st["limit_pending"] or bool(LIMIT_RE.search(terminal_text)))
    else:
        limit_seen = (not ok) and rc != 0 and bool(LIMIT_RE.search("\n".join(stderr_tail.strip().splitlines()[-5:])))
    return dict(ok=ok, cancelled=cancelled, rc=rc, text=text, structured=structured, secs=secs, session_id=st["session_id"], usage=usage,
                stderr_tail=stderr_tail, tokens_in=tokens_in, tokens_out=tokens_out, context_tokens=st["ctx"], timed_out=timed_out,
                subtype=st["subtype"], errors=st["errors"], limit_seen=limit_seen, diag=diag, observation=observation,
                work_began=st["tool_started"] or (bool(text) and not st["is_error"]), launch_error=None, started=st["started"])


def run_claude_seat(seat, prompt, d, cwd, *, write=False, shell=False, schema=None, resume=None, max_turns=None, clock=None, identity=None, strict=False):
    tag = seat.tag
    if is_cancelled(d):
        return dict(ok=False, cancelled=True, text="", secs=0, session_id=None, error="cancelled before start")
    claude = resolve_claude()
    if not claude:
        update_seat(d, tag, state="failed", activity="failed: claude binary not found", finished=time.time())
        return dict(ok=False, text="claude binary not found (install Claude Code and sign in once)", secs=0, session_id=None, error="claude not found")
    write_private_text(Path(d) / f"prompt-{tag}.md", prompt)
    routing = SeatRouting(d, seat, clock)  # never pinned: a Claude session travels between accounts with `relay transplant`
    session_account = (identity or {}).get("account")  # the account whose store holds the session to resume (None: this machine's own store)
    link = registry_link()
    evidence_text = _expected_evidence(link, seat)
    _seat_common_init(d, seat, dict(routing.identity, model_expected=seat.model, expected_evidence=evidence_text,
                                    registry_sha256=link.sha256 if link is not None else None))
    t0 = time.time()
    attempt, limit_hits, work_began, cur_prompt, resume_id = 1, 0, False, prompt, resume
    avoid, until = None, None  # the account whose limit was just reported, and the reset relay gave for it (its block lapses then, A2)
    last_session = None  # the session of the last attempt that started one, kept for the record when a failover dead-ends
    advanced = None      # the snapshot of the LAST attempt whose session started or continued: what a reply's continuation persists
    cli_ran = False      # an attempt has started (S-LAUNCH); one relay refused or could not start does not count (RR-1)

    def ids(r):
        return {"session_id": r.get("session_id") or resume_id or last_session, "context_tokens": r.get("context_tokens") or None, **routing.record()}

    def finish(res):
        res["advanced"] = advanced  # mode_reply moves the seat's continuation only from an attempt whose session advanced, even on failure
        return _finish_seat(d, seat, res, attempt, limit_hits, ids(res))

    while True:
        if attempt > 1:
            update_seat(d, tag, attempt=attempt, activity=f"attempt {attempt}")
        err = routing.choose(avoid, until=until)
        if err:
            if err != "cancelled":
                err += routing.whereabouts("session", resume_id or last_session)
            return finish(_no_attempt(d, err, t0))
        if resume_id and routing.account and routing.account != session_account:
            ok, msg = routing.transplant(resume_id, cwd)
            if not ok:  # nowhere to carry the session: keep it, and its owner, where it already is
                return finish(_no_attempt(d, f"could not carry session {resume_id} to account {routing.account}: {msg}", t0))
            session_account = routing.account
        # right before the launch, after the account selection, its waits and the transplant (DESIGN §3): until the CLI has actually
        # run, each attempt is validated in `initial` mode (a refusal ends the seat: nothing has run yet); a retry of an attempt
        # that ran is a continuation
        refusal = _launch_check(d, seat, not cli_ran, claude, env=clean_env())
        if refusal:
            return finish(_no_attempt(d, refusal, t0))
        r = _claude_attempt(seat, cur_prompt, d, cwd, write=write, shell=shell, schema=schema, resume=resume_id, max_turns=max_turns,
                            clock=clock, claude=claude, prefix=routing.prefix())
        refused = routing.refused(r)
        if refused:  # relay refused the account before any CLI ran: there is no observation to judge, whatever ends the seat (Astra C2b-3)
            r.pop("observation", None)
        else:  # per-attempt provenance from this process's own stream (docs/model-routing.md 6)
            _claude_provenance(d, seat, link, r, f"{Path(d).name}/{tag}#{attempt}", strict, evidence_text)
        if r.get("started"):  # the CLI began (S-LAUNCH; recorded as it began): its retries are continuations
            cli_ran = True
        if routing.routed and not refused:  # an attempt executed on this account
            routing.ran()
        if r.get("session_id"):  # a session actually started/continued under this account: it owns the conversation now
            routing.owns()
            session_account, last_session = routing.account, r["session_id"]
            advanced = {"model": seat.model, "family": seat.family, "effort": seat.effort, "session_id": r["session_id"], **routing.record()}
        work_began = work_began or r.get("work_began", False)
        if r.get("ok") or r.get("cancelled") or r.get("timed_out") or r.get("launch_error"):  # launch_error: an exception inside the attempt, whenever it struck
            if r.get("launch_error"):
                r["error"] = r["launch_error"]
            elif not r.get("ok") and r.get("diag"):
                r["error"] = r["diag"]
            return finish(r)
        obs = r.get("observation")
        if obs is not None and not refused and _judge_observation(d, seat, r, obs):
            return finish(r)  # every attempt is judged before a retry or re-route (DESIGN §4); a model mismatch is terminal
        if refused and _busy_refusal(r, routing.account):
            # BUSY-1: the account's slots are in use, not its usage: no report, no limit count, and the session, its prompt and `avoid`
            # stay as they are (a seat that began work resumes as after any other wait). After a short pause the seat picks afresh.
            err = routing.busy(f"account {routing.account}")
            if err:
                r["error"] = err if err == "cancelled" else err + routing.whereabouts("session", resume_id or last_session)
                return finish(r)
            _archive_attempt(d, tag, f"{attempt}.busy{routing.busy_waits}")  # before the relaunch: no later attempt inherits this stderr
            continue
        if (r.get("limit_seen") or refused) and routing.routed:
            # Routed: tell relay and continue on the next account; the session is carried there before the resumed turn.
            limit_hits += 1
            label = routing.account
            until = None if refused else routing.hit(label)  # relay's reset for the label: choose() takes it again once that has lapsed (A2)
            if work_began:
                sid = r.get("session_id") or resume_id
                if not sid:
                    r["error"] = "usage limit after work began and no session to resume; rerun the task manually"
                    return finish(r)
                resume_id, cur_prompt = sid, CONTINUE_PROMPT
            _archive_attempt(d, tag, attempt)
            routing.leave()
            avoid = label
            attempt += 1
            continue
        if r.get("limit_seen") and LIMIT_LADDER and not routing.routed:
            limit_hits += 1
            if work_began:
                sid = r.get("session_id") or resume_id
                if not sid:
                    r["error"] = "usage limit after work began and no session to resume; rerun the task manually"
                    return finish(r)
                resume_id, cur_prompt = sid, CONTINUE_PROMPT
            _archive_attempt(d, tag, attempt)
            if not _wait_on_ladder(d, tag, limit_hits, clock):
                r["error"] = "usage limit; wait interrupted"
                return finish(r)
            attempt += 1
            continue
        if r.get("diag"):
            r["error"] = r["diag"]
        if not work_began and STARTUP_MODEL_REJECT_RE.search((r.get("stderr_tail") or "") + " " + " ".join(r.get("errors") or [])):
            # a Claude seat has no model fallback of any kind (DESIGN §4); the CLI's rejection is still the seat's first line
            add_notice(d, "startup-rejected", f"NOTICE: seat {tag}: {seat.model} rejected at startup: {_cli_error_line(r)}", seat=tag)
        return finish(r)


_warned_user_config = {"done": False}


def launch_class_of(a, default):
    """The launch class of a job (5.2): `--class` on the subcommand, else BRIDGE_CLASS, else the mode's default (fanout: batch; ask,
    council, review, task, reply: interactive). A value that is neither is a one-line usage error."""
    value = (getattr(a, "launch_class", None) or os.environ.get("BRIDGE_CLASS") or default).strip().lower()
    if value not in LAUNCH_CLASSES:
        die(f"the launch class must be one of {', '.join(LAUNCH_CLASSES)} (--class or BRIDGE_CLASS), not {value!r}")
    return value


def run_seat(seat, prompt, d, cwd, *, mode, schema=None, write=False, shell=False, full_access=False, user_config=False, resume=None, timeout=None,
             identity=None, pinned=False, strict=False, launch_class="interactive"):
    clock = Clock(timeout if timeout else None)  # deadline-free without --timeout: the uncharged credits still reach the linked routing budget (B-4)
    if user_config and not (write or full_access):
        # the user's own Codex MCP servers/plugins act outside the sandbox: never load them into a read-only seat
        user_config = False
        if not _warned_user_config["done"]:
            sys.stderr.write("bridge: --with-user-config is honoured only for write tasks; ignored for read-only seats\n")
            _warned_user_config["done"] = True
    if seat.vendor == "codex":
        sandbox = "workspace-write" if write else "read-only"
        return run_codex_seat(seat, prompt, d, cwd, sandbox=sandbox, schema=schema, resume=resume, user_config=user_config,
                              full_access=full_access, clock=clock, identity=identity, pinned=pinned, strict=strict, launch_class=launch_class)
    if full_access:
        die("--full-access is a Codex-only option; Claude seats always run under the restricted profile")
    return run_claude_seat(seat, prompt, d, cwd, write=write, shell=shell, schema=schema, resume=resume,
                           max_turns=CLAUDE_MAX_TURNS.get(mode, CLAUDE_MAX_TURNS["default"]), clock=clock, identity=identity, strict=strict)


def run_all(seats, prompts, d, cwd, **kw):
    results = {}
    threads = []
    for s in seats:
        def worker(s=s):
            try:
                results[s.tag] = run_seat(s, prompts[s.tag], d, cwd, **kw)
            except SystemExit as exc:  # die() inside a worker becomes a failed seat, not a crash
                results[s.tag] = dict(ok=False, text="", secs=0, error=f"seat aborted (exit {exc.code})")
                update_seat(d, s.tag, state="failed", finished=time.time(), activity=f"failed: aborted (exit {exc.code})")
            except Exception as exc:
                results[s.tag] = dict(ok=False, text="", secs=0, error=repr(exc))
                update_seat(d, s.tag, state="failed", finished=time.time(), activity="failed: " + short(repr(exc), 90))
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return [results.get(s.tag) or dict(ok=False, text="", secs=0, error="no result recorded") for s in seats]


def finalize(d, results):
    """Set the job's terminal state and return the process exit code."""
    if is_cancelled(d):
        update_meta(d, state="cancelled", finished=datetime.now().isoformat())
        return EXIT_FAIL
    oks = sum(1 for r in results if r.get("ok"))
    state = "done" if oks == len(results) else ("partial" if oks else "failed")
    update_meta(d, state=state, finished=datetime.now().isoformat())
    return {"done": EXIT_OK, "partial": EXIT_PARTIAL, "failed": EXIT_FAIL}[state]


def footer(jid, seats, results, d, extra=None):
    parts = [f"job: {jid}"]
    for s, r in zip(seats, results):
        acct = f" account={r['account']}" if r.get("account") else ""
        ref = f" thread={r['thread_id']}" if r.get("thread_id") else (f" session={r['session_id']}" if r.get("session_id") else "")
        tok = f" [{r.get('tokens_in') or 0:,} in / {r.get('tokens_out') or 0:,} out]"
        flag = "" if r.get("ok") else (" CANCELLED" if r.get("cancelled") else (" TIMED OUT" if r.get("timed_out") else f" FAILED ({short(r.get('error', ''), 60)})"))
        label = s.label() + (f" (fell back from {s.model_requested})" if s.model != s.model_requested else "")  # the model that ran
        prov = r.get("provenance") if isinstance(r.get("provenance"), dict) else {}
        if prov.get("substituted"):  # design 5: a run with an unauthorized substitution is marked in its footer
            label += f" SUBSTITUTED {prov.get('model_expected')} -> {prov.get('observed_model_id')}"
        parts.append(f"{s.tag}={label} {fmt_secs(r.get('secs'))}{acct}{ref}{tok}{flag}")
    if extra:
        parts.append(extra)
    parts.append(f"logs: {d}")
    return "\n— " + "\n— ".join(parts)


def save_findings(d, seat, res):
    data = res.get("structured")
    if isinstance(data, list):
        data = {"findings": data, "assessment": ""}
    if not isinstance(data, dict):
        try:
            data = json.loads(res.get("text") or "")
        except Exception:
            m = re.search(r"\{.*\}", res.get("text") or "", re.S)
            try:
                data = json.loads(m.group(0)) if m else None
            except Exception:
                data = None
    if isinstance(data, list):
        data = {"findings": data, "assessment": ""}
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        data = {"findings": [], "assessment": f"[{seat.tag}] returned no parseable JSON; see last-{seat.tag}.md", "_invalid": True}
        if res.get("ok"):  # a seat that finished but produced no usable review has failed the review
            res["ok"] = False
            res["error"] = "no parseable findings JSON"
            update_seat(d, seat.tag, state="failed", activity="failed: no parseable findings JSON")
    prov = res.get("provenance") if isinstance(res.get("provenance"), dict) else {}
    data.update(_reviewer=seat.tag, _model=seat.model, _effort=seat.effort, _secs=res.get("secs"), _thread_id=res.get("thread_id"),
                _session_id=res.get("session_id"), _ok=bool(res.get("ok")), _error=str(res.get("error") or ""),
                _timed_out=bool(res.get("timed_out")), _cancelled=bool(res.get("cancelled")),
                # docs/model-routing.md 5: requested, expected and observed stay distinct; observed values are never back-filled
                _model_requested=prov.get("model_requested") or seat.seat_name, _model_expected=prov.get("model_expected") or seat.model,
                _observed_models=list(prov.get("observed_models") or []), _observed_model_id=prov.get("observed_model_id"),
                _downgraded_from=prov.get("downgraded_from") or seat.downgraded_from, _substitution=prov.get("substitution"))
    atomic_write_json(Path(d) / f"findings-{seat.tag}.json", data)
    return data


def do_merge(d):
    """Returns (markdown, data, ok). A failed merge is reported, never silently treated as a clean review."""
    if merge_findings is None:
        return f"(merge unavailable: {_MERGE_IMPORT_ERROR}; per-seat findings-*.json are intact in {d})", None, False
    try:
        md, data = merge_findings.merge(d)
        return md, data, True
    except Exception as exc:
        return f"(merge failed: {exc!r}; per-seat findings-*.json are intact in {d})", None, False


class job_guard:
    """Guarantees a job reaches a terminal state even when a mode raises: children are killed and meta is finalized."""

    def __init__(self, d):
        self.d = d

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            return False
        _kill_all_children()
        m = read_json(Path(self.d) / "meta.json")
        if m.get("state") in (None, "created", "running"):
            update_meta(self.d, state="cancelled" if is_cancelled(self.d) else "failed", finished=datetime.now().isoformat(),
                        error=short(repr(exc), 300))
        return False


# ============================================================================ modes
def _install():
    _install_signal_handlers()


def mode_ask(a):
    _install()
    cwd = resolve_cwd(a.cwd)
    text = " ".join(a.text).strip() or sys.stdin.read()
    if not text.strip():
        die("ask needs a question")
    if a.file:
        text += "\n\nFiles to look at first: " + ", ".join(a.file)
    check = pre_check()  # DESIGN §3: (1) the re-check, (2-3) resolution and validation, (4) the gate, (5) the job
    seats = validate_seats(parse_seats(a.seats, DEFAULT_SEAT, a.effort, default_var="BRIDGE_DEFAULT_SEAT"))
    notice = admission(check)
    if len(seats) > 1:
        return council(seats, text, cwd, a, mode="ask", check=check, notice=notice)
    s = seats[0]
    jid, d = new_job("ask", a.job_dir)
    with job_guard(d):
        record_check(d, check, notice, seats)
        cls = launch_class_of(a, "interactive")
        update_meta(d, mode="ask", cwd=cwd, seats=[s.label()], question=text[:2000], state="running", started=datetime.now().isoformat(), **{"class": cls})
        prompt = render("ask", TEXT=text)
        r = run_seat(s, prompt, d, cwd, mode="ask", user_config=a.with_user_config, timeout=a.timeout, strict=bool(getattr(a, "strict_model", False)),
                     launch_class=cls)
        code = finalize(d, [r])
    body = r.get("text") or f"(no answer: {r.get('error', 'unknown error')})\n{r.get('stderr_tail', '')}"
    emit(body, d / f"last-{s.tag}.md", footer(jid, [s], [r], d), notices=job_notices(d))
    return code


def council(seats, text, cwd, a, mode="council", check=None, notice=None):
    jid, d = new_job(mode, a.job_dir)
    with job_guard(d):
        if check is not None:
            record_check(d, check, notice, seats)
        cls = launch_class_of(a, "interactive")
        update_meta(d, mode=mode, cwd=cwd, seats=[s.label() for s in seats], question=text[:2000], state="running", started=datetime.now().isoformat(),
                    **{"class": cls})
        prompt = render("council" if mode == "council" else "ask", TEXT=text)
        results = run_all(seats, {s.tag: prompt for s in seats}, d, cwd, mode=mode, user_config=a.with_user_config, timeout=a.timeout,
                          strict=bool(getattr(a, "strict_model", False)), launch_class=cls)
        code = finalize(d, results)
    per = max(1500, (MAX_STDOUT - 600) // max(1, len(results)))
    out = []
    for s, r in zip(seats, results):
        body = (r.get("text") or f"(no answer: {r.get('error', 'failed')})").strip()
        if len(body) > per:
            body = body[:per] + f"\n[… truncated; full answer: {d / ('last-' + s.tag + '.md')}]"
        out.append(f"## {s.tag} — {s.label()} ({fmt_secs(r.get('secs'))}){'' if r.get('ok') else ' — FAILED'}\n\n{body}")
    notices = job_notices(d)
    write_private_text(d / "council.md", lead_with(notices, "\n\n".join(out)))
    emit("\n\n".join(out), d / "council.md", footer(jid, seats, results, d), notices=notices)
    return code


def mode_council(a):
    _install()
    cwd = resolve_cwd(a.cwd)
    text = " ".join(a.text).strip() or sys.stdin.read()
    if not text.strip():
        die("council needs a question")
    check = pre_check()
    seats = validate_seats(parse_seats(a.seats, DEFAULT_COUNCIL_SEATS, a.effort, default_var="BRIDGE_COUNCIL_SEATS"))
    return council(seats, text, cwd, a, check=check, notice=admission(check))


def _read_intent(a):
    """--intent/--focus as flags, or --intent-stdin: a JSON object {"intent": …, "focus": …} on stdin (keeps user text out of argv)."""
    intent, focus = a.intent, a.focus
    if getattr(a, "intent_stdin", False):
        raw = sys.stdin.read()
        try:
            obj = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            die(f"--intent-stdin expects a JSON object on stdin: {exc}")
        if not isinstance(obj, dict):
            die("--intent-stdin expects a JSON object with optional 'intent' and 'focus' strings")
        for k in ("intent", "focus"):
            v = obj.get(k)
            if v is not None and not isinstance(v, str):
                die(f"--intent-stdin: '{k}' must be a string")
        intent = obj.get("intent") or intent
        focus = obj.get("focus") or focus
    return intent, focus


def mode_review(a):
    _install()
    cwd = resolve_cwd(a.cwd)
    check = pre_check()
    seats = validate_seats(parse_seats(a.seats, DEFAULT_REVIEW_SEATS, a.effort, default_var="BRIDGE_REVIEW_SEATS"))
    notice = admission(check)
    intent_text, focus_text = _read_intent(a)
    change = capture_change(cwd, base=a.base, commit=a.commit)
    if not change["patch"].strip():
        print(f"Nothing to review: no changes for {change['scope']} in {cwd}.")
        return EXIT_OK
    if change["lines"] > MAX_PATCH_LINES and not a.allow_large:
        die(f"change too large to review well: {change['lines']:,} diff lines across {len(change['files'])} files (limit {MAX_PATCH_LINES:,}). "
            f"Review per commit (--commit), against a nearer base, split with `fanout`, or pass --allow-large to proceed anyway.")
    jid, d = new_job("review", a.job_dir)
    with job_guard(d):
        record_check(d, check, notice, seats)
        patch_path = d / "review.patch"
        write_private_text(patch_path, change["patch"])
        atomic_write_json(d / "review.meta.json", change["meta"])
        intent = intent_text or "(not stated)"
        focus = f"\nExtra focus requested by the author:\n{focus_text}\n" if focus_text else ""
        warn = ""
        if change["lines"] > MAX_PATCH_LINES:
            warn = (f"large change: {change['lines']:,} diff lines across {len(change['files'])} files (reviewed with --allow-large); "
                    f"reviewer attention is spread thin, prefer per-commit reviews")
        fp_before = tree_fingerprint(cwd)
        cls = launch_class_of(a, "interactive")
        update_meta(d, mode="review", cwd=cwd, scope=change["scope"], kind=change["kind"], base=a.base, commit=a.commit, intent=intent, focus=focus_text,
                    seats=[s.label() for s in seats], diffstat=change["stat"][-4000:], patch_sha256=change["meta"]["sha256"],
                    patch_lines=change["lines"], files=change["files"][:500], warning=warn, tree_fingerprint=fp_before, state="running",
                    started=datetime.now().isoformat(), **{"class": cls})
        stat_line = f"{len(change['files'])} files, {change['lines']:,} diff lines"
        prompt = render("review", SCOPE=change["scope"], INTENT=intent, FOCUS=focus, PATCH_PATH=str(patch_path), PATCH_STAT=stat_line, CWD=cwd)
        results = run_all(seats, {s.tag: prompt for s in seats}, d, cwd, mode="review", schema=SCHEMA, user_config=a.with_user_config, timeout=a.timeout,
                          strict=bool(getattr(a, "strict_model", False)), launch_class=cls)
        for s, r in zip(seats, results):
            save_findings(d, s, r)
        # Did the reviewed inputs (the change, or anything else the seats could read) move while they were working?
        drift = ""
        try:
            now = capture_change(cwd, base=a.base, commit=a.commit)
            if now["meta"]["sha256"] != change["meta"]["sha256"]:
                drift = "the reviewed change moved during the review; findings refer to the frozen review.patch, not the current tree"
            elif fp_before is not None and tree_fingerprint(cwd) != fp_before:
                drift = "other files in the working tree changed during the review; seats may have read a mix of old and new context"
            if drift:
                update_meta(d, drift=True, drift_note=drift)
        except SystemExit:
            drift = "could not re-check the working tree after the review"
        code = finalize(d, results)
        update_meta(d, threads={s.tag: (r.get("thread_id") or r.get("session_id")) for s, r in zip(seats, results)})
        md, _, merged_ok = do_merge(d)
        if not merged_ok:
            code = EXIT_FAIL if code == EXIT_FAIL else EXIT_PARTIAL
            update_meta(d, merge_failed=True, state="partial" if code == EXIT_PARTIAL else read_json(d / "meta.json").get("state"))
    notes = " · ".join(x for x in (warn, drift, "" if merged_ok else "merge failed; read the per-seat findings files") if x)
    emit(without_notices_block(md), d / "merged.md", footer(jid, seats, results, d, extra=(f"note: {notes}" if notes else None)),
         notices=job_notices(d))
    return code


def mode_task(a):
    _install()
    cwd = resolve_cwd(a.cwd)
    text = " ".join(a.text).strip() or sys.stdin.read()
    if not text.strip():
        die("task needs instructions")
    check = pre_check()
    seats = parse_seats(a.seats, DEFAULT_SEAT, a.effort, default_var="BRIDGE_DEFAULT_SEAT")
    if len(seats) != 1:
        die("task takes exactly one seat; run separate tasks for several models")
    s = validate_seats(seats)[0]
    notice = admission(check)
    write = a.write or a.full_access
    if a.full_access and s.vendor != "codex":
        die("--full-access is a Codex-only option")
    if a.shell and not write:
        die("--shell only applies to --write tasks (it gives a Claude seat a fenced Bash tool)")
    if a.shell and s.vendor != "claude":
        sys.stderr.write("bridge: --shell has no effect on Codex seats (their sandbox already allows commands)\n")
    jid, d = new_job("task", a.job_dir)
    with job_guard(d):
        record_check(d, check, notice, [s])
        note = (" You MAY edit files in this repository; keep changes minimal and coherent and do not commit." if write else
                " You may NOT edit files (read-only): produce the plan, the exact patch as a unified diff, and the verification steps instead.")
        if write and s.vendor == "claude" and not a.shell:
            note += " You have no shell in this run: verify by reading files; if a command is essential, say exactly which one in your report."
        cls = launch_class_of(a, "interactive")
        update_meta(d, mode="task", cwd=cwd, seats=[s.label()], task=text[:2000], write=write, shell=bool(a.shell), full_access=a.full_access,
                    state="running", started=datetime.now().isoformat(), **{"class": cls})
        prompt = render("task", TEXT=text, WRITE_NOTE=note)
        r = run_seat(s, prompt, d, cwd, mode="task", write=write, shell=bool(a.shell), full_access=a.full_access, user_config=a.with_user_config,
                     timeout=a.timeout, strict=bool(getattr(a, "strict_model", False)), launch_class=cls)
        changed = ""
        if write and is_git_repo(cwd):
            rc, out, _ = git(cwd, "status", "--short")
            changed = out if rc == 0 else ""
        code = finalize(d, [r])
    body = r.get("text") or f"(no report: {r.get('error', 'failed')})\n{r.get('stderr_tail', '')}"
    if changed:
        body += "\n\nWorking tree after the task (git status --short):\n" + changed
    emit(body, d / f"last-{s.tag}.md", footer(jid, [s], [r], d), notices=job_notices(d))
    return code


def mode_fanout(a):
    """Spec JSON: {"seats": "astra:ultra", "schema": true, "context": "…", "tasks": {"name": "prompt" | {"prompt"|"prompt_file", "seats"}}}"""
    _install()
    spec = json.loads(Path(a.spec).read_text())
    cwd = resolve_cwd(a.cwd)
    tasks = spec.get("tasks") or {}
    if not tasks:
        die("fanout spec has no tasks")
    use_schema = bool(spec.get("schema", True))
    context = spec.get("context") or ""
    check = pre_check()
    # Validate and build everything BEFORE a job exists, so a bad spec never leaves a job behind.
    seats, prompts, used = [], {}, set()
    for name, task in tasks.items():
        task = {"prompt": task} if isinstance(task, str) else dict(task)
        try:
            prompt = task.get("prompt") or (Path(task["prompt_file"]).read_text() if task.get("prompt_file") else "")
        except OSError as exc:
            die(f"task {name}: cannot read prompt_file: {exc}")
        if not prompt.strip():
            die(f"task {name} has no prompt")
        if use_schema:
            prompt += ("\n\nOutput: respond ONLY with JSON matching the provided schema: `findings` (one entry per issue: priority P0-P3, "
                       "imperative title, file path relative to the repository root, line, explanation, suggested_fix, confidence 0-1), "
                       "ordered by priority, and `assessment` (what you covered, what you could not, residual risks). "
                       "Return an empty findings array if nothing qualifies; never invent a finding.")
        for s_ in parse_seats(task.get("seats") or spec.get("seats") or a.seats, DEFAULT_SEAT, a.effort, tag_prefix=sanitize_prefix(name), used_tags=used,
                              default_var="BRIDGE_DEFAULT_SEAT"):
            seats.append(s_)
            prompts[s_.tag] = (context + "\n\n" if context else "") + prompt
    validate_seats(seats)
    notice = admission(check)
    jid, d = new_job("fanout", a.job_dir)
    with job_guard(d):
        record_check(d, check, notice, seats)
        cls = launch_class_of(a, "batch")
        update_meta(d, mode="fanout", cwd=cwd, seats=[f"{s_.tag}={s_.label()}" for s_ in seats], schema=use_schema, spec=str(Path(a.spec).resolve()),
                    state="running", started=datetime.now().isoformat(), **{"class": cls})
        results = run_all(seats, prompts, d, cwd, mode="review" if use_schema else "ask", schema=SCHEMA if use_schema else None,
                          user_config=a.with_user_config, timeout=a.timeout, strict=bool(getattr(a, "strict_model", False)), launch_class=cls)
        if use_schema:
            for s_, r in zip(seats, results):
                save_findings(d, s_, r)
        code = finalize(d, results)
        update_meta(d, threads={s_.tag: (r.get("thread_id") or r.get("session_id")) for s_, r in zip(seats, results)})
        merged_ok = True
        if use_schema:
            md, _, merged_ok = do_merge(d)
            if not merged_ok:
                code = EXIT_FAIL if code == EXIT_FAIL else EXIT_PARTIAL
                update_meta(d, merge_failed=True, state="partial" if code == EXIT_PARTIAL else read_json(d / "meta.json").get("state"))
    notices = job_notices(d)
    if use_schema:
        emit(without_notices_block(md), d / "merged.md",
             footer(jid, seats, results, d, extra=None if merged_ok else "note: merge failed; read the per-seat findings files"), notices=notices)
    else:
        per = max(1500, (MAX_STDOUT - 600) // max(1, len(results)))
        out = []
        for s_, r in zip(seats, results):
            body = (r.get("text") or f"(no answer: {r.get('error', 'failed')})").strip()
            if len(body) > per:
                body = body[:per] + f"\n[… truncated; full answer: {d / ('last-' + s_.tag + '.md')}]"
            out.append(f"## {s_.tag} — {s_.label()} ({fmt_secs(r.get('secs'))}){'' if r.get('ok') else ' — FAILED'}\n\n{body}")
        write_private_text(d / "council.md", lead_with(notices, "\n\n".join(out)))
        emit("\n\n".join(out), d / "council.md", footer(jid, seats, results, d), notices=notices)
    return code


def _seat_files(d):
    return {p.name[5:-5]: read_json(p) for p in sorted(Path(d).glob("seat-*.json"))}


def _aggregate_state(d, meta=None):
    """Recompute the job state: running while ANY seat (original or reply) or registered reply is provably live; otherwise
    the outcome of the original seats (a merge failure keeps a job partial), so a reply never masks a partial or failed run
    and never ends a job another reply is still using."""
    meta = meta if meta is not None else read_json(Path(d) / "meta.json")
    all_seats = _seat_files(d)
    if is_cancelled(d):
        return "cancelled"
    for s in all_seats.values():
        if seat_live(s):
            return "running"
    for info in (meta.get("active_replies") or {}).values():
        if isinstance(info, dict) and info.get("pid") and pid_alive(info["pid"]) and proc_start(info["pid"]) == info.get("start"):
            return "running"
    states = [s.get("state") for t, s in all_seats.items() if not REPLY_TAG_RE.search(t)]
    if not states:
        return "done"
    oks = sum(1 for st in states if st == "done")
    if any(st in ("running", "waiting", "starting") for st in states):
        return "partial" if oks else "failed"  # a dead original seat that never finished
    if oks == len(states):
        return "partial" if meta.get("merge_failed") else "done"
    return "partial" if oks else "failed"


def _reply_preference(t, st, reply):
    """The preference list a Codex reply inherits from its recorded seat (docs/model-routing.md 5): the names of `astra|sol` after
    the one the thread runs now, resolved by today's table, so a startup rejection on the thread's own account can move the
    reply's model exactly as far as the seat's could. A seat that named no list leaves the reply without one: no silent descent."""
    names = [n.strip() for n in str(st.get("seat_name") or "").split("|") if n.strip()]
    if len(names) < 2:
        return []
    resolved = []
    for n in names:
        try:
            r = t.resolve(n)
        except bridge_models.Refusal:
            continue
        if r.vendor == "codex" and (r.model, r.family) not in resolved:
            resolved.append((r.model, r.family))
    at = next((i for i, (m, _f) in enumerate(resolved) if m == reply.model), None)
    if at is None:
        at = next((i for i, (_m, f) in enumerate(resolved) if f == reply.family), -1)
    return [x for x in resolved[at + 1:] if x[0] != reply.model]


def _reply_transition(d, tag, info=None):
    """Register (info given) or retire (info None) a reply and persist the recomputed job state under ONE job-wide lock,
    so two replies to different seats can never interleave their reads and writes of the job state."""
    with job_lock(d, name=".replies.lock"):
        m = read_json(Path(d) / "meta.json")
        active = dict(m.get("active_replies") or {})
        if info is not None:
            active[tag] = info
        else:
            active.pop(tag, None)
        update_meta(d, active_replies=active)
        m = read_json(Path(d) / "meta.json")
        state = "running" if info is not None else _aggregate_state(d, m)
        update_meta(d, state=state, replying=(tag if info is not None else None), **({} if info is not None else {"finished": datetime.now().isoformat()}))
        return state


def mode_reply(a):
    _install()
    d = find_job(a.job)
    meta = read_json(d / "meta.json")
    text = " ".join(a.text).strip() or sys.stdin.read()
    if not text.strip():
        die("reply needs a message")
    seat_files = {t: s for t, s in _seat_files(d).items() if not REPLY_TAG_RE.search(t)}
    if not seat_files:
        die(f"job {d.name} has no seats", EXIT_FAIL)
    tag = a.seat or (next(iter(seat_files)) if len(seat_files) == 1 else None)
    if not tag or tag not in seat_files:
        die(f"say which seat to continue with --seat; available: {', '.join(seat_files)}")
    st = seat_files[tag]
    if st.get("state") == "running" and (seat_process_live(st) or seat_supervised_live(st)):
        die(f"seat {tag} is still running (or between attempts under a live supervisor); wait for it to finish before replying", EXIT_FAIL)
    if st.get("state") == "waiting" and seat_waiting_live(st):
        die(f"seat {tag} {_waiting_words(st)} under a live supervisor; cancel the job or wait before replying", EXIT_FAIL)
    if a.shell and not a.write:
        die("--shell only applies to --write replies")
    cont = st.get("continuation") if isinstance(st.get("continuation"), dict) else {}
    ref = cont.get("thread_id") or cont.get("session_id") or st.get("thread_id") or st.get("session_id")
    if not ref:
        die(f"seat {tag} has nothing to resume", EXIT_FAIL)
    # DESIGN §2 "Reply": the base is the continuation (else what the record says ran); the reply runs on that family's head,
    # with a notice when it moves; the K1 seat keeps its own recorded identity; the effort is the base effort unless --effort is
    # given; then the validator in `initial` mode (§3). A refusal leaves the original seat's outcome unchanged and exits 1 (§4).
    check = pre_check()  # the re-check first, so that a marker it learns is seen by the validator; the gate only notifies a reply
    t = table()
    effort_preference = []
    try:
        base, _base_model, base_effort, moved = t.resolve_recorded(dict(st, tag=tag))
        if a.effort:
            # `--effort ultra|max`: the first is the reply's effort, the rest its effort preference list (docs/model-routing.md 5)
            efforts = [e.strip() for e in str(a.effort).split("|") if e.strip()]
            if not efforts or (base.vendor != "codex" and len(efforts) > 1):
                raise bridge_models.Refusal(f"'{a.effort}' is not an effort" if not efforts else
                                            "an effort preference list (`|`) is a Codex option; a Claude seat runs one effort")
            effort = t.resolve_effort(base.vendor, base.model, base.family, efforts[0], explicit=True)
            effort_preference = [t.resolve_effort(base.vendor, base.model, base.family, e, explicit=True) for e in efforts[1:]]
        elif base_effort is None:  # nothing recorded (a model without effort levels): the built-in default, as for a new seat
            effort = t.resolve_effort(base.vendor, base.model, base.family, None, explicit=False)
        else:  # the recorded effort is kept as it is; one the head does not support is refused, never silently replaced
            effort = base_effort
            supported = t.supported_efforts(base.model)
            if effort not in supported:
                allowed = ", ".join(supported) or "no effort level"
                raise bridge_models.Refusal(f"the recorded effort '{effort}' is not supported by {base.model} ({allowed}); "
                                            f"pass --effort with one of them" if supported else
                                            f"the recorded effort '{effort}' is not supported by {base.model} (no effort level); start a new seat")
        bridge_models.validate(t, evidence(), base, effort, "initial", _cli_facts(base.vendor))
    except bridge_models.Refusal as exc:
        return _refused_reply(d, tag, st, str(exc) if exc.complete else f"refused reply to seat '{tag}': {exc}")
    seat = Seat(base.vendor, base.model, effort, st.get("fast", False), tag, family=base.family, seat_name=st.get("seat_name") or tag,
                effort_explicit=bool(a.effort), k1=base.k1)
    reply_tag = f"{tag}-reply-{uuid.uuid4().hex[:5]}"
    try:
        lock = job_lock(d, name=f".resume-{tag}.lock", blocking=False)
        lock.__enter__()
    except OSError:
        die(f"another reply to seat {tag} is in progress", EXIT_FAIL)
    try:
        cwd = os.path.realpath(meta.get("cwd") or os.getcwd())
        if not os.path.isdir(cwd):
            die(f"the job's working directory no longer exists: {cwd}", EXIT_FAIL)
        if BRIDGE_ROOT and not _inside(cwd, BRIDGE_ROOT):
            die(f"the job's working directory {cwd} is outside the authorized root {BRIDGE_ROOT} (BRIDGE_ROOT)")
        # the reply is now certain to run: only here are its notices persisted, so a reply that died on the checks above (another
        # reply in progress, the directory gone or outside the root) leaves none behind in the job (B-opus-2)
        gate_notice = admission(check, refuse_allowed=False)
        if gate_notice:
            add_notice(d, "models-check", gate_notice, once=True)  # the same failed check across replies is ONE warning (Astra C3-13)
        if moved:  # recorded for the reply's own tag, so that its composed artifact discloses the substitution too
            add_notice(d, "reply-model", f"NOTICE: seat {tag}: reply runs on {seat.model} {moved}", seat=reply_tag)
        note_promotion(d, seat.model)
        reply = Seat(seat.vendor, seat.model, seat.effort, seat.fast, reply_tag, family=seat.family, seat_name=seat.seat_name,
                     effort_explicit=seat.effort_explicit, k1=seat.k1)
        reply.effort_preference = list(effort_preference)
        if seat.vendor == "codex":
            reply.preference = _reply_preference(t, st, reply)
        _reply_transition(d, reply.tag, {"pid": os.getpid(), "start": proc_start(os.getpid())})
        try:
            # A Codex thread lives in one account's home: the reply is pinned to the recorded account (or runs unrouted, as the
            # seat did). A Claude session is carried to whichever account relay routes the reply to.
            r = run_seat(reply, text, d, cwd, mode="reply", write=bool(a.write), shell=bool(a.shell), resume=ref, user_config=a.with_user_config,
                         timeout=a.timeout, identity={k: st.get(k) for k in IDENTITY_KEYS}, pinned=(seat.vendor == "codex"),
                         strict=bool(getattr(a, "strict_model", False)), launch_class=launch_class_of(a, "interactive"))
        except BaseException:
            _kill_all_children()
            _reply_transition(d, reply.tag, None)
            raise
        snap = r.get("advanced")  # the LAST attempt whose thread or session advanced, whatever the reply's final outcome (DESIGN §2)
        if seat.vendor == "codex":
            # A Codex reply is pinned: the account never moves. A reply that never advanced (rejected at startup, no account)
            # leaves the recorded location untouched; a fallback that never started a thread persists nothing of itself.
            if snap and snap.get("thread_id"):
                update_seat(d, tag, thread_id=snap["thread_id"], continuation=_continuation(reply, snap, "thread_id"))
        elif snap and snap.get("session_id"):
            # A Claude reply may transplant the session to another account and do work there, then still fail (max turns, an
            # error). The continuation now lives where it ran, so persist the new owner even on failure.
            update_seat(d, tag, session_id=snap["session_id"], continuation=_continuation(reply, snap, "session_id"),
                        **{k: snap.get(k) for k in IDENTITY_KEYS})
        _reply_transition(d, reply.tag, None)
    finally:
        lock.__exit__(None, None, None)
    body = r.get("text") or f"(no answer: {r.get('error', 'failed')})\n{r.get('stderr_tail', '')}"
    emit(body, d / f"last-{reply.tag}.md", footer(d.name, [reply], [r], d), notices=job_notices(d))
    return EXIT_OK if (r.get("ok") and not r.get("cancelled")) else EXIT_FAIL


def _continuation(reply, snap, ref_key):
    """The seat's continuation after a reply that advanced (DESIGN §1): the snapshot of the last attempt whose thread or session
    advanced (its model, family, effort, thread or session id and account), the reply tag and the time. The next reply starts
    from it, never from the requested family or from a later attempt that never started."""
    return {"model": snap["model"], "family": snap["family"], "effort": snap["effort"], "reply_tag": reply.tag, ref_key: snap.get(ref_key),
            "account": snap.get("account"), "at": time.time()}


def _refused_reply(d, tag, st, line):
    """DESIGN §4 "Reply outcome": a refused reply leaves the original seat's outcome unchanged. The reply gets its own seat record
    (failed), the parent's meta.json gains the notice and the usual bookkeeping, the refusal leads the output, exit 1."""
    reply_tag = f"{tag}-reply-{uuid.uuid4().hex[:5]}"
    _reply_transition(d, reply_tag, {"pid": os.getpid(), "start": proc_start(os.getpid())})
    now = time.time()
    update_seat(d, reply_tag, vendor=st.get("vendor"), model=st.get("model"), effort=st.get("effort"), fast=st.get("fast", False),
                seat_name=st.get("seat_name") or tag, state="failed", started=now, finished=now, first_launch_at=None,
                activity=f"failed: refused ({short(line, 70)})", supervisor_pid=os.getpid(), supervisor_start=proc_start(os.getpid()))
    kind = "models-marker" if " is marked " in line else "reply-model"
    add_notice(d, kind, f"NOTICE: seat {tag}: reply refused: {line}", seat=tag)
    _reply_transition(d, reply_tag, None)
    emit(f"(reply refused: {line})", None, f"\n— job: {d.name}\n— {reply_tag}: FAILED (refused)\n— logs: {d}", notices=job_notices(d))
    return EXIT_FAIL


def seat_rows(d):
    rows = []
    for p in sorted(Path(d).glob("seat-*.json")):
        s = read_json(p)
        state = s.get("state", "?")
        if state == "running" and not seat_process_live(s) and not seat_supervised_live(s):
            state = "died"
        elif state == "waiting":
            state = "waiting" if seat_waiting_live(s) else "died (waiting)"
        secs = (s.get("finished") or time.time()) - (s.get("started") or time.time())
        ctx = s.get("context_tokens")
        win = s.get("context_window") or 0
        if s.get("vendor") == "codex":
            ctxs = "n/a (Codex reports per-turn totals only)"
        else:
            ctxs = f"{ctx:,} ({ctx * 100 // win}% of {win // 1000}k)" if ctx and win else (f"{ctx:,}" if ctx else "—")
        rows.append((s.get("tag", p.name), str(s.get("model")) + (f"@{s.get('effort')}" if s.get("effort") else "") + ("@fast" if s.get("fast") else ""),
                     s.get("account") or "—", state,
                     fmt_secs(secs), ctxs, f"{s.get('tokens_in') or 0:,}/{s.get('tokens_out') or 0:,}", s.get("turns", 0), s.get("items", 0),
                     short(s.get("activity", ""), 60)))
    return rows


def render_seats(d):
    rows = seat_rows(d)
    if not rows:
        return "(no seats yet)"
    hdr = ("seat", "model@effort", "account", "state", "elapsed", "context (Claude: exact, % of window)", "in/out tokens (total)", "turns", "steps", "last activity")
    widths = [max(len(str(x)) for x in col) for col in zip(hdr, *rows)]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    return "\n".join([fmt.format(*hdr), fmt.format(*["-" * w for w in widths])] + [fmt.format(*[str(x) for x in r]) for r in rows])


def fallback_marks(d):
    """`<tag>: <requested> -> <ran>` for every original seat that ran on a fallback model, for the job list (B-astra-3): `ran` is the
    last model recorded when the seat's CLI actually ran (`models_launched`), never the seat's current model, which moves to a
    fallback target before the target launches (RR-6)."""
    marks = []
    for tag, s in _seat_files(d).items():
        ran = (s.get("models_launched") or [None])[-1]
        if not REPLY_TAG_RE.search(tag) and s.get("model_requested") and ran and ran != s["model_requested"]:
            marks.append(f"{tag}: {s['model_requested']} -> {ran}")
    return marks


def render_notices(d):
    """The job's persisted notices, one per line and verbatim, as the status and monitor views show them under the job line
    (B-astra-3); '' when there are none. Read as they are: no check and no gate runs here."""
    notices = job_notices(d)
    return "\n".join(notices) + "\n" if notices else ""


def job_is_live(d, m=None):
    m = m if m is not None else read_json(Path(d) / "meta.json")
    if m.get("state") not in NONTERMINAL_STATES:
        return False
    seats = list(_seat_files(d).values())
    if any(seat_live(s) for s in seats):
        return True
    for info in (m.get("active_replies") or {}).values():  # a reply registered but whose seat file is not written yet
        if isinstance(info, dict) and info.get("pid") and pid_alive(info["pid"]) and proc_start(info["pid"]) == info.get("start"):
            return True
    sup = m.get("supervisor_pid")
    if seats:
        return False
    try:
        age = time.time() - datetime.fromisoformat(m.get("started") or m.get("created")).timestamp()
    except Exception:
        return False  # no usable creation time: never treat as live
    return age < 600 and bool(sup) and pid_alive(sup)  # brief startup grace while the supervising engine is alive


def mode_status(a):
    jobs_root()
    if a.job:
        d = find_job(a.job)
        m = read_json(d / "meta.json")
        state = m.get("state")
        if state in NONTERMINAL_STATES and not job_is_live(d, m):
            state = "stale"
        print(f"{d.name}  mode={m.get('mode')} state={state} cwd={m.get('cwd')}" + (f"  note: {m.get('warning')}" if m.get("warning") else ""))
        print(render_notices(d) + render_seats(d))
        return EXIT_OK
    jobs = sorted((p for p in JOBS.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True)
    if not jobs:
        print("no jobs yet")
        print("\n".join(capacity_lines()))
        return EXIT_OK
    for p in jobs[: (100 if a.all else 15)]:
        m = read_json(p / "meta.json")
        state = m.get("state", "?")
        if state in NONTERMINAL_STATES and not job_is_live(p, m):
            state = "stale"
        marks = fallback_marks(p)  # the seats as requested, and which ran on a fallback model
        seats = ", ".join(m.get("seats") or []) + (f"  (fell back: {'; '.join(marks)})" if marks else "")
        print(f"{p.name:<34} {m.get('mode', '?'):<8} {state:<9} {str(m.get('started', ''))[:16].replace('T', ' '):<16} {seats}  {m.get('cwd', '')}")
        notices = job_notices(p)
        if notices:  # the count and the first notice; `status <job>` shows them all
            print(f"{'':<34} {len(notices)} notice{'s' if len(notices) != 1 else ''}: {notices[0]}")
    print("\n".join(capacity_lines()))
    return EXIT_OK


def mode_monitor(a):
    d = find_job(a.job)
    interval = max(1, a.interval)
    dead_checks = 0
    while True:
        m = read_json(d / "meta.json")
        live = job_is_live(d, m)
        block = f"{d.name}  mode={m.get('mode')} state={m.get('state')}  {datetime.now():%H:%M:%S}\n" + render_notices(d) + render_seats(d)
        if a.once:
            print(block + ("" if live or m.get("state") not in (None, "created", "running") else "\n(no live seat process: this job looks stale)"))
            return EXIT_OK
        sys.stdout.write("\x1b[2J\x1b[H" + block + "\n\n(ctrl-c to stop watching; the job keeps running)\n")
        sys.stdout.flush()
        if m.get("state") not in NONTERMINAL_STATES:
            return EXIT_OK
        dead_checks = 0 if live else dead_checks + 1
        if dead_checks >= 3:
            print("\nno live seat process remains and the job never reached a terminal state: stale")
            return EXIT_FAIL
        time.sleep(interval)


def _outcome_code(state, live):
    """The exit status a caller collecting a job should see: the persisted outcome, never a bare success for a failed job."""
    if state == "done":
        return EXIT_OK
    if state in ("failed", "cancelled"):
        return EXIT_FAIL
    return EXIT_PARTIAL  # partial, or still running/created (incomplete output)


def mode_result(a):
    d = find_job(a.job)
    m = read_json(d / "meta.json")
    state = m.get("state")
    live = job_is_live(d, m)
    if state in (None, "created", "running") and not live:
        state = "failed"  # nothing is running and the job never reached a terminal state: stale
    tail = f"\n— job: {d.name} · state: {state}{' (still running)' if live else ''} · logs: {d}"
    notices = job_notices(d)  # the job's notices lead every retrieval (DESIGN §4), whatever mode wrote the job

    def answer(path, raw):  # a raw answer verbatim; a composed artifact without the notice block the bridge added
        text = path.read_text()
        return text if raw else strip_notices(text, notices)

    if a.raw:
        if notices:
            print("\n".join(notices))
        for path, raw in answer_files(d):
            print(f"\n===== {path.name}\n{answer(path, raw)}")
        for f in sorted(d.glob("findings-*.json")):
            print(f"\n===== {f.name}\n{f.read_text()}")
        return _outcome_code(state, live)
    structured = m.get("mode") == "review" or (m.get("mode") == "fanout" and m.get("schema", True))
    if structured:
        md, _, merged_ok = do_merge(d)
        notices = job_notices(d)  # read after the merge: a notice persisted meanwhile is in merged.md's block and must lead stdout (Astra C2b-2)
        emit(without_notices_block(md), d / "merged.md", tail.replace(" · logs:", "" if merged_ok else " · MERGE FAILED · logs:"), notices=notices)
        if not merged_ok:
            return EXIT_FAIL if state in ("failed", "cancelled") else EXIT_PARTIAL
    elif (d / "council.md").exists():
        emit(strip_notices((d / "council.md").read_text(), notices), d / "council.md", tail, notices=notices)
    else:
        # the latest ACTUAL answer, from its own raw or last file: a reply refused before anything launched leaves an artifact
        # that is empty once the bridge's notices are removed, and it never hides the answer before it (Astra C2b-1)
        answers = sorted(answer_files(d), key=lambda x: x[0].stat().st_mtime)
        latest = [(path, raw) for path, raw in answers if answer(path, raw).strip()] or answers
        emit(answer(*latest[-1]) if latest else "(no output yet)", latest[-1][0] if latest else None, tail, notices=notices)
    return _outcome_code(state, live)


def mode_cancel(a):
    d = find_job(a.job)
    write_private_text(d / "CANCELLED", datetime.now().isoformat() + "\n")  # durable: runners check this before every launch/retry
    killed, skipped = [], []
    for tag, s in _seat_files(d).items():
        if s.get("state") == "waiting":
            # the supervisor's interruptible sleep watches the CANCELLED marker; nothing else to kill
            update_seat(d, tag, state="cancelled", activity="cancelled while waiting for a usage window", finished=time.time(), waiting_until=None)
            killed.append(f"{tag}: waiting supervisor told to stop")
            continue
        if s.get("state") != "running":
            continue
        if not seat_process_live(s):
            skipped.append(f"{tag}: process gone")
            update_seat(d, tag, state="cancelled", activity="cancelled (process already gone)", finished=time.time())
            continue
        pid = int(s["pid"])
        pgid = s.get("pgid") or pid
        own = os.getpgid(0)

        # A leaderless group is the supervising engine's job while that engine lives (it kills its groups on exit and honours
        # the CANCELLED marker); only when the supervisor is gone does cancel touch it, and then only with members that all
        # started inside the leader's lifetime (recorded start .. the moment cancel first observed the leader gone).
        seen = {}

        def leader_gone():
            if seat_process_live(s):
                return False
            seen.setdefault("t", time.time())
            return True

        def leaderless_ok():
            return leader_gone() and not _supervisor_live(s) and _group_alive(pgid) and _group_verified(pgid, s.get("pid_start"), seen["t"])

        def signal_seat(sig):
            # identity re-verified immediately before each signal: a live leader proves the group
            if seat_process_live(s):
                target_group = pgid if pgid != own else None
            elif leaderless_ok():
                target_group = pgid
            else:
                return False
            try:
                if target_group:
                    os.killpg(target_group, sig)
                else:
                    os.kill(pid, sig)
                return True
            except Exception:
                return False

        def still_alive():
            return seat_process_live(s) or (pgid != own and leaderless_ok())

        desc = _descendants(pid, s.get("pid_start")) if seat_process_live(s) else []  # sandboxed commands live in their own sessions
        signal_seat(signal.SIGTERM)
        deadline = time.time() + 5
        while time.time() < deadline and still_alive():
            time.sleep(0.1)
        if still_alive():
            signal_seat(signal.SIGKILL)
        if desc:
            _kill_descendants(desc, skip_pgids=(pgid,))
        killed.append(f"{tag}:{pid}")
        update_seat(d, tag, state="cancelled", activity="cancelled", finished=time.time())
    update_meta(d, state="cancelled", finished=datetime.now().isoformat())
    print(f"cancelled {d.name}: {', '.join(killed) or 'nothing was running'}" + (f"; {'; '.join(skipped)}" if skipped else ""))
    return EXIT_OK


def mode_merge(a):
    d = find_job(a.job)
    md, _, merged_ok = do_merge(d)
    emit(md, d / "merged.md", f"\n— job: {d.name}{'' if merged_ok else ' · MERGE FAILED'} · logs: {d}")
    return EXIT_OK if merged_ok else EXIT_PARTIAL


def _doctor_router_lines():
    """The router's state. Doctor never asks relay what it would pick: availability is decided by the CLI at launch (DESIGN §5)."""
    if not ROUTER_ON:
        return ["router: off (BRIDGE_ROUTER=off); seats run on this machine's own logins"]
    router = resolve_router()
    if router is None:
        where = (f"RELAY_BIN={os.environ['RELAY_BIN']} is not a usable file" if os.environ.get("RELAY_BIN") else
                 "no ~/.claude/skills/relay/scripts/relay.py and no `relay` on PATH; set RELAY_BIN to use another")
        return [f"router: relay not installed ({where}); seats run on this machine's own logins"]
    return [f"router: {router.path} (BRIDGE_ROUTER=off disables; a routed seat waits up to {fmt_secs(ROUTED_MAX_WAIT)} for an account, BRIDGE_ROUTED_MAX_WAIT)"]


def _iso(ts):
    e = bridge_models._epoch(ts) if bridge_models is not None else None
    return datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if e is not None else "never"


def _doctor_check_lines(t, check):
    """The verdict first, one line per finding (a remote line with the read time of the observation that supports it, a local
    line marked now), then the markers with their sources (informational ones marked)."""
    lines = [f"check: {check['verdict']} (evaluated_at {_iso(check['evaluated_at'])}; sources_refreshed_at {_iso(check.get('sources_refreshed_at'))}; "
             f"table_sha256 {check.get('table_sha256') or 'none'})" + (" [not refreshed: the evidence was busy]" if check.get("busy") else "")]
    for f in check["findings"]:
        lines.append(f"  {f['text']} [{'now' if f.get('local') else 'read ' + _iso(f.get('read_at'))}]")
    relevant = t.relevant_targets()
    markers = check.get("markers") or []
    lines.append("markers: " + ("; ".join(f"{m['id']} {m.get('kind')} ({m.get('source')}, seen {_iso(m.get('seen_at'))}"
                                          f"{', recorded as ' + m['spelling'] if m.get('spelling') else ''}"
                                          f"{'' if m['id'] in relevant else ', informational'})" for m in markers) if markers else "none"))
    return lines


def _doctor_table_lines(t):
    lines = [f"models: {MODELS_FILE} (table v{t.data.get('version')}, {t.data.get('updated')}); heads: "
             + ", ".join(f"{fam}={t.head(fam)}{' (promoted on this Mac)' if t.promotion_step(t.head(fam)) else ''}" for fam in t.family_names())]
    for fam in t.family_names():
        head, vendor = t.head(fam), t.vendor_of_family(fam)
        name = "codex-cli" if vendor == "codex" else "Claude Code"
        step = t.promotion_step(head)
        auto = (f"; promoted automatically {bridge_models._date_text(step['at'])} from {step['from']} ({step.get('cli') or 'codex'}; shipped head "
                f"{t.base.head(fam)})" if step else "")
        lines.append(f"  {head}: family {fam}; floor {name} >= {t.floor(head) or 'none'}; efforts {', '.join(t.supported_efforts(head)) or 'none'}{auto}")
    refused = ", ".join(key for vendor in ("codex", "claude") for key in t.refused[vendor])
    lines.append(f"  refused (each names its replacement head): {refused}")
    edges = ", ".join(f"{a} -> {b}" for vendor in ("codex", "claude") for a, b in t.fallback[vendor].items()) or "none"
    lines.append(f"  k1 seat {t.k1['seat']} -> {t.k1['id']}: {'on' if t.k1_enabled() else 'off'}; fallback: {edges}")
    return lines


def _doctor_promotion_lines(t):
    """This Mac's automatic promotions (DECISIONS A28): the ledger, every step in force, every step skipped and why, every model whose
    promotion was undone, and the last pass's result with the listed models it held back."""
    path = t.promotion.get("ledger") or str(evidence().promotions_path)
    try:
        ledger = bridge_models.load_promotions(path)
    except bridge_models.TableError:
        ledger = bridge_models.empty_ledger()
    applied, skipped = t.promotion["applied"], t.promotion["skipped"]
    mode, origin = promotion_mode(t)
    head = (f"promotions: {path}: " + ("; ".join(f"{s['family']} {s['from']} -> {s['to']} ({bridge_models._date_text(s['at'])}, {s.get('cli') or 'codex'})"
                                                  for s in applied) if applied else "none in force")
            + (" (Codex families: the check adopts a newer model this Mac's signed-in catalog lists; Claude families stay human checkpoints)"
               if mode == "auto" else f" (automatic promotion is off: {origin}; promotions in force stay until undone)"))
    lines = [head]
    for s, why in skipped:
        lines.append(f"  skipped: {s.get('family')} {s.get('from')} -> {s.get('to')}: {why}")
    for fam in dict.fromkeys(s["family"] for s in applied):  # models.json moved the head since the family's first step (applied or
        # skipped: the head it replaced): this Mac keeps the newer head of its ledger, and says so
        first = (next((s for s in ledger.get("steps") or [] if isinstance(s, dict) and s.get("family") == fam), None)
                 or next(s for s in applied if s["family"] == fam))
        shipped = t.base.head(fam)
        if shipped != first.get("from"):
            lines.append(f"  {fam}: models.json now names {shipped} as the head; this Mac keeps the newer {t.head(fam)} from its ledger "
                         f"(bridge.py doctor --undo-promotion {fam} --reason \"<why>\" goes back)")
    for mid, b in t.blocked.items():
        lines.append(f"  blocked: {b.get('family')} {mid}: {bridge_models.blocked_reason(b)}; not promoted here again unless models.json names it")
    for b, why in t.promotion.get("ignored") or []:
        lines.append(f"  block ignored: {b.get('id')}: {why}")
    last = ledger.get("last_pass") if isinstance(ledger.get("last_pass"), dict) else None
    if last:
        lines.append(f"  last pass {_iso(last.get('at'))}: {last.get('result')}")
        for h in last.get("held") or []:
            lines.append(f"    held back: {h.get('family')} {h.get('id')}: {h.get('reason')}")
    if applied:
        lines.append('  to go back on this Mac: bridge.py doctor --undo-promotion <family> --reason "<why>" (blocks that model here)')
    return lines


def _doctor_registry_lines(t):
    """The model registry the bridge links to (docs/model-routing.md 2.1) and whether the table's Codex heads agree with the heads
    the registry has accepted on this Mac (the F3 notes: the table decides the seat, the registry the routing evidence; neither
    replaces the other and a disagreement is reported). Model-free: local files only, no relay, no catalog read."""
    link = registry_link()
    if link is None:
        return [f"registry: none ({registry_link_error()}); the bridge keeps its built-in table and routing (no capable set, no per-attempt records)"]
    lines = [link.describe()]
    try:
        diff, floors = link.head_disagreements(t), link.floor_heads(t)
    except Exception as exc:
        diff, floors = [("?", "?", None, f"could not compare: {exc!r}")], []
    if not diff and not floors:
        lines.append("  codex heads: the table and the registry agree on every family")
    if floors:  # the registry's state holds no catalog evidence for these families yet: its floor is behind the table, not against it
        lines.append("  codex heads: no catalog evidence yet for " + ", ".join(f"{fam} (registry floor {floor}; the table's {mine} is its listed successor)"
                                                                            for fam, mine, floor in floors)
                     + "; a seat's catalog read promotes the registry; the table decides the seat")
    for fam, table_head, reg_head, source in diff:
        auto = (" (promoted automatically on this Mac; the registry moves only on a vendor edge or a checkpoint)"
                if t.promotion_step(table_head) else "")
        lines.append(f"  codex heads: {fam}: table {table_head}{auto}, registry {reg_head or 'unresolved'} ({source}); the table decides the seat")
    lines.append("  constrained routing: a Codex seat is routed only to accounts whose fresh catalog lists its model at the requested effort "
                 "(relay's pick confined to that set, protocol 2); no descent without a `|` preference list")
    return lines


def _doctor_source_lines(t, check):
    """Each source from the composed snapshot (no second load): its retained observation's read time and its latest attempt."""
    sources = check.get("sources") if isinstance(check.get("sources"), dict) else {}
    urls = bridge_models.source_urls()
    lines = []
    for name in ("codex", "npm", "deprecations", "overview"):
        src = sources.get(name) if isinstance(sources.get(name), dict) else {}
        att = src.get("attempt") or {}
        where = f"via {resolve_codex() or 'no codex'} app-server in {JOBS.parent}" if name == "codex" else urls[name]
        attempt = f"; last attempt {_iso(att.get('at'))}: {'ok' if att.get('ok') else 'failed: ' + str(att.get('error') or 'not read')}" if att else ""
        lines.append(f"  {name}: {where}; read {_iso(src.get('read_at'))}{attempt}")
    keys = (sources.get("codex") or {}).get("config_keys") if isinstance(sources.get("codex"), dict) else None
    if isinstance(keys, dict):
        lines.append("  codex config (config/read, keys only): " + ", ".join(f"{k}: {v}" for k, v in keys.items()))
    return ["sources (read in the local context only):"] + lines


def mode_doctor(a):
    _install()  # the check runs an app-server subprocess; a Ctrl-C or SIGTERM must not leave it behind
    lines = []
    plugin = read_json(SKILL_DIR.parent.parent / ".claude-plugin" / "plugin.json")
    if plugin.get("version"):
        lines.append(f"bridge: {plugin['version']}")
    t, table_error = table_or_error()
    if t is None:
        lines.append(f"models: {table_error}")
        print("\n".join(lines))
        return EXIT_USAGE  # a missing or malformed table: named, no traceback (R-E8); job control keeps working
    if a.undo_promotion:
        if not a.reason:
            die('--undo-promotion needs --reason "<text>"')
        ev = evidence()
        try:
            with ev.lock(deadline=bridge_models.lock_deadline()):
                ledger = bridge_models.load_promotions(ev.promotions_path)
                ledger, step, after = bridge_models.undo_promotion(t.base, ledger, a.undo_promotion, a.reason, ledger_path=ev.promotions_path)
                bridge_models.write_private_json(ev.promotions_path, ledger)
        except bridge_models.EvidenceBusy:  # A1: never claim an undo that did not happen; nothing was read or changed
            print(f"not undone: evidence busy (another process held the evidence lock for {bridge_models.lock_deadline():g}s, "
                  f"BRIDGE_MODELS_LOCK_DEADLINE); run the undo again")
            return EXIT_FAIL
        except bridge_models.TableError as exc:
            print(f"not undone: {exc}")
            return EXIT_FAIL
        except OSError as exc:  # the ledger could not be written (a read-only or missing cache directory): nothing changed
            print(f"not undone: cannot write the promotions ledger {ev.promotions_path}: {exc.strerror or exc}")
            return EXIT_FAIL
        _table_cache["table"] = (after, None)
        t = after
        undone_family, restored_head = step["family"], after.head(step["family"])
        lines.append(f"undone: {undone_family} {step['to']} -> {restored_head}; {step['to']} is blocked on this Mac (never promoted here again "
                     f"unless models.json names it); reason: {a.reason}")
    if a.clear_lifecycle:
        if not a.reason:
            die('--clear-lifecycle needs --reason "<text>"')
        try:
            marker = evidence().clear_lifecycle(t, a.clear_lifecycle, a.reason, deadline=bridge_models.lock_deadline())
        except bridge_models.EvidenceBusy:  # A1: never claim a clear that did not happen; nothing was read or changed
            print(f"not cleared: evidence busy (another process held the evidence lock for {bridge_models.lock_deadline():g}s, "
                  f"BRIDGE_MODELS_LOCK_DEADLINE); run the clear again")
            return EXIT_FAIL
        what = bridge_models.marker_line(marker) if marker else f"no marker on {a.clear_lifecycle}"
        lines.append(f"cleared: {what}; reason: {a.reason}")
    check = pre_check(force=True)
    t = table()  # the check's promotion pass may have changed it
    if a.undo_promotion and t.head(undone_family) != restored_head:  # the pass adopted another newer listed model (never the blocked one)
        lines.append(f"then: the check adopted {t.head(undone_family)} for {undone_family} (listed by this Mac's catalog and newer than "
                     f"{restored_head}); --undo-promotion {undone_family} again goes back to {restored_head}")
    codex, claude = resolve_codex(), resolve_claude()
    code = EXIT_FAIL if check["verdict"] == "FAIL" or check.get("busy") or not (codex or claude) else EXIT_OK
    if a.json:
        print(json.dumps(check, indent=1, default=str))
        return code
    lines += _doctor_check_lines(t, check)
    if codex:
        v = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT).stdout.strip()
        st = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
        status_text = ((st.stdout or "") + "\n" + (st.stderr or "")).strip().lower()  # Codex 0.153 prints this on stderr
        signed = st.returncode == 0 and "logged in" in status_text and "not logged in" not in status_text
        lines += [f"codex: {codex} ({v}) [{'via CODEX_BIN' if codex == os.environ.get('CODEX_BIN') else ('app bundle' if os.path.realpath(codex) in map(os.path.realpath, APP_LAUNCHERS) else 'standalone')}]",
                  f"  login: {'signed in — ' if signed else 'NOT signed in — '}{short((st.stdout or st.stderr).strip(), 80)}"
                  + (" | OPENAI_API_KEY set" if os.environ.get("OPENAI_API_KEY") else "")
                  + (f" | CODEX_HOME={os.environ['CODEX_HOME']}" if os.environ.get("CODEX_HOME") else "")]
    else:
        lines.append("codex: NOT FOUND or failed its --version probe — install the ChatGPT desktop app (bundles Codex) or `npm i -g @openai/codex`, then `codex login`")
    claude = resolve_claude()
    if claude:
        v = subprocess.run([claude, "--version"], capture_output=True, text=True, env=clean_env(), timeout=PROBE_TIMEOUT).stdout.strip()
        st = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, env=clean_env(), timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
        try:
            j = json.loads(st.stdout)
            auth = f"loggedIn={j.get('loggedIn')} via {j.get('authMethod')}" + (f" ({j.get('email')})" if j.get("email") else "")
        except Exception:
            auth = ((st.stdout or st.stderr).strip().splitlines() or ["unknown"])[0]
        lines += [f"claude: {claude} ({v})", f"  auth: {auth}"
                  + (" | CLAUDE_CODE_OAUTH_TOKEN set" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "")
                  + (" | ANTHROPIC_API_KEY set" if os.environ.get("ANTHROPIC_API_KEY") else "")
                  + (f" | CLAUDE_CONFIG_DIR={os.environ['CLAUDE_CONFIG_DIR']}" if os.environ.get("CLAUDE_CONFIG_DIR") else "")]
    else:
        lines.append("claude: NOT FOUND or failed its --version probe — install Claude Code and run `claude` once to sign in")
    lines += _doctor_table_lines(t)
    lines += _doctor_promotion_lines(t)
    lines += _doctor_source_lines(t, check)
    facts = local_facts()
    overrides = [k for k in bridge_models.HOST_OVERRIDE_VARS if k in facts["env"]]
    defaults = []
    for var, name in bridge_models.DEFAULT_LIST_NAMES.items():  # each effective list with its origin (S-DEF)
        seats, source = default_seats(var)
        defaults.append(f"{name}={seats} ({source or 'built-in default'})")
    lines += [f"defaults: {' '.join(defaults)} (env BRIDGE_DEFAULT_SEAT / BRIDGE_REVIEW_SEATS / BRIDGE_COUNCIL_SEATS override)",
              f"host: Claude Code {facts['claude_version'] or 'not found'} (the host agents' aliases need >= {t.highest_floor('claude')}); "
              f"override variables set: {', '.join(overrides) or 'none'}",
              f"relay claude cli: {facts['relay_claude_bin'] or 'none resolved'} ({facts['relay_claude_version'] or 'no version'})",
              "routed accounts: availability is decided by the CLI at launch; doctor does not consult relay"]
    lines += _doctor_router_lines()
    lines += _doctor_registry_lines(t)
    lines += capacity_lines()
    lines += [f"claude seats: --restricted, read-only tools {','.join(CLAUDE_READ_TOOLS)}; --write tools {','.join(CLAUDE_WRITE_TOOLS_NOSHELL)}"
              f" (+ fenced Bash only with --shell)",
              f"usage-limit ladder: {'on' if LIMIT_LADDER else 'off'} ({fmt_secs(LIMIT_WAIT_SHORT)} ×2 then {fmt_secs(LIMIT_WAIT_LONG)} cycles, each ×0.6-1.4; unrouted seats only)",
              f"throttle ladder: batch seats {'/'.join(fmt_secs(w) for w in THROTTLE_WAITS_BATCH)} then every {fmt_secs(THROTTLE_WAITS_BATCH[-1])} (×0.8-1.2, unbounded); "
              f"interactive seats {'/'.join(fmt_secs(w) for w in THROTTLE_WAITS_INTERACTIVE)} (×0.6-1.4) then the seat fails; never relay's hit, never a failover",
              f"jobs: {JOBS} (mode {oct(os.stat(JOBS).st_mode & 0o777) if JOBS.exists() else 'n/a'})",
              f"schema: {SCHEMA} ({'ok' if SCHEMA.exists() else 'MISSING'})", f"merger: {'ok' if merge_findings else 'IMPORT FAILED: ' + _MERGE_IMPORT_ERROR}"]
    print("\n".join(lines))
    return code


# ============================================================================ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="bridge.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    def common(p, text=True):
        p.add_argument("--seats", "--reviewers", "--model", dest="seats", help="model[:effort][@fast],... (see top of --help)")
        p.add_argument("--effort", help="effort applied to every seat that does not name one")
        p.add_argument("--cwd")
        p.add_argument("--timeout", type=int, help="seconds before a seat's process group is killed (default none)")
        p.add_argument("--with-user-config", action="store_true", help="Codex seats load ~/.codex/config.toml (your MCP servers, plugins)")
        p.add_argument("--job-dir", help="job NAME to create under the jobs root (default: generated)")
        p.add_argument("--strict-model", action="store_true",
                       help="fail a seat whose attempt ran a model other than the requested one, or whose model observation could not be collected")
        p.add_argument("--class", dest="launch_class", choices=LAUNCH_CLASSES,
                       help="launch class for the Codex capacity gate (default BRIDGE_CLASS, else interactive; fanout: batch)")
        if text:
            p.add_argument("text", nargs="*")

    p = sub.add_parser("ask"); common(p); p.add_argument("--file", action="append")
    p = sub.add_parser("council"); common(p)
    p = sub.add_parser("review"); common(p, text=False)
    g = p.add_mutually_exclusive_group(); g.add_argument("--base"); g.add_argument("--commit"); g.add_argument("--uncommitted", action="store_true")
    p.add_argument("--intent"); p.add_argument("--focus")
    p.add_argument("--intent-stdin", action="store_true", help='read a JSON object {"intent": …, "focus": …} from stdin instead of flags')
    p.add_argument("--allow-large", action="store_true", help=f"review even when the change exceeds {MAX_PATCH_LINES:,} diff lines")
    p = sub.add_parser("task"); common(p); p.add_argument("--write", action="store_true"); p.add_argument("--full-access", action="store_true")
    p.add_argument("--shell", action="store_true", help="with --write: give a Claude seat a fenced Bash tool (git status/diff/log/add, tests); off by default")
    p.add_argument("--no-user-config", action="store_true", help="(default now; kept for compatibility)")
    p = sub.add_parser("fanout"); common(p, text=False); p.add_argument("--spec", required=True)
    p = sub.add_parser("reply"); p.add_argument("job"); p.add_argument("--seat"); p.add_argument("--effort"); p.add_argument("--timeout", type=int)
    p.add_argument("--cwd", help="accepted for symmetry; the job's recorded working directory is used")
    p.add_argument("--with-user-config", action="store_true"); p.add_argument("--write", action="store_true"); p.add_argument("--shell", action="store_true")
    p.add_argument("--strict-model", action="store_true")
    p.add_argument("--class", dest="launch_class", choices=LAUNCH_CLASSES, help="launch class for the Codex capacity gate (default BRIDGE_CLASS, else interactive)")
    p.add_argument("text", nargs="*")
    p = sub.add_parser("status"); p.add_argument("job", nargs="?"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("monitor"); p.add_argument("job", nargs="?", default="last"); p.add_argument("--once", action="store_true"); p.add_argument("--interval", type=int, default=3)
    p = sub.add_parser("result"); p.add_argument("job", nargs="?", default="last"); p.add_argument("--raw", action="store_true")
    p = sub.add_parser("cancel"); p.add_argument("job")
    p = sub.add_parser("merge"); p.add_argument("job", nargs="?", default="last")
    p = sub.add_parser("doctor"); p.add_argument("--json", action="store_true", help="print the composed verdict as one JSON object")
    p.add_argument("--clear-lifecycle", metavar="MODEL_ID", help="clear the sticky lifecycle marker on a model (with --reason)")
    p.add_argument("--undo-promotion", metavar="FAMILY", help="undo this Mac's latest automatic promotion of a Codex family (with --reason); "
                                                             "its model is blocked here")
    p.add_argument("--reason", help='why the marker is cleared or the promotion undone, e.g. "vendor reversed the deprecation"')

    argv = list(sys.argv[1:] if argv is None else argv)
    tail = []
    if "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if tail:
        a.text = list(getattr(a, "text", None) or []) + tail
    if getattr(a, "strict_model", False) and registry_link() is None:  # the flag's guarantee needs the collector (docs/model-routing.md 6)
        die(f"--strict-model needs the model registry, which collects each attempt's model observation; none is linked: {registry_link_error()}")
    return {"ask": mode_ask, "council": mode_council, "review": mode_review, "task": mode_task, "fanout": mode_fanout, "reply": mode_reply,
            "status": mode_status, "monitor": mode_monitor, "result": mode_result, "cancel": mode_cancel, "merge": mode_merge,
            "doctor": mode_doctor}[a.mode](a)


if __name__ == "__main__":
    sys.exit(main())

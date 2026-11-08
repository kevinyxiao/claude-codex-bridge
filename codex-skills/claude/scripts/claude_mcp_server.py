#!/usr/bin/env python3
"""Serve the Claude bridge over newline-delimited JSON-RPC on stdio.

Only explicit CLAUDE_BRIDGE_ALLOW_WRITE=1 at startup enables write tasks.
Attached engines and their seats are terminated on cancellation or shutdown.
Detached review/task jobs survive requests and server shutdown.
Python 3.9+, standard library only.
"""
import codecs
import contextlib
import json
import math
import os
import queue
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
_ENGINES = (_HERE.parents[3] / "skills" / "codex" / "scripts" / "bridge.py",
            _HERE.parents[2] / "codex" / "scripts" / "bridge.py")
ENGINE = Path(os.environ.get("CLAUDE_BRIDGE_ENGINE") or
              next((path for path in _ENGINES if path.is_file()), _ENGINES[0])).resolve()
PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
NEWEST_PROTOCOL_VERSION = "2025-06-18"
JOB_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}"
SEATS_PATTERN = r"^[A-Za-z0-9_./:@,|-]+$"  # `|` joins a Codex seat's preference list (docs/model-routing.md 5); the engine validates the grammar
SINGLE_SEAT_PATTERN = r"^[A-Za-z0-9_./:@|-]+$"
TAG_PATTERN = r"^[A-Za-z0-9_.-]+$"
TEXT_LIMIT = 16000
TERMINATE_GRACE = 5.0
OUTPUT_FLUSH_GRACE = 0.5
SERVER_INFO = {"name": "claude-bridge", "title": "Claude (via local Claude Code login)", "version": "2.0.0"}
BUILTIN_SEATS = {"BRIDGE_BUILTIN_DEFAULT_SEAT": "fable:max", "BRIDGE_BUILTIN_REVIEW_SEATS": "opus:max",
                 "BRIDGE_BUILTIN_COUNCIL_SEATS": "fable:max,opus:max"}  # the server's own lists, never the user-facing variables (S-DEF)
SEATS_DESC = ("Seat spec model[:effort]. Claude families: fable (strongest), opus, sonnet, haiku, plus the pinned seat opus-5 (the K1 seat "
              "for legal work); a family name always means the newest model in that family (the engine's doctor lists the exact IDs), and a "
              "retired or superseded model ID is refused with the replacement named. Effort low|medium|high|xhigh|max (haiku takes none). "
              "Several seats comma-separated run in parallel (e.g. 'fable:max,opus:max'). A Codex seat may name its only permitted "
              "descent with | (e.g. 'astra|sol:low'); a Claude seat never descends.")
WAIT_DESC = ("With wait=true (default), calls are subject to CLAUDE_BRIDGE_TOOL_DEADLINE (default 3300 seconds) "
             "and Codex's tool timeout. The usage-limit retry ladder inside a seat may need wait=false: "
             "start a detached job, then poll claude_status and collect with claude_result.")
TOOLS = [
    {"name": "claude_ask",
     "description": "Ask Claude one hard question in a fresh context with read-only access to the repository at cwd. Default seat fable:max. "
                    "Give it the context it needs in the prompt; it cannot see this conversation.",
     "inputSchema": {"type": "object", "required": ["prompt"], "additionalProperties": False, "properties": {
         "prompt": {"type": "string", "minLength": 1},
         "seats": {"type": "string", "pattern": SEATS_PATTERN, "description": SEATS_DESC},
         "cwd": {"type": "string", "description": "Repository or directory (default: current)."},
         "files": {"type": "array", "items": {"type": "string"}, "description": "Files Claude should read first."}}}},
    {"name": "claude_review",
     "description": "Independent defect-first code review by Claude (default opus:max) of a git scope: uncommitted changes (default), "
                    "branch vs a base ref, or one commit. Returns P0-P3 findings with file:line, explanation, fix, confidence, assessment. "
                    "Pass intent (what changed and why). " + WAIT_DESC,
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "scope": {"type": "string", "enum": ["uncommitted", "base", "commit"]},
         "ref": {"type": "string", "minLength": 1},
         "intent": {"type": "string"}, "focus": {"type": "string"},
         "wait": {"type": "boolean", "default": True, "description": WAIT_DESC},
         "seats": {"type": "string", "pattern": SEATS_PATTERN, "description": SEATS_DESC},
         "cwd": {"type": "string"}},
         "allOf": [{"if": {"required": ["scope"], "properties": {"scope": {"enum": ["base", "commit"]}}},
                    "then": {"required": ["ref"]}}]}},
    {"name": "claude_task",
     "description": "Delegate a single-seat task to Claude (default fable:max; exactly one seat). Read-only unless write=true and the server was started with "
                    "CLAUDE_BRIDGE_ALLOW_WRITE=1. Write tasks instruct Claude to edit the repository without committing. " + WAIT_DESC,
     "inputSchema": {"type": "object", "required": ["prompt"], "additionalProperties": False, "properties": {
         "prompt": {"type": "string", "minLength": 1}, "write": {"type": "boolean"},
         "seats": {"type": "string", "pattern": SINGLE_SEAT_PATTERN,
                   "description": "Exactly one seat model[:effort], e.g. fable:max; no commas."},
         "wait": {"type": "boolean", "default": True, "description": WAIT_DESC},
         "cwd": {"type": "string"}}}},
    {"name": "claude_reply",
     "description": "Continue a previous seat (follow-up) using the job id from an earlier result; add seat when the job had several.",
     "inputSchema": {"type": "object", "required": ["job", "prompt"], "additionalProperties": False, "properties": {
         "job": {"type": "string", "pattern": "^" + JOB_PATTERN + "$"},
         "seat": {"type": "string", "pattern": TAG_PATTERN},
         "prompt": {"type": "string", "minLength": 1}, "cwd": {"type": "string"}}}},
    {"name": "claude_status",
     "description": "Check a job's live state and seats, including a detached review or task. Pass its job id (or last).",
     "inputSchema": {"type": "object", "required": ["job"], "additionalProperties": False, "properties": {
         "job": {"type": "string", "pattern": "^" + JOB_PATTERN + "$"}}}},
    {"name": "claude_result",
     "description": "Collect a job's current output, including a detached review or task. Poll claude_status to check completion first.",
     "inputSchema": {"type": "object", "required": ["job"], "additionalProperties": False, "properties": {
         "job": {"type": "string", "pattern": "^" + JOB_PATTERN + "$"}}}},
]
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
_out = threading.Lock()
_log = threading.Lock()


def log(msg):
    # Diagnostics must never prevent cancellation when the host stops reading.
    with _log:
        try:
            fd = sys.stderr.fileno()
            os.set_blocking(fd, False)
            os.write(fd, f"[claude-bridge] {msg}\n".encode("utf-8", errors="backslashreplace"))
        except OSError:
            pass


class InvalidArguments(ValueError):
    pass


def validate(value, schema, field="arguments"):
    """Enforce the small subset of JSON Schema used by the tool arguments."""
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    kind = schema.get("type")
    if kind and not isinstance(value, types[kind]):
        raise InvalidArguments(f"{field} must be of type {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidArguments(f"{field} must be one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise InvalidArguments(f"{field}.{key} is required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties") is False:
                    raise InvalidArguments(f"{field}.{key} is not allowed")
            else:
                validate(item, properties[key], f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{field}[{index}]")
    elif isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            raise InvalidArguments(f"{field} must be a non-empty string")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise InvalidArguments(f"{field} does not match {schema['pattern']}")
        if "\0" in value:
            raise InvalidArguments(f"{field} must not contain NUL characters")


def confined(path, root, allow_any, field):
    if not allow_any and path != root and root not in path.parents:
        raise InvalidArguments(f"{field} must be inside the authorized root {root}")
    return path


def resolve_job(ref, jobs_root, root, allow_any, launched_jobs=None):
    """Resolve aliases once, then authorize the cwd the engine will resume/read."""
    try:
        names = sorted(path.name for path in jobs_root.iterdir()
                       if path.is_dir() and not path.is_symlink())
        if ref in ("last", "latest"):
            ref = max(names, key=lambda name: (job_created_at(jobs_root / name), name)) if names else ref
        if ref not in names:
            hits = [name for name in names if ref in name]
            if len(hits) != 1:
                raise ValueError("job not found or ambiguous")
            ref = hits[0]
        if not re.fullmatch(JOB_PATTERN, ref):
            raise ValueError("invalid job name")
        directory = jobs_root / ref
        if directory.is_symlink() or directory.resolve(strict=True).parent != jobs_root:
            raise ValueError("job is outside the jobs root")
        if not allow_any:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            cwd = meta.get("cwd") if isinstance(meta, dict) else None
            # A new job is announced before the engine records its cwd. Polling
            # may use this server's authorized launch cwd until that happens.
            if isinstance(meta, dict) and "cwd" not in meta and launched_jobs:
                cwd = launched_jobs.get(ref)
            if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
                raise ValueError("meta.json must record an absolute cwd")
            confined(Path(cwd).resolve(), root, allow_any, "arguments.job cwd")
        return directory
    except (OSError, ValueError, RuntimeError) as exc:
        raise InvalidArguments(f"arguments.job cannot be authorized: {exc}") from exc


def job_created_at(directory):
    # Match the engine's alias ordering, including pre-existing job metadata.
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    for key in ("created", "started"):
        try:
            return datetime.fromisoformat(meta[key]).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            pass
    try:
        return directory.stat().st_mtime
    except OSError:
        return 0.0


def prepare_call(params, server):
    name = params.get("name")
    if not isinstance(name, str) or name not in SCHEMAS:
        raise InvalidArguments("name must identify a known tool")
    args = params.get("arguments", {})
    validate(args, SCHEMAS[name])
    if name == "claude_review" and args.get("scope") in ("base", "commit") and "ref" not in args:
        raise InvalidArguments("arguments.ref is required when scope is base or commit")
    try:
        default_cwd = server.root if name in ("claude_status", "claude_result") else server.cwd
        cwd = str(confined(Path(args.get("cwd", default_cwd)).resolve(strict=True),
                          server.root, server.allow_any_cwd, "arguments.cwd"))
        if not Path(cwd).is_dir():
            raise ValueError("not a directory")
    except (OSError, ValueError, RuntimeError) as exc:
        raise InvalidArguments(f"arguments.cwd must resolve to an existing directory: {exc}") from exc
    seats = ["--seats", args["seats"]] if "seats" in args else []
    payload = args.get("prompt", "")
    job_dir = None
    if name == "claude_ask":
        files = []
        for path in args.get("files", []):
            try:
                resolved = confined((Path(cwd) / path).resolve(), server.root,
                                    server.allow_any_cwd, "arguments.files")
            except (OSError, ValueError, RuntimeError) as exc:
                raise InvalidArguments(f"arguments.files cannot be resolved: {exc}") from exc
            files.extend(["--file", str(resolved)])
        argv = ["ask", *seats, *files]
    elif name == "claude_review":
        extra = []
        scope = args.get("scope", "uncommitted")
        if scope in ("base", "commit"):
            extra.extend(["--" + scope, args["ref"]])
        payload = json.dumps({key: args.get(key, "") for key in ("intent", "focus")})
        argv = ["review", *seats, *extra, "--intent-stdin"]
    elif name == "claude_task":
        argv = ["task", *seats, *(["--write"] if args.get("write", False) else [])]
    else:
        with server.lock:
            launched = dict(server.launched_jobs) if name in ("claude_status", "claude_result") else None
        job_dir = resolve_job(args["job"], server.jobs_root, server.root, server.allow_any_cwd, launched)
        if name == "claude_reply":
            seat = ["--seat", args["seat"]] if "seat" in args else []
            argv = ["reply", job_dir.name, *seat]
        else:
            argv = [name.removeprefix("claude_"), job_dir.name]
    cwd_fd = None
    try:
        # Retain the directory itself until launch, not just a checked pathname.
        cwd_fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)
        resolved = confined(Path(os.path.realpath(cwd)), server.root,
                            server.allow_any_cwd, "arguments.cwd")
        opened, current = os.fstat(cwd_fd), resolved.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("directory changed during validation")
    except (OSError, ValueError, RuntimeError) as exc:
        if cwd_fd is not None:
            os.close(cwd_fd)
        raise InvalidArguments(f"arguments.cwd cannot be opened safely: {exc}") from exc
    return name, args, cwd, argv, payload, job_dir, cwd_fd


def tool_result(stdout, stderr, exit_code, started, status=None, prefix="", job=None):
    status = status or ("ok" if exit_code == 0 else "partial" if exit_code == 3 else "failed")
    lines = stdout.rstrip().splitlines()
    footer = None
    for _line in reversed(lines):  # the engine's footer has several "— " lines; the job line is the first of them
        footer = re.fullmatch(r"— job: (" + JOB_PATTERN + r")(?: · .*)?", _line.strip())
        if footer:
            break
    if status == "partial":
        prefix = "PARTIAL: some seats failed\n" + prefix
    failed = status not in ("ok", "partial", "started")
    suffix = "\n\nstderr:\n" + stderr[-2000:] if failed and stderr else ""
    body = stdout.strip()
    budget = max(0, TEXT_LIMIT - len(prefix) - len(suffix))
    if len(body) > budget:
        marker = "\n[output truncated]"
        body = body[:max(0, budget - len(marker))] + marker
    text = (prefix + body + suffix)[:TEXT_LIMIT]
    return {"content": [{"type": "text", "text": text}], "isError": failed,
            "structuredContent": {"exit_code": exit_code, "job": job or (footer.group(1) if footer else None),
                                  "elapsed_seconds": round(time.monotonic() - started, 3), "status": status}}


def terminate_group(process, on_exit=None):
    """Give the whole group five seconds to exit, then kill and reap the child."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Sandboxed macOS can report EPERM while the group is disappearing.
        # Popen checks/reaps the owned child before attempting a direct signal.
        process.terminate()
        if on_exit is not None and process.returncode is not None:
            on_exit()
    deadline = time.monotonic() + TERMINATE_GRACE
    while True:
        # Observe reaping before waiting for a descendant's termination grace.
        if process.poll() is not None and on_exit is not None:
            on_exit()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            # A denied existence probe is inconclusive; keep reaping/retrying
            # instead of abandoning the child during its exit transition.
            pass
        if time.monotonic() >= deadline:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
                if on_exit is not None and process.returncode is not None:
                    on_exit()
            break
        time.sleep(0.05)
    process.wait()
    if on_exit is not None:
        on_exit()


class Capture:
    """Read a child's spool without sharing its seek offset or buffering all output."""
    def __init__(self, stream, on_line):
        self.stream, self.on_line = stream, on_line
        self.offset = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.head = self.tail = self.line = ""
        self.finished = False

    def read(self, final=False):
        # Bound each poll even if the engine continuously writes diagnostics.
        end = os.fstat(self.stream.fileno()).st_size
        while self.offset < end:
            chunk = os.pread(self.stream.fileno(), min(65536, end - self.offset), self.offset)
            if not chunk:
                break
            self.offset += len(chunk)
            self.consume(self.decoder.decode(chunk))
        if final and not self.finished:
            self.consume(self.decoder.decode(b"", final=True))
            if self.line:
                self.on_line(self.line)
            self.finished = True

    def consume(self, text):
        self.head = (self.head + text)[:TEXT_LIMIT + 1]
        self.tail = (self.tail + text)[-4000:]
        lines = (self.line + text).split("\n")
        for line in lines[:-1]:
            self.on_line(line.rstrip("\r"))
        self.line = lines[-1][-16384:]


def process_start(pid, env=None):
    """Return a process's creation identity, or fail closed if it cannot be read."""
    try:
        live = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=2, env=env)
        if live.returncode == 0:
            return live.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def leaderless_group_owned(pgid, recorded_start, env=None, engine_exit_observed=None):
    """Bound every member's start by seat start - 1s and observed engine exit + 1s."""
    try:
        if type(engine_exit_observed) not in (int, float) or not math.isfinite(engine_exit_observed):
            return False
        lower = datetime.strptime(recorded_start.strip(), "%a %b %d %H:%M:%S %Y").timestamp() - 1
        upper = engine_exit_observed + 1
        live = subprocess.run(["ps", "-axo", "pid=,pgid=,lstart="],
                              capture_output=True, text=True, timeout=2, env=env)
        if live.returncode != 0:
            return False
        members = 0
        for line in live.stdout.splitlines():
            if not line.strip():
                continue
            pid_text, group_text, started = line.split(None, 2)
            pid, group = int(pid_text), int(group_text)
            if pid == pgid:
                return False  # The leader PID has reappeared, possibly in another group.
            if group == pgid:
                member_start = datetime.strptime(started.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
                if pid <= 1 or not lower <= member_start <= upper:
                    return False
                members += 1
        return members > 0
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return False


def kill_seats(job_dir, supervisor_pid, supervisor_start, env=None, nonterminal_only=False,
               engine_exit_observed=None):
    """Escalate only this invocation's separately sessioned, verified seats."""
    if (job_dir is None or type(supervisor_pid) is not int or supervisor_pid <= 1 or
            not isinstance(supervisor_start, str) or not supervisor_start.strip()):
        return
    for path in job_dir.glob("seat-*.json"):
        try:
            seat = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(seat, dict):
                continue
            if nonterminal_only and seat.get("state") in ("done", "partial", "failed", "cancelled"):
                continue
            owner, owner_start = seat.get("supervisor_pid"), seat.get("supervisor_start")
            if (type(owner) is not int or owner != supervisor_pid or
                    not isinstance(owner_start, str) or owner_start.strip() != supervisor_start.strip()):
                continue
            pid, recorded = seat.get("pid"), seat.get("pid_start")
            if type(pid) is not int or pid <= 1 or not isinstance(recorded, str) or not recorded.strip():
                continue
            # Seats own their groups; never signal a shared group based on a stale file.
            pgid = seat.get("pgid")
            if "pgid" in seat and (type(pgid) is not int or pgid != pid):
                continue
            try:
                group = os.getpgid(pid)
            except ProcessLookupError:
                # Only a missing leader permits this fallback. An inaccessible
                # or mismatched live PID must never authorize its old group.
                if pgid is None:
                    continue
                if not leaderless_group_owned(pgid, recorded, env, engine_exit_observed):
                    continue
                os.killpg(pgid, 0)
                try:
                    os.getpgid(pid)
                except ProcessLookupError:
                    os.killpg(pgid, signal.SIGKILL)
            else:
                if group == pid and process_start(pid, env) == recorded.strip():
                    os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue  # Missing, malformed, stale or unverifiable records fail closed.


class Call:
    def __init__(self, request_id, name, args, cwd, argv, payload, job_dir, cwd_fd):
        self.request_id = request_id
        self.name, self.args, self.cwd, self.argv = name, args, cwd, argv
        self.payload = payload
        self.cwd_fd = cwd_fd
        self.job_dir = job_dir
        self.job = job_dir.name if job_dir else None
        self.detached = False
        self.handoff_pending = False
        self.started = time.monotonic()
        self.cancelled = threading.Event()
        self.thread = None

    def close_cwd(self):
        if self.cwd_fd is not None:
            os.close(self.cwd_fd)
            self.cwd_fd = None


class ServerStopped(BaseException):
    pass


class Server:
    def __init__(self):
        self.env = dict(os.environ)
        self.env.setdefault("BRIDGE_CALLER", "OpenAI Codex")
        # the server's built-in seat lists travel in their own variables and never as the user-facing BRIDGE_DEFAULT_SEAT /
        # BRIDGE_REVIEW_SEATS / BRIDGE_COUNCIL_SEATS (S-DEF): the engine uses each where the user's variable is unset, names a
        # variable in a refusal only when the user set it, and drops these from every seat CLI's environment
        self.env.update(BUILTIN_SEATS)
        self.allow_write = self.env.get("CLAUDE_BRIDGE_ALLOW_WRITE") == "1"
        self.cwd = Path.cwd().resolve()
        self.root = Path(self.env.get("CLAUDE_BRIDGE_ROOT") or self.cwd).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("CLAUDE_BRIDGE_ROOT must be a directory")
        self.allow_any_cwd = self.env.get("CLAUDE_BRIDGE_ALLOW_ANY_CWD") == "1"
        if self.allow_any_cwd:
            # the operator disabled confinement for this server: the engine must not re-impose it
            self.env.pop("BRIDGE_ROOT", None)
        else:
            self.env["BRIDGE_ROOT"] = str(self.root)
        cache = Path(self.env.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.jobs_root = Path(self.env.get("BRIDGE_JOBS") or cache / "codex-claude-bridge" / "jobs").resolve()
        self.env["BRIDGE_JOBS"] = str(self.jobs_root)
        self.deadline = float(self.env.get("CLAUDE_BRIDGE_TOOL_DEADLINE", "3300"))
        if not math.isfinite(self.deadline) or self.deadline <= 0:
            raise ValueError("CLAUDE_BRIDGE_TOOL_DEADLINE must be a positive finite number")
        self.lock = threading.RLock()
        self.launched_jobs = {}  # job id -> authorized cwd, for polling during initialization.
        self.calls = {}  # Includes accepted calls whose worker has not launched yet.
        self.processes = {}  # request id -> Popen, protected by self.lock.
        self.stopped = threading.Event()
        self.responses = queue.Queue()
        self.output_closing = threading.Event()
        self.output_deadline = None
        self.output_stream = sys.stdout
        self.output_fd = self.output_stream.fileno()
        os.set_blocking(self.output_fd, False)
        self.output_thread = threading.Thread(target=self.write_responses, name="mcp-output")
        self.output_thread.start()

    def send(self, obj, call=None, handoff=False):
        payload = (json.dumps(obj, allow_nan=False) + "\n").encode("utf-8")
        self.responses.put((payload, call, handoff))

    def forget_call(self, call):
        """Remove only this invocation; its id may subsequently be reused."""
        if self.calls.get(call.request_id) is call:
            self.calls.pop(call.request_id)

    def default_seats(self, tool):
        """The seat list a tool runs without `seats`, as the engine resolves it (S-DEF): the user's BRIDGE_REVIEW_SEATS (claude_review)
        or BRIDGE_DEFAULT_SEAT when set, else the server's built-in list."""
        var = "BRIDGE_REVIEW_SEATS" if tool == "claude_review" else "BRIDGE_DEFAULT_SEAT"
        return self.env[var] if var in self.env else self.env[var.replace("BRIDGE_", "BRIDGE_BUILTIN_", 1)]

    def write_response(self, payload, call, handoff=False):
        offset = 0
        with _out:
            while offset < len(payload):
                if self.output_closing.is_set() and time.monotonic() >= self.output_deadline:
                    return False
                try:
                    if call is not None:
                        # Serialize every nonblocking write and the final handoff
                        # with cancellation and shutdown, including partial writes.
                        with self.lock:
                            if handoff and time.monotonic() - call.started >= self.deadline:
                                call.handoff_pending = False
                            if (call.cancelled.is_set() or self.stopped.is_set() or
                                    (handoff and not call.handoff_pending)):
                                if offset:
                                    # A cancelled partial frame cannot be followed
                                    # by another JSON line on this transport.
                                    self.stopped.set()
                                    return False
                                if not handoff:
                                    self.forget_call(call)
                                return True
                            written = os.write(self.output_fd, payload[offset:])
                            offset += written
                            if offset == len(payload):
                                if handoff:
                                    call.detached = True
                                    call.handoff_pending = False
                                self.forget_call(call)
                    else:
                        written = os.write(self.output_fd, payload[offset:])
                        offset += written
                except BlockingIOError:
                    select.select([], [self.output_fd], [], 0.05)
        return True

    def write_responses(self):
        try:
            while True:
                item = self.responses.get()
                if item is None or not self.write_response(*item):
                    return
        except OSError as exc:
            with self.lock:
                self.stopped.set()
            log(f"transport failed: {exc}")
        finally:
            if self.stopped.is_set():
                # In particular, an abandoned partial frame must expose EOF;
                # leaving stdout open would strand the client's JSON reader.
                with contextlib.suppress(OSError):
                    self.output_stream.close()

    def error(self, msg, code, message):
        if (isinstance(msg, dict) and "id" not in msg and msg.get("jsonrpc") == "2.0" and
                isinstance(msg.get("method"), str) and isinstance(msg.get("params", {}), dict)):
            return
        request_id = msg.get("id") if isinstance(msg, dict) else None
        if not self.valid_id(request_id):
            request_id = None
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    @staticmethod
    def valid_id(value):
        return type(value) in (str, int)

    def handle(self, msg):
        if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or
                not isinstance(msg.get("method"), str) or
                ("params" in msg and not isinstance(msg["params"], dict)) or
                ("id" in msg and not self.valid_id(msg["id"]))):
            self.error(msg, -32600, "Invalid Request: expected jsonrpc 2.0, method string, object params and a string or integer id")
            return
        method, params = msg["method"], msg.get("params", {})
        if method == "notifications/cancelled":
            target = params.get("requestId")
            if "requestId" in params and self.valid_id(target):
                with self.lock:
                    call = self.calls.get(target)
                    if call:
                        call.cancelled.set()
        if "id" not in msg:
            return
        mid = msg["id"]
        if method == "initialize":
            version = params.get("protocolVersion", NEWEST_PROTOCOL_VERSION)
            if not isinstance(version, str):
                self.error(msg, -32602, "params.protocolVersion must be a string")
                return
            result = {"protocolVersion": version if version in PROTOCOL_VERSIONS else NEWEST_PROTOCOL_VERSION,
                      "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}
        elif method in ("ping", "notifications/initialized", "notifications/cancelled"):
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                prepared = prepare_call(params, self)
            except InvalidArguments as exc:
                self.error(msg, -32602, str(exc))
                log(json.dumps({"tool": params.get("name"), "request_id": mid, "cwd": None,
                                "seats": None, "exit_code": None, "elapsed_seconds": 0,
                                "status": "invalid", "error": str(exc)}))
                return
            call = Call(mid, *prepared)
            with self.lock:
                if mid in self.calls:
                    call.close_cwd()
                    self.error(msg, -32600, "request id is already in use")
                    return
                self.calls[mid] = call
                call.thread = threading.Thread(target=self.run_call, args=(call,))
                try:
                    call.thread.start()
                except BaseException:
                    self.forget_call(call)
                    call.close_cwd()
                    raise
            return
        else:
            self.error(msg, -32601, f"method not found: {method}")
            return
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    def run_engine(self, call):
        process = None
        supervisor_start = None
        engine_exit_observed = None
        handoff_result = None
        seats_cleaned = False
        captures = []
        resources = contextlib.ExitStack()

        def job_line(line):
            match = re.fullmatch(r"\[bridge\] job (" + JOB_PATTERN + r") (.+)", line)
            if match:
                directory = Path(match.group(2))
                expected = self.jobs_root / match.group(1)
                if directory.is_absolute() and directory == expected and directory.resolve() == expected:
                    call.job, call.job_dir = match.group(1), directory
                    with self.lock:
                        self.launched_jobs[call.job] = call.cwd

        def footer_line(line):
            match = re.fullmatch(r"— job: (" + JOB_PATTERN + r")(?: · .*)?", line.strip())
            if match and call.job is None:
                call.job = match.group(1)

        def read_output(final=False):
            for capture in captures:
                capture.read(final)

        def record_engine_exit():
            nonlocal engine_exit_observed
            if engine_exit_observed is None:
                engine_exit_observed = time.time()

        def poll_engine():
            code = process.poll()
            if code is not None:
                record_engine_exit()
            return code

        def terminate():
            terminate_group(process, on_exit=record_engine_exit)
            # Include job/seat publication that raced with cancellation.
            read_output(final=True)
            if call.name not in ("claude_status", "claude_result"):
                kill_seats(call.job_dir, process.pid, supervisor_start, self.env,
                           engine_exit_observed=engine_exit_observed)

        def clean_exited_seats():
            nonlocal seats_cleaned
            if not seats_cleaned:
                # A crashed supervisor cannot clean up its independent groups.
                read_output(final=True)
                kill_seats(call.job_dir, process.pid, supervisor_start, self.env, nonterminal_only=True,
                           engine_exit_observed=engine_exit_observed)
                seats_cleaned = True

        try:
            # Anonymous private regular files stay open in a detached child even
            # after these parent handles close. No pipe or server lifetime can
            # block or break the engine's later output; results live in its job.
            stdin, stdout, stderr = [resources.enter_context(tempfile.TemporaryFile()) for _ in range(3)]
            stdin.write(call.payload.encode("utf-8"))
            stdin.seek(0)
            out, err = Capture(stdout, footer_line), Capture(stderr, job_line)
            captures = [out, err]
            with self.lock:
                if call.cancelled.is_set() or self.stopped.is_set():
                    return tool_result("", "", None, call.started, "cancelled")
                cwd_fd = call.cwd_fd
                try:
                    # Native child setup enters the pinned inode without Python
                    # after fork; the engine authorizes its inherited real cwd.
                    command = [sys.executable, str(ENGINE), *call.argv]
                    launch = dict(env=self.env, stdin=stdin, stdout=stdout, stderr=stderr,
                                  pass_fds=(cwd_fd,), start_new_session=True)
                    pinned_cwd = f"/dev/fd/{cwd_fd}"
                    try:
                        process = subprocess.Popen(command, cwd=pinned_cwd, **launch)
                    except NotADirectoryError as exc:
                        if exc.filename != pinned_cwd:
                            raise
                        # macOS /dev/fd entries cannot be used with chdir. Run
                        # fchdir only AFTER exec in a fresh interpreter, then
                        # exec the engine, preserving its PID/start identity.
                        bootstrap = ("import os,sys; fd=int(sys.argv[1]); os.fchdir(fd); os.close(fd); "
                                     "os.execv(sys.executable, [sys.executable, *sys.argv[2:]])")
                        process = subprocess.Popen([sys.executable, "-I", "-S", "-c", bootstrap, str(cwd_fd),
                                                    *command[1:]], **launch)
                finally:
                    call.close_cwd()
                self.processes[call.request_id] = process
            supervisor_start = process_start(process.pid, self.env)
            status = None
            while True:
                read_output()
                remaining = self.deadline - (time.monotonic() - call.started)
                with self.lock:
                    # A delivered handoff remains detached even if shutdown
                    # immediately follows the writer's commit.
                    if call.detached:
                        threading.Thread(target=process.wait, daemon=True).start()
                        return handoff_result
                    finished = poll_engine() is not None
                    if call.cancelled.is_set() or self.stopped.is_set():
                        status = "cancelled"
                    elif remaining <= 0:
                        status = "timeout"
                    if status:
                        call.handoff_pending = False
                if finished:
                    clean_exited_seats()
                if status:
                    terminate()
                    break
                if (handoff_result is None and call.args.get("wait", True) is False and
                        call.job_dir is not None):
                    with self.lock:
                        if call.cancelled.is_set() or self.stopped.is_set():
                            continue
                        handoff_result = tool_result("", "", None, call.started, "started",
                                                     f"Started job {call.job}. Poll claude_status, then collect with claude_result.",
                                                     job=call.job)
                        call.handoff_pending = True
                        self.send({"jsonrpc": "2.0", "id": call.request_id, "result": handoff_result},
                                  call, handoff=True)
                if finished and handoff_result is None:
                    break
                call.cancelled.wait(min(0.05, remaining))
            prefix = f"claude-bridge tool timeout after {self.deadline:g} seconds.\n" if status == "timeout" else ""
            if not status and process.returncode not in (0, 3):
                prefix = f"Engine failed (exit code {process.returncode}).\n"
                if call.name == "claude_result":
                    if process.returncode == 2:
                        status = "invalid"
                        prefix = "Invalid result request (exit code 2).\n"
                    elif process.returncode == 1:
                        state = "failed"
                        try:
                            meta = json.loads((call.job_dir / "meta.json").read_text(encoding="utf-8"))
                            if isinstance(meta, dict) and meta.get("state") in ("failed", "cancelled"):
                                state = meta["state"]
                        except (OSError, ValueError):
                            pass
                        prefix = f"Job {call.job} {state} (exit code 1).\n"
            return tool_result(out.head, err.tail, process.returncode, call.started, status, prefix, job=call.job)
        finally:
            call.close_cwd()
            with self.lock:
                call.handoff_pending = False
            if process is not None:
                if not call.detached:
                    if poll_engine() is None:
                        terminate()
                    process.wait()
                    record_engine_exit()
                    clean_exited_seats()
                with self.lock:
                    if self.processes.get(call.request_id) is process:
                        self.processes.pop(call.request_id, None)
            resources.close()

    def run_call(self, call):
        queued = False
        try:
            try:
                if call.name == "claude_task" and call.args.get("write", False) and not self.allow_write:
                    result = tool_result("", "", None, call.started, "failed",
                                         "Write tasks are disabled. Set CLAUDE_BRIDGE_ALLOW_WRITE=1 in the server environment and restart to enable them.")
                else:
                    result = self.run_engine(call)
            except Exception as exc:
                log(f"engine error for request {call.request_id!r}: {exc}")
                result = tool_result("", str(exc), None, call.started, "failed", "claude-bridge engine error.\n")
            with self.lock:
                cancelled = not call.detached and (call.cancelled.is_set() or self.stopped.is_set())
                if call.detached:
                    queued = True  # The output writer already delivered the handoff.
                elif not cancelled:
                    self.send({"jsonrpc": "2.0", "id": call.request_id, "result": result}, call)
                    queued = True
            details = dict(result["structuredContent"])
            if cancelled:
                details["status"] = "cancelled"
            details.update(tool=call.name, request_id=call.request_id, cwd=call.cwd,
                           seats=call.args.get("seats", call.args.get("seat", self.default_seats(call.name))))
            log(json.dumps(details))
        except (BrokenPipeError, OSError) as exc:
            with self.lock:
                self.stopped.set()
            log(f"transport failed: {exc}")
        finally:
            call.close_cwd()
            if not queued:
                with self.lock:
                    self.forget_call(call)

    def shutdown(self):
        with self.lock:
            self.stopped.set()
            calls = list(self.calls.values())
            for call in calls:
                call.cancelled.set()
        for call in calls:
            if call.thread and call.thread.ident is not None:
                call.thread.join()
        # Preserve short queued protocol responses on EOF, but a host that no
        # longer drains stdout must not keep the process alive indefinitely.
        self.output_deadline = time.monotonic() + OUTPUT_FLUSH_GRACE
        self.output_closing.set()
        self.responses.put(None)
        self.output_thread.join()
        with self.lock:
            self.calls.clear()


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def input_lines(stream, stopped):
    """Read complete lines while still noticing an output-side transport failure."""
    pending = bytearray()
    scanned = 0
    fd = stream.fileno()
    while not stopped.is_set():
        if not select.select([fd], [], [], 0.05)[0]:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            if pending:
                yield pending.decode(stream.encoding or "utf-8", errors=stream.errors or "strict")
            return
        pending.extend(chunk)
        while not stopped.is_set():
            end = pending.find(b"\n", scanned)
            if end < 0:
                scanned = len(pending)
                break
            line = pending[:end + 1]
            del pending[:end + 1]
            scanned = 0
            yield line.decode(stream.encoding or "utf-8", errors=stream.errors or "strict")


def main():
    try:
        server = Server()
    except (OSError, ValueError, RuntimeError) as exc:
        log(f"invalid server configuration: {exc}")
        return 2

    def stop(signum, frame):
        raise ServerStopped()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    log(f"starting; engine={ENGINE}")
    try:
        for line in input_lines(sys.stdin, server.stopped):
            try:
                msg = json.loads(line, parse_constant=reject_constant)
            except (ValueError, RecursionError):
                server.error(None, -32700, "Parse error: invalid JSON")
                continue
            server.handle(msg)
    except (ServerStopped, BrokenPipeError):
        pass
    finally:
        # Further termination signals must not interrupt reaping or the grace period.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        server.shutdown()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())

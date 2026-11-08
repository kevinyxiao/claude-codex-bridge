#!/usr/bin/env python3
"""Stand-in for the `codex` CLI used by the engine tests. Behaviour is steered by [[STUB:...]] directives in the prompt.

Directives (comma separated inside one marker, e.g. [[STUB:sleep=30,argv=/tmp/a.json]]):
  sleep=N        sleep N seconds printing nothing (watchdog tests)
  hang           sleep 600 seconds (cancel tests)
  orphanfile=PATH  spawn `sleep 600` in its own session (like a sandboxed command), write its pid to PATH, then hang
  fail           write "boom" to stderr and exit 1
  limit=PATH     first call (PATH absent): create PATH, print "usage limit reached" to stderr, exit 1; later calls succeed
  alwayslimit    every call: print "usage limit reached" to stderr, exit 1
  limitafterwork=PATH  first call: emit a started command item, then a turn.failed usage-limit event, exit 1; later calls succeed
  limitseq=PATH  call 1: as limitafterwork (a thread starts, work begins, then the limit); call 2: a stderr-only limit with NO
                 thread; later calls succeed. PATH counts the calls.
  limitthenfail  a recovered 429 message followed by an unrelated terminal failure (must not enter the ladder)
  badjson        finish successfully with prose instead of the requested JSON
  recoveredthenlimit=PATH  first call: a recovered structured error, then a stderr-only usage limit with exit 1; later calls succeed
  badmodelafterthread=A+B  (checked before badeffort/badmodel) if -m is one of A, B, ...: the thread starts (thread.started,
                 the same id a normal run would name), then the turn fails with "model A is not supported" before any work,
                 exit 1: a startup rejection whose attempt nevertheless started or continued the thread
  badmodel       if -m is the Astra head of the shipped table: stderr "model <id> is not supported", exit 1; otherwise succeed
  badmodel=A+B   the same for every model listed (A, B, ...): the account does not offer them
  badmodelon=L+M the rejection of `badmodel` (the requested model is not supported) only when the stub relay ran us under one of the
                 accounts L, M, ... (RELAY_STUB_ACCOUNT): an account that lists a model its login cannot run
  badeffort=E    if the reasoning effort (-c model_reasoning_effort="E") is E: stderr "reasoning effort 'E' is not supported", exit 1
  failafterwork  (checked after badmodel/badeffort) the thread starts and a command runs, then the turn fails with an ordinary
                 error (not a usage limit, not a throttle or network failure: "stream disconnected before completion", its old text,
                 is the codex-network class since 2026-10-13), exit 1: the conversation advanced and the attempt failed
  argv=PATH      append this invocation's argv and the prompt to a JSON-lines file (with `bridge_env`: every BRIDGE_* variable
                 in the CLI's environment, name -> value)
  findings       answer with a findings JSON object (used when --output-schema is present too)
  text=WORD      answer with WORD (default OK)
  emptyout       create the -o file but leave it empty: the answer reaches the engine only through the JSON events (B-astra-2)
  threadfirst    before any other directive acts, the thread starts (thread.started, the id a normal run names): evidence that
                 the CLI started, e.g. ahead of a stderr-only usage limit (S-LAUNCH); a normal run then does not start it again
  badevent       (a normal run) right after thread.started, a JSON line that is not an object: the engine's handling of the
                 stream raises after the CLI started (S-LAUNCH)
  hold=PATH      (a normal run) right after thread.started (and badevent's line), create the readiness file PATH.ready, then
                 wait until PATH.release exists (at most 120 s) before the rest of the stream: an attempt blocked after its
                 first event (S-LAUNCH)
  throttle=KIND  the thread starts, a command runs, then the turn fails with the Oct. 12 text of KIND: 429 (exceeded retry limit,
                 last status 429), 503, demand (high demand), capacity (model at capacity), refused (connection refused), stream
                 (stream disconnected); exit 1. KIND 403ws instead emits recoverable `error` events (WebSocket 403s, the post-wake
                 storm) and falls through to the next directive (with `hang`: a run the watchdog kills while still reconnecting)
                 (STUB_CODEX_THROTTLE=KIND in the environment: the same on every call, whatever the prompt says)
  throttleonce=PATH  with throttle=KIND: only the first call (PATH absent, then created) is throttled; later calls succeed
  throttlestderr the Oct. 12 429 line on stderr only (no event), exit 1: evidence the engine must not read as a usage limit
  rolloutmodel=ID  the rollout file's turn_context names ID instead of the requested model (what a substitution looks like in
                 the record the bridge's provenance reads; docs/model-routing.md 6); `rolloutmodel=none` writes the
                 turn_context without any model (a collection error: the window holds no readable observation)
The account the stub relay ran us under (RELAY_STUB_ACCOUNT, None when unrouted) is written into the argv record and into
the thread.started event, so a test can prove which account a seat ran under.

The rollout file (docs/model-routing.md 6): like the real CLI, every run that starts a thread writes
CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread_id>.jsonl (a new thread: a new file, session_meta first; a resumed
thread: appended to that thread's latest file) with one turn_context per turn.started, carrying the model and the effort of the
run. A run rejected before any thread writes nothing.

The bundled catalog (docs/model-routing.md 2.6): this file embeds, verbatim, the `{"models": [...]}` succession object the native
binary of CLI 0.156.1 carries (BUNDLED_CATALOG below), so the model registry's bundled-catalog reader extracts the same edges
from the stub as from the CLI and a seat's first catalog read promotes the registry's floors as it does on a real Mac.

Environment (DESIGN §8):
  STUB_CODEX_VERSION   what `--version` prints (default "codex-cli 99.0.0": above every floor)
  STUB_CODEX_NAME      a label written into every record (tells several stub executables on PATH apart)
  STUB_CODEX_RECORD    JSON-lines file: EVERY invocation is recorded ({name, argv, cwd, mode, ...}); app-server requests are
                       recorded one line each ({mode: "app-server", method, params, cwd, codex_home, client}: `client` is the
                       clientInfo name of the session's initialize, "claude-codex-bridge" for the check, "model-registry" for
                       the registry's catalog reads)
  STUB_CODEX_CATALOG   `app-server` model/list answers from this file ({"models": [...]} or a list; default: the tests' clean
                       catalog, the newest real capture in fixtures/evidence completed for the shipped table (catalog_fixture.py);
                       the engine tests pass it explicitly as CATALOG_CURRENT, the bootstrap tests' installed engine relies on
                       this default), in pages of 3 with nextCursor; hidden entries only with includeHidden
  STUB_CODEX_CATALOG_BY_HOME=1  the app-server answers model/list from $CODEX_HOME/stub_catalog.json when that file exists (one
                       catalog per account home; the routing tests give accounts different catalogs), else as above
  STUB_CODEX_CATALOG_PROVIDER  model/list answers from this file whenever OPENAI_BASE_URL is in the app-server's environment: an
                       ambient provider override answers with its own catalog (docs/model-routing.md 3.3: a discovery under it
                       validates a different login than a launch without it)
  STUB_CODEX_CONFIG    `app-server` config/read answers {"config", "origins"} from this file, but ONLY when the request carries
                       `cwd` (the project-aware form); the parameterless form answers a layer-free default (every key default)
  STUB_CODEX_ACCOUNT=none       account/read answers that no login is present ({"account": null}); otherwise a ChatGPT Pro login
  STUB_CODEX_APPSERVER_FAIL=1   the app-server exits at once with an error (the catalog cannot be read)
  STUB_CODEX_APPSERVER_SLEEP=N  the app-server sleeps N seconds before answering model/list (timeout tests)
  STUB_CODEX_RPC_ERROR=M[,M]    answer each listed method (config/read, model/list, ...) with a JSON-RPC error whose `message`
                                and `data.detail` are STUB_CODEX_RPC_ERROR_TEXT and whose `code` is STUB_CODEX_RPC_ERROR_CODE
                                (default -32603): server text that may carry a configuration value (Astra C3-2)
  STUB_CODEX_CONFIG_RESULT=JSON every config/read answers exactly this `result` (e.g. null, or an object without `config`)
  STUB_CODEX_MODEL_LIST_RESULT=JSON  every model/list answers exactly this `result` (e.g. null, or an object without `data`)
  STUB_CODEX_APPSERVER_FRAMES=together   every response is preceded by a notification, both frames in ONE write (Astra C3-7)
  STUB_CODEX_APPSERVER_FRAMES=partial:N  the initialize answer stops halfway through its frame (no newline); the app-server then
                                sleeps N seconds with its stdout open and exits (Astra C3-7)
  STUB_CODEX_APPSERVER_IGNORE_TERM=1  the app-server ignores SIGTERM, so only SIGKILL ends it (Astra C3b-2: a helper that does
                                not end when asked; with STUB_CODEX_APPSERVER_SLEEP it is asleep when the engine gives up on it)
  STUB_CODEX_PROBE_SLEEP=P:N    the probe P (`version`: `--version`; `help`: `exec --help`, the capability probe) records itself,
                                then sleeps N seconds before it answers (Astra C3b-2)
Every record carries the recording process's `pid`, so a test can end an app-server its engine left behind.
Like the real CLI (DESIGN §5, model-routing.md:760), `app-server` rejects `--ignore-user-config`: it exits 2 with a usage
error and serves nothing (the rejected invocation is recorded with `rejected`).
"""
import glob
import json
import os
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT = os.environ.get("RELAY_STUB_ACCOUNT") or None
EVIDENCE = Path(__file__).resolve().parent / "evidence"
DEFAULT_CATALOG = None  # the tests' clean catalog (catalog_fixture.py), built when no STUB_CODEX_CATALOG names a file
MODELS_JSON = Path(__file__).resolve().parents[2] / "models.json"  # the shipped table: the default rejected model is its Astra head
CATALOG_KEYS = ("model_provider", "model_providers", "model_catalog_json", "profile", "profiles", "openai_base_url",
                "chatgpt_base_url", "oss_provider", "service_tier")
HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
_ROLLOUT = {"path": None}
# The succession edges the native binary of CLI 0.156.1 embeds as its bundled catalog (docs/model-routing.md 2.6, verified
# 2026-09-24), kept here verbatim so the model registry's bundled-catalog reader finds them in this file exactly as it finds them
# in the CLI: a Codex seat's first catalog read then promotes the registry's Sol and Luna floors to the table's heads (the Terra
# edge crosses families and promotes nothing). The stub never reads this itself.
BUNDLED_CATALOG = '{"models": [{"slug": "gpt-5.6-sol", "upgrade": {"model": "gpt-6-sol"}}, {"slug": "gpt-5.6-luna", "upgrade": {"model": "gpt-6-luna"}}, {"slug": "gpt-5.5", "upgrade": {"model": "gpt-6-sol"}}, {"slug": "gpt-5.4", "upgrade": {"model": "gpt-6-sol"}}, {"slug": "gpt-5.6-terra", "upgrade": {"model": "gpt-6-sol"}}]}'  # model-lint: allow the CLI's bundled succession edges, real ids by design


def record(obj):
    path = os.environ.get("STUB_CODEX_RECORD")
    if not path:
        return
    obj = dict({"name": os.environ.get("STUB_CODEX_NAME"), "cwd": os.getcwd(), "codex_home": os.environ.get("CODEX_HOME"),
                "home": os.environ.get("HOME"), "t": time.time(), "pid": os.getpid()}, **obj)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def _load(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k != "_provenance"}
    return data


def astra_head():
    """The Astra head of the shipped table (the default model `badmodel` rejects)."""
    try:
        t = json.loads(MODELS_JSON.read_text())
        return t["families"]["codex"]["astra"]["known_good"]
    except Exception:
        return "no-such-head"


def probe_sleep(probe):
    """STUB_CODEX_PROBE_SLEEP=P:N: the probe named P sleeps N seconds before it answers."""
    name, _, seconds = (os.environ.get("STUB_CODEX_PROBE_SLEEP") or "").partition(":")
    if name == probe and seconds:
        time.sleep(float(seconds))


def version_number():
    text = os.environ.get("STUB_CODEX_VERSION", "codex-cli 99.0.0")
    return text.split()[-1] if text.split() else text


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# The Oct. 12 texts (de-identified), one per throttle kind: what a throttled, overloaded or unreachable ChatGPT looked like to the CLI.
THROTTLE_TEXTS = {
    "429": "exceeded retry limit, last status: 429 Too Many Requests",
    "503": "exceeded retry limit, last status: 503 Service Unavailable",
    "demand": "We're currently experiencing high demand, which may cause temporary errors.",
    "capacity": "Selected model is at capacity. Please try again later or choose a different model.",
    "refused": "error sending request for url (https://chatgpt.com/backend-api/codex/responses): client error (Connect): tcp connect error: Connection refused (os error 61)",
    "stream": "stream disconnected before completion: error decoding response body",
    "403ws": "Reconnecting... 2/5 (unexpected status 403 Forbidden for wss://chatgpt.com/backend-api/codex/ws)",
}


def rollout_started(thread_id, resume):
    """Like the real CLI: a new thread gets its own rollout file under CODEX_HOME/sessions/YYYY/MM/DD (session_meta first); a
    resumed thread appends to that thread's latest file."""
    if _ROLLOUT["path"] is not None or not thread_id:
        return
    base = os.path.join(HOME, "sessions")
    path = None
    if resume:
        hits = sorted(glob.glob(os.path.join(base, "*", "*", "*", "rollout-*-%s.jsonl" % thread_id)))
        path = hits[-1] if hits else None
    if path is None:
        d = os.path.join(base, "2026", "09", "26")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "rollout-%s-%s.jsonl" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f"), thread_id))
        with open(path, "a") as fh:
            fh.write(json.dumps({"timestamp": now_iso(), "type": "session_meta",
                                 "payload": {"id": thread_id, "cli_version": version_number(), "cwd": os.getcwd(), "originator": "codex_exec",
                                             "model_context_window": 272000}}) + "\n")
    _ROLLOUT["path"] = path


def rollout_turn(model, effort):
    """One turn_context per turn.started, naming the model and effort of this run (or the `rolloutmodel` directive's model;
    `rolloutmodel=none` writes a turn_context without any model, a record the collector cannot read)."""
    if _ROLLOUT["path"] is None:
        return
    payload = {"cwd": os.getcwd(), "approval_policy": "never", "model": model, "effort": effort, "summary": "auto", "turn_id": uuid.uuid4().hex[:8]}
    if model == "none":
        payload.pop("model")
    with open(_ROLLOUT["path"], "a") as fh:
        fh.write(json.dumps({"timestamp": now_iso(), "type": "turn_context", "payload": payload}) + "\n")


def rollout_event(payload_type):
    """A turn-boundary event as the real CLI records it (`event_msg` with payload.type task_started / task_complete / turn_aborted):
    what a live-session count reads to tell a running session from a finished one."""
    if _ROLLOUT["path"] is None:
        return
    with open(_ROLLOUT["path"], "a") as fh:
        fh.write(json.dumps({"timestamp": now_iso(), "type": "event_msg", "payload": {"type": payload_type}}) + "\n")


def emit(e, rollout_model=None, effort=None, resume=None):
    """Write one JSON event and mirror it into the rollout file as the real CLI records a thread."""
    sys.stdout.write(json.dumps(e) + "\n")
    sys.stdout.flush()
    if isinstance(e, dict):
        if e.get("type") == "thread.started":
            rollout_started(e.get("thread_id"), resume)
        elif e.get("type") == "turn.started":
            rollout_turn(rollout_model, effort)
            rollout_event("task_started")
        elif e.get("type") == "turn.completed":
            rollout_event("task_complete")
        elif e.get("type") == "turn.failed":
            rollout_event("turn_aborted")


def app_server(argv):
    """A line-delimited JSON-RPC app-server: initialize, initialized, config/read, model/list (account/read answered
    without any credential). Every request is recorded."""
    if os.environ.get("STUB_CODEX_APPSERVER_IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "--ignore-user-config" in argv:  # the app-server always loads user configuration and refuses the seat flag
        record({"mode": "app-server", "argv": argv, "method": None, "params": None, "rejected": "--ignore-user-config"})
        print("error: unexpected argument '--ignore-user-config' found\n\nUsage: codex app-server [OPTIONS]", file=sys.stderr)
        return 2
    if os.environ.get("STUB_CODEX_APPSERVER_FAIL") == "1":
        record({"mode": "app-server", "argv": argv, "method": None, "params": None, "failed": True})
        print("error: stub app-server failed to start", file=sys.stderr)
        return 1
    catalog_path = os.environ.get("STUB_CODEX_CATALOG") or DEFAULT_CATALOG
    if os.environ.get("STUB_CODEX_CATALOG_BY_HOME") == "1":  # one catalog per account home, when the home carries one
        per_home = os.path.join(os.environ.get("CODEX_HOME") or "", "stub_catalog.json")
        if os.path.isfile(per_home):
            catalog_path = per_home
    if os.environ.get("STUB_CODEX_CATALOG_PROVIDER") and os.environ.get("OPENAI_BASE_URL"):  # an ambient provider answers with its own catalog
        catalog_path = os.environ["STUB_CODEX_CATALOG_PROVIDER"]
    if catalog_path:
        catalog = _load(catalog_path)
    else:  # the shipped table's clean catalog: the newest real capture with a derived entry for any head it lacks
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # beside this file, also when run isolated (-I)
        import catalog_fixture
        catalog = {"models": catalog_fixture.complete(catalog_fixture.load_models(), json.loads(MODELS_JSON.read_text()))[0]}
    models = catalog.get("models", catalog.get("data", [])) if isinstance(catalog, dict) else catalog
    default_config = {"config": {k: ({} if k in ("model_providers", "profiles") else None) for k in CATALOG_KEYS}, "origins": {}}
    frames = os.environ.get("STUB_CODEX_APPSERVER_FRAMES") or ""
    rpc_errors = [m.strip() for m in (os.environ.get("STUB_CODEX_RPC_ERROR") or "").split(",") if m.strip()]

    def send(obj):
        data = json.dumps(obj) + "\n"
        if frames == "together":  # a notification and the response in one write: both arrive in the reader's buffer at once
            data = json.dumps({"method": "stub/notice", "params": {"text": "a notification written with the response"}}) + "\n" + data
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.flush()

    client = None  # who is reading: the check ("claude-codex-bridge") or the model registry ("model-registry"), from initialize
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, params, mid = msg.get("method"), msg.get("params"), msg.get("id")
        if method == "initialize" and isinstance(params, dict) and isinstance(params.get("clientInfo"), dict):
            client = params["clientInfo"].get("name")
        record({"mode": "app-server", "argv": argv, "method": method, "params": params, "client": client,
                # the provider and credential variables the app-server sees: a routed account's discovery must run under the environment
                # relay gives the launch (none of these), this machine's login under the bridge's own (docs/model-routing.md 3.3)
                "provider_env": sorted(k for k in os.environ if k.startswith(("OPENAI_", "ANTHROPIC_")) or k == "CODEX_API_KEY")})
        if mid is None:
            continue  # a notification (initialized)
        if method in rpc_errors:
            text = os.environ.get("STUB_CODEX_RPC_ERROR_TEXT") or "stub app-server error"
            send({"id": mid, "error": {"code": int(os.environ.get("STUB_CODEX_RPC_ERROR_CODE") or -32603), "message": text,
                                       "data": {"detail": text}}})
        elif method == "initialize":
            answer = {"id": mid, "result": {"userAgent": "stub_codex/%s" % os.environ.get("STUB_CODEX_VERSION", "codex-cli 99.0.0"),
                                            "codexHome": os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
                                            "platformFamily": "unix", "platformOs": "macos"}}
            if frames.startswith("partial:"):  # half a frame, no newline; stdout stays open while the app-server sleeps
                text = json.dumps(answer)
                sys.stdout.buffer.write(text[:len(text) // 2].encode("utf-8"))
                sys.stdout.buffer.flush()
                time.sleep(float(frames.split(":", 1)[1]))
                return 0
            send(answer)
        elif method == "config/read" and os.environ.get("STUB_CODEX_CONFIG_RESULT") is not None:
            send({"id": mid, "result": json.loads(os.environ["STUB_CODEX_CONFIG_RESULT"])})
        elif method == "model/list" and os.environ.get("STUB_CODEX_MODEL_LIST_RESULT") is not None:
            send({"id": mid, "result": json.loads(os.environ["STUB_CODEX_MODEL_LIST_RESULT"])})
        elif method == "config/read":
            if isinstance(params, dict) and params.get("cwd") and os.environ.get("STUB_CODEX_CONFIG"):
                send({"id": mid, "result": _load(os.environ["STUB_CODEX_CONFIG"])})
            else:
                send({"id": mid, "result": default_config})  # the parameterless form: user layer only, no project layers
        elif method == "model/list":
            if os.environ.get("STUB_CODEX_APPSERVER_SLEEP"):
                time.sleep(float(os.environ["STUB_CODEX_APPSERVER_SLEEP"]))
            params = params if isinstance(params, dict) else {}
            visible = [m for m in models if params.get("includeHidden") or not m.get("hidden")]
            start = int(params.get("cursor") or 0)
            page = visible[start:start + 3]
            nxt = str(start + 3) if start + 3 < len(visible) else None
            send({"id": mid, "result": {"data": page, "nextCursor": nxt}})
        elif method == "account/read":
            if os.environ.get("STUB_CODEX_ACCOUNT") == "none":  # no login: the real app-server then lists its bundled catalog
                send({"id": mid, "result": {"account": None, "requiresOpenaiAuth": True}})
            else:
                send({"id": mid, "result": {"account": {"type": "chatgpt", "email": "stub@example.test", "planType": "pro"},
                                            "requiresOpenaiAuth": True}})
        else:
            send({"id": mid, "error": {"code": -32601, "message": "stub app-server: unknown method %s" % method}})
    return 0


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        record({"mode": "version", "argv": argv})
        probe_sleep("version")
        print(os.environ.get("STUB_CODEX_VERSION", "codex-cli 99.0.0"))
        return 0
    if argv[:2] == ["login", "status"]:
        record({"mode": "login", "argv": argv})
        print("Logged in using ChatGPT")
        return 0
    if argv[:2] == ["exec", "--help"]:
        record({"mode": "help", "argv": argv})
        probe_sleep("help")
        print("Run Codex non-interactively\n\nOptions:\n  --json\n  --output-schema <FILE>\n  -o, --output-last-message <FILE>\n  -m, --model <MODEL>\n  --ignore-user-config")
        return 0
    if "app-server" in argv and "exec" not in argv:
        return app_server(argv)
    if argv[:1] != ["exec"]:
        record({"mode": "unsupported", "argv": argv})
        print("stub: unsupported", file=sys.stderr)
        return 2
    args = argv[1:]
    out_file = schema = model = effort = None
    resume = None
    if args[:1] == ["resume"]:
        resume = args[1]
        args = args[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            out_file = args[i + 1]; i += 2; continue
        if a == "--output-schema":
            schema = args[i + 1]; i += 2; continue
        if a == "-m":
            model = args[i + 1]; i += 2; continue
        if a == "-c":
            em = re.match(r'model_reasoning_effort="?([^"]*)"?$', args[i + 1])
            if em:
                effort = em.group(1)
            i += 2; continue
        if a in ("-C", "-s"):
            i += 2; continue
        i += 1
    prompt = sys.stdin.read() if "-" in args else ""
    m = re.search(r"\[\[STUB:([^\]]*)\]\]", prompt)
    d = {}
    if m:
        for part in m.group(1).split(","):
            k, _, v = part.partition("=")
            d[k.strip()] = v.strip()
    record({"mode": "exec", "argv": argv, "model": model, "effort": effort, "resume": resume, "account": ACCOUNT})
    if "argv" in d:
        with open(d["argv"], "a") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "resume": resume, "env_has_claudecode": "CLAUDECODE" in os.environ,
                                "account": ACCOUNT, "model": model, "effort": effort, "name": os.environ.get("STUB_CODEX_NAME"),
                                "bridge_env": {k: v for k, v in os.environ.items() if k.startswith("BRIDGE_")}}) + "\n")
    if os.environ.get("STUB_CODEX_THROTTLE") and "throttle" not in d:  # every call, whatever the prompt says (a resumed turn carries no directive)
        d["throttle"] = os.environ["STUB_CODEX_THROTTLE"]
    rollout_model = d.get("rolloutmodel") or model
    thread_first = "threadfirst" in d
    if thread_first:  # the CLI's first event, before whatever the other directives make of this run
        emit({"type": "thread.started", "thread_id": "stub-thread-" + (resume or "01a0"), "account": ACCOUNT}, rollout_model, effort, resume)
    if "throttle" in d and ("throttleonce" not in d or not os.path.exists(d["throttleonce"])):
        if "throttleonce" in d:
            open(d["throttleonce"], "w").write("1")
        text = THROTTLE_TEXTS[d["throttle"]]
        head_events = [{"type": "thread.started", "thread_id": "stub-thread-" + (resume or "01a0"), "account": ACCOUNT}, {"type": "turn.started"},
                       {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "/bin/zsh -lc 'git status'", "exit_code": 0}}]
        if d["throttle"] == "403ws":  # still reconnecting: recoverable errors, then whatever the next directive does (hang: killed mid-reconnect)
            for e in head_events + [{"type": "error", "message": text}, {"type": "error", "message": text.replace("2/5", "3/5")}]:
                emit(e, rollout_model, effort, resume)
        else:
            for e in head_events + [{"type": "turn.failed", "error": {"message": text}}]:
                emit(e, rollout_model, effort, resume)
            print("ERROR: " + text, file=sys.stderr)
            return 1
    if "throttlestderr" in d:
        print("ERROR: " + THROTTLE_TEXTS["429"], file=sys.stderr)
        return 1
    if "sleep" in d:
        time.sleep(float(d["sleep"]))
    if "orphanfile" in d:
        # like Codex's sandbox: a command child in its OWN session, outside our process group; then hang
        import subprocess
        child = subprocess.Popen(["sleep", "600"], start_new_session=True)
        open(d["orphanfile"], "w").write(str(child.pid))
        time.sleep(600)
    if "hang" in d:
        time.sleep(600)
    if "fail" in d or "codexfail" in d:
        print("boom", file=sys.stderr)
        return 1
    if ("limit" in d and not os.path.exists(d["limit"])) or "alwayslimit" in d:
        if "limit" in d:
            open(d["limit"], "w").write("1")
        print("error: usage limit reached for this account", file=sys.stderr)
        return 1
    if "limitseq" in d:
        try:
            n = int(open(d["limitseq"]).read().strip() or 0)
        except OSError:
            n = 0
        open(d["limitseq"], "w").write(str(n + 1))
        if n == 0:
            d["limitafterwork"] = d["limitseq"] + ".first"  # handled just below, exactly like limitafterwork's first call
        elif n == 1:
            print("error: usage limit reached for this account", file=sys.stderr)  # a limit before any thread started
            return 1
    if "limitafterwork" in d and not os.path.exists(d["limitafterwork"]):
        # work visibly began (a command ran) before the limit hit: the engine must resume this thread, not restart it
        open(d["limitafterwork"], "w").write("1")
        for e in [{"type": "thread.started", "thread_id": "stub-thread-01a0", "account": ACCOUNT}, {"type": "turn.started"},
                  {"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "/bin/zsh -lc 'pytest -q'"}},
                  {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "/bin/zsh -lc 'pytest -q'", "exit_code": 0}},
                  {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}}]:
            emit(e, rollout_model, effort, resume)
        return 1
    if "limitthenfail" in d:
        # an early limit message that the turn recovers from (also echoed on stderr), then an unrelated terminal failure: NOT a usage-limit exit
        print("warning: 429 rate limit exceeded, retrying in 2s", file=sys.stderr)
        for e in [{"type": "thread.started", "thread_id": "stub-thread-01a0"}, {"type": "turn.started"},
                  {"type": "error", "message": "HTTP 429 rate limit exceeded; retrying"},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}},
                  {"type": "turn.started"},
                  {"type": "turn.failed", "error": {"message": "permission denied: sandbox blocked the command"}}]:
            emit(e, rollout_model, effort, resume)
        return 1
    if "recoveredthenlimit" in d and not os.path.exists(d["recoveredthenlimit"]):
        # a structured error the turn recovers from, then a usage limit reported ONLY on stderr with a non-zero exit
        open(d["recoveredthenlimit"], "w").write("1")
        for e in [{"type": "thread.started", "thread_id": "stub-thread-01a0"}, {"type": "turn.started"},
                  {"type": "error", "message": "transient stream error; reconnecting"},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}}]:
            emit(e, rollout_model, effort, resume)
        print("error: usage limit reached for this account", file=sys.stderr)
        return 1
    if "badjson" in d:
        # a finished seat whose answer is not the requested JSON
        text = "I looked at the change and found nothing structured to say."
        for e in [{"type": "thread.started", "thread_id": "stub-thread-01a0"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}]:
            emit(e, rollout_model, effort, resume)
        if out_file:
            open(out_file, "w").write(text)
        return 0
    if "badmodelafterthread" in d and model in d["badmodelafterthread"].split("+"):
        # the CLI creates (or resumes) the thread before its first model request, so the rejection follows thread.started
        for e in [{"type": "thread.started", "thread_id": "stub-thread-" + (resume or "01a0"), "account": ACCOUNT}, {"type": "turn.started"},
                  {"type": "turn.failed", "error": {"message": f"model {model} is not supported for this account"}}]:
            emit(e, rollout_model, effort, resume)
        return 1
    if "badeffort" in d and effort == d["badeffort"]:
        print(f"error: reasoning effort '{effort}' is not supported", file=sys.stderr)
        return 1
    if "badmodel" in d and model in (d["badmodel"].split("+") if d["badmodel"] else [astra_head()]):
        print(f"error: model {model} is not supported for this account", file=sys.stderr)
        return 1
    if "badmodelon" in d and ACCOUNT and ACCOUNT in d["badmodelon"].split("+"):  # this account's login cannot run the model
        print(f"error: model {model} is not supported for this account", file=sys.stderr)
        return 1
    if "failafterwork" in d:
        for e in [{"type": "thread.started", "thread_id": "stub-thread-" + (resume or "01a0"), "account": ACCOUNT}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "/bin/zsh -lc 'pytest -q'", "exit_code": 1}},
                  {"type": "turn.failed", "error": {"message": "turn failed: the model produced an invalid tool call"}}]:
            emit(e, rollout_model, effort, resume)
        return 1
    thread_id = "stub-thread-" + (resume or "01a0")
    text = d.get("text", "OK")
    if schema or "findings" in d:
        text = json.dumps({"findings": [{"priority": "P2", "title": f"Stub finding from {model or 'codex'}", "file": "store.py", "line": 1,
                                         "explanation": "stub explanation", "suggested_fix": "stub fix", "confidence": 0.9}],
                           "assessment": f"stub assessment ({model})"})
    events = [{"type": "thread.started", "thread_id": thread_id, "account": ACCOUNT}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "/bin/zsh -lc 'git status'", "exit_code": 0}},
              {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}},
              {"type": "turn.completed", "usage": {"input_tokens": 1234, "cached_input_tokens": 1000, "output_tokens": 56}}]
    if thread_first:  # the thread already started
        events = events[1:]
    for n, e in enumerate(events):
        emit(e, rollout_model, effort, resume)
        if n == 0 and "badevent" in d:  # valid JSON that is not an event object
            sys.stdout.write(json.dumps(["not", "an", "event"]) + "\n")
            sys.stdout.flush()
        if n == 0 and "hold" in d:  # the readiness file says the first event is out; the test's release file lets the run go on
            with open(d["hold"] + ".ready", "w") as f:
                f.write(str(os.getpid()))
            deadline = time.time() + 120
            while not os.path.exists(d["hold"] + ".release") and time.time() < deadline:
                time.sleep(0.02)
    if out_file:
        with open(out_file, "w") as f:
            f.write("" if "emptyout" in d else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

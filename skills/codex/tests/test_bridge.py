"""Engine tests. They run bridge.py as a subprocess against stub `codex`/`claude` CLIs (tests/fixtures), so no model is
called and no account is used. Directives inside prompts ([[STUB:...]]) steer the stubs. Account routing is exercised
against a scripted stub relay (fixtures/stub_relay.py); the suite's default RELAY_BIN names a missing file, so no test can
reach a real relay installed on the machine.

The models change (DESIGN rev 13) adds a free check that reads the local Codex catalog through the resolved codex (the
stub's `app-server` mode answers from fixtures/evidence) and three public pages. Every test points those pages at a
loopback fixture server (fixtures/page_server.py) through BRIDGE_MODELS_NPM_URL, BRIDGE_MODELS_DEPRECATIONS_URL and
BRIDGE_MODELS_OVERVIEW_URL, so the network is never reached, and runs with HOME, CODEX_HOME and XDG_CACHE_HOME under its
temporary root, the relay's Claude CLI (RELAY_CLAUDE_BIN) pinned to the stub, and every model-override variable removed.
BRIDGE_CODEX_FIXED_ROOT is an empty directory under the temporary root (TEST_RULINGS F-E6: the default fixed directories
/opt/homebrew/bin and /usr/local/bin are looked up beneath it, so no host codex is found), and git never reads the
reviewer's global or system configuration.
Classes named GroupA_... to GroupE_... hold the DESIGN §8 groups (`python3 -m unittest ... -k GroupA`)."""
import ast
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
ENGINE = SCRIPTS / "bridge.py"
FIXTURES = TESTS / "fixtures"
EVIDENCE = FIXTURES / "evidence"
STUB_CODEX = FIXTURES / "stub_codex.py"
STUB_CLAUDE = FIXTURES / "stub_claude.py"
STUB_RELAY = FIXTURES / "stub_relay.py"
RELAY_PY = TESTS.parents[3] / "skills" / "relay" / "scripts" / "relay.py"  # relay itself: its busy refusal line is read from its source
HOST_GUARD = FIXTURES / "host_guard"                      # sitecustomize: no codex/claude outside the test root may start
MODELS_JSON = TESTS.parent / "models.json"                 # DESIGN §1: claude-codex-bridge/skills/codex/models.json
PLUGIN_JSON = TESTS.parents[2] / ".claude-plugin" / "plugin.json"
CATALOG_0_156_1 = EVIDENCE / "codex_model_list_cli0.156.1.json"
CATALOG_0_159_3 = EVIDENCE / "codex_model_list_cli0.159.3.json"  # the live list of Kevin's Sol checkpoint (2026-10-01)
CATALOG_0_154_0 = EVIDENCE / "codex_model_list_cli0.154.0.json"
CONFIG_DEFAULT = EVIDENCE / "codex_config_read_all_default.json"
CONFIG_CATALOG_JSON = EVIDENCE / "codex_config_read_model_catalog_json.json"
CONFIG_SENTINEL = EVIDENCE / "codex_config_read_unused_provider_sentinel.json"
SENTINEL_TOKEN = "SENTINEL-7f3a9c2e41d0-DO-NOT-PRINT"
DEPRECATIONS = EVIDENCE / "anthropic_model-deprecations_20261008.md"  # the pages of 2026-10-08: they list every Claude head of the table
OVERVIEW = EVIDENCE / "anthropic_models-overview_20261008.md"          # (C7's 2026-09-29 captures stay for test_bridge_cleanup_c7.py)
FROZEN_NOW = 1790306400  # 2026-09-25T03:20:00Z (DESIGN §8)
# relay 1.5.2's reason for an account whose usage reads have kept failing for an hour (skills/relay/scripts/relay.py evaluate_codex),
# as its pick answer carries it in `rows`, the read failure written as relay's own suite simulates one (tests/fake_codex.py)
RELAY_UNREAD_REASON = ("its usage could not be read for 1h01m (account/rateLimits/read: simulated failure of account/rateLimits/read): "
                       "no new work is routed on numbers that old")

sys.path.insert(0, str(FIXTURES))
from page_server import PageServer, unreachable_pages  # noqa: E402
import catalog_fixture  # noqa: E402

# Variables a developer's shell may carry that would change what the engine resolves or checks: never inherited.
ISOLATED_NAMES = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "BRIDGE_DEFAULT_SEAT", "BRIDGE_REVIEW_SEATS",
                  "BRIDGE_COUNCIL_SEATS", "BRIDGE_MODELS_GATE", "BRIDGE_MODELS_FILE", "BRIDGE_MODELS_EVIDENCE", "BRIDGE_CODEX_EFFORT",
                  "BRIDGE_CLAUDE_EFFORT", "BRIDGE_ROUTER", "BRIDGE_ROOT", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CACHE_HOME",
                  "RELAY_CLAUDE_BIN", "RELAY_HOME", "MODEL_REGISTRY_DIR", "MODEL_REGISTRY_HOME", "STUB_CODEX_RECORD", "STUB_CLAUDE_RECORD", "STUB_CODEX_VERSION",
                  "STUB_CLAUDE_VERSION", "STUB_CODEX_CATALOG", "STUB_CODEX_CONFIG", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                  "BRIDGE_MODELS_NPM_URL", "BRIDGE_MODELS_DEPRECATIONS_URL", "BRIDGE_MODELS_OVERVIEW_URL",
                  "BRIDGE_CODEX_FIXED_ROOT", "CLAUDE_BRIDGE_ENGINE", "XDG_CONFIG_HOME",
                  # the user's own wait knobs (a profile that lets a routed seat wait a week would turn every dead-end test into a wait)
                  "BRIDGE_ROUTED_MAX_WAIT", "BRIDGE_LIMIT_LADDER",
                  "BRIDGE_MODELS_FETCH_DEADLINE", "BRIDGE_MODELS_LOCK_DEADLINE", "STUB_CODEX_APPSERVER_FRAMES", "STUB_CODEX_RPC_ERROR",
                  "STUB_CODEX_RPC_ERROR_TEXT", "STUB_CODEX_RPC_ERROR_CODE", "STUB_CODEX_CONFIG_RESULT", "STUB_CODEX_MODEL_LIST_RESULT",
                  "STUB_CODEX_PROBE_SLEEP", "STUB_CODEX_APPSERVER_IGNORE_TERM",
                  # S-DEF: a Codex-side entry point's built-in seat lists (a suite run from under one), and the provenance
                  # variable that ruling removes
                  "BRIDGE_BUILTIN_DEFAULT_SEAT", "BRIDGE_BUILTIN_REVIEW_SEATS", "BRIDGE_BUILTIN_COUNCIL_SEATS", "BRIDGE_DEFAULTS_SUPPLIED",
                  # the capacity gate, the pacer and the throttle class (build spec 2026-10-13 section 5): a developer's knobs, a real
                  # lease or capacity dir, or a stub throttle left in the shell would change what every engine test sees
                  "BRIDGE_CLASS", "BRIDGE_CAPACITY", "BRIDGE_CAPACITY_DIR", "LR_CU_COORD_DIR", "BRIDGE_CAP_MODE", "BRIDGE_CODEX_SESSION_CAP",
                  "LR_CODEX_SESSION_CAP", "BRIDGE_CODEX_INTERACTIVE_RESERVE", "BRIDGE_INTERACTIVE_BUDGET", "BRIDGE_CODEX_WEIGHT_ULTRA",
                  "BRIDGE_THROTTLE_WAIT_SCALE", "BRIDGE_NET_PROBE", "BRIDGE_FAKE_WAKETIME", "BRIDGE_WAKE_QUIET", "BRIDGE_MIN_GAP",
                  "BRIDGE_RAMP_START", "BRIDGE_RAMP_STEP", "BRIDGE_CAPACITY_MAX_WAIT_INTERACTIVE", "STUB_CODEX_THROTTLE",
                  # automatic promotion (DECISIONS A28) and the stub's login answer
                  "BRIDGE_MODELS_PROMOTE", "STUB_CODEX_ACCOUNT")


def load_table():
    """The shipped table (DESIGN §1), the only one the tests read (DESIGN D23): without it the suite does not load."""
    if not MODELS_JSON.is_file():
        raise FileNotFoundError(f"DESIGN §1: the model table must ship at {MODELS_JSON}; the tests read no other table")
    return json.loads(MODELS_JSON.read_text())


TABLE = load_table()


def clean_catalog_file():
    """The clean catalog every engine test serves unless it says otherwise (STUB_CODEX_CATALOG): the newest real capture
    (catalog_fixture.NEWEST) as it is when it lists every head of the shipped table, else that capture completed with a derived entry
    for each head it lacks, written once per run to a private temporary file (catalog_fixture.complete)."""
    models, added = catalog_fixture.complete(catalog_fixture.load_models(), TABLE)
    if not added:
        return catalog_fixture.NEWEST
    folder = Path(tempfile.mkdtemp(prefix="bridge-test-catalog-"))
    import atexit
    atexit.register(shutil.rmtree, str(folder), True)
    path = folder / "clean_catalog.json"
    path.write_text(json.dumps({"models": models}))
    return path


CATALOG_CURRENT = clean_catalog_file()
PAGES = -(-len(json.loads(CATALOG_CURRENT.read_text())["models"]) // 3)  # model/list requests per catalog read (the stub pages by three)

# The ids a test needs beside the heads come from the table (docs/model-routing.md 9.1, 10.1: a versioned id written into a test
# is a lint error unless the table supplies it or it is one of the registry's fictional fixture ids): the K1 seat's target, the
# Haiku head's listed spelling, the retired Sol predecessor that is not also a refused name, the older Opus predecessors, and the
# refused records' spellings. The fictional ids are the registry's fixture ids (registry.json lint.fixture_ids).
K1_ID = TABLE["k1_seat"]["id"]
HAIKU_ALIAS = "claude-haiku-9-8"  # synthetic distinct spelling; the current dateless API alias equals its ID
RETIRED_SOL = next(x for x in TABLE["retired"]["codex"]["sol"] if x not in TABLE["refused"]["codex"])
RETIRED_OPUS = [x for x in TABLE["retired"]["claude"]["opus"] if x != K1_ID]                        # newest first
RETIRING_FULL = next(k for k, v in TABLE["refused"]["codex"].items() if v.get("same_as") == "5.5")           # the retiring model's full spelling
SPARK_ID = TABLE["refused"]["codex"]["spark"]["id"]
REFUSED_GPT6 = next(k for k, v in TABLE["refused"]["codex"].items() if v.get("family") == "astra" and "*" not in k and "same_as" not in v)
REFUSED_SOL_NAME = next(k for k, v in TABLE["refused"]["codex"].items() if v.get("family") == "sol" and "id" not in v and "same_as" not in v)
NEWER_SOL, NEWER_LUNA = "gpt-9-sol", "gpt-9.9-luna"   # fictional successors the shipped table does not know: a stale FAIL when listed
NEWER_HAIKU, NEWER_OPUS = "claude-haiku-9-9", "claude-opus-9-9"
OTHER_OPUS = "claude-opus-9-8"                        # what the Claude stub's unflagged synthetic message reports


def vendor_of(family):
    return "codex" if family in TABLE["families"]["codex"] else "claude"


def head(family):
    """A family's head (its `known_good` ID), read from the table."""
    return TABLE["families"][vendor_of(family)][family]["known_good"]


def descend_mutation(vendor, src, dst):
    """A table mutation adding the fallback edge src -> dst with dst's generation set below every real one: a cross-generation
    descent whatever the shipped generations are."""
    def mutate(t):
        t["models"][dst]["generation"] = "0.1"
        t["fallback"][vendor][src] = dst
    return mutate


def with_haiku_alias(t):
    """A derived table with a distinct spelling to keep alias canonicalization regressions meaningful."""
    t["models"][t["families"]["claude"]["haiku"]["known_good"]]["aliases"].append(HAIKU_ALIAS)


def efforts_of(model):
    return TABLE["models"][model]["efforts"]


def families():
    return list(TABLE["families"]["codex"]) + list(TABLE["families"]["claude"])


def refusal_record(vendor, key):
    rec = TABLE["refused"][vendor][key]
    return TABLE["refused"][vendor][rec["same_as"]] if "same_as" in rec else rec


def exec_registered(name, path):
    """Load `path` as a fresh module registered in sys.modules under `name` before it executes, as a normal import is
    (dataclasses with postponed annotations look their module up there); a failed load restores the previous entry."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    previous = sys.modules.get(name)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return mod


def load_engine_module(env):
    """Import bridge.py in-process with the test environment applied (module-level constants read env at import)."""
    saved = dict(os.environ)
    for name in ISOLATED_NAMES:
        os.environ.pop(name, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        return exec_registered("bridge_under_test", ENGINE)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def import_bridge_models():
    """skills/codex/scripts/bridge_models.py (DESIGN §0: the table's one reader), loaded fresh."""
    return exec_registered("bridge_models_under_test", SCRIPTS / "bridge_models.py")


def without_git_overrides(env):
    """`env` without inherited GIT_* variables (a repository, index or configuration of the reviewer's)."""
    return {k: v for k, v in env.items() if not k.startswith("GIT_")}


def fixture_git(repo, home, *args):
    """Git for a fixture repository: HOME and XDG_CONFIG_HOME under the test root, no global or system configuration, no
    inherited GIT_* override, no hooks and no commit signing, so no helper or hook of the reviewer's can run."""
    env = without_git_overrides(os.environ)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(Path(home) / ".config"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", *args],
                          cwd=str(repo), env=env, check=True, capture_output=True, text=True, timeout=30)


def epoch_of(value):
    """A recorded timestamp as epoch seconds: a number, or an ISO 8601 string (`Z` or an offset; naive means UTC)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment is not None:
            return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    raise AssertionError(f"not a timestamp (epoch seconds or ISO 8601): {value!r}")


def shifted(value, seconds):
    """The timestamp `value` moved by `seconds`, written back in the representation it came in."""
    epoch_of(value)  # rejects anything that is not a timestamp
    if isinstance(value, int):
        return value + int(seconds)
    if isinstance(value, float):
        return value + seconds
    try:
        number = float(value)
    except ValueError:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00")) + timedelta(seconds=seconds)
        text = moment.isoformat()
        return text.replace("+00:00", "Z") if value.strip().endswith("Z") else text
    return str(int(number + seconds)) if re.fullmatch(r"\s*-?\d+\s*", value) else str(number + seconds)


def walk(obj, parent_key=None):
    """Every (key, dict) pair nested anywhere in obj (the key under which a dict was found, or None)."""
    if isinstance(obj, dict):
        yield parent_key, obj
        for k, v in obj.items():
            yield from walk(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, parent_key)


def strings(obj):
    """Every string nested anywhere in obj."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def markers_in(obj):
    """Sticky marker records ({id, kind, source, seen_at, detail}, DESIGN §5) wherever an evidence file keeps them (a list
    of records, or records keyed by model ID). Cleared-log entries ({id, at, reason, marker}) are not markers."""
    found = []
    for key, d in walk(obj):
        if key in ("cleared", "marker"):
            continue
        if "kind" in d and "seen_at" in d and d.get("kind") in ("deprecated", "retiring", "retired"):
            found.append(dict(d, id=d.get("id") or key))
    return found


def cleared_in(obj):
    # changed for S-C3: the cleared log's entries are {id, at, reason, marker}; clear_seq (and each entry's seq) is withdrawn
    return [d for _, d in walk(obj) if "id" in d and "at" in d and "reason" in d]


def keys_in(obj):
    """Every dict key nested anywhere in obj."""
    return {k for _, d in walk(obj) for k in d}


def leading_notices(text):
    """The NOTICE lines an output starts with (DESIGN §4: notices lead every user-facing output)."""
    lead = []
    for line in (text or "").splitlines():
        if not line.startswith("NOTICE: "):
            break
        lead.append(line)
    return lead


def relay_busy_line(label, slots, wake):
    """relay's busy refusal exactly as `relay exec --account <label>` prints it, built from relay's own source (ruling BUSY-1 A9):
    cmd_exec's `why` for a busy verdict (the expression built from "its %d slot%s in use"), the die() call on the line that follows
    it, and die()'s own prefix, evaluated for `slots` (relay's per_account_concurrency) and an integer wake time. A change of
    relay's wording changes this line, so the bridge's busy detector has to follow it."""
    tree = ast.parse(RELAY_PY.read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def value(node, **names):
        return eval(compile(ast.Expression(node), str(RELAY_PY), "eval"), {"__builtins__": {}}, names)

    for node in ast.walk(funcs["cmd_exec"]):
        body = getattr(node, "body", None)
        for i, st in enumerate(body if isinstance(body, list) else []):
            if not (isinstance(st, ast.Assign) and [ast.unparse(t) for t in st.targets] == ["why"] and "slot" in ast.unparse(st.value)):
                continue
            call = getattr(body[i + 1], "value", None) if i + 1 < len(body) else None
            writes = [c for c in ast.walk(funcs["die"]) if isinstance(c, ast.Call) and ast.unparse(c.func) == "sys.stderr.write"]
            if not (isinstance(call, ast.Call) and ast.unparse(call.func) == "die" and call.args and len(writes) == 1):
                raise AssertionError(f"relay's busy `why` in cmd_exec is no longer followed by its die() line ({RELAY_PY})")
            why = value(st.value, cap=slots, v={"result": "busy"}, short=None)
            return value(writes[0].args[0], msg=value(call.args[0], account=label, why=why, wake=wake))
    raise AssertionError(f"cmd_exec in {RELAY_PY} no longer builds a busy refusal from 'its %d slot%s in use'")


def without_wake(text):
    """A relay refusal with its wake time blanked, to compare lines printed at different moments."""
    return re.sub(r"wake_at=\d+", "wake_at=<int>", text)


class EngineBase(unittest.TestCase):
    """Shared fixture: a git repository, a private jobs root, the stub CLIs, the page server and an isolated environment.
    Holds no tests itself."""

    @classmethod
    def setUpClass(cls):
        for s in (STUB_CODEX, STUB_CLAUDE, STUB_RELAY):
            s.chmod(s.stat().st_mode | stat.S_IXUSR)
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
        cls.jobs = cls.tmp / "jobs"
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        (cls.tmp / "git-home").mkdir()
        run = lambda *a: fixture_git(cls.repo, cls.tmp / "git-home", *a)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (cls.repo / "store.py").write_text("def avg(v):\n    return sum(v)/len(v)\n")
        run("add", "-A")
        run("commit", "-q", "--no-verify", "--no-gpg-sign", "-m", "base")
        (cls.repo / "store.py").write_text("def avg(v):\n    return sum(v)/len(v)\n\ndef parse_port(s):\n    p=int(s)\n    if p>65536: raise ValueError\n    return p\n")
        (cls.repo / "newfile.txt").write_text("untracked content line\n")
        for sub in ("home", "codex-home", "cache", "config", "tables", "catalogs", "cases", "fixed-root", "mr-home", "relay-home", "capacity", "coord"):
            (cls.tmp / sub).mkdir()
        cls.pages = PageServer()
        cls.env = {
            "BRIDGE_JOBS": str(cls.jobs), "CODEX_BIN": str(STUB_CODEX), "CLAUDE_BIN": str(STUB_CLAUDE),
            "BRIDGE_APP_LAUNCHER": str(cls.tmp / "no-such-app"), "BRIDGE_LIMIT_WAIT_SHORT": "0.2", "BRIDGE_LIMIT_WAIT_LONG": "0.3",
            "CLAUDE_CODE_OAUTH_TOKEN": "test-token", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "outer",
            "RELAY_BIN": str(cls.tmp / "no-such-relay"),  # authoritative and missing: the real relay is never consulted
            # docs/model-routing.md 2.1, 10.3: the engine finds this checkout's model registry above itself; every piece of the
            # registry's mutable state (catalog snapshots, heads, scratch homes) lives under the test root, never in ~/.claude
            "MODEL_REGISTRY_HOME": str(cls.tmp / "mr-home"), "RELAY_HOME": str(cls.tmp / "relay-home"),
            # DESIGN §5/§8 isolation: the check reads the stub catalog and the fixture pages, never ~/.codex or the network
            "HOME": str(cls.tmp / "home"), "CODEX_HOME": str(cls.tmp / "codex-home"), "XDG_CACHE_HOME": str(cls.tmp / "cache"),
            "RELAY_CLAUDE_BIN": str(STUB_CLAUDE), "STUB_CODEX_CATALOG": str(CATALOG_CURRENT), "STUB_CODEX_CONFIG": str(CONFIG_DEFAULT),
            # TEST_RULINGS F-E6: the default fixed directories are looked up beneath this empty directory, never the host's
            "BRIDGE_CODEX_FIXED_ROOT": str(cls.tmp / "fixed-root"),
            # the engine's own git (review capture) reads no global or system configuration of the reviewer's
            "XDG_CONFIG_HOME": str(cls.tmp / "config"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            # the capacity gate (spec 5.7): a private capacity dir and coordination dir (no lease, no PAUSE file), no network probe
            # and no launch gap, so the suite keeps its timing and never reads a real lease or ~/.codex
            "BRIDGE_CAPACITY_DIR": str(cls.tmp / "capacity"), "LR_CU_COORD_DIR": str(cls.tmp / "coord"), "BRIDGE_NET_PROBE": "off",
            "BRIDGE_MIN_GAP": "0",
            # automatic promotion (DECISIONS A28) is off for every test that does not ask for it: the stale and catalog scenarios
            # serve newer models on purpose; GroupP_AutoPromotion switches it on (env_extra BRIDGE_MODELS_PROMOTE=None: the default)
            "BRIDGE_MODELS_PROMOTE": "off",
            **cls.pages.pages("default"),
        }

    @classmethod
    def tearDownClass(cls):
        cls.pages.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_engine(self, *args, env_extra=None, timeout=120, cwd=None, stdin_text=None):
        env = without_git_overrides(os.environ)
        for name in ISOLATED_NAMES:
            env.pop(name, None)
        env.update(self.env)
        for k, v in (env_extra or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        r = subprocess.run([sys.executable, str(ENGINE), *args], capture_output=True, text=True, timeout=timeout, env=env,
                           cwd=str(cwd or self.repo), **({"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}))
        return r.returncode, r.stdout, r.stderr

    def seat(self, d, tag):
        return json.loads((Path(d) / f"seat-{tag}.json").read_text())

    def meta(self, d):
        return json.loads((Path(d) / "meta.json").read_text())

    def job_dir(self, out):
        for line in out.splitlines():
            if line.startswith("— job: "):
                return self.jobs / line.split("— job: ", 1)[1].strip()
        raise AssertionError("no job footer in output:\n" + out)

    def read_records(self, path):
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]

    def start_engine(self, env_extra, *args):
        env = without_git_overrides(os.environ)
        for name in ISOLATED_NAMES:
            env.pop(name, None)
        env.update(self.env)
        for k, v in (env_extra or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return subprocess.Popen([sys.executable, str(ENGINE), *args], cwd=str(self.repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for(self, d, tag, pred, tries=150):
        seat = {}
        for _ in range(tries):
            if (Path(d) / f"seat-{tag}.json").exists():
                seat = self.seat(d, tag)
                if pred(seat):
                    return seat
            time.sleep(0.1)
        self.fail(f"seat {tag} never reached the expected state: {seat}")

    def relay(self, name, script):
        """A scripted stub relay (fixtures/stub_relay.py): the env overrides that select it, and its record directory."""
        rd = self.tmp / f"relay-{name}"
        rd.mkdir(exist_ok=True)
        (rd / "script.json").write_text(json.dumps(script))
        return {"RELAY_BIN": str(STUB_RELAY), "STUB_RELAY_DIR": str(rd)}, rd

    def records(self, rd, name):
        p = Path(rd) / name
        return self.read_records(p) if p.exists() else []

    def picks(self, rd):
        return [c["argv"] for c in self.records(rd, "calls.jsonl") if c["argv"][:1] == ["pick"]]

    @staticmethod
    def _procs_matching(needle):
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
        return [line for line in out.splitlines() if needle in line]

    # ---------------------------------------------------------------- models change helpers (DESIGN rev 13)
    def case_dir(self, name):
        d = self.tmp / "cases" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def table_file(self, name, mutate=None):
        """A synthetic table (BRIDGE_MODELS_FILE): a deep copy of the table under test, changed by `mutate`."""
        t = copy.deepcopy(TABLE)
        if mutate:
            mutate(t)
        p = self.tmp / "tables" / f"{name}.json"
        p.write_text(json.dumps(t, indent=1))
        return p

    def catalog_file(self, name, mutate=None, base=CATALOG_CURRENT):
        """A synthetic `model/list` answer for the stub app-server (STUB_CODEX_CATALOG): the real capture, changed by
        `mutate(models)` (a list of model entries, edited in place)."""
        models = copy.deepcopy(json.loads(Path(base).read_text())["models"])
        if mutate:
            mutate(models)
        p = self.tmp / "catalogs" / f"{name}.json"
        p.write_text(json.dumps({"models": models}, indent=1))
        return p

    @staticmethod
    def entry(models, model_id):
        return next(m for m in models if m["id"] == model_id)

    @staticmethod
    def new_entry(model_id, efforts=("low", "medium", "high", "xhigh", "max", "ultra"), hidden=False, **extra):
        e = {"id": model_id, "model": model_id, "upgrade": None, "upgradeInfo": None, "displayName": model_id, "description": "",
             "hidden": hidden, "isDefault": False, "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": x, "description": x} for x in efforts]}
        e.update(extra)
        return e

    def isolated(self, name, catalog=None, config=None, table=None, npm=None, deprecations=None, overview=None, **extra):
        """The environment of an isolated check: its own evidence store (BRIDGE_MODELS_EVIDENCE), stub records, and fixture
        pages served under /<name>/ (an int serves that HTTP status: a failed fetch)."""
        d = self.case_dir(name)
        env = {"BRIDGE_MODELS_EVIDENCE": str(d / "models-evidence.json"), "STUB_CODEX_RECORD": str(d / "codex.jsonl"),
               "STUB_CLAUDE_RECORD": str(d / "claude.jsonl"), "MODEL_REGISTRY_HOME": str(d / "mr-home"),  # the case's own registry state
               "STUB_CODEX_CATALOG": str(catalog or CATALOG_CURRENT), "STUB_CODEX_CONFIG": str(config or CONFIG_DEFAULT)}
        env.update(self.pages.pages(name, npm=npm, deprecations=deprecations, overview=overview))
        if table:
            env["BRIDGE_MODELS_FILE"] = str(table)
        env.update(extra)
        return env, d

    def codex_records(self, env, mode=None):
        p = Path(env["STUB_CODEX_RECORD"])
        recs = self.read_records(p) if p.exists() else []
        return [r for r in recs if mode is None or r.get("mode") == mode]

    def check_records(self, env):
        """The app-server requests of the bridge's own models check (its initialize names the client "claude-codex-bridge"; a
        session that never got that far has none). The model registry's per-account catalog reads (client "model-registry",
        docs/model-routing.md 3.3) are the routing's, not the check's, and are left out."""
        return [r for r in self.codex_records(env, "app-server") if r.get("client") in (None, "claude-codex-bridge")]

    def registry_reads(self, env):
        """The model registry's own catalog reads through the app-server (docs/model-routing.md 3.3)."""
        return [r for r in self.codex_records(env, "app-server") if r.get("client") == "model-registry"]

    def claude_records(self, env, mode="prompt"):
        p = Path(env["STUB_CLAUDE_RECORD"])
        recs = self.read_records(p) if p.exists() else []
        return [r for r in recs if mode is None or r.get("mode") == mode]

    def launched_models(self, env):
        """Every model a stub CLI was asked to run (codex -m / claude --model), in launch order."""
        recs = [(r["t"], r["model"]) for r in self.codex_records(env, "exec")] + [(r["t"], r["model"]) for r in self.claude_records(env)]
        return [m for _, m in sorted(recs, key=lambda x: x[0])]

    def page_calls(self, name):
        return self.pages.calls(f"/{name}/")

    def evidence(self, env):
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        return json.loads(p.read_text()) if p.exists() else {}

    def notices(self, d):
        return self.meta(d).get("notices") or []

    def new_jobs(self, before):
        return sorted({p.name for p in self.jobs.iterdir()} - before) if self.jobs.exists() else []

    def job_names(self):
        return {p.name for p in self.jobs.iterdir()} if self.jobs.exists() else set()

    @staticmethod
    def deprecate(models, model_id, **fields):
        """Mark a catalog entry (adding it when absent) with lifecycle fields; default: `deprecated: true`."""
        fields = fields or {"deprecated": True}
        for m in models:
            if m["id"] == model_id:
                m.update(fields)
                return
        models.append(EngineBase.new_entry(model_id, **fields))

    @staticmethod
    def deprecations_with(model_id, state, deprecated, retirement, base=DEPRECATIONS):
        """The deprecations page with `model_id`'s status-table row replaced (or added)."""
        text = Path(base).read_text()
        row = f"| {model_id} | {state} | {deprecated} | {retirement} |"
        pattern = re.compile(r"(?m)^\| " + re.escape(model_id) + r" +\|.*$")
        if pattern.search(text):
            return pattern.sub(row, text, count=1)
        anchor = text.index(f"| {head('fable')} ")
        return text[:anchor] + row + "\n" + text[anchor:]

    def mark(self, env, model_id, name):
        """Record a sticky marker on `model_id` the way the design records one: a check (`doctor`) whose applicable source
        affirmatively marks it. The caller's environment keeps its own (clean) sources afterwards."""
        marking = dict(env)
        if model_id.startswith("gpt-"):
            marking["STUB_CODEX_CATALOG"] = str(self.catalog_file(f"{name}-marks-{model_id}", lambda ms: self.deprecate(ms, model_id)))
        else:
            marking.update(self.pages.pages(f"{name}-marks-{model_id}",
                                            deprecations=self.deprecations_with(model_id, "Deprecated", "September 20, 2026", "March 20, 2027")))
        rc, out, err = self.run_engine("doctor", env_extra=marking)
        self.assertTrue(any(m["id"] == model_id for m in markers_in(self.evidence(env))),
                        f"the check recorded no marker on {model_id} in {env['BRIDGE_MODELS_EVIDENCE']}:\n{out}{err}")

    def doctor(self, env, *args):
        return self.run_engine("doctor", *args, env_extra=env)

    def doctor_json(self, env):
        rc, out, err = self.run_engine("doctor", "--json", env_extra=env)
        try:
            data = json.loads(out)
        except ValueError:
            self.fail(f"doctor --json printed no JSON (exit {rc}):\n{out}{err}")
        self.assertIsInstance(data, dict)
        return rc, data, err

    def reads(self, env, name):
        """(Codex catalog requests of the check, page fetches) so far for an isolated check."""
        return len(self.check_records(env)), len(self.page_calls(name))

    def assert_refused(self, rc, out, err, code=2):
        """A refusal (DESIGN §2/§3): the exit code and ONE line on stderr starting `bridge: ` (progress lines the engine
        prefixes with `[bridge]` aside), no traceback."""
        lines = [l for l in err.splitlines() if l.strip() and not l.startswith("[bridge]")]
        self.assertEqual(rc, code, out + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(lines), 1, f"expected ONE line on stderr, got: {lines}")
        self.assertTrue(lines[0].startswith("bridge: "), lines[0])
        return lines[0]


class EngineTestCase(EngineBase):
    """The suite as it stood before the models change; tests the design changes are marked `# changed for DESIGN §n`."""

    # ---------------------------------------------------------------- pure functions
    def test_seat_parsing_effort_override_tags_and_fast(self):
        # changed for DESIGN §1/§2: models come from the table (family names mean the head); luna:ultra is refused
        b = load_engine_module(self.env)
        seats = b.parse_seats("astra:max,astra:ultra,opus,sol@fast", "x", effort_override="low")
        self.assertEqual([s.tag for s in seats], ["astra", "astra-2", "opus", "sol"])
        self.assertEqual([s.effort for s in seats], ["max", "ultra", "low", "low"])
        self.assertEqual([s.model for s in seats], [head("astra"), head("astra"), head("opus"), head("sol")])
        self.assertTrue(seats[3].fast and not seats[0].fast)
        with self.assertRaises(SystemExit):
            b.parse_seats("sonnet@fast", "x")
        with self.assertRaises(SystemExit):
            b.parse_seats("luna:ultra", "x")  # luna has no ultra tier (DESIGN §2: the table's effort set refuses it)
        used = set()
        a = b.parse_seats("astra", "x", tag_prefix="S1 arch/decisions", used_tags=used)
        c = b.parse_seats("astra,sol", "x", tag_prefix="S1 arch/decisions", used_tags=used)
        self.assertEqual(a[0].tag, "S1-arch-decisions")
        self.assertEqual([s.tag for s in c], ["S1-arch-decisions-astra", "S1-arch-decisions-sol"])

    def test_render_is_single_pass(self):
        b = load_engine_module(self.env)
        out = b.render("ask", TEXT="what does {{CALLER}} mean here?")
        self.assertIn("what does {{CALLER}} mean here?", out)  # inserted text is never rescanned
        self.assertNotIn("({{CALLER}})", out)  # the template's own placeholder was replaced

    def test_clean_env_keeps_auth_and_drops_session_vars(self):
        b = load_engine_module(self.env)
        os.environ["CLAUDECODE"] = "1"
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "tok"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s"
        os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/x"
        try:
            env = b.clean_env()
            self.assertNotIn("CLAUDECODE", env)
            self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
            self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/x")
        finally:
            for k in ("CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR"):
                os.environ.pop(k, None)

    def test_find_job_rejects_paths_and_confines_to_root(self):
        b = load_engine_module(self.env)
        jid, d = b.new_job("ask")
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        self.assertEqual(b.find_job(jid), d)
        for bad in ("../" + jid, "/etc", "a/b", ".."):
            with self.assertRaises(SystemExit):
                b.find_job(bad)
        with self.assertRaises(SystemExit):
            b.new_job("ask", name="../escape")
        with self.assertRaises(SystemExit):
            b.new_job("ask", name=jid)  # already exists

    # ---------------------------------------------------------------- codex seat
    def test_ask_codex_stub_uses_stdin_sandbox_and_private_files(self):
        rec = self.tmp / "codex-argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", f"[[STUB:argv={rec}]] hello")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("OK"), out)
        d = self.job_dir(out)
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        for f in d.iterdir():
            if f.is_file():
                self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600", f.name)
        r = self.read_records(rec)[0]
        self.assertIn("-", r["argv"])
        self.assertNotIn("hello", " ".join(r["argv"]))
        self.assertIn("hello", r["prompt"])
        self.assertIn("--ignore-user-config", r["argv"])
        self.assertEqual(r["argv"][r["argv"].index("-s") + 1], "read-only")
        seat = json.loads((d / "seat-astra.json").read_text())
        self.assertEqual(seat["state"], "done")
        self.assertEqual(seat["thread_id"], "stub-thread-01a0")
        self.assertIn("pid_start", seat)
        meta = json.loads((d / "meta.json").read_text())
        self.assertEqual(meta["state"], "done")

    def test_ask_claude_stub_is_restricted_and_gets_prompt_on_stdin(self):
        rec = self.tmp / "claude-argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:argv={rec}]] hi there")
        self.assertEqual(rc, 0, err)
        d = self.job_dir(out)
        r = self.read_records(rec)[0]
        argv = r["argv"]
        self.assertEqual(r["positional"], [], "prompt must not be on the command line")
        self.assertIn("hi there", r["prompt"])
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(os.path.realpath(argv[argv.index("--add-dir") + 1]), os.path.realpath(str(d)))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--allowedTools", argv)
        self.assertFalse(r["env_has_claudecode"])
        self.assertTrue(r["env_has_token"])
        seat = json.loads((d / "seat-sonnet.json").read_text())
        self.assertEqual(seat["state"], "done")
        self.assertEqual(seat["context_tokens"], 4002)  # latest call: 2 + 4000 + 0
        self.assertEqual(seat["context_window"], 1000000)

    def test_claude_write_task_gets_write_tools_and_shell_is_opt_in(self):
        rec = self.tmp / "claude-write-argv.jsonl"
        rc, out, err = self.run_engine("task", "--write", "--seats", "sonnet:low", "--", f"[[STUB:argv={rec}]] do it")
        self.assertEqual(rc, 0, err)
        argv = self.read_records(rec)[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Edit,Write,MultiEdit")  # no shell unless asked
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        rec2 = self.tmp / "claude-write-shell-argv.jsonl"
        rc, out, err = self.run_engine("task", "--write", "--shell", "--seats", "sonnet:low", "--", f"[[STUB:argv={rec2}]] do it")
        self.assertEqual(rc, 0, err)
        argv = self.read_records(rec2)[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Edit,Write,MultiEdit,Bash")
        self.assertIn("Bash(git commit*)", argv[argv.index("--disallowedTools") + 1])
        self.assertIn("Bash(git status *)", argv[argv.index("--allowedTools") + 1])
        rc, out, err = self.run_engine("task", "--shell", "--seats", "sonnet:low", "--", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--shell only applies", err)
        rc, out, err = self.run_engine("task", "--full-access", "--seats", "sonnet:low", "--", "x")
        self.assertEqual(rc, 2)
        self.assertIn("Codex-only", err)

    def test_user_config_is_honoured_only_for_write_tasks(self):
        rec = self.tmp / "uc-argv.jsonl"
        rc, out, err = self.run_engine("ask", "--with-user-config", "--seats", "astra:low", "--", f"[[STUB:argv={rec}]] q")
        self.assertEqual(rc, 0, err)
        self.assertIn("--ignore-user-config", self.read_records(rec)[0]["argv"])
        self.assertIn("honoured only for write tasks", err)
        rec2 = self.tmp / "uc-argv2.jsonl"
        rc, out, err = self.run_engine("task", "--write", "--with-user-config", "--seats", "astra:low", "--", f"[[STUB:argv={rec2}]] t")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("--ignore-user-config", self.read_records(rec2)[0]["argv"])

    def test_codex_bin_is_authoritative_and_needs_the_capability_floor(self):
        old = self.tmp / "old-codex"
        old.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo codex-cli 0.40.0; exit 0; fi\necho 'exec: no such flags'; exit 0\n")
        old.chmod(0o755)
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", "q", env_extra={"CODEX_BIN": str(old)})
        self.assertEqual(rc, 1, out + err)
        self.assertIn("codex binary not found or too old", out + err)

    # ---------------------------------------------------------------- review
    def test_review_freezes_patch_saves_findings_and_merges(self):
        rec = self.tmp / "review-argv.jsonl"
        rc, out, err = self.run_engine("review", "--seats", "luna:low,sonnet:low", "--intent", "add parse_port", "--focus", f"[[STUB:argv={rec}]]")
        self.assertEqual(rc, 0, err)
        d = self.job_dir(out)
        patch = (d / "review.patch").read_text()
        self.assertIn("parse_port", patch)
        self.assertIn("untracked content line", patch)  # untracked files are part of the frozen change
        meta = json.loads((d / "review.meta.json").read_text())
        self.assertEqual(meta["kind"], "uncommitted")
        self.assertEqual(len(meta["sha256"]), 64)
        for tag in ("luna", "sonnet"):
            f = json.loads((d / f"findings-{tag}.json").read_text())
            self.assertTrue(f["_ok"], f)
            self.assertEqual(len(f["findings"]), 1)
            self.assertIn(str(d / "review.patch"), (d / f"prompt-{tag}.md").read_text())
        self.assertTrue((d / "merged.md").exists())
        self.assertIn("Stub finding", out)
        recs = self.read_records(rec)
        self.assertEqual(len(recs), 2)

    def test_review_intent_stdin_and_large_change_policy(self):
        rec = self.tmp / "intent-argv.jsonl"
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--intent-stdin",
                                       stdin_text=json.dumps({"intent": "INTENT-VIA-STDIN", "focus": f"FOCUS-VIA-STDIN [[STUB:argv={rec}]]"}))
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        m = self.meta(d)
        self.assertEqual(m["intent"], "INTENT-VIA-STDIN")
        self.assertIn("FOCUS-VIA-STDIN", m["focus"])
        prompt = (d / "prompt-luna.md").read_text()
        self.assertIn("INTENT-VIA-STDIN", prompt)
        self.assertIn("FOCUS-VIA-STDIN", prompt)
        self.assertIsNotNone(m.get("tree_fingerprint"))
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--intent-stdin", stdin_text="[1,2]")
        self.assertEqual(rc, 2)
        rc, out, err = self.run_engine("review", "--seats", "luna:low", env_extra={"BRIDGE_MAX_PATCH_LINES": "1"})
        self.assertEqual(rc, 2, out + err)
        self.assertIn("--allow-large", err)
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--allow-large", env_extra={"BRIDGE_MAX_PATCH_LINES": "1"})
        self.assertEqual(rc, 0, out + err)
        self.assertIn("large change", out)

    def test_untracked_symlink_is_omitted_from_the_frozen_patch(self):
        link = self.repo / "escape-link"
        link.symlink_to("/etc/hosts")
        try:
            rc, out, err = self.run_engine("review", "--seats", "luna:low")
            self.assertEqual(rc, 0, out + err)
            patch = (self.job_dir(out) / "review.patch").read_text()
            self.assertIn("# capture note:", patch)
            self.assertIn("escape-link", patch.split("diff --git", 1)[0])  # named in the notes, not diffed
            self.assertNotIn("localhost", patch)  # the link target's contents never enter the patch
        finally:
            link.unlink()

    def test_review_partial_and_total_failure_exit_codes(self):
        rc, out, err = self.run_engine("review", "--seats", "luna:low,sonnet:low", "--focus", "[[STUB:claudefail]]")
        self.assertEqual(rc, 3, out + err)
        d = self.job_dir(out)
        self.assertEqual(json.loads((d / "meta.json").read_text())["state"], "partial")
        self.assertFalse(json.loads((d / "findings-sonnet.json").read_text())["_ok"])
        self.assertIn("FAILED", out)
        rc, out, err = self.run_engine("review", "--seats", "luna:low,sonnet:low", "--focus", "[[STUB:fail]]", env_extra={"BRIDGE_LIMIT_LADDER": "0"})
        self.assertEqual(rc, 1, out + err)
        d = self.job_dir(out)
        self.assertEqual(json.loads((d / "meta.json").read_text())["state"], "failed")

    def test_bad_ref_fails_loudly(self):
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--base", "no-such-branch")
        self.assertEqual(rc, 1)
        self.assertIn("does not resolve", err)
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--commit=--output=/tmp/x")
        self.assertEqual(rc, 2)
        self.assertIn("looks like an option", err)

    # ---------------------------------------------------------------- lifecycle
    def test_timeout_kills_a_silent_seat(self):
        t0 = time.time()
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--timeout", "2", "--", "[[STUB:sleep=40]]")
        self.assertLess(time.time() - t0, 25)
        self.assertEqual(rc, 1)
        d = self.job_dir(out)
        seat = json.loads((d / "seat-astra.json").read_text())
        self.assertTrue(seat["timed_out"])
        self.assertIn("timed out", seat["activity"])
        self.assertFalse(self._pid_alive(seat["pid"]))

    def test_cancel_is_durable_and_kills_the_process_group(self):
        env = dict(os.environ)
        env.update(self.env)
        p = subprocess.Popen([sys.executable, str(ENGINE), "ask", "--seats", "astra:low", "--job-dir", "cancel-me", "--", "[[STUB:hang]]"],
                             cwd=str(self.repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        d = self.jobs / "cancel-me"
        for _ in range(100):
            if (d / "seat-astra.json").exists() and json.loads((d / "seat-astra.json").read_text()).get("pid"):
                break
            time.sleep(0.1)
        seat = json.loads((d / "seat-astra.json").read_text())
        self.assertTrue(self._pid_alive(seat["pid"]))
        rc, out, err = self.run_engine("cancel", "cancel-me")
        self.assertEqual(rc, 0)
        self.assertIn("cancelled cancel-me", out)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertTrue((d / "CANCELLED").exists())
        self.assertEqual(json.loads((d / "meta.json").read_text())["state"], "cancelled")
        self.assertEqual(json.loads((d / "seat-astra.json").read_text())["state"], "cancelled")
        self.assertFalse(self._pid_alive(seat["pid"]))

    def test_cancel_also_kills_commands_the_seat_started_in_their_own_sessions(self):
        env = dict(os.environ)
        env.update(self.env)
        orphan = self.tmp / "orphan-pid"
        p = subprocess.Popen([sys.executable, str(ENGINE), "ask", "--seats", "astra:low", "--job-dir", "cancel-orphans", "--", f"[[STUB:orphanfile={orphan}]]"],
                             cwd=str(self.repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if orphan.exists() and orphan.read_text().strip():
                break
            time.sleep(0.1)
        child = int(orphan.read_text().strip())
        self.assertTrue(self._pid_alive(child))
        self.assertNotEqual(os.getpgid(child), json.loads((self.jobs / "cancel-orphans" / "seat-astra.json").read_text())["pgid"])
        rc, out, err = self.run_engine("cancel", "cancel-orphans")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        for _ in range(50):
            if not self._pid_alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(self._pid_alive(child))

    def test_usage_limit_ladder_retries(self):
        counter = self.tmp / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", f"[[STUB:limit={counter}]]")
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        seat = json.loads((d / "seat-astra.json").read_text())
        self.assertEqual(seat["attempt"], 2)
        self.assertEqual(seat["limit_hits"], 1)
        self.assertTrue((d / "events-astra.attempt1.jsonl").exists())
        counter2 = self.tmp / "limit-counter-claude"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:limit={counter2}]]")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads((self.job_dir(out) / "seat-sonnet.json").read_text())["attempt"], 2)

    def test_usage_limit_after_work_resumes_the_same_thread(self):
        counter = self.tmp / "law-counter"
        rec = self.tmp / "law-argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", f"[[STUB:limitafterwork={counter},argv={rec}]]")
        self.assertEqual(rc, 0, out + err)
        recs = self.read_records(rec)
        self.assertEqual(len(recs), 1)  # the resumed turn carries the continuation prompt, not the directive
        d = self.job_dir(out)
        events2 = (d / "events-astra.jsonl").read_text()
        self.assertTrue((d / "events-astra.attempt1.jsonl").exists())
        self.assertIn("thread_id", events2)
        seat = self.seat(d, "astra")
        self.assertEqual(seat["attempt"], 2)
        self.assertEqual(seat["limit_hits"], 1)
        self.assertEqual(seat["thread_id"], "stub-thread-stub-thread-01a0")  # resumed from the first attempt's thread
        prompt_resumed = (d / "prompt-astra.md").read_text()
        self.assertIn("limitafterwork", prompt_resumed)  # the original prompt is kept on file
        # Claude: the stub's `limit` directive runs a tool first, so the retry must --resume the session
        crec = self.tmp / "claude-law-argv.jsonl"
        ccounter = self.tmp / "claude-law-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:limit={ccounter},argv={crec}]]")
        self.assertEqual(rc, 0, out + err)
        recs = self.read_records(crec)
        self.assertEqual(len(recs), 1)
        seat = self.seat(self.job_dir(out), "sonnet")
        self.assertEqual(seat["attempt"], 2)
        self.assertEqual(seat["session_id"], "stub-session-stub-session-aaaa")
        # and before any tool ran, the same prompt is simply retried (no --resume)
        crec2 = self.tmp / "claude-lbw-argv.jsonl"
        ccounter2 = self.tmp / "claude-lbw-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:limitbeforework={ccounter2},argv={crec2}]]")
        self.assertEqual(rc, 0, out + err)
        recs = self.read_records(crec2)
        self.assertEqual(len(recs), 2)
        self.assertNotIn("--resume", recs[1]["argv"])

    def test_waiting_seat_is_visible_and_cancel_interrupts_the_wait(self):
        env = dict(os.environ)
        env.update(self.env)
        env["BRIDGE_LIMIT_WAIT_SHORT"] = "30"
        counter = self.tmp / "wait-counter"
        p = subprocess.Popen([sys.executable, str(ENGINE), "ask", "--seats", "astra:low", "--job-dir", "wait-me", "--", f"[[STUB:limit={counter}]]"],
                             cwd=str(self.repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        d = self.jobs / "wait-me"
        seat = {}
        for _ in range(100):
            if (d / "seat-astra.json").exists():
                seat = self.seat(d, "astra")
                if seat.get("state") == "waiting":
                    break
            time.sleep(0.1)
        self.assertEqual(seat.get("state"), "waiting", seat)
        self.assertGreater(seat.get("waiting_until", 0), time.time())
        rc, out, err = self.run_engine("status", "wait-me")
        self.assertEqual(rc, 0)
        self.assertIn("waiting", out)
        self.assertNotIn("stale", out)
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "wait-me")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 15)  # the ladder wait was interrupted, not served
        self.assertEqual(self.meta(d)["state"], "cancelled")
        self.assertEqual(self.seat(d, "astra")["state"], "cancelled")

    def test_startup_model_rejection_descends_only_through_the_seats_preference_list(self):
        # changed for DESIGN §3/§4 and docs/model-routing.md 5 (F3): the table's fallback edge is a suggestion the bridge names, never
        # one it applies. A plain `astra` seat ends on the CLI's rejection (nothing else launches) and its failure names the spec that
        # would allow the descent; `astra|sol` descends to the Sol head once no account can run Astra, the CLI's rejection leads the
        # output and the descent is noticed as the list's
        astra, sol = head("astra"), head("sol")
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", "[[STUB:badmodel]]")
        self.assertEqual(rc, 1, out + err)
        seat = json.loads((self.job_dir(out) / "seat-astra.json").read_text())
        self.assertEqual((seat["model"], seat["state"]), (astra, "failed"))
        self.assertIn("the seat has no preference list, so it does not descend (say astra|sol:low to allow it)", seat["activity"])
        self.assertIn(f"model {astra} is not supported", leading_notices(out)[0])
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--", "[[STUB:badmodel]]")
        self.assertEqual(rc, 0, out + err)
        seat = json.loads((self.job_dir(out) / "seat-astra.json").read_text())
        self.assertEqual(seat["model"], TABLE["fallback"]["codex"][astra])  # the suggestion and the list agree here
        self.assertEqual((seat["model"], seat["attempt"], seat["seat_name"]), (sol, 2, "astra|sol"))
        lead = leading_notices(out)
        self.assertTrue(lead, "the output must start with the notices:\n" + out)
        self.assertIn(f"model {astra} is not supported", lead[0])  # the CLI's own error is the seat's first line
        self.assertTrue(any(l.startswith(f"NOTICE: seat astra: model {astra} -> {sol} (no capable account for {astra}: ") and "attempt 2)" in l
                            for l in lead), lead)

    def test_reply_resumes_and_ignores_previous_replies(self):
        rec = self.tmp / "reply-argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "reply-job", "--", "first")
        self.assertEqual(rc, 0, err)
        for i in range(2):
            rc, out, err = self.run_engine("reply", "reply-job", "--", f"[[STUB:argv={rec}]] follow-up {i}")
            self.assertEqual(rc, 0, out + err)
        recs = self.read_records(rec)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["resume"], "stub-thread-01a0")
        self.assertIn('sandbox_mode="read-only"', " ".join(recs[0]["argv"]))
        meta = json.loads((self.jobs / "reply-job" / "meta.json").read_text())
        self.assertEqual(meta["state"], "done")
        self.assertIsNone(meta.get("replying"))

    def test_reply_keeps_a_partial_job_partial(self):
        rc, out, err = self.run_engine("council", "--seats", "luna:low,sonnet:low", "--job-dir", "partial-job", "--", "[[STUB:codexfail]] q",
                                       env_extra={"BRIDGE_LIMIT_LADDER": "0"})
        self.assertEqual(rc, 3, out + err)
        self.assertEqual(self.meta(self.jobs / "partial-job")["state"], "partial")
        rc, out, err = self.run_engine("reply", "partial-job", "--seat", "sonnet", "--", "more please")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.meta(self.jobs / "partial-job")["state"], "partial")  # a reply never upgrades the job

    def test_job_guard_finalizes_and_failed_merge_is_reported(self):
        b = load_engine_module(self.env)
        jid, d = b.new_job("ask", "guard-job")
        with self.assertRaises(RuntimeError):
            with b.job_guard(d):
                b.update_meta(d, state="running")
                raise RuntimeError("mode blew up")
        self.assertEqual(self.meta(d)["state"], "failed")
        self.assertIn("mode blew up", self.meta(d)["error"])
        real = b.merge_findings.merge
        try:
            b.merge_findings.merge = lambda _d: (_ for _ in ()).throw(ValueError("bad findings"))
            md, data, ok = b.do_merge(d)
        finally:
            b.merge_findings.merge = real
        self.assertFalse(ok)
        self.assertIn("merge failed", md)

    def test_default_seat_efforts_yield_to_an_explicit_effort(self):
        b = load_engine_module(self.env)
        seats = b.parse_seats(None, "astra:ultra,sol:ultra@fast,opus:max", effort_override="low")
        self.assertEqual([s.effort for s in seats], ["low", "low", "low"])
        self.assertTrue(seats[1].fast)
        seats = b.parse_seats("astra:ultra,opus", "x", effort_override="low")  # explicit seats keep their own efforts
        self.assertEqual([s.effort for s in seats], ["ultra", "low"])
        # checkpoint F3 round 1, P2: a default's effort LIST (docs/model-routing.md 5) is one segment; --effort replaces the whole of
        # it, the model list and @fast stay
        seats = b.parse_seats(None, "astra|sol:ultra|max,luna:max|xhigh@fast,opus:max", effort_override="high")
        self.assertEqual([(s.model, s.effort, s.effort_preference, s.fast) for s in seats],
                         [(head("astra"), "high", [], False), (head("luna"), "high", [], True), (head("opus"), "high", [], False)])
        self.assertEqual((seats[0].seat_name, seats[0].preference), ("astra|sol", [(head("sol"), "sol")]))
        # checkpoint F3 round 2, P2: the grammar tolerates spaces around `|` and before @fast; the whole segment still goes
        seats = b.parse_seats(None, "astra | sol : ultra | max, luna:max | xhigh @fast", effort_override="high")
        self.assertEqual([(s.model, s.effort, s.effort_preference, s.fast) for s in seats], [(head("astra"), "high", [], False), (head("luna"), "high", [], True)])
        self.assertEqual(seats[0].preference, [(head("sol"), "sol")])

    # ---------------------------------------------------------------- root confinement, capture scopes, drift, outcomes
    def _clone(self, name):
        clone = self.tmp / name
        fixture_git(self.tmp, self.tmp / "git-home", "clone", "-q", str(self.repo), str(clone))
        run = lambda *a: fixture_git(clone, self.tmp / "git-home", *a)
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        return clone, run

    def test_bridge_root_confines_the_effective_cwd_and_the_capture(self):
        clone, run = self._clone("rooted")
        (clone / "sub").mkdir()
        (clone / "sub" / "a.py").write_text("A = 1\n")
        run("add", "-A")
        run("commit", "-q", "-m", "sub")
        (clone / "store.py").write_text("ROOT_CHANGE = True\n")  # outside the authorized subtree
        (clone / "sub" / "b.txt").write_text("inside the subtree\n")  # untracked, inside
        root = os.path.realpath(str(clone / "sub"))
        env = {"BRIDGE_ROOT": root}
        rc, out, err = self.run_engine("ask", "--cwd", str(clone / "sub"), "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.meta(self.job_dir(out))["cwd"], root)  # not promoted to the git top level
        rc, out, err = self.run_engine("ask", "--cwd", str(clone), "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("outside the authorized root", err)
        rc, out, err = self.run_engine("review", "--cwd", str(clone / "sub"), "--seats", "luna:low", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        patch = (d / "review.patch").read_text()
        self.assertIn("inside the subtree", patch)
        self.assertIn("+++ b/sub/b.txt", patch)  # headers are repository-relative, like files and inventory
        self.assertNotIn("ROOT_CHANGE", patch)  # the root modification is outside the subtree
        meta = json.loads((d / "review.meta.json").read_text())
        self.assertTrue(meta["subtree"])
        self.assertTrue(meta["inventory_complete"])
        self.assertIn("sub/a.py", meta["inventory"])
        self.assertIn("sub/b.txt", meta["inventory"])
        self.assertNotIn("store.py", meta["inventory"])
        self.assertEqual(meta["files"], ["sub/b.txt"])

    def test_commit_and_base_capture_and_supporting_tree_drift(self):
        clone, run = self._clone("scopes")
        (clone / "store.py").write_text("def avg(v):\n    return sum(v)/len(v)\n\ndef parse_port(s):\n    return int(s)\n")
        run("commit", "-qam", "add parse_port")
        rc, out, err = self.run_engine("review", "--cwd", str(clone), "--commit", "HEAD", "--seats", "luna:low")
        self.assertEqual(rc, 0, out + err)
        meta = json.loads((self.job_dir(out) / "review.meta.json").read_text())
        self.assertEqual(meta["kind"], "commit")
        self.assertIn("parse_port", (self.job_dir(out) / "review.patch").read_text())
        self.assertEqual(meta["files"], ["store.py"])
        rc, out, err = self.run_engine("review", "--cwd", str(clone), "--base", "HEAD~1", "--seats", "luna:low")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads((self.job_dir(out) / "review.meta.json").read_text())["kind"], "base")
        # the inventory describes the reviewed revision and keeps deleted paths: a deleted root auth.py must still compete
        (clone / "auth.py").write_text("ROOT = 1\n")
        (clone / "client").mkdir()
        (clone / "client" / "auth.py").write_text("CLIENT = 1\n")
        run("add", "-A")
        run("commit", "-qm", "two auths")
        (clone / "auth.py").unlink()
        (clone / "client" / "auth.py").write_text("CLIENT = 2\n")
        run("add", "-A")
        run("commit", "-qm", "delete root auth")
        rc, out, err = self.run_engine("review", "--cwd", str(clone), "--commit", "HEAD", "--seats", "luna:low")
        self.assertEqual(rc, 0, out + err)
        meta = json.loads((self.job_dir(out) / "review.meta.json").read_text())
        self.assertEqual(sorted(meta["files"]), ["auth.py", "client/auth.py"])
        self.assertTrue(meta["inventory_complete"])
        self.assertIn("auth.py", meta["inventory"])
        self.assertIn("client/auth.py", meta["inventory"])
        # supporting-tree drift: an already-modified file changes AGAIN while a commit-scope review runs
        helper = clone / "helper.py"
        helper.write_text("X = 1\n")
        run("add", "helper.py")
        run("commit", "-qm", "helper")
        helper.write_text("X = 2\n")  # modified before the review starts: porcelain status will not change
        env = dict(os.environ)
        env.update(self.env)
        p = subprocess.Popen([sys.executable, str(ENGINE), "review", "--cwd", str(clone), "--commit", "HEAD~1", "--seats", "luna:low",
                              "--focus", "[[STUB:sleep=3]]"], env=env, cwd=str(clone), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.2)
        helper.write_text("X = 3\n")
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("other files in the working tree changed", out)
        self.assertTrue(self.meta(self.job_dir(out)).get("drift"))
        # an UNTRACKED supporting file whose bytes change but whose size and mtime are preserved is still detected (content hash)
        notes = clone / "notes.txt"
        notes.write_text("AAAA\n")
        st0 = notes.stat()
        p = subprocess.Popen([sys.executable, str(ENGINE), "review", "--cwd", str(clone), "--commit", "HEAD~1", "--seats", "luna:low",
                              "--focus", "[[STUB:sleep=3]]"], env=env, cwd=str(clone), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.2)
        notes.write_text("BBBB\n")
        os.utime(notes, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("other files in the working tree changed", out)
        notes.unlink()

    def test_invalid_structured_output_fails_the_seat(self):
        rc, out, err = self.run_engine("review", "--seats", "luna:low,sonnet:low", "--focus", "[[STUB:badjson]]")
        self.assertEqual(rc, 1, out + err)
        d = self.job_dir(out)
        for tag in ("luna", "sonnet"):
            f = json.loads((d / f"findings-{tag}.json").read_text())
            self.assertTrue(f["_invalid"])
            self.assertFalse(f["_ok"])
            self.assertEqual(self.seat(d, tag)["state"], "failed")
        self.assertEqual(self.meta(d)["state"], "failed")

    def test_only_a_terminal_usage_limit_enters_the_ladder(self):
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", "[[STUB:limitthenfail]]")
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.job_dir(out), "astra")
        self.assertEqual(seat["attempt"], 1)
        self.assertEqual(seat.get("limit_hits", 0), 0)
        self.assertIn("permission denied", seat["activity"])

    def test_recovered_json_error_does_not_hide_a_terminal_stderr_limit(self):
        counter = self.tmp / "recovered-then-limit"
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", f"[[STUB:recoveredthenlimit={counter}]]")
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.job_dir(out), "astra")
        self.assertEqual(seat["attempt"], 2)  # the stderr-only terminal limit entered the ladder despite the earlier recovered error
        self.assertEqual(seat["limit_hits"], 1)

    def test_result_exit_code_follows_the_persisted_outcome(self):
        rc, out, err = self.run_engine("council", "--seats", "luna:low,sonnet:low", "--job-dir", "outcome-partial", "--", "[[STUB:codexfail]] q",
                                       env_extra={"BRIDGE_LIMIT_LADDER": "0"})
        self.assertEqual(rc, 3)
        rc, out, err = self.run_engine("result", "outcome-partial")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("state: partial", out)
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "outcome-failed", "--", "[[STUB:fail]]", env_extra={"BRIDGE_LIMIT_LADDER": "0"})
        self.assertEqual(rc, 1)
        rc, out, err = self.run_engine("result", "outcome-failed")
        self.assertEqual(rc, 1, out + err)
        rc, out, err = self.run_engine("result", "reply-job")
        self.assertEqual(rc, 0, out + err)

    def test_fanout_rejects_a_bad_spec_before_creating_a_job(self):
        before = {p.name for p in self.jobs.iterdir()} if self.jobs.exists() else set()
        spec = self.tmp / "bad-spec.json"
        spec.write_text(json.dumps({"tasks": {"broken": ""}}))
        rc, out, err = self.run_engine("fanout", "--spec", str(spec))
        self.assertEqual(rc, 2, out + err)
        self.assertIn("has no prompt", err)
        after = {p.name for p in self.jobs.iterdir()}
        self.assertEqual(before, after)

    def test_global_shutdown_kills_a_group_whose_leader_already_exited(self):
        b = load_engine_module(self.env)
        # without a recorded leader identity the leaderless group is NOT signalled (fail closed against PID reuse)
        p0 = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 0.5; exit 0"], start_new_session=True)
        p0._bridge_pgid = p0.pid
        p0.wait(timeout=10)
        b._register("unverified", p0)
        try:
            b._kill_all_children()
        finally:
            b._unregister("unverified")
        self.assertTrue(b._group_alive(p0.pid))
        os.killpg(p0.pid, signal.SIGKILL)
        # with the identity _start() records, the verified leaderless group is killed
        p = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 0.5; exit 0"], start_new_session=True)
        pgid = p.pid
        p._bridge_pgid, p._bridge_start = pgid, b.proc_start(p.pid)
        p.wait(timeout=10)  # the leader is gone, its sleeping child survives in the group
        b._note_exit(p)
        self.assertTrue(b._group_alive(pgid))
        b._register("orphan", p)
        try:
            b._kill_all_children()
        finally:
            b._unregister("orphan")
        for _ in range(30):
            if not b._group_alive(pgid):
                break
            time.sleep(0.1)
        self.assertFalse(b._group_alive(pgid))

    def test_emit_respects_a_small_stdout_budget(self):
        import contextlib, io
        b = load_engine_module(dict(self.env, BRIDGE_MAX_STDOUT="400"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.emit("x" * 5000, Path("/tmp/full.md"), footer="\n— job: abc")
        self.assertLessEqual(len(buf.getvalue()), 400)
        self.assertIn("[output truncated", buf.getvalue())
        for budget in ("30", "1"):  # smaller than the framing itself: the cap still holds
            b = load_engine_module(dict(self.env, BRIDGE_MAX_STDOUT=budget))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                b.emit("x" * 500, Path("/tmp/full.md"), footer="\n— job: abc · logs: /very/long/path/to/the/job/directory")
            self.assertLessEqual(len(buf.getvalue()), int(budget))

    def test_reply_respects_bridge_root_and_clears_active_replies(self):
        clone, run = self._clone("reply-root")
        (clone / "sub").mkdir()
        rc, out, err = self.run_engine("ask", "--cwd", str(clone), "--seats", "luna:low", "--job-dir", "reply-root-job", "--", "q")
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_engine("reply", "reply-root-job", "--", "more", env_extra={"BRIDGE_ROOT": os.path.realpath(str(clone / "sub"))})
        self.assertEqual(rc, 2, out + err)
        self.assertIn("outside the authorized root", err)
        rc, out, err = self.run_engine("reply", "reply-root-job", "--", "more")
        self.assertEqual(rc, 0, out + err)
        m = self.meta(self.jobs / "reply-root-job")
        self.assertEqual(m["active_replies"], {})
        self.assertEqual(m["state"], "done")

    def test_status_marks_an_orphaned_created_job_stale(self):
        b = load_engine_module(self.env)
        jid, d = b.new_job("ask", "created-orphan")
        b.update_meta(d, supervisor_pid=2147480000)  # no such process
        rc, out, err = self.run_engine("status", "created-orphan")
        self.assertEqual(rc, 0, err)
        self.assertIn("state=stale", out)

    def test_aggregate_state_keeps_merge_failure_and_supervised_seats_live(self):
        b = load_engine_module(self.env)
        jid, d = b.new_job("review", "agg-merge-failed")
        # changed for DESIGN §1: the seat record names the Luna head from the table, not a retired ID
        b.update_seat(d, "luna", state="done", vendor="codex", model=head("luna"), effort="low")
        b.update_meta(d, merge_failed=True)
        self.assertEqual(b._aggregate_state(d), "partial")
        me = {"state": "running", "pid": 2147480000, "pid_start": "x", "supervisor_pid": os.getpid(), "supervisor_start": b.proc_start(os.getpid())}
        self.assertTrue(b.seat_supervised_live(me))  # child gone, supervisor (this process) verified alive
        self.assertFalse(b.seat_supervised_live(dict(me, supervisor_start="Mon Jan  1 00:00:00 2001")))
        self.assertFalse(b.seat_supervised_live(dict(me, state="done")))

    def test_group_verification_by_member_start_times(self):
        b = load_engine_module(self.env)
        p = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 0.5; exit 0"], start_new_session=True)
        start = b.proc_start(p.pid)
        self.assertTrue(start)
        pgid = p.pid
        p.wait(timeout=10)
        exit_seen = time.time()
        self.assertTrue(b._group_alive(pgid))
        self.assertTrue(b._group_verified(pgid, start, exit_seen))
        self.assertFalse(b._group_verified(pgid, "Mon Jan  1 00:00:00 2099", exit_seen))  # members older than the claimed leader: not ours
        self.assertFalse(b._group_verified(pgid, start, time.mktime(time.strptime(start.strip(), "%a %b %d %H:%M:%S %Y")) - 120))  # members newer than the bound: not ours
        self.assertFalse(b._group_verified(pgid, start, None))  # no upper bound: fail closed
        self.assertFalse(b._group_verified(pgid, None, exit_seen))
        p._bridge_pgid, p._bridge_start, p._bridge_exit_observed = pgid, start, exit_seen
        b.kill_group(p, grace=0.5)
        for _ in range(30):
            if not b._group_alive(pgid):
                break
            time.sleep(0.1)
        self.assertFalse(b._group_alive(pgid))
        # a descendant snapshot is only taken from a root whose identity matches the recorded leader
        q = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 5; exit 0"], start_new_session=True)
        try:
            qstart = b.proc_start(q.pid)
            time.sleep(0.3)
            self.assertTrue(any(pid for pid, _, _ in b._descendants(q.pid, qstart)))
            self.assertEqual(b._descendants(q.pid, "Mon Jan  1 00:00:00 2099"), [])  # wrong leader identity: nothing claimed
            self.assertEqual(b._descendants(q.pid, None), [])
        finally:
            os.killpg(q.pid, signal.SIGKILL)

    def test_descendant_sweep_refreshes_identity_per_entry_and_signals_a_group_once(self):
        b = load_engine_module(self.env)
        calls = []
        identities = {101: (900, "Mon Sep  8 06:00:00 2026"), 102: None, 103: (901, "Mon Sep  8 06:00:02 2026")}
        real_killpg, real_kill, real_identity, real_start = os.killpg, os.kill, b._proc_identity, b.proc_start
        try:
            b._proc_identity = lambda pid: identities.get(pid)
            b.proc_start = lambda pid: (identities.get(pid) or (None, None))[1]
            os.killpg = lambda pgid, sig: calls.append(("pg", pgid, sig))
            os.kill = lambda pid, sig: calls.append(("pid", pid, sig))
            # 101 and 102 were recorded in group 900; 102 vanished after an earlier signal; 103 moved from 900 to 901
            desc = [(101, 900, "Mon Sep  8 06:00:00 2026"), (102, 900, "Mon Sep  8 06:00:01 2026"), (103, 900, "Mon Sep  8 06:00:02 2026")]
            b._kill_descendants(desc)
        finally:
            os.killpg, os.kill, b._proc_identity, b.proc_start = real_killpg, real_kill, real_identity, real_start
        term_round = [c for c in calls if c[2] == signal.SIGTERM]
        self.assertEqual(term_round.count(("pg", 900, signal.SIGTERM)), 1)  # group 900 signalled once, not once per member
        self.assertIn(("pid", 103, signal.SIGTERM), term_round)  # the mover is signalled alone, never its old or new group
        self.assertNotIn(("pg", 901, signal.SIGTERM), term_round)
        self.assertFalse(any(c[1] == 102 for c in calls))  # the vanished entry is never touched

    def test_global_shutdown_escalates_on_a_group_whose_leader_dies_during_the_grace(self):
        b = load_engine_module(self.env)
        # the leader dies on TERM; its in-group child ignores TERM and must be reached by the KILL round
        p = subprocess.Popen(["sh", "-c", "(trap '' TERM; sleep 60) & sleep 30"], start_new_session=True)
        p._bridge_pgid, p._bridge_start = p.pid, b.proc_start(p.pid)
        time.sleep(0.5)
        b._register("grace", p)
        try:
            b._kill_all_children()
        finally:
            b._unregister("grace")
        for _ in range(50):
            if not b._group_alive(p.pid):
                break
            time.sleep(0.1)
        self.assertFalse(b._group_alive(p.pid))

    def test_fanout_with_schema_merges_and_result_replays_it(self):
        spec = self.tmp / "spec.json"
        spec.write_text(json.dumps({"seats": "luna:low", "schema": True, "tasks": {"T one": "look at parse_port", "T two": "look at avg"}}))
        rc, out, err = self.run_engine("fanout", "--spec", str(spec))
        self.assertEqual(rc, 0, err)
        d = self.job_dir(out)
        self.assertTrue((d / "findings-T-one.json").exists() and (d / "findings-T-two.json").exists())
        rc, out2, err = self.run_engine("result", d.name)
        self.assertEqual(rc, 0)
        self.assertIn("Stub finding", out2)

    def test_status_and_monitor_once(self):
        rc, out, err = self.run_engine("status")
        self.assertEqual(rc, 0)
        rc, out, err = self.run_engine("status", "--all")  # the bare listing shows only the newest 15 of the suite's jobs
        self.assertEqual(rc, 0)
        self.assertIn("reply-job", out)
        rc, out, err = self.run_engine("monitor", "reply-job", "--once")
        self.assertEqual(rc, 0)
        self.assertIn("model@effort", out)

    def test_doctor_reports_stubs(self):
        # changed for DESIGN §3/§8: the stubs print parseable versions (STUB_CODEX_VERSION / STUB_CLAUDE_VERSION)
        rc, out, err = self.run_engine("doctor", env_extra={"STUB_CODEX_VERSION": "codex-cli 99.0.0",
                                                            "STUB_CLAUDE_VERSION": "99.0.0 (Claude Code)"})
        self.assertEqual(rc, 0, out + err)
        self.assertIn("codex-cli 99.0.0", out)
        self.assertIn("99.0.0 (Claude Code)", out)
        self.assertIn("usage-limit ladder: on", out)

    # ---------------------------------------------------------------- relay-routed seats
    def test_unrouted_answers_run_the_seat_exactly_as_before(self):
        answers = {"exit2": {"exit": 2}, "nonjson": {"exit": 0, "raw": "f01\n"}, "v2": {"exit": 0, "json": {"v": 2, "result": "ok", "label": "a"}},
                   "noaccounts": {"exit": 3}}
        for name, ans in answers.items():
            with self.subTest(answer=name):
                env, rd = self.relay(f"unrouted-{name}", {"pick": {"codex": [ans]}})
                rec = rd / "argv.jsonl"
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.job_dir(out)
                self.assertEqual(self.meta(d)["router"], {"codex": "unrouted"})
                self.assertIsNone(self.seat(d, "luna")["account"])
                self.assertNotIn("account=", out)
                self.assertIsNone(self.read_records(rec)[0]["account"])
                self.assertEqual(self.records(rd, "execs.jsonl"), [])
                # changed for docs/model-routing.md 3.3 (F3): relay's account listing comes first; a relay with no Codex account
                # spares the pick, every other answer is asked once and, unusable for a confined pick, validates this machine's login
                self.assertEqual(len(self.picks(rd)), 0 if name == "noaccounts" else 1)

    def test_routed_codex_launch_shows_the_account(self):
        env, rd = self.relay("launch", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rec = rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "routed-launch", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        self.assertIn(f"luna={head('luna')}@low", out)  # changed for DESIGN §1: the label names the head that ran
        self.assertIn(" account=a thread=stub-thread-01a0", out)
        seat = self.seat(d, "luna")
        self.assertEqual((seat["account"], seat["account_email"], seat["account_plan"]), ("a", "a@example.test", "pro"))
        self.assertEqual(self.meta(d)["router"], {"codex": "routed"})
        self.assertEqual(self.read_records(rec)[0]["account"], "a")
        self.assertEqual(self.read_records(d / "events-luna.jsonl")[0]["account"], "a")
        # changed for DESIGN §1/§3: relay receives the family alias (Seat.relay_model()), the CLI the full ID; and for
        # docs/model-routing.md 3.3 (F3): the pick is confined to the capable set (`--only`), here relay's one account
        self.assertEqual(self.picks(rd), [["pick", "--vendor", "codex", "--json", "--model", "luna", "--only", "a"]])
        execs = self.records(rd, "execs.jsonl")
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["argv"], ["exec", "--vendor", "codex", "--account", "a", "--model", "luna"])  # no --force: relay's ceilings apply
        self.assertEqual(execs[0]["command"][:2], [str(STUB_CODEX), "exec"])
        self.assertEqual(execs[0]["command"][execs[0]["command"].index("-m") + 1], head("luna"))
        self.assertEqual(self.records(rd, "hits.jsonl"), [])
        trail = self.read_records(d / "router-luna.jsonl")
        self.assertEqual((trail[0]["args"][:2], trail[0]["rc"]), (["pick", "--vendor"], 0))
        rc, out, err = self.run_engine("status", "routed-launch", env_extra=env)
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"(?m)^seat\s+model@effort\s+account\s+state")
        self.assertRegex(out, r"(?m)^luna\s+" + re.escape(head("luna")) + r"@low\s+a\s+done")  # changed for DESIGN §1

    def test_routed_codex_limit_moves_to_the_next_account_with_the_original_prompt(self):
        env, rd = self.relay("failover", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rec, counter = rd / "argv.jsonl", rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:limit={counter},argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        self.assertIn(" account=b ", out)
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: alias to relay
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        recs = self.read_records(rec)
        self.assertEqual([r["account"] for r in recs], ["a", "b"])
        self.assertIsNone(recs[1]["resume"])
        self.assertIn("[[STUB:limit=", recs[1]["prompt"])  # the original prompt again, not a continuation
        seat = self.seat(d, "luna")
        self.assertEqual((seat["account"], seat["attempt"], seat["limit_hits"], seat["state"]), ("b", 2, 1, "done"))
        self.assertTrue((d / "events-luna.attempt1.jsonl").exists())
        self.assertEqual(len(self.picks(rd)), 2)

    def test_routed_claude_limit_after_work_transplants_and_resumes(self):
        env, rd = self.relay("claude-failover", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rec, counter = rd / "argv.jsonl", rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:limit={counter},argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "claude", "model": "sonnet"}])
        self.assertEqual(self.records(rd, "transplants.jsonl"), [{"session": "stub-session-aaaa", "to": "b", "cwd": self.meta(d)["cwd"]}])
        recs = self.read_records(rec)
        self.assertEqual([r["account"] for r in recs], ["a"])  # the resumed turn carries the continuation prompt, not the directive
        self.assertNotIn("--resume", recs[0]["argv"])
        execs = self.records(rd, "execs.jsonl")
        self.assertEqual([e["argv"] for e in execs], [["exec", "--vendor", "claude", "--account", label, "--model", "sonnet"] for label in ("a", "b")])
        self.assertEqual(execs[1]["command"][execs[1]["command"].index("--resume") + 1], "stub-session-aaaa")
        init = self.read_records(d / "events-sonnet.jsonl")[0]
        self.assertEqual((init["account"], init["session_id"]), ("b", "stub-session-stub-session-aaaa"))  # resumed, under b
        seat = self.seat(d, "sonnet")
        self.assertEqual((seat["account"], seat["session_id"], seat["attempt"]), ("b", "stub-session-stub-session-aaaa", 2))
        self.assertIn(" account=b session=stub-session-stub-session-aaaa", out)
        # before any tool ran, the seat is simply restarted on the next account: no transplant, no --resume
        env, rd = self.relay("claude-restart", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rec, counter = rd / "argv.jsonl", rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:limitbeforework={counter},argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.records(rd, "transplants.jsonl"), [])
        recs = self.read_records(rec)
        self.assertEqual([r["account"] for r in recs], ["a", "b"])
        self.assertNotIn("--resume", recs[1]["argv"])
        self.assertIn("limitbeforework", recs[1]["prompt"])

    def test_writable_codex_seat_fails_explicitly_on_a_limit_after_work_began(self):
        blocked = time.time() + 6 * 3600
        env, rd = self.relay("writable", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "blocked_until": blocked})
        counter = rd / "law-counter"
        rc, out, err = self.run_engine("task", "--write", "--seats", "luna:low", "--", f"[[STUB:limitafterwork={counter}]] edit", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.job_dir(out)
        seat = self.seat(d, "luna")
        self.assertEqual(seat["state"], "failed")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(blocked))
        self.assertIn(f"usage limit on account a after work began (thread stub-thread-01a0); rerun manually or wait for its reset at {when}", seat["activity"])
        self.assertEqual((seat["account"], seat["thread_id"], seat["attempt"], seat["limit_hits"]), ("a", "stub-thread-01a0", 1, 1))
        self.assertIn("FAILED (usage limit on account a after work began", out)
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: alias to relay
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])  # never restarted elsewhere
        # the same limit on a READ-ONLY seat that had begun work moves on: a fresh thread on the next account, original prompt
        env, rd = self.relay("readonly-law", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        counter, rec = rd / "law-counter", rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:limitafterwork={counter},argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        recs = self.read_records(rec)
        self.assertEqual(len(recs), 2)
        self.assertIsNone(recs[1]["resume"])
        self.assertIn("limitafterwork", recs[1]["prompt"])
        self.assertEqual(self.seat(self.job_dir(out), "luna")["account"], "b")

    def test_routed_pick_waits_for_a_near_wake_time(self):
        wake = time.time() + 3
        env, rd = self.relay("wait", {"pick": {"codex": [{"exit": 1, "wake_at": wake}, {"exit": 0, "label": "a"}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "routed-wait", "--", "q")
        d = self.jobs / "routed-wait"
        seat = self.wait_for(d, "luna", lambda s: s.get("state") == "waiting")
        self.assertAlmostEqual(seat["waiting_until"], wake, delta=1.0)
        self.assertIn("every codex account is out", seat["activity"])
        rc, out, err = self.run_engine("status", "routed-wait", env_extra=env)
        self.assertEqual(rc, 0, err)
        self.assertIn("waiting", out)
        self.assertNotIn("stale", out)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn(" account=a ", out)
        self.assertEqual(self.seat(d, "luna")["state"], "done")
        self.assertEqual(len(self.picks(rd)), 2)

    def test_routed_pick_fails_when_every_account_is_out_for_too_long(self):
        wake = time.time() + 100000
        env, rd = self.relay("far", {"pick": {"codex": [{"exit": 1, "wake_at": wake}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.job_dir(out)
        self.assertIn("every codex account is out until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(wake)), self.seat(d, "luna")["activity"])
        self.assertIn("FAILED (every codex account is out until", out)
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        self.assertEqual(self.meta(d)["state"], "failed")
        # the cap is BRIDGE_ROUTED_MAX_WAIT: a wake time 30 s away is too far when the cap is 1 s
        env, rd = self.relay("far-cap", {"pick": {"codex": [{"exit": 1, "wake_at": time.time() + 30}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_ROUTED_MAX_WAIT="1"))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("every codex account is out until", out)
        # and a seat whose own --timeout cannot cover the wait fails at once instead of waiting past its deadline
        env, rd = self.relay("far-clock", {"pick": {"codex": [{"exit": 1, "wake_at": time.time() + 30}]}})
        t0 = time.time()
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--timeout", "5", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertLess(time.time() - t0, 5)
        self.assertIn("every codex account is out until", out)

    # relay can hold back an account that is not out: one whose usage reads have kept failing for an hour. Its none answer then
    # has a null top-level reason, says why per account in `rows`, and gives a wake time that is only "ask again in a minute".
    # In October 2026 such a seat said "every codex account is out" and failed "until" that minute; relay's reason reached
    # only the router trail, cut at 300 characters.
    def test_routed_wait_shows_relays_reason_for_each_account(self):
        wake = time.time() + 3
        env, rd = self.relay("held-back", {"pick": {"codex": [
            {"exit": 1, "wake_at": wake, "reason": None, "rows": [{"label": "a", "reason": RELAY_UNREAD_REASON, "wake_at": wake}]},
            {"exit": 0, "label": "a"}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "routed-held-back", "--", "q")
        d = self.jobs / "routed-held-back"
        seat = self.wait_for(d, "luna", lambda s: s.get("state") == "waiting")
        self.assertTrue(seat["activity"].startswith("relay: a: its usage could not be read for 1h01m"), seat["activity"])
        self.assertNotIn("account is out", seat["activity"])
        rc, out, err = self.run_engine("status", "routed-held-back", env_extra=env)
        self.assertEqual(rc, 0, err)
        self.assertIn("relay: a: its usage could not be read", out)  # status keeps 60 characters of the activity: relay's words lead
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn(" account=a ", out)

    def test_routed_failure_gives_relays_reason_and_its_ask_again_time_not_a_reset(self):
        wake = time.time() + 60
        env, rd = self.relay("held-back-far", {"pick": {"codex": [
            {"exit": 1, "wake_at": wake, "reason": None, "rows": [{"label": "a", "reason": RELAY_UNREAD_REASON, "wake_at": wake}]}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_ROUTED_MAX_WAIT="1"))
        self.assertEqual(rc, 1, out + err)
        d = self.job_dir(out)
        activity = self.seat(d, "luna")["activity"]
        self.assertTrue(activity.startswith("failed: relay: a: " + RELAY_UNREAD_REASON), activity)
        self.assertIn("relay says to ask again at " + time.strftime("%Y-%m-%d %H:%M", time.localtime(wake)), activity)
        self.assertNotIn("account is out", activity)
        self.assertIn("FAILED (relay: a: its usage could not be read", out)  # the footer keeps 60 characters: relay's words lead
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        trail = self.read_records(d / "router-luna.jsonl")  # relay's whole answer, the end of its reason included
        self.assertIn("no new work is routed on numbers that old", trail[0]["out"])

    def test_cancel_interrupts_a_routed_wait(self):
        env, rd = self.relay("cancel-wait", {"pick": {"codex": [{"exit": 1, "wake_at": time.time() + 60}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "routed-cancel-wait", "--", "q")
        d = self.jobs / "routed-cancel-wait"
        self.wait_for(d, "luna", lambda s: s.get("state") == "waiting")
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "routed-cancel-wait", env_extra=env)
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 15)  # the wait was interrupted, not served
        self.assertEqual(self.meta(d)["state"], "cancelled")
        self.assertEqual(self.seat(d, "luna")["state"], "cancelled")
        self.assertEqual(self.records(rd, "execs.jsonl"), [])

    def test_cancel_during_the_pick_launches_nothing(self):
        env, rd = self.relay("cancel-pick", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 4}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "routed-cancel-pick", "--", "q")
        d = self.jobs / "routed-cancel-pick"
        self.wait_for(d, "luna", lambda s: "asking relay" in s.get("activity", ""))
        rc, out, err = self.run_engine("cancel", "routed-cancel-pick", env_extra=env)
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        self.assertEqual(self.seat(d, "luna")["state"], "cancelled")
        self.assertEqual(self.meta(d)["state"], "cancelled")

    def test_cancel_kills_a_routed_attempt_and_leaves_nothing_behind(self):
        env, rd = self.relay("cancel-run", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "routed-cancel-run", "--", "[[STUB:hang]]")
        d = self.jobs / "routed-cancel-run"
        seat = self.wait_for(d, "luna", lambda s: s.get("pid"))
        self.assertTrue(self._pid_alive(seat["pid"]))
        self.assertEqual(seat["pgid"], seat["pid"])
        self.assertEqual(seat["account"], "a")
        for _ in range(50):  # relay execs into the CLI: the recorded pid becomes the codex process itself
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(seat["pid"])], capture_output=True, text=True).stdout
            if "stub_codex.py" in cmd:
                break
            time.sleep(0.1)
        self.assertIn("stub_codex.py", cmd)
        # changed for DESIGN §8 (suite isolation): relay's own command line already names stub_codex.py, so the loop above
        # can end before the stub relay has written its exec record; the isolated environment shifted the engine's timing
        # into that window, so wait for the record
        for _ in range(50):
            if self.records(rd, "execs.jsonl"):
                break
            time.sleep(0.1)
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 1)
        rc, out, err = self.run_engine("cancel", "routed-cancel-run", env_extra=env)
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertFalse(self._pid_alive(seat["pid"]))
        self.assertEqual(self.seat(d, "luna")["state"], "cancelled")
        self.assertEqual(self.meta(d)["state"], "cancelled")
        self.assertEqual(self._procs_matching(str(d)), [])  # neither relay nor the exec'd CLI survives

    def test_a_hanging_relay_fails_the_seat_cleanly(self):
        env, rd = self.relay("hang", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 30}]}})
        t0 = time.time()
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "routed-hang", "--timeout", "2", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertLess(time.time() - t0, 15)
        d = self.jobs / "routed-hang"
        seat = self.seat(d, "luna")
        self.assertEqual(seat["state"], "failed")
        self.assertIn("relay pick did not answer within", seat["activity"])
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        trail = self.read_records(d / "router-luna.jsonl")
        self.assertEqual((trail[0]["args"][:3], trail[0]["rc"]), (["pick", "--vendor", "codex"], None))
        for _ in range(30):
            if not self._procs_matching("stub_relay.py pick"):
                break
            time.sleep(0.1)
        self.assertEqual(self._procs_matching("stub_relay.py pick"), [])  # the hung helper was killed, not abandoned

    def test_the_same_label_after_a_hit_is_retried_then_given_up(self):
        env, rd = self.relay("repeat-ok", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        counter = rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:limit={counter}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.job_dir(out), "luna")
        self.assertEqual((seat["account"], seat["attempt"], seat["limit_hits"]), ("b", 2, 1))
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])  # `a` was offered again but never re-run
        self.assertEqual(len(self.picks(rd)), 3)
        env, rd = self.relay("repeat-fail", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "[[STUB:alwayslimit]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("relay keeps offering account a after its usage limit was reported", self.seat(self.job_dir(out), "luna")["activity"])
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: alias to relay
        self.assertEqual(len(self.picks(rd)), 4)  # the launch pick, then `a` offered three more times
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 1)

    def test_startup_model_rejection_re_picks_within_the_capable_set_then_descends_by_the_preference_list(self):
        # changed for DESIGN §1/§3/§4 and docs/model-routing.md 3.3 and 5 (F3): relay is asked with family aliases; the rejecting
        # account leaves the capable set and relay is asked again for the same model, confined to the rest (`--only`); only once no
        # account is left does the seat descend, through its `|` list alone, relay then asked with the new alias; the CLI error leads
        astra, sol = head("astra"), head("sol")
        env, rd = self.relay("fallback", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--", "[[STUB:badmodel]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        picks = self.picks(rd)
        self.assertEqual([(p[p.index("--model") + 1], p[p.index("--only") + 1]) for p in picks], [("astra", "a,b"), ("astra", "b"), ("sol", "a,b")])
        execs = self.records(rd, "execs.jsonl")
        self.assertEqual([(e["label"], e["argv"][e["argv"].index("--model") + 1]) for e in execs], [("a", "astra"), ("b", "astra"), ("b", "sol")])
        self.assertEqual([e["command"][e["command"].index("-m") + 1] for e in execs], [astra, astra, sol])
        seat = self.seat(self.job_dir(out), "astra")
        self.assertEqual((seat["model"], seat["account"], seat["attempt"]), (sol, "b", 3))
        lead = leading_notices(out)
        self.assertTrue(lead and f"model {astra} is not supported" in lead[0], out)
        # without a preference list the seat ends once every capable account rejected its model: nothing else is asked or launched
        env, rd = self.relay("fallback-none", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", "[[STUB:badmodel]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual([p[p.index("--model") + 1] for p in self.picks(rd)], ["astra", "astra"])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        seat = self.seat(self.job_dir(out), "astra")
        self.assertEqual((seat["model"], seat["state"]), (astra, "failed"))
        self.assertIn("the seat has no preference list, so it does not descend", seat["activity"])

    def test_codex_reply_is_pinned_to_the_recorded_account(self):
        env, rd = self.relay("reply-pin", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "routed-reply", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rec = rd / "reply-argv.jsonl"
        rc, out, err = self.run_engine("reply", "routed-reply", "--", f"[[STUB:argv={rec}]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])  # relay would now say b; the thread lives under a
        self.assertEqual(self.read_records(rec)[0]["resume"], "stub-thread-01a0")
        self.assertEqual(len(self.picks(rd)), 1)  # the reply never asked
        self.assertIn(" account=a ", out)
        # when the recorded account verifies but cannot take work, the reply fails with relay's reason, not by moving the thread
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "b"}]},
                                                    "accounts": {"a": {"email": "a@example.test"}}, "refuse": {"a": 1790000000}}))
        rc, out, err = self.run_engine("reply", "routed-reply", "--", "again", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        # changed for ruling BUSY-1 (A6): the stub relay refuses in relay's real format, `...: <reason>; wake_at=<int>`
        self.assertIn("account a cannot take work now: weekly window at 99% (worker ceiling 99%); wake_at=1790000000; this thread lives there", out)
        self.assertEqual(self.records(rd, "hits.jsonl"), [])  # relay already knows; nothing to report
        self.assertEqual(len(self.picks(rd)), 1)

    def test_claude_reply_moves_the_session_to_the_routed_account(self):
        env, rd = self.relay("claude-reply", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "routed-claude-reply", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "routed-claude-reply"
        self.assertEqual(self.seat(d, "sonnet")["account"], "a")
        rec = rd / "reply-argv.jsonl"
        rc, out, err = self.run_engine("reply", "routed-claude-reply", "--", f"[[STUB:argv={rec}]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.records(rd, "transplants.jsonl"), [{"session": "stub-session-aaaa", "to": "b", "cwd": self.meta(d)["cwd"]}])
        r = self.read_records(rec)[0]
        self.assertEqual(r["account"], "b")
        self.assertEqual(r["argv"][r["argv"].index("--resume") + 1], "stub-session-aaaa")
        seat = self.seat(d, "sonnet")
        self.assertEqual((seat["account"], seat["session_id"]), ("b", "stub-session-stub-session-aaaa"))  # the session now lives under b
        # a failed transplant fails the reply explicitly instead of resuming a session that is not there
        (rd / "script.json").write_text(json.dumps({"pick": {"claude": [{"exit": 0, "label": "c"}]}, "transplant_exit": 1}))
        rc, out, err = self.run_engine("reply", "routed-claude-reply", "--", "again", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("could not carry session stub-session-stub-session-aaaa to account c", out)

    def test_router_off_never_calls_relay(self):
        env, rd = self.relay("off", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env["BRIDGE_ROUTER"] = "off"
        rec = rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        self.assertFalse((rd / "calls.jsonl").exists())
        self.assertEqual(self.meta(d)["router"], {"codex": "off"})
        self.assertIsNone(self.read_records(rec)[0]["account"])
        self.assertNotIn("account=", out)
        rc, out, err = self.run_engine("doctor", env_extra=env)
        self.assertEqual(rc, 0, err)
        self.assertIn("router: off (BRIDGE_ROUTER=off)", out)
        self.assertFalse((rd / "calls.jsonl").exists())

    def test_doctor_reports_the_router_and_what_pick_answers(self):
        # changed for DESIGN §5 (doctor): the relay pick probes are removed; doctor states that the CLI decides availability
        # at launch and never runs relay, whatever relay would answer. The plugin version is read from plugin.json.
        for name, script in (("doctor", {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 3}]}}),
                             ("doctor-none", {"pick": {"codex": [{"exit": 1, "wake_at": time.time() + 600}], "claude": [{"exit": 2}]}})):
            with self.subTest(relay=name):
                env, rd = self.relay(name, script)
                rc, out, err = self.run_engine("doctor", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("bridge: " + json.loads(PLUGIN_JSON.read_text())["version"], out)
                self.assertIn("routed accounts: availability is decided by the CLI at launch; doctor does not consult relay", out)
                self.assertNotIn("pick answers", out)
                self.assertIn("unrouted seats only", out)
                self.assertFalse((rd / "calls.jsonl").exists(), "doctor ran relay")

    # ------------------------------------------------- relay-routed seats: vendors, stale wake times, pinning, cleanup
    def test_the_two_vendors_route_independently_in_one_job(self):
        """Codex and Claude are separate namespaces: relay having no Claude accounts must not unroute the Codex seat."""
        env, rd = self.relay("two-vendors", {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 3}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low,sonnet:low", "--job-dir", "two-vendors", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "two-vendors"
        self.assertEqual(self.meta(d)["router"], {"codex": "routed", "claude": "unrouted"})
        self.assertEqual(self.seat(d, "luna")["account"], "a")
        self.assertIsNone(self.seat(d, "sonnet")["account"])
        self.assertEqual(self.read_records(d / "events-luna.jsonl")[0]["account"], "a")
        self.assertIsNone(self.read_records(d / "events-sonnet.jsonl")[0]["account"])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])  # the Claude seat never went through relay
        self.assertEqual(sorted(p[p.index("--vendor") + 1] for p in self.picks(rd)), ["claude", "codex"])  # one pick per vendor
        # changed for DESIGN §1: labels and the model column name the heads (Claude seats get full IDs too)
        self.assertRegex(out, r"(?m)^— luna=" + re.escape(head("luna")) + r"@low .*account=a ")
        self.assertNotRegex(out, r"(?m)^— sonnet=.*account=")
        rc, out, err = self.run_engine("monitor", "two-vendors", "--once", env_extra=env)
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"(?m)^luna\s+" + re.escape(head("luna")) + r"@low\s+a\s+done")
        self.assertRegex(out, r"(?m)^sonnet\s+" + re.escape(head("sonnet")) + r"@low\s+—\s+done")

    def test_two_routed_codex_seats_take_one_account_each(self):
        """Parallel seats each get their own pick and their concurrent writes leave one consistent meta.json."""
        env, rd = self.relay("parallel", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low,sol:low", "--job-dir", "two-codex-seats", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "two-codex-seats"
        self.assertEqual(sorted([self.seat(d, "luna")["account"], self.seat(d, "sol")["account"]]), ["a", "b"])
        self.assertEqual(sorted(e["label"] for e in self.records(rd, "execs.jsonl")), ["a", "b"])
        self.assertEqual(len(self.picks(rd)), 2)
        m = self.meta(d)
        self.assertEqual((m["router"], m["state"], m["seats"]), ({"codex": "routed"}, "done", [f"{head('luna')}@low", f"{head('sol')}@low"]))  # changed for DESIGN §1

    def test_a_past_or_missing_wake_time_never_counts_as_availability(self):
        """`none` with a reset that already passed (or none at all) is not recovery: the seat re-asks twice, then fails."""
        for name, ans in (("past", {"exit": 1, "wake_at": time.time() - 3600}), ("missing", {"exit": 1})):
            with self.subTest(answer=name):
                env, rd = self.relay(f"stale-wake-{name}", {"pick": {"codex": [ans]}})
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                d = self.job_dir(out)
                self.assertIn("no codex account is available and relay gives no wake time", self.seat(d, "luna")["activity"])
                self.assertEqual(self.records(rd, "execs.jsonl"), [])  # nothing was run on an account relay says is spent
                self.assertEqual(self.records(rd, "hits.jsonl"), [])
                self.assertEqual(len(self.picks(rd)), 3)
                self.assertEqual(self.meta(d)["state"], "failed")

    def test_a_pinned_codex_reply_fails_on_the_limit_of_its_own_account(self):
        blocked = time.time() + 5 * 3600
        env, rd = self.relay("reply-limit", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "blocked_until": blocked})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "reply-limit", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-limit"
        rc, out, err = self.run_engine("reply", "reply-limit", "--", "[[STUB:alwayslimit]] more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(blocked))
        self.assertIn(f"usage limit on account a, where this thread lives; retry after its reset at {when}", out)
        self.assertIn(" account=a ", out)
        reply_seat = json.loads(sorted(d.glob("seat-luna-reply-*.json"))[0].read_text())  # the reply keeps the pinned identity
        self.assertEqual((reply_seat["account"], reply_seat["thread_id"], reply_seat["state"]), ("a", "stub-thread-01a0", "failed"))
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: relay receives the alias
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])  # never re-run on b
        self.assertEqual(len(self.picks(rd)), 1)
        seat = self.seat(d, "luna")
        self.assertEqual((seat["account"], seat["thread_id"], seat["state"]), ("a", "stub-thread-01a0", "done"))
        m = self.meta(d)
        self.assertEqual(m.get("active_replies"), {})  # the failed reply was retired: its lock and its slot are free
        self.assertIsNone(m.get("replying"))
        rc, out, err = self.run_engine("reply", "reply-limit", "--", "again", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(" account=a ", out)
        self.assertEqual(len(self.picks(rd)), 1)

    def test_a_pinned_codex_reply_refuses_to_run_when_the_router_is_gone(self):
        """BRIDGE_ROUTER=off cannot silently move a recorded thread onto this machine's own login."""
        env, rd = self.relay("reply-norouter", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "reply-norouter", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-norouter"
        rec = rd / "reply-argv.jsonl"
        rc, out, err = self.run_engine("reply", "reply-norouter", "--", f"[[STUB:argv={rec}]] more", env_extra=dict(env, BRIDGE_ROUTER="off"))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("this seat ran on relay account a, but relay is not available now", out)
        self.assertFalse(rec.exists())  # codex was never launched under the wrong sign-in
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 1)
        self.assertEqual(self.seat(d, "luna")["account"], "a")

    def test_a_routed_claude_seat_keeps_the_outer_session_out_of_the_cli(self):
        """clean_env() must survive the `relay exec` prefix: the nested claude keeps its auth and loses the outer session."""
        env, rd = self.relay("claude-env", {"pick": {"claude": [{"exit": 0, "label": "a"}]}})
        rec = rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        r = self.read_records(rec)[0]
        self.assertEqual(r["account"], "a")
        self.assertFalse(r["env_has_claudecode"])
        self.assertTrue(r["env_has_token"])

    def test_a_routed_seat_that_times_out_kills_the_cli_and_blames_no_account(self):
        env, rd = self.relay("routed-timeout", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        t0 = time.time()
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "routed-timeout", "--timeout", "2", "--", "[[STUB:hang]]",
                                       env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertLess(time.time() - t0, 25)
        d = self.jobs / "routed-timeout"
        seat = self.seat(d, "luna")
        self.assertTrue(seat["timed_out"])
        self.assertEqual((seat["state"], seat["account"]), ("failed", "a"))
        self.assertIn(" TIMED OUT", out)
        self.assertFalse(self._pid_alive(seat["pid"]))
        self.assertEqual(self.records(rd, "hits.jsonl"), [])  # a timeout is not a usage limit: the account is not blamed
        self.assertEqual(len(self.picks(rd)), 1)
        for _ in range(50):
            if not self._procs_matching(str(d)):
                break
            time.sleep(0.1)
        self.assertEqual(self._procs_matching(str(d)), [])  # neither relay nor the exec'd CLI survives

    def test_failover_continues_when_relay_hit_fails(self):
        env, rd = self.relay("hit-fails", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "hit_exit": 1})
        counter = rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:limit={counter}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        self.assertEqual(len(self.records(rd, "hits.jsonl")), 1)
        self.assertIn(" account=b ", out)

    def test_an_ok_answer_without_a_label_does_not_route_the_seat(self):
        """A v1 answer that claims ok but names no account is unusable; the seat falls back to this machine's own login."""
        env, rd = self.relay("ok-no-label", {"pick": {"codex": [{"exit": 0, "json": {"v": 1, "result": "ok", "label": None, "wake_at": None}}]}})
        rec = rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        self.assertEqual(self.meta(d)["router"], {"codex": "unrouted"})
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        self.assertIsNone(self.read_records(rec)[0]["account"])
        self.assertIsNone(self.seat(d, "luna")["account"])

    def test_a_relay_exec_refusal_moves_the_seat_without_a_redundant_hit(self):
        """`relay exec` can refuse the picked account (its ceilings moved): that is a failover, and relay already knows."""
        env, rd = self.relay("refused", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "refuse": {"a": 1790000000}})
        rec = rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir(out)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        self.assertEqual(self.records(rd, "hits.jsonl"), [])  # the refusal came from relay itself: nothing to report back
        self.assertEqual([r["account"] for r in self.read_records(rec)], ["b"])  # codex never started under a
        seat = self.seat(d, "luna")
        self.assertEqual((seat["account"], seat["attempt"], seat["limit_hits"], seat["state"]), ("b", 2, 1, "done"))

    def test_a_writable_codex_seat_moves_on_when_the_limit_precedes_any_work(self):
        """A3 stops a writable seat only after work began; a limit before the first action is a plain failover."""
        env, rd = self.relay("writable-early", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        counter, rec = rd / "limit-counter", rd / "argv.jsonl"
        rc, out, err = self.run_engine("task", "--write", "--seats", "luna:low", "--", f"[[STUB:limit={counter},argv={rec}]] edit", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: relay receives the alias
        recs = self.read_records(rec)
        self.assertEqual([r["account"] for r in recs], ["a", "b"])
        self.assertIsNone(recs[1]["resume"])  # a fresh thread with the original prompt, no side effect replayed
        self.assertIn("[[STUB:limit=", recs[1]["prompt"])
        self.assertIn(" account=b ", out)

    def test_a_busy_answer_is_awaited_like_none(self):
        """A4's `busy` (every eligible account's slots are taken) is waited out, not failed. Changed for ruling BUSY-1 (A4): relay's
        `busy` is its own answer, asked again after the busy interval rather than at its wake_at, and counted toward nothing
        (test_a_busy_pick_is_asked_again_within_the_busy_interval)."""
        env, rd = self.relay("busy", {"pick": {"codex": [{"exit": 1, "result": "busy", "wake_at": time.time() + 2}, {"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "routed-busy", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "routed-busy"
        self.assertIn(" account=a ", out)
        self.assertEqual(len(self.picks(rd)), 2)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
        seat = self.seat(d, "luna")
        self.assertEqual((seat["state"], seat["account"]), ("done", "a"))
        self.assertIsNone(seat["waiting_until"])  # the wait is cleared once the seat runs

    def test_a_routed_review_names_the_account_and_still_writes_findings(self):
        """The critic path triad drives: a routed review keeps its artifacts and records which account paid for them."""
        env, rd = self.relay("review", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--job-dir", "routed-review", "--intent", "add parse_port", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "routed-review"
        self.assertIn(" account=a ", out)
        self.assertEqual(self.meta(d)["router"], {"codex": "routed"})
        self.assertEqual(self.seat(d, "luna")["account"], "a")
        f = json.loads((d / "findings-luna.json").read_text())
        self.assertTrue(f["_ok"], f)
        self.assertEqual(len(f["findings"]), 1)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])

    def test_a_base_review_of_an_uncommitted_tree_produces_no_artifacts_and_spends_no_account(self):
        """--base compares merge-base..HEAD: uncommitted work is invisible. The caller gets no job and no findings file —
        the "no artifacts" state a caller (triad, A1) must never read as a clean review."""
        env, rd = self.relay("empty-base", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("review", "--seats", "luna:low", "--base", "HEAD", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Nothing to review", out)
        self.assertNotIn("— job:", out)  # no job dir, so no meta.json and no findings-<seat>.json exist to be mistaken for clean
        self.assertNotIn("parse_port", out)
        self.assertEqual(self.picks(rd), [])  # and no account was spent on an empty change
        self.assertEqual(self.records(rd, "execs.jsonl"), [])

    def test_a_failed_failover_keeps_the_spent_account_and_thread_in_the_record(self):
        """A seat that spends account `a` and then finds no account to move to must still say where its half-run thread is."""
        env, rd = self.relay("failover-dead-end", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + 100000}]}})
        counter = rd / "law-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "failover-dead-end", "--", f"[[STUB:limitafterwork={counter}]] q",
                                       env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.jobs / "failover-dead-end"
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: relay receives the alias
        seat = self.seat(d, "luna")
        self.assertEqual(seat["state"], "failed")
        self.assertEqual((seat["account"], seat["account_email"], seat["thread_id"]), ("a", "a@example.test", "stub-thread-01a0"))
        self.assertIn("every codex account is out until", seat["activity"])
        self.assertIn("; last ran on account a (thread stub-thread-01a0)", seat["activity"])
        self.assertIn(" account=a thread=stub-thread-01a0 ", out)  # the footer names them too

    def test_a_failed_claude_failover_keeps_the_spent_account_and_session_in_the_record(self):
        env, rd = self.relay("claude-dead-end", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + 100000}]}})
        counter = rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "claude-dead-end", "--", f"[[STUB:limit={counter}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.jobs / "claude-dead-end"
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "claude", "model": "sonnet"}])
        self.assertEqual(self.records(rd, "transplants.jsonl"), [])  # nowhere to carry the session to
        seat = self.seat(d, "sonnet")
        self.assertEqual((seat["state"], seat["account"], seat["session_id"]), ("failed", "a", "stub-session-aaaa"))
        self.assertIn("; last ran on account a (session stub-session-aaaa)", seat["activity"])
        self.assertIn(" account=a session=stub-session-aaaa ", out)

    def test_repeat_offers_are_counted_per_failover(self):
        """`a` re-offered once after its hit, then `b` re-offered once, then `c`: each is a fresh count, none of them is the third."""
        answers = [{"exit": 0, "label": "a"}, {"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 0, "label": "b"},
                   {"exit": 0, "label": "c"}, {"exit": 1, "wake_at": time.time() + 100000}]
        env, rd = self.relay("repeat-reset", {"pick": {"codex": answers}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "[[STUB:alwayslimit]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b", "c"])
        self.assertEqual([h["label"] for h in self.records(rd, "hits.jsonl")], ["a", "b", "c"])
        self.assertEqual(len(self.picks(rd)), 6)
        seat = self.seat(self.job_dir(out), "luna")
        self.assertIn("every codex account is out until", seat["activity"])
        self.assertNotIn("keeps offering", seat["activity"])
        self.assertEqual((seat["account"], seat["limit_hits"]), ("c", 3))

    def test_waits_are_bounded_together_by_the_routed_wait_budget(self):
        """Each wait alone fits under the cap; together they do not: the third is refused instead of served."""
        t = time.time()
        answers = [{"exit": 1, "wake_at": t + 2}, {"exit": 1, "wake_at": t + 4}, {"exit": 1, "wake_at": t + 7}, {"exit": 0, "label": "a"}]
        env, rd = self.relay("wait-budget", {"pick": {"codex": answers}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_ROUTED_MAX_WAIT="5"))
        self.assertEqual(rc, 1, out + err)
        self.assertLess(time.time() - t, 7)
        self.assertEqual(len(self.picks(rd)), 3)
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        seat = self.seat(self.job_dir(out), "luna")
        self.assertIn("every codex account is out until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(t + 7)), seat["activity"])
        self.assertIsNone(seat["waiting_until"])

    def test_a_pinned_codex_reply_refuses_when_the_label_changed_hands(self):
        """A3/#3: a pinned reply re-reads the live sign-in with `--verify`. The cached registry is unchanged; only a live
        re-read reveals the CODEX_HOME now holds another account, and the bridge must use it and refuse."""
        env, rd = self.relay("reply-identity", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "reply-identity", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-identity"
        self.assertEqual(self.seat(d, "luna")["account_email"], "a@example.test")
        rc, out, err = self.run_engine("reply", "reply-identity", "--", "more", env_extra=env)  # the same sign-in: runs, under a
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])
        listings = [c["argv"] for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"]]
        # changed for docs/model-routing.md 3.3 (F3): the ask's own listing (the capable set's candidates, cached is fine) comes
        # first; the reply's verification is a separate read of the LIVE sign-in, never the cached registry
        self.assertEqual(listings[0], ["codex", "accounts", "--json"])
        self.assertEqual([l for l in listings if "--verify" in l], [["codex", "accounts", "--verify", "--json"]])
        reply_seat = json.loads(sorted(d.glob("seat-luna-reply-*.json"))[0].read_text())
        self.assertEqual((reply_seat["account"], reply_seat["account_email"]), ("a", "a@example.test"))
        # the cached registry still says a@example.test, but a live --verify re-read reveals the sign-in changed hands
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]},
                                                    "accounts": {"a": {"email": "a@example.test"}},
                                                    "verify": {"a": {"email": "someone-else@example.test"}}}))
        rc, out, err = self.run_engine("reply", "reply-identity", "--", "again", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("account a is now signed in as someone-else@example.test, not a@example.test whose thread this is; refusing to run", out)
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 2)  # codex was not launched under the replacement sign-in
        self.assertEqual(sum(1 for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"] and "--verify" in c["argv"]), 2)
        self.assertEqual(self.seat(d, "luna")["account_email"], "a@example.test")  # the original record is untouched
        # a relay that cannot verify (an older one without --verify, or a signed-out account) refuses rather than running
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "accounts_exit": 2}))
        rc, out, err = self.run_engine("reply", "reply-identity", "--", "old relay", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("cannot verify account a before replying on its thread", out)
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 2)  # still not launched
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "verify": {"a": {"signed_in": False}}}))
        rc, out, err = self.run_engine("reply", "reply-identity", "--", "signed out", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("account a is signed out", out)
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 2)

    def test_a_malformed_wait_setting_is_a_usage_error_not_a_traceback(self):
        for name in ("BRIDGE_ROUTED_MAX_WAIT", "BRIDGE_LIMIT_WAIT_SHORT", "BRIDGE_LIMIT_WAIT_LONG"):
            with self.subTest(name=name):
                rc, out, err = self.run_engine("status", env_extra={name: "soon"})
                self.assertEqual(rc, 2, out + err)
                self.assertEqual(err.strip(), f"bridge: {name} must be a number of seconds, not 'soon'")
                self.assertNotIn("Traceback", err)
        rc, out, err = self.run_engine("status", env_extra={"BRIDGE_ROUTED_MAX_WAIT": ""})  # empty means the default
        self.assertEqual(rc, 0, out + err)

    def test_unknown_slugs_are_refused_and_relays_echo_never_changes_the_model(self):
        # changed for DESIGN §2 rule 6: REPLACES test_any_gpt_slug_is_accepted_from_the_seat_spec_and_from_relay (:1511-1523).
        # The design removes "any gpt-* slug runs": an unknown slug is refused naming a head, before any job or relay call.
        b = load_engine_module(self.env)
        for spec in ("gpt-9-nova:xhigh", "gpt-5.4-mini:low", f"codex/{NEWER_LUNA}"):  # unknown, refused, and a fictional newer Luna
            with self.subTest(spec=spec):
                with self.assertRaises(SystemExit) as cm:
                    b.parse_seats(spec, "x")
                self.assertEqual(cm.exception.code, 2)
        # changed for docs/model-routing.md 3.3 (F3): the pick is confined to the capable set, so a usable answer is protocol 2 with
        # the set applied; relay's echo of a model still never changes what the seat runs
        answer = {"v": 2, "only_applied": True, "only": ["a"], "result": "ok", "label": "a", "model": "gpt-9-nova", "email": "a@example.test",
                  "plan": "pro", "wake_at": None}
        env, rd = self.relay("slug", {"pick": {"codex": [{"exit": 0, "json": answer}]}})
        rec = rd / "argv.jsonl"
        before = self.job_names()
        rc, out, err = self.run_engine("ask", "--seats", "gpt-9-nova:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assert_refused(rc, out, err)
        self.assertEqual(self.new_jobs(before), [])
        self.assertFalse(rec.exists())
        self.assertFalse((rd / "calls.jsonl").exists())  # refused before relay was asked anything
        rc, out, err = self.run_engine("ask", "--seats", f"{head('luna')}:low", "--", f"[[STUB:argv={rec}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        argv = self.read_records(rec)[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], head("luna"))  # the seat's own model runs, whatever relay echoes back
        self.assertEqual(self.seat(self.job_dir(out), "luna")["account"], "a")

    def test_nonfinite_relay_timestamps_are_treated_as_missing(self):
        """#5: a wake time that is NaN, infinite, or too large to be a date is missing, not a traceback; the bounded retry applies."""
        for val in ("NaN", "Infinity", 1e100):
            with self.subTest(wake=val):
                env, rd = self.relay(f"badts-{val}", {"pick": {"codex": [{"exit": 1, "wake_at": val}]}})
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                self.assertNotIn("Traceback", err)
                seat = self.seat(self.job_dir(out), "luna")
                self.assertIn("no codex account is available and relay gives no wake time", seat["activity"])
                self.assertEqual(len(self.picks(rd)), 3)  # re-asked twice, then gave up — never treated the bad time as a date
                self.assertEqual(self.records(rd, "execs.jsonl"), [])

    def test_a_chatty_relay_hit_never_fails_the_seat(self):
        """#7: an older relay whose `hit` prints prose, not JSON, still blocks the account; the bridge carries on, reset unknown."""
        env, rd = self.relay("chatty-hit", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "hit_text": True})
        counter = rd / "law-counter"
        rc, out, err = self.run_engine("task", "--write", "--seats", "luna:low", "--", f"[[STUB:limitafterwork={counter}]] edit", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        seat = self.seat(self.job_dir(out), "luna")
        self.assertIn("usage limit on account a after work began (thread stub-thread-01a0); rerun manually or wait for its reset", seat["activity"])
        self.assertNotIn("wait for its reset at", seat["activity"])  # relay gave no parseable reset time, so none is claimed
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "codex", "model": "luna"}])  # changed for DESIGN §1: relay receives the alias

    def test_cancel_kills_a_sleeping_relay_helper(self):
        """#1: a cancel during a slow pick kills the relay helper at once instead of leaving it running for its full timeout."""
        env, rd = self.relay("cancel-helper", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 30}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "cancel-helper", "--", "q")
        d = self.jobs / "cancel-helper"
        self.wait_for(d, "luna", lambda s: "asking relay" in s.get("activity", ""))
        for _ in range(50):
            if self._procs_matching("stub_relay.py pick"):
                break
            time.sleep(0.1)
        self.assertTrue(self._procs_matching("stub_relay.py pick"))  # the helper is really running (sleeping)
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "cancel-helper", env_extra=env)
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 15)  # killed, not waited out (the pick sleeps 30 s)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.seat(d, "luna")["state"], "cancelled")
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        for _ in range(50):
            if not self._procs_matching("stub_relay.py pick"):
                break
            time.sleep(0.1)
        self.assertEqual(self._procs_matching("stub_relay.py pick"), [])  # no relay helper left behind

    def test_doctor_interrupt_kills_the_relay_helper(self):
        """#6, changed for DESIGN §5 (doctor): doctor no longer spawns relay at all, so a relay whose pick would hang for 30 s
        is never started: doctor finishes promptly and leaves no relay helper behind (there is nothing to interrupt)."""
        env, rd = self.relay("doctor-int", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 30}]}})
        t0 = time.time()
        p = self.start_engine(env, "doctor")
        out, err = p.communicate(timeout=60)
        self.assertLess(time.time() - t0, 25)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(self.records(rd, "calls.jsonl"), [])  # relay was never run
        self.assertEqual(self._procs_matching("stub_relay.py pick"), [])

    def test_a_codex_thread_stays_paired_with_its_account_when_the_next_launch_fails(self):
        """#2: thread T started on A; B is selected but never launches (refused), then the pool dead-ends. The record must still
        name A and T, never B, which never acquired the conversation."""
        far = time.time() + 100000
        env, rd = self.relay("pair-codex", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 1, "wake_at": far}]},
                                            "refuse": {"b": int(far)}})  # changed for ruling BUSY-1 (A6): relay's wake times are integers
        counter = rd / "law-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "pair-codex", "--", f"[[STUB:limitafterwork={counter}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "pair-codex", "luna")
        self.assertEqual((seat["state"], seat["account"], seat["thread_id"]), ("failed", "a", "stub-thread-01a0"))
        self.assertIn("; last ran on account a (thread stub-thread-01a0)", seat["activity"])
        self.assertIn(" account=a thread=stub-thread-01a0 ", out)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])  # B was tried
        self.assertEqual([h["label"] for h in self.records(rd, "hits.jsonl")], ["a"])  # only A's limit was reported

    def test_a_local_fallback_after_a_routed_limit_clears_the_account(self):
        """#2: after a routed limit the pool reports no accounts; the seat finishes locally. A local conversation carries no
        account, so the routed identity is cleared, not left pointing at the spent account."""
        env, rd = self.relay("local-fallback", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 3}]}})
        counter, rec = rd / "law-counter", rd / "argv.jsonl"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "local-fallback", "--", f"[[STUB:limitafterwork={counter},argv={rec}]] q",
                                       env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "local-fallback"
        seat = self.seat(d, "luna")
        self.assertEqual((seat["state"], seat["account"], seat["thread_id"]), ("done", None, "stub-thread-01a0"))
        self.assertNotIn(" account=", out)  # the footer no longer names an account
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])  # only the routed attempt used relay exec
        self.assertEqual(self.meta(d)["router"], {"codex": "unrouted"})
        self.assertEqual([r["account"] for r in self.read_records(rec)], ["a", None])  # first under A via relay, then local

    def test_a_claude_session_stays_with_its_source_when_transplant_fails(self):
        """#2: A holds session S; on a limit the seat routes to B but the transplant fails. The record keeps S with A, never B."""
        env, rd = self.relay("claude-tf", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "transplant_exit": 1})
        counter = rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "claude-tf", "--", f"[[STUB:limit={counter}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "claude-tf", "sonnet")
        self.assertEqual((seat["state"], seat["account"], seat["session_id"]), ("failed", "a", "stub-session-aaaa"))
        self.assertIn("could not carry session stub-session-aaaa to account b", seat["activity"])
        self.assertIn(" account=a session=stub-session-aaaa ", out)
        self.assertEqual(self.records(rd, "transplants.jsonl"), [{"session": "stub-session-aaaa", "to": "b", "cwd": self.meta(self.jobs / "claude-tf")["cwd"]}])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])  # never ran under B

    def test_a_failed_migrated_claude_reply_moves_the_continuation_owner(self):
        """#4: a reply transplants A's session to B, works under B, then fails (max turns). The continuation now lives at B, so
        the seat record must move to B even though the reply failed; a later reply resumes B directly."""
        env, rd = self.relay("claude-mig", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 0, "label": "b"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "claude-mig", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "claude-mig"
        self.assertEqual(self.seat(d, "sonnet")["account"], "a")
        rc, out, err = self.run_engine("reply", "claude-mig", "--", "[[STUB:fail]] more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(d, "sonnet")
        self.assertEqual((seat["account"], seat["session_id"]), ("b", "stub-session-stub-session-aaaa"))  # moved to B despite the failure
        self.assertEqual(self.records(rd, "transplants.jsonl"), [{"session": "stub-session-aaaa", "to": "b", "cwd": self.meta(d)["cwd"]}])
        rc, out, err = self.run_engine("reply", "claude-mig", "--", "again", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.records(rd, "transplants.jsonl")), 1)  # B already holds the session: no second transplant

    # ------------------------------------------- round-3 fixes, adversarial cases (tester round 2)
    def _wait_gone(self, needle, tries=60):
        for _ in range(tries):
            if not self._procs_matching(needle):
                return
            time.sleep(0.1)

    def test_cancel_kills_a_relay_helper_that_ignores_sigterm(self):
        """#1 adversarial: a helper that traps SIGTERM must still be gone after a cancel — the grace escalates to SIGKILL."""
        env, rd = self.relay("cancel-trap", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 30, "trap": True}]}})
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "cancel-trap", "--", "q")
        d = self.jobs / "cancel-trap"
        self.wait_for(d, "luna", lambda s: "asking relay" in s.get("activity", ""))
        for _ in range(50):
            if self._procs_matching("stub_relay.py pick"):
                break
            time.sleep(0.1)
        self.assertTrue(self._procs_matching("stub_relay.py pick"))
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "cancel-trap", env_extra=env)
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 20)  # not waited out: the pick sleeps 30 s and ignores SIGTERM
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.seat(d, "luna")["state"], "cancelled")
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        self._wait_gone("stub_relay.py pick")
        self.assertEqual(self._procs_matching("stub_relay.py pick"), [])

    def test_cancel_between_the_pick_and_the_exec_kills_the_relay_wrapper(self):
        """#1 adversarial: the window after a successful pick where the seat's child is still relay, not the CLI. The cancel
        must reach that child (the recorded pid/pgid) and the CLI must never start."""
        env, rd = self.relay("cancel-preexec", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "exec_sleep": 20})
        rec = rd / "argv.jsonl"
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "cancel-preexec", "--", f"[[STUB:argv={rec}]] q")
        d = self.jobs / "cancel-preexec"
        seat = self.wait_for(d, "luna", lambda s: s.get("pid"))
        # changed for DESIGN §8 (suite isolation): the engine publishes the pid ~15 ms before the stub relay writes its exec
        # record; the new isolated environment shifted the engine's timing into that window, so wait for the record
        for _ in range(50):
            if self.records(rd, "execs.jsonl"):
                break
            time.sleep(0.1)
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 1)  # relay was launched
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(seat["pid"])], capture_output=True, text=True).stdout
        self.assertIn("stub_relay.py", cmd)  # …and is still relay: it has not exec'd the CLI yet
        self.assertEqual(seat["pgid"], seat["pid"])
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "cancel-preexec", env_extra=env)
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(p.returncode, 1)
        self.assertFalse(self._pid_alive(seat["pid"]))
        self.assertFalse(rec.exists())  # codex never started under the account
        self.assertEqual(self.seat(d, "luna")["state"], "cancelled")
        self.assertEqual(self.meta(d)["state"], "cancelled")
        self._wait_gone(str(d))
        self.assertEqual(self._procs_matching(str(d)), [])

    def test_doctor_terminated_kills_the_relay_helper(self):
        """#6 adversarial, changed for DESIGN §5 (doctor): against a relay whose pick would ignore SIGTERM and hang, doctor
        never starts it: it finishes on its own and no relay helper exists to be terminated."""
        env, rd = self.relay("doctor-term", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 30, "trap": True}]}})
        t0 = time.time()
        p = self.start_engine(env, "doctor")
        out, err = p.communicate(timeout=60)
        self.assertLess(time.time() - t0, 25)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(self.records(rd, "calls.jsonl"), [])
        self.assertEqual(self._procs_matching("stub_relay.py pick"), [])

    def test_a_cached_or_unreadable_verification_refuses_the_pinned_reply(self):
        """#3 adversarial: relay answers `accounts --verify --json` but the reading is not a fresh live observation —
        a cached verified_at, an unparseable one, or a label it no longer lists. Each refuses instead of spending a sign-in."""
        env, rd = self.relay("pinned-verify-stale", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "pinned-verify-stale", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rec = rd / "reply-argv.jsonl"
        cases = [
            ("cached", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "ignore_verify": True}, "relay returned a cached, unverified reading for a"),
            ("nan", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "accounts": {"a": {"verified_at": "NaN"}}},
             "relay returned a cached, unverified reading for a"),
            ("gone", {"pick": {"codex": [{"exit": 0, "label": "b"}]}}, "relay no longer lists account a"),
        ]
        for name, script, reason in cases:
            with self.subTest(case=name):
                before = len(self.records(rd, "execs.jsonl"))
                (rd / "script.json").write_text(json.dumps(script))
                rc, out, err = self.run_engine("reply", "pinned-verify-stale", "--", f"[[STUB:argv={rec}]] more", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                self.assertNotIn("Traceback", err)
                self.assertIn(f"cannot verify account a before replying on its thread ({reason})", out)
                self.assertEqual(len(self.records(rd, "execs.jsonl")), before)  # codex was never launched
                self.assertFalse(rec.exists())
        self.assertEqual(self.seat(self.jobs / "pinned-verify-stale", "luna")["account_email"], "a@example.test")  # record untouched

    def test_a_pinned_reply_accepts_an_rfc3339_verification_time(self):
        """A `verified_at` in any contract form — RFC3339 with `Z`, with an explicit offset, or a naive ISO string (read as
        UTC) — is a fresh verification; Python 3.9's fromisoformat alone would reject `Z` and misread a naive string as local."""
        env, rd = self.relay("pinned-verify-rfc3339", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "pinned-verify-rfc3339", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "pinned-verify-rfc3339"
        markers = {"rfc3339z": 'Z"', "rfc3339offset": "-07:00", "naive-utc": None, "epoch": None}
        for form, marker in markers.items():
            with self.subTest(form=form):
                before = len(self.records(rd, "execs.jsonl"))
                (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "verified_at_format": form}))
                rc, out, err = self.run_engine("reply", "pinned-verify-rfc3339", "--", f"more ({form})", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")][before:], ["a"])
                trail = max(d.glob("router-luna-reply-*.jsonl"), key=lambda p: p.stat().st_mtime)  # the reply's own relay trail
                listing = next(t for t in self.read_records(trail) if t["args"][:2] == ["codex", "accounts"])
                self.assertIn('"verified_at": ', listing["out"])
                if marker:
                    self.assertIn(marker, listing["out"])  # the stub really answered in that form

    def test_epoch_reads_every_contract_timestamp_form_and_nothing_else(self):
        """The bridge↔relay timestamp contract, pinned at the unit level (independent of this machine's zone)."""
        b = load_engine_module(self.env)
        instant = datetime(2026, 9, 22, 17, 5, 0, tzinfo=timezone.utc).timestamp()
        for form in (instant, int(instant), str(instant), f" {int(instant)} ", "2026-09-22T17:05:00Z", "2026-09-22T17:05:00z",
                     "2026-09-22T17:05:00+00:00", "2026-09-22T10:05:00-07:00", "2026-09-22T17:05:00", "2026-09-22T17:05:00.000Z",
                     "2026-09-22T17:05:00.0Z", "2026-09-22T17:05:00.1234567+00:00"):
            with self.subTest(form=form):
                self.assertAlmostEqual(b._epoch(form), instant if "1234567" not in str(form) else instant + 0.123456, places=3)
        for bad in (None, True, "", "soon", "NaN", "Infinity", 1e100, "2026-13-45T00:00:00Z", "17:05:00", [instant], {"t": instant}):
            with self.subTest(bad=bad):
                self.assertIsNone(b._epoch(bad))

    def test_cancel_during_a_pinned_verification_ends_the_seat_cancelled(self):
        """A cancel that lands while the reply verifies its account (relay `accounts` still running) kills the helper and
        ends the seat as cancelled — not as a failed verification."""
        env, rd = self.relay("pinned-verify-cancel", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "pinned-verify-cancel", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "pinned-verify-cancel"
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "accounts_sleep": 30}))
        p = self.start_engine(env, "reply", "pinned-verify-cancel", "--", "more")
        reply_seat = None
        for _ in range(150):
            files = sorted(d.glob("seat-luna-reply-*.json"))
            if files and "verifying account a" in json.loads(files[0].read_text()).get("activity", "") and self._procs_matching("stub_relay.py codex accounts"):
                reply_seat = files[0]
                break
            time.sleep(0.1)
        self.assertIsNotNone(reply_seat, "the reply never reached its verification")
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "pinned-verify-cancel", env_extra=env)
        self.assertEqual(rc, 0, err)
        out, err = p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 15)  # the sleeping `accounts` helper was killed, not waited out
        self.assertEqual(p.returncode, 1)
        seat = json.loads(reply_seat.read_text())
        self.assertEqual((seat["state"], seat["activity"]), ("cancelled", "cancelled"))
        self.assertNotIn("cannot verify", out)
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 1)  # codex was never launched for the reply
        for _ in range(50):
            if not self._procs_matching("stub_relay.py codex accounts"):
                break
            time.sleep(0.1)
        self.assertEqual(self._procs_matching("stub_relay.py codex accounts"), [])

    def test_a_dead_end_names_the_owner_and_where_the_last_attempt_ran(self):
        """Thread T started on A; B ran next and hit its limit before starting any thread; then the pool dead-ended. The record
        pairs T with A, and the failure text says both that A holds T and that the last attempt ran on B."""
        far = time.time() + 100000
        env, rd = self.relay("dead-end-both", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 1, "wake_at": far}]}})
        counter = rd / "seq-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "dead-end-both", "--", f"[[STUB:limitseq={counter}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "dead-end-both", "luna")
        self.assertEqual((seat["state"], seat["account"], seat["thread_id"], seat["limit_hits"]), ("failed", "a", "stub-thread-01a0", 2))
        self.assertIn("every codex account is out until", seat["activity"])
        self.assertIn("; account a holds the conversation (thread stub-thread-01a0); the last attempt ran on account b without starting one",
                      seat["activity"])
        self.assertIn(" account=a thread=stub-thread-01a0 ", out)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])
        self.assertEqual([h["label"] for h in self.records(rd, "hits.jsonl")], ["a", "b"])

    def test_a_thread_pinned_to_its_account_is_resumed_there_by_a_later_reply(self):
        """#2 + #3 adversarial: thread T started on A, B was selected but never launched, the pool dead-ended. A later reply
        must verify and run on A — the account that actually holds T — and never on the B relay now offers."""
        far = time.time() + 100000
        env, rd = self.relay("pair-reply", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 1, "wake_at": far}]},
                                            "refuse": {"b": int(far)}})  # changed for ruling BUSY-1 (A6): relay's wake times are integers
        counter = rd / "law-counter"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "pair-reply", "--", f"[[STUB:limitafterwork={counter}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.jobs / "pair-reply"
        self.assertEqual((self.seat(d, "luna")["account"], self.seat(d, "luna")["thread_id"]), ("a", "stub-thread-01a0"))
        picks_before = len(self.picks(rd))
        # relay still knows A (signed in, verifiable) but would now route new work to B
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "b"}]}, "accounts": {"a": {}}}))
        rec = rd / "reply-argv.jsonl"
        rc, out, err = self.run_engine("reply", "pair-reply", "--", f"[[STUB:argv={rec}]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.picks(rd)), picks_before)  # a pinned reply never picks
        r = self.read_records(rec)[0]
        self.assertEqual((r["account"], r["resume"]), ("a", "stub-thread-01a0"))  # resumed on A, the account that holds it
        self.assertIn(" account=a ", out)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b", "a"])

    def test_a_claude_reply_that_never_advanced_leaves_the_record_alone(self):
        """#4 boundary: the reply transplants nothing (it fails) and no session starts, so the recorded continuation must
        stay exactly where it was; the next reply still resumes from the same account."""
        env, rd = self.relay("claude-noadv", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 0, "label": "a"}]},
                                              "transplant_exit": 1})
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "claude-noadv", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "claude-noadv"
        before = self.seat(d, "sonnet")
        self.assertEqual((before["account"], before["session_id"]), ("a", "stub-session-aaaa"))
        rec = rd / "reply-argv.jsonl"
        rc, out, err = self.run_engine("reply", "claude-noadv", "--", f"[[STUB:argv={rec}]] more", env_extra=env)  # routed to b, transplant fails
        self.assertEqual(rc, 1, out + err)
        self.assertIn("could not carry session stub-session-aaaa to account b", out)
        self.assertFalse(rec.exists())  # claude never ran
        after = self.seat(d, "sonnet")
        self.assertEqual((after["account"], after["session_id"]), ("a", "stub-session-aaaa"))  # unmoved
        self.assertEqual(after["account_email"], before["account_email"])
        rc, out, err = self.run_engine("reply", "claude-noadv", "--", f"[[STUB:argv={rec}]] again", env_extra=env)  # relay routes back to a
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_records(rec)[0]["account"], "a")
        self.assertEqual(len(self.records(rd, "transplants.jsonl")), 1)  # a already holds it: no second transplant

    def test_a_seat_record_without_a_recorded_email_still_verifies_and_replies(self):
        """#3 boundary: a job written by an older bridge has an account but no account_email. The reply cannot compare
        emails, so it must still demand a fresh live verification — and run when relay gives one."""
        env, rd = self.relay("legacy-record", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "legacy-record", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "legacy-record"
        seat_file = d / "seat-luna.json"
        st = json.loads(seat_file.read_text())
        st.pop("account_email"), st.pop("account_plan")
        seat_file.write_text(json.dumps(st))
        rec = rd / "reply-argv.jsonl"
        rc, out, err = self.run_engine("reply", "legacy-record", "--", f"[[STUB:argv={rec}]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_records(rec)[0]["account"], "a")
        verify_calls = [c["argv"] for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"] and "--verify" in c["argv"]]
        self.assertEqual(len(verify_calls), 1)  # the ask's own account listing (no --verify, docs/model-routing.md 3.3) is not one
        # and when that verification cannot be had, the reply refuses rather than assuming the old record is still true
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "accounts_exit": 2}))
        rc, out, err = self.run_engine("reply", "legacy-record", "--", "again", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("cannot verify account a before replying on its thread", out)

    def test_an_unreadable_reset_time_from_hit_is_simply_unknown(self):
        """#5 + #7 adversarial: `hit --json` answering with a nonfinite blocked_until (a raw JSON NaN) claims no reset time
        and never raises; the same for a raw NaN wake_at from `pick`."""
        env, rd = self.relay("nan-hit", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "blocked_until": float("nan")})
        counter = rd / "law-counter"
        rc, out, err = self.run_engine("task", "--write", "--seats", "luna:low", "--", f"[[STUB:limitafterwork={counter}]] edit", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        seat = self.seat(self.job_dir(out), "luna")
        self.assertIn("usage limit on account a after work began (thread stub-thread-01a0); rerun manually or wait for its reset", seat["activity"])
        self.assertNotIn("wait for its reset at", seat["activity"])
        self.assertEqual([h["label"] for h in self.records(rd, "hits.jsonl")], ["a"])
        env, rd = self.relay("nan-wake", {"pick": {"codex": [{"exit": 1, "wake_at": float("nan")}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertIn("no codex account is available and relay gives no wake time", self.seat(self.job_dir(out), "luna")["activity"])
        self.assertEqual(self.records(rd, "execs.jsonl"), [])

    # ------------------------------------------- ruling BUSY-1 (busy/RULING_BUSY.md): a busy relay account is a wait, not a usage limit
    # relay exec refuses an account whose slots are all taken with one line (relay.py cmd_exec). The seat waits and asks again: no
    # `hit`, no limit_hits, no count toward `repeats`, `stale` or the fallback's attempts, no change to the conversation or to
    # `avoid`. The detector is _busy_refusal(r, label) in bridge.py, beside SeatRouting.refused(); SeatRouting.busy is the wait. The
    # busy interval is LIMIT_WAIT_SHORT/4 with ±40 % jitter (A7), never under 0.05 s (BUSY-R1 R1-1), so about 0.05 s under the
    # suite's BRIDGE_LIMIT_WAIT_SHORT=0.2; a test that measures the spacing raises BRIDGE_LIMIT_WAIT_SHORT
    def busy_interval(self, short):
        """The shortest and the longest busy wait, in seconds, under BRIDGE_LIMIT_WAIT_SHORT=`short` (A7; the 0.05 s floor, R1-1)."""
        return max(0.05, short / 4 * 0.6), max(0.05, short / 4 * 1.4)

    def busy_archives(self, d, tag):
        """The stderr files of a seat's busy refusals, each archived before the relaunch as <attempt>.busy<n> (A3)."""
        return sorted(Path(d).glob(f"stderr-{tag}.attempt*.busy*.log"))

    def test_the_busy_detector_takes_relays_own_busy_lines_and_nothing_else(self):
        """BUSY-1 test 1 (rule 1, A8, A9): an attempt relay exec refused before the CLI ran is busy only when the LAST NON-EMPTY line
        of its stderr is relay's busy line, in either spelling (built from relay's own source), naming the attempt's own account.
        Busy is one busy wait on the same attempt and no limit; anything else keeps today's refusal path (the account counts as
        limited and is avoided: attempt 2). The stub relay's busy mode prints relay's line exactly."""
        wake = int(time.time()) + 60
        singular, plural = relay_busy_line("a", 1, wake), relay_busy_line("a", 2, wake)
        for slots, line in ((1, singular), (2, plural)):
            env, rd = self.relay(f"busy-contract-stub-{slots}", {"busy": {"a": {"slots": slots}}})
            r = subprocess.run([sys.executable, str(STUB_RELAY), "exec", "--vendor", "codex", "--account", "a", "--", "true"],
                               capture_output=True, text=True, timeout=30, env=dict(os.environ, **env))
            self.assertEqual((r.returncode, without_wake(r.stderr)), (1, without_wake(line)), "the stub relay's busy line is not relay's")
        machine = ("relay: account a cannot take work now: this machine is signed into b@example.test now, not a's account (a@example.test); "
                   "nothing runs under a through this machine's login until the binding is resolved (any relay routing command does that); "
                   "wake_at=none\n")
        busy = {"singular": singular, "plural": plural, "three slots": relay_busy_line("a", 3, wake), "then an empty line": plural + "\n"}
        other = {"ceiling": f"relay: account a cannot take work now: weekly window at 99% (worker ceiling 99%); wake_at={wake}\n",
                 "rate limit": f"relay: account a cannot take work now: rate_limit_reached; resets 2026-09-28 04:00; wake_at={wake}\n",
                 "not signed in": "relay: account a cannot take work now: not signed in (run: relay codex login a); wake_at=none\n",
                 "machine login": machine,
                 "not the last relay line": plural + "relay: could not run codex: [Errno 8] Exec format error: 'codex'\n",
                 "not the last line": plural + "[relay] running under account a\n",
                 "another account": relay_busy_line("b", 2, wake)}
        cases = [("luna:low", "codex", name, text, True) for name, text in busy.items()]
        cases += [("luna:low", "codex", name, text, False) for name, text in other.items()]
        cases += [("sonnet:low", "claude", "singular", singular, True), ("sonnet:low", "claude", "plural", plural, True),
                  ("sonnet:low", "claude", "machine login", machine, False), ("sonnet:low", "claude", "another account", other["another account"], False)]
        for spec, vendor, name, text, is_busy in cases:
            tag = spec.split(":")[0]
            with self.subTest(seat=spec, line=name):
                env, rd = self.relay(f"busy-contract-{vendor}-{name.replace(' ', '-')}",
                                     {"pick": {vendor: [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]},
                                      "exec_fail": {"a": {"stderr": text, "exit": 1, "times": 1}}})
                rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                seat = self.seat(self.job_dir(out), tag)
                got = (seat["limit_hits"], seat.get("busy_waits") or 0, seat["attempt"])
                self.assertEqual(got, (0, 1, 1) if is_busy else (1, 0, 2), f"(limit_hits, busy_waits, attempt) after relay refused a with {text!r}")
                self.assertEqual(self.records(rd, "hits.jsonl"), [])  # relay refused, not the CLI: nothing is reported back

    def test_a_busy_account_is_waited_for_and_run_on_without_a_limit(self):
        """BUSY-1 test 2 (rules 2-4, A3): relay's only account is busy at the first exec, then free. The seat waits and runs there:
        no `hit`, no limit, no "keeps offering", `attempt` still 1 and one busy wait recorded; the refused attempt's stderr was
        archived as <attempt>.busy<n> before the relaunch. Codex and Claude alike."""
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec):
                job = f"busy-once-{vendor}"
                env, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 1}}})
                rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", "q", env_extra=env)
                d = self.jobs / job
                seat = self.seat(d, tag)
                self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "a"), out + err)
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])
                self.assertEqual(self.records(rd, "hits.jsonl"), [])
                self.assertEqual((seat["limit_hits"], seat["attempt"], seat.get("busy_waits")), (0, 1, 1))
                self.assertNotIn("keeps offering", out)
                self.assertEqual([without_wake(p.read_text()) for p in self.busy_archives(d, tag)], [without_wake(relay_busy_line("a", 2, 0))])

    def test_busy_refusals_past_the_repeat_cap_still_end_in_success(self):
        """BUSY-1 test 3 (rules 2 and 4): a busy refusal is no re-offer of a spent account, so the `repeats` cap (the seat gives up at
        the third re-offer) never applies: four busy refusals in a row on the only account, each followed by a fresh pick, then a
        free slot, and the seat succeeds there. No archive overwrites another (A3). Codex and Claude alike."""
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec):
                job = f"busy-many-{vendor}"
                env, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 4}}})
                rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", "q", env_extra=env)
                d = self.jobs / job
                seat = self.seat(d, tag)
                self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "a"), out + err)
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"] * 5)
                self.assertEqual(len(self.picks(rd)), 5)  # rule 4: after each busy wait the account is chosen again through the normal path
                self.assertEqual((seat["limit_hits"], seat["attempt"], seat.get("busy_waits")), (0, 1, 4))
                self.assertEqual(self.records(rd, "hits.jsonl"), [])
                self.assertEqual(len(self.busy_archives(d, tag)), 4)

    def test_a_seat_busy_until_its_wait_budget_ends_says_so_and_never_spins(self):
        """BUSY-1 test 4 (rule 3, A5, A7): relay's only account stays busy. Before each wait the seat checks what is left of its routed
        budget (BRIDGE_ROUTED_MAX_WAIT from its start, bounded by its --timeout); the busy interval spaces the execs, so nothing
        spins; when the next wait would pass the budget the seat ends with one line naming the busy account, and that wait, never
        begun, is not counted in busy_waits (BUSY-R1 R1-4). Without a --timeout, with one the budget ends before, and with one that
        ends first (no busy wait fits in it)."""
        short = 2.0  # busy waits of 0.3-0.7 s
        low, _high = self.busy_interval(short)
        cases = (("codex, no timeout", "luna:low", "codex", [], {"BRIDGE_ROUTED_MAX_WAIT": "3"}),
                 ("claude, no timeout", "sonnet:low", "claude", [], {"BRIDGE_ROUTED_MAX_WAIT": "3"}),
                 ("codex, the budget ends first", "luna:low", "codex", ["--timeout", "60"], {"BRIDGE_ROUTED_MAX_WAIT": "3"}),
                 ("codex, the timeout ends first", "luna:low", "codex", ["--timeout", "5"], {"BRIDGE_LIMIT_WAIT_SHORT": "40"}))  # waits of 6-14 s
        for name, spec, vendor, args, extra in cases:
            tag = spec.split(":")[0]
            with self.subTest(case=name):
                env, rd = self.relay("busy-budget-" + re.sub(r"\W+", "-", name), {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "busy": {"a": {}}})
                env.update({"BRIDGE_LIMIT_WAIT_SHORT": str(short)}, **extra)
                t0 = time.time()
                rc, out, err = self.run_engine("ask", "--seats", spec, *args, "--", "q", env_extra=env)
                elapsed = time.time() - t0
                seat = self.seat(self.job_dir(out), tag)
                self.assertEqual((rc, seat["state"], bool(seat.get("timed_out"))), (1, "failed", False), out + err)
                self.assertRegex(seat["activity"], r"\baccount a\b[^;]*\bbusy\b", "the seat's last line does not name the busy account")
                self.assertNotIn("keeps offering", seat["activity"])
                self.assertNotIn("usage limit", seat["activity"])
                self.assertEqual((seat["limit_hits"], self.records(rd, "hits.jsonl")), (0, []))
                t = [e["t"] for e in self.records(rd, "execs.jsonl")]
                # BUSY-R1 R1-4: a busy wait is counted only when it begins; the refusal the budget ends the seat at adds none
                self.assertEqual(seat.get("busy_waits") or 0, len(t) - 1, f"busy_waits after {len(t)} busy refusals, the last ending the seat")
                if "timeout ends first" in name:
                    self.assertEqual(len(t), 1, "a busy wait longer than what was left of the seat's --timeout was started")
                    continue
                gaps = [round(b - a, 2) for a, b in zip(t, t[1:])]
                self.assertTrue(gaps and min(gaps) >= low - 0.05, f"execs {gaps} s apart: the busy wait did not space them")
                self.assertLess(len(t), 3 / low + 2)
                self.assertLess(elapsed, 3 + 15, "the seat waited past its routed budget")

    def test_a_pinned_reply_waits_for_its_busy_account_and_re_verifies_after_a_long_spell(self):
        """BUSY-1 test 5 (rule 4, A1, A5): a Codex reply is pinned to the account its thread lives in. When relay finds that account
        busy the reply waits for it and runs there, never asking `pick`. Its sign-in was verified live before the first exec
        (verified_at); after a busy wait it is verified again only once REPICK_WAIT (3 x BRIDGE_LIMIT_WAIT_SHORT) has passed since:
        a short spell verifies once, a long one twice, and a sign-in that changed hands during the long spell refuses the reply
        there. A spell that outlasts the routed budget ends with a line naming the busy account, where the thread lives."""
        env, rd = self.relay("busy-pinned", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "busy-pinned", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "busy-pinned"
        picks = len(self.picks(rd))

        def mark():
            verifies = [c for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"] and "--verify" in c["argv"]]
            return len(self.records(rd, "execs.jsonl")), len(verifies), set(d.glob("seat-luna-reply-*.json"))

        def since(before):
            """What one reply did: (its execs, its --verify reads, its seat record)."""
            now = mark()
            (seat,) = now[2] - before[2]
            return now[0] - before[0], now[1] - before[1], json.loads(seat.read_text())

        def script(busy, **more):
            tmp = rd / "script.json.new"  # replaced atomically: relay may be reading it
            tmp.write_text(json.dumps(dict({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "busy": {"a": busy}}, **more)))
            os.replace(tmp, rd / "script.json")

        with self.subTest(spell="short"):  # REPICK_WAIT is 6 s here: one busy wait of 0.3-0.7 s does not reach it
            before = mark()
            (rd / "busy-a").unlink(missing_ok=True)
            script({"times": 1})
            rc, out, err = self.run_engine("reply", "busy-pinned", "--", "more", env_extra=dict(env, BRIDGE_LIMIT_WAIT_SHORT="2"))
            execs, verifies, seat = since(before)
            self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "a"), out + err)
            self.assertEqual((execs, verifies), (2, 1), "(execs, --verify reads) of a reply that waited out a short busy spell")
            self.assertEqual((seat["limit_hits"], seat.get("busy_waits")), (0, 1))
        with self.subTest(spell="long"):  # REPICK_WAIT is 0.6 s here, and relay takes 1.5 s over its busy refusal
            before = mark()
            (rd / "busy-a").unlink(missing_ok=True)
            script({"times": 1, "sleep": 1.5})
            rc, out, err = self.run_engine("reply", "busy-pinned", "--", "again", env_extra=env)
            execs, verifies, seat = since(before)
            self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "a"), out + err)
            self.assertEqual((execs, verifies), (2, 2), "(execs, --verify reads) of a reply that waited out a long busy spell")
        with self.subTest(spell="long, and the sign-in changed hands during it"):
            before = mark()
            (rd / "busy-a").unlink(missing_ok=True)
            script({"times": 1, "sleep": 3})
            p = self.start_engine(env, "reply", "busy-pinned", "--", "once more")
            try:
                for _ in range(100):  # relay is taking its time over the busy refusal: the first verification is behind the reply
                    if len(self.records(rd, "execs.jsonl")) > before[0]:
                        break
                    time.sleep(0.1)
                script({"times": 1, "sleep": 3}, verify={"a": {"email": "someone-else@example.test"}})  # what a live re-read now finds
            finally:
                out, err = p.communicate(timeout=60)
            execs, verifies, seat = since(before)
            self.assertEqual((p.returncode, seat["state"]), (1, "failed"), out + err)
            self.assertIn("account a is now signed in as someone-else@example.test, not a@example.test whose thread this is; refusing to run", out)
            self.assertEqual((execs, verifies), (1, 2), "codex ran under the replacement sign-in, or it was never verified again")
        with self.subTest(spell="past the routed budget"):
            before = mark()
            script({})
            rc, out, err = self.run_engine("reply", "busy-pinned", "--", "last", env_extra=dict(env, BRIDGE_LIMIT_WAIT_SHORT="2", BRIDGE_ROUTED_MAX_WAIT="2"))
            _execs, verifies, seat = since(before)
            self.assertEqual((rc, seat["state"], verifies), (1, "failed", 1), out + err)
            self.assertRegex(seat["activity"], r"\baccount a\b[^;]*\bbusy\b")
            self.assertIn("this thread lives there", seat["activity"])
        self.assertEqual(len(self.picks(rd)), picks)  # a pinned reply never picks
        self.assertEqual(self.records(rd, "hits.jsonl"), [])
        self.assertEqual(self.seat(d, "luna")["account"], "a")

    def test_a_cancel_during_a_busy_wait_ends_the_seat_cancelled(self):
        """BUSY-1 test 6 (rule 3, A5): the busy wait is _wait_seat's, shown as `waiting` with the busy account named, and cancellable.
        A cancel during it ends the seat cancelled at once: nothing else is executed, nothing is reported to relay, and a Claude
        seat's refused attempt leaves no observation to judge. The busy wait (24-56 s here) outlasts the 15 s the cancel is given, so
        a wait that is served rather than interrupted fails the test (BUSY-R1 R1-2). Codex and Claude alike."""
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec):
                job = f"busy-cancel-{vendor}"
                env, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "busy": {"a": {}}})
                p = self.start_engine(dict(env, BRIDGE_LIMIT_WAIT_SHORT="160"), "ask", "--seats", spec, "--job-dir", job, "--", "q")  # waits of 24-56 s
                d = self.jobs / job
                try:
                    seat = self.wait_for(d, tag, lambda s: s.get("state") in ("waiting", "done", "failed", "cancelled"))
                    self.assertEqual(seat["state"], "waiting", seat)
                    self.assertIn("account a is busy (its slots are in use)", seat["activity"])
                finally:
                    t0 = time.time()
                    self.run_engine("cancel", job, env_extra=env)
                    out, err = p.communicate(timeout=60)
                self.assertLess(time.time() - t0, 15)  # the wait (24 s at the least) was interrupted, not served
                seat = self.seat(d, tag)
                self.assertEqual((p.returncode, seat["state"], self.meta(d)["state"]), (1, "cancelled", "cancelled"), out + err)
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
                self.assertEqual((seat["limit_hits"], self.records(rd, "hits.jsonl")), (0, []))
                judged = [n.get("kind") for n in self.notices(d) if n.get("kind") in ("observation-incomplete", "ran-model", "usage-unexplained")]
                self.assertEqual(judged, [], out)

    def test_a_cli_that_dies_after_a_busy_wait_fails_fast_with_its_own_error(self):
        """BUSY-1 test 9 (rule 2, A8): the busy attempt's stderr is archived before the relaunch, so the next attempt never inherits
        relay's busy line. Here the next attempt's CLI dies before any event ("boom", exit 1): the seat fails at once with the CLI's
        own error, neither as busy nor after waiting out its routed budget."""
        env, rd = self.relay("busy-then-boom", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 1}}})
        t0 = time.time()
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "busy-then-boom", "--", "[[STUB:fail]] q",
                                       env_extra=dict(env, BRIDGE_ROUTED_MAX_WAIT="30"))
        self.assertLess(time.time() - t0, 20, "the seat waited for its budget to run out")
        d = self.jobs / "busy-then-boom"
        seat = self.seat(d, "luna")
        self.assertEqual((rc, seat["state"]), (1, "failed"), out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])  # the CLI ran after the busy wait
        self.assertIn("boom", seat["activity"])
        self.assertNotIn("busy", seat["activity"])
        self.assertEqual((seat["limit_hits"], seat.get("busy_waits"), self.records(rd, "hits.jsonl")), (0, 1, []))
        self.assertNotIn("cannot take work now", (d / "stderr-luna.log").read_text())  # the last attempt's stderr is its own
        self.assertEqual(len(self.busy_archives(d, "luna")), 1)

    def test_a_claude_seat_busy_after_a_limit_resumes_with_one_transplant(self):
        """BUSY-1 test 10 (rule 2): a busy refusal changes no conversation state. A Claude seat hits a usage limit on account a after
        work began (its session goes on elsewhere, with the continuation prompt), then relay finds b busy. The seat waits, keeps the
        session, the account that holds it and the continuation prompt, and resumes on b: exactly one transplant, no fresh start."""
        env, rd = self.relay("busy-claude-resume", {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}, "busy": {"b": {"times": 1}}})
        env["STUB_CLAUDE_RECORD"] = str(rd / "claude.jsonl")
        rec, counter = rd / "argv.jsonl", rd / "limit-counter"
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "busy-claude-resume", "--", f"[[STUB:limit={counter},argv={rec}]] q",
                                       env_extra=env)
        d = self.jobs / "busy-claude-resume"
        seat = self.seat(d, "sonnet")
        self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "b"), out + err)
        self.assertEqual(self.records(rd, "hits.jsonl"), [{"label": "a", "vendor": "claude", "model": "sonnet"}])
        self.assertEqual(self.records(rd, "transplants.jsonl"), [{"session": "stub-session-aaaa", "to": "b", "cwd": self.meta(d)["cwd"]}])
        execs = self.records(rd, "execs.jsonl")
        self.assertEqual([e["label"] for e in execs], ["a", "b", "b"])
        self.assertEqual(execs[2]["command"][execs[2]["command"].index("--resume") + 1], "stub-session-aaaa")
        self.assertEqual([(r["account"], r["resume"]) for r in self.claude_records(env)], [("a", None), ("b", "stub-session-aaaa")])
        self.assertEqual([r["account"] for r in self.read_records(rec)], ["a"])  # the resumed turn carries the continuation prompt, not the directive
        self.assertEqual((seat["session_id"], seat["attempt"], seat["limit_hits"], seat.get("busy_waits")), ("stub-session-stub-session-aaaa", 2, 1, 1))

    def test_the_hit_guard_survives_a_busy_wait_until_relays_block_lapses(self):
        """BUSY-1 test 11 (rule 2, A2): a busy refusal neither sets `avoid` nor clears it. Account a hits a usage limit (relay blocks
        it until `blocked_until`), then b is busy, then relay offers a again. While a's block lasts, that offer is the hit not
        sticking and the guard gives up as today; once the busy spell has outlasted the block (relay takes 3 s over b's refusal,
        the block ends 2 s from now) the offer is usable and the seat runs on a. Codex, and Claude, whose path kept no reset time."""
        for spec, vendor, directive in (("luna:low", "codex", "limit"), ("sonnet:low", "claude", "limitbeforework")):
            tag = spec.split(":")[0]
            for lapsed in (False, True):
                with self.subTest(seat=spec, block="lapsed" if lapsed else "in force"):
                    job = f"busy-guard-{vendor}-{'lapsed' if lapsed else 'held'}"
                    env, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 0, "label": "a"}]},
                                               "blocked_until": int(time.time()) + (2 if lapsed else 3600),
                                               "busy": {"b": dict({"times": 1}, **({"sleep": 3} if lapsed else {}))}})
                    rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", f"[[STUB:{directive}={rd / 'limit-counter'}]] q",
                                                   env_extra=env)
                    seat = self.seat(self.jobs / job, tag)
                    self.assertEqual([h["label"] for h in self.records(rd, "hits.jsonl")], ["a"], out + err)
                    self.assertEqual((seat["limit_hits"], seat.get("busy_waits")), (1, 1), out + err)
                    if lapsed:
                        self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "a"), out + err)
                        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b", "a"])
                    else:
                        self.assertEqual((rc, seat["state"]), (1, "failed"), out + err)
                        self.assertIn("relay keeps offering account a after its usage limit was reported; giving up", seat["activity"])
                        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"])  # never re-run inside its block

    def test_five_seats_on_two_busy_codex_slots_all_succeed(self):
        """BUSY-1 test 12, the reported shape (bridge job 20260927-110206-ask-fb64d): seats astra, sol, terra, luna and opus in one
        job, and relay's one Codex account has two slots, so two of the four Codex seats are refused busy at first. They wait for a
        slot and run: all five seats succeed, and nothing is reported to relay as limited."""
        seats = ("astra", "sol", "terra", "luna", "opus")
        env, rd = self.relay("busy-shape", {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 0, "label": "c"}]},
                                            "busy": {"a": {"times": 2}}})
        rc, out, err = self.run_engine("ask", "--seats", ",".join(f"{s}:low" for s in seats), "--job-dir", "busy-shape", "--", "q", env_extra=env)
        d = self.jobs / "busy-shape"
        records = {s: self.seat(d, s) for s in seats}
        self.assertEqual((rc, {s: r["state"] for s, r in records.items()}), (0, dict.fromkeys(seats, "done")), out + err)
        self.assertEqual({s: r["account"] for s, r in records.items()}, dict(dict.fromkeys(seats[:4], "a"), opus="c"))
        self.assertEqual(sum(r.get("busy_waits") or 0 for r in records.values()), 2)
        self.assertEqual([r["limit_hits"] for r in records.values()], [0] * 5)
        self.assertEqual(self.records(rd, "hits.jsonl"), [])
        self.assertEqual(sorted(e["label"] for e in self.records(rd, "execs.jsonl")), ["a"] * 6 + ["c"])
        self.assertEqual(self.meta(d)["state"], "done")

    def test_a_non_busy_refusal_still_gives_up_at_the_third_re_offer(self):
        """BUSY-1 test 13, the pin (rule 1: every other refusal keeps today's path; A10): relay exec refuses the only account for
        another reason (a ceiling; no sign-in, `wake_at=none`). The seat counts it as spent and avoids it; relay offers it again;
        after two REPICK_WAIT pauses the seat gives up at the third re-offer, with today's line."""
        cases = (("luna:low", "codex", int(time.time() + 3600)), ("sonnet:low", "claude", int(time.time() + 3600)),
                 ("luna:low", "codex", {"wake_at": None, "reason": "not signed in (run: relay codex login a)"}))
        for n, (spec, vendor, refusal) in enumerate(cases):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec, refusal=refusal):
                env, rd = self.relay(f"busy-pin-{n}", {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "refuse": {"a": refusal}})
                rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
                seat = self.seat(self.job_dir(out), tag)
                self.assertEqual((rc, seat["state"]), (1, "failed"), out + err)
                self.assertIn("relay keeps offering account a after its usage limit was reported; giving up", seat["activity"])
                self.assertEqual(len(self.picks(rd)), 4)  # the launch pick, then `a` offered three more times
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
                self.assertEqual(self.records(rd, "hits.jsonl"), [])  # relay refused it itself: nothing to report
                self.assertEqual((seat["attempt"], seat["limit_hits"], seat.get("busy_waits") or 0), (2, 1, 0))

    def test_a_busy_pick_is_asked_again_within_the_busy_interval(self):
        """BUSY-1 test 14 (A4): relay's pick answers `busy` (every eligible account's slots are taken; its wake_at a minute out), then
        ok. `busy` is its own answer: the seat asks again after the busy interval, not at the wake time, which here lies beyond the
        routed budget. BRIDGE_LIMIT_WAIT_SHORT=4 makes the interval 0.6-1.4 s. Codex (a confined pick, protocol 2) and Claude (v1)."""
        low, high = self.busy_interval(4.0)
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec):
                env, rd = self.relay(f"busy-pick-{vendor}", {"pick": {vendor: [{"exit": 1, "result": "busy", "wake_at": time.time() + 60},
                                                                               {"exit": 0, "label": "a"}]}})
                rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=dict(env, BRIDGE_LIMIT_WAIT_SHORT="4", BRIDGE_ROUTED_MAX_WAIT="30"))
                seat = self.seat(self.job_dir(out), tag)
                self.assertEqual((rc, seat["state"], seat["account"]), (0, "done", "a"), out + err)
                t = [c["t"] for c in self.records(rd, "calls.jsonl") if c["argv"][:1] == ["pick"]]
                self.assertEqual(len(t), 2)
                self.assertGreaterEqual(t[1] - t[0], low - 0.05, "the busy answer was asked again without the busy interval")
                self.assertLessEqual(t[1] - t[0], high + 4.0, "the busy answer was waited out like another (to relay's wake time, or REPICK_WAIT: 12 s)")
                self.assertEqual(self.records(rd, "hits.jsonl"), [])

    # ------------------------------------------- ruling BUSY-R1 (busy/RULING_BUSY.md, review round 1): R1-1 and R1-3
    def test_a_zero_busy_interval_still_ends_the_seat_at_its_wait_budget(self):
        """BUSY-R1 R1-1: BRIDGE_LIMIT_WAIT_SHORT=0 leaves the busy interval its 0.05 s floor, and relay's only account stays busy. Once
        the routed budget (2 s) is spent the seat ends with the rule-3 line naming the account, rather than asking relay again with
        no pause for ever, and its execs stay bounded (at most one per floor interval). A seat still asking long after its budget is
        cancelled and fails the test. Codex and Claude alike."""
        floor, _ = self.busy_interval(0)
        budget, limit = 2, 2 + 15
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec):
                job = f"busy-zero-{vendor}"
                env, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "busy": {"a": {}}})
                env.update(BRIDGE_LIMIT_WAIT_SHORT="0", BRIDGE_ROUTED_MAX_WAIT=str(budget))
                t0 = time.time()
                p = self.start_engine(env, "ask", "--seats", spec, "--job-dir", job, "--", "q")
                try:
                    out, err = p.communicate(timeout=limit)
                except subprocess.TimeoutExpired:  # still asking relay: stop the seat, then fail below
                    self.run_engine("cancel", job, env_extra=env)
                    out, err = p.communicate(timeout=60)
                elapsed = time.time() - t0
                execs = self.records(rd, "execs.jsonl")
                seat = self.seat(self.jobs / job, tag)
                self.assertLess(elapsed, limit, f"the seat was still asking relay {elapsed:.0f} s after its start, its budget {budget} s "
                                                f"({len(execs)} execs)")
                self.assertEqual((p.returncode, seat["state"]), (1, "failed"), out + err)
                self.assertIn("account a stayed busy (its slots are in use) until the seat's wait budget ran out", seat["activity"])
                self.assertLessEqual(len(execs), budget / floor + 2, "the execs were not spaced by the busy interval's floor")
                self.assertEqual((seat["limit_hits"], self.records(rd, "hits.jsonl")), (0, []))

    def test_the_busy_wait_never_drops_below_its_floor(self):
        """BUSY-R1 R1-1, the floor alone: with BRIDGE_LIMIT_WAIT_SHORT=0 every busy wait is still at least 0.05 s. (The test above
        passes without the floor as long as the budget check stays, so this one pins the floor itself.)"""
        b = load_engine_module(dict(self.env, BRIDGE_LIMIT_WAIT_SHORT="0"))
        self.assertGreaterEqual(min(b._busy_wait() for _ in range(200)), 0.05)
        b = load_engine_module(dict(self.env, BRIDGE_LIMIT_WAIT_SHORT="20"))
        floor, ceiling = self.busy_interval(20)
        waits = [b._busy_wait() for _ in range(200)]
        self.assertTrue(floor <= min(waits) and max(waits) <= ceiling, (min(waits), max(waits)))

    def test_a_busy_refusal_has_its_own_provenance_attempt_id(self):
        """BUSY-R1 R1-3: a Codex attempt relay refused busy writes its provenance under the suffix of its archive, <attempt>.busy<n>
        (n: the busy wait it leads to), and the attempt after the wait keeps the plain <attempt>. One busy refusal, then a success:
        two distinct attempt ids, no record of one filed under the other's. (A Claude seat writes no provenance for an attempt relay
        refused.)"""
        job = "busy-provenance"
        env, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 1}}})
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", job, "--", "q", env_extra=env)
        d = self.jobs / job
        seat = self.seat(d, "luna")
        self.assertEqual((rc, seat["state"], seat["attempt"], seat.get("busy_waits")), (0, "done", 1, 1), out + err)
        records = self.read_records(d / "provenance-luna.jsonl")
        completes = [(r["attempt_id"], r["outcome"]) for r in records if r["kind"] == "complete"]
        self.assertEqual(completes, [(f"{job}/luna#1.busy1", "refused"), (f"{job}/luna#1", "success")],
                         "the busy refusal and the attempt after its wait share a provenance attempt id")
        self.assertEqual({r["attempt_id"] for r in records}, {f"{job}/luna#1.busy1", f"{job}/luna#1"})
        self.assertEqual([p.name for p in self.busy_archives(d, "luna")], ["stderr-luna.attempt1.busy1.log"])  # the same suffix

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False


# ==================================================================================================================
# DESIGN §8 group A: the table and name resolution, through the engine CLI (pure Table/evaluate tests: test_bridge_models.py)
# ==================================================================================================================
class GroupA_TableAndResolution(EngineBase):
    """DESIGN §1-§2: one table; a seat name is a family name, a head, a listed spelling, or the K1 seat while it is on;
    everything else is refused in ONE line naming a concrete head, before any job, relay call or CLI launch."""

    # facts/MODEL_FACTS.md (2026-09-24, primary sources) and DESIGN §1: the one test that does not read the table. The
    # literal holds every behaviour-driving field of the table: the check's policy (gate, refresh window, timeouts, fallback
    # attempts) and catalog configuration keys, the effort vocabularies, each family's head and matching pattern, each
    # model's family, generation, effort set, CLI floor and listed spellings, the fallback edges (model and effort), the
    # retired predecessors, every refused record (the reason its refusal line prints, its replacement family, the model it
    # meant, the record it shares) and the K1 seat. Only descriptive provenance (PROVENANCE) is left out. A change to any
    # other field in models.json fails this test until the literal is updated too; every other test reads the loaded table.
    _CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]
    _CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
    VERIFIED = {
        "policy": {"stale_gate": "warn", "recheck_after_hours": 24, "catalog_timeout_s": 20, "fetch_timeout_s": 5, "fallback_attempts": 3,
                   "auto_promote": True},  # DECISIONS A28 (Kevin, 2026-11-07): newer Codex models are adopted automatically
        # L2 (review/rulings_final_round.md): service_tier is not a catalog key. Codex rust-v0.157.0 app-server/src/models.rs
        # supported_models() filters only on include_hidden || show_in_picker; service tiers are per-model metadata
        "catalog_config_keys": ["chatgpt_base_url", "model_catalog_json", "model_provider", "model_providers", "openai_base_url",
                                "oss_provider", "profile", "profiles"],
        "efforts": {"codex": _CODEX_EFFORTS, "claude": _CLAUDE_EFFORTS},
        # The literal below is the verified declaration itself (facts/MODEL_FACTS.md), the one place these tests spell exact ids
        # (docs/model-routing.md 9.1: every line carries its own marker; the rest of the suite reads the table)
        "families": {  # each family's head, and the pattern that makes a live ID a candidate member (the stale-table rule)
            "codex": {"astra": {"id_glob": "gpt-*-astra", "known_good": "gpt-6-astra"},  # model-lint: allow verified table literal
                      "sol": {"id_glob": "gpt-*-sol", "known_good": "gpt-6.1-sol"},  # model-lint: allow verified table literal
                      "terra": {"id_glob": "gpt-*-terra", "known_good": "gpt-6.1-terra"},  # Kevin's checkpoint 2026-11-07; model-lint: allow verified table literal
                      "luna": {"id_glob": "gpt-*-luna", "known_good": "gpt-6-luna"}},  # model-lint: allow verified table literal
            "claude": {"fable": {"id_prefix": "claude-fable-", "known_good": "claude-fable-5-1"},  # model-lint: allow verified table literal
                       "opus": {"id_prefix": "claude-opus-", "known_good": "claude-opus-5-5"},  # model-lint: allow verified table literal
                       "sonnet": {"id_prefix": "claude-sonnet-", "known_good": "claude-sonnet-5-5"},  # cleanup C7 (facts_C7, 2026-09-29), PR #34; model-lint: allow verified table literal
                       "haiku": {"id_prefix": "claude-haiku-", "known_good": "claude-haiku-5-5"}},  # model-lint: allow verified table literal
        },
        "models": {
            "gpt-6-astra": {"family": "astra", "generation": "6", "efforts": _CODEX_EFFORTS, "min_cli": "0.153.0", "aliases": []},  # model-lint: allow verified table literal
            "gpt-6.1-sol": {"family": "sol", "generation": "6.1", "efforts": _CODEX_EFFORTS, "min_cli": "0.153.0", "aliases": []},  # Kevin's checkpoint 2026-10-01; model-lint: allow verified table literal
            # Kevin's checkpoint 2026-11-07: listed by model/list under 0.160.0 and 0.167.0; floor = the bundled minimal_client_version
            "gpt-6.1-terra": {"family": "terra", "generation": "6.1", "efforts": _CODEX_EFFORTS, "min_cli": "0.153.0", "aliases": []},  # model-lint: allow verified table literal
            "gpt-6-luna": {"family": "luna", "generation": "6", "efforts": ["low", "medium", "high", "xhigh", "max"], "min_cli": "0.156.1",  # model-lint: allow verified table literal
                           "aliases": []},
            "claude-fable-5-1": {"family": "fable", "generation": "5.1", "efforts": _CLAUDE_EFFORTS, "min_cli": "2.1.257", "aliases": []},  # model-lint: allow verified table literal
            "claude-opus-5-5": {"family": "opus", "generation": "5.5", "efforts": _CLAUDE_EFFORTS, "min_cli": "2.1.280", "aliases": []},  # model-lint: allow verified table literal
            # cleanup C7: the efforts of the 2.1.284 catalog, and its floor, the first Claude Code whose catalog lists the model
            "claude-sonnet-5-5": {"family": "sonnet", "generation": "5.5", "efforts": _CLAUDE_EFFORTS, "min_cli": "2.1.284", "aliases": []},  # the Models API listing of 2026-10-01; model-lint: allow verified table literal
            "claude-haiku-5-5": {"family": "haiku", "generation": "5.5", "efforts": _CLAUDE_EFFORTS, "min_cli": "2.1.295", "aliases": []},  # P-16; model-lint: allow verified table literal
            "claude-haiku-4-5-20251001": {"family": "haiku", "generation": "4.5", "efforts": [], "min_cli": None, "aliases": ["claude-haiku-4-5"]},  # model-lint: allow verified table literal
            "claude-opus-5": {"family": "opus", "generation": "5", "efforts": _CLAUDE_EFFORTS, "min_cli": None, "aliases": []},  # the K1 seat's target; model-lint: allow verified table literal
        },
        "fallback": {"codex": {"gpt-6-astra": "gpt-6.1-sol"}, "claude": {}},  # a 6 -> 6.1 suggestion, not a descent; model-lint: allow verified table literal
        "effort_fallback": {"codex": {"ultra": "max", "max": "xhigh"}},
        "k1_seat": {"seat": "opus-5", "id": "claude-opus-5", "enabled": True},  # ruling K1-ON (review/ruling_k1_on.md): ships on; model-lint: allow verified table literal
        "retired": {  # superseded predecessors, each replaced by its family's head
            "codex": {"astra": [], "luna": ["gpt-5.6-luna"], "sol": ["gpt-5.5", "gpt-5.6-sol", "gpt-6-sol"], "terra": ["gpt-5.6-terra"]},  # model-lint: allow verified table literal
            "claude": {"fable": ["claude-fable-5"],  # model-lint: allow verified table literal
                       "haiku": ["claude-3-5-haiku-20241022", "claude-3-haiku-20240307", "claude-haiku-4-5-20251001"],  # model-lint: allow verified table literal
                       "opus": ["claude-opus-4-1-20250805", "claude-opus-4-20250514", "claude-opus-4-5-20251101", "claude-opus-4-6",  # model-lint: allow verified table literal
                                "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5"],  # model-lint: allow verified table literal
                       "sonnet": ["claude-3-7-sonnet-20250219", "claude-sonnet-4-20250514", "claude-sonnet-4-5-20250929", "claude-sonnet-4-6",  # model-lint: allow verified table literal
                                  "claude-sonnet-5"]},  # superseded by claude-sonnet-5-5 (cleanup C7, PR #34); model-lint: allow verified table literal
        },
        "refused": {  # refused names: the reason printed, the family whose head replaces them, the model they meant, or a shared record
            "codex": {"5.5": {"id": "gpt-5.5", "family": "sol", "reason": "retires 2026-10-14; the current Sol replaces it"},  # model-lint: allow verified table literal
                      "gpt-5.5": {"same_as": "5.5"},  # model-lint: allow verified table literal
                      "spark": {"id": "gpt-5.3-codex-spark", "family": "luna",  # model-lint: allow verified table literal
                                "reason": "retired; OpenAI names no replacement; nearest current fast model"},
                      "gpt-5.3-codex-spark": {"same_as": "spark"},  # model-lint: allow verified table literal
                      "gpt-5.4": {"family": "sol", "reason": "retired 2026-08-31; OpenAI's upgrade pointer names Sol"},  # model-lint: allow verified table literal
                      "gpt-5.4-mini": {"family": "luna", "reason": "in no catalog; nearest current fast model"},  # model-lint: allow verified table literal
                      "gpt-5.2": {"family": "sol", "reason": "in no catalog; the current Sol is the workhorse"},  # model-lint: allow verified table literal
                      "gpt-6": {"family": "astra",  # model-lint: allow verified table literal
                                "reason": "ambiguous since GPT-6 Sol and Luna shipped; the GPT-6 flagship is Astra (say sol or luna for the others)"},  # model-lint: allow verified table literal
                      "gpt6": {"same_as": "gpt-6"},  # model-lint: allow verified table literal
                      "gpt-reserve": {"family": "luna", "reason": "hidden internal model, not selectable; nearest current fast model"},
                      "codex-auto-review": {"family": "sol", "reason": "hidden internal review model, not selectable"},
                      "gpt-daybreak-*": {"family": "astra", "reason": "hidden preview models, not selectable"}},
            "claude": {"claude-mythos-5-1": {"family": "fable",
                                             "reason": "excluded: Project Glasswing only; Fable is the top generally available model"},
                       "claude-mythos-5": {"same_as": "claude-mythos-5-1"},
                       "claude-mythos-preview": {"family": "fable", "reason": "deprecated 2026-06-09"},
                       "opus5": {"family": "opus", "reason": "ambiguous: opus is the newest Opus; the K1 seat is opus-5"},
                       "fable5": {"family": "fable", "reason": "ambiguous: fable is the newest Fable"}},
        },
    }
    # Descriptive provenance, the only fields VERIFIED leaves out, by where they occur ("*" = any key): the table's revision
    # stamp and date, its lint marker (the table declares ids by design; docs/model-routing.md 9.2), when and from what a family
    # was verified, each model's source note, the K1 seat's note and declaration date, and the dated history of decisions (DESIGN §1).
    PROVENANCE = {(): ("version", "updated", "history", "model_lint"), ("families", "*", "*"): ("verified", "verified_by"),
                  ("models", "*"): ("source",), ("k1_seat",): ("reason", "declared")}
    # Lists that are sets of names, compared sorted (their order carries no meaning); effort lists keep their order.
    UNORDERED = (("catalog_config_keys",), ("models", "*", "aliases"), ("retired", "*", "*"))

    @classmethod
    def binding_fields(cls, data, at=()):
        """A parsed table (or any part of it at path `at`) without its descriptive provenance and with its unordered lists
        sorted: every other field, in the shape of VERIFIED."""
        def matches(pattern):
            return len(pattern) == len(at) and all(p in ("*", a) for p, a in zip(pattern, at))
        if isinstance(data, dict):
            drop = {key for pattern, keys in cls.PROVENANCE.items() if matches(pattern) for key in keys}
            return {k: cls.binding_fields(v, at + (k,)) for k, v in data.items() if k not in drop}
        if isinstance(data, list):
            items = [cls.binding_fields(v, at) for v in data]
            return sorted(items) if any(matches(pattern) for pattern in cls.UNORDERED) else items
        return data

    def unknown_line(self, name):
        fams = families()
        return (f"bridge: refused seat '{name}': '{name}' is not in the model table (skills/codex/models.json). "
                f"Use one of {fams[0]} ({head(fams[0])}), " + ", ".join(fams[1:]) + ".")

    def refuse(self, name, spec, env=None, relay=True):
        """Run `ask --seats <spec>` and return the refusal line; asserts nothing launched, no job, no relay call."""
        env = dict(env or {})
        if relay:
            renv, rd = self.relay(f"refuse-{name}", {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 0, "label": "a"}]}})
            env.update(renv)
        before = self.job_names()
        rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
        line = self.assert_refused(rc, out, err)
        self.assertEqual(self.new_jobs(before), [], "a refused seat must not create a job")
        if "STUB_CODEX_RECORD" in env:
            self.assertEqual(self.launched_models(env), [])
        if relay:
            self.assertEqual(self.records(rd, "calls.jsonl"), [], "a refused seat must not reach relay")
        return line

    def persist_marker(self, env, model_id, kind="retired", detail="Retired; retirement September 1, 2026"):
        """A sticky marker in the isolated evidence store, as the check persists one (TEST_RULINGS R-T1: records keyed by model
        ID beside the `cleared` log and `clear_seq`). The launch validator reads it in commit 1, before the check exists."""
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        doc = json.loads(p.read_text()) if p.is_file() else {}
        doc.setdefault("markers", {})[model_id] = {"id": model_id, "kind": kind, "kinds": [kind], "source": "anthropic model-deprecations",
                                                   "seen_at": time.time(), "read_seq": int(doc.get("clear_seq") or 0), "detail": detail}
        doc.setdefault("cleared", [])
        doc.setdefault("clear_seq", 0)
        p.write_text(json.dumps(doc, indent=1))
        p.chmod(0o600)

    def test_the_shipped_table_loads(self):
        self.assertTrue(MODELS_JSON.is_file(), f"DESIGN §1: the model table must ship at {MODELS_JSON}")
        data = json.loads(MODELS_JSON.read_text())
        for key in ("version", "updated", "policy", "catalog_config_keys", "efforts", "families", "models", "fallback",
                    "effort_fallback", "k1_seat", "retired", "refused", "history"):
            self.assertIn(key, data)
        mod = import_bridge_models()
        self.assertIsNotNone(mod.Table.load(str(MODELS_JSON)))
        env, _ = self.isolated("shipped-table-loads")
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [data["families"]["codex"]["sol"]["known_good"]])

    def test_table_heads_are_the_verified_facts(self):
        self.assertTrue(MODELS_JSON.is_file(), f"DESIGN §1: the model table must ship at {MODELS_JSON}")
        data = json.loads(MODELS_JSON.read_text())
        shipped = self.binding_fields(data)
        self.assertEqual(self.binding_fields(self.VERIFIED), self.VERIFIED, "the literal carries no provenance and lists sets sorted")
        # the policy and catalog keys, heads and matching patterns, generations, listed spellings, CLI floors, effort sets and
        # vocabularies, the fallback edges (model and effort), retired and refused names with their reasons and replacements,
        # the K1 seat (handoff §3: retired names are refused)
        for field, expected in self.VERIFIED.items():
            with self.subTest(field=field):
                self.assertEqual(shipped.get(field), expected)
        self.assertEqual(sorted(shipped), sorted(self.VERIFIED), "a new table field is bound until it is declared provenance")
        # ... and each family name launches exactly that model
        env, _ = self.isolated("verified-facts")
        heads = {fam: rec["known_good"] for vendor in ("codex", "claude") for fam, rec in self.VERIFIED["families"][vendor].items()}
        specs = ",".join(f"{fam}:low" if self.VERIFIED["models"][model]["efforts"] else fam for fam, model in heads.items())
        rc, out, err = self.run_engine("council", "--seats", specs, "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(self.launched_models(env)), sorted(heads.values()))

    def test_loader_rejects_cross_generation_or_non_head_fallback_edges(self):
        # each cross-generation case sets the target's generation below the source's itself, so it does not depend on which
        # shipped head is older (since Kevin's checkpoint of 2026-11-07 the standard head, terra, is newer than the top head, astra)
        cases = {
            "cross-generation": descend_mutation("codex", head("astra"), head("terra")),
            "target-not-a-head": lambda t: t["fallback"]["codex"].__setitem__(head("astra"), RETIRED_SOL),
            "source-not-a-head": lambda t: t["fallback"]["codex"].__setitem__(RETIRED_SOL, head("sol")),
            "claude-cross-generation": descend_mutation("claude", head("opus"), head("fable")),
        }
        edges = lambda t: {(k, v) for vendor in ("codex", "claude") for k, v in t["fallback"][vendor].items()}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                table = self.table_file(f"bad-fallback-{name}", mutate)
                env, _ = self.isolated(f"bad-fallback-{name}", table=table)
                line = self.refuse(f"bad-fallback-{name}", "sol:low", env)
                (src, dst), = edges(json.loads(table.read_text())) - edges(TABLE)
                self.assertTrue("fallback" in line or (src in line and dst in line), line)
                rc, out, err = self.run_engine("status", env_extra=env)  # the table is loaded lazily
                self.assertEqual(rc, 0, out + err)

    def test_every_refusal_record_has_a_family_and_a_reason(self):
        for vendor in ("codex", "claude"):
            for key, raw in TABLE["refused"][vendor].items():
                with self.subTest(vendor=vendor, key=key):
                    if "same_as" in raw:
                        target = TABLE["refused"][vendor].get(raw["same_as"])
                        self.assertIsNotNone(target, f"{key}: same_as names no record")
                        self.assertNotIn("same_as", target, f"{key}: a chained same_as")
                    rec = refusal_record(vendor, key)
                    self.assertIn(rec.get("family"), TABLE["families"][vendor])
                    self.assertTrue(str(rec.get("reason") or "").strip())
        bad = {
            "no-family": ("codex", REFUSED_SOL_NAME, {"reason": "in no catalog"}),
            "empty-reason": ("codex", REFUSED_SOL_NAME, {"family": "sol", "reason": ""}),
            "unknown-family": ("codex", REFUSED_SOL_NAME, {"family": "nova", "reason": "in no catalog"}),
            "unresolvable-same-as": ("codex", RETIRING_FULL, {"same_as": "gpt-no-such-record"}),
            "chained-same-as": ("codex", RETIRING_FULL, {"same_as": "gpt6"}),  # gpt6 is itself a same_as record
            "cross-vendor-same-as": ("codex", RETIRING_FULL, {"same_as": "opus5"}),
            "claude-no-reason": ("claude", "fable5", {"family": "fable"}),
        }
        for name, (vendor, key, rec) in bad.items():
            with self.subTest(case=name):
                table = self.table_file(f"bad-refusal-{name}", lambda t, v=vendor, k=key, r=rec: t["refused"][v].__setitem__(k, r))
                env, _ = self.isolated(f"bad-refusal-{name}", table=table)
                self.refuse(f"bad-refusal-{name}", "sol:low", env)

    def test_family_names_heads_and_listed_spellings_resolve(self):
        groups = {
            "family-names": ([f"{f}:low" if efforts_of(head(f)) else f for f in families()], families()),
            "head-ids": ([f"{head(f)}:low" if efforts_of(head(f)) else head(f) for f in families()], families()),
            "spellings": ([HAIKU_ALIAS, "SOL:low", "opus[1m]:low", "codex/luna:low", "claude/fable:low", "Astra:low@fast"],
                          ["haiku", "sol", "opus", "luna", "fable", "astra"]),
        }
        for name, (specs, fams) in groups.items():
            with self.subTest(group=name):
                env, _ = self.isolated(f"resolve-{name}", table=self.table_file(f"resolve-{name}", with_haiku_alias))
                rc, out, err = self.run_engine("council", "--seats", ",".join(specs), "--job-dir", f"resolve-{name}", "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.jobs / f"resolve-{name}"
                for fam in fams:  # tag = the family; the record names the head
                    self.assertTrue((d / f"seat-{fam}.json").is_file(), f"no seat tagged {fam}: {sorted(p.name for p in d.glob('seat-*.json'))}")
                    self.assertEqual(self.seat(d, fam)["model"], head(fam), fam)
                self.assertEqual(sorted(self.launched_models(env)), sorted(head(f) for f in fams))

    def test_everything_else_is_refused_naming_a_head(self):
        # changed for A-opus-4 (review/rulings_final_review.md): a name matching a family's pattern (a fictional newer Sol, Luna or
        # Opus, with or without a forced vendor) is refused naming THAT family's head:
        # GroupA_FinalReviewTable.test_a_name_matching_a_familys_pattern_is_refused_naming_that_familys_head. The names here match
        # no family's pattern and keep rule 6's line
        for name in ("gpt-9-nova", "gemini-3", "grok-4", "codex-mini-latest"):
            with self.subTest(name=name):
                env, _ = self.isolated(f"unknown-{name}")
                self.assertEqual(self.refuse(f"unknown-{name}", name, env), self.unknown_line(name))
        for spec in ("codex/gpt-9-nova", "claude/claude-nova-9"):
            with self.subTest(name=spec):
                line = self.refuse("unknown-" + spec.replace("/", "-"), spec)
                self.assertIn("is not in the model table (skills/codex/models.json). Use one of ", line)
                self.assertIn(f"({head(families()[0])})", line)
        for spec in ("claude/sol", "codex/opus"):  # a forced vendor that disagrees
            with self.subTest(name=spec):
                line = self.refuse("disagree-" + spec.replace("/", "-"), spec)
                self.assertTrue(line.startswith("bridge: refused seat '"), line)
                self.assertRegex(line, r"Use (?:one of )?[a-z]+ \((" + "|".join(re.escape(head(f)) for f in families()) + r")\)")

    def test_predecessors_are_refused_naming_the_head(self):
        for vendor in ("codex", "claude"):
            for fam, ids in TABLE["retired"][vendor].items():
                for old in ids:
                    if old in TABLE["refused"][vendor]:
                        continue  # rule 2 (a refusal record, the retiring Sol predecessor) decides before rule 5: test_refused_names_print_...
                    with self.subTest(retired=old):
                        env, _ = self.isolated(f"retired-{old}")
                        self.assertEqual(self.refuse(f"retired-{old}", old, env, relay=(old == ids[0])),
                                         f"bridge: refused seat '{old}': {old} is superseded by {head(fam)}. Use {fam} ({head(fam)}).")

    def test_refused_names_print_their_replacement_head(self):
        for vendor in ("codex", "claude"):
            for key in TABLE["refused"][vendor]:
                name = key.replace("*", "preview-2")
                rec = refusal_record(vendor, key)
                with self.subTest(name=name):
                    env, _ = self.isolated(f"refused-{name}")
                    line = self.refuse(f"refused-{name}", name, env, relay=(key in ("spark", "5.5")))
                    self.assertEqual(line, f"bridge: refused seat '{name}': {rec['reason']}. Use {rec['family']} ({head(rec['family'])}).")
                    self.assertLessEqual(len(line), 200)
        luna = f"Use luna ({head('luna')})."
        spark_reason = "retired; OpenAI names no replacement; nearest current fast model"
        for name in ("spark", SPARK_ID):
            with self.subTest(both_spark_spellings=name):
                self.assertEqual(self.refuse(f"spark-{name}", name, relay=False), f"bridge: refused seat '{name}': {spark_reason}. {luna}")
        self.assertEqual(self.refuse("gpt55-record", RETIRING_FULL, relay=False),
                         f"bridge: refused seat '{RETIRING_FULL}': {refusal_record('codex', '5.5')['reason']}. Use sol ({head('sol')}).")

    def test_vendor_prefix_selects_a_vendor_and_waives_nothing(self):
        env, _ = self.isolated("prefix-accept")
        rc, out, err = self.run_engine("council", "--seats", "codex/sol:low,claude/opus:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(self.launched_models(env)), sorted([head("sol"), head("opus")]))
        spark = refusal_record("codex", "spark")
        expectations = {
            f"codex/{RETIRED_SOL}": f"{RETIRED_SOL} is superseded by {head('sol')}. Use sol ({head('sol')}).",
            "codex/spark": f"{spark['reason']}. Use luna ({head('luna')}).",
            f"claude/{K1_ID}": f"{K1_ID} is superseded by {head('opus')}. Use opus ({head('opus')}).",
            "codex/5.5": f"{refusal_record('codex', '5.5')['reason']}. Use sol ({head('sol')}).",
        }
        for spec, tail in expectations.items():
            with self.subTest(spec=spec):
                env, _ = self.isolated("prefix-" + spec.replace("/", "-"))
                line = self.refuse("prefix-" + spec.replace("/", "-"), spec, env)
                self.assertTrue(line.startswith("bridge: refused seat '"), line)
                self.assertTrue(line.endswith(tail), line)

    def test_efforts_come_from_the_table(self):
        luna = head("luna")
        self.assertNotIn("ultra", efforts_of(luna))
        env, _ = self.isolated("effort-luna-ultra")
        line = self.refuse("effort-luna-ultra", "luna:ultra", env)
        self.assertIn(f"{luna} supports {', '.join(efforts_of(luna))}; 'ultra' is not available (say luna:max)", line)
        for spec, hint in (("sol:loww", None), ("sol:extra-high", "say xhigh"), ("opus:ultra", "say max"), ("fable:extrahigh", None)):
            with self.subTest(spec=spec):
                env, _ = self.isolated("effort-" + spec.replace(":", "-"))
                line = self.refuse("effort-" + spec.replace(":", "-"), spec, env)
                self.assertIn(repr(spec.split(":")[1]), line)
                if hint:
                    self.assertIn(hint, line)
        # the same validation for --effort and the environment defaults (validated like typed input)
        for args, extra, needle in ((["--seats", "luna", "--effort", "ultra"], {}, "'ultra' is not available"),
                                    (["--seats", "sol"], {"BRIDGE_CODEX_EFFORT": "loww"}, "'loww'"),
                                    (["--seats", "opus"], {"BRIDGE_CLAUDE_EFFORT": "ultra"}, "say max")):
            with self.subTest(args=args, env=extra):
                env, _ = self.isolated("effort-" + "-".join(args[1:]) + "-" + "-".join(extra), **extra)
                rc, out, err = self.run_engine("ask", *args, "--", "q", env_extra=env)
                self.assertIn(needle, self.assert_refused(rc, out, err))
                self.assertEqual(self.launched_models(env), [])
        # a built-in default lowers to the model's highest level (and the label shows it); Haiku now supports effort too
        env, _ = self.isolated("effort-defaults")
        rc, out, err = self.run_engine("council", "--seats", "luna,opus,haiku,fable:xhigh", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([r["effort"] for r in self.codex_records(env, "exec")], [efforts_of(luna)[-1]])
        self.assertIn(f"luna={luna}@{efforts_of(luna)[-1]}", out)
        by_model = {r["model"]: r for r in self.claude_records(env)}
        self.assertEqual(by_model[head("opus")]["effort"], "max")
        self.assertEqual(by_model[head("fable")]["effort"], "xhigh")
        self.assertEqual(by_model[head("haiku")]["effort"], "max")
        env, _ = self.isolated("effort-haiku-explicit")
        rc, out, err = self.run_engine("ask", "--seats", "haiku:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.claude_records(env)[-1]["effort"], "low")

    def test_k1_seat_is_a_switch_and_obeys_markers(self):
        # changed for ruling K1-ON (review/ruling_k1_on.md, Kevin 2026-09-26): the seat ships on, so opus-5 launches with the
        # shipped table; the off state is a variant table
        k1 = TABLE["k1_seat"]
        off = self.table_file("k1-off", lambda t: t["k1_seat"].__setitem__("enabled", False))
        env, _ = self.isolated("k1-off", table=off)
        self.assertEqual(self.refuse("k1-off", "opus-5", env),
                         f"bridge: refused seat 'opus-5': the opus-5 seat is switched off (models.json k1_seat; Kevin K1). "
                         f"Use opus ({head('opus')}).")
        self.assertTrue(k1["enabled"], "ruling K1-ON: the shipped table switches the K1 seat on")
        env, _ = self.isolated("k1-on")
        renv, rd = self.relay("k1-on", {"pick": {"claude": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "opus-5:high", "--job-dir", "k1-on", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [k1["id"]])
        self.assertEqual(self.claude_records(env)[0]["effort"], "high")
        self.assertEqual([p[p.index("--model") + 1] for p in self.picks(rd)], ["opus"])  # the K1 seat's family alias
        footer = next(l for l in out.splitlines() if l.startswith("— opus-5="))
        self.assertIn("(K1 seat)", footer)
        seat = self.seat(self.jobs / "k1-on", "opus-5")
        self.assertEqual((seat["seat_name"], seat["model"]), ("opus-5", k1["id"]))
        # "even when on, sticky markers apply": GroupE_MarkersAcrossPaths.test_k1_seat_obeys_markers_even_when_on

    def test_the_k1_target_typed_as_a_seat_is_still_refused_with_the_seat_on(self):
        # ruling K1-ON (review/ruling_k1_on.md): with the shipped table, which switches the seat on, the K1 target's bare id typed as
        # a seat is still refused exactly as today (a retired predecessor, §2 rule 5, naming opus and its head): only the opus-5
        # seat reaches it. No job, no relay call, no launch
        k1, opus = TABLE["k1_seat"], head("opus")
        self.assertTrue(k1["enabled"], "ruling K1-ON: the shipped table switches the K1 seat on")
        for spec in (k1["id"], f"{k1['id']}:high"):
            with self.subTest(seat=spec):
                name = "k1-target-" + spec.replace(":", "-")
                env, _ = self.isolated(name)
                self.assertEqual(self.refuse(name, spec, env),
                                 f"bridge: refused seat '{spec}': {k1['id']} is superseded by {opus}. Use opus ({opus}).")

    def test_env_default_seats_are_validated_at_first_use_and_named(self):
        cases = (("BRIDGE_REVIEW_SEATS", "spark", ["review"], "luna"),
                 ("BRIDGE_DEFAULT_SEAT", "5.5", ["ask", "--", "q"], "sol"),
                 ("BRIDGE_COUNCIL_SEATS", f"astra:low,{REFUSED_GPT6}", ["council", "--", "q"], "astra"))
        for var, value, argv, fam in cases:
            with self.subTest(var=var):
                env, _ = self.isolated(f"envdefault-{var}", **{var: value})
                before = self.job_names()
                rc, out, err = self.run_engine(*argv, env_extra=env)
                line = self.assert_refused(rc, out, err)
                self.assertIn(var, line)
                self.assertIn(f"Use {fam} ({head(fam)}).", line)
                self.assertEqual(self.new_jobs(before), [])
                self.assertEqual(self.launched_models(env), [])
                # validated at first use only: explicit seats still run, and status never looks at it
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                rc, out, err = self.run_engine("status", env_extra=env)
                self.assertEqual(rc, 0, out + err)

    def test_a_missing_or_malformed_table_leaves_status_cancel_result_and_doctor_working(self):
        env, _ = self.isolated("lazy-table")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "lazy-table-job", "--", "[[STUB:text=LAZY-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        launched = self.launched_models(env)  # the setup ask's launch: every refusal below must leave the records unchanged
        self.assertEqual(launched, [head("luna")])
        malformed = self.tmp / "tables" / "malformed.json"
        malformed.write_text('{"version": 13, "families": ')
        tables = {"missing": self.tmp / "tables" / "does-not-exist.json", "malformed": malformed,
                  "invariant": self.table_file("lazy-invariant", descend_mutation("codex", head("astra"), head("terra")))}
        names = {"missing": ("does-not-exist.json",), "malformed": ("malformed.json",), "invariant": ("lazy-invariant.json", "fallback", head("terra"))}
        for name, path in tables.items():
            with self.subTest(table=name):
                bad = dict(env, BRIDGE_MODELS_FILE=str(path))
                for argv in (["status"], ["status", "lazy-table-job"], ["result", "lazy-table-job"], ["monitor", "lazy-table-job", "--once"],
                             ["merge", "lazy-table-job"]):
                    rc, out, err = self.run_engine(*argv, env_extra=bad)
                    self.assertEqual(rc, 0, f"{argv}: {out}{err}")
                    self.assertNotIn("Traceback", err)
                rc, out, err = self.run_engine("result", "lazy-table-job", env_extra=bad)
                self.assertIn("LAZY-ANSWER", out)
                rc, out, err = self.run_engine("doctor", env_extra=bad)  # doctor keeps working and names the table problem
                self.assertNotIn("Traceback", out + err)
                self.assertIn(rc, (1, 2), out + err)
                self.assertTrue(any(n in out + err for n in names[name]), out + err)
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=bad)
                line = self.assert_refused(rc, out, err)
                self.assertTrue(any(n in line for n in names[name]), line)
                self.assertEqual(self.launched_models(bad), launched)  # nothing launched since the setup ask
        rc, out, err = self.run_engine("cancel", "lazy-table-job", env_extra=dict(env, BRIDGE_MODELS_FILE=str(tables["missing"])))
        self.assertEqual(rc, 0, out + err)

    def test_claude_seats_get_full_ids_explicit_effort_and_the_no_switch_settings(self):
        overrides = {"ANTHROPIC_MODEL": K1_ID, "ANTHROPIC_DEFAULT_OPUS_MODEL": K1_ID,  # every override names a retired predecessor
                     "ANTHROPIC_DEFAULT_FABLE_MODEL": TABLE["retired"]["claude"]["fable"][0], "ANTHROPIC_DEFAULT_SONNET_MODEL": TABLE["retired"]["claude"]["sonnet"][0],
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL": TABLE["retired"]["claude"]["haiku"][0]}
        env, _ = self.isolated("claude-launch-args", **overrides)
        rc, out, err = self.run_engine("council", "--seats", "opus,fable:high,sonnet:low,haiku", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        recs = {r["model"]: r for r in self.claude_records(env)}
        self.assertEqual(sorted(recs), sorted([head("opus"), head("fable"), head("sonnet"), head("haiku")]))
        self.assertEqual((recs[head("opus")]["effort"], recs[head("fable")]["effort"], recs[head("sonnet")]["effort"]), ("max", "high", "low"))
        self.assertEqual(recs[head("haiku")]["effort"], "max")
        for model, r in recs.items():
            with self.subTest(model=model):
                self.assertEqual(r["settings"], {"switchModelsOnFlag": False, "fallbackModel": []})
                self.assertFalse(r["fallback_model"])
                self.assertIn("--restricted", r["argv"])
                self.assertEqual(r["env_model_overrides"], [])  # the seat env drops the alias-override variables

    def test_relay_receives_family_aliases_not_ids(self):
        env, _ = self.isolated("relay-aliases")
        renv, rd = self.relay("relay-aliases", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}],
                                                         "claude": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        counter = self.case_dir("relay-aliases") / "limit"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:limit={counter}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(p[p.index("--model") + 1] for p in self.picks(rd)), ["luna", "luna", "opus"])
        self.assertEqual(sorted({e["argv"][e["argv"].index("--model") + 1] for e in self.records(rd, "execs.jsonl")}), ["luna", "opus"])
        self.assertIn({"label": "a", "vendor": "codex", "model": "luna"}, self.records(rd, "hits.jsonl"))
        self.assertEqual({r["model"] for r in self.codex_records(env, "exec")}, {head("luna")})  # the CLI gets the full ID
        self.assertEqual({r["model"] for r in self.claude_records(env)}, {head("opus")})

    def test_a_marker_on_any_spelling_of_a_head_refuses_every_spelling(self):
        # Astra C1-1: lifecycle evidence is keyed by the canonical model ID and validate() maps every spelling the table accepts
        # to it. Anthropic's page also lists the Haiku head by its alias; with that row Retired, the marker a check persisted under
        # the alias (before canonical keying) or under the head ID refuses haiku, the alias and the head ID alike, with no
        # job, no relay pick and no CLI launch
        haiku, alias = head("haiku"), HAIKU_ALIAS
        table = self.table_file("head-alias-marker", with_haiku_alias)
        page = self.deprecations_with(alias, "Retired", "August 1, 2026", "September 1, 2026")
        for key in (alias, haiku):
            for spec in ("haiku", alias, haiku):
                with self.subTest(marker_on=key, spec=spec):
                    name = f"spelling-marker-{key}-{spec}"
                    env, _ = self.isolated(name, table=table, deprecations=page)
                    self.persist_marker(env, key)
                    self.assertIn(haiku, self.refuse(name, spec, env))


# ==================================================================================================================
# DESIGN §8 group B: launch, fallback, observation, notices (marker-dependent parts: GroupE_MarkersAcrossPaths)
# ==================================================================================================================
class GroupB_LaunchFallbackObservationNotices(EngineBase):
    """DESIGN §3-§4: startup rejection first and the one fallback edge; validation before launch; the observed-model
    contract of Claude seats; notices persisted and leading every output."""

    def kinds(self, d):
        return [n.get("kind") for n in self.notices(d)]

    def observation(self, seat):
        """The seat record's `observation` (DESIGN §1 records, §4): models_observed, session_models_cumulative, complete."""
        self.assertIn("observation", seat, f"the seat record has no observation: {sorted(seat)}")
        return seat["observation"]

    def seat_notices(self, d, kind, tag):
        """The texts of the job's persisted notices of one kind recorded for one seat."""
        return [n.get("text") or "" for n in self.notices(d) if n.get("kind") == kind and n.get("seat") == tag]

    def write_marker(self, env, model_id, kind):
        """A sticky lifecycle marker in the isolated evidence store, in the layout the check persists (TEST_RULINGS R-T1, as
        GroupA_TableAndResolution.persist_marker writes it), replaced atomically: a running seat may read the store at any time."""
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        doc = json.loads(p.read_text()) if p.is_file() else {}
        doc.setdefault("markers", {})[model_id] = {
            "id": model_id, "kind": kind, "kinds": [kind], "seen_at": time.time(), "read_seq": int(doc.get("clear_seq") or 0),
            "source": "codex model/list" if model_id.startswith("gpt-") else "anthropic model-deprecations", "detail": f"{kind} (test)"}
        doc.setdefault("cleared", [])
        doc.setdefault("clear_seq", 0)
        tmp = p.with_name(f".{p.name}.test-{os.getpid()}")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.chmod(0o600)
        os.replace(tmp, p)

    def as_old_record(self, d, tag, model):
        """What a bridge before this change recorded for a seat: only the model string (as GroupC_Reply.as_old_record)."""
        p = Path(d) / f"seat-{tag}.json"
        s = json.loads(p.read_text())
        for k in ("seat_name", "family_requested", "model_requested", "continuation"):
            s.pop(k, None)
        s["model"] = model
        p.write_text(json.dumps(s))

    def exec_count(self, env):
        """How many `codex exec` invocations the stub has recorded so far (read while the engine may be appending)."""
        p = Path(env["STUB_CODEX_RECORD"])
        return p.read_text().count('"mode": "exec"') if p.exists() else 0

    def feed_stream(self, fifo, engine, events, tries=150):
        """Write `events` (one JSON line each) into the named pipe a `[[STUB:replay=<fifo>]]` Claude stub reads its stream from,
        once the stub has opened it: the stub emits them, then waits for more. Returns the pipe's descriptor; closing it ends
        the stream."""
        for _ in range(tries):
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)  # fails until the stub has the pipe open for reading
                break
            except OSError:
                if engine.poll() is not None:
                    self.fail("the engine exited before its stub read the stream:\n" + "".join(engine.communicate()))
                time.sleep(0.1)
        else:
            self.fail("the stub never opened its stream")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        os.write(fd, "".join(json.dumps(e) + "\n" for e in events).encode())
        return fd

    def test_no_catalog_or_relay_read_happens_on_any_launch_path(self):
        # changed for docs/model-routing.md 3.3 (F3): the CHECK's catalog read happens once a day (doctor here), never on a launch
        # path; the model registry's per-account catalog reads (client "model-registry") are the routing's and are counted apart
        env, d = self.isolated("no-reads")
        rc, out, err = self.run_engine("doctor", env_extra=env)  # the one read of the day happens here, not on the paths below
        reads, pages = len(self.check_records(env)), len(self.page_calls("no-reads"))
        script = {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}],
                           "claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}}
        paths = (("fallback", ["--seats", "astra|sol:low", "--", "[[STUB:badmodel]] q"], True),                # the list's descent + pinned reply
                 ("reroute", ["--seats", "luna:low", "--", f"[[STUB:limit={d / 'lim-reroute'}]] q"], False),  # re-route after a limit
                 ("claude", ["--seats", "sonnet:low", "--", "q"], True))                                        # Claude launch + reply
        relays = []
        for name, argv, reply in paths:
            renv, rd = self.relay(f"no-reads-{name}", script)
            relays.append(rd)
            rc, out, err = self.run_engine("ask", "--job-dir", f"no-reads-{name}", *argv, env_extra=dict(env, **renv))
            self.assertEqual(rc, 0, f"{name}: {out}{err}")
            if reply:
                rc, out, err = self.run_engine("reply", f"no-reads-{name}", "--", "more", env_extra=dict(env, **renv))
                self.assertEqual(rc, 0, f"{name} reply: {out}{err}")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", f"[[STUB:limit={d / 'lim-ladder'}]] q",  # unrouted ladder
                                       env_extra=dict(env, BRIDGE_ROUTER="off"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.check_records(env)), reads, "a launch path read the Codex catalog through the models check")
        self.assertEqual(len(self.page_calls("no-reads")), pages, "a launch path fetched a page")
        self.assertTrue(self.registry_reads(env), "the routing read no account catalog through the registry (docs/model-routing.md 3.3)")
        calls = [c for rd in relays for c in self.records(rd, "calls.jsonl")]
        verbs = {tuple(c["argv"][:2]) if c["argv"][:1] == ["codex"] else c["argv"][0] for c in calls}
        self.assertLessEqual(verbs, {"pick", "exec", "hit", "transplant", "accounts", ("codex", "accounts"), ("codex", "home")}, verbs)
        self.assertEqual(self.seat(self.jobs / "no-reads-fallback", "astra")["model"], head("sol"))
        self.assertEqual({p[p.index("--model") + 1] for rd in relays for p in self.picks(rd)}, {"astra", "sol", "luna", "sonnet"})

    def test_a_startup_rejection_surfaces_the_cli_error_first_and_descends_only_through_the_preference_list(self):
        # changed for docs/model-routing.md 3.3 and 5 (F3): the table's fallback edge is a suggestion, never applied on its own. An
        # `astra|sol` seat descends to the Sol head once no account can run Astra (unrouted: this machine's login rejected it), the
        # CLI's own error first; a plain `astra` seat ends on the rejection and names the spec that would have allowed the descent
        astra, sol = head("astra"), head("sol")
        env, _ = self.isolated("reject-astra")
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "reject-astra", "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [astra, sol])
        seat = self.seat(self.jobs / "reject-astra", "astra")
        self.assertEqual((seat["model"], seat["attempt"]), (sol, 2))
        lead = leading_notices(out)
        self.assertTrue(lead and f"model {astra} is not supported" in lead[0], out)
        self.assertTrue(any(l.startswith(f"NOTICE: seat astra: model {astra} -> {sol} (no capable account for {astra}: ") and "attempt 2)" in l for l in lead), lead)
        self.assertIn("startup-rejected", self.kinds(self.jobs / "reject-astra"))
        self.assertIn("fallback-model", self.kinds(self.jobs / "reject-astra"))
        # DESIGN §1 records: what was requested (the whole spec), what runs, the list and the descent; notices are {kind, seat, text, at}
        self.assertEqual((seat.get("seat_name"), seat.get("family_requested"), seat.get("model_requested")), ("astra|sol", "astra", astra))
        self.assertEqual((seat.get("preference"), seat["provenance"]["downgraded_from"]), ([sol], "astra"))
        self.assertIs(seat.get("effort_explicit"), True)
        self.assertTrue(seat.get("fallbacks"), seat)
        for notice in self.notices(self.jobs / "reject-astra"):
            self.assertLessEqual({"kind", "seat", "text", "at"}, set(notice), notice)
        # without a list the seat ends on the rejection: nothing else launches, and the failure names the spec that would allow it
        env, _ = self.isolated("reject-astra-plain")
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "reject-astra-plain", "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [astra])
        seat = self.seat(self.jobs / "reject-astra-plain", "astra")
        self.assertEqual((seat["model"], seat["state"]), (astra, "failed"))
        self.assertIn("the seat has no preference list, so it does not descend (say astra|sol:low to allow it)", seat["activity"])
        self.assertNotIn("fallback-model", self.kinds(self.jobs / "reject-astra-plain"))
        self.assertTrue(leading_notices(out) and f"model {astra} is not supported" in leading_notices(out)[0], out)
        # routed: relay is asked with the family aliases, astra on every capable account, then sol
        env, _ = self.isolated("reject-astra-routed")
        renv, rd = self.relay("reject-astra-routed", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([p[p.index("--model") + 1] for p in self.picks(rd)], ["astra", "astra", "sol"])
        # astra and sol both rejected: the list is exhausted after exactly two launches (Terra never)
        env, _ = self.isolated("reject-both")
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "reject-both", "--", f"[[STUB:badmodel={astra}+{sol}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [astra, sol])
        seat = self.seat(self.jobs / "reject-both", "astra")
        self.assertEqual(seat["state"], "failed")
        self.assertIn("the seat's preference list is exhausted", seat["activity"])
        self.assertTrue(leading_notices(out) and f"model {astra} is not supported" in leading_notices(out)[0], out)
        # Sol, Terra and Luna end on rejection: no fallback launch
        for fam in ("sol", "terra", "luna"):
            with self.subTest(family=fam):
                env, _ = self.isolated(f"reject-{fam}")
                rc, out, err = self.run_engine("ask", "--seats", f"{fam}:low", "--", f"[[STUB:badmodel={head(fam)}]] q", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(self.launched_models(env), [head(fam)])
                self.assertTrue(leading_notices(out) and f"model {head(fam)} is not supported" in leading_notices(out)[0], out)

    def test_a_fallback_target_is_validated_initially_with_its_own_floor_and_markers(self):
        # the `|` list's target has its own floor: with Sol's floor raised to 0.156.1 in a case table (the shipped table v17 floors
        # Astra and Sol both at 0.153.0), Codex 0.155.0 runs Astra (>= 0.153.0) but not Sol: the descent is refused (a notice says
        # why), the list is exhausted, and the seat fails after the one Astra launch
        astra, sol = head("astra"), head("sol")
        self.assertEqual(TABLE["models"][astra]["min_cli"], "0.153.0")
        table = self.table_file("fallback-floor", lambda t: t["models"][sol].__setitem__("min_cli", "0.156.1"))
        env, _ = self.isolated("fallback-floor", table=table, STUB_CODEX_VERSION="codex-cli 0.155.0")
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "fallback-floor", "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [astra])  # the target failed its initial validation: never launched
        seat = self.seat(self.jobs / "fallback-floor", "astra")
        self.assertEqual(seat["state"], "failed")
        self.assertIn("needs Codex CLI >= 0.156.1", seat["activity"] + out)
        self.assertIn("descent-refused", self.kinds(self.jobs / "fallback-floor"))
        self.assertIn("the seat's preference list is exhausted", seat["activity"])
        # markers on the target: GroupE_MarkersAcrossPaths.test_a_fallback_target_is_validated_initially_with_its_markers

    def test_validation_refusals_never_become_fallback_launches(self):
        astra = head("astra")
        floor = TABLE["models"][astra]["min_cli"]
        env, _ = self.isolated("refusal-no-fallback", STUB_CODEX_VERSION="codex-cli 0.152.0")
        before = self.job_names()
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", "q", env_extra=env)
        self.assertIn(f"needs Codex CLI >= {floor}", self.assert_refused(rc, out, err))
        self.assertEqual(self.launched_models(env), [])  # neither astra nor its fallback target
        self.assertEqual(self.new_jobs(before), [])
        for spec in ("astra:bogus", f"{RETIRED_SOL}:low"):
            with self.subTest(spec=spec):
                env, _ = self.isolated("refusal-no-fallback-" + spec.replace(":", "-"))
                rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
                self.assert_refused(rc, out, err)
                self.assertEqual(self.launched_models(env), [])

    def test_effort_rejection_descends_only_through_the_effort_preference_list_and_is_reported(self):
        # changed for docs/model-routing.md 5 (F3): the table's effort_fallback ladder is a suggestion the bridge names, never applies;
        # `astra:ultra|max` descends to max once every capable account rejected ultra
        env, _ = self.isolated("effort-reject")
        rc, out, err = self.run_engine("ask", "--seats", "astra:ultra|max", "--job-dir", "effort-reject", "--", "[[STUB:badeffort=ultra]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([r["effort"] for r in self.codex_records(env, "exec")], ["ultra", "max"])
        self.assertEqual(self.seat(self.jobs / "effort-reject", "astra")["effort"], "max")
        self.assertIn("fallback-effort", self.kinds(self.jobs / "effort-reject"))
        self.assertTrue(any(l.startswith("NOTICE: seat astra: ") and "ultra -> max" in l for l in leading_notices(out)), out)
        self.assertIn(f"astra={head('astra')}@max", out)
        # the list is the seat's own: Luna at max, listing xhigh, falls to xhigh
        env, _ = self.isolated("effort-reject-luna")
        rc, out, err = self.run_engine("ask", "--seats", "luna:max|xhigh", "--", "[[STUB:badeffort=max]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([r["effort"] for r in self.codex_records(env, "exec")], ["max", "xhigh"])
        self.assertTrue(any("max -> xhigh" in l for l in leading_notices(out)), out)
        # without an effort list the seat ends on the rejection and names the spec that would allow the descent
        env, _ = self.isolated("effort-reject-plain")
        rc, out, err = self.run_engine("ask", "--seats", "astra", "--job-dir", "effort-reject-plain", "--", "[[STUB:badeffort=ultra]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual([r["effort"] for r in self.codex_records(env, "exec")], ["ultra"])
        seat = self.seat(self.jobs / "effort-reject-plain", "astra")
        self.assertEqual((seat["effort"], seat["state"]), ("ultra", "failed"))
        self.assertIn("the seat has no effort preference list, so it does not descend (say astra:ultra|max to allow it)", seat["activity"])
        self.assertNotIn("fallback-effort", self.kinds(self.jobs / "effort-reject-plain"))

    def test_first_launch_at_drives_the_mode_and_started_stays_a_timestamp(self):
        env, _ = self.isolated("first-launch")
        renv, rd = self.relay("first-launch", {"pick": {"codex": [{"exit": 1, "wake_at": time.time() + 4}, {"exit": 0, "label": "a"}]}})
        env.update(renv)
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "first-launch", "--", "q")
        d = self.jobs / "first-launch"
        try:
            waiting = self.wait_for(d, "luna", lambda s: s.get("state") == "waiting")
            self.assertIn("first_launch_at", waiting)  # a new seat record: the flag exists and is null until the first launch
            self.assertIsNone(waiting["first_launch_at"])
            self.assertIsInstance(waiting["started"], (int, float))
            self.assertLess(abs(waiting["started"] - time.time()), 120)  # epoch seconds, as bridge.py:1370 writes it
        finally:
            out, err = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 0, out + err)
        seat = self.seat(d, "luna")
        self.assertIsInstance(seat["first_launch_at"], (int, float))
        self.assertGreaterEqual(seat["first_launch_at"], seat["started"])
        rc, out, err = self.run_engine("status", "first-launch", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"(?m)^luna\s+\S+\s+a\s+done\s+\d+(m\d\ds|s)\s")  # the elapsed column still comes from `started`
        # a reply seat record starts null again (its own operation); a slow account verification shows it before launch
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "accounts_sleep": 3}))
        p = self.start_engine(env, "reply", "first-launch", "--", "more")
        try:
            reply = None
            for _ in range(150):
                files = sorted(d.glob("seat-luna-reply-*.json"))
                if files and "verifying account" in json.loads(files[0].read_text()).get("activity", ""):
                    reply = json.loads(files[0].read_text())
                    break
                time.sleep(0.1)
            self.assertIsNotNone(reply, "the reply never reached its account verification")
            self.assertIn("first_launch_at", reply)
            self.assertIsNone(reply["first_launch_at"])
        finally:
            out, err = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIsNotNone(json.loads(sorted(d.glob("seat-luna-reply-*.json"))[0].read_text())["first_launch_at"])
        # a marker refusing in `initial` mode before any launch: GroupE_MarkersAcrossPaths

    def test_observed_model_mismatch_fails_the_seat_with_output_kept(self):
        opus, sonnet = head("opus"), head("sonnet")
        cases = {"ranmodels": f"ranmodels={opus}+{sonnet}", "assistant": f"assistantmodel={K1_ID}", "init": f"ranmodels={K1_ID}"}
        for name, directive in cases.items():
            with self.subTest(case=name):
                env, _ = self.isolated(f"mismatch-{name}")
                job = f"mismatch-{name}"
                rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", job, "--",
                                               f"[[STUB:{directive},text=MISMATCH-ANSWER-{name}]] q", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                d = self.jobs / job
                seat = self.seat(d, "opus")
                self.assertEqual(seat["state"], "failed")
                other = sonnet if name == "ranmodels" else K1_ID
                self.assertRegex(seat["activity"], rf"ran .*{re.escape(other)}.*requested {re.escape(opus)}")
                self.assertIn(f"MISMATCH-ANSWER-{name}", (d / "raw-opus.md").read_text())  # the output is kept
                self.assertIn("ran-model", self.kinds(d))
                self.assertTrue(any(other in l for l in leading_notices(out)), out)
                self.assertEqual(self.meta(d)["state"], "failed")

    def test_cumulative_usage_keys_are_not_attributed_to_this_attempt(self):
        opus = head("opus")
        fixture = EVIDENCE / "claude_stream_resumed_session.jsonl"
        for name, directive in (("usagekeys", f"usagekeys={K1_ID}+{opus}"), ("replayed-stream", f"replay={fixture}")):
            with self.subTest(case=name):
                env, _ = self.isolated(f"cumulative-{name}")
                rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", f"cumulative-{name}", "--", f"[[STUB:{directive}]] q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.jobs / f"cumulative-{name}"
                seat = self.seat(d, "opus")
                self.assertEqual(seat["state"], "done")
                obs = self.observation(seat)
                self.assertEqual(obs["models_observed"], [opus])
                self.assertEqual(sorted(obs["session_models_cumulative"]), sorted([K1_ID, opus]))
                self.assertTrue(obs["complete"])
                self.assertIn("usage-unexplained", self.kinds(d))
                self.assertNotIn("ran-model", self.kinds(d))
                self.assertTrue(any(K1_ID in n.get("text", "") for n in self.notices(d) if n.get("kind") == "usage-unexplained"))

    def test_forwarded_and_synthetic_messages_are_ignored(self):
        opus = head("opus")
        # each synthetic predicate on its own, where the stream carries it (DESIGN §4: message.model "<synthetic>" on the
        # message; error, is_api_error_message and api_error on the event), then all of them beside a forwarded message
        cases = (("synthetic-model", "synthetic=model"), ("rate-limit", "synthetic=rate_limit"),
                 ("api-error-message", "synthetic=api_error_message"), ("usage-credits", "synthetic=usage_credits"),
                 ("forwarded-synthetic", f"forwarded={head('haiku')},synthetic"))
        for job, directive in cases:
            with self.subTest(case=job):
                env, _ = self.isolated(job)
                rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", job, "--", f"[[STUB:{directive}]] q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.jobs / job
                seat = self.seat(d, "opus")
                self.assertEqual(seat["state"], "done")
                self.assertEqual(self.observation(seat)["models_observed"], [opus])
                self.assertTrue(self.observation(seat)["complete"])
                self.assertNotIn("ran-model", self.kinds(d))
        # the same message without any of those flags is an ordinary observation of another model: a mismatch
        env, _ = self.isolated("synthetic-unflagged")
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", "synthetic-unflagged", "--", "[[STUB:synthetic=unflagged]] q",
                                       env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.jobs / "synthetic-unflagged"
        self.assertEqual(self.seat(d, "opus")["state"], "failed")
        self.assertIn(OTHER_OPUS, self.observation(self.seat(d, "opus"))["models_observed"])
        self.assertIn("ran-model", self.kinds(d))

    def test_an_incomplete_observation_is_a_notice_with_unchanged_outcome(self):
        for directive, state, code in (("nousage", "done", 0), ("noinitmodel", "done", 0), ("truncate", "failed", 1)):
            with self.subTest(case=directive):
                env, _ = self.isolated(f"incomplete-{directive}")
                job = f"incomplete-{directive}"
                rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", job, "--", f"[[STUB:{directive}]] q", env_extra=env)
                self.assertEqual(rc, code, out + err)  # the exit code the seat would have had anyway
                d = self.jobs / job
                seat = self.seat(d, "opus")
                self.assertEqual(seat["state"], state)
                self.assertEqual(self.meta(d)["state"], state)
                self.assertFalse(self.observation(seat)["complete"])
                self.assertIn("observation-incomplete", self.kinds(d))
                self.assertTrue(leading_notices(out), out)

    def test_a_refused_reply_leaves_the_parent_outcome_unchanged(self):
        env, _ = self.isolated("refused-reply")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "refused-reply", "--", "[[STUB:text=PARENT-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "refused-reply"
        rc, out, err = self.run_engine("reply", "refused-reply", "--effort", "ultra", "--", "more", env_extra=env)  # Luna has no ultra
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [head("luna")])  # only the original ask ran
        replies = sorted(d.glob("seat-luna-reply-*.json"))
        self.assertEqual(len(replies), 1)
        self.assertEqual(json.loads(replies[0].read_text())["state"], "failed")
        m = self.meta(d)
        self.assertEqual(m["state"], "done")
        self.assertEqual(m.get("active_replies"), {})
        self.assertIsNone(m.get("replying"))
        self.assertTrue(any("ultra" in n.get("text", "") for n in m.get("notices") or []), m.get("notices"))
        rc, out, err = self.run_engine("result", "refused-reply", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: "), out)
        self.assertIn("PARENT-ANSWER", out)
        # the same with a marker on the head: GroupE_MarkersAcrossPaths.test_a_refused_reply_on_a_marked_head_...

    def test_notices_lead_every_output(self):
        astra, sol = head("astra"), head("sol")
        bad = f"[[STUB:badmodel={astra}]]"
        env, _ = self.isolated("notices-lead")
        # changed for docs/model-routing.md 5 (F3): the seats carry the `astra|sol` list, the only way a rejected Astra reaches Sol
        fallback = re.compile(r"NOTICE: seat \S+: model " + re.escape(astra) + " -> " + re.escape(sol) + r" \(no capable account for ")
        # ask: stdout, last-<tag>.md (notices + answer), raw-<tag>.md (the raw answer), the footer label
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "notices-ask", "--", f"{bad} q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "notices-ask"
        self.assertTrue(any(fallback.match(l) for l in leading_notices(out)), out)
        self.assertTrue((d / "last-astra.md").read_text().startswith("NOTICE: "))
        self.assertIn("OK", (d / "last-astra.md").read_text())
        self.assertEqual((d / "raw-astra.md").read_text().strip(), "OK")
        self.assertRegex(out, r"(?m)^— astra=" + re.escape(sol) + r"@low \(fell back from " + re.escape(astra) + r"\)")
        for argv in (["result", "notices-ask"], ["result", "notices-ask", "--raw"]):
            with self.subTest(retrieval=argv):
                rc, out, err = self.run_engine(*argv, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(out.startswith("NOTICE: "), out)
        self.assertIn("raw-astra.md", self.run_engine("result", "notices-ask", "--raw", env_extra=env)[1])
        # council and fanout without a schema: stdout and council.md
        rc, out, err = self.run_engine("council", "--seats", "astra|sol:low,luna:low", "--job-dir", "notices-council", "--", f"{bad} q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: "), out)
        self.assertTrue((self.jobs / "notices-council" / "council.md").read_text().startswith("NOTICE: "))
        # review and fanout with a schema: stdout and merged.md (line 2 `Notices:`)
        rc, out, err = self.run_engine("review", "--seats", "astra|sol:low", "--job-dir", "notices-review", "--focus", bad, env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: "), out)
        self.assertTrue((self.jobs / "notices-review" / "merged.md").read_text().splitlines()[1].startswith("Notices:"))
        rc, out, err = self.run_engine("result", "notices-review", env_extra=env)
        self.assertTrue(out.startswith("NOTICE: "), out)
        for name, schema in (("notices-fanout", True), ("notices-fanout-plain", False)):
            with self.subTest(fanout=name):
                spec = self.case_dir("notices-lead") / f"{name}.json"
                spec.write_text(json.dumps({"seats": "astra|sol:low", "schema": schema, "tasks": {"one": f"{bad} look"}}))
                rc, out, err = self.run_engine("fanout", "--spec", str(spec), "--job-dir", name, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(out.startswith("NOTICE: "), out)
                artifact = self.jobs / name / ("merged.md" if schema else "council.md")
                text = artifact.read_text()
                self.assertTrue(text.splitlines()[1].startswith("Notices:") if schema else text.startswith("NOTICE: "), text[:300])
        # task, and a failed-seat path (the list exhausted)
        rc, out, err = self.run_engine("task", "--seats", "astra|sol:low", "--", f"{bad} t", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: "), out)
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--", f"[[STUB:badmodel={astra}+{sol}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(out.startswith("NOTICE: "), out)
        # MCP results (claude_ask, a detached claude_review via claude_result): codex-skills/claude/tests/test_mcp_server.py

    def test_an_attempt_that_ran_another_model_fails_the_seat_before_any_retry(self):
        # Astra C2-1: each Claude attempt's observation is judged and persisted before any retry or re-route. Attempt 1 runs
        # Sonnet beside Opus and then hits a usage limit: the seat fails there with the ran-model notice (a mismatch is
        # terminal, nothing launches after it), and finishing the seat does not judge that observation a second time
        opus, sonnet = head("opus"), head("sonnet")
        for name, routed in (("ladder", False), ("re-route", True)):
            with self.subTest(path=name):
                job = f"ran-then-limited-{name}"
                env, cd = self.isolated(job)
                if routed:
                    renv, rd = self.relay(job, {"pick": {"claude": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
                    env.update(renv)
                rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", job, "--",
                                               f"[[STUB:ranmodels={opus}+{sonnet},limit={cd / 'limit'}]] q", env_extra=env)
                d = self.jobs / job
                ran = self.seat_notices(d, "ran-model", "opus")
                self.assertEqual(len(ran), 1, f"attempt 1 ran {sonnet}, then hit a usage limit: its observation must be judged once, "
                                              f"before the retry; notices {self.notices(d)}\n{out}{err}")
                self.assertIn(sonnet, ran[0])
                self.assertEqual(len(self.claude_records(env)), 1, "nothing may launch after a model mismatch")
                self.assertEqual(rc, 1, out + err)
                seat = self.seat(d, "opus")
                self.assertEqual(seat["state"], "failed")
                self.assertIn(sonnet, self.observation(seat)["models_observed"])
                self.assertTrue(any(sonnet in l for l in leading_notices(out)), out)
                if routed:
                    self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])

    def test_a_marker_learned_while_a_retry_waits_is_a_notice_before_it_launches(self):
        # Astra C2-2: continuation-mode validation runs immediately before each retry's CLI invocation, after the account
        # selection and its waits (and a Claude session's transplant), and before an effort-fallback retry too. A marker learned
        # meanwhile becomes the continuation notice and the seat continues (continuation mode never refuses a running seat)
        luna, sonnet, astra = head("luna"), head("sonnet"), head("astra")
        scenarios = (("codex-re-route", "luna:low", luna, "codex"), ("claude-re-route", "sonnet:low", sonnet, "claude"),
                     ("codex-effort-fallback", "astra:ultra|max", astra, None))  # the effort list allows the descent (docs/model-routing.md 5)
        for name, spec, model, vendor in scenarios:
            with self.subTest(scenario=name):
                job = f"marker-before-retry-{name}"
                env, cd = self.isolated(job)
                tag = spec.split(":")[0]
                if vendor:  # a limit on account a; the next account is offered only after a wait, during which the marker lands
                    renv, _ = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + 6},
                                                                 {"exit": 0, "label": "b"}]}})
                    env.update(renv)
                    # changed for S-LAUNCH: the routed attempt on a shows that its CLI started (Codex: thread.started before the
                    # stderr-only limit; Claude's init event already does), so its retry is a continuation. Without any event it
                    # has not started: GroupB_SecondReReviewLaunch.test_a_routed_attempt_without_any_event_leaves_the_next_...
                    directive, parked = f"threadfirst,limit={cd / 'limit'}", (lambda s: s.get("state") == "waiting")
                else:  # astra's default ultra is rejected at startup (ultra -> max); the marker lands while attempt 1 runs
                    directive, parked = "sleep=2,badeffort=ultra", (lambda s: s.get("first_launch_at") is not None)
                p = self.start_engine(env, "ask", "--seats", spec, "--job-dir", job, "--", f"[[STUB:{directive}]] q")
                try:
                    self.wait_for(self.jobs / job, tag, parked)
                    self.write_marker(env, model, "deprecated")  # another invocation's check learns a marker meanwhile
                finally:
                    out, err = p.communicate(timeout=120)
                self.assertEqual(p.returncode, 0, out + err)  # the seat continues
                self.assertEqual(self.launched_models(env), [model, model])
                d = self.jobs / job
                self.assertTrue([t for t in self.seat_notices(d, "models-marker", tag) if model in t],
                                f"the retry launched after a marker was learned, without its notice: {self.notices(d)}\n{out}")
                self.assertTrue(any(l.startswith(f"NOTICE: seat {tag}: {model} was marked ") and l.endswith("after this seat started; the seat continues")
                                    for l in leading_notices(out)), out)

    def test_result_prints_a_raw_answer_verbatim(self):
        # Astra C2-3: raw-<tag>.md holds the model's answer without bridge notices and is never altered on retrieval (only a
        # composed artifact drops the notice block the bridge added): an answer whose first line reads like a notice keeps it,
        # whether or not the job has notices of its own
        answer = "NOTICE: maintenance starts at 22:00"
        for name, spec, stubs in (("without-notices", "luna:low", f"text={answer}"),
                                  ("with-notices", "astra|sol:low", f"badmodel={head('astra')},text={answer}")):
            with self.subTest(job=name):
                job = f"raw-verbatim-{name}"
                env, _ = self.isolated(job)
                rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", f"[[STUB:{stubs}]] q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                tag = spec.split(":")[0].split("|")[0]  # the seat's tag is its first name; the list is the seat's descent
                self.assertEqual((self.jobs / job / f"raw-{tag}.md").read_text(), answer)
                self.assertIn(answer, out)  # the ask printed it
                rc, out, err = self.run_engine("result", job, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(answer, out, "result dropped the raw answer's first line")

    def test_retrieval_takes_each_answer_from_its_own_raw_or_last_file(self):
        # Astra C2-4: retrieval builds one artifact per seat and per reply (its raw-<tag>.md, else its last-<tag>.md) instead of
        # ignoring every last-<tag>.md once any raw file exists, and plain `result` prints the latest answer of that set
        env, _ = self.isolated("per-answer")
        # a job written before the raw/last split, then a reply: `result --raw` still includes the original answer
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "per-answer-pre-split", "--",
                                       "[[STUB:text=ORIGINAL-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "per-answer-pre-split"
        os.replace(d / "raw-luna.md", d / "last-luna.md")  # as the bridge before the split left it: the answer only in last-luna.md
        rc, out, err = self.run_engine("reply", "per-answer-pre-split", "--", "[[STUB:text=REPLY-ANSWER]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_engine("result", "per-answer-pre-split", "--raw", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("REPLY-ANSWER", out)
        self.assertIn("ORIGINAL-ANSWER", out, "result --raw left out the answer of the seat that has no raw file")
        # a newer Codex reply whose answer was recovered from the JSON events (-o wrote nothing, bridge.py:1989) is saved only in
        # last-<reply tag>.md: plain `result` prints it, not the parent's older raw answer
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "per-answer-recovered", "--",
                                       "[[STUB:text=PARENT-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "per-answer-recovered"
        rc, out, err = self.run_engine("reply", "per-answer-recovered", "--", "[[STUB:text=RECOVERED-REPLY]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        (reply_raw,) = d.glob("raw-luna-reply-*.md")
        reply_raw.unlink()
        self.assertIn("RECOVERED-REPLY", next(d.glob("last-luna-reply-*.md")).read_text())
        rc, out, err = self.run_engine("result", "per-answer-recovered", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RECOVERED-REPLY", out, "result printed an answer older than the latest reply")
        self.assertNotIn("PARENT-ANSWER", out)

    def test_a_reply_that_moves_to_the_head_discloses_it_in_its_own_artifact(self):
        # Astra C2-5: the reply-model (migration) notice is recorded against the replied-to seat, but it must also lead the
        # reply's own composed artifact (last-<seat>-reply-<id>.md), which otherwise presents the answer of a substituted model
        env, _ = self.isolated("reply-artifact-notice")
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", "reply-artifact-notice", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-artifact-notice"
        self.as_old_record(d, "opus", "opus")  # old Claude records store the alias: the reply runs on the head, with a notice
        rc, out, err = self.run_engine("reply", "reply-artifact-notice", "--", "[[STUB:text=REPLY-ANSWER]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        moved = [n["text"] for n in self.notices(d) if n.get("kind") == "reply-model"]
        self.assertTrue(moved, self.notices(d))
        self.assertIn(moved[0], leading_notices(out))  # stdout discloses it already (GroupC)
        (artifact,) = d.glob("last-opus-reply-*.md")
        text = artifact.read_text()
        self.assertIn(moved[0], leading_notices(text), f"{artifact.name} does not disclose the substitution:\n{text}")
        self.assertIn("REPLY-ANSWER", text)

    def test_a_cancelled_attempt_still_records_its_observation_notices(self):
        # Astra C2-6: a Claude attempt cancelled after its stream switched models, or before its result arrived, still has its
        # observation judged: the ran-model / observation-incomplete notices are recorded, while cancellation stays the seat's
        # and the job's terminal state and decides the exit status. The stub replays a stream the test feeds through a pipe
        opus, sonnet = head("opus"), head("sonnet")
        usage = {"input_tokens": 2, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 0, "output_tokens": 10}

        def assistant(n, model, content):
            return {"type": "assistant", "parent_tool_use_id": None,
                    "message": {"id": f"msg_{n}", "model": model, "usage": usage, "content": content}}
        init = {"type": "system", "subtype": "init", "session_id": "stub-session-cancelled", "model": opus}
        tool = [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]
        cases = (("switched", [init, assistant(1, opus, tool), assistant(2, sonnet, [{"type": "text", "text": "more"}])],
                  [opus, sonnet], {"ran-model", "observation-incomplete"}),
                 ("before-the-result", [init, assistant(1, opus, tool)], [opus], {"observation-incomplete"}))
        for name, events, observed, expected in cases:
            with self.subTest(case=name):
                job = f"cancel-observed-{name}"
                env, cd = self.isolated(job)
                fifo = cd / "stream.fifo"
                os.mkfifo(fifo)
                p = self.start_engine(env, "ask", "--seats", "opus:low", "--job-dir", job, "--", f"[[STUB:replay={fifo}]] q")
                fd = None
                try:
                    fd = self.feed_stream(fifo, p, events)
                    self.wait_for(self.jobs / job, "opus", lambda s: s.get("turns") == len(events) - 1)  # every message is in
                    rc, out, err = self.run_engine("cancel", job, env_extra=env)
                    self.assertEqual(rc, 0, out + err)
                finally:
                    if fd is None:  # the stream never started: release a stub still waiting on the pipe
                        try:
                            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                        except OSError:
                            pass
                    if fd is not None:
                        os.close(fd)
                    out, err = p.communicate(timeout=60)
                d = self.jobs / job
                seat = self.seat(d, "opus")
                self.assertEqual(p.returncode, 1, out + err)
                self.assertEqual((seat["state"], self.meta(d)["state"]), ("cancelled", "cancelled"))
                self.assertRegex(out, r"(?m)^— opus=.* CANCELLED")  # the footer reports the cancellation, not a failure
                self.assertEqual(self.observation(seat)["models_observed"], observed)
                kinds = set(self.kinds(d))
                self.assertLessEqual(expected, kinds, f"the cancelled attempt's observation notices are missing: {self.notices(d)}\n{out}")
                self.assertEqual("ran-model" in kinds, "ran-model" in expected)

    def test_structured_reports_print_each_notice_once(self):
        # Astra C2-7: stdout leads with the job's notices, so a review, a schema fanout and a structured `result` print each
        # notice once; the `Notices:` block stays in merged.md (line 2) and is not repeated in the body printed after the lead
        astra = head("astra")
        env, _ = self.isolated("notice-once")
        bad = f"[[STUB:badmodel={astra}]]"

        def as_text(line):  # a line of the merged Markdown as plain text (merge_findings.markdown_text's escapes undone)
            return re.sub(r"\\(.)", r"\1", line).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

        spec = self.case_dir("notice-once") / "fanout.json"
        spec.write_text(json.dumps({"seats": "astra|sol:low", "schema": True, "tasks": {"one": f"{bad} look"}}))
        for name, argv in (("review", ["review", "--seats", "astra|sol:low", "--job-dir", "notice-once-review", "--focus", bad]),
                           ("fanout", ["fanout", "--spec", str(spec), "--job-dir", "notice-once-fanout"])):
            rc, out, err = self.run_engine(*argv, env_extra=env)
            self.assertEqual(rc, 0, out + err)
            d = self.jobs / f"notice-once-{name}"
            texts = [n["text"] for n in self.notices(d)]
            self.assertTrue(texts, f"{name}: the job has no notices\n{out}")
            merged = (d / "merged.md").read_text()
            self.assertTrue(merged.splitlines()[1].startswith("Notices:"), f"{name}: merged.md lost its Notices block:\n{merged[:600]}")
            for label, printed in ((name, out), (f"result of the {name}", self.run_engine("result", d.name, env_extra=env)[1])):
                with self.subTest(output=label):
                    self.assertTrue(printed.startswith("NOTICE: "), printed)
                    lines = printed.splitlines()
                    self.assertEqual([sum(1 for l in lines if t in as_text(l)) for t in texts], [1] * len(texts),
                                     f"times stdout carries each of the job's notices {texts}:\n{printed}")

    def test_a_marker_notice_is_persisted_once_per_marker(self):
        # Astra C2-8: every continuation check re-reads the same sticky marker; its notice is persisted once per seat and marker
        # (identity and severity), so a seat moved from account to account does not pile up copies of it, while the same
        # model's marker turning more severe (deprecated -> retired) is announced again
        luna = head("luna")
        env, _ = self.isolated("marker-once")
        renv, rd = self.relay("marker-once", {"pick": {"codex": [{"exit": 0, "label": x} for x in "abcde"] +
                                                                 [{"exit": 1, "wake_at": time.time() + 100000}]}})
        env.update(renv)
        # changed for S-LAUNCH: each routed attempt shows that its CLI started (thread.started, then the 2 s and the stderr-only limit),
        # so first_launch_at is stamped while attempt 1 runs and every later check is a continuation
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "marker-once", "--", "[[STUB:threadfirst,sleep=2,alwayslimit]] q")
        try:
            self.wait_for(self.jobs / "marker-once", "luna", lambda s: s.get("first_launch_at") is not None)
            self.write_marker(env, luna, "deprecated")  # learned while attempt 1 runs: every later check reads it
            for _ in range(300):
                if self.exec_count(env) >= 3:  # attempt 3 runs: the checks before it have read `deprecated`
                    break
                time.sleep(0.1)
            self.write_marker(env, luna, "retired")
        finally:
            out, err = p.communicate(timeout=180)
        self.assertEqual(p.returncode, 1, out + err)  # every account hit its limit; the pool dead-ends
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], list("abcde"))
        texts = self.seat_notices(self.jobs / "marker-once", "models-marker", "luna")
        kinds = [m.group(1) if m else t for t, m in ((t, re.search(r" was marked (\w+) ", t)) for t in texts)]
        self.assertEqual(sorted(kinds), ["deprecated", "retired"], "one notice per marker and severity, got:\n" + "\n".join(texts))

    def test_a_notice_persisted_while_result_merges_still_leads_its_output(self):
        # Astra C2b-2: `result` of a structured job reads the job's notices after do_merge() (as review and fanout do) or reuses
        # the merger's snapshot. A seat or reply of a live job that persists a notice while `result` merges puts it in merged.md's
        # Notices block, which stdout drops from the body: stdout must lead with it, so every notice merged.md carries is printed
        # once. Here the opus seat's ran-model notice is persisted from inside the merge (merge_findings.merge wrapped in-process)
        import contextlib, io
        from unittest import mock
        astra, opus, sonnet = head("astra"), head("opus"), head("sonnet")
        job = "notice-during-merge"
        env, _ = self.isolated(job)
        rc, out, err = self.run_engine("review", "--seats", "astra|sol:low,opus:low", "--job-dir", job, "--focus", f"[[STUB:badmodel={astra}]]",
                                       env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / job
        self.assertTrue(self.notices(d), f"the review has no notices of its own:\n{out}")  # the snapshot a `result` finds first
        late = f"NOTICE: seat opus: ran {opus}, {sonnet}, requested {opus}; the seat failed (its output is kept in raw-opus.md)"
        b = load_engine_module(self.env)
        merge = b.merge_findings.merge

        def merging(job_dir, *args, **kwargs):  # the seat's observation is judged while `result` merges
            b.add_notice(job_dir, "ran-model", late, seat="opus")
            return merge(job_dir, *args, **kwargs)

        def as_text(line):  # a line of the merged Markdown as plain text (as in test_structured_reports_print_each_notice_once)
            return re.sub(r"\\(.)", r"\1", line).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

        buf = io.StringIO()
        with mock.patch.object(b.merge_findings, "merge", merging), contextlib.redirect_stdout(buf):
            code = b.main(["result", job])
        printed = buf.getvalue()
        self.assertEqual(code, 0, printed)
        lines = (d / "merged.md").read_text().splitlines()
        self.assertEqual(lines[1], "Notices:", "\n".join(lines[:8]))
        block = []
        for line in lines[2:]:
            if not line.startswith("- "):
                break
            block.append(as_text(line[2:]))
        self.assertIn(late, block, "the merge did not carry the notice persisted while it ran")
        self.assertEqual([sum(1 for l in printed.splitlines() if t in as_text(l)) for t in block], [1] * len(block),
                         f"times `result` printed each notice merged.md carries {block}:\n{printed}")
        self.assertIn(late, leading_notices(printed), printed)

    def test_a_cancel_during_a_relay_refusal_judges_no_observation(self):
        # Astra C2b-3: relay refused the account before the Claude CLI ran (a non-zero exit, no session, wake_at on its stderr)
        # and a cancel landed before the attempt returned: the attempt carries cancelled=True and an empty observation. A verified
        # pre-CLI refusal carries into finalization and its empty observation is not judged (no observation-incomplete notice for
        # a CLI that never ran); cancellation stays the seat's and the job's terminal state. The stub relay creates the job's
        # CANCELLED marker while it refuses, once the engine is feeding it the prompt (`refuse_touch`), so the cancel lands inside
        # the attempt every time. A cancelled attempt that ran keeps its judgment, even one cancelled before its first event (the
        # second case; the others: test_a_cancelled_attempt_still_records_its_observation_notices)
        judged_kinds = ("observation-incomplete", "ran-model", "usage-unexplained")
        with self.subTest(case="relay refused, then the cancel"):
            job = "cancel-during-refusal"
            env, _ = self.isolated(job)
            d = self.jobs / job
            renv, rd = self.relay(job, {"pick": {"claude": [{"exit": 0, "label": "a"}]}, "refuse": {"a": int(time.time() + 3600)},  # BUSY-1 A6: int
                                        "refuse_touch": str(d / "CANCELLED")})
            env.update(renv)
            rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", job, "--", "q", env_extra=env)
            self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"], out + err)  # relay refused a, once
            self.assertEqual(self.claude_records(env), [], "the Claude CLI started")  # the refusal came before it
            self.assertTrue((d / "CANCELLED").exists(), out + err)
            seat = self.seat(d, "sonnet")
            self.assertEqual((rc, seat["state"], self.meta(d)["state"]), (1, "cancelled", "cancelled"), out + err)
            self.assertRegex(out, r"(?m)^— sonnet=.* CANCELLED")
            judged = [n.get("text") for n in self.notices(d) if n.get("kind") in judged_kinds]
            self.assertEqual(judged, [], f"relay refused before any CLI ran, yet the attempt's empty observation was judged:\n{out}")
            self.assertFalse([l for l in leading_notices(out) if "observation" in l], out)
        with self.subTest(case="the CLI ran, then the cancel before its first event"):
            job = "cancel-before-first-event"
            env, cd = self.isolated(job)
            d = self.jobs / job
            renv, _ = self.relay(job, {"pick": {"claude": [{"exit": 0, "label": "a"}]}})
            env.update(renv)
            fifo = cd / "stream.fifo"
            os.mkfifo(fifo)
            p = self.start_engine(env, "ask", "--seats", "sonnet:low", "--job-dir", job, "--", f"[[STUB:replay={fifo}]] q")
            fd = None
            try:
                fd = self.feed_stream(fifo, p, [])  # the CLI runs on account a and has printed nothing
                rc, out, err = self.run_engine("cancel", job, env_extra=env)
                self.assertEqual(rc, 0, out + err)
            finally:
                if fd is None:  # the stream never started: release a stub still waiting on the pipe
                    try:
                        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError:
                        pass
                if fd is not None:
                    os.close(fd)
                out, err = p.communicate(timeout=60)
            self.assertEqual([r["account"] for r in self.claude_records(env)], ["a"], out + err)  # the CLI ran
            seat = self.seat(d, "sonnet")
            self.assertEqual((p.returncode, seat["state"], self.meta(d)["state"]), (1, "cancelled", "cancelled"), out + err)
            self.assertEqual([n.get("kind") for n in self.notices(d) if n.get("kind") in judged_kinds], ["observation-incomplete"],
                             f"a cancelled attempt that ran lost the judgment of its observation:\n{out}")


# ==================================================================================================================
# DESIGN §8 group C: reply
# ==================================================================================================================
class GroupC_Reply(EngineBase):
    """DESIGN §2 "Reply": the base is the record's continuation, else what the record says ran; the reply runs on the head of
    that family (a notice when it moves); the K1 seat by its recorded identity only; no family -> exit 1; efforts from the
    table. §4 "Reply outcome": a refused reply exits 1 and leaves the parent's outcome alone."""

    def rewrite_seat(self, d, tag, **changes):
        p = Path(d) / f"seat-{tag}.json"
        s = json.loads(p.read_text())
        for k, v in changes.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        p.write_text(json.dumps(s))
        return s

    def as_old_record(self, d, tag, model):
        """What a bridge before this change recorded: only the model string (an ID, or a Claude alias)."""
        return self.rewrite_seat(d, tag, model=model, seat_name=None, family_requested=None, model_requested=None, continuation=None)

    def test_reply_on_a_superseded_or_alias_record_continues_on_the_head_with_a_notice(self):
        sol, opus = head("sol"), head("opus")
        env, _ = self.isolated("reply-superseded")
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", "reply-superseded", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-superseded"
        self.as_old_record(d, "sol", RETIRED_SOL)
        rc, out, err = self.run_engine("reply", "reply-superseded", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        last = self.codex_records(env, "exec")[-1]
        self.assertEqual((last["model"], last["resume"]), (sol, "stub-thread-01a0"))
        self.assertIn(f"NOTICE: seat sol: reply runs on {sol} (recorded {RETIRED_SOL} is superseded)", leading_notices(out))
        self.assertIn("reply-model", [n.get("kind") for n in self.notices(d)])
        env, _ = self.isolated("reply-alias")
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", "reply-alias", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-alias"
        self.as_old_record(d, "opus", "opus")  # old Claude records store the alias
        rc, out, err = self.run_engine("reply", "reply-alias", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        last = self.claude_records(env)[-1]
        self.assertEqual((last["model"], last["resume"]), (opus, "stub-session-aaaa"))
        self.assertIn(f"NOTICE: seat opus: reply runs on {opus} (recorded as the alias 'opus'; what ran is not recorded)", leading_notices(out))

    def test_two_replies_follow_the_continuation_not_the_requested_family(self):
        astra, sol = head("astra"), head("sol")
        env, _ = self.isolated("two-replies")
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "two-replies", "--", f"[[STUB:badmodel={astra}]] first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "two-replies"
        seat = self.seat(d, "astra")
        self.assertEqual((seat.get("model"), seat.get("family_requested")), (sol, "astra"))
        for n in (1, 2):
            with self.subTest(reply=n):
                rc, out, err = self.run_engine("reply", "two-replies", "--", f"reply {n}", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.codex_records(env, "exec")[-1]["model"], sol)  # the continuation, never back to astra
                self.assertFalse(any("reply runs on" in l for l in leading_notices(out)), out)  # head == base: nothing moved
                self.assertIn("continuation", self.seat(d, "astra"))
                cont = self.seat(d, "astra")["continuation"]
                reply_tag = sorted(d.glob("seat-astra-reply-*.json"), key=lambda p: p.stat().st_mtime)[-1].name[5:-5]
                self.assertEqual((cont["model"], cont["family"], cont["effort"], cont["reply_tag"]), (sol, "sol", "low", reply_tag))
                self.assertEqual(cont["thread_id"], "stub-thread-" + self.codex_records(env, "exec")[-1]["resume"])  # the thread it advanced
                self.assertIn("account", cont)
                self.assertIn("at", cont)

    def test_a_failed_reply_that_advanced_still_moves_the_continuation(self):
        # Astra C1-2: the continuation (model, family, effort, thread, account) is persisted after every reply that advanced,
        # success or failure; a reply that never advanced leaves it alone. An Astra reply that descends to Sol through the seat's
        # `astra|sol` list (docs/model-routing.md 5), works on the thread and then fails leaves the conversation on Sol: the next
        # reply runs Sol, at that reply's effort, on its thread
        astra, sol = head("astra"), head("sol")
        env, _ = self.isolated("failed-reply-advanced")
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "failed-reply-advanced", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "failed-reply-advanced"
        # never advanced: Astra and then Sol rejected at startup, before any thread event
        rc, out, err = self.run_engine("reply", "failed-reply-advanced", "--", f"[[STUB:badmodel={astra}+{sol}]] more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIsNone(self.seat(d, "astra").get("continuation"))
        # advanced, then failed: Astra rejected at startup, then Sol runs a command on the thread and its turn fails
        replies, n = set(d.glob("seat-astra-reply-*.json")), len(self.codex_records(env, "exec"))
        rc, out, err = self.run_engine("reply", "failed-reply-advanced", "--effort", "medium", "--",
                                       f"[[STUB:badmodel={astra},failafterwork]] more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        new = sorted(set(d.glob("seat-astra-reply-*.json")) - replies)
        self.assertEqual(len(new), 1, new)
        execs = self.codex_records(env, "exec")[n:]
        self.assertEqual([r["model"] for r in execs], [astra, sol])
        advanced = f"stub-thread-{execs[-1]['resume']}"  # the thread the Sol attempt continued (the stub's thread.started)
        parent = self.seat(d, "astra")
        self.assertEqual((parent["state"], self.meta(d)["state"]), ("done", "done"))  # the original seat's outcome stands
        rc, out, err = self.run_engine("reply", "failed-reply-advanced", "--", "again", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        last = self.codex_records(env, "exec")[-1]
        self.assertEqual((last["model"], last["effort"], last["resume"]), (sol, "medium", advanced))
        cont = parent.get("continuation") or {}
        self.assertEqual((cont.get("model"), cont.get("family"), cont.get("effort"), cont.get("reply_tag"), cont.get("thread_id")),
                         (sol, "sol", "medium", new[0].name[5:-5], advanced))
        self.assertIn("account", cont)

    def test_a_fallback_that_never_starts_a_thread_leaves_the_continuation_on_the_attempt_that_advanced(self):
        # Astra C1b-1: the continuation is the snapshot (model, family, effort, thread, account) of the LAST attempt that
        # advanced, not the reply's final seat. The Astra attempt starts its thread and is then rejected before any work; the
        # Sol target of the seat's `astra|sol` list fails before starting a thread (rejected at startup, or first stepped down an
        # effort through the reply's `ultra|max` effort list and then rejected; docs/model-routing.md 5). Only Astra advanced, so
        # the next reply runs Astra, at that attempt's effort, on the thread it started
        astra, sol = head("astra"), head("sol")
        cases = (("sol rejected at startup", "medium", "medium", f"badmodelafterthread={astra},badmodel={sol}", [(astra, "medium"), (sol, "medium")]),
                 ("sol stepped down an effort, then rejected", "ultra|max", "ultra", f"badmodelafterthread={astra},badeffort=ultra,badmodel={sol}",
                  [(astra, "ultra"), (sol, "ultra"), (sol, "max")]))
        for i, (case, effort_arg, effort, stubs, ran) in enumerate(cases):
            with self.subTest(case=case):
                job = f"reply-advanced-then-never-started-{i}"
                env, _ = self.isolated(job)
                rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", job, "--", "first", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.jobs / job
                replies, n = set(d.glob("seat-astra-reply-*.json")), len(self.codex_records(env, "exec"))
                rc, out, err = self.run_engine("reply", job, "--effort", effort_arg, "--", f"[[STUB:{stubs}]] more", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                new = sorted(set(d.glob("seat-astra-reply-*.json")) - replies)
                self.assertEqual(len(new), 1, new)
                execs = self.codex_records(env, "exec")[n:]
                self.assertEqual([(r["model"], r["effort"]) for r in execs], ran)
                started = f"stub-thread-{execs[0]['resume']}"  # the thread the Astra attempt started (the stub's thread.started)
                parent = self.seat(d, "astra")
                self.assertEqual((parent["state"], self.meta(d)["state"]), ("done", "done"))  # the original seat's outcome stands
                rc, out, err = self.run_engine("reply", job, "--", "again", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                last = self.codex_records(env, "exec")[-1]
                self.assertEqual((last["model"], last["effort"], last["resume"]), (astra, effort, started))
                cont = parent.get("continuation") or {}
                self.assertEqual((cont.get("model"), cont.get("family"), cont.get("effort"), cont.get("reply_tag"), cont.get("thread_id")),
                                 (astra, "astra", effort, new[0].name[5:-5], started))
                self.assertIn("account", cont)

    def test_reply_keeps_the_k1_seat_by_its_recorded_identity_only(self):
        k1, opus = TABLE["k1_seat"]["id"], head("opus")
        on = self.table_file("reply-k1-on", lambda t: t["k1_seat"].__setitem__("enabled", True))
        env, _ = self.isolated("reply-k1", table=on)
        rc, out, err = self.run_engine("ask", "--seats", "opus-5:high", "--job-dir", "reply-k1", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "reply-k1"
        rc, out, err = self.run_engine("reply", "reply-k1", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.claude_records(env)[-1]["model"], k1)  # its own recorded identity
        self.assertFalse(any("reply runs on" in l for l in leading_notices(out)), out)
        # the same ID recorded by a plain opus seat is never acquired by ID matching: it moves to the head
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", "reply-k1-by-id", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.rewrite_seat(self.jobs / "reply-k1-by-id", "opus", model=k1, seat_name="opus")
        rc, out, err = self.run_engine("reply", "reply-k1-by-id", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.claude_records(env)[-1]["model"], opus)
        self.assertIn(f"NOTICE: seat opus: reply runs on {opus} (recorded {k1} is superseded)", leading_notices(out))
        # with the seat switched off the K1 record's reply is refused, exit 1, nothing launched. Changed for ruling K1-ON
        # (review/ruling_k1_on.md): the shipped table switches the seat on, so the off state is a variant table
        off = self.table_file("reply-k1-off", lambda t: t["k1_seat"].__setitem__("enabled", False))
        launched = len(self.claude_records(env))
        rc, out, err = self.run_engine("reply", "reply-k1", "--", "again", env_extra=dict(env, BRIDGE_MODELS_FILE=str(off)))
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"the opus-5 seat is switched off (models.json k1_seat; Kevin K1). Use opus ({opus}).", out + err)
        self.assertEqual(len(self.claude_records(env)), launched)
        # "subject to markers": GroupE_MarkersAcrossPaths.test_reply_keeps_the_k1_seat_subject_to_markers

    def test_reply_without_a_replacement_is_refused(self):
        for old in (SPARK_ID, "gpt-9-nova"):
            with self.subTest(recorded=old):
                env, _ = self.isolated(f"reply-nofamily-{old}")
                job = f"reply-nofamily-{old}"
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", job, "--", "first", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.jobs / job
                self.as_old_record(d, "luna", old)
                rc, out, err = self.run_engine("reply", job, "--", "more", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(old, out + err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(len(self.codex_records(env, "exec")), 1)  # only the original ask ran
                self.assertEqual(self.meta(d)["state"], "done")

    def test_reply_effort_is_validated_against_the_table(self):
        luna = head("luna")
        env, _ = self.isolated("reply-effort")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "reply-effort", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        for effort, needle in (("ultra", f"{luna} supports {', '.join(efforts_of(luna))}; 'ultra' is not available (say luna:max)"),
                               ("loww", "'loww'")):
            with self.subTest(effort=effort):
                rc, out, err = self.run_engine("reply", "reply-effort", "--effort", effort, "--", "more", env_extra=env)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(needle, out + err)
                self.assertEqual(len(self.codex_records(env, "exec")), 1)
        rc, out, err = self.run_engine("reply", "reply-effort", "--effort", "high", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.codex_records(env, "exec")[-1]["effort"], "high")
        rc, out, err = self.run_engine("reply", "reply-effort", "--", "again", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.codex_records(env, "exec")[-1]["effort"], "high")  # the continuation's effort is the base now
        env, _ = self.isolated("reply-effort-claude")
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", "reply-effort-claude", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_engine("reply", "reply-effort-claude", "--effort", "ultra", "--", "more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("say max", out + err)
        self.assertEqual(len(self.claude_records(env)), 1)

    def test_a_recorded_effort_the_head_no_longer_supports_is_refused_not_replaced(self):
        # Astra C1-3: a reply keeps its base effort unless --effort is given. When the current table's head no longer supports
        # the recorded effort, the reply is refused (exit 1, nothing launched) with one line naming the allowed efforts and
        # --effort; it never runs at a substituted effort
        sol = head("sol")
        env, _ = self.isolated("reply-effort-narrowed")
        rc, out, err = self.run_engine("ask", "--seats", "sol:high", "--job-dir", "reply-effort-narrowed", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        narrowed = self.table_file("reply-effort-narrowed", lambda t: t["models"][sol].__setitem__("efforts", ["low", "medium"]))
        env = dict(env, BRIDGE_MODELS_FILE=str(narrowed))
        rc, out, err = self.run_engine("reply", "reply-effort-narrowed", "--", "more", env_extra=env)
        self.assertEqual([r["effort"] for r in self.codex_records(env, "exec")], ["high"], out + err)  # only the ask ran
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertTrue([l for l in (out + err).splitlines() if "--effort" in l and re.search(r"\blow\b", l) and re.search(r"\bmedium\b", l)],
                        f"no line names the allowed efforts (low, medium) and --effort:\n{out}{err}")
        self.assertEqual(self.meta(self.jobs / "reply-effort-narrowed")["state"], "done")
        rc, out, err = self.run_engine("reply", "reply-effort-narrowed", "--effort", "medium", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)  # the override the refusal names runs
        self.assertEqual(self.codex_records(env, "exec")[-1]["effort"], "medium")

    def test_a_recorded_model_in_another_accepted_spelling_continues_on_its_head(self):
        # Astra C1b-n1: reply resolution maps the recorded model through Table.canonical(), as launch and the check map every
        # spelling the table accepts (case, a trailing [1m], a listed spelling). A bridge before this change lowercased a
        # claude-* name, or a name forced with claude/, and recorded it with its [1m] (the Sonnet head's id with `[1m]`, `opus[1m]`); the
        # upper-case and listed-spelling records are constructed (no bridge writes them). Each names a head or its family, so
        # the reply runs on that head: no refusal, and no "superseded" notice for a spelling of the head itself
        sonnet, opus, haiku = head("sonnet"), head("opus"), head("haiku")
        cases = (("sonnet:low", "sonnet", f"{sonnet}[1m]", sonnet, "low", None),
                 ("opus:low", "opus", "opus[1m]", opus, "low", f"NOTICE: seat opus: reply runs on {opus} (recorded as the alias '"),
                 ("sonnet:low", "sonnet", sonnet.upper(), sonnet, "low", None),
                 ("haiku", "haiku", HAIKU_ALIAS, haiku, "max", None))
        for i, (spec, tag, recorded, runs, effort, notice) in enumerate(cases):
            with self.subTest(recorded=recorded):
                job = f"reply-spelling-{i}"
                env, _ = self.isolated(job, table=self.table_file(job, with_haiku_alias))
                rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", "first", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.as_old_record(self.jobs / job, tag, recorded)
                rc, out, err = self.run_engine("reply", job, "--", "more", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                last = self.claude_records(env)[-1]
                self.assertEqual((last["model"], last["effort"], last["resume"]), (runs, effort, "stub-session-aaaa"))
                moved = [l for l in leading_notices(out) if "reply runs on" in l]
                if notice:
                    self.assertTrue([l for l in moved if l.startswith(notice)], out)
                else:
                    self.assertEqual(moved, [], out)  # the recorded spelling is the head: nothing moved, nothing superseded

    def test_a_reply_refused_before_launch_leaves_retrieval_on_the_latest_answer(self):
        # Astra C2b-1: a pinned Codex reply refused before anything launched (relay unavailable, the account's sign-in changed)
        # still ends through _finish_seat(), which composes last-<reply tag>.md without an answer: empty, or only the notices the
        # reply carries. Plain `result` ignores a candidate whose body is empty once the bridge's notices are removed and prints
        # the latest actual answer: the parent's, or a later reply's. The retrieval side of EngineTestCase's pre-launch refusals
        # (test_a_pinned_codex_reply_refuses_to_run_when_the_router_is_gone, test_a_pinned_codex_reply_refuses_when_the_label_changed_hands)
        sol = head("sol")
        job = "reply-refused-retrieval"
        env, _ = self.isolated(job)
        renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", job, "--", "[[STUB:text=PARENT-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / job
        refusals = (("relay unavailable", dict(env, BRIDGE_ROUTER="off"), None, "this seat ran on relay account a, but relay is not available now"),
                    ("sign-in changed hands", env, {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "verify": {"a": {"email": "someone-else@example.test"}}},
                     "account a is now signed in as someone-else@example.test, not a@example.test whose thread this is; refusing to run"))
        for case, reply_env, script, line in refusals:
            with self.subTest(refusal=case):
                if script:
                    (rd / "script.json").write_text(json.dumps(script))
                rc, out, err = self.run_engine("reply", job, "--", "more", env_extra=reply_env)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(line, out)
                rc, out, err = self.run_engine("result", job, env_extra=env)
                self.assertEqual(rc, 0, out + err)  # the parent's outcome stands
                self.assertIn("PARENT-ANSWER", out, f"`result` printed the refused reply's empty artifact instead of the parent's answer:\n{out}")
        # a reply answers; then an older record makes the next reply move to the head, with a notice recorded for its own tag, and
        # that reply is refused before launch: its artifact holds the notice and nothing else
        (rd / "script.json").write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}}))
        rc, out, err = self.run_engine("reply", job, "--", "[[STUB:text=REPLY-ANSWER]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.as_old_record(d, "sol", RETIRED_SOL)
        rc, out, err = self.run_engine("reply", job, "--", "again", env_extra=dict(env, BRIDGE_ROUTER="off"))
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"NOTICE: seat sol: reply runs on {sol} (recorded {RETIRED_SOL} is superseded)", leading_notices(out))
        self.assertIn("this seat ran on relay account a, but relay is not available now", out)
        rc, out, err = self.run_engine("result", job, env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("REPLY-ANSWER", out, f"`result` printed a notice-only artifact instead of the latest answer:\n{out}")
        self.assertNotIn("PARENT-ANSWER", out)


# ==================================================================================================================
# DESIGN §8 group D: floors and discovery (bootstrap and package scenarios: claude-codex-bridge/tests/test_bootstrap.sh)
# ==================================================================================================================
# The ChatGPT app of a discovery case, as paths in the case directory, in its two bundle layouts: the legacy binary, which is the
# ONE path BRIDGE_APP_LAUNCHER names, and the launcher the app moved its CLI to in bundle 26.928, derived from the legacy path's
# folder as the engine derives it (bridge.py APP_LAUNCHERS). A case's override therefore keeps both layouts inside the case.
APP_LEGACY = "Applications/ChatGPT.app/Contents/Resources/codex"
APP_NEW = os.path.dirname(APP_LEGACY) + "/codex-cli/bin/codex"


class GroupD_FloorsAndDiscovery(EngineBase):
    """DESIGN §3: versions parsed from `--version` (a prerelease sorts below its release); a CLI below a model's floor
    refuses before relay or any launch; unparseable output fails closed for floored models; resolve_codex() scans all of
    PATH and prefers the first candidate meeting the highest Codex floor; the app launcher is considered in both of the
    app's bundle layouts, the newest first."""

    def attempt(self, name, spec, **extra):
        env, _ = self.isolated(name, **extra)
        renv, rd = self.relay(name, {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
        return rc, out, err, env, rd

    def test_versions_parse_with_prerelease_order(self):
        sol, opus = head("sol"), head("opus")
        self.assertEqual((TABLE["models"][sol]["min_cli"], TABLE["models"][opus]["min_cli"]), ("0.153.0", "2.1.280"))
        codex = (("codex-cli 0.153.0", True), ("codex-cli 0.153.0-alpha.3", False), ("codex-cli 0.152.0", False),
                 ("codex-cli 0.152.9", False), ("codex-cli 0.154.0-alpha", True), ("codex 0.153.0", True), ("codex-cli v0.153.1", True),
                 ("codex-cli 1.0.0", True))
        claude = (("2.1.280 (Claude Code)", True), ("2.1.279 (Claude Code)", False), ("2.1.280-beta.1 (Claude Code)", False),
                  ("v2.1.281 (Claude Code)", True), ("2.2.0 (Claude Code)", True))
        cases = [("STUB_CODEX_VERSION", v, "sol:low", sol, ok) for v, ok in codex] + [("STUB_CLAUDE_VERSION", v, "opus:low", opus, ok) for v, ok in claude]
        for var, version, spec, model, runs in cases:
            with self.subTest(version=version):
                rc, out, err, env, rd = self.attempt("version-" + re.sub(r"[^a-z0-9.]+", "-", version.lower()), spec, **{var: version})
                if runs:
                    self.assertEqual(rc, 0, out + err)
                    self.assertEqual(self.launched_models(env), [model])
                else:
                    self.assert_refused(rc, out, err)
                    self.assertEqual(self.launched_models(env), [])
                    self.assertEqual(self.records(rd, "calls.jsonl"), [])

    def test_an_unparseable_version_fails_closed_for_floored_models_and_names_the_fix(self):
        banner = "Codex (build 7f3a9c2e) nightly - a banner that runs well past sixty characters of text"
        for spec in ("sol:low", "astra:low"):
            with self.subTest(spec=spec):
                rc, out, err, env, rd = self.attempt("unparseable-" + spec.split(":")[0], spec, STUB_CODEX_VERSION=banner)
                line = self.assert_refused(rc, out, err)
                self.assertIn(f"cannot read the Codex CLI version from '{banner[:60]}'; set CODEX_BIN to a codex whose --version prints codex-cli X.Y.Z", line)
                self.assertEqual(self.launched_models(env), [])
        rc, out, err, env, rd = self.attempt("unparseable-claude-opus", "opus:low", STUB_CLAUDE_VERSION="claude-stub 0.0.1")
        line = self.assert_refused(rc, out, err)
        self.assertIn("cannot read", line)
        self.assertIn("'claude-stub 0.0.1'", line)
        rc, out, err, env, rd = self.attempt("unparseable-claude-haiku", "haiku", STUB_CLAUDE_VERSION="claude-stub 0.0.1")
        self.assertIn("cannot read", self.assert_refused(rc, out, err))
        self.assertEqual(self.launched_models(env), [])

    def test_a_codex_cli_below_a_floor_is_refused_before_relay(self):
        # changed for L4 (review/rulings_final_round.md; the result is unchanged): the upgrade hint follows the executable's real
        # path, so the codex here is the stub installed where npm installs @openai/codex, and the line keeps its npm hint. The
        # other installers: GroupD_UpgradeHints
        sol = head("sol")
        floor = TABLE["models"][sol]["min_cli"]
        npm_codex = Path(os.path.realpath(str(self.case_dir("below-floor")))) / "npm-global/lib/node_modules/@openai/codex/bin/codex"
        npm_codex.parent.mkdir(parents=True, exist_ok=True)
        npm_codex.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{STUB_CODEX}' \"$@\"\n")
        npm_codex.chmod(0o755)
        before = self.job_names()
        rc, out, err, env, rd = self.attempt("below-floor", "sol:low", STUB_CODEX_VERSION="codex-cli 0.152.0-alpha", CODEX_BIN=str(npm_codex))
        self.assertEqual(self.assert_refused(rc, out, err),
                         f"bridge: seat 'sol' ({sol}) needs Codex CLI >= {floor}; {npm_codex} is 0.152.0-alpha. "
                         f"Upgrade: npm install -g @openai/codex@latest, or point CODEX_BIN at a newer codex.")
        self.assertEqual(self.records(rd, "calls.jsonl"), [])
        self.assertEqual(self.launched_models(env), [])
        self.assertEqual(self.new_jobs(before), [])
        rc, out, err = self.run_engine("review", env_extra=env)  # the default review seats include sol
        self.assertIn(f"needs Codex CLI >= {floor}", self.assert_refused(rc, out, err))
        self.assertEqual(self.records(rd, "calls.jsonl"), [])

    def test_claude_code_below_the_floor_is_refused_per_family(self):
        # P-16: the conservative Haiku floor is 2.1.295; older versions refuse it alongside any other unmet family floors.
        floors = {f: TABLE["models"][head(f)]["min_cli"] for f in ("fable", "opus", "sonnet", "haiku")}
        # Each refusal names the specific model floor; Haiku is verified compatible at 2.1.295.
        for version, refused in (("2.1.294 (Claude Code)", {"haiku"}), ("2.1.283 (Claude Code)", {"sonnet", "haiku"}),
                                 ("2.1.279 (Claude Code)", {"opus", "sonnet", "haiku"}),
                                 ("2.1.256 (Claude Code)", {"opus", "fable", "sonnet", "haiku"}),
                                 ("2.1.196 (Claude Code)", {"opus", "fable", "sonnet", "haiku"})):
            for fam in ("fable", "opus", "sonnet", "haiku"):
                with self.subTest(version=version, family=fam):
                    spec = f"{fam}:low" if fam != "haiku" else fam
                    rc, out, err, env, rd = self.attempt(f"claude-floor-{fam}-{version.split()[0]}", spec, STUB_CLAUDE_VERSION=version)
                    if fam in refused:
                        line = self.assert_refused(rc, out, err)
                        self.assertIn(f"needs Claude Code >= {floors[fam]}", line)
                        self.assertIn("claude update", line)
                        if fam == "opus":  # changed for C-opus-7: no Opus version after the floor (GroupD_FinalReviewFloor)
                            self.assertIn(f"({head('opus')}) needs Claude Code >= {floors['opus']}", line)
                        self.assertEqual(self.records(rd, "calls.jsonl"), [])
                        self.assertEqual(self.launched_models(env), [])
                    else:
                        self.assertEqual(rc, 0, out + err)
                        self.assertEqual(self.launched_models(env), [head(fam)])

    def test_resolve_codex_scans_all_of_path_and_prefers_a_candidate_meeting_the_floor(self):
        root = self.case_dir("path-scan")

        def wrapper(sub, version, name):
            (root / sub).mkdir(exist_ok=True)
            p = root / sub / "codex"
            p.write_text(f"#!/bin/sh\nSTUB_CODEX_VERSION='{version}' STUB_CODEX_NAME='{name}' exec '{sys.executable}' '{STUB_CODEX}' \"$@\"\n")
            p.chmod(0o755)
            return p

        wrapper("old", "codex-cli 0.155.0-alpha.16.3", "old")               # first on PATH, capable, below the floor
        target = wrapper("shim-target", "codex-cli 0.156.1", "shim")        # meets the floor, but only reachable through our shim
        (root / "shim").mkdir(exist_ok=True)
        (root / "shim" / "codex").write_text(f"#!/bin/sh\n# codex shim (claude-codex-bridge bootstrap): runs the bundled Codex CLI\nexec '{target}' \"$@\"\n")
        (root / "shim" / "codex").chmod(0o755)
        app = wrapper("app", "codex-cli 0.156.1", "app")                    # the app launcher, also linked from PATH
        (root / "applink").mkdir(exist_ok=True)
        if not (root / "applink" / "codex").exists():
            (root / "applink" / "codex").symlink_to(app)
        wrapper("new", "codex-cli 0.156.1", "new")                          # the first eligible candidate
        (root / "py").mkdir(exist_ok=True)
        if not (root / "py" / "python3").exists():
            (root / "py" / "python3").symlink_to(sys.executable)
        path = os.pathsep.join(str(root / s) for s in ("old", "shim", "applink", "new", "py")) + os.pathsep + "/usr/bin:/bin"
        env, _ = self.isolated("path-scan", PATH=path, CODEX_BIN=None, BRIDGE_APP_LAUNCHER=str(app))
        for spec in ("sol:low", "astra:low"):  # astra's own floor is lower, but the highest Codex floor decides
            with self.subTest(spec=spec):
                rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.codex_records(env, "exec")[-1]["name"], "new")
        rc, out, err = self.run_engine("doctor", env_extra=env)
        chosen = root / "new" / "codex"
        self.assertTrue(str(chosen) in out or os.path.realpath(str(chosen)) in out, out)

    def test_each_default_fixed_directory_is_considered_beneath_the_fixed_root(self):
        # TEST_RULINGS F-E6 (revised): the engine applies BRIDGE_CODEX_FIXED_ROOT as a path prefix to DESIGN §3's two
        # default fixed directories, /opt/homebrew/bin then /usr/local/bin, which follow PATH, the app launcher and
        # ~/.local/bin; every harness points it at an empty directory. Each case here has a root of its own with stubs
        # beneath it, and the CLI shows which stub ran the seat while the stubs' --version records show the order in which
        # the candidates were considered. The host guard (fixtures/host_guard) refuses to start any codex outside the case,
        # so an engine that ignored the root never runs the host's codex; the refused attempt fails the case.
        floor = max((m["min_cli"] for m in TABLE["models"].values() if m["min_cli"] and vendor_of(m["family"]) == "codex"),
                    key=lambda v: tuple(int(p) for p in v.split(".")))       # the highest Codex floor decides
        meets, below = f"codex-cli {floor}", f"codex-cli {floor}-alpha.1"    # a prerelease sorts below its release
        app, local = "app/codex", "home/.local/bin/codex"
        homebrew, usr_local = "fixed-root/opt/homebrew/bin/codex", "fixed-root/usr/local/bin/codex"

        def ask(name, stubs):
            """`ask` on a Sol seat with no CODEX_BIN and no codex on PATH; `stubs` maps a path in the case (app/codex is the
            app launcher, home/ is HOME, fixed-root/ is the fixed root) to the version its stub prints. Returns the exit
            code, the output, the stubs in the order of their first --version probe, and the stubs that ran the seat."""
            env, d = self.isolated(f"fixed-root-{name}")
            for sub in ("fixed-root", "home", "py"):
                (d / sub).mkdir(exist_ok=True)
            if not (d / "py" / "python3").exists():
                (d / "py" / "python3").symlink_to(sys.executable)
            for rel, version in stubs.items():
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                (d / rel).write_text(f"#!/bin/sh\nSTUB_CODEX_VERSION='{version}' STUB_CODEX_NAME='{rel}' exec '{sys.executable}' '{STUB_CODEX}' \"$@\"\n")
                (d / rel).chmod(0o755)
            guard_log = d / "host-guard.jsonl"
            env.update(PATH=str(d / "py") + os.pathsep + "/usr/bin:/bin", CODEX_BIN=None, HOME=str(d / "home"), BRIDGE_APP_LAUNCHER=str(d / app),
                       BRIDGE_CODEX_FIXED_ROOT=str(d / "fixed-root"), PYTHONPATH=str(HOST_GUARD), PYTHONDONTWRITEBYTECODE="1",
                       HOST_GUARD_ROOTS=str(d), HOST_GUARD_LOG=str(guard_log))
            rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
            refused = self.read_records(guard_log) if guard_log.exists() else []
            self.assertEqual(refused, [], "a codex outside the test's directory was about to start (BRIDGE_CODEX_FIXED_ROOT not applied)")
            probes = list(dict.fromkeys(r["name"] for r in self.codex_records(env, "version")))
            return rc, out + err, probes, [r["name"] for r in self.codex_records(env, "exec")]

        with self.subTest(case="in order: app launcher, ~/.local/bin, then beneath the root /opt/homebrew/bin, /usr/local/bin"):
            rc, out, probes, ran = ask("order", {app: below, local: below, homebrew: below, usr_local: meets})
            self.assertEqual(rc, 0, out)
            self.assertEqual(probes, [app, local, homebrew, usr_local])
            self.assertEqual(ran, [usr_local])                              # the first candidate meeting the floor
        with self.subTest(case="both default directories meet the floor: /opt/homebrew/bin wins"):
            rc, out, probes, ran = ask("first", {homebrew: meets, usr_local: meets})
            self.assertEqual(rc, 0, out)
            self.assertEqual(probes[:1], [homebrew])
            self.assertEqual(ran, [homebrew])
        with self.subTest(case="/usr/local/bin alone"):
            rc, out, probes, ran = ask("second", {usr_local: meets})
            self.assertEqual(rc, 0, out)
            self.assertEqual((probes, ran), ([usr_local], [usr_local]))
        with self.subTest(case="the root prefixes the two default directories and is not searched itself"):
            decoys = ("fixed-root/codex", "fixed-root/bin/codex", "fixed-root/opt/homebrew/codex", "fixed-root/usr/local/codex")
            rc, out, probes, ran = ask("prefix", {rel: meets for rel in decoys})
            self.assertNotEqual(rc, 0, out)                                 # no codex resolves
            self.assertEqual((probes, ran), ([], []))

    # ---------------------------------------------------------------- the app's two bundle layouts (the 2026-10-02 incident)
    LOCAL = "home/.local/bin/codex"

    @staticmethod
    def floor_versions():
        """What `--version` prints for a codex that meets the highest Codex floor of the table (the one discovery compares
        with), and for one just below it (a prerelease sorts below its release)."""
        floor = max((m["min_cli"] for m in TABLE["models"].values() if m["min_cli"] and vendor_of(m["family"]) == "codex"),
                    key=lambda v: tuple(int(p) for p in v.split(".")))
        return f"codex-cli {floor}", f"codex-cli {floor}-alpha.1"

    def bundle_case(self, name, stubs, scripts=None, links=None, path=()):
        """A discovery case of its own: no CODEX_BIN; HOME (home/), the fixed root (fixed-root/) and the app, whose legacy
        binary BRIDGE_APP_LAUNCHER names (APP_LEGACY), all inside the case; the host guard on. `stubs` maps a path in the case
        to the version its stub prints, `scripts` a path to the text of a shell script, `links` a path to the path in the case
        it is a symlink to; `path` lists the directories of the case that lead PATH. Returns (environment, case directory)."""
        env, d = self.isolated(name)
        for sub in ("fixed-root", "home", "py"):
            (d / sub).mkdir(exist_ok=True)
        if not (d / "py" / "python3").exists():
            (d / "py" / "python3").symlink_to(sys.executable)
        texts = {rel: f"#!/bin/sh\nSTUB_CODEX_VERSION='{version}' STUB_CODEX_NAME='{rel}' exec '{sys.executable}' '{STUB_CODEX}' \"$@\"\n"
                 for rel, version in stubs.items()}
        texts.update(scripts or {})
        for rel, text in texts.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
            (d / rel).chmod(0o755)
        for rel, target in (links or {}).items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            if not (d / rel).is_symlink():
                (d / rel).symlink_to(d / target)
        env.update(PATH=os.pathsep.join([str(d / p) for p in path] + [str(d / "py"), "/usr/bin", "/bin"]), CODEX_BIN=None,
                   HOME=str(d / "home"), BRIDGE_APP_LAUNCHER=str(d / APP_LEGACY), BRIDGE_CODEX_FIXED_ROOT=str(d / "fixed-root"),
                   PYTHONPATH=str(HOST_GUARD), PYTHONDONTWRITEBYTECODE="1", HOST_GUARD_ROOTS=str(d), HOST_GUARD_LOG=str(d / "host-guard.jsonl"))
        return env, d

    def bundle_ask(self, env, d):
        """`ask` on a Sol seat in a bundle_case: the exit code, the output, the stubs in the order of their first --version
        probe, and the stubs that ran the seat. No codex outside the case may have been about to start."""
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        guard = d / "host-guard.jsonl"
        self.assertEqual(self.read_records(guard) if guard.exists() else [], [], "a codex outside the case was about to start")
        probes = list(dict.fromkeys(r["name"] for r in self.codex_records(env, "version")))
        return rc, out + err, probes, [r["name"] for r in self.codex_records(env, "exec")]

    def test_a_wrapper_whose_target_vanished_does_not_hide_the_apps_bundled_codex(self):
        # The incident: the ChatGPT app's update (bundle 26.928, installed when the app quit on 2026-10-02) moved its bundled
        # CLI from Contents/Resources/codex to a launcher at Contents/Resources/codex-cli/bin/codex, and the wrapper the
        # bootstrap had written at ~/.local/bin/codex in September kept exec'ing the binary that was gone. With no other codex
        # on the Mac, an engine that knew only the legacy path resolved nothing ("codex binary not found or too old") while a
        # working CLI sat in the app. Here: that wrapper, marker and all, on PATH and at HOME/.local/bin, its target the case's
        # own legacy path (which does not exist); the case's app ships only the new layout; no other codex; no CODEX_BIN.
        meets, _ = self.floor_versions()
        d = self.case_dir("bundle-dead-wrapper")
        wrapper = f"#!/bin/sh\n# codex shim (claude-codex-bridge bootstrap): runs the bundled Codex CLI\nexec '{d / APP_LEGACY}' \"$@\"\n"
        env, d = self.bundle_case("bundle-dead-wrapper", {APP_NEW: meets}, scripts={self.LOCAL: wrapper}, path=("home/.local/bin",))
        self.assertFalse((d / APP_LEGACY).exists())
        dead = subprocess.run([str(d / self.LOCAL), "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertNotEqual(dead.returncode, 0, "the case's wrapper is not dead")  # 126 under this /bin/sh, 127 under others
        rc, out, probes, ran = self.bundle_ask(env, d)
        self.assertEqual(rc, 0, out)
        self.assertEqual((probes, ran), ([APP_NEW], [APP_NEW]))             # the app's new-layout launcher ran the seat
        rc, out, err = self.doctor(env)
        new = str(d / APP_NEW)
        line = re.search(r"(?m)^codex: (.+) \(([^()]*)\) \[([^\]]+)\]$", out)
        self.assertIsNotNone(line, out + err)
        self.assertIn(line.group(1), (new, os.path.realpath(new)), out + err)
        self.assertEqual((line.group(2), line.group(3)), (meets, "app bundle"), out + err)

    def test_the_app_launcher_is_considered_in_both_bundle_layouts_newest_first(self):
        # Both layouts are ordinary candidates at the app launcher's position (after PATH, before ~/.local/bin): the new one
        # first (the app's own manifest names codex-cli/bin/codex as its entry point; an app that had both would have the
        # legacy binary as the leftover), each passing the same probes and floor as any candidate, so a new launcher that is
        # there but broken falls through to the legacy one. A PATH entry that resolves to either is left to that position.
        meets, below = self.floor_versions()
        with self.subTest(case="both layouts meet the floor: the new one runs"):
            env, d = self.bundle_case("bundle-both", {APP_NEW: meets, APP_LEGACY: meets})
            rc, out, probes, ran = self.bundle_ask(env, d)
            self.assertEqual(rc, 0, out)
            self.assertEqual((probes[:1], ran), ([APP_NEW], [APP_NEW]))
        with self.subTest(case="the new one is there but fails its --version probe: the legacy one runs"):
            d = self.case_dir("bundle-broken")
            broken = f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{d / 'broken-new.log'}'\nexit 3\n"
            env, d = self.bundle_case("bundle-broken", {APP_LEGACY: meets}, scripts={APP_NEW: broken})
            rc, out, probes, ran = self.bundle_ask(env, d)
            self.assertEqual(rc, 0, out)
            self.assertEqual((probes, ran), ([APP_LEGACY], [APP_LEGACY]))
            asked = (d / "broken-new.log").read_text().splitlines() if (d / "broken-new.log").exists() else []
            self.assertEqual(sorted(set(asked)), ["--version"], "the new launcher was not considered, or was asked for more than its version")
        with self.subTest(case="the new one is below the floor and the legacy one meets it: the legacy one runs"):
            # a codex at ~/.local/bin meets the floor too and is never probed: both layouts come before it, the new one first
            env, d = self.bundle_case("bundle-floor", {APP_NEW: below, APP_LEGACY: meets, self.LOCAL: meets})
            rc, out, probes, ran = self.bundle_ask(env, d)
            self.assertEqual(rc, 0, out)
            self.assertEqual((probes, ran), ([APP_NEW, APP_LEGACY], [APP_LEGACY]))
        for layout, rel in (("new", APP_NEW), ("legacy", APP_LEGACY)):
            with self.subTest(case="a PATH symlink to a launcher is not taken in the PATH phase", layout=layout):
                # the link leads PATH and its launcher meets the floor: taken there, it would run the seat ahead of the standalone
                env, d = self.bundle_case(f"bundle-link-{layout}", {APP_NEW: meets, APP_LEGACY: meets, "standalone/codex": meets},
                                          links={"applink/codex": rel}, path=("applink", "standalone"))
                rc, out, probes, ran = self.bundle_ask(env, d)
                self.assertEqual(rc, 0, out)
                self.assertEqual((probes[:1], ran), (["standalone/codex"], ["standalone/codex"]))

    def test_the_app_launchers_are_the_new_layout_derived_from_the_legacy_path_then_the_legacy_path(self):
        # In-process, the constant only (nothing here looks at or runs the paths it names). Shipped: the real app's two layouts,
        # newest first. BRIDGE_APP_LAUNCHER keeps its grammar, ONE path, where the app's legacy binary would be, and the new
        # layout is derived from that path's folder, so an override (every test's) moves both; empty: no app launcher at all.
        def launchers(**override):
            env = {k: v for k, v in self.env.items() if k != "BRIDGE_APP_LAUNCHER"}
            saved = os.environ.pop("BRIDGE_APP_LAUNCHER", None)  # a developer's own: load_engine_module does not clear it
            try:
                return load_engine_module(dict(env, **override)).APP_LAUNCHERS
            finally:
                if saved is not None:
                    os.environ["BRIDGE_APP_LAUNCHER"] = saved

        self.assertEqual(launchers(), ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                                       "/Applications/ChatGPT.app/Contents/Resources/codex"))
        given = str(self.case_dir("bundle-override") / "Elsewhere.app" / "Contents" / "Resources" / "codex")
        self.assertEqual(launchers(BRIDGE_APP_LAUNCHER=given), (os.path.dirname(given) + "/codex-cli/bin/codex", given))
        self.assertEqual(launchers(BRIDGE_APP_LAUNCHER=""), ())


class GroupD_HostGuard(unittest.TestCase):
    """The host guard the default-directory tests load (fixtures/host_guard; Astra B3 P2 on 1d6d30d6). A codex outside the
    allowed root is refused, and logged as a direct launch is, wherever a launch puts it at an executable position:
    directly, through env after its options and assignments, and through a shell's -c string (combined flags, the command
    word of each simple command, a forwarded positional parameter). A path that is only an argument, and a codex inside
    the root reached through env, still run. The "host" codex is a stub outside the root that records every run; no real
    CLI is started."""

    # the guarded process: starts one command (subprocess, shell=True, os.system or os.posix_spawn) and prints the outcome
    CHILD = ("import json, os, subprocess, sys\n"
             "how, cmd = json.loads(sys.argv[1])\n"
             "try:\n"
             "    if how == 'system':\n"
             "        rc = os.system(cmd)\n"
             "    elif how == 'spawn':\n"
             "        rc = os.waitpid(os.posix_spawn(cmd[0], cmd, dict(os.environ)), 0)[1]\n"
             "    else:\n"
             "        rc = subprocess.run(cmd, shell=(how == 'shell'), timeout=30).returncode\n"
             "    print(json.dumps({'rc': rc}))\n"
             "except PermissionError as e:\n"
             "    print(json.dumps({'refused': str(e)}))\n")

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="host-guard-")))
        cls.root, cls.runs = cls.tmp / "root", cls.tmp / "runs.log"
        cls.host = cls.tmp / "host" / "usr" / "local" / "bin" / "codex"   # outside the root: must never run
        cls.stub = cls.root / "bin" / "codex"                              # inside the root: may run
        cls.helper = cls.root / "bin" / "helper"                           # records the arguments it is given
        for path, label in ((cls.host, "host"), (cls.stub, "root"), (cls.helper, "helper")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"#!/bin/sh\necho \"{label} $*\" >> '{cls.runs}'\n")
            path.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def launch(self, how, cmd, path="/usr/bin:/bin"):
        """Start `cmd` from a Python process that loads the guard; returns its outcome, the refusals it logged and the runs
        the stubs recorded."""
        log = self.tmp / "guard.jsonl"
        for p in (log, self.runs):
            if p.exists():
                p.unlink()
        env = dict(os.environ, PATH=path, PYTHONPATH=str(HOST_GUARD), PYTHONDONTWRITEBYTECODE="1",
                   HOST_GUARD_ROOTS=str(self.root), HOST_GUARD_LOG=str(log))
        r = subprocess.run([sys.executable, "-c", self.CHILD, json.dumps([how, cmd])], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        records = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        runs = self.runs.read_text().splitlines() if self.runs.exists() else []
        return json.loads(r.stdout.splitlines()[-1]), records, runs

    def test_a_codex_outside_the_root_is_refused_at_every_executable_position(self):
        h = str(self.host)
        cases = {
            "direct (the reference)": ("popen", [h, "--version"]),
            "env": ("popen", ["/usr/bin/env", h, "--version"]),                        # Astra's first bypass
            "bash -lc": ("popen", ["/bin/bash", "-lc", f"{h} --version"]),             # Astra's second bypass
            "env options, --, assignments": ("popen", ["/usr/bin/env", "-i", "-u", "HOME", "--", "LANG=C", h, "--version"]),
            "env -S": ("popen", ["/usr/bin/env", "-S", f"LANG=C {h} --version"]),
            "env, then sh -ec": ("popen", ["/usr/bin/env", "FOO=1", "/bin/sh", "-ec", f"exec {h} --version"]),
            "a later simple command": ("popen", ["/bin/sh", "-c", f"cd / && FOO=1 '{h}' --version 2>&1 | cat"]),
            "after a newline": ("popen", ["/bin/sh", "-c", f"true\n{h} --version"]),
            "bash -euo pipefail -c": ("popen", ["/bin/bash", "-euo", "pipefail", "-c", f"if true; then {h} --version; fi"]),
            "command substitution": ("popen", ["/bin/sh", "-c", f"v=$({h} --version)"]),
            "forwarded $0": ("popen", ["/bin/sh", "-c", 'exec "$0" "$@"', h, "--version"]),
            "forwarded $@": ("popen", ["/bin/bash", "-c", '"$@"', "bash", h, "--version"]),
            "shell=True": ("shell", f"{h} --version"),
            "os.system": ("system", f"FOO=1 {h} --version"),
            "os.posix_spawn of env": ("spawn", ["/usr/bin/env", h, "--version"]),
        }
        direct = None
        for name, (how, cmd) in cases.items():
            with self.subTest(launch=name):
                outcome, records, runs = self.launch(how, cmd)
                self.assertIn("refused", outcome, f"the guard let {cmd!r} start")
                self.assertEqual(runs, [], "the host codex ran")
                self.assertEqual([r["refused"] for r in records], [[h]], "the refusal was not logged as a direct launch's is")
                direct = direct or records[0]
                self.assertEqual(sorted(records[0]), sorted(direct))
        with self.subTest(launch="a bare name found on PATH, through env"):
            outcome, records, runs = self.launch("popen", ["/usr/bin/env", "codex", "--version"], path=f"{self.host.parent}:/usr/bin:/bin")
            self.assertIn("refused", outcome)
            self.assertEqual((runs, [r["refused"] for r in records]), ([], [["codex"]]))

    def test_an_argument_and_a_codex_inside_the_root_still_run(self):
        h, stub, helper = str(self.host), str(self.stub), str(self.helper)
        cases = {
            "a codex inside the root, through env": (["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", stub, "--version"], ["root --version"]),
            "a codex inside the root, through env and sh -c": (["/usr/bin/env", "LANG=C", "/bin/sh", "-c", f"{stub} --version"],
                                                               ["root --version"]),
            "the host path as a helper's argument": ([helper, h], [f"helper {h}"]),
            "... through env": (["/usr/bin/env", helper, h], [f"helper {h}"]),
            "... through sh -c": (["/bin/sh", "-c", f'{helper} "$1"', "sh", h], [f"helper {h}"]),
            "... after a script operand": (["/bin/sh", helper, "-c", h], [f"helper -c {h}"]),
        }
        for name, (cmd, expected) in cases.items():
            with self.subTest(launch=name):
                outcome, records, runs = self.launch("popen", cmd)
                self.assertEqual((outcome, records, runs), ({"rc": 0}, [], expected))


# ==================================================================================================================
# DESIGN §8 group E: the check, through `bridge.py doctor` and model-starting invocations (pure evaluate(): test_bridge_models.py)
# ==================================================================================================================
class CheckBase(EngineBase):
    """Helpers of the check's tests (DESIGN §5; the S-C3 single-writer ruling in review/astra_C3_findings.md). Holds no tests."""

    STALE_NEWER_SOL = f"stale: newer model may exist: {NEWER_SOL} is listed but not in the model table"

    def slow_table(self, name, updated=None, **policy):
        """The table under test with policy changes (a long fetch timeout for held writers, a short refresh window); a new
        `updated` date changes the table's bytes (its digest) and nothing else."""
        def mutate(t):
            t["policy"].update(policy)
            if updated:
                t["updated"] = updated
        return self.table_file(name, mutate)

    def hold_all(self, prefix):
        for key in ("npm", "deprecations", "overview"):
            self.pages.hold(f"/{prefix}/{key}")

    def wait_page_call(self, prefix, tries=300):
        for _ in range(tries):
            if self.page_calls(prefix):
                return
            time.sleep(0.1)
        self.fail(f"no fetch reached /{prefix}/")

    def marker_ids(self, env):
        return {m["id"] for m in markers_in(self.evidence(env))}

    def assert_app_server_argv_clean(self, env):
        """DESIGN §5: the app-server always loads user configuration and rejects the seats' --ignore-user-config (the stub
        refuses it as the real CLI does): the check never passes it."""
        apps = self.codex_records(env, "app-server")
        self.assertTrue(apps, "the check never started the app-server")
        self.assertEqual([r["argv"] for r in apps if "--ignore-user-config" in (r.get("argv") or []) or r.get("rejected")], [])

    def age_refresh(self, env, value):
        """Rewrite the evidence file's `sources_refreshed_at` (a controlled clock for the daily re-check)."""
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        data = json.loads(p.read_text())
        data["sources_refreshed_at"] = value
        p.write_text(json.dumps(data))

    def marker_state(self, env):
        """The retained markers, independent of how the file orders them."""
        return sorted((m["id"], m["kind"], str(m.get("seen_at"))) for m in markers_in(self.evidence(env)))

    @staticmethod
    def adopt_newer_sol(t):
        """A corrected table: the fictional newer Sol is the Sol head (today's head a predecessor; no Astra->Sol edge across generations)."""
        old = t["families"]["codex"]["sol"]["known_good"]
        t["models"][NEWER_SOL] = dict(t["models"][old], generation=NEWER_SOL.split("-")[1], source=f"test: adopted {NEWER_SOL}")
        del t["models"][old]
        t["families"]["codex"]["sol"]["known_good"] = NEWER_SOL
        t["retired"]["codex"]["sol"].insert(0, old)
        t["fallback"]["codex"] = {}

    @staticmethod
    def old_sol_head(t):
        """A stale table: the Sol head is still the retired predecessor (the pre-change CODEX_ALIASES), today's head unknown."""
        old = t["families"]["codex"]["sol"]["known_good"]
        t["models"][RETIRED_SOL] = dict(t["models"][old], generation=RETIRED_SOL.split("-")[1], min_cli="0.144.0", source="test: stale head")
        del t["models"][old]
        t["families"]["codex"]["sol"]["known_good"] = RETIRED_SOL
        t["retired"]["codex"]["sol"] = [x for x in t["retired"]["codex"]["sol"] if x != RETIRED_SOL]
        t["fallback"]["codex"] = {}

    # ---------------------------------------------------------------- S-C3: one writer at a time (review/astra_C3_findings.md)
    HOLD = {"BRIDGE_MODELS_FETCH_DEADLINE": "120", "BRIDGE_MODELS_LOCK_DEADLINE": "120"}  # only release_holder() ends a hold

    def start_holder(self, env, prefix, *argv, **pages):
        """A writer in the middle of its source reads: `doctor` (or argv) whose three page fetches, served under /<prefix>/,
        the page server holds open until release_holder(). A refresh reads its sources under the evidence lock (S-C3 item 1),
        so this process holds the lock until then. Returns once its first page fetch has arrived."""
        self.hold_all(prefix)
        p = self.start_engine(dict(env, **self.pages.pages(prefix, **pages), **self.HOLD), *(argv or ("doctor",)))
        try:
            self.wait_page_call(prefix)
        except BaseException:
            self.release_holder(p)
            raise
        return p

    def release_holder(self, p):
        """Release every held page and wait for the holder: (exit code, stdout, stderr)."""
        self.pages.release()
        try:
            out, err = p.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        return p.returncode, out, err

    def run_within(self, seconds, env, *args):
        """Run the engine: (exit code, stdout, stderr). A run still going after `seconds` is killed and fails the test (an
        assertion, never an error)."""
        p = self.start_engine(env, *args)
        try:
            out, err = p.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            self.fail(f"`bridge.py {' '.join(args)}` was still running after {seconds:g} s and was killed:\n{out}{err}")
        return p.returncode, out, err

    def keeps_waiting(self, p, seconds, moved):
        """True when `p` is still running and `moved()` stayed false for `seconds`; False as soon as either changes."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if p.poll() is not None or moved():
                return False
            time.sleep(0.1)
        return p.poll() is None and not moved()

    def source_reads(self, env, prefix):
        """(the check's app-server requests recorded in env's STUB_CODEX_RECORD, page fetches under /<prefix>/): one process's own
        reads of the check's sources. The registry's catalog reads for the routing (client "model-registry", docs/model-routing.md
        3.3) are not the check's and are left out, as in reads()."""
        return len(self.check_records(env)), len(self.page_calls(prefix))

    def evidence_bytes(self, env):
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        return p.read_bytes() if p.exists() else b""

    def shift_timestamps(self, env, seconds):
        """Move every clock value the evidence file records by `seconds`, as if the machine's clock had moved the other way
        since the checks that wrote them. Clock values are the keys `at` and `*_at`, the file's naming convention (R-E3:
        sources_refreshed_at, evaluated_at, seen_at, the cleared log's at; each source's read and attempt times). Vendor data
        kept in observations (camelCase `retirementAt`) is not a clock and stays."""
        def stamp(value):
            try:
                epoch_of(value)
            except AssertionError:
                return False
            return True

        def move(obj):
            if isinstance(obj, dict):
                return {k: (shifted(v, seconds) if (k == "at" or k.endswith("_at")) and stamp(v) else move(v)) for k, v in obj.items()}
            if isinstance(obj, list):
                return [move(v) for v in obj]
            return obj
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        p.write_text(json.dumps(move(json.loads(p.read_text())), indent=1))

    def overdue(self, env):
        """Age the recorded refresh past twice the table's window: every model-starting invocation now wants to refresh."""
        refreshed = self.evidence(env)["sources_refreshed_at"]
        self.age_refresh(env, shifted(refreshed, -2 * TABLE["policy"]["recheck_after_hours"] * 3600))

    def json_of(self, rc, out, err):
        try:
            data = json.loads(out)
        except ValueError:
            self.fail(f"doctor --json printed no JSON (exit {rc}):\n{out}{err}")
        self.assertIsInstance(data, dict)
        return data

    @staticmethod
    def models_notices(text):
        """The leading `NOTICE: models:` lines of an output."""
        return [l for l in leading_notices(text) if l.startswith("NOTICE: models:")]

    @staticmethod
    def unverified_about(data, name):
        """The UNVERIFIED findings of a `doctor --json` evaluation that concern the source `name` (by source or by text)."""
        return [f for f in data.get("findings") or [] if isinstance(f, dict) and f.get("class") == "unverified"
                and (name in str(f.get("source") or "").lower() or name in str(f.get("text") or "").lower())]

    def kill_app_servers(self, env):
        """End the app-servers a killed or timed-out engine left behind (every stub record carries its pid)."""
        for pid in {r.get("pid") for r in self.codex_records(env, "app-server") if r.get("pid")}:
            if [l for l in self._procs_matching("stub_codex.py") if l.split(None, 1)[0] == str(pid)]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


class GroupE_TheCheck(CheckBase):
    """DESIGN §5: local sources only (the stub app-server, the fixture pages); sticky markers, removed only by an explicit
    clear, under one evidence lock with one writer at a time (S-C3); remote findings bound to the table digest; local
    findings composed at every invocation; the gate for NEW jobs only; doctor's report and exit codes."""

    def test_a_fake_live_list_with_gpt_7_sol_reports_stale(self):
        for new_id, fam in ((NEWER_SOL, "sol"), (NEWER_LUNA, "luna")):
            with self.subTest(listed=new_id):
                cat = self.catalog_file(f"stale-{new_id}", lambda ms: ms.append(self.new_entry(new_id)))
                env, _ = self.isolated(f"stale-{new_id}", catalog=cat)
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(f"stale: newer model may exist: {new_id} is listed but not in the model table", out)
                rc, data, _ = self.doctor_json(env)
                self.assertEqual(data.get("verdict"), "FAIL")

    def test_the_real_0_159_3_list_with_the_real_table_is_exactly_ok(self):
        # the live list of Kevin's Sol checkpoint (CATALOG_CURRENT) against the shipped table: every head listed, no stale member;
        # a Claude Code at or above the Sonnet head's floor 2.1.284 (2.1.282 is below it now)
        env, _ = self.isolated("real-0.159.3", STUB_CODEX_VERSION="codex-cli 0.156.1", STUB_CLAUDE_VERSION="2.1.295 (Claude Code)")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotRegex(out, r"(?m)^\s*(stale|missing): ")
        self.assertNotIn("not applicable", out)
        rc, data, err = self.doctor_json(env)
        self.assertEqual(data.get("verdict"), "OK", data)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(leading_notices(out), [])

    def test_config_read_uses_the_project_aware_form(self):
        env, _ = self.isolated("config-read", config=CONFIG_CATALOG_JSON)
        rc, out, err = self.doctor(env)
        reqs = self.check_records(env)
        cfg = [r for r in reqs if r["method"] == "config/read"]
        self.assertTrue(cfg, f"the check never asked config/read:\n{out}{err}")
        parent = os.path.realpath(str(self.jobs.parent))
        self.assertEqual(cfg[0]["params"], {"cwd": parent, "includeLayers": False})
        self.assertEqual(os.path.realpath(cfg[0]["cwd"]), parent)  # one app-server in the jobs root's parent directory
        lists = [r for r in reqs if r["method"] == "model/list"]
        self.assertEqual(len(lists), PAGES, lists)  # ten entries, pages of three: every cursor page
        self.assertTrue(all(r["params"].get("includeHidden") is True for r in lists), lists)
        self.assert_app_server_argv_clean(env)
        self.assertIn("local catalog not applicable: model_catalog_json is non-default", out)
        self.assertNotIn("VALUE-MUST-NOT-PRINT", out + err)
        self.assertEqual(rc, 0, out + err)  # UNVERIFIED, not FAIL
        self.assertEqual(oct(Path(env["BRIDGE_MODELS_EVIDENCE"]).stat().st_mode & 0o777), "0o600")  # the evidence file is private
        # the parameterless form would have answered "all default": the same catalog with an all-default answer IS applicable
        env, _ = self.isolated("config-read-default")
        rc, out, err = self.doctor(env)
        self.assertNotIn("local catalog not applicable", out)

    def test_no_configuration_value_ever_appears_anywhere(self):
        env, d = self.isolated("sentinel", config=CONFIG_SENTINEL)
        texts = []
        for argv, gate in ((["doctor"], "warn"), (["doctor", "--json"], "warn"),
                           (["ask", "--seats", "luna:low", "--job-dir", "sentinel-job", "--", "q"], "warn"),
                           (["ask", "--seats", "astra:low", "--job-dir", "sentinel-refuse-gate", "--", "q"], "refuse")):
            rc, out, err = self.run_engine(*argv, env_extra=dict(env, BRIDGE_MODELS_GATE=gate))
            texts.append(out + err)
        self.assertIn("model_providers: non-default", texts[0])
        self.assertTrue(Path(env["BRIDGE_MODELS_EVIDENCE"]).is_file(), "the check wrote no evidence file")
        blob = "\n".join(texts)
        self.assertNotIn(SENTINEL_TOKEN, blob)
        self.assertNotIn("proxy.invalid", blob)
        files = [Path(env["BRIDGE_MODELS_EVIDENCE"])] + [p for p in self.jobs.rglob("*") if p.is_file() and "sentinel" in str(p)]
        files += [p for p in (self.tmp / "home").rglob("*") if p.is_file()] + [p for p in (self.tmp / "codex-home").rglob("*") if p.is_file()]
        for p in files:
            with self.subTest(file=str(p)):
                self.assertNotIn(SENTINEL_TOKEN, p.read_text(errors="replace"))
        # MCP results: codex-skills/claude/tests/test_mcp_server.py GroupE_MCPNotices

    def test_coverage_needs_both_sources_for_a_missing_fail(self):
        # K1 enabled, an empty evidence store, the deprecations fetch failing and the overview parsed without the K1 target:
        # coverage of the K1 target is UNVERIFIED, not FAIL, and `ask` proceeds under `refuse` with the notice
        on = self.table_file("coverage-k1", lambda t: t["k1_seat"].__setitem__("enabled", True))
        env, _ = self.isolated("coverage-k1", table=on, deprecations=503, BRIDGE_MODELS_GATE="refuse")
        self.assertFalse(Path(env["BRIDGE_MODELS_EVIDENCE"]).exists())
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: models: unverified ("), out)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("missing: ", out)
        # the pure cases (both pages without the Opus head, one page only, a mangled table): test_bridge_models.py

    def test_anthropic_pages_drive_claude_findings(self):
        sonnet = head("sonnet")
        legacy = self.deprecations_with(sonnet, "Legacy", "N/A", "Not sooner than June 30, 2027")
        env, _ = self.isolated("pages-retained", deprecations=legacy)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue([l for l in out.splitlines() if l.lstrip().startswith("stale:") and sonnet in l], out)
        # the page cannot be fetched any more: the retained observation still decides, and the source is UNVERIFIED
        down = dict(env, **self.pages.pages("pages-retained-down", deprecations=503))
        rc, out, err = self.doctor(down)
        self.assertEqual(rc, 1, out + err)  # an affirmative FAIL takes precedence over an unverified source
        self.assertTrue([l for l in out.splitlines() if l.lstrip().startswith("stale:") and sonnet in l], out)
        self.assertIn("unverified", out.lower())
        rc, data, _ = self.doctor_json(down)
        self.assertEqual(data.get("verdict"), "FAIL")

    def test_markers_are_sticky(self):
        # changed for S-C3: the clear is logged as {id, at, reason, marker}; the clear_seq assertions are withdrawn (R-E3)
        sol = head("sol")
        env, _ = self.isolated("sticky")
        self.mark(env, sol, "sticky")

        def refused_everywhere(extra=None):
            for gate in ("warn", "refuse"):
                rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE=gate, **(extra or {})))
                self.assertIn(sol, self.assert_refused(rc, out, err))
            self.assertEqual([r for r in self.codex_records(env, "exec")], [])

        refused_everywhere()
        # a later successful model/list whose entry carries no indicator (the app-server answering from its bundled catalog)
        rc, out, err = self.doctor(env)
        self.assertIn(sol, self.marker_ids(env))
        refused_everywhere()
        # a refresh error
        rc, out, err = self.doctor(dict(env, STUB_CODEX_APPSERVER_FAIL="1", **self.pages.pages("sticky-down", npm=503, deprecations=503, overview=503)))
        self.assertIn(sol, self.marker_ids(env))
        refused_everywhere()
        # a table change
        edited = self.slow_table("sticky-edited", updated="2026-09-26")
        refused_everywhere({"BRIDGE_MODELS_FILE": str(edited)})
        self.assertIn(sol, self.marker_ids(env))
        # cleared only by doctor --clear-lifecycle: printed, logged, removed
        rc, out, err = self.doctor(env, "--clear-lifecycle", sol, "--reason", "vendor reversed the deprecation")
        self.assertNotIn("Traceback", err)
        self.assertIn(sol, out)
        self.assertIn("vendor reversed the deprecation", out)
        ev = self.evidence(env)
        self.assertNotIn(sol, {m["id"] for m in markers_in(ev)})
        entry = [c for c in cleared_in(ev) if c["id"] == sol]
        self.assertEqual(len(entry), 1, ev)
        self.assertEqual(entry[0]["reason"], "vendor reversed the deprecation")
        self.assertIn("marker", entry[0])
        self.assertIn("at", entry[0])
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)

    def test_a_clear_waits_for_a_running_refresh_and_only_a_later_fresh_observation_restores_the_marker(self):
        # changed for S-C3: replaces test_a_clear_is_versioned (read_seq/clear_seq are withdrawn). Astra C3-1: a clear takes the
        # evidence lock, so it waits while refresh A is in the middle of its reads; A publishes first (its affirmative
        # observation joins the sticky marker), then the clear removes the marker. A's retained observation still shows the
        # predicate, yet neither a digest recomputation nor a failed refresh derives a marker from it: the stored set passed on
        # is `{}` (the catalog carries no other lifecycle evidence and the deprecations page is never read). Only a later
        # fresh successful observation restores the marker, with a new seen_at
        sol = head("sol")
        table = self.slow_table("clear-wait", fetch_timeout_s=90, catalog_timeout_s=90)

        def only_sol(ms):
            for m in ms:
                m["upgrade"], m["upgradeInfo"] = None, None
            self.deprecate(ms, sol)
        marked = self.catalog_file("clear-wait-marked", only_sol)
        env, d = self.isolated("clear-wait", catalog=marked, table=table, deprecations=503)
        rc, out, err = self.doctor(env)
        first = [m for m in markers_in(self.evidence(env)) if m["id"] == sol]
        self.assertTrue(first, out + err)
        self.assertEqual(self.marker_ids(env), {sol}, "the setup needs Sol's marker to be the only one")
        # refresh A, held in the middle of its reads; its npm page names a version no other read serves, so A's publication is
        # visible in the evidence file
        a_npm = "0.156.77"
        pa = self.start_holder(env, "clear-wait-a", npm=json.dumps({"name": "@openai/codex", "dist-tags": {"latest": a_npm}}), deprecations=503)
        # the clear reads nothing itself (no catalog, every page failing), so no observation of its own can restore anything
        dark = dict(env, STUB_CODEX_APPSERVER_FAIL="1", STUB_CODEX_RECORD=str(d / "clear-codex.jsonl"), BRIDGE_MODELS_LOCK_DEADLINE="120",
                    **self.pages.pages("clear-wait-b", npm=503, deprecations=503, overview=503))
        held, pb = self.evidence_bytes(env), None
        try:
            pb = self.start_engine(dark, "doctor", "--clear-lifecycle", sol, "--reason", "reviewed: not deprecated")
            self.assertTrue(self.keeps_waiting(pb, 3, lambda: self.evidence_bytes(env) != held),
                            "the clear completed (or changed the evidence) while refresh A held the evidence lock")
        finally:
            rc_a, out_a, err_a = self.release_holder(pa)
            out_b, err_b = pb.communicate(timeout=180) if pb else ("", "")
        self.assertNotIn("Traceback", err_a + err_b)
        self.assertEqual(rc_a, 1, out_a + err_a)  # A's findings carry the marker its observation showed
        self.assertEqual(pb.returncode, 0, out_b + err_b)
        self.assertIn("reviewed: not deprecated", out_b)
        ev = self.evidence(env)
        self.assertIn(a_npm, list(strings(ev)), "refresh A never published")
        self.assertEqual(self.marker_ids(env), set(), "the clear did not remove the marker once A had published")
        self.assertTrue([c for c in cleared_in(ev) if c["id"] == sol], ev)
        # a launch's digest recomputation (in memory) from the retained observation that shows the predicate derives nothing,
        # reads nothing and writes nothing
        edited = self.slow_table("clear-wait-edited", updated="2026-09-26", fetch_timeout_s=90, catalog_timeout_s=90)
        before, stored = self.reads(env, "clear-wait"), self.evidence_bytes(env)
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q",
                                       env_extra=dict(env, BRIDGE_MODELS_FILE=str(edited), BRIDGE_MODELS_GATE="refuse"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.reads(env, "clear-wait"), before)
        self.assertEqual(self.evidence_bytes(env), stored)
        # a failed refresh (nothing read) derives nothing from the retained observation either
        rc, out, err = self.doctor(dark)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.marker_ids(env), set(), out + err)
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
        self.assertEqual(rc, 0, out + err)
        # a later fresh successful observation that shows the predicate restores the marker, with a new seen_at
        rc, out, err = self.doctor(env)
        again = [m for m in markers_in(self.evidence(env)) if m["id"] == sol]
        self.assertTrue(again, out + err)
        self.assertNotEqual(again[0]["seen_at"], first[0]["seen_at"])

    def test_absence_and_hidden_are_never_persisted(self):
        # each on its own, so that neither can stand in for the other: the head absent from the catalog, the head hidden
        luna, sol = head("luna"), head("sol")
        cases = {"absent": (luna, lambda ms: ms.remove(self.entry(ms, luna))),
                 "hidden": (sol, lambda ms: self.entry(ms, sol).__setitem__("hidden", True))}
        for name, (model, mutate) in cases.items():
            with self.subTest(case=name, model=model):
                env, _ = self.isolated(f"{name}-head", catalog=self.catalog_file(f"{name}-head", mutate))
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 1, out + err)  # a `local` FAIL for this machine's own login
                self.assertTrue(Path(env["BRIDGE_MODELS_EVIDENCE"]).is_file())
                self.assertNotIn(model, self.marker_ids(env), markers_in(self.evidence(env)))
                # changed for docs/model-routing.md 3.3 (F3): the seat launches only where a fresh catalog lists its model, so the
                # absent Luna head fails Luna's seat (still nothing persisted), while the hidden Sol head leaves it untouched
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
                self.assertEqual(rc, 1 if name == "absent" else 0, out + err)
                if name == "absent":
                    self.assertIn(f"this machine's own login cannot run {luna}", self.seat(self.job_dir(out), "luna")["activity"])
                first = out.splitlines()[0] if out else ""
                self.assertTrue(first.startswith("NOTICE: models: check FAILED ("), out)
                self.assertIn(model, first)  # the only FAIL line names the affected head
                rc, out, err = self.doctor(dict(env, STUB_CODEX_CATALOG=str(CATALOG_CURRENT)))  # back: nothing was kept
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn(model, self.marker_ids(env))

    def test_a_daily_check_marker_refuses_the_next_new_launch_and_only_notifies_a_running_job(self):
        sol = head("sol")
        table = self.slow_table("daily-marker", recheck_after_hours=0.0005)  # a "day" of 1.8 s
        env, _ = self.isolated("daily-marker", table=table)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        p = self.start_engine(env, "ask", "--seats", "sol:low", "--job-dir", "daily-running", "--", "[[STUB:sleep=8]] q")
        try:
            self.wait_for(self.jobs / "daily-running", "sol", lambda s: s.get("pid"))
            time.sleep(2.5)  # the refresh is due now
            marking = dict(env, STUB_CODEX_CATALOG=str(self.catalog_file("daily-marker", lambda ms: self.deprecate(ms, sol))))
            rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=marking)  # its daily re-check marks Sol
            self.assertEqual(rc, 0, out + err)
            self.assertIn(sol, self.marker_ids(env))
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)  # the running job is never refused
        self.assertEqual(self.meta(self.jobs / "daily-running")["state"], "done")
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        self.assertIn(sol, self.assert_refused(rc, out, err))

    def test_a_disabled_k1_marker_is_informational_and_enabling_restores_refusal_without_a_fetch(self):
        # changed for ruling K1-ON (review/ruling_k1_on.md): the shipped table switches the seat on, so the disabled seat is a
        # variant table. The shipped table's own check: test_doctor_covers_the_k1_target_and_a_marker_on_it_is_a_lifecycle_fail
        k1 = TABLE["k1_seat"]["id"]
        off = self.table_file("k1-marker-off", lambda t: t["k1_seat"].__setitem__("enabled", False))
        dep = self.deprecations_with(k1, "Deprecated", "September 20, 2026", "March 20, 2027")
        env, _ = self.isolated("k1-marker", table=off, deprecations=dep)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"marker on {k1} (K1 seat off): informational", out)
        self.assertIn(k1, self.marker_ids(env))
        on = self.table_file("k1-marker-on", lambda t: t["k1_seat"].__setitem__("enabled", True))
        before = self.reads(env, "k1-marker")
        rc, out, err = self.run_engine("ask", "--seats", "opus-5:high", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(on)))
        self.assertIn(k1, self.assert_refused(rc, out, err))
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(on)))
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: models: check FAILED ("), out)  # now a relevant target: a lifecycle FAIL
        self.assertEqual(self.reads(env, "k1-marker"), before)  # ... learned without any fetch

    def test_doctor_covers_the_k1_target_and_a_marker_on_it_is_a_lifecycle_fail(self):
        # ruling K1-ON (review/ruling_k1_on.md): the shipped table switches the seat on, so doctor's Claude lifecycle check covers
        # the K1 target (DESIGN §5: every enabled Claude head, plus the K1 target while enabled). A marker on it is a lifecycle
        # FAIL, not the "(K1 seat off)" informational note; both Anthropic pages read without it is `missing`, a FAIL. First the
        # seat off (a variant table), with the same pages: the target is not covered
        k1 = TABLE["k1_seat"]["id"]
        named = re.compile(r"(?<![\w.-])" + re.escape(k1) + r"(?![\w.-])")  # the K1 target's id, never inside the Opus head's
        marked = self.deprecations_with(k1, "Deprecated", "September 20, 2026", "March 20, 2027")
        unlisted, rows = re.subn(r"(?m)^\| " + re.escape(k1) + r" +\|.*\n", "", DEPRECATIONS.read_text())
        self.assertEqual(rows, 1, f"the deprecations capture has no status row for {k1}")
        self.assertFalse(named.search(OVERVIEW.read_text()), f"the overview capture lists {k1}")
        off = self.table_file("k1-doctor-off", lambda t: t["k1_seat"].__setitem__("enabled", False))
        env, _ = self.isolated("k1-doctor-off", table=off, deprecations=unlisted)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("missing: ", out)
        # the shipped table: a marker on the K1 target is a lifecycle FAIL
        env, _ = self.isolated("k1-doctor-marked", deprecations=marked)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue([l for l in out.splitlines() if l.strip().startswith("lifecycle:") and named.search(l)], out)
        self.assertNotIn("(K1 seat off)", out)
        self.assertFalse([l for l in out.splitlines() if l.strip().startswith(f"marker on {k1}")], out)
        entry = [m for l in out.splitlines() if l.startswith("markers: ") for m in l[len("markers: "):].split("; ") if m.startswith(k1 + " ")]
        self.assertEqual(len(entry), 1, out)
        self.assertNotIn("informational", entry[0])
        rc, data, _ = self.doctor_json(env)
        self.assertEqual(data.get("verdict"), "FAIL")
        self.assertTrue([f for f in data.get("findings") or [] if f.get("class") == "lifecycle" and named.search(f.get("text") or "")], data)
        # the shipped table, both pages read and neither lists it: missing, a FAIL
        env, _ = self.isolated("k1-doctor-unlisted", deprecations=unlisted)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"missing: {k1} is in neither Anthropic source", out)

    def test_the_verdict_is_bound_to_the_table_digest_and_keeps_the_refresh_clock(self):
        # changed for S-C3 (item 4): a launch recomputes on a digest change IN MEMORY and never writes the evidence file, so the
        # recorded table_sha256 stays the one of the last persisted evaluation; doctor and the refresh persist under the current
        # table. (Before S-C3 this test asserted that the launch itself re-recorded the digest.)
        # a retained FAIL from a fictional newer Sol; a table that adopts it as the head: OK at the next invocation, without a fetch
        cat = self.catalog_file("digest-newer-sol", lambda ms: ms.append(self.new_entry(NEWER_SOL)))
        env, _ = self.isolated("digest", catalog=cat, BRIDGE_MODELS_GATE="refuse")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)
        refreshed, markers = self.evidence(env).get("sources_refreshed_at"), self.marker_state(env)
        recorded = self.evidence(env).get("table_sha256")
        self.assertIsNotNone(refreshed)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(self.STALE_NEWER_SOL, err)
        self.assertIn("Run: bridge.py doctor", err)
        fixed = self.table_file("digest-newer-sol-adopted", self.adopt_newer_sol)
        before = self.reads(env, "digest")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(fixed)))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(leading_notices(out), [])
        self.assertEqual(self.reads(env, "digest"), before)
        ev = self.evidence(env)
        self.assertEqual(ev.get("table_sha256"), recorded, "a launch wrote its recomputation (only doctor and the refresh write)")
        self.assertEqual(ev.get("sources_refreshed_at"), refreshed)
        self.assertEqual(self.marker_state(env), markers)
        rc, out, err = self.doctor(dict(env, BRIDGE_MODELS_FILE=str(fixed)))  # doctor persists under the current table
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.evidence(env).get("table_sha256"), hashlib.sha256(fixed.read_bytes()).hexdigest())
        # a retained OK; a table whose Sol head is stale: FAIL without a fetch
        env, _ = self.isolated("digest-ok", BRIDGE_MODELS_GATE="refuse")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        refreshed, markers = self.evidence(env).get("sources_refreshed_at"), self.marker_state(env)
        recorded = self.evidence(env).get("table_sha256")
        before = self.reads(env, "digest-ok")
        stale = self.table_file("digest-stale-sol", self.old_sol_head)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(stale)))
        self.assertEqual(rc, 2, out + err)
        self.assertIn(f"stale: newer model may exist: {head('sol')} is listed but not in the model table", err)
        self.assertEqual(self.reads(env, "digest-ok"), before)
        self.assertEqual(self.evidence(env).get("sources_refreshed_at"), refreshed)
        self.assertEqual(self.marker_state(env), markers)
        self.assertEqual(self.evidence(env).get("table_sha256"), recorded, "a launch wrote its recomputation")
        # a digest change inside the window does not postpone the next refresh: the recorded refresh is aged to "hour 23" and
        # then to "hour 25" of the table's window (a controlled clock: no sleeps, nothing depends on how fast a run is)
        day = self.slow_table("digest-day")
        env, _ = self.isolated("digest-day", table=day)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        refreshed = self.evidence(env).get("sources_refreshed_at")
        self.assertIsNotNone(refreshed)
        window = TABLE["policy"]["recheck_after_hours"] * 3600
        hour23, hour25 = shifted(refreshed, -window * 23 / 24), shifted(refreshed, -window * 25 / 24)
        self.age_refresh(env, hour23)
        edited = self.slow_table("digest-day-edited", updated="2026-09-26")
        before = self.reads(env, "digest-day")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(edited)))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.reads(env, "digest-day"), before)  # recomputed, not refreshed
        ev = self.evidence(env)
        self.assertEqual(ev.get("table_sha256"), hashlib.sha256(day.read_bytes()).hexdigest())  # in memory: nothing written
        self.assertEqual(ev.get("sources_refreshed_at"), hour23)  # the refresh clock is untouched
        self.age_refresh(env, hour25)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(edited)))
        self.assertEqual(rc, 0, out + err)
        after = self.reads(env, "digest-day")
        self.assertGreater(after[0], before[0])
        self.assertGreater(after[1], before[1])
        renewed = self.evidence(env).get("sources_refreshed_at")
        self.assertNotEqual(renewed, hour25)
        self.assertGreaterEqual(epoch_of(renewed), epoch_of(refreshed))
        self.assertEqual(self.evidence(env).get("table_sha256"), hashlib.sha256(edited.read_bytes()).hexdigest())  # the refresh persists

    def test_local_findings_are_composed_at_each_invocation(self):
        env, _ = self.isolated("compose")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        refreshed, before = self.evidence(env).get("sources_refreshed_at"), self.reads(env, "compose")

        def ask(extra, spec="luna:low"):
            return self.run_engine("ask", "--seats", spec, "--", "q", env_extra=dict(env, **extra))

        rc, out, err = ask({"BRIDGE_REVIEW_SEATS": "spark"})  # `defaults` FAIL: the job proceeds under warn with the notice
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: models: check FAILED (") and "BRIDGE_REVIEW_SEATS" in out.splitlines()[0], out)
        jobs = self.job_names()
        rc, out, err = ask({"BRIDGE_REVIEW_SEATS": "spark", "BRIDGE_MODELS_GATE": "refuse"})
        self.assertEqual(rc, 2, out + err)
        self.assertIn("Run: bridge.py doctor", err)
        self.assertEqual(self.new_jobs(jobs), [])
        rc, out, err = ask({"BRIDGE_MODELS_GATE": "refuse"})  # unset again: clears at once
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(leading_notices(out), [])
        # `cli`/`discovery` below 0.156.1 (Astra itself only needs 0.153.0), cleared by an upgrade
        rc, out, err = ask({"STUB_CODEX_VERSION": "codex-cli 0.155.0"}, "astra:low")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: models: check FAILED ("), out)
        rc, out, err = ask({"STUB_CODEX_VERSION": "codex-cli 0.155.0", "BRIDGE_MODELS_GATE": "refuse"}, "astra:low")
        self.assertEqual(rc, 2, out + err)
        rc, out, err = ask({"STUB_CODEX_VERSION": "codex-cli 0.156.1", "BRIDGE_MODELS_GATE": "refuse"}, "astra:low")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(leading_notices(out), [])
        # the reverse: a retained OK turns FAIL when an override is set, a default goes bad or the CLI is downgraded
        for extra in ({"ANTHROPIC_DEFAULT_OPUS_MODEL": K1_ID}, {"BRIDGE_DEFAULT_SEAT": REFUSED_GPT6},
                      {"STUB_CODEX_VERSION": "codex-cli 0.155.0"}):
            with self.subTest(change=extra):
                rc, out, err = ask(dict(extra, BRIDGE_MODELS_GATE="refuse"), "astra:low")
                self.assertEqual(rc, 2, out + err)
                self.assertIn("Run: bridge.py doctor", err)
        self.assertEqual(self.reads(env, "compose"), before)  # no catalog read and no page fetch on any of these
        self.assertEqual(self.evidence(env).get("sources_refreshed_at"), refreshed)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)

    def test_writers_are_serialized_and_a_waiting_refresh_skips_when_no_longer_due(self):
        # changed for S-C3 (item 1): replaces test_overlapping_writers_never_lose_a_marker_or_publish_findings_without_it (writers
        # no longer overlap, so nothing is merged). While writer A is in the middle of its reads, holding the evidence lock, a
        # second doctor B starts no read of its own: it waits for the lock. Once A has published, the refresh B wanted is no
        # longer due (A refreshed after B started), so B skips its reads and reports A's publication. A's catalog lists
        # a fictional newer Sol and B's own sources are clean, so a verdict carrying that stale line is A's
        table = self.slow_table("serial", fetch_timeout_s=90, catalog_timeout_s=90)
        env, d = self.isolated("serial", table=table)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        newer = self.catalog_file("serial-a", lambda ms: ms.append(self.new_entry(NEWER_SOL)))
        pa = self.start_holder(dict(env, STUB_CODEX_CATALOG=str(newer)), "serial-a")
        b_env = dict(env, STUB_CODEX_RECORD=str(d / "b-codex.jsonl"), BRIDGE_MODELS_LOCK_DEADLINE="120", **self.pages.pages("serial-b"))
        pb = None
        try:
            pb = self.start_engine(b_env, "doctor")
            self.assertTrue(self.keeps_waiting(pb, 3, lambda: any(self.source_reads(b_env, "serial-b"))),
                            f"doctor B read its sources (or finished) while writer A held the evidence lock: "
                            f"(app-server requests, page fetches) = {self.source_reads(b_env, 'serial-b')}")
        finally:
            rc_a, out_a, err_a = self.release_holder(pa)
            out_b, err_b = pb.communicate(timeout=180) if pb else ("", "")
        self.assertEqual(rc_a, 1, out_a + err_a)
        self.assertIn(self.STALE_NEWER_SOL, out_a)
        self.assertEqual(self.source_reads(b_env, "serial-b"), (0, 0), "B refreshed again right after A's refresh")
        self.assertEqual(pb.returncode, 1, out_b + err_b)
        self.assertIn(self.STALE_NEWER_SOL, out_b)  # B reported A's publication
        self.assertIn(NEWER_SOL, list(strings(self.evidence(env))))

    def test_the_gate_acts_on_the_composed_verdict(self):
        luna = head("luna")
        no_luna = self.catalog_file("gate-local", lambda ms: ms.remove(self.entry(ms, luna)))
        cases = {"local": ({"catalog": no_luna}, [luna]),
                 "host": ({"ANTHROPIC_DEFAULT_OPUS_MODEL": K1_ID}, ["ANTHROPIC_DEFAULT_OPUS_MODEL"]),
                 "mixed": ({"catalog": no_luna, "ANTHROPIC_DEFAULT_OPUS_MODEL": K1_ID}, [luna])}
        for name, (kw, needles) in cases.items():
            with self.subTest(fail=name):
                env, _ = self.isolated(f"gate-{name}", **kw)
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 1, out + err)
                rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", f"gate-{name}", "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)  # warn: one persisted notice, the job proceeds
                first = out.splitlines()[0]
                self.assertTrue(first.startswith("NOTICE: models: check FAILED (") and first.endswith("); run bridge.py doctor"), first)
                for needle in needles:
                    self.assertIn(needle, first)
                meta = self.meta(self.jobs / f"gate-{name}")
                self.assertEqual([n["kind"] for n in meta.get("notices") or []].count("models-check"), 1)
                self.assertEqual(meta["check"]["verdict"], "FAIL")
                for key in ("evaluated_at", "sources_refreshed_at", "table_sha256"):
                    self.assertIn(key, meta["check"])
                jobs, launched = self.job_names(), len(self.launched_models(env))
                rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
                self.assertEqual(rc, 2, out + err)  # refuse: refused before the job exists, with the lines and the fix
                self.assertIn("Run: bridge.py doctor", err)
                for needle in needles:
                    self.assertIn(needle, err)
                self.assertEqual(self.new_jobs(jobs), [])
                self.assertEqual(len(self.launched_models(env)), launched)
                # a reply on an existing job proceeds with the notice; status and result are unaffected
                rc, out, err = self.run_engine("reply", f"gate-{name}", "--", "more", env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(out.startswith("NOTICE: models: check FAILED ("), out)
                for argv in (["status", f"gate-{name}"], ["result", f"gate-{name}"]):
                    rc, out, err = self.run_engine(*argv, env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
                    self.assertEqual(rc, 0, out + err)
        # UNVERIFIED: one notice; the job proceeds under both gates
        for gate in ("warn", "refuse"):
            with self.subTest(unverified=gate):
                env, _ = self.isolated(f"gate-unverified-{gate}", npm=503, deprecations=503, overview=503, BRIDGE_MODELS_GATE=gate)
                rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(out.splitlines()[0].startswith("NOTICE: models: unverified ("), out)
                self.assertEqual(len([l for l in leading_notices(out) if l.startswith("NOTICE: models: ")]), 1)
        # the table's policy.stale_gate = refuse behaves like the environment variable
        strict = self.table_file("gate-policy-refuse", lambda t: t["policy"].__setitem__("stale_gate", "refuse"))
        env, _ = self.isolated("gate-policy", catalog=no_luna, table=strict)
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("Run: bridge.py doctor", err)

    def test_the_recheck_runs_once_a_day_in_the_parent(self):
        luna = head("luna")
        env, _ = self.isolated("recheck")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        cfg = [r for r in self.check_records(env) if r["method"] == "config/read"]
        self.assertEqual(len(cfg), 1, "the first invocation of the day refreshes the sources once")
        self.assertEqual(os.path.realpath(cfg[0]["cwd"]), os.path.realpath(str(self.jobs.parent)))
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in self.page_calls("recheck")), ["deprecations", "npm", "overview"])
        self.assert_app_server_argv_clean(env)  # the daily re-check on a model-starting invocation, too
        once = self.reads(env, "recheck")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.reads(env, "recheck"), once, "a second invocation within the day read the sources again")
        # a stale sources_refreshed_at: one re-check before validation, its markers published before the validator runs
        short = self.slow_table("recheck-short", recheck_after_hours=0.0003)
        marking = self.catalog_file("recheck-marking", lambda ms: self.deprecate(ms, luna))
        time.sleep(1.5)
        stale_env = dict(env, BRIDGE_MODELS_FILE=str(short), STUB_CODEX_CATALOG=str(marking))
        execs = len(self.codex_records(env, "exec"))
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=stale_env)
        self.assertIn(luna, self.assert_refused(rc, out, err))
        self.assertEqual(len([r for r in self.check_records(env) if r["method"] == "config/read"]), 2)
        self.assertEqual(len(self.codex_records(env, "exec")), execs)

    def test_a_bad_env_default_is_a_fail(self):
        for var, value in (("BRIDGE_REVIEW_SEATS", "spark"), ("BRIDGE_DEFAULT_SEAT", NEWER_SOL),
                           ("BRIDGE_COUNCIL_SEATS", "astra:ultra,5.5"), ("BRIDGE_DEFAULT_SEAT", "luna:ultra")):
            with self.subTest(var=var, value=value):
                env, _ = self.isolated(f"bad-default-{var}-{value.replace(':', '-').replace(',', '-')}", **{var: value})
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(var, out)
                rc, data, _ = self.doctor_json(env)
                self.assertEqual(data.get("verdict"), "FAIL")

    def test_host_overrides_and_relay_claude_cli_are_reported_by_name(self):
        secret = "OPUS-OVERRIDE-SECRET-VALUE-7731"
        for var in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            with self.subTest(var=var):
                env, _ = self.isolated(f"host-{var}", **{var: secret})
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(var, out)
                self.assertNotIn(secret, out + err)
        old = self.case_dir("host-relay-claude") / "claude"
        old.write_text(f"#!/bin/sh\nSTUB_CLAUDE_VERSION='2.1.279 (Claude Code)' exec '{sys.executable}' '{STUB_CLAUDE}' \"$@\"\n")
        old.chmod(0o755)
        env, _ = self.isolated("host-relay-claude", RELAY_CLAUDE_BIN=str(old))
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)  # relay's Claude CLI below the floor
        self.assertTrue(str(old) in out or os.path.realpath(str(old)) in out, out)
        self.assertIn("2.1.279", out)
        env, _ = self.isolated("host-claude-code", STUB_CLAUDE_VERSION="2.1.279 (Claude Code)")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)  # the host agents' Claude Code below the highest Claude floor (2.1.284)

    def test_nothing_reads_models_cache_or_credentials(self):
        root = self.case_dir("canaries")
        home, codex_home = root / "home", root / "codex-home"
        canaries = FifoCanaries([codex_home / "models_cache.json", codex_home / "auth.json", home / ".codex" / "models_cache.json",
                                 home / ".codex" / "auth.json", home / ".claude" / ".credentials.json"])
        try:
            env, _ = self.isolated("canaries", HOME=str(home), CODEX_HOME=str(codex_home))
            for argv in (["doctor"], ["ask", "--seats", "luna:low", "--job-dir", "canary-job", "--", "q"],
                         ["ask", "--seats", "opus:low", "--", "q"], ["reply", "canary-job", "--", "more"]):
                rc, out, err = self.run_engine(*argv, env_extra=env, timeout=90)
                self.assertEqual(rc, 0, f"{argv}: {out}{err}")
            apps = self.codex_records(env, "app-server")
            self.assertTrue(apps, "the check never ran here, so the canaries prove nothing")
            self.assertTrue(all(os.path.realpath(r["codex_home"] or "") == os.path.realpath(str(codex_home)) for r in apps), apps)
        finally:
            opened = canaries.close()
        self.assertEqual(opened, [], "models_cache.json or a credential file was opened")

    def test_doctor_never_launches_a_model_or_calls_relay(self):
        env, _ = self.isolated("doctor-quiet")
        renv, rd = self.relay("doctor-quiet", {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("routed accounts: availability is decided by the CLI at launch; doctor does not consult relay", out)
        self.assertEqual(self.codex_records(env, "exec"), [])
        self.assertEqual(self.claude_records(env), [])
        self.assertEqual(self.records(rd, "calls.jsonl"), [])
        self.assertTrue(self.codex_records(env, "app-server"), "doctor ran no check")

    def test_doctor_states_and_exit_codes(self):
        env, _ = self.isolated("states-ok")
        rc, data, err = self.doctor_json(env)
        self.assertEqual((rc, data.get("verdict")), (0, "OK"))
        env, _ = self.isolated("states-unverified", npm=503, deprecations=503, overview=503)
        rc, data, err = self.doctor_json(env)
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"))
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("unverified", out.lower())
        env, _ = self.isolated("states-fail", catalog=self.catalog_file("states-fail", lambda ms: ms.append(self.new_entry(NEWER_SOL))))
        rc, data, err = self.doctor_json(env)
        self.assertEqual((rc, data.get("verdict")), (1, "FAIL"))
        self.assertIn(self.STALE_NEWER_SOL, list(strings(data)))
        env, _ = self.isolated("states-no-cli", CODEX_BIN=str(self.tmp / "missing-codex"), CLAUDE_BIN=str(self.tmp / "missing-claude"))
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)

    def test_doctor_prints_versions_floors_defaults_sources_markers_digest_clocks_and_the_k1_state(self):
        # changed for S-C3 (item 3, R-E3): clear_seq is no longer printed (nor read_seq); the other clocks and the digest are
        # a Claude Code at or above the Sonnet head's floor 2.1.284 (2.1.282 is below it now)
        env, _ = self.isolated("doctor-report", STUB_CODEX_VERSION="codex-cli 0.156.1", STUB_CLAUDE_VERSION="2.1.295 (Claude Code)")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("clear_seq", out)
        self.assertNotIn("read_seq", out)
        table_bytes = MODELS_JSON.read_bytes()
        needles = ["codex-cli 0.156.1", "2.1.295 (Claude Code)", hashlib.sha256(table_bytes).hexdigest(),
                   "sources_refreshed_at", "evaluated_at", "opus-5", "spark", str(STUB_CLAUDE),
                   "routed accounts: availability is decided by the CLI at launch; doctor does not consult relay",
                   "astra:ultra,sol:ultra,opus:max", RETIRING_FULL]
        for fam in families():
            needles.append(head(fam))
        for needle in needles:
            self.assertIn(needle, out)
        for var, name in (("BRIDGE_MODELS_NPM_URL", "npm"), ("BRIDGE_MODELS_DEPRECATIONS_URL", "deprecations"),
                          ("BRIDGE_MODELS_OVERVIEW_URL", "overview")):  # each source, by its URL or its name
            self.assertTrue(env[var] in out or re.search(rf"(?im)^.*\b{name}\b.*$", out), f"source {name} not reported")
        for model, rec in TABLE["models"].items():  # every floor, on the line naming its model
            if rec["min_cli"] and model in [head(f) for f in families()]:
                self.assertTrue([l for l in out.splitlines() if model in l and rec["min_cli"] in l], f"{model} >= {rec['min_cli']}")
        self.assertTrue([l for l in out.splitlines() if head("astra") in l and head("sol") in l and "->" in l], "the fallback edge")
        # changed for ruling K1-ON (review/ruling_k1_on.md): the shipped table switches the seat on. The lines naming the seat
        # itself (opus-5, not inside the K1 target's id or the Opus head's)
        k1_lines = [l for l in out.splitlines() if re.search(r"(?<![\w.-])opus-5(?![\w.-])", l)]
        self.assertTrue([l for l in k1_lines if re.search(r"(?i)\b(?:on|true)\b", l)], f"the K1 state (on): {k1_lines}")
        self.assertFalse([l for l in k1_lines if re.search(r"(?i)\b(?:off|false)\b", l)], f"the K1 state (on): {k1_lines}")
        self.assertTrue(out.lstrip().splitlines()[0].strip(), out)


class GroupE_MarkersAcrossPaths(EngineBase):
    """The marker-dependent halves of DESIGN §8 tests listed under A, B and C. A sticky marker exists only once the check
    has recorded one (DESIGN §9 commit 3), so these run with group E; each name says which §8 test it completes."""

    def test_k1_seat_obeys_markers_even_when_on(self):  # A test_k1_seat_is_a_switch_and_obeys_markers
        k1 = TABLE["k1_seat"]["id"]
        on = self.table_file("markers-k1-on", lambda t: t["k1_seat"].__setitem__("enabled", True))
        env, _ = self.isolated("markers-k1", table=on)
        self.mark(env, k1, "markers-k1")
        rc, out, err = self.run_engine("ask", "--seats", "opus-5:high", "--", "q", env_extra=env)
        self.assertIn(k1, self.assert_refused(rc, out, err))
        self.assertEqual(self.launched_models(env), [])

    def test_a_fallback_target_is_validated_initially_with_its_markers(self):  # B test_a_fallback_target_is_validated_...
        astra, sol = head("astra"), head("sol")
        env, _ = self.isolated("markers-fallback")
        self.mark(env, sol, "markers-fallback")
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "markers-fallback", "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [astra])  # the marked target is validated `initial` and never launched
        seat = self.seat(self.jobs / "markers-fallback", "astra")
        self.assertEqual(seat["state"], "failed")
        self.assertIn(sol, seat["activity"] + out)

    def test_a_marker_refuses_in_initial_mode_before_any_launch(self):  # B first_launch_at; validation refusals never fall back
        astra = head("astra")
        env, _ = self.isolated("markers-initial")
        renv, rd = self.relay("markers-initial", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        self.mark(env, astra, "markers-initial")
        before = self.job_names()
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--", "q", env_extra=env)
        self.assertIn(astra, self.assert_refused(rc, out, err))
        self.assertEqual(self.launched_models(env), [])  # neither the marked model nor its fallback target
        self.assertEqual(self.new_jobs(before), [])
        self.assertEqual(self.picks(rd), [])

    def test_continuation_attempts_turn_new_markers_into_notices(self):  # B, both vendors, routed and unrouted
        luna, sonnet = head("luna"), head("sonnet")
        routed = lambda vendor: {vendor: [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + 8}, {"exit": 0, "label": "b"}]}
        scenarios = (
            ("cont-codex-ladder", "luna:low", luna, None, {"BRIDGE_LIMIT_WAIT_SHORT": "6"}),
            ("cont-codex-routed", "luna:low", luna, "codex", {}),
            ("cont-claude-ladder", "sonnet:low", sonnet, None, {"BRIDGE_LIMIT_WAIT_SHORT": "6"}),
            ("cont-claude-routed", "sonnet:low", sonnet, "claude", {}),
        )
        for name, spec, model, vendor, extra in scenarios:
            with self.subTest(scenario=name):
                env, d = self.isolated(name, **extra)
                if vendor:  # moved after a limit: the next account is only offered 8 s later (written now, not at definition)
                    renv, rd = self.relay(name, {"pick": routed(vendor)})
                    env.update(renv)
                rc, out, err = self.doctor(env)
                # changed for S-LAUNCH: a routed Codex attempt shows that its CLI started (thread.started before the stderr-only
                # limit), so its retry is a continuation; a routed attempt without any event has not started (initial mode)
                stub = f"threadfirst,limit={d / 'limit'}" if vendor == "codex" else f"limit={d / 'limit'}"
                p = self.start_engine(env, "ask", "--seats", spec, "--job-dir", name, "--", f"[[STUB:{stub}]] q")
                try:
                    self.wait_for(self.jobs / name, spec.split(":")[0], lambda s: s.get("state") == "waiting")
                    self.mark(env, model, name)  # another invocation's check learns a marker while the seat is parked
                finally:
                    out, err = p.communicate(timeout=180)
                self.assertEqual(p.returncode, 0, out + err)  # the seat continues
                tag = spec.split(":")[0]
                self.assertTrue(any(l.startswith(f"NOTICE: seat {tag}: {model} was marked ") and l.endswith("after this seat started; the seat continues")
                                    for l in leading_notices(out)), out)
                rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)  # the next NEW launch is refused
                self.assertIn(model, self.assert_refused(rc, out, err))
                rc, out, err = self.run_engine("reply", name, "--", "more", env_extra=env)  # a user-requested reply validates `initial`
                self.assertEqual(rc, 1, out + err)
                self.assertIn(model, out + err)

    def test_a_refused_reply_on_a_marked_head_leaves_the_parent_outcome_unchanged(self):  # B test_a_refused_reply_...
        luna = head("luna")
        env, _ = self.isolated("markers-reply")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "markers-reply", "--", "[[STUB:text=PARENT-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.mark(env, luna, "markers-reply")
        d = self.jobs / "markers-reply"
        rc, out, err = self.run_engine("reply", "markers-reply", "--", "more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(len(self.codex_records(env, "exec")), 1)
        replies = sorted(d.glob("seat-luna-reply-*.json"))
        self.assertEqual([json.loads(p.read_text())["state"] for p in replies], ["failed"])
        m = self.meta(d)
        self.assertEqual(m["state"], "done")
        self.assertTrue(any(luna in n.get("text", "") for n in m.get("notices") or []), m.get("notices"))
        rc, out, err = self.run_engine("result", "markers-reply", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: "), out)
        self.assertIn("PARENT-ANSWER", out)

    def test_reply_keeps_the_k1_seat_subject_to_markers(self):  # C test_reply_keeps_the_k1_seat_by_its_recorded_identity_only
        k1 = TABLE["k1_seat"]["id"]
        on = self.table_file("markers-k1-reply", lambda t: t["k1_seat"].__setitem__("enabled", True))
        env, _ = self.isolated("markers-k1-reply", table=on)
        rc, out, err = self.run_engine("ask", "--seats", "opus-5:high", "--job-dir", "markers-k1-reply", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.mark(env, k1, "markers-k1-reply")
        rc, out, err = self.run_engine("reply", "markers-k1-reply", "--", "more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn(k1, out + err)
        self.assertEqual(len(self.claude_records(env)), 1)


# ==================================================================================================================
# Astra's findings on build commit 3 (review/astra_C3_findings.md; D24: failing tests first) and the S-C3 ruling
# ==================================================================================================================
class GroupE_C3Findings(CheckBase):
    """Regression tests for the commit-3 findings accepted as Astra wrote the fixes: C3-2 and C3-5 to C3-14, each named in a
    comment. C3-1, C3-3 and C3-4 are resolved by S-C3 and tested with it (GroupE_SingleWriter, and the clear test in
    GroupE_TheCheck); the pure pieces are in test_bridge_models.py GroupE_C3Pure."""

    # server text that begins and ends with a marker, so that any piece of it (a truncated message included) is detectable
    SERVER_TEXT = f"stub-server-text-3f1c: rejected model_providers.proxy.http_headers: Authorization: Bearer {SENTINEL_TOKEN} (stub-server-text-3f1c)"

    def test_app_server_errors_become_an_allowlisted_diagnostic_never_server_text(self):
        # C3-2 [P1]: a config/read or model/list error whose message and data carry a configuration value (an authorization
        # header with the sentinel). What is kept and printed is the RPC method and the error code, never the server's text:
        # not in doctor's report or JSON, a job's output or result, the evidence file or any job file
        for method in ("config/read", "model/list"):
            with self.subTest(method=method):
                name = "c3-2-" + method.replace("/", "-")
                env, _ = self.isolated(name, STUB_CODEX_RPC_ERROR=method, STUB_CODEX_RPC_ERROR_CODE="-32603",
                                       STUB_CODEX_RPC_ERROR_TEXT=self.SERVER_TEXT)
                outputs = {}
                for label, argv in (("doctor", ["doctor"]), ("doctor --json", ["doctor", "--json"]),
                                    ("ask", ["ask", "--seats", "luna:low", "--job-dir", name, "--", "q"]), ("result", ["result", name])):
                    rc, out, err = self.run_engine(*argv, env_extra=env)
                    outputs[label] = out + err
                self.assertTrue([r for r in self.codex_records(env, "app-server") if r.get("method") == method],
                                f"the check never sent {method}")
                files = [Path(env["BRIDGE_MODELS_EVIDENCE"])] + [p for p in (self.jobs / name).rglob("*") if p.is_file()]
                texts = list(outputs.items()) + [(str(p), p.read_text(errors="replace")) for p in files]
                leaks = [where for where, text in texts
                         if SENTINEL_TOKEN in text or "stub-server-text-3f1c" in text or "Authorization: Bearer" in text]
                self.assertEqual(leaks, [], "the app-server's error text (carrying a configuration value) reached these")
                named = [l for l in outputs["doctor"].splitlines() if method in l and "-32603" in l]
                self.assertTrue(named, "doctor names the failed request by its RPC method and error code:\n" + outputs["doctor"])

    def test_absence_is_confirmed_only_by_both_sources_read_in_this_check(self):
        # C3-5: the deprecations page read earlier omitted the Opus head while the overview listed it; now the deprecations fetch
        # fails and the new overview omits it. The page read earlier is not a successful read in THIS check, so its omission
        # cannot establish `missing` (a FAIL that refuses new jobs under `refuse`): coverage of the head is UNVERIFIED, and stays
        # so through a digest recomputation (which carries each source's latest attempt). Both pages parsed in one check and
        # both silent still confirm the absence
        opus = head("opus")
        dep_without = EVIDENCE / "anthropic_model-deprecations_20261008_without-opus-5-5.md"
        ov_without = EVIDENCE / "anthropic_models-overview_20261008_without-opus-5-5.md"
        env, _ = self.isolated("c3-5", deprecations=dep_without)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)  # the overview lists the head: present
        later = dict(env, **self.pages.pages("c3-5-later", deprecations=503, overview=ov_without))
        rc, data, err = self.doctor_json(later)
        texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
        self.assertEqual([t for t in texts if t.startswith("missing:")], [], "a cached omission established absence")
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), texts)
        self.assertTrue([t for t in texts if t.startswith("unverified") and opus in t], texts)
        refuse = dict(later, BRIDGE_MODELS_GATE="refuse")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=refuse)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: models: unverified ("), out)
        edited = self.table_file("c3-5-edited", lambda t: t.__setitem__("updated", "2026-09-26"))
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(refuse, BRIDGE_MODELS_FILE=str(edited)))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("missing: ", out + err)
        both = dict(env, **self.pages.pages("c3-5-both", deprecations=dep_without, overview=ov_without))
        rc, out, err = self.doctor(both)
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"missing: {opus} is in neither Anthropic source", out)

    def test_a_marker_stored_under_a_listed_spelling_is_the_heads_lifecycle_fail_everywhere(self):
        # C3-6: a legacy marker keyed by the Haiku head's listed spelling. The launch validator already treats it as the head's
        # marker; the digest recomputation (which passes the stored set canonicalized), doctor's evaluation and doctor's marker
        # report must too, instead of calling it an informational marker on an unknown ID
        haiku, alias = head("haiku"), HAIKU_ALIAS
        env, _ = self.isolated("c3-6", table=self.table_file("c3-6", with_haiku_alias))
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        doc = json.loads(p.read_text())
        record = {"id": alias, "kind": "retired", "kinds": ["retired"], "source": "anthropic model-deprecations", "seen_at": time.time(),
                  "detail": "Retired; retirement September 1, 2026"}
        markers = doc.setdefault("markers", {})
        if isinstance(markers, list):
            markers.append(record)
        else:
            markers[alias] = record
        p.write_text(json.dumps(doc, indent=1))
        with self.subTest(path="a launch's digest recomputation"):
            edited = self.table_file("c3-6-edited", lambda t: (with_haiku_alias(t), t.__setitem__("updated", "2026-09-26")))
            rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_FILE=str(edited)))
            self.assertEqual(rc, 0, out + err)
            first = out.splitlines()[0] if out else ""
            self.assertTrue(first.startswith("NOTICE: models: check FAILED (") and haiku in first, out)
            rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q",
                                           env_extra=dict(env, BRIDGE_MODELS_FILE=str(edited), BRIDGE_MODELS_GATE="refuse"))
            self.assertEqual(rc, 2, out + err)
            self.assertIn(haiku, err)
        with self.subTest(path="doctor's evaluation and marker report"):
            rc, out, err = self.doctor(env)
            self.assertEqual(rc, 1, out + err)
            self.assertTrue([l for l in out.splitlines() if l.strip().startswith("lifecycle:") and haiku in l], out)
            self.assertNotRegex(out, re.escape(alias) + r"[^;\n]*informational")
            rc, out, err = self.run_engine("ask", "--seats", "haiku", "--", "q", env_extra=env)
            self.assertIn(haiku, self.assert_refused(rc, out, err))

    def test_app_server_frames_are_read_from_bytes_against_a_deadline(self):
        # C3-7: frames read with non-blocking byte reads, a newline buffer and a monotonic deadline. (1) Every response comes in
        # one write together with a notification: the response must be found although nothing is left in the pipe. (2) The
        # first answer stops halfway through its frame while the app-server keeps its stdout open for 60 s: the read gives up
        # at its deadline (BRIDGE_MODELS_FETCH_DEADLINE, the table's catalog_timeout_s) and the catalog is UNVERIFIED
        fast = self.slow_table("c3-7", catalog_timeout_s=3, fetch_timeout_s=3)
        with self.subTest(frames="a notification and each response in one write"):
            env, _ = self.isolated("c3-7-together", table=fast, STUB_CODEX_APPSERVER_FRAMES="together", BRIDGE_MODELS_FETCH_DEADLINE="4")
            rc, out, err = self.run_within(60, env, "doctor", "--json")
            data = self.json_of(rc, out, err)
            self.assertEqual(data.get("verdict"), "OK", [f.get("text") for f in data.get("findings") or [] if isinstance(f, dict)])
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(len([r for r in self.check_records(env) if r.get("method") == "model/list"]), PAGES)
        with self.subTest(frames="half a frame, then silence with stdout open"):
            env, _ = self.isolated("c3-7-partial", table=fast, STUB_CODEX_APPSERVER_FRAMES="partial:60", BRIDGE_MODELS_FETCH_DEADLINE="4")
            try:
                rc, out, err = self.run_within(20, env, "doctor", "--json")
            finally:
                self.kill_app_servers(env)
            data = self.json_of(rc, out, err)
            self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), out + err)
            self.assertTrue(self.unverified_about(data, "codex"), data.get("findings"))

    def test_a_page_fetch_has_a_total_deadline(self):
        # C3-8: the npm page answers at once, then delivers its body one byte every 0.25 s (a minute in all). Every read returns
        # well within any socket timeout, so only a total deadline across connection and body ends the fetch; its expiry is
        # UNVERIFIED
        fast = self.slow_table("c3-8", catalog_timeout_s=3, fetch_timeout_s=3)
        env, _ = self.isolated("c3-8", table=fast, BRIDGE_MODELS_FETCH_DEADLINE="4")
        self.pages.trickle("/c3-8/npm", json.dumps({"name": "@openai/codex", "dist-tags": {"latest": "0.156.1"}}).ljust(240), interval=0.25)
        rc, out, err = self.run_within(20, env, "doctor", "--json")
        data = self.json_of(rc, out, err)
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), out + err)
        self.assertTrue(self.unverified_about(data, "npm"), data.get("findings"))

    def test_unrecognized_catalog_answers_are_unverified_and_keep_the_last_observation(self):
        # C3-9: config/read and every model/list page are validated structurally. An answer of an unrecognized shape is not an
        # empty catalog (that would be a `local` FAIL on every Codex head and refuse every job under `refuse`): the catalog is
        # UNVERIFIED and the last observation is kept. The first catalog lists a hidden entry of its own, gpt-9-nova; the later
        # reads serve the plain catalog, so a replaced observation would lose it
        kept = "gpt-9-nova"
        good = self.catalog_file("c3-9-good", lambda ms: ms.append(self.new_entry(kept, hidden=True)))
        cases = {"model/list result null": {"STUB_CODEX_MODEL_LIST_RESULT": "null"},
                 "model/list result without data": {"STUB_CODEX_MODEL_LIST_RESULT": json.dumps({"unrelated": True})},
                 "model/list data not a list": {"STUB_CODEX_MODEL_LIST_RESULT": json.dumps({"data": {"id": head("sol")}, "nextCursor": None})},
                 "model/list entries not objects": {"STUB_CODEX_MODEL_LIST_RESULT": json.dumps({"data": [head("sol"), head("luna")],
                                                                                                "nextCursor": None})},
                 "config/read result null": {"STUB_CODEX_CONFIG_RESULT": "null"},
                 "config/read result without config": {"STUB_CODEX_CONFIG_RESULT": json.dumps({"unrelated": True})}}
        for i, (case, bad) in enumerate(cases.items()):
            with self.subTest(answer=case):
                env, _ = self.isolated(f"c3-9-{i}", catalog=good)
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(kept, list(strings(self.evidence(env))))
                bad_env = dict(env, STUB_CODEX_CATALOG=str(CATALOG_CURRENT), **bad)
                rc, data, err = self.doctor_json(bad_env)
                texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
                self.assertEqual([t for t in texts if t.startswith("local:")], [], "an unrecognized answer was read as an empty catalog")
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), texts)
                self.assertIn(kept, list(strings(self.evidence(env))), "the unrecognized answer replaced the last observation")
                # the gate admits the job (UNVERIFIED never refuses); changed for docs/model-routing.md 3.3 (F3): the seat launches only
                # where a readable fresh catalog lists its model, so the unrecognized answer leaves this machine's login unproven
                # and the seat fails, naming the read, instead of running unchecked
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(bad_env, BRIDGE_MODELS_GATE="refuse"))
                self.assertEqual(rc, 1, out + err)
                self.assertIn(f"this machine's own login cannot run {head('luna')}", self.seat(self.job_dir(out), "luna")["activity"])
                self.assertEqual(self.launched_models(env), [], "a seat launched on a login whose catalog could not be read")

    def test_a_future_dated_evidence_file_is_refreshed_and_its_observations_replaced(self):
        # C3-10: the machine's clock moved back seven days since the last check. A sources_refreshed_at in the future (negative
        # age) makes the daily re-check due; observations dated in the future do not block their replacement by later
        # successful reads; sticky markers stay
        week = 7 * 86400
        with self.subTest(clock="sources_refreshed_at a week ahead"):
            env, _ = self.isolated("c3-10")
            rc, out, err = self.doctor(env)
            self.assertEqual(rc, 0, out + err)
            self.shift_timestamps(env, week)
            before = self.reads(env, "c3-10")
            rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
            self.assertEqual(rc, 0, out + err)
            after = self.reads(env, "c3-10")
            self.assertGreater(after[0], before[0], "a sources_refreshed_at seven days ahead suppressed the daily re-check")
            self.assertGreater(after[1], before[1])
            self.assertLessEqual(epoch_of(self.evidence(env)["sources_refreshed_at"]), time.time() + 60)
        with self.subTest(clock="observations read a week ahead"):
            sonnet, sol = head("sonnet"), head("sol")
            legacy = self.deprecations_with(sonnet, "Legacy", "N/A", "Not sooner than June 30, 2027")
            marked = self.catalog_file("c3-10-marked", lambda ms: self.deprecate(ms, sol))
            env, _ = self.isolated("c3-10-replace", catalog=marked, deprecations=legacy)
            rc, out, err = self.doctor(env)
            self.assertTrue([l for l in out.splitlines() if l.lstrip().startswith("stale:") and sonnet in l], out)
            self.assertIn(sol, self.marker_ids(env))
            self.shift_timestamps(env, week)
            rc, out, err = self.doctor(dict(env, STUB_CODEX_CATALOG=str(CATALOG_CURRENT), **self.pages.pages("c3-10-replace-clean")))
            self.assertEqual([l for l in out.splitlines() if l.lstrip().startswith("stale:") and sonnet in l], [],
                             "an observation dated in the future was kept over a later successful read")
            self.assertIn(sol, self.marker_ids(env))  # sticky

    def test_the_catalog_directory_is_created_before_the_first_read(self):
        # C3-11: a fresh installation, or a new nested BRIDGE_JOBS path: the jobs root's parent, where the app-server runs, does
        # not exist yet. It is created (private) before the first source read, without creating a job record, with the evidence
        # file in its default place there and elsewhere
        for where in ("default", "elsewhere"):
            with self.subTest(evidence=where):
                name = f"c3-11-{where}"
                jobs = self.case_dir(name) / "fresh" / "nested" / "jobs"
                env, _ = self.isolated(name, BRIDGE_JOBS=str(jobs))
                if where == "default":
                    env["BRIDGE_MODELS_EVIDENCE"] = None  # <jobs root parent>/models-evidence.json
                rc, data, err = self.doctor_json(env)
                cfg = [r for r in self.check_records(env) if r.get("method") == "config/read"]
                self.assertTrue(cfg, f"the catalog was never read (the app-server could not start in {jobs.parent}): {data.get('findings')}")
                self.assertEqual(os.path.realpath(cfg[0]["cwd"]), os.path.realpath(str(jobs.parent)))
                self.assertEqual((rc, data.get("verdict")), (0, "OK"), data.get("findings"))
                self.assertFalse(jobs.exists() and any(jobs.iterdir()), "doctor created a job record")
                self.assertEqual(oct(jobs.parent.stat().st_mode & 0o077), oct(0), "the catalog directory is not private")

    def test_relays_claude_cli_is_resolved_the_way_relay_resolves_it(self):
        # C3-12: relay.resolve_claude() expands and absolutizes RELAY_CLAUDE_BIN and takes only an executable, for the override
        # and for its fallback candidates alike; doctor's `host` check must look at the same file relay would run
        d = self.case_dir("c3-12")
        with self.subTest(relay="RELAY_CLAUDE_BIN=~/.local/bin/claude"):
            home = d / "home"
            wrapper = home / ".local" / "bin" / "claude"
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{STUB_CLAUDE}' \"$@\"\n")
            wrapper.chmod(0o755)
            env, _ = self.isolated("c3-12", HOME=str(home), RELAY_CLAUDE_BIN="~/.local/bin/claude")
            rc, data, err = self.doctor_json(env)
            self.assertEqual([f.get("text") for f in data.get("findings") or [] if isinstance(f, dict) and f.get("class") == "host"], [])
            self.assertEqual((rc, data.get("verdict")), (0, "OK"), data.get("findings"))
            rc, out, err = self.doctor(env)
            relay = [l for l in out.splitlines() if l.startswith("relay claude cli:")]
            self.assertTrue(relay and (str(wrapper) in relay[0] or os.path.realpath(str(wrapper)) in relay[0]), out)
        with self.subTest(relay="no override, no claude on PATH, a non-executable ~/.local/bin/claude"):
            home2 = d / "home2"
            plain = home2 / ".local" / "bin" / "claude"
            plain.parent.mkdir(parents=True, exist_ok=True)
            plain.write_text("#!/bin/sh\necho 'not executable'\n")
            plain.chmod(0o644)
            pybin = d / "pybin"  # the stubs' `#!/usr/bin/env python3`, without the rest of the developer's PATH
            pybin.mkdir(exist_ok=True)
            if not (pybin / "python3").exists():
                (pybin / "python3").symlink_to(sys.executable)
            env, _ = self.isolated("c3-12-fallback", HOME=str(home2), RELAY_CLAUDE_BIN=None,
                                   PATH=os.pathsep.join([str(pybin), "/usr/bin", "/bin"]), PYTHONPATH=str(HOST_GUARD),
                                   HOST_GUARD_ROOTS=str(d), HOST_GUARD_LOG=str(d / "guard.jsonl"))
            rc, out, err = self.doctor(env)
            self.assertNotIn("Traceback", err)
            self.assertTrue([l for l in out.splitlines() if l.startswith("relay claude cli:")], out)
            named = [l for l in out.splitlines() if str(plain) in l or os.path.realpath(str(plain)) in l]
            self.assertEqual(named, [], "a non-executable fallback candidate was taken for relay's Claude CLI")

    def test_the_gate_notice_is_recorded_once_across_replies(self):
        # C3-13: an ask and two replies under the same failed check: ONE job-level models-check notice, and every output leads
        # with it once
        cat = self.catalog_file("c3-13", lambda ms: ms.append(self.new_entry(NEWER_SOL)))
        env, _ = self.isolated("c3-13", catalog=cat)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "c3-13", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("NOTICE: models: check FAILED ("), out)
        for n in (1, 2):
            rc, out, err = self.run_engine("reply", "c3-13", "--", f"more {n}", env_extra=env)
            self.assertEqual(rc, 0, out + err)
        persisted = [x for x in self.notices(self.jobs / "c3-13") if (x.get("text") or "").startswith("NOTICE: models:")]
        self.assertEqual(len(persisted), 1, persisted)
        self.assertEqual(len(self.models_notices(out)), 1, out)
        rc, out, err = self.run_engine("result", "c3-13", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("NOTICE: models:")]), 1, out)

    def test_each_remote_finding_carries_the_time_of_its_own_observation(self):
        # C3-14: the Codex catalog was read a day ago (it lists a fictional newer Sol and marks Sol deprecated); today only npm could
        # be read. The stale and lifecycle lines stay dated by the catalog observation that supports them, not by today's npm read
        sol = head("sol")
        cat = self.catalog_file("c3-14", lambda ms: (ms.append(self.new_entry(NEWER_SOL)), self.deprecate(ms, sol)))
        env, _ = self.isolated("c3-14", catalog=cat)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 1, out + err)
        day = 86400
        read = epoch_of(self.evidence(env)["sources_refreshed_at"]) - day
        self.shift_timestamps(env, -day)
        only_npm = dict(env, STUB_CODEX_APPSERVER_FAIL="1", **self.pages.pages("c3-14-today", deprecations=503, overview=503))
        rc, out, err = self.doctor(only_npm)
        self.assertEqual(rc, 1, out + err)
        then = datetime.fromtimestamp(read, timezone.utc).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = {"stale": [l for l in out.splitlines() if l.strip().startswith("stale:") and NEWER_SOL in l],
                 "lifecycle": [l for l in out.splitlines() if l.strip().startswith("lifecycle:") and sol in l]}
        for cls, found in lines.items():
            with self.subTest(finding=cls):
                self.assertEqual(len(found), 1, out)
                self.assertIn(then, found[0], "the line is not dated by the observation that supports it")
                self.assertNotIn(today, found[0])


class GroupE_SingleWriter(CheckBase):
    """The S-C3 ruling (review/astra_C3_findings.md: one writer at a time, amendments A1 and A2, the invariants): every write
    of the evidence file runs under its lock, a refresh's source reads included, and a launch never waits for it. The writer
    in the middle of its reads is a `doctor` whose page fetches the page server holds open (start_holder). The replacements of
    the multi-writer tests are in GroupE_TheCheck: the clear test (C3-1) and the serialized writers."""

    def test_a_launch_composes_from_one_snapshot_and_recomputes_in_memory(self):
        # C3-3 (resolved by S-C3 item 4): a launch reads the evidence ONCE, checks that snapshot's table_sha256 against its
        # table, recomputes in memory when they differ and composes from that same snapshot; it never writes. The table here is
        # stale (the retired Sol predecessor as the head: the real catalog reads FAIL). While the launch probes relay's Claude CLI (a local fact),
        # another writer replaces the file with an evaluation that is OK under a different table: composing from that second
        # snapshot, whose digest was never checked, admits the job under `refuse`
        stale = self.table_file("snapshot-stale", self.old_sol_head)
        stale_line = f"stale: newer model may exist: {head('sol')} is listed but not in the model table"
        env, d = self.isolated("snapshot")
        rc, out, err = self.doctor(dict(env, BRIDGE_MODELS_FILE=str(stale)))
        self.assertEqual(rc, 1, out + err)
        evidence = Path(env["BRIDGE_MODELS_EVIDENCE"])
        s1 = evidence.read_bytes()  # evaluated under the stale table: FAIL, its digest
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        s2 = d / "evaluated-ok.json"
        s2.write_bytes(evidence.read_bytes())  # evaluated under the shipped table: OK, another digest
        evidence.write_bytes(s1)
        flag, relay_claude = d / "swapped", d / "relay-claude"
        relay_claude.write_text("\n".join([
            f"#!{sys.executable}",
            "import os, shutil, sys",
            f"FLAG, NEW, EVIDENCE = {str(flag)!r}, {str(s2)!r}, {str(evidence)!r}",
            "if sys.argv[1:] == ['--version'] and not os.path.exists(FLAG):  # another writer publishes, once",
            "    open(FLAG, 'w').close()",
            "    shutil.copyfile(NEW, EVIDENCE + '.swap')",
            "    os.replace(EVIDENCE + '.swap', EVIDENCE)",
            f"os.execv(sys.executable, [sys.executable, {str(STUB_CLAUDE)!r}] + sys.argv[1:])", ""]))
        relay_claude.chmod(0o755)
        with self.subTest(snapshot="replaced while the launch probes its local facts"):
            jobs = self.job_names()
            rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(
                env, BRIDGE_MODELS_FILE=str(stale), BRIDGE_MODELS_GATE="refuse", RELAY_CLAUDE_BIN=str(relay_claude)))
            self.assertTrue(flag.exists(), "the launch never probed relay's Claude CLI, so nothing was swapped")
            self.assertEqual(rc, 2, "admitted under refuse: composed from a snapshot whose digest was never checked\n" + out + err)
            self.assertIn(stale_line, err)
            self.assertEqual(self.new_jobs(jobs), [])
            self.assertEqual(evidence.read_bytes(), s2.read_bytes(), "the launch wrote the evidence file")
        with self.subTest(snapshot="evaluated under another table"):
            # the OK snapshot, no swap: its digest differs from the stale table's, so the launch recomputes in memory from that
            # snapshot's retained observations (FAIL), reads no source and writes nothing
            evidence.write_bytes(s2.read_bytes())
            before = self.reads(env, "snapshot")
            rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q",
                                           env_extra=dict(env, BRIDGE_MODELS_FILE=str(stale), BRIDGE_MODELS_GATE="refuse"))
            self.assertEqual(rc, 2, out + err)
            self.assertIn(stale_line, err)
            self.assertEqual(self.reads(env, "snapshot"), before)
            self.assertEqual(evidence.read_bytes(), s2.read_bytes(), "a launch wrote its recomputation (only doctor and the refresh write)")

    def test_a_failed_later_attempt_keeps_the_retained_success_and_records_the_error(self):
        # C3-4 (resolved by S-C3 item 2): per source the retained observation is the latest SUCCESSFUL read, and the latest
        # attempt is recorded beside it. A failed attempt keeps the success (still used; the source is UNVERIFIED) and records
        # its error; a success always replaces the retained one, in the order the writers ran, even when the clock moved back
        # since the retained read (no "not read" error is made up). Each catalog lists a hidden entry of its own, which shows
        # which observation is retained
        nova, orion = "gpt-9-nova", "gpt-9-terra"  # two fictional hidden entries (the registry's fixture ids)
        c1 = self.catalog_file("attempts-nova", lambda ms: ms.append(self.new_entry(nova, hidden=True)))
        c2 = self.catalog_file("attempts-orion", lambda ms: ms.append(self.new_entry(orion, hidden=True)))
        env, _ = self.isolated("attempts", catalog=c1)

        def retained():
            return set(strings(self.evidence(env))) & {nova, orion}

        def codex_source(out):  # doctor's line(s) about the Codex source; its CLI line (`codex: <path> (codex-cli X)`) aside
            return "\n".join(l for l in out.splitlines() if l.strip().startswith("codex:") and "(codex-cli " not in l)

        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(retained(), {nova})
        failing = dict(env, STUB_CODEX_APPSERVER_FAIL="1")
        rc, out, err = self.doctor(failing)
        self.assertEqual(retained(), {nova}, "a failed attempt replaced or dropped the retained success")
        self.assertRegex(codex_source(out), r"(?i)error|fail", out)
        rc, data, err = self.doctor_json(failing)
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), data.get("findings"))
        rc, out, err = self.doctor(dict(env, STUB_CODEX_CATALOG=str(c2)))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(retained(), {orion})
        self.assertNotRegex(codex_source(out), r"(?i)error|fail", out)
        self.shift_timestamps(env, 3600)  # the clock moved back an hour: the retained read is dated in the future
        rc, out, err = self.doctor(env)
        self.assertEqual(retained(), {nova}, "a successful read was rejected because the retained one is dated later by the clock")
        self.assertNotRegex(codex_source(out), r"(?i)error|fail", out)
        self.assertEqual(rc, 0, out + err)

    def test_a_clear_that_cannot_get_the_lock_in_time_changes_nothing(self):
        # A1: lock acquisition has its own deadline (BRIDGE_MODELS_LOCK_DEADLINE). A clear that cannot get the lock in time
        # exits non-zero with one line "not cleared: evidence busy", changes nothing, reads nothing and never claims success
        sol = head("sol")
        table = self.slow_table("busy-clear", fetch_timeout_s=90, catalog_timeout_s=90)
        env, d = self.isolated("busy-clear", table=table, catalog=self.catalog_file("busy-clear", lambda ms: self.deprecate(ms, sol)))
        rc, out, err = self.doctor(env)
        self.assertIn(sol, self.marker_ids(env), out + err)
        pa = self.start_holder(env, "busy-clear-a")
        b_env = dict(env, BRIDGE_MODELS_LOCK_DEADLINE="1", STUB_CODEX_RECORD=str(d / "b-codex.jsonl"), **self.pages.pages("busy-clear-b"))
        try:
            held = self.evidence_bytes(env)
            rc, out, err = self.run_within(20, b_env, "doctor", "--clear-lifecycle", sol, "--reason", "reviewed")
            after, reads = self.evidence_bytes(env), self.source_reads(b_env, "busy-clear-b")
        finally:
            self.release_holder(pa)
        self.assertNotEqual(rc, 0, out + err)
        self.assertEqual((out + err).count("not cleared: evidence busy"), 1, out + err)
        self.assertEqual([l for l in out.splitlines() if l.startswith("cleared")], [], out)
        self.assertEqual(after, held, "a clear that never got the lock changed the evidence file")
        self.assertEqual(reads, (0, 0))
        self.assertIn(sol, self.marker_ids(env))  # still there once the holder has published: the clear never happened
        self.assertEqual([c for c in cleared_in(self.evidence(env)) if c["id"] == sol], [])

    def test_doctor_that_cannot_get_the_lock_in_time_reports_unverified_and_exits_1(self):
        # A1: doctor waits for the lock up to BRIDGE_MODELS_LOCK_DEADLINE; past it, it reports the current (OK) snapshot as
        # UNVERIFIED, not refreshed, and exits 1, without reading any source or writing the evidence
        table = self.slow_table("busy-doctor", fetch_timeout_s=90, catalog_timeout_s=90)
        env, d = self.isolated("busy-doctor", table=table)
        rc, data, err = self.doctor_json(env)
        self.assertEqual((rc, data.get("verdict")), (0, "OK"), data.get("findings"))
        pa = self.start_holder(env, "busy-doctor-a")
        b_env = dict(env, BRIDGE_MODELS_LOCK_DEADLINE="1", STUB_CODEX_RECORD=str(d / "b-codex.jsonl"), **self.pages.pages("busy-doctor-b"))
        try:
            held = self.evidence_bytes(env)
            rc_j, out_j, err_j = self.run_within(20, b_env, "doctor", "--json")
            rc_t, out_t, err_t = self.run_within(20, b_env, "doctor")
            after, reads = self.evidence_bytes(env), self.source_reads(b_env, "busy-doctor-b")
        finally:
            self.release_holder(pa)
        data = self.json_of(rc_j, out_j, err_j)
        self.assertEqual((rc_j, data.get("verdict")), (1, "UNVERIFIED"), out_j + err_j)
        self.assertEqual(rc_t, 1, out_t + err_t)
        self.assertIn("UNVERIFIED", out_t)
        self.assertEqual(reads, (0, 0))
        self.assertEqual(after, held)

    def test_a_refresh_has_one_total_budget_across_its_sources(self):
        # A1: the lock is held for a bounded time because the refresh has ONE total budget, BRIDGE_MODELS_FETCH_DEADLINE, across
        # all its source reads (and the app-server's cleanup), not a deadline per source that adds up. Here each source would
        # take far longer than the budget and the table allows 30 s per source: the app-server answers model/list after 30 s,
        # and all three pages trickle a byte every 0.25 s for a minute
        slow = self.slow_table("budget", catalog_timeout_s=30, fetch_timeout_s=30)
        env, _ = self.isolated("budget", table=slow, STUB_CODEX_APPSERVER_SLEEP="30", BRIDGE_MODELS_FETCH_DEADLINE="4")
        body = {"npm": json.dumps({"name": "@openai/codex", "dist-tags": {"latest": "0.156.1"}}).ljust(240),
                "deprecations": DEPRECATIONS.read_text()[:240], "overview": OVERVIEW.read_text()[:240]}
        for name, text in body.items():
            self.pages.trickle(f"/budget/{name}", text, interval=0.25)
        try:
            rc, out, err = self.run_within(15, env, "doctor", "--json")
        finally:
            self.kill_app_servers(env)
        data = self.json_of(rc, out, err)
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), out + err)

    def test_an_overdue_launch_that_loses_the_try_lock_composes_the_snapshot_with_one_deferred_notice(self):
        # A2 (contention is never silent): the evidence is overdue and another process holds the lock mid-refresh. A new job does
        # not wait: it composes from the current snapshot plus an in-memory refresh-deferred finding (UNVERIFIED), keeps every
        # FAIL and sticky marker, emits ONE deduplicated notice (a reply on the job adds none), reads no source and leaves the
        # file, its freshness included, as it was. UNVERIFIED never refuses, not even under `refuse`
        table = self.slow_table("deferred", fetch_timeout_s=90, catalog_timeout_s=90)
        with self.subTest(snapshot="OK"):
            env, d = self.isolated("deferred-ok", table=table)
            rc, out, err = self.doctor(env)
            self.assertEqual(rc, 0, out + err)
            self.overdue(env)
            pa = self.start_holder(env, "deferred-ok-a")
            b_env = dict(env, STUB_CODEX_RECORD=str(d / "b-codex.jsonl"), **self.pages.pages("deferred-ok-b"))
            try:
                held = self.evidence_bytes(env)
                rc, out, err = self.run_within(20, dict(b_env, BRIDGE_MODELS_GATE="refuse"), "ask", "--seats", "luna:low", "--job-dir", "deferred-ok",
                                               "--", "q")
                rc2, out2, err2 = self.run_within(20, b_env, "reply", "deferred-ok", "--", "more")
                after, reads = self.evidence_bytes(env), self.source_reads(b_env, "deferred-ok-b")
            finally:
                self.release_holder(pa)
            self.assertEqual(rc, 0, out + err)
            notices = self.models_notices(out)
            self.assertEqual(len(notices), 1, out)
            self.assertTrue(notices[0].startswith("NOTICE: models: unverified (") and re.search(r"(?i)deferred", notices[0]), notices)
            self.assertEqual(self.meta(self.jobs / "deferred-ok")["check"]["verdict"], "UNVERIFIED")
            self.assertEqual(rc2, 0, out2 + err2)
            self.assertEqual(len(self.models_notices(out2)), 1, out2)
            persisted = [n for n in self.notices(self.jobs / "deferred-ok") if (n.get("text") or "").startswith("NOTICE: models:")]
            self.assertEqual(len(persisted), 1, persisted)
            self.assertEqual(reads, (0, 0), "a launch that lost the try-lock read the sources itself")
            self.assertEqual(after, held, "a launch that lost the try-lock wrote the evidence file")
        with self.subTest(snapshot="FAIL"):
            sol = head("sol")
            failing = self.catalog_file("deferred-fail", lambda ms: (ms.append(self.new_entry(NEWER_SOL)), self.deprecate(ms, sol)))
            env, d = self.isolated("deferred-fail", table=table, catalog=failing)
            rc, out, err = self.doctor(env)
            self.assertEqual(rc, 1, out + err)
            self.assertIn(sol, self.marker_ids(env))
            self.overdue(env)
            pa = self.start_holder(env, "deferred-fail-a")
            # the launches' own sources are clean: had one refreshed, the newer-Sol line would be gone
            b_env = dict(env, STUB_CODEX_CATALOG=str(CATALOG_CURRENT), STUB_CODEX_RECORD=str(d / "b-codex.jsonl"),
                         **self.pages.pages("deferred-fail-b"))
            try:
                held = self.evidence_bytes(env)
                rc, out, err = self.run_within(20, b_env, "ask", "--seats", "luna:low", "--", "q")
                rc_s, out_s, err_s = self.run_within(20, b_env, "ask", "--seats", "sol:low", "--", "q")
                rc_r, out_r, err_r = self.run_within(20, dict(b_env, BRIDGE_MODELS_GATE="refuse"), "ask", "--seats", "luna:low", "--", "q")
                after, reads = self.evidence_bytes(env), self.source_reads(b_env, "deferred-fail-b")
            finally:
                self.release_holder(pa)
            self.assertEqual(rc, 0, out + err)
            notices = self.models_notices(out)
            self.assertEqual(len(notices), 1, out)
            self.assertTrue(notices[0].startswith("NOTICE: models: check FAILED (") and NEWER_SOL in notices[0], notices)
            self.assertIn(sol, self.assert_refused(rc_s, out_s, err_s))  # the sticky marker is kept
            self.assertEqual(rc_r, 2, out_r + err_r)  # the FAIL is kept, and refuses under `refuse`
            self.assertIn(NEWER_SOL, err_r)
            self.assertEqual(reads, (0, 0), "a launch that lost the try-lock read the sources itself")
            self.assertEqual(after, held, "a launch that lost the try-lock wrote the evidence file")

    def test_job_control_replies_and_continuations_never_wait_for_a_refresh(self):
        # S-C3 invariant (review/astra_SC3_full.md, "Running jobs"): while a refresh holds the evidence lock, status and result
        # answer, a reply proceeds notice-only, and a seat's continuation attempt (the retry after a usage limit) re-reads the
        # markers, none of them waiting for the lock. The lock deadline stays at its default, so any of them waiting would overrun
        table = self.slow_table("no-wait", fetch_timeout_s=90, catalog_timeout_s=90)
        env, d = self.isolated("no-wait", table=table)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "no-wait", "--", "[[STUB:text=FIRST]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.overdue(env)  # every model-starting invocation now wants to refresh
        pa = self.start_holder(env, "no-wait-a")
        b_env = dict(env, STUB_CODEX_RECORD=str(d / "b-codex.jsonl"), **self.pages.pages("no-wait-b"))
        try:
            for argv in (["status", "no-wait"], ["result", "no-wait"], ["reply", "no-wait", "--", "more"],
                         ["ask", "--seats", "luna:low", "--", f"[[STUB:limit={d / 'limit'}]] q"]):
                rc, out, err = self.run_within(20, b_env, *argv)
                self.assertEqual(rc, 0, f"{argv}: {out}{err}")
            self.assertTrue((d / "limit").exists(), "the seat never hit its usage limit, so no continuation attempt ran")
            self.assertIsNone(pa.poll(), "the refresh ended before the checks ran; they prove nothing")
        finally:
            self.release_holder(pa)

    def test_process_death_releases_the_lock_and_keeps_the_old_snapshot(self):
        # S-C3 invariant: a writer killed in the middle of its catalog read (its app-server helper still alive in its own
        # session) releases the lock at once, because the helper never inherited the lock's descriptor; the file keeps the
        # previous snapshot (atomic replace), and the stable lock file is never unlinked
        table = self.slow_table("crash", fetch_timeout_s=90, catalog_timeout_s=90)
        env, d = self.isolated("crash", table=table)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        folder = Path(env["BRIDGE_MODELS_EVIDENCE"]).parent
        locks = {p.name: p.stat().st_ino for p in folder.iterdir() if "lock" in p.name}
        self.assertTrue(locks, "no lock file beside the evidence file")
        before = self.evidence_bytes(env)
        a_env = dict(env, STUB_CODEX_APPSERVER_SLEEP="60", STUB_CODEX_RECORD=str(d / "a-codex.jsonl"), **self.HOLD)
        pa = self.start_engine(a_env, "doctor")
        try:
            for _ in range(300):
                if [r for r in self.codex_records(a_env, "app-server") if r.get("method") == "model/list"]:
                    break
                time.sleep(0.1)
            else:
                self.fail("writer A never asked its app-server for model/list")
            os.kill(pa.pid, signal.SIGKILL)
            pa.communicate(timeout=30)
            self.assertEqual(self.evidence_bytes(env), before, "a writer killed mid-refresh left a changed evidence file")
            b_env = dict(env, BRIDGE_MODELS_LOCK_DEADLINE="5", STUB_CODEX_RECORD=str(d / "b-codex.jsonl"), **self.pages.pages("crash-b"))
            rc, out, err = self.run_within(60, b_env, "doctor", "--json")
        finally:
            self.kill_app_servers(a_env)
        data = self.json_of(rc, out, err)
        self.assertEqual((rc, data.get("verdict")), (0, "OK"), "the lock outlived its dead holder:\n" + out + err)
        self.assertEqual({p.name: p.stat().st_ino for p in folder.iterdir() if p.name in locks}, locks, "the lock file was unlinked or replaced")

    def test_the_evidence_file_and_doctor_carry_no_clear_seq_or_read_seq(self):
        # S-C3 item 3, TEST_RULINGS R-E3 (revised): one writer at a time needs no clear sequence. The file keeps a `cleared`
        # history of {id, at, reason, marker} entries and no clear_seq, read_seq or seq anywhere; doctor prints none of them
        sol = head("sol")
        env, _ = self.isolated("no-seq", catalog=self.catalog_file("no-seq", lambda ms: self.deprecate(ms, sol)))
        clean = dict(env, STUB_CODEX_CATALOG=str(CATALOG_CURRENT))
        outputs = []
        for argv, run_env in ((["doctor"], env), (["doctor", "--clear-lifecycle", sol, "--reason", "reviewed"], clean), (["doctor", "--json"], clean)):
            rc, out, err = self.run_engine(*argv, env_extra=run_env)
            self.assertNotIn("Traceback", err)
            outputs.append(out)
        ev = self.evidence(env)
        entries = [c for c in ev.get("cleared") or [] if isinstance(c, dict) and c.get("id") == sol]
        self.assertEqual(len(entries), 1, ev.get("cleared"))
        self.assertTrue({"id", "at", "reason", "marker"} <= set(entries[0]), entries[0])
        self.assertEqual(sorted({"clear_seq", "read_seq", "seq"} & keys_in(ev)), [], "the evidence file still carries a clear sequence")
        for out in outputs:
            self.assertNotIn("clear_seq", out)
            self.assertNotIn("read_seq", out)


# ==================================================================================================================
# Astra's re-review of the S-C3 fix round 1e845a10 (review/astra_C3b_findings.md; D24: failing tests first)
# ==================================================================================================================
class GroupE_C3bFindings(CheckBase):
    """Regression tests for C3b-1 .. C3b-5, accepted as Astra wrote the fixes, each named in a comment. Every timing is an upper
    bound (the budget plus a margin for a loaded machine), never an exact time; the slow parts are the page server's paced
    responses and the stubs' own sleeps."""

    BUDGET = 4  # BRIDGE_MODELS_FETCH_DEADLINE in these tests: the refresh's one total budget (A1)
    MARGIN = 3  # seconds on top of the budget for the work around its deadline on a loaded machine

    def still_running(self, env):
        """The pids of this check's stub codex processes (probes and app-servers) still running. A killed process its parent has
        not reaped yet shows as <defunct>, not as stub_codex.py."""
        pids = {str(r["pid"]) for r in self.codex_records(env) if r.get("pid")}
        return sorted(l.split(None, 1)[0] for l in self._procs_matching("stub_codex.py") if l.split(None, 1)[0] in pids)

    def kill_stub_codex(self, env):
        for pid in self.still_running(env):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass

    def test_a_page_fetch_ends_at_the_deadline_while_its_headers_or_chunk_framing_trickle(self):
        # C3b-1: the deadline bounds the whole HTTP operation (connection, headers, chunk framing), not only the moments between
        # reads of the body: urlopen() reading headers, or read1() reading the size line of a chunk, blocks for as long as bytes
        # keep arriving within the socket timeout. The npm page sends its last header, or the size line of its one chunk, one
        # byte every 0.05 s for about 20 s. No codex resolves, so the page is the refresh's first read and has the whole budget.
        # The fetch is abandoned at the deadline (its socket closed, so the blocked read ends) and the source is UNVERIFIED: the
        # refresh is published, and doctor exits, within the budget plus margins of the npm request's arrival
        slow = self.slow_table("c3b-1", catalog_timeout_s=30, fetch_timeout_s=30)  # only the refresh's budget binds
        npm = json.dumps({"name": "@openai/codex", "dist-tags": {"latest": "0.156.1"}})
        for mode, serve in (("headers", self.pages.slow_headers), ("chunk framing", self.pages.slow_chunks)):
            with self.subTest(trickling=mode):
                name = "c3b-1-" + mode.replace(" ", "-")
                env, _ = self.isolated(name, table=slow, CODEX_BIN=str(self.tmp / "no-codex"), BRIDGE_MODELS_FETCH_DEADLINE=str(self.BUDGET))
                serve(f"/{name}/npm", npm, interval=0.05, seconds=20)
                rc, out, err = self.run_within(90, env, "doctor", "--json")
                ended = time.time()
                data = self.json_of(rc, out, err)
                asked = self.pages.requested_at(f"/{name}/npm")
                self.assertEqual(len(asked), 1, f"the npm page was requested {len(asked)} times")
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), out + err)
                self.assertTrue(self.unverified_about(data, "npm"), data.get("findings"))
                published = epoch_of(self.evidence(env)["sources_refreshed_at"]) - asked[0]
                self.assertLessEqual(published, self.BUDGET + self.MARGIN,
                                     f"the refresh was published {published:.1f} s after the npm request arrived, under a {self.BUDGET} s "
                                     f"budget: the fetch went on while its {mode} trickled")
                self.assertLessEqual(ended - asked[0], self.BUDGET + 2 * self.MARGIN,
                                     "doctor kept running after its refresh: the abandoned read never ended")

    def test_discovery_and_the_catalog_helpers_cleanup_stay_within_the_refresh_budget(self):
        # C3b-2: A1's one total budget also covers executable discovery and the catalog helper's cleanup. Each version or
        # capability probe, and the wait for the helper to end, gets the smaller of its own cap and what is left of the budget;
        # a helper still alive when the budget runs out is killed. Under the budget: (1) the `--version` probe or (2) the
        # `exec --help` capability probe answers only after 30 s (a probe's own cap is 10 s); (3) the app-server, asleep before
        # its model/list answer, ignores SIGTERM (the cleanup's own cap is 5 s). From the first stub record (the refresh's first
        # probe) to its publication (sources_refreshed_at) the refresh stays within the budget plus a margin; it ends
        # UNVERIFIED, and no stub process is left running
        slow = self.slow_table("c3b-2", catalog_timeout_s=30, fetch_timeout_s=30)  # only the refresh's budget binds
        cases = {"the version probe": {"STUB_CODEX_PROBE_SLEEP": "version:30"},
                 "the capability probe": {"STUB_CODEX_PROBE_SLEEP": "help:30"},
                 "a catalog helper that ignores SIGTERM": {"STUB_CODEX_APPSERVER_SLEEP": "30", "STUB_CODEX_APPSERVER_IGNORE_TERM": "1"}}
        for i, (case, extra) in enumerate(cases.items()):
            with self.subTest(slow=case):
                env, _ = self.isolated(f"c3b-2-{i}", table=slow, BRIDGE_MODELS_FETCH_DEADLINE=str(self.BUDGET), **extra)
                try:
                    rc, out, err = self.run_within(90, env, "doctor", "--json")
                    left = self.still_running(env)
                finally:
                    self.kill_stub_codex(env)
                data = self.json_of(rc, out, err)
                records = self.codex_records(env)
                self.assertTrue(records, "the refresh started no probe")
                if "STUB_CODEX_APPSERVER_SLEEP" in extra:
                    self.assertTrue([r for r in records if r.get("method") == "model/list"], "the helper was never asked for model/list")
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), out + err)
                took = epoch_of(self.evidence(env)["sources_refreshed_at"]) - min(r["t"] for r in records)
                self.assertLessEqual(took, self.BUDGET + self.MARGIN,
                                     f"the refresh took {took:.1f} s from its first probe to its publication, under a {self.BUDGET} s budget")
                self.assertEqual(left, [], "a stub process the refresh started was left running")

    def test_malformed_catalog_entries_are_unverified_and_keep_the_last_observation(self):
        # C3b-3: C3-9 checks the shape of each answer, but any object passes as a model entry. An entry needs a recognized string
        # model ID, and every field the check consumes needs its type. `data: [{}]` (an entry without an ID: read as the catalog,
        # it makes a false `local` FAIL of every Codex head, which refuses every new job under `refuse`) and a head whose
        # supportedReasoningEfforts is 17 (a TypeError in retain_catalog(), outside the reader's handling) are allowlisted
        # source errors: the catalog is UNVERIFIED and the last observation is kept. The first catalog lists a hidden entry of
        # its own, gpt-9-nova; a replaced observation would lose it
        kept = "gpt-9-nova"
        good = self.catalog_file("c3b-3-good", lambda ms: ms.append(self.new_entry(kept, hidden=True)))
        seventeen = self.catalog_file("c3b-3-efforts-17", lambda ms: self.entry(ms, head("sol")).__setitem__("supportedReasoningEfforts", 17))
        cases = {"data: [{}]": {"STUB_CODEX_MODEL_LIST_RESULT": json.dumps({"data": [{}], "nextCursor": None})},
                 "supportedReasoningEfforts: 17": {"STUB_CODEX_CATALOG": str(seventeen)}}
        for i, (case, bad) in enumerate(cases.items()):
            with self.subTest(answer=case):
                env, _ = self.isolated(f"c3b-3-{i}", catalog=good)
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(kept, list(strings(self.evidence(env))))
                bad_env = dict(env, **dict({"STUB_CODEX_CATALOG": str(CATALOG_CURRENT)}, **bad))
                rc, out, err = self.run_engine("doctor", "--json", env_extra=bad_env)
                self.assertNotIn("Traceback", err, "a malformed entry raised outside the catalog reader's handling")
                data = self.json_of(rc, out, err)
                texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
                self.assertEqual([t for t in texts if t.startswith("local:")], [], "a malformed answer was read as the catalog")
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), texts)
                self.assertTrue(self.unverified_about(data, "codex"), texts)
                self.assertIn(kept, list(strings(self.evidence(env))), "the malformed answer replaced the last observation")
                # the gate admits the job; changed for docs/model-routing.md 3.3 (F3): the routing reads the catalog itself and launches
                # only where it lists the seat's model: an answer of entries without ids lists nothing (the Luna seat fails, naming the
                # read); a malformed Sol entry beside a sound Luna entry leaves Luna's seat untouched
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(bad_env, BRIDGE_MODELS_GATE="refuse"))
                self.assertEqual(rc, 1 if i == 0 else 0, out + err)
                if i == 0:
                    self.assertIn(f"this machine's own login cannot run {head('luna')}", self.seat(self.job_dir(out), "luna")["activity"])

    def test_a_marker_only_snapshot_is_recomputed_under_a_changed_table_when_the_try_lock_is_lost(self):
        # C3b-4: a legacy snapshot keeps sticky markers but no source observations (a layout clear_lifecycle() also keeps). Its
        # K1 marker was evaluated while the K1 seat was off: an informational finding. The table then switches K1 on, and a new
        # launch's overdue re-check loses the try-lock to a refresh in the middle of its reads, so the launch composes from that
        # snapshot. Its digest is not the table's, so its findings are recomputed from its stored markers (stored_findings()):
        # the K1 marker, now on a relevant target, is a lifecycle FAIL that refuses a new job on another seat under `refuse`.
        # The cached informational finding (UNVERIFIED with the deferred notice) must not admit it. Changed for ruling K1-ON
        # (review/ruling_k1_on.md): the shipped table switches the seat on, so both tables are variants that set the switch
        k1 = TABLE["k1_seat"]["id"]
        off = self.table_file("c3b-4-off", lambda t: (t["policy"].update(fetch_timeout_s=90, catalog_timeout_s=90),
                                                      t["k1_seat"].__setitem__("enabled", False)))
        on = self.table_file("c3b-4-on", lambda t: (t["policy"].update(fetch_timeout_s=90, catalog_timeout_s=90),
                                                    t["k1_seat"].__setitem__("enabled", True)))
        env, _ = self.isolated("c3b-4", table=off, deprecations=self.deprecations_with(k1, "Deprecated", "September 20, 2026", "March 20, 2027"))
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        p = Path(env["BRIDGE_MODELS_EVIDENCE"])
        legacy = {k: v for k, v in json.loads(p.read_text()).items() if k not in ("sources", "sources_refreshed_at")}
        self.assertIn(k1, {m["id"] for m in markers_in(legacy)})
        self.assertIn(f"marker on {k1} (K1 seat off): informational", [f.get("text") for f in legacy.get("findings") or []])
        p.write_text(json.dumps(legacy, indent=1))  # markers and the findings evaluated with K1 off; no observation, no refresh clock
        pa = self.start_holder(env, "c3b-4-a")
        try:
            jobs = self.job_names()
            rc, out, err = self.run_within(20, dict(env, BRIDGE_MODELS_FILE=str(on), BRIDGE_MODELS_GATE="refuse"), "ask", "--seats", "luna:low",
                                           "--", "q")
            created = self.new_jobs(jobs)
            self.assertIsNone(pa.poll(), "the refresh ended before the launch ran; it proves nothing")
        finally:
            self.release_holder(pa)
        self.assertEqual(rc, 2, "a new job was admitted under refuse although the K1 marker is on a relevant target now\n" + out + err)
        self.assertTrue([l for l in err.splitlines() if l.strip().startswith("lifecycle:") and k1 in l], err)
        self.assertEqual(created, [])

    def test_the_catalog_applicability_finding_carries_the_catalog_observations_read_time(self):
        # C3b-5: with a catalog configuration key at a non-default value (the model_catalog_json fixture), "local catalog not
        # applicable: ... is non-default" is established by the catalog observation's successful config/read. The finding
        # carries source 'codex' and inherits that observation's read time; doctor never reports it as "[read never]"
        env, _ = self.isolated("c3b-5", config=CONFIG_CATALOG_JSON)
        rc, data, err = self.doctor_json(env)
        found = [f for f in data.get("findings") or [] if isinstance(f, dict) and "local catalog not applicable:" in (f.get("text") or "")]
        self.assertEqual(len(found), 1, data.get("findings"))
        self.assertIn("model_catalog_json is non-default", found[0]["text"])
        read = ((data.get("sources") or {}).get("codex") or {}).get("read_at")
        self.assertIsNotNone(read, "doctor --json reports no read time for the catalog observation")
        self.assertIsNotNone(found[0].get("read_at"), f"the applicability finding carries no read time, although its config/read was read at {read}")
        self.assertEqual(epoch_of(found[0]["read_at"]), epoch_of(read), "the finding is not dated by the catalog observation that established it")
        self.assertEqual(found[0].get("source"), "codex", found[0])
        rc, out, err = self.doctor(env)
        lines = [l for l in out.splitlines() if "local catalog not applicable: model_catalog_json is non-default" in l]
        self.assertEqual(len(lines), 1, out)
        self.assertNotIn("[read never]", lines[0])


# ==================================================================================================================
# The final fix round (review/rulings_final_round.md, binding): Astra's C4b findings (review/astra_C4b_findings.md; C4b-1 and
# C4b-2 are resolved by S-FETCH) and the live-check findings L1-L4. The pure pieces: test_bridge_models.py
# GroupE_LiveCheckPure; bootstrap's upgrade hints: tests/test_bootstrap.sh groupD_upgrade_hint_*
# ==================================================================================================================
class patched_environ:
    """This process's environment with `changes` applied (a value of None removes the name) inside a with-block."""

    def __init__(self, **changes):
        self.changes, self.saved = changes, None

    def __enter__(self):
        self.saved = dict(os.environ)
        for name, value in self.changes.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)


UPGRADE_HINTS = {"npm": "npm install -g @openai/codex@latest", "brew": "brew upgrade codex", "app": "update the ChatGPT app",
                 "curl": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"}  # L4: the four installers' upgrade commands


def hints_in(text):
    """The installers (UPGRADE_HINTS keys) whose upgrade hint a text names: the app's in any case, the commands verbatim."""
    return {k for k, hint in UPGRADE_HINTS.items() if (hint.lower() in text.lower() if k == "app" else hint in text)}


class GroupE_C4bFindings(CheckBase):
    """Astra's four findings on 43496272 as the final round resolves them, each named in a comment. C4b-1 and C4b-2 by S-FETCH:
    each page fetch runs in a CHILD process, subprocess.run([sys.executable, "-c", FETCH, url, str(max_bytes)], timeout=<what is
    left of the refresh's budget>), which kills it at the deadline, so nothing survives the call; the child reads with plain
    urllib.request.urlopen and the default SSL context (certificate and hostname verification on), at most max_bytes (default
    2 MiB), and reports an error as ONE allowlisted code (http-404, timeout, tls, dns, too-large, error). C4b-3 and C4b-4 as
    Astra wrote the fixes. The in-process tests call bridge_models.fetch_page(url, timeout), which returns (text, None) or
    (None, error)."""

    BUDGET = 4  # BRIDGE_MODELS_FETCH_DEADLINE where a test sets one: the refresh's one total budget (A1)
    MARGIN = 3  # seconds on top of a deadline for the work around it on a loaded machine
    NPM = json.dumps({"name": "@openai/codex", "dist-tags": {"latest": "0.156.1"}})

    @staticmethod
    def threads_running(path):
        """The idents of this process's other threads whose current stack runs code from the file `path`."""
        path = os.path.realpath(str(path))
        found = []
        for ident, frame in sys._current_frames().items():
            if ident == threading.get_ident():
                continue
            while frame is not None:
                if os.path.realpath(frame.f_code.co_filename) == path:
                    found.append(ident)
                    break
                frame = frame.f_back
        return found

    @staticmethod
    def children():
        """{pid: `ps` line} of this test process's live children (a zombie, and `ps` itself, aside)."""
        out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,stat=,command="], capture_output=True, text=True, timeout=10).stdout
        found = {}
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) == os.getpid() \
                    and not parts[2].startswith("Z") and not parts[3].startswith("ps "):
                found[int(parts[0])] = line.strip()
        return found

    @staticmethod
    def naming(needle):
        """The `ps` lines that name `needle` (a URL a fetch child gets as an argument)."""
        out = subprocess.run(["ps", "-axww", "-o", "pid=,stat=,command="], capture_output=True, text=True, timeout=10).stdout
        return [line.strip() for line in out.splitlines() if needle in line]

    def leftovers(self, needle, before, grace=1.0):
        """(threads running bridge_models.py, processes: children of this process that `before` did not have, and processes
        naming `needle`) still there `grace` seconds after a fetch returned; both empty as soon as they are."""
        end = time.monotonic() + grace
        while True:
            threads = self.threads_running(SCRIPTS / "bridge_models.py")
            processes = [line for pid, line in self.children().items() if pid not in before] + self.naming(needle)
            if (not threads and not processes) or time.monotonic() >= end:
                return threads, processes
            time.sleep(0.1)

    def test_a_timed_out_fetch_returns_at_its_deadline_and_leaves_nothing_running(self):
        # C4b-1 -> S-FETCH: C3b-1 stayed open. On Python 3.9 urllib clears conn.sock once the headers are read, so the
        # withdrawn cleanup could not reach a response handed the socket (`Connection: close`): its daemon worker read on
        # after the deadline; and closing a persistent response blocked on the worker's read, past the deadline (holding the
        # evidence lock). The page server sends its last header, or the size line of its one chunk, a byte every 0.05 s for
        # about 12 s: with `Connection: close`, and as a persistent response. fetch_page(url, 2) returns within its timeout
        # plus a margin with an error, and nothing it started survives the call: within a second no thread of this process
        # runs bridge_models.py, and no new child of this process, nor any process naming the page, is alive (S-FETCH: the
        # child is killed at the deadline). The existing doctor-level cases: GroupE_C3bFindings
        bm = import_bridge_models()
        timeout = 2.0
        modes = {"headers": lambda p: self.pages.slow_headers(p, self.NPM, interval=0.05, seconds=12),
                 "chunk framing, Connection: close": lambda p: self.pages.slow_chunks(p, self.NPM, interval=0.05, seconds=12),
                 "chunk framing, a persistent response": lambda p: self.pages.slow_chunks(p, self.NPM, interval=0.05, seconds=12,
                                                                                         keep_alive=True)}
        for i, (mode, serve) in enumerate(modes.items()):
            with self.subTest(trickling=mode):
                path = f"/c4b-1-{i}/npm"
                serve(path)
                url = self.pages.url(path)
                before = set(self.children())
                started = time.monotonic()
                text, error = bm.fetch_page(url, timeout)
                took = time.monotonic() - started
                self.assertLessEqual(took, timeout + self.MARGIN,
                                     f"fetch_page returned {took:.1f} s after it started, under a {timeout:g} s timeout ({mode})")
                self.assertIsNone(text)
                self.assertTrue(error, "an expired fetch returned no error")
                threads, processes = self.leftovers(url, before)
                self.assertEqual(threads, [], f"a thread the fetch started still runs bridge_models.py after it returned ({mode})")
                self.assertEqual(processes, [], f"a process the fetch started is still alive after it returned ({mode})")
        # through doctor, the persistent response: the refresh is published within its budget, and nothing names the page after
        slow = self.slow_table("c4b-1", catalog_timeout_s=30, fetch_timeout_s=30)  # only the refresh's budget binds
        name = "c4b-1-doctor"
        env, _ = self.isolated(name, table=slow, CODEX_BIN=str(self.tmp / "no-codex"), BRIDGE_MODELS_FETCH_DEADLINE=str(self.BUDGET))
        self.pages.slow_chunks(f"/{name}/npm", self.NPM, interval=0.05, seconds=20, keep_alive=True)
        rc, out, err = self.run_within(90, env, "doctor", "--json")
        data = self.json_of(rc, out, err)
        asked = self.pages.requested_at(f"/{name}/npm")
        self.assertEqual(len(asked), 1, f"the npm page was requested {len(asked)} times")
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), out + err)
        published = epoch_of(self.evidence(env)["sources_refreshed_at"]) - asked[0]
        self.assertLessEqual(published, self.BUDGET + self.MARGIN,
                             f"the refresh was published {published:.1f} s after the npm request arrived, under a {self.BUDGET} s budget")
        self.assertEqual(self.naming(self.pages.url(f"/{name}/npm")), [], "a fetch child outlived doctor")

    @staticmethod
    def openssl(d, *args):
        r = subprocess.run([shutil.which("openssl"), *args], cwd=str(d), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"openssl {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[-400:]}")

    def certificate_authority(self, d, name):
        """A throwaway certificate authority made with the openssl CLI: (certificate, key)."""
        (d / f"{name}.cnf").write_text("[req]\ndistinguished_name = dn\n[dn]\n[v3_ca]\nbasicConstraints = critical, CA:TRUE\n"
                                       "keyUsage = critical, keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\n")
        self.openssl(d, "req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-sha256", "-days", "2", "-subj",
                     f"/CN=claude-codex-bridge test {name}", "-config", f"{name}.cnf", "-extensions", "v3_ca", "-keyout", f"{name}.key",
                     "-out", f"{name}.pem")
        return d / f"{name}.pem", d / f"{name}.key"

    def server_certificate(self, d, ca, ca_key):
        """A certificate for 127.0.0.1 (and localhost) that `ca` signs: (certificate, key)."""
        (d / "server-req.cnf").write_text("[req]\ndistinguished_name = dn\n[dn]\n")
        (d / "server-ext.cnf").write_text("[v3_server]\nbasicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature, "
                                          "keyEncipherment\nextendedKeyUsage = serverAuth\nsubjectAltName = IP:127.0.0.1, DNS:localhost\n")
        self.openssl(d, "req", "-new", "-nodes", "-newkey", "rsa:2048", "-sha256", "-subj", "/CN=127.0.0.1", "-config", "server-req.cnf",
                     "-keyout", "server.key", "-out", "server.csr")
        self.openssl(d, "x509", "-req", "-in", "server.csr", "-CA", str(ca), "-CAkey", str(ca_key), "-set_serial", "2", "-days", "2",
                     "-sha256", "-extfile", "server-ext.cnf", "-extensions", "v3_server", "-out", "server.pem")
        return d / "server.pem", d / "server.key"

    def test_https_pages_are_verified_and_an_untrusted_certificate_is_the_tls_code(self):
        # C4b-2 -> S-FETCH: all three production pages are HTTPS, and the HTTP-only fixtures hid that the withdrawn handler
        # override raised AttributeError on Python 3.12 before every HTTPS request. A local TLS server (key and certificates
        # made here with the openssl CLI) serves the npm page for 127.0.0.1: with its CA in SSL_CERT_FILE the page is read;
        # with a CA that did not sign its certificate in SSL_CERT_FILE, or with the system's trust store, the fetch fails with
        # the allowlisted code `tls` and carries no text of the exception. In this process and through doctor (the child
        # inherits SSL_CERT_FILE; the failed attempt is recorded as `tls`). Note: a LibreSSL build of Python (macOS's
        # /usr/bin/python3 3.9, LibreSSL 2.8.3) does not read SSL_CERT_FILE for its default context; there the variable is
        # honoured only when the fetch hands it to ssl.create_default_context itself
        if not shutil.which("openssl"):
            self.skipTest("the openssl CLI is absent: the test CA and server certificate cannot be generated")
        d = Path(os.path.realpath(str(self.case_dir("c4b-2"))))
        ca, ca_key = self.certificate_authority(d, "trusted-ca")
        other, _ = self.certificate_authority(d, "other-ca")
        certificate, key = self.server_certificate(d, ca, ca_key)
        server = PageServer(tls=(str(certificate), str(key)))
        self.addCleanup(server.close)
        server.set("/c4b-2/npm", self.NPM)
        url = server.url("/c4b-2/npm")
        self.assertTrue(url.startswith("https://127.0.0.1:"), url)
        bm = import_bridge_models()
        trust = {"its CA in SSL_CERT_FILE": (str(ca), True), "another CA in SSL_CERT_FILE": (str(other), False),
                 "the system's trust store": (None, False)}
        for case, (cafile, trusted) in trust.items():
            with self.subTest(trust=case):
                with patched_environ(SSL_CERT_FILE=cafile, SSL_CERT_DIR=None):
                    text, error = bm.fetch_page(url, 20)
                if trusted:
                    self.assertIsNone(error, f"an HTTPS page whose CA is in SSL_CERT_FILE was not read: {error}")
                    self.assertEqual(json.loads(text)["dist-tags"]["latest"], "0.156.1")
                else:
                    self.assertIsNone(text)
                    self.assertRegex(error or "", r"(?<![\w-])tls(?![\w-])", "an untrusted certificate is not reported as `tls`")
                    for raw in ("SSL", "CERTIFICATE_VERIFY_FAILED", "certificate verify failed", "urlopen error", "[Errno"):
                        self.assertNotIn(raw, error, "the error carries the exception's text, not an allowlisted code")
        for case, (cafile, trusted) in list(trust.items())[:2]:
            with self.subTest(trust=case, through="doctor"):
                name = "c4b-2-doctor-" + ("trusted" if trusted else "other")
                env, _ = self.isolated(name, BRIDGE_MODELS_NPM_URL=url, SSL_CERT_FILE=cafile, SSL_CERT_DIR=None)
                rc, data, err = self.doctor_json(env)
                attempt = ((self.evidence(env).get("sources") or {}).get("npm") or {}).get("attempt") or {}
                if trusted:
                    self.assertTrue(attempt.get("ok"), attempt)
                    self.assertEqual(self.unverified_about(data, "npm"), [], data.get("findings"))
                else:
                    self.assertFalse(attempt.get("ok"), attempt)
                    self.assertRegex(str(attempt.get("error")), r"(?<![\w-])tls(?![\w-])", attempt)
                    self.assertNotIn("CERTIFICATE_VERIFY_FAILED", json.dumps(self.evidence(env)))

    def test_fetch_errors_are_allowlisted_codes_and_a_page_is_read_up_to_its_size_limit(self):
        # S-FETCH (C4b-1, C4b-2): the child reports an error as ONE allowlisted code and never the text of an exception (an HTTP
        # 404 is `http-404`), and reads at most max_bytes, 2 MiB by default: a 3 MiB page is `too-large`, a 1 MiB page is read
        bm = import_bridge_models()
        self.pages.set("/c4b-codes/one-mib", "x" * (1 << 20), ctype="text/markdown")
        self.pages.set("/c4b-codes/three-mib", "x" * (3 << 20), ctype="text/markdown")
        text, error = bm.fetch_page(self.pages.url("/c4b-codes/one-mib"), 20)
        self.assertIsNone(error)
        self.assertEqual(len(text or ""), 1 << 20)
        for code, path in (("http-404", "/c4b-codes/no-such-page"), ("too-large", "/c4b-codes/three-mib")):
            with self.subTest(code=code):
                text, error = bm.fetch_page(self.pages.url(path), 20)
                self.assertIsNone(text, f"the page was read instead of failing as `{code}`")
                self.assertRegex(error or "", rf"(?<![\w-]){re.escape(code)}(?![\w-])")
                for raw in ("HTTP Error", "Not Found", "urlopen error", "Traceback", "[Errno"):
                    self.assertNotIn(raw, error, "the error carries the exception's text, not an allowlisted code")

    def test_the_fetch_code_uses_no_private_urllib_http_client_or_ssl_attribute(self):
        # C4b-2 -> S-FETCH: Python 3.12's HTTPSHandler no longer defines _check_hostname and HTTPSConnection no longer accepts
        # check_hostname, so the withdrawn override failed before every HTTPS request there. The fetch is plain urlopen with
        # the default SSL context: bridge_models.py and bridge.py name no private attribute of urllib, http.client or ssl,
        # subclass no urllib handler, build no connection by hand, and never switch verification off
        banned = {"HTTPSHandler._check_hostname": r"\b_check_hostname\b",
                  "a handler's _context": r"\._context\b",
                  "the _Connections factory": r"\b_Connections\b",
                  "a private attribute of urllib, http.client or ssl": r"\b(?:urllib(?:\.\w+)?|http\.client|ssl)\._\w+",
                  "a urllib handler subclass": r"\bclass\s+\w+\s*\([^)]*\bHTTPS?Handler\b",
                  "an http.client connection made by hand": r"\bHTTPS?Connection\b",
                  "AbstractHTTPHandler.do_open": r"\bdo_open\b",
                  "verification switched off": r"\bCERT_NONE\b|check_hostname\s*=\s*False|_create_unverified_context"}
        for path in (SCRIPTS / "bridge_models.py", ENGINE):
            lines = path.read_text().splitlines()
            for what, pattern in banned.items():
                with self.subTest(file=path.name, banned=what):
                    used = [f"{path.name}:{n}: {line.strip()}" for n, line in enumerate(lines, 1) if re.search(pattern, line)]
                    self.assertEqual(used, [], f"the fetch code uses {what}")

    def test_probes_the_refresh_budget_skipped_or_cut_short_are_not_cached(self):
        # C4b-3: a probe the refresh's budget skipped, or cut short, is not a failed probe: neither the version probe
        # (probe_ok) nor the capability probe (codex_capable) caches it, and discovery after the refresh probes again with the
        # normal timeout. An exhausted refresh followed by a launch in the same process finds the healthy codex: (1)
        # BRIDGE_MODELS_FETCH_DEADLINE=0 skips every probe; with a 1 s budget (2) the version probe or (3) the capability probe
        # answers only after 2 s. The refresh reads nothing (the notice says so) and the seat runs on the stub codex, not
        # "codex not found"
        luna = head("luna")
        cases = {"a budget of 0: every probe skipped": {"BRIDGE_MODELS_FETCH_DEADLINE": "0"},
                 "the version probe cut short": {"BRIDGE_MODELS_FETCH_DEADLINE": "1", "STUB_CODEX_PROBE_SLEEP": "version:2"},
                 "the capability probe cut short": {"BRIDGE_MODELS_FETCH_DEADLINE": "1", "STUB_CODEX_PROBE_SLEEP": "help:2"}}
        for i, (case, extra) in enumerate(cases.items()):
            with self.subTest(refresh=case):
                env, _ = self.isolated(f"c4b-3-{i}", **extra)
                rc, out, err = self.run_within(90, env, "ask", "--seats", "luna:low", "--", "q")
                self.assertEqual(rc, 0, "the launch after an exhausted refresh did not find the healthy codex:\n" + out + err)
                self.assertEqual(self.launched_models(env), [luna])
                self.assertNotIn("codex not found", out + err)
                self.assertTrue(out.startswith("NOTICE: models: unverified ("), out)

    def test_malformed_upgrade_info_fields_are_a_source_error_that_keeps_the_last_observation(self):
        # C4b-4 (C3b-3 was partial): the fields read inside upgradeInfo are validated too: `model` and `migrationMarkdown` are
        # strings or null, `retirementAt` a supported timestamp or null. A malformed one is an allowlisted source error: the
        # catalog is UNVERIFIED, the last observation is kept and no marker is added. The ruling's case, an otherwise valid Sol
        # entry whose migrationMarkdown is {"deprecated": false}, was stringified, matched as prose ("deprecated") and made a
        # sticky marker that refused new Sol seats even under `warn`. The first catalog lists a hidden entry of its own,
        # gpt-9-nova; a replaced observation would lose it
        sol, kept = head("sol"), "gpt-9-nova"
        good = self.catalog_file("c4b-4-good", lambda ms: ms.append(self.new_entry(kept, hidden=True)))
        blank = self.case_dir("c4b-4") / "config-read-blank.json"  # no origin and no value: applicable with or without L1
        data = json.loads(CONFIG_DEFAULT.read_text())
        blank.write_text(json.dumps(dict(data, config={k: ({} if isinstance(v, dict) else None) for k, v in data["config"].items()})))
        info = {"model": None, "upgradeCopy": None, "modelLink": None, "migrationMarkdown": None, "retirementAt": None}
        cases = {"migrationMarkdown {'deprecated': false}": dict(info, migrationMarkdown={"deprecated": False}),
                 "model 17": dict(info, model=17),
                 "retirementAt 'soon'": dict(info, retirementAt="soon"),
                 "retirementAt an object": dict(info, retirementAt={"at": FROZEN_NOW + 30 * 86400})}
        for i, (case, upgrade_info) in enumerate(cases.items()):
            with self.subTest(upgradeInfo=case):
                env, _ = self.isolated(f"c4b-4-{i}", catalog=good, config=blank)
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(kept, list(strings(self.evidence(env))))
                before = self.marker_ids(env)
                bad = self.catalog_file(f"c4b-4-bad-{i}", lambda ms, u=upgrade_info: self.entry(ms, sol).__setitem__("upgradeInfo", u))
                bad_env = dict(env, STUB_CODEX_CATALOG=str(bad))
                rc, out, err = self.run_engine("doctor", "--json", env_extra=bad_env)
                self.assertNotIn("Traceback", err, "a malformed field raised outside the catalog reader's handling")
                data = self.json_of(rc, out, err)
                texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), texts)
                self.assertTrue(self.unverified_about(data, "codex"), texts)
                self.assertIn(kept, list(strings(self.evidence(env))), "the malformed answer replaced the last observation")
                self.assertEqual(self.marker_ids(env), before, "the malformed entry added a marker")
                rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=bad_env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.launched_models(bad_env)[-1:], [sol])


class GroupE_LiveCheckFindings(CheckBase):
    """The live-check findings (verify/live/doctor_premerge.txt: the branch's own doctor FAILed against the live sources on
    Kevin's Mac; the synthetic fixtures had hidden them), through the engine: L1 resolved defaults are defaults, L2 service_tier
    is not a catalog key, L3 a Codex CLI older than npm latest is UNVERIFIED. The pure pieces: test_bridge_models.py
    GroupE_LiveCheckPure; L4: GroupD_UpgradeHints."""

    CAPTURE = EVIDENCE / "codex_config_read_catalog_keys_20260925_cli0.156.1_sanitized.json"  # real, CLI 0.156.1, 2026-09-25
    USER_LAYER = {"name": {"type": "user", "file": "/Users/USER/.codex/config.toml", "profile": None},
                  "version": "sha256:b999efde173cfa78babd2a964d440879317bb134254ee4a0d3a220558163bedc"}  # the capture's own origin

    def capture_variant(self, name, mutate):
        """The real config/read capture changed by `mutate(data)`, as the case's STUB_CODEX_CONFIG file."""
        data = json.loads(self.CAPTURE.read_text())
        mutate(data)
        p = self.case_dir(name) / "config-read.json"
        p.write_text(json.dumps(data, indent=1))
        return p

    @staticmethod
    def applicability(data):
        return [f.get("text") for f in data.get("findings") or [] if isinstance(f, dict) and "not applicable" in (f.get("text") or "")]

    @staticmethod
    def config_keys(data):
        keys = ((data.get("sources") or {}).get("codex") or {}).get("config_keys")
        return keys if isinstance(keys, dict) else {}

    def assert_runs_under_refuse_without_a_notice(self, env):
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(leading_notices(out), [])

    def test_resolved_defaults_are_default_and_only_a_config_layer_makes_a_key_non_default(self):
        # L1: Codex's config/read answers RESOLVED defaults. The real capture (fixtures/evidence; CLI 0.156.1, 2026-09-25) has
        # chatgpt_base_url = "https://chatgpt.com/backend-api/" with no origin. A catalog key is non-default only when a config
        # layer sets it: `origins` has an entry for the key or for a sub-key (`key.`). With the capture, its service_tier origin
        # removed (L2 takes that key out of the table anyway), the local catalog is applicable and the check is OK, every key
        # is reported default, and a new job runs under `refuse` without a notice. The same capture with a user-layer origin on
        # chatgpt_base_url is not applicable because of that key alone. The value is never printed or stored
        default = self.capture_variant("l1", lambda d: d["origins"].pop("service_tier"))
        env, _ = self.isolated("l1", config=default)
        rc, data, err = self.doctor_json(env)
        texts = [f.get("text") for f in data.get("findings") or [] if isinstance(f, dict)]
        self.assertEqual(self.applicability(data), [], "a resolved default without an origin was counted as non-default")
        self.assertEqual((rc, data.get("verdict")), (0, "OK"), texts)
        keys = self.config_keys(data)
        self.assertTrue(keys, data.get("sources"))
        self.assertEqual({k: v for k, v in keys.items() if v != "default"}, {}, keys)
        self.assert_runs_under_refuse_without_a_notice(env)
        user_set = self.capture_variant("l1-user-url", lambda d: (d["origins"].pop("service_tier"),
                                                                  d["origins"].__setitem__("chatgpt_base_url", self.USER_LAYER)))
        env_set, _ = self.isolated("l1-user-url", config=user_set)
        rc, data, err = self.doctor_json(env_set)
        self.assertEqual(self.applicability(data), ["local catalog not applicable: chatgpt_base_url is non-default"])
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"))
        self.assertEqual({k: v for k, v in self.config_keys(data).items() if v != "default"}, {"chatgpt_base_url": "non-default"})
        for e in (env, env_set):
            rc, out, err = self.doctor(e)
            self.assertNotIn("chatgpt.com/backend-api", out + err + self.evidence_bytes(e).decode())

    def test_a_user_layer_service_tier_leaves_the_local_catalog_applicable(self):
        # L2: service_tier is not a catalog key (Codex rust-v0.157.0 app-server/src/models.rs: supported_models() filters only on
        # include_hidden || show_in_picker; service tiers are per-model metadata). The real capture as it was read, with
        # service_tier = "priority" set by the user layer (Kevin's ~/.codex/config.toml), leaves the local catalog applicable:
        # the check is OK, service_tier is not among the reported keys, and a new job runs under `refuse` without a notice
        env, _ = self.isolated("l2", config=self.CAPTURE)
        rc, data, err = self.doctor_json(env)
        texts = [f.get("text") for f in data.get("findings") or [] if isinstance(f, dict)]
        self.assertEqual(self.applicability(data), [], "the user layer's service_tier made the local catalog not applicable")
        self.assertEqual((rc, data.get("verdict")), (0, "OK"), texts)
        keys = self.config_keys(data)
        self.assertTrue(keys, data.get("sources"))
        self.assertNotIn("service_tier", keys)
        self.assertEqual({k: v for k, v in keys.items() if v != "default"}, {}, keys)
        self.assert_runs_under_refuse_without_a_notice(env)

    def test_a_codex_older_than_npm_latest_is_unverified_and_a_new_job_runs_under_refuse(self):
        # L3: Codex ships a release every few days and 0.157.0 added no model, so an installed CLI older than npm latest is
        # class `unverified` (the CLI may not list newer models; the upgrade hint), not a FAIL: the verdict is UNVERIFIED when
        # nothing else fails, and under BRIDGE_MODELS_GATE=refuse (and warn) a new job still runs with its one notice. A CLI below
        # a model's own floor is still refused at launch
        npm = json.dumps({"name": "@openai/codex", "dist-tags": {"latest": "0.157.0"}})
        env, _ = self.isolated("l3", npm=npm, STUB_CODEX_VERSION="codex-cli 0.156.1")
        rc, data, err = self.doctor_json(env)
        findings = [f for f in data.get("findings") or [] if isinstance(f, dict)]
        found = [f for f in findings if "0.157.0" in (f.get("text") or "")]
        self.assertEqual(len(found), 1, findings)
        self.assertEqual(found[0].get("class"), "unverified", found[0])
        self.assertIn("0.156.1", found[0]["text"])
        self.assertEqual([f for f in findings if f.get("class") not in ("unverified", "info")], [])
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"))
        for gate in ("refuse", "warn"):
            with self.subTest(gate=gate):
                jobs = self.job_names()
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE=gate))
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(len(self.new_jobs(jobs)), 1)
                notices = self.models_notices(out)
                self.assertEqual(len(notices), 1, out)
                self.assertTrue(notices[0].startswith("NOTICE: models: unverified ("), notices)
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q",
                                       env_extra=dict(env, STUB_CODEX_VERSION="codex-cli 0.152.0", BRIDGE_MODELS_GATE="refuse"))
        self.assertIn(f"needs Codex CLI >= {TABLE['models'][head('sol')]['min_cli']}", self.assert_refused(rc, out, err))


class GroupD_UpgradeHints(CheckBase):
    """L4: every Codex upgrade hint (the floor refusal, the discovery finding, bootstrap's messages) is chosen by ONE helper
    from the resolved executable's real path: under /node_modules/@openai/codex/ `npm install -g @openai/codex@latest`; under
    a Homebrew prefix (/opt/homebrew/, /usr/local/Cellar/, a Caskroom) `brew upgrade codex`; inside ChatGPT.app "update the
    ChatGPT app"; under <CODEX_HOME>/packages/standalone/ (OpenAI's installer, linked from ~/.local/bin/codex) or anywhere else
    `curl -fsSL https://chatgpt.com/codex/install.sh | sh`. One test per branch. In each case the only codex is a stub installed
    the way that installer installs it (its real path inside the case directory), found by the engine's own discovery (PATH,
    the app launcher, ~/.local/bin, and /opt/homebrew/bin and /usr/local/bin beneath BRIDGE_CODEX_FIXED_ROOT) with the host
    guard on. Its installer's hint, and no other, is in the refusal of a Sol seat when the stub is older than Sol's floor, and in
    the discovery finding (class `unverified` under L3) when it meets every floor but is older than npm latest. Bootstrap's
    messages: tests/test_bootstrap.sh groupD_upgrade_hint_*."""

    BELOW, CURRENT, LATEST = "0.152.0", "0.156.1", "0.157.0"  # older than Sol's floor (0.153.0); meets every floor (Luna's 0.156.1); npm latest in these cases

    def installed(self, name, layout, version):
        """A case whose only codex is installed as `layout` says ({path in the case: None for the stub printing `version`, or
        the target of a symlink: relative when it starts with ../, else a path in the case}): (environment, case directory)."""
        env, d = self.isolated(name, npm=json.dumps({"name": "@openai/codex", "dist-tags": {"latest": self.LATEST}}))
        d = Path(os.path.realpath(str(d)))
        for sub in ("bin", "home", "codex-home", "fixed-root", "py"):
            (d / sub).mkdir(exist_ok=True)
        if not (d / "py" / "python3").exists():
            (d / "py" / "python3").symlink_to(sys.executable)
        for rel, target in layout.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if target is None:
                p.write_text(f"#!/bin/sh\nSTUB_CODEX_VERSION='codex-cli {version}' exec '{sys.executable}' '{STUB_CODEX}' \"$@\"\n")
                p.chmod(0o755)
            elif not p.is_symlink():
                p.symlink_to(target if target.startswith("../") else str(d / target))
        env.update(PATH=os.pathsep.join([str(d / "bin"), str(d / "npm-global" / "bin"), str(d / "py"), "/usr/bin", "/bin"]),
                   CODEX_BIN=None, HOME=str(d / "home"), CODEX_HOME=str(d / "codex-home"),
                   BRIDGE_APP_LAUNCHER=str(d / APP_LEGACY),  # ONE path, the legacy binary's: the engine derives APP_NEW from its folder
                   BRIDGE_CODEX_FIXED_ROOT=str(d / "fixed-root"), PYTHONPATH=str(HOST_GUARD), PYTHONDONTWRITEBYTECODE="1",
                   HOST_GUARD_ROOTS=str(d), HOST_GUARD_LOG=str(d / "host-guard.jsonl"))
        return env, d

    def assert_hint(self, text, hint, where):
        named = hints_in(text)
        self.assertIn(hint, named, f"{where} does not name `{UPGRADE_HINTS[hint]}`: {text}")
        self.assertEqual(named - {hint}, set(), f"{where} names another installer's upgrade: {text}")

    def assert_branch(self, name, layout, hint):
        """The floor refusal and the discovery finding of one installation shape both carry `hint` and no other."""
        env, d = self.installed(f"{name}-floor", layout, self.BELOW)
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        guard = d / "host-guard.jsonl"
        self.assertEqual(self.read_records(guard) if guard.exists() else [], [], "a codex outside the case was about to start")
        line = self.assert_refused(rc, out, err)
        self.assertIn(f"needs Codex CLI >= {TABLE['models'][head('sol')]['min_cli']}", line)
        self.assert_hint(line, hint, "the floor refusal")
        env, d = self.installed(f"{name}-discovery", layout, self.CURRENT)
        rc, data, err = self.doctor_json(env)
        found = [f for f in data.get("findings") or [] if isinstance(f, dict) and self.LATEST in (f.get("text") or "")]
        self.assertEqual(len(found), 1, f"no single discovery finding naming npm latest {self.LATEST}: {data.get('findings')}")
        self.assertEqual(found[0].get("class"), "unverified", found[0])  # L3
        self.assert_hint(found[0]["text"], hint, "the discovery finding")

    def test_npm(self):
        # L4, npm: npm links <prefix>/bin/codex to ../lib/node_modules/@openai/codex/bin/codex. Also with the npm prefix of a
        # Node.js installed by Homebrew (/opt/homebrew): the real path is under /opt/homebrew/ as well, and npm's hint wins. The
        # branch is exactly /node_modules/@openai/codex/: a codex vendored by another package (@openai/codex-sdk) is anything
        # else, OpenAI's installer
        sdk = "lib/node_modules/@openai/codex-sdk/vendor/aarch64-apple-darwin/codex/codex"
        cases = {"an npm prefix on PATH": ({"npm-global/lib/node_modules/@openai/codex/bin/codex": None,
                                           "npm-global/bin/codex": "../lib/node_modules/@openai/codex/bin/codex"}, "npm"),
                 "the npm prefix of Homebrew's Node.js": ({"fixed-root/opt/homebrew/lib/node_modules/@openai/codex/bin/codex": None,
                                                           "fixed-root/opt/homebrew/bin/codex": "../lib/node_modules/@openai/codex/bin/codex"},
                                                          "npm"),
                 "a codex vendored by @openai/codex-sdk": ({f"npm-global/{sdk}": None, "npm-global/bin/codex": f"../{sdk}"}, "curl")}
        for i, (case, (layout, hint)) in enumerate(cases.items()):
            with self.subTest(installed=case):
                self.assert_branch(f"l4-npm-{i}", layout, hint)

    def test_homebrew(self):
        # L4, Homebrew: a formula in the Cellar (Apple silicon /opt/homebrew, Intel /usr/local/Cellar) or a cask in the
        # Caskroom, linked from the prefix's bin; and a formula reached through a link elsewhere on PATH (the real path decides)
        cases = {"a formula, /opt/homebrew": {"fixed-root/opt/homebrew/Cellar/codex/0.155.0/bin/codex": None,
                                              "fixed-root/opt/homebrew/bin/codex": "../Cellar/codex/0.155.0/bin/codex"},
                 "a formula, /usr/local/Cellar": {"fixed-root/usr/local/Cellar/codex/0.155.0/bin/codex": None,
                                                  "fixed-root/usr/local/bin/codex": "../Cellar/codex/0.155.0/bin/codex"},
                 "a cask, /usr/local/Caskroom": {"fixed-root/usr/local/Caskroom/codex/0.155.0/codex-x86_64-apple-darwin": None,
                                                 "fixed-root/usr/local/bin/codex": "../Caskroom/codex/0.155.0/codex-x86_64-apple-darwin"},
                 "a formula linked from a directory on PATH": {"fixed-root/opt/homebrew/Cellar/codex/0.155.0/bin/codex": None,
                                                               "bin/codex": "fixed-root/opt/homebrew/Cellar/codex/0.155.0/bin/codex"}}
        for i, (case, layout) in enumerate(cases.items()):
            with self.subTest(installed=case):
                self.assert_branch(f"l4-brew-{i}", layout, "brew")

    def test_chatgpt_app(self):
        # L4, the ChatGPT app: its bundled launcher, the app launcher of discovery, in each of the app's two bundle layouts (the
        # legacy binary; since bundle 26.928 the launcher at codex-cli/bin/codex). Either is the case's only codex
        for i, (case, rel) in enumerate({"the legacy layout": APP_LEGACY, "the new layout (codex-cli/bin/codex)": APP_NEW}.items()):
            with self.subTest(installed=case):
                self.assert_branch(f"l4-app-{i}", {rel: None}, "app")

    def test_openai_installer_and_anything_else(self):
        # L4, OpenAI's installer (install.sh puts the CLI under $CODEX_HOME/packages/standalone and links it from
        # ${CODEX_INSTALL_DIR:-$HOME/.local/bin}), and a codex anywhere else
        cases = {"OpenAI's installer, linked from ~/.local/bin/codex": {"codex-home/packages/standalone/0.155.0/bin/codex": None,
                                                                       "home/.local/bin/codex": "codex-home/packages/standalone/0.155.0/bin/codex"},
                 "anything else": {"bin/codex": None}}
        for i, (case, layout) in enumerate(cases.items()):
            with self.subTest(installed=case):
                self.assert_branch(f"l4-curl-{i}", layout, "curl")


class GroupE_NpmDistTags(CheckBase):
    """briefs/npm_round.md, through the engine: the live check on a8d8f7e7 read npm's full packument at the old default URL
    (15.7 MB) and failed as `too-large` under the fetch's 2 MiB cap. The default npm URL becomes the registry's dist-tags
    endpoint, whose answer carries the tags at the top level (the real one, fixtures/evidence/npm_dist_tags_endpoint_20260925.json,
    is served here through BRIDGE_MODELS_NPM_URL); a packument (the synthetic npm_dist_tags.json every other test serves) still
    reads; anything else is an allowlisted source error that keeps the previous observation. The pure pieces, the default URL
    among them: test_bridge_models.py GroupE_NpmDistTagsPure."""

    ENDPOINT = EVIDENCE / "npm_dist_tags_endpoint_20260925.json"  # REAL, 2026-09-25, byte-identical: latest 0.157.0
    SENTINEL = "SENTINEL-NPM-BODY-83d7"  # in a body the reader rejects: never printed or stored

    def npm_source(self, env):
        """The npm source's record in the evidence file: its retained observation, read time and latest attempt."""
        return (self.evidence(env).get("sources") or {}).get("npm") or {}

    @staticmethod
    def findings_of(data):
        return [f for f in data.get("findings") or [] if isinstance(f, dict)]

    def test_doctor_reads_the_real_endpoint_capture(self):
        # the capture's bytes as the npm page: the fetch reads them, the store keeps latest 0.157.0, and the installed Codex CLI
        # is compared with it: 0.156.1 gets the one L3 line naming both versions (UNVERIFIED) and no finding concerns the npm
        # source; with 0.157.0 installed the check is OK
        env, _ = self.isolated("npm-endpoint", npm=self.ENDPOINT, STUB_CODEX_VERSION="codex-cli 0.156.1")
        rc, data, err = self.doctor_json(env)
        npm = self.npm_source(env)
        self.assertTrue((npm.get("attempt") or {}).get("ok"), f"the endpoint's answer was not read: {npm}")
        self.assertIn("0.157.0", list(strings(npm.get("observation"))))
        findings = self.findings_of(data)
        self.assertEqual([f for f in findings if f.get("source") == "npm"], [], findings)
        found = [f for f in findings if "0.157.0" in (f.get("text") or "")]
        self.assertEqual([f.get("class") for f in found], ["unverified"], findings)
        self.assertIn("0.156.1", found[0]["text"])
        self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"))
        rc, data, err = self.doctor_json(dict(env, STUB_CODEX_VERSION="codex-cli 0.157.0"))
        self.assertEqual((rc, data.get("verdict")), (0, "OK"), self.findings_of(data))

    def test_a_body_with_neither_shape_keeps_the_previous_observation(self):
        # after doctor read the endpoint (latest 0.157.0 kept), an npm page with neither a top-level `latest` string nor
        # `dist-tags.latest` is a source error in the check's own words: the attempt fails, the observation and its read time
        # stay, npm is unverified, the kept latest still drives the L3 line, no traceback, and nothing of the body is printed or
        # stored. The full set of shapes: GroupE_NpmDistTagsPure
        env, _ = self.isolated("npm-neither", npm=self.ENDPOINT, STUB_CODEX_VERSION="codex-cli 0.156.1")
        rc, out, err = self.doctor(env)
        kept = self.npm_source(env)
        self.assertIn("0.157.0", list(strings(kept.get("observation"))), f"the endpoint's answer was not kept: {kept}\n{out}{err}")
        bodies = {"the endpoint's tags without latest": json.dumps({"beta": self.SENTINEL, "alpha": "0.159.0-alpha.2"}),
                  "a number for latest": json.dumps({"latest": 157, "beta": self.SENTINEL}),
                  "a packument without dist-tags.latest": json.dumps({"name": "@openai/codex", "dist-tags": {"beta": self.SENTINEL}}),
                  "not JSON (an HTML page)": f"<!doctype html><title>{self.SENTINEL}</title>"}
        for i, (case, body) in enumerate(bodies.items()):
            with self.subTest(body=case):
                bad = dict(env, **self.pages.pages(f"npm-neither-{i}", npm=body))
                rc, data, err = self.doctor_json(bad)
                self.assertNotIn("Traceback", err)
                npm = self.npm_source(env)
                self.assertEqual((npm.get("observation"), npm.get("read_at")), (kept.get("observation"), kept.get("read_at")),
                                 "the body replaced the previous observation")
                attempt = npm.get("attempt") or {}
                self.assertFalse(attempt.get("ok"), attempt)
                self.assertTrue(str(attempt.get("error") or "").strip(), attempt)
                findings = self.findings_of(data)
                self.assertTrue([f for f in findings if f.get("class") == "unverified" and f.get("source") == "npm"], findings)
                self.assertTrue([f for f in findings if "0.157.0" in (f.get("text") or "") and "0.156.1" in (f.get("text") or "")], findings)
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"))
                self.assertNotIn(self.SENTINEL, json.dumps(data) + err + self.evidence_bytes(env).decode())


# ==================================================================================================================
# The final review (review/rulings_final_review.md, binding; its findings verbatim with file:line: review/final_A_findings.md,
# final_B_findings.md, final_C_findings.md), through the engine; each test names its finding. The pure pieces:
# test_bridge_models.py GroupA_FinalReviewPure and GroupE_FinalReviewPure; merged.md's blank line (A-opus-5):
# test_merge_findings.py GroupB_MergeNotices (stdout without it: GroupB_FinalReviewLaunchAndOutputs here); claude_status
# (B-astra-3): codex-skills/claude/tests/test_mcp_server.py GroupB_MCPFinalReview; bootstrap and package.sh (C-astra-1,
# C-opus-1, C-opus-6, C-opus-7): tests/test_bootstrap.sh groupD_final_review_* (the engine's own Claude floor refusal, C-opus-7:
# GroupD_FinalReviewFloor here); the West kit's pin (C-astra-2): distribution/west-ai-kit/tests/test_sync_codex_sol_pin.py
# ==================================================================================================================
class GroupA_FinalReviewTable(EngineBase):
    """A-opus-3 and A-opus-4 through the engine: an alias on a model that is not its family's head is a one-line table error
    (never a KeyError traceback), and a name matching a family's pattern is refused naming that family's head."""

    refuse = GroupA_TableAndResolution.refuse  # `ask --seats <spec>` refused: no job, no relay call, no launch

    @staticmethod
    def new_haiku_head(t):
        """Haiku gets a new (fictional) head, and the old head moves to `retired.claude.haiku` but keeps its record in `models`,
        WITH its listed spelling (as the table keeps the K1 target there): A-opus-3's maintainer step."""
        old = t["families"]["claude"]["haiku"]["known_good"]
        new = NEWER_HAIKU
        t["models"][new] = dict(t["models"][old], generation=NEWER_HAIKU.rsplit("-", 2)[1], aliases=[], source="test: a new Haiku head (A-opus-3)")
        t["families"]["claude"]["haiku"]["known_good"] = new
        t["retired"]["claude"]["haiku"].insert(0, old)

    def test_a_preserved_predecessor_alias_is_refused_without_key_error(self):
        old = TABLE["retired"]["claude"]["haiku"][0]
        alias = TABLE["models"][old]["aliases"][0]
        env, _ = self.isolated("preserved-haiku-alias")
        for spec in (old, alias, "claude/" + alias):
            with self.subTest(seat=spec):
                before = self.job_names()
                line = self.refuse("preserved-haiku-alias", spec, env)
                self.assertIn(f"is superseded by {head('haiku')}", line)
                self.assertEqual(self.new_jobs(before), [])
                self.assertEqual(self.launched_models(env), [])

    def test_a_name_matching_a_familys_pattern_is_refused_naming_that_familys_head(self):
        # A-opus-4 [P3]: Anthropic's dateless aliases of retired snapshots (a dated retired id with its date stripped) are not in
        # `retired`, so they fell to rule 6, whose hint leads with the first family ("Use one of astra (<its head>), sol, ..."): a
        # Codex model as the only concrete replacement of an obviously Claude name. A name matching a family's pattern (`id_prefix`,
        # `id_glob`) that is not in the table is refused naming THAT family's head, and no other head, with no job, no relay call
        # and no launch. The names matching no pattern: test_everything_else_is_refused_naming_a_head
        cases = {}
        for fam in ("sonnet", "opus"):  # the table's dated retired snapshots, spelled dateless (Anthropic's aliases for them)
            prefix = TABLE["families"]["claude"][fam]["id_prefix"]
            for x in TABLE["retired"]["claude"][fam]:
                if x.startswith(prefix) and re.search(r"-\d{8}$", x):
                    cases[re.sub(r"-\d{8}$", "", x)] = fam
        self.assertGreaterEqual(len(cases), 2, "the table lists dated retired Sonnet and Opus snapshots")
        cases.update({NEWER_OPUS: "opus", NEWER_SOL: "sol", NEWER_LUNA: "luna", f"claude/{NEWER_OPUS}": "opus", f"codex/{NEWER_SOL}": "sol"})
        for i, (name, fam) in enumerate(cases.items()):
            with self.subTest(name=name):
                env, _ = self.isolated(f"fr-a-opus-4-{i}")
                line = self.refuse(f"fr-a-opus-4-{i}", name, env, relay=(i == 0))
                self.assertTrue(line.startswith(f"bridge: refused seat '{name}': "), line)
                self.assertIn(f"Use {fam} ({head(fam)}).", line, f"the refusal of {name} does not name the {fam} head")
                self.assertEqual([head(f) for f in families() if f != fam and head(f) in line], [], f"another family's head is named: {line}")


class GroupB_FinalReviewLaunchAndOutputs(EngineBase):
    """B-astra-1 (= B-opus-1), B-astra-2, B-astra-3, B-opus-2, B-opus-3 and the stdout half of A-opus-5 through the engine."""

    write_marker = GroupB_LaunchFallbackObservationNotices.write_marker    # a marker in the store's layout, replaced atomically
    as_old_record = GroupB_LaunchFallbackObservationNotices.as_old_record  # a record that names only the model string
    WAIT = 10  # seconds (from before the engine starts) a routed seat waits for an account here: a marker is published meanwhile

    def texts(self, d):
        return [n.get("text") for n in self.notices(d) if isinstance(n, dict)]

    # ---- B-astra-1 [P1] (= B-opus-1 [P3])
    def test_a_marker_published_while_a_seat_waits_for_its_first_account_refuses_its_first_launch(self):
        # B-astra-1 [P1]: validation ran before the job, then the seat parked in routing.choose()'s account wait; the pre-launch
        # check ran only for `attempt > 1`, so a retirement marker another doctor published during that wait launched the
        # marked model with neither refusal nor notice (Luna; a Claude seat's first attempt the same). The ruling: after
        # routing.choose() returns (after _wait_seat too) and immediately before the FIRST launch, `initial`-mode validation
        # runs, and a refusal is allowed: nothing has run yet. Here relay answers "every account is out" with a wake time, the
        # marker lands while the seat waits, and relay then offers account a: the seat is refused, relay never execs and no CLI
        # starts. (A retry of an attempt that already ran keeps `continuation` mode: GroupB_LaunchFallbackObservationNotices
        # .test_a_marker_learned_while_a_retry_waits_is_a_notice_before_it_launches)
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            model = head(tag)
            with self.subTest(seat=spec):
                job = f"fr-b-astra-1-first-{tag}"
                env, _ = self.isolated(job)
                renv, rd = self.relay(job, {"pick": {vendor: [{"exit": 1, "wake_at": time.time() + self.WAIT}, {"exit": 0, "label": "a"}]}})
                env.update(renv)
                p = self.start_engine(env, "ask", "--seats", spec, "--job-dir", job, "--", "q")
                try:
                    self.wait_for(self.jobs / job, tag, lambda s: s.get("state") == "waiting")
                    self.write_marker(env, model, "deprecated")  # another invocation's check publishes a marker meanwhile
                finally:
                    out, err = p.communicate(timeout=120)
                self.assertEqual(self.launched_models(env), [], f"the first launch ran {model}, marked while the seat waited for an "
                                                               f"account, without the initial-mode validation:\n{out}{err}")
                self.assertEqual(self.records(rd, "execs.jsonl"), [], "relay was asked to exec the CLI")
                seat = self.seat(self.jobs / job, tag)
                self.assertEqual((seat.get("state"), seat.get("first_launch_at")), ("failed", None), seat)
                self.assertIn(f"{model} is marked deprecated", seat.get("activity", "") + out)
                self.assertNotEqual(p.returncode, 0, out + err)

    def test_a_marker_published_while_a_fallback_target_waits_for_an_account_refuses_it(self):
        # B-astra-1 [P1] / B-opus-1 [P3], changed for docs/model-routing.md 3.3 and 5 (F3): Astra is rejected at startup on account a
        # and the seat re-picks Astra for the rest of its capable set; every account is out until the wake time, then b is offered
        # and rejects Astra too, so the seat descends to Sol through its `astra|sol` list. `new_candidate` used to skip the pre-launch
        # check, so a marker published on the Sol head during the wait launched Sol with no check at all. The ruling: the new target
        # gets `initial`-mode validation immediately before it first launches: refused, only the two Astra attempts ever ran
        astra, sol = head("astra"), head("sol")
        job = "fr-b-astra-1-fallback"
        env, _ = self.isolated(job)
        renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + self.WAIT},
                                                       {"exit": 0, "label": "b"}]}})
        env.update(renv)
        p = self.start_engine(env, "ask", "--seats", "astra|sol:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra}]] q")
        try:
            self.wait_for(self.jobs / job, "astra", lambda s: s.get("state") == "waiting")
            self.write_marker(env, sol, "deprecated")
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(self.launched_models(env), [astra, astra], f"the list's target {sol}, marked while the seat waited for an "
                                                                    f"account, launched without the initial-mode validation:\n{out}{err}")
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"], "relay was asked to exec the target")
        seat = self.seat(self.jobs / job, "astra")
        self.assertEqual(seat.get("state"), "failed", seat)
        self.assertIn(f"{sol} is marked deprecated", seat.get("activity", "") + out)
        self.assertNotEqual(p.returncode, 0, out + err)

    # ---- B-astra-2 [P2]
    def test_an_empty_raw_file_falls_back_to_the_composed_answer(self):
        # B-astra-2 [P2]: when Codex's -o file exists but is empty, _codex_attempt recovers the answer from the JSON events and
        # _compose_last saves it in last-<tag>.md, but answer_files chose the empty raw file because it existed: plain `result`
        # printed the parent's older answer (exit 0) and `result --raw` left the recovered reply out. answer_files prefers a
        # NON-EMPTY raw file; an empty one falls back to the composed answer. The stub leaves -o empty (`emptyout`); the
        # existing recovery test deletes the raw file instead (test_retrieval_takes_each_answer_from_its_own_raw_or_last_file)
        env, _ = self.isolated("fr-b-astra-2")
        job = "fr-b-astra-2-ask"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", job, "--", "[[STUB:emptyout,text=RECOVERED-ANSWER]] q",
                                       env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RECOVERED-ANSWER", out)  # the ask itself printed the recovered answer
        d = self.jobs / job
        self.assertEqual((d / "raw-luna.md").read_text(), "", "the stub did not leave the -o file empty")
        self.assertIn("RECOVERED-ANSWER", (d / "last-luna.md").read_text())
        for argv in (["result", job], ["result", job, "--raw"]):
            with self.subTest(retrieval=" ".join(argv)):
                rc, out, err = self.run_engine(*argv, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("RECOVERED-ANSWER", out, f"`{' '.join(argv)}` left out the answer recovered from the events")
        # a reply whose -o file is empty: plain `result` prints its answer, not the parent's older one; `--raw` includes both
        job = "fr-b-astra-2-reply"
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", job, "--", "[[STUB:text=PARENT-ANSWER]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_engine("reply", job, "--", "[[STUB:emptyout,text=RECOVERED-REPLY]] more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / job
        (raw,) = d.glob("raw-luna-reply-*.md")
        self.assertEqual(raw.read_text(), "", "the stub did not leave the reply's -o file empty")
        rc, out, err = self.run_engine("result", job, env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RECOVERED-REPLY", out, "result printed an answer older than the latest reply")
        self.assertNotIn("PARENT-ANSWER", out)
        rc, out, err = self.run_engine("result", job, "--raw", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RECOVERED-REPLY", out, "result --raw left out the reply recovered from the events")
        self.assertIn("PARENT-ANSWER", out)

    # ---- B-astra-3 [P2]
    def test_status_and_monitor_show_the_jobs_notices_and_the_job_list_marks_a_fallback(self):
        # B-astra-3 [P2]: mode_status and mode_monitor never read the persisted notices, so a job admitted under a failed or
        # unverified check (and one whose seat fell back) showed ordinary status output, and the job list kept the requested
        # model labels after a fallback. The ruling: `status` and `monitor` show each job's persisted notices, all of them in the
        # detail views (`status <job>`, `monitor <job>`) and the count with the first one in the job list, and the job list marks
        # a seat that fell back (`requested -> ran`). The job has its own jobs root, so the list shows it alone: it was admitted
        # under an unverified check (npm unreadable: the gate's notice comes first) and Astra fell back to Sol. The MCP
        # claude_status: codex-skills/claude/tests/test_mcp_server.py GroupB_MCPFinalReview
        astra, sol = head("astra"), head("sol")
        env, cd = self.isolated("fr-b-astra-3", npm=404)
        env["BRIDGE_JOBS"] = str(cd / "jobs")
        job = "fr-b-astra-3"
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        texts = self.texts(cd / "jobs" / job)
        self.assertTrue(texts and texts[0].startswith("NOTICE: models: unverified ("), texts)  # the gate's notice leads
        self.assertTrue([t for t in texts if f"model {astra} -> {sol}" in t], texts)          # and the seat fell back
        for argv in (["status", job], ["monitor", job, "--once"]):
            with self.subTest(view=" ".join(argv)):
                rc, out, err = self.run_engine(*argv, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual([t for t in texts if t not in out], [], f"`{' '.join(argv)}` leaves out the job's persisted notices:\n{out}")
        rc, out, err = self.run_engine("status", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        n = len(texts)
        self.assertRegex(out, rf"(?i)\b{n}\s+notices?\b|\bnotices?\W{{0,3}}{n}\b", f"the job list does not count the job's {n} notices:\n{out}")
        self.assertIn(texts[0][len("NOTICE: "):][:40], out, f"the job list does not show the job's first notice:\n{out}")
        marked = [line for line in out.splitlines() if re.search(re.escape(astra) + r".*->.*" + re.escape(sol), line)
                  and "rejected at startup" not in line]
        self.assertTrue(marked, f"the job list does not mark the seat that fell back ({astra} -> {sol}):\n{out}")

    # ---- B-opus-2 [P3]
    def test_a_reply_that_dies_on_its_own_checks_persists_no_notices(self):
        # B-opus-2 [P3]: mode_reply persisted the gate's notice and the reply-model notice in meta.json BEFORE the checks that can
        # still die: the seat's resume lock (another reply in progress), the job's working directory gone, and a directory
        # outside BRIDGE_ROOT. The loser of two replies left a notice about a reply that never ran, and job_notices() leads every
        # later output of the job with it. The ruling: a reply persists its notices only after those checks. Each job's record
        # names only the alias (the reply would move to the head: a reply-model notice), and a host override makes the reply's
        # check FAIL (under `warn`: the gate's notice); each reply dies on one check, and the job's notices are unchanged
        cases = {"another reply holds the seat's resume lock": "another reply to seat opus is in progress",
                 "the job's working directory is gone": "no longer exists",
                 "the job's working directory is outside BRIDGE_ROOT": "outside the authorized root"}
        for i, (case, words) in enumerate(cases.items()):
            with self.subTest(dies_because=case):
                job = f"fr-b-opus-2-{i}"
                env, cd = self.isolated(job)
                rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", job, "--", "first", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                d = self.jobs / job
                self.as_old_record(d, "opus", "opus")
                before = self.notices(d)
                reply_env = dict(env, ANTHROPIC_DEFAULT_OPUS_MODEL=K1_ID)
                lock = None
                if i == 0:
                    lock = os.open(str(d / ".resume-opus.lock"), os.O_RDWR | os.O_CREAT, 0o600)
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif i == 1:
                    meta = self.meta(d)
                    meta["cwd"] = str(cd / "gone")
                    (d / "meta.json").write_text(json.dumps(meta))
                else:
                    (cd / "authorized-root").mkdir(exist_ok=True)
                    reply_env["BRIDGE_ROOT"] = str(cd / "authorized-root")
                try:
                    rc, out, err = self.run_engine("reply", job, "--", "more", env_extra=reply_env)
                finally:
                    if lock is not None:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        os.close(lock)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(words, err)
                self.assertEqual(self.launched_models(env), [head("opus")], "the reply launched")
                self.assertEqual(sorted(p.name for p in d.glob("seat-opus-reply-*.json")), [])
                self.assertEqual(self.notices(d), before, "a reply that died on its own checks persisted notices in the job")

    # ---- B-opus-3 [P3]
    def test_a_default_seat_refusal_names_the_variable_only_when_it_is_set(self):
        # B-opus-3 [P3]: parse_seats named the default list's environment variable in every refusal of a default seat, set or
        # not: `/codex review --effort ultra` on a machine with no overrides refused the built-in list's Opus seat as
        # "refused seat 'opus' (BRIDGE_REVIEW_SEATS) (--effort): ...", blaming a variable the user never set. The variable is
        # named only when it is set; the refusal itself stays. For each default list: unset, then set to a list with the same
        # refusal
        cases = (("BRIDGE_REVIEW_SEATS", ["review", "--effort", "ultra"], "astra:ultra,opus:max"),
                 ("BRIDGE_COUNCIL_SEATS", ["council", "--effort", "ultra", "--", "q"], "sol:ultra,opus:max"),
                 ("BRIDGE_DEFAULT_SEAT", ["ask", "--effort", "bogus", "--", "q"], "astra:ultra"))
        for i, (var, argv, value) in enumerate(cases):
            for is_set in (False, True):
                with self.subTest(variable=var, set=is_set):
                    env, _ = self.isolated(f"fr-b-opus-3-{i}-{int(is_set)}", **({var: value} if is_set else {}))
                    before = self.job_names()
                    rc, out, err = self.run_engine(*argv, env_extra=env)
                    line = self.assert_refused(rc, out, err)
                    self.assertEqual(self.new_jobs(before), [])
                    if is_set:
                        self.assertIn(f"({var})", line)
                    else:
                        self.assertNotIn(var, line, f"the refusal names {var}, which is not set")

    # ---- A-opus-5 [P3], its second half
    def test_review_fanout_and_result_print_no_empty_line_under_the_heading_when_notices_exist(self):
        # A-opus-5 [P3]: merged.md's Notices list ends with a blank line (test_merge_findings.py GroupB_MergeNotices
        # .test_a_blank_line_ends_the_notices_list). stdout carries the notices once, as its leading lines, through bridge.py
        # without_notices_block, which drops `Notices:` and the `- ` items but not that blank line: review, fanout and `result`
        # would print an empty line under the heading. The blank line goes with the block, so the heading is followed at once by
        # the Reviewers and Findings lines. First the function, on merged.md as A-opus-5 renders it; then through the engine, with
        # jobs whose seat fell back (its notice) and whose merged.md ends the list with the blank line
        with self.subTest(part="without_notices_block"):
            b = load_engine_module(self.env)
            heading, rest = "# Review of uncommitted changes in /repo", "Reviewers: astra\nFindings: P2 1\n\n| # | Pri |\n"
            md = f"{heading}\nNotices:\n- NOTICE: one\n- NOTICE: two\n\n{rest}"
            self.assertEqual(b.without_notices_block(md), f"{heading}\n{rest}", "the blank line that ends the Notices list is kept")
            self.assertEqual(b.without_notices_block(f"{heading}\n{rest}"), f"{heading}\n{rest}")  # a report without notices: unchanged
        bad = f"[[STUB:badmodel={head('astra')}]]"
        env, cd = self.isolated("fr-a-opus-5")
        spec = cd / "fanout.json"
        spec.write_text(json.dumps({"seats": "astra|sol:low", "schema": True, "tasks": {"one": f"{bad} look"}}))
        runs = (("review", ["review", "--seats", "astra|sol:low", "--job-dir", "fr-a-opus-5-review", "--focus", bad], "fr-a-opus-5-review"),
                ("result", ["result", "fr-a-opus-5-review"], "fr-a-opus-5-review"),
                ("fanout", ["fanout", "--spec", str(spec), "--job-dir", "fr-a-opus-5-fanout"], "fr-a-opus-5-fanout"))
        for part, argv, job in runs:
            with self.subTest(part=part):
                rc, out, err = self.run_engine(*argv, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(self.notices(self.jobs / job) and leading_notices(out), out)
                md = (self.jobs / job / "merged.md").read_text().splitlines()
                end = next((i for i in range(2, len(md)) if not md[i].startswith("- ")), len(md))  # the line after the notice items
                self.assertEqual((md[1], md[end:end + 1]), ("Notices:", [""]),
                                 f"merged.md's Notices list does not end with a blank line (the first half of A-opus-5): {md[:end + 2]}")
                lines = out.splitlines()
                self.assertIn(md[0], lines, out)
                at = lines.index(md[0])
                self.assertEqual([line.split(":")[0] for line in lines[at + 1:at + 3]], ["Reviewers", "Findings"],
                                 f"`{argv[0]}` prints something else between the heading and the Reviewers and Findings lines:\n{out}")


class GroupD_FinalReviewFloor(EngineBase):
    """C-opus-7 in the engine: the Claude floor refusal names no specific Opus version."""

    attempt = GroupD_FloorsAndDiscovery.attempt  # `ask --seats <spec>` with a scripted relay: (rc, out, err, env, relay records)

    @staticmethod
    def newer_opus_head(t):
        """A newer Opus head (fictional, DESIGN §8) with a higher floor; the old head becomes a predecessor, as a reviewed table
        update would make it (predecessors live in `retired` only; `models` keeps heads and the K1 target)."""
        fam = t["families"]["claude"]["opus"]
        old = fam["known_good"]
        was = t["models"].pop(old)
        x, y, z = (int(p) for p in was["min_cli"].split("."))
        t["models"]["claude-opus-9-9"] = dict(was, generation="9.9", min_cli=f"{x}.{y}.{z + 20}", aliases=[],
                                              source="test: a newer Opus head (C-opus-7)")
        fam["known_good"] = "claude-opus-9-9"
        t["retired"]["claude"]["opus"].insert(0, old)

    def test_the_claude_floor_refusal_names_no_specific_opus_version(self):
        # C-opus-7 [P3]: below the Opus head's floor the refusal appended "(before it, opus meant the previous Opus)" (validate(),
        # bridge_models.py:831), true only for today's table: after a newer Opus head raises the floor, an older Claude Code maps
        # `opus` to the head before it, and the table is meant to move without code changes. The ruling names bootstrap and triad
        # (tests/test_bootstrap.sh groupD_final_review_claude_floor_note_names_no_opus_version; skills/triad/tests
        # test_triad_models.py GroupF_FinalReviewFindings); the engine's refusal carries the same literal. Say generically that an
        # older Claude Code resolves the short names to older models (that wording is not pinned). The refusal still names the
        # head, the floor and `claude update`, and no Opus version ("opus meant", "Opus <n>") and no other Opus model: with the
        # shipped table, and with a table whose Opus head is newer (the table the note gets wrong). No relay call, no launch
        for i, (label, mutate) in enumerate((("the shipped table", None), ("a newer Opus head", self.newer_opus_head))):
            with self.subTest(table=label):
                path = self.table_file(f"fr-c-opus-7-{i}", mutate)
                t = json.loads(path.read_text())
                opus = t["families"]["claude"]["opus"]["known_good"]
                floor = t["models"][opus]["min_cli"]
                x, y, z = (int(p) for p in floor.split("."))
                below = "%d.%d.%d" % ((x, y, z - 1) if z else (x, y - 1, 999) if y else (x - 1, 999, 999))  # one release short
                rc, out, err, env, rd = self.attempt(f"fr-c-opus-7-{i}", "opus:low", table=path, STUB_CLAUDE_VERSION=f"{below} (Claude Code)")
                line = self.assert_refused(rc, out, err)
                for fragment in (f"({opus})", f"needs Claude Code >= {floor}", "claude update"):
                    self.assertIn(fragment, line)
                self.assertNotRegex(line, r"(?i)opus meant|opus\s+[0-9]", "the Claude floor refusal names an Opus version")
                others = ({m for m, rec in t["models"].items() if rec["family"] == "opus"} | set(t["retired"]["claude"]["opus"])) - {opus}
                self.assertEqual([m for m in sorted(others) if re.search(rf"(?<![\w.-]){re.escape(m)}(?![\w.-])", line)], [],
                                 f"the Claude floor refusal names an Opus other than the head it refuses: {line}")
                self.assertEqual(self.records(rd, "calls.jsonl"), [])
                self.assertEqual(self.launched_models(env), [])


class GroupE_FinalReviewCheck(CheckBase):
    """A-opus-1 and A-astra-1 / A-opus-2 through the engine: the fetch child imports nothing from the caller's directory, and a
    retirementAt that is no representable timestamp is a source error, not a traceback."""

    def hostile_checkout(self, name):
        """A caller's working directory (a contributor's branch under review) holding socket.py, ssl.py and http/__init__.py
        that record their own import in a marker file outside it: (the directory, the marker file)."""
        d = Path(os.path.realpath(str(self.case_dir(name))))
        caller, marker = d / "caller-checkout", d / "imported-from-the-callers-directory.txt"
        (caller / "http").mkdir(parents=True, exist_ok=True)
        for rel in ("socket.py", "ssl.py", "http/__init__.py"):
            (caller / rel).write_text(f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n")
        return caller, marker

    @staticmethod
    def imported(marker):
        return marker.read_text().split() if marker.exists() else []

    def test_a_refresh_from_a_checkout_with_socket_ssl_and_http_modules_imports_none_of_them(self):
        # A-opus-1 [P1]: fetch_page ran [sys.executable, "-c", FETCH, ...] with no -I and no cwd=, so the child had the caller's
        # directory first on sys.path: a checkout under review (bridge.py runs "from the project directory") with a top-level
        # socket.py, ssl.py or http/__init__.py ran that code outside any sandbox, with the user's whole environment, on every
        # refresh (and the benign variant made every page fetch fail). The ruling: every Python child runs with -I (no cwd or
        # script directory on sys.path, no PYTHON* variables, no user site) and cwd= a private directory the bridge owns. A
        # refresh (doctor) run from such a checkout imports none of the three and reads every page
        caller, marker = self.hostile_checkout("fr-a-opus-1")
        env, _ = self.isolated("fr-a-opus-1")
        rc, out, err = self.run_engine("doctor", env_extra=env, cwd=caller)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.imported(marker), [], f"a refresh run from {caller} imported these modules from it:\n{out}{err}")
        sources = self.evidence(env).get("sources") or {}
        for name in ("npm", "deprecations", "overview"):
            attempt = (sources.get(name) or {}).get("attempt") or {}
            self.assertTrue(attempt.get("ok"), f"the {name} page was not read by a refresh run from {caller}: {attempt}")

    def test_the_fetch_child_imports_nothing_from_the_callers_directory_or_pythonpath(self):
        # A-opus-1 [P1], the fetch itself (fetch_page, as Opus's fix describes the test): called in this process from the
        # checkout's directory, and with that directory on PYTHONPATH (-I ignores PYTHON* variables), the child imports none of
        # the checkout's modules and reads the page
        caller, marker = self.hostile_checkout("fr-a-opus-1-fetch")
        bm = import_bridge_models()
        self.pages.set("/fr-a-opus-1-fetch/page", "a page body")
        url = self.pages.url("/fr-a-opus-1-fetch/page")
        here = os.getcwd()
        for on in ("the working directory", "PYTHONPATH"):
            with self.subTest(hostile_modules_on=on):
                if marker.exists():
                    marker.unlink()
                try:
                    if on == "the working directory":
                        os.chdir(str(caller))
                    with patched_environ(**({"PYTHONPATH": str(caller)} if on == "PYTHONPATH" else {})):
                        text, error = bm.fetch_page(url, 20)
                finally:
                    os.chdir(here)
                self.assertEqual(self.imported(marker), [], f"the fetch child imported these modules from {caller} ({on})")
                self.assertEqual((text, error), ("a page body", None))

    def test_a_non_finite_or_out_of_range_retirement_at_is_a_source_error_not_a_traceback(self):
        # A-astra-1 [P2] / A-opus-2 [P2] (C4b-4 was closed only for the tested shapes): entry_ok accepted any retirementAt for
        # which _epoch returned a number, including NaN, +-inf and values beyond datetime's range; evaluate() then crashed in
        # _date_text (ValueError, OverflowError) inside apply_refresh, under the evidence lock: doctor and every model-starting
        # command whose re-check was due exited with a traceback, and nothing was written, so it repeated. The ruling: such a
        # value is no timestamp, and entry_ok turns the entry into the allowlisted catalog source error that keeps the last
        # observation and adds no marker; a new job still runs. The same steps as the C4b-4 test
        # (GroupE_C4bFindings.test_malformed_upgrade_info_fields_are_a_source_error_that_keeps_the_last_observation)
        sol, kept = head("sol"), "gpt-9-nova"
        good = self.catalog_file("fr-retirement-good", lambda ms: ms.append(self.new_entry(kept, hidden=True)))
        blank = self.case_dir("fr-retirement") / "config-read-blank.json"  # no origin and no value: applicable with or without L1
        data = json.loads(CONFIG_DEFAULT.read_text())
        blank.write_text(json.dumps(dict(data, config={k: ({} if isinstance(v, dict) else None) for k, v in data["config"].items()})))
        info = {"model": None, "upgradeCopy": None, "modelLink": None, "migrationMarkdown": None}
        cases = {"NaN (a JSON token)": float("nan"), "Infinity (a JSON token)": float("inf"), "-Infinity (a JSON token)": float("-inf"),
                 "the string 'NaN'": "NaN", "the string '1e400'": "1e400", "1e100": 1e100, "9223372036854775807": 9223372036854775807}
        for i, (case, value) in enumerate(cases.items()):
            with self.subTest(retirementAt=case):
                env, _ = self.isolated(f"fr-retirement-{i}", catalog=good, config=blank)
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(kept, list(strings(self.evidence(env))))
                before = self.marker_ids(env)
                bad = self.catalog_file(f"fr-retirement-bad-{i}",
                                        lambda ms, v=value: self.entry(ms, sol).__setitem__("upgradeInfo", dict(info, retirementAt=v)))
                bad_env = dict(env, STUB_CODEX_CATALOG=str(bad))
                rc, out, err = self.run_engine("doctor", "--json", env_extra=bad_env)
                self.assertNotIn("Traceback", err, f"retirementAt {case} crashed the refresh:\n{err[-1500:]}")
                data = self.json_of(rc, out, err)
                texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
                self.assertEqual((rc, data.get("verdict")), (0, "UNVERIFIED"), texts)
                self.assertTrue(self.unverified_about(data, "codex"), texts)
                self.assertIn(kept, list(strings(self.evidence(env))), "the malformed answer replaced the last observation")
                self.assertEqual(self.marker_ids(env), before, "the malformed entry added a marker")
                rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=bad_env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.launched_models(bad_env)[-1:], [sol])


# ==================================================================================================================
# The re-review of the final-review fix round (review/rulings_rr.md, binding; its findings verbatim with file:line:
# review/final_rr_findings.md), through the engine; each test names its item. The Codex-side entry points (RR-8):
# codex-skills/claude/tests/test_mcp_server.py GroupB_MCPReReview; bootstrap (RR-2, RR-5, RR-9): tests/test_bootstrap.sh
# groupD_rereview_*; the West kit's pin (RR-3): distribution/west-ai-kit/tests/test_sync_codex_sol_pin.py
# ==================================================================================================================
class GroupB_ReReviewLaunchAndStatus(EngineBase):
    """RR-1 (astra-1 = opus-7) and RR-6 (astra-5 = opus-6) through the engine: `initial`-mode validation lasts until a
    candidate's CLI has actually run, and the job list's fallback mark comes from what launched."""

    write_marker = GroupB_LaunchFallbackObservationNotices.write_marker  # a marker in the store's layout, replaced atomically
    WAIT = 10       # seconds (from before the engine starts) a routed seat waits for an account here: a marker is published meanwhile
    REFUSED = 3600  # how far ahead relay exec's refusal puts the refused account's wake_at (it only has to be on stderr)

    def execs(self, rd):
        return [e["label"] for e in self.records(rd, "execs.jsonl")]

    # ---- RR-1 [P1]
    def test_a_marker_published_after_relay_refused_the_first_attempt_refuses_the_next_one(self):
        # RR-1 [P1] (astra-1 = opus-7): relay exec refused the seat's first attempt before the CLI started (a non-zero exit, no
        # thread or session, wake_at on stderr: the stub relay's `refuse`), and that attempt still counted as the launch: the
        # next one, after the account wait, got `continuation` mode, so a marker another check published during the wait became
        # a notice and the marked model launched on the next account. The ruling: until a candidate's CLI has actually run,
        # every attempt of it gets `initial`-mode validation after the account wait (a refusal is allowed: nothing has run).
        # Relay offers a and refuses to exec there, every account is then out until the wake time, then it offers b: the marker
        # lands during the wait, and the seat is refused before b, with no CLI start. Codex and Claude alike
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            model = head(tag)
            with self.subTest(seat=spec):
                job = f"rr-1-first-{tag}"
                env, _ = self.isolated(job)
                renv, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + self.WAIT},
                                                              {"exit": 0, "label": "b"}]},
                                            "refuse": {"a": int(time.time() + self.REFUSED)}})  # BUSY-1 A6: relay's wake times are integers
                env.update(renv)
                p = self.start_engine(env, "ask", "--seats", spec, "--job-dir", job, "--", "q")
                try:
                    self.wait_for(self.jobs / job, tag, lambda s: s.get("state") == "waiting")  # relay refused a: the seat waits
                    self.write_marker(env, model, "deprecated")  # another invocation's check publishes a marker meanwhile
                finally:
                    out, err = p.communicate(timeout=120)
                self.assertEqual(self.launched_models(env), [], f"{model}, marked while the seat waited after relay refused its first "
                                                               f"attempt, launched without the initial-mode validation:\n{out}{err}")
                self.assertEqual(self.execs(rd), ["a"], "relay was asked to exec the CLI on another account")
                seat = self.seat(self.jobs / job, tag)
                self.assertEqual(seat.get("state"), "failed", seat)
                self.assertIn(f"{model} is marked deprecated", seat.get("activity", "") + out)
                self.assertNotEqual(p.returncode, 0, out + err)

    def test_a_marker_published_after_relay_refused_a_fallback_targets_first_attempt_refuses_it(self):
        # RR-1 [P1], the new target, changed for docs/model-routing.md 3.3 and 5 (F3): Astra's only capable account, a, rejects it at
        # startup (account x's catalog lists Sol but not Astra, so Astra never tries x), and the seat descends to Sol through its
        # `astra|sol` list. Sol's capable set is a and x: relay offers x first and its exec refuses the account before the CLI starts,
        # then every account is out until the wake time, then a. `new_candidate` used to be cleared already, so the attempt after the
        # wait got `continuation` mode: a marker published on the Sol head during the wait became a notice and Sol launched. The
        # ruling: `new_candidate` stays set until the target has run, so Sol is refused after the wait and never starts
        astra, sol = head("astra"), head("sol")
        job = "rr-1-fallback"
        env, _ = self.isolated(job, STUB_CODEX_CATALOG_BY_HOME="1")  # the stub app-server answers from <CODEX_HOME>/stub_catalog.json when one exists
        renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "x"},
                                                       {"exit": 1, "wake_at": time.time() + self.WAIT}, {"exit": 0, "label": "a"}]},
                                    "refuse": {"x": int(time.time() + self.REFUSED)}})  # BUSY-1 A6: relay's wake times are integers
        env.update(renv)
        without_astra = self.catalog_file("rr-1-fallback-x", lambda ms: ms.remove(self.entry(ms, astra)))
        (rd / "homes" / "x").mkdir(parents=True, exist_ok=True)  # the stub relay's CODEX_HOME for account x
        shutil.copyfile(str(without_astra), str(rd / "homes" / "x" / "stub_catalog.json"))
        p = self.start_engine(env, "ask", "--seats", "astra|sol:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra}]] q")
        try:
            self.wait_for(self.jobs / job, "astra", lambda s: s.get("state") == "waiting" and s.get("model") == sol)
            self.write_marker(env, sol, "deprecated")
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(self.launched_models(env), [astra], f"the list's target {sol}, marked while it waited after relay refused "
                                                             f"its first attempt, launched without the initial-mode validation:\n{out}{err}")
        self.assertEqual(self.execs(rd), ["a", "x"], "relay was asked to exec the target on another account")
        seat = self.seat(self.jobs / job, "astra")
        self.assertEqual(seat.get("state"), "failed", seat)
        self.assertIn(f"{sol} is marked deprecated", seat.get("activity", "") + out)
        self.assertNotEqual(p.returncode, 0, out + err)

    # ---- ruling BUSY-1 (busy/RULING_BUSY.md), test 7
    def test_busy_refusals_keep_the_fallback_and_a_marked_target_is_still_refused(self):
        # BUSY-1 test 7 (rules 2 and 4, A3): relay refusing a busy account is no attempt: `attempt` stays as it was, so busy refusals
        # spend none of the fallback's allowance (CodexSelection.rejected gives up once `attempt` reaches REPICK_LIMIT +
        # fallback_attempts), and every launch after a busy wait is validated again (RR-1). 1: eight busy refusals on the only
        # account, then Astra rejected at startup there: the seat still descends to Sol through its `astra|sol` list, and Sol runs.
        # 2: the RR-1 fallback case above with the wait a busy one: Astra's only capable account a rejects it, Sol's first exec
        # finds account x busy, and a marker published on the Sol head during that busy wait refuses Sol before it launches
        astra, sol = head("astra"), head("sol")
        with self.subTest(case="eight busy refusals, then the startup rejection"):
            job = "busy-1-fallback"
            env, _ = self.isolated(job)
            renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 8}}})
            env.update(renv)
            rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
            seat = self.seat(self.jobs / job, "astra")
            self.assertEqual((rc, seat.get("state"), seat.get("model")), (0, "done", sol), out + err)
            self.assertEqual(self.launched_models(env), [astra, sol])
            self.assertEqual(self.execs(rd), ["a"] * 10)  # eight busy refusals, Astra (rejected), then Sol
            self.assertEqual((seat.get("attempt"), seat.get("busy_waits"), seat.get("limit_hits")), (2, 8, 0))
        with self.subTest(case="a marker on the fallback target during its busy wait"):
            job = "busy-1-fallback-marker"
            env, _ = self.isolated(job, STUB_CODEX_CATALOG_BY_HOME="1", BRIDGE_LIMIT_WAIT_SHORT="16", BRIDGE_ROUTED_MAX_WAIT="20")  # busy waits of 2.4-5.6 s
            renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "x"}]}, "busy": {"x": {}}})
            env.update(renv)
            without_astra = self.catalog_file("busy-1-fallback-marker-x", lambda ms: ms.remove(self.entry(ms, astra)))
            (rd / "homes" / "x").mkdir(parents=True, exist_ok=True)  # the stub relay's CODEX_HOME for account x
            shutil.copyfile(str(without_astra), str(rd / "homes" / "x" / "stub_catalog.json"))
            p = self.start_engine(env, "ask", "--seats", "astra|sol:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra}]] q")
            try:
                seat = self.wait_for(self.jobs / job, "astra", lambda s: (s.get("state") == "waiting" and s.get("model") == sol)
                                     or s.get("state") in ("done", "failed", "cancelled"))
                self.assertEqual(seat.get("state"), "waiting", f"relay found x busy for {sol}, and the seat did not wait for it: {seat}")
                self.write_marker(env, sol, "deprecated")  # another invocation's check publishes a marker meanwhile
            finally:
                out, err = p.communicate(timeout=120)
            self.assertEqual(self.launched_models(env), [astra], f"the list's target {sol}, marked while it waited out a busy account, launched "
                                                                 f"without the initial-mode validation:\n{out}{err}")
            self.assertEqual(self.execs(rd), ["a", "x"])
            seat = self.seat(self.jobs / job, "astra")
            self.assertEqual((seat.get("state"), seat.get("limit_hits")), ("failed", 0), seat)
            self.assertIn(f"{sol} is marked deprecated", seat.get("activity", "") + out)
            self.assertNotEqual(p.returncode, 0, out + err)

    # ---- RR-6 [P3]
    def test_the_job_list_marks_a_fallback_only_when_its_target_launched(self):
        # RR-6 [P3] (astra-5 = opus-6): fallback_marks built `requested -> ran` from the seat's model, which run_codex_seat moves to
        # the list's target as soon as the descent is chosen, before the target launches. In B-astra-1's scenario (the target
        # refused by the launch check after the account wait: GroupB_FinalReviewLaunchAndOutputs
        # .test_a_marker_published_while_a_fallback_target_waits_for_an_account_refuses_it) only Astra ran, yet `status` listed
        # the job as "(fell back: astra: <the Astra head> -> <the Sol head>)"; the same when relay then has no account for the target.
        # The ruling: the mark comes from what actually launched (the model recorded when a CLI starts), never from the seat's
        # model. One jobs root holds both jobs whose Sol never launched and one whose Sol ran: the job list (`status`, and
        # `status --all`) marks the last one only. Every seat carries the `astra|sol` list (docs/model-routing.md 5)
        astra, sol = head("astra"), head("sol")
        jobs = self.case_dir("rr-6") / "jobs"
        prompt = f"[[STUB:badmodel={astra}]] q"
        # 1. B-astra-1's scenario: Astra rejected on a and (after the wait) on b, then Sol is refused by the marker before its launch
        refused = "rr-6-refused-after-the-wait"
        env1, _ = self.isolated(refused, BRIDGE_JOBS=str(jobs))
        renv, _ = self.relay(refused, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + self.WAIT},
                                                          {"exit": 0, "label": "b"}]}})
        p = self.start_engine(dict(env1, **renv), "ask", "--seats", "astra|sol:low", "--job-dir", refused, "--", prompt)
        try:
            self.wait_for(jobs / refused, "astra", lambda s: s.get("state") == "waiting")
            self.write_marker(env1, sol, "deprecated")
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual((p.returncode != 0, self.launched_models(env1)), (True, [astra, astra]), out + err)  # only Astra ran
        # 2. relay has no account for Sol within the seat's wait budget: the target never launches either
        no_account = "rr-6-no-account-for-the-target"
        env2, _ = self.isolated(no_account, BRIDGE_JOBS=str(jobs))
        renv, _ = self.relay(no_account, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + 100000}]}})
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", no_account, "--", prompt, env_extra=dict(env2, **renv))
        self.assertEqual((rc != 0, self.launched_models(env2)), (True, [astra]), out + err)
        # 3. the descent that ran (unrouted): Sol answered
        ran = "rr-6-fell-back-and-ran"
        env3, _ = self.isolated(ran, BRIDGE_JOBS=str(jobs))
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", ran, "--", prompt, env_extra=env3)
        self.assertEqual((rc, self.launched_models(env3)), (0, [astra, sol]), out + err)
        mark = re.compile(re.escape(astra) + r"\s*->\s*" + re.escape(sol))
        for argv in (["status"], ["status", "--all"]):
            with self.subTest(view=" ".join(argv)):
                rc, out, err = self.run_engine(*argv, env_extra=env3)
                self.assertEqual(rc, 0, out + err)
                rows = {job: [line for line in out.splitlines() if line.split()[:1] == [job]] for job in (refused, no_account, ran)}
                self.assertEqual([len(r) for r in rows.values()], [1, 1, 1], f"one job-list line per job:\n{out}")
                for job in (refused, no_account):
                    self.assertFalse("fell back" in rows[job][0] or mark.search(rows[job][0]),
                                     f"the job list marks a fallback to {sol}, which never launched ({job}):\n{out}")
                self.assertTrue("fell back" in rows[ran][0] and mark.search(rows[ran][0]),
                                f"the job list no longer marks the fallback that ran ({ran}):\n{out}")


class GroupB_SecondReReviewDefaults(EngineBase):
    """S-DEF (review/rulings_rr2.md, binding; review/rr2_findings.md: RR2-astra-2, RR2-astra-3 and Opus's note on inherited
    variables) through the engine: the Codex-side entry points' built-in seat lists travel as BRIDGE_BUILTIN_DEFAULT_SEAT,
    BRIDGE_BUILTIN_REVIEW_SEATS and BRIDGE_BUILTIN_COUNCIL_SEATS; each effective default list is the user-facing variable if
    set, else the BRIDGE_BUILTIN_* one, else the engine's own; the models check validates every effective list and labels its
    origin; a user's own setting keeps its name; no model child inherits the built-in lists. Through the entry points and the
    MCP server: codex-skills/claude/tests/test_mcp_server.py GroupB_MCPSecondReReview"""

    BUILTINS = {"BRIDGE_BUILTIN_DEFAULT_SEAT": "fable:max", "BRIDGE_BUILTIN_REVIEW_SEATS": "opus:max",
                "BRIDGE_BUILTIN_COUNCIL_SEATS": "fable:max,opus:max"}  # what the Codex-side entry points pass (S-DEF)
    SUPPLIED = {"BRIDGE_DEFAULTS_SUPPLIED": "BRIDGE_DEFAULT_SEAT,BRIDGE_REVIEW_SEATS,BRIDGE_COUNCIL_SEATS"}  # left by an entry point before S-DEF
    USER_VARS = re.compile(r"\bBRIDGE_(?:DEFAULT_SEAT|REVIEW_SEATS|COUNCIL_SEATS)\b")

    def table_without_max(self):
        """The table under test with `max` gone from the Fable and Opus heads' efforts (a reviewed table update may do that):
        the entry points' built-in lists (fable:max, opus:max) are refused, and so are the engine's own review and council
        lists (their opus:max); its own default seat (astra:ultra) is not."""
        def mutate(t):
            for family in ("fable", "opus"):
                t["models"][head(family)]["efforts"] = [e for e in efforts_of(head(family)) if e != "max"]
        return self.table_file("s-def-without-max", mutate)

    @staticmethod
    def defaults_findings(data):
        """The texts of a `doctor --json` verdict's `defaults` findings."""
        return [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict) and f.get("class") == "defaults"]

    def assert_built_in(self, texts, seats, lists, where):
        """`texts` report at least `lists` refused default lists and name each of `seats` between them; each is labelled
        "built-in default" and none is attributed to a user-facing variable (the user set none)."""
        shown = "\n".join(texts)
        self.assertGreaterEqual(len(texts), lists, f"{where}: not every effective default list is checked:\n{shown}")
        for seat in seats:
            self.assertTrue([t for t in texts if seat in t], f"{where}: no defaults finding refuses {seat}:\n{shown}")
        for t in texts:
            self.assertIn("built-in default", t, f"{where}: a built-in list's finding does not say it is the built-in default")
            self.assertNotRegex(t, self.USER_VARS, f"{where}: a built-in list is attributed to a variable the user never set")

    def test_the_check_validates_every_built_in_default_list_and_labels_it(self):
        # S-DEF (RR2-astra-2 [P2]): local_facts dropped the lists a Codex-side entry point supplied (RR-8's _user_set filter), so
        # the models check stopped validating them: with the entry point's built-ins and a table whose Fable and Opus heads lack
        # `max`, doctor and the composed verdict had no `defaults` finding (three before RR-8), although ask, review and council
        # refuse those very lists. The ruling: the check (local findings, doctor, the composed verdict and the gate) validates
        # EVERY effective default list, whatever its origin, and labels a built-in one "built-in default". The origins here: the
        # entry point's BRIDGE_BUILTIN_* lists, and the engine's own constants when neither those nor the user's are set
        table = self.table_without_max()
        origins = (("entry-point", self.BUILTINS, ("fable:max", "opus:max"), 3),  # each of the three lists is refused
                   ("engine", {}, ("opus:max",), 2))                              # review and council (opus:max); astra:ultra passes
        for origin, builtins, seats, lists in origins:
            with self.subTest(built_in_lists_of=origin):
                job = f"s-def-check-{origin}"
                env, _ = self.isolated(job, table=table, **builtins)
                rc, data, err = self.doctor_json(env)
                self.assertEqual((rc, data.get("verdict")), (1, "FAIL"), f"doctor --json: {data.get('findings')}\n{err}")
                texts = self.defaults_findings(data)
                self.assert_built_in(texts, seats, lists, "doctor --json")
                rc, out, err = self.doctor(env)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual([t for t in texts if t not in out], [], f"doctor leaves out defaults findings:\n{out}")
                # the composed verdict of a new job under `warn` (its seats are explicit: the check is about the defaults)
                rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", job, "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual((self.meta(self.jobs / job).get("check") or {}).get("verdict"), "FAIL")
                lead = leading_notices(out)
                self.assertTrue(lead and lead[0].startswith("NOTICE: models: check FAILED (") and "built-in default" in lead[0], out)
                # and the gate under `refuse`: no job, and its FAIL lines carry the findings
                before = self.job_names()
                rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--", "q", env_extra=dict(env, BRIDGE_MODELS_GATE="refuse"))
                self.assertEqual(rc, 2, out + err)
                self.assertEqual(self.new_jobs(before), [])
                self.assertEqual([t for t in texts if t not in err], [], f"the gate's refusal leaves out defaults findings:\n{err}")

    def test_a_users_own_default_list_keeps_its_name_whatever_is_inherited(self):
        # S-DEF (RR2-astra-3 [P3]): BRIDGE_DEFAULTS_SUPPLIED was trusted by name and reached every child, so a process under a
        # Codex-side entry point that set its own BRIDGE_REVIEW_SEATS=spark was refused as "refused seat 'spark': ..." with its
        # variable unnamed, and the check dropped the setting; the marker could hide any checked name, a host override too.
        # The ruling: the built-in lists travel as BRIDGE_BUILTIN_*, BRIDGE_DEFAULTS_SUPPLIED is gone, and a user-facing variable
        # that is set is the user's: named in the refusal and checked under its name, whatever the environment inherited (the
        # entry point's BRIDGE_BUILTIN_* lists, or the provenance variable an entry point before this ruling left behind)
        cases = (("BRIDGE_REVIEW_SEATS", "spark", ["review"], "luna", "spark"),
                 ("BRIDGE_DEFAULT_SEAT", "5.5", ["ask", "--", "q"], "sol", "5.5"),
                 ("BRIDGE_COUNCIL_SEATS", f"astra:low,{REFUSED_GPT6}", ["council", "--", "q"], "astra", REFUSED_GPT6))
        inherited = (("BRIDGE_BUILTIN_*", self.BUILTINS), ("BRIDGE_BUILTIN_* and BRIDGE_DEFAULTS_SUPPLIED", dict(self.BUILTINS, **self.SUPPLIED)))
        for i, (var, value, argv, family, refused) in enumerate(cases):
            for j, (name, extra) in enumerate(inherited):
                with self.subTest(variable=var, inherited=name):
                    env, _ = self.isolated(f"s-def-user-{i}-{j}", **extra, **{var: value})
                    before = self.job_names()
                    rc, out, err = self.run_engine(*argv, env_extra=env)
                    line = self.assert_refused(rc, out, err)
                    self.assertIn(f"({var})", line, f"the refusal of the user's own {var} does not name it")
                    self.assertIn(f"Use {family} ({head(family)}).", line)
                    self.assertEqual(self.new_jobs(before), [])
                    rc, data, err = self.doctor_json(env)
                    self.assertEqual(data.get("verdict"), "FAIL", data.get("findings"))
                    self.assertTrue([t for t in self.defaults_findings(data) if var in t and refused in t],
                                    f"the check does not report the user's {var} under its name: {self.defaults_findings(data)}")
        # the removed provenance variable hides no other checked name either: a host override it names is still reported
        env, _ = self.isolated("s-def-host", ANTHROPIC_DEFAULT_OPUS_MODEL=K1_ID, BRIDGE_DEFAULTS_SUPPLIED="ANTHROPIC_DEFAULT_OPUS_MODEL")
        rc, data, err = self.doctor_json(env)
        self.assertTrue([f for f in data.get("findings") or [] if f.get("class") == "host" and "ANTHROPIC_DEFAULT_OPUS_MODEL" in (f.get("text") or "")],
                        f"a BRIDGE_DEFAULTS_SUPPLIED naming a host override hides it from the check: {data.get('findings')}")

    def test_no_model_child_inherits_the_built_in_lists(self):
        # S-DEF (Opus's note on inherited variables): os.environ.copy() and seat_env() kept the entry point's lists (then with
        # BRIDGE_DEFAULTS_SUPPLIED) in every seat CLI's environment, so an engine run underneath a seat took them for its own.
        # The ruling: every model child (each Codex and Claude seat CLI, and relay exec's children) drops BRIDGE_BUILTIN_DEFAULT_SEAT,
        # BRIDGE_BUILTIN_REVIEW_SEATS and BRIDGE_BUILTIN_COUNCIL_SEATS, so a nested engine uses its own defaults. The stubs record
        # the BRIDGE_* variables they see (`bridge_env` in their argv record); unrouted, then under the stub relay's exec
        for routed in (False, True):
            with self.subTest(routed=routed):
                job = f"s-def-children-{'routed' if routed else 'unrouted'}"
                env, cd = self.isolated(job, **self.BUILTINS)
                if routed:
                    renv, _ = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 0, "label": "a"}]}})
                    env.update(renv)
                rec = cd / "argv.jsonl"
                rc, out, err = self.run_engine("council", "--seats", "luna:low,sonnet:low", "--job-dir", job, "--", f"[[STUB:argv={rec}]] q",
                                               env_extra=env)
                self.assertEqual(rc, 0, out + err)
                records = self.read_records(rec)
                self.assertEqual(sorted(r["model"] for r in records), sorted([head("luna"), head("sonnet")]), out + err)
                self.assertEqual([r["account"] for r in records], ["a", "a"] if routed else [None, None])
                for r in records:
                    leaked = sorted(k for k in r["bridge_env"] if k.startswith("BRIDGE_BUILTIN_"))
                    self.assertEqual(leaked, [], f"the {r['model']} seat's CLI inherited the built-in lists")


class GroupB_SecondReReviewLaunch(EngineBase):
    """S-LAUNCH (review/rulings_rr2.md, binding; review/rr2_findings.md: RR2-astra-4, RR2-opus-2 and Opus's note on
    `first_launch_at`) through the engine: an attempt has STARTED on positive evidence that its CLI began (unrouted: the process
    spawned; routed: the first vendor event, or a thread or session id). The first evidence sets a per-attempt `started` flag
    that later exceptions never clear and, at that moment, records the model in `models_launched` and stamps `first_launch_at`
    if unset; RR-1's validation mode and RR-6's fallback mark follow that flag."""

    write_marker = GroupB_LaunchFallbackObservationNotices.write_marker  # a marker in the store's layout, replaced atomically
    WAIT = 10  # seconds (from before the engine starts) a routed seat waits for an account here: a marker is published meanwhile

    def job_line(self, env, job):
        """`job`'s line in the job list (`status`), or ''."""
        rc, out, err = self.run_engine("status", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        return next((line for line in out.splitlines() if line.split()[:1] == [job]), "")

    @staticmethod
    def marks(line, requested, ran):
        """Whether a job-list line marks the fallback `requested -> ran`."""
        return "fell back" in line and re.search(re.escape(requested) + r"\s*->\s*" + re.escape(ran), line) is not None

    def wait_file(self, path, engine, tries=600):
        """Wait until a stub's readiness file exists; fail if the engine exits first or it never appears."""
        for _ in range(tries):
            if Path(path).exists():
                return
            if engine.poll() is not None:
                self.fail(f"the engine exited before {Path(path).name} appeared:\n" + "".join(engine.communicate()))
            time.sleep(0.1)
        self.fail(f"{path} never appeared")

    def test_a_fallback_targets_exec_that_fails_before_its_cli_starts_is_no_launch(self):
        # S-LAUNCH (RR2-opus-2 [P3]): an attempt that relay did not refuse (no wake_at) and that raised nothing counted as a launch,
        # but `relay exec` can also end before the CLI starts without wake_at: `could not run codex: ...` after its pre-exec line,
        # or a registry it cannot load. Astra is rejected at startup on account a and re-picked for b (docs/model-routing.md 3.3),
        # where relay exec fails that way (exit 1, no wake_at) and never runs the CLI. The seat fails; nothing started on b, so only
        # the first launch is recorded and the job list shows no fallback mark
        astra, sol = head("astra"), head("sol")
        failures = (("could-not-run", "[relay] running under account b\nrelay: could not run codex: [Errno 8] Exec format error: 'codex'\n"),
                    ("registry", "Traceback (most recent call last):\n  File \"relay.py\", line 4484, in cmd_exec\n"
                                 "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)\n"))
        for name, stderr in failures:
            with self.subTest(relay_exec_fails=name):
                job = f"s-launch-exec-fails-{name}"
                jobs = self.case_dir(job) / "jobs"
                env, _ = self.isolated(job, BRIDGE_JOBS=str(jobs))
                renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]},
                                            "exec_fail": {"b": {"stderr": stderr, "exit": 1}}})
                env.update(renv)
                rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b"], out + err)  # Sol's exec was tried on b
                self.assertEqual(self.launched_models(env), [astra], "the fallback's CLI started")                   # relay failed before it
                seat = self.seat(jobs / job, "astra")
                self.assertEqual((rc, seat.get("state")), (1, "failed"), out + err)
                self.assertNotIn(sol, seat.get("models_launched") or [], f"{sol} never started, yet it is recorded as launched")
                line = self.job_line(env, job)
                self.assertTrue(line, "the job list has no line for the job")
                self.assertFalse("fell back" in line or self.marks(line, astra, sol), f"the job list marks a fallback to {sol}, which never started:\n{line}")

    def test_status_marks_the_fallback_while_its_target_is_blocked_after_its_first_event(self):
        # S-LAUNCH (RR2-astra-4 [P3]): _record_launched ran only after the attempt returned, so while Sol, Astra's fallback, ran
        # (its thread had started) models_launched held Astra alone and the job list showed no fallback for the whole attempt.
        # The ruling: the launch is recorded at the first evidence that the CLI started, not after the attempt returns. Sol's
        # stub emits thread.started, then holds (its readiness file <hold>.ready; it goes on once <hold>.release exists), and the
        # job list already marks the fallback: routed (the evidence is that first event) and unrouted (the spawn)
        astra, sol = head("astra"), head("sol")
        for routed in (True, False):
            with self.subTest(routed=routed):
                job = f"s-launch-blocked-{'routed' if routed else 'unrouted'}"
                cd = self.case_dir(job)
                env, _ = self.isolated(job, BRIDGE_JOBS=str(cd / "jobs"))
                if routed:
                    renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
                    env.update(renv)
                hold = cd / "hold"
                p = self.start_engine(env, "ask", "--seats", "astra|sol:low", "--job-dir", job, "--", f"[[STUB:badmodel={astra},hold={hold}]] q")
                line = ""
                try:
                    self.wait_file(f"{hold}.ready", p)  # Sol's first event is out, and its stub holds
                    self.wait_for(cd / "jobs" / job, "astra", lambda s: s.get("model") == sol and s.get("thread_id"))  # the engine read it
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:  # each look is one `status` run
                        line = self.job_line(env, job)
                        if self.marks(line, astra, sol) or p.poll() is not None:
                            break
                    self.assertIsNone(p.poll(), "the attempt ended before the test released it")
                    self.assertTrue(self.marks(line, astra, sol), f"while {sol} runs, blocked after its first event, the job list does not "
                                                                   f"mark the fallback:\n{line}")
                finally:
                    Path(f"{hold}.release").touch()
                    out, err = p.communicate(timeout=120)
                # routed, Astra is re-picked for account b and rejected there too before the list's descent (docs/model-routing.md 3.3)
                self.assertEqual((p.returncode, self.launched_models(env)), (0, [astra, astra, sol] if routed else [astra, sol]), out + err)
                self.assertTrue(self.marks(self.job_line(env, job), astra, sol), "the job list lost the mark once the fallback finished")

    def test_an_exception_after_the_cli_started_keeps_the_launch_recorded(self):
        # S-LAUNCH (RR2-astra-4 [P3], RR2-opus-2 [P3]): both vendor helpers set launch_error for ANY exception inside an attempt,
        # also one raised after the CLI started (handling its events, writing the seat record), and such an attempt was never
        # recorded: a fallback that did run was left out of models_launched and off the job list. The ruling: the `started`
        # flag, once set, survives later exceptions. The stub starts (Codex: thread.started; Claude: its init event), then prints
        # valid JSON that is not an event object, and the engine's handling of it raises: the seat fails, and the launch stays
        # recorded. Codex (Astra's fallback to Sol: the job list marks it) and Claude (the seat's own model), unrouted and routed
        astra, sol, sonnet = head("astra"), head("sol"), head("sonnet")
        usage = {"input_tokens": 2, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 0, "output_tokens": 10}
        stream = [{"type": "system", "subtype": "init", "session_id": "stub-session-exception", "model": sonnet}, ["not", "an", "event"],
                  {"type": "assistant", "parent_tool_use_id": None, "message": {"id": "msg_1", "model": sonnet, "usage": usage,
                                                                                 "content": [{"type": "text", "text": "OK"}]}},
                  {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "session_id": "stub-session-exception",
                   "usage": usage, "modelUsage": {sonnet: {"inputTokens": 10, "outputTokens": 5, "contextWindow": 1000000}}}]
        for vendor, spec, tag, model in (("codex", "astra|sol:low", "astra", sol), ("claude", "sonnet:low", "sonnet", sonnet)):
            for routed in (False, True):
                with self.subTest(vendor=vendor, routed=routed):
                    job = f"s-launch-exception-{vendor}-{'routed' if routed else 'unrouted'}"
                    cd = self.case_dir(job)
                    env, _ = self.isolated(job, BRIDGE_JOBS=str(cd / "jobs"))
                    if routed:
                        renv, _ = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
                        env.update(renv)
                    if vendor == "codex":
                        prompt = f"[[STUB:badmodel={astra},badevent]] q"
                    else:  # the Claude stub replays its stream from a file: init, the line that is not an event, the rest
                        (cd / "stream.jsonl").write_text("".join(json.dumps(e) + "\n" for e in stream))
                        prompt = f"[[STUB:replay={cd / 'stream.jsonl'}]] q"
                    rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", prompt, env_extra=env)
                    seat = self.seat(cd / "jobs" / job, tag)
                    self.assertEqual((rc, seat.get("state")), (1, "failed"), "the attempt did not end on an exception after its CLI started "
                                                                            f"(the stub's line that is not an event):\n{out}{err}")
                    self.assertEqual(self.launched_models(env)[-1:], [model], out + err)  # its CLI ran
                    self.assertIn(model, seat.get("models_launched") or [], f"{model} started, then the attempt raised: its launch is not recorded")
                    if vendor == "codex":
                        line = self.job_line(env, job)
                        self.assertTrue(self.marks(line, astra, sol), f"the job list does not mark the fallback to {sol}, which ran:\n{line}")

    def test_a_relay_refused_spawn_stamps_no_first_launch_at(self):
        # S-LAUNCH (Opus's note on `first_launch_at`): _mark_launched stamped first_launch_at as soon as the attempt's process
        # spawned, and for a routed attempt that process is `relay exec`, which may refuse the account before any CLI runs
        # (bridge_models.validate's docstring says the validation mode follows first_launch_at). The ruling: it is stamped at the
        # first evidence that the CLI started. Relay refuses its only account (wake_at on stderr), then has none within the
        # seat's wait budget: the seat fails with nothing started, and first_launch_at stays null. Codex and Claude alike
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            with self.subTest(seat=spec):
                job = f"s-launch-refused-{tag}"
                env, _ = self.isolated(job)
                renv, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + 100000}]},
                                            "refuse": {"a": int(time.time() + 3600)}})  # BUSY-1 A6: relay's wake times are integers
                env.update(renv)
                rc, out, err = self.run_engine("ask", "--seats", spec, "--job-dir", job, "--", "q", env_extra=env)
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"], out + err)  # relay refused a
                self.assertEqual(self.launched_models(env), [], "a CLI started")
                seat = self.seat(self.jobs / job, tag)
                self.assertEqual((rc != 0, seat.get("state"), seat.get("models_launched") or []), (True, "failed", []), out + err)
                self.assertIn("first_launch_at", seat)
                self.assertIsNone(seat["first_launch_at"], "relay refused the only spawn before any CLI ran, yet first_launch_at is stamped")

    def test_busy_refusals_stamp_no_first_launch_at_and_keep_initial_validation(self):
        # ruling BUSY-1 (busy/RULING_BUSY.md) test 8 (rule 2, S-LAUNCH, RR-1): relay finds the only account busy at the seat's first
        # exec, so no CLI starts: first_launch_at stays null and nothing is recorded as launched, and the attempt after the busy wait
        # is still the candidate's first, validated in `initial` mode: a marker published during the busy wait refuses the launch
        # there. Codex and Claude alike
        for spec, vendor in (("luna:low", "codex"), ("sonnet:low", "claude")):
            tag = spec.split(":")[0]
            model = head(tag)
            with self.subTest(seat=spec):
                job = f"busy-8-first-launch-{tag}"
                env, _ = self.isolated(job, BRIDGE_LIMIT_WAIT_SHORT="16", BRIDGE_ROUTED_MAX_WAIT="20")  # busy waits of 2.4-5.6 s
                renv, rd = self.relay(job, {"pick": {vendor: [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 1}}})
                env.update(renv)
                p = self.start_engine(env, "ask", "--seats", spec, "--job-dir", job, "--", "q")
                try:
                    seat = self.wait_for(self.jobs / job, tag, lambda s: s.get("state") in ("waiting", "done", "failed", "cancelled"))
                    self.assertEqual(seat.get("state"), "waiting", f"relay found the account busy, and the seat did not wait for it: {seat}")
                    self.assertIsNone(seat.get("first_launch_at"), "a busy refusal stamped first_launch_at")
                    self.write_marker(env, model, "deprecated")  # another invocation's check publishes a marker meanwhile
                finally:
                    out, err = p.communicate(timeout=120)
                self.assertEqual(self.launched_models(env), [], f"{model}, marked while the seat waited out a busy account, launched without "
                                                                f"the initial-mode validation:\n{out}{err}")
                self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
                seat = self.seat(self.jobs / job, tag)
                self.assertEqual((seat.get("state"), seat.get("first_launch_at"), seat.get("models_launched") or []), ("failed", None, []), out + err)
                self.assertIn(f"{model} is marked deprecated", seat.get("activity", "") + out)
                self.assertNotEqual(p.returncode, 0, out + err)

    def test_a_routed_attempt_without_any_event_leaves_the_next_attempt_in_initial_mode(self):
        # S-LAUNCH (RR-1's validation mode follows the `started` flag): a routed attempt whose CLI printed no event and started no
        # thread (the stub's stderr-only usage limit on account a) has not started, so the candidate keeps `initial` validation:
        # a marker published while the seat waits for the next account refuses it there, before anything else launches (today
        # that attempt counts as a run: `continuation` mode, a notice, and the marked model launches on b). A limit after the CLI
        # started keeps `continuation` mode: GroupB_LaunchFallbackObservationNotices
        # .test_a_marker_learned_while_a_retry_waits_is_a_notice_before_it_launches
        luna = head("luna")
        job = "s-launch-no-event"
        env, cd = self.isolated(job)
        renv, rd = self.relay(job, {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 1, "wake_at": time.time() + self.WAIT},
                                                       {"exit": 0, "label": "b"}]}})
        env.update(renv)
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", job, "--", f"[[STUB:limit={cd / 'limit'}]] q")
        try:
            self.wait_for(self.jobs / job, "luna", lambda s: s.get("state") == "waiting")  # the limit on a: the seat waits for b
            self.write_marker(env, luna, "deprecated")
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(self.launched_models(env), [luna], f"{luna}, marked while the seat waited after an attempt that never started, "
                                                            f"launched again without the initial-mode validation:\n{out}{err}")
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"], "relay was asked to exec the CLI on another account")
        seat = self.seat(self.jobs / job, "luna")
        self.assertEqual(seat.get("state"), "failed", seat)
        self.assertIn(f"{luna} is marked deprecated", seat.get("activity", "") + out)
        self.assertNotEqual(p.returncode, 0, out + err)


# ==================================================================================================================
# DESIGN §7: the model-selection scan (commit 4 with group F; it only READS skills/triad/scripts/triad.py)
# ==================================================================================================================
class GroupF_ModelSelectionScan(EngineBase):
    """DESIGN §7: model-selection constants (default seat strings, triad DEFAULTS, SEATS_DESC defaults), frontmatter `model:`
    fields and the launch arguments the stubs capture name only current models: family names, heads or a head's listed
    spelling. Explanatory text, comments, prompts and test strings are not scanned."""

    PLUGIN = TESTS.parents[2]                                   # claude-codex-bridge/
    TRIAD = TESTS.parents[3] / "skills" / "triad" / "scripts" / "triad.py"
    SHIM = TESTS.parents[2] / "codex-skills" / "claude" / "scripts" / "bridge.py"
    MCP_SERVER = TESTS.parents[2] / "codex-skills" / "claude" / "scripts" / "claude_mcp_server.py"

    @staticmethod
    def current(name):
        """The head a seat name selects when it is a family name, a head or a head's listed spelling; else None."""
        n = re.sub(r"\[1m\]$", "", name.strip().lower())
        vendor = None
        if n.startswith(("codex/", "claude/")):
            vendor, n = n.split("/", 1)
        for fam in families():
            if vendor and vendor_of(fam) != vendor:
                continue
            h = head(fam)
            if n in (fam, h) or n in TABLE["models"][h]["aliases"]:
                return h
        return None

    @staticmethod
    def names(spec):
        return [p.strip().split("@")[0].split(":")[0] for p in spec.split(",") if p.strip()]

    def test_model_selection_constants_frontmatter_and_launch_arguments_name_current_models(self):
        heads = {head(f) for f in families()}
        # default seat strings: every quoted seat-list literal in the engine, the Codex-side entry point and the MCP server
        effort = r"(?::(?:low|medium|high|xhigh|max|ultra))?(?:@fast)?"
        literal = re.compile(r'"([a-z][a-z0-9.\-/\[\]]*' + effort + r'(?:,[a-z][a-z0-9.\-/\[\]]*' + effort + r')*)"')
        for path in (ENGINE, self.SHIM, self.MCP_SERVER):
            seat_lists = [s for s in literal.findall(path.read_text()) if re.search(r":(?:low|medium|high|xhigh|max|ultra)\b", s)]
            self.assertTrue(seat_lists, f"no default seat list found in {path}")
            for spec in seat_lists:
                for name in self.names(spec):
                    with self.subTest(constant=str(path.relative_to(self.PLUGIN)), name=name):
                        self.assertIsNotNone(self.current(name), f"{path}: a default seat list names {name!r}")
        # triad DEFAULTS: relay workers are Claude models and never the K1 seat; the critic is a bridge seat
        import ast
        tree = ast.parse(self.TRIAD.read_text())
        defaults = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                        and any(getattr(t, "id", None) == "DEFAULTS" for t in node.targets))
        values = {kw.arg: kw.value.value for kw in defaults.keywords if isinstance(kw.value, ast.Constant)}
        for role in ("architect", "tester", "critic"):
            for name in self.names(values[role]):
                with self.subTest(triad=role, name=name):
                    self.assertIsNotNone(self.current(name), f"triad DEFAULTS[{role!r}] names {name!r}")
                    if role != "critic":
                        self.assertTrue(self.current(name).startswith("claude-"), name)
                        self.assertNotEqual(name, TABLE["k1_seat"]["seat"])
        # SEATS_DESC and the tool descriptions' defaults and examples
        text = self.MCP_SERVER.read_text()
        descs = re.findall(r'SEATS_DESC = \((.*?)\)\n', text, re.S) + re.findall(r'"description": ("[^\n]*)', text)
        specs = re.findall(r"\b([a-z][a-z0-9.\-]*):(?:low|medium|high|xhigh|max|ultra)\b", "\n".join(descs))
        self.assertTrue(specs, "no seat defaults found in the MCP descriptions")
        for name in specs:
            with self.subTest(description_seat=name):
                self.assertIsNotNone(self.current(name), f"an MCP description names {name!r}")
        # frontmatter `model:` fields (agents keep the family alias `opus`, DESIGN §7) and the reviewer's `_model` line
        docs = sorted((self.PLUGIN / "agents").glob("*.md")) + sorted(self.PLUGIN.glob("skills/*/SKILL.md")) \
            + sorted(self.PLUGIN.glob("codex-skills/*/SKILL.md")) + sorted((TESTS.parents[3] / "skills" / "triad").rglob("*.md"))
        for doc in docs:
            front = re.match(r"---\n(.*?)\n---\n", doc.read_text(), re.S)
            for value in re.findall(r"(?m)^model:\s*(\S+)\s*$", front.group(1) if front else ""):
                with self.subTest(frontmatter=str(doc.relative_to(TESTS.parents[3]))):
                    self.assertIn(value, TABLE["families"]["claude"], f"{doc}: model: {value}")
        self.assertIn('"_model": "opus"', (self.PLUGIN / "agents" / "opus-reviewer.md").read_text())
        # launch arguments captured by the stubs for every default seat list and the Codex-side entry point
        env, _ = self.isolated("scan-launches")
        for argv in (["ask", "--", "q"], ["council", "--", "q"], ["review"]):
            with self.subTest(defaults=argv[0]):
                rc, out, err = self.run_engine(*argv, env_extra=env)
                self.assertEqual(rc, 0, out + err)
        full = without_git_overrides(os.environ)
        for name in ISOLATED_NAMES:
            full.pop(name, None)
        full.update(self.env)
        full.update(env)
        full["CLAUDE_BRIDGE_ENGINE"] = str(ENGINE)  # the shim honours this before its own engine: pin the engine under review
        r = subprocess.run([sys.executable, str(self.SHIM), "ask", "--", "q"], capture_output=True, text=True, env=full,
                           cwd=str(self.repo), stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        launched = self.launched_models(env)
        self.assertTrue(launched)
        self.assertEqual(sorted(set(launched) - heads), [], "a default launched a model that is not a current head")
        self.assertIn(head("fable"), launched)  # the Codex-side default seat


class FifoCanaries:
    """Named pipes standing in for files nothing may read (DESIGN §5: no models_cache.json, no auth.json or credential).
    A writer thread waits on each; any open() for reading by the process under test completes the handshake and is
    recorded. close() releases the writers still waiting and returns the paths that were opened."""

    def __init__(self, paths):
        self.paths = [Path(p) for p in paths]
        self.opened = []
        self.closing = False
        self.lock = threading.Lock()
        self.threads = []
        for p in self.paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(p)
            t = threading.Thread(target=self._writer, args=(p,), daemon=True)
            t.start()
            self.threads.append(t)

    def _writer(self, p):
        try:
            fd = os.open(p, os.O_WRONLY)  # blocks until someone opens the pipe for reading
        except OSError:
            return
        with self.lock:
            if not self.closing:
                self.opened.append(str(p))
        try:
            os.write(fd, b'{"canary": "this file must never be read"}\n')
        except OSError:
            pass
        os.close(fd)

    def close(self):
        with self.lock:
            self.closing = True
        for p in self.paths:
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
                time.sleep(0.05)
                os.close(fd)
            except OSError:
                pass
        for t in self.threads:
            t.join(timeout=2)
        return list(self.opened)



class GroupP_AutoPromotion(CheckBase):
    """DECISIONS A28 (Kevin, 2026-11-07): when this Mac's own signed-in Codex catalog lists a newer GPT model of a family, the check
    makes it that family's head: a step in the promotions ledger beside the evidence store (never models.json), announced on stderr,
    shown by doctor, named by a notice on the jobs that run it for a week, and reversible with `doctor --undo-promotion`. The engine
    tests switch promotion off (BRIDGE_MODELS_PROMOTE=off); these switch it back on (None: the variable removed, the default)."""

    NEW = "gpt-9-terra"  # fictional (a fixture id of the registry's lint)

    def promoting(self, name, mutate=None, **extra):
        """An isolated case whose stub catalog also lists NEW (visible, every Codex effort) unless `mutate` says otherwise."""
        catalog = self.catalog_file(name, mutate or (lambda ms: ms.append(self.new_entry(self.NEW))))
        env, d = self.isolated(name, catalog=catalog, **extra)
        env.setdefault("BRIDGE_MODELS_PROMOTE", None)
        return env, d

    def ledger_path(self, env):
        return Path(env["BRIDGE_MODELS_EVIDENCE"]).with_name("models-evidence.promotions.json")

    def ledger(self, env):
        p = self.ledger_path(env)
        return json.loads(p.read_text()) if p.exists() else None

    def bm_write_ledger(self, env, ledger):
        p = self.ledger_path(env)
        p.write_text(json.dumps(ledger))
        p.chmod(0o600)

    def refused(self, env, spec):
        """`ask --seats <spec>` refused before anything runs: exit 2, one `bridge:` line (returned), no launch."""
        launched = len(self.launched_models(env))
        rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
        self.assertEqual(rc, 2, out + err)
        lines = [l for l in err.splitlines() if l.startswith("bridge: ")]
        self.assertEqual(len(lines), 1, err)
        self.assertEqual(len(self.launched_models(env)), launched)
        return lines[0]

    def test_doctor_adopts_a_newer_listed_model_and_never_writes_the_table(self):
        old = head("terra")
        shipped = MODELS_JSON.read_bytes()
        env, _ = self.promoting("promote-doctor")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"[bridge] models: terra promoted automatically: {old} -> {self.NEW} (this Mac's own Codex catalog lists it, codex-cli 99.0.0)", err)
        self.assertIn(f"terra={self.NEW} (promoted on this Mac)", out)
        self.assertIn(f"promotions: {self.ledger_path(env)}: terra {old} -> {self.NEW}", out)
        self.assertIn(f"  {self.NEW}: family terra; floor codex-cli >= 99.0.0", out)
        self.assertNotIn(f"stale: newer model may exist: {self.NEW}", out)
        self.assertIn("check: OK", out)
        self.assertEqual(MODELS_JSON.read_bytes(), shipped, "models.json was written")
        ledger = self.ledger(env)
        self.assertEqual(oct(self.ledger_path(env).stat().st_mode & 0o777), "0o600")
        self.assertEqual([(s["family"], s["from"], s["to"], s["min_cli"], s["cli"]) for s in ledger["steps"]],
                         [("terra", old, self.NEW, "99.0.0", "codex-cli 99.0.0")])
        for text in (self.ledger_path(env).read_text(), Path(env["BRIDGE_MODELS_EVIDENCE"]).read_text()):
            self.assertNotIn("stub@example.test", text)  # the login is checked, never recorded
        # the check reads the login in the same app-server session as the list
        methods = [r.get("method") for r in self.check_records(env)]
        self.assertIn("account/read", methods)
        self.assertLess(methods.index("model/list"), methods.index("account/read"))
        # the family name now runs the new model, with a notice; its old head is refused as superseded
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--job-dir", "promote-doctor-ask", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [self.NEW])
        lead = leading_notices(out)
        self.assertTrue([l for l in lead if l.startswith(f"NOTICE: models: terra runs {self.NEW}, promoted automatically on ") and
                         f"from {old}" in l and "--undo-promotion terra" in l], out)
        self.assertIn("promoted automatically", self.seat(self.jobs / "promote-doctor-ask", "terra")["expected_evidence"])
        line = self.refused(env, f"{old}:low")
        self.assertIn(f"{old} is superseded by {self.NEW}. Use terra ({self.NEW}).", line)
        # a seat of another family carries no promotion notice
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertFalse([l for l in leading_notices(out) if "promoted automatically" in l], out)

    def test_a_model_starting_mode_adopts_in_the_same_invocation(self):
        # the daily re-check runs before the seats are parsed: the invocation that learns of the model already runs it
        env, _ = self.promoting("promote-same-run")
        self.assertFalse(Path(env["BRIDGE_MODELS_EVIDENCE"]).exists())
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [self.NEW])
        self.assertIn("terra promoted automatically", err)
        self.assertEqual([s["to"] for s in self.ledger(env)["steps"]], [self.NEW])

    def test_a_refresh_that_skipped_the_pass_does_not_postpone_it_for_a_day(self):
        # a process with promotion off (or an older engine sharing the store) refreshes the sources without the pass: the next
        # model-starting mode with promotion on finds no recent pass (`promotion_pass_at`) and refreshes so that it runs, once
        env, _ = self.promoting("promote-pass-due", BRIDGE_MODELS_PROMOTE="off")
        rc, out, err = self.doctor(env)
        self.assertIn(f"stale: newer model may exist: {self.NEW}", out)
        self.assertNotIn("promotion_pass_at", self.evidence(env))
        reads = len([r for r in self.check_records(env) if r.get("method") == "model/list"])
        env["BRIDGE_MODELS_PROMOTE"] = None
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [self.NEW])
        self.assertIn("promotion_pass_at", self.evidence(env))
        after = len([r for r in self.check_records(env) if r.get("method") == "model/list"])
        self.assertGreater(after, reads, "the pass ran without reading the catalog")
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len([r for r in self.check_records(env) if r.get("method") == "model/list"]), after, "a second launch refreshed again")

    def test_a_friends_copy_without_relay_or_registry_promotes_and_runs_the_new_head(self):
        # the friend zip (package.sh): the engine folder alone, no relay, no model registry anywhere; the check still promotes from
        # the local login's catalog and the seat runs the new head on that login
        friend = self.tmp / "friend" / "claude-codex-bridge" / "skills" / "codex"
        shutil.copytree(str(SCRIPTS.parent), str(friend), ignore=shutil.ignore_patterns("__pycache__", "tests"))
        env, _ = self.promoting("promote-friend")
        run_env = without_git_overrides(os.environ)
        for name in ISOLATED_NAMES:
            run_env.pop(name, None)
        run_env.update(self.env)
        for k, v in env.items():
            if v is None:
                run_env.pop(k, None)
            else:
                run_env[k] = v
        run_env.pop("MODEL_REGISTRY_DIR", None)

        def run(*args):
            r = subprocess.run([sys.executable, str(friend / "scripts" / "bridge.py"), *args], capture_output=True, text=True, timeout=120,
                               env=run_env, cwd=str(self.repo), stdin=subprocess.DEVNULL)
            return r.returncode, r.stdout, r.stderr
        rc, out, err = run("doctor")
        self.assertIn("registry: none", out)
        self.assertIn(f"terra={self.NEW} (promoted on this Mac)", out)
        rc, out, err = run("ask", "--seats", "terra:low", "--", "q")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [self.NEW])
        self.assertTrue([l for l in leading_notices(out) if l.startswith(f"NOTICE: models: terra runs {self.NEW}")], out)

    def test_nothing_is_promoted_without_a_login_with_promotion_off_or_for_a_held_back_model(self):
        old = head("terra")
        efforts = TABLE["models"][old]["efforts"]
        cases = {
            "no login": ({"STUB_CODEX_ACCOUNT": "none"}, None, "last pass", "no login stands behind the catalog"),
            "promotion off": ({"BRIDGE_MODELS_PROMOTE": "off"}, None, "promotions:", "automatic promotion is off: BRIDGE_MODELS_PROMOTE"),
            "hidden": ({}, lambda ms: ms.append(self.new_entry(self.NEW, hidden=True)), "held back", "not listed as visible"),
            "preview": ({}, lambda ms: ms.append(self.new_entry(self.NEW, status="preview")), "held back", "its catalog status is 'preview'"),
            "an effort missing": ({}, lambda ms: ms.append(self.new_entry(self.NEW, efforts=efforts[:2])), "held back", f"lacks efforts {old} offers"),
        }
        for name, (extra, mutate, where, reason) in cases.items():
            with self.subTest(case=name):
                env, _ = self.promoting(f"promote-not-{name.replace(' ', '-')}", mutate, **extra)
                rc, out, err = self.doctor(env)
                self.assertNotIn("promoted automatically", err)
                self.assertIn(f"terra={old}", out)
                self.assertTrue([l for l in out.splitlines() if where in l and reason in l], out)
                ledger = self.ledger(env)
                self.assertEqual(ledger["steps"] if ledger else [], [])
                if name == "promotion off":
                    self.assertIsNone(ledger, "the pass ran although promotion is off")
                if name in ("no login", "promotion off", "preview", "an effort missing"):  # a visible newer name stays a stale FAIL for a human
                    self.assertIn(f"stale: newer model may exist: {self.NEW} is listed but not in the model table", out)
                    self.assertEqual(rc, 1, out + err)

    def test_undo_restores_the_shipped_head_blocks_the_model_and_a_later_check_keeps_it_blocked(self):
        old = head("terra")
        env, _ = self.promoting("promote-undo")
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={self.NEW}", out)
        rc, out, err = self.run_engine("doctor", "--undo-promotion", "terra", env_extra=env)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("--undo-promotion needs --reason", err)
        rc, out, err = self.doctor(env, "--undo-promotion", "terra", "--reason", "answers worse on our briefs")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"undone: terra {self.NEW} -> {old}; {self.NEW} is blocked on this Mac (never promoted here again", out)
        self.assertNotIn("then: the check adopted", out)  # nothing else newer is listed
        self.assertIn(f"terra={old}", out)
        self.assertIn(f"  blocked: terra {self.NEW}: its automatic promotion was undone (", out)
        self.assertNotIn("terra promoted automatically", err)  # the check that followed did not promote it again
        self.assertIn(f"info: {self.NEW} is listed but blocked on this Mac: its automatic promotion was undone (", out)
        self.assertNotIn(f"stale: newer model may exist: {self.NEW}", out)
        ledger = self.ledger(env)
        self.assertEqual((ledger["steps"], [b["id"] for b in ledger["blocked"]]), ([], [self.NEW]))
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [old])
        line = self.refused(env, f"{self.NEW}:low")
        self.assertIn(f"{self.NEW} is blocked on this Mac: its automatic promotion was undone (", line)
        self.assertIn(f"answers worse on our briefs). Use terra ({old}).", line)
        rc, out, err = self.doctor(env, "--undo-promotion", "terra", "--reason", "again")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("not undone: no automatic promotion of terra is in effect on this Mac", out)

    def test_a_seat_that_descends_to_a_promoted_head_leads_with_the_promotion_notice(self):
        # Astra's review of 2026-11-07 (finding 2): `astra|terra` whose Astra is rejected at startup runs the promoted Terra head by its
        # preference list; the job then leads with the descent notice AND the promotion notice, once
        env, _ = self.promoting("promote-descent")
        self.doctor(env)
        astra = head("astra")
        rc, out, err = self.run_engine("ask", "--seats", "astra|terra:low", "--job-dir", "promote-descent", "--", f"[[STUB:badmodel={astra}]] q",
                                       env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env)[-1], self.NEW)
        lead = leading_notices(out)
        self.assertTrue([l for l in lead if l.startswith(f"NOTICE: seat astra: model {astra} -> {self.NEW}")], out)
        promo = [l for l in lead if l.startswith(f"NOTICE: models: terra runs {self.NEW}, promoted automatically")]
        self.assertEqual(len(promo), 1, out)

    def test_every_seat_and_reply_that_runs_a_promoted_head_leads_its_own_answer_with_the_notice(self):
        # Astra's second review (P2): the notice is one per job, at job scope, so that every standalone answer that runs the promoted
        # head (each seat's and each reply's last-<tag>.md) leads with it, not only the first one that ran it
        old = head("terra")
        env, _ = self.promoting("promote-replies", BRIDGE_MODELS_PROMOTE="off")
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--job-dir", "promote-replies", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [old])
        env["BRIDGE_MODELS_PROMOTE"] = None
        self.doctor(env)
        d = self.jobs / "promote-replies"
        for n in (1, 2):
            with self.subTest(reply=n):
                rc, out, err = self.run_engine("reply", "promote-replies", "--", f"more {n}", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.codex_records(env, "exec")[-1]["model"], self.NEW)
                last = sorted(d.glob("last-terra-reply-*.md"), key=lambda q: q.stat().st_mtime)[-1]
                for text in (out, last.read_text()):
                    self.assertEqual(len([l for l in leading_notices(text) if l.startswith(f"NOTICE: models: terra runs {self.NEW}")]), 1, text)
        # two seats of one job that descend to the promoted head: each seat's own answer leads with the notice, the combined one once
        astra = head("astra")
        rc, out, err = self.run_engine("ask", "--seats", "astra|terra:low,astra|terra:medium", "--job-dir", "promote-two-seats", "--",
                                       f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "promote-two-seats"
        lasts = sorted(d.glob("last-*.md"))
        self.assertEqual(len(lasts), 2, lasts)
        for last in lasts:
            with self.subTest(answer=last.name):
                self.assertEqual(len([l for l in leading_notices(last.read_text()) if l.startswith(f"NOTICE: models: terra runs {self.NEW}")]), 1)
        self.assertEqual(len([l for l in leading_notices(out) if l.startswith(f"NOTICE: models: terra runs {self.NEW}")]), 1, out)

    def test_a_doctor_that_waited_behind_a_refresh_without_the_pass_runs_the_pass_itself(self):
        # Astra's second review (P3): a doctor that waits for the lock while a refresh without the promotion pass runs (an older
        # engine, or BRIDGE_MODELS_PROMOTE=off) does not take that refresh as its own: the pass is still due, so it refreshes and adopts
        env, _ = self.promoting("promote-coalesce")
        holder = self.start_holder(dict(env, BRIDGE_MODELS_PROMOTE="off"), "promote-coalesce-holder")
        waiter = None
        try:
            waiter = self.start_engine(env, "doctor")
            lock = Path(env["BRIDGE_MODELS_EVIDENCE"] + ".lock")
            names = {str(lock), os.path.realpath(str(lock))}
            for _ in range(300):  # the waiting doctor has opened the evidence lock (its `started` precedes the holder's publication)
                listing = subprocess.run(["lsof", "-p", str(waiter.pid), "-Fn"], capture_output=True, text=True, timeout=30).stdout
                if any(f"\nn{name}\n" in listing or listing.endswith(f"\nn{name}") for name in names):
                    break
                time.sleep(0.1)
            else:
                self.fail("the second doctor never waited on the evidence lock")
        finally:
            hrc, hout, herr = self.release_holder(holder)
        self.assertNotIn("promoted automatically", herr)
        out, err = waiter.communicate(timeout=180)
        self.assertIn(f"terra promoted automatically: {head('terra')} -> {self.NEW}", err)
        self.assertIn(f"terra={self.NEW} (promoted on this Mac)", out)
        self.assertEqual([s["to"] for s in self.ledger(env)["steps"]], [self.NEW])

    def test_an_undo_with_a_newer_runner_up_says_that_the_check_adopted_it(self):
        # Fable's review (design): the undo blocks only the undone model; the same doctor's check then adopts the other newer model the
        # catalog lists, and doctor says so in a line of its own (Astra's first review: never report a head the command did not end on)
        old, lower, higher = head("terra"), self.NEW, "gpt-9.9-terra"
        env, _ = self.promoting("promote-undo-runner-up", lambda ms: ms.extend([self.new_entry(lower), self.new_entry(higher)]))
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={higher} (promoted on this Mac)", out)
        rc, out, err = self.doctor(env, "--undo-promotion", "terra", "--reason", "worse on briefs")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"undone: terra {higher} -> {old}; {higher} is blocked on this Mac", out)
        self.assertIn(f"then: the check adopted {lower} for terra (listed by this Mac's catalog and newer than {old}); "
                      f"--undo-promotion terra again goes back to {old}", out)
        self.assertIn(f"terra promoted automatically: {old} -> {lower}", err)
        self.assertIn(f"terra={lower} (promoted on this Mac)", out)
        self.assertIn(f"    held back: terra {higher}: blocked on this Mac: its automatic promotion was undone", out)
        # the `then:` line's promise: undoing again goes back to the shipped head, and nothing newer is left to adopt
        rc, out, err = self.doctor(env, "--undo-promotion", "terra", "--reason", "worse too")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"undone: terra {lower} -> {old}; {lower} is blocked on this Mac", out)
        self.assertNotIn("then: the check adopted", out)
        self.assertNotIn("promoted automatically", err)
        self.assertIn(f"terra={old}", out)
        for model, reason in ((higher, "worse on briefs"), (lower, "worse too")):
            with self.subTest(blocked=model):
                self.assertIn(f"  blocked: terra {model}: its automatic promotion was undone (", out)
                self.assertIn(f": {reason}); not promoted here again unless models.json names it", out)

    def test_the_tables_policy_switches_promotion_off_and_doctor_json_carries_the_promotions(self):
        # the table's policy.auto_promote=false stops new promotions everywhere (not only the tests' environment switch); doctor --json
        # carries the promotions block of the composed check
        off = self.table_file("promote-policy-off", lambda t: t["policy"].__setitem__("auto_promote", False))
        env, _ = self.promoting("promote-policy-off", table=off)
        rc, out, err = self.doctor(env)
        self.assertIn("automatic promotion is off: models.json policy.auto_promote", out)
        self.assertIn(f"stale: newer model may exist: {self.NEW} is listed but not in the model table", out)
        self.assertIsNone(self.ledger(env))
        env, _ = self.promoting("promote-json")
        rc, out, err = self.doctor(env, "--json")
        promotions = json.loads(out)["promotions"]
        self.assertEqual([(s["family"], s["from"], s["to"], s["min_cli"]) for s in promotions["applied"]], [("terra", head("terra"), self.NEW, "99.0.0")])
        self.assertEqual((promotions["skipped"], promotions["blocked"]), ([], []))
        self.assertEqual(promotions["ledger"], str(self.ledger_path(env)))

    def test_a_block_of_a_dropped_family_is_ignored_and_shown_in_text_and_json(self):
        # Fable's reviews (P3 x2): a block whose family the shipped table no longer has is ignored, and doctor shows it in both outputs
        env, _ = self.promoting("promote-ignored", lambda ms: None)  # the clean catalog: nothing newer listed
        self.bm_write_ledger(env, {"version": 1, "steps": [], "log": [], "last_pass": None,
                                   "blocked": [{"vendor": "codex", "family": "nova", "id": self.NEW, "at": time.time(), "reason": "x"}]})
        rc, out, err = self.doctor(env)
        self.assertIn(f"  block ignored: {self.NEW}: nova is not a Codex family of the shipped table", out)
        rc, out, err = self.doctor(env, "--json")
        promotions = json.loads(out)["promotions"]
        self.assertEqual(promotions["ignored"], [{"family": "nova", "id": self.NEW, "reason": "nova is not a Codex family of the shipped table"}])
        self.assertEqual(promotions["blocked"], [])
        self.assertNotIn("with", json.dumps(promotions))

    def test_a_refused_intermediate_of_a_chain_is_not_reported_as_a_moved_head(self):
        # Fable's re-review (P3): ledger steps old -> 9 and 9 -> 9.9, then a models.json that refuses 9 and leaves the head where it was:
        # the first step is skipped, the second applies, and doctor does not claim that models.json moved the head
        old, lower, higher = head("terra"), self.NEW, "gpt-9.9-terra"
        env, _ = self.promoting("promote-chain")
        self.doctor(env)
        env["STUB_CODEX_CATALOG"] = str(self.catalog_file("promote-chain-2", lambda ms: ms.extend([self.new_entry(lower), self.new_entry(higher)])))
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra promoted automatically: {lower} -> {higher}", err)
        self.assertEqual([(s["from"], s["to"]) for s in self.ledger(env)["steps"]], [(old, lower), (lower, higher)])
        env["BRIDGE_MODELS_FILE"] = str(self.table_file("promote-chain-refused", lambda t: t["refused"]["codex"].__setitem__(
            lower, {"family": "terra", "reason": "test: pulled"})))
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={higher} (promoted on this Mac)", out)
        self.assertIn(f"  skipped: terra {old} -> {lower}: the shipped table names {lower} now", out)
        self.assertNotIn("models.json now names", out)

    def test_an_undo_that_cannot_write_the_ledger_changes_nothing_and_says_so(self):
        # Fable's re-review (P3): the undo's ledger write fails (a read-only cache directory): one line, exit 1, the ledger unchanged
        env, d = self.promoting("promote-undo-readonly")
        self.doctor(env)
        before = self.ledger_path(env).read_text()
        os.chmod(str(d), 0o500)
        try:
            rc, out, err = self.doctor(env, "--undo-promotion", "terra", "--reason", "x")
        finally:
            os.chmod(str(d), 0o700)
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"not undone: cannot write the promotions ledger {self.ledger_path(env)}: Permission denied", out)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(self.ledger_path(env).read_text(), before)

    def test_two_promoted_families_in_one_job_lead_with_one_notice_each(self):
        env, _ = self.promoting("promote-two-families", lambda ms: ms.extend([self.new_entry(self.NEW), self.new_entry("gpt-9-sol")]))
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={self.NEW} (promoted on this Mac)", out)
        self.assertIn("sol=gpt-9-sol (promoted on this Mac)", out)
        rc, out, err = self.run_engine("ask", "--seats", "terra:low,sol:low", "--job-dir", "promote-two-families", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        lead = leading_notices(out)
        for fam, model in (("terra", self.NEW), ("sol", "gpt-9-sol")):
            with self.subTest(family=fam):
                self.assertEqual(len([l for l in lead if l.startswith(f"NOTICE: models: {fam} runs {model}, promoted automatically")]), 1, out)

    def test_doctor_says_when_models_json_moved_the_head_below_this_macs_promotion(self):
        # Fable's review (design): a later models.json that moves the head without naming the promoted (newer) model does not demote it
        # on this Mac (no silent descent); doctor says the ledger's newer head wins and how to go back
        lower, higher = self.NEW, "gpt-9.9-terra"
        env, _ = self.promoting("promote-shipped-moved", lambda ms: ms.extend([self.new_entry(lower), self.new_entry(higher)]))
        self.doctor(env)

        def move(t):  # a checkpoint that makes the lower model the head, the old head a predecessor; it does not name the higher one
            old = t["families"]["codex"]["terra"]["known_good"]
            t["models"][lower] = dict(t["models"][old], generation="9", source="test: a human checkpoint")
            t["families"]["codex"]["terra"]["known_good"] = lower
            t["retired"]["codex"]["terra"].insert(0, old)
            del t["models"][old]
        env["BRIDGE_MODELS_FILE"] = str(self.table_file("promote-shipped-moved", move))
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={higher} (promoted on this Mac)", out)
        self.assertIn(f"  terra: models.json now names {lower} as the head; this Mac keeps the newer {higher} from its ledger", out)
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env)[-1], higher)

    def test_a_broken_ledger_stops_model_starting_modes_in_one_line_and_leaves_job_control_working(self):
        env, _ = self.isolated("promote-broken")
        self.ledger_path(env).write_text("{")
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 2, out + err)
        lines = [l for l in err.splitlines() if l.startswith("bridge: ")]
        self.assertEqual(len(lines), 1, err)
        self.assertIn(f"promotions ledger {self.ledger_path(env)} is not valid JSON", lines[0])
        self.assertIn("remove it to start over", lines[0])
        self.assertEqual(self.launched_models(env), [])
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(f"models: promotions ledger {self.ledger_path(env)} is not valid JSON", out)
        rc, out, err = self.run_engine("status", env_extra=env)
        self.assertEqual(rc, 0, out + err)

    def test_a_shipped_checkpoint_takes_over_from_a_promotion(self):
        env, _ = self.promoting("promote-checkpoint")
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={self.NEW} (promoted on this Mac)", out)
        def adopt(t):
            old = t["families"]["codex"]["terra"]["known_good"]
            t["models"][self.NEW] = dict(t["models"][old], generation="9", source="test: a human checkpoint")
            t["families"]["codex"]["terra"]["known_good"] = self.NEW
            t["retired"]["codex"]["terra"].insert(0, old)
            del t["models"][old]
        env["BRIDGE_MODELS_FILE"] = str(self.table_file("promote-checkpoint", adopt))
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={self.NEW}", out)
        self.assertNotIn("(promoted on this Mac)", out)
        self.assertIn(f"skipped: terra {head('terra')} -> {self.NEW}: the shipped table names {self.NEW} now", out)
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [self.NEW])
        self.assertFalse([l for l in leading_notices(out) if "promoted automatically" in l], out)

    def test_the_floor_is_the_listing_clis_version_and_an_older_cli_is_refused(self):
        env, _ = self.promoting("promote-floor", STUB_CODEX_VERSION="codex-cli 0.170.0-alpha.2")
        rc, out, err = self.doctor(env)
        self.assertIn(f"  {self.NEW}: family terra; floor codex-cli >= 0.170.0-alpha.2", out)
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=dict(env, STUB_CODEX_VERSION="codex-cli 0.169.0"))
        self.assertEqual(rc, 2, out + err)
        self.assertIn(f"seat 'terra' ({self.NEW}) needs Codex CLI >= 0.170.0-alpha.2", err)
        self.assertEqual(self.launched_models(env), [])

    def test_a_reply_to_a_seat_on_the_old_head_continues_on_the_promoted_head(self):
        old = head("terra")
        env, _ = self.promoting("promote-reply", BRIDGE_MODELS_PROMOTE="off")
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--job-dir", "promote-reply", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [old])
        env["BRIDGE_MODELS_PROMOTE"] = None
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={self.NEW}", out)
        rc, out, err = self.run_engine("reply", "promote-reply", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.codex_records(env, "exec")[-1]["model"], self.NEW)
        lead = leading_notices(out)
        self.assertIn(f"NOTICE: seat terra: reply runs on {self.NEW} (recorded {old} is superseded)", lead)
        self.assertTrue([l for l in lead if l.startswith(f"NOTICE: models: terra runs {self.NEW}, promoted automatically")], out)

    def test_the_notice_stops_after_a_week_and_doctor_keeps_showing_the_promotion(self):
        env, _ = self.promoting("promote-week")
        self.doctor(env)
        p = self.ledger_path(env)
        ledger = json.loads(p.read_text())
        ledger["steps"][0]["at"] = time.time() - 8 * 86400
        p.write_text(json.dumps(ledger))
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [self.NEW])
        self.assertFalse([l for l in leading_notices(out) if "promoted automatically" in l], out)
        rc, out, err = self.doctor(env)
        self.assertIn(f"terra={self.NEW} (promoted on this Mac)", out)


if __name__ == "__main__":
    unittest.main()

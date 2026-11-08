"""Exercise the real stdio server using only a local, deterministic engine."""

import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


CLAUDE = Path(__file__).resolve().parents[1]
SERVER = CLAUDE / "scripts" / "claude_mcp_server.py"
ENTRYPOINT = CLAUDE / "scripts" / "bridge.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STUB = FIXTURES / "stub_engine.py"
GATED_SERVER = FIXTURES / "gated_server.py"
ENGINE_FIXTURES = CLAUDE.parents[1] / "skills" / "codex" / "tests" / "fixtures"
REAL_ENGINE = CLAUDE.parents[1] / "skills" / "codex" / "scripts" / "bridge.py"
JOB = "20260906-123456-ask-abcde"
DEFAULTS = {
    "BRIDGE_CALLER": "OpenAI Codex",
    "BRIDGE_DEFAULT_SEAT": "fable:max",
    "BRIDGE_REVIEW_SEATS": "opus:max",
    "BRIDGE_COUNCIL_SEATS": "fable:max,opus:max",
}
# S-DEF (review/rulings_rr2.md): the entry points pass their built-in seat lists in these variables and never set the user-facing
# ones above; BRIDGE_DEFAULTS_SUPPLIED (RR-8's provenance variable) is removed
BUILTINS = {
    "BRIDGE_BUILTIN_DEFAULT_SEAT": "fable:max",
    "BRIDGE_BUILTIN_REVIEW_SEATS": "opus:max",
    "BRIDGE_BUILTIN_COUNCIL_SEATS": "fable:max,opus:max",
}
USER_SEAT_VARS = ("BRIDGE_DEFAULT_SEAT", "BRIDGE_REVIEW_SEATS", "BRIDGE_COUNCIL_SEATS")
NEVER_INHERITED = (*BUILTINS, "BRIDGE_DEFAULTS_SUPPLIED")  # a suite run from under an entry point must not carry these in
# The models change (DESIGN rev 13): the real engine runs a free check (the stub app-server's catalog and three pages).
EVIDENCE = ENGINE_FIXTURES / "evidence"
MODELS_JSON = CLAUDE.parents[1] / "skills" / "codex" / "models.json"  # the shipped table, the only one read (DESIGN D23)
SENTINEL_TOKEN = "SENTINEL-7f3a9c2e41d0-DO-NOT-PRINT"
MODEL_OVERRIDES = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "BRIDGE_MODELS_FILE", "BRIDGE_MODELS_EVIDENCE",
                   "BRIDGE_MODELS_GATE", "BRIDGE_CODEX_EFFORT", "BRIDGE_CLAUDE_EFFORT", "STUB_CODEX_VERSION", "STUB_CLAUDE_VERSION")
sys.path.insert(0, str(ENGINE_FIXTURES))
from page_server import PageServer  # noqa: E402
import catalog_fixture  # noqa: E402  the engine tests' clean catalog: the newest real capture, completed for the shipped table


def model_head(family):
    table = json.loads(MODELS_JSON.read_text())
    vendor = "codex" if family in table["families"]["codex"] else "claude"
    return table["families"][vendor][family]["known_good"]


def fixture_git(root, cwd, *args):
    """Git for a fixture repository: HOME and XDG_CONFIG_HOME under the test root, no global or system configuration, no
    inherited GIT_* override, no hooks and no commit signing, so no helper or hook of the reviewer's can run."""
    home = Path(root) / "git-home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", *args],
                          cwd=str(cwd), env=env, capture_output=True, text=True, timeout=10)


try:
    USE_FIXTURE_PS = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(os.getpid())],
        capture_output=True, text=True, timeout=2,
    ).returncode != 0
except (OSError, subprocess.SubprocessError):
    USE_FIXTURE_PS = True


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ServerProcess:
    def __init__(self, cwd, record_path, overrides=None, drain_stdout=True, server_path=SERVER):
        self.record_path = record_path
        self.env = dict(os.environ)
        for key in (*DEFAULTS, *NEVER_INHERITED, "CLAUDE_BRIDGE_ALLOW_WRITE", "CLAUDE_BRIDGE_TOOL_DEADLINE",
                    "CLAUDE_BRIDGE_ROOT", "CLAUDE_BRIDGE_ALLOW_ANY_CWD",
                    "MCP_STUB_EXIT", "MCP_STUB_STDERR", "MCP_STUB_STDOUT_LENGTH",
                    "MCP_STUB_JOB", "MCP_STUB_SEAT_IDENTITY", "MCP_STUB_META_DELAY",
                    "MCP_STUB_IDENTITIES", "MCP_STUB_PS_RECORD", "MCP_STUB_SUPERVISOR_IDENTITY",
                    "MCP_STUB_RESULT_EXIT", "MCP_STUB_RESULT_HIDE_STATE", "BRIDGE_ROOT",
                    "MCP_STUB_DELIVERY_GATE", "MCP_STUB_DELIVERY_MODE",
                    "MCP_STUB_FORCE_PS", "MCP_STUB_PS_GROUP_MODE",
                    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                    "BRIDGE_JOBS", "CLAUDE_BIN", "CODEX_BIN", "MCP_PASSTHROUGH_SENTINEL",
                    "MODEL_REGISTRY_DIR", "MODEL_REGISTRY_HOME", "RELAY_HOME"):
            self.env.pop(key, None)
        self.env.update({
            "CLAUDE_BRIDGE_ENGINE": str(STUB),
            "MCP_STUB_RECORD": str(record_path),
            "BRIDGE_JOBS": str(record_path.parent / "jobs"),
            "TMPDIR": str(record_path.parent),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key, value in (overrides or {}).items():
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = value
        if USE_FIXTURE_PS or self.env.get("MCP_STUB_FORCE_PS") == "1":
            # Some managed macOS sandboxes prohibit executing /bin/ps. Keep
            # the identity protocol exercised with a fixture-only substitute.
            self.env.update({
                "PATH": str(FIXTURES) + os.pathsep + self.env.get("PATH", ""),
                "MCP_STUB_IDENTITIES": str(record_path.parent / "identities"),
                "MCP_STUB_PS_RECORD": str(record_path.parent / "ps-calls.jsonl"),
            })
        self.jobs = Path(self.env["BRIDGE_JOBS"])
        if not self.jobs.is_absolute():
            self.jobs = (Path(cwd) / self.jobs).resolve()
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(server_path)], cwd=str(cwd), env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", start_new_session=True,
        )
        self.stdout = queue.Queue()
        self.stderr = []
        self.readers = [threading.Thread(target=self._read_stderr, daemon=True)]
        if drain_stdout:
            self.readers.append(threading.Thread(target=self._read_stdout, daemon=True))
        for reader in self.readers:
            reader.start()

    def _read_stdout(self):
        for line in self.process.stdout:
            self.stdout.put(line)

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line)

    def send_raw(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def send(self, message):
        self.send_raw(json.dumps(message))

    def read(self, timeout=5):
        try:
            line = self.stdout.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("No server response; exit=%r; stderr=%s" % (
                self.process.poll(), "".join(self.stderr)))
        try:
            message = json.loads(line)
        except ValueError as error:
            raise AssertionError("Non-JSON stdout line: %r" % line) from error
        if message.get("jsonrpc") != "2.0":
            raise AssertionError("Invalid JSON-RPC response: %r" % message)
        return message

    def request(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.read()
        if response.get("id") != request_id:
            raise AssertionError("Unexpected response id: %r" % response)
        return response

    def call(self, name, arguments, request_id=1):
        return self.request("tools/call", {"name": name, "arguments": arguments}, request_id)

    def records(self):
        try:
            lines = self.record_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                break  # A concurrently starting stub may still be writing.
        return records

    def wait_records(self, count=1, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self.records()
            if len(records) >= count:
                return records
            time.sleep(0.02)
        raise AssertionError("Stub did not start; stderr=" + "".join(self.stderr))

    def assert_quiet(self, timeout=0.2):
        try:
            line = self.stdout.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError("Unexpected server response: " + line)

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
        # Clean up even when testing a broken implementation. Only signal
        # recorded fixture PIDs, never the caller's process group.
        for record in self.records():
            for pid, expected_start in (
                (record.get("seat_pid"), record.get("seat_start")),
                (record.get("seat_child_pid"), record.get("seat_child_start")),
                (record.get("child_pid"), record.get("child_start")),
                (record["pid"], record.get("pid_start")),
            ):
                if not pid:
                    continue
                try:
                    live_start = subprocess.run(
                        ["ps", "-o", "lstart=", "-p", str(pid)],
                        capture_output=True, text=True, timeout=2, env=self.env,
                    ).stdout.strip()
                    if not expected_start or live_start != expected_start:
                        continue
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for reader in self.readers:
            reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


class MCPBase(unittest.TestCase):
    """Fixture and helpers shared by every class in this file; holds no tests."""

    def setUp(self):
        # Keep temporary data inside the allowed fixture directory and remove
        # it after every test, including failures.
        self.temporary = tempfile.TemporaryDirectory(prefix=".mcp-test-", dir=str(FIXTURES))
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.addCleanup(self.temporary.cleanup)
        self.servers = []

    def start(self, overrides=None, drain_stdout=True, server_path=SERVER):
        record = self.root / ("calls-%d.jsonl" % len(self.servers))
        server = ServerProcess(self.cwd, record, overrides, drain_stdout=drain_stdout, server_path=server_path)
        self.servers.append(server)
        self.addCleanup(server.close)
        return server

    def saved_job(self, server, job=JOB, cwd=None, state="done"):
        directory = server.jobs / job
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(json.dumps({
            "cwd": str(cwd or self.cwd.resolve()), "state": state, "mode": "ask",
        }), encoding="utf-8")
        return directory

    def assert_error(self, response, code, field=None):
        self.assertNotIn("result", response)
        self.assertEqual(response["error"]["code"], code)
        if field:
            self.assertIn(field, response["error"]["message"])

    def assert_result(self, response, status="ok", exit_code=0):
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertIs(result["isError"], status not in ("ok", "partial"))
        structured = result["structuredContent"]
        self.assertEqual(structured["status"], status)
        self.assertEqual(structured["exit_code"], exit_code)
        self.assertIsInstance(structured["elapsed_seconds"], (int, float))
        self.assertGreaterEqual(structured["elapsed_seconds"], 0)
        self.assertIn("job", structured)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertLessEqual(len(result["content"][0]["text"]), 16000)
        return result

    def assert_pid_gone(self, pid, timeout=3):
        deadline = time.monotonic() + timeout
        while process_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if process_exists(pid):
            try:
                process_state = subprocess.run(
                    ["ps", "-o", "pid=,ppid=,pgid=,stat=,command=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError) as error:
                process_state = "unavailable: " + str(error)
            context = []
            for server in self.servers:
                records = [{key: value for key, value in record.items() if key != "env"}
                           for record in server.records()]
                context.append("records=%r; stderr=%s" % (records, "".join(server.stderr)))
            self.fail("Fixture process %s is still alive; ps=%r; %s" % (pid, process_state, "\n".join(context)))

    def begin_sleep(self, server, request_id="sleeping", prompt="sleep:30"):
        expected_records = len(server.records()) + 1
        server.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                     "params": {"name": "claude_ask", "arguments": {"prompt": prompt}}})
        return server.wait_records(expected_records)[-1]

    def begin_pending_started(self, mode="before", drain_stdout=True, tool="claude_task", overrides=None):
        gate = self.root / ("delivery-%d" % len(self.servers))
        gate.mkdir()
        environment = dict(overrides or {}, MCP_STUB_DELIVERY_GATE=str(gate), MCP_STUB_DELIVERY_MODE=mode)
        server = self.start(environment,
                            drain_stdout=drain_stdout, server_path=GATED_SERVER)
        argument = "intent" if tool == "claude_review" else "prompt"
        server.send({"jsonrpc": "2.0", "id": "pending", "method": "tools/call",
                     "params": {"name": tool, "arguments": {argument: "seat-sleep:30", "wait": False}}})
        record = server.wait_records()[0]
        deadline = time.monotonic() + 5
        while not (gate / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue((gate / "ready").exists(), "Started response never reached delivery gate: " + "".join(server.stderr))
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assertTrue(process_exists(record[field]))
        return server, record, gate

    def begin_leaderless(self, overrides=None):
        environment = dict(overrides or {}, MCP_STUB_FORCE_PS="1")
        server = self.start(environment)
        record = self.begin_sleep(server, prompt="leaderless-exit:1")
        self.assert_pid_gone(record["seat_pid"])
        self.assertTrue(process_exists(record["pid"]))
        self.assertTrue(process_exists(record["seat_child_pid"]))
        self.assertEqual(os.getpgid(record["seat_child_pid"]), record["seat_pid"])
        return server, record

    def catalog_with(self, name, *extra_models):
        """The stub catalog of a real-engine run (real_engine_env serves it, so it lives here on MCPBase): the engine tests' clean
        catalog (the newest real capture, a derived entry for any shipped head it lacks) plus `extra_models`."""
        table = json.loads((ENGINE_FIXTURES.parents[1] / "models.json").read_text())
        models = catalog_fixture.complete(catalog_fixture.load_models(), table)[0] + [{"id": m, "model": m, "hidden": False, "upgrade": None, "upgradeInfo": None,
                                    "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]}
                                   for m in extra_models]
        path = self.root / name
        path.write_text(json.dumps({"models": models}))
        return path

    def real_engine_env(self, **extra):
        """DESIGN §8: a run of the REAL engine is hermetic: BRIDGE_ROUTER=off and a missing RELAY_BIN (no relay), HOME,
        CODEX_HOME and XDG_CACHE_HOME under the test root, the engine's stub CLIs (also as relay's Claude CLI), the stub
        catalog and config for the check's app-server, the fixture pages (loopback), and no model-override variable.
        BRIDGE_CODEX_FIXED_ROOT is an empty directory under the test root (TEST_RULINGS F-E6: the default fixed directories
        /opt/homebrew/bin and /usr/local/bin are looked up beneath it, so no host codex is found), and the engine's git reads
        no global or system configuration and no inherited GIT_* override."""
        if not hasattr(self, "pages"):
            self.pages = PageServer()
            self.addCleanup(self.pages.close)
        for sub in ("home", "codex-home", "cache", "config", "fixed-root", "mr-home", "relay-home"):
            (self.root / sub).mkdir(exist_ok=True)
        env = {key: None for key in os.environ if key.startswith("GIT_")}
        # the engine finds this checkout's model registry above itself (docs/model-routing.md 2.1); its state stays under the test root
        env.update({"CLAUDE_BRIDGE_ENGINE": None, "BRIDGE_ROUTER": "off", "RELAY_BIN": str(self.root / "no-such-relay"),
                    "MODEL_REGISTRY_DIR": None, "MODEL_REGISTRY_HOME": str(self.root / "mr-home"), "RELAY_HOME": str(self.root / "relay-home"),
                    "HOME": str(self.root / "home"), "CODEX_HOME": str(self.root / "codex-home"), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "CODEX_BIN": str(ENGINE_FIXTURES / "stub_codex.py"), "CLAUDE_BIN": str(ENGINE_FIXTURES / "stub_claude.py"),
                    "RELAY_CLAUDE_BIN": str(ENGINE_FIXTURES / "stub_claude.py"),
                    "STUB_CODEX_CATALOG": str(self.catalog_with("clean_catalog.json")),  # the engine tests' clean catalog: lists every table head
                    "STUB_CODEX_CONFIG": str(EVIDENCE / "codex_config_read_all_default.json"),
                    "STUB_CODEX_RECORD": str(self.root / "stub-codex.jsonl"), "STUB_CLAUDE_RECORD": str(self.root / "stub-claude.jsonl"),
                    "BRIDGE_CODEX_FIXED_ROOT": str(self.root / "fixed-root"),
                    # the stale scenarios serve a newer Sol on purpose: automatic promotion (DECISIONS A28) stays off here
                    "BRIDGE_MODELS_PROMOTE": "off",
                    "XDG_CONFIG_HOME": str(self.root / "config"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        env.update({name: None for name in MODEL_OVERRIDES})
        env.update({name: None for name in ("BRIDGE_ROUTED_MAX_WAIT", "BRIDGE_LIMIT_LADDER")})  # the user's own wait knobs never reach a test run
        env.update(self.pages.pages(re.sub(r"[^A-Za-z0-9]+", "-", self.id())[-60:]))
        env.update(extra)
        return env

    def stub_records(self, name):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


class MCPServerTests(MCPBase):
    def test_initialize_negotiates_supported_versions(self):
        server = self.start()
        for version in ("2025-06-18", "2025-03-26", "2024-11-05", "2099-01-01"):
            with self.subTest(version=version):
                result = server.request("initialize", {"protocolVersion": version})["result"]
                self.assertEqual(result["protocolVersion"], version if version != "2099-01-01" else "2025-06-18")
                self.assertIn("tools", result["capabilities"])
                self.assertIn("name", result["serverInfo"])

    def test_ping_answers_string_and_integer_ids(self):
        server = self.start()
        for request_id in (0, -1, "ping", ""):
            self.assertEqual(server.request("ping", request_id=request_id)["result"], {})

    def test_notifications_never_receive_responses_or_dispatch_tools(self):
        server = self.start()
        messages = [
            {"method": "ping"}, {"method": "notifications/initialized"},
            {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"method": "tools/list"}, {"method": "unknown"},
            {"method": "tools/call", "params": {"name": "claude_ask", "arguments": {"prompt": "notify"}}},
            {"method": "notifications/cancelled", "params": {"requestId": "absent"}},
        ]
        for message in messages:
            server.send(dict(message, jsonrpc="2.0"))
        self.assertEqual(server.request("ping", request_id="after-notifications")["result"], {})
        server.assert_quiet()
        self.assertEqual(server.records(), [])

    def test_parse_error_does_not_stop_server(self):
        server = self.start()
        server.send_raw("{not json")
        response = server.read()
        self.assertIsNone(response["id"])
        self.assert_error(response, -32700)
        self.assertEqual(server.request("ping")["result"], {})

    def test_invalid_envelopes_are_protocol_errors(self):
        server = self.start()
        for message in ([], None, 42, "string", {}, {"jsonrpc": "2.0"},
                        {"jsonrpc": "2.0", "method": []},
                        {"jsonrpc": "2.0", "method": "ping", "params": []},
                        {"jsonrpc": "2.0", "id": 7},
                        {"jsonrpc": "2.0", "id": 7, "method": []},
                        {"jsonrpc": "1.0", "id": 7, "method": "ping"}):
            with self.subTest(message=message):
                server.send(message)
                response = server.read()
                self.assert_error(response, -32600)
                self.assertEqual(response["id"], message.get("id") if isinstance(message, dict) else None)
        for params in ([], None, "bad", 0):
            with self.subTest(params=params):
                server.send({"jsonrpc": "2.0", "id": 8, "method": "ping", "params": params})
                response = server.read()
                self.assert_error(response, -32600)
                self.assertEqual(response["id"], 8)
        self.assert_error(server.request("missing/method"), -32601)

    def test_nonfinite_numeric_id_is_rejected_without_stopping_server(self):
        server = self.start()
        server.send_raw('{"jsonrpc":"2.0","id":1e999,"method":"ping"}')
        response = server.read()
        self.assert_error(response, -32600)
        self.assertIsNone(response["id"])
        self.assertEqual(server.request("ping")["result"], {})

    def test_invalid_id_types_are_rejected_with_null_response_id(self):
        server = self.start()
        for request_id in (None, True, False, 1.5, 1.0, [], {}):
            with self.subTest(request_id=request_id):
                server.send({"jsonrpc": "2.0", "id": request_id, "method": "ping"})
                response = server.read()
                self.assert_error(response, -32600)
                self.assertIsNone(response["id"])
        self.assertEqual(server.request("ping")["result"], {})

    def test_tools_list_schemas_are_strict(self):
        tools = self.start().request("tools/list")["result"]["tools"]
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        self.assertEqual(set(schemas), {"claude_ask", "claude_review", "claude_task", "claude_reply",
                                       "claude_status", "claude_result"})
        for schema in schemas.values():
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            for name, prop in schema["properties"].items():
                self.assertEqual(prop["type"], {"files": "array", "write": "boolean", "wait": "boolean"}.get(name, "string"))
        ask = schemas["claude_ask"]
        self.assertIn("prompt", ask["required"])
        self.assertEqual(ask["properties"]["files"]["items"]["type"], "string")
        self.assertIn("pattern", ask["properties"]["seats"])
        self.assertEqual(set(schemas["claude_review"]["properties"]["scope"]["enum"]), {"uncommitted", "base", "commit"})
        self.assertIn("prompt", schemas["claude_task"]["required"])
        self.assertEqual(set(schemas["claude_reply"]["required"]), {"job", "prompt"})
        for field in ("job", "seat"):
            self.assertIn("pattern", schemas["claude_reply"]["properties"][field])
        for name in ("claude_review", "claude_task"):
            self.assertIs(schemas[name]["properties"]["wait"]["default"], True)
            description = next(tool["description"] for tool in tools if tool["name"] == name)
            for phrase in ("CLAUDE_BRIDGE_TOOL_DEADLINE", "3300", "Codex", "usage", "wait"):
                self.assertIn(phrase, description)
        task = schemas["claude_task"]["properties"]["seats"]
        self.assertIsNone(re.fullmatch(task["pattern"], "fable:max,opus:max"))
        self.assertIsNotNone(re.fullmatch(task["pattern"], "fable:max"))
        description = next(tool["description"] for tool in tools if tool["name"] == "claude_task")
        self.assertIn("single", description.lower())
        for name in ("claude_status", "claude_result"):
            self.assertEqual(schemas[name]["required"], ["job"])
            self.assertEqual(set(schemas[name]["properties"]), {"job"})

    def test_valid_ask_passes_literal_arguments_and_resolved_cwd(self):
        actual = self.cwd / "actual"
        actual.mkdir()
        (self.cwd / "alias").symlink_to(actual, target_is_directory=True)
        (actual / "target.py").write_text("# fixture", encoding="utf-8")
        (actual / "file-alias.py").symlink_to(actual / "target.py")
        server = self.start()
        prompt = "--literal prompt with spaces; $(do-not-run)"
        result = self.assert_result(server.call("claude_ask", {
            "prompt": prompt, "cwd": "alias", "seats": "fable:max,opus:max",
            "files": ["file with spaces.py", "second.py", "file-alias.py"],
        }))
        record = server.wait_records()[0]
        self.assertEqual(record["cwd"], str(actual.resolve()))
        self.assertEqual(record["argv"], ["ask",
                         "--seats", "fable:max,opus:max", "--file", str(actual / "file with spaces.py"),
                         "--file", str(actual / "second.py"), "--file", str(actual / "target.py")])
        self.assertEqual(record["stdin"], prompt)
        self.assertNotIn(prompt, " ".join(record["argv"]))
        self.assertEqual(result["structuredContent"]["job"], JOB)
        self.assertIn(prompt, result["content"][0]["text"])

    def test_default_cwd_is_the_server_working_directory(self):
        server = self.start()
        self.assert_result(server.call("claude_ask", {"prompt": "hello"}))
        record = server.wait_records()[0]
        self.assertEqual(record["cwd"], str(self.cwd.resolve()))
        self.assertNotIn("--cwd", record["argv"])

    def test_invalid_arguments_never_launch_engine(self):
        server = self.start()
        cases = [
            ("claude_ask", {}, "prompt"),
            ("claude_ask", {"prompt": ""}, "prompt"),
            ("claude_ask", {"prompt": " \t\n"}, "prompt"),
            ("claude_ask", {"prompt": 123}, "prompt"),
            ("claude_ask", {"prompt": "x", "seats": "fable max"}, "seats"),
            ("claude_ask", {"prompt": "x", "seats": ""}, "seats"),
            ("claude_ask", {"prompt": "x", "seats": []}, "seats"),
            ("claude_ask", {"prompt": "x", "seats": "fable:max\n"}, "seats"),
            ("claude_ask", {"prompt": "x", "cwd": False}, "cwd"),
            ("claude_ask", {"prompt": "x", "files": "file.py"}, "files"),
            ("claude_ask", {"prompt": "x", "files": [1]}, "files"),
            ("claude_ask", {"prompt": "x", "extra": "value"}, "extra"),
            ("claude_task", {"prompt": "x", "write": "false"}, "write"),
            ("claude_task", {"prompt": "x", "write": 1}, "write"),
            ("claude_task", {"prompt": "x", "write": None}, "write"),
            ("claude_task", {"prompt": "x", "seats": "fable:max,opus:max"}, "seats"),
            ("claude_task", {"prompt": "x", "wait": "false"}, "wait"),
            ("claude_review", {"wait": 0}, "wait"),
            ("claude_review", {"wait": None}, "wait"),
            ("claude_review", {"scope": "other"}, "scope"),
            ("claude_review", {"scope": "base"}, "ref"),
            ("claude_review", {"scope": "commit"}, "ref"),
            ("claude_review", {"scope": "base", "ref": ""}, "ref"),
            ("claude_review", {"focus": []}, "focus"),
            ("claude_reply", {"prompt": "x", "job": "../outside"}, "job"),
            ("claude_reply", {"prompt": "x", "job": JOB + "\n"}, "job"),
            ("claude_reply", {"prompt": "x", "job": "last", "seat": "bad/tag"}, "seat"),
            ("claude_reply", {"prompt": "x", "job": "last", "seat": "tag\n"}, "seat"),
            ("claude_reply", {"job": "last"}, "prompt"),
            ("claude_status", {}, "job"),
            ("claude_result", {"job": JOB, "cwd": str(self.cwd)}, "cwd"),
        ]
        for tool, arguments, field in cases:
            with self.subTest(tool=tool, arguments=arguments):
                self.assert_error(server.call(tool, arguments), -32602, field)
        for arguments in ([], "bad", None):
            with self.subTest(arguments=arguments):
                self.assert_error(server.call("claude_ask", arguments), -32602, "arguments")
        self.assert_error(server.call("unknown_tool", {}), -32602)
        self.assertEqual(server.records(), [])

    def test_cwd_must_be_an_existing_directory(self):
        server = self.start()
        file_path = self.cwd / "file"
        file_path.write_text("not a directory", encoding="utf-8")
        for cwd in (str(file_path), str(self.cwd / "missing")):
            with self.subTest(cwd=cwd):
                self.assert_error(server.call("claude_ask", {"prompt": "x", "cwd": cwd}), -32602, "cwd")
        self.assertEqual(server.records(), [])

    def test_write_requires_exact_environment_opt_in(self):
        for opt_in in (None, "true", "0"):
            with self.subTest(opt_in=opt_in):
                server = self.start({"CLAUDE_BRIDGE_ALLOW_WRITE": opt_in} if opt_in is not None else {})
                result = server.call("claude_task", {"prompt": "edit", "write": True})["result"]
                self.assertIs(result["isError"], True)
                self.assertIn("CLAUDE_BRIDGE_ALLOW_WRITE=1", result["content"][0]["text"])
                self.assertEqual(server.records(), [])

    def test_opted_in_write_passes_write_without_full_access(self):
        server = self.start({"CLAUDE_BRIDGE_ALLOW_WRITE": "1"})
        self.assert_result(server.call("claude_task", {"prompt": "edit", "write": True}))
        argv = server.wait_records()[0]["argv"]
        self.assertEqual(argv[0], "task")
        self.assertIn("--write", argv)
        self.assertNotIn("--full-access", argv)

    def test_false_write_is_read_only_without_opt_in(self):
        server = self.start()
        self.assert_result(server.call("claude_task", {"prompt": "inspect", "write": False}))
        argv = server.wait_records()[0]["argv"]
        self.assertNotIn("--write", argv)
        self.assertNotIn("--full-access", argv)

    def test_review_and_reply_build_expected_arguments(self):
        server = self.start()
        for scope, flag in (("base", "--base"), ("commit", "--commit")):
            self.assert_result(server.call("claude_review", {
                "scope": scope, "ref": "main", "intent": "intent text", "focus": "focus text",
            }))
            argv = server.records()[-1]["argv"]
            self.assertEqual(argv[0], "review")
            self.assertNotIn("--cwd", argv)
            self.assertEqual(argv[argv.index(flag) + 1], "main")
            self.assertIn("--intent-stdin", argv)
            self.assertNotIn("intent text", " ".join(argv))
            self.assertNotIn("focus text", " ".join(argv))
            self.assertEqual(json.loads(server.records()[-1]["stdin"]),
                             {"intent": "intent text", "focus": "focus text"})
        for job in (JOB, "last"):
            self.assert_result(server.call("claude_reply", {"job": job, "seat": "seat-1_tag.ok", "prompt": "follow up"}))
            argv = server.records()[-1]["argv"]
            self.assertEqual(argv[:2], ["reply", JOB])
            self.assertEqual(argv[argv.index("--seat") + 1], "seat-1_tag.ok")
            self.assertNotIn("--cwd", argv)
            self.assertNotIn("follow up", " ".join(argv))
            self.assertEqual(server.records()[-1]["stdin"], "follow up")

    def test_large_prompts_use_stdin_for_ask_task_and_reply(self):
        server = self.start()
        self.saved_job(server)
        prompt = "private prompt\n'\"; $(not-a-command) " + "x" * (2 * 1024 * 1024)
        for name in ("claude_ask", "claude_task", "claude_reply"):
            with self.subTest(tool=name):
                args = {"prompt": prompt}
                if name == "claude_reply":
                    args["job"] = JOB
                self.assert_result(server.call(name, args))
                record = server.records()[-1]
                self.assertEqual(record["stdin"], prompt)
                self.assertNotIn("private prompt", " ".join(record["argv"]))
                self.assertLess(sum(map(len, record["argv"])), 4096)

    def test_review_preserves_empty_and_unicode_stdin_fields(self):
        server = self.start()
        for args in ({}, {"intent": "", "focus": ""}, {"intent": "why\nα", "focus": "look at β"}):
            with self.subTest(args=args):
                self.assert_result(server.call("claude_review", args))
                record = server.records()[-1]
                self.assertIn("--intent-stdin", record["argv"])
                self.assertEqual(json.loads(record["stdin"]),
                                 {"intent": args.get("intent", ""), "focus": args.get("focus", "")})

    def test_cwd_and_files_are_confined_to_the_startup_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("fixture", encoding="utf-8")
        (self.cwd / "outside-link").symlink_to(outside, target_is_directory=True)
        (self.cwd / "secret-link").symlink_to(secret)
        server = self.start()
        for cwd in (str(outside), "../outside", "outside-link"):
            with self.subTest(cwd=cwd):
                self.assert_error(server.call("claude_ask", {"prompt": "x", "cwd": cwd}), -32602, "cwd")
        for path in (str(secret), "../outside/secret.txt", "outside-link/secret.txt", "secret-link",
                     "outside-link/missing.txt"):
            with self.subTest(file=path):
                self.assert_error(server.call("claude_ask", {"prompt": "x", "files": [path]}), -32602, "files")
        self.assertEqual(server.records(), [])

    def test_explicit_root_allows_siblings_and_rejects_paths_beyond_it(self):
        sibling = self.root / "sibling"
        sibling.mkdir()
        server = self.start({"CLAUDE_BRIDGE_ROOT": str(self.root)})
        self.assert_result(server.call("claude_ask", {
            "prompt": "x", "cwd": str(sibling), "files": [str(self.cwd / "future.txt")],
        }))
        self.assertEqual(server.records()[-1]["cwd"], str(sibling.resolve()))
        self.assert_error(server.call("claude_ask", {"prompt": "x", "cwd": str(self.root.parent)}), -32602, "cwd")

    def test_every_engine_launch_receives_the_authorized_root_realpath(self):
        root_alias = self.root / "root-alias"
        root_alias.symlink_to(self.cwd, target_is_directory=True)
        server = self.start({"CLAUDE_BRIDGE_ROOT": str(root_alias), "BRIDGE_ROOT": str(self.root)})
        calls = [
            ("claude_ask", {"prompt": "environment"}),
            ("claude_review", {}),
            ("claude_task", {"prompt": "environment"}),
            ("claude_reply", {"job": JOB, "prompt": "environment"}),
            ("claude_status", {"job": JOB}),
            ("claude_result", {"job": JOB}),
        ]
        for name, arguments in calls:
            with self.subTest(tool=name):
                self.assert_result(server.call(name, arguments))
                self.assertEqual(server.records()[-1]["env"]["BRIDGE_ROOT"], str(self.cwd.resolve()))
                self.assertNotIn("--cwd", server.records()[-1]["argv"])

    def test_real_engine_keeps_git_subdirectory_inside_authorized_root(self):
        repository = self.root / "repository"
        authorized = repository / "authorized"
        authorized.mkdir(parents=True)
        completed = fixture_git(self.root, self.root, "init", "-q", str(repository))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cli_record = self.root / "real-engine-cli.jsonl"
        # changed for DESIGN §8: the real engine runs with BRIDGE_ROUTER=off, a missing RELAY_BIN, HOME/CODEX_HOME/
        # XDG_CACHE_HOME under the test root and the stub catalog/config (and fixture pages) for its models check
        server = self.start(self.real_engine_env(CLAUDE_BRIDGE_ROOT=str(authorized)))
        self.assertNotIn("CLAUDE_BRIDGE_ENGINE", server.env)
        server.send({"jsonrpc": "2.0", "id": "real-engine", "method": "tools/call",
                     "params": {"name": "claude_ask", "arguments": {
                         "cwd": str(authorized),
                         "prompt": "[[STUB:argv=%s,text=confined-success]]" % cli_record,
                     }}})
        result = self.assert_result(server.read(timeout=15))
        self.assertIn("confined-success", result["content"][0]["text"])
        job = result["structuredContent"]["job"]
        metadata = json.loads((server.jobs / job / "meta.json").read_text(encoding="utf-8"))
        recorded_cwd = Path(metadata["cwd"]).resolve()
        self.assertEqual(recorded_cwd, authorized.resolve())
        self.assertEqual(os.path.commonpath([str(recorded_cwd), str(authorized.resolve())]),
                         str(authorized.resolve()))
        self.assertEqual(len(cli_record.read_text(encoding="utf-8").splitlines()), 1)

    def test_replacing_validated_cwd_cannot_redirect_the_real_engine(self):
        spec = importlib.util.spec_from_file_location("mcp_server_race_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for destination in ("inside", "outside"):
            with self.subTest(destination=destination):
                server = module.Server.__new__(module.Server)
                server.cwd = server.root = self.cwd.resolve()
                server.allow_any_cwd = False
                server.lock = threading.RLock()
                server.processes = {}
                server.launched_jobs = {}
                server.stopped = threading.Event()
                server.deadline = 10
                server.jobs_root = self.root / ("race-jobs-" + destination)
                cli_record = self.root / ("race-cli-" + destination + ".jsonl")
                server.env = dict(os.environ, **DEFAULTS, BRIDGE_ROOT=str(server.root),
                                  BRIDGE_JOBS=str(server.jobs_root),
                                  PYTHONDONTWRITEBYTECODE="1")
                # changed for DESIGN §8: the same isolation as every real-engine run (no relay, private homes, stub check)
                for key, value in self.real_engine_env().items():
                    if value is None:
                        server.env.pop(key, None)
                    else:
                        server.env[key] = value
                server.env.pop("CLAUDE_BRIDGE_ENGINE", None)
                if USE_FIXTURE_PS:
                    server.env.update({
                        "PATH": str(FIXTURES) + os.pathsep + server.env.get("PATH", ""),
                        "MCP_STUB_IDENTITIES": str(self.root / "race-identities"),
                        "MCP_STUB_PS_RECORD": str(self.root / "race-ps.jsonl"),
                    })
                original = self.cwd / ("original-" + destination)
                original.mkdir()
                prepared = module.prepare_call({"name": "claude_ask", "arguments": {
                    "prompt": "[[STUB:argv=%s,text=retained-directory]]" % cli_record,
                    "cwd": str(original),
                }}, server)
                call = module.Call("race-" + destination, *prepared)
                self.addCleanup(call.close_cwd)
                retained_fd = call.cwd_fd
                original_inode = original.stat().st_ino
                self.assertEqual(os.fstat(retained_fd).st_ino, original_inode)
                retained = (self.cwd if destination == "inside" else self.root) / ("retained-" + destination)
                original.rename(retained)
                # Point the original name across the opposite side of the root
                # boundary so reopening it would produce the wrong outcome.
                decoy = (self.root if destination == "inside" else self.cwd) / ("decoy-" + destination)
                decoy.mkdir()
                original.symlink_to(decoy, target_is_directory=True)
                with mock.patch.object(module, "ENGINE", REAL_ENGINE), \
                        mock.patch.object(module.subprocess, "Popen", wraps=subprocess.Popen) as launches:
                    response = {"result": server.run_engine(call)}
                engine_launches = [item for item in launches.call_args_list if str(REAL_ENGINE) in item.args[0]]
                self.assertEqual(engine_launches[0].kwargs["cwd"], "/dev/fd/%d" % retained_fd)
                self.assertIn(len(engine_launches), (1, 2))
                for launch in engine_launches:
                    self.assertIn(retained_fd, launch.kwargs["pass_fds"])
                    self.assertNotIn("preexec_fn", launch.kwargs)
                    self.assertNotIn("--cwd", launch.args[0])
                if len(engine_launches) == 2:
                    # Darwin cannot chdir through /dev/fd; the fallback must
                    # enter a fresh interpreter before touching the descriptor.
                    self.assertNotIn("cwd", engine_launches[1].kwargs)
                    self.assertIn("-c", engine_launches[1].args[0])
                if destination == "inside":
                    result = self.assert_result(response)
                    self.assertIn("retained-directory", result["content"][0]["text"])
                    metadata = json.loads((server.jobs_root / result["structuredContent"]["job"] /
                                           "meta.json").read_text(encoding="utf-8"))
                    self.assertEqual(metadata["cwd"], str(retained.resolve()))
                    self.assertEqual(Path(metadata["cwd"]).stat().st_ino, original_inode)
                    self.assertTrue(cli_record.exists())
                else:
                    result = self.assert_result(response, "failed", 2)
                    self.assertIn("root", result["content"][0]["text"].lower())
                    self.assertFalse(cli_record.exists(), "CLI started from outside the authorized root")
                for path in server.jobs_root.glob("*/meta.json"):
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                    if "cwd" in metadata:
                        saved = Path(metadata["cwd"]).resolve()
                        self.assertTrue(saved == server.root or server.root in saved.parents)
                with self.assertRaises(OSError):
                    os.fstat(retained_fd)

    def test_polling_uses_explicit_root_when_startup_cwd_is_outside_it(self):
        authorized = self.cwd / "authorized"
        authorized.mkdir()
        server = self.start({"CLAUDE_BRIDGE_ROOT": str(authorized)})
        self.assert_result(server.call("claude_task", {"prompt": "inspect", "cwd": str(authorized)}))
        for name in ("claude_status", "claude_result"):
            with self.subTest(tool=name):
                result = self.assert_result(server.call(name, {"job": JOB}))
                self.assertEqual(result["structuredContent"]["job"], JOB)
                self.assertEqual(server.records()[-1]["cwd"], str(authorized))

    def test_allow_any_cwd_requires_exact_startup_opt_in(self):
        outside = self.root / "outside"
        outside.mkdir()
        for value in ("true", "0", "1"):
            with self.subTest(value=value):
                server = self.start({"CLAUDE_BRIDGE_ALLOW_ANY_CWD": value})
                args = {"prompt": "x", "cwd": str(outside), "files": [str(self.cwd / "missing.txt")]}
                response = server.call("claude_ask", args)
                if value == "1":
                    self.assert_result(response)
                    self.assertEqual(server.records()[0]["cwd"], str(outside.resolve()))
                else:
                    self.assert_error(response, -32602, "cwd")
                    self.assertEqual(server.records(), [])

    def test_saved_job_cwd_is_validated_for_reply_status_and_result(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.cwd / "outside-link").symlink_to(outside, target_is_directory=True)
        server = self.start()
        for recorded_cwd in (str(outside), str(self.cwd / "outside-link")):
            self.saved_job(server, cwd=recorded_cwd)
            for name in ("claude_reply", "claude_status", "claude_result"):
                with self.subTest(tool=name, cwd=recorded_cwd):
                    args = {"job": JOB}
                    if name == "claude_reply":
                        args.update(prompt="follow up", cwd=str(self.cwd))
                    self.assert_error(server.call(name, args), -32602, "cwd")
        self.assertEqual(server.records(), [])
        opted_in = self.start({"CLAUDE_BRIDGE_ALLOW_ANY_CWD": "1"})
        self.saved_job(opted_in, cwd=outside)
        self.assert_result(opted_in.call("claude_reply", {"job": JOB, "prompt": "allowed"}))

    def test_missing_or_malformed_job_metadata_never_launches_engine(self):
        server = self.start()
        directory = self.saved_job(server)
        for metadata in (None, "{broken", "[]", "{}", '{"cwd": false}'):
            with self.subTest(metadata=metadata):
                path = directory / "meta.json"
                if metadata is None:
                    path.unlink()
                else:
                    path.write_text(metadata, encoding="utf-8")
                self.assert_error(server.call("claude_reply", {"job": JOB, "prompt": "x"}), -32602)
        self.assertEqual(server.records(), [])

    def test_custom_job_names_work_in_footer_reply_status_and_result(self):
        for job in ("My.custom_job-2", "x", "A" + "b" * 79):
            with self.subTest(job=job):
                server = self.start({"MCP_STUB_JOB": job})
                result = self.assert_result(server.call("claude_ask", {"prompt": "create custom job"}))
                self.assertEqual(result["structuredContent"]["job"], job)
                for name in ("claude_reply", "claude_status", "claude_result"):
                    args = {"job": job}
                    if name == "claude_reply":
                        args["prompt"] = "follow up"
                    result = self.assert_result(server.call(name, args))
                    self.assertEqual(result["structuredContent"]["job"], job)
                    self.assertEqual(server.records()[-1]["argv"][:2], [name.removeprefix("claude_"), job])

    def test_invalid_job_names_never_launch_engine(self):
        server = self.start()
        for job in ("", "../escape", "a/b", ".hidden", "-flag", "_prefix", "A" * 81,
                    "white space", "job\n", "é"):
            for name in ("claude_reply", "claude_status", "claude_result"):
                with self.subTest(job=job, tool=name):
                    args = {"job": job}
                    if name == "claude_reply":
                        args["prompt"] = "x"
                    self.assert_error(server.call(name, args), -32602, "job")
        self.assertEqual(server.records(), [])

    def test_latest_alias_follows_creation_time_for_custom_job_names(self):
        server = self.start()
        for job, created in (("z-older", "2026-09-01T00:00:00+00:00"),
                             ("a-newer", "2026-09-07T00:00:00+00:00")):
            directory = self.saved_job(server, job=job)
            metadata = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            metadata["created"] = created
            (directory / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        for alias in ("last", "latest"):
            result = self.assert_result(server.call("claude_status", {"job": alias}))
            self.assertEqual(result["structuredContent"]["job"], "a-newer")
            self.assertEqual(server.records()[-1]["argv"], ["status", "a-newer"])

    def test_relative_jobs_root_is_stable_when_a_job_uses_a_subdirectory(self):
        nested = self.cwd / "nested"
        nested.mkdir()
        server = self.start({"BRIDGE_JOBS": "relative-jobs"})
        response = server.call("claude_task", {"prompt": "sleep:0.2", "wait": False,
                                               "cwd": str(nested)})["result"]
        self.assertEqual(response["structuredContent"]["status"], "started")
        record = server.wait_records()[0]
        self.assertEqual(record["env"]["BRIDGE_JOBS"], str(self.cwd / "relative-jobs"))
        self.assertEqual(Path(record["job_dir"]), self.cwd / "relative-jobs" / JOB)
        result = self.assert_result(server.call("claude_status", {"job": JOB}))
        self.assertEqual(result["structuredContent"]["job"], JOB)

    def test_detached_status_can_poll_before_cwd_metadata_is_published(self):
        server = self.start({"MCP_STUB_META_DELAY": "1"})
        response = server.call("claude_task", {"prompt": "sleep:0.1", "wait": False})["result"]
        self.assertEqual(response["structuredContent"]["status"], "started")
        result = self.assert_result(server.call("claude_status", {"job": JOB}))
        self.assertIn("state=running", result["content"][0]["text"])
        self.assert_error(server.call("claude_reply", {"job": JOB, "prompt": "x"}), -32602)
        # Once present, an explicit unauthorized cwd overrides no cache entry.
        outside = self.root / "outside"
        outside.mkdir()
        self.saved_job(server, cwd=outside)
        count = len(server.records())
        self.assert_error(server.call("claude_status", {"job": JOB}), -32602, "cwd")
        self.assert_error(server.call("claude_result", {"job": JOB}), -32602, "cwd")
        self.assertEqual(len(server.records()), count)

    def test_detached_jobs_survive_deadline_and_shutdown_then_can_be_collected(self):
        for index, (name, signum) in enumerate((("claude_review", None), ("claude_task", signal.SIGTERM),
                                               ("claude_task", signal.SIGINT))):
            with self.subTest(tool=name, signal=signum):
                job = "detached-%d" % index
                server = self.start({"CLAUDE_BRIDGE_TOOL_DEADLINE": "0.3", "MCP_STUB_JOB": job})
                args = {"wait": False, "intent" if name == "claude_review" else "prompt": "sleep:2.5"}
                response = server.call(name, args)["result"]
                self.assertIs(response["isError"], False)
                self.assertEqual(response["structuredContent"]["status"], "started")
                self.assertEqual(response["structuredContent"]["job"], job)
                record = server.wait_records()[0]
                self.assertEqual(os.getsid(record["pid"]), record["pid"])
                status = self.assert_result(server.call("claude_status", {"job": job}))
                self.assertIn("state=running", status["content"][0]["text"])
                self.assertEqual(status["structuredContent"]["job"], job)
                # Outlive the request deadline before shutting down the server.
                time.sleep(0.4)
                self.assertTrue(process_exists(record["pid"]))
                if signum is None:
                    server.process.stdin.close()
                else:
                    server.process.send_signal(signum)
                server.process.wait(timeout=4)
                self.assertTrue(process_exists(record["pid"]), "Detached engine died with the server")
                metadata = Path(record["job_dir"]) / "meta.json"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        if json.loads(metadata.read_text(encoding="utf-8"))["state"] == "done":
                            break
                    except ValueError:
                        pass  # The fixture may be completing its metadata write.
                    time.sleep(0.02)
                self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["state"], "done")
                collector = self.start()
                status = self.assert_result(collector.call("claude_status", {"job": job}))
                self.assertIn("state=done", status["content"][0]["text"])
                result = self.assert_result(collector.call("claude_result", {"job": job}))
                self.assertEqual(result["structuredContent"]["job"], job)
                self.assertIn("stub result: sleep:2.5", result["content"][0]["text"])

    def test_wait_true_review_and_task_remain_subject_to_deadline(self):
        for name in ("claude_review", "claude_task"):
            for explicit in (False, True):
                with self.subTest(tool=name, explicit=explicit):
                    server = self.start({"CLAUDE_BRIDGE_TOOL_DEADLINE": "0.3"})
                    args = {"intent" if name == "claude_review" else "prompt": "sleep:30"}
                    if explicit:
                        args["wait"] = True
                    result = server.call(name, args)["result"]
                    self.assertIs(result["isError"], True)
                    self.assertEqual(result["structuredContent"]["status"], "timeout")
                    self.assertEqual(result["structuredContent"]["job"], JOB)
                    self.assert_pid_gone(server.records()[0]["pid"])

    def test_cancellation_before_started_delivery_kills_engine_and_owned_seats(self):
        for tool in ("claude_review", "claude_task"):
            with self.subTest(tool=tool):
                server, record, gate = self.begin_pending_started(tool=tool)
                server.assert_quiet()
                server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                             "params": {"requestId": "pending"}})
                for field in ("pid", "seat_pid", "seat_child_pid"):
                    self.assert_pid_gone(record[field])
                (gate / "release").touch()
                self.assertEqual(server.request("ping", request_id="after-cancel")["result"], {})
                server.assert_quiet()

    def test_shutdown_before_started_delivery_kills_engine_and_owned_seats(self):
        for signum in (None, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                server, record, _gate = self.begin_pending_started()
                if signum is None:
                    server.process.stdin.close()
                else:
                    server.process.send_signal(signum)
                server.process.wait(timeout=8)
                for field in ("pid", "seat_pid", "seat_child_pid"):
                    self.assert_pid_gone(record[field])
                server.assert_quiet()

    def test_transport_failure_during_started_delivery_kills_engine_and_owned_seats(self):
        for mode in ("before", "partial"):
            with self.subTest(delivery=mode):
                server, record, gate = self.begin_pending_started(mode=mode, drain_stdout=False)
                if mode == "partial":
                    prefix = os.read(server.process.stdout.fileno(), 13)
                    self.assertEqual(len(prefix), 13)
                    self.assertNotIn(b"\n", prefix)
                server.process.stdout.close()
                (gate / "release").touch()
                for field in ("pid", "seat_pid", "seat_child_pid"):
                    self.assert_pid_gone(record[field])
                # Output failure must also wake an otherwise idle stdin loop.
                server.process.wait(timeout=8)
                server.process.stdin.close()
                self.assertIn("transport failed", "".join(server.stderr).lower())

    def test_cancellation_after_partial_started_write_kills_engine_and_owned_seats(self):
        server, record, _gate = self.begin_pending_started(mode="partial", drain_stdout=False)
        prefix = os.read(server.process.stdout.fileno(), 13)
        self.assertEqual(len(prefix), 13)
        self.assertNotIn(b"\n", prefix)
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": "pending"}})
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(record[field])
        # The abandoned frame closes the transport without client cooperation.
        server.process.wait(timeout=8)
        server.process.stdin.close()
        self.assertNotIn("\n", server.process.stdout.read(), "Cancelled partial response was committed")

    def test_engine_exit_before_started_delivery_cleans_up_owned_seats(self):
        server, record, gate = self.begin_pending_started()
        os.kill(record["pid"], signal.SIGKILL)
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(record[field])
        server.assert_quiet()
        (gate / "release").touch()
        response = server.read()["result"]
        self.assertIs(response["isError"], False)
        self.assertEqual(response["structuredContent"]["status"], "started")
        self.assertEqual(response["structuredContent"]["job"], record["job"])

    def test_deadline_before_started_delivery_replaces_queued_handoff_with_timeout(self):
        server, record, gate = self.begin_pending_started(overrides={"CLAUDE_BRIDGE_TOOL_DEADLINE": "0.7"})
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(record[field])
        server.assert_quiet()
        (gate / "release").touch()
        response = server.read()
        self.assertEqual(response["id"], "pending")
        result = self.assert_result(response, "timeout", -signal.SIGTERM)
        self.assertEqual(result["structuredContent"]["job"], record["job"])
        self.assertEqual(server.request("ping", request_id="after-timeout")["result"], {})
        server.assert_quiet()

    def test_delivered_started_response_preserves_engine_and_owned_seats_on_immediate_eof(self):
        server, record, gate = self.begin_pending_started()
        (gate / "release").touch()
        response = server.read()["result"]
        self.assertIs(response["isError"], False)
        self.assertEqual(response["structuredContent"]["status"], "started")
        self.assertEqual(response["structuredContent"]["job"], record["job"])
        server.process.stdin.close()
        server.process.wait(timeout=8)
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assertTrue(process_exists(record[field]), "Committed handoff did not preserve " + field)

    def test_partial_result_remains_usable(self):
        result = self.assert_result(self.start().call("claude_ask", {"prompt": "exit:3"}), "partial", 3)
        self.assertTrue(result["content"][0]["text"].startswith("PARTIAL: some seats failed\n"))
        self.assertEqual(result["structuredContent"]["job"], JOB)

    def test_detached_result_preserves_success_and_partial_exit_codes(self):
        for state, code, status in (("done", 0, "ok"), ("partial", 3, "partial"),
                                    ("running", 3, "partial")):
            with self.subTest(state=state):
                server = self.start({"MCP_STUB_RESULT_EXIT": str(code)})
                self.saved_job(server, state=state)
                result = self.assert_result(server.call("claude_result", {"job": JOB}), status, code)
                self.assertEqual(result["structuredContent"]["job"], JOB)
                self.assertEqual(result["content"][0]["text"].startswith("PARTIAL:"), code == 3)

    def test_detached_failed_and_cancelled_results_name_the_persisted_state(self):
        for state in ("failed", "cancelled"):
            with self.subTest(state=state):
                server = self.start({"MCP_STUB_RESULT_EXIT": "1", "MCP_STUB_RESULT_HIDE_STATE": "1"})
                self.saved_job(server, state=state)
                result = self.assert_result(server.call("claude_result", {"job": JOB}), "failed", 1)
                self.assertEqual(result["structuredContent"]["job"], JOB)
                self.assertIn(state, result["content"][0]["text"].lower())

    def test_detached_result_exit_two_is_invalid(self):
        server = self.start({"MCP_STUB_RESULT_EXIT": "2"})
        self.saved_job(server)
        result = self.assert_result(server.call("claude_result", {"job": JOB}), "invalid", 2)
        self.assertEqual(result["structuredContent"]["job"], JOB)
        self.assertIn("invalid", result["content"][0]["text"].lower())

    def test_failure_appends_stderr_tail_and_preserves_exit_code(self):
        stderr = "discard-this-prefix" + "E" * 2500 + "stderr-tail-marker"
        server = self.start({"MCP_STUB_STDERR": stderr})
        for code in (1, 2):
            result = self.assert_result(server.call("claude_ask", {"prompt": "exit:%d" % code}), "failed", code)
            text = result["content"][0]["text"]
            self.assertIn("stub result", text)
            self.assertIn(stderr[-2000:], text)
            self.assertNotIn("discard-this-prefix", text)

    def test_killed_engine_is_a_failed_result(self):
        result = self.assert_result(self.start().call("claude_ask", {"prompt": "signal-self"}), "failed", -signal.SIGTERM)
        self.assertEqual(result["structuredContent"]["job"], JOB)

    def test_engine_exit_cleans_up_its_nonterminal_seat_group(self):
        cases = (("return-zero", "seat-exit:0", 0), ("return-one", "seat-exit:1", 1),
                 ("hard-exit", "seat-hard-exit:1", 1), ("external-kill", "seat-sleep:30", -signal.SIGKILL))
        for label, prompt, code in cases:
            with self.subTest(exit=label):
                server = self.start({"MCP_STUB_JOB": "orphan-" + label})
                record = self.begin_sleep(server, prompt=prompt)
                if label == "external-kill":
                    os.kill(record["pid"], signal.SIGKILL)
                else:
                    Path(record["exit_release"]).touch()
                result = self.assert_result(server.read(), "ok" if code == 0 else "failed", code)
                self.assertEqual(result["structuredContent"]["job"], record["job"])
                for field in ("pid", "seat_pid", "seat_child_pid"):
                    self.assert_pid_gone(record[field])
                self.assertEqual(server.request("ping", request_id="after-exit")["result"], {})

    def test_engine_exit_preserves_terminal_seat_records(self):
        for state in ("done", "failed", "cancelled"):
            with self.subTest(state=state):
                server = self.start({"MCP_STUB_JOB": "terminal-" + state})
                record = self.begin_sleep(server, prompt="seat-hard-exit:1")
                seat_path = Path(record["job_dir"]) / ("seat-fixture-%d.json" % record["pid"])
                seat = json.loads(seat_path.read_text(encoding="utf-8"))
                seat["state"] = state
                seat_path.write_text(json.dumps(seat), encoding="utf-8")
                Path(record["exit_release"]).touch()
                self.assert_result(server.read(), "failed", 1)
                self.assert_pid_gone(record["pid"])
                self.assertTrue(process_exists(record["seat_pid"]))
                self.assertTrue(process_exists(record["seat_child_pid"]))

    def test_engine_exit_preserves_another_replys_seat_group(self):
        server = self.start()
        self.saved_job(server)
        records = []
        for request_id, prompt in (("crashing", "seat-hard-exit:1"), ("surviving", "seat-sleep:30")):
            server.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                         "params": {"name": "claude_reply", "arguments": {
                             "job": JOB, "seat": request_id, "prompt": prompt,
                         }}})
            records.append(server.wait_records(len(records) + 1)[-1])
        crashing, surviving = records
        Path(crashing["exit_release"]).touch()
        response = server.read()
        self.assertEqual(response["id"], "crashing")
        self.assert_result(response, "failed", 1)
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(crashing[field])
            self.assertTrue(process_exists(surviving[field]), "Engine exit killed another invocation's " + field)
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": "surviving"}})
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(surviving[field])
        server.assert_quiet()

    def test_engine_exit_cleans_up_a_verified_leaderless_seat_group(self):
        server, record = self.begin_leaderless()
        Path(record["exit_release"]).touch()
        self.assert_result(server.read(), "failed", 1)
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(record[field])
        probes = [json.loads(line) for line in Path(server.env["MCP_STUB_PS_RECORD"]).read_text().splitlines()]
        self.assertIn(["-axo", "pid=,pgid=,lstart="], probes)

    def test_leaderless_cleanup_requires_a_complete_verified_group_snapshot(self):
        for mode in ("older", "malformed", "unavailable", "empty", "leader-present"):
            with self.subTest(snapshot=mode):
                server, record = self.begin_leaderless({"MCP_STUB_PS_GROUP_MODE": mode,
                                                        "MCP_STUB_JOB": "snapshot-" + mode})
                Path(record["exit_release"]).touch()
                self.assert_result(server.read(), "failed", 1)
                self.assert_pid_gone(record["pid"])
                self.assertTrue(process_exists(record["seat_child_pid"]))
                probes = [json.loads(line) for line in Path(server.env["MCP_STUB_PS_RECORD"]).read_text().splitlines()]
                self.assertIn(["-axo", "pid=,pgid=,lstart="], probes)

    def test_engine_exit_preserves_leaderless_group_with_a_newer_member(self):
        server, record = self.begin_leaderless({"MCP_STUB_PS_GROUP_MODE": "newer"})
        Path(record["exit_release"]).touch()
        self.assert_result(server.read(), "failed", 1)
        self.assert_pid_gone(record["pid"])
        self.assertTrue(process_exists(record["seat_child_pid"]))
        probes = [json.loads(line) for line in Path(server.env["MCP_STUB_PS_RECORD"]).read_text().splitlines()]
        self.assertIn(["-axo", "pid=,pgid=,lstart="], probes)

    def test_leaderless_group_members_must_fit_the_inclusive_time_window(self):
        spec = importlib.util.spec_from_file_location("mcp_server_group_window_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        group = 12345
        started = "Mon Sep  7 12:34:56 2026"
        origin = time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))
        observed = origin + 10
        cases = (("earliest", [-1], True), ("too-early", [-2], False),
                 ("leader-start", [0], True), ("exit-time", [10], True),
                 ("latest", [11], True), ("too-late", [12], False),
                 ("mixed-newer", [0, 12], False), ("mixed-older", [0, -2], False))
        for label, offsets, expected in cases:
            with self.subTest(boundary=label):
                snapshot = "\n".join("%d %d %s" % (group + index + 1, group,
                                                     time.asctime(time.localtime(origin + offset)))
                                     for index, offset in enumerate(offsets))
                with mock.patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess(
                        ["ps"], 0, stdout=snapshot, stderr="")):
                    self.assertIs(module.leaderless_group_owned(group, started,
                                                               engine_exit_observed=observed), expected)

    def test_leaderless_cleanup_without_a_valid_exit_bound_never_kills(self):
        spec = importlib.util.spec_from_file_location("mcp_server_group_bound_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        group, supervisor = 12345, 12344
        started = "Mon Sep  7 12:34:56 2026"
        directory = self.root / "unbounded-job"
        directory.mkdir()
        (directory / "seat-fixture.json").write_text(json.dumps({
            "pid": group, "pgid": group, "pid_start": started, "state": "running",
            "supervisor_pid": supervisor, "supervisor_start": started,
        }), encoding="utf-8")
        snapshot = "%d %d %s" % (group + 1, group, started)
        for bound in (None, float("nan"), float("inf"), -float("inf"), "invalid", {}, False):
            with self.subTest(bound=bound), \
                    mock.patch.object(module.os, "getpgid", side_effect=ProcessLookupError()), \
                    mock.patch.object(module.os, "killpg") as kill, \
                    mock.patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess(
                        ["ps"], 0, stdout=snapshot, stderr="")):
                self.assertFalse(module.leaderless_group_owned(group, started, engine_exit_observed=bound))
                module.kill_seats(directory, supervisor, started, engine_exit_observed=bound)
                kill.assert_not_called()

    def test_termination_records_the_engine_exit_before_waiting_for_its_group(self):
        spec = importlib.util.spec_from_file_location("mcp_server_exit_observation_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        process = mock.Mock(pid=12345, returncode=0)
        process.poll.return_value = process.wait.return_value = 0
        elapsed = [0.0]
        observed = []

        def advance(_seconds):
            elapsed[0] += module.TERMINATE_GRACE + 1

        def record_first_exit():
            if not observed:
                observed.append(1000.0 + elapsed[0])

        with mock.patch.object(module.os, "killpg") as kill, \
                mock.patch.object(module.time, "monotonic", side_effect=lambda: elapsed[0]), \
                mock.patch.object(module.time, "sleep", side_effect=advance):
            module.terminate_group(process, on_exit=record_first_exit)
        self.assertEqual(observed, [1000.0])
        self.assertGreater(elapsed[0], module.TERMINATE_GRACE)
        self.assertIn(mock.call(process.pid, signal.SIGKILL), kill.call_args_list)
        process.wait.assert_called_once()

    def test_leaderless_cleanup_requires_recorded_group_and_supervisor_identity(self):
        cases = (("missing-group", "pgid", None), ("mismatched-group", "pgid", 999999),
                 ("malformed-group", "pgid", "not a group"), ("boolean-group", "pgid", True),
                 ("missing-owner", "supervisor_pid", None), ("mismatched-owner", "supervisor_pid", 999999),
                 ("missing-owner-start", "supervisor_start", None),
                 ("mismatched-owner-start", "supervisor_start", "Mon Jan  1 00:00:00 1900"),
                 ("missing-leader-start", "pid_start", None), ("malformed-leader-start", "pid_start", "invalid"))
        for label, field, value in cases:
            with self.subTest(identity=label):
                server, record = self.begin_leaderless({"MCP_STUB_JOB": "leaderless-" + label})
                path = Path(record["job_dir"]) / ("seat-fixture-%d.json" % record["pid"])
                seat = json.loads(path.read_text(encoding="utf-8"))
                if value is None:
                    seat.pop(field)
                else:
                    seat[field] = value
                path.write_text(json.dumps(seat), encoding="utf-8")
                Path(record["exit_release"]).touch()
                self.assert_result(server.read(), "failed", 1)
                self.assert_pid_gone(record["pid"])
                self.assertTrue(process_exists(record["seat_child_pid"]))

    def test_live_stale_seat_identity_never_uses_leaderless_fallback(self):
        server = self.start({"MCP_STUB_FORCE_PS": "1", "MCP_STUB_SEAT_IDENTITY": "stale"})
        record = self.begin_sleep(server, prompt="seat-hard-exit:1")
        Path(record["exit_release"]).touch()
        self.assert_result(server.read(), "failed", 1)
        self.assert_pid_gone(record["pid"])
        self.assertTrue(process_exists(record["seat_pid"]))
        self.assertTrue(process_exists(record["seat_child_pid"]))
        probes = [json.loads(line) for line in Path(server.env["MCP_STUB_PS_RECORD"]).read_text().splitlines()]
        self.assertIn(["-o", "lstart=", "-p", str(record["seat_pid"])], probes)
        self.assertNotIn(["-axo", "pid=,pgid=,lstart="], probes)

    def test_leaderless_cleanup_rechecks_that_the_leader_has_not_reappeared(self):
        spec = importlib.util.spec_from_file_location("mcp_server_group_race_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        group = 12345
        supervisor = 12344
        started = "Mon Sep  7 12:34:56 2026"
        directory = self.root / "mock-job"
        directory.mkdir()
        (directory / "seat-fixture.json").write_text(json.dumps({
            "pid": group, "pgid": group, "pid_start": started, "state": "running",
            "supervisor_pid": supervisor, "supervisor_start": started,
        }), encoding="utf-8")
        with mock.patch.object(module.os, "getpgid", side_effect=[ProcessLookupError(), group]) as lookup, \
                mock.patch.object(module.os, "killpg") as kill, \
                mock.patch.object(module, "leaderless_group_owned", return_value=True) as ownership:
            observed = time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y")) + 10
            module.kill_seats(directory, supervisor, started, engine_exit_observed=observed)
        self.assertEqual(lookup.call_args_list, [mock.call(group), mock.call(group)])
        ownership.assert_called_once()
        kill.assert_called_once_with(group, 0)

    def test_text_is_bounded_and_job_is_parsed_before_truncation(self):
        server = self.start({"MCP_STUB_STDOUT_LENGTH": "20000"})
        result = self.assert_result(server.call("claude_ask", {"prompt": "large"}))
        self.assertEqual(result["structuredContent"]["job"], JOB)

    def test_large_failure_output_retains_stderr_tail_within_text_limit(self):
        stderr = "discard-prefix" + "E" * 2500 + "stderr-end"
        server = self.start({"MCP_STUB_STDOUT_LENGTH": "20000", "MCP_STUB_STDERR": stderr,
                             "MCP_STUB_EXIT": "1"})
        result = self.assert_result(server.call("claude_ask", {"prompt": "large failure"}), "failed", 1)
        self.assertIn(stderr[-2000:], result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["job"], JOB)

    def test_environment_preserves_login_variables_and_sets_claude_defaults(self):
        passthrough = {
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token",
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "CODEX_HOME": str(self.root / "codex-home"),
            "BRIDGE_JOBS": str(self.root / "jobs"),
            "CLAUDE_BIN": "fixture-claude", "CODEX_BIN": "fixture-codex",
            "MCP_PASSTHROUGH_SENTINEL": "preserved",
        }
        server = self.start(passthrough)
        self.assert_result(server.call("claude_ask", {"prompt": "environment"}))
        environment = server.wait_records()[0]["env"]
        # changed for S-DEF: the Claude defaults reach the engine as BRIDGE_BUILTIN_*; the user-facing seat variables stay unset
        # (the user set none) and BRIDGE_DEFAULTS_SUPPLIED is gone
        for key, value in dict(BUILTINS, BRIDGE_CALLER=DEFAULTS["BRIDGE_CALLER"], **passthrough).items():
            self.assertEqual(environment[key], value)
        for key in (*USER_SEAT_VARS, "BRIDGE_DEFAULTS_SUPPLIED"):
            self.assertIsNone(environment[key], key)
        self.assertEqual(environment["BRIDGE_ROOT"], str(self.cwd.resolve()))

    def test_explicit_bridge_defaults_are_preserved(self):
        overrides = {key: "custom-" + key.lower() for key in DEFAULTS}
        server = self.start(overrides)
        self.assert_result(server.call("claude_ask", {"prompt": "environment"}))
        environment = server.wait_records()[0]["env"]
        for key, value in overrides.items():
            self.assertEqual(environment[key], value)

    def test_concurrent_responses_are_complete_json_lines(self):
        server = self.start()
        for request_id in range(8):
            server.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                         "params": {"name": "claude_ask", "arguments": {"prompt": "sleep:0.05"}}})
        responses = [server.read() for _ in range(8)]
        self.assertEqual({response["id"] for response in responses}, set(range(8)))
        for response in responses:
            self.assert_result(response)

    def test_cancellation_kills_engine_and_suppresses_response(self):
        server = self.start()
        record = self.begin_sleep(server)
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "sleeping"}})
        self.assert_pid_gone(record["pid"])
        self.assertEqual(server.request("ping", request_id="still-serving")["result"], {})
        server.assert_quiet()

    def test_cancellation_kills_descendants_in_the_process_group(self):
        server = self.start()
        record = self.begin_sleep(server, prompt="group-sleep:30")
        self.assertTrue(process_exists(record["child_pid"]))
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "sleeping"}})
        self.assert_pid_gone(record["pid"])
        self.assert_pid_gone(record["child_pid"])
        self.assertEqual(server.request("ping")["result"], {})
        server.assert_quiet()

    def test_cancellation_kills_separately_sessioned_seat_and_descendant(self):
        server = self.start()
        record = self.begin_sleep(server, prompt="seat-sleep:30")
        self.assertEqual(os.getsid(record["seat_pid"]), record["seat_pid"])
        self.assertEqual(os.getpgid(record["seat_child_pid"]), record["seat_pid"])
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": "sleeping"}})
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(record[field])
        self.assertEqual(server.request("ping")["result"], {})
        server.assert_quiet()
        if USE_FIXTURE_PS:
            probes = [json.loads(line) for line in Path(server.env["MCP_STUB_PS_RECORD"]).read_text().splitlines()]
            self.assertIn(["-o", "lstart=", "-p", str(record["seat_pid"])], probes)

    def test_seat_cleanup_requires_a_recorded_matching_identity(self):
        for identity in ("missing", "stale"):
            with self.subTest(identity=identity):
                server = self.start({"MCP_STUB_SEAT_IDENTITY": identity,
                                     "MCP_STUB_JOB": "identity-" + identity})
                record = self.begin_sleep(server, prompt="seat-sleep:30")
                server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                             "params": {"requestId": "sleeping"}})
                self.assert_pid_gone(record["pid"])
                server.assert_quiet(timeout=0.3)
                self.assertTrue(process_exists(record["seat_pid"]))
                self.assertTrue(process_exists(record["seat_child_pid"]))

    def test_seat_cleanup_requires_matching_supervisor_pid_and_start(self):
        for identity in ("missing", "stale", "other"):
            with self.subTest(identity=identity):
                server = self.start({"MCP_STUB_SUPERVISOR_IDENTITY": identity,
                                     "MCP_STUB_JOB": "supervisor-" + identity})
                record = self.begin_sleep(server, prompt="seat-sleep:30")
                server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                             "params": {"requestId": "sleeping"}})
                self.assert_pid_gone(record["pid"])
                server.assert_quiet(timeout=0.3)
                self.assertTrue(process_exists(record["seat_pid"]))
                self.assertTrue(process_exists(record["seat_child_pid"]))

    def test_cancelling_one_reply_preserves_the_other_invocations_seats(self):
        server = self.start()
        self.saved_job(server)
        records = []
        for request_id in ("reply-one", "reply-two"):
            server.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                         "params": {"name": "claude_reply", "arguments": {
                             "job": JOB, "seat": request_id, "prompt": "seat-sleep:30",
                         }}})
            records.append(server.wait_records(len(records) + 1)[-1])
        first, second = records
        self.assertEqual(first["job"], second["job"])
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertNotEqual(first["seat_pid"], second["seat_pid"])
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": "reply-one"}})
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(first[field])
            self.assertTrue(process_exists(second[field]), "Cancelling one reply killed another reply's " + field)
        self.assertEqual(server.request("ping", request_id="between-cancellations")["result"], {})
        server.assert_quiet()
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": "reply-two"}})
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(second[field])
        server.assert_quiet()

    def test_unknown_cancellation_does_not_cancel_a_future_request(self):
        server = self.start()
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "later"}})
        self.assert_result(server.call("claude_ask", {"prompt": "still runs"}, request_id="later"))

    def test_deadline_kills_separately_sessioned_seat_and_descendant(self):
        server = self.start({"CLAUDE_BRIDGE_TOOL_DEADLINE": "0.5"})
        record = self.begin_sleep(server, prompt="seat-sleep:30")
        response = server.read(timeout=8)["result"]
        self.assertEqual(response["structuredContent"]["status"], "timeout")
        self.assertEqual(response["structuredContent"]["job"], JOB)
        for field in ("pid", "seat_pid", "seat_child_pid"):
            self.assert_pid_gone(record[field])

    def test_eof_and_signals_kill_separately_sessioned_seat_groups(self):
        for index, signum in enumerate((None, signal.SIGTERM, signal.SIGINT)):
            with self.subTest(signal=signum):
                server = self.start({"MCP_STUB_JOB": "shutdown-%d" % index})
                record = self.begin_sleep(server, prompt="seat-sleep:30")
                if signum is None:
                    server.process.stdin.close()
                else:
                    server.process.send_signal(signum)
                server.process.wait(timeout=8)
                for field in ("pid", "seat_pid", "seat_child_pid"):
                    self.assert_pid_gone(record[field])

    def test_cancellation_escalates_for_an_uncooperative_engine(self):
        server = self.start()
        record = self.begin_sleep(server, prompt="ignore-term-sleep:30")
        started = time.monotonic()
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "sleeping"}})
        # The five-second termination grace must not block the transport.
        server.send({"jsonrpc": "2.0", "id": "responsive", "method": "ping"})
        self.assertEqual(server.read(timeout=2)["id"], "responsive")
        self.assert_pid_gone(record["pid"], timeout=7)
        self.assertGreaterEqual(time.monotonic() - started, 4.8)
        server.assert_quiet()

    def test_immediate_cancellation_does_not_lose_the_launch_race(self):
        server = self.start()
        call = {"jsonrpc": "2.0", "id": "race", "method": "tools/call",
                "params": {"name": "claude_ask", "arguments": {"prompt": "sleep:30"}}}
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "race"}}
        server.send_raw(json.dumps(call) + "\n" + json.dumps(cancel))
        self.assertEqual(server.request("ping", request_id="after-race")["result"], {})
        server.assert_quiet(timeout=0.5)
        for record in server.records():
            self.assert_pid_gone(record["pid"])

    def test_deadline_kills_engine_and_returns_timeout(self):
        server = self.start({"CLAUDE_BRIDGE_TOOL_DEADLINE": "2"})
        record = self.begin_sleep(server)
        response = server.read(timeout=8)
        self.assertEqual(response["id"], "sleeping")
        result = response["result"]
        self.assertIs(result["isError"], True)
        self.assertEqual(result["structuredContent"]["status"], "timeout", result)
        self.assertIn("exit_code", result["structuredContent"])
        self.assertIn("job", result["structuredContent"])
        self.assertGreaterEqual(result["structuredContent"]["elapsed_seconds"], 1.8)
        self.assertRegex(result["content"][0]["text"].lower(), r"timeout|timed out")
        self.assert_pid_gone(record["pid"])
        self.assertEqual(server.request("ping")["result"], {})

    def test_eof_exits_and_kills_every_sleeping_engine(self):
        server = self.start()
        records = [self.begin_sleep(server, request_id="sleep-%d" % index) for index in range(3)]
        server.process.stdin.close()
        server.process.wait(timeout=8)
        for record in records:
            self.assert_pid_gone(record["pid"])

    def test_sigterm_and_sigint_kill_sleeping_engine(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                server = self.start()
                record = self.begin_sleep(server)
                server.process.send_signal(signum)
                server.process.wait(timeout=8)
                self.assert_pid_gone(record["pid"])

    def assert_shutdown_with_unread_stdout(self, signum=None):
        server = self.start(drain_stdout=False)
        record = self.begin_sleep(server)
        messages = [{
            "jsonrpc": "2.0", "id": "refused-%d" % index, "method": "tools/call",
            "params": {"name": "claude_task", "arguments": {"prompt": "edit", "write": True}},
        } for index in range(400)]
        payload = ("\n".join(json.dumps(message) for message in messages) + "\n").encode("utf-8")
        # A separate raw descriptor avoids a blocked TextIOWrapper write
        # holding its lock during cleanup of a broken implementation.
        input_fd = os.dup(server.process.stdin.fileno())
        server.process.stdin.close()
        input_finished = threading.Event()

        def feed_requests():
            try:
                remaining = memoryview(payload)
                while remaining:
                    remaining = remaining[os.write(input_fd, remaining):]
            except BrokenPipeError:
                pass  # Expected when the signal case shuts down mid-input.
            finally:
                os.close(input_fd)
                input_finished.set()

        sender = threading.Thread(target=feed_requests, daemon=True)
        sender.start()
        if signum is None:
            self.assertTrue(input_finished.wait(timeout=5), "Unread stdout blocked stdin processing before EOF")
        else:
            # Leave stdout undrained long enough for refused-write results to
            # fill the output pipe before signalling shutdown.
            time.sleep(0.5)
            server.process.send_signal(signum)
        server.process.wait(timeout=8)
        sender.join(timeout=2)
        self.assertFalse(sender.is_alive(), "Input sender did not stop after server shutdown")
        self.assert_pid_gone(record["pid"])

    def test_eof_with_unread_stdout_kills_engine_and_exits(self):
        self.assert_shutdown_with_unread_stdout()

    def test_sigterm_with_unread_stdout_kills_engine_and_exits(self):
        self.assert_shutdown_with_unread_stdout(signal.SIGTERM)

    def test_call_diagnostics_go_to_stderr(self):
        server = self.start()
        self.assert_result(server.call("claude_ask", {"prompt": "logged", "seats": "opus:max"}, request_id="log-request"))
        deadline = time.monotonic() + 2
        while "elapsed" not in "".join(server.stderr).lower() and time.monotonic() < deadline:
            time.sleep(0.02)
        logs = "".join(server.stderr)
        for value in ("claude_ask", "log-request", str(self.cwd.resolve()), "opus:max"):
            self.assertIn(value, logs)
        self.assertRegex(logs.lower(), r"exit[^\n]*0")
        self.assertIn("elapsed", logs.lower())
        server.assert_quiet()

    def test_thin_entrypoint_honors_engine_override_and_defaults(self):
        # changed for S-DEF: the entry point passes its defaults as BRIDGE_BUILTIN_* and never sets the user-facing seat variables
        # (nor BRIDGE_DEFAULTS_SUPPLIED); values the user set pass through untouched
        for preserve in (False, True):
            with self.subTest(preserve=preserve):
                server = self.start()  # Reuse the isolated test environment.
                environment = dict(server.env)
                expected = dict(BUILTINS, BRIDGE_CALLER=DEFAULTS["BRIDGE_CALLER"], BRIDGE_DEFAULTS_SUPPLIED=None,
                                **{key: None for key in USER_SEAT_VARS})
                if preserve:
                    expected = {key: "override-" + key.lower() for key in DEFAULTS}
                    environment.update(expected)
                    expected["BRIDGE_DEFAULTS_SUPPLIED"] = None
                completed = subprocess.run(
                    [sys.executable, str(ENTRYPOINT), "ask", "--", "exit:3"],
                    cwd=str(self.cwd), env=environment, capture_output=True,
                    text=True, encoding="utf-8", timeout=5,
                )
                self.assertEqual(completed.returncode, 3, completed.stderr)
                self.assertIn("— job: " + JOB, completed.stdout)
                self.assertIn("[bridge] job " + JOB, completed.stderr)
                record = server.wait_records()[0]
                self.assertEqual(record["argv"], ["ask", "--", "exit:3"])
                for key, value in expected.items():
                    self.assertEqual(record["env"][key], value)

    def test_server_and_entrypoint_resolve_engine_before_and_after_directory_move(self):
        for layout in ("skills", "codex-skills"):
            with self.subTest(layout=layout):
                plugin = self.root / ("plugin-" + layout)
                scripts = plugin / layout / "claude" / "scripts"
                engine = plugin / "skills" / "codex" / "scripts" / "bridge.py"
                scripts.mkdir(parents=True)
                engine.parent.mkdir(parents=True)
                shutil.copy2(SERVER, scripts / SERVER.name)
                shutil.copy2(ENTRYPOINT, scripts / ENTRYPOINT.name)
                shutil.copy2(STUB, engine)
                server = self.start({"CLAUDE_BRIDGE_ENGINE": ""}, server_path=scripts / SERVER.name)
                self.assert_result(server.call("claude_ask", {"prompt": "layout"}))
                completed = subprocess.run(
                    [sys.executable, str(scripts / ENTRYPOINT.name), "ask"],
                    input="entry layout", cwd=str(self.cwd), env=server.env,
                    capture_output=True, text=True, encoding="utf-8", timeout=5,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("stub result: entry layout", completed.stdout)
                self.assertEqual(server.wait_records(2)[1]["stdin"], "entry layout")


# ==================================================================================================================
# The models change (DESIGN rev 13) through the MCP server and the REAL engine: classes named by their DESIGN §8 group
# ==================================================================================================================
class ModelsMCPCase(MCPBase):
    def ask(self, server, arguments, request_id="ask", timeout=60):
        server.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "claude_ask", "arguments": arguments}})
        return self.assert_result(server.read(timeout=timeout))

    def git_workspace(self):
        def run(*args):
            completed = fixture_git(self.root, self.cwd, *args)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (self.cwd / "store.py").write_text("A = 1\n")
        run("add", "-A")
        run("commit", "-q", "--no-verify", "--no-gpg-sign", "-m", "base")
        (self.cwd / "store.py").write_text("A = 2\n")


class GroupA_MCPRealEngine(ModelsMCPCase):
    """DESIGN §1/§4 through the MCP server: the Codex-side default Claude seat reaches `claude -p` as the family head with an
    explicit effort and the no-switch settings."""

    def test_the_default_claude_seat_launches_its_head_with_effort_and_the_no_switch_settings(self):
        server = self.start(self.real_engine_env())
        result = self.ask(server, {"prompt": "q"})
        self.assertIn("OK", result["content"][0]["text"])
        launches = [r for r in self.stub_records("stub-claude.jsonl") if r.get("mode") == "prompt"]
        self.assertEqual([(r["model"], r["effort"]) for r in launches], [(model_head("fable"), "max")])
        self.assertEqual(launches[0]["settings"], {"switchModelsOnFlag": False, "fallbackModel": []})
        self.assertIn("--restricted", launches[0]["argv"])


class GroupB_MCPNotices(ModelsMCPCase):
    """DESIGN §4: notices lead the server's successful results (their body is the engine's stdout), including a detached
    claude_review collected later with claude_result."""

    def test_claude_ask_and_a_detached_review_collected_by_claude_result_lead_with_the_notice(self):
        astra = model_head("astra")
        self.git_workspace()
        server = self.start(self.real_engine_env())
        # changed for docs/model-routing.md 5 (F3): a seat descends only through its `|` preference list, so the seat that falls back
        # to Sol after Astra's rejection is `astra|sol`; a plain `astra` ends on the rejection
        text = self.ask(server, {"seats": "astra|sol:low", "prompt": "[[STUB:badmodel=%s]] q" % astra})["content"][0]["text"]
        self.assertTrue(text.startswith("NOTICE: "), text)
        self.assertIn("model %s is not supported" % astra, text.splitlines()[0])
        server.send({"jsonrpc": "2.0", "id": "review", "method": "tools/call", "params": {"name": "claude_review", "arguments": {
            "wait": False, "seats": "astra|sol:low", "focus": "[[STUB:badmodel=%s]]" % astra}}})
        started = server.read(timeout=60)["result"]
        self.assertEqual(started["structuredContent"]["status"], "started", started)
        job = started["structuredContent"]["job"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self.assert_result(server.call("claude_status", {"job": job}, request_id="status"))
            if "state=done" in status["content"][0]["text"]:
                break
            time.sleep(0.5)
        result = self.assert_result(server.call("claude_result", {"job": job}, request_id="result"))
        self.assertTrue(result["content"][0]["text"].startswith("NOTICE: "), result["content"][0]["text"][:500])


class GroupE_MCPNotices(ModelsMCPCase):
    """DESIGN §5 gate under `warn` and the no-configuration-value rule, through the MCP server."""

    def test_claude_ask_under_warn_starts_with_the_models_notice(self):
        newer_sol = "gpt-7-sol"  # model-lint: allow fictional successor id
        server = self.start(self.real_engine_env(STUB_CODEX_CATALOG=str(self.catalog_with("gpt7.json", newer_sol))))
        result = self.ask(server, {"prompt": "q"})  # status ok, exit 0: the gate only warns
        text = result["content"][0]["text"]
        self.assertTrue(text.startswith("NOTICE: models: check FAILED ("), text[:500])
        self.assertIn(newer_sol, text.splitlines()[0])

    def test_no_configuration_value_appears_in_an_mcp_result(self):
        config = EVIDENCE / "codex_config_read_unused_provider_sentinel.json"
        server = self.start(self.real_engine_env(STUB_CODEX_CONFIG=str(config)))
        result = self.ask(server, {"prompt": "q"})
        texts = [result["content"][0]["text"]]
        job = result["structuredContent"]["job"]
        texts.append(self.assert_result(server.call("claude_status", {"job": job}, request_id="status"))["content"][0]["text"])
        texts.append(self.assert_result(server.call("claude_result", {"job": job}, request_id="result"))["content"][0]["text"])
        self.assertTrue(texts[0].startswith("NOTICE: models: unverified ("), texts[0][:500])
        for text in texts:
            self.assertNotIn(SENTINEL_TOKEN, text)
            self.assertNotIn("proxy.invalid", text)


class GroupB_MCPFinalReview(ModelsMCPCase):
    """The final review (review/rulings_final_review.md, binding; review/final_B_findings.md) through the MCP server and the
    REAL engine; the engine's own views: skills/codex/tests/test_bridge.py GroupB_FinalReviewLaunchAndOutputs."""

    def test_claude_status_shows_the_jobs_persisted_notices(self):
        # B-astra-3 [P2]: claude_status returns the engine's `status <job>`, which never read the persisted notices, so a job whose
        # seat fell back (or that was admitted under a failed check) looked ordinary there. `status` shows each of the job's
        # persisted notices in its detail view, and claude_status carries them
        astra = model_head("astra")
        server = self.start(self.real_engine_env())
        # `astra|sol`: the seat may descend to Sol (docs/model-routing.md 5, F3); a plain `astra` would end on the rejection
        result = self.ask(server, {"seats": "astra|sol:low", "prompt": "[[STUB:badmodel=%s]] q" % astra})
        job = result["structuredContent"]["job"]
        meta = json.loads((server.jobs / job / "meta.json").read_text())
        texts = [n["text"] for n in meta.get("notices") or [] if isinstance(n, dict) and n.get("text")]
        self.assertTrue([t for t in texts if "%s -> %s" % (astra, model_head("sol")) in t], texts)  # the seat fell back
        status = self.assert_result(server.call("claude_status", {"job": job}, request_id="status"))["content"][0]["text"]
        self.assertEqual([t for t in texts if t not in status], [], "claude_status leaves out the job's persisted notices:\n" + status)


class GroupB_MCPReReview(ModelsMCPCase):
    """The re-review of the final-review fix round (review/rulings_rr.md, binding; review/final_rr_findings.md) through the
    Codex-side entry points and the REAL engine: RR-8. The engine's own items: skills/codex/tests/test_bridge.py
    GroupB_ReReviewLaunchAndStatus"""

    @staticmethod
    def refusal(text):
        """The engine's refusal line (`bridge: refused seat ...`) in an output, or ''."""
        return next((line.strip() for line in text.splitlines() if line.strip().startswith("bridge: refused seat")), "")

    def entry_point_env(self, **extra):
        """The environment a Codex user's shell gives the Codex-side entry point (codex-skills/claude/scripts/bridge.py): no
        seat default of the user's (the entry point supplies its built-ins), the engine under review pinned, and
        real_engine_env()'s hermetic stubs; `extra` sets a variable (None unsets it)."""
        env = {key: value for key, value in os.environ.items()
               if key not in DEFAULTS and key not in NEVER_INHERITED and key not in ("BRIDGE_ROOT", "BRIDGE_JOBS")}
        env.update(BRIDGE_JOBS=str(self.root / "jobs"), TMPDIR=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        for key, value in dict(self.real_engine_env(CLAUDE_BRIDGE_ENGINE=str(REAL_ENGINE)), **extra).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def jobs_made(self):
        jobs = self.root / "jobs"
        return sorted(p.name for p in jobs.iterdir()) if jobs.is_dir() else []

    def table_without_max(self):
        """The shipped table with `max` gone from the Fable and Opus heads' efforts (a reviewed table update may do that), so
        that the MCP server's built-in seats (fable:max, opus:max), which it cannot pass an --effort to, are refused."""
        table = json.loads(MODELS_JSON.read_text())
        for family in ("fable", "opus"):
            efforts = table["models"][model_head(family)]["efforts"]
            efforts.remove("max")
        path = self.root / "models-without-max.json"
        path.write_text(json.dumps(table, indent=1))
        return path

    def test_the_codex_side_entry_point_names_a_default_variable_only_when_the_user_set_it(self):
        # RR-8 [P3] (opus-3; B-opus-3 still open on the Codex side): parse_seats names the default list's variable when it is in
        # the environment, and codex-skills/claude/scripts/bridge.py puts all three there (os.environ.setdefault) before the
        # engine runs, so `bridge.py review --effort ultra` on a machine with no overrides was refused as "refused seat 'opus'
        # (BRIDGE_REVIEW_SEATS) (--effort): 'ultra' is not a Claude effort (say max)", blaming a variable the user never set.
        # The ruling: the entry point passes its built-in lists without making them look user-set; the variable is named
        # only when the user set it (also to the built-in's own value). The refusal itself stays, and no job is made
        cases = (("BRIDGE_REVIEW_SEATS", ["review", "--effort", "ultra"], "sonnet:max"),
                 ("BRIDGE_COUNCIL_SEATS", ["council", "--effort", "ultra", "--", "q"], "opus:max,sonnet:max"),
                 ("BRIDGE_DEFAULT_SEAT", ["ask", "--effort", "ultra", "--", "q"], "sonnet:max"))
        for var, argv, other in cases:
            for user_value in (None, DEFAULTS[var], other):
                with self.subTest(variable=var, user_set=user_value):
                    before = self.jobs_made()
                    completed = subprocess.run([sys.executable, str(ENTRYPOINT), *argv], cwd=str(self.cwd),
                                               env=self.entry_point_env(**{var: user_value}), stdin=subprocess.DEVNULL,
                                               capture_output=True, text=True, encoding="utf-8", timeout=120)
                    line = self.refusal(completed.stderr)
                    self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                    self.assertNotIn("Traceback", completed.stderr)
                    self.assertIn("'ultra'", line, completed.stderr)
                    self.assertEqual(self.jobs_made(), before)
                    if user_value is None:
                        self.assertNotRegex(line, r"BRIDGE_[A-Z_]+", "the refusal names a variable the user never set: " + line)
                    else:
                        self.assertIn("(%s)" % var, line)

    def test_the_mcp_servers_default_seat_refusal_names_a_variable_only_when_the_user_set_it(self):
        # RR-8 [P3] (opus-3): claude_mcp_server.py sets the same three variables in the engine's environment. Its tools take no
        # effort, so the built-in seats are refused here by a table whose Fable and Opus heads have no `max`: claude_review
        # (opus:max) and claude_ask (fable:max) with the server's built-in lists name no BRIDGE_* variable; with the user's own
        # BRIDGE_REVIEW_SEATS or BRIDGE_DEFAULT_SEAT (the built-in's value) in the server's environment, the refusal names it
        table = self.table_without_max()
        calls = (("claude_review", {"intent": "q"}, "BRIDGE_REVIEW_SEATS", "claude-opus"),
                 ("claude_ask", {"prompt": "q"}, "BRIDGE_DEFAULT_SEAT", "claude-fable"))
        for tool, arguments, var, model in calls:
            for user_set in (False, True):
                with self.subTest(tool=tool, user_set=user_set):
                    overrides = self.real_engine_env(BRIDGE_MODELS_FILE=str(table))
                    if user_set:
                        overrides[var] = DEFAULTS[var]
                    server = self.start(overrides)
                    before = sorted(p.name for p in server.jobs.iterdir()) if server.jobs.is_dir() else []
                    server.send({"jsonrpc": "2.0", "id": tool, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
                    result = self.assert_result(server.read(timeout=120), status="failed", exit_code=2)
                    text = result["content"][0]["text"]
                    line = self.refusal(text)
                    self.assertIn(model, line, text)  # the built-in seat is refused: its head has no max
                    self.assertIn("'max'", line)
                    self.assertEqual(sorted(p.name for p in server.jobs.iterdir()) if server.jobs.is_dir() else [], before)
                    if user_set:
                        self.assertIn("(%s)" % var, line)
                    else:
                        self.assertNotRegex(line, r"BRIDGE_[A-Z_]+", "the refusal names a variable the user never set: " + line)


class GroupB_MCPSecondReReview(ModelsMCPCase):
    """S-DEF of the second re-review (review/rulings_rr2.md, binding; review/rr2_findings.md: RR2-astra-2, RR2-astra-3 and
    Opus's note on inherited variables) through the Codex-side entry points and the REAL engine: the built-in seat lists travel
    as BRIDGE_BUILTIN_*, the models check validates them (labelled "built-in default"), no seat CLI inherits them, and a user's
    own setting keeps its name. The engine's own view: skills/codex/tests/test_bridge.py GroupB_SecondReReviewDefaults"""

    refusal = staticmethod(GroupB_MCPReReview.refusal)
    entry_point_env = GroupB_MCPReReview.entry_point_env
    table_without_max = GroupB_MCPReReview.table_without_max
    USER_VARS = re.compile(r"\bBRIDGE_(?:DEFAULT_SEAT|REVIEW_SEATS|COUNCIL_SEATS)\b")

    def run_script(self, script, env, *argv):
        """A bridge script (the Codex-side entry point, or the engine itself) run as a user runs it, from the workspace."""
        return subprocess.run([sys.executable, str(script), *argv], cwd=str(self.cwd), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def doctor_json(self, script, env):
        completed = self.run_script(script, env, "doctor", "--json")
        try:
            return completed, json.loads(completed.stdout)
        except ValueError:
            self.fail("doctor --json printed no JSON (exit %s):\n%s%s" % (completed.returncode, completed.stdout, completed.stderr))

    @staticmethod
    def defaults_findings(data):
        return [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict) and f.get("class") == "defaults"]

    def argv_record(self, path):
        """The one launch a stub CLI recorded with [[STUB:argv=<path>]]."""
        records = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()] if Path(path).exists() else []
        self.assertEqual(len(records), 1, records)
        return records[0]

    def test_doctor_and_the_composed_verdict_check_the_entry_points_built_in_lists(self):
        # S-DEF (RR2-astra-2 [P2]): the entry points' lists reached the engine through the user-facing variables and RR-8 filtered
        # them out of local_facts, so with a table whose Fable and Opus heads lack `max`, doctor and the composed verdict had no
        # `defaults` finding (three before RR-8) while ask, review and council refused those very lists. The ruling: the check
        # validates every effective default list, the entry point's built-ins included, labelled "built-in default" and never
        # attributed to a variable the user did not set. Doctor and a job through the entry point, then a claude_ask
        table = self.table_without_max()
        completed, data = self.doctor_json(ENTRYPOINT, self.entry_point_env(BRIDGE_MODELS_FILE=str(table)))
        self.assertEqual((completed.returncode, data.get("verdict")), (1, "FAIL"), data.get("findings"))
        texts = self.defaults_findings(data)
        self.assertGreaterEqual(len(texts), 3, "not every built-in list (ask, review, council) is checked: %r" % texts)
        for seat in ("fable:max", "opus:max"):
            self.assertTrue([t for t in texts if seat in t], "no defaults finding refuses %s: %r" % (seat, texts))
        for text in texts:
            self.assertIn("built-in default", text)
            self.assertNotRegex(text, self.USER_VARS, "a built-in list is attributed to a variable the user never set")
        completed = self.run_script(ENTRYPOINT, self.entry_point_env(BRIDGE_MODELS_FILE=str(table)), "ask", "--seats", "sonnet:low", "--", "q")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        first = (completed.stdout.splitlines() or [""])[0]
        self.assertTrue(first.startswith("NOTICE: models: check FAILED (") and "built-in default" in first, completed.stdout)
        text = self.ask(self.start(self.real_engine_env(BRIDGE_MODELS_FILE=str(table))), {"prompt": "q", "seats": "sonnet:low"})["content"][0]["text"]
        first = (text.splitlines() or [""])[0]
        self.assertTrue(first.startswith("NOTICE: models: check FAILED (") and "built-in default" in first, text[:800])

    def test_no_seat_launched_through_an_entry_point_inherits_its_lists(self):
        # S-DEF (RR2-astra-3 [P3], Opus's note on inherited variables): the entry points put their lists in BRIDGE_DEFAULT_SEAT,
        # BRIDGE_REVIEW_SEATS and BRIDGE_COUNCIL_SEATS with BRIDGE_DEFAULTS_SUPPLIED naming them, and every seat CLI inherited all
        # four. The ruling: the lists travel only as BRIDGE_BUILTIN_*, the entry points never set the user-facing variables,
        # BRIDGE_DEFAULTS_SUPPLIED is gone, and no model child keeps a BRIDGE_BUILTIN_* variable. The built-in default seat still
        # reaches `claude -p` (Fable at max); the stub records the BRIDGE_* variables it sees (`bridge_env`)
        for via in ("entry-point", "mcp-server"):
            with self.subTest(via=via):
                path = self.root / ("argv-%s.jsonl" % via)
                prompt = "[[STUB:argv=%s]] q" % path
                if via == "entry-point":
                    completed = self.run_script(ENTRYPOINT, self.entry_point_env(), "ask", "--", prompt)
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                else:
                    self.ask(self.start(self.real_engine_env()), {"prompt": prompt})
                record = self.argv_record(path)
                self.assertEqual((record["model"], record["effort"]), (model_head("fable"), "max"))  # the built-in default seat ran
                inherited = sorted(key for key in record["bridge_env"] if key in (*USER_SEAT_VARS, *NEVER_INHERITED))
                self.assertEqual(inherited, [], "the seat CLI inherited the entry point's lists or their provenance")

    def test_a_users_own_setting_under_a_seat_keeps_its_name(self):
        # S-DEF (RR2-astra-3 [P3]): a process under a seat that the Codex-side entry point launched inherited
        # BRIDGE_DEFAULTS_SUPPLIED, so when it set its own BRIDGE_REVIEW_SEATS=spark and ran the Claude-side engine, the refusal
        # left the variable out and the check dropped the setting. The ruling: a user-facing variable that is set is the user's,
        # named in the refusal and checked under its name, whatever BRIDGE_BUILTIN_* list is inherited. The nested engine gets
        # the BRIDGE_* environment the seat's CLI recorded, BRIDGE_BUILTIN_REVIEW_SEATS inherited, and BRIDGE_REVIEW_SEATS=spark
        path = self.root / "argv-seat.jsonl"
        completed = self.run_script(ENTRYPOINT, self.entry_point_env(), "ask", "--seats", "sonnet:low", "--", "[[STUB:argv=%s]] q" % path)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        nested = self.entry_point_env()
        nested.update(self.argv_record(path)["bridge_env"])
        nested.update(BRIDGE_BUILTIN_REVIEW_SEATS=BUILTINS["BRIDGE_BUILTIN_REVIEW_SEATS"], BRIDGE_REVIEW_SEATS="spark")
        completed = self.run_script(REAL_ENGINE, nested, "review")
        line = self.refusal(completed.stderr)
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("'spark'", line, completed.stderr)
        self.assertIn("(BRIDGE_REVIEW_SEATS)", line, "the refusal of the user's own BRIDGE_REVIEW_SEATS does not name it")
        completed, data = self.doctor_json(REAL_ENGINE, nested)
        texts = self.defaults_findings(data)
        self.assertTrue([t for t in texts if "BRIDGE_REVIEW_SEATS" in t and "spark" in t],
                        "the check does not report the user's BRIDGE_REVIEW_SEATS under its name: %r" % texts)


class GroupA_MCPK1OnReview(ModelsMCPCase):
    """The review of ruling K1-ON (review/ruling_k1_on.md, binding) on the Codex side: K1-ON-2, the seat docs name the K1 seat
    the shipped table switches on. The engine's own view of the seat: skills/codex/tests/test_bridge.py GroupA_TableAndResolution"""

    def test_the_codex_side_seat_docs_name_the_k1_seat_while_it_is_on(self):
        # K1-ON-2 [P3] (Opus): $claude reaches the same engine and table as /codex, and the seats pattern accepts the K1 seat, but
        # SEATS_DESC (the seats description claude_ask and claude_review publish) and the seat paragraph of
        # codex-skills/claude/SKILL.md list only the Claude families, so a Codex user asking for the pinned Opus seat for legal work cannot learn
        # the seat's name. The ruling: one clause in each names the seat. The seat comes from the shipped table and must stand as
        # a name of its own, not inside a model ID
        k1 = json.loads(MODELS_JSON.read_text())["k1_seat"]
        self.assertTrue(k1["enabled"], "ruling K1-ON: the shipped table switches the K1 seat on")
        named = re.compile(r"(?<![\w.-])" + re.escape(k1["seat"]) + r"(?![\w.-])")
        tools = {tool["name"]: tool for tool in self.start().request("tools/list")["result"]["tools"]}
        texts = {}
        for name in ("claude_ask", "claude_review"):
            seats = tools[name]["inputSchema"]["properties"]["seats"]
            self.assertRegex(k1["seat"], seats["pattern"])  # the seat can be asked for here
            texts["SEATS_DESC (%s seats)" % name] = seats["description"]
        paragraphs = [p for p in (CLAUDE / "SKILL.md").read_text(encoding="utf-8").split("\n\n") if "model[:effort]" in p]
        self.assertEqual(len(paragraphs), 1, "SKILL.md has no single seat paragraph (the one defining model[:effort])")
        texts["the SKILL.md seat paragraph"] = paragraphs[0]
        self.assertEqual([where for where, text in texts.items() if not named.search(text)], [],
                         "the Codex-side seat docs do not name the K1 seat %r" % k1["seat"])


if __name__ == "__main__":
    unittest.main()

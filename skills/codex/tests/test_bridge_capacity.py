"""Throttle class, capacity gate, priority lease, pacer and launch classes (the Oct. 12 fixes; build spec section 5).

Same harness as test_bridge.py (stub codex/claude/relay, isolated HOME, CODEX_HOME and BRIDGE_JOBS). EngineBase keeps the
default environment of the 420 baseline tests: a private capacity dir and coordination dir, BRIDGE_NET_PROBE=off,
BRIDGE_MIN_GAP=0, no lease and no PAUSE file, so nothing here (or there) touches the network, ~/.codex or a real lease."""
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `discover -s tests` adds it; `-m unittest tests.test_bridge_capacity` does not
from test_bridge import ENGINE, EngineBase, head, load_engine_module  # noqa: E402

# The exact Oct. 12 texts (understand/agent1.txt facts 0-13), de-identified: every one is pinned to its class below.
OCT12_429 = "exceeded retry limit, last status: 429 Too Many Requests"
OCT12_REFUSED = "error sending request for url (https://chatgpt.com/backend-api/codex/responses): client error (Connect): tcp connect error: Connection refused (os error 61)"
OCT12_403WS = "Reconnecting... 2/5 (unexpected status 403 Forbidden for wss://chatgpt.com/backend-api/codex/ws)"
OCT12_DEMAND = "We're currently experiencing high demand, which may cause temporary errors."
OCT12_CAPACITY = "Selected model is at capacity. Please try again later or choose a different model."
OCT12_503 = "exceeded retry limit, last status: 503 Service Unavailable"
OCT12_STREAM = "stream disconnected before completion: error decoding response body"

THROTTLED = ("codex-throttled", [OCT12_429, OCT12_503, "429 Too Many Requests", "503 Service Unavailable", OCT12_DEMAND, OCT12_CAPACITY,
                                 "Flex capacity unavailable right now", "server_overloaded", "We’re currently experiencing high demand"])
NETWORK = ("codex-network", [OCT12_REFUSED, "Connection reset by peer (os error 54)", "Network is unreachable", "No route to host",
                             "dns error: failed to lookup address information", OCT12_STREAM,
                             "error sending request for url (https://chatgpt.com/backend-api/codex/responses)", "Connection failed",
                             "Waiting for network..."])
NEITHER = ["You've hit your usage limit. Try again later.", "HTTP 429 rate limit exceeded; retrying", "status 429", "a 503 came back",
           "permission denied: sandbox blocked the command", "Quota exceeded for this plan", OCT12_403WS,
           "ERR_BLOCKED_BY_CLIENT", "usage limit reached"]


def compressed(**extra):
    """Throttle and pacer waits compressed for a test: the 300/900/1800 s batch ladder and 60/300 s interactive ladder at 1/100."""
    env = {"BRIDGE_THROTTLE_WAIT_SCALE": "0.01"}
    env.update(extra)
    return env


class ThrottleClassUnit(unittest.TestCase):
    """The signatures of spec 3.2, pinned string by string, through the engine's classifier (loaded in-process)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-cap-unit-"))
        cls.b = load_engine_module({"BRIDGE_JOBS": str(cls.tmp / "jobs"), "HOME": str(cls.tmp), "BRIDGE_CAPACITY_DIR": str(cls.tmp / "cap"),
                                    "LR_CU_COORD_DIR": str(cls.tmp / "coord"), "BRIDGE_NET_PROBE": "off"})

    def test_every_oct12_text_lands_in_its_class(self):
        for code, texts in (THROTTLED, NETWORK):
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(self.b.throttle_class(text), code)
                    self.assertEqual(self.b.throttle_class(text.upper()), code)  # case-insensitive
        for text in NEITHER:
            with self.subTest(text=text):
                self.assertIsNone(self.b.throttle_class(text))

    def test_the_websocket_403_row_is_turn_tail_only(self):
        self.assertIsNone(self.b.throttle_class(OCT12_403WS))                      # turn.failed: not a network failure
        self.assertEqual(self.b.throttle_class(OCT12_403WS, tail=True), "codex-network")  # the final error of a killed run
        self.assertIsNone(self.b.throttle_class("unexpected status 403 Forbidden for https://example.test", tail=True))  # host-qualified

    def test_a_bare_429_or_503_is_never_a_throttle(self):
        for text in ("429", "503", "last status: 429", "error 429", "HTTP 503"):
            self.assertIsNone(self.b.throttle_class(text), text)

    def test_the_throttle_class_wins_over_the_usage_limit_regex(self):
        self.assertTrue(self.b.LIMIT_RE.search(OCT12_429))  # the old reading, kept for stderr-only evidence
        self.assertEqual(self.b.throttle_class(OCT12_429), "codex-throttled")
        self.assertFalse(self.b.limit_text_seen(OCT12_429))   # the throttle class is evaluated first: LIMIT_RE is not consulted
        self.assertTrue(self.b.limit_text_seen("You've hit your usage limit."))

    def test_the_usage_limit_ladder_is_jittered_and_bounded(self):
        shorts = {self.b._ladder_wait(1) for _ in range(50)}
        longs = {self.b._ladder_wait(3) for _ in range(50)}
        self.assertGreater(len(shorts), 5)
        self.assertTrue(all(self.b.LIMIT_WAIT_SHORT * 0.6 - 1e-9 <= w <= self.b.LIMIT_WAIT_SHORT * 1.4 + 1e-9 for w in shorts), shorts)
        self.assertTrue(all(self.b.LIMIT_WAIT_LONG * 0.6 - 1e-9 <= w <= self.b.LIMIT_WAIT_LONG * 1.4 + 1e-9 for w in longs), longs)

    def test_the_throttle_continuation_prompt_names_no_usage_limit(self):
        self.assertNotIn("usage limit", self.b.THROTTLE_CONTINUE_PROMPT.lower())
        self.assertIn("temporary", self.b.THROTTLE_CONTINUE_PROMPT.lower())

    def test_the_throttle_ladders_per_class(self):
        batch = [self.b._throttle_wait(n, "batch") for n in (1, 2, 3, 4, 9)]
        for w, base in zip(batch, (300, 900, 1800, 1800, 1800)):
            self.assertTrue(base * 0.8 - 1e-9 <= w <= base * 1.2 + 1e-9, (w, base))
        inter = [self.b._throttle_wait(n, "interactive") for n in (1, 2)]
        for w, base in zip(inter, (60, 300)):
            self.assertTrue(base * 0.6 - 1e-9 <= w <= base * 1.4 + 1e-9, (w, base))
        self.assertIsNone(self.b._throttle_wait(3, "interactive"))  # the interactive seat fails after two waits


LEASE_HOLDER = Path(__file__).resolve().parent / "fixtures" / "lease_holder.py"


def write_rollout(home, name, boundary="task_started", age_s=0, subagent=False, cwd="/work/a", day="2026/10/01", big_tail=False, partial=False,
                  no_boundary=False, big_turn=False, resumed=False, no_newline=False, exact_tail=False):
    """A synthetic rollout file under <home>/sessions/<day>/ in the real CLI's shape (4.2): session_meta first (no account or user ids),
    event_msg turn boundaries, trailing token_usage_record / item_completed lines as finished sessions really end. `boundary` is the
    last boundary event; `age_s` its mtime age; `big_tail` appends one > 64 KiB non-boundary line after it (the 1 MiB growth);
    `partial` ends the file in a cut line (being written); `no_boundary` writes no boundary at all; `big_turn` follows the
    task_started with ~2 MB of ordinary lines (a long turn: no boundary within the grown tail); `resumed` writes a finished first
    turn and then a second task_started (a resumed thread); `no_newline` appends 1.4 MB without any newline (nothing in the tail
    parses: degraded); `exact_tail` pads the lines after the boundary so the boundary line begins exactly 64 KiB before EOF."""
    d = Path(home) / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-2026-10-01T10-00-00-{name}.jsonl"
    source = {"subagent": {"thread_spawn": {"parent_thread_id": "00000000-0000-4000-8000-000000000001", "depth": 1}}} if subagent else "exec"
    lines = [{"timestamp": "2026-10-01T14:00:00.000Z", "type": "session_meta",
              "payload": {"id": name, "cwd": cwd, "originator": "codex_exec", "source": source, "thread_source": "subagent" if subagent else "user",
                          "base_instructions": {"text": "x" * 20000}}},
             {"timestamp": "2026-10-01T14:00:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "user"}}]
    if resumed:
        lines.append({"timestamp": "2026-10-01T14:00:01.000Z", "type": "event_msg", "payload": {"type": "task_started"}})
        lines.append({"timestamp": "2026-10-01T14:00:02.000Z", "type": "event_msg", "payload": {"type": "task_complete"}})
        lines.append({"timestamp": "2026-10-01T14:00:03.000Z", "type": "token_usage_record", "payload": {"total": 1}})
    if not no_boundary:
        lines.append({"timestamp": "2026-10-01T14:00:01.000Z", "type": "event_msg", "payload": {"type": "task_started"}})
        if boundary != "task_started":
            lines.append({"timestamp": "2026-10-01T14:00:02.000Z", "type": "event_msg", "payload": {"type": boundary}})
    text = "".join(json.dumps(l) + "\n" for l in lines)
    trailer = [{"timestamp": "2026-10-01T14:00:03.000Z", "type": "token_usage_record", "payload": {"total": 1}},
               {"timestamp": "2026-10-01T14:00:03.000Z", "type": "event_msg", "payload": {"type": "token_count"}}]
    if exact_tail:  # the boundary line plus the trailer fill exactly 64 KiB
        boundary_line = json.dumps(lines[-1]) + "\n"
        base = json.dumps({"type": "token_usage_record", "payload": {"total": 1, "pad": ""}}) + "\n"
        trailer = [{"type": "token_usage_record", "payload": {"total": 1, "pad": "p" * (64 * 1024 - len(boundary_line) - len(base))}}]
    text += "".join(json.dumps(l) + "\n" for l in trailer)
    if big_turn:
        row = json.dumps({"timestamp": "2026-10-01T14:00:05.000Z", "type": "response_item", "payload": {"type": "function_call_output", "output": "z" * 4000}}) + "\n"
        text += row * (2 * 1024 * 1024 // len(row) + 1)
    if big_tail:
        text += json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "y" * 70000}}) + "\n"
    if partial:
        text += '{"timestamp": "2026-10-01T14:00:04.000Z", "type": "event_msg", "payload": {"type": "task_com'
    if no_newline:
        text += "w" * (1400 * 1024)
    path.write_text(text)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def finish_rollout(path):
    """Append a task_complete boundary (and the trailing lines a finished session ends with) and touch the file."""
    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        fh.write(json.dumps({"type": "token_usage_record", "payload": {"total": 2}}) + "\n")


class CapacityMixin:
    """Per-test capacity and coordination dirs (a ramp one test triggers must not pace the next) and the lease helper."""

    def cap_env(self, name, **extra):
        cap = self.tmp / f"cap-{name}"
        coord = self.tmp / f"coord-{name}"
        cap.mkdir(exist_ok=True)
        coord.mkdir(exist_ok=True)
        env = {"BRIDGE_CAPACITY_DIR": str(cap), "LR_CU_COORD_DIR": str(coord)}
        env.update(extra)
        return env

    def fresh_home(self, name):
        home = self.tmp / f"codex-home-{name}"
        home.mkdir(exist_ok=True)
        return home

    def hold_lease(self, env, task_dir="/runs/2026-10-13_cu-task", unlocked=False):
        """A helper process holding the lease (LOCK_EX on its holder file) under the test's coordination dir; returns (process, path)."""
        lease_dir = Path(env["LR_CU_COORD_DIR"]) / "codex-cu-priority.d"
        args = [sys.executable, str(LEASE_HOLDER), str(lease_dir), task_dir] + (["--unlocked"] if unlocked else [])
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if unlocked:
            out, _ = p.communicate(timeout=30)
            return None, out.strip()
        path = p.stdout.readline().strip()
        self.addCleanup(self.stop_helper, p)
        return p, path

    @staticmethod
    def stop_helper(p):
        if p.poll() is None:
            p.kill()
            p.communicate(timeout=30)
        else:
            p.communicate(timeout=30)

    def events(self, env):
        p = Path(env["BRIDGE_CAPACITY_DIR"]) / "events.jsonl"
        return self.read_records(p) if p.exists() else []

    def pacer(self, env, home=None):
        key = hashlib.sha256(os.path.realpath(home or self.env["CODEX_HOME"]).encode()).hexdigest()[:12]
        return json.loads((Path(env["BRIDGE_CAPACITY_DIR"]) / f"{key}.pacer.json").read_text())

    def account_lock(self, env, home):
        key = hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:12]
        return Path(env["BRIDGE_CAPACITY_DIR"]) / f"{key}.lock"

    def ledger(self, env, home):
        key = hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:12]
        p = Path(env["BRIDGE_CAPACITY_DIR"]) / f"{key}.json"
        return json.loads(p.read_text()) if p.exists() else {"entries": []}

    def still_held(self, d, tag):
        """The seat is still held by the gate: parked (state waiting) at the next sample, allowing the instant between two hold polls
        when the record reads running/"retrying after the wait", and nothing launched. Returns the seat record."""
        seat = self.wait_for(d, tag, lambda s: s.get("state") == "waiting", tries=30)
        self.assertIsNone(seat.get("first_launch_at"), seat)
        self.assertEqual(seat.get("models_launched"), [], seat)
        return seat

    def ledger_entries(self, env):
        """Every ledger entry of every account under the test's capacity dir (a routed seat's ledger is keyed by relay's account home)."""
        return [e for p in Path(env["BRIDGE_CAPACITY_DIR"]).glob("*.json") if re.fullmatch(r"[0-9a-f]{12}\.json", p.name)
                for e in json.loads(p.read_text()).get("entries", [])]


class ThrottleEngine(CapacityMixin, EngineBase):
    """A throttled or network attempt waits (its class's ladder), never hits relay, never fails over, never enters the usage-limit
    ladder; the pacer records the trigger."""

    def test_an_interactive_seat_waits_twice_on_a_429_then_fails_without_the_ladder(self):
        env = self.cap_env("thr-inter", **compressed(STUB_CODEX_THROTTLE="429"))  # every call, including the resumed ones (no directive there)
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "thr-inter", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.jobs / "thr-inter"
        seat = self.seat(d, "astra")
        self.assertEqual(seat["state"], "failed")
        self.assertEqual(seat.get("limit_hits", 0), 0)              # not a usage limit
        self.assertEqual(seat["throttle_hits"], 3)                   # three throttled attempts, two waits between them
        self.assertEqual(seat["attempt"], 3)
        self.assertIn("Codex throttled twice", seat["activity"])
        self.assertIn("not retried further", seat["activity"])
        self.assertIn("429", seat["activity"])                       # the evidence
        self.assertTrue((d / "events-astra.attempt1.jsonl").exists())
        self.assertTrue((d / "events-astra.attempt2.jsonl").exists())
        self.assertIn("Codex throttled twice", out)

    def test_a_batch_seat_keeps_waiting_and_resumes_its_thread(self):
        counter = self.tmp / "thr-batch-counter"
        rec = self.tmp / "thr-batch-argv.jsonl"
        env = self.cap_env("thr-batch", **compressed(BRIDGE_CLASS="batch", STUB_CODEX_RECORD=str(self.tmp / "thr-batch-record.jsonl")))
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "thr-batch", "--",
                                       f"[[STUB:throttle=429,throttleonce={counter},argv={rec}]]", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "thr-batch"
        seat = self.seat(d, "astra")
        self.assertEqual((seat["state"], seat["attempt"], seat["throttle_hits"], seat.get("limit_hits", 0)), ("done", 2, 1, 0))
        self.assertEqual(seat["class"], "batch")
        self.assertEqual(self.meta(d)["class"], "batch")
        self.assertEqual(len(self.read_records(rec)), 1)  # the resumed turn carries the continuation prompt, not the directive
        execs = [r for r in self.read_records(env["STUB_CODEX_RECORD"]) if r.get("mode") == "exec"]
        self.assertEqual([r["resume"] for r in execs], [None, "stub-thread-01a0"])   # work had begun: the thread is resumed, not restarted
        self.assertEqual(seat["thread_id"], "stub-thread-stub-thread-01a0")
        self.assertTrue((d / "events-astra.attempt1.jsonl").exists())
        # the pacer recorded the trigger for this account
        pacer = self.pacer(env)
        self.assertEqual(pacer["reason"], "codex-throttled")
        self.assertGreater(pacer["ramp_started_at"], time.time() - 120)
        self.assertIn("trigger", [e["event"] for e in self.events(env)])

    def test_a_batch_throttle_wait_is_unbounded_but_cancellable(self):
        env = self.cap_env("thr-cancel", BRIDGE_CLASS="batch")   # the real ladder: the first step is 300 s
        p = self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", "thr-cancel", "--", "[[STUB:throttle=503]]")
        d = self.jobs / "thr-cancel"
        seat = self.wait_for(d, "astra", lambda s: s.get("state") == "waiting" and s.get("waiting_kind") == "throttle")
        self.assertGreater(seat["waiting_until"], time.time() + 200)   # the 300 s first step (x 0.8..1.2)
        self.assertLess(seat["waiting_until"], time.time() + 370)
        self.assertIn("throttled", seat["activity"])
        self.assertEqual(seat["waiting_kind"], "throttle")
        rc, out, err = self.run_engine("reply", "thr-cancel", "--", "more")
        self.assertEqual(rc, 1)
        self.assertIn("Codex throttled", err)                           # the new waiting cause has its own wording
        self.assertNotIn("waiting out a usage limit", err)
        t0 = time.time()
        rc, out, err = self.run_engine("cancel", "thr-cancel")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=30)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(self.seat(d, "astra")["state"], "cancelled")

    def test_a_network_failure_waits_like_a_throttle(self):
        counter = self.tmp / "thr-net-counter"
        env = self.cap_env("thr-net", **compressed())
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "thr-net", "--",
                                       f"[[STUB:throttle=refused,throttleonce={counter}]]", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "thr-net", "astra")
        self.assertEqual((seat["attempt"], seat["throttle_hits"], seat.get("limit_hits", 0)), (2, 1, 0))
        self.assertEqual(seat["throttle_class"], "codex-network")

    def test_a_routed_throttle_never_calls_relay_hit_or_fails_over(self):
        counter = self.tmp / "thr-routed-counter"
        env, rd = self.relay("thr-routed", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "a"}]}})
        env.update(self.cap_env("thr-routed", **compressed()))
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "thr-routed", "--",
                                       f"[[STUB:throttle=429,throttleonce={counter}]]", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        calls = [c["argv"][:1] for c in self.records(rd, "calls.jsonl")]
        self.assertNotIn(["hit"], calls)
        seat = self.seat(self.jobs / "thr-routed", "astra")
        self.assertEqual((seat["attempt"], seat["throttle_hits"], seat.get("limit_hits", 0), seat["account"]), (2, 1, 0, "a"))

    def test_a_stderr_only_throttle_text_is_not_a_usage_limit(self):
        # LIMIT_RE matched "too many requests" in the Oct. 12 line; the throttle signatures are evaluated first, and stderr never
        # classifies a throttle, so the attempt fails plainly instead of parking on the ladder or hitting relay
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "thr-stderr", "--", "[[STUB:throttlestderr]]",
                                       env_extra=self.cap_env("thr-stderr", **compressed()))
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "thr-stderr", "astra")
        self.assertEqual((seat["attempt"], seat.get("limit_hits", 0), seat.get("throttle_hits", 0)), (1, 0, 0))

    def test_a_killed_run_reconnecting_on_websocket_403s_records_a_network_trigger(self):
        env = self.cap_env("thr-403", **compressed())
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--timeout", "4", "--job-dir", "thr-403", "--", "[[STUB:throttle=403ws,hang]]",
                                       env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "thr-403", "astra")
        self.assertTrue(seat["timed_out"])
        self.assertEqual(seat["throttle_class"], "codex-network")
        self.assertEqual(self.pacer(env)["reason"], "codex-network")

    def test_throttle_waits_are_not_charged_to_the_timeout(self):
        counter = self.tmp / "thr-clock-counter"
        env = self.cap_env("thr-clock", **compressed(BRIDGE_THROTTLE_WAIT_SCALE="0.05"))   # interactive first wait 3 s (x 0.6..1.4)
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--timeout", "4", "--job-dir", "thr-clock", "--",
                                       f"[[STUB:throttle=429,throttleonce={counter},sleep=2]]", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "thr-clock", "astra")
        self.assertEqual((seat["attempt"], seat["throttle_hits"]), (2, 1))
        self.assertGreater(seat["uncharged_wait_s"], 1.0)


class PacerRampUnit(unittest.TestCase):
    """B-1: the ramp ends when the doubled rate reaches the cap, decided before any exponentiation, so pacer state days old never
    raises; BRIDGE_RAMP_START=0 or BRIDGE_RAMP_STEP=0 means no ramp at all."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-cap-ramp-"))
        cls.base = {"BRIDGE_JOBS": str(cls.tmp / "jobs"), "HOME": str(cls.tmp), "BRIDGE_CAPACITY_DIR": str(cls.tmp / "cap"),
                    "LR_CU_COORD_DIR": str(cls.tmp / "coord"), "BRIDGE_NET_PROBE": "off"}
        cls.b = load_engine_module(cls.base)

    def test_old_pacer_state_ends_the_ramp_without_an_exception(self):
        now = time.time()
        for age in (2 * 86400, 30 * 86400, 123000, 10 * 365 * 86400):
            with self.subTest(age=age):
                pacer = {"ramp_started_at": now - age, "last_launch_at": now - 1}
                self.assertIsNone(self.b._ramp_rate(pacer, now, 100))          # no OverflowError: the ramp is simply over
                self.assertTrue(self.b._ramp_finished(pacer, now, 100))
                self.assertLessEqual(self.b._next_slot(pacer, now, 100), now - 1 + self.b.MIN_GAP * 1.5 + 1e-6)
        fresh = {"ramp_started_at": now - 1}
        self.assertEqual(self.b._ramp_rate(fresh, now, 100), 4.0)
        self.assertFalse(self.b._ramp_finished(fresh, now, 100))
        self.assertEqual(self.b._ramp_rate({"ramp_started_at": now - 4 * 120}, now, 100), 64.0)   # 4 doublings: 64/min, still under 100
        self.assertIsNone(self.b._ramp_rate({"ramp_started_at": now - 5 * 120}, now, 100))         # the fifth would reach 128 >= 100: over
        self.assertIsNone(self.b._ramp_rate({"ramp_started_at": now - 4 * 120}, now, 64))          # 64 >= 64: over exactly at the cap
        self.assertIsNone(self.b._ramp_rate({"ramp_started_at": now - 1}, now, 4))                 # a cap at or under the start: no ramp
        self.assertIsNone(self.b._ramp_rate({"ramp_started_at": now + 60}, now, 100))              # a clock skew: not started yet
        self.assertIsNone(self.b._ramp_rate({}, now, 100))

    def test_a_disabled_ramp_never_paces(self):
        now = time.time()
        for env in ({"BRIDGE_RAMP_START": "0"}, {"BRIDGE_RAMP_STEP": "0"}):
            with self.subTest(env=env):
                b = load_engine_module(dict(self.base, **env))
                pacer = {"ramp_started_at": now - 1, "last_launch_at": now}
                self.assertIsNone(b._ramp_rate(pacer, now, 100))
                self.assertTrue(b._ramp_finished(pacer, now, 100))
                self.assertIsNone(b._ramp_rate({"ramp_started_at": now - 30 * 86400}, now, 100))


class NoticeOnceUnit(unittest.TestCase):
    """B-7: the once-per-job capacity notices are checked and appended under ONE job lock, so two seats of a job that both see no
    notice of the kind cannot both append one."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-cap-notice-"))
        cls.b = load_engine_module({"BRIDGE_JOBS": str(cls.tmp / "jobs"), "HOME": str(cls.tmp), "BRIDGE_CAPACITY_DIR": str(cls.tmp / "cap"),
                                    "LR_CU_COORD_DIR": str(cls.tmp / "coord"), "BRIDGE_NET_PROBE": "off"})

    def test_two_seats_racing_the_same_notice_kind_leave_one_notice(self):
        d = self.tmp / "job-notice"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"notices": []}))
        barrier = threading.Barrier(2, timeout=5)
        real_add = self.b.add_notice

        def racing_add(*a, **kw):  # the old helper released the job lock between its check and add_notice: both seats get here
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return real_add(*a, **kw)

        self.b.add_notice = racing_add
        self.addCleanup(setattr, self.b, "add_notice", real_add)
        errors = []

        def seat(tag):
            try:
                self.b._notice_once_kind(d, "capacity-lease", f"NOTICE: seat {tag}: admitted while a computer-use task holds priority", seat=tag)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=seat, args=(tag,)) for tag in ("astra", "sol")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(errors, [])
        notices = [n for n in json.loads((d / "meta.json").read_text())["notices"] if n["kind"] == "capacity-lease"]
        self.assertEqual(len(notices), 1, notices)
        # a second, later call of the same kind is still a no-op; another kind is appended
        self.b._notice_once_kind(d, "capacity-lease", "NOTICE: seat luna: admitted while a computer-use task holds priority", seat="luna")
        self.b._notice_once_kind(d, "capacity", "NOTICE: seat luna: held", seat="luna")
        notices = json.loads((d / "meta.json").read_text())["notices"]
        self.assertEqual([n["kind"] for n in notices], ["capacity-lease", "capacity"])
        self.assertTrue(all(n.get("at") for n in notices))


class SharedContractUnit(unittest.TestCase):
    """The bridge's copies of the shared contract (spec 4): the config loader, the live-session count table (4.2) and the lease reader (4.1)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-cap-contract-"))
        (cls.tmp / "coord").mkdir()
        cls.env = {"BRIDGE_JOBS": str(cls.tmp / "jobs"), "HOME": str(cls.tmp), "BRIDGE_CAPACITY_DIR": str(cls.tmp / "cap"),
                   "LR_CU_COORD_DIR": str(cls.tmp / "coord"), "BRIDGE_NET_PROBE": "off"}
        cls.b = load_engine_module(cls.env)

    def with_env(self, extra):
        saved = dict(os.environ)
        os.environ.update(self.env)
        os.environ.update(extra)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))

    def test_config_defaults_file_and_environment_precedence(self):
        self.with_env({})
        cfg = self.b.capacity_config()
        self.assertEqual((cfg["session_cap"], cfg["interactive_reserve"], cfg["interactive_budget"], cfg["cap_mode"]), (100, 16, 24, "shadow"))
        self.assertEqual(cfg["source"], "built-in defaults")
        path = self.tmp / "coord" / "codex-capacity.json"
        path.write_text(json.dumps({"session_cap": 60, "interactive_reserve": 10, "cap_mode": "enforce", "interactive_budget": True}))
        cfg = self.b.capacity_config()
        self.assertEqual((cfg["session_cap"], cfg["interactive_reserve"], cfg["interactive_budget"], cfg["cap_mode"]), (60, 10, 24, "enforce"))
        self.assertEqual(cfg["source"], str(path))
        self.with_env({"BRIDGE_CODEX_SESSION_CAP": "7", "BRIDGE_CAP_MODE": "shadow", "BRIDGE_INTERACTIVE_BUDGET": "2", "BRIDGE_CODEX_INTERACTIVE_RESERVE": "1"})
        cfg = self.b.capacity_config()
        self.assertEqual((cfg["session_cap"], cfg["interactive_reserve"], cfg["interactive_budget"], cfg["cap_mode"]), (7, 1, 2, "shadow"))
        self.assertEqual(cfg["source"], "environment")
        path.write_text("not json")
        self.assertEqual(self.b.capacity_config()["session_cap"], 7)
        self.with_env({"BRIDGE_CODEX_SESSION_CAP": "many"})
        with self.assertRaises(SystemExit):
            self.b.capacity_config()
        self.with_env({"BRIDGE_CODEX_SESSION_CAP": "", "BRIDGE_CAP_MODE": "maybe"})
        with self.assertRaises(SystemExit):
            self.b.capacity_config()
        path.unlink()

    def test_count_live_sessions_table(self):
        # the fix-round table (FIX-ROUND-1 lane F-B; identical in the runner's copy): live = a fresh task_started turn, a ~2 MB long
        # turn with task_started at the top (long_turns), a resumed second turn in an old day folder with a fresh mtime, a subagent,
        # a partial last line after task_started, a 1.4 MB tail with no newline (degraded), a task_started 300 s old
        # (stalled_in_turn); not live = task_complete + token_usage_record, turn_aborted, an mtime older than the window,
        # task_complete beginning exactly 64 KiB before EOF, task_complete followed by one post-completion line longer than 64 KiB
        home = self.tmp / "home-count"
        write_rollout(home, "live")
        write_rollout(home, "longturn", big_turn=True)
        write_rollout(home, "olddayresumed", day="2026/09/01", cwd="/work/b", resumed=True)
        write_rollout(home, "sub", subagent=True, cwd="/work/b")
        write_rollout(home, "partial", partial=True)
        write_rollout(home, "nonewline", no_newline=True)
        write_rollout(home, "stalled", age_s=300)
        write_rollout(home, "finished", boundary="task_complete")
        write_rollout(home, "aborted", boundary="turn_aborted")
        write_rollout(home, "stale", age_s=2000)
        exact = write_rollout(home, "exact64k", boundary="task_complete", exact_tail=True)
        write_rollout(home, "bigtail", boundary="task_complete", big_tail=True)
        (home / "sessions" / "2026" / "10" / "01" / "notes.txt").write_text("ignored")
        data = exact.read_bytes()
        tc_at = data.rfind(b"\n", 0, data.index(b'"task_complete"')) + 1   # where the task_complete line begins
        self.assertEqual(len(data) - tc_at, 64 * 1024)         # the task_complete line begins exactly 64 KiB before EOF
        r = self.b.count_live_sessions(home)
        self.assertEqual(r["scanned"], 12)
        self.assertEqual(r["candidates"], 11)                 # the stale one is outside the window
        self.assertEqual(r["total"], 7, r)                     # live, longturn, olddayresumed, sub, partial, nonewline, stalled
        self.assertEqual((r["top_level"], r["subagents"]), (6, 1))
        self.assertEqual((r["long_turns"], r["degraded"], r["stalled_in_turn"]), (1, 1, 1))
        for k in ("total", "top_level", "subagents", "candidates", "long_turns", "degraded", "stalled_in_turn"):
            self.assertIs(type(r[k]), int, k)
        self.assertEqual(r["by_cwd"][0], ["/work/a", 5])
        self.assertEqual(r["by_cwd"][1], ["/work/b", 2])
        self.assertIsInstance(r["elapsed_s"], float)
        self.assertEqual(set(r), {"total", "top_level", "subagents", "by_cwd", "scanned", "candidates", "long_turns", "degraded",
                                  "stalled_in_turn", "elapsed_s"})
        # per file: the last boundary and whether the tail parsed at all
        s = home / "sessions" / "2026" / "10" / "01"
        rollout = lambda name: s / f"rollout-2026-10-01T10-00-00-{name}.jsonl"
        self.assertEqual(self.b._last_boundary(rollout("exact64k")), ("task_complete", True))   # the read starts on a line boundary: nothing is cut
        self.assertEqual(self.b._last_boundary(rollout("bigtail")), ("task_complete", True))    # beyond the 64 KiB tail: the 1 MiB growth finds it
        self.assertEqual(self.b._last_boundary(rollout("partial")), ("task_started", True))
        self.assertEqual(self.b._last_boundary(rollout("longturn")), (None, True))              # a long turn: no boundary in 1 MiB, lines parsed
        self.assertEqual(self.b._last_boundary(rollout("nonewline")), (None, False))            # no complete line at all
        self.assertEqual(self.b._last_boundary(home / "sessions" / "2026" / "09" / "01" / rollout("olddayresumed").name), ("task_started", True))
        r2 = self.b.count_live_sessions(home, window_s=5000)
        self.assertEqual(r2["total"], 8)
        self.assertEqual(self.b.count_live_sessions(self.tmp / "no-such-home")["total"], 0)
        # no account or user id is ever copied out
        self.assertNotIn("creator", json.dumps(r))

    def test_lease_reader_probes_the_lock_and_reads_reservations(self):
        self.with_env({})
        lease_dir = self.tmp / "coord" / "codex-cu-priority.d"
        lease_dir.mkdir(exist_ok=True)
        self.assertFalse(self.b.lease_state()["live"])
        host = self.b.lease_host()
        holder = lease_dir / f"holder-{host}-4242-abcd1234.json"
        holder.write_text(json.dumps({"v": 1, "kind": "process", "host": host, "pid": 4242, "task_dir": "/runs/x", "since": "2026-10-13T00:00:00Z"}))
        st = self.b.lease_state()
        self.assertFalse(st["live"])                          # a holder file nobody locks is stale
        self.assertEqual([x["task_dir"] for x in st["stale"]], ["/runs/x"])
        fd = os.open(str(holder), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # another open file description: what the runner holds
            st = self.b.lease_state()
            self.assertTrue(st["live"])
            self.assertEqual([x["task_dir"] for x in st["holders"]], ["/runs/x"])
            self.assertEqual(st["stale"], [])
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        self.assertFalse(self.b.lease_state()["live"])
        self.assertTrue(holder.exists())                       # the bridge never prunes
        holder.unlink()
        now = time.time()
        res = lease_dir / "reservation-0123456789abcdef.json"
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res.write_text(json.dumps({"v": 1, "kind": "reservation", "host": host, "task_dir": "/runs/y", "expires_at": iso(now + 600), "wait_cycle": 1}))
        st = self.b.lease_state()
        self.assertTrue(st["live"])
        self.assertEqual(st["reservations"][0]["task_dir"], "/runs/y")
        res.write_text(json.dumps({"v": 1, "kind": "reservation", "host": host, "task_dir": "/runs/y", "expires_at": iso(now - 1), "wait_cycle": 1}))
        self.assertFalse(self.b.lease_state()["live"])        # expired
        res.write_text(json.dumps({"v": 1, "kind": "reservation", "host": "other-mac", "task_dir": "/runs/y", "expires_at": iso(now + 600), "wait_cycle": 1}))
        self.assertFalse(self.b.lease_state()["live"])        # another host's reservation never holds here
        res.unlink()
        self.assertEqual(self.b.account_key(self.tmp), hashlib.sha256(os.path.realpath(str(self.tmp)).encode()).hexdigest()[:12])


class CapacityGate(CapacityMixin, EngineBase):
    """The gate in the Codex attempt loop (5.3): the session cap per class, the lease, PAUSE, the pacer, the budgets and the modes."""

    ENFORCE = {"BRIDGE_CAP_MODE": "enforce"}

    def test_fresh_live_rollouts_hold_a_batch_seat_until_they_finish(self):
        home = self.fresh_home("a")
        env = self.cap_env("a", CODEX_HOME=str(home), BRIDGE_CODEX_SESSION_CAP="5", BRIDGE_CODEX_INTERACTIVE_RESERVE="2", BRIDGE_CLASS="batch", **self.ENFORCE)
        paths = [write_rollout(home, f"live{i}", cwd="/work/fleet") for i in range(3)]
        p = self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", "cap-a", "--", "q")
        d = self.jobs / "cap-a"
        seat = self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
        self.assertEqual(seat["waiting_kind"], "capacity")
        self.assertIn("waiting for Codex capacity: 3 live sessions of 3 allowed for batch seats on", seat["activity"])
        self.assertEqual(seat["class"], "batch")
        for path in paths:
            finish_rollout(path)
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("[bridge] capacity: astra: waiting for Codex capacity: 3 live sessions of 3 allowed", err)
        self.assertIn("[bridge] capacity: astra: admitted after", err)
        seat = self.seat(d, "astra")
        self.assertEqual(seat["state"], "done")
        self.assertGreater(seat["uncharged_wait_s"], 0)
        kinds = [e["event"] for e in self.events(env)]
        self.assertEqual(kinds.count("hold"), 1)              # once per change of cause, not per poll
        self.assertIn("admit", kinds)
        self.assertIn("release", kinds)
        texts = [n["text"] for n in self.meta(d)["notices"] if n["kind"] == "capacity"]
        self.assertEqual(len(texts), 1)                        # one capacity notice per job
        self.assertIn("waiting for Codex capacity", texts[0])
        # an interactive seat may use the reserve: 3 live + 1 <= 5
        for path in paths:
            os.utime(path, None)
            path.write_text(path.read_text().rsplit(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}), 1)[0])
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "cap-a-inter", "--", "q", env_extra=dict(env, BRIDGE_CLASS="interactive"))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "cap-a-inter", "astra"))

    def test_the_ledger_serializes_simultaneous_launches(self):
        home = self.fresh_home("race")
        env = self.cap_env("race", CODEX_HOME=str(home), BRIDGE_CODEX_SESSION_CAP="3", BRIDGE_CODEX_INTERACTIVE_RESERVE="0", BRIDGE_CLASS="batch", **self.ENFORCE)
        holds = [self.tmp / f"race-hold-{i}" for i in range(6)]
        procs = [self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", f"race-{i}", "--", f"[[STUB:hold={holds[i]}]]") for i in range(6)]
        time.sleep(8)
        ready = [h for h in holds if Path(str(h) + ".ready").exists()]
        self.assertEqual(len(ready), 3, ready)                 # at most cap / weight reached the stub
        waiting = [i for i in range(6) if self.seat(self.jobs / f"race-{i}", "astra").get("state") == "waiting"]
        self.assertEqual(len(waiting), 3)
        for h in holds:
            Path(str(h) + ".release").write_text("go")
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, out + err)
        ledger = json.loads((Path(env["BRIDGE_CAPACITY_DIR"]) / f"{hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:12]}.json").read_text())
        self.assertEqual(ledger["entries"], [])              # every entry released
        self.assertEqual(ledger["home"], os.path.realpath(str(home)))

    def test_the_lease_holds_batch_seats_and_admits_interactive_ones(self):
        env = self.cap_env("lease", BRIDGE_CLASS="batch")
        helper, holder_path = self.hold_lease(env, task_dir="/runs/2026-10-13_westlaw-pull")
        p = self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", "lease-batch", "--", "q")
        d = self.jobs / "lease-batch"
        seat = self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
        self.assertEqual(seat["activity"].split(";")[0], "held: computer-use task /runs/2026-10-13_westlaw-pull has priority")
        # a two-seat interactive review is admitted at once, with a notice (shadow mode is the default: the lease still holds batch)
        rc, out, err = self.run_engine("review", "--seats", "astra:low,sol:low", "--job-dir", "lease-review", env_extra=dict(env, BRIDGE_CLASS="interactive"))
        self.assertEqual(rc, 0, out + err)
        rd = self.jobs / "lease-review"
        for tag in ("astra", "sol"):
            self.assertEqual(self.seat(rd, tag)["state"], "done")
            self.assertNotIn("uncharged_wait_s", self.seat(rd, tag))
        texts = [n["text"] for n in self.meta(rd)["notices"] if n["kind"] == "capacity-lease"]
        self.assertEqual(len(texts), 1)
        self.assertIn("/runs/2026-10-13_westlaw-pull", texts[0])
        self.assertEqual(self.seat(d, "astra")["state"], "waiting")   # the batch seat is still held
        self.stop_helper(helper)                                        # the runner dies: the kernel drops the lock
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(self.seat(d, "astra")["state"], "done")
        self.assertTrue(Path(holder_path).exists())                     # the bridge never prunes the runner's files
        # a holder file with no holder and no reservation does not hold; it is reported stale once
        _none, stale_path = self.hold_lease(env, task_dir="/runs/dead", unlocked=True)
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "lease-stale", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "lease-stale", "astra"))
        stale = [e for e in self.events(env) if e["event"] == "stale-lease"]
        self.assertTrue(stale and Path(stale_path).name in stale[-1]["files"], stale)
        self.assertTrue(Path(stale_path).exists())
        # reservations: an unexpired one for this host holds, an expired one does not
        lease_dir = Path(env["LR_CU_COORD_DIR"]) / "codex-cu-priority.d"
        host = socket.gethostname().split(".")[0]
        res = lease_dir / "reservation-00112233aabbccdd.json"
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res.write_text(json.dumps({"v": 1, "kind": "reservation", "host": host, "task_dir": "/runs/reserved", "expires_at": iso(time.time() + 3), "wait_cycle": 1}))
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "lease-res", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "lease-res", "astra")
        self.assertGreater(seat["uncharged_wait_s"], 1)                 # held until the reservation expired
        self.assertIn("held: computer-use task /runs/reserved has priority", err)
        res.write_text(json.dumps({"v": 1, "kind": "reservation", "host": host, "task_dir": "/runs/reserved", "expires_at": iso(time.time() - 1), "wait_cycle": 1}))
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "lease-res2", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "lease-res2", "astra"))

    def test_a_wake_quiets_batch_launches_then_ramps_and_never_holds_interactive_seats(self):
        env = self.cap_env("wake", BRIDGE_CLASS="batch", BRIDGE_WAKE_QUIET="3", BRIDGE_RAMP_START="20", BRIDGE_RAMP_STEP="600")
        # the first observation of an account only initializes last_seen_waketime
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "wake-0", "--", "q", env_extra=dict(env, BRIDGE_FAKE_WAKETIME="1000"))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "wake-0", "astra"))
        woke = dict(env, BRIDGE_FAKE_WAKETIME=str(int(time.time())))
        # an interactive seat skips the quiet period
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "wake-inter", "--", "q", env_extra=dict(woke, BRIDGE_CLASS="interactive"))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "wake-inter", "astra"))
        self.assertEqual(self.pacer(env)["reason"], "wake")
        # a batch seat waits out the quiet period, then is admitted
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "wake-1", "--", "q", env_extra=woke)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "wake-1", "astra")
        self.assertGreater(seat["uncharged_wait_s"], 0.5)
        self.assertIn("post-wake quiet period", err)
        # the next batch launches are paced by the ramp (20/min at the start: a 3 s slot, +/-30%)
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "wake-2", "--", "q", env_extra=woke)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("launch pacer: ramping after wake (20/min)", err)
        self.assertGreater(self.seat(self.jobs / "wake-2", "astra")["uncharged_wait_s"], 0.5)
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "wake-inter2", "--", "q", env_extra=dict(woke, BRIDGE_CLASS="interactive"))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "wake-inter2", "astra"))   # the rate limit is for batch seats

    def test_days_old_pacer_state_neither_crashes_admission_nor_doctor(self):
        # B-1: ramp_started_at was never cleared and `4.0 * 2**steps` overflowed about 34 h after any trigger, crashing every batch
        # admission (even in shadow mode) and doctor/status. Pacer state 2 and 30 days old: admission proceeds, the views print, and the
        # finished ramp is cleared from the pacer under the account lock
        for days in (2, 30):
            with self.subTest(days=days):
                home = self.fresh_home(f"oldramp-{days}")
                env = self.cap_env(f"oldramp-{days}", CODEX_HOME=str(home), BRIDGE_CLASS="batch")
                key = hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:12]
                pacer_path = Path(env["BRIDGE_CAPACITY_DIR"]) / f"{key}.pacer.json"
                pacer_path.write_text(json.dumps({"ramp_started_at": time.time() - days * 86400, "last_launch_at": time.time() - days * 86400,
                                                  "reason": "codex-throttled", "home": os.path.realpath(str(home))}))
                for argv in (["doctor"], ["status"]):
                    rc, out, err = self.run_engine(*argv, env_extra=env)
                    self.assertEqual(rc, 0, out + err)
                    self.assertNotIn("OverflowError", out + err)
                    self.assertIn("ramp: none", out)
                rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", f"oldramp-{days}", "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("OverflowError", err)
                self.assertEqual(self.seat(self.jobs / f"oldramp-{days}", "astra")["state"], "done")
                self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / f"oldramp-{days}", "astra"))
                self.assertIsNone(self.pacer(env, home=str(home)).get("ramp_started_at"))   # the finished ramp is cleared

    def test_a_batch_seat_that_first_sees_a_wake_waits_the_quiet_period_once(self):
        env = self.cap_env("wake-batch-first", BRIDGE_CLASS="batch", BRIDGE_WAKE_QUIET="2", BRIDGE_RAMP_START="600")
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "wbf-0", "--", "q", env_extra=dict(env, BRIDGE_FAKE_WAKETIME="1000"))
        self.assertEqual(rc, 0, out + err)
        t0 = time.time()
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "wbf-1", "--", "q", env_extra=dict(env, BRIDGE_FAKE_WAKETIME=str(int(t0))))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("post-wake quiet period", err)
        self.assertLess(time.time() - t0, 40)                  # the wake was seen once: the quiet period did not restart at every poll
        self.assertEqual(self.pacer(env)["last_seen_waketime"], float(int(t0)))

    def test_a_capacity_wait_is_not_charged_to_the_timeout(self):
        env = self.cap_env("clock", BRIDGE_CLASS="batch")
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "task", "--seats", "astra:low", "--timeout", "4", "--job-dir", "clock", "--", "[[STUB:sleep=2]]")
        d = self.jobs / "clock"
        seat = self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
        self.assertEqual(seat["activity"].split(";")[0], f"batch launches paused by {pause}")
        time.sleep(3)
        pause.unlink()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        seat = self.seat(d, "astra")
        self.assertEqual(seat["state"], "done")
        self.assertGreater(seat["uncharged_wait_s"], 2.5)
        self.assertFalse(seat.get("timed_out"))

    def test_shadow_logs_would_hold_enforce_holds_and_off_skips_the_gate(self):
        home = self.fresh_home("modes")
        base = self.cap_env("modes", CODEX_HOME=str(home), BRIDGE_CODEX_SESSION_CAP="1", BRIDGE_CODEX_INTERACTIVE_RESERVE="0", BRIDGE_CLASS="batch")
        write_rollout(home, "busy1")
        write_rollout(home, "busy2")
        # shadow (the default): over the cap is logged once and admitted
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "modes-shadow", "--", "q", env_extra=base)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(err.count("would hold: waiting for Codex capacity: 2 live sessions of 1 allowed for batch seats"), 1)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "modes-shadow", "astra"))
        self.assertIn("shadow", [e["event"] for e in self.events(base)])
        # ... while the lease still holds in shadow mode
        helper, _path = self.hold_lease(base)
        p = self.start_engine(base, "task", "--seats", "astra:low", "--job-dir", "modes-shadow-lease", "--", "q")
        seat = self.wait_for(self.jobs / "modes-shadow-lease", "astra", lambda s: s.get("state") == "waiting")
        self.assertTrue(seat["activity"].startswith("held: computer-use task"))
        self.stop_helper(helper)
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        # enforce: held until the cap clears
        p = self.start_engine(dict(base, **self.ENFORCE), "task", "--seats", "astra:low", "--job-dir", "modes-enforce", "--", "q")
        seat = self.wait_for(self.jobs / "modes-enforce", "astra", lambda s: s.get("state") == "waiting")
        self.assertTrue(seat["activity"].startswith("waiting for Codex capacity: 2 live sessions of 1 allowed"))
        rc, out, err = self.run_engine("cancel", "modes-enforce")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=60)
        self.assertEqual(self.seat(self.jobs / "modes-enforce", "astra")["state"], "cancelled")
        # off: no gate at all (over the cap, lease live, PAUSE set): nothing waits, no ledger is written
        helper, _path = self.hold_lease(base)
        (Path(base["BRIDGE_CAPACITY_DIR"]) / "PAUSE").write_text("x")
        off = dict(base, BRIDGE_CAPACITY="off", **self.ENFORCE)
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "modes-off", "--", "q", env_extra=off)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "modes-off", "astra"))
        self.assertNotIn("[bridge] capacity", err)
        self.assertEqual([e["event"] for e in self.events(base) if e.get("job") == "modes-off"], [])
        (Path(base["BRIDGE_CAPACITY_DIR"]) / "PAUSE").unlink()
        # ... but the throttle class still applies with the gate off
        counter = self.tmp / "modes-off-counter"
        rc, out, err = self.run_engine("task", "--seats", "astra:low", "--job-dir", "modes-off-thr", "--", f"[[STUB:throttle=429,throttleonce={counter}]]",
                                       env_extra=dict(off, **compressed()))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.seat(self.jobs / "modes-off-thr", "astra")["throttle_hits"], 1)

    def test_pause_holds_batch_seats_only(self):
        env = self.cap_env("pause")
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("Kevin: fleet paused")
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "pause-inter", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("uncharged_wait_s", self.seat(self.jobs / "pause-inter", "astra"))
        p = self.start_engine(dict(env, BRIDGE_CLASS="batch"), "task", "--seats", "astra:low", "--job-dir", "pause-batch", "--", "q")
        seat = self.wait_for(self.jobs / "pause-batch", "astra", lambda s: s.get("state") == "waiting")
        self.assertEqual(seat["activity"].split(";")[0], f"batch launches paused by {pause}")
        rc, out, err = self.run_engine("reply", "pause-batch", "--", "more", env_extra=env)
        self.assertEqual(rc, 1)
        self.assertIn("is waiting for Codex capacity", err)
        pause.unlink()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)

    def test_an_interactive_seat_over_the_budget_waits_like_a_batch_seat(self):
        env = self.cap_env("budget", BRIDGE_INTERACTIVE_BUDGET="1")
        helper, _path = self.hold_lease(env)
        hold = self.tmp / "budget-hold"
        p = self.start_engine(env, "council", "--seats", "astra:low,sol:low", "--job-dir", "budget", "--", f"[[STUB:hold={hold}]] q")
        d = self.jobs / "budget"
        states = {}
        for _ in range(150):   # whichever seat took the lock first is the interactive one; the other is over the budget and held by the lease
            states = {t: self.seat(d, t).get("state") for t in ("astra", "sol") if (d / f"seat-{t}.json").exists()}
            if sorted(states.values()) == ["running", "waiting"] and Path(str(hold) + ".ready").exists():
                break
            time.sleep(0.1)
        self.assertEqual(sorted(states.values()), ["running", "waiting"], states)
        held = next(t for t, st in states.items() if st == "waiting")
        admitted = next(t for t, st in states.items() if st == "running")
        self.assertTrue(self.seat(d, held)["activity"].startswith("held: computer-use task"))
        self.assertEqual((self.seat(d, held)["class"], self.seat(d, admitted)["class"]), ("batch", "interactive"))
        texts = [n["text"] for n in self.meta(d)["notices"] if n["kind"] == "capacity-class"]
        self.assertEqual(len(texts), 1)
        self.assertIn("interactive budget used; this seat waits like a batch seat", texts[0])
        Path(str(hold) + ".release").write_text("go")
        self.stop_helper(helper)
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)

    def test_an_interactive_seat_held_at_the_hard_cap_fails_after_its_budget(self):
        home = self.fresh_home("hard")
        env = self.cap_env("hard", CODEX_HOME=str(home), BRIDGE_CODEX_SESSION_CAP="1", BRIDGE_CODEX_INTERACTIVE_RESERVE="0",
                           BRIDGE_CAPACITY_MAX_WAIT_INTERACTIVE="2", **self.ENFORCE)
        write_rollout(home, "busy1")
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "hard", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "hard", "astra")
        self.assertEqual(seat["state"], "failed")
        self.assertIn("Codex capacity: waiting for Codex capacity: 1 live sessions of 1 allowed for interactive seats", seat["activity"])
        self.assertIn("not launched", seat["activity"])

    def test_the_class_flag_and_its_validation(self):
        env = self.cap_env("class")
        rc, out, err = self.run_engine("task", "--class", "batch", "--seats", "astra:low", "--job-dir", "class-flag", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((self.meta(self.jobs / "class-flag")["class"], self.seat(self.jobs / "class-flag", "astra")["class"]), ("batch", "batch"))
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "class-env", "--", "q", env_extra=dict(env, BRIDGE_CLASS="fleet"))
        self.assertEqual(rc, 2, out + err)
        self.assertIn("launch class must be one of interactive, batch", err)
        spec = self.tmp / "class-fanout.json"
        spec.write_text(json.dumps({"seats": "astra:low", "schema": False, "tasks": {"one": "q"}}))
        rc, out, err = self.run_engine("fanout", "--spec", str(spec), "--job-dir", "class-fanout", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.meta(self.jobs / "class-fanout")["class"], "batch")

    def test_doctor_and_status_show_the_capacity_section(self):
        home = self.fresh_home("show")
        env = self.cap_env("show", CODEX_HOME=str(home))
        write_rollout(home, "one")
        write_rollout(home, "two", subagent=True)
        helper, _path = self.hold_lease(env, task_dir="/runs/2026-10-13_shown")
        (Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE").write_text("x")
        for argv in (["doctor"], ["status"]):
            with self.subTest(view=argv[0]):
                rc, out, err = self.run_engine(*argv, env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("capacity: cap_mode shadow (session cap 100, interactive reserve 16, interactive budget 24; from built-in defaults)", out)
                self.assertIn("lease: LIVE: computer-use task /runs/2026-10-13_shown has priority", out)
                self.assertIn("PAUSE: set at", out)
                self.assertRegex(out, r"account [0-9a-f]{12} \(.*codex-home-show\): live sessions 2 \(top-level 1, subagents 1\), pending 0, allowed 84 batch / 100 interactive; ramp: none")



def write_marker(env, model_id, kind):
    """A sticky lifecycle marker in the isolated evidence store, in the layout the check persists (as test_bridge's write_marker),
    replaced atomically: a running seat may read the store at any time."""
    p = Path(env["BRIDGE_MODELS_EVIDENCE"])
    doc = json.loads(p.read_text()) if p.is_file() else {}
    doc.setdefault("markers", {})[model_id] = {
        "id": model_id, "kind": kind, "kinds": [kind], "seen_at": time.time(), "read_seq": int(doc.get("clear_seq") or 0),
        "source": "codex model/list", "detail": f"{kind} (test)"}
    doc.setdefault("cleared", [])
    doc.setdefault("clear_seq", 0)
    tmp = p.with_name(f".{p.name}.test-{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.chmod(0o600)
    os.replace(tmp, p)


class FixRound1Gate(CapacityMixin, EngineBase):
    """Fix round 1, B-2 .. B-6: the admission interval and the routed wait budget, the effective class, the re-check after a hold and
    the pinned throttled continuation."""

    def test_the_whole_admission_interval_is_uncharged(self):
        # B-6: only the polling sleeps were credited back to --timeout; the lock waits, probes, ps snapshots and rollout scans of every
        # admission decision stayed charged. Here the test holds the account lock in 1.2-s bursts while a PAUSE holds the seat
        # (BRIDGE_HOLD_POLL 0.5 s: every decision blocks on the lock), then lifts the PAUSE: the 3-s run must still fit its 5-s --timeout
        home = self.fresh_home("admit-slow")
        env = self.cap_env("admit-slow", CODEX_HOME=str(home), BRIDGE_CLASS="batch", BRIDGE_HOLD_POLL="0.5")
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "task", "--seats", "astra:low", "--timeout", "5", "--job-dir", "admit-slow", "--", "[[STUB:sleep=3]]")
        d = self.jobs / "admit-slow"
        self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
        fd = os.open(str(self.account_lock(env, home)), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            t0 = time.time()
            for _ in range(4):
                fcntl.flock(fd, fcntl.LOCK_EX)
                time.sleep(1.2)
                fcntl.flock(fd, fcntl.LOCK_UN)
                time.sleep(0.3)
        finally:
            os.close(fd)
        held_s = time.time() - t0
        pause.unlink()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        seat = self.seat(d, "astra")
        self.assertEqual(seat["state"], "done", seat)
        self.assertFalse(seat.get("timed_out"))
        self.assertGreater(seat["uncharged_wait_s"], held_s - 1.0)   # the whole hold, decisions included (not only the 0.3-0.7 s sleeps)
        self.assertEqual(self.ledger(env, home)["entries"], [])

    def test_a_routed_seats_wait_budget_follows_its_clock_through_a_hold(self):
        # B-4: SeatRouting.budget was a separate deadline cut from the original --timeout and never moved by uncharged waits, so a routed
        # seat held longer than its --timeout failed on the first busy refusal after the hold. Hold (PAUSE, 5 s) -> busy -> success
        home = self.fresh_home("routed-hold")
        renv, rd = self.relay("routed-hold", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 1}}})
        env = self.cap_env("routed-hold", CODEX_HOME=str(home), BRIDGE_CLASS="batch", **renv)
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "task", "--seats", "astra:low", "--timeout", "4", "--job-dir", "routed-hold", "--", "q")
        d = self.jobs / "routed-hold"
        self.wait_for(d, "astra", lambda s: s.get("state") == "waiting" and s.get("waiting_kind") == "capacity")
        time.sleep(5)
        pause.unlink()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        seat = self.seat(d, "astra")
        self.assertEqual((seat["state"], seat["account"], seat.get("busy_waits"), seat.get("limit_hits", 0)), ("done", "a", 1, 0), seat)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])
        self.assertGreater(seat["uncharged_wait_s"], 4)
        # BRIDGE_ROUTED_MAX_WAIT stays its own limit: the same hold with a 2-s routed budget still ends at that budget
        (rd / "busy-a").unlink(missing_ok=True)
        pause.write_text("hold on")
        p = self.start_engine(dict(env, BRIDGE_ROUTED_MAX_WAIT="2", BRIDGE_LIMIT_WAIT_SHORT="20"), "task", "--seats", "astra:low", "--timeout", "60",
                              "--job-dir", "routed-hold-2", "--", "q")
        self.wait_for(self.jobs / "routed-hold-2", "astra", lambda s: s.get("state") == "waiting" and s.get("waiting_kind") == "capacity")
        time.sleep(3)
        pause.unlink()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 1, out + err)
        seat = self.seat(self.jobs / "routed-hold-2", "astra")
        self.assertEqual(seat["state"], "failed")
        self.assertIn("wait budget", seat["activity"])

    def test_a_marker_published_during_a_capacity_hold_refuses_the_launch_and_releases_the_ledger(self):
        # B-3: the capacity hold ran after _launch_check and could last indefinitely, so a deprecation marker published during a PAUSE,
        # lease or capacity hold was ignored at launch (the post-wait validation that routing waits already have). The check runs
        # again after admission, immediately before the launch, and a refusal releases the ledger entry
        model = head("astra")
        env, _case = self.isolated("cap-marker")
        home = self.fresh_home("cap-marker")
        env.update(self.cap_env("cap-marker", CODEX_HOME=str(home), BRIDGE_CLASS="batch"))
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", "cap-marker", "--", "q")
        d = self.jobs / "cap-marker"
        try:
            self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
            write_marker(env, model, "deprecated")   # another invocation's check publishes a marker meanwhile
            pause.unlink()
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(self.launched_models(env), [], f"the launch ran {model}, marked while the seat waited for capacity:\n{out}{err}")
        seat = self.seat(d, "astra")
        self.assertEqual((seat.get("state"), seat.get("first_launch_at")), ("failed", None), seat)
        self.assertIn(f"{model} is marked deprecated", seat.get("activity", "") + out)
        self.assertNotEqual(p.returncode, 0, out + err)
        self.assertEqual(self.ledger(env, home)["entries"], [])              # the admitted entry was released with the refusal
        self.assertIn("release", [e["event"] for e in self.events(env)])

    def test_a_pinned_reply_re_verifies_its_sign_in_after_a_long_hold(self):
        # B-3 (sol): a pinned reply verified its account's sign-in before the hold; after a hold of REPICK_WAIT or more the verification is
        # stale and is refreshed right before the launch, so a sign-in that changed hands during the hold refuses the reply
        renv, rd = self.relay("pinned-hold", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env = self.cap_env("pinned-hold", **renv)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "pinned-hold", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "pinned-hold"
        verifies = lambda: len([c for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"] and "--verify" in c["argv"]])
        before = verifies()
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "reply", "pinned-hold", "--class", "batch", "--", "more")
        try:
            reply_seat = None
            for _ in range(150):
                seats = list(d.glob("seat-luna-reply-*.json"))
                if seats and json.loads(seats[0].read_text()).get("state") == "waiting":
                    reply_seat = seats[0]
                    break
                time.sleep(0.1)
            self.assertIsNotNone(reply_seat, "the reply never parked on the PAUSE")
            self.assertEqual(verifies(), before + 1)                           # verified once, before the hold
            time.sleep(1.0)                                                    # REPICK_WAIT is 0.6 s here: the verification goes stale
            tmp = rd / "script.json.new"
            tmp.write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "verify": {"a": {"email": "someone-else@example.test"}}}))
            os.replace(tmp, rd / "script.json")                                # what a live re-read now finds
            pause.unlink()
        finally:
            out, err = p.communicate(timeout=120)
        seat = json.loads(reply_seat.read_text())
        self.assertEqual((p.returncode, seat["state"]), (1, "failed"), out + err)
        self.assertIn("account a is now signed in as someone-else@example.test, not a@example.test whose thread this is; refusing to run", out)
        self.assertEqual(verifies(), before + 2, "the stale verification was not refreshed after the hold")
        self.assertEqual(len([e for e in self.records(rd, "execs.jsonl")]), 1)   # only the first ask ran

    def test_a_seat_over_the_interactive_budget_follows_the_batch_network_hold(self):
        # B-5: the interactive budget overflow admitted the seat as batch, but the network bypass still used the original interactive
        # class: a demoted seat launched after the interactive network hold with the probe still failing. A closed local port stands
        # for an unreachable chatgpt.com (BRIDGE_NETWORK_HOLD_INTERACTIVE 2 s): the demoted seat is still held well after it
        env = self.cap_env("budget-net", BRIDGE_INTERACTIVE_BUDGET="0", BRIDGE_NET_PROBE="127.0.0.1:1", BRIDGE_NETWORK_HOLD_INTERACTIVE="2",
                           BRIDGE_HOLD_POLL="0.5")
        p = self.start_engine(env, "ask", "--seats", "astra:low", "--job-dir", "budget-net", "--", "q")
        d = self.jobs / "budget-net"
        seat = self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
        self.assertTrue(seat["activity"].startswith("network down: 127.0.0.1 unreachable"), seat)
        self.assertEqual(seat["class"], "batch")
        time.sleep(6)
        self.still_held(d, "astra")   # a demoted seat must not escape the network hold like an interactive one
        rc, out, err = self.run_engine("cancel", "budget-net")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=60)
        self.assertEqual(self.seat(d, "astra")["state"], "cancelled")
        # the control: a seat within the interactive budget proceeds after the interactive network hold
        env2 = self.cap_env("budget-net-inter", BRIDGE_NET_PROBE="127.0.0.1:1", BRIDGE_NETWORK_HOLD_INTERACTIVE="2", BRIDGE_HOLD_POLL="0.5")
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "budget-net-inter", "--", "q", env_extra=env2)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "budget-net-inter", "astra")
        self.assertEqual((seat["state"], seat["class"]), ("done", "interactive"))
        self.assertGreater(seat["uncharged_wait_s"], 1.5)

    def test_a_seat_over_the_interactive_budget_follows_the_batch_throttle_ladder(self):
        # B-5: the throttle ladder used the original interactive class, so a seat admitted as batch failed after two waits ("Codex
        # throttled twice") instead of waiting on the unbounded batch ladder. Three throttled attempts: the seat is still waiting
        env = self.cap_env("budget-thr", BRIDGE_INTERACTIVE_BUDGET="0", **compressed(STUB_CODEX_THROTTLE="429"))
        p = self.start_engine(env, "ask", "--seats", "astra:low", "--job-dir", "budget-thr", "--", "q")
        d = self.jobs / "budget-thr"
        seat = self.wait_for(d, "astra", lambda s: s.get("throttle_hits", 0) >= 3 and s.get("state") in ("waiting", "failed"), tries=600)
        self.assertEqual((seat["state"], seat["waiting_kind"], seat["class"]), ("waiting", "throttle", "batch"), seat)
        self.assertNotIn("throttled twice", seat["activity"])
        rc, out, err = self.run_engine("cancel", "budget-thr")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=60)
        self.assertEqual(self.seat(d, "astra")["state"], "cancelled")

    def test_a_throttled_continuation_stays_on_its_account_through_a_busy_refusal(self):
        # B-2: a throttled attempt that did work kept its thread but not its account; a relay busy refusal on the retry then leave()d,
        # and the next pick could choose account b with account a's resume id. The continuation is pinned to the owning account for
        # every retry, busy refusals included: the second exec under a is busy, the third resumes the thread there; b is never used
        counter = self.tmp / "pin-thr-counter"
        renv, rd = self.relay("pin-thr", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]},
                                          "busy": {"a": {"times": 1, "after": 1}}})
        env = self.cap_env("pin-thr", STUB_CODEX_RECORD=str(self.tmp / "pin-thr-record.jsonl"), **compressed(**renv))
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "pin-thr", "--",
                                       f"[[STUB:throttle=429,throttleonce={counter}]]", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "pin-thr", "astra")
        self.assertEqual((seat["state"], seat["account"], seat["throttle_hits"], seat.get("busy_waits"), seat.get("limit_hits", 0)), ("done", "a", 1, 1, 0), seat)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a", "a"])
        self.assertEqual(len(self.picks(rd)), 1)                                 # the continuation never asked for another account
        self.assertEqual(self.records(rd, "hits.jsonl"), [])
        execs = [r for r in self.read_records(env["STUB_CODEX_RECORD"]) if r.get("mode") == "exec"]
        self.assertEqual([r["resume"] for r in execs], [None, "stub-thread-01a0"])  # the resumed turn ran where the thread lives
        self.assertEqual(seat["thread_id"], "stub-thread-stub-thread-01a0")


class FixRound2Gate(CapacityMixin, EngineBase):
    """Fix round 2 (the closing review of round 1), B2b-1 .. B2b-3: the pre-launch re-check after EVERY admission (a first decision that
    blocked on the account lock, the pinned re-verification itself, a throttled continuation admitted at once), the routed wait budget
    without --timeout, and the network exception decided from the class resolved under the lock."""

    def wait_for_lock_open(self, p, lock):
        """Until the engine process has opened the account lock file (job_lock opens it, then blocks in flock): past its pre-admission
        _launch_check, inside its first admission decision."""
        names = {str(lock), os.path.realpath(str(lock))}  # lsof prints the resolved path (/private/var/... for a temp dir under /var)
        for _ in range(300):
            out = subprocess.run(["lsof", "-p", str(p.pid), "-Fn"], capture_output=True, text=True, timeout=30).stdout
            if any(f"\nn{name}\n" in out or out.endswith(f"\nn{name}") for name in names):
                return
            time.sleep(0.1)
        self.fail(f"the engine never opened the account lock {lock}")

    def add_ledger_entry(self, env, home, pid, cls, **extra):
        """An admitted ledger entry of a live process on the account (as the gate writes them), appended under the account lock."""
        mod = load_engine_module(dict(self.env, **env))
        key = hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:12]
        path = Path(env["BRIDGE_CAPACITY_DIR"]) / f"{key}.json"
        fd = os.open(str(self.account_lock(env, home)), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            doc = json.loads(path.read_text()) if path.exists() else {"home": os.path.realpath(str(home)), "entries": []}
            doc["entries"].append({"id": "test-" + cls, "job": "other-job", "tag": "other", "class": cls, "weight": 1, "pid": pid,
                                   "pid_start": mod.proc_start(pid), "admitted_at": time.time(), **extra})
            tmp = path.with_name(path.name + ".new")
            tmp.write_text(json.dumps(doc))
            os.replace(tmp, path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_a_marker_published_while_the_first_decision_waits_on_the_account_lock_refuses_the_launch(self):
        # B2b-1 (a): the post-admission re-check ran only after a hold (ctx["held"]), but a seat's FIRST decision can block on the account
        # lock for a while and then admit directly (held False): a marker published during that wait launched. The test holds the lock,
        # waits until the engine has opened it (past the pre-admission check, blocked in flock), publishes the marker, releases the lock
        model = head("astra")
        env, _case = self.isolated("lock-marker")
        home = self.fresh_home("lock-marker")
        env.update(self.cap_env("lock-marker", CODEX_HOME=str(home), BRIDGE_CLASS="batch"))
        lock = self.account_lock(env, home)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        p = self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", "lock-marker", "--", "q")
        d = self.jobs / "lock-marker"
        try:
            self.wait_for(d, "astra", lambda s: "catalog lists" in s.get("activity", ""))   # the selection is done
            self.wait_for_lock_open(p, lock)
            write_marker(env, model, "deprecated")   # another invocation's check publishes a marker while the decision waits for the lock
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            out, err = p.communicate(timeout=120)
        self.assertEqual(self.launched_models(env), [], f"the launch ran {model}, marked while the first decision waited for the lock:\n{out}{err}")
        seat = self.seat(d, "astra")
        self.assertEqual((seat.get("state"), seat.get("first_launch_at")), ("failed", None), seat)
        self.assertIn(f"{model} is marked deprecated", seat.get("activity", "") + out)
        self.assertNotEqual(p.returncode, 0, out + err)
        self.assertEqual(self.ledger(env, home)["entries"], [])              # the admitted entry was released with the refusal
        events = [e["event"] for e in self.events(env)]
        self.assertIn("admit", events)                                       # admitted directly (no hold event) ...
        self.assertNotIn("hold", events)
        self.assertIn("release", events)                                     # ... and released by the refusal

    def test_a_marker_published_during_the_pinned_re_verification_after_a_hold_refuses_the_launch(self):
        # B2b-1 (b): after a hold the marker check ran BEFORE the pinned seat's stale sign-in re-verification (up to 60 s of relay calls),
        # so a marker published during the verification launched. The verification runs first, the marker check last, right before the
        # launch: the stub relay's `accounts --verify` sleeps 3 s and the marker lands meanwhile
        model = head("luna")
        env, _case = self.isolated("verify-marker")
        renv, rd = self.relay("verify-marker", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(self.cap_env("verify-marker", BRIDGE_HOLD_POLL="0.5", **renv))
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "verify-marker", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "verify-marker"
        verifies = lambda: len([c for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"] and "--verify" in c["argv"]])
        before = verifies()
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "reply", "verify-marker", "--class", "batch", "--", "more")
        try:
            reply_seat = None
            for _ in range(150):
                seats = list(d.glob("seat-luna-reply-*.json"))
                if seats and json.loads(seats[0].read_text()).get("state") == "waiting":
                    reply_seat = seats[0]
                    break
                time.sleep(0.1)
            self.assertIsNotNone(reply_seat, "the reply never parked on the PAUSE")
            self.assertEqual(verifies(), before + 1)                           # verified once, before the hold
            time.sleep(1.0)                                                    # REPICK_WAIT is 0.6 s here: the verification goes stale
            tmp = rd / "script.json.new"
            tmp.write_text(json.dumps({"pick": {"codex": [{"exit": 0, "label": "a"}]}, "accounts_sleep": 3}))  # the re-verification is slow
            os.replace(tmp, rd / "script.json")
            pause.unlink()
            for _ in range(300):                                               # the re-verification has begun (calls.jsonl records the start)
                if verifies() >= before + 2:
                    break
                time.sleep(0.1)
            self.assertEqual(verifies(), before + 2, "the stale verification was not refreshed after the hold")
            write_marker(env, model, "deprecated")                             # published while the verification is in flight
        finally:
            out, err = p.communicate(timeout=120)
        seat = json.loads(reply_seat.read_text())
        self.assertEqual(len(self.records(rd, "execs.jsonl")), 1, f"the reply launched {model}, marked during its re-verification:\n{out}{err}")
        self.assertEqual((p.returncode, seat["state"], seat.get("first_launch_at")), (1, "failed", None), out + err)
        self.assertIn(f"{model} is marked deprecated", seat.get("activity", "") + out)
        self.assertEqual(self.ledger_entries(env), [])                       # the admitted entry was released with the refusal

    def test_a_throttled_pinned_continuation_admitted_at_once_still_re_verifies_a_stale_sign_in(self):
        # B2b-1 (c): a throttled continuation is pinned to its account with a fresh verification (B-2); its batch throttle wait (2.4-3.6 s
        # here) outlasts REPICK_WAIT (0.6 s), and with the ramp disabled its next admission is immediate (no hold), so the stale
        # verification was never refreshed: the continuation ran on whatever sign-in account a now holds. A live re-read that finds
        # another sign-in refuses the continuation
        counter = self.tmp / "pin-stale-counter"
        renv, rd = self.relay("pin-stale", {"pick": {"codex": [{"exit": 0, "label": "a"}]},
                                            "verify": {"a": {"email": "someone-else@example.test"}}})
        env = self.cap_env("pin-stale", BRIDGE_CLASS="batch", BRIDGE_RAMP_START="0", STUB_CODEX_RECORD=str(self.tmp / "pin-stale-record.jsonl"),
                           **compressed(**renv))
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "pin-stale", "--",
                                       f"[[STUB:throttle=429,throttleonce={counter}]]", env_extra=env)
        d = self.jobs / "pin-stale"
        seat = self.seat(d, "astra")
        self.assertEqual((rc, seat["state"], seat["account"], seat["throttle_hits"]), (1, "failed", "a", 1), out + err)
        self.assertIn("account a is now signed in as someone-else@example.test, not a@example.test whose thread this is; refusing to run", out)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])   # the continuation never ran
        verifies = [c for c in self.records(rd, "calls.jsonl") if c["argv"][:2] == ["codex", "accounts"] and "--verify" in c["argv"]]
        self.assertEqual(len(verifies), 1)                                               # the one re-verification, before the continuation
        self.assertEqual(self.records(rd, "hits.jsonl"), [])
        self.assertNotIn("hold", [e["event"] for e in self.events(env)])                 # both admissions were immediate
        self.assertEqual(self.ledger_entries(env), [])

    def test_a_routed_seats_wait_budget_follows_a_hold_without_a_timeout(self):
        # B2b-2: the routing budget followed the seat Clock only when --timeout supplied one; the default invocation passed clock=None, so
        # an uncharged hold never extended the routing limit and a hold longer than BRIDGE_ROUTED_MAX_WAIT failed the first busy refusal
        # at once. Hold (PAUSE, 3 s, budget 2 s) -> busy -> success, without --timeout
        home = self.fresh_home("routed-hold-nt")
        renv, rd = self.relay("routed-hold-nt", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "busy": {"a": {"times": 1}}})
        env = self.cap_env("routed-hold-nt", CODEX_HOME=str(home), BRIDGE_CLASS="batch", BRIDGE_ROUTED_MAX_WAIT="2", BRIDGE_HOLD_POLL="0.5", **renv)
        pause = Path(env["BRIDGE_CAPACITY_DIR"]) / "PAUSE"
        pause.write_text("hold on")
        p = self.start_engine(env, "task", "--seats", "astra:low", "--job-dir", "routed-hold-nt", "--", "q")
        d = self.jobs / "routed-hold-nt"
        self.wait_for(d, "astra", lambda s: s.get("state") == "waiting" and s.get("waiting_kind") == "capacity")
        time.sleep(3)
        pause.unlink()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        seat = self.seat(d, "astra")
        self.assertEqual((seat["state"], seat["account"], seat.get("busy_waits"), seat.get("limit_hits", 0)), ("done", "a", 1, 0), seat)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "a"])
        self.assertGreater(seat["uncharged_wait_s"], 2.5)
        self.assertFalse(seat.get("timed_out"))

    def test_an_interactive_seat_demoted_during_an_offline_hold_keeps_waiting_on_the_network(self):
        # B2b-3: ignore_network came from the PREVIOUS decision's class while _capacity_decide recomputed the class under the lock: when
        # other interactive launches filled the interactive budget during an offline hold, the poll after the interactive window demoted
        # the seat to batch and still skipped the network hold, so a batch seat launched with the probe failing. The exception is decided
        # from the class resolved in THIS decision: a budget of 1, filled by another live interactive entry while the seat sleeps through
        # its one interactive poll (BRIDGE_HOLD_POLL 10 s, cut to the 4-s window), so the decision at the window's end is the first to
        # see both the overflow and the elapsed window
        home = self.fresh_home("demote-net")
        env = self.cap_env("demote-net", CODEX_HOME=str(home), BRIDGE_INTERACTIVE_BUDGET="1", BRIDGE_NET_PROBE="127.0.0.1:1",
                           BRIDGE_NETWORK_HOLD_INTERACTIVE="4", BRIDGE_HOLD_POLL="10")
        p = self.start_engine(env, "ask", "--seats", "astra:low", "--job-dir", "demote-net", "--", "q")
        d = self.jobs / "demote-net"
        seat = self.wait_for(d, "astra", lambda s: s.get("state") == "waiting")
        self.assertTrue(seat["activity"].startswith("network down: 127.0.0.1 unreachable"), seat)
        self.assertEqual(seat["class"], "interactive")
        self.add_ledger_entry(env, home, p.pid, "interactive")   # another interactive launch takes the budget during the hold
        time.sleep(8)                                              # well past the 4-s interactive network window
        seat = self.still_held(d, "astra")                         # a seat demoted during the hold must not escape the network hold
        self.assertEqual(seat["class"], "batch", seat)
        self.assertTrue(seat["activity"].startswith("network down: 127.0.0.1 unreachable"), seat)
        rc, out, err = self.run_engine("cancel", "demote-net")
        self.assertEqual(rc, 0, err)
        p.communicate(timeout=60)
        self.assertEqual(self.seat(d, "astra")["state"], "cancelled")
        self.assertIn("capacity-class", [n.get("kind") for n in self.notices(d)])


if __name__ == "__main__":
    unittest.main()

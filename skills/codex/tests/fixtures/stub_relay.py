#!/usr/bin/env python3
"""Stand-in for relay (skills/relay/scripts/relay.py) used by the engine tests. Scripted by $STUB_RELAY_DIR/script.json:

  {"pick": {"codex": [ANSWER, ...], "claude": [ANSWER, ...]},   # consumed in order per vendor; the last answer repeats
   "hit_exit": 0, "transplant_exit": 0,                          # exit codes of `hit` and `transplant` (default 0)
   "blocked_until": <epoch|null>, "hit_text": false,            # `hit --json` prints blocked_until; hit_text makes it print prose (an older relay)
   "refuse": {"<label>": <wake_at epoch>},                      # `exec` under that label exits 1 with relay's one refusal line (relay.py
                                                               #   cmd_exec): `relay: account L cannot take work now: <reason>; wake_at=<int>`,
                                                               #   the reason a ceiling's (REFUSE_REASON). {"wake_at": <epoch|null>, "reason":
                                                               #   "<text>"} names the reason; a null wake_at prints `wake_at=none`
   "busy": {"<label>": {"times": K, "slots": N, "sleep": S,     # `exec` under that label exits 1 with relay's busy line, the account's N slots
            "after": J}},                                       #   ("after": J skips the first J execs under the label: the (J+1)th .. (J+K)th are busy)
                                                               #   all taken (N default 2, relay's per_account_concurrency; 1 prints the
                                                               #   singular): `relay: account L cannot take work now: its 2 slots are all in
                                                               #   use; wake_at=<now+60>`. Only the first K execs under the label are refused
                                                               #   (omit times: every one), counted in $STUB_RELAY_DIR/busy-<label> under the
                                                               #   cursor lock (delete it to count afresh); later execs run. sleep: seconds
                                                               #   relay takes before it refuses. A `refuse` of the same label wins, as in relay
   "exec_fail": {"<label>": {"stderr": "<text>", "exit": 1}},   # `exec` under that label fails before the CLI starts with NO wake_at, as
                                                               #   relay itself can (`could not run codex: ...` after its pre-exec line, a
                                                               #   registry it cannot load): the text on stderr, that exit (default 1).
                                                               #   "times": K fails only the first K execs (counted in fail-<label>, as busy)
   "refuse_touch": "<path>",                                   # a refused `exec` first reads its stdin to the end (the engine feeds the
                                                               #   prompt only after its post-launch cancel check), then creates <path>
                                                               #   (a job's CANCELLED marker: a cancel that lands while relay refuses)
   "accounts": {"<label>": {"email": "...", ...}},             # cached registry: what `accounts` lists WITHOUT --verify (labels named by
   "verify": {"<label>": {"email": "...", "signed_in": false}}, #   pick answers default to <label>@example.test / pro / signed in / fresh)
   "ignore_verify": false, "accounts_exit": 0,                 # verify: what a live `--verify` re-read discovers; ignore_verify models an old relay
   "accounts_sleep": 0,                                        # seconds `accounts` sleeps before answering (cancel-during-verify tests)
   "verified_at_format": "epoch"}                              # or rfc3339z | rfc3339offset | naive-utc: how a fresh verified_at is written
   "exec_sleep": 0,                                            # `exec` waits this long BEFORE exec'ing the CLI (the window where the seat's
                                                               #   child is still relay itself: cancel/kill must reach it there too)
   "homes": {"<label>": "<path>"},                             # a Codex account's CODEX_HOME (default $STUB_RELAY_DIR/homes/<label>, created);
                                                               #   `codex home <label>` prints it, a v2 pick answer carries it as codex_home and a
                                                               #   Codex `exec` runs the command with CODEX_HOME set to it, as relay does
   "only_support": "v2",                                       # how `pick --only` is answered (docs/model-routing.md 3.3): "v2" (default: protocol
                                                               #   2 with only_applied true, the choice confined to the set); "exit2" (an old relay:
                                                               #   argparse usage error, no JSON); "v1" (a relay that ignores the flag and answers
                                                               #   v1); "no_only_applied" (a v2 answer without the field); "false" (only_applied false)
   "no_codex_home": false}                                     # `codex home` is a usage error (an older relay without the command)

  ANSWER  {"exit": 0, "label": "a"}          JSON, result ok (email <label>@example.test, plan pro)
          {"exit": 1, "wake_at": <epoch>}    JSON, result none, with wake_at (omit wake_at to answer without one; "result": "busy"
                                             answers relay's busy verdict instead: every eligible account's slots are taken;
                                             "rows": [{"label", "reason", "wake_at"}] gives relay's verdict on each account, and
                                             "reason" replaces the top-level reason, which relay itself leaves null for none)
          {"exit": 2}                        an old relay without `pick`: argparse usage error on stderr, no JSON
          {"exit": 3}                        JSON, result no_accounts
          {"exit": 0, "raw": "<text>"}       print <text> verbatim (a non-JSON answer)
          {"exit": 0, "json": {...}}         print that object verbatim (e.g. another protocol version)
          any answer may add "sleep": N      sleep N seconds before answering (helper-timeout tests)
          any answer may add "trap": true    ignore SIGTERM and SIGINT while sleeping (only SIGKILL ends it)

  A legacy request (no --only, no --protocol 2) is answered exactly as before (v1). With --only or --protocol 2 (and
  only_support v2) the same scripted answer is given in protocol 2; an ok answer whose label is not in the set names the
  set's first label instead (relay never routes outside the set), recorded in coercions.jsonl.

Records in $STUB_RELAY_DIR: calls.jsonl (every invocation, {argv, cwd, t}), hits.jsonl, execs.jsonl ({argv, command, label, t}),
transplants.jsonl, coercions.jsonl ({coerced_label, scripted, only}); `t` is the epoch time the call began. `exec` execs the command
after `--` with RELAY_STUB_ACCOUNT=<label> in its environment, exactly as relay execs into the CLI (same pid, same group)."""
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

DIR = os.environ.get("STUB_RELAY_DIR") or "/nonexistent"
REFUSE_REASON = "weekly window at 99% (worker ceiling 99%)"  # a ceiling, in relay's words (skills/relay/tests/test_relay.py)


def stamp(ts, form):
    """A verified_at in the form the script asks for — the forms of the bridge<->relay timestamp contract: epoch seconds
    (default), RFC3339 ending in Z, RFC3339 with an explicit -07:00 offset, or a naive UTC ISO string."""
    if form == "rfc3339z":
        return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
    if form == "rfc3339offset":
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=-7))).isoformat()
    if form == "naive-utc":
        return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None).isoformat()
    return ts


def record(name, obj):
    with open(os.path.join(DIR, name), "a") as f:
        f.write(json.dumps(obj) + "\n")


def script():
    try:
        with open(os.path.join(DIR, "script.json")) as f:
            return json.load(f)
    except OSError:
        return {}


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def home_of(label):
    """The CODEX_HOME of a Codex account (script `homes`, else $STUB_RELAY_DIR/homes/<label>), created so a seat's rollout file can land there."""
    path = (script().get("homes") or {}).get(label) or os.path.join(DIR, "homes", label)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def next_answer(vendor):
    """The next scripted pick answer for a vendor; a cursor file under a lock keeps parallel seats in order."""
    answers = (script().get("pick") or {}).get(vendor) or [{"exit": 3}]
    with open(os.path.join(DIR, ".cursor.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cursor = os.path.join(DIR, f"cursor-{vendor}")
        try:
            n = int(open(cursor).read().strip() or 0)
        except OSError:
            n = 0
        with open(cursor, "w") as f:
            f.write(str(n + 1))
    return answers[min(n, len(answers) - 1)]


def take(kind, label, times, after=0):
    """Whether this `exec` under `label` is one of the `times` a scripted mode answers (None: every one), skipping the first `after`
    execs under the label. The count of execs lives in $STUB_RELAY_DIR/<kind>-<label>, under the cursor lock, so parallel seats on
    one account are counted in order."""
    if times is None and not after:
        return True
    with open(os.path.join(DIR, ".cursor.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = os.path.join(DIR, f"{kind}-{label}")
        try:
            n = int(open(path).read().strip() or 0)
        except OSError:
            n = 0
        with open(path, "w") as f:
            f.write(str(n + 1))
    return n >= int(after) and (times is None or n < int(after) + int(times))


def main():
    argv = sys.argv[1:]
    record("calls.jsonl", {"argv": argv, "cwd": os.getcwd(), "t": time.time()})
    if argv[:1] == ["--version"]:
        print("relay 1.4.0-stub")
        return 0
    cmd = argv[0] if argv else ""
    if argv[:2] == ["codex", "home"]:
        if script().get("no_codex_home"):  # an older relay without the command
            sys.stderr.write("usage: relay codex [-h] {add,login,accounts,remove,usage,route,ping} ...\nrelay codex: error: argument codex_command: invalid choice: 'home'\n")
            return 2
        label = argv[2] if len(argv) > 2 else ""
        print(home_of(label))
        return 0
    if argv[:2] == ["codex", "accounts"] or cmd == "accounts":
        s = script()
        code = int(s.get("accounts_exit", 0))
        if code:  # an older relay without the subcommand: a usage error, no listing
            sys.stderr.write("usage: relay [-h] command ...\nrelay: error: unrecognized arguments: --verify\n")
            return code
        verify = "--verify" in argv and not s.get("ignore_verify")
        if s.get("accounts_sleep"):
            time.sleep(float(s["accounts_sleep"]))
        now = time.time()
        listed = {}
        for ans in (s.get("pick") or {}).get("codex") or []:
            if ans.get("label"):
                listed[ans["label"]] = {"email": f"{ans['label']}@example.test", "plan": "pro", "signed_in": True}
        for label, rec in (s.get("accounts") or {}).items():  # cached registry values (seen with or without --verify)
            listed.setdefault(label, {}).update(rec)
        codex_answers = (s.get("pick") or {}).get("codex") or []
        if not listed and codex_answers and all(int(a.get("exit", 0)) != 3 for a in codex_answers):
            listed["a"] = {"email": "a@example.test", "plan": "pro", "signed_in": True}  # accounts that are all out are still registered
        if verify:  # a live re-read discovers the current sign-in
            for label, rec in (s.get("verify") or {}).items():
                listed.setdefault(label, {}).update(rec)
        out = []
        for label, rec in listed.items():
            rec = dict(rec)
            rec.setdefault("email", f"{label}@example.test")
            rec.setdefault("plan", "pro")
            rec.setdefault("signed_in", True)
            # verified_at is fresh only when this reading actually re-read the live sign-in (--verify); otherwise it is cached/stale
            if "verified_at" not in rec:
                rec["verified_at"] = stamp(now if (verify and rec["signed_in"]) else now - 100000, s.get("verified_at_format"))
            out.append(dict(label=label, **rec))
        print(json.dumps({"accounts": out}))
        return 0
    if cmd == "pick":
        vendor, model = option(argv, "--vendor"), option(argv, "--model")
        only_text, protocol = option(argv, "--only"), option(argv, "--protocol")
        constrained = only_text is not None or protocol == "2"
        support = str(script().get("only_support") or "v2")
        if constrained and support == "exit2":  # an old relay: the flag is unknown
            sys.stderr.write("usage: relay pick [-h] --vendor VENDOR [--model MODEL] [--json]\nrelay pick: error: unrecognized arguments: --only\n")
            return 2
        only = [l.strip() for l in only_text.split(",") if l.strip()] if only_text is not None else None
        ans = next_answer(vendor)
        if ans.get("trap"):  # a helper that refuses to die politely: SIGTERM must be escalated to SIGKILL
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, signal.SIG_IGN)
        if ans.get("sleep"):
            time.sleep(float(ans["sleep"]))
        code = int(ans.get("exit", 0))
        if "raw" in ans:
            sys.stdout.write(ans["raw"])
            return code
        if "json" in ans:
            print(json.dumps(ans["json"]))
            return code
        if code == 2:
            sys.stderr.write("usage: relay [-h] command ...\nrelay: error: argument command: invalid choice: 'pick'\n")
            return 2
        base = {"v": 1, "vendor": vendor, "model": model, "label": None, "email": None, "plan": None, "reason": None, "wake_at": None, "rows": []}
        if code == 0:
            label = ans["label"]
            if only and label not in only:  # relay never routes outside the set
                label = only[0]
                record("coercions.jsonl", {"coerced_label": label, "scripted": ans["label"], "only": only})
            base.update(result="ok", label=label, email=f"{label}@example.test", plan="pro", reason="earliest weekly reset")
        elif code == 1:
            base.update(result=ans.get("result", "none"), reason=ans.get("reason", "every account is blocked or over its ceiling"),
                        wake_at=ans.get("wake_at"), rows=ans.get("rows", []))
        else:
            base.update(result="no_accounts", reason=f"no {vendor} accounts are registered")
        if constrained and support != "v1":
            out = {"v": 2, "capabilities": ["only"], "only_applied": only is not None, "only": only}
            if support == "no_only_applied":
                out.pop("only_applied")
            elif support == "false":
                out["only_applied"] = False
            out.update(base)
            out["v"] = 2
            out["codex_home"] = home_of(base["label"]) if (vendor == "codex" and base["result"] == "ok" and base["label"]) else None
            print(json.dumps(out))
            return code
        print(json.dumps(base))
        return code
    if cmd == "hit":
        s = script()
        record("hits.jsonl", {"label": argv[1], "vendor": option(argv, "--vendor"), "model": option(argv, "--model")})
        code = int(s.get("hit_exit", 0))
        if s.get("hit_text"):  # an older relay: human-readable prose, no JSON — the bridge must not choke on it
            sys.stdout.write("relay: blocked %s; it resets later today\n" % argv[1])
            return code
        if "--json" in argv:
            print(json.dumps({"v": 1, "vendor": option(argv, "--vendor"), "label": argv[1], "blocked_until": s.get("blocked_until"),
                              "window": s.get("hit_window", "seven_day"), "reason": "usage limit reported"}))
        return code
    if cmd == "exec":
        if "--" not in argv:
            sys.stderr.write("relay: give a command after --\n")
            return 2
        label = option(argv, "--account")
        command = argv[argv.index("--") + 1:]
        record("execs.jsonl", {"argv": argv[:argv.index("--")], "command": command, "label": label, "t": time.time()})
        failure = (script().get("exec_fail") or {}).get(label)
        if failure is not None and take("fail", label, failure.get("times")):  # relay's own failure before it becomes the CLI: the command never runs
            sys.stderr.write(failure.get("stderr") or "relay: exec failed\n")
            return int(failure.get("exit", 1))
        refusal = (script().get("refuse") or {}).get(label)
        if refusal is not None:
            touch = script().get("refuse_touch")
            if touch:  # the engine has published this child and is feeding it the prompt: the file lands inside the attempt
                sys.stdin.read()
                open(touch, "a").close()
            wake, reason = (refusal.get("wake_at"), refusal.get("reason") or REFUSE_REASON) if isinstance(refusal, dict) else (refusal, REFUSE_REASON)
            sys.stderr.write("relay: account %s cannot take work now: %s; wake_at=%s\n" % (label, reason, int(wake) if wake else "none"))
            return 1
        busy = (script().get("busy") or {}).get(label)
        if busy is not None and take("busy", label, busy.get("times"), busy.get("after") or 0):  # every slot of the account is taken: relay refuses before the CLI
            if busy.get("sleep"):
                time.sleep(float(busy["sleep"]))
            cap = max(1, int(busy.get("slots", 2)))
            why = "its %d slot%s in use" % (cap, " is" if cap == 1 else "s are all")  # relay.py cmd_exec's words for a busy verdict
            sys.stderr.write("relay: account %s cannot take work now: %s; wake_at=%s\n" % (label, why, int(time.time() + 60)))
            return 1
        env = dict(os.environ, RELAY_STUB_ACCOUNT=label or "")
        if option(argv, "--vendor") == "codex" and label:  # relay points a Codex command at the account's own home
            env["CODEX_HOME"] = home_of(label)
        sys.stderr.write(f"[relay] running under account {label}\n")
        sys.stderr.flush()
        if script().get("exec_sleep"):  # relay still working (slots, ledger) before it becomes the CLI
            time.sleep(float(script()["exec_sleep"]))
        os.execvpe(command[0], command, env)
    if cmd == "transplant":
        record("transplants.jsonl", {"session": argv[1], "to": option(argv, "--to"), "cwd": option(argv, "--cwd")})
        return int(script().get("transplant_exit", 0))
    sys.stderr.write(f"usage: relay [-h] command ...\nrelay: error: argument command: invalid choice: {cmd!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())

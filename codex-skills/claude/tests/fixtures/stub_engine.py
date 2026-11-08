#!/usr/bin/env python3
"""A deterministic engine implementing the MCP subprocess contract."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


JOB = "20260906-123456-ask-abcde"
ENV_KEYS = (
    "BRIDGE_CALLER", "BRIDGE_DEFAULT_SEAT", "BRIDGE_REVIEW_SEATS",
    "BRIDGE_COUNCIL_SEATS", "BRIDGE_JOBS", "BRIDGE_ROOT", "CLAUDE_BIN", "CODEX_BIN",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "MCP_PASSTHROUGH_SENTINEL",
    # S-DEF: the entry points' built-in seat lists, and the provenance variable that ruling removes
    "BRIDGE_BUILTIN_DEFAULT_SEAT", "BRIDGE_BUILTIN_REVIEW_SEATS", "BRIDGE_BUILTIN_COUNCIL_SEATS",
    "BRIDGE_DEFAULTS_SUPPLIED",
)


def pid_start(pid):
    if os.environ.get("MCP_STUB_IDENTITIES"):
        root = Path(os.environ["MCP_STUB_IDENTITIES"])
        root.mkdir(parents=True, exist_ok=True)
        path = root / str(pid)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(time.asctime())
        except FileExistsError:
            pass
        # The parent can request this identity as soon as Popen returns.
        deadline = time.monotonic() + 1
        while not path.read_text(encoding="utf-8"):
            if time.monotonic() >= deadline:
                raise RuntimeError("fixture process identity was not published")
            time.sleep(0.001)
        return path.read_text(encoding="utf-8")
    return subprocess.check_output(
        ["ps", "-o", "lstart=", "-p", str(pid)], text=True,
    ).strip()


def main():
    argv = sys.argv[1:]
    mode = argv[0]
    stdin = ""
    if mode in ("ask", "council", "task", "reply"):
        if "--" in argv and argv.index("--") + 1 < len(argv):
            prompt = argv[argv.index("--") + 1]
        else:
            stdin = sys.stdin.read()
            prompt = stdin
    elif mode == "review" and "--intent-stdin" in argv:
        stdin = sys.stdin.read()
        prompt = json.loads(stdin).get("intent", "")
    else:
        prompt = ""

    jobs = Path(os.environ["BRIDGE_JOBS"])
    jobs.mkdir(parents=True, exist_ok=True)
    job = argv[1] if mode in ("reply", "status", "result") else os.environ.get("MCP_STUB_JOB", JOB)
    if job == "last":
        job = sorted(path.name for path in jobs.iterdir() if path.is_dir())[-1]
    job_dir = jobs / job
    job_dir.mkdir(exist_ok=True)
    metadata = {"cwd": os.getcwd(), "state": "running", "mode": mode}
    meta_delay = float(os.environ.get("MCP_STUB_META_DELAY", "0"))
    if mode not in ("status", "result"):
        initial = {key: value for key, value in metadata.items() if key != "cwd"} if meta_delay else metadata
        (job_dir / "meta.json").write_text(json.dumps(initial), encoding="utf-8")

    child = seat = None
    seat_child_pid = seat_start = seat_child_start = None
    if prompt.startswith("group-sleep:"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        def reap_child(signum, frame):
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, reap_child)
    elif prompt.startswith("ignore-term-sleep:"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif prompt.startswith(("seat-sleep:", "seat-exit:", "seat-hard-exit:", "leaderless-exit:")):
        ready = job_dir / ("ready-%d.json" % os.getpid())
        leaderless = prompt.startswith("leaderless-exit:")
        seat = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("stub_seat.py")), str(ready),
             *(["--leaderless"] if leaderless else [])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        ready_data = json.loads(ready.read_text(encoding="utf-8"))
        seat_child_pid = ready_data["child_pid"]
        seat_start, seat_child_start = ready_data["pid_start"], ready_data["child_start"]
        if leaderless:
            seat.wait(timeout=3)
        seat_record = {"pid": seat.pid, "pid_start": seat_start, "pgid": seat.pid, "state": "running",
                       "supervisor_pid": os.getpid(), "supervisor_start": pid_start(os.getpid())}
        identity = os.environ.get("MCP_STUB_SEAT_IDENTITY", "matching")
        if identity == "missing":
            seat_record.pop("pid_start")
        elif identity == "stale":
            seat_record["pid_start"] = time.asctime(time.localtime(time.time() - 86400))
        supervisor_identity = os.environ.get("MCP_STUB_SUPERVISOR_IDENTITY", "matching")
        if supervisor_identity == "missing":
            seat_record.pop("supervisor_pid")
            seat_record.pop("supervisor_start")
        elif supervisor_identity == "stale":
            seat_record["supervisor_start"] = time.asctime(time.localtime(time.time() - 86400))
        elif supervisor_identity == "other":
            seat_record["supervisor_pid"] = os.getppid()
        (job_dir / ("seat-fixture-%d.json" % os.getpid())).write_text(json.dumps(seat_record), encoding="utf-8")

    # Announce only newly started jobs. Polling commands do not create jobs.
    if mode not in ("status", "result"):
        print("[bridge] job %s %s" % (job, job_dir.resolve()), file=sys.stderr, flush=True)
    record = {
        "pid": os.getpid(), "pid_start": pid_start(os.getpid()),
        "argv": argv, "stdin": stdin, "cwd": os.getcwd(),
        "env": {key: os.environ.get(key) for key in ENV_KEYS},
        "child_pid": child.pid if child else None,
        "seat_pid": seat.pid if seat else None,
        "seat_child_pid": seat_child_pid, "job": job, "job_dir": str(job_dir),
        "exit_release": str(job_dir / ("exit-%d.release" % os.getpid())),
    }
    for prefix in ("child", "seat", "seat_child"):
        pid = record[prefix + "_pid"]
        saved_start = {"seat": seat_start, "seat_child": seat_child_start}.get(prefix)
        record[prefix + "_start"] = saved_start or (pid_start(pid) if pid else None)
    with open(os.environ["MCP_STUB_RECORD"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")

    if mode == "status":
        saved = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
        print("%s mode=%s state=%s cwd=%s" % (job, saved["mode"], saved["state"], saved.get("cwd", "")))
        return 0
    if mode == "result":
        saved = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
        result = job_dir / "result.txt"
        print(result.read_text(encoding="utf-8") if result.exists() else "(no output yet)")
        if os.environ.get("MCP_STUB_RESULT_HIDE_STATE") != "1":
            print("— job: %s · state: %s · logs: %s" % (job, saved["state"], job_dir))
        default_code = {"done": 0, "partial": 3, "running": 3, "failed": 1, "cancelled": 1}[saved["state"]]
        return int(os.environ.get("MCP_STUB_RESULT_EXIT", default_code))
    if meta_delay:
        time.sleep(meta_delay)
        (job_dir / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    if prompt.startswith(("seat-exit:", "seat-hard-exit:", "leaderless-exit:")):
        deadline = time.monotonic() + 15
        while not Path(record["exit_release"]).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture exit was not released")
            time.sleep(0.005)
        code = int(prompt.rsplit(":", 1)[1])
        if prompt.startswith(("seat-hard-exit:", "leaderless-exit:")):
            os._exit(code)
        return code
    if prompt.startswith(("sleep:", "group-sleep:", "ignore-term-sleep:", "seat-sleep:")):
        time.sleep(float(prompt.rsplit(":", 1)[1]))
    if prompt == "signal-self":
        os.kill(os.getpid(), signal.SIGTERM)

    stderr = os.environ.get("MCP_STUB_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)
    length = int(os.environ.get("MCP_STUB_STDOUT_LENGTH", "0"))
    output = "X" * length if length else "stub result: " + prompt
    print(output)
    print("— job: " + job)
    code = int(prompt.split(":", 1)[1]) if prompt.startswith("exit:") else int(os.environ.get("MCP_STUB_EXIT", "0"))
    (job_dir / "result.txt").write_text(output, encoding="utf-8")
    metadata["state"] = {0: "done", 3: "partial"}.get(code, "failed")
    (job_dir / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Stand-in for a running `codex_task.py run` holding the computer-use priority lease (build spec 4.1), for the engine tests.

usage: lease_holder.py <lease dir> <task dir> [--unlocked]

Creates holder-<host>-<pid>-<hex>.json in <lease dir> the way the runner does: under a temp name, locked LOCK_EX before it is
visible, the display JSON written and fsynced, then renamed to its final name. Prints the final path on stdout, then sleeps
until killed (the kernel drops the lock with the process). `--unlocked` writes the same file without taking the lock and exits:
what a holder file left behind by a dead runner looks like to a reader."""
import fcntl
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone


def main():
    lease_dir, task_dir = sys.argv[1], sys.argv[2]
    unlocked = "--unlocked" in sys.argv[3:]
    os.makedirs(lease_dir, mode=0o700, exist_ok=True)
    host = socket.gethostname().split(".")[0]
    final = os.path.join(lease_dir, f"holder-{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
    tmp = final + ".tmp"
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    if not unlocked:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    body = {"v": 1, "kind": "process", "host": host, "pid": os.getpid(), "started": "stub lstart", "task_dir": task_dir,
            "since": datetime.now(timezone.utc).isoformat(), "marker": "codex_task.py run"}
    os.write(fd, json.dumps(body).encode())
    os.fsync(fd)
    os.rename(tmp, final)
    sys.stdout.write(final + "\n")
    sys.stdout.flush()
    if unlocked:
        os.close(fd)
        return 0
    while True:
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())

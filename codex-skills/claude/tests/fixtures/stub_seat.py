#!/usr/bin/env python3
"""A separately sessioned fixture seat with a child in its process group."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from stub_engine import pid_start

start = pid_start(os.getpid())
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
child_start = pid_start(child.pid)


def terminate(signum, frame):
    # A group signal also stops the child; signalling only the seat leaves it
    # alive so the test can detect incomplete process-group cleanup.
    try:
        child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, terminate)
ready = Path(sys.argv[1])
temporary = ready.with_suffix(".tmp")
temporary.write_text(json.dumps({"child_pid": child.pid, "pid_start": start, "child_start": child_start}),
                     encoding="utf-8")
temporary.replace(ready)
if "--leaderless" in sys.argv:
    os._exit(0)
time.sleep(60)

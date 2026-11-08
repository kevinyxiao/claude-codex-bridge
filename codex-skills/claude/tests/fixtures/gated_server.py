#!/usr/bin/env python3
"""Run the real server with deterministic barriers around started-response writes."""

import errno
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time


path = Path(__file__).resolve().parents[2] / "scripts" / "claude_mcp_server.py"
spec = importlib.util.spec_from_file_location("gated_mcp_server", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_write = os.write
writing = threading.local()


def controlled_write(fd, payload):
    current = getattr(writing, "current", None)
    if current is None or fd != current[0].output_fd:
        return real_write(fd, payload)
    server, gate, state = current
    if not state["partial"]:
        written = real_write(fd, payload[:13])
        state["partial"] = True
        (gate / "ready").touch()
        return written
    if not (gate / "release").exists() and not server.stopped.is_set():
        # Return immediately: the real writer can hold the server lock here.
        raise BlockingIOError(errno.EAGAIN, "fixture keeps response partially written")
    return real_write(fd, payload)


class GatedServer(module.Server):
    def write_response(self, payload, call, *delivery):
        result = json.loads(payload).get("result", {})
        if not isinstance(result, dict) or result.get("structuredContent", {}).get("status") != "started":
            return super().write_response(payload, call, *delivery)
        gate = Path(os.environ["MCP_STUB_DELIVERY_GATE"])
        if os.environ.get("MCP_STUB_DELIVERY_MODE") == "partial":
            writing.current = (self, gate, {"partial": False})
            try:
                return super().write_response(payload, call, *delivery)
            finally:
                writing.current = None
        (gate / "ready").touch()
        deadline = time.monotonic() + 15
        while not (gate / "release").exists() and not self.stopped.is_set() and not call.cancelled.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError("started-response fixture gate was not released")
            time.sleep(0.005)
        return super().write_response(payload, call, *delivery)


module.os.write = controlled_write
module.Server = GatedServer
if __name__ == "__main__":
    sys.exit(module.main())

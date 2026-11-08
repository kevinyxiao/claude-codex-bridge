"""A loopback HTTP server that stands in for the three free pages the models check reads (DESIGN §5): the npm registry
document for @openai/codex and Anthropic's two Markdown model pages. Tests point the engine at it with
BRIDGE_MODELS_NPM_URL / BRIDGE_MODELS_DEPRECATIONS_URL / BRIDGE_MODELS_OVERVIEW_URL, so no test ever reaches the network.

Every request is recorded (path and time). A route can answer with any status, a path can be held open until the test
releases it (a writer paused in the middle of its fetch), and a path can trickle its body a byte at a time (a response that
never pauses long enough to trip a socket timeout). A path can also deliver its header section, or the framing of its
chunked body, a byte at a time (Astra C3b-1): the client is then blocked inside urlopen() or inside one read of the body.
The chunked form can also be a persistent response (no `Connection: close`, Astra C4b-1). With `tls=(certificate, key)` the
server speaks HTTPS with that certificate (Astra C4b-2; the tests generate it with the openssl CLI)."""
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent / "evidence"
# Anthropic's cached pages of 2026-10-08 list every current Claude head (P-16: the new Haiku head).
# Earlier captures remain available for the historical checks that name them.
DEFAULT_PAGES = {"npm": EVIDENCE / "npm_dist_tags.json",
                 "deprecations": EVIDENCE / "anthropic_model-deprecations_20261008.md",
                 "overview": EVIDENCE / "anthropic_models-overview_20261008.md"}
URL_VARS = {"npm": "BRIDGE_MODELS_NPM_URL", "deprecations": "BRIDGE_MODELS_DEPRECATIONS_URL", "overview": "BRIDGE_MODELS_OVERVIEW_URL"}


class PageServer:
    def __init__(self, tls=None):
        self.routes = {}
        self.requests = []
        self.gates = {}
        self.trickles = {}
        self.drips = {}
        self.closing = threading.Event()
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with server.lock:
                    server.requests.append((self.path, time.time()))
                    route = server.routes.get(self.path)
                    gate = server.gates.get(self.path)
                    trickle = server.trickles.get(self.path)
                    drip = server.drips.get(self.path)
                if gate is not None:
                    gate.wait(120)
                    with server.lock:
                        route = server.routes.get(self.path)
                if drip is not None:
                    self.drip(*drip)
                    return
                if trickle is not None:
                    self.trickle(*trickle)
                    return
                status, body, ctype = route if route else (404, b"not found", "text/plain")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:  # the client stopped reading (a fetch that gives up on a page too large)
                    return

            def trickle(self, body, interval, ctype):
                """Status 200 and the whole Content-Length at once, then the body one byte every `interval` seconds, until it
                is sent, the client goes away or the server closes."""
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for i in range(len(body)):
                    if server.closing.is_set():
                        return
                    try:
                        self.wfile.write(body[i:i + 1])
                        self.wfile.flush()
                    except OSError:
                        return
                    server.closing.wait(interval)

            def drip(self, start, slow, rest, interval):
                """A raw response: `start` at once, then `slow` one byte every `interval` seconds, then `rest` at once, until it
                is sent, the client goes away or the server closes."""
                try:
                    self.wfile.write(start)
                    for i in range(len(slow)):
                        if server.closing.is_set():
                            return
                        self.wfile.write(slow[i:i + 1])
                        server.closing.wait(interval)
                    self.wfile.write(rest)
                except OSError:
                    return

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.scheme = "http"
        if tls:  # the handshake runs when a connection is accepted; a client that rejects the certificate is simply dropped
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return "%s://127.0.0.1:%d%s" % (self.scheme, self.port, path)

    def set(self, path, body, status=200, ctype=None):
        if isinstance(body, Path):
            body = body.read_bytes()
        if isinstance(body, str):
            body = body.encode("utf-8")
        ctype = ctype or ("application/json" if path.endswith("npm") else "text/markdown; charset=utf-8")
        with self.lock:
            self.routes[path] = (status, body, ctype)

    def pages(self, prefix, npm=None, deprecations=None, overview=None, status=None):
        """Serve one set of the three pages under /<prefix>/ and return the engine environment that selects them. A page
        given as an int is served as that HTTP status (a failed fetch); None serves the default capture."""
        env = {}
        for key, value in (("npm", npm), ("deprecations", deprecations), ("overview", overview)):
            path = "/%s/%s" % (prefix, key)
            if isinstance(value, int):
                self.set(path, "unavailable", status=value, ctype="text/plain")
            else:
                self.set(path, DEFAULT_PAGES[key] if value is None else value, status=(status or {}).get(key, 200))
            env[URL_VARS[key]] = self.url(path)
        return env

    def trickle(self, path, body, interval=0.25, ctype=None):
        """Answer `path` with its full headers at once and then `body` one byte every `interval` seconds: each read of the
        response returns within the interval, so only a total deadline across the body ends the fetch early."""
        if isinstance(body, Path):
            body = body.read_bytes()
        if isinstance(body, str):
            body = body.encode("utf-8")
        ctype = ctype or ("application/json" if path.endswith("npm") else "text/markdown; charset=utf-8")
        with self.lock:
            self.trickles[path] = (body, interval, ctype)

    def slow_headers(self, path, body, interval=0.05, seconds=25, ctype=None):
        """Answer `path` with its status line and headers at once except the last header, which arrives one byte every
        `interval` seconds for about `seconds`, then the end of the headers and `body` at once: urlopen() is reading headers
        all that time, and each of its reads returns within the interval, so no socket timeout ends it (Astra C3b-1)."""
        body, ctype = _encoded(path, body, ctype)
        start = (b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode("ascii")
                 + b"\r\nContent-Length: %d\r\nConnection: close\r\n" % len(body))
        with self.lock:
            self.drips[path] = (start, b"X-Slow: " + _padding(seconds, interval, 10) + b"\r\n", b"\r\n" + body, interval)

    def slow_chunks(self, path, body, interval=0.05, seconds=25, ctype=None, keep_alive=False):
        """Answer `path` with complete headers at once (a chunked body), then the size line of its one chunk one byte every
        `interval` seconds for about `seconds` (a chunk extension pads it), then the chunk and the last chunk at once: the
        first read of the body is reading chunk framing all that time (Astra C3b-1). `keep_alive` leaves out `Connection:
        close`, so the client keeps the HTTP/1.1 response as a persistent one (Astra C4b-1)."""
        body, ctype = _encoded(path, body, ctype)
        start = (b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode("ascii")
                 + b"\r\nTransfer-Encoding: chunked\r\n" + (b"" if keep_alive else b"Connection: close\r\n") + b"\r\n")
        size = b"%x;x=" % len(body)
        with self.lock:
            self.drips[path] = (start, size + _padding(seconds, interval, len(size) + 2) + b"\r\n", body + b"\r\n0\r\n\r\n", interval)

    def requested_at(self, path):
        """The times (epoch seconds) at which requests for exactly `path` arrived."""
        with self.lock:
            return [t for p, t in self.requests if p == path]

    def hold(self, path):
        """Hold every request for `path` open until release(path)."""
        with self.lock:
            self.gates[path] = threading.Event()

    def release(self, path=None):
        with self.lock:
            gates = [self.gates.pop(path)] if path else list(self.gates.values())
            if not path:
                self.gates.clear()
        for gate in gates:
            gate.set()

    def calls(self, prefix=""):
        with self.lock:
            return [p for p, _ in self.requests if p.startswith(prefix)]

    def close(self):
        self.closing.set()
        self.release()
        self.httpd.shutdown()
        self.httpd.server_close()


def _encoded(path, body, ctype):
    """(body as bytes, content type): a page's text or file, typed the way set() types it."""
    if isinstance(body, Path):
        body = body.read_bytes()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return body, ctype or ("application/json" if path.endswith("npm") else "text/markdown; charset=utf-8")


def _padding(seconds, interval, fixed):
    """Filler that makes a paced line of `fixed` other bytes last about `seconds` at one byte every `interval` seconds."""
    return b"x" * max(1, int(seconds / interval) - fixed)


def unreachable_pages():
    """URLs that fail at once (a closed loopback port): a check with these reads nothing and reports the pages UNVERIFIED."""
    return {var: "http://127.0.0.1:9/%s" % key for key, var in URL_VARS.items()}

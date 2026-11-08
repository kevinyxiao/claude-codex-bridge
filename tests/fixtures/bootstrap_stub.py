#!/usr/bin/env python3
"""Offline CLI fixtures for test_bootstrap.sh; never import production code.

The `file` fixture recognizes only paths listed explicitly in TEST_NATIVE_LIST.
This lets executable CLI scripts stand in for native binaries without adding a
validation bypass to bootstrap.sh. Every call log lives outside the fake HOME.
"""

import ast
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


def record(tool):
    names = (
        "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID",
        "CLAUDE_EFFORT",
    )
    with open(os.environ["TEST_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "tool": tool, "args": sys.argv[1:],
            "path": os.path.realpath(sys.argv[0]),
            "env": {name: os.environ[name] for name in names if name in os.environ},
        }) + "\n")
    if tool in ("codex", "claude") and os.environ.get("TEST_CACHE_ON_INVOKE") == "1":
        (Path.home() / (".fixture-cli-cache-" + tool)).write_text("CLI probe wrote a cache\n")
    if os.environ.get("TEST_SLEEP_COMMAND") == " ".join([tool] + sys.argv[1:]):
        # Ignore TERM in both descendants: the supervisor must escalate and
        # reap its direct child even when only bootstrap's Bash PID is signalled.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)"])
        Path(os.environ["TEST_LOG"] + ".sleep-pids").write_text(
            json.dumps([os.getppid(), os.getpid(), child.pid]))
        time.sleep(90)


def registration(text):
    # These controlled fixtures use plain triple-quoted strings. Mask their
    # contents independently of the production editor, retaining file offsets.
    syntax = re.sub(r'""".*?"""|\x27\x27\x27.*?\x27\x27\x27',
                    lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)
    header = re.search(r"(?m)^\s*\[mcp_servers\.claude-bridge\][ \t]*(?:#.*)?$", syntax)
    if not header:
        return None
    following = re.search(r"(?m)^[ \t]*\[", syntax[header.end():])
    end = header.end() + following.start() if following else len(text)
    values = {}
    for line in syntax[header.end():end].splitlines():
        match = re.match(r"\s*(command|args|enabled)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match[1]] = (match[2] == "true" if match[1] == "enabled"
                               else ast.literal_eval(match[2]))
    return header.start(), end, values


def codex_version():
    """The version this fixture executable reports (DESIGN §3 floors): TEST_CODEX_VERSIONS maps a realpath to a version
    (several fixtures on one PATH); the default meets every Codex floor of the model table (the Sol and Luna heads >= 0.156.1)."""
    versions = json.loads(os.environ.get("TEST_CODEX_VERSIONS") or "{}")
    return versions.get(os.path.realpath(sys.argv[0]), os.environ.get("TEST_CODEX_VERSION") or "0.156.1")


def codex(args):
    record("codex")
    if args == ["--version"]:
        if os.environ.get("TEST_HANG_VERSION") == os.path.realpath(sys.argv[0]):
            time.sleep(60)
            Path(os.environ["TEST_LOG"] + ".late-version").write_text("escaped timeout")
        if os.environ.get("TEST_VERSION_FAIL") == os.path.realpath(sys.argv[0]):
            return 7
        print("codex-cli " + codex_version() + " (bootstrap fixture)")
        return 0
    if args == ["exec", "--help"]:
        if os.environ.get("TEST_HANG_HELP") == os.path.realpath(sys.argv[0]):
            time.sleep(60)
            Path(os.environ["TEST_LOG"] + ".late-help").write_text("escaped timeout")
        if os.environ.get("TEST_HELP_FAIL") == os.path.realpath(sys.argv[0]):
            return 7
        flags = ("--json", "--output-schema", "--output-last-message", "--ignore-user-config")
        print("Usage: codex exec [OPTIONS]\n" + "\n".join(
            flag for flag in flags if flag != os.environ.get("TEST_MISSING_CAPABILITY")))
        return 0
    if args == ["login", "status"]:
        stream = sys.stdout if os.environ.get("TEST_LOGIN_STDOUT") == "1" else sys.stderr
        if os.environ.get("TEST_STDERR_WARNING") == "1":
            print("warning: optional terminal integration unavailable", file=stream)
        signed_out = os.environ.get("TEST_SIGNED_OUT") == "1"
        print("Not logged in" if signed_out else "Logged in using ChatGPT", file=stream)
        if os.environ.get("TEST_LOGIN_CONTRADICTION") == "1":
            print("Not logged in", file=sys.stderr if stream is sys.stdout else sys.stdout)
        # Deliberately return 0 even when signed out: the text matters as well.
        return int(os.environ.get("TEST_LOGIN_STATUS", "0"))
    config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if args[:3] == ["mcp", "get", "claude-bridge"]:
        if "--json" in args and os.environ.get("TEST_NO_JSON") == "1":
            print("error: unexpected argument '--json'", file=sys.stderr)
            return 2
        text = config.read_text() if config.exists() else ""
        found = registration(text)
        if not found:
            print("No MCP server named 'claude-bridge' found.", file=sys.stderr)
            return 1
        if os.environ.get("TEST_FAIL_VALIDATION") == "1" and "tool_timeout_sec = 3600" in text:
            if os.environ.get("TEST_INTERVENING_EDIT") == "1":
                config.write_text(text + "\n# concurrent writer preserved\n")
            return 9
        values = found[2]
        enabled = values.pop("enabled", True)
        if os.environ.get("TEST_STDERR_WARNING") == "1":
            print("warning: optional terminal integration unavailable", file=sys.stderr)
        if "--json" in args:
            print(json.dumps({"name": "claude-bridge", "enabled": enabled,
                              "fixture_padding": "x" * int(os.environ.get("TEST_JSON_SIZE", "0")),
                              "transport": {"type": "stdio", **values}}))
        else:
            print("claude-bridge\n  enabled: " + str(enabled).lower() + "\n  transport: stdio")
            print("  command: " + values.get("command", ""))
            print("  args: " + " ".join(values.get("args", [])))
        return 0
    if args[:3] == ["mcp", "remove", "claude-bridge"]:
        text = config.read_text() if config.exists() else ""
        found = registration(text)
        if found:
            config.write_text(text[:found[0]] + text[found[1]:])
        return 0
    if args[:3] == ["mcp", "add", "claude-bridge"] and "--" in args:
        if os.environ.get("TEST_FAIL_ADD") == "1":
            print("fixture registration add rejected", file=sys.stderr)
            return 8
        command = args[args.index("--") + 1:]
        config.parent.mkdir(parents=True, exist_ok=True)
        previous = config.read_text() if config.exists() else ""
        config.write_text(previous.rstrip("\n") + ("\n\n" if previous else "") +
                          "[mcp_servers.claude-bridge]\ncommand = " + json.dumps(command[0]) +
                          "\nargs = " + json.dumps(command[1:]) + "\n")
        return 0
    print("unexpected codex fixture call: " + repr(args), file=sys.stderr)
    return 97


def main():
    name = Path(sys.argv[0]).name
    args = sys.argv[1:]
    if name == "file":
        native = Path(os.environ["TEST_NATIVE_LIST"]).read_text().splitlines()
        print("ELF 64-bit LSB executable" if os.path.realpath(args[-1]) in native
              else "POSIX shell script, ASCII text executable")
        return 0
    if name == "claude":
        record("claude")
        if args == ["--version"]:
            # parseable like the real CLI (DESIGN §3); the default meets every Claude floor (the highest: the Haiku head >= 2.1.295)
            print(os.environ.get("TEST_CLAUDE_VERSION") or "2.1.295 (Claude Code)")
        elif args == ["auth", "status"]:
            print(json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                              "fixture_auth_payload": "private-auth-response"}))
        elif args == ["plugin", "marketplace", "list"]:
            print("[]")
        elif args == ["plugin", "marketplace", "add", "openai/codex-plugin-cc"]:
            if os.environ.get("TEST_PLUGIN_FAILURE") == "marketplace":
                print("fixture marketplace unavailable", file=sys.stderr)
                return 6
            print("marketplace added")
        elif args == ["plugin", "install", "codex@openai-codex", "--scope", "user", "-y"]:
            if os.environ.get("TEST_PLUGIN_FAILURE") == "install":
                print("fixture install unavailable", file=sys.stderr)
                return 7
            print("plugin installed")
        elif args[:2] == ["plugin", "list"]:
            print(json.dumps([{"name": "claude-codex-bridge@claude-codex-bridge",
                               "installPath": os.environ["TEST_PLUGIN"]}]))
        else:
            print("unexpected claude fixture call: " + repr(args), file=sys.stderr)
            return 97
        return 0
    if name == "npm":
        record("npm")
        if args != ["install", "-g", "@openai/codex"]:
            return 97
        repair = os.environ.get("TEST_NPM_REPAIR")
        if repair:
            # npm repairs the same path that failed the earlier version probe.
            shutil.copyfile(os.environ["TEST_NPM_TARGET"], repair)
            with open(repair, "a", encoding="utf-8") as handle:
                handle.write("\n# repaired package payload\n")
            os.chmod(repair, 0o755)
        else:
            Path(os.environ["TEST_BIN"], "codex").symlink_to(os.environ["TEST_NPM_TARGET"])
        return 0
    if name == "git":
        record("git")
        if args == ["--version"]:
            print("git version 2.50.0 (fixture)")
            return 0
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            print(os.environ["TEST_PLUGIN"])
            return 0
        if args and args[0] in ("show-ref", "rev-parse"):
            return 1
        # Publishing dry runs must never perform even a stubbed mutation.
        print("unexpected git mutation: " + repr(args), file=sys.stderr)
        return 97
    if name == "node":
        print("v22.0.0")
        return 0
    return codex(args)


if __name__ == "__main__":
    sys.exit(main())

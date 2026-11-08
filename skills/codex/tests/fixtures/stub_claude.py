#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the engine tests. The prompt MUST arrive on stdin (never argv).

Directives in the prompt, e.g. [[STUB:argv=/tmp/c.json]]:
  sleep=N / hang / argv=PATH / text=WORD  as in stub_codex (the argv record's `bridge_env` too)
  fail           result with is_error true, subtype error_max_turns, errors ["max turns"], exit 0
  limit=PATH     first call: a tool call, then an is_error result "Rate limit exceeded"; later calls succeed (engine must --resume)
  limitbeforework=PATH  first call: rate_limit_event rejected + error result before any tool call; later calls succeed
The account the stub relay ran us under (RELAY_STUB_ACCOUNT, None when unrouted) is written into the argv record and into
the init event, so a test can prove which account a seat ran under.

The stream echoes the requested model (--model): the init event's `model`, every assistant `message.model` and the result's
`modelUsage` key. Observed-model directives (DESIGN §4, §8):
  ranmodels=A+B        the init model is A; assistant messages ran A then B; modelUsage has A and B
  assistantmodel=ID    every assistant message.model is ID (the init model stays the requested one); modelUsage has ID
  forwarded=ID         adds a subagent's assistant message (parent_tool_use_id set) whose model is ID
  usagekeys=A+B        modelUsage keys are A and B (a resumed session's cumulative ledger)
  nousage              the result carries an empty modelUsage
  truncate             the stream stops before the result event
  noinitmodel          the init event carries no model
  synthetic            adds the CLI's own synthetic assistant messages naming other models, one per predicate of DESIGN §4,
                       each flag where the stream carries it: message.model "<synthetic>" on the message; `error:
                       "rate_limit"`, `is_api_error_message: true` and `api_error: "model_requires_usage_credits"` on the
                       event (never copied into the message)
  synthetic=K+K        only the named ones: model, rate_limit, api_error_message, usage_credits; `unflagged` is the same
                       message with no flag at all (an ordinary observation of another, fictional Opus id)
  replay=PATH          replay a recorded stream-json file verbatim (fixtures/evidence/claude_stream_resumed_session.jsonl)
Environment:
  STUB_CLAUDE_VERSION  what `--version` prints (default "99.0.0 (Claude Code)": above every floor)
  STUB_CLAUDE_RECORD   JSON-lines file: EVERY invocation is recorded ({argv, mode, model, effort, settings, fallback_model,
                       env_model_overrides, account})
"""
import json
import os
import re
import sys
import time

ACCOUNT = os.environ.get("RELAY_STUB_ACCOUNT") or None
MODEL_OVERRIDES = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
# the CLI's own synthetic assistant messages: (fields of `message`, fields of the event itself); the ids are the registry's
# fictional fixture ids (docs/model-routing.md 10.1: a real current id in a test is a lint error)
SYNTHETIC = {"model": ({"model": "<synthetic>"}, {}),
             "rate_limit": ({"model": "claude-sonnet-9-9"}, {"error": "rate_limit"}),
             "api_error_message": ({"model": "claude-opus-9-8"}, {"is_api_error_message": True}),
             "usage_credits": ({"model": "claude-haiku-9-9"}, {"api_error": "model_requires_usage_credits"}),
             "unflagged": ({"model": "claude-opus-9-8"}, {})}


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def record(obj):
    path = os.environ.get("STUB_CLAUDE_RECORD")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps(dict(obj, cwd=os.getcwd(), t=time.time())) + "\n")


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        record({"mode": "version", "argv": argv})
        print(os.environ.get("STUB_CLAUDE_VERSION", "99.0.0 (Claude Code)"))
        return 0
    if argv[:2] == ["auth", "status"]:
        record({"mode": "auth", "argv": argv})
        print(json.dumps({"loggedIn": True, "authMethod": "stub"}))
        return 0
    if "-p" not in argv:
        record({"mode": "unsupported", "argv": argv})
        print("stub: unsupported", file=sys.stderr)
        return 2
    # a positional prompt would be any argument that is not an option or an option value
    opts_with_value = {"--output-format", "--model", "--effort", "--permission-mode", "--allowedTools", "--disallowedTools", "--max-turns",
                       "--json-schema", "--resume", "--tools", "--add-dir", "--settings", "--fallback-model", "--setting-sources"}  # model-lint: allow the stub recognizes the CLI's fallback flag to report it
    positional = []
    i = 0
    schema = None
    resume = None
    while i < len(argv):
        a = argv[i]
        if a in opts_with_value:
            if a == "--json-schema":
                schema = argv[i + 1]
            if a == "--resume":
                resume = argv[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        positional.append(a)
        i += 1
    model = option(argv, "--model") or "claude-stub-default"
    settings_raw = option(argv, "--settings")
    try:
        settings = json.loads(settings_raw) if settings_raw is not None else None
    except ValueError:
        settings = {"_unparsed": settings_raw}
    overrides = sorted(k for k in MODEL_OVERRIDES if k in os.environ)
    record({"mode": "prompt", "argv": argv, "model": option(argv, "--model"), "effort": option(argv, "--effort"), "settings": settings,
            "fallback_model": "--fallback-model" in argv, "env_model_overrides": overrides, "account": ACCOUNT, "resume": resume})  # model-lint: allow the stub records whether the flag was passed
    prompt = sys.stdin.read()
    m = re.search(r"\[\[STUB:([^\]]*)\]\]", prompt)
    d = {}
    if m:
        for part in m.group(1).split(","):
            k, _, v = part.partition("=")
            d[k.strip()] = v.strip()
    if "argv" in d:
        with open(d["argv"], "a") as f:
            f.write(json.dumps({"argv": argv, "positional": positional, "prompt": prompt, "env_has_claudecode": "CLAUDECODE" in os.environ,
                                "env_has_token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ, "account": ACCOUNT,
                                "model": option(argv, "--model"), "effort": option(argv, "--effort"), "settings": settings,
                                "fallback_model": "--fallback-model" in argv, "env_model_overrides": overrides,  # model-lint: allow the stub records whether the flag was passed
                                "bridge_env": {k: v for k, v in os.environ.items() if k.startswith("BRIDGE_")}}) + "\n")
    if "sleep" in d:
        time.sleep(float(d["sleep"]))
    if "hang" in d:
        time.sleep(600)
    session_id = "stub-session-" + (resume or "aaaa")
    emit = lambda o: (sys.stdout.write(json.dumps(o) + "\n"), sys.stdout.flush())
    if "replay" in d:
        with open(d["replay"]) as f:
            for line in f:
                if line.strip():
                    emit(json.loads(line))
        return 0
    ran = d["ranmodels"].split("+") if d.get("ranmodels") else None
    init_model = ran[0] if ran else model
    assistant_models = ran if ran else [d.get("assistantmodel") or model]
    usage_keys = (d["usagekeys"].split("+") if d.get("usagekeys") else
                  (ran if ran else [d.get("assistantmodel") or model]))
    init = {"type": "system", "subtype": "init", "session_id": session_id, "account": ACCOUNT, "model": init_model}
    if "noinitmodel" in d:
        init.pop("model")
    emit(init)
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 0, "output_tokens": 10}
    if "limitbeforework" in d and not os.path.exists(d["limitbeforework"]):
        # no tool ran yet: the engine may simply retry the same prompt
        open(d["limitbeforework"], "w").write("1")
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 0}})
        emit({"type": "result", "subtype": "error", "is_error": True, "result": "Rate limit exceeded. Please try again later.",
              "session_id": session_id, "usage": usage, "errors": ["rate_limit"]})
        return 0
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "msg_1", "model": assistant_models[0], "usage": usage,
                                                                       "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}})
    for n, other in enumerate(assistant_models[1:], 2):
        emit({"type": "assistant", "parent_tool_use_id": None, "message": {"id": f"msg_{n}", "model": other, "usage": usage,
                                                                           "content": [{"type": "text", "text": "more"}]}})
    if d.get("forwarded"):
        emit({"type": "assistant", "parent_tool_use_id": "toolu_sub_1", "message": {"id": "msg_sub", "model": d["forwarded"], "usage": usage,
                                                                                    "content": [{"type": "text", "text": "subagent says hi"}]}})
    if "synthetic" in d:
        kinds = d["synthetic"].split("+") if d["synthetic"] else [k for k in SYNTHETIC if k != "unflagged"]
        for kind in kinds:
            message_fields, event_fields = SYNTHETIC[kind]
            msg = dict({"id": "msg_syn", "usage": usage, "content": [{"type": "text", "text": "API Error"}]}, **message_fields)
            emit(dict({"type": "assistant", "parent_tool_use_id": None, "message": msg}, **event_fields))
    if "truncate" in d:
        return 0
    text = d.get("text", "OK")
    structured = None
    if schema:
        structured = {"findings": [{"priority": "P1", "title": "Stub finding from claude", "file": "store.py", "line": 1,
                                    "explanation": "stub explanation", "suggested_fix": "stub fix", "confidence": 0.8}],
                      "assessment": "stub claude assessment"}
        text = json.dumps(structured)
    if "limit" in d and not os.path.exists(d["limit"]):
        open(d["limit"], "w").write("1")
        emit({"type": "result", "subtype": "error", "is_error": True, "result": "Rate limit exceeded. Please try again later.",
              "session_id": session_id, "usage": usage, "errors": ["rate_limit"]})
        return 0
    model_usage = {} if "nousage" in d else {k: {"inputTokens": 10, "outputTokens": 5, "contextWindow": 1000000} for k in usage_keys}
    if "badjson" in d:
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "Prose instead of JSON.", "session_id": session_id,
              "usage": usage, "modelUsage": model_usage})
        return 0
    if "fail" in d or "claudefail" in d:
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "", "session_id": session_id, "usage": usage,
              "errors": ["Reached max turns"], "modelUsage": model_usage})
        return 0
    res = {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session_id,
           "usage": {"input_tokens": 4, "cache_creation_input_tokens": 4100, "cache_read_input_tokens": 4000, "output_tokens": 50},
           "modelUsage": model_usage}
    if structured is not None:
        res["structured_output"] = structured
    emit(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())

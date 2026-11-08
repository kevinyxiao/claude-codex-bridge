---
name: claude
description: Call Claude (the Fable, Opus, Sonnet and Haiku families, always the newest model in each) from Codex through this machine's own Claude Code login — ask a hard question, get an independent defect-first code review, delegate a task, run several seats in parallel, or continue a Claude session. Nothing account-specific, no API keys. Use when the user says $claude, "ask Claude", "have Claude review this", "second opinion from Anthropic", or has authorized consulting Claude for the task at hand. Difficulty alone does not authorize spending the user's Claude quota.
user-invocable: true
---

# $claude — use Claude from inside Codex

Claude runs through the `claude` CLI that is already signed in on this machine, so this works on any machine with any account. Each call is a fresh Claude Code process under its restricted profile (explicit tool list, no MCP servers, no inherited settings); read-only calls have no shell at all. Only Claude's answer comes back here; logs stay under `~/.cache/codex-claude-bridge/jobs/` (private to the user).

## Preferred path: the `claude-bridge` MCP tools

If these tools are available (the machine's bootstrap registered the `claude-bridge` MCP server), call them directly:

- `claude_ask` `{prompt, seats?, cwd?, files?}` — one hard question; Claude reads the repository read-only. Default seat `fable:max`.
- `claude_review` `{scope?: uncommitted|base|commit, ref?, intent?, focus?, seats?, cwd?, wait?}` — independent review with P0–P3 findings, file:line, fix, confidence, assessment. Default seat `opus:max`. Always pass `intent` (what changed and why). `ref` is required with `base` or `commit`.
- `claude_task` `{prompt, write?, seats?, cwd?, wait?}` — delegate work. Read-only by default. `write: true` is only honoured when the server was started with `CLAUDE_BRIDGE_ALLOW_WRITE=1`, and Codex asks the user to approve the call; expect a refusal otherwise. Exactly one seat.
- `claude_reply` `{job, seat?, prompt}` — follow-up in the same Claude session; the job id is in every result footer.
- `claude_status` `{job}` and `claude_result` `{job}` — poll and collect a job, including one started with `wait: false`.

Long jobs: a call with `wait: true` (the default) is bounded by the server deadline (`CLAUDE_BRIDGE_TOOL_DEADLINE`, default 3,300 s) and by Codex's own tool timeout. For a review or task that may run longer, or that may sit on the usage-limit ladder, pass `wait: false`: the tool returns at once with the job id and `status: "started"`, the job keeps running after the call ends, and you collect it with `claude_status` then `claude_result`.

Seats are `model[:effort]`, comma-separated to run several in parallel: the Claude families `fable` (strongest), `opus`, `sonnet`, `haiku`, plus the pinned `opus-5` seat (the K1 seat for legal work: the pinned Opus), with effort `low|medium|high|xhigh|max` (`haiku` takes none); GPT seats (`astra`, `sol`, …) also work for a mixed council. A family name always means the newest model in that family (the engine's table decides which; its `doctor` lists the exact IDs and the CLI versions they need); a retired or superseded model ID is refused with the replacement named, and nothing falls back to another model on its own: a Codex seat moves to another family or effort only when its spec lists one after a `|` (`astra|sol:high`), and the answer then says so first with a leading `NOTICE:` line. `ask` and `review` accept several seats; `task` accepts exactly one. The defaults (`fable:max` for ask and task, `opus:max` for review) can be changed per machine through `BRIDGE_DEFAULT_SEAT` and `BRIDGE_REVIEW_SEATS` in the server's environment; the `doctor` command prints the effective values.

The server only reads inside its authorized root: `CLAUDE_BRIDGE_ROOT` if it was set when the server started, otherwise the directory Codex started it in. Pass `cwd` and `files` inside that root; anything outside is refused with a clear error unless the machine opted into `CLAUDE_BRIDGE_ALLOW_ANY_CWD=1`.

A result whose text starts with `PARTIAL:` means some seats failed and the rest is usable. An error result names the cause; do not retry blindly.

## Fallback path: the CLI

If the MCP tools are missing, run the engine directly. It needs network access and write access to the Claude configuration directory, so it cannot run inside a restricted shell sandbox: request approval to run it unsandboxed, or ask the user to run the plugin's bootstrap so the MCP server is registered.

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/claude/scripts/bridge.py" ask [--seats fable:max] -- "question"
python3 "${CODEX_HOME:-$HOME/.codex}/skills/claude/scripts/bridge.py" review [--base <ref> | --commit <sha>] --intent "..." [--focus "..."] [--seats opus:max,fable:xhigh]
python3 "${CODEX_HOME:-$HOME/.codex}/skills/claude/scripts/bridge.py" task [--write [--shell]] -- "task"
python3 "${CODEX_HOME:-$HOME/.codex}/skills/claude/scripts/bridge.py" reply <job|last> [--seat opus] -- "follow-up"
python3 "${CODEX_HOME:-$HOME/.codex}/skills/claude/scripts/bridge.py" status <job> | monitor <job> --once | doctor
```

Long text goes on stdin instead of the command line (`review --intent-stdin` reads `{"intent": …, "focus": …}`). `task --write` gives a Claude seat edit tools and no shell; `--shell` adds a fenced Bash tool. Exit status 0 = every seat succeeded, 3 = partial, 1 = failed, 2 = usage error. If `doctor` reports that `claude` is missing or signed out, tell the user to run the plugin's `bootstrap.sh` or `claude` once to sign in. Never ask for an API key or improvise another auth path.

## How to use the answers

- Present Claude's output verbatim, findings first for reviews, and keep the footer (job id, session) so follow-ups are possible.
- After a review, stop and ask which findings to fix; do not auto-apply fixes unless the user already said to.
- Give Claude the context it needs in the prompt (paths, symptoms, what you tried). It reads the repository but cannot see this conversation.
- Calls at `max` effort take one to several minutes; say so before waiting. `status`/`monitor --once` show each Claude seat's exact context size and activity while it runs; a seat in state `waiting` is parked on the usage-limit ladder and resumes its own session afterwards.

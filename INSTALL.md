# claude-codex-bridge — install on your Mac

This gives you two things:

- **`/codex` inside Claude Code** (also `/gpt`): ask OpenAI's models (the newest GPT Astra, Sol, Terra or Luna) a hard question, have several of them review your changes at once, or delegate a task.
- **`$claude` inside Codex**: ask Claude (the newest Fable or Opus) the same way from the other side.

It runs through the Claude Code and Codex you already have on this Mac, with the accounts you are already signed into. There are no API keys and nothing to register. Your prompts and files leave your machine only in the model calls those two apps make anyway; the one thing the bridge itself reads over the network is a small daily check of which models are current (three public pages, no prompt, no file content, no credentials; details under "Costs and privacy").

## Before you start (about five minutes)

1. **Claude Code**, installed, signed in and current: version 2.1.284 or newer (`claude update` upgrades it; the installer checks and tells you). Open Terminal, type `claude`, sign in if it asks, then quit it.
2. **Codex**, one of:
   - the **ChatGPT desktop app**, signed in, with Codex enabled (the installer finds its built-in Codex; when that Codex is older than 0.156.1, which the newest Sol and Luna need, the installer installs a current command-line Codex with npm, so Node.js must be installed), or
   - the command-line Codex: `npm install -g @openai/codex@latest`, then `codex login`.
3. **Apple's command-line tools** (they provide `python3` and `git`). Type `python3 --version` in Terminal; if macOS offers to install developer tools, accept and wait for it to finish.

## Install

1. Unzip the file. Move the `claude-codex-bridge` folder into your home folder, so it lives at `~/claude-codex-bridge`. Leave it there afterwards: the install points at this folder.
2. In Terminal:

   ```
   bash ~/claude-codex-bridge/install.sh
   ```

3. Read the summary at the end. Every line should say `ok`. If it tells you to add a line to your PATH, copy that exact line into `~/.zshrc` and open a new Terminal window.
4. Quit and reopen Claude Code and Codex (and any terminal windows running them).

## Try it

In Claude Code, inside a project folder:

- `/codex ask what does this repository do?` (the first call takes about a minute)
- `/codex review` after you change something: Astra, Sol and Opus review the change independently and you get one merged report
- `/codex council should this use SQLite or Postgres?`
- `/codex monitor` while something long runs; `/codex status` for the list of jobs

In Codex:

- `$claude ask what does this repository do?`
- `$claude review`

Seats are `model:effort`. Defaults are Astra at ultra for single questions and Astra, Sol and Opus for reviews; pass `--seats luna:low` for something cheap and fast, or `--seats astra:max,opus:max` to choose your own.

## Model names

Names such as `astra`, `sol`, `luna`, `fable` and `opus` always mean the newest model in that family. The bridge keeps one table of what that is today (`skills/codex/models.json` in this folder; updating the zip updates it), and `python3 ~/claude-codex-bridge/skills/codex/scripts/bridge.py doctor` prints the exact model names and checks, for free, whether the table is still current. When OpenAI releases a newer GPT model and the Codex you are signed into lists it, the bridge starts using it for that family on its own, within a day (at once when you run `doctor`), says so at the top of every answer from it for a week, and `doctor` shows it; `bridge.py doctor --undo-promotion <family> --reason "<why>"` goes back to the previous model. Newer Claude models arrive with an updated zip. If you type an old model name, the bridge refuses and names the replacement instead of quietly running something else. If your ChatGPT account does not offer a model, its seat stops and says so; it moves to another model only when you list one after a `|` (`astra|sol`), and the answer then says so at the top.

## What the installer changes, exactly

- Four links under `~/.claude` (`skills/codex`, `skills/gpt`, `agents/opus-reviewer.md`, `agents/opus-consultant.md`) pointing into this folder.
- One link, `~/.codex/skills/claude`, pointing into this folder.
- One block in `~/.codex/config.toml` that registers the `claude-bridge` tool server. A dated backup of the file is kept next to it.
- A tiny `codex` launcher script in `~/.local/bin` that runs the Codex the installer chose (the ChatGPT app's built-in Codex, or a command-line Codex it found or installed), so `codex` works in Terminal too. It is written when the app's Codex is chosen, when the chosen Codex lives in a folder that may not be on your PATH, or when the installer already owns such a launcher there.
- If no Codex on this Mac meets the model table's floor: the current command-line Codex from npm (`npm install -g @openai/codex`; a global npm package, which `--uninstall` leaves in place).

Nothing else. `bash ~/claude-codex-bridge/install.sh --check` shows what would change without changing anything. `bash ~/claude-codex-bridge/install.sh --uninstall` removes the links, the `config.toml` block and the launcher script and nothing more (the npm package, if one was installed, stays; `npm uninstall -g @openai/codex` removes it); then delete the folder.

## Costs and privacy

- Calls use your own Claude and ChatGPT plan quotas. Astra at `ultra` is the most expensive seat; `luna:low` or `haiku:low` are the cheapest.
- Logs for every call live in `~/.cache/codex-claude-bridge/jobs/`, readable only by you. Delete that folder whenever you like.
- Prompts and file contents go to the two CLIs on this Mac only. The bridge itself makes one kind of network read, its models check: once a day before a command that starts a model, and every time you run `doctor`, it fetches npm's version list for Codex (`registry.npmjs.org`) and two Anthropic documentation pages (`platform.claude.com`) over HTTPS, and asks the Codex on this Mac for its model list and whether you are signed in (Codex answers that through your own ChatGPT sign-in, as the app does; the bridge keeps only "signed in" and the plan, never who). These reads carry no prompt, no file content and no credentials; what they learned is kept in `~/.cache/codex-claude-bridge/models-evidence.json`, and the GPT models it adopted in `models-evidence.promotions.json` beside it. Behind a network that blocks those hosts the check reports `UNVERIFIED` and everything else keeps working.

## If something does not work

- `bash ~/claude-codex-bridge/install.sh --check` and `python3 ~/claude-codex-bridge/skills/codex/scripts/bridge.py doctor` both explain what is missing.
- `Codex version: FAILED` or `Claude Code version: FAILED` in the installer's summary: that line names the upgrade for the Codex you have (`npm install -g @openai/codex@latest` for an npm install, `brew upgrade codex` for Homebrew, updating the ChatGPT app for its built-in Codex, or OpenAI's installer) or `claude update` for Claude Code; run it, then run the installer again. A seat refused with "needs Codex CLI >=" or "needs Claude Code >=" names the same upgrade (for Codex, pointing `CODEX_BIN` at a newer `codex` also works).
- A model name is refused: use the family name the message suggests (`sol`, `opus`, ...); `doctor` lists the current names.
- `codex` in Terminal answers `No such file or directory` after the ChatGPT app updated itself: the app moved its built-in Codex (app build 26.928), and an installer from before this version does not know the new place. If you have not yet replaced the folder with this version, do that first (see "Updating" below). Then run `bash ~/claude-codex-bridge/install.sh`. It finds the built-in Codex at its new place and re-points the `codex` launcher script in `~/.local/bin`; it needs neither npm nor a second Codex.
- "Codex is not signed in": run `codex login` (or sign into the ChatGPT app).
- "Claude is not signed in": run `claude` once and sign in.
- `command not found: codex`: add the PATH line the installer printed to `~/.zshrc`.
- `/codex` does not appear in Claude Code, or Codex does not know `$claude`: quit and reopen the app.
- A call seems stuck: `/codex status`, then `/codex cancel <job>`.

## Updating

Unzip the new version, replace the folder at `~/claude-codex-bridge`, and run `bash ~/claude-codex-bridge/install.sh` again. It refreshes its own links and registration and touches nothing else.

## Two accounts, or a machine without a browser

- Two Claude or two ChatGPT accounts on one Mac: point `CLAUDE_CONFIG_DIR` and `CODEX_HOME` at separate folders before starting the apps; the bridge follows whichever is active.
- No browser for signing in: `claude setup-token` gives a long-lived token (export it as `CLAUDE_CODE_OAUTH_TOKEN`); `codex login --device-auth` does the same for Codex.

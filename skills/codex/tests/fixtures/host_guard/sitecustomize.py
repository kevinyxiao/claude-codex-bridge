"""Test guard for the Codex discovery tests (TEST_RULINGS F-E6): no host `codex` or `claude` starts during a guarded run.

A guarded run puts this directory on PYTHONPATH, so every Python process of the run (the engine CLI, bootstrap's Python
helpers, the stubs) imports this module at startup as `sitecustomize`. Its audit hook refuses to start an executable
named codex or claude (or codex.*, claude.*) whose real path lies outside the allowed roots; the stubs under the test root
run normally. It inspects executable positions only, never arguments (a helper that is merely given such a path runs):
  - the program started;
  - the command `env` runs, after env's options and NAME=value assignments;
  - the command word of each simple command in a shell's -c string (-c, -lc, -ec, -euo pipefail -c, ...), where "$0",
    "$1" and "$@" stand for the operands after the string;
each followed through env and shells again. A product that ignored BRIDGE_CODEX_FIXED_ROOT therefore cannot run the
host's /opt/homebrew/bin/codex or /usr/local/bin/codex even for a `--version` probe, directly or as `env codex` or
`bash -lc 'codex ...'`: the start fails with PermissionError (the caller treats that candidate as unusable) and the
refusal is logged for the test to assert on. It never reads or changes product code. Not followed: other wrappers (nohup,
timeout, xargs, sudo), variables other than the positional parameters, and a PATH changed on the way (a bare name is
looked up on this process's PATH, as for a direct launch).

Environment:
  HOST_GUARD_ROOTS  the allowed directories, os.pathsep-separated (the test root); unset = the guard does nothing
  HOST_GUARD_LOG    JSON-lines file, one line per refusal: {"event", "refused", "pid"}
"""
import json
import os
import re
import shlex
import shutil
import sys

ROOTS = [os.path.realpath(p) for p in os.environ.get("HOST_GUARD_ROOTS", "").split(os.pathsep) if p]
LOG = os.environ.get("HOST_GUARD_LOG")
SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
POSITIONAL = re.compile(r"\$\{?([0-9@*])\}?$")
PREFIXES = ("!", "{", "if", "then", "elif", "else", "while", "until", "do", "time", "exec", "command")  # a command follows


def _text(value):
    try:
        return os.fsdecode(value)
    except TypeError:
        return str(value)


def _outside(word):
    """True when `word` names a codex or claude executable whose real path is outside every allowed root."""
    base = os.path.basename(word).lower()
    if not (base in ("codex", "claude") or base.startswith(("codex.", "claude."))):
        return False
    path = word if os.sep in word else shutil.which(word)
    if not path:
        return False
    real = os.path.realpath(path)
    return not any(real == root or real.startswith(root + os.sep) for root in ROOTS)


def _split(text):
    try:
        return shlex.split(text)
    except ValueError:  # an unterminated quote
        return text.split()


def _env_command(args):
    """The command line `env args...` runs: env's options up to `--` (-i, -u NAME, -C DIR, -P PATH, --unset=NAME, ...) and
    the NAME=value assignments are skipped; the value of -S (--split-string) is split into words and read on, as env does."""
    args, options = list(args), True
    while args:
        word = args.pop(0)
        if options and word.startswith("-") and word != "-":
            if word == "--":
                options = False
            elif word.startswith("--"):
                name, given, value = word.partition("=")
                if name in ("--unset", "--chdir", "--split-string") and not given:
                    value = args.pop(0) if args else ""
                if name == "--split-string":
                    args[:0] = _split(value)
            else:
                for i, flag in enumerate(word[1:], 2):
                    if flag in "uCPS":  # an option with a value: the rest of this word, or the next word
                        value = word[i:] or (args.pop(0) if args else "")
                        if flag == "S":
                            args[:0] = _split(value)
                        break
        elif word != "-" and not ASSIGNMENT.match(word):
            return [word] + args
    return []


def _command_string(args):
    """(command string, operands) of a shell started with -c, alone or in a cluster (-lc, -ec, -euo pipefail -c), or None.
    Options end at the first operand; -o and -O (and +o, +O) take an option name, --rcfile and --init-file a file."""
    command = False
    args = list(args)
    while args:
        word = args.pop(0)
        if word in ("-", "--"):
            break
        if word in ("--rcfile", "--init-file"):
            del args[:1]
        elif word[:1] in ("-", "+") and not word.startswith("--"):
            command = command or (word[0] == "-" and "c" in word)
            del args[:word.count("o") + word.count("O")]
        elif not word.startswith("--"):
            args.insert(0, word)
            break
    return (args[0], args[1:]) if command and args else None


def _commands(script):
    """The simple commands of a shell command string, as lists of words (quotes removed): control operators, brackets,
    backquotes and newlines separate them. A string shlex cannot read (an unterminated quote): each word on its own."""
    lex = shlex.shlex(script, posix=True, punctuation_chars=";&|()`<>\n")
    lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
    commands = [[]]
    try:
        for token in lex:
            if token and set(token) <= set(";&|()`\n"):
                commands.append([])
            else:
                commands[-1].append(token)
    except ValueError:
        return [[word.strip("'\"")] for word in script.split()]
    return commands


def _command(words, operands):
    """A simple command from its command word on: reserved words, exec and command, NAME=value assignments and
    redirections before it are skipped, and a positional parameter ("$0", "$1", "$@") becomes the operands it names."""
    while words:
        word = words[0]
        if word and set(word) <= set("<>&|"):  # a redirection, and the file it names
            words = words[2:]
        elif word.isdigit() and len(words) > 1 and words[1] and set(words[1]) <= set("<>&|"):  # its descriptor
            words = words[1:]
        elif word in PREFIXES or ASSIGNMENT.match(word):
            words = words[1:]
        elif POSITIONAL.match(word):
            n = POSITIONAL.match(word).group(1)
            return (operands[1:] if n in "@*" else operands[int(n):int(n) + 1]) + words[1:]
        else:
            return words
    return []


def _targets(argv):
    """The executables starting `argv` runs: argv[0], and through env and a shell's -c string the commands they run."""
    if not argv:
        return []
    name = os.path.basename(argv[0])
    if name == "env":
        return [argv[0]] + _targets(_env_command(argv[1:]))
    found = _command_string(argv[1:]) if name in SHELLS else None
    if not found:
        return [argv[0]]
    script, operands = found
    return [argv[0]] + [t for words in _commands(script) for t in _targets(_command(words, operands))]


def _start(event, args):
    """(program, argv) of an audited process start, or None for any other event."""
    if event in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
        return args[0], args[1]
    if event == "os.spawn":
        return args[1], args[2]
    if event == "os.system":
        return "/bin/sh", ["/bin/sh", "-c", args[0]]
    return None


def _hook(event, args):
    start = _start(event, args)
    if start is None:
        return
    program, argv = start
    argv = [_text(a) for a in ([argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or []))]
    program = _text(program) if program is not None else (argv[0] if argv else "")
    refused = [w for w in _targets([program] + argv[1:]) if _outside(w)]
    if not refused:
        return
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"event": event, "refused": refused, "pid": os.getpid()}) + "\n")
    raise PermissionError("host guard (TEST_RULINGS F-E6): refused to start %s; only stubs under %s may run"
                          % (refused[0], os.pathsep.join(ROOTS)))


if ROOTS:
    sys.addaudithook(_hook)

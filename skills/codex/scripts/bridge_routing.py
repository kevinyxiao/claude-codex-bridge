#!/usr/bin/env python3
"""
bridge_routing.py: the bridge's link to the model registry (docs/model-routing.md sections 2.3, 3.3, 5 and 6).

The registry module is found the way every script of the repository finds it (design 2.1): MODEL_REGISTRY_DIR, then the
nearest skills/model-registry above this file (a worktree finds its own copy; the installed symlink resolves into the main
clone), then ~/.claude/skills/model-registry. Without one the bridge keeps its built-in table and behaviour; with one it
gains what this module wraps:

  * the launch fingerprint of a Codex process (executable, version, CODEX_HOME, -c overrides, directory, configuration mode
    and the effective catalog-affecting configuration), computed through the app-server's config/read and cached per process;
  * the per-account catalog snapshots in the registry's state (read through the app-server under the account's own home,
    stored bound to the fingerprint) and the capable set: the accounts whose fresh, compatible snapshot lists the head,
    not hidden, at the requested effort (and with the fast tier when asked);
  * the pre-launch re-check of the picked account;
  * per-attempt provenance: a Codex attempt from its own window of the rollout file, a Claude attempt from its own stream.

A key the server resolves to a default value is a default (the peer's L1 finding: only an `origins` entry means a layer
set it), so the equivalence rule of design 3.3 compares origins-normalized values. Nothing here launches a model, calls
relay or reaches the network: every read is the free app-server catalog read of design 3.3.
"""
import hashlib
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ENV_DIR = "MODEL_REGISTRY_DIR"
LOCAL_LABEL = "default"  # the machine's own login, as the registry's own Codex launcher names it
IGNORE = "ignore-user-config"
USER = "user"
# The environment variables a Codex process reads to choose its provider or credential: they change which provider, and so
# which catalog, answers model/list, so a snapshot is bound to them as it is to a configuration file (design 3.3).
PROVIDER_ENV_PREFIX = "OPENAI_"
PROVIDER_ENV_KEYS = ("CODEX_API_KEY",)


def provider_env_digest(env):
    """A digest of the provider and credential variables in `env` (names and values), or None when there are none: a launch under a
    clean environment fingerprints exactly as the registry's own launcher fingerprints it, one under an ambient provider or
    credential differently, so a catalog read under one is never consumed by a launch under the other."""
    items = sorted((k, v) for k, v in (env or {}).items() if k.startswith(PROVIDER_ENV_PREFIX) or k in PROVIDER_ENV_KEYS)
    if not items:
        return None
    return "sha256:" + hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()[:16]


def one_line(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def utc_now():
    """A precise UTC timestamp in the registry's format (the timestamp fallback of the rollout window)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def registry_module(caller_path):
    """(module, path) of model_registry.py for a caller, or (None, None) when no copy exists."""
    here = Path(caller_path).resolve()
    roots = []
    if os.environ.get(ENV_DIR):
        roots.append(Path(os.environ[ENV_DIR]).expanduser())
    roots += [p / "skills" / "model-registry" for p in here.parents]
    roots.append(Path.home() / ".claude" / "skills" / "model-registry")
    for root in roots:
        candidate = root / "scripts" / "model_registry.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("model_registry", str(candidate))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, str(candidate)
    return None, None


def defaults_by_origin(result, keys):
    """A config/read answer with every catalog-affecting key that no layer set replaced by None: the server answers resolved
    defaults (a default `chatgpt_base_url` carries a URL), and only an `origins` entry for the key, or for one of its
    sub-keys, means a configuration layer set it."""
    if not isinstance(result, dict):
        return result
    config = result.get("config") if isinstance(result.get("config"), dict) else None
    origins = result.get("origins") if isinstance(result.get("origins"), dict) else {}
    if config is None:
        return result
    out = dict(result)
    cfg = dict(config)
    for k in keys:
        if not any(o == k or o.startswith(k + ".") for o in origins):
            cfg[k] = None
    out["config"] = cfg
    return out


def kinds_of(errors):
    """The typed collection errors of a record: a Claude complete record also lists the telemetry of the events its observer
    excluded (synthetic messages, forwarded subagent turns, API errors), dicts without a `kind`, which are not errors."""
    return [e["kind"] for e in errors or [] if isinstance(e, dict) and isinstance(e.get("kind"), str) and e["kind"]]


_METHODS = ("config/read", "model/list", "initialize")


def diagnose(text, default="catalog read failed"):
    """The sanitized form of an app-server failure text: the RPC method it names (the first named), else `default`; never the
    server's text, which can carry a configuration value (the bridge's C3-2 rule)."""
    text = str(text or "")
    found = [(text.find(m), m) for m in _METHODS if m in text]
    return (min(found)[1] + " failed") if found else default


def diagnostic(exc):
    """What the bridge repeats of an app-server failure: the RPC method when the registry named one, else the exception class."""
    return diagnose(str(exc), default=exc.__class__.__name__)


_NO_CATALOG = re.compile(r"no catalog \((.*)\)$")


def clean_reason(reason):
    """A registry verdict with the stored read error (server text) replaced by its sanitized diagnosis."""
    return _NO_CATALOG.sub(lambda m: "no catalog (%s)" % diagnose(m.group(1)), str(reason))


def attempt_summary(records, registry_path=None, registry_sha256=None):
    """The seat record's `provenance` (design 6): the complete record's requested, expected and observed identities, the
    substitution verdict, the outcome and the collection errors, plus the registry that judged them."""
    done = records[-1] if records else {}
    start = records[0] if records else {}
    sub = done.get("substitution")
    return {"attempt_id": done.get("attempt_id"), "model_requested": done.get("model_requested"), "model_expected": done.get("model_expected"),
            "expected_evidence": done.get("expected_evidence"), "observed_model_id": done.get("observed_model_id"),
            "observed_models": list(done.get("observed_models") or []), "observed_via": done.get("observed_via"),
            "session_models_cumulative": done.get("session_models_cumulative"), "substitution": sub, "substituted": bool(done.get("substituted")),
            "downgraded_from": done.get("downgraded_from"), "outcome": done.get("outcome"), "errors": kinds_of(done.get("errors")),
            "cursor_source": start.get("cursor_source"), "thread_ref": done.get("thread_ref"),
            "registry_path": done.get("registry_path") or registry_path, "registry_sha256": done.get("registry_sha256") or registry_sha256}


def builtin_summary(attempt_id, requested, expected, observed_models, outcome, observed_via="none (no registry)"):
    """What the bridge records without a registry: the identities and the outcome, no classification, no registry hash."""
    observed = list(observed_models or [])
    other = [m for m in observed if m != expected]
    return {"attempt_id": attempt_id, "model_requested": requested, "model_expected": expected, "expected_evidence": "bridge table",
            "observed_model_id": observed[-1] if observed else None, "observed_models": sorted(observed), "observed_via": observed_via,
            "session_models_cumulative": None,
            "substitution": ({"observed": other[0], "expected": expected, "class": "unclassified", "authorized": False} if other else None),
            "substituted": bool(other), "downgraded_from": None, "outcome": outcome, "errors": [], "cursor_source": None, "thread_ref": None,
            "registry_path": None, "registry_sha256": None}


class LaunchUnavailable(Exception):
    """A Codex launch whose catalog configuration cannot be discovered: str() is the one-line reason."""


class Link:
    """The registry as the bridge sees it: the module and a loaded Registry, or the one-line reason there is none."""

    def __init__(self, caller_path, catalog_keys=None):
        self.mod, self.reg, self.module_path, self.error = None, None, None, None
        self.catalog_keys = tuple(catalog_keys or ())  # the bridge table's catalog-affecting keys (L2: service_tier is not one)
        self._launches = {}
        self._bundled = {}
        try:
            self.mod, self.module_path = registry_module(caller_path)
        except Exception as exc:  # a broken copy: say so once, keep the built-in defaults
            self.mod = None
            self.error = "the model registry module could not be imported: %s" % one_line(exc)
            return
        if self.mod is None:
            self.error = "no model registry found (%s unset, no skills/model-registry above the engine, none under ~/.claude/skills)" % ENV_DIR
            return
        try:  # the registry.json beside the module copy that was found (a worktree's own), never another checkout's
            self.reg = self.mod.Registry.load(registry_dir=str(Path(self.module_path).resolve().parent.parent))
        except Exception as exc:
            self.reg = None
            self.error = "the model registry could not be loaded: %s" % one_line(exc)

    @property
    def available(self):
        return self.reg is not None

    @property
    def path(self):
        return str(self.reg.path) if self.reg is not None else None

    @property
    def sha256(self):
        return self.reg.sha256 if self.reg is not None else None

    def describe(self):
        """One doctor line: where the registry is and its hash, or why there is none."""
        if self.reg is None:
            return "registry: none (%s); the bridge keeps its built-in table and routing" % self.error
        return "registry: %s (sha256 %s); state %s" % (self.reg.path, self.reg.sha256[:12], self.reg.state.home)

    # ---------------------------------------------------------------- heads
    def registry_head(self, family, vendor="codex"):
        """(head id, source text) of a family on this Mac as the registry resolves it from local state (no refresh here)."""
        registry = self.reg
        r = registry.resolve(family, vendor=vendor)
        return r.model_id, r.source

    def head_status(self, table, family):
        """(state, registry head, registry source) of a Codex family. "agree": the head the registry's state resolves is the table's.
        "floor": the registry holds no catalog evidence for the family yet and resolves its floor (its last human checkpoint), a
        predecessor the table retires or refuses, while the table's head is a model the registry itself lists for the family: no
        disagreement, only evidence the registry has not read (a seat's catalog read promotes it). "differs": the registry's
        head is another id. "unresolved": the registry could not resolve the family."""
        mine = table.head(family)
        try:
            head, source = self.registry_head(family, "codex")
        except Exception as exc:
            return "unresolved", None, "unresolved: %s" % one_line(exc)
        if head == mine:
            return "agree", head, source
        if source == "registry:floor":
            rec = (self.reg.registry.get("models") or {}).get(mine) or {}
            retired = (table.data.get("retired") or {}).get("codex", {}).get(family) or []
            refused = (table.data.get("refused") or {}).get("codex") or {}
            if rec.get("family") == family and rec.get("vendor", "codex") == "codex" and (head in retired or head in refused):
                return "floor", head, source
        return "differs", head, source

    def head_disagreements(self, table):
        """[(family, table head, registry head, registry source)] for every Codex family whose accepted head in the registry's
        state differs from the bridge table's head: both are kept (the table decides the seat, the registry the routing
        evidence) and the disagreement is reported, never hidden (F3 notes: neither replaces the other). A registry that holds
        no evidence yet (`floor_heads`) is not a disagreement."""
        out = []
        for fam in table.family_names("codex"):
            state, head, source = self.head_status(table, fam)
            if state in ("differs", "unresolved"):
                out.append((fam, table.head(fam), head, source))
        return out

    def floor_heads(self, table):
        """[(family, table head, registry floor)] for the Codex families the registry holds no catalog evidence for yet."""
        out = []
        for fam in table.family_names("codex"):
            state, head, _source = self.head_status(table, fam)
            if state == "floor":
                out.append((fam, table.head(fam), head))
        return out

    def expected_evidence(self, table, family):
        """The evidence text a seat's expectation carries: the table's revision and what the registry says of the family's head."""
        base = (table.head_evidence(family) if hasattr(table, "head_evidence") else
                "bridge table v%s (%s) known_good" % (table.data.get("version"), table.data.get("updated")))
        if table.vendor_of_family(family) != "codex":
            return base
        state, head, source = self.head_status(table, family)
        if state == "agree":
            return base + "; registry agrees (%s)" % source
        if state == "floor":
            return base + "; registry floor %s, no catalog evidence yet (%s is its listed successor)" % (head, table.head(family))
        if state == "unresolved":
            return base
        return base + "; registry head %s (%s) differs" % (head, source)

    # ---------------------------------------------------------------- the launch fingerprint and the catalogs
    def keys(self):
        """The catalog-affecting keys the fingerprint compares: the bridge table's (the L2 ruling: `service_tier` does not change
        model/list), else the registry's."""
        return self.catalog_keys or tuple(self.reg.registry.get("catalog_config_keys") or self.mod.CATALOG_CONFIG_KEYS)

    def catalog_overrides(self, overrides):
        """The -c overrides that set a catalog-affecting key: the only ones the fingerprint compares. The bridge's own
        `service_tier="fast"` override selects a speed tier of the same catalog (L2), so a fast seat and a plain one share an
        account's snapshot instead of invalidating each other's."""
        keys = self.keys()
        out = []
        for ov in overrides:
            key = str(ov).split("=", 1)[0].strip().strip('"').split(".")[0]
            if key in keys:
                out.append(ov)
        return out

    def bundled(self, exe, version):
        """The installed CLI's bundled succession edges, read once per process per executable (a replaced binary, seen by its
        size or mtime, is read again); never fails the caller."""
        try:
            real = os.path.realpath(str(exe))
            st = os.stat(real)
            key = (real, st.st_size, st.st_mtime, version)
        except OSError:
            return None
        if key not in self._bundled:
            try:
                self._bundled[key] = self.reg.bundled_catalog_if_changed(str(exe), version=version)
            except Exception as exc:
                self._bundled[key] = {"unavailable": True, "error": one_line(exc)}
        return self._bundled[key]

    @staticmethod
    def discovery_env(codex_home, overrides, env, scratch_path=None):
        """The environment and global -c arguments of a discovery under `codex_home` (or a scratch home standing in for it)."""
        run_env = dict(env if env is not None else os.environ)
        run_env["CODEX_HOME"] = str(scratch_path or codex_home)
        global_args = []
        for ov in overrides:
            global_args += ["-c", ov]
        return run_env, global_args

    def _fingerprint(self, exe, version, codex_home, cwd, config_mode, overrides, env, discovery_home=None):
        mod = self.mod
        keys = self.keys()
        run_env, global_args = self.discovery_env(codex_home, overrides, env, discovery_home)
        result = mod.codex_config_read(str(exe), env=run_env, cwd=str(cwd), global_args=global_args)
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):  # an answer of another shape proves nothing
            raise RuntimeError("config/read answered without a config object")
        result = defaults_by_origin(result, keys)
        catalog_config, files, refs = mod.codex_catalog_config(str(exe), codex_home=str(codex_home), cwd=str(cwd), env=run_env, keys=keys,
                                                               result=result, global_args=global_args)
        digest = provider_env_digest(env if env is not None else os.environ)
        if digest:  # an ambient provider or credential is part of what the launch sees (the equivalence rule reads only the catalog keys)
            catalog_config = dict(catalog_config, provider_env=digest)
        return mod.launch_fingerprint(exe=os.path.realpath(str(exe)), version=version, codex_home=str(codex_home),
                                      cli_overrides=self.catalog_overrides(overrides), cwd=str(cwd), config_mode=config_mode,
                                      catalog_config=catalog_config, config_files=files, referenced_files=refs)

    def equivalent(self, fp, overrides):
        return self.mod.discovery_equivalent_to_ignore_user_config(fp.get("catalog_config"), self.keys(), overrides=self.catalog_overrides(overrides))

    def files_current(self, fp, exe, codex_home, cwd, env=None, overrides=()):
        """Whether the configuration files a cached fingerprint hashed are unchanged and no new layer file appeared: the layer
        stack is re-derived from disk (the registry's own rule for which files count) and every file re-hashed. An edit,
        a removal or a new file forces re-discovery (design 3.3)."""
        try:  # the cached answer's values name the profile and the referenced catalog file; the layer files come from disk
            run_env, global_args = self.discovery_env(codex_home, overrides, env)
            _cfg, files, refs = self.mod.codex_catalog_config(str(exe), codex_home=str(codex_home), cwd=str(cwd), env=run_env, keys=self.keys(),
                                                             result={"config": dict(fp.get("catalog_config") or {}), "layers": []}, global_args=global_args)
        except Exception:
            return False
        for recorded, now in ((fp.get("config_file_hashes") or {}, files), (fp.get("referenced_hashes") or {}, refs)):
            paths = set(recorded) | {str(p) for p in now}
            for p in paths:
                try:
                    digest = self.mod.sha256_file(p)
                except OSError:
                    digest = "missing"
                if recorded.get(p, "missing") != digest:
                    return False
        return True

    def launch(self, exe, version, codex_home, cwd, config_mode=IGNORE, overrides=(), env=None):
        """The fingerprint of a Codex launch under `codex_home` (design 3.3), cached per process while the configuration files it
        hashed are unchanged (`files_current`: an edit during a wait or between attempts forces re-discovery). For an
        ignore-user-config launch whose user configuration sets a catalog-affecting key, the catalog configuration is
        discovered under a scratch home holding only the account's auth.json; the fingerprint keeps the account's home and
        configuration files, so an edit to them still forces re-discovery. With no auth.json the launch is refused
        (LaunchUnavailable). Returns (fingerprint, scratch_needed): whether a catalog read for this launch must run under such a
        scratch home."""
        key = (str(exe), str(version), str(codex_home), str(cwd), config_mode, tuple(overrides), provider_env_digest(env if env is not None else os.environ))
        cached = self._launches.get(key)
        if cached is not None:
            if self.files_current(cached[0], exe, codex_home, cwd, env, overrides):
                return cached
            self._launches.pop(key, None)
        try:
            fp = self._fingerprint(exe, version, codex_home, cwd, config_mode, overrides, env)
        except Exception as exc:
            raise LaunchUnavailable("could not read the effective Codex configuration under %s (%s)" % (codex_home, diagnostic(exc)))
        scratch_needed = False
        if config_mode == IGNORE and not self.equivalent(fp, overrides):
            set_keys = [k for k, v in (fp.get("catalog_config") or {}).items() if v not in (None, "", [], {}, False)]
            auth = Path(str(codex_home)) / "auth.json"
            if not auth.is_file():
                raise LaunchUnavailable("the user configuration under %s sets %s and it has no auth.json for a scratch-home discovery of what an "
                                        "ignore-user-config launch sees" % (codex_home, ", ".join(set_keys) or "a catalog-affecting key"))
            scratch = self.mod.scratch_codex_home(self.reg.state.home, auth)
            scratch_path = scratch.__enter__()
            try:
                fp2 = self._fingerprint(exe, version, codex_home, cwd, IGNORE, overrides, env, discovery_home=scratch_path)
            except Exception as exc:
                raise LaunchUnavailable("the scratch-home discovery under %s failed (%s)" % (codex_home, diagnostic(exc)))
            finally:
                scratch.__exit__(None, None, None)
            if not self.equivalent(fp2, overrides):
                raise LaunchUnavailable("the scratch-home discovery under %s still reports a catalog-affecting key; nothing proves what an "
                                        "ignore-user-config launch sees" % codex_home)
            fp, scratch_needed = fp2, True
        self._launches[key] = (fp, scratch_needed)
        return fp, scratch_needed

    def snapshot_usable(self, label, launch):
        return self.reg.snapshot_usable("codex", label, launch=launch)

    def refresh(self, label, exe, version, codex_home, cwd, config_mode=IGNORE, overrides=(), env=None, force=False):
        """Make `label`'s catalog snapshot consumable by a launch under `codex_home` (design 3.3 step 1): compute the launch
        fingerprint; when the stored snapshot is fresh, of the current identity generation and compatible (and no `force`),
        nothing is read; otherwise the catalog is read through the app-server under the account's home (or a scratch home)
        and stored bound to the fingerprint, then the registry's promotion pass runs over it. Returns
        (fingerprint or None, reason or None, read: bool). A failed read excludes the account and names the reason."""
        try:
            fp, scratch_needed = self.launch(exe, version, codex_home, cwd, config_mode, overrides, env)
        except LaunchUnavailable as exc:
            return None, "%s: %s" % (label, exc), False
        mod, reg = self.mod, self.reg
        verdict = reg.snapshot_usable("codex", label, launch=fp)
        if verdict["usable"] and not force:
            return fp, None, False
        scratch = None
        scratch_path = None
        if scratch_needed:
            scratch = mod.scratch_codex_home(reg.state.home, Path(str(codex_home)) / "auth.json")
            scratch_path = scratch.__enter__()
        try:
            run_env, global_args = self.discovery_env(codex_home, overrides, env, scratch_path)

            def reader(_label, _launch):
                return mod.codex_model_list(str(exe), env=run_env, global_args=global_args, cwd=str(cwd))
            try:
                reg.refresh_if_due(vendor="codex", reader=reader, accounts=[label], launch=fp, force=True, require=True)
            except Exception as exc:
                return fp, "%s: catalog read failed under the launch configuration (%s)" % (label, diagnostic(exc)), True
            return fp, None, True
        finally:
            if scratch is not None:
                scratch.__exit__(None, None, None)

    def capable(self, head, candidates, effort=None, fast=False):
        """The capable set (design 3.3 step 2): of `candidates` ([(label, fingerprint)]) the accounts whose usable snapshot lists
        `head`, not hidden, with `effort` among its efforts (when the catalog lists any) and, with `fast`, the fast speed tier
        advertised. Returns (labels, notes naming every excluded account and why, whether any account lists the head at all,
        whether an account listing the head lacked only the effort: the seat's effort preference list applies then)."""
        mod, reg = self.mod, self.reg
        capable, notes, listed, effort_short = [], [], False, False
        for label, launch in candidates:
            if launch is None:
                notes.append("%s: no launch fingerprint" % label)
                continue
            verdict = reg.snapshot_usable("codex", label, launch=launch)
            if not verdict["usable"]:
                notes.append(clean_reason(verdict["reason"]))  # a failed read's stored error is server text: only its diagnosis is repeated
                continue
            entry, unreadable = None, False
            for e in verdict["record"].get("data") or []:
                try:
                    ce = mod.catalog_entry(e)
                except Exception:  # an entry the reader cannot normalize (a field of the wrong type) confirms nothing
                    unreadable = True
                    continue
                if ce["id"] == head:
                    entry = ce
                    break
            if entry is None:
                notes.append("%s: %s not listed%s" % (label, head, " (an entry of the catalog could not be read)" if unreadable else ""))
                continue
            listed = True
            if entry["hidden"]:
                notes.append("%s: %s is hidden" % (label, head))
                continue
            if effort and entry["efforts"] and effort not in entry["efforts"]:
                notes.append("%s: %s does not offer effort %s (%s)" % (label, head, effort, ", ".join(entry["efforts"])))
                effort_short = True
                continue
            if fast and "fast" not in entry["speed_tiers"]:  # the registry's rule: a fast seat needs the tier advertised (design 3.3)
                notes.append("%s: %s has no fast speed tier (%s)" % (label, head, ", ".join(entry["speed_tiers"]) or "none advertised"))
                continue
            capable.append(label)
        return capable, notes, listed, effort_short

    def check_launch(self, label, head, launch):
        """Right before the launch (design 3.3 step 4): the picked account's snapshot is fresh, compatible, of the current
        identity generation and lists the head not hidden. {"ok", "reason"}."""
        if launch is None:
            return {"ok": False, "reason": "%s: no launch fingerprint" % label}
        out = self.reg.check_launch(label, head, launch=launch)
        if not out.get("ok"):
            out = dict(out, reason=clean_reason(out.get("reason")))
        return out

    # ---------------------------------------------------------------- provenance (design 6)
    def find_rollout(self, home, thread_id):
        return self.mod.find_rollout(home, thread_id) if home and thread_id else None

    @staticmethod
    def born_at(path):
        """When a rollout file was created (the file system's birth time where it keeps one, else the last modification)."""
        try:
            st = Path(str(path)).stat()
        except OSError:
            return None
        return getattr(st, "st_birthtime", None) or st.st_mtime

    @staticmethod
    def rollouts(home, thread_id):
        """Every rollout file of a thread under `home`, oldest first by creation time (a thread id the CLI reuses across runs, as
        the test stub does, has several)."""
        if not home or not thread_id:
            return []
        base = Path(str(home)).expanduser() / "sessions"
        if not base.is_dir():
            return []
        hits = list(base.glob("*/*/*/rollout-*-%s.jsonl" % thread_id)) or list(base.rglob("rollout-*-%s.jsonl" % thread_id))
        return sorted(hits, key=lambda p: Link.born_at(p) or 0.0)

    def read_events(self, path):
        try:
            return self.mod.read_jsonl(str(path))[0]
        except Exception:
            return []

    def codex_attempt(self, home, thread_id, attempt_id, cursor_start, cursor_end, events_path, requested, expected, evidence, exit_status=None,
                      thread_ref=None, start_ts=None, end_ts=None, effort=None, rollout_path=None):
        """One Codex attempt's records from its own window of the rollout file (design 6): {"records", "errors", "substituted", ...}."""
        return self.reg.provenance_codex(home, thread_id, attempt_id, cursor_start, cursor_end, events_path=str(events_path) if events_path else None,
                                         model_requested=requested, model_expected=expected, expected_evidence=evidence, exit_status=exit_status,
                                         thread_ref=thread_ref, start_ts=start_ts, end_ts=end_ts, rollout_path=str(rollout_path) if rollout_path else None,
                                         strict=False, effort_requested=effort, source="bridge")

    def codex_no_thread(self, attempt_id, requested, expected, evidence, exit_status, outcome, effort=None, thread_ref=None):
        """The records of a Codex attempt that started no thread (relay refused it, the CLI rejected the launch before any turn): a
        null observation, never a guess."""
        base = {"attempt_id": attempt_id, "thread_id": None, "model_requested": requested, "model_expected": expected, "expected_evidence": evidence,
                "thread_ref": thread_ref, "source": "bridge"}
        return [dict(base, kind="start", observed_model_id=None, observed_via="rollout:none-in-attempt", effort=effort, cursor_source="none"),
                dict(base, kind="complete", observed_model_id=None, observed_via="rollout:none-in-attempt", observed_models=[], turn_contexts=0,
                     turn_completed=False, outcome=outcome, substitution=None, substitutions=[], substituted=False, exit_status=exit_status,
                     effort_requested=effort, errors=[], registry_path=str(self.reg.path), registry_sha256=self.reg.sha256)]

    def codex_ambiguous(self, attempt_id, thread_id, count, requested, expected, evidence, exit_status, outcome, effort=None, thread_ref=None):
        """The records of a Codex attempt whose rollout file cannot be told apart: several files of its thread were created during
        the attempt (a thread id shared by concurrent runs, which the CLI's own ids never are). A collection error, never a guess
        at another run's turns (design 6)."""
        base = {"attempt_id": attempt_id, "thread_id": thread_id, "model_requested": requested, "model_expected": expected, "expected_evidence": evidence,
                "thread_ref": thread_ref, "source": "bridge"}
        err = {"kind": "rollout:ambiguous", "detail": "%d rollout files of thread %s were created during the attempt" % (count, thread_id)}
        return [dict(base, kind="start", observed_model_id=None, observed_via="rollout:ambiguous", effort=effort, cursor_source="new-thread", rollout_path=None),
                dict(base, kind="complete", observed_model_id=None, observed_via="rollout:ambiguous", observed_models=[], turn_contexts=0,
                     turn_completed=False, outcome=outcome, substitution=None, substitutions=[], substituted=False, exit_status=exit_status,
                     effort_requested=effort, errors=[err], registry_path=str(self.reg.path), registry_sha256=self.reg.sha256)]

    def claude_attempt(self, events, attempt_id, requested, expected, evidence, exit_status=None):
        """One Claude attempt's records from its own stream (design 6)."""
        return self.reg.claude_attempt_records(events, attempt_id, requested, expected, expected_evidence=evidence, exit_status=exit_status, source="bridge")

    def classify(self, observed, expected):
        return self.reg.classify_substitution(observed, expected)

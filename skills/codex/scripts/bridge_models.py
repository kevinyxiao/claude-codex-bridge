#!/usr/bin/env python3
"""
bridge_models.py: the model table (skills/codex/models.json) and its one reader.

A seat name means "the newest model in its family". After normalization a name is accepted only if it is a family name
(astra, sol, terra, luna, fable, opus, sonnet, haiku), a family head ID, a listed equivalent spelling of a head, or the
single K1 seat (`opus-5`) while that seat is switched on; everything else is refused in one line naming a concrete
replacement. Efforts come from the table. The launch validator (`validate`) checks local facts only: table membership,
the sticky lifecycle markers of the evidence store, the effort, and the parsed CLI version against the model's floor.
The check's pure evaluation (`evaluate`) turns the sources doctor reads (the local Codex catalog, npm, Anthropic's two
pages) plus the current local facts into findings, markers and a verdict; reading the sources, the evidence lock and
doctor itself live in bridge.py.

The API takes its paths explicitly: `Table.load(path)` and `Evidence(path)` never read BRIDGE_MODELS_FILE or
BRIDGE_MODELS_EVIDENCE. bridge.py supplies the defaults; triad binds both to the locations its job recorded.
"""
import copy
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

VENDORS = ("codex", "claude")
VENDOR_NAMES = {"codex": "Codex", "claude": "Claude"}
BUILTIN_DEFAULT_EFFORT = {"codex": "ultra", "claude": "max"}  # lowered to the model's highest supported level
TABLE_NAME = "skills/codex/models.json"  # how refusals name the table
MARKER_KINDS = ("retired", "retiring", "deprecated")  # most severe first
FAIL_CLASSES = ("lifecycle", "missing", "stale", "local", "cli", "host", "defaults")  # L3: a CLI older than npm latest is `unverified`
DEFAULT_SEAT_VARS = ("BRIDGE_DEFAULT_SEAT", "BRIDGE_REVIEW_SEATS", "BRIDGE_COUNCIL_SEATS")
# the default list each variable governs, as doctor and the `defaults` finding name a built-in one (S-DEF: "seat (built-in default)")
DEFAULT_LIST_NAMES = {"BRIDGE_DEFAULT_SEAT": "seat", "BRIDGE_REVIEW_SEATS": "review", "BRIDGE_COUNCIL_SEATS": "council"}
HOST_OVERRIDE_VARS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                      "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")


class TableError(Exception):
    """A missing, unreadable, malformed or invariant-violating table; str() is ONE line naming the file and the problem."""


class Refusal(Exception):
    """A seat that must not run. str() is the one-line reason without the `bridge: ` prefix; `complete` says whether it is a
    whole line (the validator's floor and version lines) or the part after `refused seat '<name>': ` (resolution)."""

    def __init__(self, text, complete=False):
        super().__init__(text)
        self.complete = complete


def _one_line(value):
    return re.sub(r"\s+", " ", str(value)).strip()


# ============================================================================ versions
FLOOR_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
MIN_CLI_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9a-z.]+))?$")  # an auto-promoted model's floor: the listing CLI's exact version
VERSION_RE = {"codex": re.compile(r"^codex(?:-cli)?\s+v?(\d+)\.(\d+)\.(\d+)(?:-([0-9a-z.]+))?"),
              "claude": re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9a-z.]+))?\s+\(Claude Code\)")}


class Version:
    """A parsed CLI version. A prerelease sorts below its release: 0.155.0-alpha < 0.155.0 < 0.156.1."""

    def __init__(self, major, minor, patch, pre=None):
        self.parts = (int(major), int(minor), int(patch))
        self.pre = pre or None
        pre_key = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.pre.split(".")) if self.pre else ()
        self.key = self.parts + ((1, ()) if self.pre is None else (0, pre_key))

    @property
    def text(self):
        return ".".join(str(p) for p in self.parts) + (f"-{self.pre}" if self.pre else "")

    def __ge__(self, other):
        return self.key >= other.key

    def __lt__(self, other):
        return self.key < other.key

    def __repr__(self):
        return f"Version({self.text})"


def first_line(text):
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def parse_version(vendor, text):
    """The Version on the first line of `<cli> --version` output (`codex-cli 0.156.1`, `2.1.282 (Claude Code)`), or None."""
    m = VERSION_RE[vendor].match(first_line(text))
    return Version(*m.groups()) if m else None


def parse_floor(text):
    m = FLOOR_RE.match(str(text).strip()) if isinstance(text, str) else None
    return Version(*m.groups()) if m else None


def parse_min_cli(text):
    """A model's floor (`min_cli`) as a Version: the shipped table's X.Y.Z, or an automatically promoted model's floor, the exact
    version of the CLI that listed it, a prerelease included (the ChatGPT app's bundled Codex is one, e.g. 0.166.0-alpha.6)."""
    m = MIN_CLI_RE.match(str(text).strip()) if isinstance(text, str) else None
    return Version(*m.groups()) if m else None


def _generation_key(text):
    """A model's `generation` ("6", "6.1", "5.6") as a comparable tuple; a non-numeric part sorts after every number."""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in str(text).split("."))


# ============================================================================ the seat grammar
def normalize(name):
    """(forced vendor or None, normalized name): lowercase, trimmed, a trailing `[1m]` dropped, `codex/` or `claude/` taken off."""
    s = str(name or "").strip().lower()
    if s.endswith("[1m]"):
        s = s[:-4].strip()
    for vendor in VENDORS:
        if s.startswith(vendor + "/"):
            return vendor, s[len(vendor) + 1:].strip()
    return None, s


def parse_spec(spec):
    """`<name>[:<effort>][@fast]` -> (name as typed, effort text or None, [modifiers]); bridge.py's seat grammar."""
    parts = str(spec).strip().split("@")
    name, _, effort = parts[0].partition(":")
    return name.strip(), (effort.strip() or None), [p.strip().lower() for p in parts[1:]]


class Resolved:
    """One seat name resolved (DESIGN §2): the vendor, the full model ID, its family, the seat tag (the family, or the K1
    seat's own name) and whether it is the K1 seat."""

    __slots__ = ("vendor", "model", "family", "tag", "k1")

    def __init__(self, vendor, model, family, tag, k1=False):
        self.vendor, self.model, self.family, self.tag, self.k1 = vendor, model, family, tag, k1

    def __repr__(self):
        return f"Resolved({self.vendor}/{self.model} family={self.family} tag={self.tag}{' K1' if self.k1 else ''})"


class SeatSpec(Resolved):
    """A seat spec parsed and resolved by Table.parse_seat: Resolved plus the effort to run (None: no effort flag), `fast`,
    whether the effort was explicit, the seat name as typed (normalized), and the seat's explicit preference lists
    (docs/model-routing.md 5): `preference`, the Resolved families after the first in `astra|sol`, the only models the seat may
    descend to once no capable account is left; `effort_preference`, the efforts after the first in `ultra|max`, likewise."""

    __slots__ = ("effort", "fast", "explicit", "seat_name", "preference", "effort_preference")

    def __init__(self, resolved, effort, fast, explicit, seat_name, preference=(), effort_preference=()):
        super().__init__(resolved.vendor, resolved.model, resolved.family, resolved.tag, resolved.k1)
        self.effort, self.fast, self.explicit, self.seat_name = effort, fast, explicit, seat_name
        self.preference, self.effort_preference = list(preference), list(effort_preference)


# ============================================================================ the table
class Table:
    """The loaded table. `Table.load(path)` raises TableError (one line) for a missing, malformed or invariant-violating file.
    `load_table` (below) is what the engine and triad read: this table with this Mac's automatic promotions applied
    (`effective_table`); `base` is the shipped table it was built from (itself for a shipped table), `promotion` reports what the
    ledger changed, and `blocked` holds the models an undone promotion blocked on this Mac (blocked_reason says why)."""

    def __init__(self, data, path=None, raw=b"", sha256=None, effective=False):
        self.path = str(path) if path is not None else None
        self.data = data
        self.sha256 = sha256 or hashlib.sha256(raw).hexdigest()
        self.base = self
        self.promotion = {"applied": [], "skipped": [], "ignored": [], "ledger": None}
        self.blocked = {}
        self._check(effective)

    @classmethod
    def load(cls, path):
        path = str(path)
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise TableError(f"model table {path}: {exc.strerror or _one_line(exc)}") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TableError(f"model table {path} is not valid JSON: {_one_line(exc)}") from None
        if not isinstance(data, dict):
            raise TableError(f"model table {path} is not a JSON object")
        return cls(data, path, raw)

    # ---------------------------------------------------------------- sections
    @property
    def policy(self):
        return self.data.get("policy") or {}

    @property
    def catalog_config_keys(self):
        return [k for k in self.data.get("catalog_config_keys") or [] if isinstance(k, str)]

    @property
    def efforts(self):
        return self.data["efforts"]

    @property
    def families(self):
        return self.data["families"]

    @property
    def models(self):
        return self.data["models"]

    @property
    def fallback(self):
        return self.data["fallback"]

    @property
    def effort_fallback(self):
        return self.data.get("effort_fallback") or {}

    @property
    def k1(self):
        return self.data["k1_seat"]

    @property
    def retired(self):
        return self.data["retired"]

    @property
    def refused(self):
        return self.data["refused"]

    @property
    def history(self):
        return self.data.get("history") or []

    # ---------------------------------------------------------------- invariants (DESIGN §1)
    def _check(self, effective=False):
        """The invariants of DESIGN §1. `effective`: a table built by effective_table, the only one whose model records may be
        `auto_promoted` (their floor is the listing CLI's exact version, a prerelease allowed); a shipped table never carries one, so
        its floors keep the X.Y.Z grammar bootstrap.sh reads."""
        d = self.data
        where = f"model table {self.path}" if self.path else "model table"

        def fail(msg):
            raise TableError(f"{where}: {msg}")

        for key, typ in (("policy", dict), ("catalog_config_keys", list), ("efforts", dict), ("families", dict), ("models", dict),
                         ("fallback", dict), ("effort_fallback", dict), ("k1_seat", dict), ("retired", dict), ("refused", dict),
                         ("history", list)):
            if not isinstance(d.get(key), typ):
                fail(f"'{key}' must be a JSON {'array' if typ is list else 'object'}")
        for vendor in VENDORS:
            vocab = d["efforts"].get(vendor)
            if not isinstance(vocab, list) or not vocab or any(not isinstance(e, str) for e in vocab):
                fail(f"'efforts.{vendor}' must list the {VENDOR_NAMES[vendor]} effort vocabulary")
            for key in ("families", "fallback", "retired", "refused"):
                if not isinstance(d[key].get(vendor), dict):
                    fail(f"'{key}.{vendor}' must be a JSON object")
        heads = {}  # head id -> (vendor, family)
        family_vendor = {}
        for vendor in VENDORS:
            for fam, rec in d["families"][vendor].items():
                if fam in family_vendor:
                    fail(f"family {fam} is listed under both vendors")
                family_vendor[fam] = vendor
                head = rec.get("known_good") if isinstance(rec, dict) else None
                if not isinstance(head, str) or head not in d["models"]:
                    fail(f"family {fam}: head {head!r} is not in 'models'")
                if not isinstance(d["models"][head], dict):
                    fail(f"model {head}: not a JSON object")
                if d["models"][head].get("family") != fam:
                    fail(f"head {head} is recorded under family {d['models'][head].get('family')!r}, not {fam}")
                if head in heads:
                    fail(f"head {head} heads two families")
                heads[head] = (vendor, fam)
        aliases = {}
        for model_id, rec in d["models"].items():
            if not isinstance(rec, dict):
                fail(f"model {model_id}: not a JSON object")
            fam = rec.get("family")
            if fam not in family_vendor:
                fail(f"model {model_id}: family {fam!r} is not a table family")
            if not isinstance(rec.get("generation"), str):
                fail(f"model {model_id}: 'generation' must be a string")
            if "auto_promoted" in rec and not effective:
                fail(f"model {model_id}: 'auto_promoted' records come only from this Mac's promotions ledger, never from the table file")
            floor = rec.get("min_cli")
            if floor is not None and (parse_min_cli(floor) if rec.get("auto_promoted") else parse_floor(floor)) is None:
                fail(f"model {model_id}: 'min_cli' must be an X.Y.Z string or null")
            vocab = d["efforts"][family_vendor[fam]]
            if not isinstance(rec.get("efforts"), list) or any(e not in vocab for e in rec["efforts"]):
                fail(f"model {model_id}: 'efforts' must list {VENDOR_NAMES[family_vendor[fam]]} efforts ({', '.join(vocab)})")
            if not isinstance(rec.get("aliases", []), list) or any(not isinstance(a, str) for a in rec.get("aliases", [])):
                fail(f"model {model_id}: 'aliases' must be a list of strings")
            for alias in rec.get("aliases", []):
                if alias in d["models"] or alias in aliases:
                    fail(f"model {model_id}: alias {alias} names another model")
                if model_id not in heads:
                    predecessors = d["retired"][family_vendor[fam]].get(fam, [])
                    if not isinstance(predecessors, list) or model_id not in predecessors:
                        fail(f"model {model_id}: alias {alias} is listed on a model that is neither its family's head nor a declared predecessor")
                aliases[alias] = model_id
        for vendor in VENDORS:
            for src, dst in d["fallback"][vendor].items():
                edge = f"fallback {src} -> {dst}"
                if heads.get(src, (None, None))[0] != vendor:
                    fail(f"{edge}: the source is not a current {VENDOR_NAMES[vendor]} head")
                if not isinstance(dst, str) or heads.get(dst, (None, None))[0] != vendor:
                    fail(f"{edge}: the target is not a current {VENDOR_NAMES[vendor]} head")
                g1, g2 = d["models"][src]["generation"], d["models"][dst]["generation"]
                if _generation_key(g2) < _generation_key(g1):  # a suggestion never descends to an older generation (6 -> 6.1 is not a descent)
                    fail(f"{edge} joins generations {g1} and {g2}")
            ladder = d["effort_fallback"].get(vendor) or {}
            if not isinstance(ladder, dict) or any(a not in d["efforts"][vendor] or b not in d["efforts"][vendor] for a, b in ladder.items()):
                fail(f"'effort_fallback.{vendor}' must map {VENDOR_NAMES[vendor]} efforts to {VENDOR_NAMES[vendor]} efforts")
        taken = set(heads) | set(aliases)  # retired and refused names never collide with heads or listed spellings
        for vendor in VENDORS:
            for fam, ids in d["retired"][vendor].items():
                if family_vendor.get(fam) != vendor:
                    fail(f"retired.{vendor}.{fam}: not a {VENDOR_NAMES[vendor]} family")
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                    fail(f"retired.{vendor}.{fam}: must be a list of model IDs")
                for old in ids:
                    if old in taken:
                        fail(f"retired {old} is a current head or a listed spelling of one")
            for key, rec in d["refused"][vendor].items():
                if key in taken:
                    fail(f"refused {key} is a current head or a listed spelling of one")
                self._refusal_target(vendor, key, rec, fail)
        k1 = d["k1_seat"]
        if not isinstance(k1.get("seat"), str) or not k1["seat"] or not isinstance(k1.get("id"), str) or not isinstance(k1.get("enabled"), bool):
            fail("k1_seat needs a 'seat' name, an 'id' and a boolean 'enabled'")
        if k1["id"] not in d["models"]:
            fail(f"k1_seat id {k1['id']} is not in 'models'")
        if k1["id"] in heads:
            fail(f"k1_seat id {k1['id']} is a current head, not a retired predecessor")
        k1_fam = d["models"][k1["id"]]["family"]
        if k1["id"] not in (d["retired"][family_vendor[k1_fam]].get(k1_fam) or []):
            fail(f"k1_seat id {k1['id']} is not a retired predecessor of {k1_fam}")
        if k1["seat"] in taken or k1["seat"] in family_vendor:
            fail(f"k1_seat name {k1['seat']} collides with a family, a head or a listed spelling")
        self._heads, self._family_vendor, self._aliases = heads, family_vendor, aliases

    def _refusal_target(self, vendor, key, rec, fail):
        """The record a refused name resolves to (a single-level `same_as` reference to a record of the same vendor)."""
        recs = self.data["refused"][vendor]
        if not isinstance(rec, dict):
            fail(f"refused {key}: not a JSON object")
        if "same_as" in rec:
            target = recs.get(rec["same_as"]) if isinstance(rec["same_as"], str) else None
            if not isinstance(target, dict):
                fail(f"refused {key}: same_as {rec.get('same_as')!r} names no {VENDOR_NAMES[vendor]} refusal record")
            if "same_as" in target:
                fail(f"refused {key}: same_as {rec['same_as']} is itself a same_as reference (one level only)")
            rec = target
        if rec.get("family") not in self.data["families"][vendor]:
            fail(f"refused {key}: family {rec.get('family')!r} is not a {VENDOR_NAMES[vendor]} family")
        if not isinstance(rec.get("reason"), str) or not rec["reason"].strip():
            fail(f"refused {key}: no reason")
        if "id" in rec and not isinstance(rec["id"], str):
            fail(f"refused {key}: 'id' must be a string")
        return rec

    # ---------------------------------------------------------------- families, heads, members
    def family_names(self, vendor=None):
        return [f for v in VENDORS if vendor in (None, v) for f in self.families[v]]

    def vendor_of_family(self, family):
        return self._family_vendor.get(family)

    def head(self, family):
        return self.families[self._family_vendor[family]][family]["known_good"]

    def heads(self, vendor=None):
        return [self.head(f) for f in self.family_names(vendor)]

    def is_head(self, model_id):
        return model_id in self._heads

    def vendor_of_model(self, model_id):
        rec = self.models.get(model_id)
        return self._family_vendor.get(rec["family"]) if rec else None

    def family_of(self, model_id):
        """The family of a known member (a head, a listed spelling of one, a retired predecessor, the K1 target) or of a model whose
        automatic promotion was undone on this Mac; None otherwise."""
        if model_id in self._heads:
            return self._heads[model_id][1]
        if model_id in self._aliases:
            return self.models[self._aliases[model_id]]["family"]
        for vendor in VENDORS:
            for fam, ids in self.retired[vendor].items():
                if model_id in ids:
                    return fam
        if model_id in self.blocked:
            return self.blocked[model_id]["family"]
        return None

    def promotion_step(self, model_id):
        """The applied automatic promotion step that made `model_id` a head on this Mac, or None (a shipped head, or no head)."""
        return next((s for s in self.promotion["applied"] if s["to"] == model_id), None)

    def head_evidence(self, family):
        """What a seat's expectation says of the family's head: the shipped table's known_good, or this Mac's automatic promotion."""
        base = f"bridge table v{self.data.get('version')} ({self.data.get('updated')}) known_good"
        step = self.promotion_step(self.head(family)) if family in self._family_vendor else None
        if step is None:
            return base
        return (f"{base} {self.base.head(family)}, promoted automatically on {_date_text(step['at'])} to {step['to']} "
                f"(this Mac's Codex catalog, {step.get('cli') or 'codex'})")

    def canonical(self, name):
        """The canonical model ID of any spelling the table accepts for a model: case and the `[1m]` modifier normalized, a listed
        equivalent spelling mapped to its model. Lifecycle evidence, markers and relevant targets are keyed by it. Any other
        string comes back normalized and otherwise unchanged (it names no table model)."""
        _vendor, key = normalize(name)
        if key in self.models:
            return key
        return self._aliases.get(key, key)

    def known_members(self, vendor, family):
        head = self.head(family)
        members = {head} | set(self.retired[vendor].get(family) or [])
        return members | {alias for model_id in members for alias in self.models.get(model_id, {}).get("aliases", [])}

    def family_matching(self, vendor, model_id):
        """The family whose `id_glob` (Codex) or `id_prefix` (Claude) a live ID matches; None when none does."""
        for fam, rec in self.families[vendor].items():
            if rec.get("id_glob") and fnmatch.fnmatchcase(model_id, rec["id_glob"]):
                return fam
            if rec.get("id_prefix") and model_id.startswith(rec["id_prefix"]):
                return fam
        return None

    def k1_enabled(self):
        return bool(self.k1.get("enabled"))

    def relevant_targets(self):
        """The current heads, the reachable fallback targets, and the K1 target only while the seat is on (DESIGN §1)."""
        targets = set(self._heads)
        for vendor in VENDORS:
            targets.update(self.canonical(t) for t in self.fallback[vendor].values())
        if self.k1_enabled():
            targets.add(self.canonical(self.k1["id"]))
        return targets

    def is_selectable(self, model_id):
        return model_id in self._heads or (self.k1_enabled() and model_id == self.k1["id"])

    def floor(self, model_id):
        return (self.models.get(model_id) or {}).get("min_cli")

    def highest_floor(self, vendor):
        """The highest non-null `min_cli` of the vendor's models (a null floor is excluded), as its X.Y.Z string."""
        floors = [rec["min_cli"] for rec in self.models.values() if rec.get("min_cli") and self._family_vendor[rec["family"]] == vendor]
        return max(floors, key=lambda f: parse_min_cli(f).key) if floors else None

    def supported_efforts(self, model_id):
        return list(self.models[model_id].get("efforts") or [])

    def highest_effort(self, model_id):
        supported = self.supported_efforts(model_id)
        vocab = self.efforts[self.vendor_of_model(model_id)]
        return max(supported, key=vocab.index) if supported else None

    def next_fallback(self, model_id):
        vendor = self.vendor_of_model(model_id)
        return self.fallback[vendor].get(model_id) if vendor else None

    def next_effort(self, vendor, effort, model_id=None):
        """The next lower effort of the vendor's ladder (ultra -> max -> xhigh), capped by the model's table set; None at the end."""
        ladder = self.effort_fallback.get(vendor) or {}
        supported = self.supported_efforts(model_id) if model_id else self.efforts[vendor]
        seen = set()
        while effort in ladder and effort not in seen:
            seen.add(effort)
            effort = ladder[effort]
            if effort in supported:
                return effort
        return None

    # ---------------------------------------------------------------- resolution (DESIGN §2)
    def families_hint(self):
        fams = self.family_names()
        return f"Use one of {fams[0]} ({self.head(fams[0])}), " + ", ".join(fams[1:]) + "."

    def refusal(self, vendor, name):
        """The refusal record a name hits (exact, `*` pattern, or through `same_as`), or None."""
        recs = self.refused[vendor]
        rec = recs.get(name)
        if rec is None:
            for pattern, candidate in recs.items():
                if "*" in pattern and fnmatch.fnmatchcase(name, pattern):
                    rec = candidate
                    break
        if rec is None:
            return None
        return recs[rec["same_as"]] if "same_as" in rec else rec

    def _disagrees(self, forced, vendor, name, family):
        head = self.head(family)
        raise Refusal(f"{name} is a {VENDOR_NAMES[vendor]} {'seat' if name == self.k1['seat'] else 'name'}; {forced}/ selects "
                      f"{VENDOR_NAMES[forced]}. Use {family} ({head}).")

    def resolve(self, name):
        """A seat name (as typed) -> Resolved, or Refusal(reason). The first matching rule decides, in the order of DESIGN §2."""
        forced, key = normalize(name)
        k1 = self.k1
        if key == k1["seat"]:  # rule 1: the K1 seat, a switch
            fam = self.models[k1["id"]]["family"]
            vendor = self._family_vendor[fam]
            if forced and forced != vendor:
                self._disagrees(forced, vendor, key, fam)
            if not k1.get("enabled"):
                raise Refusal(f"the {key} seat is switched off (models.json k1_seat; Kevin K1). Use {fam} ({self.head(fam)}).")
            return Resolved(vendor, k1["id"], fam, key, True)
        for vendor in ([forced] if forced else VENDORS):  # rule 2: refused names
            rec = self.refusal(vendor, key)
            if rec:
                raise Refusal(f"{rec['reason']}. Use {rec['family']} ({self.head(rec['family'])}).")
        if key in self._family_vendor:  # rule 3: a family name
            vendor = self._family_vendor[key]
            if forced and forced != vendor:
                self._disagrees(forced, vendor, key, key)
            return Resolved(vendor, self.head(key), key, key)
        canonical = self.canonical(key)
        head = canonical if canonical in self._heads else None  # rule 4: a head, or a listed spelling of one
        if head:
            vendor, fam = self._heads[head]
            if forced and forced != vendor:
                self._disagrees(forced, vendor, key, fam)
            return Resolved(vendor, head, fam, fam)
        for vendor in VENDORS:  # rule 5: a retired predecessor
            for fam, ids in self.retired[vendor].items():
                if canonical in ids:
                    head = self.head(fam)
                    raise Refusal(f"{key} is superseded by {head}. Use {fam} ({head}).")
        if canonical in self.blocked and (not forced or forced == "codex"):  # blocked by an automatic promotion undone on this Mac
            rec = self.blocked[canonical]
            raise Refusal(f"{key} is blocked on this Mac: {blocked_reason(rec)}. Use {rec['family']} ({self.head(rec['family'])}).")
        for vendor in ([forced] if forced else VENDORS):  # a name shaped like one family's IDs (id_glob, id_prefix): that family's head
            fam = self.family_matching(vendor, key)
            if fam:
                raise Refusal(f"'{key}' is not in the model table ({TABLE_NAME}). Use {fam} ({self.head(fam)}).")
        raise Refusal(f"'{key}' is not in the model table ({TABLE_NAME}). {self.families_hint()}")  # rule 6

    def resolve_effort(self, vendor, model, family, effort, explicit=True):
        """DESIGN §2 "Efforts". `effort`: the text typed on the seat, given with --effort or set in BRIDGE_*_EFFORT (explicit,
        validated like typed input) or None (the built-in default, lowered to the model's highest level). Returns the effort
        to run, or None for a model with an empty set (no --effort flag)."""
        vocab = self.efforts[vendor]
        supported = self.supported_efforts(model)
        if not explicit or effort is None:
            default = BUILTIN_DEFAULT_EFFORT[vendor]
            return default if default in supported else self.highest_effort(model)
        e = str(effort).strip().lower()
        if e not in vocab:
            if e in ("extra-high", "extrahigh", "extra_high"):
                hint = "say xhigh"
            elif e == "ultra" and "max" in vocab:
                hint = "say max"
            else:
                hint = "say one of " + ", ".join(vocab)
            raise Refusal(f"'{e}' is not a {VENDOR_NAMES[vendor]} effort ({hint})")
        if e not in supported:
            if supported:
                raise Refusal(f"{model} supports {', '.join(supported)}; '{e}' is not available (say {family}:{self.highest_effort(model)})")
            raise Refusal(f"{model} supports no effort level; '{e}' is not available (say {family})")
        return e

    def parse_seat(self, spec, effort_override=None, env_efforts=None, source=None):
        """The seat grammar `<name>[|<name>...][:<effort>[|<effort>...]][@fast]` resolved against the table (DESIGN §2;
        docs/model-routing.md 5): the ONE parser that bridge.py's parse_seats() and the check's `defaults` finding share. The
        first name is the seat's model; the names after a `|` are the seat's explicit preference list, the only models a Codex
        seat may descend to once no capable account is left (a Claude seat is never substituted, so a list is refused for it);
        the efforts after a `|` are the effort preference, the same way. `effort_override` is --effort; `env_efforts` maps a
        vendor to its BRIDGE_*_EFFORT value; `source` names the environment variable a default seat list came from. Returns a
        SeatSpec or raises Refusal with the complete one-line error (`refused seat '<spec>'[ (<source>)]: <reason>`)."""
        spec = str(spec).strip()
        name, effort_text, mods = parse_spec(spec)
        shown = f"{name}:{effort_text}" if effort_text else name
        where = f" ({source})" if source else ""
        if any(m != "fast" for m in mods):
            raise Refusal(f"refused seat '{shown}'{where}: unknown seat modifier in {spec!r} (only @fast is supported)", complete=True)
        fast = "fast" in mods
        names = [n.strip() for n in name.split("|")]
        if any(not n for n in names):
            raise Refusal(f"refused seat '{shown}'{where}: an empty name in the preference list {name!r}", complete=True)
        resolved = []
        for n in names:
            try:
                resolved.append(self.resolve(n))
            except Refusal as exc:
                raise Refusal(f"refused seat '{shown}'{where}: {exc}", complete=True) from None
        r = resolved[0]
        if len(resolved) > 1:
            if any(x.vendor != "codex" for x in resolved):
                raise Refusal(f"refused seat '{shown}'{where}: a preference list (`|`) is a Codex option; a Claude seat is never substituted "
                              f"by the bridge. Use {r.family} ({r.model}).", complete=True)
            seen = []
            for x in resolved:
                if x.model in seen:
                    raise Refusal(f"refused seat '{shown}'{where}: {x.family} ({x.model}) is listed twice in the preference list", complete=True)
                seen.append(x.model)
        efforts_text = [e.strip() for e in effort_text.split("|")] if effort_text else []
        if effort_text and any(not e for e in efforts_text):
            raise Refusal(f"refused seat '{shown}'{where}: an empty effort in the preference list {effort_text!r}", complete=True)
        first_effort = efforts_text[0] if efforts_text else None
        env_effort = (env_efforts or {}).get(r.vendor) or None
        text = first_effort or effort_override or env_effort
        origin = "" if first_effort else (" (--effort)" if effort_override else (f" (BRIDGE_{r.vendor.upper()}_EFFORT)" if text else ""))
        try:
            effort = self.resolve_effort(r.vendor, r.model, r.family, text, explicit=text is not None)
        except Refusal as exc:
            raise Refusal(f"refused seat '{shown}'{where}{origin}: {exc}", complete=True) from None
        effort_preference = []
        for e in efforts_text[1:]:
            if r.vendor != "codex":
                raise Refusal(f"refused seat '{shown}'{where}: an effort preference list (`|`) is a Codex option; a Claude seat runs one effort. "
                              f"Use {r.family}:{effort}.", complete=True)
            try:
                effort_preference.append(self.resolve_effort(r.vendor, r.model, r.family, e, explicit=True))
            except Refusal as exc:
                raise Refusal(f"refused seat '{shown}'{where}: {exc}", complete=True) from None
        if r.vendor == "claude" and fast:
            raise Refusal(f"refused seat '{shown}'{where}: @fast is a Codex option; Claude seats do not support it. Use {r.family} ({r.model}).",
                          complete=True)
        return SeatSpec(r, effort, fast, text is not None, "|".join(normalize(n)[1] for n in names), preference=resolved[1:],
                        effort_preference=effort_preference)

    def preference_hint(self, model, effort=None):
        """How a seat could have opted into the descent the table suggests for `model` (its `fallback` edge, a suggestion the bridge
        never applies on its own: docs/model-routing.md 5): `astra|sol[:effort]`, or None when the table suggests nothing."""
        nxt = self.next_fallback(model)
        if not nxt or nxt not in self.models or model not in self.models:
            return None
        text = f"{self.models[model]['family']}|{self.models[nxt]['family']}"
        return text + (f":{effort}" if effort else "")

    def effort_hint(self, vendor, model, effort):
        """How a seat could have opted into the effort descent the table suggests (its `effort_fallback` ladder): `astra:ultra|max`."""
        nxt = self.next_effort(vendor, effort, model) if effort else None
        if not nxt or model not in self.models:
            return None
        return f"{self.models[model]['family']}:{effort}|{nxt}"

    def parse_seat_list(self, raw, effort_override=None, env_efforts=None, source=None):
        """A comma-separated seat list -> [SeatSpec], each through parse_seat; blanks are skipped and at least one seat is required."""
        specs = [s for s in str(raw if raw is not None else "").split(",") if s.strip()]
        if not specs:
            raise Refusal(f"no seats given{f' ({source})' if source else ''}", complete=True)
        return [self.parse_seat(s, effort_override, env_efforts, source) for s in specs]

    def resolve_recorded(self, record):
        """DESIGN §2 "Reply": the base of a reply is the record's `continuation` when present, else the model the record says
        ran. Returns (Resolved for the head the reply runs on, base model, base effort, note) where `note` is the parenthesised
        reason when the reply moves off the base model, else None. Refusal when the seat is off or the base has no family."""
        cont = record.get("continuation") if isinstance(record.get("continuation"), dict) else {}
        base_model = cont.get("model") or record.get("model")
        base_effort = cont.get("effort") if "effort" in cont else record.get("effort")
        vendor = record.get("vendor")
        k1 = self.k1
        if record.get("seat_name") == k1["seat"]:  # its own recorded identity, never acquired by ID matching
            fam = self.models[k1["id"]]["family"]
            if not k1.get("enabled"):
                raise Refusal(f"the {k1['seat']} seat is switched off (models.json k1_seat; Kevin K1). Use {fam} ({self.head(fam)}).")
            return Resolved(self._family_vendor[fam], k1["id"], fam, k1["seat"], True), base_model, base_effort, None
        if not isinstance(base_model, str) or not base_model:
            raise Refusal(f"the record names no model; start a new seat. {self.families_hint()}")
        key = self.canonical(base_model)  # every spelling the table accepts (case, a trailing [1m], a listed spelling) maps to its ID
        alias = key in self._family_vendor  # old Claude records store the family alias
        fam = key if alias else self.family_of(key)
        if fam is None or (vendor and self._family_vendor[fam] != vendor):
            raise Refusal(f"recorded model {base_model} has no current family in the model table ({TABLE_NAME}); it cannot be continued. "
                          f"Start a new seat: {self.families_hint()[0].lower() + self.families_hint()[1:]}")
        head = self.head(fam)
        if alias:
            note = f"(recorded as the alias '{base_model}'; what ran is not recorded)"
        elif key in self.blocked and head != key:
            note = f"(recorded {base_model} is blocked on this Mac: {blocked_reason(self.blocked[key])})"
        else:
            note = f"(recorded {base_model} is superseded)" if head != key else None
        return Resolved(self._family_vendor[fam], head, fam, record.get("tag") or fam), base_model, base_effort, note


# ============================================================================ the evidence store (read side)
def normalize_markers(raw):
    """{model id: marker record} from the persisted layout (R-T1: records keyed by ID, each with `kind` and `kinds`); a legacy
    list of records is accepted, and a record without `kinds` is read as [kind]."""
    found = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((r.get("id") if isinstance(r, dict) else None, r) for r in raw)
    else:
        items = ()
    for key, rec in items:
        if not isinstance(rec, dict):
            continue
        mid = rec.get("id") or key
        if not mid:
            continue
        rec = dict(rec, id=mid)
        if not rec.get("kinds"):
            rec["kinds"] = [rec["kind"]] if rec.get("kind") else []
        found[mid] = rec
    return found


def canonical_markers(table, raw):
    """{canonical model id: marker record} from the persisted layout: every record keyed by the CANONICAL ID of the spelling it was
    persisted under (a marker recorded under a head's listed spelling is that head's, everywhere it is read: Astra C3-6); records that
    collapse onto one ID keep the most severe kind and the union of kinds and remember their spelling; the withdrawn sequence
    fields of older files are dropped. Without a table, the records as persisted."""
    merged = {}
    for spelling, rec in normalize_markers(raw).items():
        rec = {k: v for k, v in rec.items() if k not in ("read_seq", "seq")}
        canon = table.canonical(spelling) if table is not None else spelling
        rec = dict(rec, id=canon, **({"spelling": spelling} if canon != spelling else {}))
        prev = merged.get(canon)
        if prev is not None:
            kinds = [k for k in MARKER_KINDS if k in set(prev.get("kinds") or []) | set(rec.get("kinds") or [])]
            rec = dict(prev if _severity(prev) <= _severity(rec) else rec, kinds=kinds, kind=kinds[0] if kinds else prev.get("kind"))
        merged[canon] = rec
    return merged


def private_dir(path):
    """The directory, created private (0700) when it does not exist yet; an existing directory is left as it is."""
    p = Path(path)
    if not p.exists():
        os.makedirs(str(p), mode=0o700, exist_ok=True)
        try:
            os.chmod(str(p), 0o700)
        except OSError:
            pass
    return p


class EvidenceBusy(Exception):
    """The evidence lock could not be taken within its deadline (S-C3, A1): the caller goes on from the current snapshot."""


def lock_deadline(env=None):
    """Seconds a doctor or a clear waits for the evidence lock: BRIDGE_MODELS_LOCK_DEADLINE, default 30 (A1)."""
    env = os.environ if env is None else env
    try:
        return max(0.0, float(env.get("BRIDGE_MODELS_LOCK_DEADLINE") or 30))
    except ValueError:
        return 30.0


def fetch_budget(env=None):
    """Seconds ONE refresh may spend on all its source reads together: BRIDGE_MODELS_FETCH_DEADLINE, default 20 (A1)."""
    env = os.environ if env is None else env
    try:
        return max(0.0, float(env.get("BRIDGE_MODELS_FETCH_DEADLINE") or 20))
    except ValueError:
        return 20.0


class Evidence:
    """The evidence store (BRIDGE_MODELS_EVIDENCE, or <jobs root parent>/models-evidence.json), bound to an explicit path. It
    keeps the REMOTE observations of the check and the sticky lifecycle markers the validator consults. One writer at a time
    (S-C3): every write runs under `lock()`, a refresh's source reads included; readers never write."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def markers(self, data=None, table=None):
        """{model id: marker record}, keyed by the canonical ID when a table is given (canonical_markers)."""
        return canonical_markers(table, (self.load() if data is None else data).get("markers"))

    def marker(self, model_id, table=None):
        """The marker on a model: with a table, one recorded under any spelling the table accepts for it."""
        return self.markers(table=table).get(table.canonical(model_id) if table is not None else model_id)

    # ---------------------------------------------------------------- the store's mutations (S-C3): one writer at a time, under the lock
    def lock(self, deadline=None, blocking=True):
        return EvidenceLock(self.path.with_name(self.path.name + ".lock"), deadline, blocking)

    def write(self, data):
        """Atomic replace, private (0600): a reader sees the old or the new snapshot, never a mix."""
        write_private_json(self.path, data)

    @property
    def promotions_path(self):
        """The store's promotions ledger (DECISIONS A28), written under the same lock."""
        return promotions_path(self.path)

    def clear_lifecycle(self, table, model_id, reason, now=None, deadline=None):
        """`doctor --clear-lifecycle <id> --reason <text>`: under the lock (waiting up to `deadline` seconds; EvidenceBusy past it,
        and nothing changes), the marker is removed, the clear logged ({id, at, reason, marker}) and the findings re-evaluated from
        the stored set without it. No refresh can be mid-read while a clear happens (S-C3 item 3), and only a LATER fresh
        observation that shows the predicate restores the marker. Returns the removed marker, or None."""
        now = time.time() if now is None else now
        mid = table.canonical(model_id)
        with self.lock(deadline=deadline):
            data = self.load()
            markers = canonical_markers(table, data.get("markers"))
            marker = markers.pop(mid, None)
            data["markers"] = markers
            data.setdefault("cleared", []).append({"id": mid, "at": now, "reason": reason, "marker": marker})
            finish_evaluation(data, table, now, refreshed=False)
            self.write(data)
        return marker

    def compose(self, table, local, now=None, data=None, deferred=False, busy=False):
        """The composed verdict (DESIGN §5, S-C3 item 4) from ONE snapshot: `data` as the caller loaded it, else one load now."""
        return compose_verdict(table, self.load() if data is None else data, local, time.time() if now is None else now, deferred=deferred, busy=busy)


def write_private_json(path, data):
    """Atomic replace, private (0600): a reader sees the old or the new file, never a mix."""
    path = Path(path)
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, default=str)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


class EvidenceLock:
    """`flock` on the store's sibling `.lock`, which is never unlinked and never inherited by a helper (`os.open` makes the
    descriptor non-inheritable, so a killed writer releases it at once). Acquisition is bounded (A1): `blocking` waits up to
    `deadline` seconds (default lock_deadline()), a non-blocking lock tries once; EvidenceBusy when it cannot be taken."""

    def __init__(self, path, deadline=None, blocking=True):
        self.path, self.deadline, self.blocking, self.fd = Path(path), deadline, blocking, None

    def __enter__(self):
        private_dir(self.path.parent)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        end = time.monotonic() + (lock_deadline() if self.deadline is None else self.deadline)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if not self.blocking or time.monotonic() >= end:
                    os.close(fd)
                    raise EvidenceBusy(f"another process holds the evidence lock {self.path}")
                time.sleep(0.05)
            else:
                self.fd = fd
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _severity(marker):
    kind = marker.get("kind")
    return MARKER_KINDS.index(kind) if kind in MARKER_KINDS else len(MARKER_KINDS)


def marker_line(marker):
    """One line describing a marker: `<id> is marked <kind> (<source>, seen <date>[, recorded as <spelling>]): <detail>`."""
    seen = marker.get("seen_at")
    when = f", seen {_date_text(seen)}" if seen is not None else ""
    spelling = f", recorded as {marker['spelling']}" if marker.get("spelling") else ""
    detail = f": {marker['detail']}" if marker.get("detail") else ""
    return f"{marker['id']} is marked {marker.get('kind') or 'retiring'} ({marker.get('source') or 'unknown source'}{when}{spelling}){detail}"


def _date_text(value):
    ts = _epoch(value)
    if ts is None:
        return str(value)
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):  # a platform whose gmtime() covers less than datetime's range
        return str(value)


# ============================================================================ the launch validator (DESIGN §3)
UPGRADE_HINTS = {"npm": "npm install -g @openai/codex@latest", "brew": "brew upgrade codex", "app": "update the ChatGPT app",
                 "curl": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"}


def upgrade_hint(path):
    """L4: how to upgrade the Codex CLI at `path`, chosen from the executable's REAL path; the ONE helper every Codex upgrade hint
    uses (the floor refusal, the discovery finding; bootstrap.sh's upgrade_hint applies the same rules). Under
    /node_modules/@openai/codex/: npm's package (also when npm's prefix is Homebrew's); under a Homebrew prefix (/opt/homebrew/,
    /usr/local/Cellar/, a Caskroom): the formula or cask; inside ChatGPT.app: the app; under <CODEX_HOME>/packages/standalone/
    (OpenAI's installer, linked from ~/.local/bin/codex) or anywhere else, including no path: OpenAI's installer."""
    real = os.path.realpath(path) if path else ""
    if "/node_modules/@openai/codex/" in real:
        return UPGRADE_HINTS["npm"]
    if any(prefix in real for prefix in ("/opt/homebrew/", "/usr/local/Cellar/", "/Caskroom/")):
        return UPGRADE_HINTS["brew"]
    if "/ChatGPT.app/" in real:
        return UPGRADE_HINTS["app"]
    return UPGRADE_HINTS["curl"]


def validate(table, evidence, resolved, effort, mode, cli=None):
    """One function reading local facts only: (i) table membership, (ii) the sticky markers on the model, (iii) the effort,
    (iv) the resolved executable's parsed version against the model's floor. `mode` follows the evidence that an attempt's CLI
    started (bridge.py's `started` flag, which stamps the seat record's `first_launch_at`; S-LAUNCH): `initial` refuses on any
    failing check (Refusal, one line); `continuation` re-reads the markers only,
    turns a newly learned one into a notice and proceeds. `cli` is {"path", "version"} (the first line of `--version`) for the
    model's vendor, or None when no executable resolved (the launch reports that itself). Returns the notice lines."""
    vendor, model, tag = resolved.vendor, resolved.model, resolved.tag
    notices = []
    marker = evidence.marker(model, table) if evidence is not None else None  # under any spelling the table accepts for it
    if mode != "initial":
        if marker:
            notices.append(f"NOTICE: seat {tag}: {model} was marked {marker.get('kind') or 'retiring'} after this seat started; the seat continues")
        return notices
    if not table.is_selectable(model):
        fam = table.family_of(model)
        use = f"Use {fam} ({table.head(fam)})." if fam else table.families_hint()
        raise Refusal(f"refused seat '{tag}': {model} is not a current head. {use}", complete=True)
    if marker:
        if resolved.k1:
            fix = f"Use {table.models[model]['family']} ({table.head(table.models[model]['family'])})."
        else:
            fix = f"Run bridge.py doctor; after review, clear it with --clear-lifecycle {model} --reason \"<why>\"."
        raise Refusal(f"refused seat '{tag}': {marker_line(marker)}. {fix}", complete=True)
    supported = table.supported_efforts(model)
    if (effort is None and supported) or (effort is not None and effort not in supported):
        raise Refusal(f"refused seat '{tag}': {model} supports {', '.join(supported) or 'no effort level'}; {effort!r} is not available.", complete=True)
    floor = table.floor(model)
    if floor and cli and cli.get("path"):
        name = "Codex CLI" if vendor == "codex" else "Claude Code"
        text = first_line(cli.get("version"))
        version = parse_version(vendor, text)
        if version is None:
            fix = ("set CODEX_BIN to a codex whose --version prints codex-cli X.Y.Z" if vendor == "codex" else
                   "set CLAUDE_BIN to a claude whose --version prints X.Y.Z (Claude Code)")
            raise Refusal(f"seat '{tag}' ({model}): cannot read the {name} version from '{text[:60]}'; {fix}", complete=True)
        if not version >= parse_min_cli(floor):
            if vendor == "codex":
                raise Refusal(f"seat '{tag}' ({model}) needs {name} >= {floor}; {cli['path']} is {version.text}. "
                              f"Upgrade: {upgrade_hint(cli['path'])}, or point CODEX_BIN at a newer codex.", complete=True)
            raise Refusal(f"seat '{tag}' ({model}) needs {name} >= {floor} (an older Claude Code resolves the family names to older models); "
                          f"run: claude update ({cli['path']} is {version.text})", complete=True)
    return notices


# ============================================================================ the check's pure evaluation (DESIGN §5)
STATE_WORDS = {"active": None, "current": None, "available": None, "legacy": "legacy", "deprecated": "deprecated",
               "retiring": "retiring", "sunset": "retiring", "scheduled_retirement": "retiring", "retired": "retired", "removed": "retired"}
PROSE_RE = re.compile(r"deprecat|no longer available|retire", re.I)
STATUS_ROW_RE = re.compile(r"^\|\s*(claude-[a-z0-9.-]+)\s*\|\s*(active|legacy|deprecated|retired)\s*\|\s*([^|\n]*?)\s*\|\s*([^|\n]*?)\s*\|\s*$", re.I | re.M)
API_ID_ROW_RE = re.compile(r"^\|\s*Claude API ID\s*\|(.*)$", re.M)
NOT_SCHEDULED_RE = re.compile(r"not sooner than|to be announced|tba|n/a", re.I)


MAX_EPOCH = 253402300800.0  # 10000-01-01T00:00:00Z, the first moment past datetime's range


def _seconds(number):
    """A numeric timestamp as seconds: a value implausibly large for seconds is read as milliseconds."""
    try:
        x = float(number)
    except OverflowError:  # an integer beyond float's range
        return math.inf
    return x / 1000.0 if x > 1e11 else x


def _epoch(value):
    """Epoch seconds from a number (milliseconds when implausibly large) or an ISO 8601 string; None otherwise, and None for a value
    that is no representable timestamp (NaN, an infinity, a moment outside datetime's range: A-astra-1 / A-opus-2)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = _seconds(value)
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            ts = _seconds(float(s))
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            ts = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return ts if math.isfinite(ts) and 0 < ts < MAX_EPOCH else None


def _page_date(text):
    """Epoch seconds of a date written as Anthropic writes them (`March 1, 2027`), or None when the text is not a date."""
    s = (text or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def lifecycle_record(entry):
    """DESIGN §5: the normalized lifecycle record of one live Codex catalog entry."""
    info = entry.get("upgradeInfo") if isinstance(entry.get("upgradeInfo"), dict) else {}
    state = next((entry[k] for k in ("status", "lifecycle", "state") if isinstance(entry.get(k), str)), None)
    return {"id": entry.get("id") or entry.get("model"), "hidden": bool(entry.get("hidden")),
            "upgrade_to": entry.get("upgrade") or info.get("model"), "retirement_at": _epoch(info.get("retirementAt")),
            "state": state.strip().lower() if state else None, "deprecated": bool(entry.get("deprecated")),
            "prose": str(info.get("migrationMarkdown") or "")}


def marker_kinds(rec, now):
    """The marker predicates a lifecycle record satisfies (each an explicit lifecycle statement or retirement metadata)."""
    kinds = set()
    state = STATE_WORDS.get(rec["state"]) if rec["state"] is not None else None  # any other string: not a lifecycle statement
    if rec["deprecated"] or state == "deprecated" or PROSE_RE.search(rec["prose"]):
        kinds.add("deprecated")
    at = rec["retirement_at"]
    if state == "retiring" or (at is not None and at > now):
        kinds.add("retiring")
    if state == "retired" or (at is not None and at <= now):
        kinds.add("retired")
    return kinds


def parse_deprecations(text):
    """{model id: {"state", "deprecated", "retirement"}} from the "Model status" table of Anthropic's model-deprecations page;
    None when the page was not read or carries no recognizable table."""
    if not isinstance(text, str):
        return None
    rows = {m.group(1): {"state": m.group(2).capitalize(), "deprecated": m.group(3).strip(), "retirement": m.group(4).strip()}
            for m in STATUS_ROW_RE.finditer(text)}
    return rows or None


def parse_overview(text):
    """{"current": [the IDs of the "Claude API ID" row]} from Anthropic's models overview; None when unrecognized."""
    if not isinstance(text, str):
        return None
    m = API_ID_ROW_RE.search(text)
    ids = re.findall(r"`([^`]+)`", m.group(1)) if m else []
    return {"current": ids} if ids else None


def row_kinds(row, now):
    """Marker predicates of a deprecations-table row: Deprecated or Retired states, or a scheduled retirement date."""
    kinds = set()
    state = row["state"]
    if state == "Deprecated":
        kinds.add("deprecated")
    if state == "Retired":
        kinds.add("retired")
    when = None if NOT_SCHEDULED_RE.search(row["retirement"] or "") else _page_date(row["retirement"])
    if when is not None:
        kinds.add("retired" if when <= now else "retiring")
    return kinds


def non_default_keys(table, config_result):
    """The `catalog_config_keys` a configuration layer sets, in a config/read answer ({"config", "origins"}; names only, never
    values). The server answers RESOLVED defaults (L1: `chatgpt_base_url` carries its default URL), so a key is non-default only
    when `origins` has an entry for it or for one of its sub-keys (`key.` prefix); a key without an origin is the default,
    whatever its value. None when the answer is unreadable."""
    if not isinstance(config_result, dict) or not isinstance(config_result.get("config"), dict) or not isinstance(config_result.get("origins"), dict):
        return None
    origins = config_result["origins"]
    return [k for k in table.catalog_config_keys if any(o == k or o.startswith(k + ".") for o in origins)]


def evaluate(table, sources, local=None, now=None, markers=None, fresh=None):
    """The verdict of the check from the sources doctor read and the current local facts (DESIGN §5). No source is fetched here.

    sources = {"codex": {"config": <config/read answer>, "models": [<model/list entries>]} or None,
               "npm": <registry JSON> or None, "deprecations": <page text> or None, "overview": <page text> or None}
    local   = {"codex_version", "claude_version", "relay_claude_version", "env": {name: value}} or None (remote findings only)
    markers = the store's sticky markers ({id: record}, any accepted spelling: canonicalized here, C3-6), authoritative even when
              empty; None derives them from these sources (the pure evaluation)
    fresh   = the names of the sources read successfully in THIS check; None means all of them. Only two fresh Anthropic sources
              confirm an absence (C3-5): a retained page's omission never makes a `missing` FAIL.
    Returns {"verdict": OK|FAIL|UNVERIFIED, "findings": [{"class", "text", "source"?, ...}], "markers": [{"id", "kind", "kinds",
             "source", "detail"}], "derived": [the markers these sources show, each with the `source_name` it came from]}."""
    now = time.time() if now is None else now
    findings, derived = [], {}

    def find(cls, text, source=None, **extra):
        findings.append({"class": cls, "text": text, **({"source": source} if source else {}), **extra})

    def mark(model_id, kinds, source, detail, source_name):
        if not kinds:
            return
        rec = derived.setdefault(model_id, {"id": model_id, "kinds": [], "source": source, "detail": detail, "source_name": source_name})
        rec["kinds"] = [k for k in MARKER_KINDS if k in set(rec["kinds"]) | set(kinds)]
        rec["kind"] = rec["kinds"][0]

    # ---- the local Codex catalog: applicable only with every catalog configuration key at its default
    codex = sources.get("codex") if isinstance(sources, dict) else None
    entries = codex.get("models") if isinstance(codex, dict) else None
    if not isinstance(entries, list):
        find("unverified", "unverified: the local Codex catalog (model/list) could not be read", "codex")
    else:
        non_default = non_default_keys(table, codex.get("config"))
        if non_default is None:
            find("unverified", "unverified: local catalog not applicable: config/read could not be read", "codex")
        elif non_default:
            find("unverified", f"local catalog not applicable: {', '.join(non_default)} is non-default", "codex")
        else:
            records = [(e, lifecycle_record(e)) for e in entries if isinstance(e, dict)]
            by_id = {table.canonical(rec["id"]): (entry, rec) for entry, rec in records if rec["id"]}  # keyed by the canonical ID
            for _entry, rec in records:
                if not rec["id"]:
                    continue
                kinds = marker_kinds(rec, now)
                detail = "; ".join(x for x in (f"status {rec['state']}" if rec["state"] else "", "deprecated flag" if rec["deprecated"] else "",
                                               f"retires {_date_text(rec['retirement_at'])}" if rec["retirement_at"] is not None else "",
                                               f"upgrade to {rec['upgrade_to']}" if rec["upgrade_to"] else "",
                                               "prose: " + _one_line(rec["prose"])[:80] if PROSE_RE.search(rec["prose"]) else "") if x)
                mark(table.canonical(rec["id"]), kinds, "codex model/list", detail, "codex")
                if rec["state"] and rec["state"] not in STATE_WORDS:
                    find("info", f"info: {rec['id']} has status '{rec['state']}' in the local catalog (not a lifecycle statement)", "codex")
                if rec["upgrade_to"] and rec["retirement_at"] is None and not kinds:
                    find("info", f"info: {rec['id']} points at {rec['upgrade_to']} without a retirement date (an upgrade pointer, not a marker)", "codex")
                fam = table.family_matching("codex", rec["id"]) if not rec["hidden"] else None
                if fam and table.canonical(rec["id"]) not in table.known_members("codex", fam):
                    undone = table.blocked.get(table.canonical(rec["id"]))
                    if undone:  # an undone automatic promotion blocked it on this Mac: known, and not a newer model to report again
                        find("info", f"info: {rec['id']} is listed but blocked on this Mac: {blocked_reason(undone)}", "codex")
                    else:
                        find("stale", f"stale: newer model may exist: {rec['id']} is listed but not in the model table", "codex")
            for head in table.heads("codex"):
                entry, rec = by_id.get(head, (None, None))
                if rec is None:
                    find("local", f"local: {head} is not in this machine's Codex catalog (model/list); this login cannot run it", "codex")
                    continue
                if rec["hidden"]:
                    find("local", f"local: {head} is hidden in this machine's Codex catalog (model/list); this login cannot select it", "codex")
                    continue
                live = sorted(x.get("reasoningEffort") for x in entry.get("supportedReasoningEfforts") or [] if isinstance(x, dict))
                if live != sorted(table.supported_efforts(head)):
                    find("stale", f"stale: effort set of {head} differs from the table", "codex")

    # ---- npm dist-tags.latest (compared with the installed Codex CLI among the local findings below)
    npm = sources.get("npm") if isinstance(sources, dict) else None
    latest = (npm.get("dist-tags") or {}).get("latest") if isinstance(npm, dict) and isinstance(npm.get("dist-tags"), dict) else None
    if not isinstance(latest, str):
        latest = None
        find("unverified", "unverified: npm dist-tags.latest of @openai/codex could not be read", "npm")

    # ---- Anthropic's two pages: markers, stale members, coverage of every enabled Claude head
    dep = parse_deprecations(sources.get("deprecations") if isinstance(sources, dict) else None)
    ov = parse_overview(sources.get("overview") if isinstance(sources, dict) else None)
    if dep is None:
        find("unverified", "unverified: Anthropic model-deprecations page not read or not recognized (no Model status table)", "deprecations")
    if ov is None:
        find("unverified", "unverified: Anthropic models overview not read or not recognized (no Claude API ID row)", "overview")
    claude_heads = table.heads("claude")
    for model_id, row in (dep or {}).items():  # evidence is keyed by the canonical ID: a row for a listed spelling marks its model
        mark(table.canonical(model_id), row_kinds(row, now), "anthropic model-deprecations", f"{row['state']}; retirement {row['retirement'] or 'unknown'}",
             "deprecations")
        fam = table.family_matching("claude", model_id)
        if fam and row["state"] == "Active" and table.canonical(model_id) not in table.known_members("claude", fam):
            find("stale", f"stale: newer model may exist: {model_id} is listed as Active but not in the model table", "deprecations")
        if row["state"] == "Legacy" and table.canonical(model_id) in claude_heads:
            find("stale", f"stale: {model_id} is marked Legacy by Anthropic; a newer model replaces it", "deprecations")
    for model_id in (ov or {}).get("current", []):  # membership through the canonical ID: a modifier or case is not a newer model
        fam = table.family_matching("claude", model_id)
        if fam and table.canonical(model_id) not in table.known_members("claude", fam):
            find("stale", f"stale: newer model may exist: {model_id} is the current Claude API ID but not in the model table", "overview")
    targets = list(claude_heads) + ([table.k1["id"]] if table.k1_enabled() else [])
    seen_dep = {table.canonical(m) for m in (dep or {})}
    seen_ov = {table.canonical(m) for m in (ov or {}).get("current", [])}
    confirmed = {"deprecations", "overview"} if fresh is None else set(fresh)  # only a source read in THIS check confirms an absence
    for head in targets:
        if head in seen_dep or head in seen_ov:  # a positive observation under any accepted spelling establishes presence
            continue
        if dep is not None and ov is not None and {"deprecations", "overview"} <= confirmed:
            find("missing", f"missing: {head} is in neither Anthropic source", "deprecations", sources=["deprecations", "overview"])
        else:
            find("unverified", f"unverified: {head} could not be confirmed (an Anthropic source was not read in this check)")

    # ---- markers on relevant targets are lifecycle failures; elsewhere they are informational
    used = canonical_markers(table, markers) if markers is not None else derived  # the store's sticky markers, or the ones these sources show
    relevant = table.relevant_targets()
    for model_id, rec in used.items():
        if model_id in relevant:
            find("lifecycle", f"lifecycle: {model_id} is marked {rec.get('kind') or 'retiring'} ({rec.get('source') or 'unknown source'}): {rec.get('detail') or ''}",
                 read_at=rec.get("seen_at"))
        else:
            k1 = " (K1 seat off)" if model_id == table.k1["id"] and not table.k1_enabled() else ""
            find("info", f"marker on {model_id}{k1}: informational", read_at=rec.get("seen_at"))

    if local is not None:
        local_findings(table, local, latest, find)
    return {"verdict": verdict_of(findings), "findings": findings, "markers": list(used.values()), "derived": list(derived.values())}


def verdict_of(findings):
    """FAIL if any FAIL line (an affirmative FAIL takes precedence over an unverified source); else UNVERIFIED if anything is
    unverified; else OK."""
    classes = {f.get("class") for f in findings if isinstance(f, dict)}
    return "FAIL" if classes & set(FAIL_CLASSES) else ("UNVERIFIED" if "unverified" in classes else "OK")


def local_findings(table, local, latest, find):
    """The findings evaluated from the current local facts at composition time (DESIGN §5): `cli`, `host`, `defaults`, and the
    `unverified` line for a Codex CLI older than npm latest (L3)."""
    relevant = table.relevant_targets()
    versions = {}
    for vendor in VENDORS:
        name = "Codex CLI" if vendor == "codex" else "Claude Code"
        text = local.get(f"{vendor}_version")
        version = parse_version(vendor, text) if text else None
        versions[vendor] = version
        floors = sorted({table.floor(m) for m in relevant if table.vendor_of_model(m) == vendor and table.floor(m)}, key=lambda f: parse_min_cli(f).key)
        if not floors:
            continue
        if not text:
            find("unverified", f"unverified: no {name} resolved; its version could not be checked against the floors ({', '.join(floors)})")
        elif version is None:
            find("cli", f"cli: cannot read the {name} version from '{first_line(text)[:60]}'; models need >= {floors[-1]}")
        else:
            below = [m for m in sorted(relevant) if table.vendor_of_model(m) == vendor and table.floor(m) and not version >= parse_min_cli(table.floor(m))]
            if below:
                find("cli", f"cli: {name} {version.text} is below the floor of {', '.join(f'{m} (>= {table.floor(m)})' for m in below)}")
    host_floor = table.highest_floor("claude")
    if host_floor and versions["claude"] is not None and not versions["claude"] >= parse_floor(host_floor):
        find("host", f"host: Claude Code {versions['claude'].text} is below {host_floor}, the floor the host agents' aliases rely on (run: claude update)")
    relay = local.get("relay_claude_version")
    if host_floor and relay is not None:
        rv = parse_version("claude", relay)
        which = f"relay's Claude CLI {local['relay_claude_bin']}" if local.get("relay_claude_bin") else "relay's Claude CLI"
        if rv is None:
            find("host", f"host: cannot read the version of {which} from '{first_line(relay)[:60]}'")
        elif not rv >= parse_floor(host_floor):
            find("host", f"host: {which} is {rv.text}, below {host_floor}; relay workers would run on old aliases (run: claude update)")
    env = local.get("env") or {}
    for var in HOST_OVERRIDE_VARS:
        if env.get(var) is not None:
            find("host", f"host: {var} is set; it overrides the model an alias means for the host agents (unset it)")
    env_efforts = {v: env.get(f"BRIDGE_{v.upper()}_EFFORT") or None for v in VENDORS}
    builtin = local.get("builtin_defaults") or {}  # the list that stands in for each unset variable (an entry point's, or the engine's own)
    # every effective default list, whatever its origin (S-DEF), with the same grammar and code path as parse_seats(): modifiers,
    # @fast per vendor, at least one seat; a user's variable is named, a built-in list is labelled "<list> (built-in default)"
    for var in DEFAULT_SEAT_VARS:
        if env.get(var) is not None:
            raw, origin = env[var], var
        elif builtin.get(var) is not None:
            raw, origin = builtin[var], f"{DEFAULT_LIST_NAMES[var]} (built-in default)"
        else:
            continue
        try:
            table.parse_seat_list(raw, env_efforts=env_efforts)
        except Refusal as exc:
            find("defaults", f"defaults: {origin}: {exc}")
    if latest and versions["codex"] is not None and parse_floor(latest) is not None and versions["codex"] < parse_floor(latest):
        # L3: Codex ships every few days and a release rarely adds a model; a CLI below a model's floor is refused at launch (§3)
        find("unverified", f"unverified: the installed Codex CLI {versions['codex'].text} is older than npm latest {latest}; it may not list "
                           f"newer models (run: {upgrade_hint(local.get('codex_bin'))})")


# ============================================================================ the check: sources, retention, publication, composition (DESIGN §5)
SOURCE_URLS = {"npm": "https://registry.npmjs.org/-/package/@openai/codex/dist-tags",  # the tags alone; the packument is over 15 MB
               "deprecations": "https://platform.claude.com/docs/en/about-claude/model-deprecations.md",
               # where the page lives since 2026-09 (its own frontmatter url); the old about-claude/models/overview.md redirects here,
               # and a redirect is one more thing a fetch can fail on (Python 3.9's urllib follows no 308)
               "overview": "https://platform.claude.com/docs/en/models/overview.md"}
SOURCE_URL_VARS = {"npm": "BRIDGE_MODELS_NPM_URL", "deprecations": "BRIDGE_MODELS_DEPRECATIONS_URL", "overview": "BRIDGE_MODELS_OVERVIEW_URL"}
SOURCE_LABELS = {"codex": "the local Codex catalog (model/list)", "npm": "npm dist-tags.latest", "deprecations": "Anthropic model-deprecations",
                 "overview": "Anthropic models overview"}
ENTRY_KEYS = ("id", "model", "hidden", "upgrade", "deprecated", "status", "lifecycle", "state")  # what the store keeps of a catalog entry


def source_urls(env=None):
    """The three page URLs, each overridable by its environment hook (TEST_RULINGS R-E1)."""
    env = os.environ if env is None else env
    return {name: env.get(var) or SOURCE_URLS[name] for name, var in SOURCE_URL_VARS.items()}


BUDGET_TEXT = "the refresh's budget (BRIDGE_MODELS_FETCH_DEADLINE) ran out before this source was read"


def _end(timeout, deadline):
    """The monotonic moment a read must be over: its own `timeout`, capped by the refresh's absolute `deadline` when given."""
    end = time.monotonic() + timeout
    return end if deadline is None else min(end, deadline)


FETCH_MAX_BYTES = 2 << 20  # a page is read up to 2 MiB
FETCH_CODES = ("timeout", "tls", "dns", "too-large", "error")  # plus `http-<status>`: the only words a fetch reports an error as
# The fetch child (S-FETCH): plain urlopen with the default SSL context (certificate and hostname verification on; SSL_CERT_FILE
# and SSL_CERT_DIR handed over, since a LibreSSL build of Python ignores them), the body on stdout up to max_bytes, else ONE
# allowlisted code on stderr and a non-zero exit. Arguments: the URL, max_bytes, the socket timeout in seconds.
FETCH = r'''
import os, socket, ssl, sys, urllib.error, urllib.request
url, max_bytes, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None, capath=os.environ.get("SSL_CERT_DIR") or None)
    request = urllib.request.Request(url, headers={"User-Agent": "claude-codex-bridge models check"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        body = response.read(max_bytes + 1)
except Exception as exc:
    if isinstance(exc, urllib.error.HTTPError):
        code = "http-%d" % exc.code
    else:
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        code = ("timeout" if isinstance(cause, (socket.timeout, TimeoutError)) else "tls" if isinstance(cause, ssl.SSLError)
                else "dns" if isinstance(cause, socket.gaierror) else "error")
    sys.stderr.write(code)
    sys.exit(1)
if len(body) > max_bytes:
    sys.stderr.write("too-large")
    sys.exit(1)
sys.stdout.buffer.write(body)
'''


def fetch_page(url, timeout, deadline=None, max_bytes=FETCH_MAX_BYTES, cwd=None):
    """One GET of a page in a CHILD process, bounded in TOTAL by `timeout` seconds and by the refresh's `deadline` (S-FETCH):
    subprocess.run kills the child at that moment, so the deadline is absolute and nothing survives the call. The child runs
    isolated (`-I`: neither the working directory nor a script directory on sys.path, no PYTHON* variables, no user site) in
    `cwd`, a private directory the bridge owns (the catalog directory; a private temporary directory when none is given), so a
    checkout holding a `socket.py`, `ssl.py` or `http/` is never imported by it (A-opus-1). (text, None) or (None, an error):
    the child's allowlisted code, `timeout` when it was killed, never the text of an exception."""
    remaining = _end(timeout, deadline) - time.monotonic()
    if remaining <= 0:
        return None, BUDGET_TEXT
    tmp = tempfile.mkdtemp(prefix="bridge-fetch-") if cwd is None else None
    try:
        r = subprocess.run([sys.executable, "-I", "-c", FETCH, url, str(max_bytes), f"{remaining:.3f}"], capture_output=True,
                           timeout=remaining, stdin=subprocess.DEVNULL, cwd=cwd or tmp)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode == 0:
        return r.stdout.decode("utf-8", "replace"), None
    code = r.stderr.decode("ascii", "replace").strip()
    return None, code if code in FETCH_CODES or re.fullmatch(r"http-\d{3}", code) else "error"


def retain_page(name, text):
    """A page as the store keeps it: npm -> {"latest"}, from the dist-tags endpoint (the tags at the top level: {"latest": ...}) or
    from a packument ({"dist-tags": {"latest": ...}}, which a BRIDGE_MODELS_NPM_URL override may still point at); a Markdown page
    verbatim, but only when its structure is recognized (otherwise the last observation is kept and the source is unverified).
    (observation, None) or (None, why), the reason in the check's own words, never the body's."""
    if name == "npm":
        try:
            data = json.loads(text)
        except ValueError:
            return None, "not JSON"
        latest = None
        if isinstance(data, dict):
            tags = data["dist-tags"] if isinstance(data.get("dist-tags"), dict) else data
            latest = tags.get("latest")
        return ({"latest": latest}, None) if isinstance(latest, str) else (None, "no latest tag (neither a top-level `latest` nor dist-tags.latest)")
    if name == "deprecations":
        return (text, None) if parse_deprecations(text) else (None, "not recognized (no Model status table)")
    if name == "overview":
        return (text, None) if parse_overview(text) else (None, "not recognized (no Claude API ID row)")
    return None, "unknown source"


def rpc_diagnostic(method, error):
    """The allowlisted diagnostic of a JSON-RPC error (Astra C3-2): the request's method and the error's code, never the server's
    text, which may carry a configuration value."""
    code = error.get("code") if isinstance(error, dict) else None
    return f"{method} answered error code {code if isinstance(code, int) and not isinstance(code, bool) else 'unknown'}"


def _text_or_none(value):
    return value is None or isinstance(value, str)


def entry_ok(entry):
    """A model/list entry the check can consume (Astra C3b-3, C4b-4): a non-empty string id (`id` or `model`), and the type of
    every field read later: `supportedReasoningEfforts` absent or a list of objects each with a string `reasoningEffort`;
    `upgradeInfo` absent or an object whose `model` and `migrationMarkdown` are absent or strings and whose `retirementAt` is
    absent or a supported timestamp (an epoch number or an ISO 8601 string); `model`, `upgrade`, `status`, `lifecycle`, `state`
    absent or strings; `hidden`, `deprecated` absent or booleans."""
    if not isinstance(entry, dict):
        return False
    ident = entry.get("id") if entry.get("id") is not None else entry.get("model")
    if not isinstance(ident, str) or not ident.strip():
        return False
    efforts = entry.get("supportedReasoningEfforts")
    if efforts is not None and not (isinstance(efforts, list) and all(isinstance(x, dict) and isinstance(x.get("reasoningEffort"), str) for x in efforts)):
        return False
    info = entry.get("upgradeInfo")
    if info is not None:
        if not isinstance(info, dict) or not all(_text_or_none(info.get(k)) for k in ("model", "migrationMarkdown")):
            return False
        if info.get("retirementAt") is not None and _epoch(info.get("retirementAt")) is None:
            return False
    if not all(_text_or_none(entry.get(k)) for k in ("model", "upgrade", "status", "lifecycle", "state")):
        return False
    return all(entry.get(k) is None or isinstance(entry.get(k), bool) for k in ("hidden", "deprecated"))


def read_codex_catalog(codex, cwd, timeout, env=None, deadline=None):
    """Codex `model/list` through the given executable in the local CODEX_HOME: one `app-server` in `cwd` (the jobs root's parent):
    `initialize`, `initialized`, the project-aware `config/read` ({"cwd", "includeLayers": false}), then `model/list` with
    includeHidden over every cursor page. Never `--ignore-user-config` (the app-server rejects it). Frames are read as bytes
    without blocking, split on newlines, against a monotonic deadline (`timeout`, capped by the refresh's `deadline`; Astra C3-7);
    every answer is validated structurally before it counts (C3-9), and an error is reported by method and code only (C3-2).
    (answer, None) or (None, error)."""
    end = _end(timeout, deadline)
    if end <= time.monotonic():
        return None, BUDGET_TEXT
    try:
        p = subprocess.Popen([codex, "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError as exc:
        return None, f"cannot start `codex app-server`: {_one_line(exc)[:120]}"
    fd = p.stdout.fileno()
    os.set_blocking(fd, False)
    buf = bytearray()

    def send(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        p.stdin.flush()

    def answer(rid, method, limit=None):
        stop = end if limit is None else min(end, time.monotonic() + limit)
        while True:
            while True:  # every complete frame already in the buffer, before the pipe is asked for more
                cut = buf.find(b"\n")
                if cut < 0:
                    break
                line = bytes(buf[:cut])
                del buf[:cut + 1]
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == rid:
                    if msg.get("error") is not None:
                        raise RuntimeError(rpc_diagnostic(method, msg["error"]))
                    return msg.get("result")
            remaining = stop - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError(f"{method}: no answer within {timeout if limit is None else min(timeout, limit):g}s")
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise RuntimeError(f"{method}: the app-server ended before answering")
            buf.extend(chunk)

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "claude-codex-bridge", "title": "claude-codex-bridge models check",
                                                                          "version": "1"}}})
        answer(1, "initialize")
        send({"method": "initialized"})
        send({"id": 2, "method": "config/read", "params": {"cwd": cwd, "includeLayers": False}})
        config = answer(2, "config/read")
        if not (isinstance(config, dict) and isinstance(config.get("config"), dict) and isinstance(config.get("origins"), dict)):
            raise RuntimeError("config/read answered no object with `config` and `origins` objects")
        models, cursor, rid = [], None, 3
        while True:
            params = {"includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            send({"id": rid, "method": "model/list", "params": params})
            page = answer(rid, "model/list")
            if not (isinstance(page, dict) and isinstance(page.get("data"), list) and all(isinstance(e, dict) for e in page["data"])):
                raise RuntimeError("model/list answered no object with a `data` list of objects")
            if not all(entry_ok(e) for e in page["data"]):
                raise RuntimeError("model/list answered an entry without a string model id or with a field of the wrong type")
            rid += 1
            models.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        # whether a login stands behind the list (DECISIONS A28): without one, model/list answers the CLI's bundled catalog, which
        # is no evidence of what an account can run; the answer only counts toward automatic promotion with a login confirmed
        try:  # an older CLI without account/read, an error, a closed pipe or no time left: the login is not confirmed, the list stands
            send({"id": rid, "method": "account/read", "params": {"refreshToken": False}})
            account = account_summary(answer(rid, "account/read", limit=ACCOUNT_READ_TIMEOUT_S))
        except (OSError, TimeoutError, RuntimeError) as exc:
            account = {"signed_in": None, "error": _one_line(exc)[:160]}
        return {"config": config, "models": models, "account": account}, None
    except (TimeoutError, RuntimeError) as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"{exc.__class__.__name__} while reading the catalog"  # our own words only: never anything the server sent
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        # the helper's cleanup is part of the refresh's budget too (Astra C3b-2): it may end politely while budget remains,
        # then it is killed
        grace = 5.0 if deadline is None else max(0.0, min(5.0, deadline - time.monotonic()))
        try:
            p.terminate()
            p.wait(timeout=grace)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass


ACCOUNT_READ_TIMEOUT_S = 5  # account/read answers from the local auth file; it never holds a refresh's budget hostage


def account_summary(result):
    """What the check keeps of an `account/read` answer: whether a login is present, its kind and its plan; never an email or an
    account id."""
    if not isinstance(result, dict):
        return {"signed_in": None, "error": "account/read answered no object"}
    acct = result.get("account")
    if acct is None:
        return {"signed_in": False}
    if not isinstance(acct, dict):
        return {"signed_in": None, "error": "account/read answered an account that is not an object"}
    kind, plan = acct.get("type"), acct.get("planType")
    return {"signed_in": True, "type": kind if isinstance(kind, str) else None, "plan": plan if isinstance(plan, str) else None}


def retain_catalog(table, answer):
    """The value-free observation the store keeps of a catalog answer: each catalog configuration key as default/non-default
    (names only; non_default_keys decides), the lifecycle-relevant fields of every entry and the login summary (account_summary);
    a configuration value is never copied anywhere."""
    non_default = non_default_keys(table, answer.get("config") if isinstance(answer, dict) else None)
    keys = None if non_default is None else {k: ("non-default" if k in non_default else "default") for k in table.catalog_config_keys}
    models = []
    for e in (answer.get("models") if isinstance(answer, dict) else None) or []:
        if not isinstance(e, dict):
            continue
        kept = {k: e[k] for k in ENTRY_KEYS if k in e}
        info = e.get("upgradeInfo") if isinstance(e.get("upgradeInfo"), dict) else None
        if info is not None:
            kept["upgradeInfo"] = {k: info.get(k) for k in ("model", "retirementAt", "migrationMarkdown") if k in info}
        kept["supportedReasoningEfforts"] = [{"reasoningEffort": x.get("reasoningEffort")} for x in e.get("supportedReasoningEfforts") or []
                                             if isinstance(x, dict)]
        models.append(kept)
    out = {"config_keys": keys, "models": models}
    if isinstance(answer, dict) and isinstance(answer.get("account"), dict):
        out["account"] = {k: v for k, v in answer["account"].items() if k in ("signed_in", "type", "plan", "error")}
    return out


def retained_sources(sources):
    """evaluate()'s `sources` from the store's retained observations (None for a source never read)."""
    def observation(name):
        rec = sources.get(name) if isinstance(sources, dict) else None
        return rec.get("observation") if isinstance(rec, dict) else None
    codex, npm = observation("codex"), observation("npm")
    codex_src = None
    if isinstance(codex, dict):
        keys = codex.get("config_keys")
        # the store keeps names only: each non-default key is replayed as an origin entry of an answer-shaped config (L1)
        config = {"config": {}, "origins": {k: {} for k, v in keys.items() if v == "non-default"}} if isinstance(keys, dict) else None
        codex_src = {"config": config, "models": codex.get("models")}
    npm_src = {"name": "@openai/codex", "dist-tags": {"latest": npm["latest"]}} if isinstance(npm, dict) and npm.get("latest") else None
    return {"codex": codex_src, "npm": npm_src, "deprecations": observation("deprecations"), "overview": observation("overview")}


def refresh_due(data, table, now):
    """The daily re-check is due when the store records no `sources_refreshed_at`, when it is older than the table's
    `recheck_after_hours`, or when it lies in the future (a clock that moved back: a negative age never postpones it, Astra C3-10)."""
    refreshed = _epoch(data.get("sources_refreshed_at")) if isinstance(data, dict) else None
    try:
        window = float(table.policy.get("recheck_after_hours", 24)) * 3600.0
    except (TypeError, ValueError):
        window = 24 * 3600.0
    return refreshed is None or now - refreshed < 0 or now - refreshed > window


def promotion_due(data, table, now):
    """True when the promotion pass has not run within the re-check window (`promotion_pass_at`, written by every refresh that ran
    it): an older engine, or a process with promotion off, may have refreshed the sources without it, and the next model-starting
    mode then refreshes so that the pass runs (DECISIONS A28)."""
    last = _epoch(data.get("promotion_pass_at")) if isinstance(data, dict) else None
    try:
        window = float(table.policy.get("recheck_after_hours", 24)) * 3600.0
    except (TypeError, ValueError):
        window = 24 * 3600.0
    return last is None or now - last < 0 or now - last > window


def refreshed_since(data, started, now):
    """True when a refresh was published between `started` and `now`: a doctor that waited for the lock then skips its own reads
    and reports that publication (S-C3 item 1)."""
    refreshed = _epoch(data.get("sources_refreshed_at")) if isinstance(data, dict) else None
    return refreshed is not None and started <= refreshed <= now


def attempt_of(src):
    """A source's latest attempt, {at, ok, error} (S-C3 item 2); a record from before the ruling is read from its `error` fields."""
    src = src if isinstance(src, dict) else {}
    att = src.get("attempt")
    if isinstance(att, dict):
        return {"at": att.get("at"), "ok": bool(att.get("ok")), "error": att.get("error")}
    if src.get("error"):
        return {"at": src.get("error_at") or src.get("read_at"), "ok": False, "error": src["error"]}
    ok = src.get("observation") is not None
    return {"at": src.get("read_at"), "ok": ok, "error": None if ok else "not read"}


def _clean_marker(rec):
    return {k: v for k, v in rec.items() if k not in ("read_seq", "seq")} if isinstance(rec, dict) else rec


def _clean_cleared(entries):
    return [{"id": e.get("id"), "at": e.get("at"), "reason": e.get("reason"), "marker": _clean_marker(e.get("marker"))}
            for e in (entries or []) if isinstance(e, dict)]


def stored_findings(table, data, now):
    """The remote findings of a stored snapshot, evaluated under `table` without any fetch: the retained observations, each
    source's latest attempt (only a source the latest attempt read successfully confirms an absence, C3-5) and the stored
    canonical marker set exactly as it is, even empty: markers are never derived here (S-C3 item 3). Every finding carries the
    read time of the observation that supports it, a lifecycle finding the marker's `seen_at` (C3-14)."""
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    attempts = {name: attempt_of(src) for name, src in sources.items()}
    fresh = {name for name, att in attempts.items() if att["ok"]}
    read_at = {name: src.get("read_at") for name, src in sources.items() if isinstance(src, dict)}
    findings = []
    for f in evaluate(table, retained_sources(sources), None, now, markers=canonical_markers(table, data.get("markers")), fresh=fresh)["findings"]:
        f = dict(f)
        if "read_at" not in f:
            times = [read_at[n] for n in (f.get("sources") or [f.get("source")]) if read_at.get(n) is not None]
            f["read_at"] = max(times, key=lambda t: _epoch(t) or 0) if times else None
        findings.append(f)
    for name, att in attempts.items():
        src = sources[name]
        if isinstance(src, dict) and src.get("observation") is not None and not att["ok"]:
            findings.append({"class": "unverified", "source": name, "read_at": src.get("read_at"),
                             "text": f"unverified: {SOURCE_LABELS.get(name, name)} was not read at the last check ({att['error'] or 'not read'}); "
                                     f"using its observation of {_date_text(src.get('read_at'))}"})
    return findings


def finish_evaluation(data, table, now, refreshed):
    """Under the lock: the snapshot's findings re-evaluated under `table` and recorded with its digest and `evaluated_at` (and,
    after a refresh, `sources_refreshed_at`), the file's layout brought to S-C3's (each source with its retained observation and
    latest attempt; no clear_seq, read_seq or seq anywhere; the `cleared` history as {id, at, reason, marker})."""
    sources = {}
    for name, src in (data.get("sources") if isinstance(data.get("sources"), dict) else {}).items():
        if isinstance(src, dict):
            sources[name] = {"observation": src.get("observation"), "read_at": src.get("read_at"), "attempt": attempt_of(src)}
    data["sources"] = sources
    data["markers"] = canonical_markers(table, data.get("markers"))
    data["cleared"] = _clean_cleared(data.get("cleared"))
    data.pop("clear_seq", None)
    data["findings"] = stored_findings(table, data, now)
    data["table_sha256"] = table.sha256
    data["evaluated_at"] = now
    if refreshed:
        data["sources_refreshed_at"] = now
    return data


def apply_refresh(data, table, observations, now):
    """A refresh's publication, under the lock (S-C3 items 2-3): per source a successful read replaces the retained observation
    and a failed one only records its attempt (the retained success stays, unverified); markers are derived from THIS refresh's
    successful observations alone and merged into the sticky set (a new sighting keeps its own `seen_at`; a known marker gains
    kinds, never loses any); then the findings, the digest and the clocks."""
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    fresh = {}
    for name, rec in (observations or {}).items():
        src = dict(sources.get(name)) if isinstance(sources.get(name), dict) else {}
        at, ok = rec.get("read_at") or now, rec.get("observation") is not None
        src["attempt"] = {"at": at, "ok": ok, "error": None if ok else (rec.get("error") or "not read")}
        if ok:
            src["observation"], src["read_at"] = rec["observation"], at
            fresh[name] = {"observation": rec["observation"]}
        sources[name] = src
    data["sources"] = sources
    markers = canonical_markers(table, data.get("markers"))
    for rec in evaluate(table, retained_sources(fresh), None, now)["derived"]:
        cur = markers.get(rec["id"])
        if cur is None:
            markers[rec["id"]] = {"id": rec["id"], "kind": rec["kind"], "kinds": list(rec["kinds"]), "source": rec["source"], "seen_at": now,
                                  "detail": rec["detail"]}
        else:  # sticky: the first sighting stays; a new kind (severity) joins it
            kinds = [k for k in MARKER_KINDS if k in set(cur.get("kinds") or []) | set(rec["kinds"])]
            cur.update(kinds=kinds, kind=kinds[0], source=rec["source"], detail=rec["detail"])
    data["markers"] = markers
    return finish_evaluation(data, table, now, refreshed=True)


DEFERRED_TEXT = ("unverified: the daily re-check is overdue and was deferred (another process is refreshing the evidence); "
                 "composed from the last snapshot")
BUSY_TEXT = ("unverified: not refreshed: another process held the evidence lock past the deadline (BRIDGE_MODELS_LOCK_DEADLINE); "
             "showing the last snapshot")


def compose_verdict(table, data, local, now, deferred=False, busy=False):
    """The composed verdict (DESIGN §5; S-C3 item 4) from ONE snapshot of the store, never cached as a whole: its remote findings,
    recomputed in memory (never written) when the snapshot was evaluated under another table, plus the local findings evaluated
    right now from the current local facts (versions, defaults, overrides), with `evaluated_at` = now. `deferred` (A2: the
    overdue re-check is running elsewhere) and `busy` (A1: doctor could not take the lock in time) add one in-memory UNVERIFIED
    finding and keep every FAIL and every sticky marker."""
    data = data if isinstance(data, dict) else {}
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    if (sources or data.get("markers")) and data.get("table_sha256") != table.sha256:  # any snapshot, markers only included (C3b-4)
        findings = stored_findings(table, data, now)
    else:
        findings = [dict(f) for f in (data.get("findings") or []) if isinstance(f, dict)]
    if not sources and not findings:
        findings.append({"class": "unverified", "text": "unverified: the check has not run yet (run bridge.py doctor)"})
    npm = (sources.get("npm") or {}).get("observation") if isinstance(sources.get("npm"), dict) else None
    latest = npm.get("latest") if isinstance(npm, dict) else None
    if local is not None:
        local_findings(table, local, latest, lambda cls, text, source=None: findings.append({"class": cls, "text": text, "local": True}))
    if deferred:
        findings.append({"class": "unverified", "text": DEFERRED_TEXT, "local": True})
    if busy:
        findings.append({"class": "unverified", "text": BUSY_TEXT, "local": True})
    summary = {}
    for name, src in sources.items():
        if isinstance(src, dict):
            obs = src.get("observation")
            summary[name] = {"read_at": src.get("read_at"), "attempt": attempt_of(src),
                             **({"config_keys": obs.get("config_keys")} if name == "codex" and isinstance(obs, dict) else {})}
    return {"verdict": verdict_of(findings), "findings": findings, "markers": list(canonical_markers(table, data.get("markers")).values()),
            "evaluated_at": now, "sources_refreshed_at": data.get("sources_refreshed_at"), "table_sha256": table.sha256,
            "cleared": _clean_cleared(data.get("cleared")), "sources": summary, "deferred": bool(deferred), "busy": bool(busy)}


def unverified_summary(findings):
    """What the unverified notice names: each unverified source by name, any other unverified finding by its text."""
    names = []
    for f in findings:
        if isinstance(f, dict) and f.get("class") == "unverified":
            label = f.get("source") or re.sub(r"^unverified: ", "", f.get("text") or "")
            if label and label not in names:
                names.append(label)
    return names


# ============================================================================ automatic promotion of Codex heads (DECISIONS A28, 2026-11-07)
# Kevin's rule of 2026-11-07: when the signed-in login's own Codex catalog lists a newer GPT model of a known family, the bridge adopts
# it as that family's head without anyone editing models.json. The check's promotion pass (`promote`, under the evidence lock, after
# a refresh read the catalog) records a step in the evidence store's companion ledger; every reader of the table (`load_table`: the
# engine, doctor, triad) applies the ledger's steps over the shipped table (`effective_table`). The shipped table is never written:
# it ships in a zip and lives in a repository, and its human checkpoints take over from a step as soon as they name its model.
# Claude families are not promoted: Anthropic's pages describe the API, not what this Mac's Claude Code can run, so a newer Claude
# model stays a `stale` FAIL for a human checkpoint.
PROMOTIONS_VERSION = 1
PROMOTION_NOTICE_DAYS = 7  # how long a new job whose seats run an automatically promoted head leads with a notice saying so
PROMOTABLE_STATES = (None, "active", "current", "available")  # catalog status words that do not hold a candidate back
STEP_FIELDS = (("family", str), ("from", str), ("to", str), ("generation", str), ("efforts", list), ("min_cli", str))


class PromotionsError(TableError):
    """The promotions ledger cannot be read, is not a ledger, or an undo has nothing to undo: one line naming the file or the family.
    A TableError, so that every reader of the table (bridge.py, triad) reports a broken ledger as it reports a broken models.json."""


def promotions_path(evidence_path):
    """The promotions ledger of an evidence store: its companion `<store name without .json>.promotions.json` beside it
    (models-evidence.json -> models-evidence.promotions.json), so whatever binds the store (BRIDGE_MODELS_EVIDENCE, the store a triad
    job recorded, a test's) binds the ledger too."""
    p = Path(evidence_path)
    return p.with_name((p.name[:-5] if p.name.endswith(".json") else p.name) + ".promotions.json")


def empty_ledger():
    return {"version": PROMOTIONS_VERSION, "steps": [], "blocked": [], "log": [], "last_pass": None}


def ledger_problem(data):
    """What keeps `data` from being a promotions ledger, or None."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if data.get("version") != PROMOTIONS_VERSION:
        return f"'version' is {data.get('version')!r}, not {PROMOTIONS_VERSION}"
    for key in ("steps", "blocked", "log"):
        if not isinstance(data.get(key), list) or any(not isinstance(x, dict) for x in data[key]):
            return f"'{key}' must be a list of objects"
    for i, step in enumerate(data["steps"], 1):
        if step.get("vendor") != "codex":
            return f"step {i}: only Codex heads are promoted automatically"
        for key, typ in STEP_FIELDS:
            if not isinstance(step.get(key), typ) or (typ is str and not step[key].strip()):
                return f"step {i}: '{key}' must be a {'list' if typ is list else 'non-empty string'}"
        if any(not isinstance(e, str) for e in step["efforts"]) or not step["efforts"]:
            return f"step {i}: 'efforts' must list effort names"
        if not isinstance(step.get("superseded", []), list) or any(not isinstance(x, str) for x in step.get("superseded", [])):
            return f"step {i}: 'superseded' must be a list of model IDs"
        if parse_min_cli(step["min_cli"]) is None:
            return f"step {i}: 'min_cli' is not a CLI version"
        if _epoch(step.get("at")) is None:
            return f"step {i}: 'at' is not a timestamp"
    for i, rec in enumerate(data["blocked"], 1):
        if not all(isinstance(rec.get(k), str) and rec[k].strip() for k in ("id", "family")):
            return f"blocked {i}: needs an 'id' and a 'family'"
    return None


def load_promotions(path):
    """The ledger at `path` (an empty one when the file does not exist), checked; PromotionsError (one line) otherwise."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return empty_ledger()
    except OSError as exc:
        raise PromotionsError(f"promotions ledger {p}: {exc.strerror or _one_line(exc)}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PromotionsError(f"promotions ledger {p} is not valid JSON: {_one_line(exc)}") from None
    problem = ledger_problem(data)
    if problem:
        raise PromotionsError(f"promotions ledger {p}: {problem}")
    return data


def load_table(path, evidence_path):
    """The table every reader uses: the shipped table at `path` with the promotions ledger of the evidence store at `evidence_path`
    applied (effective_table). TableError (one line) for a broken table, a broken ledger, or a ledger the table cannot take."""
    base = Table.load(path)
    ledger_path = promotions_path(evidence_path)
    try:
        ledger = load_promotions(ledger_path)
    except PromotionsError as exc:
        raise PromotionsError(f"{exc}; remove it to start over (the next check adopts again what this Mac's Codex catalog lists)") from None
    try:
        return effective_table(base, ledger, ledger_path)
    except PromotionsError:
        raise
    except TableError as exc:
        raise PromotionsError(f"promotions ledger {ledger_path} does not apply to {path}: {exc}") from None


def blocked_reason(rec):
    """Why a model is blocked on this Mac, in words (refusals, findings, doctor): its automatic promotion was undone, with the date and
    the user's reason."""
    return f"its automatic promotion was undone ({_date_text(rec.get('at'))}: {rec.get('reason')})"


def generation_pattern(id_glob):
    """The strict form of a Codex family's `id_glob` that promotion accepts: its one `*` must be a dotted number (`gpt-*-terra`
    accepts gpt-<number>-terra such as a 6.2 or a 7, never gpt-<number>-mini-terra or a codename); None for a glob without exactly one `*`."""
    if not isinstance(id_glob, str) or id_glob.count("*") != 1:
        return None
    before, after = id_glob.split("*")
    return re.compile("^" + re.escape(before) + r"(\d+(?:\.\d+)*)" + re.escape(after) + "$")


def generation_order(text):
    """A dotted numeric generation as a comparable tuple, trailing zeros dropped (6 == 6.0 < 6.1 < 6.10); None when not numeric."""
    parts = str(text).split(".")
    if not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def _shipped_knows(base, model_id):
    """Whether the shipped table names `model_id` in any role: a model record, a head, a listed spelling, a retired predecessor or a
    refusal (a `*` pattern included). From then on the human checkpoint decides, not the ledger."""
    key = base.canonical(model_id)
    return key in base.models or base.family_of(key) is not None or any(base.refusal(v, key) for v in VENDORS)


def effective_table(base, ledger, ledger_path=None):
    """`base` (the shipped table) with the ledger's steps applied in order. For each step of a Codex family:
    - its model is named by the shipped table (head, predecessor, spelling, refusal): skipped; the human checkpoint decides;
    - its model was blocked on this Mac (an undone promotion): skipped;
    - its generation is not above the family's head at that point: skipped, its model kept as a retired predecessor (a shipped
      checkpoint or a later step went past it), so that it is refused as superseded and not reported as a newer model;
    - in each of those three cases the step's `superseded` models (listed beside its model when it was taken) stay retired
      predecessors too when the family's head at that point is at least as new (the table went past them), unless the shipped
      table names them or they are blocked; one newer than that head (the shipped table refused or retired the step's model without
      moving the head) is left unknown: a `stale` finding, and a candidate of the next pass;
    - otherwise its model becomes the head: a model record from the step (generation, efforts, floor, `auto_promoted`), the previous
      head and the step's `superseded` models join the family's retired predecessors, and an `auto_promotion` history entry is added.
    Fallback edges are re-keyed to the heads they now join, and an edge that would then descend a generation is dropped (the edges are
    suggestions, never applied on their own). The result passes the table's invariants (TableError otherwise); its sha256 also covers
    the applied steps, the blocked models and the models kept as predecessors, so a stored evaluation is recomputed when any of them
    changes. With none of them, the result is `base` itself (same sha256). A block of a family the shipped table no longer has is
    ignored (`promotion["ignored"]`, shown by doctor)."""
    ledger = ledger if isinstance(ledger, dict) else empty_ledger()
    blocked, ignored = {}, []
    for rec in ledger.get("blocked") or []:
        mid = base.canonical(rec["id"])
        if rec.get("family") not in base.families["codex"]:  # its refusal would name a head the table no longer has
            ignored.append((rec, f"{rec.get('family')} is not a Codex family of the shipped table"))
        elif not _shipped_knows(base, mid):  # a block the shipped table has caught up with is moot
            blocked[mid] = dict(rec, id=mid)
    data = copy.deepcopy(base.data)
    families, retired = data["families"]["codex"], data["retired"]["codex"]
    applied, skipped, kept = [], [], []

    def retire(fam, mid):
        """`mid` joins the family's retired predecessors: the shipped head a step replaces, or a model the shipped table does not name
        and no undone promotion blocked (a blocked model keeps its own refusal)."""
        ids = retired.setdefault(fam, [])
        if mid != families[fam]["known_good"] and mid not in ids and mid not in blocked and (mid == base.head(fam) or not _shipped_knows(base, mid)):
            ids.append(mid)
            kept.append(mid)

    def keep_superseded(fam, step):
        """The step's `superseded` models at or below the family's head at this point become retired predecessors; a newer one stays
        unknown (Fable's review of 2026-11-07: a checkpoint that refuses the step's model must not turn a newer runner-up into a
        false "superseded" refusal that no pass would ever adopt)."""
        pattern = generation_pattern(families[fam].get("id_glob"))
        head_gen = generation_order(data["models"][families[fam]["known_good"]]["generation"])
        for old in step.get("superseded") or []:
            mid = base.canonical(old)
            m = pattern.match(mid) if pattern else None
            gen = generation_order(m.group(1)) if m else None
            if gen is not None and head_gen is not None and gen <= head_gen:
                retire(fam, mid)

    for step in ledger.get("steps") or []:
        fam, to = step["family"], base.canonical(step["to"])
        if fam not in families:
            skipped.append((step, f"{fam} is not a Codex family of the shipped table"))
            continue
        if _shipped_knows(base, to):
            keep_superseded(fam, step)
            skipped.append((step, f"the shipped table names {to} now"))
            continue
        if to in blocked:
            keep_superseded(fam, step)
            skipped.append((step, "blocked on this Mac"))
            continue
        pattern = generation_pattern(families[fam].get("id_glob"))
        m = pattern.match(to) if pattern else None
        head = families[fam]["known_good"]
        new, cur = generation_order(step["generation"]), generation_order(data["models"][head]["generation"])
        if m is None or new is None or cur is None or generation_order(m.group(1)) != new:
            skipped.append((step, f"{to} does not match {fam}'s pattern {families[fam].get('id_glob')} with generation {step['generation']}"))
            continue
        if new <= cur:
            retire(fam, to)
            keep_superseded(fam, step)
            skipped.append((step, f"not newer than {fam}'s head {head}"))
            continue
        date = _date_text(step["at"])
        data["models"][to] = {"family": fam, "generation": step["generation"], "efforts": list(step["efforts"]), "min_cli": step["min_cli"],
                              "aliases": [], "source": f"automatic promotion {date}: listed by this Mac's own Codex catalog ({step.get('cli') or 'codex'})",
                              "auto_promoted": {"at": step["at"], "from": head, "cli": step.get("cli")}}
        families[fam] = dict(families[fam], known_good=to, verified=date,
                             verified_by=f"automatic promotion on this Mac {date} (this Mac's own Codex catalog, {step.get('cli') or 'codex'}, "
                                         f"signed in); the shipped head is {base.head(fam)}")
        retire(fam, head)
        keep_superseded(fam, step)
        data["history"].append({"kind": "auto_promotion", "family": fam, "from": head, "to": to, "observed": date,
                                "source": f"this Mac's own Codex catalog (model/list, signed in), {step.get('cli') or 'codex'}",
                                "decision": "automatic promotion (DECISIONS A28); the shipped table is unchanged"})
        applied.append(step)
    if not applied and not blocked and not kept:
        out = base
        out.promotion = {"applied": [], "skipped": skipped, "ignored": ignored, "ledger": str(ledger_path) if ledger_path else None}
        return out
    heads = {fam: families[fam]["known_good"] for fam in families}
    family_of_head = {rec["known_good"]: fam for v in VENDORS for fam, rec in base.families[v].items()}
    for vendor in VENDORS:  # the suggestions follow the heads; one that would now descend a generation is dropped
        edges = {}
        for src, dst in base.fallback[vendor].items():
            src2, dst2 = heads.get(family_of_head.get(src), src), heads.get(family_of_head.get(dst), dst)
            if _generation_key(data["models"][dst2]["generation"]) >= _generation_key(data["models"][src2]["generation"]):
                edges[src2] = dst2
        data["fallback"][vendor] = edges
    digest = hashlib.sha256((base.sha256 + json.dumps({"applied": applied, "blocked": sorted(blocked), "kept": sorted(kept)}, sort_keys=True,
                                                      default=str)).encode())
    out = Table(data, base.path, sha256=digest.hexdigest(), effective=True)
    out.base, out.blocked = base, blocked
    out.promotion = {"applied": applied, "skipped": skipped, "ignored": ignored, "ledger": str(ledger_path) if ledger_path else None}
    return out


def plan_promotions(table, observation, markers=None, now=None):
    """The promotion pass (pure): from ONE observation of this Mac's own Codex catalog that THIS refresh read successfully (a retained
    observation as retain_catalog keeps it, with the reading CLI's `--version` line under `cli`), the steps that move a Codex family's
    head to a newer listed model. `table` is the effective table before the pass, `markers` the store's sticky markers.
    Returns {"steps": [new steps], "held": [{"family", "id", "reason"}], "reason": why the catalog could not be considered, or None}.

    The catalog counts only when it is applicable (every catalog configuration key at its default), a login stands behind it
    (account/read in the same session: without one, model/list answers the CLI's bundled list) and the reading CLI's version parses
    (it becomes the new model's floor). A listed model is a candidate for a family when it is explicitly not hidden; its id is the
    family's id_glob with a dotted number for the `*` (gpt-<number>-terra; never gpt-<number>-mini-terra or a codename); no family knows it
    and no refusal names it (a `*` pattern included); it was not undone on this Mac; it carries no lifecycle statement (a status
    other than active, current or available, a deprecated flag, an upgrade pointer, a retirement date, deprecation prose) and no
    sticky marker; its efforts are all Codex efforts and include every effort of the family's current head (no seat valid today
    becomes invalid); and its generation is above the head's. A family's highest candidate wins (two of one generation hold the
    family back); the lower ones are recorded as superseded. Every listed model that matches a family's pattern but fails a gate is
    `held`, with the reason (doctor shows them; the check's `stale` line still reports each)."""
    now = time.time() if now is None else now
    out = {"steps": [], "held": [], "reason": None}
    if not isinstance(observation, dict) or not isinstance(observation.get("models"), list):
        out["reason"] = "the catalog was not read in this check"
        return out
    keys = observation.get("config_keys")
    if not isinstance(keys, dict) or any(v != "default" for v in keys.values()):
        out["reason"] = "the catalog is not applicable (a catalog configuration key is non-default, or config/read could not be read)"
        return out
    account = observation.get("account") if isinstance(observation.get("account"), dict) else {}
    if account.get("signed_in") is not True:
        out["reason"] = ("no login stands behind the catalog (account/read: not signed in)" if account.get("signed_in") is False else
                         f"the login behind the catalog could not be confirmed ({account.get('error') or 'no account/read answer'})")
        return out
    version = parse_version("codex", observation.get("cli") or "")
    if version is None:
        out["reason"] = f"the reading CLI's version could not be read ({first_line(observation.get('cli') or '')[:60] or 'none'})"
        return out
    vocab = table.efforts["codex"]
    marked = canonical_markers(table, markers) if markers is not None else {}
    candidates = {}  # family -> [(generation order, generation text, id, efforts)]
    for entry in observation["models"]:
        if not isinstance(entry, dict):
            continue
        rec = lifecycle_record(entry)
        mid = table.canonical(rec["id"]) if isinstance(rec["id"], str) else None
        if not mid:
            continue
        fam, gen = None, None
        for name, frec in table.families["codex"].items():
            pattern = generation_pattern(frec.get("id_glob"))
            m = pattern.match(mid) if pattern else None
            if m:
                fam, gen = name, m.group(1)
                break
        if fam is None:  # not a numbered generation of any family; a visible name that only the looser id_glob matches is reported
            loose = table.family_matching("codex", mid)
            if loose and entry.get("hidden") is False and table.family_of(mid) is None and not table.refusal("codex", mid):
                out["held"].append({"family": loose, "id": mid, "reason": f"not a numbered generation of {loose} "
                                                                          f"({table.families['codex'][loose].get('id_glob')} with a number for *)"})
            continue
        if table.family_of(mid) is not None and mid not in table.blocked:
            continue  # a member already: a head, a retired predecessor, a listed spelling
        head = table.head(fam)
        cur = generation_order(table.models[head]["generation"])
        if cur is None or generation_order(gen) <= cur:
            continue  # not newer than the head: not a promotion question (the `stale` line, if any, still reports it)

        def hold(reason):
            out["held"].append({"family": fam, "id": mid, "reason": reason})

        efforts = [x.get("reasoningEffort") for x in entry.get("supportedReasoningEfforts") or [] if isinstance(x, dict)]
        if entry.get("hidden") is not False:
            hold("not listed as visible (hidden is not false)")
        elif table.refusal("codex", mid) or table.refusal("claude", mid):
            hold("a refused name of the model table")
        elif mid in table.blocked:
            hold(f"blocked on this Mac: {blocked_reason(table.blocked[mid])}")
        elif rec["state"] not in PROMOTABLE_STATES:
            hold(f"its catalog status is '{rec['state']}'")
        elif marker_kinds(rec, now) or rec["upgrade_to"] or rec["deprecated"]:
            hold("the catalog carries a lifecycle statement for it (" + ", ".join(sorted(marker_kinds(rec, now)) or ["an upgrade pointer"]) + ")")
        elif mid in marked:
            hold(f"the evidence store marks it {marked[mid].get('kind')}")
        elif not efforts or any(e not in vocab for e in efforts):
            hold("its efforts are not Codex efforts (" + (", ".join(str(e) for e in efforts) or "none listed") + ")")
        elif not set(table.supported_efforts(head)) <= set(efforts):
            hold(f"it lacks efforts {head} offers ({', '.join(e for e in table.supported_efforts(head) if e not in efforts)})")
        else:
            candidates.setdefault(fam, []).append((generation_order(gen), gen, mid, sorted(set(efforts), key=vocab.index)))
    for fam, found in candidates.items():
        found.sort(key=lambda c: c[0], reverse=True)
        best = found[0]
        if len(found) > 1 and found[1][0] == best[0]:
            for c in found:
                if c[0] == best[0]:
                    out["held"].append({"family": fam, "id": c[2], "reason": f"two candidates share generation {best[1]}"})
            continue
        out["steps"].append({"vendor": "codex", "family": fam, "from": table.head(fam), "to": best[2], "generation": best[1],
                             "efforts": best[3], "min_cli": version.text, "superseded": [c[2] for c in found[1:]], "at": now,
                             "observed_at": observation.get("read_at"), "cli": first_line(observation.get("cli")),
                             "account": {k: account.get(k) for k in ("type", "plan")}})
    return out


def promote(base, ledger, observation, markers=None, now=None, ledger_path=None):
    """Under the evidence lock, after a refresh read the Codex catalog: the promotion pass (plan_promotions) over the table `base` (the
    shipped table) and the ledger as they are now give, recorded in a copy of the ledger: its `last_pass`, any new steps, one log line
    per step. The new effective table is built (and so checked) before anything is returned; a pass whose steps the table cannot take
    records that and adds no step. Returns (ledger, effective table, new steps)."""
    now = time.time() if now is None else now
    base = base.base
    new = copy.deepcopy(ledger if isinstance(ledger, dict) else empty_ledger())
    plan = plan_promotions(effective_table(base, new, ledger_path), observation, markers, now)
    steps = plan["steps"]
    result = plan["reason"] or ("promoted " + ", ".join(f"{s['family']} {s['from']} -> {s['to']}" for s in steps) if steps else "nothing newer to adopt")
    if steps:
        trial = dict(new, steps=new["steps"] + steps)
        try:
            after = effective_table(base, trial, ledger_path)
        except TableError as exc:
            steps, result, after = [], f"held: the table could not take the promotion ({exc})", None
        else:
            new = trial
            new["log"] = new["log"] + [{"at": now, "event": "promoted", "family": s["family"], "from": s["from"], "to": s["to"], "cli": s["cli"]}
                                       for s in steps]
    new["last_pass"] = {"at": now, "observed_at": observation.get("read_at") if isinstance(observation, dict) else None,
                        "cli": first_line(observation.get("cli")) if isinstance(observation, dict) else None, "result": result,
                        "held": plan["held"][:20]}
    if not steps:
        after = effective_table(base, new, ledger_path)
    return new, after, steps


def undo_promotion(base, ledger, family, reason, now=None, ledger_path=None):
    """`doctor --undo-promotion <family> --reason <text>` (pure): the family's last applied step leaves the ledger's steps, its model is
    blocked on this Mac (never promoted here again and refused by name until the shipped table names it), and the log records it;
    the family's head becomes what the remaining steps give. Only the model the user undid is blocked: another newer model the
    catalog lists (one the step had superseded) stays a candidate, adopted by the next pass with its own announcement and notice
    (Fable's review of 2026-11-07: a block the user never asked for would hold a newer model back for good). Returns (ledger, the
    undone step, the new effective table); PromotionsError when no automatic promotion of the family is in effect."""
    now = time.time() if now is None else now
    fam = str(family or "").strip().lower()
    if fam not in base.families["codex"]:
        raise PromotionsError(f"'{family}' is not a Codex family ({', '.join(base.families['codex'])}); only Codex heads are promoted automatically")
    current = effective_table(base, ledger, ledger_path)
    applied = [s for s in current.promotion["applied"] if s["family"] == fam]
    if not applied:
        raise PromotionsError(f"no automatic promotion of {fam} is in effect on this Mac; its head {current.head(fam)} is the shipped table's")
    step = applied[-1]
    new = copy.deepcopy(ledger)
    new["steps"] = [s for s in new["steps"] if not (s["family"] == fam and s["to"] == step["to"])]
    new["blocked"] = new["blocked"] + [{"vendor": "codex", "family": fam, "id": step["to"], "at": now, "reason": reason, "step": step}]
    after = effective_table(base, new, ledger_path)
    new["log"] = new["log"] + [{"at": now, "event": "undone", "family": fam, "from": step["to"], "to": after.head(fam), "reason": reason}]
    return new, step, after


def promotion_notice(table, model_id, now=None):
    """The notice a new job leads with while its seat runs an automatically promoted head (PROMOTION_NOTICE_DAYS after the step), or
    None."""
    step = table.promotion_step(model_id)
    now = time.time() if now is None else now
    if step is None or now - (_epoch(step["at"]) or 0) > PROMOTION_NOTICE_DAYS * 86400:
        return None
    return (f"NOTICE: models: {step['family']} runs {step['to']}, promoted automatically on {_date_text(step['at'])} from {step['from']} "
            f"(this Mac's own Codex catalog lists it, {step.get('cli') or 'codex'}); "
            f"to go back: bridge.py doctor --undo-promotion {step['family']} --reason \"<why>\"")

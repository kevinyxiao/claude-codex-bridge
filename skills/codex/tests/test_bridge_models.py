"""Pure tests of skills/codex/scripts/bridge_models.py (DESIGN rev 13 §1-§5, §8 groups A and E): the loader's invariants,
explicit path binding, and evaluate() on the real captures in fixtures/evidence with the clock frozen at
2026-09-25T03:20:00Z. Behaviour through the engine CLI is tested in test_bridge.py; these add the module-level checks.

The interface these tests assume. DESIGN §1 gives `Table.load(path)` and `Evidence(path)`; §9 names `evaluate`; §5 gives
its inputs (the four sources, the local facts) and outputs (the verdict, finding classes and lines, markers). The call
shape below is fixed HERE because the design does not state it (TESTS_REPORT_engine.md, "Design questions"):

    Table.load(path) -> Table       raises an Exception whose str() is ONE line for any §1 invariant violation
    Evidence(path)                  bound to `path` (exposed as .path), whatever BRIDGE_MODELS_EVIDENCE says
    evaluate(table, sources, local=None, now=None) -> dict
      sources = {"codex": {"config": <config/read result>, "models": [<model/list entries>]} or None,
                 "npm": <registry JSON object> or None,
                 "deprecations": <Markdown page text> or None,
                 "overview": <Markdown page text> or None}           (None: the source could not be read)
      local   = {"codex_version": <first line of `codex --version`>, "claude_version": <first line of `claude --version`>,
                 "relay_claude_version": <first line, or None>, "env": {<variable>: <value>}}   (None: remote findings only)
      now     = epoch seconds
      returns {"verdict": "OK" | "FAIL" | "UNVERIFIED",
               "findings": [{"class": lifecycle|missing|stale|local|unverified|cli|host|defaults|discovery|info, "text": <line>}],
               "markers": [{"id", "kind": deprecated|retiring|retired, "source", "detail"}]}   (derived from these sources)
"""
import copy
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
MODULE = SCRIPTS / "bridge_models.py"
FIXTURES = TESTS / "fixtures"
EVIDENCE = FIXTURES / "evidence"
MODELS_JSON = TESTS.parent / "models.json"  # the shipped table, the only one the tests read (DESIGN D23)
FROZEN_NOW = 1790306400  # 2026-09-25T03:20:00Z
DAY = 86400
SENTINEL_TOKEN = "SENTINEL-7f3a9c2e41d0-DO-NOT-PRINT"
FAIL_CLASSES = {"lifecycle", "missing", "stale", "local", "cli", "host", "defaults", "discovery"}

if not MODELS_JSON.is_file():
    raise FileNotFoundError(f"DESIGN §1: the model table must ship at {MODELS_JSON}; the tests read no other table")
TABLE = json.loads(MODELS_JSON.read_text())
sys.path.insert(0, str(FIXTURES))
import catalog_fixture  # noqa: E402  the tests' clean catalog: the newest real capture, completed for the table under test
CATALOG_0_156_1 = json.loads((EVIDENCE / "codex_model_list_cli0.156.1.json").read_text())["models"]
CATALOG_0_159_3 = json.loads((EVIDENCE / "codex_model_list_cli0.159.3.json").read_text())["models"]  # Kevin's Sol checkpoint (2026-10-01)
CATALOG_0_154_0 = json.loads((EVIDENCE / "codex_model_list_cli0.154.0.json").read_text())["models"]
CATALOG_NEWEST = catalog_fixture.load_models()  # the newest real capture, raw (0.167.0, 2026-11-08: lists every head of table v21)
# the clean catalog every test reads unless it says otherwise: the newest capture with a derived entry for any shipped head it lacks
# (a head moved by a human checkpoint breaks only test_the_newest_real_list_with_the_shipped_table_is_exactly_ok, which reads it raw)
CATALOG_CURRENT, DERIVED_HEADS = catalog_fixture.complete(CATALOG_NEWEST, TABLE)
CONFIG_DEFAULT = {k: v for k, v in json.loads((EVIDENCE / "codex_config_read_all_default.json").read_text()).items() if k != "_provenance"}
CONFIG_CATALOG_JSON = {k: v for k, v in json.loads((EVIDENCE / "codex_config_read_model_catalog_json.json").read_text()).items() if k != "_provenance"}
CONFIG_SENTINEL = {k: v for k, v in json.loads((EVIDENCE / "codex_config_read_unused_provider_sentinel.json").read_text()).items()
                   if k != "_provenance"}
# L1/L2: a real config/read answer (Kevin's Mac, CLI 0.156.1, 2026-09-25; catalog keys only, sanitized): RESOLVED defaults, so
# chatgpt_base_url has a value and no origin; service_tier = "priority" from the user layer
CONFIG_CAPTURE = {k: v for k, v in json.loads((EVIDENCE / "codex_config_read_catalog_keys_20260925_cli0.156.1_sanitized.json")
                                              .read_text()).items() if k != "_provenance"}
NPM = {k: v for k, v in json.loads((EVIDENCE / "npm_dist_tags.json").read_text()).items() if k != "_provenance"}
# briefs/npm_round.md: the check's default npm URL is npm's dist-tags endpoint; its REAL answer of 2026-09-25 (byte-identical,
# 596 bytes): the tags at the top level, no `dist-tags` wrapper, latest 0.157.0
NPM_ENDPOINT_URL = "https://registry.npmjs.org/-/package/@openai/codex/dist-tags"
NPM_ENDPOINT_TEXT = (EVIDENCE / "npm_dist_tags_endpoint_20260925.json").read_text()
# the pages of 2026-10-08: they list every Claude head of the table (the sonnet head moved in the table's v18; C7's pages of
# 2026-09-29 are read by test_bridge_cleanup_c7.py)
DEPRECATIONS =(EVIDENCE / "anthropic_model-deprecations_20261008.md").read_text()
OVERVIEW = (EVIDENCE / "anthropic_models-overview_20261008.md").read_text()
DEPRECATIONS_NO_OPUS_5_5 = (EVIDENCE / "anthropic_model-deprecations_20261008_without-opus-5-5.md").read_text()
OVERVIEW_NO_OPUS_5_5 = (EVIDENCE / "anthropic_models-overview_20261008_without-opus-5-5.md").read_text()
DEPRECATIONS_MANGLED = (EVIDENCE / "anthropic_model-deprecations_20261008_mangled-status-table.md").read_text()


def _version_key(text):
    return tuple(int(x) for x in text.split("-")[0].split("."))


def at_or_above_every_floor(vendor, baseline):
    """The highest `min_cli` of the shipped table's models of `vendor`, or `baseline` when that is higher: a CLI version every test that
    is not about floors can run with, whatever floor a checkpoint sets (the floor tests name their versions themselves)."""
    family_vendor = {fam: v for v in ("codex", "claude") for fam in TABLE["families"][v]}
    floors = [rec["min_cli"] for rec in TABLE["models"].values() if rec.get("min_cli") and family_vendor.get(rec.get("family")) == vendor]
    return max(floors + [baseline], key=_version_key)


def next_minor(version):
    """The release after `version` that npm would call newer (0.156.1 -> 0.157.0)."""
    major, minor = version.split(".")[:2]
    return f"{major}.{int(minor) + 1}.0"


LOCAL_CODEX = at_or_above_every_floor("codex", "0.156.1")   # 0.156.1 since table v14 (luna's floor; the synthetic npm latest)
LOCAL_CLAUDE = at_or_above_every_floor("claude", "2.1.295")  # 2.1.295 since table v20 (haiku's floor)
LOCAL = {"codex_version": f"codex-cli {LOCAL_CODEX}", "claude_version": f"{LOCAL_CLAUDE} (Claude Code)",
         "relay_claude_version": f"{LOCAL_CLAUDE} (Claude Code)", "env": {}}


def head(family):
    vendor = "codex" if family in TABLE["families"]["codex"] else "claude"
    return TABLE["families"][vendor][family]["known_good"]


# The ids a test needs beside the heads come from the table (docs/model-routing.md 9.1, 10.1: a versioned id written into a test is
# a lint error unless the table supplies it or it is one of the registry's fictional fixture ids, registry.json lint.fixture_ids)
K1_ID = TABLE["k1_seat"]["id"]
HAIKU_ALIAS = "claude-haiku-9-8"  # synthetic distinct spelling; the current dateless API alias equals its ID
RETIRED_SOL = next(x for x in TABLE["retired"]["codex"]["sol"] if x not in TABLE["refused"]["codex"])
RETIRED_LUNA = TABLE["retired"]["codex"]["luna"][0]
RETIRED_OPUS = [x for x in TABLE["retired"]["claude"]["opus"] if x != K1_ID]                        # newest first
RETIRING_FULL = next(k for k, v in TABLE["refused"]["codex"].items() if v.get("same_as") == "5.5")           # the retiring model's full spelling
REFUSED_SOL_NAME = next(k for k, v in TABLE["refused"]["codex"].items() if v.get("family") == "sol" and "id" not in v and "same_as" not in v)
NEWER_SOL, NEWER_SOL2, NEWER_LUNA, NEWER_TERRA = "gpt-9-sol", "gpt-9.9-sol", "gpt-9.9-luna", "gpt-9-terra"  # fictional successors
NEWER_OPUS, NEWER_HAIKU, NEWER_FABLE = "claude-opus-9-9", "claude-haiku-9-9", "claude-fable-9-9"


def with_haiku_alias(t):
    """A derived table with a distinct spelling to keep alias canonicalization regressions meaningful."""
    t["models"][t["families"]["claude"]["haiku"]["known_good"]]["aliases"].append(HAIKU_ALIAS)


def entry(model_id, efforts=("low", "medium", "high", "xhigh", "max", "ultra"), hidden=False, **extra):
    e = {"id": model_id, "model": model_id, "upgrade": None, "upgradeInfo": None, "displayName": model_id, "description": "",
         "hidden": hidden, "isDefault": False, "defaultReasoningEffort": "medium",
         "supportedReasoningEfforts": [{"reasoningEffort": x, "description": x} for x in efforts]}
    e.update(extra)
    return e


def catalog(mutate=None, base=None):
    models = copy.deepcopy(CATALOG_CURRENT if base is None else base)
    if mutate:
        mutate(models)
    return models


def find(models, model_id):
    return next(m for m in models if m["id"] == model_id)


def sources(models=None, config=None, npm=None, deprecations=None, overview=None, unread=()):
    s = {"codex": {"config": copy.deepcopy(config or CONFIG_DEFAULT), "models": models if models is not None else catalog()},
         "npm": npm if npm is not None else copy.deepcopy(NPM),
         "deprecations": deprecations if deprecations is not None else DEPRECATIONS,
         "overview": overview if overview is not None else OVERVIEW}
    for key in unread:
        s[key] = None
    return s


def deprecations_with(model_id, state, deprecated, retirement, base=DEPRECATIONS):
    row = f"| {model_id} | {state} | {deprecated} | {retirement} |"
    pattern = re.compile(r"(?m)^\| " + re.escape(model_id) + r" +\|.*$")
    if pattern.search(base):
        return pattern.sub(row, base, count=1)
    anchor = base.index(f"| {head('fable')} ")
    return base[:anchor] + row + "\n" + base[anchor:]


def overview_api_id(old, new, base=OVERVIEW):
    """The overview with one cell of the "Claude API ID" row replaced."""
    lines = base.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("| Claude API ID "):
            assert f"`{old}`" in line
            lines[i] = line.replace(f"`{old}`", f"`{new}`")
    return "".join(lines)


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


class ModuleCase(unittest.TestCase):
    """Loads bridge_models.py fresh for every test class; a missing module is an assertion failure, not an import error.
    The module is registered in sys.modules while it executes, as a normal import is (dataclasses with postponed
    annotations look their module up there); the previous entry is restored after a failed load and at teardown."""

    MODULE_NAME = "bridge_models_pure_test"
    _ABSENT = object()

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-models-test-"))
        cls.bm = None
        cls._previous = sys.modules.get(cls.MODULE_NAME, cls._ABSENT)
        if MODULE.is_file():
            spec = importlib.util.spec_from_file_location(cls.MODULE_NAME, MODULE)
            mod = importlib.util.module_from_spec(spec)
            sys.path.insert(0, str(SCRIPTS))
            sys.modules[cls.MODULE_NAME] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException:
                cls._restore_module()
                raise
            cls.bm = mod

    @classmethod
    def _restore_module(cls):
        if cls._previous is cls._ABSENT:
            sys.modules.pop(cls.MODULE_NAME, None)
        else:
            sys.modules[cls.MODULE_NAME] = cls._previous

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls._restore_module()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertIsNotNone(self.bm, f"DESIGN §0/§9: the table's one reader must exist at {MODULE}")

    def write_table(self, name, mutate=None, base=None):
        t = copy.deepcopy(base or TABLE)
        if mutate:
            mutate(t)
        p = self.tmp / f"{name}.json"
        p.write_text(json.dumps(t, indent=1))
        return p

    def load(self, mutate=None, name="table"):
        return self.bm.Table.load(str(self.write_table(name, mutate)))

    def shipped(self):
        return self.bm.Table.load(str(MODELS_JSON))

    def evaluate(self, table=None, srcs=None, local=LOCAL, now=FROZEN_NOW):
        result = self.bm.evaluate(table or self.shipped(), srcs if srcs is not None else sources(), local, now)
        self.assertIsInstance(result, dict)
        self.assertIn(result.get("verdict"), ("OK", "FAIL", "UNVERIFIED"), result)
        return result

    @staticmethod
    def texts(result):
        return [f["text"] if isinstance(f, dict) else str(f) for f in result.get("findings") or []]

    @staticmethod
    def classes(result):
        return {f.get("class") for f in result.get("findings") or [] if isinstance(f, dict)}

    @staticmethod
    def of_class(result, cls):
        return [f["text"] for f in result.get("findings") or [] if isinstance(f, dict) and f.get("class") == cls]

    @staticmethod
    def markers(result):
        found = result.get("markers") or []
        if isinstance(found, dict):
            found = [dict(v, id=v.get("id") or k) for k, v in found.items()]
        return found

    def marker_kinds(self, result, model_id):
        return {m.get("kind") for m in self.markers(result) if m.get("id") == model_id}

    def assert_one_line_error(self, path):
        with self.assertRaises(Exception) as cm:
            self.bm.Table.load(str(path))
        msg = str(cm.exception).strip()
        self.assertTrue(msg, "the loader's error names nothing")
        self.assertNotIn("\n", msg, f"a table error must be one line: {msg!r}")
        return msg


# ==================================================================================================================
# group A (pure): the loader, known members, explicit binding
# ==================================================================================================================
class GroupA_TableModule(ModuleCase):

    def test_the_shipped_table_loads(self):
        self.assertTrue(MODELS_JSON.is_file(), f"DESIGN §1: the table must ship at {MODELS_JSON}")
        self.assertIsNotNone(self.shipped())

    def test_loader_rejects_cross_generation_or_non_head_fallback_edges(self):
        # a suggestion never descends to an older generation. Each cross-generation case sets the target's generation below the
        # source's itself, so the case does not depend on which shipped head happens to be older (since Kevin's checkpoint of
        # 2026-11-07 the standard head, terra, is newer than the top head, astra, the case used to point from)
        def descend(vendor, src, dst):
            def mutate(t):
                t["models"][dst]["generation"] = "0.1"  # below every real generation
                t["fallback"][vendor][src] = dst
            return mutate
        cases = {"cross-generation": descend("codex", head("astra"), head("terra")),
                 "target-not-a-head": lambda t: t["fallback"]["codex"].__setitem__(head("astra"), RETIRED_SOL),
                 "source-not-a-head": lambda t: t["fallback"]["codex"].__setitem__(RETIRED_SOL, head("sol")),
                 "claude-cross-generation": descend("claude", head("opus"), head("fable"))}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.assert_one_line_error(self.write_table(f"fallback-{name}", mutate))

    def test_every_refusal_record_has_a_family_and_a_reason(self):
        cases = {"no-family": ("codex", REFUSED_SOL_NAME, {"reason": "in no catalog"}),
                 "empty-reason": ("codex", REFUSED_SOL_NAME, {"family": "sol", "reason": "  "}),
                 "unknown-family": ("codex", REFUSED_SOL_NAME, {"family": "nova", "reason": "in no catalog"}),
                 "unresolvable-same-as": ("codex", RETIRING_FULL, {"same_as": "gpt-no-such-record"}),
                 "chained-same-as": ("codex", RETIRING_FULL, {"same_as": "gpt6"}),
                 "cross-vendor-same-as": ("codex", RETIRING_FULL, {"same_as": "opus5"}),
                 "claude-no-reason": ("claude", "fable5", {"family": "fable"})}
        for name, (vendor, key, rec) in cases.items():
            with self.subTest(case=name):
                self.assert_one_line_error(self.write_table(f"refusal-{name}", lambda t: t["refused"][vendor].__setitem__(key, rec)))

    def test_loader_rejects_the_other_invariant_violations(self):
        # DESIGN §1: `k1_seat.id` is in `models` and is a `retired` predecessor of its family
        def k1_not_in_models(t):
            t["k1_seat"]["id"] = next(x for x in RETIRED_OPUS if x not in t["models"])   # a retired Opus, but not in `models`

        def k1_not_a_retired_predecessor(t):
            k1 = t["k1_seat"]["id"]                         # still in `models`, no longer in its family's `retired` list
            family = t["models"][k1]["family"]
            t["retired"]["claude"][family] = [x for x in t["retired"]["claude"][family] if x != k1]

        def k1_is_a_current_head(t):
            family = t["models"][t["k1_seat"]["id"]]["family"]
            t["k1_seat"]["id"] = t["families"]["claude"][family]["known_good"]    # in `models`, but its family's head

        cases = {"k1-id-not-in-models": k1_not_in_models, "k1-id-not-a-retired-predecessor": k1_not_a_retired_predecessor,
                 "k1-id-is-a-current-head": k1_is_a_current_head,
                 "retired-collides-with-a-head": lambda t: t["retired"]["codex"]["sol"].append(head("sol")),
                 "refused-collides-with-an-alias": lambda t: t["refused"]["claude"].__setitem__(
                     TABLE["models"][TABLE["retired"]["claude"]["haiku"][0]]["aliases"][0], {"family": "haiku", "reason": "x"}),
                 "generation-not-a-string": lambda t: t["models"][head("sol")].__setitem__("generation", 6),
                 "min-cli-not-a-string-or-null": lambda t: t["models"][head("sol")].__setitem__("min_cli", 156),
                 "head-not-in-models": lambda t: t["families"]["codex"]["sol"].__setitem__("known_good", NEWER_SOL2),
                 "head-in-another-family": lambda t: t["models"][head("sol")].__setitem__("family", "luna")}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.assert_one_line_error(self.write_table(f"invariant-{name}", mutate))

    def test_the_shipped_k1_seat_is_on_and_only_the_seat_reaches_its_target(self):
        # ruling K1-ON (review/ruling_k1_on.md, Kevin 2026-09-26): the shipped table switches the K1 seat on. `opus-5` resolves to
        # the K1 target as the K1 seat (tag opus-5, family opus), `opus` still means the Opus head, and the target's bare id is
        # still refused exactly as today. First the off state, a variant table: the seat is refused with its line
        k1, opus = TABLE["k1_seat"], head("opus")
        off = self.load(lambda t: t["k1_seat"].__setitem__("enabled", False), "k1-off")
        with self.assertRaises(self.bm.Refusal) as cm:
            off.resolve(k1["seat"])
        self.assertEqual(str(cm.exception), f"the {k1['seat']} seat is switched off (models.json k1_seat; Kevin K1). Use opus ({opus}).")
        t = self.shipped()
        self.assertTrue(t.k1_enabled(), "ruling K1-ON: the shipped table switches the K1 seat on")
        r = t.resolve(k1["seat"])
        self.assertEqual((r.vendor, r.model, r.family, r.tag, r.k1), ("claude", k1["id"], "opus", k1["seat"], True))
        for spec, effort in ((k1["seat"], "max"), (k1["seat"] + ":high", "high")):  # opus-5[:effort]; Claude's built-in default is max
            s = t.parse_seat(spec)
            self.assertEqual((s.model, s.tag, s.k1, s.effort), (k1["id"], k1["seat"], True, effort), spec)
        r = t.resolve("opus")
        self.assertEqual((r.model, r.tag, r.k1), (opus, "opus", False))
        with self.assertRaises(self.bm.Refusal) as cm:
            t.resolve(k1["id"])
        self.assertEqual(str(cm.exception), f"{k1['id']} is superseded by {opus}. Use opus ({opus}).")

    def test_a_head_record_that_is_not_an_object_names_the_table_and_the_model(self):
        # Astra C1-5: every record is type-checked before it is read; the one-line error names the table path and the offending
        # ID (DESIGN §1, R-E8), not an interpreter message such as "'NoneType' object has no attribute 'get'"
        sol = head("sol")
        for name, value in (("null", None), ("array", [])):
            with self.subTest(record=name):
                path = self.write_table(f"head-record-{name}", lambda t: t["models"].__setitem__(sol, value))
                msg = self.assert_one_line_error(path)
                self.assertIn(str(path), msg)
                self.assertIn(sol, msg)

    def test_known_members_are_head_predecessors_and_aliases(self):
        # the real list: every listed Sol/Luna ID is the head or a retired predecessor -> no stale finding
        self.assertEqual(self.of_class(self.evaluate(), "stale"), [])
        # a predecessor removed from the table makes the unchanged catalog read stale (DESIGN §10 risk 3)
        t = self.load(lambda t: t["retired"]["codex"]["sol"].remove(RETIRED_SOL), "no-predecessor")
        self.assertIn(f"stale: newer model may exist: {RETIRED_SOL} is listed but not in the model table", self.texts(self.evaluate(t)))
        # `refused` entries and `history` are not members
        t = self.load(lambda t: (t["refused"]["codex"].__setitem__(NEWER_SOL2, {"family": "sol", "reason": "test"}),
                                 t["history"].append({"kind": "bundled_upgrade_pointer", "from": head("terra"), "to": NEWER_TERRA,
                                                      "retirement_at": None, "observed": "2026-09-24", "source": "test", "decision": "test"})),
                      "refused-and-history")
        listed = catalog(lambda ms: ms.extend([entry(NEWER_SOL2), entry(NEWER_TERRA)]))
        texts = self.texts(self.evaluate(t, sources(listed)))
        for model_id in (NEWER_SOL2, NEWER_TERRA):
            self.assertIn(f"stale: newer model may exist: {model_id} is listed but not in the model table", texts)
        # a head's listed spelling is a member: the overview naming Haiku by its alias is neither stale nor missing
        r = self.evaluate(self.load(with_haiku_alias, "head-alias"), srcs=sources(overview=overview_api_id(head("haiku"), HAIKU_ALIAS)))
        self.assertEqual(r["verdict"], "OK", r)

    def test_the_module_binds_to_explicit_table_and_evidence_paths(self):
        good = MODELS_JSON
        bad = self.tmp / "malformed.json"
        bad.write_text('{"version": ')
        saved = dict(os.environ)
        try:
            os.environ["BRIDGE_MODELS_FILE"] = str(bad)
            self.assertIsNotNone(self.bm.Table.load(str(good)))       # the environment does not redirect an explicit path
            os.environ["BRIDGE_MODELS_FILE"] = str(good)
            with self.assertRaises(Exception):
                self.bm.Table.load(str(bad))
            here, elsewhere = self.tmp / "store" / "models-evidence.json", self.tmp / "elsewhere" / "models-evidence.json"
            os.environ["BRIDGE_MODELS_EVIDENCE"] = str(elsewhere)
            ev = self.bm.Evidence(str(here))
            self.assertEqual(os.path.realpath(str(ev.path)), os.path.realpath(str(here)))
        finally:
            os.environ.clear()
            os.environ.update(saved)


# ==================================================================================================================
# group E (pure evaluate): stale, coverage, applicability, Anthropic pages, discovery, marker predicates, local findings
# ==================================================================================================================
class GroupE_Evaluate(ModuleCase):

    def old_heads(self, t):
        """The pre-change heads: Sol and Luna still their retired predecessors (bridge.py:86-87 before this change)."""
        for fam, old, gen in (("sol", RETIRED_SOL, RETIRED_SOL.split("-")[1]), ("luna", RETIRED_LUNA, RETIRED_LUNA.split("-")[1])):
            new = t["families"]["codex"][fam]["known_good"]
            t["models"][old] = dict(t["models"][new], generation=gen, min_cli="0.144.0", source="test: pre-change head")
            del t["models"][new]
            t["families"]["codex"][fam]["known_good"] = old
            t["retired"]["codex"][fam] = [x for x in t["retired"]["codex"][fam] if x != old]
        t["fallback"]["codex"] = {}

    def test_a_fake_live_list_with_gpt_7_sol_reports_stale(self):
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry(NEWER_SOL)))))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn(f"stale: newer model may exist: {NEWER_SOL} is listed but not in the model table", self.of_class(r, "stale"))
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry("gpt-9.9-luna", efforts=("low", "medium", "high", "xhigh", "max"))))))
        self.assertIn("stale: newer model may exist: gpt-9.9-luna is listed but not in the model table", self.texts(r))
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry("gpt-9-nova")))))  # matches no family glob
        self.assertFalse([x for x in self.texts(r) if "gpt-9-nova" in x and x.startswith("stale:")])

    def test_the_newest_real_list_with_the_shipped_table_is_exactly_ok(self):
        # the shipped table against real evidence, not the completed catalog: every Codex head listed, visible, with the table's
        # efforts. A human checkpoint that moves a head past this capture fails here (and only here): capture a model/list that
        # lists it (fixtures/evidence/PROVENANCE.md) and point catalog_fixture.NEWEST at it
        self.assertEqual(DERIVED_HEADS, [], f"{catalog_fixture.NEWEST.name} does not list the shipped head(s) {DERIVED_HEADS}: capture a "
                                            f"model/list that does and point catalog_fixture.NEWEST at it")
        r = self.evaluate(srcs=sources(catalog(base=CATALOG_NEWEST)))
        self.assertEqual(r["verdict"], "OK", r)
        self.assertEqual(self.classes(r) & (FAIL_CLASSES | {"unverified"}), set(), r)
        for fam in TABLE["families"]["codex"]:
            self.assertEqual(self.marker_kinds(r, head(fam)), set())
        for fam in TABLE["families"]["claude"]:
            self.assertEqual(self.marker_kinds(r, head(fam)), set())  # "Not sooner than" is not a schedule

    def test_the_real_0_159_3_list_marks_its_retiring_pointer_informational(self):
        # the capture of Kevin's Sol checkpoint carries a live upgrade pointer with a retirement date (RETIRING_FULL -> the Sol head,
        # retiring 2026-10-14): a retiring marker on a retired predecessor, informational; no marker on any head. The capture predates
        # the Terra checkpoint, so its derived completion supplies the Terra head
        r = self.evaluate(srcs=sources(catalog(base=catalog_fixture.complete(CATALOG_0_159_3, TABLE)[0])))
        self.assertEqual(r["verdict"], "OK", r)
        self.assertEqual(self.classes(r) & (FAIL_CLASSES | {"unverified"}), set(), r)
        self.assertIn("retiring", self.marker_kinds(r, RETIRING_FULL))  # the live pointer with a future date: informational
        self.assertIn(f"marker on {RETIRING_FULL}: informational", self.of_class(r, "info"))
        for fam in TABLE["families"]["codex"]:
            self.assertEqual(self.marker_kinds(r, head(fam)), set())

    def test_the_real_0_154_0_list_reports_cli_and_local_failures(self):
        r = self.evaluate(srcs=sources(catalog(base=CATALOG_0_154_0)), local=dict(LOCAL, codex_version="codex-cli 0.154.0"))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("cli", self.classes(r))
        self.assertIn("local", self.classes(r))
        local = " ".join(self.of_class(r, "local"))
        self.assertIn(head("sol"), local)
        self.assertIn(head("luna"), local)
        self.assertIn("0.156.1", " ".join(self.of_class(r, "cli")))
        self.assertEqual(self.of_class(r, "stale"), [])  # the retired Sol and Luna predecessors are known members

    def test_old_sol_and_luna_heads_are_stale_against_the_real_gpt6_list(self):
        r = self.evaluate(self.load(self.old_heads, "old-heads"))
        self.assertEqual(r["verdict"], "FAIL")
        for model_id in (head("sol"), head("luna")):
            self.assertIn(f"stale: newer model may exist: {model_id} is listed but not in the model table", self.texts(r))

    def test_a_hidden_unknown_member_is_not_stale(self):
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry(NEWER_SOL, hidden=True)))))
        self.assertEqual(r["verdict"], "OK", r)
        self.assertEqual(self.of_class(r, "stale"), [])

    def test_a_live_effort_set_difference_is_stale(self):
        def widen(ms):
            find(ms, head("luna"))["supportedReasoningEfforts"].append({"reasoningEffort": "ultra", "description": "ultra"})

        def narrow(ms):
            e = find(ms, head("sol"))
            e["supportedReasoningEfforts"] = [x for x in e["supportedReasoningEfforts"] if x["reasoningEffort"] != "ultra"]
        for mutate, model_id in ((widen, head("luna")), (narrow, head("sol"))):
            with self.subTest(model=model_id):
                r = self.evaluate(srcs=sources(catalog(mutate)))
                self.assertEqual(r["verdict"], "FAIL")
                self.assertIn(f"stale: effort set of {model_id} differs from the table", self.texts(r))

    def test_coverage_needs_both_sources_for_a_missing_fail(self):
        opus = head("opus")
        r = self.evaluate(srcs=sources(deprecations=DEPRECATIONS_NO_OPUS_5_5, overview=OVERVIEW_NO_OPUS_5_5))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn(f"missing: {opus} is in neither Anthropic source", self.of_class(r, "missing"))
        self.assertEqual(self.marker_kinds(r, opus), set())  # no sticky marker for absence
        for name, dep, ov in (("deprecations-only", DEPRECATIONS_NO_OPUS_5_5, OVERVIEW), ("overview-only", DEPRECATIONS, OVERVIEW_NO_OPUS_5_5)):
            with self.subTest(removed_from=name):
                r = self.evaluate(srcs=sources(deprecations=dep, overview=ov))
                self.assertEqual(r["verdict"], "OK", r)  # one positive observation establishes presence
        for name, srcs in (("deprecations-unread", sources(overview=OVERVIEW_NO_OPUS_5_5, unread=("deprecations",))),
                           ("status-table-mangled", sources(deprecations=DEPRECATIONS_MANGLED)),
                           ("mangled-and-silent-overview", sources(deprecations=DEPRECATIONS_MANGLED, overview=OVERVIEW_NO_OPUS_5_5))):
            with self.subTest(case=name):
                r = self.evaluate(srcs=srcs)
                self.assertEqual(r["verdict"], "UNVERIFIED", r)
                self.assertEqual(self.of_class(r, "missing"), [])
        # an affirmative FAIL keeps precedence over an unverified source
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry(NEWER_SOL))), unread=("deprecations",)))
        self.assertEqual(r["verdict"], "FAIL")

    def test_the_shipped_k1_target_is_covered_and_a_marker_on_it_is_a_lifecycle_fail(self):
        # ruling K1-ON (review/ruling_k1_on.md): the shipped table switches the seat on, so the K1 target is a relevant target and
        # the Claude coverage checks it (DESIGN §5). A marker on it is a lifecycle FAIL, not the "(K1 seat off)" informational
        # note; both pages parsed without it is `missing`. First the seat off, a variant table: informational, and not covered
        k1 = TABLE["k1_seat"]["id"]
        named = re.compile(r"(?<![\w.-])" + re.escape(k1) + r"(?![\w.-])")  # the K1 target's id, never inside the Opus head's
        marked = sources(deprecations=deprecations_with(k1, "Deprecated", "September 20, 2026", "March 20, 2027"))
        page, rows = re.subn(r"(?m)^\| " + re.escape(k1) + r" +\|.*\n", "", DEPRECATIONS)
        self.assertEqual(rows, 1, f"the deprecations capture has no status row for {k1}")
        self.assertFalse(named.search(OVERVIEW), f"the overview capture lists {k1}")
        unlisted = sources(deprecations=page)
        off = self.load(lambda t: t["k1_seat"].__setitem__("enabled", False), "k1-off")
        self.assertNotIn(k1, off.relevant_targets())
        r = self.evaluate(off, marked)
        self.assertEqual(r["verdict"], "OK", r)
        self.assertIn(f"marker on {k1} (K1 seat off): informational", self.of_class(r, "info"))
        r = self.evaluate(off, unlisted)
        self.assertEqual(r["verdict"], "OK", r)
        # the shipped table
        self.assertIn(k1, self.shipped().relevant_targets())
        r = self.evaluate(srcs=marked)
        self.assertEqual(r["verdict"], "FAIL", r)
        self.assertTrue([t for t in self.of_class(r, "lifecycle") if named.search(t)], r)
        self.assertFalse([t for t in self.texts(r) if t.startswith(f"marker on {k1}")], r)
        r = self.evaluate(srcs=unlisted)
        self.assertEqual(r["verdict"], "FAIL", r)
        self.assertEqual(self.of_class(r, "missing"), [f"missing: {k1} is in neither Anthropic source"], r)

    def test_a_non_applicable_local_catalog_is_unverified_and_creates_nothing(self):
        def affirm(ms):
            ms.append(entry(NEWER_SOL))
            find(ms, head("sol"))["deprecated"] = True
        r = self.evaluate(srcs=sources(catalog(affirm), config=CONFIG_CATALOG_JSON))
        self.assertEqual(r["verdict"], "UNVERIFIED", r)
        self.assertIn("local catalog not applicable: model_catalog_json is non-default", self.texts(r))
        self.assertEqual(self.of_class(r, "stale"), [])
        self.assertEqual(self.marker_kinds(r, head("sol")), set())
        self.assertFalse([s for s in strings(r) if "VALUE-MUST-NOT-PRINT" in s])
        r = self.evaluate(srcs=sources(config=CONFIG_SENTINEL))
        self.assertEqual(r["verdict"], "UNVERIFIED", r)
        self.assertTrue([t for t in self.texts(r) if "model_providers" in t and "non-default" in t], r)
        self.assertFalse([s for s in strings(r) if SENTINEL_TOKEN in s or "proxy.invalid" in s])

    def test_anthropic_pages_drive_claude_findings(self):
        opus, sonnet, fable = head("opus"), head("sonnet"), head("fable")
        r = self.evaluate(srcs=sources(overview=overview_api_id(opus, NEWER_OPUS)))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertTrue([t for t in self.of_class(r, "stale") if NEWER_OPUS in t], r)
        r = self.evaluate(srcs=sources(deprecations=deprecations_with(sonnet, "Legacy", "N/A", "Not sooner than June 30, 2027")))
        self.assertTrue([t for t in self.of_class(r, "stale") if sonnet in t], r)  # a head marked Legacy
        r = self.evaluate(srcs=sources(deprecations=deprecations_with(fable, "Deprecated", "September 20, 2026", "March 1, 2027")))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertTrue(self.marker_kinds(r, fable) & {"deprecated", "retiring"}, r)
        self.assertIn("lifecycle", self.classes(r))
        r = self.evaluate(srcs=sources(deprecations=deprecations_with(sonnet, "Active", "N/A", "Not sooner than October 1, 2026")))
        self.assertEqual(self.marker_kinds(r, sonnet), set())  # "Not sooner than" = not scheduled
        self.assertEqual(r["verdict"], "OK", r)
        older = RETIRED_OPUS[0]  # a retired Opus that is not a relevant target
        r = self.evaluate(srcs=sources(deprecations=deprecations_with(older, "Active", "N/A", "December 1, 2026")))
        self.assertEqual(self.marker_kinds(r, older), {"retiring"})  # any other date is a schedule
        self.assertEqual(r["verdict"], "OK", r)  # ... on a model that is not a relevant target: informational
        self.assertIn("retired", self.marker_kinds(self.evaluate(), "claude-opus-4-1-20250805"))  # Retired, a past date; model-lint: allow the capture's retired row
        r = self.evaluate(srcs=sources(unread=("deprecations",)))
        self.assertEqual(r["verdict"], "UNVERIFIED", r)

    def test_a_retired_row_for_a_listed_spelling_marks_the_head(self):
        # Astra C1-1: lifecycle evidence is keyed by the canonical model ID. Anthropic's page with a row for the Haiku head's listed
        # spelling marked Retired marks the head, a relevant target, so the check FAILS
        haiku, alias = head("haiku"), HAIKU_ALIAS
        table = self.load(with_haiku_alias, "lifecycle-head-alias")
        r = self.evaluate(table, srcs=sources(deprecations=deprecations_with(alias, "Retired", "August 1, 2026", "September 1, 2026")))
        self.assertEqual(r["verdict"], "FAIL", r)
        self.assertIn("lifecycle", self.classes(r))
        self.assertIn("retired", self.marker_kinds(r, haiku))
        self.assertEqual(self.marker_kinds(r, alias), set())  # the evidence is keyed by the head's ID, not the spelling

    def test_an_overview_id_spelled_with_the_1m_modifier_is_the_head_not_a_newer_model(self):
        # Astra C1b-n2: the overview's current IDs meet the known members through Table.canonical(), as the markers, the
        # relevant targets and the coverage check already do. The Opus head's id with `[1m]` is a spelling the table accepts for the
        # Opus head (the modifier normalized). With the head's deprecations row removed it is the head's only observation: it
        # establishes presence, so it cannot also be "a newer model" (a stale FAIL). Anthropic's pages do not write [1m]; the
        # page is constructed
        opus = head("opus")
        for spelling in (f"{opus}[1m]", f"{opus}[1M]"):
            with self.subTest(spelling=spelling):
                r = self.evaluate(srcs=sources(deprecations=DEPRECATIONS_NO_OPUS_5_5, overview=overview_api_id(opus, spelling)))
                self.assertEqual([t for t in self.of_class(r, "stale") if spelling in t], [], r)
                self.assertFalse([t for t in self.texts(r) if opus in t and t.startswith(("missing:", "unverified:"))], r)
                self.assertEqual(r["verdict"], "OK", r)

    def test_npm_latest_newer_than_installed_is_reported_with_stale(self):
        # changed for L3 (review/rulings_final_round.md): an installed Codex CLI older than npm latest is class `unverified` (it
        # may not list newer models), and the verdict UNVERIFIED, no longer a `discovery` FAIL; a prerelease still sorts below
        # its release
        latest = next_minor(LOCAL_CODEX)  # 0.157.0 while the tests' CLI is 0.156.1
        newer = {"name": "@openai/codex", "dist-tags": {"latest": latest}}
        r = self.evaluate(srcs=sources(npm=newer))
        self.assertEqual(r["verdict"], "UNVERIFIED", r)
        self.assertTrue([t for t in self.of_class(r, "unverified") if latest in t], r)
        self.assertNotIn("discovery", self.classes(r))
        r = self.evaluate(srcs=sources(npm=newer), local=dict(LOCAL, codex_version=f"codex-cli {latest}-alpha.2"))
        self.assertTrue([t for t in self.of_class(r, "unverified") if latest in t], r)  # a prerelease sorts below its release
        r = self.evaluate(srcs=sources(npm=newer), local=dict(LOCAL, codex_version=f"codex-cli {latest}"))
        self.assertEqual([t for t in self.texts(r) if latest in t], [], r)

    # ---- marker predicates, one test per representation (DESIGN §5 "Normalized lifecycle record")
    def kinds_for(self, **fields):
        sol = head("sol")
        r = self.evaluate(srcs=sources(catalog(lambda ms: find(ms, sol).update(fields))))
        return self.marker_kinds(r, sol), r

    def test_marker_from_a_deprecated_flag(self):
        kinds, r = self.kinds_for(deprecated=True)
        self.assertEqual(kinds, {"deprecated"})
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("lifecycle", self.classes(r))

    def test_marker_from_a_deprecated_status(self):
        for value in ("deprecated", "Deprecated"):
            with self.subTest(status=value):
                self.assertEqual(self.kinds_for(status=value)[0], {"deprecated"})
        self.assertEqual(self.kinds_for(state="deprecated")[0], {"deprecated"})

    def test_marker_from_a_retired_status_without_date_or_prose(self):
        self.assertEqual(self.kinds_for(status="retired")[0], {"retired"})
        self.assertEqual(self.kinds_for(state="removed")[0], {"retired"})

    def test_marker_from_a_retiring_lifecycle(self):
        for value in ("retiring", "sunset", "scheduled_retirement"):
            with self.subTest(lifecycle=value):
                self.assertEqual(self.kinds_for(lifecycle=value)[0], {"retiring"})

    def test_no_marker_from_unavailable_or_an_unknown_status(self):
        for value in ("unavailable", "preview", "active", "current", "available"):
            with self.subTest(status=value):
                kinds, r = self.kinds_for(status=value)
                self.assertEqual(kinds, set())
                self.assertNotIn("lifecycle", self.classes(r))

    def test_marker_from_prose(self):
        for prose in ("This model is deprecated; use GPT-7 Sol.", "GPT-6 Sol is no longer available in Codex.",  # model-lint: allow vendor prose fixture
                      "GPT-6 Sol retires soon. Switch to a newer model."):  # model-lint: allow vendor prose fixture
            with self.subTest(prose=prose):
                kinds, _ = self.kinds_for(upgradeInfo={"model": None, "upgradeCopy": None, "modelLink": None,
                                                       "migrationMarkdown": prose, "retirementAt": None})
                self.assertEqual(kinds, {"deprecated"})

    def test_marker_from_future_and_past_retirement_at(self):
        for when, kind in ((FROZEN_NOW + 30 * DAY, "retiring"), (FROZEN_NOW - DAY, "retired")):
            with self.subTest(kind=kind):
                kinds, _ = self.kinds_for(upgradeInfo={"model": None, "upgradeCopy": None, "modelLink": None,
                                                       "migrationMarkdown": None, "retirementAt": when})
                self.assertEqual(kinds, {kind})

    def test_a_pointer_with_a_date_is_retiring_without_is_info(self):
        pointer = {"model": head("astra"), "upgradeCopy": None, "modelLink": None, "migrationMarkdown": None}
        kinds, _ = self.kinds_for(upgrade=head("astra"), upgradeInfo=dict(pointer, retirementAt=FROZEN_NOW + 20 * DAY))
        self.assertEqual(kinds, {"retiring"})
        kinds, r = self.kinds_for(upgrade=head("astra"), upgradeInfo=dict(pointer, retirementAt=None))
        self.assertEqual(kinds, set())
        self.assertEqual(r["verdict"], "OK", r)  # a cross-family pointer without a date is information only (the Terra case)

    def test_absence_and_hidden_are_never_persisted(self):
        # each on its own, so that neither can stand in for the other: the head absent from the catalog, the head hidden
        cases = {"absent": (head("luna"), lambda ms: ms.remove(find(ms, head("luna")))),
                 "hidden": (head("sol"), lambda ms: find(ms, head("sol")).__setitem__("hidden", True))}
        for name, (model_id, mutate) in cases.items():
            with self.subTest(case=name, model=model_id):
                r = self.evaluate(srcs=sources(catalog(mutate)))
                self.assertEqual(r["verdict"], "FAIL")
                self.assertTrue([t for t in self.of_class(r, "local") if model_id in t], r)
                self.assertEqual(self.marker_kinds(r, model_id), set())
                self.assertNotIn("lifecycle", self.classes(r))

    def test_a_bad_env_default_is_a_fail(self):
        for var, value in (("BRIDGE_REVIEW_SEATS", "spark"), ("BRIDGE_DEFAULT_SEAT", NEWER_SOL), ("BRIDGE_COUNCIL_SEATS", "astra:ultra,5.5")):
            with self.subTest(var=var):
                r = self.evaluate(local=dict(LOCAL, env={var: value}))
                self.assertEqual(r["verdict"], "FAIL")
                self.assertTrue([t for t in self.of_class(r, "defaults") if var in t], r)
        r = self.evaluate(local=dict(LOCAL, env={"BRIDGE_REVIEW_SEATS": "luna:high,opus:max"}))
        self.assertNotIn("defaults", self.classes(r))

    def test_env_defaults_are_checked_with_the_launch_seat_grammar(self):
        # Astra C1-4: the defaults finding applies parse_seats()'s whole grammar: @fast on a Claude seat, an unknown modifier and
        # a list naming no seat are each a `defaults` FAIL naming the variable; @fast on a Codex seat stays valid
        for value in ("opus@fast", "sol@slow", ", ,"):
            with self.subTest(value=value):
                r = self.evaluate(local=dict(LOCAL, env={"BRIDGE_DEFAULT_SEAT": value}))
                self.assertEqual(r["verdict"], "FAIL", r)
                self.assertTrue([t for t in self.of_class(r, "defaults") if "BRIDGE_DEFAULT_SEAT" in t], r)
        r = self.evaluate(local=dict(LOCAL, env={"BRIDGE_DEFAULT_SEAT": "sol:low@fast"}))
        self.assertNotIn("defaults", self.classes(r))

    def test_host_overrides_and_relay_claude_cli_are_reported_by_name(self):
        secret = "OPUS-OVERRIDE-SECRET-VALUE-7731"
        for var in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            with self.subTest(var=var):
                r = self.evaluate(local=dict(LOCAL, env={var: secret}))
                self.assertEqual(r["verdict"], "FAIL")
                self.assertTrue([t for t in self.of_class(r, "host") if var in t], r)
                self.assertFalse([s for s in strings(r) if secret in s])
        r = self.evaluate(local=dict(LOCAL, relay_claude_version="2.1.279 (Claude Code)"))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("host", self.classes(r))
        r = self.evaluate(local=dict(LOCAL, claude_version="2.1.279 (Claude Code)"))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertTrue(self.classes(r) & {"host", "cli"}, r)


# ==================================================================================================================
# Astra's findings on build commit 3 (review/astra_C3_findings.md), the pure pieces; the rest: test_bridge.py GroupE_C3Findings
# ==================================================================================================================
class GroupE_C3Pure(ModuleCase):

    def test_stored_markers_are_canonicalized_before_their_relevance_is_judged(self):
        # C3-6: a stored marker keyed by the Haiku head's listed spelling is the head's lifecycle FAIL, as
        # Evidence.marker(..., table) already treats it, not an informational marker on an ID the table does not know
        haiku, alias = head("haiku"), HAIKU_ALIAS
        stored = {alias: {"id": alias, "kind": "retired", "kinds": ["retired"], "source": "anthropic model-deprecations",
                          "seen_at": FROZEN_NOW - DAY, "detail": "Retired; retirement September 1, 2026"}}
        r = self.bm.evaluate(self.load(with_haiku_alias, "stored-head-alias"), sources(), LOCAL, FROZEN_NOW, markers=stored)
        self.assertEqual(r["verdict"], "FAIL", r)
        self.assertTrue([t for t in self.of_class(r, "lifecycle") if haiku in t], r)
        self.assertFalse([t for t in self.texts(r) if t.startswith(f"marker on {alias}")], r)
        # the stored set is authoritative even when empty: no marker is derived from the sources in its place
        r = self.bm.evaluate(self.shipped(), sources(catalog(lambda ms: find(ms, head("sol")).update(deprecated=True))), LOCAL, FROZEN_NOW,
                             markers={})
        self.assertNotIn("lifecycle", self.classes(r), r)

    def test_a_refresh_time_in_the_future_makes_the_refresh_due(self):
        # C3-10: after the clock moves back, a sources_refreshed_at in the future is a negative cache age: the refresh is due
        t = self.shipped()
        for ahead in (3600, 7 * DAY):
            with self.subTest(ahead=ahead):
                self.assertTrue(self.bm.refresh_due({"sources_refreshed_at": FROZEN_NOW + ahead}, t, FROZEN_NOW))
        self.assertFalse(self.bm.refresh_due({"sources_refreshed_at": FROZEN_NOW - 3600}, t, FROZEN_NOW))  # inside the window
        self.assertTrue(self.bm.refresh_due({"sources_refreshed_at": FROZEN_NOW - 2 * DAY}, t, FROZEN_NOW))


# ==================================================================================================================
# The live-check findings of the final round (review/rulings_final_round.md), the pure pieces; through the engine:
# test_bridge.py GroupE_LiveCheckFindings, and L4's hints GroupD_UpgradeHints
# ==================================================================================================================
USER_LAYER = {"name": {"type": "user", "file": "/Users/USER/.codex/config.toml", "profile": None},
              "version": "sha256:b999efde173cfa78babd2a964d440879317bb134254ee4a0d3a220558163bedc"}  # the capture's own origin record


def capture(drop=(), add=None):
    """The real config/read capture with the origins in `drop` removed and those in `add` ({key: origin}) set."""
    cfg = copy.deepcopy(CONFIG_CAPTURE)
    for key in drop:
        cfg["origins"].pop(key)
    cfg["origins"].update(add or {})
    return cfg


class GroupE_LiveCheckPure(ModuleCase):

    @staticmethod
    def applicability(result):
        return [t for t in ModuleCase.texts(result) if "not applicable" in t]

    def test_a_resolved_default_without_an_origin_is_default(self):
        # L1: config/read answers RESOLVED defaults (the capture: chatgpt_base_url = "https://chatgpt.com/backend-api/", no
        # origin). A key with no origin is the default, whatever its value. With the capture, its service_tier origin removed
        # (L2: not a catalog key anyway), the local catalog is applicable: the real 0.156.1 list is exactly OK, and a listed
        # fictional newer Sol is a stale FAIL again (a non-applicable catalog shows nothing)
        cfg = capture(drop=("service_tier",))
        r = self.evaluate(srcs=sources(config=cfg))
        self.assertEqual(self.applicability(r), [], r)
        self.assertEqual(r["verdict"], "OK", r)
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry(NEWER_SOL))), config=cfg))
        self.assertIn(f"stale: newer model may exist: {NEWER_SOL} is listed but not in the model table", self.texts(r))
        self.assertEqual(r["verdict"], "FAIL", r)

    def test_a_catalog_key_is_non_default_only_when_a_config_layer_sets_it_or_one_of_its_sub_keys(self):
        # L1: a catalog key is non-default when `origins` has an entry for the key or for a sub-key (`key.` prefix), and only
        # then: the capture with a user-layer origin on chatgpt_base_url, on a sub-key of model_providers, or on a sub-key of
        # profiles (which is not `profile`) makes the catalog not applicable because of that one key. Names only, never values
        cases = {"chatgpt_base_url": ("chatgpt_base_url", "chatgpt_base_url"),
                 "a sub-key of model_providers": ("model_providers.openai.base_url", "model_providers"),
                 "a sub-key of profiles, which is not profile": ("profiles.work.model", "profiles")}
        for case, (origin, key) in cases.items():
            with self.subTest(origin=case):
                r = self.evaluate(srcs=sources(config=capture(drop=("service_tier",), add={origin: USER_LAYER})))
                self.assertEqual(self.applicability(r), [f"local catalog not applicable: {key} is non-default"], r)
                self.assertEqual(r["verdict"], "UNVERIFIED", r)
                self.assertFalse([s for s in strings(r) if "chatgpt.com/backend-api" in s or "priority" in s])

    def test_a_user_layer_service_tier_leaves_the_catalog_applicable(self):
        # L2: service_tier is not a catalog key (Codex rust-v0.157.0 app-server/src/models.rs supported_models() filters only on
        # include_hidden || show_in_picker; service tiers are per-model metadata). The capture as it was read, service_tier =
        # "priority" from the user layer (Kevin's ~/.codex/config.toml), leaves the catalog applicable: exactly OK
        self.assertNotIn("service_tier", self.shipped().catalog_config_keys)
        r = self.evaluate(srcs=sources(config=capture()))
        self.assertEqual(self.applicability(r), [], r)
        self.assertEqual(r["verdict"], "OK", r)

    def test_an_installed_cli_older_than_npm_latest_is_unverified_never_a_fail(self):
        # L3: Codex 0.157.0 (2026-09-25) added no model, and Codex ships a release every few days: a CLI older than npm latest is
        # one `unverified` finding naming both versions (the CLI may not list newer models; the upgrade hint), and the verdict
        # is UNVERIFIED when nothing else fails; an affirmative FAIL beside it (a listed fictional newer Sol) still decides. The
        # config has no origin and no value, applicable before and after L1
        blank = {"config": {k: None for k in TABLE["catalog_config_keys"]}, "origins": {}}
        latest = next_minor(LOCAL_CODEX)  # 0.157.0 while the tests' CLI is 0.156.1
        newer = {"name": "@openai/codex", "dist-tags": {"latest": latest}}
        r = self.evaluate(srcs=sources(npm=newer, config=blank))
        found = [f for f in r["findings"] if latest in f["text"]]
        self.assertEqual(len(found), 1, r)
        self.assertEqual(found[0]["class"], "unverified", found[0])
        self.assertIn(LOCAL_CODEX, found[0]["text"])
        self.assertEqual(self.classes(r) & FAIL_CLASSES, set(), r)
        self.assertEqual(r["verdict"], "UNVERIFIED", r)
        r = self.evaluate(srcs=sources(catalog(lambda ms: ms.append(entry(NEWER_SOL))), npm=newer, config=blank))
        self.assertEqual(r["verdict"], "FAIL", r)
        self.assertEqual([f["class"] for f in r["findings"] if latest in f["text"]], ["unverified"], r)


# ==================================================================================================================
# npm's dist-tags endpoint (briefs/npm_round.md), the pure pieces; through the engine: test_bridge.py GroupE_NpmDistTags
# ==================================================================================================================
class GroupE_NpmDistTagsPure(ModuleCase):
    """The live check on a8d8f7e7 read npm's full packument (https://registry.npmjs.org/@openai/codex, 15.7 MB) and failed as
    `too-large` under the fetch's 2 MiB cap. The ruling: the default npm URL is the registry's dist-tags endpoint; the reader
    (retain_page, the page as the store keeps it: {"latest"}) accepts a top-level `latest` string (the endpoint) or
    `dist-tags.latest` (a packument, which a BRIDGE_MODELS_NPM_URL override may still point at); anything else is an
    allowlisted source error that keeps the previous observation. A refresh is published here as the engine publishes one
    (bridge.py read_sources: retain_page per page, then apply_refresh; compose_verdict at every invocation), with the real
    0.156.1 catalog, both Anthropic captures and the npm page under test."""

    SENTINEL = "SENTINEL-NPM-BODY-5c1e"  # in a body the reader rejects: never in its error, the store or a finding
    NEITHER = {"the endpoint's tags without latest": json.dumps({"beta": SENTINEL, "alpha": "0.159.0-alpha.2"}),
               "a number for latest": json.dumps({"latest": 157, "beta": SENTINEL}),
               "an object for latest": json.dumps({"latest": {"version": SENTINEL}}),
               "null for latest": json.dumps({"latest": None, "beta": SENTINEL}),
               "a packument without dist-tags.latest": json.dumps({"name": "@openai/codex", "dist-tags": {"beta": SENTINEL}}),
               "a list for dist-tags.latest": json.dumps({"name": "@openai/codex", "dist-tags": {"latest": [SENTINEL]}}),
               "a string for dist-tags": json.dumps({"name": "@openai/codex", "dist-tags": SENTINEL}),
               "npm's error object": json.dumps({"error": SENTINEL}),
               "a JSON list": json.dumps([{"latest": SENTINEL}]),
               "a JSON string": json.dumps(SENTINEL),
               "not JSON (an HTML page)": f"<!doctype html><title>{SENTINEL}</title>",
               "an empty body": ""}

    def npm_table(self):
        """The shipped table with every Codex floor at or below 0.156.1, the CLI these tests install beside the real endpoint
        capture's latest 0.157.0 (they are about npm, not floors)."""
        def cap(t):
            for rec in t["models"].values():
                if rec.get("min_cli") and rec.get("family") in t["families"]["codex"] and _version_key(rec["min_cli"]) > _version_key("0.156.1"):
                    rec["min_cli"] = "0.156.1"
        return self.load(cap, "npm-floors")

    def publish(self, data, npm_text, at):
        """`data` after one refresh at `at` that read every source: the real catalog and pages, and `npm_text` as the npm page."""
        t = self.npm_table()
        observations = {"codex": {"read_at": at, "observation": self.bm.retain_catalog(t, {"config": CONFIG_DEFAULT, "models": catalog()}),
                                  "error": None}}
        for name, text in (("npm", npm_text), ("deprecations", DEPRECATIONS), ("overview", OVERVIEW)):
            observation, error = self.bm.retain_page(name, text)
            observations[name] = {"read_at": at, "observation": observation, "error": error}
        return self.bm.apply_refresh(data, t, observations, at)

    def composed(self, data, codex_version="codex-cli 0.156.1"):
        return self.bm.compose_verdict(self.npm_table(), data, dict(LOCAL, codex_version=codex_version), FROZEN_NOW)

    @staticmethod
    def not_info(result):
        return [f for f in result["findings"] if f.get("class") != "info"]

    def test_the_default_npm_url_is_the_dist_tags_endpoint(self):
        # the endpoint answered 596 bytes on 2026-09-25; the packument at the old default URL (15.7 MB) is over the fetch's cap.
        # BRIDGE_MODELS_NPM_URL still overrides the default
        self.assertEqual(self.bm.source_urls({})["npm"], NPM_ENDPOINT_URL)
        override = "http://127.0.0.1:9/packument"
        self.assertEqual(self.bm.source_urls({"BRIDGE_MODELS_NPM_URL": override})["npm"], override)

    def test_the_real_endpoint_capture_reads_latest_0_157_0(self):
        # the real answer: `latest` among fifteen other top-level tags (beta 0.1.2505172116, alpha 0.159.0-alpha.2, per platform
        # 0.157.0-darwin-arm64 ...). The store keeps {"latest": "0.157.0"} and the installed Codex CLI is compared with it:
        # 0.156.1 gets the one L3 line naming both versions and nothing else is unverified or failing; 0.157.0 is exactly OK
        self.assertEqual(self.bm.retain_page("npm", NPM_ENDPOINT_TEXT), ({"latest": "0.157.0"}, None))
        data = self.publish({}, NPM_ENDPOINT_TEXT, FROZEN_NOW)
        self.assertEqual(data["sources"]["npm"]["observation"], {"latest": "0.157.0"})
        r = self.composed(data)
        found = self.not_info(r)
        self.assertEqual([f["class"] for f in found], ["unverified"], found)
        self.assertIn("0.157.0", found[0]["text"])
        self.assertIn("0.156.1", found[0]["text"])
        self.assertEqual(r["verdict"], "UNVERIFIED")
        r = self.composed(data, codex_version="codex-cli 0.157.0")
        self.assertEqual((r["verdict"], self.not_info(r)), ("OK", []))

    def test_a_packument_still_reads_as_the_endpoint_with_the_same_tags_does(self):
        # a BRIDGE_MODELS_NPM_URL override may still point at a packument: its `dist-tags.latest` is read (the synthetic fixture
        # every engine test serves: 0.156.1), and a packument carrying the capture's tags beside versions and times is kept
        # exactly as the capture itself is
        self.assertEqual(self.bm.retain_page("npm", (EVIDENCE / "npm_dist_tags.json").read_text()), ({"latest": "0.156.1"}, None))
        tags = json.loads(NPM_ENDPOINT_TEXT)
        packument = json.dumps({"_id": "@openai/codex", "name": "@openai/codex", "dist-tags": tags,
                                "versions": {v: {"name": "@openai/codex", "version": v} for v in sorted(set(tags.values()))},
                                "time": {"modified": "2026-09-25T21:48:14.000Z"}})
        self.assertEqual(self.bm.retain_page("npm", packument), ({"latest": "0.157.0"}, None))
        self.assertEqual(self.bm.retain_page("npm", NPM_ENDPOINT_TEXT), self.bm.retain_page("npm", packument),
                         "the endpoint's tags are not kept as the same tags in a packument are")

    def test_a_body_with_neither_shape_is_a_source_error_that_keeps_the_previous_observation(self):
        # after a refresh that read the endpoint (latest 0.157.0 kept), an npm answer with neither a top-level `latest` string nor
        # `dist-tags.latest` is a source error in the check's own words (one line, nothing of the body): the observation and its
        # read time stay, the attempt records the error, npm is unverified, and the kept latest still drives the L3 line
        first = self.publish({}, NPM_ENDPOINT_TEXT, FROZEN_NOW - DAY)
        self.assertEqual(first["sources"]["npm"]["observation"], {"latest": "0.157.0"}, "the endpoint's answer was not kept")
        for case, body in self.NEITHER.items():
            with self.subTest(body=case):
                observation, error = self.bm.retain_page("npm", body)
                self.assertIsNone(observation)
                self.assertIsInstance(error, str)
                self.assertTrue(error.strip(), "a rejected body has no error")
                self.assertNotIn("\n", error)
                self.assertNotIn(self.SENTINEL, error, "the error carries the body's text")
                data = self.publish(copy.deepcopy(first), body, FROZEN_NOW)
                npm = data["sources"]["npm"]
                self.assertEqual((npm["observation"], npm["read_at"]), ({"latest": "0.157.0"}, FROZEN_NOW - DAY))
                self.assertEqual((npm["attempt"]["ok"], npm["attempt"]["error"]), (False, error))
                r = self.composed(data)
                self.assertTrue([f for f in r["findings"] if f["class"] == "unverified" and f.get("source") == "npm"], r["findings"])
                self.assertTrue([f for f in self.not_info(r) if "0.157.0" in f["text"] and "0.156.1" in f["text"]], r["findings"])
                self.assertEqual(self.classes(r) & FAIL_CLASSES, set(), r["findings"])
                self.assertNotIn(self.SENTINEL, json.dumps(data) + json.dumps(r))


# ==================================================================================================================
# The final review (review/rulings_final_review.md, binding; the findings verbatim with file:line: review/final_A_findings.md),
# the pure pieces, each test naming its finding; through the engine: test_bridge.py GroupA_FinalReviewTable and
# GroupE_FinalReviewCheck
# ==================================================================================================================
def new_haiku_head(t):
    """Haiku gets a new (fictional) head, and the old head moves to `retired.claude.haiku` but keeps its record in `models`, WITH its
    listed spelling (as the table keeps the K1 target there): the natural maintainer step A-opus-3 describes."""
    old = t["families"]["claude"]["haiku"]["known_good"]
    new = NEWER_HAIKU
    t["models"][new] = dict(t["models"][old], generation=NEWER_HAIKU.rsplit("-", 2)[1], aliases=[], source="test: a new Haiku head (A-opus-3)")
    t["families"]["claude"]["haiku"]["known_good"] = new
    t["retired"]["claude"]["haiku"].insert(0, old)


class GroupA_FinalReviewPure(ModuleCase):

    def test_declared_predecessor_aliases_are_preserved_and_refused_without_key_error(self):
        # P-16 preserves the Active predecessor record, including its real API alias. A declared predecessor's
        # spelling is canonicalized for lifecycle evidence and refused for selection, even on the K1 target.
        t = self.shipped()
        old = TABLE["retired"]["claude"]["haiku"][0]
        alias = TABLE["models"][old]["aliases"][0]
        self.assertEqual(t.canonical(alias), old)
        self.assertIn(alias, t.known_members("claude", "haiku"))
        for spec in (old, alias, "claude/" + alias):
            with self.subTest(spec=spec), self.assertRaises(self.bm.Refusal) as cm:
                t.resolve(spec)
            self.assertIn(f"is superseded by {head('haiku')}", str(cm.exception))
        k1 = TABLE["k1_seat"]["id"]
        table = self.load(lambda t: t["models"][k1].__setitem__("aliases", [k1 + "-alias"]), "k1-predecessor-alias")
        with self.assertRaises(self.bm.Refusal):
            table.resolve(k1 + "-alias")
        orphan = self.write_table("orphan-alias", lambda t: t["retired"]["claude"]["haiku"].remove(old))
        self.assertIn("neither its family's head nor a declared predecessor", self.assert_one_line_error(orphan))

    def test_a_name_matching_a_familys_pattern_is_refused_naming_that_familys_head(self):
        # A-opus-4 [P3]: Anthropic's dateless aliases of retired snapshots (a dated retired id with its date stripped) are not in
        # `retired`, so they fell to rule 6, whose hint leads with the first family: "Use one of astra (<its head>), sol, ...", a
        # Codex model as the only concrete replacement of an obviously Claude name. The ruling: a name matching a family's pattern
        # (`id_prefix`, `id_glob`) that is not in the table is refused naming THAT family's head, and no other head. A name that
        # matches no family's pattern keeps rule 6's line
        cases = {}
        for fam in ("sonnet", "opus"):  # the table's dated retired snapshots, spelled dateless
            prefix = TABLE["families"]["claude"][fam]["id_prefix"]
            for x in TABLE["retired"]["claude"][fam]:
                if x.startswith(prefix) and re.search(r"-\d{8}$", x):
                    cases[re.sub(r"-\d{8}$", "", x)] = fam
        self.assertGreaterEqual(len(cases), 2, "the table lists dated retired Sonnet and Opus snapshots")
        dateless_sonnet = next(n for n, f in cases.items() if f == "sonnet")
        cases.update({NEWER_OPUS: "opus", NEWER_FABLE: "fable", NEWER_HAIKU: "haiku", NEWER_SOL: "sol", NEWER_LUNA: "luna",
                      "gpt-9.9-astra": "astra", "gpt-9.9-terra": "terra", f"claude/{dateless_sonnet}": "sonnet", f"codex/{NEWER_SOL}": "sol"})
        t = self.shipped()
        fams = list(TABLE["families"]["codex"]) + list(TABLE["families"]["claude"])
        for name, fam in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(self.bm.Refusal) as cm:
                    t.resolve(name)
                text = str(cm.exception)
                self.assertIn(f"Use {fam} ({head(fam)}).", text, "the refusal does not name the family's head")
                self.assertEqual([head(f) for f in fams if f != fam and head(f) in text], [], f"another family's head is named: {text}")
        for name in ("gpt-9-nova", "gemini-3", "grok-4"):
            with self.subTest(matches_no_pattern=name):
                with self.assertRaises(self.bm.Refusal) as cm:
                    t.resolve(name)
                self.assertIn(f"'{name}' is not in the model table ({self.bm.TABLE_NAME}). Use one of {fams[0]} ({head(fams[0])})",
                              str(cm.exception))


class GroupE_FinalReviewPure(ModuleCase):

    INFO = {"model": None, "upgradeCopy": None, "modelLink": None, "migrationMarkdown": None}
    # retirementAt values json.loads can produce that are no timestamp: JSON's NaN and Infinity tokens, strings that parse as
    # non-finite floats, and numbers beyond datetime's range (A-astra-1: 1e100; A-opus-2: 9223372036854775807, 9.2e15 s after
    # the millisecond heuristic)
    NOT_TIMESTAMPS = {"NaN (a JSON token)": float("nan"), "Infinity (a JSON token)": float("inf"), "-Infinity (a JSON token)": float("-inf"),
                      "the string 'NaN'": "NaN", "the string 'Infinity'": "Infinity", "the string '1e400'": "1e400", "1e100": 1e100,
                      "9223372036854775807": 9223372036854775807}

    def test_a_non_finite_or_out_of_range_retirement_at_is_no_timestamp_and_the_entry_is_rejected(self):
        # A-astra-1 [P2] / A-opus-2 [P2] (C4b-4 closed only for the tested shapes): `_epoch` returned NaN, +-inf and values beyond
        # datetime's range, so entry_ok accepted the entry and evaluate() crashed in _date_text (ValueError, OverflowError) inside
        # apply_refresh: doctor and every due re-check exited with a traceback. The ruling: `_epoch` returns None unless the value is
        # finite and within datetime's range, and entry_ok rejects such an entry (the allowlisted source-error path; through the
        # engine: test_bridge.py GroupE_FinalReviewCheck). Supported timestamps stay accepted
        sol = head("sol")
        for case, value in self.NOT_TIMESTAMPS.items():
            with self.subTest(retirementAt=case):
                self.assertIsNone(self.bm._epoch(value), "a value that is no representable timestamp")
                self.assertFalse(self.bm.entry_ok(entry(sol, upgradeInfo=dict(self.INFO, retirementAt=value))),
                                 "entry_ok accepted an entry whose retirementAt is no timestamp")
        later = FROZEN_NOW + 30 * DAY
        for case, value in {"epoch seconds": later, "epoch milliseconds": later * 1000, "ISO 8601 with Z": "2026-10-25T03:20:00Z",
                            "an ISO 8601 date": "2026-10-25", "null": None}.items():
            with self.subTest(supported=case):
                self.assertTrue(self.bm.entry_ok(entry(sol, upgradeInfo=dict(self.INFO, retirementAt=value))), case)
                if value is not None:
                    self.assertAlmostEqual(self.bm._epoch(value), later if case != "an ISO 8601 date" else later - 3 * 3600 - 20 * 60)



class GroupP_PromotionPure(ModuleCase):
    """DECISIONS A28 (Kevin, 2026-11-07): a newer GPT model that this Mac's own signed-in Codex catalog lists becomes its family's head
    without anyone editing models.json. Pure: plan_promotions, promote, effective_table, undo_promotion, the ledger, the notice."""

    NOW = FROZEN_NOW
    CLI = "codex-cli 0.167.0"

    def observe(self, extra=(), mutate=None, cli=CLI, account=None, config=None, table=None):
        """The retained observation of one refresh's catalog read: the clean catalog plus `extra` entries, a login (account/read)
        unless `account` says otherwise, and the reading CLI's --version line."""
        models = catalog(lambda ms: ms.extend(copy.deepcopy(list(extra))))
        if mutate:
            mutate(models)
        answer = {"config": copy.deepcopy(config or CONFIG_DEFAULT), "models": models,
                  "account": {"signed_in": True, "type": "chatgpt", "plan": "pro"} if account is None else account}
        obs = self.bm.retain_catalog(table or self.shipped(), answer)
        obs["cli"] = cli
        obs["read_at"] = self.NOW
        return obs

    def promote(self, obs, ledger=None, markers=None, base=None, now=None):
        return self.bm.promote(base or self.shipped(), ledger or self.bm.empty_ledger(), obs, markers or {}, now or self.NOW,
                               self.tmp / "models-evidence.promotions.json")

    # ---------------------------------------------------------------- adoption
    def test_a_newer_visible_model_becomes_the_head_and_the_old_head_a_predecessor(self):
        old = head("terra")
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)]))
        self.assertEqual([(s["family"], s["from"], s["to"], s["generation"], s["min_cli"], s["cli"]) for s in steps],
                         [("terra", old, NEWER_TERRA, NEWER_TERRA.split("-")[1], "0.167.0", self.CLI)])
        self.assertEqual(steps[0]["efforts"], TABLE["efforts"]["codex"])
        self.assertEqual(steps[0]["account"], {"type": "chatgpt", "plan": "pro"})  # never an email or an account id
        self.assertEqual(t.head("terra"), NEWER_TERRA)
        self.assertIn(old, t.retired["codex"]["terra"])
        self.assertEqual(t.parse_seat("terra:ultra").model, NEWER_TERRA)
        with self.assertRaises(self.bm.Refusal) as cm:
            t.parse_seat(old)
        self.assertIn(f"{old} is superseded by {NEWER_TERRA}. Use terra ({NEWER_TERRA}).", str(cm.exception))
        rec = t.models[NEWER_TERRA]
        self.assertEqual((rec["family"], rec["min_cli"], rec["aliases"], rec["auto_promoted"]["from"]), ("terra", "0.167.0", [], old))
        self.assertEqual([h["kind"] for h in t.history][-1], "auto_promotion")
        self.assertEqual(t.promotion_step(NEWER_TERRA)["to"], NEWER_TERRA)
        self.assertIsNone(t.promotion_step(head("sol")))
        self.assertIn(f"promoted automatically on 2026-09-25 to {NEWER_TERRA}", t.head_evidence("terra"))
        self.assertEqual(t.head_evidence("sol"), self.shipped().head_evidence("sol"))
        # the shipped table is untouched; the effective one has its own digest, so a stored evaluation is recomputed
        self.assertEqual(self.shipped().head("terra"), old)
        self.assertNotEqual(t.sha256, self.shipped().sha256)
        self.assertEqual(t.base.head("terra"), old)
        # the ledger: the step, one log line, the pass
        self.assertEqual(ledger["steps"], steps)
        self.assertEqual([(l["event"], l["family"], l["to"]) for l in ledger["log"]], [("promoted", "terra", NEWER_TERRA)])
        self.assertEqual(ledger["last_pass"]["result"], f"promoted terra {old} -> {NEWER_TERRA}")
        self.assertIsNone(self.bm.ledger_problem(ledger))
        # the check now knows it: no `stale` line for the model the table adopted
        r = self.evaluate(t, sources(catalog(lambda ms: ms.append(entry(NEWER_TERRA)))))
        self.assertEqual([x for x in self.of_class(r, "stale") if NEWER_TERRA in x], [], r)
        # a second pass over the same catalog adds nothing
        again, t2, steps2 = self.promote(self.observe([entry(NEWER_TERRA)]), ledger=ledger)
        self.assertEqual((steps2, again["steps"], t2.head("terra")), ([], steps, NEWER_TERRA))
        self.assertEqual(again["last_pass"]["result"], "nothing newer to adopt")

    def test_the_highest_candidate_wins_and_a_lower_one_becomes_a_predecessor(self):
        lower = NEWER_TERRA                       # gpt-9-terra
        higher = "gpt-9.9-terra"                  # a fixture id of the registry's lint (fictional)
        ledger, t, steps = self.promote(self.observe([entry(lower), entry(higher)]))
        self.assertEqual([(s["to"], s["superseded"]) for s in steps], [(higher, [lower])])
        self.assertEqual(t.head("terra"), higher)
        self.assertIn(lower, t.retired["codex"]["terra"])
        r = self.evaluate(t, sources(catalog(lambda ms: ms.extend([entry(lower), entry(higher)]))))
        self.assertEqual([x for x in self.of_class(r, "stale") if "terra" in x], [], r)
        # a later, higher model moves the head again; the previous automatic head becomes a predecessor too
        ledger2, t2, steps2 = self.promote(self.observe([entry(lower), entry(higher), entry("gpt-9.99-sol")]), ledger=ledger)
        self.assertEqual([(s["family"], s["from"], s["to"]) for s in steps2], [("sol", head("sol"), "gpt-9.99-sol")])
        self.assertEqual((t2.head("terra"), t2.head("sol")), (higher, "gpt-9.99-sol"))
        self.assertEqual(len(ledger2["steps"]), 2)

    def test_two_candidates_of_one_generation_hold_the_family_back(self):
        a, b = NEWER_TERRA, NEWER_TERRA.replace("-9-", "-9.0-")
        self.assertEqual(self.bm.generation_order("9"), self.bm.generation_order("9.0"))
        ledger, t, steps = self.promote(self.observe([entry(a), entry(b)]))
        self.assertEqual(steps, [])
        self.assertEqual(t.head("terra"), head("terra"))
        self.assertEqual(sorted(h["id"] for h in ledger["last_pass"]["held"] if h["family"] == "terra"), sorted([a, b]))
        self.assertTrue(all("share generation" in h["reason"] for h in ledger["last_pass"]["held"]))

    def test_generation_order_is_numeric_with_trailing_zeros_dropped(self):
        order = self.bm.generation_order
        self.assertLess(order("6.1"), order("6.10"))
        self.assertLess(order("6.9"), order("6.10"))
        self.assertEqual(order("7"), order("7.0.0"))
        self.assertLess(order("6.1"), order("7"))
        self.assertIsNone(order("6.1a"))
        self.assertIsNone(order(""))

    # ---------------------------------------------------------------- what is never promoted
    def test_a_candidate_that_fails_a_gate_is_held_back_with_its_reason(self):
        head_efforts = TABLE["models"][head("terra")]["efforts"]
        cases = {
            "hidden": (entry(NEWER_TERRA, hidden=True), "not listed as visible"),
            "hidden unset": ({k: v for k, v in entry(NEWER_TERRA).items() if k != "hidden"}, "not listed as visible"),
            "preview": (entry(NEWER_TERRA, status="preview"), "its catalog status is 'preview'"),
            "legacy": (entry(NEWER_TERRA, status="legacy"), "its catalog status is 'legacy'"),
            "deprecated flag": (entry(NEWER_TERRA, deprecated=True), "lifecycle statement"),
            "upgrade pointer": (entry(NEWER_TERRA, upgrade=head("sol")), "lifecycle statement"),
            "retirement date": (entry(NEWER_TERRA, upgradeInfo={"model": head("sol"), "retirementAt": "2027-01-01T00:00:00Z"}), "lifecycle statement"),
            "an effort the head offers is missing": (entry(NEWER_TERRA, efforts=head_efforts[:-1]), f"lacks efforts {head('terra')} offers"),
            "an unknown effort": (entry(NEWER_TERRA, efforts=list(head_efforts) + ["turbo"]), "not Codex efforts"),
            "no efforts": (entry(NEWER_TERRA, efforts=()), "not Codex efforts"),
            "not a numbered generation": (entry(NEWER_TERRA.replace("-terra", "-mini-terra")), "not a numbered generation of terra"),
        }
        for name, (e, reason) in cases.items():
            with self.subTest(case=name):
                ledger, t, steps = self.promote(self.observe([e]))
                self.assertEqual(steps, [])
                self.assertEqual(t.head("terra"), head("terra"))
                held = [h for h in ledger["last_pass"]["held"] if h["id"] == e["id"]]
                if name == "hidden unset" or name == "hidden":
                    self.assertTrue(held and reason in held[0]["reason"], ledger["last_pass"])
                else:
                    self.assertEqual(len(held), 1, ledger["last_pass"])
                    self.assertIn(reason, held[0]["reason"])
        # the real catalog's hidden models (a chat model, a reserve, the reviewer) and a codename never even match a numbered pattern
        hidden_real = [entry(m["id"], hidden=True) for m in CATALOG_NEWEST if m.get("hidden")]
        self.assertTrue(hidden_real)
        for e in hidden_real + [entry("gpt-daybreak-blue-latest", hidden=True)]:
            with self.subTest(case=e["id"]):
                ledger, t, steps = self.promote(self.observe([e]))
                self.assertEqual((steps, t.heads("codex")), ([], self.shipped().heads("codex")))

    def test_a_sticky_marker_or_a_refusal_holds_a_candidate_back(self):
        marker = {NEWER_TERRA: {"id": NEWER_TERRA, "kind": "deprecated", "kinds": ["deprecated"], "source": "codex model/list", "seen_at": 1.0}}
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)]), markers=marker)
        self.assertEqual(steps, [])
        self.assertIn("the evidence store marks it deprecated", ledger["last_pass"]["held"][0]["reason"])
        refusing = self.load(lambda t: t["refused"]["codex"].__setitem__(NEWER_TERRA, {"family": "terra", "reason": "test refusal"}), "refuses-newer-terra")
        ledger, t, steps = self.bm.promote(refusing, self.bm.empty_ledger(), self.observe([entry(NEWER_TERRA)], table=refusing), {}, self.NOW)
        self.assertEqual((steps, t.head("terra")), ([], refusing.head("terra")))

    def test_an_older_or_equal_generation_is_never_a_promotion(self):
        # ids built from the family's pattern: one generation below the head (no silent descent) and one equal to it (6.1 == 6.1.0)
        glob, gen = TABLE["families"]["codex"]["sol"]["id_glob"], TABLE["models"][head("sol")]["generation"]
        for name, other in (("older", glob.replace("*", "0.1")), ("equal", glob.replace("*", gen + ".0"))):
            with self.subTest(generation=name):
                ledger, t, steps = self.promote(self.observe([entry(other)]))
                self.assertEqual((steps, t.head("sol")), ([], head("sol")))
                self.assertEqual(ledger["last_pass"]["held"], [])
                # the check still reports it, as before: a human looks at a name the table does not know
                r = self.evaluate(t, sources(catalog(lambda ms: ms.append(entry(other)))))
                self.assertIn(f"stale: newer model may exist: {other} is listed but not in the model table", self.of_class(r, "stale"))

    def test_only_an_applicable_signed_in_catalog_read_by_a_known_cli_counts(self):
        newer = [entry(NEWER_TERRA)]
        provider = copy.deepcopy(CONFIG_DEFAULT)
        provider["origins"] = {"model_provider": {"name": "user", "version": "x"}}
        cases = {"not signed in": (dict(account={"signed_in": False}), "no login stands behind the catalog"),
                 "no account answer": (dict(account={"signed_in": None, "error": "account/read: no answer within 5s"}), "could not be confirmed"),
                 "a non-default catalog key": (dict(config=provider), "not applicable"),
                 "an unreadable CLI version": (dict(cli="codex something"), "version could not be read")}
        for name, (kw, reason) in cases.items():
            with self.subTest(case=name):
                ledger, t, steps = self.promote(self.observe(newer, **kw))
                self.assertEqual((steps, t.head("terra")), ([], head("terra")))
                self.assertIn(reason, ledger["last_pass"]["result"])
        ledger, t, steps = self.promote(None)
        self.assertEqual(steps, [])
        self.assertIn("not read in this check", ledger["last_pass"]["result"])

    def test_the_floor_is_the_reading_clis_exact_version_a_prerelease_included(self):
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)], cli="codex-cli 0.170.0-alpha.2"))
        self.assertEqual(t.floor(NEWER_TERRA), "0.170.0-alpha.2")
        self.assertEqual(t.highest_floor("codex"), "0.170.0-alpha.2")
        resolved = t.resolve("terra")
        for version, ok in (("codex-cli 0.170.0-alpha.2", True), ("codex-cli 0.170.0", True), ("codex-cli 0.170.0-alpha.1", False),
                            ("codex-cli 0.169.9", False)):
            with self.subTest(cli=version):
                call = lambda: self.bm.validate(t, None, resolved, "low", "initial", {"path": "/x/codex", "version": version})
                if ok:
                    call()
                else:
                    with self.assertRaises(self.bm.Refusal) as cm:
                        call()
                    self.assertIn("needs Codex CLI >= 0.170.0-alpha.2", str(cm.exception))
        # the shipped table keeps its X.Y.Z grammar (bootstrap.sh reads it): a prerelease floor there is refused as before, and so is a
        # record that claims to be an automatic promotion (only the ledger makes those)
        bad = self.write_table("prerelease-floor", lambda d: d["models"][head("terra")].__setitem__("min_cli", "0.170.0-alpha.2"))
        self.assert_one_line_error(bad)
        sneaked = self.write_table("auto-promoted-in-file", lambda d: d["models"][head("terra")].update(
            min_cli="0.170.0-alpha.2", auto_promoted={"at": self.NOW, "from": head("terra"), "cli": self.CLI}))
        self.assertIn("'auto_promoted' records come only from this Mac's promotions ledger", self.assert_one_line_error(sneaked))

    # ---------------------------------------------------------------- the shipped table and the ledger
    def test_a_shipped_checkpoint_takes_over_and_nothing_is_demoted(self):
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)]))
        # the shipped table now names the model (a human checkpoint): the step is skipped and the shipped record rules
        def adopt(d):
            old = d["families"]["codex"]["terra"]["known_good"]
            d["models"][NEWER_TERRA] = dict(d["models"][old], generation="9", min_cli="0.160.0")
            d["families"]["codex"]["terra"]["known_good"] = NEWER_TERRA
            d["retired"]["codex"]["terra"].insert(0, old)
            del d["models"][old]
        checkpoint = self.load(adopt, "checkpoint-newer-terra")
        t2 = self.bm.effective_table(checkpoint, ledger)
        self.assertIs(t2, checkpoint)  # nothing applied, nothing blocked: the shipped table itself
        self.assertEqual(t2.floor(NEWER_TERRA), "0.160.0")
        self.assertEqual(t2.promotion["skipped"][0][1], f"the shipped table names {NEWER_TERRA} now")
        # a shipped head newer than the step: the step's model stays a known predecessor, never the head again
        def newer(d):
            old = d["families"]["codex"]["terra"]["known_good"]
            d["models"]["gpt-9.9-terra"] = dict(d["models"][old], generation="9.9")
            d["families"]["codex"]["terra"]["known_good"] = "gpt-9.9-terra"
            d["retired"]["codex"]["terra"].insert(0, old)
            del d["models"][old]
        past = self.load(newer, "checkpoint-past-terra")
        t3 = self.bm.effective_table(past, ledger)
        self.assertEqual(t3.head("terra"), "gpt-9.9-terra")
        self.assertIn(NEWER_TERRA, t3.retired["codex"]["terra"])
        self.assertIn("not newer than terra's head gpt-9.9-terra", t3.promotion["skipped"][0][1])
        with self.assertRaises(self.bm.Refusal):
            t3.parse_seat(NEWER_TERRA)

    def test_fallback_edges_follow_the_heads_and_never_descend(self):
        shipped = self.shipped()
        self.assertTrue(shipped.fallback["codex"], "the shipped table has a Codex fallback edge to follow")
        src, dst = next(iter(shipped.fallback["codex"].items()))
        src_fam, dst_fam = shipped.family_of(src), shipped.family_of(dst)
        # the source family moves above the target's generation: the re-keyed edge would descend, so it is dropped
        up = {"astra": "gpt-9-astra", "sol": "gpt-9-sol", "terra": NEWER_TERRA, "luna": "gpt-9-luna"}[src_fam]
        ledger, t, steps = self.promote(self.observe([entry(up)]))
        self.assertEqual(t.head(src_fam), up)
        self.assertEqual(t.fallback["codex"], {})
        # the target family moves up instead: the edge follows it
        up2 = {"astra": "gpt-9-astra", "sol": "gpt-9-sol", "terra": NEWER_TERRA, "luna": "gpt-9-luna"}[dst_fam]
        ledger, t, steps = self.promote(self.observe([entry(up2)]))
        self.assertEqual(t.fallback["codex"], {src: up2})

    def test_the_ledger_is_checked_and_load_table_names_it_in_one_line(self):
        evidence = self.tmp / "ledger-check" / "models-evidence.json"
        evidence.parent.mkdir()
        ledger_path = self.bm.promotions_path(evidence)
        self.assertEqual(ledger_path.name, "models-evidence.promotions.json")
        # no ledger: the shipped table itself
        t = self.bm.load_table(str(MODELS_JSON), str(evidence))
        self.assertEqual(t.sha256, self.shipped().sha256)
        good, _t, _s = self.promote(self.observe([entry(NEWER_TERRA)]))
        self.bm.write_private_json(ledger_path, good)
        self.assertEqual(oct(ledger_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.bm.load_table(str(MODELS_JSON), str(evidence)).head("terra"), NEWER_TERRA)
        broken = {"not JSON": "{", "a list": "[]", "another version": json.dumps(dict(good, version=99)),
                  "a step without a family": json.dumps(dict(good, steps=[{k: v for k, v in good["steps"][0].items() if k != "family"}])),
                  "a Claude step": json.dumps(dict(good, steps=[dict(good["steps"][0], vendor="claude")])),
                  "a bad floor": json.dumps(dict(good, steps=[dict(good["steps"][0], min_cli="soon")]))}
        for name, text in broken.items():
            with self.subTest(ledger=name):
                ledger_path.write_text(text)
                with self.assertRaises(self.bm.TableError) as cm:
                    self.bm.load_table(str(MODELS_JSON), str(evidence))
                msg = str(cm.exception)
                self.assertNotIn("\n", msg)
                self.assertIn(str(ledger_path), msg)
        # a step the table cannot take (its family is gone from the shipped table) is skipped, not an error
        ledger_path.write_text(json.dumps(dict(good, steps=[dict(good["steps"][0], family="nova")])))
        t = self.bm.load_table(str(MODELS_JSON), str(evidence))
        self.assertEqual(t.head("terra"), head("terra"))
        self.assertIn("not a Codex family", t.promotion["skipped"][0][1])

    def test_undo_restores_the_previous_head_and_blocks_the_model_on_this_mac(self):
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)]))
        undone, step, after = self.bm.undo_promotion(self.shipped(), ledger, "terra", "it misbehaved", now=self.NOW + 60)
        self.assertEqual((step["to"], after.head("terra")), (NEWER_TERRA, head("terra")))
        self.assertEqual(undone["steps"], [])
        self.assertEqual([(b["id"], b["reason"]) for b in undone["blocked"]], [(NEWER_TERRA, "it misbehaved")])
        self.assertEqual(undone["log"][-1]["event"], "undone")
        with self.assertRaises(self.bm.Refusal) as cm:
            after.parse_seat(NEWER_TERRA)
        self.assertIn(f"{NEWER_TERRA} is blocked on this Mac: its automatic promotion was undone (2026-09-25: it misbehaved). "
                      f"Use terra ({head('terra')}).", str(cm.exception))
        self.assertNotEqual(after.sha256, self.shipped().sha256)  # a block changes what the check reports: its own digest
        # never promoted again on this Mac, and the check says why instead of calling it a newer model
        again, t2, steps2 = self.promote(self.observe([entry(NEWER_TERRA)]), ledger=undone)
        self.assertEqual((steps2, t2.head("terra")), ([], head("terra")))
        r = self.evaluate(t2, sources(catalog(lambda ms: ms.append(entry(NEWER_TERRA)))))
        self.assertEqual([x for x in self.of_class(r, "stale") if NEWER_TERRA in x], [])
        self.assertIn(f"info: {NEWER_TERRA} is listed but blocked on this Mac: its automatic promotion was undone (2026-09-25: it misbehaved)",
                      self.of_class(r, "info"))
        # a later, higher model is still adopted
        later, t3, steps3 = self.promote(self.observe([entry(NEWER_TERRA), entry("gpt-9.9-terra")]), ledger=undone)
        self.assertEqual(t3.head("terra"), "gpt-9.9-terra")
        # a reply to a seat recorded on the blocked model continues on the head and says why
        base, _m, _e, note = t2.resolve_recorded({"model": NEWER_TERRA, "vendor": "codex", "tag": "terra", "effort": "low"})
        self.assertEqual((base.model, note), (head("terra"), f"(recorded {NEWER_TERRA} is blocked on this Mac: its automatic promotion was "
                                                             f"undone (2026-09-25: it misbehaved))"))
        # nothing to undo, or not a Codex family: one line, nothing changed
        for fam, text in (("terra", "no automatic promotion of terra is in effect"), ("opus", "not a Codex family")):
            with self.subTest(family=fam):
                with self.assertRaises(self.bm.PromotionsError) as cm:
                    self.bm.undo_promotion(self.shipped(), undone, fam, "x")
                self.assertIn(text, str(cm.exception))
        # the shipped table naming the blocked model lifts the block (a human decision wins)
        def adopt(d):
            old = d["families"]["codex"]["terra"]["known_good"]
            d["models"][NEWER_TERRA] = dict(d["models"][old], generation="9")
            d["families"]["codex"]["terra"]["known_good"] = NEWER_TERRA
            d["retired"]["codex"]["terra"].insert(0, old)
            del d["models"][old]
        t4 = self.bm.effective_table(self.load(adopt, "adopts-blocked"), undone)
        self.assertEqual((t4.head("terra"), t4.blocked), (NEWER_TERRA, {}))

    def test_the_models_a_step_superseded_follow_the_head_after_a_checkpoint_or_an_undo(self):
        # A catalog listing two newer models promotes the higher and records the lower as superseded. Astra's first review (finding 3):
        # when a shipped checkpoint moves the head to the step's model, the lower one stays a known predecessor (no `stale` FAIL for a
        # model the table went past). Fable's review (P2): when the checkpoint refuses the step's model instead, a lower one that is
        # still newer than the head is no predecessor: a `stale` finding and the next pass's candidate. Fable's review (design): an undo
        # blocks only the model the user undid; the next pass adopts the runner-up, announced like any promotion
        lower, higher = NEWER_TERRA, "gpt-9.9-terra"
        listed = [entry(lower), entry(higher)]
        ledger, t, steps = self.promote(self.observe(listed))
        self.assertEqual([(s["to"], s["superseded"]) for s in steps], [(higher, [lower])])

        def adopt(d):
            old = d["families"]["codex"]["terra"]["known_good"]
            d["models"][higher] = dict(d["models"][old], generation="9.9")
            d["families"]["codex"]["terra"]["known_good"] = higher
            d["retired"]["codex"]["terra"].insert(0, old)
            del d["models"][old]
        checkpoint = self.load(adopt, "checkpoint-higher-terra")
        t2 = self.bm.effective_table(checkpoint, ledger)
        self.assertEqual(t2.head("terra"), higher)
        self.assertIn(lower, t2.retired["codex"]["terra"])
        self.assertNotEqual(t2.sha256, checkpoint.sha256)  # a kept predecessor changes what the check reports: its own digest
        r = self.evaluate(t2, sources(catalog(lambda ms: ms.extend(copy.deepcopy(listed)))))
        self.assertEqual([x for x in self.of_class(r, "stale") if "terra" in x], [], r)
        again, t3, steps3 = self.bm.promote(checkpoint, ledger, self.observe(listed, table=t2), {}, self.NOW)
        self.assertEqual((steps3, t3.head("terra")), ([], higher))
        # the checkpoint refuses the step's model and keeps the head: the newer runner-up is no predecessor, and the next pass adopts it
        refusing = self.load(lambda d: d["refused"]["codex"].__setitem__(higher, {"family": "terra", "reason": "test: pulled by the vendor"}),
                             "refuses-higher-terra")
        t4 = self.bm.effective_table(refusing, ledger)
        self.assertEqual(t4.head("terra"), head("terra"))
        self.assertNotIn(lower, t4.retired["codex"]["terra"])
        r = self.evaluate(t4, sources(catalog(lambda ms: ms.extend(copy.deepcopy(listed)))))
        self.assertIn(f"stale: newer model may exist: {lower} is listed but not in the model table", self.of_class(r, "stale"))
        with self.assertRaises(self.bm.Refusal) as cm:
            t4.parse_seat(lower)
        self.assertIn(f"'{lower}' is not in the model table", str(cm.exception))
        later, t5, steps5 = self.bm.promote(refusing, ledger, self.observe(listed, table=t4), {}, self.NOW + 60)
        self.assertEqual(([s["to"] for s in steps5], t5.head("terra")), ([lower], lower))
        # undo: the head goes back and only the undone model is blocked; the next pass over the same catalog adopts the runner-up
        undone, step, after = self.bm.undo_promotion(self.shipped(), ledger, "terra", "worse on briefs", now=self.NOW + 60)
        self.assertEqual((step["to"], after.head("terra"), sorted(after.blocked)), (higher, head("terra"), [higher]))
        self.assertNotIn(lower, after.retired["codex"]["terra"])
        nxt, t6, steps6 = self.promote(self.observe(listed), ledger=undone, now=self.NOW + 120)
        self.assertEqual(([(s["from"], s["to"]) for s in steps6], t6.head("terra")), ([(head("terra"), lower)], lower))
        self.assertEqual([h["id"] for h in nxt["last_pass"]["held"]], [higher])
        self.assertIn("blocked on this Mac: its automatic promotion was undone", nxt["last_pass"]["held"][0]["reason"])

    def test_a_chain_of_steps_applies_in_order_and_a_refused_link_is_skipped(self):
        # Fable's re-review: two steps of one family through effective_table. 6.1 -> 9 then 9 -> 9.9 apply in order; a checkpoint that
        # refuses the intermediate 9 skips that step and the later one still applies (from the step's own `from`, which the table now
        # refuses): the head is 9.9 and the shipped head is untouched
        lower, higher = NEWER_TERRA, "gpt-9.9-terra"
        first, t1, _s1 = self.promote(self.observe([entry(lower)]))
        chain, t2, steps = self.promote(self.observe([entry(lower), entry(higher)]), ledger=first, now=self.NOW + 60)
        self.assertEqual([(s["from"], s["to"]) for s in chain["steps"]], [(head("terra"), lower), (lower, higher)])
        self.assertEqual(t2.head("terra"), higher)
        self.assertEqual(sorted(x for x in t2.retired["codex"]["terra"] if x in (head("terra"), lower)), sorted([head("terra"), lower]))
        refusing = self.load(lambda d: d["refused"]["codex"].__setitem__(lower, {"family": "terra", "reason": "test: pulled"}), "refuses-lower")
        t3 = self.bm.effective_table(refusing, chain)
        self.assertEqual(t3.head("terra"), higher)
        self.assertEqual([(s["to"], why) for s, why in t3.promotion["skipped"]], [(lower, f"the shipped table names {lower} now")])
        self.assertEqual(t3.base.head("terra"), head("terra"))
        # an undo of the chain's last step goes back one step, to the intermediate the user did not reject
        undone, step, after = self.bm.undo_promotion(self.shipped(), chain, "terra", "x", now=self.NOW + 120)
        self.assertEqual((step["to"], after.head("terra"), sorted(after.blocked)), (higher, lower, [higher]))

    def test_a_block_of_a_family_the_shipped_table_no_longer_has_is_ignored(self):
        # Fable's review (P3): a block whose family a later models.json dropped would make its refusal name a head that no longer exists
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)]))
        undone, _step, _after = self.bm.undo_promotion(self.shipped(), ledger, "terra", "x", now=self.NOW + 60)
        undone["blocked"][0]["family"] = "nova"
        t2 = self.bm.effective_table(self.shipped(), undone)
        self.assertEqual(t2.blocked, {})
        self.assertEqual([(b["id"], why) for b, why in t2.promotion["ignored"]], [(NEWER_TERRA, "nova is not a Codex family of the shipped table")])
        with self.assertRaises(self.bm.Refusal) as cm:
            t2.parse_seat(NEWER_TERRA)
        self.assertIn(f"'{NEWER_TERRA}' is not in the model table", str(cm.exception))

    def test_the_notice_names_the_promotion_for_a_week(self):
        ledger, t, steps = self.promote(self.observe([entry(NEWER_TERRA)]))
        text = self.bm.promotion_notice(t, NEWER_TERRA, now=self.NOW + 3600)
        self.assertTrue(text.startswith(f"NOTICE: models: terra runs {NEWER_TERRA}, promoted automatically on 2026-09-25 from {head('terra')}"), text)
        self.assertIn("--undo-promotion terra", text)
        self.assertIsNone(self.bm.promotion_notice(t, NEWER_TERRA, now=self.NOW + 8 * DAY))
        self.assertIsNone(self.bm.promotion_notice(t, head("sol"), now=self.NOW))

    def test_the_account_summary_keeps_no_identity(self):
        summary = self.bm.account_summary({"account": {"type": "chatgpt", "email": "someone@example.test", "planType": "pro",
                                                       "accountId": "acct-1"}, "workspaceRouting": {"chatgptAccountId": "acct-2"}})
        self.assertEqual(summary, {"signed_in": True, "type": "chatgpt", "plan": "pro"})
        self.assertEqual(self.bm.account_summary({"account": None, "requiresOpenaiAuth": True}), {"signed_in": False})
        self.assertIsNone(self.bm.account_summary("nonsense")["signed_in"])
        obs = self.observe([entry(NEWER_TERRA)], account=summary)
        self.assertNotIn("someone@example.test", json.dumps(obs))


if __name__ == "__main__":
    unittest.main()

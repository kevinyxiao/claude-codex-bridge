"""Cleanup group C7 (docs/model-routing-review.md, "Cleanup: C7"), the bridge's side: model table v17 adopts the new Sonnet.

Each test was written to fail on the v16 table:
- `sonnet` launches the new head, the one Anthropic's pages of 2026-09-29 name, and a Claude Code older than the head's floor
  (2.1.284, the first build whose baked model catalog lists it) is refused with `claude update`, before any launch;
- the old Sonnet is superseded: refused in one line that names the replacement, never swapped silently (PR #25), and a reply to a
  seat that ran it continues on the new head and says so;
- the models check passes against fresh verbatim captures of both pages (fixtures/evidence/*_20260929.md, PROVENANCE.md), while
  the v16 table fails against them and the v17 table cannot pass on the 2026-09-24 pages (they predate the head);
- the check's default page URLs are the pages' own canonical locations (the overview's old URL now redirects);
- the table's highest Claude floor, which the bootstrap requires of the installed Claude Code (and so Prof. West's kit through
  it), is the new head's.

Offline: the engine against the stub CLIs and the loopback page server of test_bridge.py (EngineBase); pure checks on
bridge_models.py.
"""
import json
import re
import unittest

from test_bridge import EVIDENCE, MODELS_JSON, TABLE, EngineBase, head, import_bridge_models

SONNET_OLD = "claude-sonnet-5"      # model-lint: allow the Sonnet the v16 table named, superseded by C7
SONNET_NEW = "claude-sonnet-5-5"    # model-lint: allow the Sonnet head of table v17 (C7)
PAGES_0929 = {"deprecations": EVIDENCE / "anthropic_model-deprecations_20260929.md", "overview": EVIDENCE / "anthropic_models-overview_20260929.md"}
PAGES_0924 = {"deprecations": EVIDENCE / "anthropic_model-deprecations_20260924.md", "overview": EVIDENCE / "anthropic_models-overview_20260924.md"}


def historical_haiku(t):
    """The pre-P-16 Haiku head, for checks against the frozen September pages."""
    current = t["families"]["claude"]["haiku"]["known_good"]
    old = next(m for m, rec in t["models"].items() if rec["family"] == "haiku" and rec["generation"] == "4.5")
    del t["models"][current]
    t["families"]["claude"]["haiku"]["known_good"] = old
    t["retired"]["claude"]["haiku"].remove(old)


def v16_sonnet(t):
    """The table as v16 had its Sonnet: the old head with its old floor, the new one unknown."""
    historical_haiku(t)
    rec = t["models"].pop(SONNET_NEW)
    t["models"][SONNET_OLD] = dict(rec, generation="5", min_cli="2.1.197", source="test: the v16 Sonnet head")
    t["families"]["claude"]["sonnet"]["known_good"] = SONNET_OLD
    t["retired"]["claude"]["sonnet"] = [x for x in t["retired"]["claude"]["sonnet"] if x != SONNET_OLD]


class GroupC7_SonnetHead(EngineBase):
    def attempt(self, name, spec, **extra):
        env, _ = self.isolated(name, **extra)
        renv, rd = self.relay(name, {"pick": {"codex": [{"exit": 0, "label": "a"}], "claude": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", spec, "--", "q", env_extra=env)
        return rc, out, err, env, rd

    def test_the_sonnet_seat_launches_the_new_head(self):
        self.assertEqual(head("sonnet"), SONNET_NEW)
        for spec in ("sonnet:low", "sonnet", SONNET_NEW, "claude/sonnet:high"):
            with self.subTest(spec=spec):
                rc, out, err, env, rd = self.attempt("c7-sonnet-" + re.sub(r"[^a-z0-9]+", "-", spec), spec, STUB_CLAUDE_VERSION="2.1.284 (Claude Code)")
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.launched_models(env), [SONNET_NEW])

    def test_a_claude_code_older_than_the_heads_floor_is_refused_before_any_launch(self):
        self.assertEqual(TABLE["models"][SONNET_NEW]["min_cli"], "2.1.284")
        rc, out, err, env, rd = self.attempt("c7-sonnet-2.1.283", "sonnet:low", STUB_CLAUDE_VERSION="2.1.283 (Claude Code)")
        line = self.assert_refused(rc, out, err)
        self.assertIn("seat 'sonnet' (%s) needs Claude Code >= 2.1.284" % SONNET_NEW, line)
        self.assertIn("claude update", line)
        self.assertEqual(self.launched_models(env), [])
        self.assertEqual(self.records(rd, "calls.jsonl"), [], "a refused seat must not reach relay")
        # the Opus floor is lower: that Claude Code still runs it
        rc, out, err, env, rd = self.attempt("c7-opus-2.1.283", "opus:low", STUB_CLAUDE_VERSION="2.1.283 (Claude Code)")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [head("opus")])

    def test_the_old_sonnet_is_refused_with_its_replacement_named(self):
        env, _ = self.isolated("c7-old-sonnet")
        before = self.job_names()
        rc, out, err = self.run_engine("ask", "--seats", SONNET_OLD + ":low", "--", "q", env_extra=env)
        line = self.assert_refused(rc, out, err)
        self.assertEqual(line, "bridge: refused seat '%s:low': %s is superseded by %s. Use sonnet (%s)." % (SONNET_OLD, SONNET_OLD, SONNET_NEW, SONNET_NEW))
        self.assertEqual(self.new_jobs(before), [])
        self.assertEqual(self.launched_models(env), [])
        self.assertIn(SONNET_OLD, TABLE["retired"]["claude"]["sonnet"])
        self.assertNotIn(SONNET_OLD, TABLE["models"], "a superseded head leaves the table's models, as every earlier one did")

    def test_a_reply_to_a_seat_that_ran_the_old_sonnet_continues_on_the_new_head_and_says_so(self):
        t = import_bridge_models().Table.load(str(MODELS_JSON))
        r, base, effort, note = t.resolve_recorded({"vendor": "claude", "model": SONNET_OLD, "effort": "high", "tag": "sonnet", "seat_name": "sonnet"})
        self.assertEqual((r.model, r.family, base, effort), (SONNET_NEW, "sonnet", SONNET_OLD, "high"))
        self.assertEqual(note, "(recorded %s is superseded)" % SONNET_OLD)


class GroupC7_TheCheck(EngineBase):
    def test_the_check_passes_against_the_2026_09_29_pages(self):
        env, _ = self.isolated("c7-check-0929", table=self.table_file("c7-historical-0929", historical_haiku), deprecations=PAGES_0929["deprecations"], overview=PAGES_0929["overview"],
                               STUB_CODEX_VERSION="codex-cli 0.156.1", STUB_CLAUDE_VERSION="2.1.284 (Claude Code)")
        rc, data, err = self.doctor_json(env)
        texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
        self.assertEqual((rc, data.get("verdict")), (0, "OK"), texts)
        self.assertFalse([t for t in texts if t.startswith(("stale:", "missing:", "cli:", "host:"))], texts)
        rc, out, err = self.run_engine("doctor", env_extra=env)
        self.assertEqual(rc, 0, out + err)

    def test_the_v16_table_fails_against_those_pages_and_v17_cannot_pass_on_the_older_ones(self):
        # the audit's S3: the v16 table against Anthropic's pages of 2026-09-29 names the new Sonnet as a newer model, in both pages
        table = self.table_file("c7-v16", v16_sonnet)
        env, _ = self.isolated("c7-check-v16", table=table, deprecations=PAGES_0929["deprecations"].read_text(), overview=PAGES_0929["overview"].read_text(),
                               STUB_CLAUDE_VERSION="2.1.284 (Claude Code)")
        rc, data, err = self.doctor_json(env)
        texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
        self.assertEqual((rc, data.get("verdict")), (1, "FAIL"), texts)
        self.assertIn("stale: newer model may exist: %s is listed as Active but not in the model table" % SONNET_NEW, texts)
        self.assertIn("stale: newer model may exist: %s is the current Claude API ID but not in the model table" % SONNET_NEW, texts)
        # the v17 table against the pages of 2026-09-24, both read: its Sonnet head is in neither
        env, _ = self.isolated("c7-check-0924", table=self.table_file("c7-historical-0924", historical_haiku), deprecations=PAGES_0924["deprecations"].read_text(), overview=PAGES_0924["overview"].read_text(),
                               STUB_CLAUDE_VERSION="2.1.284 (Claude Code)")
        rc, data, err = self.doctor_json(env)
        texts = [f.get("text") or "" for f in data.get("findings") or [] if isinstance(f, dict)]
        self.assertEqual((rc, data.get("verdict")), (1, "FAIL"), texts)
        self.assertIn("missing: %s is in neither Anthropic source" % SONNET_NEW, texts)

    def test_the_default_page_urls_are_the_pages_own_locations(self):
        # the overview's old URL (about-claude/models/overview.md) now redirects (facts_C7: curl's effective URL); a Python 3.9
        # urllib does not follow a 308, so the check reads the page where it lives. Each capture's frontmatter names its location
        bm = import_bridge_models()
        for name, path in PAGES_0929.items():
            with self.subTest(page=name):
                url = re.search(r"(?m)^url: (\S+)$", path.read_text()).group(1)
                self.assertEqual(bm.SOURCE_URLS[name], url + ".md")
        self.assertEqual(bm.source_urls({})["overview"], "https://platform.claude.com/docs/en/models/overview.md")
        self.assertEqual(bm.source_urls({"BRIDGE_MODELS_OVERVIEW_URL": "http://127.0.0.1:9/o"})["overview"], "http://127.0.0.1:9/o")


class GroupC7_Floors(unittest.TestCase):
    def test_the_claude_floor_the_bootstrap_requires_is_the_new_heads(self):
        # bootstrap.sh reads the highest non-null Claude min_cli of the table (read_model_table) and FAILS its `Claude Code version`
        # step below it; Prof. West's kit installs the bridge through that bootstrap (distribution/west-ai-kit, not edited here)
        t = import_bridge_models().Table.load(str(MODELS_JSON))
        self.assertEqual(t.highest_floor("claude"), t.floor(t.head("haiku")))
        floors = {m: rec["min_cli"] for m, rec in json.loads(MODELS_JSON.read_text())["models"].items() if rec.get("min_cli") and t.vendor_of_model(m) == "claude"}
        self.assertEqual(max(floors.values(), key=lambda v: tuple(int(p) for p in v.split("."))), floors[t.head("haiku")])
        data = json.loads(MODELS_JSON.read_text())
        # C7's v17 merged with main's v17 (Kevin's Sol checkpoint of 2026-10-01) into v18; that v18 merged with PR #34's v18 (the same
        # Sonnet head from the Models API listing) into v19 at the merge of origin/main cfdc6691; v20 is Kevin's Haiku checkpoint. Every
        # later human checkpoint bumps the version again (v21: Kevin's Terra checkpoint of 2026-11-07), so the merge is a lower bound
        self.assertGreaterEqual(data["version"], 20)
        self.assertGreaterEqual(data["updated"], "2026-09-29")


if __name__ == "__main__":
    unittest.main()

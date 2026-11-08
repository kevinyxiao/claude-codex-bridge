"""Family F3 of docs/model-routing.md (sections 2.1, 3.3, 5 and 6; the bridge's part of the test plan, 12.1 item 4): selection
bound to routing through the model registry, no descent without a `|` preference list, per-attempt provenance for Codex and
Claude seats, `--strict-model`, doctor's registry line and the seat grammar.

Runs bridge.py as a subprocess against the stub CLIs and the scripted stub relay of test_bridge.py. The engine finds this
checkout's model registry above itself (docs/model-routing.md 2.1); every case has its own MODEL_REGISTRY_HOME (catalog
snapshots, heads, scratch homes) and its own CODEX_HOME (where this machine's login writes its rollout files), so nothing under
~/.claude, ~/.codex or ~/.claude-relay is read or written and no network is reached. A relay account's CODEX_HOME is the stub
relay's `homes/<label>` directory: with STUB_CODEX_CATALOG_BY_HOME=1 the stub app-server answers model/list from the
`stub_catalog.json` found there, so accounts can carry different catalogs."""
import json
import os
import re
import shutil
import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_bridge import EngineBase, head, leading_notices, load_engine_module, CATALOG_CURRENT, NEWER_SOL, PAGES  # noqa: E402


def pages(catalog_path):
    """model/list requests one read of `catalog_path` takes (the stub pages by three; an entry removed can drop a page)."""
    return -(-len(json.loads(Path(catalog_path).read_text())["models"]) // 3)

REGISTRY_DIR = TESTS.parents[3] / "skills" / "model-registry"  # this checkout's registry: the copy the engine finds above itself


class GroupF_RegistryRouting(EngineBase):
    """docs/model-routing.md 3.3 (selection bound to routing), 5 (no silent descent) and 6 (per-attempt provenance) for the bridge."""

    # ---------------------------------------------------------------- helpers
    def case(self, name, **extra):
        """An isolated case (its own evidence store, stub records, registry state) with its own CODEX_HOME beside them."""
        env, cd = self.isolated(name, **extra)
        (cd / "codex-home").mkdir(exist_ok=True)
        env["CODEX_HOME"] = str(cd / "codex-home")
        return env, cd

    def home_catalog(self, rd, label, catalog):
        """Give the stub relay's account `label` its own catalog: <homes/label>/stub_catalog.json (STUB_CODEX_CATALOG_BY_HOME=1)."""
        home = rd / "homes" / label
        home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(catalog), str(home / "stub_catalog.json"))
        return home

    def provenance(self, d, tag):
        return self.read_records(Path(d) / f"provenance-{tag}.jsonl")

    def catalog_reads(self, env):
        """{basename of the CODEX_HOME: model/list requests} of the registry's own catalog reads (client "model-registry")."""
        out = {}
        for r in self.registry_reads(env):
            if r.get("method") == "model/list":
                key = os.path.basename(r.get("codex_home") or "")
                out[key] = out.get(key, 0) + 1
        return out

    def reply_seat(self, d, tag):
        return json.loads(sorted(Path(d).glob(f"seat-{tag}-reply-*.json"))[0].read_text())

    # ---------------------------------------------------------------- 3.3 steps 1-3: the capable set, the confined pick
    def test_the_pick_is_confined_to_the_accounts_whose_fresh_catalog_lists_the_model(self):
        # docs/model-routing.md 3.3 steps 1-3: every candidate account's catalog is read through the app-server under its own
        # CODEX_HOME when no fresh compatible snapshot exists; the capable set S holds the accounts whose catalog lists the head, not
        # hidden, at the effort; relay's pick is confined to S (`--only`, protocol 2 with only_applied); a fresh snapshot bound to
        # the same launch fingerprint is consumed without a read; the account's own rollout file is the attempt's provenance
        luna = head("luna")
        env, cd = self.case("f3-only", STUB_CODEX_CATALOG_BY_HOME="1")
        renv, rd = self.relay("f3-only", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        env.update(renv)
        self.home_catalog(rd, "a", CATALOG_CURRENT)
        no_luna = self.catalog_file("f3-only-b", lambda ms: ms.remove(self.entry(ms, luna)))
        self.home_catalog(rd, "b", no_luna)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-only", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.picks(rd), [["pick", "--vendor", "codex", "--json", "--model", "luna", "--only", "a"]])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
        self.assertIn(" account=a ", out)
        reads = self.catalog_reads(env)
        self.assertEqual(reads, {"a": PAGES, "b": pages(no_luna)}, f"each account's catalog is read once, under its own home: {reads}")
        d = self.jobs / "f3-only"
        seat = self.seat(d, "luna")
        self.assertEqual((seat["account"], seat["model"], seat["state"], seat["substituted"]), ("a", luna, "done", False))
        self.assertEqual(seat["provenance"]["observed_models"], [luna])  # the rollout under account a's home, this attempt's window
        self.assertTrue(str(self.provenance(d, "luna")[0]["rollout_path"]).startswith(str(rd / "homes" / "a")))
        # a second seat consumes the fresh snapshots: nothing is read again, the set is the same
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.catalog_reads(env), reads, "a fresh snapshot bound to the same launch fingerprint was read again")
        self.assertEqual([p[p.index("--only") + 1] for p in self.picks(rd)], ["a", "a"])

    def test_an_automatically_promoted_head_is_routed_only_to_accounts_whose_catalog_lists_it(self):
        # DECISIONS A28: the check promotes from this Mac's own login's catalog; a relay account whose catalog does not list the new
        # head is out of the capable set like any account that cannot run a seat's model, and relay's pick is confined to the rest
        new = "gpt-9-terra"  # fictional
        listing = self.catalog_file("f3-promoted-listing", lambda ms: ms.append(self.new_entry(new)))
        env, cd = self.case("f3-promoted", STUB_CODEX_CATALOG_BY_HOME="1", STUB_CODEX_CATALOG=str(listing))
        env["BRIDGE_MODELS_PROMOTE"] = None  # the engine tests' default is off
        renv, rd = self.relay("f3-promoted", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        self.home_catalog(rd, "a", listing)
        self.home_catalog(rd, "b", CATALOG_CURRENT)
        rc, out, err = self.run_engine("doctor", env_extra=env)
        self.assertIn(f"terra={new} (promoted on this Mac)", out)
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--job-dir", "f3-promoted", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.picks(rd), [["pick", "--vendor", "codex", "--json", "--model", "terra", "--only", "a"]])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
        seat = self.seat(self.jobs / "f3-promoted", "terra")
        self.assertEqual((seat["account"], seat["model"], seat["state"]), ("a", new, "done"))
        self.assertIn("promoted automatically", seat["expected_evidence"])
        # no account lists it: nothing launches, nothing descends, and the failure says where the head came from and how to go back
        (rd / "homes" / "a" / "stub_catalog.json").write_text(CATALOG_CURRENT.read_text())
        (cd / "mr-home" / "state.json").unlink()  # the stored snapshots go: the accounts' catalogs are read again
        rc, out, err = self.run_engine("ask", "--seats", "terra:low", "--job-dir", "f3-promoted-none", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        seat = self.seat(self.jobs / "f3-promoted-none", "terra")
        self.assertEqual((seat["state"], seat["account"]), ("failed", None))
        self.assertIn(f"no account can run {new} (a: {new} not listed; {new} was promoted automatically on ", seat["activity"])
        self.assertIn(f"--undo-promotion terra --reason \"<why>\" goes back to {head('terra')})); the seat has no preference list", seat["activity"])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"], "a seat launched although no account lists its model")

    def test_an_empty_capable_set_never_launches_and_names_every_account(self):
        # docs/model-routing.md 3.3: with S empty nothing runs: relay is not even asked, no CLI starts, and the failure names each
        # account and why (not listed, hidden, the effort not offered); without a `|` list there is nothing to descend to
        luna = head("luna")
        env, cd = self.case("f3-empty", STUB_CODEX_CATALOG_BY_HOME="1")
        renv, rd = self.relay("f3-empty", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}, {"exit": 0, "label": "c"}]}})
        env.update(renv)
        self.home_catalog(rd, "a", self.catalog_file("f3-empty-a", lambda ms: ms.remove(self.entry(ms, luna))))
        self.home_catalog(rd, "b", self.catalog_file("f3-empty-b", lambda ms: self.entry(ms, luna).__setitem__("hidden", True)))
        self.home_catalog(rd, "c", self.catalog_file("f3-empty-c", lambda ms: self.entry(ms, luna).__setitem__(
            "supportedReasoningEfforts", [{"reasoningEffort": x, "description": x} for x in ("medium", "high", "xhigh", "max")])))
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-empty", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.picks(rd), [], "relay was asked although no account could run the seat")
        self.assertEqual(self.records(rd, "execs.jsonl"), [])
        self.assertEqual(self.launched_models(env), [])
        seat = self.seat(self.jobs / "f3-empty", "luna")
        self.assertEqual((seat["state"], seat["account"]), ("failed", None))
        self.assertIn(f"no account can run {luna} (a: {luna} not listed; b: {luna} is hidden; c: {luna} does not offer effort low (medium, high, xhigh, max)); "
                      f"the seat has no preference list, so it does not descend", seat["activity"])
        self.assertEqual(self.meta(self.jobs / "f3-empty")["state"], "failed")

    def test_a_startup_rejection_invalidates_the_accounts_catalog_and_re_picks_within_the_rest(self):
        # docs/model-routing.md 3.3 step 5: a rejection at startup excludes the account for the model, its catalog is read again
        # (invalidated), and the seat re-picks within S minus the account; only once S is empty does the `astra|sol` list descend,
        # relay asked with the new family alias and the whole set again. Every attempt has its own provenance record
        astra, sol = head("astra"), head("sol")
        env, cd = self.case("f3-reject")
        renv, rd = self.relay("f3-reject", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "f3-reject", "--", f"[[STUB:badmodel={astra}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        picks = self.picks(rd)
        self.assertEqual([(p[p.index("--model") + 1], p[p.index("--only") + 1]) for p in picks], [("astra", "a,b"), ("astra", "b"), ("sol", "a,b")])
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a", "b", "b"])
        reads = self.catalog_reads(env)
        self.assertEqual(reads, {"a": 2 * PAGES, "b": 2 * PAGES}, f"each rejecting account's catalog is read once more after its rejection: {reads}")
        d = self.jobs / "f3-reject"
        completes = [r for r in self.provenance(d, "astra") if r["kind"] == "complete"]
        self.assertEqual([c["attempt_id"] for c in completes], [f"f3-reject/astra#{n}" for n in (1, 2, 3)])
        self.assertEqual([(c["model_expected"], c["observed_models"]) for c in completes], [(astra, []), (astra, []), (sol, [sol])])
        seat = self.seat(d, "astra")
        self.assertEqual((seat["model"], seat["account"], seat["attempt"], seat["provenance"]["downgraded_from"], seat["substituted"]),
                         (sol, "b", 3, "astra", False))
        self.assertTrue(any(l.startswith(f"NOTICE: seat astra: model {astra} -> {sol} (no capable account for {astra}: a: rejected {astra} at startup; "
                                         f"b: rejected {astra} at startup; the seat's preference list astra|sol allows it; attempt 3)")
                            for l in leading_notices(out)), out)

    def test_the_pre_launch_re_check_refreshes_and_re_picks_when_the_picked_snapshot_is_no_longer_usable(self):
        # docs/model-routing.md 3.3 step 4: right before the launch the picked account's snapshot is checked again (fresh, of the
        # current identity generation, compatible, listing the head). Here the account's identity generation moves while relay is
        # still picking (another process re-read its sign-in): the snapshot is stale for the launch, so the bridge refreshes it and
        # picks again within the set instead of launching on it
        luna = head("luna")
        env, cd = self.case("f3-recheck")
        renv, rd = self.relay("f3-recheck", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 4}, {"exit": 0, "label": "a"}]}})
        env.update(renv)
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "f3-recheck", "--", "q")
        d = self.jobs / "f3-recheck"
        try:
            self.wait_for(d, "luna", lambda s: "asking relay" in s.get("activity", ""))
            for _ in range(50):  # the relay helper is sleeping in its pick
                if self._procs_matching("stub_relay.py pick"):
                    break
                time.sleep(0.1)
            state_path = cd / "mr-home" / "state.json"
            state = json.loads(state_path.read_text())
            state.setdefault("identities", {}).setdefault("codex", {})["a"] = {"generation": 1}
            state_path.write_text(json.dumps(state))
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(len(self.picks(rd)), 2, "the seat launched on a snapshot the pre-launch re-check should have rejected")
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
        self.assertEqual(self.catalog_reads(env), {"a": 2 * PAGES}, "the stale snapshot was not read again before the re-pick")
        seat = self.seat(d, "luna")
        self.assertEqual((seat["state"], seat["account"], seat["model"]), ("done", "a", luna))
        state = json.loads((cd / "mr-home" / "state.json").read_text())
        self.assertEqual(state["catalogs"]["codex"]["a"]["identity_generation"], 1)  # the refreshed snapshot belongs to the current generation

    # ---------------------------------------------------------------- 3.3 step 3: an old relay, no accounts, no home
    def test_a_relay_that_cannot_apply_the_set_makes_the_bridge_validate_its_own_login_never_fall_through(self):
        # docs/model-routing.md 3.3 (Astra's finding 4 of round 3): a pick that does not answer protocol 2 with only_applied true
        # (an old relay's usage error, a v1 answer, a v2 answer without the field or with it false) is `unsupported`: the bridge then
        # validates this machine's own login exactly as it would an account, and launches there only when its catalog lists the model
        luna = head("luna")
        no_luna = self.catalog_file("f3-local-none", lambda ms: ms.remove(self.entry(ms, luna)))
        for support in ("exit2", "v1", "no_only_applied", "false"):
            with self.subTest(relay=support):
                env, cd = self.case(f"f3-local-{support}")
                renv, rd = self.relay(f"f3-local-{support}", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "only_support": support})
                env.update(renv)
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", f"f3-local-{support}", "--", "q", env_extra=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(len(self.picks(rd)), 1)
                self.assertIn("--only", self.picks(rd)[0])
                self.assertEqual(self.records(rd, "execs.jsonl"), [], "the seat ran through an account relay could not confine the pick to")
                d = self.jobs / f"f3-local-{support}"
                self.assertEqual(self.meta(d)["router"], {"codex": "unrouted"})
                seat = self.seat(d, "luna")
                self.assertEqual((seat["state"], seat["account"]), ("done", None))
                self.assertIn("codex-home", self.catalog_reads(env), "this machine's own catalog was not read before the launch")
                self.assertEqual(seat["provenance"]["observed_models"], [luna])  # the rollout under this machine's CODEX_HOME
                # this machine's own catalog does not list the model (account a's does, so the set is not empty and relay is asked):
                # nothing launches, the reason names the login and the read
                env2, cd2 = self.case(f"f3-local-{support}-none", STUB_CODEX_CATALOG_BY_HOME="1")
                renv2, rd2 = self.relay(f"f3-local-{support}-none", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "only_support": support})
                env2.update(renv2)
                self.home_catalog(rd2, "a", CATALOG_CURRENT)
                shutil.copyfile(str(no_luna), str(cd2 / "codex-home" / "stub_catalog.json"))
                rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", f"f3-local-{support}-none", "--", "q", env_extra=env2)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(len(self.picks(rd2)), 1, "relay was not asked although account a could run the seat")
                self.assertEqual(self.launched_models(env2), [])
                self.assertEqual(self.records(rd2, "execs.jsonl"), [])
                seat = self.seat(self.jobs / f"f3-local-{support}-none", "luna")
                self.assertEqual(seat["state"], "failed")
                self.assertIn(f"this machine's own login cannot run {luna} (default: {luna} not listed)", seat["activity"])
        # relay lists no Codex account at all: no pick, this machine's login validated
        env, cd = self.case("f3-local-noaccounts")
        renv, rd = self.relay("f3-local-noaccounts", {"pick": {"codex": [{"exit": 3}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-local-noaccounts", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.picks(rd), [])
        self.assertEqual(self.meta(self.jobs / "f3-local-noaccounts")["router"], {"codex": "unrouted"})
        self.assertIn("codex-home", self.catalog_reads(env))
        # an older relay that cannot say where an account's CODEX_HOME is: that account cannot be validated, so it leaves the set
        env, cd = self.case("f3-no-home")
        renv, rd = self.relay("f3-no-home", {"pick": {"codex": [{"exit": 0, "label": "a"}]}, "no_codex_home": True})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-no-home", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.picks(rd), [])
        self.assertEqual(self.launched_models(env), [])
        self.assertIn("a: relay does not say where its CODEX_HOME is", self.seat(self.jobs / "f3-no-home", "luna")["activity"])

    def test_this_machines_login_is_validated_before_every_launch_and_descends_only_by_its_list(self):
        # docs/model-routing.md 3.3 step 3 and 5 (checkpoint F3 round 1, P1): the local candidate is validated like an account before
        # EVERY launch, a descent included: a login whose catalog does not list the first model descends through the seat's list to
        # one it does list (nothing else launches), a descent target the catalog does not list ends the seat, and a plain seat ends on
        # the missing listing naming the list that would allow it
        astra, sol = head("astra"), head("sol")
        no_astra = self.catalog_file("f3-local-noastra", lambda ms: ms.remove(self.entry(ms, astra)))
        no_sol = self.catalog_file("f3-local-nosol", lambda ms: ms.remove(self.entry(ms, sol)))
        env, cd = self.case("f3-local-descent", STUB_CODEX_CATALOG=str(no_astra))  # no relay: the local login is the candidate
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "f3-local-descent", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.launched_models(env), [sol], "a model the login's catalog does not list was launched")
        seat = self.seat(self.jobs / "f3-local-descent", "astra")
        self.assertEqual((seat["model"], seat["state"], seat["account"], seat["provenance"]["downgraded_from"]), (sol, "done", None, "astra"))
        self.assertTrue(any(l.startswith(f"NOTICE: seat astra: model {astra} -> {sol} (no capable account for {astra}: default: {astra} not listed; "
                                         "the seat's preference list astra|sol allows it; attempt 1)") for l in leading_notices(out)), out)
        # the login rejects Astra at startup and its catalog does not list Sol: the descent target is validated, not launched
        env, cd = self.case("f3-local-descent-unlisted", STUB_CODEX_CATALOG=str(no_sol))
        rc, out, err = self.run_engine("ask", "--seats", "astra|sol:low", "--job-dir", "f3-local-descent-unlisted", "--", f"[[STUB:badmodel={astra}]] q",
                                       env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [astra])
        seat = self.seat(self.jobs / "f3-local-descent-unlisted", "astra")
        self.assertEqual((seat["model"], seat["state"]), (sol, "failed"))
        self.assertIn(f"this machine's own login cannot run {sol} (default: {sol} not listed); the seat's preference list is exhausted", seat["activity"])
        # a plain seat ends on the missing listing, naming the spec that would allow a descent
        env, cd = self.case("f3-local-plain", STUB_CODEX_CATALOG=str(no_astra))
        rc, out, err = self.run_engine("ask", "--seats", "astra:low", "--job-dir", "f3-local-plain", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [])
        self.assertIn(f"this machine's own login cannot run {astra} (default: {astra} not listed); the seat has no preference list, so it does not "
                      f"descend (say astra|sol:low to allow it)", self.seat(self.jobs / "f3-local-plain", "astra")["activity"])

    def test_a_relay_account_labelled_default_is_resolved_through_relay_never_this_machines_login(self):
        # checkpoint F3 round 1, P1: `default` is the registry's name for this machine's own login only once relay is out of the
        # picture; a relay account may carry the label too, and its home is what relay says. Here relay's `default` lists Luna and
        # this machine's login does not: the seat runs, under relay's home, because relay's account was the one validated
        luna = head("luna")
        env, cd = self.case("f3-relay-default", STUB_CODEX_CATALOG_BY_HOME="1")
        renv, rd = self.relay("f3-relay-default", {"pick": {"codex": [{"exit": 0, "label": "default"}]}})
        env.update(renv)
        self.home_catalog(rd, "default", CATALOG_CURRENT)
        shutil.copyfile(str(self.catalog_file("f3-relay-default-local", lambda ms: ms.remove(self.entry(ms, luna)))), str(cd / "codex-home" / "stub_catalog.json"))
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-relay-default", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["default"])
        self.assertEqual(self.picks(rd), [["pick", "--vendor", "codex", "--json", "--model", "luna", "--only", "default"]])
        reads = self.catalog_reads(env)
        self.assertEqual(reads, {"default": PAGES}, f"the catalog validated was not relay's account's, read under relay's home: {reads}")
        seat = self.seat(self.jobs / "f3-relay-default", "luna")
        self.assertEqual((seat["account"], seat["state"], seat["provenance"]["observed_models"]), ("default", "done", [luna]))
        self.assertTrue(str(self.provenance(self.jobs / "f3-relay-default", "luna")[0]["rollout_path"]).startswith(str(rd / "homes" / "default")))

    def test_a_routed_accounts_catalog_is_read_under_the_environment_relay_gives_the_launch(self):
        # checkpoint F3 round 1, P1: `relay exec` strips the caller's provider and credential variables (OPENAI_*, CODEX_API_KEY,
        # ANTHROPIC_*) before it becomes the CLI, so a routed account's discovery runs without them too: the catalog validated is
        # the one the launch will use. This machine's own login launches with the bridge's environment, and is discovered with it
        luna = head("luna")
        ambient = {"OPENAI_BASE_URL": "http://proxy.invalid", "CODEX_API_KEY": "not-a-real-key"}
        env, cd = self.case("f3-env-routed", **ambient)
        renv, rd = self.relay("f3-env-routed", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-env-routed", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        routed = [r for r in self.registry_reads(env) if os.path.basename(r.get("codex_home") or "") == "a"]
        self.assertTrue(routed)
        self.assertEqual({tuple(r.get("provider_env") or []) for r in routed}, {()}, "the routed discovery saw the caller's provider variables")
        env, cd = self.case("f3-env-local", **ambient)  # no relay: the local login, discovered as it launches
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-env-local", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        local = [r for r in self.registry_reads(env) if os.path.basename(r.get("codex_home") or "") == "codex-home"]
        self.assertTrue(local)
        self.assertEqual({tuple(r.get("provider_env") or []) for r in local}, {("CODEX_API_KEY", "OPENAI_BASE_URL")})
        self.assertEqual(self.codex_records(env, "exec")[-1].get("codex_home"), str(cd / "codex-home"))

    def test_a_relay_default_sharing_this_machines_home_is_read_again_under_the_local_environment_when_relay_cannot_confine_the_pick(self):
        # checkpoint F3 round 2, P1: relay's registered `default` is this machine's own login (same CODEX_HOME). Its catalog is
        # discovered under the environment relay gives a launch (no ambient provider); when relay cannot apply the set the seat
        # falls to the local candidate, whose launch keeps the ambient provider, so its snapshot is bound to a different fingerprint
        # and read again under that environment; here the ambient provider's catalog lacks Luna and the seat does not run
        luna = head("luna")
        no_luna = self.catalog_file("f3-shared-provider", lambda ms: ms.remove(self.entry(ms, luna)))
        env, cd = self.case("f3-shared-home", OPENAI_BASE_URL="http://proxy.invalid", STUB_CODEX_CATALOG_PROVIDER=str(no_luna))
        renv, rd = self.relay("f3-shared-home", {"pick": {"codex": [{"exit": 0, "label": "default"}]}, "only_support": "exit2",
                                                 "homes": {"default": str(cd / "codex-home")}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-shared-home", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.launched_models(env), [], "the local launch ran on a catalog read under relay's environment")
        self.assertEqual(len(self.picks(rd)), 1)
        seat = self.seat(self.jobs / "f3-shared-home", "luna")
        self.assertEqual((seat["state"], seat["account"]), ("failed", None))
        self.assertIn(f"this machine's own login cannot run {luna} (default: {luna} not listed)", seat["activity"])
        reads = [r for r in self.registry_reads(env) if r.get("method") == "model/list" and os.path.basename(r.get("codex_home") or "") == "codex-home"]
        self.assertEqual(sorted({tuple(r.get("provider_env") or []) for r in reads}), [(), ("OPENAI_BASE_URL",)], "the home was not read under both environments")
        self.assertEqual(len(reads), PAGES + pages(no_luna))
        # the same scenario without an ambient provider: the launch and relay's discovery see the same login, one read serves both
        env, cd = self.case("f3-shared-home-clean")
        renv, rd = self.relay("f3-shared-home-clean", {"pick": {"codex": [{"exit": 0, "label": "default"}]}, "only_support": "exit2",
                                                       "homes": {"default": str(cd / "codex-home")}})
        env.update(renv)
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-shared-home-clean", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.catalog_reads(env), {"codex-home": PAGES}, "an identical launch environment re-read the shared home")
        self.assertEqual(self.seat(self.jobs / "f3-shared-home-clean", "luna")["account"], None)

    def test_a_configuration_edit_during_the_pick_is_discovered_again_before_the_launch(self):
        # checkpoint F3 round 2, P2 (design 3.3: an edit to any file in the fingerprint forces re-discovery before selection): relay's
        # pick may wait; the picked account's launch is fingerprinted again when the pick returns, so a config.toml written into its
        # home during the wait re-discovers the launch and re-reads its catalog before the pre-launch check accepts the snapshot
        luna = head("luna")
        env, cd = self.case("f3-edit-during-pick")
        renv, rd = self.relay("f3-edit-during-pick", {"pick": {"codex": [{"exit": 0, "label": "a", "sleep": 4}]}})
        env.update(renv)
        home = rd / "homes" / "a"
        home.mkdir(parents=True, exist_ok=True)
        p = self.start_engine(env, "ask", "--seats", "luna:low", "--job-dir", "f3-edit-during-pick", "--", "q")
        d = self.jobs / "f3-edit-during-pick"
        try:
            self.wait_for(d, "luna", lambda s: "asking relay" in s.get("activity", ""))
            for _ in range(50):  # the relay helper is sleeping in its pick
                if self._procs_matching("stub_relay.py pick"):
                    break
                time.sleep(0.1)
            (home / "config.toml").write_text("# written while relay was picking\n", encoding="utf-8")
        finally:
            out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual([e["label"] for e in self.records(rd, "execs.jsonl")], ["a"])
        self.assertEqual(len(self.picks(rd)), 2, "the changed launch was not re-picked within the set")
        self.assertEqual(self.catalog_reads(env), {"a": 2 * PAGES}, "the edited configuration was not discovered again before the launch")
        state = json.loads((cd / "mr-home" / "state.json").read_text())
        self.assertIn(str(home / "config.toml"), state["catalogs"]["codex"]["a"]["client"]["config_file_hashes"])
        self.assertEqual((self.seat(d, "luna")["state"], self.seat(d, "luna")["account"]), ("done", "a"))

    def test_an_effort_shortfall_after_a_model_rejection_still_descends_by_the_effort_list(self):
        # checkpoint F3 round 2, P2: account a advertises ultra but rejects the model at startup; account b lists Astra at max only.
        # Once a is out, the remaining catalogs lack the effort, and the seat's effort list applies whatever a's rejection was about
        astra = head("astra")
        env, cd = self.case("f3-effort-after-rejection", STUB_CODEX_CATALOG_BY_HOME="1")
        renv, rd = self.relay("f3-effort-after-rejection", {"pick": {"codex": [{"exit": 0, "label": "a"}, {"exit": 0, "label": "b"}]}})
        env.update(renv)
        self.home_catalog(rd, "a", CATALOG_CURRENT)
        self.home_catalog(rd, "b", self.catalog_file("f3-effort-b", lambda ms: self.entry(ms, astra).__setitem__(
            "supportedReasoningEfforts", [{"reasoningEffort": x, "description": x} for x in ("low", "medium", "high", "xhigh", "max")])))
        rc, out, err = self.run_engine("ask", "--seats", "astra:ultra|max", "--job-dir", "f3-effort-after-rejection", "--", "[[STUB:badmodelon=a]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(r["account"], r["model"], r["effort"]) for r in self.codex_records(env, "exec")], [("a", astra, "ultra"), ("b", astra, "max")])
        # at ultra only a is capable (b lacks the effort); at max, a excluded, only b
        self.assertEqual([(p[p.index("--model") + 1], p[p.index("--only") + 1]) for p in self.picks(rd)], [("astra", "a"), ("astra", "b")])
        seat = self.seat(self.jobs / "f3-effort-after-rejection", "astra")
        self.assertEqual((seat["model"], seat["effort"], seat["account"], seat["attempt"]), (astra, "max", "b", 2))
        self.assertTrue(any(l.startswith(f"NOTICE: seat astra: effort ultra -> max (no account's catalog offers ultra for {astra}; "
                                         "the seat's effort preference list allows it; attempt 2)") for l in leading_notices(out)), out)

    def test_an_effort_no_catalog_offers_descends_by_the_effort_list_and_a_fast_seat_needs_the_tier_advertised(self):
        # checkpoint F3 round 1, P2: the capable set can be empty because the catalogs list the model without the seat's effort; the
        # seat's effort preference list applies then (the model stays), as it does after a startup rejection; without one the seat
        # ends naming the effort list that would allow it. A fast seat needs the fast speed tier advertised (the registry's rule)
        astra = head("astra")
        env, cd = self.case("f3-effort-catalog", STUB_CODEX_CATALOG_BY_HOME="1")
        renv, rd = self.relay("f3-effort-catalog", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        no_ultra = self.catalog_file("f3-effort-a", lambda ms: self.entry(ms, astra).__setitem__(
            "supportedReasoningEfforts", [{"reasoningEffort": x, "description": x} for x in ("low", "medium", "high", "xhigh", "max")]))
        self.home_catalog(rd, "a", no_ultra)
        rc, out, err = self.run_engine("ask", "--seats", "astra:ultra|max", "--job-dir", "f3-effort-catalog", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        execs = self.codex_records(env, "exec")
        self.assertEqual([(r["model"], r["effort"]) for r in execs], [(astra, "max")])
        seat = self.seat(self.jobs / "f3-effort-catalog", "astra")
        self.assertEqual((seat["model"], seat["effort"], seat["account"]), (astra, "max", "a"))
        self.assertTrue(any(l.startswith(f"NOTICE: seat astra: effort ultra -> max (no account's catalog offers ultra for {astra}; "
                                         "the seat's effort preference list allows it; attempt 1)") for l in leading_notices(out)), out)
        rc, out, err = self.run_engine("ask", "--seats", "astra:ultra", "--job-dir", "f3-effort-plain", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(len(self.codex_records(env, "exec")), 1)
        self.assertIn(f"no account can run {astra} (a: {astra} does not offer effort ultra (low, medium, high, xhigh, max)); the seat has no effort "
                      f"preference list, so it does not descend (say astra:ultra|max to allow it)", self.seat(self.jobs / "f3-effort-plain", "astra")["activity"])
        # no fast tier advertised: the account is out for a fast seat
        env, cd = self.case("f3-fast-catalog", STUB_CODEX_CATALOG_BY_HOME="1")
        renv, rd = self.relay("f3-fast-catalog", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env.update(renv)
        self.home_catalog(rd, "a", self.catalog_file("f3-fast-a", lambda ms: self.entry(ms, astra).__setitem__("additionalSpeedTiers", [])))
        rc, out, err = self.run_engine("ask", "--seats", "astra:low@fast", "--job-dir", "f3-fast-catalog", "--", "q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.codex_records(env, "exec"), [])
        self.assertIn(f"no account can run {astra} (a: {astra} has no fast speed tier (none advertised))", self.seat(self.jobs / "f3-fast-catalog", "astra")["activity"])

    # ---------------------------------------------------------------- 5: the descent is recorded everywhere it shows
    def test_a_preference_list_descent_is_recorded_in_the_seat_the_findings_and_the_report(self):
        # docs/model-routing.md 5: requested, expected and observed stay distinct in every record; a descent through the `|` list
        # is recorded (downgraded_from) and shown, never silent: findings-<seat>.json, the merged report's header, the footer
        astra, sol = head("astra"), head("sol")
        env, cd = self.case("f3-descent")
        rc, out, err = self.run_engine("review", "--seats", "astra|sol:low", "--job-dir", "f3-descent", "--intent", "add parse_port",
                                       "--focus", f"[[STUB:badmodel={astra}]]", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "f3-descent"
        f = json.loads((d / "findings-astra.json").read_text())
        self.assertTrue(f["_ok"], f)
        self.assertEqual((f["_model_requested"], f["_model_expected"], f["_observed_models"], f["_downgraded_from"], f["_substitution"]),
                         ("astra|sol", sol, [sol], "astra", None))
        merged = (d / "merged.md").read_text()
        self.assertIn(f"requested astra|sol -> ran {sol}", merged)
        self.assertNotIn("SUBSTITUTED", merged)
        self.assertRegex(out, r"(?m)^— astra=" + re.escape(sol) + r"@low \(fell back from " + re.escape(astra) + r"\)")
        completes = [r for r in self.provenance(d, "astra") if r["kind"] == "complete"]
        self.assertEqual([c["attempt_id"] for c in completes], ["f3-descent/astra#1", "f3-descent/astra#2"])
        self.assertEqual([(c["model_requested"], c["model_expected"], c["observed_model_id"], c["observed_via"]) for c in completes],
                         [("astra|sol", astra, None, "rollout:none-in-attempt"), ("astra|sol", sol, sol, "rollout:turn_context")])
        self.assertEqual(completes[0]["outcome"], "error")  # rejected at startup: no thread, a non-zero exit
        self.assertTrue(all(c["source"] == "bridge" for c in completes))
        seat = self.seat(d, "astra")
        self.assertEqual((seat["provenance"]["model_requested"], seat["provenance"]["model_expected"], seat["provenance"]["downgraded_from"]),
                         ("astra|sol", sol, "astra"))
        self.assertFalse(seat["substituted"])
        self.assertIn("registry agrees", seat["expected_evidence"])  # the table's head and the registry's head coincide today

    # ---------------------------------------------------------------- 6: Codex provenance from the attempt's own rollout window
    def test_codex_provenance_reads_only_the_attempts_own_rollout_window_and_strict_model_fails_a_substitution(self):
        # docs/model-routing.md 6: a Codex attempt is observed from its own window of the rollout file under the account's CODEX_HOME
        # (a new thread from the start of the file it created; a resumed one from where the file ended); the turn_context's model is
        # the observation; another model than the seat asked for is a substitution, noticed and marked, and fatal under
        # --strict-model, as is a window the collector cannot read
        sol = head("sol")
        env, cd = self.case("f3-codex-prov")
        # 1. an honest run
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", "f3-prov-ok", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "f3-prov-ok"
        start, complete = self.provenance(d, "sol")
        self.assertEqual((start["kind"], start["observed_model_id"], start["cursor_source"], start["effort"]), ("start", sol, "new-thread", "low"))
        self.assertTrue(str(start["rollout_path"]).startswith(str(cd / "codex-home")), start)
        self.assertEqual((complete["kind"], complete["observed_models"], complete["turn_contexts"], complete["substituted"], complete["errors"]),
                         ("complete", [sol], 1, False, []))
        self.assertEqual((complete["model_requested"], complete["model_expected"], complete["attempt_id"]), ("sol", sol, "f3-prov-ok/sol#1"))
        seat = self.seat(d, "sol")
        self.assertEqual((seat["provenance"]["observed_model_id"], seat["provenance"]["outcome"], seat["substituted"]), (sol, "success", False))
        self.assertEqual(seat["provenance"]["registry_path"], str(REGISTRY_DIR / "registry.json"))
        self.assertNotIn("SUBSTITUTED", out)
        # 2. the rollout names another model: a substitution, noticed and marked in the footer; the seat completes without --strict-model
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", "f3-prov-sub", "--", f"[[STUB:rolloutmodel={NEWER_SOL}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "f3-prov-sub"
        seat = self.seat(d, "sol")
        self.assertTrue(seat["substituted"])
        self.assertEqual((seat["provenance"]["observed_model_id"], seat["provenance"]["substitution"]["observed"], seat["provenance"]["substitution"]["expected"]),
                         (NEWER_SOL, NEWER_SOL, sol))
        self.assertTrue(any(l.startswith(f"NOTICE: seat sol: SUBSTITUTED {sol} -> {NEWER_SOL} (") for l in leading_notices(out)), out)
        self.assertRegex(out, r"(?m)^— sol=.*SUBSTITUTED " + re.escape(sol) + " -> " + re.escape(NEWER_SOL))
        self.assertIn("substituted", [n["kind"] for n in self.notices(d)])
        # 3. --strict-model fails it (the output is kept), and the attempt is not retried
        rc, out, err = self.run_engine("ask", "--strict-model", "--seats", "sol:low", "--job-dir", "f3-prov-strict", "--", f"[[STUB:rolloutmodel={NEWER_SOL}]] q",
                                       env_extra=env)
        self.assertEqual(rc, 1, out + err)
        d = self.jobs / "f3-prov-strict"
        seat = self.seat(d, "sol")
        self.assertEqual((seat["state"], seat["attempt"]), ("failed", 1))
        self.assertIn(f"--strict-model: ran {NEWER_SOL}, requested {sol}", seat["activity"])
        self.assertIn("strict-model", [n["kind"] for n in self.notices(d)])
        self.assertTrue((d / "raw-sol.md").read_text().strip(), "the answer of the substituted run is kept")
        self.assertEqual(len(self.codex_records(env, "exec")), 3)
        # 4. a window the collector cannot read (a turn_context without any model) is a collection error: recorded, fatal under --strict-model
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", "f3-prov-unreadable", "--", "[[STUB:rolloutmodel=none]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "f3-prov-unreadable", "sol")
        self.assertEqual((seat["provenance"]["observed_model_id"], seat["provenance"]["errors"], seat["substituted"]), (None, ["rollout:unrecognized"], False))
        rc, out, err = self.run_engine("ask", "--strict-model", "--seats", "sol:low", "--job-dir", "f3-prov-unreadable-strict", "--",
                                       "[[STUB:rolloutmodel=none]] q", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--strict-model: provenance collection error(s): rollout:unrecognized", self.seat(self.jobs / "f3-prov-unreadable-strict", "sol")["activity"])
        # 5. a reply resumes the honest thread: its window is its own, and --strict-model passes
        rc, out, err = self.run_engine("reply", "f3-prov-ok", "--strict-model", "--", "more", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        reply = self.reply_seat(self.jobs / "f3-prov-ok", "sol")
        self.assertEqual((reply["state"], reply["provenance"]["observed_models"], reply["substituted"]), ("done", [sol], False))
        self.assertEqual(reply["provenance"]["model_requested"], "sol")

    # ---------------------------------------------------------------- 6: Claude provenance from the attempt's own stream
    def test_claude_provenance_is_recorded_per_attempt_from_its_own_stream(self):
        # docs/model-routing.md 6: a Claude attempt's records come from its own stream (system/init, then every message.model
        # observation, the result's cumulative ledger): a start record and a complete record per attempt, `source` bridge; a retry
        # after a usage limit is another attempt with its own records; --strict-model passes a run that observed its model
        sonnet = head("sonnet")
        env, cd = self.case("f3-claude-prov")
        rc, out, err = self.run_engine("ask", "--strict-model", "--seats", "sonnet:low", "--job-dir", "f3-claude-prov", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        d = self.jobs / "f3-claude-prov"
        records = self.provenance(d, "sonnet")
        self.assertEqual([r["kind"] for r in records], ["start", "complete"])
        self.assertEqual((records[0]["observed_via"], records[0]["observed_model_id"], records[0]["session_id"]), ("cli:init", sonnet, "stub-session-aaaa"))
        self.assertEqual((records[1]["observed_models"], records[1]["outcome"], records[1]["substituted"], records[1]["source"]), ([sonnet], "success", False, "bridge"))
        seat = self.seat(d, "sonnet")
        self.assertEqual((seat["provenance"]["model_requested"], seat["provenance"]["model_expected"], seat["provenance"]["observed_model_id"]),
                         ("sonnet", sonnet, sonnet))
        self.assertFalse(seat["substituted"])
        self.assertEqual(seat["provenance"]["registry_path"], str(REGISTRY_DIR / "registry.json"))
        rc, out, err = self.run_engine("ask", "--seats", "sonnet:low", "--job-dir", "f3-claude-retry", "--", f"[[STUB:limit={cd / 'limit'}]] q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        completes = [r for r in self.provenance(self.jobs / "f3-claude-retry", "sonnet") if r["kind"] == "complete"]
        self.assertEqual([c["attempt_id"] for c in completes], ["f3-claude-retry/sonnet#1", "f3-claude-retry/sonnet#2"])
        self.assertIn(completes[0]["outcome"], ("limit", "error"))
        self.assertEqual(completes[1]["outcome"], "success")
        # checkpoint F3 round 1, P2: the CLI's own synthetic messages (a rate-limit notice, an API error, a forwarded subagent turn)
        # are telemetry the observer excludes, not collection errors: recorded on the attempt, they never fail --strict-model
        rc, out, err = self.run_engine("ask", "--strict-model", "--seats", "sonnet:low", "--job-dir", "f3-claude-synthetic", "--", "[[STUB:synthetic]] q",
                                       env_extra=env)
        self.assertEqual(rc, 0, out + err)
        seat = self.seat(self.jobs / "f3-claude-synthetic", "sonnet")
        self.assertEqual((seat["state"], seat["provenance"]["errors"], seat["provenance"]["observed_model_id"], seat["substituted"]), ("done", [], sonnet, False))
        complete = [r for r in self.provenance(self.jobs / "f3-claude-synthetic", "sonnet") if r["kind"] == "complete"][0]
        self.assertTrue(complete["errors"], "the excluded events' telemetry is kept on the record")
        self.assertTrue(all("kind" not in e for e in complete["errors"]))

    # ---------------------------------------------------------------- doctor and the registry link
    def older_registry(self, cd):
        """A copy of this checkout's registry as it stood before the last checkpoint of the Sol and Luna heads (the registry suite's
        `older_registry_dir`; Luna's: cleanup C1, a vendor edge; Sol's: Kevin's of 2026-10-01, no vendor edge): each of those floors
        set back to the predecessor its checkpoint link names, the link and the retired entry removed. Doctor's floor case runs on
        this copy, since the live registry's floors are the table's heads now."""
        reg = json.loads((REGISTRY_DIR / "registry.json").read_text())
        for fam in ("sol", "luna"):
            rec = reg["families"]["codex"][fam]
            link = [l for l in rec.get("succession") or [] if l["to"] == rec["known_good"]][0]
            rec["known_good"] = link["from"]
            rec["succession"] = [l for l in rec["succession"] if l is not link]
            reg["retired"]["codex"][fam] = [r for r in reg["retired"]["codex"][fam] if r != link["from"]]
        root = cd / "older-registry"
        (root / "scripts").mkdir(parents=True)
        for name in ("model_registry.py", "dispatch_scan.py"):
            shutil.copyfile(str(REGISTRY_DIR / "scripts" / name), str(root / "scripts" / name))
        (root / "registry.json").write_text(json.dumps(reg, indent=2))
        return root

    def test_doctor_reports_the_registry_and_the_heads_agreement_and_a_broken_registry_keeps_the_built_in_routing(self):
        # docs/model-routing.md 2.1 and the F3 notes: doctor names the registry copy the bridge found (this checkout's) and its state,
        # and says whether the table's Codex heads agree with the heads the registry accepted; a disagreement is reported, the table
        # decides the seat. Doctor stays model-free: no account catalog is read. A registry copy that cannot load is said once and the
        # bridge keeps its built-in table and routing (relay's legacy pick, identities-only provenance)
        env, cd = self.case("f3-doctor")
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"registry: {REGISTRY_DIR / 'registry.json'}", out)
        self.assertIn(f"state {cd / 'mr-home'}", out)
        self.assertIn("constrained routing:", out)
        self.assertEqual(self.registry_reads(env), [])
        # since cleanup C1 checkpointed the Sol and Luna heads every Codex floor of this checkout's registry is the table's head: a
        # fresh state agrees on every family, with no floor case and no disagreement
        floors = json.loads((REGISTRY_DIR / "registry.json").read_text())["families"]["codex"]
        self.assertEqual([fam for fam in ("astra", "sol", "terra", "luna") if floors[fam]["known_good"] != head(fam)], [])
        self.assertIn("codex heads: the table and the registry agree on every family", out)
        self.assertNotIn("no catalog evidence yet", out)
        self.assertNotIn("differs", out)
        # a fresh registry state holds no catalog evidence: for the families whose floor (the registry's human checkpoint) is a
        # predecessor the table retires, doctor says so instead of calling it a disagreement; the table decides the seat. Exercised
        # on the registry as it stood before C1's checkpoints, in a case of its own so its state starts fresh
        env, cd = self.case("f3-doctor-floor")
        older = self.older_registry(cd)
        env["MODEL_REGISTRY_DIR"] = str(older)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"registry: {older.resolve() / 'registry.json'}", out)
        self.assertIn(f"state {cd / 'mr-home'}", out)
        self.assertEqual(self.registry_reads(env), [])
        floors = json.loads((older / "registry.json").read_text())["families"]["codex"]
        behind = [(fam, floors[fam]["known_good"]) for fam in ("astra", "sol", "terra", "luna") if floors[fam]["known_good"] != head(fam)]
        self.assertEqual(sorted(fam for fam, _ in behind), ["luna", "sol"], "the older copy does not exercise the floor case")
        self.assertNotIn("the table and the registry agree on every family", out)
        for fam, floor in behind:
            self.assertIn(f"{fam} (registry floor {floor}; the table's {head(fam)} is its listed successor)", out)
        self.assertNotIn("differs", out)
        self.assertNotRegex(out, r"codex heads: \w+: table ")
        # a Codex seat's catalog read promotes the registry past its floor where a vendor edge reaches the table's head (the stub CLI
        # carries the CLI's bundled succession edges, the capture lists the successors): Luna. Changed by Kevin's Sol checkpoint
        # (2026-10-01): Sol's older floor is the head the checkpoint retired, which no vendor edge leaves, so on this copy the seat
        # still names the floor case and the next doctor too, still reading nothing itself
        rc, out, err = self.run_engine("ask", "--seats", "sol:low", "--job-dir", "f3-doctor-seat", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("registry floor %s, no catalog evidence yet (%s is its listed successor)" % (floors["sol"]["known_good"], head("sol")),
                      self.seat(self.jobs / "f3-doctor-seat", "sol")["expected_evidence"])
        reads = len(self.registry_reads(env))
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"sol (registry floor {floors['sol']['known_good']}; the table's {head('sol')} is its listed successor)", out)
        self.assertNotIn(f"luna (registry floor {floors['luna']['known_good']}", out)   # promoted by the evidence
        self.assertNotIn("differs", out)
        self.assertEqual(len(self.registry_reads(env)), reads, "doctor read a catalog")
        # the shipped registry over that same state: Kevin's checkpoint moves Sol to the table's head (design 3.2); doctor finds every
        # family agreeing, still reading nothing itself
        env["MODEL_REGISTRY_DIR"] = str(REGISTRY_DIR)
        rc, out, err = self.doctor(env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"registry: {REGISTRY_DIR.resolve() / 'registry.json'}", out)
        self.assertIn("codex heads: the table and the registry agree on every family", out)
        self.assertEqual(len(self.registry_reads(env)), reads, "doctor read a catalog")

        def newer_sol_head(t):
            old = t["families"]["codex"]["sol"]["known_good"]
            t["models"][NEWER_SOL] = dict(t["models"][old], generation=NEWER_SOL.split("-")[1], source="test: a fictional newer Sol")
            del t["models"][old]
            t["families"]["codex"]["sol"]["known_good"] = NEWER_SOL
            t["retired"]["codex"]["sol"].insert(0, old)
            t["fallback"]["codex"] = {}
        adopted = self.table_file("f3-doctor-adopted", newer_sol_head)
        rc, out, err = self.doctor(dict(env, BRIDGE_MODELS_FILE=str(adopted)))
        self.assertIn(f"codex heads: sol: table {NEWER_SOL}, registry {head('sol')}", out)
        self.assertIn("the table decides the seat", out)
        # a broken registry copy
        broken = cd / "broken-registry"
        (broken / "scripts").mkdir(parents=True)
        (broken / "scripts" / "model_registry.py").write_text("raise RuntimeError('fixture: a broken registry copy')\n")
        env2 = dict(env, MODEL_REGISTRY_DIR=str(broken))
        rc, out, err = self.doctor(env2)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("registry: none (the model registry module could not be imported: fixture: a broken registry copy)", out)
        self.assertIn("the bridge keeps its built-in table and routing", out)
        # checkpoint F3 round 1, P2: --strict-model promises a collected observation; without the collector it is refused up front
        launched = len(self.launched_models(env2))  # the case's record file already holds the sol seat's launch above
        rc, out, err = self.run_engine("ask", "--strict-model", "--seats", "luna:low", "--job-dir", "f3-broken-strict", "--", "q", env_extra=env2)
        line = self.assert_refused(rc, out, err)
        self.assertIn("--strict-model needs the model registry", line)
        self.assertIn("could not be imported", line)
        self.assertEqual(len(self.launched_models(env2)), launched, "a refused strict launch ran a model")
        self.assertFalse((self.jobs / "f3-broken-strict").exists(), "a refused strict launch created a job")
        renv, rd = self.relay("f3-doctor-broken", {"pick": {"codex": [{"exit": 0, "label": "a"}]}})
        env2.update(renv)
        reads = len(self.registry_reads(env2))  # the case's record file already holds the sol seat's reads above
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--job-dir", "f3-broken-seat", "--", "q", env_extra=env2)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("[bridge] model registry: the model registry module could not be imported", err)
        self.assertEqual(self.picks(rd), [["pick", "--vendor", "codex", "--json", "--model", "luna"]])  # the legacy pick: no set to confine it to
        self.assertEqual(len(self.registry_reads(env2)), reads, "without a registry the bridge read a catalog")
        seat = self.seat(self.jobs / "f3-broken-seat", "luna")
        self.assertEqual((seat["account"], seat["state"], seat["provenance"]["observed_via"], seat["provenance"]["registry_sha256"]),
                         ("a", "done", "none (no registry)", None))

    def test_registry_state_stays_under_the_cases_model_registry_home(self):
        # docs/model-routing.md 10.3: every piece of mutable state is redirected through MODEL_REGISTRY_HOME (and RELAY_HOME); the
        # snapshot of this machine's login is stored under the registry's own name for it, bound to the launch's CODEX_HOME
        env, cd = self.case("f3-state")
        rc, out, err = self.run_engine("ask", "--seats", "luna:low", "--", "q", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        state = json.loads((cd / "mr-home" / "state.json").read_text())
        rec = state["catalogs"]["codex"]["default"]
        self.assertEqual(rec["client"]["codex_home"], str(cd / "codex-home"))
        self.assertEqual(rec["client"]["config_mode"], "ignore-user-config")
        self.assertTrue(rec["data"], "the snapshot holds the catalog")
        self.assertFalse((self.tmp / "home" / ".claude" / "model-registry").exists(), "registry state escaped MODEL_REGISTRY_HOME")

    # ---------------------------------------------------------------- 5: the seat grammar
    def test_the_seat_grammar_reads_preference_lists_and_refuses_them_where_they_cannot_apply(self):
        # docs/model-routing.md 5: `<name>[|<name>...][:<effort>[|<effort>...]][@fast]`; the names after the first are the seat's
        # only descent, the efforts after the first its only effort descent; a Claude seat is never substituted, so a list on one is
        # refused; a name twice, an empty entry, an effort the model lacks are refused before any job
        b = load_engine_module(self.env)
        astra, sol = head("astra"), head("sol")
        seats = b.parse_seats("astra|sol:ultra|max,luna:max|xhigh,opus:max", "x")
        self.assertEqual([s.tag for s in seats], ["astra", "luna", "opus"])
        self.assertEqual((seats[0].model, seats[0].seat_name, seats[0].preference, seats[0].effort, seats[0].effort_preference),
                         (astra, "astra|sol", [(sol, "sol")], "ultra", ["max"]))
        self.assertEqual((seats[1].preference, seats[1].effort_preference), ([], ["xhigh"]))
        self.assertEqual((seats[2].preference, seats[2].effort_preference), ([], []))
        for spec in ("opus|sonnet:max", "astra|astra:low", "astra|:low", "astra:ultra|", "sonnet:max|high", "luna:max|ultra", "astra|opus:low"):
            with self.subTest(spec=spec):
                with self.assertRaises(SystemExit) as cm:
                    b.parse_seats(spec, "x")
                self.assertEqual(cm.exception.code, 2)
        env, _ = self.case("f3-grammar")
        rc, out, err = self.run_engine("ask", "--seats", "opus|sonnet:max", "--", "q", env_extra=env)
        line = self.assert_refused(rc, out, err)
        self.assertIn("a preference list (`|`) is a Codex option", line)
        self.assertEqual(self.launched_models(env), [])
        # a reply's --effort list is the reply's effort and its effort preference; refused for a Claude seat, nothing launched
        rc, out, err = self.run_engine("ask", "--seats", "opus:low", "--job-dir", "f3-grammar-claude", "--", "first", env_extra=env)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_engine("reply", "f3-grammar-claude", "--effort", "max|high", "--", "more", env_extra=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("an effort preference list (`|`) is a Codex option", out + err)
        self.assertEqual(len(self.claude_records(env)), 1)


if __name__ == "__main__":
    unittest.main()

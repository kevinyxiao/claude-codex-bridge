import itertools
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import merge_findings  # noqa: E402


def write(job, tag, findings, assessment="ok", model=None, effort=None):
    (job / f"findings-{tag}.json").write_text(json.dumps({
        "findings": findings, "assessment": assessment, "_reviewer": tag, "_model": model or tag, "_effort": effort or "max"}))


def finding(**changes):
    result = {"priority": "P2", "title": "Cache entries retain stale records", "file": "cache.py", "line": 20,
              "explanation": "Cache entries retain stale records.", "suggested_fix": "", "confidence": 0.8}
    result.update(changes)
    return result


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Path(self.tmp.name)
        (self.job / "meta.json").write_text(json.dumps({"scope": "uncommitted changes", "cwd": "/repo"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_header_names_the_model_that_finished_the_attempt_never_the_sorted_sets_last_element(self):
        # docs/model-routing.md 5 (checkpoint F3 round 1, P2): _observed_models is the registry's SORTED set, so its order says nothing
        # about which model finished the attempt; the header renders the recorded finishing model and lists the others it saw
        record = {"findings": [], "assessment": "ok", "_reviewer": "sol", "_model": "gpt-9-sol", "_effort": "low", "_ok": True,
                  "_model_requested": "sol", "_model_expected": "gpt-9-sol", "_observed_models": ["gpt-9-luna", "gpt-9-sol"],
                  "_observed_model_id": "gpt-9-luna",
                  "_substitution": {"observed": "gpt-9-luna", "expected": "gpt-9-sol", "class": "sideways", "authorized": False}}
        (self.job / "findings-sol.json").write_text(json.dumps(record))
        md, _ = merge_findings.merge(self.job)
        self.assertIn("(requested sol -> ran gpt-9-luna (also observed gpt-9-sol); SUBSTITUTED)", md)
        # a record without the finishing model (an older findings file) lists every model observed, singling out none
        record.pop("_observed_model_id")
        (self.job / "findings-sol.json").write_text(json.dumps(record))
        md, _ = merge_findings.merge(self.job)
        self.assertIn("(requested sol -> ran gpt-9-luna, gpt-9-sol; SUBSTITUTED)", md)
        # one model observed, no substitution
        record.update(_observed_models=["gpt-9-sol"], _observed_model_id="gpt-9-sol", _substitution=None)
        (self.job / "findings-sol.json").write_text(json.dumps(record))
        md, _ = merge_findings.merge(self.job)
        self.assertIn("(requested sol -> ran gpt-9-sol)", md)

    def test_explanation_only_port_reports_become_linked_rows(self):
        write(self.job, "astra", [{"priority": "P2", "title": "Reject port 65536", "file": "store.py", "line": 23,
                                   "explanation": "Port range allows 65536 above maximum 65535.", "suggested_fix": "use <= 65535", "confidence": 0.9}])
        write(self.job, "sol", [{"priority": "P1", "title": "Port validation accepts 65536", "file": "./store.py", "line": 24,
                                 "explanation": "The port range permits 65536 above maximum 65535.", "suggested_fix": "fix range", "confidence": 0.8}])
        write(self.job, "opus", [{"priority": "P2", "title": "Off-by-one in port upper bound (65536 accepted)", "file": "store.py", "line": 23,
                                  "explanation": "The port range allows 65536 above maximum 65535.", "suggested_fix": "…", "confidence": 0.95}])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 3)
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
        self.assertEqual(data["findings"][0]["priority"], "P1")
        self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]], [[2, 3], [1, 3], [1, 2]])
        self.assertNotIn("✔consensus", md)
        self.assertTrue((self.job / "merged.md").exists() and (self.job / "merged.json").exists())

    def test_different_bugs_stay_separate_and_sort_by_priority_then_consensus(self):
        write(self.job, "astra", [
            {"priority": "P3", "title": "Close the file deterministically", "file": "store.py", "line": 28,
             "explanation": "open() without with", "suggested_fix": "with open", "confidence": 0.7},
            {"priority": "P2", "title": "Cache lookup stores fallback", "file": "store.py", "line": 18,
             "explanation": "miss mutates cache", "suggested_fix": "cache.get", "confidence": 0.8}])
        write(self.job, "sol", [
            {"priority": "P2", "title": "cache_get poisons misses with the default", "file": "store.py", "line": 19,
             "explanation": "A lookup miss mutates the cache", "suggested_fix": "return cache.get(key, default)", "confidence": 0.85},
            {"priority": "P0", "title": "SQL injection in get_user", "file": "store.py", "line": 5,
             "explanation": "string formatting into SQL", "suggested_fix": "parameterize", "confidence": 0.99}])
        md, data = merge_findings.merge(self.job)
        titles = [f["title"] for f in data["findings"]]
        self.assertEqual(len(data["findings"]), 4)
        self.assertEqual(data["findings"][0]["priority"], "P0")
        # Cache explanations overlap, but the titles/fixes do not qualify.
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
        self.assertEqual(data["findings"][1]["possible_duplicate_of"], [3])
        self.assertEqual(data["findings"][2]["possible_duplicate_of"], [2])
        self.assertEqual(data["findings"][3]["priority"], "P3")
        self.assertIn("SQL injection", titles[0])

    def test_severity_words_and_invalid_reviewer_output_are_handled(self):
        write(self.job, "other", [{"severity": "critical", "title": "Crash on empty list", "file": "a.py", "line": 3,
                                   "explanation": "avg divides by zero", "suggested_fix": "guard", "confidence": 0.9}])
        (self.job / "findings-broken.json").write_text("not json")
        md, data = merge_findings.merge(self.job)
        self.assertEqual(data["findings"][0]["priority"], "P0")
        broken = [r for r in data["reviewers"] if r["tag"] == "broken"][0]
        self.assertTrue(broken["invalid"])
        self.assertIn("INVALID OUTPUT", md)

    def test_no_findings_renders_cleanly(self):
        write(self.job, "astra", [], assessment="No defects found; tests cover the change.")
        md, data = merge_findings.merge(self.job)
        self.assertEqual(data["findings"], [])
        self.assertIn("Findings: none", md)
        self.assertIn("No defects found", md)

    def test_cli_preserves_markdown_stdout_and_json_output_contract(self):
        write(self.job, "astra", [finding()])
        result = subprocess.run([sys.executable, merge_findings.__file__, str(self.job)],
                                text=True, capture_output=True, check=False,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        persisted_md = (self.job / "merged.md").read_text()
        persisted_json = json.loads((self.job / "merged.json").read_text())
        self.assertEqual(result.stdout, persisted_md + "\n")
        md, data = merge_findings.merge(self.job)
        self.assertEqual((persisted_md, persisted_json), (md, data))

    def write_payload(self, tag, payload):
        (self.job / f"findings-{tag}.json").write_text(json.dumps(payload))

    def write_inventory(self, paths):
        (self.job / "review.meta.json").write_text(json.dumps({"inventory": paths, "inventory_complete": True}))

    def test_numeric_string_lines_and_invalid_lines(self):
        valid = [finding(line="21", file="string.py"), finding(line=0, file="zero.py"),
                 finding(line=22.0, file="integral.py")]
        invalid = [finding(line=value) for value in
                   ("not a line", "2.5", 2.5, -1, "-2", None, True, [], {}, float("nan"), float("inf"))]
        write(self.job, "astra", valid + invalid)
        md, data = merge_findings.merge(self.job)
        self.assertEqual({f["file"]: f["line"] for f in data["findings"]},
                         {"string.py": 21, "zero.py": 0, "integral.py": 22})
        self.assertTrue(all(type(f["line"]) is int for f in data["findings"]))
        self.assertEqual(data["reviewers"][0]["dropped"], len(invalid))
        self.assertIn(f"{len(invalid)} dropped", md)

    def test_invalid_confidence_defaults_and_bounds(self):
        cases = [(None, 0.5), ("invalid", 0.5), ([], 0.5), ({}, 0.5), (True, 0.5),
                 (float("nan"), 0.5), ("0.75", 0.75), (-3, 0.0), (7, 1.0),
                 (float("inf"), 1.0), (float("-inf"), 0.0)]
        findings = [finding(file=f"case-{i}.py", confidence=value) for i, (value, _) in enumerate(cases)]
        missing = finding(file="missing.py")
        missing.pop("confidence")
        write(self.job, "astra", findings + [missing])
        _, data = merge_findings.merge(self.job)
        actual = {f["file"]: f["confidence"] for f in data["findings"]}
        self.assertEqual(actual, dict([(f"case-{i}.py", expected) for i, (_, expected) in enumerate(cases)] +
                                     [("missing.py", 0.5)]))
        json.dumps(data, allow_nan=False)

    def test_missing_required_keys_are_dropped_and_optional_keys_default(self):
        invalid = []
        for key in ("priority", "title", "file", "line"):
            item = finding()
            item.pop(key)
            invalid.append(item)
        invalid.extend([finding(title=" "), finding(title=123), finding(file=""), finding(file=[]),
                        finding(priority="urgent"), finding(priority={}), None, "finding", []])
        valid = {"priority": "P1", "title": "Reject stale cache", "file": "valid.py", "line": 2}
        severity = finding(file="severity.py", priority="unknown", severity=" HIGH ")
        write(self.job, "astra", invalid + [valid, severity])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        defaulted = next(f for f in data["findings"] if f["file"] == "valid.py")
        self.assertEqual((defaulted["explanation"], defaulted["suggested_fix"], defaulted["confidence"]), ("", "", 0.5))
        self.assertTrue(all(f["priority"] == "P1" for f in data["findings"]))
        self.assertEqual(data["reviewers"][0]["dropped"], len(invalid))
        self.assertIn(f"{len(invalid)} dropped", md)

    def test_top_level_list_has_empty_assessment(self):
        self.write_payload("list-seat", [finding()])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        reviewer = data["reviewers"][0]
        self.assertEqual(reviewer["tag"], "list-seat")
        self.assertEqual(reviewer["assessment"], "")
        self.assertFalse(reviewer["failed"])

    def test_non_string_assessment_is_stringified(self):
        for assessment in (None, 17, False, ["unfinished"], {"status": "partial"}):
            with self.subTest(assessment=assessment):
                write(self.job, "astra", [], assessment=assessment)
                _, data = merge_findings.merge(self.job)
                self.assertEqual(data["reviewers"][0]["assessment"], str(assessment))

    def test_failed_parseable_findings_are_partial_and_excluded_from_consensus(self):
        write(self.job, "success", [finding(confidence=0.7)])
        self.write_payload("failure", {"findings": [finding(confidence=0.95)], "_ok": False,
                                       "_error": "seat timed out", "_secs": 12.5})
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 1)
        self.assertEqual(data["findings"][0]["reviewers"], ["failure", "success"])
        self.assertEqual(len(data["findings"][0]["members"]), 2)
        self.assertIn("failure (partial: seat failed)", md)
        self.assertIn("FAILED", md)
        self.assertIn("seat timed out", md)
        self.assertIn("12.5s", md)
        self.assertNotIn("✔consensus", md)

    def test_failed_empty_and_invalid_seats_do_not_read_as_clean_reviews(self):
        self.write_payload("failure", {"findings": [], "_ok": False, "_error": "cancelled", "_secs": 0})
        self.write_payload("invalid", {"findings": [], "_invalid": True, "_secs": 2})
        md, data = merge_findings.merge(self.job)
        self.assertEqual(data["findings"], [])
        self.assertTrue(all(r["failed"] for r in data["reviewers"]))
        self.assertNotIn("Findings: none", md)
        self.assertIn("FAILED", md)
        self.assertIn("INVALID OUTPUT", md)
        self.assertIn("cancelled", md)
        self.assertIn("0 dropped, 0s", md)
        self.assertIn("0 dropped, 2s", md)

    def test_malformed_seconds_do_not_leak_invalid_metadata_into_report(self):
        for seconds in ([1, [2]], {"elapsed": 3}, True, False, float("nan"), float("inf"), float("-inf")):
            with self.subTest(seconds=seconds):
                self.write_payload("astra", {"findings": [], "_secs": seconds})
                _, data = merge_findings.merge(self.job)
                self.assertIsNone(data["reviewers"][0]["secs"])
                json.dumps(data, allow_nan=False)

    def test_invalid_seat_with_findings_is_partial_even_when_ok_is_true(self):
        self.write_payload("invalid", {"findings": [finding()], "_ok": True, "_invalid": True})
        md, data = merge_findings.merge(self.job)
        self.assertEqual(data["findings"][0]["consensus"], 0)
        self.assertIn("(partial: seat failed)", md)
        self.assertIn("INVALID OUTPUT", md)
        self.assertIn("FAILED", md)

    def test_malformed_entries_mark_partial_and_count_only_successful_seats(self):
        self.write_payload("clean", {"_ok": True, "findings": [finding()]})
        for ok, expected_consensus in ((True, 2), (False, 1), (1, 1), ("true", 1), (None, 1)):
            with self.subTest(ok=ok):
                self.write_payload("mixed", {"_ok": ok, "findings": [finding(), "bad", finding(title=123)]})
                md, data = merge_findings.merge(self.job)
                mixed = next(r for r in data["reviewers"] if r["tag"] == "mixed")
                self.assertEqual(mixed["malformed"], 2)
                self.assertEqual(mixed["dropped"], 2)
                self.assertEqual(mixed["ok"], ok is True)
                self.assertIn("PARTIAL (2 malformed)", md)
                self.assertEqual(len(data["findings"]), 1)
                self.assertEqual(data["findings"][0]["consensus"], expected_consensus)
                self.assertEqual(len(data["findings"][0]["members"]), 2)
                self.assertEqual(data, json.loads((self.job / "merged.json").read_text()))

    def test_all_malformed_entries_do_not_read_as_a_clean_review(self):
        self.write_payload("mixed", {"_ok": True, "findings": [None, "bad", {}]})
        md, data = merge_findings.merge(self.job)
        self.assertEqual(data["findings"], [])
        self.assertEqual(data["reviewers"][0]["malformed"], 3)
        self.assertIn("PARTIAL (3 malformed)", md)
        self.assertNotIn("Findings: none", md)

    def test_duplicate_findings_and_seat_tags_count_successful_reviewer_once(self):
        for tag, ok in (("one", True), ("two", True), ("failed", False)):
            self.write_payload(tag, {"_reviewer": "same-reviewer", "_ok": ok,
                                     "findings": [finding(), finding()]})
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 1)
        self.assertEqual(len(data["findings"][0]["members"]), 6)
        self.assertNotIn("✔consensus", md)

    def test_existing_root_file_and_nested_same_basename_do_not_merge(self):
        repo = self.job / "repo"
        (repo / "worker").mkdir(parents=True)
        (repo / "auth.py").write_text("")
        (repo / "worker" / "auth.py").write_text("")
        (self.job / "meta.json").write_text(json.dumps({"cwd": str(repo)}))
        write(self.job, "root", [finding(file="auth.py")])
        write(self.job, "nested", [finding(file="worker/auth.py")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))

    def test_explicit_root_paths_do_not_alias_nested_files(self):
        self.write_inventory(["worker/auth.py"])
        for root_path in ("./auth.py", "a/auth.py", "b/auth.py", ".\\auth.py"):
            with self.subTest(path=root_path):
                write(self.job, "root", [finding(file=root_path)])
                write(self.job, "nested", [finding(file="worker/auth.py")])
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)

    def test_explicit_root_evidence_survives_merge_and_matches_root_spelling(self):
        self.write_inventory(["worker/auth.py"])
        write(self.job, "explicit", [finding(file="./auth.py")])
        write(self.job, "root", [finding(file="auth.py")])
        write(self.job, "nested", [finding(file="worker/auth.py")])
        md, data = merge_findings.merge(self.job)
        self.assertEqual({tuple(f["reviewers"]) for f in data["findings"]},
                         {("explicit", "root"), ("nested",)})
        explicit = next(member for f in data["findings"] for member in f["members"]
                        if member["reviewer"] == "explicit")
        self.assertEqual(explicit["file"], "./auth.py")
        self.assertIn("`./auth.py:20`", md)
        self.assertFalse(merge_findings.same_issue(explicit, finding(file="worker/auth.py")))

    def test_unique_bare_basename_merges_with_one_distinct_qualified_path(self):
        self.write_inventory(["client/cache.py"])
        write(self.job, "bare", [finding(file="cache.py")])
        write(self.job, "qualified", [finding(file="client/cache.py")])
        write(self.job, "repeated", [finding(file="client/cache.py", line=21)])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 3)

    def test_ambiguous_bare_basename_and_distinct_qualified_paths_stay_separate(self):
        for tag, path in (("bare", "cache.py"), ("client", "client/cache.py"), ("server", "server/cache.py")):
            write(self.job, tag, [finding(file=path)])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 3)
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))

    def test_path_prefixes_slashes_and_separator_normalization(self):
        for tag, path in enumerate(("./client/cache.py", "a/client//cache.py", "b/client/cache.py",
                                    "client\\cache.py", ".\\a\\client\\\\cache.py")):
            write(self.job, str(tag), [finding(file=path)])
        _, data = merge_findings.merge(self.job)
        self.assertEqual({f["file"]: f["consensus"] for f in data["findings"]},
                         {"client/cache.py": 2, "a/client/cache.py": 2, "b/client/cache.py": 1})

    def test_real_a_and_b_directories_never_collapse_into_root_paths(self):
        paths = ["cache.py", "a/cache.py", "b/cache.py", "a/b/cache.py"]
        self.write_inventory(paths)
        for index, path in enumerate(paths):
            write(self.job, str(index), [finding(file=path)])
        _, data = merge_findings.merge(self.job)
        self.assertEqual({f["file"] for f in data["findings"]}, set(paths))
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
        self.assertEqual({member["file"] for f in data["findings"] for member in f["members"]}, set(paths))

    def test_unreported_snapshot_basename_competitor_prevents_alias(self):
        self.write_inventory(["client/auth.py", "server/auth.py"])
        write(self.job, "bare", [finding(file="auth.py")])
        write(self.job, "client", [finding(file="client/auth.py")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))

    def test_complete_inventory_includes_unchanged_competitors_beyond_changed_files(self):
        (self.job / "review.meta.json").write_text(json.dumps({
            "files": ["client/auth.py"], "inventory": ["client/auth.py", "server/auth.py"],
            "inventory_complete": True}))
        write(self.job, "bare", [finding(file="auth.py")])
        write(self.job, "client", [finding(file="client/auth.py")])
        md, data = merge_findings.merge(self.job)
        self.assertEqual({f["file"] for f in data["findings"]}, {"auth.py", "client/auth.py"})
        self.assertTrue(all(f["consensus"] == 1 and not f["possible_duplicate_of"] for f in data["findings"]))
        self.assertNotIn("✔consensus", md)

    def test_aliases_require_a_valid_inventory_and_explicit_completeness(self):
        write(self.job, "bare", [finding(file="auth.py")])
        write(self.job, "client", [finding(file="client/auth.py")])
        write(self.job, "exact", [finding(file="client/auth.py")])
        cases = [
            {"files": ["client/auth.py"]},
            {"files": ["client/auth.py"], "inventory_complete": True},
            {"inventory": ["client/auth.py"]},
        ]
        cases.extend({"inventory": ["client/auth.py"], "inventory_complete": complete}
                     for complete in (False, None, 0, 1, "true"))
        cases.extend({"inventory": inventory, "inventory_complete": True}
                     for inventory in (None, "client/auth.py", {}, ["client/auth.py", 17],
                                       ["client/auth.py", ""], ["client/auth.py", "./"]))
        for metadata in cases:
            with self.subTest(metadata=metadata):
                (self.job / "review.meta.json").write_text(json.dumps(metadata))
                _, data = merge_findings.merge(self.job)
                self.assertEqual({f["file"]: f["consensus"] for f in data["findings"]},
                                 {"auth.py": 1, "client/auth.py": 2})

    def test_deleted_root_in_snapshot_prevents_basename_alias(self):
        self.write_inventory(["auth.py", "worker/auth.py"])
        (self.job / "meta.json").write_text(json.dumps({"cwd": str(self.job)}))
        self.assertFalse((self.job / "auth.py").exists())
        write(self.job, "root", [finding(file="auth.py")])
        write(self.job, "worker", [finding(file="worker/auth.py")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)

    def test_snapshot_inventory_is_authoritative_over_current_worktree(self):
        (self.job / "auth.py").write_text("created after snapshot")
        (self.job / "meta.json").write_text(json.dumps({"cwd": str(self.job)}))
        self.write_inventory(["./worker\\auth.py"])
        write(self.job, "bare", [finding(file="auth.py")])
        write(self.job, "worker", [finding(file="worker/auth.py")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 2)

    def test_snapshot_alias_requires_a_listed_qualified_file(self):
        write(self.job, "bare", [finding(file="auth.py")])
        write(self.job, "server", [finding(file="server/auth.py")])
        for paths in ([], ["client/auth.py"]):
            with self.subTest(paths=paths):
                self.write_inventory(paths)
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)
                self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))

    def test_fallback_reported_inventory_keeps_listed_bare_file_separate(self):
        write(self.job, "bare", [finding(file="cache.py")])
        write(self.job, "qualified", [finding(file="client/cache.py")])
        for metadata in (None, "not json", "[]", '{"files": null}', '{"files": [17]}'):
            with self.subTest(metadata=metadata):
                if metadata is not None:
                    (self.job / "review.meta.json").write_text(metadata)
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)

    def test_different_defects_on_same_line_remain_separate(self):
        cases = [
            ("Disable TLS verification", "The upstream request accepts an untrusted certificate.",
             "Missing request timeout", "The upstream request can hang forever."),
            ("Close the upload file", "The upload file leaks its handle after the call.",
             "Reject oversized payloads", "The upload file has no maximum payload size check."),
        ]
        for left_title, left_explanation, right_title, right_explanation in cases:
            with self.subTest(left=left_title, right=right_title):
                write(self.job, "left", [finding(title=left_title, explanation=left_explanation)])
                write(self.job, "right", [finding(title=right_title, explanation=right_explanation)])
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)

    def test_adjacent_defect_with_inflected_words_merges(self):
        write(self.job, "left", [finding(title="Cached entries poison retry state", line=20,
                                        explanation="Cached entries poison retry state.")])
        write(self.job, "right", [finding(title="Caching entry poisons retries state", line=21,
                                         explanation="Caching entry poisons retries state.")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 2)

    def test_token_normalization_numbers_and_generic_stop_words(self):
        for expected, variants in (("cache", "cache cached caching"), ("entry", "entry entries"),
                                   ("retry", "retry retries"), ("port", "port ports"),
                                   ("accept", "accept accepts accepted"), ("permit", "permit permits"),
                                   ("allow", "allow allows")):
            with self.subTest(variants=variants):
                self.assertEqual(merge_findings.tokens(variants), {expected})
        self.assertEqual(merge_findings.tokens("0 2 65536"), {"0", "2", "65536"})
        self.assertEqual(merge_findings.tokens("read file value return function call check upload"), set())

    def test_vocabulary_evidence_is_required_at_each_distance(self):
        cases = [
            (0, "cache", "cache", True),
            (0, "cache stale alpha beta gamma delta", "cache stale epsilon zeta theta lambda", False),
            (1, "cache stale alpha beta gamma delta", "cache stale epsilon zeta theta lambda", False),
            (3, "cache stale alpha beta gamma delta", "cache stale epsilon zeta theta lambda", False),
            (3, "cache stale alpha", "cache stale beta", False),
        ]
        for distance, left, right, merged in cases:
            with self.subTest(distance=distance, left=left, right=right):
                write(self.job, "left", [finding(title=left, line=20, explanation=left)])
                write(self.job, "right", [finding(title=right, line=20 + distance, explanation=right)])
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 1 if merged else 2)

    def test_identical_titles_with_missing_fixes_do_not_require_explanation_overlap(self):
        for left, right in (("", ""), ("alpha beta", "alpha gamma"),
                            ("file check upload cache", "file check upload cache")):
            for distance in (0, 1, merge_findings.LINE_TOLERANCE):
                with self.subTest(explanations=(left, right), distance=distance):
                    write(self.job, "left", [finding(explanation=left)])
                    write(self.job, "right", [finding(explanation=right, line=20 + distance)])
                    _, data = merge_findings.merge(self.job)
                    self.assertEqual(len(data["findings"]), 1)
                    self.assertEqual(data["findings"][0]["consensus"], 2)

    def test_former_combined_jaccard_matches_are_only_possible_duplicates(self):
        cases = [
            (0, "cache stale alpha beta gamma delta", "cache stale alpha epsilon zeta theta iota", True),
            (1, "cache stale alpha beta gamma delta", "cache stale alpha epsilon zeta theta iota", False),
            (1, "cache stale alpha", "cache stale beta gamma", True),
            (merge_findings.LINE_TOLERANCE, "cache stale alpha", "cache stale beta gamma", True),
            (merge_findings.LINE_TOLERANCE + 1, "cache stale alpha", "cache stale alpha", False),
        ]
        for distance, left, right, linked in cases:
            with self.subTest(distance=distance, left=left, right=right):
                write(self.job, "left", [finding(title=left, explanation=left)])
                write(self.job, "right", [finding(title=right, explanation=right, line=20 + distance)])
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)
                self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]],
                                 [[2], [1]] if linked else [[], []])

    def test_expired_and_unsigned_bearer_tokens_are_possible_duplicates_only(self):
        write(self.job, "expiry", [finding(title="Reject expired bearer tokens", file="api/auth.py",
                                           explanation="Expiration validation is disabled, allowing stale credentials.")])
        write(self.job, "signature", [finding(title="Reject unsigned bearer tokens", file="api/auth.py",
                                              explanation="Signature verification is bypassed, accepting forged identities.")])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
        self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]], [[2], [1]])
        table = md.split("## Details", 1)[0]
        self.assertIn("possibly the same as #2", table)
        self.assertIn("possibly the same as #1", table)
        self.assertNotIn("✔consensus", md)

    def test_shared_bearer_validation_vocabulary_never_collapses_distinct_fixes(self):
        for distance in (0, 1, merge_findings.LINE_TOLERANCE, merge_findings.LINE_TOLERANCE + 1):
            with self.subTest(distance=distance):
                write(self.job, "expiry", [finding(
                    title="Reject expired bearer tokens", file="api/auth.py",
                    explanation="Bearer token expiration validation is disabled, allowing stale credentials to authenticate.",
                    suggested_fix="check the exp claim")])
                write(self.job, "signature", [finding(
                    title="Reject unsigned bearer tokens", file="api/auth.py", line=20 + distance,
                    explanation="Bearer token signature validation is disabled, allowing forged credentials to authenticate.",
                    suggested_fix="verify the signature")])
                md, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)
                self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
                linked = distance <= merge_findings.LINE_TOLERANCE
                self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]],
                                 [[2], [1]] if linked else [[], []])
                self.assertEqual("possibly the same as #1" in md, linked)
                self.assertEqual("possibly the same as #2" in md, linked)
                self.assertIn("Fix: check the exp claim", md)
                self.assertIn("Fix: verify the signature", md)
                self.assertNotIn("✔consensus", md)

    def test_parallel_bearer_validation_wording_stays_separate_and_linked(self):
        for distance in (0, 3):
            with self.subTest(distance=distance):
                write(self.job, "expiry", [finding(
                    title="Validate bearer token expiration before authentication", file="api/auth.py",
                    explanation="Bearer token expiration validation is disabled, allowing stale credentials to authenticate.",
                    suggested_fix="Reject tokens whose exp claim is in the past")])
                write(self.job, "signature", [finding(
                    title="Validate bearer token signature before authentication", file="api/auth.py", line=20 + distance,
                    explanation="Bearer token signature validation is disabled, allowing forged credentials to authenticate.",
                    suggested_fix="Reject tokens whose signature fails verification")])
                md, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)
                self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
                self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]], [[2], [1]])
                self.assertIn("possibly the same as #1", md)
                self.assertIn("possibly the same as #2", md)
                self.assertIn("Fix: Reject tokens whose exp claim is in the past", md)
                self.assertIn("Fix: Reject tokens whose signature fails verification", md)
                self.assertNotIn("✔consensus", md)

    def test_same_expiration_defect_with_paraphrased_fixes_shows_both(self):
        left = finding(title="Validate bearer token expiration before authentication", file="api/auth.py",
                       explanation="Expired bearer tokens authenticate because their exp claim is never checked.",
                       suggested_fix="Reject tokens whose exp claim is in the past")
        right = finding(title="Check bearer token expiration before authentication", file="api/auth.py", line=21,
                        explanation="Authentication accepts a bearer token after its expiration timestamp.",
                        suggested_fix="Reject tokens whose exp claim predates the current time")
        write(self.job, "astra", [left])
        write(self.job, "sol", [right])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 2)
        self.assertIn("✔consensus", md)
        details = md.split("## Details", 1)[1].split("## Each reviewer's assessment", 1)[0]
        members = {member["reviewer"]: member for member in data["findings"][0]["members"]}
        for reviewer, original in (("astra", left), ("sol", right)):
            self.assertIn(f"Explanation ({reviewer}): " + original["explanation"], details)
            self.assertIn(f"Fix ({reviewer}): " + original["suggested_fix"], details)
            for field in ("explanation", "suggested_fix"):
                self.assertEqual(details.count(original[field]), 1)
                self.assertEqual(members[reviewer][field], original[field])

    def test_title_jaccard_with_fixes_includes_half_boundary(self):
        for distance in (0, 1, merge_findings.LINE_TOLERANCE, merge_findings.LINE_TOLERANCE + 1):
            for right, qualifies in (("Cache stale beta", True), ("Cache stale beta gamma", False)):
                with self.subTest(distance=distance, title=right):
                    write(self.job, "left", [finding(title="Cache stale alpha", suggested_fix="Refresh entries")])
                    write(self.job, "right", [finding(title=right, line=20 + distance, suggested_fix="Refresh entry")])
                    _, data = merge_findings.merge(self.job)
                    merged = qualifies and distance <= merge_findings.LINE_TOLERANCE
                    self.assertEqual(len(data["findings"]), 1 if merged else 2)

    def test_fix_jaccard_includes_half_boundary(self):
        # Ten shared tokens out of twenty qualify; scores below .50 do not.
        for shared, merged in ((10, True), (9, False), (7, False)):
            with self.subTest(shared=shared):
                left_fix = " ".join("term%d" % i for i in range(13))
                right_fix = " ".join("term%d" % i for i in list(range(shared)) + list(range(13, 20)))
                write(self.job, "left", [finding(suggested_fix=left_fix)])
                write(self.job, "right", [finding(suggested_fix=right_fix)])
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 1 if merged else 2)

    def test_missing_fix_requires_point_seven_title_overlap_on_either_side(self):
        shared = "cache stale alpha beta gamma delta epsilon"
        # Seven shared tokens out of ten; the extra token falls below .7.
        for left_fix, right_fix in (("", ""), (None, "Refresh records"), ("Refresh records", " \t")):
            for extra, merged in (("", True), (" extra", False)):
                with self.subTest(fixes=(left_fix, right_fix), extra=extra):
                    write(self.job, "left", [finding(title=shared + " zeta", suggested_fix=left_fix)])
                    write(self.job, "right", [finding(title=shared + " theta iota" + extra, suggested_fix=right_fix)])
                    _, data = merge_findings.merge(self.job)
                    self.assertEqual(len(data["findings"]), 1 if merged else 2)

    def test_empty_vocabulary_does_not_establish_consensus(self):
        cases = [
            (finding(title="Use the file"), finding(title="Use the file")),
            (finding(title="Use the file", suggested_fix="Refresh entries"),
             finding(title="Use the file", suggested_fix="Refresh entries")),
            (finding(suggested_fix="…"), finding(suggested_fix="…")),
            (finding(suggested_fix="use the file"), finding(suggested_fix="check the file")),
            (finding(suggested_fix="Refresh entries"), finding(suggested_fix="…")),
        ]
        for left, right in cases:
            with self.subTest(left=left, right=right):
                write(self.job, "left", [left])
                write(self.job, "right", [right])
                _, data = merge_findings.merge(self.job)
                self.assertEqual(len(data["findings"]), 2)

    def test_matching_titles_cannot_override_disjoint_fix_vocabulary(self):
        write(self.job, "expiry", [finding(title="Validate bearer tokens", suggested_fix="check the exp claim")])
        write(self.job, "signature", [finding(title="Validate bearer tokens", suggested_fix="verify the signature")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]], [[2], [1]])

    def test_cleanup_paraphrases_with_zero_fix_overlap_remain_linked(self):
        # A3 regression (b) conflicts with the mandatory fix Jaccard threshold:
        # these equivalent cleanup idioms share no tokens. Honor the threshold
        # until the specification authorizes semantic normalization or an exception.
        write(self.job, "context", [finding(title="Close the file deterministically",
                                            suggested_fix="use a context manager")])
        write(self.job, "finally", [finding(title="Close the file deterministically",
                                            suggested_fix="close the file in finally")])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertTrue(all(f["consensus"] == 1 for f in data["findings"]))
        self.assertEqual([f["possible_duplicate_of"] for f in data["findings"]], [[2], [1]])
        self.assertIn("Fix: use a context manager", md)
        self.assertIn("Fix: close the file in finally", md)
        self.assertNotIn("✔consensus", md)

    def test_three_qualifying_reviewers_retain_priority_and_consensus(self):
        for tag, title, priority, path, confidence in (
                ("astra", "Reject stale cache records", "P2", "cache.py", 0.9),
                ("sol", "Reject stale cached records", "P1", "./cache.py", 0.8),
                ("opus", "Reject stale caching records", "P2", "cache.py", 0.95)):
            write(self.job, tag, [finding(title=title, priority=priority, file=path, confidence=confidence,
                                          suggested_fix="Refresh stale entries")])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        result = data["findings"][0]
        self.assertEqual((result["priority"], result["consensus"]), ("P1", 3))
        self.assertEqual(result["reviewers"], ["astra", "opus", "sol"])
        self.assertEqual(len(result["members"]), 3)
        self.assertIn("✔consensus", md)

    def test_possible_duplicate_numbers_follow_final_priority_order(self):
        write(self.job, "seat", [finding(title="Cache alpha", explanation="alpha", priority="P3"),
                                  finding(title="Cache beta", explanation="beta", priority="P1"),
                                  finding(title="Cache gamma", explanation="gamma", priority="P2"),
                                  finding(title="Unrelated path", file="other.py", priority="P0"),
                                  finding(title="Adjacent location", line=21, explanation="delta", priority="P2")])
        _, data = merge_findings.merge(self.job)
        by_title = {f["title"]: (index, f) for index, f in enumerate(data["findings"], 1)}
        duplicate_titles = {"Cache alpha", "Cache beta", "Cache gamma"}
        for title in duplicate_titles:
            _, current = by_title[title]
            expected = sorted(by_title[other][0] for other in duplicate_titles - {title})
            self.assertEqual(current["possible_duplicate_of"], expected)
        self.assertFalse(by_title["Unrelated path"][1].get("possible_duplicate_of"))
        self.assertFalse(by_title["Adjacent location"][1].get("possible_duplicate_of"))

    def test_highest_similarity_cluster_wins_over_first_matching_cluster(self):
        write(self.job, "a", [finding(title="Cache timestamp overflow", line=10,
                                     explanation="Cache timestamp overflow")])
        write(self.job, "b", [finding(title="Retry counter overflow", line=10,
                                     explanation="Retry counter overflow")])
        write(self.job, "c", [finding(title="Retry counter cache overflow", line=11,
                                     explanation="Retry counter cache overflow")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual({tuple(f["reviewers"]) for f in data["findings"]}, {("a",), ("b", "c")})

    def test_reassigning_reviewers_preserves_three_report_cluster_membership(self):
        reports = [finding(title=title, explanation=title) for title in
                   ("Cache expiration timestamp overflow", "Retry counter cache overflow", "Retry counter overflow")]
        baseline = None
        for labels in itertools.permutations(("a", "b", "c")):
            for tag, report in zip(labels, reports):
                write(self.job, tag, [report])
            _, data = merge_findings.merge(self.job)
            groups = sorted(tuple(sorted(member["title"] for member in f["members"])) for f in data["findings"])
            if baseline is None:
                baseline = groups
            self.assertEqual(groups, baseline, labels)
            for current in data["findings"]:
                for left, right in itertools.combinations(current["members"], 2):
                    self.assertTrue(merge_findings.same_issue(left, right))
        self.assertEqual(len(baseline), 2)

    def test_equal_similarity_chooses_earliest_content_order_cluster(self):
        # Each candidate title scores .5 against m, but only .2 against the other.
        write(self.job, "z", [finding(title="Cache alpha overflow", explanation="Cache alpha overflow", line=10,
                                      suggested_fix="Prevent overflow")])
        write(self.job, "a", [finding(title="Retry beta overflow", explanation="Retry beta overflow", line=10,
                                      suggested_fix="Prevent overflow")])
        write(self.job, "m", [finding(title="Cache retry overflow", explanation="Cache retry overflow", line=11,
                                      suggested_fix="Prevent overflow")])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual({tuple(f["reviewers"]) for f in data["findings"]}, {("m", "z"), ("a",)})

    def test_file_and_finding_permutations_produce_identical_merged_findings(self):
        seats = {
            "astra": [finding(), finding(title="Reject port 65536", file="network.py", line=50,
                                         explanation="Port range allows 65536 above maximum 65535.")],
            "sol": [finding(title="Cached entry retains stale records", line=21),
                    finding(title="Port validation accepts 65536", file="network.py", line=51,
                            explanation="Port validation allows 65536 above maximum 65535.")],
            "opus": [finding(title="Caching entries retain stale records", line=22, confidence=0.95),
                     finding(title="SQL injection exposes account", file="db.py", line=5, priority="P0")],
        }
        baseline = None
        for order in itertools.permutations(seats):
            for reversals in itertools.product((False, True), repeat=3):
                for index, (reviewer, reverse) in enumerate(zip(order, reversals)):
                    values = list(reversed(seats[reviewer])) if reverse else seats[reviewer]
                    self.write_payload(str(index), {"_reviewer": reviewer, "findings": values})
                _, data = merge_findings.merge(self.job)
                persisted = json.loads((self.job / "merged.json").read_text())["findings"]
                self.assertEqual(data["findings"], persisted)
                if baseline is None:
                    baseline = persisted
                self.assertEqual(persisted, baseline, (order, reversals))
        self.assertEqual(len(baseline), 4)  # Port titles no longer establish consensus.

    def test_identical_sort_keys_have_deterministic_members_and_representative(self):
        members = [finding(explanation="Cache entry alpha", suggested_fix="Refresh stale records alpha"),
                   finding(explanation="Cache entry beta", suggested_fix="Refresh stale records beta"),
                   finding(explanation="Cache entry gamma", suggested_fix="Refresh stale records gamma", confidence=0.95)]
        baseline = None
        for order in itertools.permutations(members):
            write(self.job, "same", list(order))
            _, data = merge_findings.merge(self.job)
            if baseline is None:
                baseline = data["findings"]
            self.assertEqual(data["findings"], baseline)
        self.assertEqual(len(baseline), 1)
        self.assertEqual(baseline[0]["explanation"], "Cache entry gamma")
        self.assertEqual(baseline[0]["suggested_fix"], "Refresh stale records gamma")
        self.assertEqual(len(baseline[0]["members"]), 3)
        tied = None
        for order in itertools.permutations(members[:2]):
            write(self.job, "same", list(order))
            _, data = merge_findings.merge(self.job)
            if tied is None:
                tied = data["findings"]
            self.assertEqual(data["findings"], tied)

    def test_cluster_line_span_is_bounded_including_line_zero(self):
        for index, line in enumerate((0, merge_findings.LINE_TOLERANCE, 2 * merge_findings.LINE_TOLERANCE)):
            write(self.job, str(index), [finding(line=line)])
        _, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual(sorted(len(f["members"]) for f in data["findings"]), [1, 2])
        for cluster in data["findings"]:
            lines = [member["line"] for member in cluster["members"]]
            self.assertLessEqual(max(lines) - min(lines), merge_findings.LINE_TOLERANCE)

    def test_markdown_escapes_titles_paths_and_details_without_losing_json_text(self):
        title = "Cache | `entry`\n# injected\x00 title"
        path = "src/a|`b`\n\x01.py"
        explanation = "Details | unsafe\r\n# heading\x7f"
        fix = "Fix | `value`\nnext\tstep"
        write(self.job, "astra", [finding(title=title, file=path, explanation=explanation, suggested_fix=fix)])
        md, data = merge_findings.merge(self.job)
        table, details = md.split("## Details", 1)
        self.assertIn("Cache \\| `entry`", table)
        self.assertIn("src/a\\|`b`", table)
        self.assertIn("Cache | `entry`", details)
        self.assertIn("src/a|`b`", details)
        self.assertIn("Details | unsafe", details)
        self.assertIn("Fix |", details)
        for control in ("\x00", "\x01", "\x7f", "\r", "\t"):
            self.assertNotIn(control, md)
        self.assertNotIn("\n# injected", md)
        self.assertNotIn("\n# heading", md)
        table_rows = [line for line in md.splitlines() if line.startswith("|")]
        self.assertEqual(len(table_rows), 3)
        self.assertTrue(all(len(re.findall(r"(?<!\\)\|", line)) == 7 for line in table_rows))
        self.assertIn("``Cache", table_rows[2])
        self.assertIn("``src/a", table_rows[2])
        member = data["findings"][0]["members"][0]
        self.assertEqual(member["title"], title)
        self.assertEqual(member["file"], path)
        self.assertEqual(member["explanation"], explanation)
        self.assertEqual(member["suggested_fix"], fix)

    def test_empty_representative_explanation_uses_member_fallback_after_title_match(self):
        write(self.job, "highest", [finding(confidence=0.99, explanation="", suggested_fix="Best fix")])
        write(self.job, "middle", [finding(confidence=0.85, explanation="Refresh cache entries after expiry.")])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        result = data["findings"][0]
        self.assertEqual(result["reviewers"], ["highest", "middle"])
        self.assertEqual(result["explanation"], "Refresh cache entries after expiry.")
        self.assertEqual(result["suggested_fix"], "Best fix")
        self.assertEqual(result["consensus"], 2)
        self.assertIn("Refresh cache entries after expiry.", md)

    def test_markdown_code_path_preserves_literal_underscores_and_ampersands(self):
        path = "src/cache_state&ports.py"
        write(self.job, "astra", [finding(file=path)])
        md, _ = merge_findings.merge(self.job)
        self.assertEqual(md.count("`" + path + ":20`"), 2)

    def test_literal_pipes_are_escaped_only_in_table_code_spans(self):
        write(self.job, "astra", [finding(file="src/a|b.py", title="Cache | stale records",
                                          explanation="left | right", suggested_fix="old | new")])
        md, _ = merge_findings.merge(self.job)
        table, details = md.split("## Details", 1)
        self.assertIn("`src/a\\|b.py:20`", table)
        self.assertIn("`Cache \\| stale records`", table)
        self.assertIn("`src/a|b.py:20`", details)
        self.assertIn("`Cache | stale records`", details)
        self.assertIn("left | right", details)
        self.assertIn("old | new", details)
        self.assertNotIn("\\|", details)

    def test_paths_and_titles_use_delimiters_longer_than_backtick_runs(self):
        write(self.job, "astra", [finding(file="src/a``b`c.py", title="Cache `entry` uses ``fallback``")])
        md, data = merge_findings.merge(self.job)
        table, details = md.split("## Details", 1)
        for section in (table, details):
            self.assertIn("```src/a``b`c.py:20```", section)
            self.assertIn("``` Cache `entry` uses ``fallback`` ```", section)
        self.assertEqual(data["findings"][0]["file"], "src/a``b`c.py")
        self.assertEqual(data["findings"][0]["title"], "Cache `entry` uses ``fallback``")

    def test_private_output_modes_on_initial_write_and_remerge(self):
        write(self.job, "astra", [finding()])
        previous_umask = os.umask(0)
        try:
            for rerun in (False, True):
                if rerun:
                    for name in ("merged.md", "merged.json"):
                        (self.job / name).chmod(0o666)
                md, data = merge_findings.merge(self.job)
                for name in ("merged.md", "merged.json"):
                    self.assertEqual(stat.S_IMODE((self.job / name).stat().st_mode), 0o600)
                self.assertEqual((self.job / "merged.md").read_text(), md)
                self.assertEqual(json.loads((self.job / "merged.json").read_text()), data)
        finally:
            os.umask(previous_umask)

    def test_atomic_output_replaces_symlinks_without_modifying_targets(self):
        write(self.job, "astra", [finding()])
        target = self.job / "unrelated.txt"
        target.write_text("leave unchanged")
        target.chmod(0o644)
        for name in ("merged.md", "merged.json"):
            (self.job / name).symlink_to(target)
        before = {path.name for path in self.job.iterdir()}
        merge_findings.merge(self.job)
        self.assertEqual(target.read_text(), "leave unchanged")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        for name in ("merged.md", "merged.json"):
            self.assertFalse((self.job / name).is_symlink())
            self.assertEqual(stat.S_IMODE((self.job / name).stat().st_mode), 0o600)
        self.assertEqual({path.name for path in self.job.iterdir()}, before)

    def test_alternate_titles_and_all_member_details_survive_merge(self):
        left = finding(title="Cached entry retains stale records", confidence=0.75, explanation="Cache entry evidence.",
                       suggested_fix="Refresh stale cache entries", priority="P1")
        right = finding(confidence=0.9, explanation="Stale cache record evidence.",
                        suggested_fix="Expire stale cache entries")
        write(self.job, "left", [left])
        write(self.job, "right", [right])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertIn("Also reported as: `" + left["title"] + "`", md)
        cluster = data["findings"][0]
        self.assertEqual(cluster["explanation"], right["explanation"])
        self.assertEqual(cluster["suggested_fix"], right["suggested_fix"])
        members = {member["reviewer"]: member for member in cluster["members"]}
        for reviewer, original in (("left", left), ("right", right)):
            for field in ("title", "priority", "confidence", "explanation", "suggested_fix"):
                self.assertEqual(members[reviewer][field], original[field])

    def test_merged_detail_texts_collapse_independently_with_all_attributions(self):
        shared = finding(explanation="Cache entries retain expired records.", suggested_fix="Refresh stale cache entries")
        alternate = finding(explanation="Lookups return records after expiry.", suggested_fix="Expire stale cache entries")
        write(self.job, "astra", [shared, alternate, shared])
        write(self.job, "sol", [finding(explanation=shared["explanation"], suggested_fix=alternate["suggested_fix"])])
        write(self.job, "beta", [shared])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 3)
        self.assertEqual(len(data["findings"][0]["members"]), 5)
        details = md.split("## Details", 1)[1].split("## Each reviewer's assessment", 1)[0]
        self.assertIn("Explanation (astra+beta+sol): " + shared["explanation"], details)
        self.assertIn("Explanation (astra): " + alternate["explanation"], details)
        self.assertIn("Fix (astra+beta): " + shared["suggested_fix"], details)
        self.assertIn("Fix (astra+sol): " + alternate["suggested_fix"], details)
        for original in (shared, alternate):
            for field in ("explanation", "suggested_fix"):
                self.assertEqual(details.count(original[field]), 1)

    def test_merged_details_preserve_long_texts_that_differ_beyond_previous_limits(self):
        reports = {
            "astra": finding(explanation="Cache entry evidence. " * 25 + "Unique expiration evidence.",
                             suggested_fix="Refresh stale cache entries. " * 15 + "Handle expiration."),
            "sol": finding(explanation="Cache entry evidence. " * 25 + "Unique eviction evidence.",
                           suggested_fix="Refresh stale cache entries. " * 15 + "Handle eviction."),
        }
        for reviewer, report in reports.items():
            write(self.job, reviewer, [report])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        details = md.split("## Details", 1)[1].split("## Each reviewer's assessment", 1)[0]
        for reviewer, report in reports.items():
            self.assertGreater(len(report["explanation"]), 420)
            self.assertGreater(len(report["suggested_fix"]), 300)
            self.assertIn(f"Explanation ({reviewer}): " + report["explanation"], details)
            self.assertIn(f"Fix ({reviewer}): " + report["suggested_fix"], details)
        self.assertEqual(json.loads((self.job / "merged.json").read_text()), data)

    def test_merged_details_escape_text_and_keep_failed_partial_attribution(self):
        report = finding(explanation="Cache <script> & `entry` evidence.", suggested_fix="Refresh <entry> & cache.")
        write(self.job, "safe", [report])
        self.write_payload("failed", {"_reviewer": "failed_[seat]<tag>", "_ok": False,
                                       "findings": [report, None]})
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["consensus"], 1)
        details = md.split("## Details", 1)[1].split("## Each reviewer's assessment", 1)[0]
        attribution = "failed\\_\\[seat\\]&lt;tag&gt; PARTIAL (1 malformed) (partial: seat failed)+safe"
        self.assertIn(f"Explanation ({attribution}): Cache &lt;script&gt; &amp; \\`entry\\` evidence.", details)
        self.assertIn(f"Fix ({attribution}): Refresh &lt;entry&gt; &amp; cache.", details)
        self.assertEqual(details.count("Cache &lt;script&gt;"), 1)
        self.assertEqual(details.count("Refresh &lt;entry&gt;"), 1)
        self.assertNotIn("<script>", details)

    def test_more_than_thirty_clusters_truncates_table_and_details_only(self):
        write(self.job, "astra", [finding(file=f"modules/issue-{i:02d}.py") for i in range(35)])
        md, data = merge_findings.merge(self.job)
        self.assertEqual(len(data["findings"]), 35)
        self.assertEqual(len(json.loads((self.job / "merged.json").read_text())["findings"]), 35)
        self.assertIn("5 more in merged.json", md)
        self.assertEqual(len(re.findall(r"^\| \d+ \|", md, re.M)), 30)
        self.assertEqual(len(re.findall(r"^\d+\. \*\*", md, re.M)), 30)
        self.assertNotIn("modules/issue-30.py", md)

    def test_header_uses_metadata_and_does_not_invent_a_scope(self):
        write(self.job, "astra", [])
        md, _ = merge_findings.merge(self.job)
        self.assertEqual(md.splitlines()[0], "# Review of uncommitted changes in /repo")
        (self.job / "meta.json").unlink()
        md, _ = merge_findings.merge(self.job)
        self.assertEqual(md.splitlines()[0], "# Review")
        (self.job / "meta.json").write_text(json.dumps({"cwd": "/actual/repo"}))
        md, _ = merge_findings.merge(self.job)
        self.assertEqual(md.splitlines()[0], "# Review in /actual/repo")

    def test_malformed_top_level_values_and_findings_containers_do_not_crash(self):
        for payload in (None, 1, "text", False, {}, {"findings": None}, {"findings": {}}, {"findings": "bad"}):
            with self.subTest(payload=payload):
                self.write_payload("bad", payload)
                md, data = merge_findings.merge(self.job)
                self.assertEqual(data["findings"], [])
                self.assertTrue(data["reviewers"][0]["invalid"])
                self.assertIn("FAILED", md)
                self.assertNotIn("Findings: none", md)

    def test_malformed_metadata_falls_back_to_review_header(self):
        write(self.job, "astra", [])
        for content in ("not json", "null", "[]", '"text"', "17"):
            with self.subTest(meta=content):
                (self.job / "meta.json").write_text(content)
                md, _ = merge_findings.merge(self.job)
                self.assertEqual(md.splitlines()[0], "# Review")

    def test_sorting_uses_priority_consensus_confidence_then_path_and_line(self):
        write(self.job, "astra", [finding(file="z.py", confidence=0.9), finding(file="a.py", confidence=0.8),
                                   finding(file="b.py", confidence=0.9, line=40),
                                   finding(file="b.py", confidence=0.9, line=20),
                                   finding(file="consensus.py", confidence=0.6),
                                   finding(file="critical.py", confidence=0.1, priority="P0")])
        write(self.job, "sol", [finding(file="consensus.py", confidence=0.5)])
        _, data = merge_findings.merge(self.job)
        self.assertEqual([(f["file"], f["line"]) for f in data["findings"]],
                         [("critical.py", 20), ("consensus.py", 20), ("b.py", 20), ("b.py", 40),
                          ("z.py", 20), ("a.py", 20)])


class GroupB_MergeNotices(unittest.TestCase):
    """DESIGN §4: the job's persisted notices (meta.json `notices`: {kind, seat, text, at}) lead merged.md, on line 2
    (`Notices:`) right under the heading; a job without notices renders exactly as before."""

    NOTICES = [
        {"kind": "fallback-model", "seat": "astra", "at": "2026-09-25T03:20:00",
         "text": 'NOTICE: seat astra: model gpt-6-astra -> gpt-6-sol (rejected at startup: "model gpt-6-astra is not supported"; attempt 2)'},  # model-lint: allow fixture notice text
        {"kind": "models-check", "seat": None, "at": "2026-09-25T03:20:01",
         "text": "NOTICE: models: check FAILED (stale: newer model may exist: gpt-7-sol is listed but not in the model table); run bridge.py doctor"},  # model-lint: allow fixture notice text
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_findings_renders_notices(self):
        (self.job / "meta.json").write_text(json.dumps({"scope": "uncommitted changes", "cwd": "/repo", "notices": self.NOTICES}))
        write(self.job, "astra", [finding()], model="gpt-6-sol", effort="high")  # model-lint: allow fixture model label
        md, _ = merge_findings.merge(self.job)
        lines = md.splitlines()
        self.assertEqual(lines[0], "# Review of uncommitted changes in /repo")
        self.assertTrue(lines[1].startswith("Notices:"), lines[:4])
        reviewers = next(i for i, line in enumerate(lines) if line.startswith("Reviewers: "))
        block = "\n".join(lines[1:reviewers])
        for notice in self.NOTICES:
            self.assertTrue(notice["text"] in block or merge_findings.markdown_text(notice["text"]) in block, block)
        self.assertEqual((self.job / "merged.md").read_text(), md)
        # without notices nothing changes: the reviewers line follows the heading
        (self.job / "meta.json").write_text(json.dumps({"scope": "uncommitted changes", "cwd": "/repo"}))
        md, _ = merge_findings.merge(self.job)
        self.assertTrue(md.splitlines()[1].startswith("Reviewers: "), md)
        self.assertNotIn("Notices:", md)

    def test_a_blank_line_ends_the_notices_list(self):
        # A-opus-5 [P3] (review/rulings_final_review.md; review/final_A_findings.md): render() wrote the "- ..." notice items and
        # then "Reviewers: ..." and "Findings: ..." on the very next lines, which CommonMark/GFM read as lazy continuation lines of
        # the last notice bullet. The list ends with a blank line, so the Reviewers line starts its own paragraph: the heading,
        # `Notices:`, one item per notice, one blank line, then the Reviewers line
        (self.job / "meta.json").write_text(json.dumps({"scope": "uncommitted changes", "cwd": "/repo", "notices": self.NOTICES}))
        write(self.job, "astra", [finding()], model="gpt-6-sol", effort="high")  # model-lint: allow fixture model label
        md, _ = merge_findings.merge(self.job)
        lines = md.splitlines()
        n = len(self.NOTICES)
        self.assertEqual(lines[1], "Notices:", lines[:n + 4])
        self.assertTrue(all(line.startswith("- ") for line in lines[2:2 + n]), lines[:n + 4])
        self.assertEqual(lines[2 + n], "", f"no blank line after the notices list: {lines[:n + 4]}")
        self.assertTrue(lines[3 + n].startswith("Reviewers: "), lines[:n + 4])


if __name__ == "__main__":
    unittest.main()

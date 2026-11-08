"""The tests' clean Codex catalog (DESIGN §8): the newest real `model/list` capture in evidence/, completed with a derived entry for
every head of the table under test that the capture does not list.

The real captures stay what they are: the tests that compare the shipped table with real evidence read them raw (a head the newest
capture does not list fails exactly those tests, naming the capture to add). Everything else that only needs "a catalog that lists
every head" reads the completed list, so a head moved by a human checkpoint does not break every test that happens to read the
catalog. A derived entry copies a real entry of the same family where the capture has one (else the capture's first visible entry),
takes the head's id and its efforts from the table, and is visible, without an upgrade pointer, and marked `_derived`.
"""
import copy
import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent / "evidence"
NEWEST = EVIDENCE / "codex_model_list_cli0.167.0.json"  # the live list of 2026-11-08 under CLI 0.167.0: lists every head of table v21


def load_models(path=NEWEST):
    """The `models` list of a capture file ({"models": [...]} with a `_provenance` key, or a bare list)."""
    data = json.loads(Path(path).read_text())
    return data["models"] if isinstance(data, dict) else data


def _family_of(table, vendor, model_id):
    """The table family whose head or retired list names `model_id`, else the family whose id_glob it matches."""
    import fnmatch
    for fam, rec in table["families"][vendor].items():
        if model_id == rec.get("known_good") or model_id in (table["retired"][vendor].get(fam) or []):
            return fam
    for fam, rec in table["families"][vendor].items():
        if rec.get("id_glob") and fnmatch.fnmatchcase(model_id, rec["id_glob"]):
            return fam
    return None


def complete(models, table):
    """(the capture's entries plus one derived entry per Codex head of `table` they lack, [the derived heads])."""
    out = copy.deepcopy(models)
    listed = {e.get("id") or e.get("model") for e in out if isinstance(e, dict)}
    added = []
    for fam, rec in table["families"]["codex"].items():
        head = rec["known_good"]
        if head in listed:
            continue
        same = [e for e in models if isinstance(e, dict) and not e.get("hidden") and _family_of(table, "codex", e.get("id") or "") == fam]
        visible = [e for e in models if isinstance(e, dict) and not e.get("hidden")]
        template = copy.deepcopy((same or visible or [{}])[0])
        efforts = list(table["models"][head].get("efforts") or [])
        template.update({"id": head, "model": head, "displayName": head, "hidden": False, "isDefault": False, "upgrade": None,
                         "upgradeInfo": None, "_derived": "catalog_fixture.complete: the table's head, absent from the capture",
                         "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e} for e in efforts]})
        if template.get("defaultReasoningEffort") not in efforts:
            template["defaultReasoningEffort"] = efforts[0] if efforts else None
        out.append(template)
        added.append(head)
    return out, added

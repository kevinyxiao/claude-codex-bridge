#!/usr/bin/env python3
"""
Merge findings-<reviewer>.json files into an attributed, deduplicated review.

Usage: merge_findings.py <job_dir>
Also importable: merge(job_dir) -> (markdown, data), writing merged.md and merged.json.
Only successful reviewers count toward consensus; every accepted member is retained.
"""
import json
import math
import os
import re
import secrets
import sys
import unicodedata
from pathlib import Path

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SEVERITY_TO_P = {"critical": "P0", "blocker": "P0", "high": "P1", "medium": "P2", "moderate": "P2", "low": "P3",
                 "info": "P3", "nit": "P3"}
STOP = set("a an the of to in on for and or is are be with by from that this it as at into not no does do "
           "when while use uses used using value values file files code function functions method methods "
           "call calls called calling return returns returned returning read reads reading check checks checked "
           "checking upload uploads uploaded uploading request requests upstream url get json "
           "should could would may might can will have has had been being if then else there here "
           "also only other each any all current existing prior keep preserve before after during "
           "off one condition".split())
INFLECTIONS = {
    "cached": "cache", "caching": "cache", "caches": "cache",
    "entries": "entry", "retries": "retry", "retried": "retry", "retrying": "retry",
    "ports": "port", "accepts": "accept", "accepted": "accept", "accepting": "accept",
    "permits": "permit", "permitted": "permit", "permitting": "permit",
    "allows": "allow", "allowed": "allow", "allowing": "allow",
}
LINE_TOLERANCE = 6
TITLE_SIMILARITY = 0.50
FIX_SIMILARITY = 0.50
TITLE_ONLY_SIMILARITY = 0.70
MAX_ROWS = 30


def norm_priority(f):
    for value in (f.get("priority"), f.get("severity")):
        if isinstance(value, str):
            value = value.strip()
            if value.upper() in PRIORITY_ORDER:
                return value.upper()
            if value.lower() in SEVERITY_TO_P:
                return SEVERITY_TO_P[value.lower()]
    return None


def stem(word):
    """Small inflection normalizer, with explicit silent-e and irregular forms."""
    if word in INFLECTIONS:
        return INFLECTIONS[word]
    if word.endswith("sses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(*texts):
    out = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for word in re.findall(r"[a-z0-9_]+", text.lower()):
            if (word.isdigit() or len(word) >= 3) and word not in STOP:
                word = stem(word)
                if word not in STOP:
                    out.add(word)
    return out


def norm_file(path):
    path = re.sub(r"/+", "/", path.replace("\\", "/"))
    return re.sub(r"^(?:\./)+", "", path)


def _text(value):
    return "" if value is None else str(value)


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def validate_finding(raw):
    if not isinstance(raw, dict):
        return None
    title, path = raw.get("title"), raw.get("file")
    priority = norm_priority(raw)
    if (not isinstance(title, str) or not title.strip() or
            not isinstance(path, str) or not path.strip() or not priority):
        return None
    line = raw.get("line")
    if isinstance(line, bool) or not isinstance(line, (int, float, str)):
        return None
    try:
        # int() accepts integer strings but must not truncate fractional numbers.
        integer = int(line)
        if integer < 0 or (isinstance(line, float) and line != integer):
            return None
    except (ValueError, OverflowError):
        return None
    confidence = raw.get("confidence", 0.5)
    try:
        confidence = 0.5 if isinstance(confidence, bool) else float(confidence)
        if math.isnan(confidence):
            confidence = 0.5
    except (TypeError, ValueError, OverflowError):
        confidence = 0.5
    explicit_root = path.replace("\\", "/").startswith("./")
    path = norm_file(path)
    if not path:
        return None
    # Keep explicit root-file evidence after separator normalization. It must
    # survive in members too, so rechecking a finding cannot invent an alias.
    if explicit_root and "/" not in path:
        path = "./" + path
    return dict(priority=priority, title=title.strip(), file=path, line=integer,
                explanation=_text(raw.get("explanation")), suggested_fix=_text(raw.get("suggested_fix")),
                confidence=max(0.0, min(1.0, confidence)))


def _load_meta(job_dir, name="meta.json"):
    try:
        meta = json.loads((Path(job_dir) / name).read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def load_reviews(job_dir):
    reviews = []
    for path in sorted(Path(job_dir).glob("findings-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            data = {"_invalid": True, "_error": "unreadable " + path.name}
        if isinstance(data, list):
            data = {"findings": data, "assessment": ""}
        elif not isinstance(data, dict):
            data = {"_invalid": True, "_error": "expected an object or findings list"}
        raw_findings = data.get("findings")
        invalid = bool(data.get("_invalid"))
        malformed = int(not isinstance(raw_findings, list))
        if not isinstance(raw_findings, list):
            raw_findings = []
            invalid = True
        ok = data.get("_ok", True) is True
        observed = data.get("_observed_models")
        review = dict(tag=_text(data.get("_reviewer")) or path.stem[len("findings-"):],
                      model=_text(data.get("_model")), effort=_text(data.get("_effort")),
                      secs=_seconds(data.get("_secs")), assessment=str(data.get("assessment", "")).strip(),
                      invalid=invalid, ok=ok, failed=invalid or not ok, error=_text(data.get("_error")),
                      dropped=0, malformed=malformed, findings=[],
                      # docs/model-routing.md 5: what the seat asked for, what its attempts observed (the set, sorted by the registry,
                      # and the model that finished the attempt), whether a substitution was flagged
                      requested=_text(data.get("_model_requested")),
                      observed=[str(m) for m in observed if isinstance(m, str)] if isinstance(observed, list) else [],
                      observed_id=_text(data.get("_observed_model_id")),
                      substituted=isinstance(data.get("_substitution"), dict))
        review["model"] = review["model"] or review["tag"]
        for raw in raw_findings:
            finding = validate_finding(raw)
            if finding is None:
                review["dropped"] += 1
                review["malformed"] += 1
                continue
            review["findings"].append(finding)
        reviews.append(review)
    return sorted(reviews, key=lambda r: (r["tag"], r["model"], r["effort"]))


def same_path(a, b, paths):
    if norm_file(a) == norm_file(b):
        return True
    if paths is None:
        return False
    if "/" in a and "/" not in b:
        a, b = b, a
    if "/" in a or "/" not in b or a in paths:
        return False
    return {path for path in paths if path.endswith("/" + a)} == {b}


def jaccard(left, right):
    return len(left & right) / len(left | right) if left or right else 0.0


def issue_similarity(a, b, paths):
    if not same_path(a["file"], b["file"], paths):
        return None
    distance = abs(a["line"] - b["line"])
    if distance > LINE_TOLERANCE:
        return None
    title_similarity = jaccard(tokens(a["title"]), tokens(b["title"]))
    if a["suggested_fix"].strip() and b["suggested_fix"].strip():
        if (title_similarity < TITLE_SIMILARITY or
                jaccard(tokens(a["suggested_fix"]), tokens(b["suggested_fix"])) < FIX_SIMILARITY):
            return None
    elif title_similarity < TITLE_ONLY_SIMILARITY:
        return None
    return title_similarity


def possible_duplicate(a, b, paths):
    if not same_path(a["file"], b["file"], paths):
        return False
    distance = abs(a["line"] - b["line"])
    if distance > LINE_TOLERANCE or issue_similarity(a, b, paths) is not None:
        return False
    if distance == 0:
        return True
    # Former explanation-based matches remain linked, without consensus or
    # hiding either fix. Preserve the old nearby-location evidence threshold.
    if len(tokens(a["explanation"]) & tokens(b["explanation"])) < 2:
        return False
    left = tokens(a["title"], a["explanation"])
    right = tokens(b["title"], b["explanation"])
    return jaccard(left, right) >= 0.40


def same_issue(a, b):
    """Pairwise compatibility without job-wide evidence cannot use basename aliases."""
    a, b = validate_finding(a), validate_finding(b)
    if a is None or b is None:
        return False
    return issue_similarity(a, b, None) is not None


def _finding_key(finding):
    # Attribution must never decide which otherwise incompatible issues link.
    title = re.sub(r"\s+", " ", finding["title"]).casefold()
    return (finding["file"], finding["line"], title, finding["title"],
            finding["priority"], -finding["confidence"], finding["explanation"],
            finding["suggested_fix"], finding["failed"], finding["reviewer"])


def cluster(reviews, paths=None):
    items = [dict(finding, reviewer=review["tag"], failed=review["failed"])
             for review in reviews for finding in review["findings"]]
    items.sort(key=_finding_key)
    clusters = []
    for finding in items:
        matches = []
        for index, current in enumerate(clusters):
            lines = [member["line"] for member in current["members"]] + [finding["line"]]
            if max(lines) - min(lines) > LINE_TOLERANCE:
                continue
            scores = [issue_similarity(finding, member, paths) for member in current["members"]]
            # Require compatibility with every member, not a chain of lexical bridges.
            if all(score is not None for score in scores):
                # Creation order follows the earliest (path, line, normalized title).
                matches.append((max(scores), -index, current))
        if matches:
            max(matches, key=lambda match: match[:2])[2]["members"].append(finding)
        else:
            clusters.append({"members": [finding]})
    for current in clusters:
        members = current["members"]
        best = min(members, key=lambda member: (-member["confidence"], _finding_key(member)))
        current.update({key: best[key] for key in
                        ("title", "file", "line", "confidence", "explanation", "suggested_fix")})
        current["priority"] = min((member["priority"] for member in members), key=PRIORITY_ORDER.get)
        current["reviewers"] = sorted({member["reviewer"] for member in members})
        current["successful_reviewers"] = sorted({member["reviewer"] for member in members if not member["failed"]})
        current["failed_reviewers"] = sorted({member["reviewer"] for member in members if member["failed"]})
        current["consensus"] = len(current["successful_reviewers"])
        if not current["explanation"].strip():
            substantive = [member for member in members if member["explanation"].strip()]
            if substantive:
                fallback = min(substantive, key=lambda member: (-member["confidence"], _finding_key(member)))
                current["explanation"] = fallback["explanation"]
    clusters.sort(key=lambda c: (PRIORITY_ORDER[c["priority"]], -c["consensus"], -c["confidence"],
                                 c["file"], c["line"], c["title"]))
    # References use final, one-based row numbers, including rows beyond MAX_ROWS.
    for current in clusters:
        current["possible_duplicate_of"] = []
    for index, current in enumerate(clusters):
        for other_index in range(index + 1, len(clusters)):
            other = clusters[other_index]
            if any(possible_duplicate(left, right, paths)
                   for left in current["members"] for right in other["members"]):
                current["possible_duplicate_of"].append(other_index + 1)
                other["possible_duplicate_of"].append(index + 1)
    return clusters


def short(value, limit):
    value = re.sub(r"\s+", " ", _text(value)).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _single_line(value, limit=None):
    text = "".join(" " if char.isspace() or unicodedata.category(char).startswith("C") else char
                   for char in _text(value))
    return short(text, limit) if limit is not None else text


def markdown_text(value, limit=None, table=False):
    text = _single_line(value, limit)
    # Escape backslashes first, so an input backslash cannot cancel the pipe escape.
    text = text.replace("\\", "\\\\")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"([`*_\[\]#])", r"\\\1", text)
    return text.replace("|", "\\|") if table else text


def code_span(value, limit=None, table=False):
    text = _single_line(value, limit)
    delimiter = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    if table:
        text = text.replace("|", "\\|")
    # Padding separates backtick runs and preserves intentional edge spaces.
    if text.startswith("`") or text.endswith("`") or (
            text.startswith(" ") and text.endswith(" ") and text.strip()):
        text = " " + text + " "
    return delimiter + text + delimiter


def _partial(malformed):
    return f" PARTIAL ({malformed} malformed)" if malformed else ""


def _attribution(current, malformed, table=False):
    return "+".join(markdown_text(tag, table=table) + _partial(malformed.get(tag, 0))
                    + (" (partial: seat failed)" if tag in current["failed_reviewers"] else "")
                    for tag in current["reviewers"])


def _member_details(current, malformed):
    for field, label in (("explanation", "Explanation"), ("suggested_fix", "Fix")):
        distinct = {}
        for member in current["members"]:
            value = member[field]
            if value.strip():
                distinct.setdefault(value, []).append(member)
        for value, members in sorted(distinct.items()):
            attribution = dict(reviewers=sorted({member["reviewer"] for member in members}),
                               failed_reviewers={member["reviewer"] for member in members if member["failed"]})
            # Show full texts: truncation could hide the only difference between
            # members. Collapse identical texts while retaining every attribution.
            yield ""
            yield f"    {label} ({_attribution(attribution, malformed)}): {markdown_text(value)}"


def render(job_dir, reviews, clusters):
    meta = _load_meta(job_dir)
    heading = "# Review"
    if meta.get("scope"):
        heading += " of " + markdown_text(meta["scope"])
    if meta.get("cwd"):
        heading += " in " + markdown_text(meta["cwd"])
    names = []
    malformed = {}
    for review in reviews:
        malformed[review["tag"]] = malformed.get(review["tag"], 0) + review["malformed"]
        name = markdown_text(review["tag"])
        if review["effort"]:
            name += " (" + markdown_text(review["model"]) + "@" + markdown_text(review["effort"]) + ")"
        if review.get("requested"):  # docs/model-routing.md 5: the header lists every seat as requested -> ran
            seen = review.get("observed") or []
            final = review.get("observed_id")
            if final:  # the model that finished the attempt; the observed set is sorted, so its order says nothing about time
                others = [m for m in seen if m != final]
                ran = final + (" (also observed " + ", ".join(others) + ")" if others else "")
            elif seen:  # an older record without the finishing model: every model observed, none singled out
                ran = ", ".join(seen)
            else:
                ran = review["model"] + ", unobserved"
            name += (" (requested " + markdown_text(review["requested"]) + " -> ran " + markdown_text(ran)
                     + ("; SUBSTITUTED" if review.get("substituted") else "") + ")")
        if review["failed"]:
            name += " (FAILED" + (", INVALID OUTPUT" if review["invalid"] else "") + ")"
        name += _partial(review["malformed"])
        names.append(name)
    counts = {priority: sum(c["priority"] == priority for c in clusters) for priority in PRIORITY_ORDER}
    failed = sum(review["failed"] for review in reviews)
    dropped = sum(review["dropped"] for review in reviews)
    if clusters:
        findings = "Findings: " + ", ".join(priority + " " + str(count) for priority, count in counts.items() if count)
    elif failed or dropped or not reviews:
        findings = "Findings: no validated findings (review incomplete)"
    else:
        findings = "Findings: none"
    if failed:
        findings += "; " + str(failed) + " reviewer(s) FAILED"
    if dropped:
        findings += "; " + str(dropped) + " finding(s) dropped"
    lines = [heading]
    notices = [n["text"] for n in (meta.get("notices") or []) if isinstance(n, dict) and isinstance(n.get("text"), str) and n["text"].strip()]
    if notices:  # the job's persisted notices lead the report, right under the heading (bridge DESIGN §4)
        lines.append("Notices:")
        lines.extend("- " + markdown_text(text) for text in notices)
        lines.append("")  # ends the list: the Reviewers line is its own paragraph, not a lazy continuation of the last item
    lines += ["Reviewers: " + (", ".join(names) or "none yet"), findings, ""]
    if clusters:
        lines.extend(["| # | Pri | Finding | Where | Found by | Conf |",
                      "|---|-----|---------|-------|----------|------|"])
        for index, current in enumerate(clusters[:MAX_ROWS], 1):
            who = _attribution(current, malformed, table=True) + (" ✔consensus" if current["consensus"] > 1 else "")
            possible = "; ".join(f"possibly the same as #{number}" for number in current["possible_duplicate_of"])
            title = code_span(current["title"], 90, table=True) + (f" ({possible})" if possible else "")
            location = code_span(f"{current['file']}:{current['line']}", table=True)
            lines.append(f"| {index} | {current['priority']} | {title} | "
                         f"{location} | {who} | {current['confidence']} |")
        if len(clusters) > MAX_ROWS:
            lines.append(f"| … | | {len(clusters) - MAX_ROWS} more in merged.json | | | |")
        lines.extend(["", "## Details"])
        for index, current in enumerate(clusters[:MAX_ROWS], 1):
            location = code_span(f"{current['file']}:{current['line']}")
            lines.append(f"{index}. **[{current['priority']}] {code_span(current['title'])}** — "
                         f"{location} — {_attribution(current, malformed)}")
            if len(current["members"]) > 1:
                lines.extend(_member_details(current, malformed))
            else:
                lines.append("   " + markdown_text(current["explanation"], 420))
                if current["suggested_fix"]:
                    lines.append("   Fix: " + markdown_text(current["suggested_fix"], 300))
            other_titles = sorted({member["title"] for member in current["members"]} - {current["title"]})
            if other_titles:
                lines.append("   Also reported as: " + "; ".join(code_span(title) for title in other_titles))
        if len(clusters) > MAX_ROWS:
            lines.append(f"{len(clusters) - MAX_ROWS} more in merged.json.")
        lines.append("")
    lines.append("## Each reviewer's assessment")
    for review in reviews:
        flags = " (FAILED)" if review["failed"] else ""
        if review["invalid"]:
            flags += " (INVALID OUTPUT)"
        flags += _partial(review["malformed"])
        secs = ", " + markdown_text(review["secs"]) + "s" if review["secs"] is not None else ""
        error = " Error: " + markdown_text(review["error"], 500) if review["error"] else ""
        lines.append(f"- **{markdown_text(review['tag'])}**{flags} ({len(review['findings'])} findings, "
                     f"{review['dropped']} dropped{secs}): {markdown_text(review['assessment'], 500)}{error}")
    return "\n".join(lines)


def _write_private(path, text):
    """Replace one artifact without following links or exposing its contents."""
    while True:
        temporary = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def merge(job_dir):
    job_dir = Path(job_dir)
    reviews = load_reviews(job_dir)
    meta = _load_meta(job_dir, "review.meta.json")
    inventory = meta.get("inventory")
    paths = None
    if (meta.get("inventory_complete") is True and isinstance(inventory, list)
            and all(isinstance(path, str) and path.strip() and norm_file(path) for path in inventory)):
        paths = {norm_file(path) for path in inventory}
    clusters = cluster(reviews, paths)
    markdown = render(job_dir, reviews, clusters)
    data = {"reviewers": [{key: review[key] for key in
                           ("tag", "model", "effort", "secs", "assessment", "invalid", "ok", "failed", "error",
                            "dropped", "malformed")}
                          for review in reviews], "findings": clusters}
    _write_private(job_dir / "merged.md", markdown)
    _write_private(job_dir / "merged.json", json.dumps(data, indent=1))
    return markdown, data


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(merge(sys.argv[1])[0])

"""make_data.py STRAITJACKET_CHECKOUT OUT_DIR

Writes the film's data from Straitjacket's own receipts, so that nothing on the page is typed by hand:

  OUT_DIR/film.json   every number and specimen the page shows, each with the repository file it came from
  OUT_DIR/lines.bin   two bytes per line of the 20,001-line fixture: template family, length in characters

The fixture log is rebuilt with the algorithm of evals/field_needle.py (build_corpus, seed 1234) and must match the
sha256 recorded in evals/field-needle-record.json, and the template counts printed in the checked-in digest; if either
differs this script stops instead of writing a film that shows something else.
"""
import hashlib
import json
import random
import re
import subprocess
import sys
from pathlib import Path

repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
GITHUB = "https://github.com/vamsiramakrishnan/straitjacket/blob/main/"


def read(rel: str) -> str:
    return (repo / rel).read_text(encoding="utf-8")


record = json.loads(read("evals/field-needle-record.json"))
anchor = json.loads(read("evals/anchor-drift-2026-08-20.json"))
digest = read("tests/fixtures/field_needle_main_digest.txt")
corpus = record["corpus"]

# ---------------------------------------------------------------- the log, line for line
TOTAL, NEEDLE, LOUD, SEED = corpus["lines"], corpus["quiet_needle_line"], (5000, 17650), 1234
VERBS = ["accepted", "processed", "forwarded", "cached", "validated", "dispatched", "completed", "enqueued"]
rng = random.Random(SEED)
workers = [f"worker-{i}" for i in range(1, 33)]
lines, verb_of = [], []
for i in range(1, TOTAL + 1):
    if i == NEEDLE:
        lines.append(f"INFO {rng.choice(workers)} checkout request req-14237 fell back to legacy gateway after circuit opened")
        verb_of.append("needle")
    elif i == LOUD[0]:
        lines.append("ERROR db-pool connection refused on shard 7 (attempt 3)")
        verb_of.append("loud0")
    elif i == LOUD[1]:
        lines.append("ERROR cache eviction storm: 4128 keys dropped in 200ms window")
        verb_of.append("loud1")
    else:
        w, v, ms = rng.choice(workers), rng.choice(VERBS), rng.randint(2, 90)
        lines.append(f"INFO {w} request req-{i:05d} {v} in {ms}ms")
        verb_of.append(v)
text = "\n".join(lines) + "\n"
sha = hashlib.sha256(text.encode()).hexdigest()
if sha != corpus["sha256"] or len(text.encode()) != corpus["bytes"]:
    sys.exit(f"the rebuilt log is not the receipt's log: sha256 {sha} against {corpus['sha256']}")

# ---------------------------------------------------------------- the census, as the digest prints it
census = []
for m in re.finditer(r"^\s+([\d,]+)× (L\d+): (.+?)(?: · span ([0-9a-f]+))?$", digest, re.M):
    count, first, template = int(m.group(1).replace(",", "")), m.group(2), m.group(3)
    verb = next((v for v in VERBS if f" {v} in " in template), None)
    census.append({"count": count, "first_line": first, "template": template, "verb": verb})
families = [c for c in census if c["verb"]]
for c in families:
    if verb_of.count(c["verb"]) != c["count"]:
        sys.exit(f"the rebuilt log has {verb_of.count(c['verb'])} '{c['verb']}' lines; the digest says {c['count']}")
order = [c["verb"] for c in families] + ["loud0", "needle", "loud1"]          # family ids 0..10
fam = bytes(order.index(v) for v in verb_of)
length = bytes(min(len(s), 255) for s in lines)
(out / "lines.bin").write_bytes(bytes(b for pair in zip(fam, length) for b in pair))

arms = {a["tool"]: a for a in record["arms"]}
sj = next(a for k, a in arms.items() if k.startswith("sj "))
handle = re.search(r"\[ctx (run:[0-9a-f]+)", digest).group(1)
cov = re.search(r"shown: (\d+) spans · omitted: ([\d,]+) lines", digest)
head = re.search(r"templates: (\d+) cover ([\d,]+)/([\d,]+) lines", digest)
commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

film = {
    "source": {"repository": "vamsiramakrishnan/straitjacket", "commit": commit, "blob": GITHUB},
    "log": {
        "lines": TOTAL, "bytes": corpus["bytes"], "sha256": corpus["sha256"], "raw_tokens": corpus["raw_tokens_o200k"],
        "needle_line": NEEDLE, "needle_text": lines[NEEDLE - 1], "loud_lines": list(LOUD),
        "loud_text": [lines[LOUD[0] - 1], lines[LOUD[1] - 1]], "sample": lines[:6],
        "families": [{"id": i, "verb": c["verb"], "count": c["count"], "first_line": c["first_line"]} for i, c in enumerate(families)],
        "family_order": order, "receipt": "evals/field-needle-record.json", "generator": "evals/field_needle.py",
    },
    "fates": {
        "keep_everything": {"tokens": arms["naive (raw flood)"]["out_tokens"], "needle_kept": arms["naive (raw flood)"]["quiet_needle_survived"]},
        "head_and_tail": {"tokens": arms["caveman (head+tail trunc)"]["out_tokens"], "head_lines": 40, "tail_lines": 40,
                          "needle_kept": arms["caveman (head+tail trunc)"]["quiet_needle_survived"]},
        "digest": {"tokens": sj["out_tokens"], "needle_kept": sj["quiet_needle_survived"], "address": sj["retrieval_address"]},
        "receipt": "evals/field-needle-record.json",
    },
    "digest": {
        "text": digest, "handle": handle, "lines": digest.rstrip("\n").count("\n") + 1, "templates": int(head.group(1)),
        "covered": int(head.group(2).replace(",", "")), "spans_shown": int(cov.group(1)),
        "lines_omitted": int(cov.group(2).replace(",", "")), "receipt": "tests/fixtures/field_needle_main_digest.txt",
    },
    "residency": {
        "illustrative": True, "turns": 7, "resident_turns": 6, "token_turns": corpus["raw_tokens_o200k"] * 6,
        "note": "Illustrative seven-turn trace; payload and digest sizes from evals/field-needle-record.json",
        "source": "assets/readme/diagrams/ae-residency.svg",
    },
    "anchors": {**{k: anchor["total"][k] for k in ("cases", "relocated", "verified", "refused", "anchored_wrong", "unanchored_silently_wrong")},
                "files": anchor["files"], "overhead_pct": anchor["cost"]["overhead_pct"], "receipt": "evals/anchor-drift-2026-08-20.json"},
}
(out / "film.json").write_text(json.dumps(film, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print("log sha256 matches the receipt:", sha[:16], "…")
print("families:", [(c["verb"], c["count"]) for c in families])
print("head+tail keeps", 40 + 40, "lines;", "digest", film["digest"]["lines"], "lines,", sj["out_tokens"], "tokens; handle", handle)
print("wrote", out / "film.json", (out / "film.json").stat().st_size, "bytes;", out / "lines.bin", (out / "lines.bin").stat().st_size, "bytes")

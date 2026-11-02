"""A slow job made fast with the same output, for `lcode bench --tasks fast-dedupe`.

`dedupe.py` merges customer records that share an email address or a phone number. It compares every record
with every customer found so far, so the nightly run over 200,000 records takes hours. Making it fast means
indexing the emails and phones, but the job's exact behavior has to survive: records join up through chains
(A shares an email with B, B a phone with C), an empty email or phone matches nothing, and the merged name
comes from the newest record, the first in the file among equals. The check runs the model's version on
60,000 records it never saw, with a time limit the old version can't meet, and compares the output with a
fast reference that matches the old version (tests check that on smaller files).
"""

from __future__ import annotations

import csv
import io
import random
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

DEDUPE = '''\
"""Merge the records of customers who signed up more than once: python dedupe.py IN.csv OUT.csv"""

import csv
import re
import sys


def normal_email(text):
    return text.strip().lower()


def normal_phone(text):
    return re.sub(r"\\D", "", text)


def same_customer(a, b):
    """Two records are the same customer when they share an email address (whatever its case) or a phone
    number (whatever its punctuation). A record without an email or a phone shares nothing through it."""
    email = normal_email(a["email"])
    if email and email == normal_email(b["email"]):
        return True
    phone = normal_phone(a["phone"])
    return bool(phone) and phone == normal_phone(b["phone"])


def merge(records):
    """One customer from all its records: the smallest id, the name of the most recently updated record
    (the first in the file if several were updated that day), and all its emails and phones, sorted."""
    records = sorted(records, key=lambda r: r["line"])
    newest = max(records, key=lambda r: r["updated"])
    return {
        "id": str(min(int(r["id"]) for r in records)),
        "name": newest["name"],
        "emails": ";".join(sorted({normal_email(r["email"]) for r in records} - {""})),
        "phones": ";".join(sorted({normal_phone(r["phone"]) for r in records} - {""})),
    }


def dedupe(records):
    """The customers, each with all its records merged, by id. Records join up through any chain of shared
    emails and phones."""
    groups = []
    for record in records:
        matches = [group for group in groups if any(same_customer(record, other) for other in group)]
        joined = [other for group in matches for other in group] + [record]
        groups = [group for group in groups if all(group is not match for match in matches)] + [joined]
    return sorted((merge(group) for group in groups), key=lambda customer: int(customer["id"]))


def read(path):
    with open(path, newline="") as file:
        return [{**row, "line": n} for n, row in enumerate(csv.DictReader(file))]


def write(customers, path):
    with open(path, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["id", "name", "emails", "phones"])
        writer.writeheader()
        writer.writerows(customers)


if __name__ == "__main__":
    source, target = sys.argv[1:3]
    write(dedupe(read(source)), target)
'''

TEST_DEDUPE = """\
import unittest

from dedupe import dedupe


def record(id, name, email="", phone="", updated="2026-01-01", line=0):
    return {"id": id, "name": name, "email": email, "phone": phone, "updated": updated, "line": line}


class DedupeTest(unittest.TestCase):
    def test_shared_email_ignores_case(self):
        customers = dedupe([record("7", "Ada", "ada@example.com"), record("3", "Ada L", " ADA@example.com", line=1)])
        self.assertEqual(customers, [{"id": "3", "name": "Ada", "emails": "ada@example.com", "phones": ""}])

    def test_shared_phone_ignores_punctuation(self):
        customers = dedupe([record("1", "Bo", phone="+49 30 1234"), record("2", "Bo", phone="(4930) 12-34", line=1)])
        self.assertEqual([c["id"] for c in customers], ["1"])

    def test_different_customers_stay_apart(self):
        customers = dedupe([record("1", "Cy", "cy@example.com"), record("2", "Di", "di@example.com", line=1)])
        self.assertEqual([c["name"] for c in customers], ["Cy", "Di"])


if __name__ == "__main__":
    unittest.main()
"""

FIRST = ["Ada", "Bo", "Cy", "Di", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jo", "Kai", "Lea", "Max", "Noa", "Ola"]
LAST = ["Berg", "Costa", "Dahl", "Eng", "Fox", "Gray", "Holm", "Ito", "Kahn", "Lund", "Moss", "Nagy", "Ortiz"]


def customers_csv(count: int, seed: int) -> str:
    """Records of `count` customers' sign-ups: most once, some several times, linked by emails and phones in
    chains, with empty fields, phones in different formats, emails in different cases and dates that tie."""
    rng = random.Random(seed)
    rows = []
    next_id = 1000
    people = max(1, count * 3 // 4)
    for person in range(people):
        email = f"{rng.choice(FIRST).lower()}.{rng.choice(LAST).lower()}{person}@example.com"
        phone = f"{rng.randint(30, 89)}{rng.randint(1000000, 9999999)}"
        signups = rng.choices([1, 2, 3, 4], weights=[70, 18, 8, 4])[0]
        for _ in range(signups):
            next_id += rng.randint(1, 120)
            shown_email = email if rng.random() < 0.7 else ""
            if shown_email and rng.random() < 0.3:
                shown_email = f"  {shown_email.upper()}"
            shown_phone = phone if rng.random() < 0.6 or not shown_email else ""
            if shown_phone and rng.random() < 0.4:
                shown_phone = f"+{shown_phone[:2]} ({shown_phone[2:5]}) {shown_phone[5:]}"
            if not shown_email and not shown_phone:
                shown_phone = phone
            day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}" if rng.random() < 0.8 else "2026-06-01"
            rows.append([str(next_id), f"{rng.choice(FIRST)} {rng.choice(LAST)}", shown_email, shown_phone, day])
            if len(rows) >= count:
                break
        if len(rows) >= count:
            break
    while len(rows) < count:  # people with neither email nor phone in common, and records with empty fields
        next_id += 1
        rows.append([str(next_id), f"{rng.choice(FIRST)} {rng.choice(LAST)}", "", "", "2026-03-03"])
    rng.shuffle(rows)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id", "name", "email", "phone", "updated"])
    writer.writerows(rows)
    return out.getvalue()


FILES = {
    "README.md": (
        "# dedupe\n\nThe nightly job that merges customers who signed up more than once:\n\n"
        "    python dedupe.py customers.csv clean.csv\n\nTests: `python -m unittest`.\n"
    ),
    "dedupe.py": DEDUPE,
    "test_dedupe.py": TEST_DEDUPE,
    "sample.csv": customers_csv(2000, seed=7),
}

PROMPT = (
    "dedupe.py takes hours on our 200,000 customer records, and the nightly job has to finish in minutes. "
    "Make it fast, a few seconds for 200,000 records, without changing its output in any way. sample.csv has "
    "2,000 records to try it on."
)

REFERENCE = """\
import csv
import re
import sys


def normal_email(text):
    return text.strip().lower()


def normal_phone(text):
    return re.sub(r"\\D", "", text)


def main(source, target):
    with open(source, newline="") as file:
        records = list(csv.DictReader(file))
    parent = list(range(len(records)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owners = {}
    for i, record in enumerate(records):
        for key in (("e", normal_email(record["email"])), ("p", normal_phone(record["phone"]))):
            if key[1]:
                j = owners.setdefault(key, i)
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(records)):
        groups.setdefault(find(i), []).append(i)
    customers = []
    for members in groups.values():
        rows = [records[i] for i in sorted(members)]
        newest = max(rows, key=lambda r: r["updated"])
        customers.append({
            "id": str(min(int(r["id"]) for r in rows)),
            "name": newest["name"],
            "emails": ";".join(sorted({normal_email(r["email"]) for r in rows} - {""})),
            "phones": ";".join(sorted({normal_phone(r["phone"]) for r in rows} - {""})),
        })
    customers.sort(key=lambda c: int(c["id"]))
    with open(target, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["id", "name", "emails", "phones"])
        writer.writeheader()
        writer.writerows(customers)


if __name__ == "__main__":
    main(*sys.argv[1:3])
"""

HIDDEN_RECORDS = 60000
TIME_LIMIT = 60  # seconds for 60,000 records: the reference takes about one, the old version 40 minutes
CASES = [(3000, 11, 120), (HIDDEN_RECORDS, 12, TIME_LIMIT)]  # (records, seed, seconds): small first, for its errors


@cache
def expected(records: int, seed: int) -> str:
    with tempfile.TemporaryDirectory(prefix="lcode-bench-dedupe-") as tmp:
        folder = Path(tmp)
        (folder / "in.csv").write_text(customers_csv(records, seed))
        (folder / "reference.py").write_text(REFERENCE)
        subprocess.run([sys.executable, "reference.py", "in.csv", "out.csv"], cwd=folder, check=True, timeout=120)
        return (folder / "out.csv").read_text()


def rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def check(folder: Path, answer: str):
    from lcode.bench import Check, last_line

    if not (folder / "dedupe.py").is_file():
        return Check(False, "dedupe.py is missing")
    with tempfile.TemporaryDirectory(prefix="lcode-bench-dedupe-") as tmp:
        work = Path(tmp)
        for records, seed, limit in CASES:
            (work / "in.csv").write_text(customers_csv(records, seed))
            (work / "out.csv").unlink(missing_ok=True)
            try:
                r = subprocess.run(
                    [sys.executable, str(folder / "dedupe.py"), "in.csv", "out.csv"],
                    cwd=work,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=limit,
                )
            except subprocess.TimeoutExpired:
                return Check(False, f"still too slow: {records:,} records took over {limit} s")
            if r.returncode != 0 or not (work / "out.csv").is_file():
                return Check(False, f"failed on {records:,} records: {last_line(r.stderr) or 'no output file'}")
            got, want = rows((work / "out.csv").read_text()), rows(expected(records, seed))
            if got != want:
                if len(got) != len(want):
                    return Check(
                        False,
                        f"different output on {records:,} records: {len(got) - 1:,} customers, not {len(want) - 1:,}",
                    )
                first = next(i for i, (a, b) in enumerate(zip(got, want, strict=True)) if a != b)
                return Check(
                    False,
                    f"different output on {records:,} records, from row {first}: {got[first]} instead of {want[first]}",
                )
    return Check(True, f"same output as before, {HIDDEN_RECORDS:,} records in under {TIME_LIMIT} s")

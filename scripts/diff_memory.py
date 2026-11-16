"""Compare the memory folder with the copy saved before a dream and write REPORT.md.

Usage: python diff_memory.py --before DIR --after DIR --out FILE
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy  # noqa: E402

WITHHELD = "[line about a never-open note withheld]"


def load(folder, ignore):
    folder = Path(folder)
    if not folder.is_dir():
        return {}
    # Same rule as the copies: a symlink is never read, so a shortcut to a
    # never-open note (or to anything else) cannot reach the report.
    return {p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in folder.rglob("*.md") if p.name.lower() not in ignore and privacy.safe_note(p, folder)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ignore", action="append", default=[],
                    help="note file name to leave out (never-open notes); repeatable")
    ap.add_argument("--private-keys", help="JSON file: hashes of never-open note lines")
    a = ap.parse_args()

    ignore = {x.lower() for x in a.ignore}
    pats = privacy.patterns(sorted(ignore))
    keys = set(json.loads(Path(a.private_keys).read_text(encoding="utf-8"))) if a.private_keys else set()

    def hide(line):
        """Keep the report free of lines that describe or copy a never-open note."""
        body = line[1:] if line[:1] in "+- " else line
        if not ((pats and privacy.mentions(line, pats)) or privacy.has_private_text(body, keys)):
            return line
        return (line[0] + WITHHELD) if line[:1] in "+- " and not line.startswith(("+++", "---")) else WITHHELD

    old, new = load(a.before, ignore), load(a.after, ignore)
    if pats:
        # Compare the notes as the agents see them: index items about a
        # never-open note, with their continuation lines, are left out first.
        old = {k: privacy.scrub_text(v, pats)[0] for k, v in old.items()}
        new = {k: privacy.scrub_text(v, pats)[0] for k, v in new.items()}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(n for n in set(old) & set(new) if old[n] != new[n])

    lines = ["# What this dream changed in the memory folder", "",
             "Made by comparing the folder with the copy saved before the run.", "",
             "- Notes added: %d" % len(added),
             "- Notes changed: %d" % len(changed),
             "- Notes removed: %d (should always be 0)" % len(removed), ""]
    index = Path(a.after) / "MEMORY.md"
    if privacy.safe_note(index, a.after):
        raw = index.read_bytes()
        lines.append("- MEMORY.md now: %d lines, %.1f KB (limit 200 lines or 25 KB)"
                     % (len(raw.splitlines()), len(raw) / 1024))
        lines.append("")
    if removed:
        lines += ["## REMOVED (restore these)", ""] + ["- " + n for n in removed] + [""]
    if added:
        lines += ["## Added", ""]
        for n in added:
            body = "\n".join(hide(x) for x in new[n].strip()[:3000].splitlines())
            lines += ["### " + n, "", "```", body, "```", ""]
    if changed:
        lines += ["## Changed", ""]
        for n in changed:
            diff = list(difflib.unified_diff(old[n].splitlines(), new[n].splitlines(),
                                             "before", "after", lineterm="", n=1))
            extra = len(diff) - 80
            lines += ["### " + n, "", "```diff"] + [hide(x) for x in diff[:80]]
            if extra > 0:
                lines.append("... %d more diff lines" % extra)
            lines += ["```", ""]
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("added %d, changed %d, removed %d" % (len(added), len(changed), len(removed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

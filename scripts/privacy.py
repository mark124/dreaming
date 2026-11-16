"""Shared helpers: never-open notes and the saved state file.

Never-open notes are memory notes the agents must never see. The user lists
them in the profile (under "Never open or describe these notes") or in
config.json (`never_open`). This module turns those free-text entries into
the real note file names, and builds patterns that find a mention of any of
them, so the controller, the renderer and the diff all hide the same things.
"""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_CHECKBOX = re.compile(r"^\[[ xX]\]\s+")
_DECOR = "`*'\"_~ "


def norm(name):
    """Compare names without letter case, and with spaces and hyphens read as underscores."""
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def entry_candidates(item):
    """Every note name a free-text entry could mean. Taking all of them hides
    more rather than less when an entry is ambiguous."""
    s = re.sub(r"<!--.*?-->", "", str(item), flags=re.S).strip()
    s = _CHECKBOX.sub("", _BULLET.sub("", s)).strip()
    if not s:
        return set()
    out = set()
    s = s.replace("%20", " ")
    for t in re.findall(r"\[\[([^\]|#]+)", s):                  # [[wiki link]] / [[note#part]]
        out.add(t)
    for t in re.findall(r"\]\(\s*<?([^)>#]+?)(?:#[^)>]*)?>?\s*\)", s):   # [label](target#part)
        out.add(t)
    plain = re.sub(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)", " ", s)
    for t in re.findall(r"([^`*'\"\[\]()<>|,;]+?\.md)(?![\w])", plain, re.I):   # names ending .md
        out.add(t.strip(_DECOR))
    if plain.strip(_DECOR).lower().endswith(".md"):
        out.add(plain.strip(_DECOR))                                # the whole line as one name
    if not re.search(r"\.md(?![\w])", plain, re.I):
        bare = plain.strip(_DECOR)
        head = re.split(r"\s+(?:\(|-|—|–)\s*|\s*[(:]", bare)[0].strip(_DECOR)
        # The whole name, and the name before a description; never just the
        # first word ("my diary" must not mean my.md).
        for t in (bare, head):
            if t:
                out.add(t)
    names = set()
    for t in out:
        t = Path(t.strip(_DECOR).replace("\\", "/")).name.strip(_DECOR)
        if not t:
            continue
        if not t.lower().endswith(".md"):
            t += ".md"
        names.add(t.lower())
    return names


_EMPTY = {"none", "n/a", "na", "-", "nothing", "(none)"}
NEVER_HEADING = re.compile(r"never[\s\-_]*open", re.I)


def read_text_any(path):
    """A small text file as text, whatever editor saved it: UTF-8 (with or
    without a BOM), UTF-16 (PowerShell 5.1's `>`), or else Windows-1252."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def raw_entries(profile, cfg, shared_profile=False):
    """(where, text, strict) for every never-open entry in the config and the profile.

    strict entries must match a note in this project, or the pass stops. A
    profile shared by several projects (~/.claude-dreaming/profile.md) is not
    strict: its entries hide notes where they exist and are ignored elsewhere,
    so one project's private note never blocks another project."""
    items = []
    conf = cfg.get("never_open") or []
    if isinstance(conf, str):
        conf = [conf]
    for i, x in enumerate(conf, 1):
        items.append(("config never_open item %d" % i, str(x), True))
    if profile and Path(profile).exists():
        text = re.sub(r"<!--.*?-->", "", read_text_any(profile), flags=re.S)
        inside, n = False, 0
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                inside = bool(NEVER_HEADING.search(line))
                continue
            if inside and line.strip():
                n += 1
                if _BULLET.sub("", line).strip(_DECOR + ".").lower() in _EMPTY:
                    continue
                items.append(("profile never-open line %d" % n, line, not shared_profile))
    return items


_SPLIT = re.compile(r"\s*(?:,|;|&|\band\b|\bor\b|\+)\s*|\s+/\s+", re.I)
_MD_NAME = re.compile(r"[^\s`*'\"\[\]()<>|,;]+\.md(?![\w])", re.I)


def components(item):
    """The separate names on one entry line: "a.md and b.md", "diary, health".
    A description after " - ", " (" or ":" is not a name."""
    s = re.sub(r"<!--.*?-->", "", str(item), flags=re.S).strip()
    s = _CHECKBOX.sub("", _BULLET.sub("", s)).strip()
    links = re.findall(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)", s)
    rest = re.sub(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)", " ", s)
    rest = re.split(r"\s+(?:-|—|–)\s+|\s*\(|:\s", rest)[0]
    parts = [p.strip(_DECOR) for p in _SPLIT.split(rest) if p.strip(_DECOR)]
    return links + parts


def resolve_never_open(entries, note_names, loose_unmatched=None):
    """Match entries against the real note file names.

    Returns (matched_names, unmatched_where): the real names to hide (lower
    case), and the position of each entry with a name that matched no note.
    Every name on a line must match, so one good name cannot cover a
    misspelt one next to it."""
    by_norm = {}
    for n in note_names:
        by_norm.setdefault(norm(n), set()).add(n.lower())
    matched, unmatched = set(), []
    for entry in entries:
        where, text = entry[0], entry[1]
        strict = entry[2] if len(entry) > 2 else True
        # Everything the whole line could mean is hidden (an unclear line hides more).
        whole = set()
        for c in entry_candidates(text):
            whole |= by_norm.get(norm(c), set())
        matched |= whole
        # A line is settled if it names one note as a whole ("research and
        # development.md"), or if every separate name on it matches a note.
        # A line settles at once only if, as a whole, it is one note's name
        # ("research and development.md", "taxes 2026 (draft).md").
        bare = _CHECKBOX.sub("", _BULLET.sub("", re.sub(r"<!--.*?-->", "", str(text), flags=re.S))).strip(_DECOR)
        line_ok = bool(by_norm.get(norm(bare)) or by_norm.get(norm(bare + ".md")))
        if line_ok:
            matched |= by_norm.get(norm(bare), set()) | by_norm.get(norm(bare + ".md"), set())
        comps = components(text) or [text]
        if not line_ok:
            line_ok = True
            for comp in comps:
                # "diary.md helth.md" is one part with two names: each must match.
                names_in = _MD_NAME.findall(comp)
                rest = _MD_NAME.sub(" ", comp).strip(_DECOR) if names_in else ""
                # Every name-bearing part must match: "diary.md helth" has two.
                pieces = (names_in + ([rest] if rest else [])) if (len(names_in) > 1 or rest) else [comp]
                for piece in pieces:
                    hit = set()
                    for c in entry_candidates(piece):
                        hit |= by_norm.get(norm(c), set())
                    matched |= hit
                    if not hit:
                        line_ok = False
        if not line_ok and strict:
            unmatched.append(where)
        elif not line_ok and loose_unmatched is not None:
            loose_unmatched.append(where)
    return sorted(matched), unmatched


def is_link(q):
    """A symlink, or on Windows any reparse point (a directory junction is not
    reported as a symlink by Python before 3.12)."""
    try:
        if Path(q).is_symlink():
            return True
        attrs = getattr(os.lstat(q), "st_file_attributes", 0)
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def safe_note(p, root):
    """A real file inside root: not a link, no linked folder on the way, and not resolving outside root."""
    try:
        p = Path(p)
        if not p.is_file():
            return False
        q = p
        while q != Path(root) and q != q.parent:
            if is_link(q):
                return False
            q = q.parent
        full = os.path.normcase(str(p.resolve()))
        base = os.path.normcase(str(Path(root).resolve()))
        return full.startswith(base.rstrip("\\/") + os.sep)
    except OSError:
        return False


def safe_dest(p, root):
    """A path inside root that may be written: no symlink on the way from root
    to it (checked even when the file does not exist yet), and it resolves
    inside root."""
    try:
        p, root = Path(p), Path(root)
        if is_link(root):
            return False  # the memory folder itself was swapped for a link
        rel = p.relative_to(root)
        q = root
        for part in rel.parts:
            q = q / part
            if is_link(q):
                return False
        # Resolve the nearest folder that exists (a path that does not exist
        # yet is not resolved through a junction on every Python version).
        existing = p
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        full = os.path.normcase(str(existing.resolve() / p.relative_to(existing)))
        base = os.path.normcase(str(root.resolve()))
        return full.startswith(base.rstrip("\\/") + os.sep)
    except (OSError, ValueError):
        return False


def _line_key(line):
    return hashlib.sha256(re.sub(r"\s+", " ", line).strip().lower().encode("utf-8")).hexdigest()


def fingerprints(paths, min_len=12):
    """Hashes of the longer lines of the given notes (never the text itself),
    so a renderer can recognise a never-open note's text in a tool result
    that does not name it (cat memory/*.md, a grep, a script's output)."""
    keys = set()
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("description:"):
                line = line[len("description:"):].strip().strip("\"'")   # the note's own summary
            for form in {line, _strip_marks(line)}:
                if len(form) >= min_len and not form.startswith(("---", "name:", "metadata:", "type:")):
                    keys.add(_line_key(form))
    return keys


_MARKS = re.compile(r"^(?:[>+\-*#|]+\s*|\d+[.)]\s+)+")


def _strip_marks(s):
    """A line without quote, diff, bullet or heading marks and surrounding quotes."""
    return _MARKS.sub("", s.strip()).strip().strip("\"'`").strip()


_PREFIX = re.compile(r"^\s*(?:\d+\s*[\t→:|]\s?|[^\s:]{1,300}?:\d+[:\-]\s?|[^\s:]{1,300}?\.md[:\-]\s?)")


def has_private_text(text, keys):
    """True if any line of text (with a line-number or grep prefix removed) is a line of a never-open note."""
    if not keys:
        return False
    for line in text.splitlines():
        s = line.strip()
        if len(s) < 12:
            continue
        forms = {s, _PREFIX.sub("", line), _strip_marks(s), _strip_marks(_PREFIX.sub("", line))}
        for f in list(forms):
            if f.startswith("description:"):
                forms.add(f[len("description:"):].strip().strip("\"'"))
        # quoted parts inside a command or sentence: echo "the line" >> f
        forms |= {q for q in re.findall(r"\"([^\"]{12,})\"|'([^']{12,})'", s) for q in q if q}
        if any(_line_key(f) in keys for f in forms if f):
            return True
    return False


def _distinctive(stem):
    """A name unusual enough to hide wherever it appears as a word. Plain
    words ("finances", "q2") are hidden only as file names, links and paths."""
    return len(stem) >= 12 or bool(re.search(r"[_\-]", stem))


def patterns(names):
    """Regexes that find a mention of any of the given note file names."""
    pats = []
    for name in names:
        stem = name[:-3] if name.lower().endswith(".md") else name
        s = re.escape(stem)
        pats.append(re.compile(r"(?<![\w.\-])%s(?![\w\-])" % re.escape(name), re.I))
        pats.append(re.compile(r"\[\[\s*%s\s*(?:[|#][^\]]*)?\]\]" % s, re.I))
        pats.append(re.compile(r"\]\(\s*<?(?:[^)\s]*/)?%s(?:\.md)?(?:#[^)>\s]*)?>?\s*\)"
                               % s.replace(r"\ ", r"(?:\ |%20)"), re.I))
        if _distinctive(stem):
            pats.append(re.compile(r"(?<![\w\-])%s(?![\w\-])" % s, re.I))
        else:
            # A short plain name ("diary") is too common a word to hide in
            # prose; it is still caught as a path part (memory/diary).
            pats.append(re.compile(r"(?<=\w)[/\\]%s(?![\w\-])" % s, re.I))
    return pats


def mentions(text, pats):
    return any(p.search(text) for p in pats)


_SEG = re.compile(r"(\s+·\s+)")
_LINK = re.compile(r"\]\(|\[\[")


def scrub_line(line, pats):
    """Remove the part of an index line that points at a never-open note.

    On a line whose parts are joined by a middle dot ("- [A](a.md) - x . [B](b.md)
    - y"), the matching part goes, together with any following parts that
    carry no link of their own (they describe the same note); the rest is kept
    with its original separators. Any other line that mentions a never-open
    note, including a table row, is dropped whole. Returns the new line, or None."""
    if not mentions(line, pats):
        return line
    end = "\n" if line.endswith("\n") else ""
    body = line[:-1] if end else line
    if body.lstrip().startswith("|"):
        return None
    m = _BULLET.match(body)
    lead = m.group(0) if m else ""
    pieces = _SEG.split(body[len(lead):])        # [part, sep, part, sep, part]
    parts, seps = pieces[0::2], pieces[1::2]
    if len(parts) == 1:
        return None
    keep, dropping = [], False
    for i, part in enumerate(parts):
        if mentions(part, pats):
            dropping = True
            continue
        if dropping and not _LINK.search(part):
            continue
        dropping = False
        keep.append((seps[i - 1] if i and keep else "", part))
    if not keep:
        return None
    return lead + "".join(sep + part for sep, part in keep) + end


def scrub_text(text, pats):
    """Apply scrub_line to every line. Returns (text, number of lines changed or dropped)."""
    out, n = [], 0
    in_private = False
    for line in text.splitlines(keepends=True):
        # Indented lines under an item that pointed at a never-open note
        # continue it (blank lines in between do not end it) and go too.
        if in_private and (not line.strip() or line[:1] in " \t"):
            if line.strip():
                n += 1
            else:
                out.append(line)
            continue
        new = scrub_line(line, pats)
        in_private = new != line           # dropped whole, or a part of it was cut
        if new != line:
            n += 1
        if new is not None:
            out.append(new)
    return "".join(out), n


def strip_never_open_section(profile_text):
    """The profile without its never-open list, for the agents' copy."""
    out, inside = [], False
    for line in profile_text.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            inside = bool(NEVER_HEADING.search(line))
            if inside:
                out.append(line)
                out.append("(Those notes are already left out of everything you are given.)\n")
                continue
        if not inside:
            out.append(line)
    return "".join(out)


# ---------------------------------------------------------------- state file

class StateError(Exception):
    pass


def load_state(path):
    """Read and check state.json. {} if it does not exist; StateError if damaged."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        st = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(st, dict):
            raise ValueError("not a JSON object")
        offsets = st.get("offsets") if st.get("offsets") is not None else {}
        if not isinstance(offsets, dict) or not all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for k, v in offsets.items()):
            raise ValueError("offsets must map file paths to byte positions")
        ids = st.get("withheld_ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(x, str) for x in ids)):
            raise ValueError("withheld_ids must be a list of strings")
        if st.get("floor") is not None:
            datetime.strptime(str(st["floor"])[:16], "%Y-%m-%dT%H:%M")
        st["offsets"] = offsets
        return st
    except (OSError, ValueError) as e:
        raise StateError(
            "The state file is damaged (%s): %s\nIt records how far each transcript was read. "
            "Restore it from a backup, or delete it to start again from first_run_days "
            "(older text is then never read)." % (e, path))

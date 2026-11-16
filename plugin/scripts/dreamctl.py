"""Dreaming controller: the non-AI steps of a dreaming pass, for one project.

A dreaming pass reads new Claude Code session transcripts for a project, has
cheap analyzer agents compare them with the project's memory notes, and has
one organizer agent propose (or make) careful corrections. This script does
everything around the agents: paths, settings, snapshots, state and reports.

  python dreamctl.py paths                 show where everything lives for this project
  python dreamctl.py init                  create the settings and profile files (no overwrite)
  python dreamctl.py prepare [--apply]     render new transcript text, copy and snapshot memory
  python dreamctl.py finish RUN            check the agents' work, advance state, optional archive
  python dreamctl.py pending               one line if proposals are waiting (SessionStart hook)
  python dreamctl.py snapshot RUN          before applying reviewed proposals: snapshot memory
  python dreamctl.py reviewed RUN --decision applied|discarded|partial [--note TEXT]
  python dreamctl.py restore RUN [--review] [--force]
                                           undo an apply run's (or a review's) edits, where safe
  python dreamctl.py nightly               prepare (propose) + headless claude + finish

Every command takes --project DIR (default: current folder). Per-project files
live in ~/.claude-dreaming/<project-slug>/ unless config.json says otherwise.

Run folder layout:
  RUN/            controller files: run.json, state, memory snapshot, reports
  RUN/agent/      the only part the agents are given: transcripts/, findings/,
                  memory_view/ (a copy of the notes without the never-open notes
                  or lines that point at them), profile.md, register_view,
                  and the organizer's PROPOSALS.md or CHANGES.md
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(HERE))
import privacy  # noqa: E402
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
# Outside ~/.claude on purpose: Claude Code protects that folder, so agents
# could not write their findings there. Override with DREAMING_HOME.
DREAM_HOME = Path(os.environ.get("DREAMING_HOME") or Path.home() / ".claude-dreaming")

DEFAULTS = {
    "memory_dir": None,          # default: <store>/memory
    "runs_dir": None,            # default: <home>/runs
    "state_file": None,          # default: <home>/state.json
    "register": None,            # optional project register file the organizer may update
    "extra_stores": [],          # more transcript folders to read (other projects feeding this memory)
    "never_open": [],            # note file names agents must never see (added to the profile's list)
    "archive_dest": None,        # optional backup folder for processed transcripts (zip)
    "archive_dest_name": None,
    "prune_after_days": None,    # null = never delete local transcripts (extra_stores are never pruned)
    "first_run_days": 2,
    "budget_kb": 1500,
    "lead_model": "sonnet",
    "max_notes_changed": 30,
    "nightly_timeout_min": 180,
}


class ConfigError(Exception):
    pass


def slug(path):
    """Claude Code's folder name for a project: every non-alphanumeric becomes '-'."""
    s = str(path)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return re.sub(r"[^A-Za-z0-9]", "-", s)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), int(time.time() * 1000)))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_once(path, text):
    """Write a record that must never be replaced later (restore depends on it)."""
    if not Path(path).exists():
        write_atomic(path, text)


def file_hash(path):
    path = Path(path) if path else None
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None


def never_open_list(profile, cfg, memory=None, loose_unmatched=None):
    """The real note file names (lower case) the agents must never see.

    Entries are free text; every name an entry could mean is hidden. With a
    memory folder, entries are matched against the notes in it; without one,
    the plain candidates are returned (used only for display and tests)."""
    # The project's own profile and config are strict; the shared profile's
    # never-open lines always apply too (not strict), even when the project
    # has its own profile.
    shared_file = DREAM_HOME / "profile.md"
    own = profile if profile and Path(profile).resolve() != shared_file.resolve() else None
    entries = privacy.raw_entries(own, cfg, shared_profile=False)
    if shared_file.exists():
        entries += privacy.raw_entries(shared_file, {}, shared_profile=True)
    if memory is None:
        names = set()
        for _, text, _strict in entries:
            names |= privacy.entry_candidates(text)
        return sorted(names)
    present = [p.name for p in Path(memory).rglob("*.md") if privacy.safe_note(p, memory)] \
        if Path(memory).is_dir() else []
    return privacy.resolve_never_open(entries, present, loose_unmatched)


def md_files(folder, skip=()):
    """Every .md file under folder (relative posix path -> Path), minus skipped names (case-insensitive)
    and minus symlinks or files that resolve outside the folder."""
    folder = Path(folder)
    if not folder.is_dir():
        return {}
    skip = {s.lower() for s in skip}
    return {p.relative_to(folder).as_posix(): p for p in folder.rglob("*.md")
            if p.name.lower() not in skip and privacy.safe_note(p, folder)}


def copy_md(src, dst, skip=()):
    Path(dst).mkdir(parents=True, exist_ok=True)
    for rel, p in md_files(src, skip).items():
        t = Path(dst) / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, t)


def scrub_file(path, pats):
    """In an agent-facing copy, remove the parts of lines that point at a never-open note."""
    if not pats or not Path(path).is_file():
        return 0
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    new, n = privacy.scrub_text(text, pats)
    if n:
        Path(path).write_text(new, encoding="utf-8")
    return n


def scrub_view(view, skip):
    """In the agents' copy of memory, remove index parts and lines that point at a never-open note."""
    pats = privacy.patterns(skip)
    return sum(scrub_file(p, pats) for p in md_files(view).values()) if pats else 0


def hashes(folder, skip=()):
    return {rel: file_hash(p) for rel, p in md_files(folder, skip).items()}


def resolve(project):
    project = Path(project or os.getcwd()).resolve()
    s = slug(project)
    store = CLAUDE_HOME / "projects" / s
    if not store.is_dir() and len(s) > 150:
        # Claude Code shortens long folder names and adds a short hash; match on the start.
        try:
            hits = [d for d in (CLAUDE_HOME / "projects").iterdir()
                    if d.is_dir() and d.name != s and d.name.startswith(s[:150])]
        except OSError:
            hits = []
        if len(hits) == 1:
            store, s = hits[0], hits[0].name
    home = DREAM_HOME / s
    cfg = dict(DEFAULTS)
    cfg_path = home / "config.json"
    if cfg_path.exists():
        try:
            cfg.update(read_json(cfg_path))
        except ValueError as e:
            raise ConfigError("config.json is not valid JSON (%s): %s" % (e, cfg_path))
    profile = home / "profile.md"
    if not profile.exists():
        profile = DREAM_HOME / "profile.md"
    profile = str(profile) if profile.exists() else None
    # One spelling of every store path, so saved positions always match.
    extras = cfg.get("extra_stores") or []
    if isinstance(extras, str):
        extras = [extras]
    if not isinstance(extras, list):
        raise ConfigError("extra_stores in config.json must be a list of folders: %s" % cfg_path)
    extra = [str(Path(x).expanduser().resolve()) for x in extras]
    return {
        "project": str(project),
        "store": str(store),
        "stores": [str(store)] + extra,
        "extra_stores": extra,
        "home": str(home),
        "config_file": str(cfg_path),
        "memory": str(Path(cfg["memory_dir"]) if cfg["memory_dir"] else store / "memory"),
        "runs": str(Path(cfg["runs_dir"]) if cfg["runs_dir"] else home / "runs"),
        "state": str(Path(cfg["state_file"]) if cfg["state_file"] else home / "state.json"),
        "profile": profile,
        "never_open_entries": len(privacy.raw_entries(profile, cfg)),
        # A relative register path is read from the project folder, wherever
        # the controller is started from (the nightly runs from home).
        "register": (str((project / Path(cfg["register"]).expanduser()).resolve())
                     if cfg.get("register") else None),
        "agents": {"analyzer": "dreaming:transcript-analyzer", "organizer": "dreaming:memory-organizer"},
        "config": cfg,
    }


def out(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def py():
    return sys.executable or "python"


def run_py(args, timeout=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([py()] + [str(x) for x in args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=timeout)


class Lock:
    """A simple lock file so two passes never write state at the same moment."""

    def __init__(self, path, wait=30):
        self.path, self.wait = Path(str(path) + ".lock"), wait

    def __enter__(self):
        end = time.time() + self.wait
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 600:
                        self.path.unlink()  # left behind by a crashed pass
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise SystemExit("state is locked by another dreaming pass: %s" % self.path)
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- commands

def cmd_paths(a):
    p = resolve(a.project)
    p["memory_exists"] = Path(p["memory"]).is_dir()
    p["store_exists"] = Path(p["store"]).is_dir()
    p["controller"] = str(Path(__file__).resolve())
    p["python"] = py()
    p["config"] = dict(p["config"], never_open="(%d entries, not shown)" % len(p["config"].get("never_open") or []))
    out(p)
    return 0


PROFILE_TEMPLATE = """# Dreaming profile

Read by the dreaming agents before they judge what is worth remembering.
Keep it short. Plain sentences.

## Who the memory serves
<!-- e.g. "One person, Sam, who runs a two-person design studio across many client projects." -->

## What is worth keeping
- Standing requests and preferences, in the person's own words
- Decisions and the reasons for them
- Facts that were checked, and how
- Mistakes and traps, and the lesson

## Never open or describe these notes
<!-- One note file name per line, e.g. "- secret_project.md".
     These notes, and lines elsewhere that point at them, are left out of the
     copy of memory the agents read. -->

## Other stores of truth
<!-- where current state lives, so memory need not copy it: e.g. "NOW.md", "the project register", "Gmail" -->
"""


def cmd_init(a):
    p = resolve(a.project)
    home = Path(p["home"])
    home.mkdir(parents=True, exist_ok=True)
    made = []
    cfg = Path(p["config_file"])
    if not cfg.exists():
        cfg.write_text(json.dumps(DEFAULTS, indent=1), encoding="utf-8")
        made.append(str(cfg))
    prof = DREAM_HOME / "profile.md"
    if not prof.exists() and not (home / "profile.md").exists():
        prof.write_text(PROFILE_TEMPLATE, encoding="utf-8")
        made.append(str(prof))
    out({"created": made, "home": str(home)})
    return 0


def _unfinished(runs):
    runs = Path(runs)
    if not runs.is_dir():
        return []
    return [d.name for d in sorted(runs.iterdir())
            if (d / "run.json").exists() and not (d / "finish.json").exists()]


def _commit_state(state, pending):
    """Write state, merging offsets so an older run can never rewind a newer one.
    A file the renderer had to read again from the start (it shrank: replaced
    or cut) takes the new offset. A damaged state file is never overwritten."""
    state = Path(state)
    with Lock(state):
        try:
            cur = privacy.load_state(state)
        except privacy.StateError as e:
            raise SystemExit(str(e))
        offsets = dict(cur.get("offsets") or {})
        # A reset (the file shrank) wins only if the saved offset is still the
        # one this run saw; if another pass moved it since, the larger wins.
        reset_from = pending.get("reset_from") or {}
        for k, v in (pending.get("offsets") or {}).items():
            if k in reset_from and offsets.get(k, 0) == reset_from[k]:
                offsets[k] = v
            else:
                offsets[k] = max(v, offsets.get(k, 0))
        # Withheld call ids are kept until their result has been seen (then
        # they are dropped); the cap is only a guard against runaway growth.
        done = set(pending.get("consumed_ids") or [])
        ids = [x for x in (cur.get("withheld_ids") or []) if x not in done]
        ids += [x for x in (pending.get("withheld_ids") or []) if x not in ids and x not in done]
        new = {"floor": cur.get("floor") or pending.get("floor"), "offsets": offsets,
               "withheld_ids": ids[-50000:],
               "last_run": pending.get("last_run") or cur.get("last_run")}
        write_atomic(state, json.dumps(new, indent=1))


def cmd_prepare(a):
    p = resolve(a.project)
    cfg = p["config"]
    memory = Path(p["memory"])
    if not memory.is_dir():
        out({"error": "no memory folder", "memory": str(memory)})
        return 2
    # Fail closed: if any never-open entry matches no note, stop before any
    # agent material is made (a misspelt name would otherwise expose the note).
    loose = []
    skip, unmatched = never_open_list(p["profile"], cfg, memory, loose)
    if unmatched:
        out({"error": "never-open entries that match no note: %s. Fix the spelling (or remove the "
                      "entry if the note is gone) and run again. Nothing was read." % "; ".join(unmatched)})
        return 2
    busy = _unfinished(p["runs"])
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    runs = Path(p["runs"])
    runs.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        run = runs / (stamp if n == 1 else "%s_%d" % (stamp, n))
        try:
            run.mkdir()
            break
        except FileExistsError:
            n += 1
    agent = run / "agent"
    (agent / "findings").mkdir(parents=True)
    state = Path(p["state"])
    first_run = not state.exists()

    args = [HERE / "render_transcripts.py"]
    for s in p["stores"]:
        args += ["--store", s]
    for name in skip:
        args += ["--never-open", name]
    if skip:
        # Hashes (never the text) of the never-open notes' lines, so their text
        # is recognised in old tool output that does not name them.
        hidden = [q for q in memory.rglob("*.md") if q.name.lower() in skip and privacy.safe_note(q, memory)]
        others = list(md_files(memory, skip).values())
        # Lines that also appear in ordinary notes are left out, so a common
        # line ("Status: submitted") never hides unrelated output.
        keys = privacy.fingerprints(hidden) - privacy.fingerprints(others)
        keys_file = run / "private_line_hashes.json"
        keys_file.write_text(json.dumps(sorted(keys)), encoding="utf-8")
        args += ["--private-keys", keys_file]
    args += ["--run-dir", run, "--state", state,
             "--first-run-days", a.first_run_days or cfg["first_run_days"],
             "--budget-kb", a.budget_kb or cfg["budget_kb"]]
    try:
        r = run_py(args, timeout=1800)
        failed = r.returncode != 0
        detail = r.stderr[-2000:]
    except subprocess.TimeoutExpired:
        failed, detail = True, "rendering took longer than 30 minutes"
    if failed:
        shutil.rmtree(run, ignore_errors=True)
        out({"error": "render failed", "detail": detail})
        return 1
    shutil.move(str(run / "transcripts"), str(agent / "transcripts"))
    stats = read_json(run / "render_stats.json")
    pending = read_json(run / "pending_state.json")

    if stats["chunks"] == 0:
        _commit_state(state, pending)
        shutil.rmtree(run)
        out({"run": None, "nothing_new": True, "chunks": 0})
        return 0
    if first_run:
        # Save the floor now, so a failed first pass retried days later still
        # starts from the same point and loses nothing.
        _commit_state(state, {"floor": pending["floor"], "offsets": {}})

    pats = privacy.patterns(skip)
    view = agent / "memory_view"
    copy_md(memory, view, skip)
    scrubbed = scrub_view(view, skip)
    copy_md(memory, run / "memory_before", skip)
    reg = p["register"]
    reg_backup = reg_view = None
    if reg and Path(reg).is_dir():
        reg = None  # the register setting names a folder, not a file: ignored (see warnings)
        warn_reg = "the register setting points at a folder, not a file; it was ignored"
    else:
        warn_reg = None
    if reg and Path(reg).name.lower() in skip:
        reg = None  # the register is itself a never-open note: the agents get none
    if reg and any(privacy.is_link(q) for q in (Path(reg), Path(reg).parent)):
        reg = None  # a shortcut (or a linked folder) to somewhere else: never read or written
    if reg and Path(reg).exists():
        suffix = Path(reg).suffix or ".md"
        reg_backup = run / ("REGISTER_before" + suffix)
        reg_view = agent / ("register_view" + suffix)
        shutil.copy2(reg, reg_backup)
        shutil.copy2(reg, reg_view)
        scrubbed += scrub_file(reg_view, pats)
    prof_copy = None
    if p["profile"]:
        # The agents' copy of the profile leaves out the never-open list itself.
        prof_copy = agent / "profile.md"
        prof_text = privacy.strip_never_open_section(privacy.read_text_any(p["profile"]))
        prof_copy.write_text(privacy.scrub_text(prof_text, pats)[0], encoding="utf-8")

    mode = "apply" if a.apply else "propose"
    info = {"run": str(run), "agent_dir": str(agent), "mode": mode, "chunks": stats["chunks"],
            "chunk_files": stats.get("chunk_files", []),
            "rendered_kb": stats["rendered_kb"], "left_for_next_run": stats["left_for_next_run"],
            "memory_view": str(view),
            "memory": str(memory) if mode == "apply" else "none (propose mode: read memory_view only)",
            "memory_real": str(memory),
            # With never-open notes the organizer gets only the scrubbed copy of
            # the register, even in apply mode (the real one may name them).
            "register": (reg if mode == "apply" and not skip else (str(reg_view) if reg_view else None)),
            "register_real": reg, "register_backup": str(reg_backup) if reg_backup else None,
            "profile": str(prof_copy) if prof_copy else None,
            "max_notes_changed": cfg["max_notes_changed"], "agents": p["agents"],
            # With never-open notes, the organizer must not open the real index
            # or register (they may name them): in apply mode it writes index
            # and register changes into CHANGES.md for the user instead.
            "index_editable": mode == "apply" and not skip,
            "transcripts": str(agent / "transcripts"), "findings": str(agent / "findings"),
            "project": p["project"], "state_file": str(state), "stores": p["stores"],
            "extra_stores": p["extra_stores"], "never_open": skip,
            "state_hash": file_hash(state)}
    write_atomic(run / "run.json", json.dumps(info, indent=1))
    shown = {k: v for k, v in info.items()
             if k not in ("never_open", "state_hash", "chunk_files", "memory_real", "register_real",
                          "register_backup", "stores", "extra_stores", "state_file", "project")}
    shown["never_open_count"] = len(skip)
    shown["lines_pointing_at_never_open_notes_left_out"] = scrubbed
    warn = []
    if busy:
        warn.append("earlier pass(es) never finished: %s" % ", ".join(busy))
    if warn_reg:
        warn.append(warn_reg)
    if loose:
        warn.append("%d never-open line(s) in the shared profile match no note in this project. That is "
                    "normal for another project's notes, but check the spelling: a misspelt name hides "
                    "nothing. (Lines in this project's own profile are checked strictly.)" % len(loose))
    if stats.get("events_skipped_as_unreadable"):
        warn.append("%d transcript event(s) could not be read and were skipped"
                    % stats["events_skipped_as_unreadable"])
    if warn:
        shown["warnings"] = warn
    out(shown)
    return 0


def _run_info(run):
    run = Path(run)
    f = run / "run.json"
    if not f.exists() and (run.parent / "run.json").exists():
        run, f = run.parent, run.parent / "run.json"  # given the agent folder
    if not f.exists():
        raise SystemExit("Not a dreaming run folder: %s" % run)
    return run, read_json(f)


def _diff(before, after, report, skip):
    args = [HERE / "diff_memory.py", "--before", before, "--after", after, "--out", report]
    for s in skip:
        args += ["--ignore", s]
    keys = Path(report).parent / "private_line_hashes.json"   # made by prepare, in the run folder
    if keys.exists():
        args += ["--private-keys", keys]
    r = run_py(args)
    m = re.match(r"added (\d+), changed (\d+), removed (\d+)", r.stdout.strip())
    counts = tuple(int(x) for x in m.groups()) if (r.returncode == 0 and m) else None
    return counts, r.stdout.strip() or r.stderr.strip()[-300:]


def cmd_finish(a):
    run, info = _run_info(a.run)
    agent = Path(info["agent_dir"])
    try:
        done = (run / "finish.json").exists() and read_json(run / "finish.json").get("state_advanced")
    except (OSError, ValueError, AttributeError):
        done = False  # a damaged finish record: check the run again
    if done:
        out({"run": str(run), "error": "this run was already finished"})
        return 1
    p = resolve(info.get("project") or a.project)
    cfg = p["config"]
    skip = info.get("never_open", [])
    warnings, problems = [], []

    # 1. Did every chunk get a findings file in the expected format?
    missing = []
    for name in info.get("chunk_files", []):
        f = agent / "findings" / (Path(name).stem + ".md")
        body = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        if "# Findings" not in body:
            missing.append(name)
    if len(missing) > a.allow_missing:
        problems.append("%d chunk(s) have no proper findings file: %s" % (len(missing), ", ".join(missing[:5])))
    elif missing:
        warnings.append("advanced without findings for: " + ", ".join(missing))

    # 2. Did the organizer write the right report for the mode?
    want = agent / ("CHANGES.md" if info["mode"] == "apply" else "PROPOSALS.md")
    if not want.exists() or not want.read_text(encoding="utf-8", errors="replace").strip():
        problems.append("organizer wrote no %s" % want.name)

    # 3. What happened to the memory?
    real = Path(info["memory_real"])
    counts, diff_line = _diff(run / "memory_before", real, run / "REPORT.md", skip)
    if counts is None:
        problems.append("memory diff failed: " + diff_line)
    result = {"run": str(run), "mode": info["mode"], "chunks": info["chunks"], "diff": diff_line}
    if info["mode"] == "propose":
        if counts and any(counts):
            warnings.append("the real memory folder changed while this propose pass ran (the agents "
                            "were given only a copy, so this was most likely another session). "
                            "See REPORT.md. Nothing was undone.")
    else:
        if counts and counts[2]:
            warnings.append("%d note(s) were removed; restore RUN puts them back" % counts[2])
        if counts and counts[0] + counts[1] > info["max_notes_changed"]:
            warnings.append("%d notes added or changed, over the limit of %d; check REPORT.md"
                            % (counts[0] + counts[1], info["max_notes_changed"]))
        if problems:
            warnings.append("notes may have been edited by this unfinished apply pass; run "
                            "restore RUN --force before the next pass (it also puts back any edits "
                            "other sessions made to those notes since), or the same text is applied twice")

    ok = not problems
    if ok and info["mode"] == "apply":
        # What the run left: recorded at the successful finish, the end of the
        # run. Restore relies on it to tell the run's edits from later ones.
        write_atomic(run / "after.json", json.dumps({
            "memory": hashes(real, skip),
            "register": file_hash(info.get("register_real"))}, indent=1))
    if ok:
        if file_hash(info["state_file"]) != info.get("state_hash"):
            warnings.append("another pass finished while this one ran; offsets were merged")
        _commit_state(info["state_file"], read_json(run / "pending_state.json"))
        result["state_advanced"] = True
        if cfg.get("archive_dest"):
            result["archive"] = _archive(p, info, run, warnings)
    else:
        result["state_advanced"] = False
        result["why"] = "; ".join(problems)
        result["next"] = "the same transcript text will be tried again next run"
    if warnings:
        result["warnings"] = warnings
    write_atomic(run / "finish.json", json.dumps(result, indent=1))
    out(result)
    return 0 if ok else 1


def _archive(p, info, run, warnings):
    cfg = p["config"]
    main = [s for s in info["stores"] if s not in info.get("extra_stores", [])]
    jobs = [(main, cfg.get("prune_after_days")), (info.get("extra_stores", []), None)]
    lines = []
    for stores, prune in jobs:
        if not stores:
            continue
        args = [HERE / "archive_sessions.py"]
        for s in stores:
            args += ["--store", s]
        args += ["--state", info["state_file"], "--dest", cfg["archive_dest"]]
        if cfg.get("archive_dest_name") and stores is main:
            args += ["--dest-name", cfg["archive_dest_name"]]
        args += ["--no-prune"] if prune is None else ["--prune-after-days", prune]
        try:
            ar = run_py(args, timeout=1800)
        except subprocess.TimeoutExpired:
            warnings.append("archive step timed out after 30 minutes")
            continue
        with open(run / "archive_log.txt", "a", encoding="utf-8") as fh:
            fh.write(ar.stdout + ar.stderr)
        last = (ar.stdout.strip().splitlines() or [""])[-1]
        if ar.returncode != 0:
            warnings.append("archive step failed: " + (last or ar.stderr.strip()[-200:]))
        lines.append(last)
    return " | ".join(lines)


def _review_done(run):
    """A run's review is done when REVIEWED.md records applied or discarded;
    a partial review stays waiting so it can be resumed."""
    f = Path(run) / "REVIEWED.md"
    if not f.exists():
        return False
    first = f.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    return not (first and first[0].strip() == "decision: partial")


def _waiting(p):
    runs = Path(p["runs"])
    if not runs.is_dir():
        return []
    w = []
    for d in sorted(runs.iterdir()):
        if not (d / "agent" / "PROPOSALS.md").exists() or _review_done(d):
            continue
        try:
            if not read_json(d / "finish.json").get("state_advanced"):
                continue
        except (OSError, ValueError):
            continue
        w.append(d)
    return w


def _hook_cwd():
    """Claude Code passes hook input as JSON on stdin; it names the session's folder."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return json.loads(raw).get("cwd") if raw.strip() else None
    except Exception:
        return None


def cmd_pending(a):
    try:
        p = resolve(a.project or _hook_cwd())
        if not Path(p["memory"]).is_dir():
            return 0
        w = _waiting(p)
        if a.json:
            out({"waiting": [{"run": str(d), "proposals": str(d / "agent" / "PROPOSALS.md")} for d in w],
                 "never_open_entries": p["never_open_entries"]})
        elif w:
            print("Dreaming: %d proposal run%s waiting for review for this project (newest: %s). "
                  "Mention this to the user once; /dreaming:dream-review shows them."
                  % (len(w), "" if len(w) == 1 else "s", w[-1].name))
    except Exception:
        pass  # a hook must never break a session
    return 0


def cmd_snapshot(a):
    """Before review edits: copy the current memory (and register) into the run."""
    run, info = _run_info(a.run)
    skip = info.get("never_open", [])
    snap = run / "review_before"
    if snap.exists():
        if (run / "REVIEWED.md").exists() and not _review_done(run):
            # Resuming a partial review: keep the first snapshot, so restore
            # --review still undoes every sitting together.
            out({"run": str(run), "snapshot": str(snap), "resumed": True})
            return 0
        out({"error": "a review snapshot already exists for this run; it is kept so that review can "
                      "still be undone (restore RUN --review). Nothing done.", "snapshot": str(snap)})
        return 1
    copy_md(info["memory_real"], snap / "memory", skip)
    reg = info.get("register_real")
    if reg and Path(reg).exists() and not any(privacy.is_link(q) for q in (Path(reg), Path(reg).parent)):
        shutil.copy2(reg, snap / "register_copy")
    out({"run": str(run), "snapshot": str(snap)})
    return 0


def cmd_reviewed(a):
    run, info = _run_info(a.run)
    skip = info.get("never_open", [])
    res = {"run": str(run), "decision": a.decision}
    snap = run / "review_before"
    if snap.exists():
        counts, line = _diff(snap / "memory", info["memory_real"], run / "REVIEW_REPORT.md", skip)
        res["review_diff"] = line
        # Rewritten at every reviewed call, so a review done in two sittings
        # can still be undone as a whole.
        write_atomic(snap / "after.json", json.dumps({
            "memory": hashes(info["memory_real"], skip),
            "register": file_hash(info.get("register_real"))}, indent=1))
    (run / "REVIEWED.md").write_text(
        "decision: %s\nwhen: %s\n%s\n" % (a.decision, datetime.now(timezone.utc).isoformat(timespec="minutes"),
                                         ("note: " + a.note) if a.note else ""), encoding="utf-8")
    out(res)
    return 0


def _named_in_report(path):
    """A test for "does the run's report say it changed this file?".

    Only the part of the report before its "Not applied" section counts, and a
    name must stand alone (plan.md is not named by master_plan.md). MEMORY.md,
    the index, always counts: a run that adds or changes notes edits it too."""
    text = Path(path).read_text(encoding="utf-8", errors="replace") if Path(path).exists() else ""
    m = re.search(r"(?im)^\s*(?:#+\s*|\*\*)?not applied", text)
    text = text[:m.start()] if m else text

    def named(rel):
        """rel: the note's path relative to the memory folder (or a bare name)."""
        rel = str(rel).replace("\\", "/")
        base = rel.rsplit("/", 1)[-1]
        if base.lower() == "memory.md":
            return True
        for n in {rel, base}:
            # A name stands alone or ends a path (sub/deep.md, C:\...\deep.md),
            # but is not the tail of a longer name (master_plan.md).
            if re.search(r"(?<![\w.\-])%s(?![\w\-])" % re.escape(n).replace("/", r"[/\\]"), text, re.I):
                return True
        return False
    return named


def cmd_restore(a):
    """Put back notes an apply run (or a review) changed, but only where nothing changed since.

    Only notes named in the run's CHANGES.md (or, for a review, in PROPOSALS.md)
    are touched, unless --force is given; a whole-folder comparison cannot tell
    the run's edits from another session's.
    """
    run, info = _run_info(a.run)
    agent = Path(info["agent_dir"])
    skip = info.get("never_open", [])
    if a.review:
        before, after_file = run / "review_before" / "memory", run / "review_before" / "after.json"
        reg_before = run / "review_before" / "register_copy"
        named_in = agent / "PROPOSALS.md"
    else:
        if info["mode"] == "propose":
            out({"error": "a propose run never edits notes, so there is nothing to undo. "
                          "To undo applied proposals use: restore RUN --review"})
            return 1
        before, after_file = run / "memory_before", run / "after.json"
        reg_before = Path(info["register_backup"]) if info.get("register_backup") else None
        named_in = agent / "CHANGES.md"
    if not before.is_dir():
        out({"error": "no snapshot for this %s; nothing done" % ("review" if a.review else "run")})
        return 1
    if after_file.exists():
        after = read_json(after_file)
    elif a.force:
        after = None  # the pass died before finish: restore from the snapshot alone
    else:
        out({"error": "no record of what the %s left (it stopped before %s). Compare with the "
                      "snapshot in %s, or run restore again with --force to put back every note that "
                      "differs from it (this also undoes any other edits made since)."
                      % ("review" if a.review else "run", "reviewed" if a.review else "finish", before)})
        return 1
    named = _named_in_report(named_in)
    memory = Path(info["memory_real"])
    backup = run / ("restore_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    restored, moved_out, skipped, not_named = [], [], [], []
    before_files = md_files(before, skip)
    names = set(after["memory"] if after else ()) | set(before_files) | set(md_files(memory, skip))
    for rel in sorted(names):
        cur = memory / rel
        if not privacy.safe_dest(cur, memory):
            skipped.append(rel)  # never write through a shortcut (symlink) to somewhere else
            continue
        cur_h = file_hash(cur)
        bef = before_files.get(rel)
        already = (cur_h == file_hash(bef)) if bef else (cur_h is None)
        if already:
            continue
        if after is not None and cur_h != after["memory"].get(rel) and not a.force:
            skipped.append(rel)  # changed since the run: someone else's work
            continue
        if after is not None and not a.force and not named(rel):
            not_named.append(rel)  # changed during the run, but the run does not claim it
            continue
        if after is None and not bef and rel not in set(md_files(memory, skip)):
            continue
        if cur.exists():
            t = backup / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cur, t)
        if bef:
            cur.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bef, cur)
            restored.append(rel)
        else:
            cur.unlink()  # created by the run; a copy is kept in the backup folder
            moved_out.append(rel)
    reg = info.get("register_real")
    reg_state = None
    if reg and any(privacy.is_link(q) for q in (Path(reg), Path(reg).parent)):
        reg_state = "skipped: the register is now a shortcut (symlink or junction)"
    elif reg and reg_before and Path(reg_before).exists():
        if file_hash(reg) == file_hash(reg_before):
            reg_state = "unchanged"
        elif not a.force and not named(Path(reg).name):
            reg_state = "skipped: the report does not name it"
        elif a.force or (after is not None and file_hash(reg) == after.get("register")):
            backup.mkdir(parents=True, exist_ok=True)
            if Path(reg).exists():
                shutil.copy2(reg, backup / "register_before_restore")
            shutil.copy2(reg_before, reg)
            reg_state = "restored"
        else:
            reg_state = "skipped: changed since"
    out({"restored": restored, "created_by_run_moved_to_backup": moved_out,
         "skipped_changed_since": skipped, "skipped_not_named_in_report": not_named,
         "register": reg_state, "backup": str(backup) if backup.exists() else None,
         "note": ("skipped notes were left alone; --force overwrites them too"
                  if skipped or not_named else "")})
    return 0


def cmd_nightly(a):
    """Unattended pass. Always propose mode; the agents get only the run's agent folder."""
    p = resolve(a.project)
    cfg = p["config"]
    r = run_py([__file__, "prepare", "--project", p["project"]])
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return 1
    info = json.loads(r.stdout)
    if info.get("nothing_new"):
        print("Nothing new to dream on.")
        return 0
    run = Path(info["run"])
    agent = Path(info["agent_dir"])

    def fail(why):
        res = {"run": str(run), "state_advanced": False, "why": why}
        write_atomic(run / "finish.json", json.dumps(res, indent=1))
        out(res)
        return 1

    prompt = (PLUGIN / "scripts" / "lead_prompt.md").read_text(encoding="utf-8")
    for k, v in {"{RUN}": str(agent), "{MEMORY_VIEW}": info["memory_view"],
                 "{MEMORY}": "none (propose mode)", "{REGISTER}": info["register"] or "none",
                 "{PROFILE}": info["profile"] or "none", "{MODE}": "propose",
                 "{CHUNKS}": str(info["chunks"]), "{MAX}": str(info["max_notes_changed"])}.items():
        prompt = prompt.replace(k, v)
    (run / "lead_prompt_filled.md").write_text(prompt, encoding="utf-8")
    env = dict(os.environ)
    claude = shutil.which("claude")
    if not claude:
        return fail("the claude command was not found on PATH")
    # The file-tool guard below needs Claude Code 2.1.314 or later; an older
    # build would ignore it without a word, so refuse to run instead.
    try:
        ver = subprocess.run([claude, "--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", ver or "")
        if not m or tuple(int(x) for x in m.groups()) < (2, 1, 314):
            return fail("Claude Code 2.1.314 or later is needed for the nightly pass (found: %s)"
                        % ((ver or "unknown").strip() or "unknown"))
    except (OSError, subprocess.TimeoutExpired) as e:
        return fail("could not check the Claude Code version: %s" % e)
    # The session runs from an empty folder with only the agent folder added.
    # File tools are not pre-approved by name (a bare "Read" or "Write" rule
    # would allow any path); reads and edits inside the working folders need
    # no approval in acceptEdits mode, and Claude Code's block settings refuse
    # file-tool reads and writes outside them (Claude Code 2.1.314 or later).
    # In -p mode anything that would need approval is denied.
    guard = {"permissions": {"blockReadsOutsideWorkingDirectories": True,
                             "blockWritesOutsideWorkingDirectories": True},
             # The user's own hooks do not run in this unattended session.
             "disableAllHooks": True}
    cmd = [claude, "-p", "--model", cfg["lead_model"], "--permission-mode", "acceptEdits",
           "--allowedTools", "Agent,Task", "--settings", json.dumps(guard),
           # Denied outright, whatever the user's own settings allow: deny
           # rules beat allow rules.
           "--disallowedTools", "Bash,PowerShell,WebFetch,WebSearch,NotebookEdit,Skill,SlashCommand",
           # The user's own MCP servers are not loaded for this session.
           "--strict-mcp-config",
           "--add-dir", str(agent), "--output-format", "json"]
    if os.environ.get("DREAMING_PLUGIN_DIR"):  # testing a plugin that is not installed
        cmd += ["--plugin-dir", os.environ["DREAMING_PLUGIN_DIR"]]
    # One fixed folder, so the nightly session's own transcripts stay apart;
    # emptied first, so nothing a previous run left there (a CLAUDE.md, say)
    # can steer this one.
    work = Path(p["home"]) / "lead_cwd"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    (run / "lead_command.json").write_text(json.dumps(cmd[1:], indent=1), encoding="utf-8")
    try:
        with open(run / "lead_output.json", "w", encoding="utf-8") as fo, \
                open(run / "lead_stderr.txt", "w", encoding="utf-8") as fe:
            lr = subprocess.run(cmd, input=prompt, stdout=fo, stderr=fe, text=True, encoding="utf-8",
                                cwd=str(work), env=env, timeout=60 * float(cfg["nightly_timeout_min"]))
        code = lr.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    except OSError as e:
        return fail("could not start claude: %s" % e)
    print("claude exit %s" % code)
    if code != 0:
        return fail("claude did not finish cleanly (exit %s); see lead_stderr.txt" % code)
    try:
        lead = json.loads((run / "lead_output.json").read_text(encoding="utf-8"))
        if lead.get("is_error"):
            return fail("claude reported an error (%s); see lead_output.json" % lead.get("subtype"))
    except ValueError:
        pass
    fr = run_py([__file__, "finish", str(run), "--project", p["project"]])
    print(fr.stdout)
    return fr.returncode


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn):
        sp = sub.add_parser(name)
        sp.add_argument("--project", help="project folder (default: current folder)")
        sp.set_defaults(fn=fn)
        return sp

    add("paths", cmd_paths)
    add("init", cmd_init)
    sp = add("prepare", cmd_prepare)
    sp.add_argument("--apply", action="store_true", help="organizer edits memory (default: propose only)")
    sp.add_argument("--first-run-days", type=float)
    sp.add_argument("--budget-kb", type=int)
    sp = add("finish", cmd_finish)
    sp.add_argument("run")
    sp.add_argument("--allow-missing", type=int, default=0,
                    help="advance state even if this many chunks have no findings file")
    sp = add("pending", cmd_pending)
    sp.add_argument("--json", action="store_true")
    sp = add("snapshot", cmd_snapshot)
    sp.add_argument("run")
    sp = add("reviewed", cmd_reviewed)
    sp.add_argument("run")
    sp.add_argument("--decision", required=True, choices=["applied", "discarded", "partial"])
    sp.add_argument("--note")
    sp = add("restore", cmd_restore)
    sp.add_argument("run")
    sp.add_argument("--review", action="store_true", help="undo the review's edits instead of the run's")
    sp.add_argument("--force", action="store_true",
                    help="also put back notes changed since, or not named in the report")
    add("nightly", cmd_nightly)
    a = ap.parse_args()
    try:
        return a.fn(a)
    except ConfigError as e:
        if a.cmd == "pending":
            return 0
        out({"error": str(e)})
        return 2


if __name__ == "__main__":
    sys.exit(main())

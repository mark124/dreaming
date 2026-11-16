# Dreaming

Keeps Claude's memory notes true to what actually happened. Over weeks, memory
notes drift: a deadline moves, a supplier changes, a rule gets said once in
passing and never written down. Dreaming reads your past Claude Code session
transcripts, checks each memory note against them, and proposes careful,
evidence-backed corrections for you to approve.

## How it works
1. A small script turns the new part of each session transcript into plain-text
   chunks. Common secret formats are masked (best effort), sub-agent chatter and
   system notices are labelled or dropped, and it remembers where it stopped,
   so nothing is read twice.
2. One cheap analyzer agent (Haiku) per chunk lists what changed: new facts,
   contradictions, details to add, claims to soften, each with a quote.
3. One organizer agent (Sonnet) checks the quotes against the transcript, then
   writes **PROPOSALS.md** (the default) or, if you ask, edits the notes.
4. You review the proposals and apply the ones you agree with.

## Install
In Claude Code:

    /plugin marketplace add mark124/pipeline-reconcile
    /plugin install dreaming@rowset

The first line adds Rowset's plugin catalogue (it also lists pipeline-reconcile); the second installs this plugin. The code lives at github.com/mark124/dreaming.

Then, in a project that has Claude memory notes:
1. `/dreaming:dream-setup` shows where files live and creates the settings
   and the profile. Fill in the profile, including any notes the agents must
   never see.
2. `/dreaming:dream` runs a first pass (it reads the last two days by default)
   and writes proposals.
3. `/dreaming:dream-review` goes through them with you.

## Commands
- `/dreaming:dream`: run a pass now (propose mode). `/dreaming:dream apply`
  lets the organizer edit notes directly.
- `/dreaming:dream-review`: go through waiting proposals and apply the ones
  you approve.
- `/dreaming:dream-setup`: show where files live, create settings and a
  profile, and optionally schedule a nightly propose-only pass.

When proposals are waiting, the next session in that project mentions it in one
line.

## Files
- Per project: `~/.claude-dreaming/<project>/` holds `config.json`,
  `state.json` and `runs/`. It sits outside `~/.claude` because Claude Code
  protects that folder from agent writes. Set `DREAMING_HOME` to move it.
- `~/.claude-dreaming/profile.md`: who the memory serves, what is worth
  keeping, and notes the agents must never see.
- Each run folder holds the controller's records (snapshot, reports) and an
  `agent/` folder: the only material the agents are given.

## Private notes ("Never open")
List notes the agents must never see in the profile, under "Never open or
describe these notes", one per line (or in `never_open` in config.json).
Names are matched without regard to letter case, with or without `.md`, and
as plain names, `[[links]]` or `[label](links)`; an unclear line hides every
note it could mean. In a project's own profile (`~/.claude-dreaming/<project>/profile.md`)
or its `never_open` setting, a line that matches no note stops the pass before
anything is read, so a misspelt name can never expose the note; a line with
several names ("diary and health", "a.md, b.md") must match every one. The
shared profile (`~/.claude-dreaming/profile.md`) serves many projects, so its
lines hide notes where they exist and are skipped elsewhere: put a project's
private notes in that project's own profile or `never_open` to get the
spelling check. The shared list always applies, even when a project has its
own profile. "None" under the heading is ignored. For a listed note:
- it is left out of the agents' copy of memory and of the run's snapshot;
- index and register lines that point at it are cut from the agents' copies
  (on index lines that join several links with a middle dot, only its link
  and the description that follows it; table rows and other lines go whole);
- the list itself is left out of the agents' copy of the profile;
- in old transcripts, a tool call that names it is shown without its input;
  a tool result is withheld whole if its call named the note, or if the result
  names the note or holds one of its lines (checked against hashes of the
  note's longer lines, so `cat memory/*.md` output is caught too); any other
  line that names it or quotes it is replaced;
- lines about it are withheld from REPORT.md;
- in apply mode the organizer does not open the real index or register; it
  lists those changes in CHANGES.md for you to apply.
Notes that are shortcuts (symlinks) are never read, copied, reported or
written to. A plain name shorter than 12 letters with no `_` or `-` (like
`diary` or `therapist`) is caught as `diary.md`, as a link and as a path
part, but not as an ordinary word in a sentence; longer names and names with
`_` or `-` are caught anywhere. This hides names and file text; it cannot hide a private matter
someone described in other words.

## Safety
- Transcripts, findings and notes are treated as data, never as instructions.
  In the rendered transcript, message text is indented and tool names are
  cleaned, so a fake "USER" line inside a web page or tool output cannot pass
  for the person.
- Propose mode gives the agents only the copy of memory. In an interactive
  pass this is enforced by instructions, and `finish` reports any change to
  the real notes.
- The nightly pass runs `claude -p` from an empty folder with only the run's
  `agent/` folder added. File tools are not pre-approved by name, and Claude
  Code's `blockReadsOutsideWorkingDirectories` and
  `blockWritesOutsideWorkingDirectories` settings are turned on for the
  session, so the file tools refuse to read or write outside those two
  folders (needs Claude Code 2.1.314 or later; the pass checks the version and
  stops if it is older). Shell, web, notebook and skill tools are denied for
  the session (a deny rule wins over any allow rule in your own settings),
  your own MCP servers are not loaded, and your own hooks are turned off.
  These limits are Claude Code's own settings; the plugin's tests check that
  the command asks for them, and live runs on a test project worked under
  them, but the plugin cannot prove Claude Code enforces them in every case.
- Apply mode is snapshotted first. It never deletes or renames a note (by
  instruction), and `finish` warns if more notes changed than the limit
  (30 by default).
- `restore RUN` undoes an apply run. It puts back only notes the run's
  CHANGES.md names (before its "Not applied" part) that still hold what the
  run left; notes edited since, or not named, are listed and left alone.
  MEMORY.md counts as named. Notes the run created are moved to a backup
  folder in the run. `restore RUN --review` undoes applied proposals the same
  way (a review marked `partial` stays waiting and can be resumed; undoing
  it undoes every sitting). Whole notes are put back: if another session edited the same note
  while the run was going, that edit is undone too (the replaced text is kept
  in the backup folder). `--force` puts back every changed note, including
  edits made since; it is also the way to undo a pass that died before
  `finish`.
- The transcript position only moves forward after every chunk has a findings
  file and the organizer wrote its report. A transcript that shrinks (was
  replaced) is read again from the start, unless another pass has moved on
  since. A damaged state file stops the pass
  with a message and is never overwritten.
- Secret masking is best effort: common key formats, auth headers, and values
  whose name says they are secret (`password=...`, `"api_key": "..."`). It
  cannot catch a secret that looks like ordinary text.
- Run folders keep the rendered chunks and a copy of the notes, and are not
  deleted automatically; remove old ones by hand when you no longer need to
  undo them. Optional transcript backups (`archive_dest`) are raw zips.
  Deleting local transcripts is off unless you set `prune_after_days`, and is
  never done for `extra_stores`. Before deleting, the zip is checked file by
  file; a session resumed in the moment between that check and the delete
  could lose its newest lines, so only long-idle sessions are pruned.

## Requirements
Claude Code and Python 3.9+ (`python` or `python3` on the PATH). The
session-start reminder is a bash command, as Claude Code runs hooks through
bash (Git Bash on Windows). The nightly pass needs the plugin installed at
user scope and Claude Code 2.1.314 or later.

## Tests
    python tests/test_dreamctl.py   # checks of the non-AI steps, in a temp folder

## License
MIT. Copyright (c) 2026 Rowset LLC. See LICENSE.

# Privacy

**Dreaming collects nothing for Rowset LLC and sends nothing to anyone.** There is no
telemetry, no update check, no analytics and no server. The plugin's scripts import only the
Python standard library and open no network connections. Nothing is sent to Rowset LLC.

## What it reads
- Your own Claude Code session transcripts for the project (`~/.claude/projects/<project>/*.jsonl`,
  plus any `extra_stores` you add), from where the last pass stopped.
- Your own Claude memory notes for that project, the profile you write, and the project register
  if you name one.

Transcripts and notes often hold personal details (names, emails, plans). Dreaming treats them as
yours and keeps them on your machine.

## What goes to Claude
The analysis is done by Claude agents running in **your own Claude Code session**, under your own
Claude account, exactly like any other request you make in Claude Code. The agents are given only
the run's own folder: the rendered transcript chunks and a copy of your memory notes. Before that:
- common secret formats are masked (best effort; see the README);
- notes you list as "never open", and lines that name or quote them, are left out.

How Anthropic handles that traffic is set by your Claude account's terms, not by this plugin.

## What it stores, and where
Everything stays on your machine, under `~/.claude-dreaming/<project>/` (or `DREAMING_HOME`):
- `config.json`, `state.json` (how far each transcript was read) and your profile;
- one folder per pass, holding the rendered chunks, the agents' findings and proposals, a copy of
  your notes from before the pass, and reports.

Nothing is deleted automatically; remove run folders when you no longer need to undo a pass.
If you set `archive_dest`, processed transcripts are zipped to that folder, which you choose.
Local transcripts are deleted only if you turn on `prune_after_days`.

## Contact
Rowset LLC, mark@rowset.co

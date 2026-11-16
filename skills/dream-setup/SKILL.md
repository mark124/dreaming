---
name: dream-setup
description: Set up dreaming for this project - show where its memory and runs live, create the settings and profile files, and (only if the user asks) schedule a nightly propose-only pass. Use when the user asks to set up, configure or schedule dreaming, or asks where dreaming keeps its files.
allowed-tools: Read, Write, Edit, Glob, Bash(python *dreamctl.py*), Bash(python3 *dreamctl.py*)
---

# Set up dreaming

The controller is `${CLAUDE_PLUGIN_ROOT}/scripts/dreamctl.py` (if unfilled: `${CLAUDE_SKILL_DIR}/../../scripts/dreamctl.py`). Run it with `python` or `python3`. Call it CTL.

1. `CTL paths` shows: the project's transcript store, memory folder, dreaming home (settings, state, runs) and profile. If `memory_exists` is false, tell the user this project has no memory notes yet; dreaming has nothing to correct.
2. `CTL init` creates `config.json` in the dreaming home and a shared `profile.md` template if missing. It never overwrites.
3. Help the user fill in `profile.md` in plain sentences: who the memory serves, what is worth keeping, where current state already lives, and any notes the agents must never open (one file name per line under "Never open or describe these notes", below the comment, not inside it). Write only what the user tells you; never guess private details. Put a project's private notes in that project's own profile (`<dreaming home>/profile.md`) or in `never_open`: there a line that matches no note stops the pass, so a misspelt name can never expose the note. The shared profile's never-open lines apply to every project but are not spelling-checked.
4. Settings in `config.json` (explain only the ones the user asks about):
   - `register`: a project register file the organizer may update (optional).
   - `extra_stores`: more transcript folders to read, when several projects feed one memory. A store added later is read only from the project's first pass onward, not further back.
   - `archive_dest`: a backup folder; processed transcripts are zipped there after each pass. `prune_after_days` stays `null` (never delete local transcripts) unless the user explicitly asks; deleting is permanent for anything not backed up, and a pruned session can no longer be resumed.
   - `never_open`: note file names the agents must never see, in addition to the profile's list. They are left out of the memory copy the agents read.
   - `first_run_days`, `budget_kb` (text per pass), `lead_model`, `max_notes_changed`, `nightly_timeout_min`.
   - `clear_api_key_env`: for nightly runs, use the Claude login instead of an `ANTHROPIC_API_KEY` in the environment.
   - `memory_dir`, `runs_dir`, `state_file`: point at an existing setup to adopt its history.
   Save config.json as plain UTF-8 JSON.
5. Nightly pass (only if the user asks for it): the command is `python "<CTL path>" nightly --project "<project folder>"`. It always runs in propose mode, from an empty folder in the dreaming home (`lead_cwd`, so its own sessions are kept apart and never dreamed on), with only the run's `agent` folder added. It needs the plugin installed for the user (user scope), because it does not run inside the project. Show the user the exact scheduler entry for their system and ask before creating it, because it is a standing job:
   - Windows: write a one-line `nightly.cmd` in the dreaming home: `"<python path>" "<CTL path>" nightly --project "<project folder>" >> "%USERPROFILE%\.claude-dreaming\nightly.log" 2>&1`, then `schtasks /Create /TN "Claude_Dreaming_<name>" /SC DAILY /ST 04:30 /TR "\"<path to nightly.cmd>\""`. The wrapper keeps the task command short (schtasks allows 261 characters) and quoted.
   - macOS/Linux: a crontab line such as `30 4 * * * cd ~ && python3 "<CTL path>" nightly --project "<project folder>" >> ~/.claude-dreaming/nightly.log 2>&1`
   Use the plugin's installed path for CTL (`controller` in `CTL paths` output), and note it changes when the plugin updates: re-run setup after an update.
6. Tell the user: proposals from nightly runs wait in the runs folder; a session start in this project mentions them; `/dreaming:dream-review` applies the approved ones.

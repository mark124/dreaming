"""Archive Claude Code session transcripts to a backup folder, then optionally free local space.

For each session file in the given stores:
  1. COPY: if the dream has processed all of it (per state.json), zip the
     session .jsonl plus its sub-agent folder to the archive folder. The zip is
     rebuilt if the session has grown since.
  2. PRUNE: if the session has also been idle for --prune-after-days, the zip
     on the drive opens cleanly and holds every file with the same size and
     CRC, and nothing changed while that was checked, delete the local copy.
     A pruned session can no longer be resumed in Claude Code. Only sessions
     the dream actually read to the end count as processed.

Nothing is deleted without a verified zip. Use --dry-run to see what would happen.

Usage:
  python archive_sessions.py --store DIR [--store DIR ...] --state FILE
         --dest BACKUP_DIR [--dest-name NAME]
         [--prune-after-days 30] [--include-unprocessed] [--no-prune] [--dry-run]
"""
import argparse
import json
import sys
import time
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy  # noqa: E402


def session_files(jsonl):
    """The session file plus everything in its side folder (sub-agent transcripts)."""
    files = [jsonl]
    side = jsonl.with_suffix("")
    if side.is_dir() and not privacy.is_link(side):
        # Shortcuts (symlinks) are never followed, zipped or deleted through.
        files += [p for p in side.rglob("*") if p.is_file() and _no_shortcut(p, side)]
    return files


def _no_shortcut(p, top):
    """True if neither the file nor any folder between it and top is a symlink."""
    q = p
    while q != top and q != q.parent:
        if privacy.is_link(q):
            return False
        q = q.parent
    return True


def crc_of(path):
    crc = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(block, crc)
    return crc & 0xFFFFFFFF


def zip_is_good(zpath, files, base):
    """The zip opens, passes its own check, and holds each local file with the same size and CRC."""
    try:
        with zipfile.ZipFile(zpath) as z:
            if z.testzip() is not None:
                return False
            info = {i.filename: (i.file_size, i.CRC) for i in z.infolist()}
    except (OSError, zipfile.BadZipFile):
        return False
    for f in files:
        if info.get(f.relative_to(base).as_posix()) != (f.stat().st_size, crc_of(f)):
            return False
    return True


def unchanged_since(files, seen):
    """True if no file changed size or time since `seen` was taken (guards a session resumed meanwhile)."""
    for f in files:
        try:
            st = f.stat()
        except OSError:
            return False
        if seen.get(f) != (st.st_size, st.st_mtime):
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", action="append", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--dest-name", help="sub-folder name for this store (default: the store folder name)")
    ap.add_argument("--prune-after-days", type=float, default=30)
    ap.add_argument("--include-unprocessed", action="store_true",
                    help="also copy sessions the dream has not finished (never prunes them)")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    dest_root = Path(a.dest)
    if not Path(dest_root.anchor or "/").exists():
        print("Archive drive not available: %s. Nothing done." % dest_root)
        return 2
    state_path = Path(a.state)
    try:
        offsets = privacy.load_state(state_path).get("offsets") or {}
    except privacy.StateError as e:
        print("%s\nNothing archived or pruned." % e)
        return 2
    now = time.time()
    copied = pruned = freed = 0
    log = []

    for store in a.store:
        store = Path(store)
        dest = dest_root / (a.dest_name or store.name)
        for jsonl in sorted(store.glob("*.jsonl")):
            try:
                size = jsonl.stat().st_size
                processed = offsets.get(str(jsonl), 0) >= size
                if not processed and not a.include_unprocessed:
                    continue
                files = session_files(jsonl)
                newest = max(f.stat().st_mtime for f in files)
            except OSError:
                continue  # moved or deleted while we looked
            idle_days = (now - newest) / 86400
            zpath = dest / (jsonl.stem + ".zip")
            total = sum(f.stat().st_size for f in files)

            fresh = zpath.exists() and zpath.stat().st_mtime >= newest
            if not fresh:
                log.append("copy   %s (%d files, %.0f MB, idle %.1f d)"
                           % (jsonl.stem[:8], len(files), total / 1e6, idle_days))
                if not a.dry_run:
                    dest.mkdir(parents=True, exist_ok=True)
                    tmp = zpath.with_suffix(".zip.part")
                    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                        for f in files:
                            z.write(f, f.relative_to(store).as_posix())
                    tmp.replace(zpath)
                copied += 1

            if a.no_prune or not processed or idle_days < a.prune_after_days:
                continue
            if a.dry_run:
                log.append("prune  %s (would free %.0f MB)" % (jsonl.stem[:8], total / 1e6))
                pruned += 1
                freed += total
                continue
            if privacy.is_link(jsonl.with_suffix("")):
                log.append("KEEP   %s: its side folder is a shortcut (link), left alone" % jsonl.stem[:8])
                continue
            seen = {f: (f.stat().st_size, f.stat().st_mtime) for f in files}
            if not zip_is_good(zpath, files, store):
                log.append("KEEP   %s: zip did not verify, local copy left alone" % jsonl.stem[:8])
                continue
            if session_files(jsonl) != files or not unchanged_since(files, seen) \
                    or offsets.get(str(jsonl), 0) < seen[jsonl][0]:
                log.append("KEEP   %s: session changed during the check, left alone" % jsonl.stem[:8])
                continue
            for f in files:
                f.unlink()
            side = jsonl.with_suffix("")
            if side.is_dir():
                for d in sorted((p for p in side.rglob("*") if p.is_dir()), reverse=True):
                    d.rmdir()
                side.rmdir()
            log.append("pruned %s (freed %.0f MB)" % (jsonl.stem[:8], total / 1e6))
            pruned += 1
            freed += total

    for line in log:
        print(line)
    print("%s%d zipped to %s, %d pruned locally, %.0f MB freed"
          % ("DRY RUN: " if a.dry_run else "", copied, dest_root, pruned, freed / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())

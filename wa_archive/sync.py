"""The one-click path: back up, extract, ingest, prune, open the reader."""

from __future__ import annotations

import getpass
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from .backup import BackupSource
from .selection import domains as wa_domains
from .device import (DeviceError, backup_size, enable_encryption, keychain_get,
                     keychain_set, pick_device, prune_backup, run_backup,
                     tools_installed, install_hint)
from .extract import ingest
from .store import Archive


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _resolve_passphrase(prompt_ok: bool = True) -> str:
    """Keychain first — that's what turns a sync into one click."""
    if os.environ.get("WA_BACKUP_PASSPHRASE"):
        return os.environ["WA_BACKUP_PASSPHRASE"]
    stored = keychain_get()
    if stored:
        return stored
    if not prompt_ok or not sys.stdin.isatty():
        raise DeviceError(
            "No backup passphrase saved. Run `wa-archive sync` once from a "
            "terminal to set it — after that it comes from your Keychain."
        )
    print("\n  No passphrase saved yet. This encrypts the iPhone backup.")
    print("  It will be stored in your macOS Keychain so you only type it once.\n")
    first = getpass.getpass("  Backup passphrase: ")
    again = getpass.getpass("  Confirm: ")
    if first != again:
        raise DeviceError("Those didn't match. Nothing was saved; run sync again.")
    if not first:
        raise DeviceError("An empty passphrase can't be used for an encrypted backup.")
    if keychain_set(first):
        print("  Saved to Keychain.\n")
    else:
        print("  Could not reach the Keychain; you'll be asked again next time.\n")
    return first


def sync(archive_root: Path, udid: str | None = None, business: bool = False,
         prune: bool = True, full: bool = False, keep_backup_at: Path | None = None,
         with_media: bool = True, engine: str = "auto") -> dict:
    """Back up the phone, pull WhatsApp out of it, and fold it into the archive."""
    missing = tools_installed()
    if missing:
        raise DeviceError(install_hint())

    archive_root = Path(archive_root).expanduser()
    backup_root = Path(keep_backup_at).expanduser() if keep_backup_at \
        else archive_root / "device-backup"

    device = pick_device(udid)
    print(f"\n  Device   {device.name} · iOS {device.ios}")

    passphrase = _resolve_passphrase()

    if not device.encrypted_backups:
        print("  Backups are currently unencrypted — WhatsApp data needs encryption.")
        enable_encryption(device, passphrase)

    first_run = not (backup_root / device.udid / "Manifest.db").is_file()
    print(f"  Mode     {'full backup (first run — this takes a while)' if first_run or full else 'incremental'}")
    print(f"  Backup   {backup_root}\n")

    width = 34

    def bar(pct: int) -> None:
        done = int(width * pct / 100)
        print(f"\r  Backing up  [{'█' * done}{'·' * (width - done)}] {pct:3d}%",
              end="", flush=True)

    from . import engine_pmd3

    use_stream = engine == "stream" or (engine == "auto" and engine_pmd3.available())
    if engine == "stream" and not engine_pmd3.available():
        raise DeviceError("The streaming engine needs a newer pymobiledevice3:\n"
                          "    pip install -U pymobiledevice3")

    if use_stream:
        print("  Filter   streaming — non-WhatsApp files are never written to disk")
        backup_dir = engine_pmd3.backup_whatsapp_only(
            backup_root, passphrase, udid=device.udid, business=business,
            with_media=with_media, full=full, on_progress=bar)
        prune = False  # nothing to prune; it was never stored
    else:
        backup_dir = run_backup(device, backup_root, passphrase, full=full, on_progress=bar)
    print(f"\r  Backing up  [{'█' * width}] done" + " " * 8)

    raw_size = backup_size(backup_dir)
    print(f"  On disk  {_fmt(raw_size)}")

    # ---- pull WhatsApp out ----
    staging = Path(tempfile.mkdtemp(prefix="wa-sync-"))
    archive = Archive(archive_root)
    ingest_id = archive.begin_ingest(device.udid, device.name)
    try:
        source = BackupSource(backup_dir, passphrase, business=business)
        print("  Extracting WhatsApp …", end="", flush=True)
        chatstorage = source.extract_chatstorage(staging / "ChatStorage.sqlite")
        media_dir = source.extract_media(staging / "media") if with_media else None
        print(f"\r  Extracting WhatsApp … {_fmt(chatstorage.stat().st_size)} database")

        print("  Reading messages …", end="", flush=True)
        new_msgs, new_media = ingest(chatstorage, media_dir, archive)
        archive.finish_ingest(ingest_id, new_msgs, new_media)
        stats = archive.stats()
    finally:
        archive.close()
        shutil.rmtree(staging, ignore_errors=True)

    print(f"\r  Read      {new_msgs:,} new messages, {new_media:,} new media items")

    # ---- discard everything that isn't WhatsApp ----
    freed = 0
    if prune:
        deleted, freed = prune_backup(backup_dir, wa_domains(business))
        print(f"  Pruned    {deleted:,} non-WhatsApp files, freed {_fmt(freed)}")

    return {
        "device": device.name,
        "first_run": first_run,
        "new_messages": new_msgs,
        "new_media": new_media,
        "freed": freed,
        "backup_bytes": raw_size - freed,
        **stats,
    }


LAUNCHER = """#!/bin/bash
# Double-click this to pull new WhatsApp messages off your iPhone.
# Created by wa-archive. Safe to move anywhere — it does not depend on
# its own location, only on the paths baked in below.
export PYTHONPATH="{pkg_parent}:$PYTHONPATH"
PY="{python}"
ARCHIVE="{archive}"
PORT={port}

clear
echo
echo "  WhatsApp Archive"
echo "  ----------------"
echo "  Connect your iPhone by cable and unlock it."
echo

"$PY" -m wa_archive.cli --archive "$ARCHIVE" sync {flags}
status=$?
echo

if [ $status -ne 0 ]; then
  echo "  Sync stopped (exit $status). The message above says why."
  echo "  Press any key to close."
  read -n 1 -s
  exit $status
fi

echo "  Opening the reader. Close this window when you're finished."
# `serve` opens the browser itself once it is actually listening, which avoids
# the race a fixed `sleep` would leave.
"$PY" -m wa_archive.cli --archive "$ARCHIVE" serve --port "$PORT"
"""


READER_LAUNCHER = """#!/bin/bash
# Double-click to open your WhatsApp archive in a browser.
# Reading only — this never touches your iPhone and never changes the archive.
export PYTHONPATH="{pkg_parent}:$PYTHONPATH"
PY="{python}"
ARCHIVE="{archive}"
PORT={port}

clear
echo
echo "  WhatsApp Archive"
echo "  ----------------"

if [ ! -f "$ARCHIVE/archive.db" ]; then
  echo
  echo "  Cannot find the archive at:"
  echo "    $ARCHIVE"
  echo
  echo "  If it lives on an external drive, plug the drive in and try again."
  echo
  echo "  Press any key to close."
  read -n 1 -s
  exit 1
fi

echo "  Opening in your browser. Close this window when you are finished."
echo
"$PY" -m wa_archive.cli --archive "$ARCHIVE" serve --port "$PORT"
"""


def install_launcher(archive_root: Path, dest: Path | None = None,
                     business: bool = False, no_prune: bool = False,
                     port: int = 8730, reader_only: bool = False,
                     backup_dir: str | None = None) -> Path:
    """Write a double-clickable .command file.

    Two shapes: the full one backs up the phone and then opens the reader; the
    reader-only one just opens the archive. Most days you want the second —
    reading an archive has nothing to do with the phone being plugged in.
    """
    dest = Path(dest).expanduser() if dest else Path.home() / "Desktop"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / ("Open WhatsApp Archive.command" if reader_only
                     else "Sync WhatsApp Archive.command")

    flags = []
    if business:
        flags.append("--business")
    if no_prune:
        flags.append("--keep-backup")

    # Bake in the package location so the launcher works whether or not
    # wa-archive was pip-installed, and from wherever it gets dragged to.
    pkg_parent = Path(__file__).resolve().parent.parent

    if backup_dir:
        flags.append(f'--backup-dir "{backup_dir}"')

    template = READER_LAUNCHER if reader_only else LAUNCHER
    target.write_text(template.format(
        python=sys.executable,
        pkg_parent=str(pkg_parent),
        archive=str(Path(archive_root).expanduser()),
        flags=" ".join(flags),
        port=port,
    ))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return target

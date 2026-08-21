"""Command line interface for the WhatsApp archive ingester."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .backup import BackupSource, discover_backups, unavailable_roots
from .export import FORMATS, export_chat, media_paths
from .extract import ingest, scan_codes
from .query import FTS_OPERATORS, build_fts_query  # noqa: F401  (re-exported)
from .store import Archive

DEFAULT_ARCHIVE = Path.home() / "WhatsAppArchive"


def _passphrase(args) -> str | None:
    """Never accept the passphrase as an argv value — it lands in shell history.

    Same order `sync` uses: environment, then Keychain, then a prompt. `ingest`
    used to skip the Keychain, so it demanded a passphrase you had already
    saved and could not run unattended at all.
    """
    if os.environ.get("WA_BACKUP_PASSPHRASE"):
        return os.environ["WA_BACKUP_PASSPHRASE"]
    if getattr(args, "no_passphrase", False):
        return None

    from .device import keychain_get

    stored = keychain_get()
    if stored:
        return stored
    if not sys.stdin.isatty():
        print("No passphrase available. It is read from the WA_BACKUP_PASSPHRASE\n"
              "environment variable or your Keychain; run `wa-archive sync` once\n"
              "from a terminal to save it.", file=sys.stderr)
        raise SystemExit(1)
    return getpass.getpass("Backup passphrase: ")


def _fmt_ts(ts: int | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"


def _fmt_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} PB"


def cmd_sync(args) -> int:
    """One command: back up the phone, extract WhatsApp, fold it in."""
    from .device import DeviceError
    from .sync import sync

    try:
        r = sync(
            Path(args.archive).expanduser(),
            udid=args.udid,
            business=args.business,
            prune=not args.keep_backup,
            full=args.full,
            keep_backup_at=args.backup_dir,
            with_media=not args.no_media,
            engine=args.engine,
        )
    except DeviceError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 1

    print(f"\n  Archive   {r['messages']:,} messages · {r['chats']:,} chats · "
          f"{r['media']:,} media")
    print(f"            {_fmt_ts(r['earliest'])} → {_fmt_ts(r['latest'])}")
    print(f"            {_fmt_bytes(r['backup_bytes'])} of device backup retained\n")
    if r["first_run"]:
        print("  Next sync will be incremental and much faster.")
        print("  Run `wa-archive install-launcher` for a double-click version.\n")
    return 0


def cmd_install_launcher(args) -> int:
    from .sync import install_launcher

    target = install_launcher(Path(args.archive).expanduser(), args.dest,
                              business=args.business, no_prune=args.keep_backup,
                              port=args.port, reader_only=args.reader_only,
                              backup_dir=args.backup_dir)
    print(f"Created {target}")
    print("Double-click it to " + ("open the archive." if args.reader_only
                                   else "sync, then read."))
    print("macOS blocks unsigned .command files the first time:")
    print("  right-click → Open → Open.")
    return 0


def cmd_forget_passphrase(args) -> int:
    from .device import keychain_clear

    print("Removed from Keychain." if keychain_clear()
          else "Nothing stored in the Keychain.")
    return 0


def _no_backups_message() -> None:
    print("No backups found.", file=sys.stderr)
    for problem in unavailable_roots():
        print(f"\n  {problem}", file=sys.stderr)
    print("\nOtherwise: make a backup in Finder (macOS) or iTunes (Windows) with\n"
          "'Encrypt local backup' ticked, or run `wa-archive sync` with the iPhone\n"
          "connected to have this tool make one for you.", file=sys.stderr)


def cmd_list(args) -> int:
    backups = discover_backups()
    if not backups:
        _no_backups_message()
        return 1
    for b in backups:
        print(b.describe())
        print()
    return 0


def cmd_ingest(args) -> int:
    if args.backup:
        backup_dir = Path(args.backup).expanduser()
        udid, device = backup_dir.name, None
    else:
        backups = discover_backups()
        if not backups:
            _no_backups_message()
            print("\nOr pass --backup with an explicit path to a backup folder.",
                  file=sys.stderr)
            return 1
        if len(backups) > 1 and not args.latest:
            print("Multiple backups found — pick one with --backup, or use --latest:\n")
            for b in backups:
                print(b.describe(), "\n")
            return 1
        chosen = sorted(backups, key=lambda b: b.last_backup or "")[-1]
        backup_dir, udid, device = chosen.path, chosen.udid, chosen.device_name
        print(f"Using backup: {chosen.device_name or udid}  ({chosen.last_backup or '?'})")

    archive = Archive(Path(args.archive).expanduser())
    # Staging holds a decrypted copy of every WhatsApp attachment — tens of GB
    # for a large account. Defaulting it to the system temp dir quietly fills
    # the boot disk on a laptop whose backup lives on an external drive, so it
    # is worth being able to put it next to the backup instead.
    staging_root = Path(args.staging).expanduser() if args.staging else None
    if staging_root:
        staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="wa-staging-", dir=staging_root))
    ingest_id = archive.begin_ingest(udid, device)

    try:
        source = BackupSource(backup_dir, _passphrase(args), business=args.business)

        print("Extracting ChatStorage.sqlite …")
        chatstorage = staging / "ChatStorage.sqlite"
        source.extract_chatstorage(chatstorage)
        print(f"  {_fmt_bytes(chatstorage.stat().st_size)}")

        media_dir = None
        if not args.no_media:
            print("Extracting media (this is the slow part) …")
            media_dir = source.extract_media(staging / "media")

        print("Normalising into the archive …")

        def progress(seen, added):
            print(f"  {seen:,} rows scanned, {added:,} new", end="\r", flush=True)

        n_msg, n_media = ingest(chatstorage, media_dir, archive, progress=progress)
        print(" " * 60, end="\r")

        archive.finish_ingest(ingest_id, n_msg, n_media)
        print(f"\nAdded {n_msg:,} messages and {n_media:,} media items.")
        _print_stats(archive)
        return 0
    finally:
        archive.close()
        if not args.keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
        else:
            print(f"\nStaging kept at {staging}")


def _print_stats(archive: Archive) -> None:
    s = archive.stats()
    print(f"\nArchive: {archive.db_path}")
    print(f"  {s['messages']:,} messages across {s['chats']:,} chats, {s['contacts']:,} contacts")
    print(f"  {s['media']:,} media files ({_fmt_bytes(s['media_bytes'])}), "
          f"{s['media_missing']:,} referenced but not in backup")
    print(f"  {_fmt_ts(s['earliest'])} → {_fmt_ts(s['latest'])}")


def cmd_stats(args) -> int:
    archive = Archive(Path(args.archive).expanduser())
    try:
        _print_stats(archive)
        print("\nBusiest chats:")
        rows = archive.conn.execute(
            "SELECT c.name, c.jid, c.kind, COUNT(m.id) n FROM chats c "
            "JOIN messages m ON m.chat_id = c.id GROUP BY c.id ORDER BY n DESC LIMIT 15"
        ).fetchall()
        for r in rows:
            print(f"  {r['n']:>8,}  [{r['kind']}] {r['name'] or r['jid']}")
        return 0
    finally:
        archive.close()


def cmd_search(args) -> int:
    archive = Archive(Path(args.archive).expanduser())
    query = build_fts_query(args.query, args.exact)
    try:
        rows = archive.conn.execute(
            "SELECT m.ts_utc, m.body, c.name, c.jid, ct.display_name, ct.jid AS sender_jid "
            "FROM messages_fts f "
            "JOIN messages m ON m.id = f.rowid "
            "JOIN chats c ON c.id = m.chat_id "
            "LEFT JOIN contacts ct ON ct.id = m.sender_id "
            "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, args.limit),
        ).fetchall()
        if not rows:
            print("No matches.")
        for r in rows:
            when = dt.datetime.fromtimestamp(r["ts_utc"]).strftime("%Y-%m-%d %H:%M")
            who = r["display_name"] or r["sender_jid"] or "?"
            where = r["name"] or r["jid"]
            body = (r["body"] or "").replace("\n", " ")[:120]
            print(f"{when}  [{where}]  {who}: {body}")
        return 0
    finally:
        archive.close()


def cmd_audit(args) -> int:
    """Check the inferred code mappings against a real ChatStorage file."""
    from .extract import GROUP_EVENTS, MESSAGE_STATUS, MESSAGE_TYPES

    path = Path(args.chatstorage).expanduser()
    for column, table, label in (
        ("ZMESSAGETYPE", MESSAGE_TYPES, "message types"),
        ("ZMESSAGESTATUS", MESSAGE_STATUS, "delivery status"),
        ("ZGROUPEVENTTYPE", GROUP_EVENTS, "group events"),
    ):
        codes = scan_codes(path, column)
        print(f"\n{label}  ({column})")
        if not codes:
            print("  column not present in this database")
            continue
        for code, count in codes:
            name = table.get(code)
            flag = "" if name else "   <- UNMAPPED"
            print(f"  {code:>4}  {count:>9,}  {name or '?'}{flag}")
    print("\nUnmapped codes are stored verbatim as 'unknown:<n>' — nothing is lost.")
    print("Add them to the tables in extract.py and re-ingest to label them.")
    return 0


def cmd_prune(args) -> int:
    """Throw away everything in a device backup that isn't WhatsApp."""
    from .device import DeviceError, backup_size, keychain_get, prune_backup
    from .selection import domains as wa_domains

    backup_dir = Path(args.backup).expanduser()
    if not (backup_dir / "Manifest.db").is_file():
        print(f"No Manifest.db in {backup_dir}.\n"
              "Point --backup at the folder named after your device's UDID.",
              file=sys.stderr)
        return 1

    before = backup_size(backup_dir)
    print(f"Backup   {backup_dir}")
    print(f"Size     {_fmt_bytes(before)}")
    print(f"Keeping  {', '.join(sorted(wa_domains(args.business)))}")
    print("\nEverything outside those domains will be deleted. The backup stays")
    print("usable as an archive but can no longer be restored to an iPhone.")
    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was deleted.")
            return 0

    passphrase = os.environ.get("WA_BACKUP_PASSPHRASE") or keychain_get()
    try:
        deleted, freed = prune_backup(backup_dir, wa_domains(args.business), passphrase)
    except DeviceError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"\nDeleted {deleted:,} files, freed {_fmt_bytes(freed)}.")
    print(f"Backup is now {_fmt_bytes(backup_size(backup_dir))}.")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    try:
        serve(Path(args.archive).expanduser(), host=args.host, port=args.port,
              verbose=args.verbose, open_browser=not args.no_open)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        if exc.errno in (48, 98):        # EADDRINUSE
            print(f"Port {args.port} is already in use — the reader may already be "
                  f"running at http://{args.host}:{args.port}\n"
                  f"Use --port to pick another one.", file=sys.stderr)
            return 1
        raise
    return 0


def cmd_export(args) -> int:
    import sqlite3

    archive = Archive(Path(args.archive).expanduser())
    try:
        archive.conn.row_factory = sqlite3.Row
        targets = ([args.chat] if args.chat else
                   [r[0] for r in archive.conn.execute("SELECT id FROM chats")])
        outdir = Path(args.out).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        written = copied = 0
        for chat_id in targets:
            row = archive.conn.execute(
                "SELECT name, jid FROM chats WHERE id = ?", (chat_id,)).fetchone()
            if not row:
                print(f"No chat with id {chat_id}", file=sys.stderr)
                return 1
            text, _ = export_chat(archive.conn, chat_id, args.format)
            slug = "".join(c if c.isalnum() or c in "-_ " else "_"
                           for c in (row["name"] or row["jid"]))[:60].strip() or str(chat_id)
            dest = outdir / f"{chat_id:04d}-{slug}.{args.format}"
            dest.write_text(text, encoding="utf-8")
            written += 1

            # HTML references attachments relatively, so the export folder has
            # to carry its own copy of them to be worth anything on a USB stick
            # or in five years. Other formats only name the file.
            if args.format == "html":
                for rel in media_paths(archive.conn, chat_id):
                    src, target = archive.media_root / rel, outdir / "media" / rel
                    if src.is_file() and not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, target)
                        copied += 1

        print(f"Wrote {written} file(s) to {outdir}")
        if copied:
            print(f"Copied {copied:,} attachment(s) into {outdir / 'media'}")
        if args.format == "html":
            print("For PDF: open the .html and print to PDF from your browser.")
        return 0
    finally:
        archive.close()


def main(argv=None) -> int:
    from . import __version__

    p = argparse.ArgumentParser(prog="wa-archive", description=__doc__)
    p.add_argument("--version", action="version", version=f"wa-archive {__version__}")
    p.add_argument("--archive", default=str(DEFAULT_ARCHIVE),
                   help=f"archive folder (default: {DEFAULT_ARCHIVE})")
    sub = p.add_subparsers(dest="command", required=True)

    syn = sub.add_parser("sync", help="back up the iPhone and update the archive")
    syn.add_argument("--udid", help="specific device, if more than one is connected")
    syn.add_argument("--business", action="store_true", help="WhatsApp Business")
    syn.add_argument("--full", action="store_true", help="force a full backup")
    syn.add_argument("--no-media", action="store_true", help="messages only")
    syn.add_argument("--keep-backup", action="store_true",
                     help="keep the whole device backup instead of pruning to WhatsApp")
    syn.add_argument("--backup-dir", help="where to keep the device backup")
    syn.add_argument("--engine", choices=("auto", "stream", "full"), default="auto",
                     help="stream: never write non-WhatsApp files; "
                          "full: write everything then prune")
    syn.set_defaults(func=cmd_sync)

    lau = sub.add_parser("install-launcher", help="create a double-clickable sync file")
    lau.add_argument("--dest", help="folder for the launcher (default: Desktop)")
    lau.add_argument("--business", action="store_true")
    lau.add_argument("--keep-backup", action="store_true")
    lau.add_argument("--port", type=int, default=8730)
    lau.add_argument("--reader-only", action="store_true",
                     help="open the archive only — never touches the iPhone")
    lau.add_argument("--backup-dir", help="where the device backup should live")
    lau.set_defaults(func=cmd_install_launcher)

    sub.add_parser("forget-passphrase", help="remove the saved passphrase from Keychain"
                   ).set_defaults(func=cmd_forget_passphrase)

    sub.add_parser("list", help="show local iOS backups").set_defaults(func=cmd_list)

    ing = sub.add_parser("ingest", help="extract WhatsApp data into the archive")
    ing.add_argument("--backup", help="path to a specific backup folder")
    ing.add_argument("--latest", action="store_true", help="auto-pick the newest backup")
    ing.add_argument("--no-media", action="store_true", help="messages only, skip attachments")
    ing.add_argument("--no-passphrase", action="store_true", help="backup is unencrypted")
    ing.add_argument("--business", action="store_true", help="read WhatsApp Business instead")
    ing.add_argument("--keep-staging", action="store_true", help="don't delete decrypted temp files")
    ing.add_argument("--staging", help="where to put decrypted temp files (needs tens of GB; "
                                       "default: system temp, i.e. your boot disk)")
    ing.set_defaults(func=cmd_ingest)

    sub.add_parser("stats", help="summarise the archive").set_defaults(func=cmd_stats)

    srch = sub.add_parser("search", help="full-text search the archive")
    srch.add_argument("query")
    srch.add_argument("--limit", type=int, default=25)
    srch.add_argument("--exact", action="store_true",
                      help="disable automatic prefix matching")
    srch.set_defaults(func=cmd_search)

    au = sub.add_parser("audit", help="check inferred code mappings against a ChatStorage file")
    au.add_argument("chatstorage")
    au.set_defaults(func=cmd_audit)

    prn = sub.add_parser("prune", help="delete non-WhatsApp files from a device backup")
    prn.add_argument("backup", help="path to the backup folder (named after the UDID)")
    prn.add_argument("--business", action="store_true")
    prn.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    prn.set_defaults(func=cmd_prune)

    srv = sub.add_parser("serve", help="open the archive reader in a browser")
    srv.add_argument("--host", default="127.0.0.1",
                     help="default 127.0.0.1 — do not expose this to a network")
    srv.add_argument("--port", type=int, default=8730)
    srv.add_argument("--no-open", action="store_true", help="don't launch a browser")
    srv.add_argument("--verbose", action="store_true", help="log every request")
    srv.set_defaults(func=cmd_serve)

    exp = sub.add_parser("export", help="write chats out as files")
    exp.add_argument("--chat", type=int, help="chat id (default: every chat)")
    exp.add_argument("--format", choices=FORMATS, default="html")
    exp.add_argument("--out", default="./export")
    exp.set_defaults(func=cmd_export)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

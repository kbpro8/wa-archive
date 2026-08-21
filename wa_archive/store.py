"""The archive itself: SQLite writes, the media vault, and dedup.

Two invariants hold everything together.

**Re-ingest is additive.** Every message carries a UNIQUE `source_key`, and
inserts are `INSERT OR IGNORE`. Running a fresh backup over the top of an old
archive adds new rows and touches nothing else, so you can sync every few
months for years.

**Media is content-addressed.** Files land at `media/<aa>/<sha256><ext>`, so the
same forwarded photo across twenty chats occupies disk once. The `media` table
has one row per *reference*; the vault has one file per distinct *content*.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = "1"
_SCHEMA = Path(__file__).with_name("schema.sql")

# Fallback extensions when the original path has none worth keeping.
_KIND_EXT = {"image": ".jpg", "video": ".mp4", "audio": ".m4a", "sticker": ".webp"}


def _safe_ext(orig_path: str | None, kind: str) -> str:
    """Extension for the vault file — never trusted straight from the backup.

    ZMEDIALOCALPATH comes out of the phone's database, so it is treated as
    hostile input: anything that isn't a short alphanumeric suffix is dropped
    in favour of a default for the media kind.
    """
    suffix = Path(orig_path).suffix if orig_path else ""
    cleaned = "".join(ch for ch in suffix if ch.isalnum() or ch == ".").lower()
    if 2 <= len(cleaned) <= 10 and cleaned.startswith(".") and cleaned[1:].isalnum():
        return cleaned
    return _KIND_EXT.get(kind, "")


def _sha256(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


class Archive:
    """Read/write handle on an archive directory.

    Layout::

        <root>/archive.db
        <root>/media/ab/abcdef….jpg
    """

    def __init__(self, root: Path, read_only: bool = False):
        self.root = Path(root).expanduser()
        self.db_path = self.root / "archive.db"
        self.media_root = self.root / "media"
        self.read_only = read_only

        if read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(
                    f"No archive at {self.db_path}. Run `wa-archive sync` (with the "
                    f"iPhone connected) or `wa-archive ingest --latest` first."
                )
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                        check_same_thread=False)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.media_root.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._migrate()

        self.conn.row_factory = sqlite3.Row
        # Caches so a 200k-message ingest doesn't do a lookup per row.
        self._contacts: dict[str, int] = {}
        self._chats: dict[str, int] = {}
        self._participants: set[tuple[int, int]] = set()

    # -- setup ---------------------------------------------------------------

    def _migrate(self) -> None:
        """Apply the schema. Every statement is idempotent, so this doubles as
        the migration path for archives created by an earlier version."""
        self.conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)",
            (str(int(time.time())),),
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            if not self.read_only:
                self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- contacts and chats --------------------------------------------------

    def contact_id(self, jid: str, display_name: str | None = None,
                   is_me: bool = False) -> int:
        """Get or create a contact. A name is only ever filled in, never replaced.

        Backups disagree about display names over the years; the first one that
        isn't NULL wins, so an old export can't blank out a name you already have.
        """
        cached = self._contacts.get(jid)
        if cached is not None:
            if display_name:
                self.conn.execute(
                    "UPDATE contacts SET display_name = ? "
                    "WHERE id = ? AND (display_name IS NULL OR display_name = '')",
                    (display_name, cached),
                )
            return cached

        self.conn.execute(
            "INSERT OR IGNORE INTO contacts(jid, display_name, phone, is_me) "
            "VALUES (?, ?, ?, ?)",
            (jid, display_name, _phone_from_jid(jid), int(is_me)),
        )
        row = self.conn.execute("SELECT id, display_name FROM contacts WHERE jid = ?",
                                (jid,)).fetchone()
        if display_name and not row["display_name"]:
            self.conn.execute("UPDATE contacts SET display_name = ? WHERE id = ?",
                              (display_name, row["id"]))
        self._contacts[jid] = row["id"]
        return row["id"]

    def chat_id(self, jid: str, name: str | None = None, kind: str = "dm",
                archived: bool = False, last_message_at: int | None = None) -> int:
        """Get or create a chat.

        Unlike a message, chat metadata legitimately changes: a group gets
        renamed, new messages arrive. Names are filled in when missing and
        `last_message_at` only ever moves forward.
        """
        cid = self._chats.get(jid)
        if cid is None:
            self.conn.execute(
                "INSERT OR IGNORE INTO chats(jid, name, kind, archived, last_message_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (jid, name, kind, int(archived), last_message_at),
            )
            cid = self.conn.execute("SELECT id FROM chats WHERE jid = ?", (jid,)).fetchone()[0]
            self._chats[jid] = cid

        if name:
            self.conn.execute(
                "UPDATE chats SET name = ? WHERE id = ? AND (name IS NULL OR name = '')",
                (name, cid))
        if last_message_at:
            self.conn.execute(
                "UPDATE chats SET last_message_at = ? "
                "WHERE id = ? AND (last_message_at IS NULL OR last_message_at < ?)",
                (last_message_at, cid, last_message_at))
        return cid

    def link_participant(self, chat_id: int, contact_id: int) -> None:
        key = (chat_id, contact_id)
        if key in self._participants:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO chat_participants(chat_id, contact_id) VALUES (?, ?)",
            key)
        self._participants.add(key)

    # -- messages ------------------------------------------------------------

    def insert_message(self, *, chat_id: int, sender_id: int | None, ts_utc: int,
                       direction: str, type: str, body: str | None,
                       source_key: str, is_starred: int = 0, is_deleted: int = 0,
                       status: str | None = None, event: str | None = None,
                       reply_to_id: int | None = None, raw: dict | None = None) -> int | None:
        """Insert a message. Returns None if this message is already archived.

        The None return is what makes `extract.ingest` skip the media work for
        rows a previous run already handled.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages"
            "(chat_id, sender_id, ts_utc, direction, type, body, is_starred, "
            " is_deleted, status, event, reply_to_id, source_key, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, sender_id, ts_utc, direction, type, body, int(is_starred),
             int(is_deleted), status, event, reply_to_id, source_key,
             json.dumps(raw, separators=(",", ":")) if raw else None),
        )
        if cur.rowcount == 0:
            return None          # already present — a previous ingest got it
        return cur.lastrowid

    # -- media ---------------------------------------------------------------

    def store_media(self, message_id: int, kind: str, source: Path | None,
                    expect_file: bool = True, orig_path: str | None = None,
                    mime: str | None = None, bytes: int | None = None,
                    duration_s: float | None = None, latitude: float | None = None,
                    longitude: float | None = None, caption: str | None = None,
                    vcard: str | None = None) -> int:
        """Record a media reference, copying the file into the vault if we have it.

        `source` is None for attachments that the message references but the
        backup didn't contain (cleared from the phone, or a media-less ingest).
        Those are still recorded, with `missing = 1`, so the reader can show an
        honest placeholder instead of pretending the message had no attachment.
        """
        sha = path = None
        size = bytes
        missing = 0

        if source is not None and Path(source).is_file():
            source = Path(source)
            sha, size = _sha256(source)
            rel = f"{sha[:2]}/{sha}{_safe_ext(orig_path, kind)}"
            dest = self.media_root / rel
            if not dest.exists():                     # dedup: identical bytes stored once
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                shutil.copyfile(source, tmp)
                tmp.replace(dest)                     # atomic: no half-written vault files
            path = rel
        elif expect_file:
            missing = 1

        # ZVCARDSTRING carries the whole contact card for `contact` messages and
        # a mime type for everything else. Split them so vcf export has real
        # cards and `mime` stays a mime type.
        if mime and mime.lstrip().upper().startswith("BEGIN:VCARD"):
            vcard, mime = mime, None

        cur = self.conn.execute(
            "INSERT INTO media(message_id, kind, sha256, path, orig_path, mime, vcard, "
            "                  bytes, duration_s, latitude, longitude, caption, missing) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, kind, sha, path, orig_path, mime, vcard, size, duration_s,
             latitude, longitude, caption, missing),
        )
        return cur.lastrowid

    # -- ingest bookkeeping --------------------------------------------------

    def begin_ingest(self, udid: str | None, device_name: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingests(udid, device_name, started_at) VALUES (?, ?, ?)",
            (udid, device_name, int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def finish_ingest(self, ingest_id: int, new_messages: int, new_media: int) -> None:
        self.conn.execute(
            "UPDATE ingests SET finished_at = ?, new_messages = ?, new_media = ? WHERE id = ?",
            (int(time.time()), new_messages, new_media, ingest_id))
        self.conn.commit()

    # -- reporting -----------------------------------------------------------

    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]
        span = self.conn.execute(
            "SELECT MIN(ts_utc), MAX(ts_utc) FROM messages").fetchone()
        return {
            "messages": one("SELECT COUNT(*) FROM messages"),
            "chats": one("SELECT COUNT(*) FROM chats"),
            "contacts": one("SELECT COUNT(*) FROM contacts"),
            "media": one("SELECT COUNT(*) FROM media WHERE missing = 0"),
            # Distinct hashes, not rows: this is vault size on disk, so it
            # reflects dedup rather than counting a forward twenty times.
            "media_bytes": one(
                "SELECT COALESCE(SUM(bytes), 0) FROM "
                "(SELECT sha256, MAX(bytes) AS bytes FROM media "
                " WHERE sha256 IS NOT NULL GROUP BY sha256)"),
            "media_missing": one("SELECT COUNT(*) FROM media WHERE missing = 1"),
            "earliest": span[0],
            "latest": span[1],
        }


def _phone_from_jid(jid: str) -> str | None:
    """`6281234567890@s.whatsapp.net` -> `+6281234567890`. Groups have no number."""
    local = jid.split("@", 1)[0]
    return f"+{local}" if local.isdigit() else None

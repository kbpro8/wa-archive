"""Read-only localhost API and static host for the reader.

Standard library only — no framework, no build step, no CDN. A single-user
archive browser on 127.0.0.1 does not need more than `ThreadingHTTPServer`, and
every dependency avoided here is one the archive doesn't rot against.

Safety properties worth stating explicitly, because this serves your private
messages over HTTP:

* The database is opened `mode=ro`. The server cannot write to the archive.
* Only GET and HEAD exist.
* Media paths are resolved and checked to be inside the vault, so a crafted
  URL cannot read files elsewhere on the disk.
* The Host header must be a loopback name, which stops a hostile web page from
  DNS-rebinding your browser into reading the archive.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .export import FORMATS, export_chat
from .query import build_fts_query
from .store import Archive

PAGE = 200                      # messages per request
SEARCH_LIMIT = 300
# Sentinels wrapped around a search hit. Control characters rather than
# "<mark>" because the reader HTML-escapes the excerpt *before* swapping
# these for real tags — so a message containing literal "<mark>" text
# cannot inject markup into the results list.
MARK_OPEN, MARK_CLOSE = "\x01", "\x02"

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

# WhatsApp did not exist before 2009, so anything older is a corrupt row rather
# than history. A handful always survive in a large database, and left alone
# they drag the archive's reported date range back to 1969 and put an empty bar
# at the far end of every density rail.
_WHATSAPP_EPOCH = 1_230_768_000          # 2009-01-01


def _summary(conn) -> dict:
    """Headline counts, matching what the chat list actually shows."""
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM messages) AS messages,"
        # Only chats that hold something — the list hides the empty sessions.
        "       (SELECT COUNT(*) FROM chats c WHERE EXISTS"
        "         (SELECT 1 FROM messages m WHERE m.chat_id = c.id)) AS chats,"
        "       (SELECT COUNT(*) FROM media WHERE missing = 0) AS media,"
        "       (SELECT MIN(ts_utc) FROM messages WHERE ts_utc > ?) AS earliest,"
        "       (SELECT MAX(ts_utc) FROM messages) AS latest",
        (_WHATSAPP_EPOCH,),
    ).fetchone()
    return dict(row)


# WhatsApp pins a chat by adding a round multiple of ~1000 years to its
# ZLASTMESSAGEDATE so it sorts to the top. Taken literally that produces chats
# dated 9026, or stored values Python's datetime cannot even format.
#
# The threshold is deliberately far in the future rather than "ahead of the
# newest message". A chat whose recent messages were deleted also has a stored
# date ahead of what survives, and that is not a pin — only the century-scale
# offsets are the sorting hack.
_PIN_THRESHOLD = 4_102_444_800          # 2100-01-01


def _chats(conn) -> list[dict]:
    """Chat list with a preview line, ordered the way WhatsApp orders it.

    `last_message_at` is derived from the messages themselves rather than
    trusted from the chat row, because WhatsApp overloads that column to encode
    pinning. Chats holding no messages are omitted — WhatsApp keeps thousands
    of empty sessions for numbers that were never actually talked to, and they
    are pure noise in a reader.

    The preview is the newest message that actually has text: a chat whose last
    three messages are photos should still show what was being discussed.
    """
    rows = conn.execute(
        "SELECT c.id, c.jid, c.name, c.kind, c.archived,"
        "       (SELECT MAX(m.ts_utc) FROM messages m WHERE m.chat_id = c.id)"
        "         AS last_message_at,"
        "       (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id)"
        "         AS message_count,"
        "       (c.last_message_at > ?) AS pinned,"
        "       (SELECT m2.body FROM messages m2"
        "         WHERE m2.chat_id = c.id AND m2.body IS NOT NULL AND m2.body <> ''"
        "         ORDER BY m2.ts_utc DESC, m2.id DESC LIMIT 1) AS preview"
        " FROM chats c"
        " WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = c.id)"
        " ORDER BY pinned DESC, last_message_at DESC, c.id",
        (_PIN_THRESHOLD,),
    ).fetchall()
    return [dict(r) for r in rows]


_MESSAGE_COLUMNS = (
    "m.id, m.chat_id, m.ts_utc, m.direction, m.type, m.body, m.status, m.event,"
    " m.is_deleted, m.is_starred,"
    " ct.display_name AS sender_name, ct.jid AS sender_jid,"
    " md.kind AS media_kind, md.path AS media_path, md.missing AS missing,"
    " md.caption AS caption, md.bytes AS bytes, md.duration_s AS duration_s,"
    " md.latitude AS latitude, md.longitude AS longitude,"
    # A shared contact has no file — its payload is the vCard text — so the
    # reader needs to know a card exists without being handed the whole thing.
    " (md.vcard IS NOT NULL) AS has_vcard"
)

_MESSAGE_FROM = (
    " FROM messages m"
    " LEFT JOIN contacts ct ON ct.id = m.sender_id"
    " LEFT JOIN media    md ON md.id = (SELECT MIN(id) FROM media WHERE message_id = m.id)"
)


def _anchor(conn, chat_id: int, message_id: int) -> tuple[int, int]:
    row = conn.execute("SELECT ts_utc, id FROM messages WHERE id = ? AND chat_id = ?",
                       (message_id, chat_id)).fetchone()
    if row is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"Message {message_id} is not in this chat.")
    return row["ts_utc"], row["id"]


def _messages(conn, chat_id: int, before: int | None, around: int | None,
              after: int | None = None, oldest: bool = False) -> list[dict]:
    """A window of messages, always returned oldest-first.

    Ordering is `(ts_utc, id)`, never `id` alone: a re-ingest gives newly
    discovered old messages fresh high row-ids, so id order stops matching time
    order the moment you sync twice.

    Four ways in: the newest page (default), the oldest page (`oldest`), the
    page before a message (`before`), the page after one (`after`), or a window
    centred on one (`around`). The forward directions are what let the reader
    read a conversation from its beginning rather than only backwards from now.
    """
    if oldest:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}{_MESSAGE_FROM}"
            " WHERE m.chat_id = ?"
            " ORDER BY m.ts_utc, m.id LIMIT ?",
            (chat_id, PAGE)).fetchall()
        return [dict(r) for r in rows]

    if after is not None:
        ts, mid = _anchor(conn, chat_id, after)
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}{_MESSAGE_FROM}"
            " WHERE m.chat_id = ? AND (m.ts_utc, m.id) > (?, ?)"
            " ORDER BY m.ts_utc, m.id LIMIT ?",
            (chat_id, ts, mid, PAGE)).fetchall()
        return [dict(r) for r in rows]

    if around is not None:
        ts, mid = _anchor(conn, chat_id, around)
        half = PAGE // 2
        older = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}{_MESSAGE_FROM}"
            " WHERE m.chat_id = ? AND (m.ts_utc, m.id) < (?, ?)"
            " ORDER BY m.ts_utc DESC, m.id DESC LIMIT ?",
            (chat_id, ts, mid, half)).fetchall()
        newer = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}{_MESSAGE_FROM}"
            " WHERE m.chat_id = ? AND (m.ts_utc, m.id) >= (?, ?)"
            " ORDER BY m.ts_utc, m.id LIMIT ?",
            (chat_id, ts, mid, half)).fetchall()
        rows = list(reversed(older)) + list(newer)
    elif before is not None:
        ts, mid = _anchor(conn, chat_id, before)
        rows = list(reversed(conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}{_MESSAGE_FROM}"
            " WHERE m.chat_id = ? AND (m.ts_utc, m.id) < (?, ?)"
            " ORDER BY m.ts_utc DESC, m.id DESC LIMIT ?",
            (chat_id, ts, mid, PAGE)).fetchall()))
    else:
        rows = list(reversed(conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}{_MESSAGE_FROM}"
            " WHERE m.chat_id = ?"
            " ORDER BY m.ts_utc DESC, m.id DESC LIMIT ?",
            (chat_id, PAGE)).fetchall()))
    return [dict(r) for r in rows]


def _timeline(conn, chat_id: int) -> list[dict]:
    """Messages per month, with the first message id in each — this is what the
    density rail draws and what makes a bar clickable."""
    rows = conn.execute(
        "SELECT strftime('%Y-%m', ts_utc, 'unixepoch', 'localtime') AS month,"
        "       COUNT(*) AS n, MIN(id) AS first_id"
        " FROM messages WHERE chat_id = ?"
        " GROUP BY month ORDER BY month",
        (chat_id,)).fetchall()
    out = []
    for r in rows:
        # MIN(id) is not necessarily the chronologically first message after a
        # re-ingest, so resolve the real one for the month.
        first = conn.execute(
            "SELECT id FROM messages WHERE chat_id = ?"
            " AND strftime('%Y-%m', ts_utc, 'unixepoch', 'localtime') = ?"
            " ORDER BY ts_utc, id LIMIT 1", (chat_id, r["month"])).fetchone()
        out.append({"month": r["month"], "n": r["n"], "first_id": first["id"]})
    return out


MEDIA_PAGE = 120

# Ordered the way the browser presents its tabs: visual things first, because
# that is what people scan for, then the things you read or listen to.
_MEDIA_KIND_ORDER = ("image", "video", "audio", "document", "link", "sticker",
                     "contact", "location")

# "Links" is a tab in WhatsApp itself, but a link is a property of a message,
# not a file, so it has no row in `media`. It is served through the same
# endpoint anyway so the browser needs no special case.
#
# The source is `type = 'link'` — WhatsApp's own classification, and instant
# thanks to the index. Matching `body LIKE '%http%'` instead would find about
# 15% more (URLs pasted into plain messages that WhatsApp never classified),
# but costs a full scan of 1.2M rows: 1.1s per chat, 3.4s across all of them.
LINK_KIND = "link"


def _like(q: str) -> str:
    """A LIKE pattern that treats the user's text literally.

    `%` and `_` are wildcards in LIKE, so a search for "100%" would otherwise
    match every row. Escaping them (and the escape character itself) keeps a
    typed phrase meaning exactly what it says.
    """
    escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _media_filter(q: str | None) -> tuple[str, list]:
    """SQL fragment matching an attachment against a typed phrase.

    Searches the caption (which for a document is its filename), the message
    text it arrived with, and the original path on the phone.
    """
    if not q or not q.strip():
        return "", []
    needle = _like(q)
    return (" AND (md.caption LIKE ? ESCAPE '\\' OR m.body LIKE ? ESCAPE '\\'"
            "      OR md.orig_path LIKE ? ESCAPE '\\')"), [needle, needle, needle]


def _media_kinds(conn, chat_id: int | None, q: str | None = None) -> list[dict]:
    """How many attachments of each kind exist, for the browser's tabs.

    Counts only what is actually on disk. Offering a tab that opens onto 25,000
    "not in backup" placeholders would be worse than not offering it.
    """
    clause, args = _media_filter(q)
    if chat_id or clause:
        sql = ("SELECT md.kind, COUNT(*) n FROM media md"
               " JOIN messages m ON m.id = md.message_id"
               " WHERE md.missing = 0"
               + (" AND m.chat_id = ?" if chat_id else "") + clause
               + " GROUP BY md.kind")
        args = ([chat_id] if chat_id else []) + args
    else:
        # No filter at all means no reason to touch `messages`. Joining anyway
        # costs a second per call on a 100k-attachment archive.
        sql = "SELECT kind, COUNT(*) n FROM media WHERE missing = 0 GROUP BY kind"
        args = []
    rows = conn.execute(sql, tuple(args)).fetchall()
    counts = {r["kind"]: r["n"] for r in rows}

    link_where = ["m.type = ?"]
    link_args: list = [LINK_KIND]
    if chat_id:
        link_where.append("m.chat_id = ?")
        link_args.append(chat_id)
    if q and q.strip():
        link_where.append("m.body LIKE ? ESCAPE '\\'")
        link_args.append(_like(q))
    n_links = conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE " + " AND ".join(link_where),
        tuple(link_args)).fetchone()[0]
    if n_links:
        counts[LINK_KIND] = n_links
    ordered = [{"kind": k, "n": counts.pop(k)} for k in _MEDIA_KIND_ORDER if k in counts]
    # Anything unexpected still gets a tab rather than being silently dropped.
    ordered += [{"kind": k, "n": n} for k, n in sorted(counts.items())]
    return ordered


def _link_list(conn, chat_id: int | None, offset: int, newest_first: bool,
               q: str | None) -> list[dict]:
    """Link messages, shaped exactly like attachment rows.

    Returning the same field names means the browser renders links through the
    same code path as everything else, with no special case in the client.
    """
    where = ["m.type = ?"]
    args: list = [LINK_KIND]
    if chat_id:
        where.append("m.chat_id = ?")
        args.append(chat_id)
    if q and q.strip():
        where.append("m.body LIKE ? ESCAPE '\\'")
        args.append(_like(q))
    order = "DESC" if newest_first else "ASC"
    args += [MEDIA_PAGE, offset]
    rows = conn.execute(
        "SELECT m.id, 'link' AS kind, NULL AS media_path, NULL AS caption,"
        "       NULL AS orig_path, NULL AS bytes, NULL AS duration_s,"
        "       NULL AS latitude, NULL AS longitude, 0 AS has_vcard,"
        "       m.id AS message_id, m.chat_id, m.ts_utc, m.direction, m.body,"
        "       c.name AS chat_name, c.jid AS chat_jid,"
        "       ct.display_name AS sender_name, ct.jid AS sender_jid"
        " FROM messages m"
        " JOIN chats c ON c.id = m.chat_id"
        " LEFT JOIN contacts ct ON ct.id = m.sender_id"
        " WHERE " + " AND ".join(where) +
        f" ORDER BY m.ts_utc {order}, m.id {order} LIMIT ? OFFSET ?",
        tuple(args)).fetchall()
    return [dict(r) for r in rows]


def _media_list(conn, kind: str | None, chat_id: int | None,
                offset: int = 0, newest_first: bool = True,
                q: str | None = None) -> list[dict]:
    """A page of attachments, newest first by default, with enough context to
    jump back to the message each one came from."""
    if kind == LINK_KIND:
        return _link_list(conn, chat_id, offset, newest_first, q)
    where = ["md.missing = 0"]
    args: list = []
    if kind:
        where.append("md.kind = ?")
        args.append(kind)
    if chat_id:
        where.append("m.chat_id = ?")
        args.append(chat_id)
    clause, qargs = _media_filter(q)
    if clause:
        where.append(clause.removeprefix(" AND "))
        args += qargs
    order = "DESC" if newest_first else "ASC"
    args += [MEDIA_PAGE, offset]
    rows = conn.execute(
        "SELECT md.id, md.kind, md.path AS media_path, md.caption, md.bytes,"
        # The original filename is searchable, so it has to be returnable too:
        # otherwise a hit on the filename looks like an unexplained match.
        "       md.orig_path, md.duration_s, md.latitude, md.longitude,"
        "       (md.vcard IS NOT NULL) AS has_vcard,"
        "       m.id AS message_id, m.chat_id, m.ts_utc, m.direction, m.body,"
        "       c.name AS chat_name, c.jid AS chat_jid,"
        "       ct.display_name AS sender_name, ct.jid AS sender_jid"
        " FROM media md"
        " JOIN messages m ON m.id = md.message_id"
        " JOIN chats    c ON c.id = m.chat_id"
        " LEFT JOIN contacts ct ON ct.id = m.sender_id"
        " WHERE " + " AND ".join(where) +
        f" ORDER BY m.ts_utc {order}, md.id {order} LIMIT ? OFFSET ?",
        tuple(args)).fetchall()
    return [dict(r) for r in rows]


def _search(conn, raw: str, chat_id: int | None) -> dict:
    query = build_fts_query(raw)
    sql = (
        "SELECT m.id, m.chat_id, m.ts_utc, m.direction,"
        "       ct.display_name AS sender_name, ct.jid AS sender_jid,"
        "       c.name AS chat_name, c.jid AS chat_jid,"
        f"      snippet(messages_fts, 0, '{MARK_OPEN}', '{MARK_CLOSE}', '…', 14) AS excerpt"
        " FROM messages_fts f"
        " JOIN messages m ON m.id = f.rowid"
        " JOIN chats    c ON c.id = m.chat_id"
        " LEFT JOIN contacts ct ON ct.id = m.sender_id"
        " WHERE messages_fts MATCH ?"
        + (" AND m.chat_id = ?" if chat_id else "")
        + " ORDER BY rank LIMIT ?"
    )
    params = (query, chat_id, SEARCH_LIMIT) if chat_id else (query, SEARCH_LIMIT)
    count_sql = ("SELECT COUNT(*) FROM messages_fts f"
                 + (" JOIN messages m ON m.id = f.rowid" if chat_id else "")
                 + " WHERE messages_fts MATCH ?"
                 + (" AND m.chat_id = ?" if chat_id else ""))
    try:
        rows = conn.execute(sql, params).fetchall()
        # Counting is cheap even on a million rows, and without it a capped
        # page of 300 looks indistinguishable from "there are exactly 300".
        total = conn.execute(
            count_sql, (query, chat_id) if chat_id else (query,)).fetchone()[0]
    except sqlite3.OperationalError as exc:
        # Malformed FTS syntax is a user typo, not a server fault.
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Could not parse that search: {exc}")
    return {"total": total, "limit": SEARCH_LIMIT, "hits": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")

# Python's mimetypes table is wrong (or silent) for several formats WhatsApp
# actually ships voice notes and clips in. `.m4a` in particular maps to
# `audio/mp4a-latm`, which no browser will play — and because we send
# `X-Content-Type-Options: nosniff`, a wrong type fails silently rather than
# being guessed around. These are the types the reader depends on.
_MIME_OVERRIDES = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".opus": "audio/ogg",
    ".oga": "audio/ogg",
    ".webp": "image/webp",
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
}


def content_type_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# WhatsApp records voice notes as Opus in an Ogg container. Chrome and Firefox
# play that; Safari does not, and on a Mac Safari is usually the default browser
# — so the great majority of voice notes are silent for the person most likely
# to be reading. Transcoding to AAC on demand fixes it without touching the
# archive: the originals are never modified and the results are cached outside
# it, so the server keeps its read-only guarantee.
TRANSCODE_TO_M4A = {".opus", ".ogg", ".oga"}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class Handler(BaseHTTPRequestHandler):
    server_version = "wa-archive"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    @property
    def archive(self) -> Archive:
        return self.server.archive

    def conn(self) -> sqlite3.Connection:
        """One read-only SQLite connection per TCP connection.

        A single shared connection would interleave cursor state across the
        threading server's workers. Thread-local storage is no better here:
        ThreadingHTTPServer spawns a *new thread per request*, so a thread-local
        cache opens a connection per request and never closes it. A handler
        instance, by contrast, lives exactly as long as the client's keep-alive
        connection, and `finish()` gives us a reliable place to close it.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(
                f"file:{self.archive.db_path}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def setup(self):
        self._conn = None
        super().setup()

    def finish(self):
        try:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        finally:
            super().finish()

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in _ALLOWED_HOSTS or host == ""

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page renders other people's messages; keep it from reaching out.
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, detail: str):
        self._json({"detail": detail}, status)

    # -- routing ----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "This server only answers on localhost.")
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        params = parse_qs(url.query)
        try:
            self.route(path, params)
        except ApiError as exc:
            self._error(exc.status, exc.detail)
        except BrokenPipeError:
            pass                                     # browser closed a media stream
        except Exception as exc:                     # noqa: BLE001 - last resort
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def route(self, path: str, params: dict):
        def qs_int(name):
            v = params.get(name, [None])[0]
            if v in (None, ""):
                return None
            try:
                return int(v)
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be a number")

        if path in ("/", "/index.html"):
            return self._send(200, self.server.index_html, "text/html; charset=utf-8")

        if path == "/api/summary":
            return self._json(_summary(self.conn()))

        if path == "/api/chats":
            return self._json(_chats(self.conn()))

        if path == "/api/media/kinds":
            return self._json(_media_kinds(
                self.conn(), qs_int("chat_id"), params.get("q", [None])[0]))

        if path == "/api/media":
            kind = params.get("kind", [None])[0]
            if kind is not None and not kind.replace(":", "").replace("_", "").isalnum():
                raise ApiError(HTTPStatus.BAD_REQUEST, "Bad media kind.")
            return self._json(_media_list(
                self.conn(), kind, qs_int("chat_id"),
                offset=qs_int("offset") or 0,
                newest_first=params.get("order", ["new"])[0] != "old",
                q=params.get("q", [None])[0]))

        if path == "/api/search":
            q = params.get("q", [""])[0]
            if not q.strip():
                return self._json({"total": 0, "limit": SEARCH_LIMIT, "hits": []})
            return self._json(_search(self.conn(), q, qs_int("chat_id")))

        m = re.fullmatch(r"/api/chats/(\d+)/(messages|timeline|export)", path)
        if m:
            chat_id, what = int(m.group(1)), m.group(2)
            conn = self.conn()
            if not conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone():
                raise ApiError(HTTPStatus.NOT_FOUND, f"No chat with id {chat_id}")
            if what == "messages":
                return self._json(_messages(
                    conn, chat_id, qs_int("before"), qs_int("around"),
                    after=qs_int("after"),
                    oldest=params.get("oldest", ["0"])[0] in ("1", "true")))
            if what == "timeline":
                return self._json(_timeline(conn, chat_id))
            return self._export(conn, chat_id, params.get("format", ["html"])[0])

        m = re.fullmatch(r"/api/messages/(\d+)/vcard", path)
        if m:
            return self._vcard(self.conn(), int(m.group(1)))

        if path.startswith("/media/"):
            return self._media(path[len("/media/"):], params.get("as", [None])[0])

        raise ApiError(HTTPStatus.NOT_FOUND, "No such endpoint.")

    # -- export -----------------------------------------------------------

    def _export(self, conn, chat_id: int, fmt: str):
        if fmt not in FORMATS:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           f"format must be one of {', '.join(FORMATS)}")
        # Absolute /media so the exported HTML works while the reader is running.
        text, ctype = export_chat(conn, chat_id, fmt, media_base="/media")
        row = conn.execute("SELECT name, jid FROM chats WHERE id = ?", (chat_id,)).fetchone()
        slug = "".join(c if c.isalnum() or c in "-_ " else "_"
                       for c in (row["name"] or row["jid"]))[:60].strip() or str(chat_id)
        # HTML opens in a tab; the others are downloads.
        disp = "inline" if fmt == "html" else "attachment"
        self._send(200, text.encode("utf-8"), ctype,
                   {"Content-Disposition": f'{disp}; filename="{slug}.{fmt}"'})

    def _vcard(self, conn, message_id: int):
        """Serve one shared contact card so it can be saved to Contacts."""
        row = conn.execute(
            "SELECT vcard, caption FROM media WHERE message_id = ? AND vcard IS NOT NULL"
            " ORDER BY id LIMIT 1", (message_id,)).fetchone()
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "No contact card on that message.")
        name = "".join(c if c.isalnum() or c in "-_ " else "_"
                       for c in (row["caption"] or f"contact-{message_id}"))[:60].strip()
        self._send(200, (row["vcard"].strip() + "\n").encode("utf-8"),
                   "text/vcard; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{name or message_id}.vcf"'})

    # -- media ------------------------------------------------------------

    def _transcoded(self, source: Path) -> Path:
        """AAC copy of a voice note, built once and cached outside the archive."""
        if source.suffix.lower() not in TRANSCODE_TO_M4A:
            raise ApiError(HTTPStatus.BAD_REQUEST, "That file needs no conversion.")
        if not ffmpeg_available():
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED,
                           "ffmpeg is not installed, so Opus voice notes cannot be "
                           "converted for this browser. Install it with "
                           "`brew install ffmpeg`, or open the reader in Chrome.")

        cached = self.server.transcode_dir / (source.stem + ".m4a")
        if cached.is_file() and cached.stat().st_size:
            return cached
        # One conversion at a time: a thread per request would otherwise let a
        # scrolled-past thread start dozens of ffmpeg processes at once.
        with self.server.transcode_lock:
            if cached.is_file() and cached.stat().st_size:
                return cached
            # Written to a .part file so a half-finished conversion is never
            # served, which means ffmpeg cannot infer the container from the
            # extension — hence the explicit `-f mp4`.
            tmp = cached.with_name(cached.name + ".part")
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(source),
                 "-c:a", "aac", "-b:a", "64k", "-f", "mp4", str(tmp)],
                capture_output=True, text=True, timeout=120)
            if proc.returncode != 0 or not tmp.is_file():
                tmp.unlink(missing_ok=True)
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                               f"Could not convert that voice note: {proc.stderr[:200]}")
            tmp.replace(cached)
        return cached

    def _media(self, rel: str, convert_to: str | None = None):
        root = self.archive.media_root.resolve()
        target = (root / rel).resolve()
        # Containment check: a crafted ../.. must not escape the vault.
        if not target.is_relative_to(root) or not target.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "No such media file.")

        if convert_to:
            if convert_to != "m4a":
                raise ApiError(HTTPStatus.BAD_REQUEST, "Only 'm4a' conversion is supported.")
            target = self._transcoded(target)

        size = target.stat().st_size
        ctype = content_type_for(target.name)
        # Vault names are content hashes, so a given URL's bytes never change.
        headers = {"Accept-Ranges": "bytes",
                   "Cache-Control": "private, max-age=31536000, immutable"}

        start, end = 0, size - 1
        status = HTTPStatus.OK
        rng = self.headers.get("Range")
        if rng and (m := _RANGE.match(rng.strip())):
            # Range support is what lets you scrub a video or a voice note
            # instead of waiting for the whole file.
            lo, hi = m.group(1), m.group(2)
            if lo:
                start = int(lo)
                end = int(hi) if hi else size - 1
            elif hi:
                start = max(0, size - int(hi))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with target.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _sweep_stale_audio_caches() -> None:
    """Delete audio caches left by a previous run.

    The cache is removed on a clean shutdown, but a server killed outright
    (a `kill`, a crash, a closed laptop) never gets there, so the directories
    accumulate one per run. Clearing them at startup keeps that bounded without
    needing the previous process to have behaved.
    """
    for stale in Path(tempfile.gettempdir()).glob("wa-audio-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)


def serve(archive_root: Path, host: str = "127.0.0.1", port: int = 8730,
          verbose: bool = False, open_browser: bool = False) -> None:
    """Run the reader until interrupted."""
    archive = Archive(Path(archive_root).expanduser(), read_only=True)
    index = Path(__file__).parent / "web" / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"The reader's index.html is missing from {index.parent}")

    httpd = Server((host, port), Handler)
    httpd.archive = archive
    httpd.index_html = index.read_bytes()
    httpd.verbose = verbose
    # Converted voice notes live outside the archive, so the read-only promise
    # holds and the cache costs nothing permanent.
    _sweep_stale_audio_caches()
    httpd.transcode_dir = Path(tempfile.mkdtemp(prefix="wa-audio-"))
    httpd.transcode_lock = threading.Lock()

    stats = archive.stats()
    url = f"http://{host}:{port}"
    print(f"\n  {stats['messages']:,} messages · {stats['chats']:,} chats · "
          f"{stats['media']:,} media")
    print(f"  Reader ready at {url}")
    print("  Press Ctrl-C to stop.\n")

    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
        archive.close()
        shutil.rmtree(httpd.transcode_dir, ignore_errors=True)


if __name__ == "__main__":       # python -m wa_archive.server <archive>
    import sys
    serve(Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "WhatsAppArchive"),
          port=int(os.environ.get("WA_PORT", 8730)))

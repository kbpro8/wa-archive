"""Read ChatStorage.sqlite (Core Data) and normalise it into the archive."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .store import Archive

# Core Data counts seconds from 2001-01-01 UTC, not the Unix epoch.
COREDATA_EPOCH = 978_307_200

ME_JID = "me@archive.local"

# ZMESSAGETYPE codes drift between WhatsApp releases. Anything unmapped is
# recorded as "unknown:<n>" rather than silently dropped — check `scan-types`
# against your own database before trusting these.
MESSAGE_TYPES = {
    0: "text",
    1: "image",
    2: "video",
    3: "audio",
    4: "contact",
    5: "location",
    6: "system",
    7: "link",
    8: "document",
    10: "system",
    11: "sticker",
    14: "deleted",
    15: "sticker",
}

MEDIA_TYPES = {"image", "video", "audio", "document", "sticker", "contact", "location"}

# Delivery state of an outgoing message. Codes are inferred from observed data
# and are NOT documented by WhatsApp — run `wa-archive audit` against your own
# database before relying on them. Unmapped codes are kept verbatim.
MESSAGE_STATUS = {
    0: "received",
    1: "sending",
    3: "sent",
    5: "delivered",
    6: "read",
    8: "played",
    9: "failed",
}

# Group membership and metadata events. Same caveat as above: inferred, and
# WhatsApp adds new ones. iMazing advertises 40+ of these; the honest position
# is that only the common ones are reliably identifiable without a protobuf
# parser, so everything else is preserved as its raw code.
GROUP_EVENTS = {
    1: "group_created",
    2: "participants_added",
    3: "participant_left",
    4: "participants_removed",
    5: "subject_changed",
    6: "icon_changed",
    7: "promoted_to_admin",
    8: "demoted_from_admin",
    10: "description_changed",
    12: "invite_link_changed",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _select(prefix: str, alias: str, wanted: list[str], present: set[str]) -> list[str]:
    return [f"{alias}.{c} AS {prefix}{c}" for c in wanted if c in present]


def _chat_kind(session_type: int | None, jid: str) -> str:
    if jid.endswith("@broadcast") or jid == "status@broadcast":
        return "broadcast"
    if jid.endswith("@g.us") or session_type == 1:
        return "group"
    return "dm"


# WhatsApp pins a chat by adding a round multiple of ~1000 years to
# ZLASTMESSAGEDATE so it sorts to the top of the list. Stored literally that
# yields chats dated 9026 — beyond what Python's datetime can even format — so
# anything absurdly far in the future is kept but flagged, never trusted as a
# date. The reader re-derives the real value from the messages themselves.
MAX_PLAUSIBLE_TS = 4_102_444_800          # 2100-01-01


def _last_message_at(raw: float | None) -> int | None:
    """Core Data timestamp -> Unix, keeping WhatsApp's pin offset intact.

    The offset is deliberately *not* stripped here. It is the only record that
    a chat was pinned, and the reader needs it: it derives the real date from
    the messages and treats a value beyond MAX_PLAUSIBLE_TS as the pin flag.
    Discarding it would silently lose which conversations you had pinned.
    """
    if not raw:
        return None
    ts = int(raw + COREDATA_EPOCH)
    return ts if ts > 0 else None


def _source_key(stanza: str | None, chat_jid: str, z_pk: int, ts: int, text: str | None) -> str:
    """Stable natural key so re-importing a newer backup only adds new rows."""
    if stanza:
        return f"{chat_jid}|{stanza}"
    digest = hashlib.sha1(
        f"{chat_jid}|{z_pk}|{ts}|{(text or '')[:200]}".encode("utf-8", "replace")
    ).hexdigest()
    return f"{chat_jid}|synth:{digest}"


def scan_codes(chatstorage: Path, column: str) -> list[tuple[int, int]]:
    """Frequency of each value in a code column — used to audit the mappings."""
    conn = sqlite3.connect(f"file:{chatstorage}?mode=ro", uri=True)
    try:
        if column not in _columns(conn, "ZWAMESSAGE"):
            return []
        return conn.execute(
            f"SELECT {column}, COUNT(*) FROM ZWAMESSAGE "
            f"WHERE {column} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    finally:
        conn.close()


def scan_message_types(chatstorage: Path) -> list[tuple[int, int]]:
    return scan_codes(chatstorage, "ZMESSAGETYPE")


def ingest(chatstorage: Path, media_staging: Path | None, archive: Archive,
           progress=None) -> tuple[int, int]:
    """Walk ChatStorage and write everything new into the archive.

    Returns (new_messages, new_media).
    """
    conn = sqlite3.connect(f"file:{chatstorage}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    msg_cols = _columns(conn, "ZWAMESSAGE")
    gm_cols = _columns(conn, "ZWAGROUPMEMBER")
    mi_cols = _columns(conn, "ZWAMEDIAITEM")
    cs_cols = _columns(conn, "ZWACHATSESSION")

    archive.contact_id(ME_JID, "Me", is_me=True)

    # ---- chats -------------------------------------------------------------
    chat_rows = conn.execute(
        "SELECT " + ", ".join(
            _select("", "s", ["Z_PK", "ZCONTACTJID", "ZPARTNERNAME", "ZSESSIONTYPE",
                              "ZARCHIVED", "ZLASTMESSAGEDATE"], cs_cols)
        ) + " FROM ZWACHATSESSION s"
    ).fetchall()

    chat_by_pk: dict[int, tuple[int, str, str]] = {}
    for row in chat_rows:
        jid = row["ZCONTACTJID"]
        if not jid:
            continue
        kind = _chat_kind(row["ZSESSIONTYPE"] if "ZSESSIONTYPE" in row.keys() else None, jid)
        last = row["ZLASTMESSAGEDATE"] if "ZLASTMESSAGEDATE" in row.keys() else None
        cid = archive.chat_id(
            jid=jid,
            name=row["ZPARTNERNAME"],
            kind=kind,
            archived=bool(row["ZARCHIVED"]) if "ZARCHIVED" in row.keys() else False,
            last_message_at=_last_message_at(last),
        )
        chat_by_pk[row["Z_PK"]] = (cid, jid, kind)
        if kind == "dm":
            archive.link_participant(cid, archive.contact_id(jid, row["ZPARTNERNAME"]))

    # ---- messages ----------------------------------------------------------
    select_parts = (
        _select("m_", "m", ["Z_PK", "ZCHATSESSION", "ZMESSAGEDATE", "ZISFROMME", "ZFROMJID",
                            "ZTOJID", "ZMESSAGETYPE", "ZTEXT", "ZSTANZAID", "ZGROUPMEMBER",
                            "ZMEDIAITEM", "ZSTARRED", "ZGROUPEVENTTYPE",
                            "ZMESSAGESTATUS"], msg_cols)
        + _select("g_", "g", ["ZMEMBERJID", "ZCONTACTNAME"], gm_cols)
        + _select("i_", "i", ["ZMEDIALOCALPATH", "ZTITLE", "ZFILESIZE", "ZLATITUDE",
                              "ZLONGITUDE", "ZMOVIEDURATION", "ZVCARDSTRING"], mi_cols)
    )
    sql = (
        "SELECT " + ", ".join(select_parts) + " FROM ZWAMESSAGE m "
        "LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER "
        "LEFT JOIN ZWAMEDIAITEM  i ON i.Z_PK = m.ZMEDIAITEM "
        "ORDER BY m.ZMESSAGEDATE"
    )

    new_messages = new_media = seen = 0

    for row in conn.execute(sql):
        seen += 1
        if progress and seen % 5000 == 0:
            progress(seen, new_messages)

        chat = chat_by_pk.get(row["m_ZCHATSESSION"])
        if not chat:
            continue
        chat_id, chat_jid, chat_kind = chat

        raw_date = row["m_ZMESSAGEDATE"]
        if raw_date is None:
            continue
        ts = int(raw_date + COREDATA_EPOCH)

        is_from_me = bool(row["m_ZISFROMME"])
        mtype_code = row["m_ZMESSAGETYPE"]
        mtype = MESSAGE_TYPES.get(mtype_code, f"unknown:{mtype_code}")

        # Sender resolution: me > group member record > sender JID > the chat itself.
        if is_from_me:
            sender_jid, sender_name = ME_JID, "Me"
        elif chat_kind == "group":
            sender_jid = _get(row, "g_ZMEMBERJID") or _get(row, "m_ZFROMJID") or f"unknown@{chat_jid}"
            sender_name = _get(row, "g_ZCONTACTNAME")
        else:
            sender_jid = _get(row, "m_ZFROMJID") or chat_jid
            sender_name = None

        sender_id = archive.contact_id(sender_jid, sender_name)
        if chat_kind == "group" and not is_from_me:
            archive.link_participant(chat_id, sender_id)

        # Delivery status only means anything for messages we sent.
        status = None
        if is_from_me:
            code = _get(row, "m_ZMESSAGESTATUS")
            if code is not None:
                status = MESSAGE_STATUS.get(code, f"unknown:{code}")

        # Group events ride on ordinary message rows, but ZGROUPEVENTTYPE alone
        # does NOT identify them: on real devices it is populated on ordinary
        # messages too — over half of a large archive, including plain text,
        # photos and documents. Treating it as the marker swallowed those
        # messages into system notices and threw their attachments away.
        #
        # The message type is what actually distinguishes a system row, so an
        # event requires both. The raw code is preserved either way.
        event = None
        ev_code = _get(row, "m_ZGROUPEVENTTYPE")
        if ev_code and mtype == "system":
            event = GROUP_EVENTS.get(ev_code, f"unknown:{ev_code}")
            mtype = "event"

        body = row["m_ZTEXT"]
        msg_id = archive.insert_message(
            chat_id=chat_id,
            sender_id=sender_id,
            ts_utc=ts,
            direction="out" if is_from_me else "in",
            type=mtype,
            body=body,
            is_starred=int(bool(_get(row, "m_ZSTARRED"))),
            is_deleted=int(mtype == "deleted"),
            status=status,
            event=event,
            source_key=_source_key(_get(row, "m_ZSTANZAID"), chat_jid, row["m_Z_PK"], ts, body),
            raw={"z_pk": row["m_Z_PK"], "type_code": mtype_code,
                 "status_code": _get(row, "m_ZMESSAGESTATUS"),
                 "event_code": ev_code},
        )
        if msg_id is None:
            continue  # already imported by a previous run
        new_messages += 1

        # ---- attached media ------------------------------------------------
        local_path = _get(row, "i_ZMEDIALOCALPATH")
        lat, lon = _get(row, "i_ZLATITUDE"), _get(row, "i_ZLONGITUDE")
        vcard = _get(row, "i_ZVCARDSTRING")

        if mtype == "location" and (lat or lon):
            archive.store_media(msg_id, "location", None, expect_file=False,
                                latitude=lat, longitude=lon, caption=_get(row, "i_ZTITLE"))
            new_media += 1
        elif mtype == "contact" and vcard and not local_path:
            # A shared contact card has no file on disk — the whole payload is
            # the vCard string. Without this branch the card is dropped and
            # `export --format vcf` silently produces nothing.
            archive.store_media(msg_id, "contact", None, expect_file=False,
                                vcard=vcard, caption=_get(row, "i_ZTITLE"))
            new_media += 1
        elif mtype in MEDIA_TYPES and local_path:
            source = None
            if media_staging:
                candidate = media_staging / "Message" / local_path
                if candidate.is_file():
                    source = candidate
            archive.store_media(
                msg_id, mtype, source,
                orig_path=local_path,
                mime=vcard,          # holds a mime type on non-contact rows
                bytes=_get(row, "i_ZFILESIZE"),
                duration_s=_get(row, "i_ZMOVIEDURATION"),
                caption=_get(row, "i_ZTITLE"),
            )
            new_media += 1

    conn.close()
    archive.conn.commit()
    return new_messages, new_media


def _get(row: sqlite3.Row, key: str):
    return row[key] if key in row.keys() else None

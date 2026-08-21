"""Build a synthetic ChatStorage.sqlite and a fake iOS backup around it.

No external tools required — media files are generated in-process, so the test
suite runs anywhere Python does.

The fixture deliberately contains the awkward cases, because those are what
break in the field: unmapped code values, an attachment referenced but absent
from the backup, duplicate bytes across chats, NULL stanza ids, a contact card
with no file, group events, emoji and RTL text, and a message full of HTML.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import plistlib
import shutil
import sqlite3
import struct
import zlib
from pathlib import Path

COREDATA_EPOCH = 978_307_200
UDID = "00008110-000A4D2E1E38801E"
DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
DM, GROUP, BCAST = 1, 2, 3
DM_JID = "6281234567890@s.whatsapp.net"
GROUP_JID = "120363011122233344@g.us"
MEDIA_ROOT = f"Media/{DM_JID}"

VCARD = ("BEGIN:VCARD\nVERSION:3.0\nN:Rahmah;Siti;;;\nFN:Siti Rahmah\n"
         "TEL;type=CELL;waid=628119876543:+62 811 9876543\nEND:VCARD")

# Text that must survive the round trip untouched.
XSS_BODY = "<script>alert('xss')</script> & <mark>bold?</mark>"
UNICODE_BODY = "Emoji 🇮🇩 ☕️ 👍🏽 dan teks عربي campur"


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A real, decodable PNG, built without Pillow."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _pdf(text: str) -> bytes:
    return (b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 120]"
            b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
            b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"5 0 obj<<>>stream\nBT /F1 14 Tf 20 60 Td (" + text.encode() +
            b") Tj ET\nendstream endobj\n"
            b"trailer<</Size 6/Root 1 0 R>>\n%%EOF\n")


def _ts(y, mo, d, h=9, mi=0) -> float:
    return dt.datetime(y, mo, d, h, mi).timestamp() - COREDATA_EPOCH


def build_chatstorage(root: Path) -> tuple[Path, Path]:
    """Write ChatStorage.sqlite + a media tree. Returns (db_path, media_staging).

    The media tree is laid out as `<staging>/Message/<ZMEDIALOCALPATH>`, which
    is exactly where extract.ingest looks.
    """
    root = Path(root)
    staging = root / "media" / "Message"
    staging.mkdir(parents=True, exist_ok=True)

    def write(rel: str, data: bytes) -> int:
        p = staging / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return len(data)

    photo_bytes = _png(64, 48, (200, 30, 30))
    n_photo = write(f"{MEDIA_ROOT}/1/0/IMG-20240312-WA0001.jpg", photo_bytes)
    n_photo2 = write(f"{MEDIA_ROOT}/2/1/IMG-20240515-WA0007.jpg", _png(32, 32, (20, 90, 200)))
    n_video = write(f"{MEDIA_ROOT}/3/2/VID-20240718-WA0003.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x11" * 900)
    n_voice = write(f"{MEDIA_ROOT}/4/3/AUDIO-20240902-WA0002.m4a", b"\x00\x00\x00\x1cftypM4A " + b"\x22" * 700)
    n_stk = write(f"{MEDIA_ROOT}/5/4/STK-20241101-WA0009.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x33" * 200)
    n_doc = write(f"{MEDIA_ROOT}/6/5/Prospektus-Aceh-2024.pdf", _pdf("Prospektus Investasi Aceh 2024"))
    # Identical bytes to the first photo, different chat and filename.
    n_dup = write(f"Media/{GROUP_JID}/7/6/IMG-20240312-WA0044.jpg", photo_bytes)

    db = root / "ChatStorage.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE ZWACHATSESSION (
        Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT,
        ZSESSIONTYPE INTEGER, ZARCHIVED INTEGER, ZLASTMESSAGEDATE REAL);
    CREATE TABLE ZWAGROUPMEMBER (
        Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
    CREATE TABLE ZWAMEDIAITEM (
        Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT, ZFILESIZE INTEGER,
        ZLATITUDE REAL, ZLONGITUDE REAL, ZMOVIEDURATION REAL, ZVCARDSTRING TEXT);
    CREATE TABLE ZWAMESSAGE (
        Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMESSAGEDATE REAL,
        ZISFROMME INTEGER, ZFROMJID TEXT, ZTOJID TEXT, ZMESSAGETYPE INTEGER,
        ZTEXT TEXT, ZSTANZAID TEXT, ZGROUPMEMBER INTEGER, ZMEDIAITEM INTEGER,
        ZSTARRED INTEGER, ZGROUPEVENTTYPE INTEGER, ZMESSAGESTATUS INTEGER);
    """)
    con.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?)", [
        (DM, DM_JID, "Budi Santoso", 0, 0, _ts(2025, 6, 25)),
        (GROUP, GROUP_JID, "Tim Prospektus Aceh", 1, 0, _ts(2025, 6, 20)),
        (BCAST, "status@broadcast", "Status Updates", 0, 1, _ts(2024, 12, 25)),
    ])
    con.executemany("INSERT INTO ZWAGROUPMEMBER VALUES (?,?,?)", [
        (1, "628119876543@s.whatsapp.net", "Siti Rahmah"),
        (2, "628551234567@s.whatsapp.net", "Danil Akbar"),
        (3, "628770001111@s.whatsapp.net", None),      # member with no stored name
    ])

    media, msgs = [], []

    def add_media(**kw):
        media.append((len(media) + 1, kw.get("path"), kw.get("title"), kw.get("size"),
                      kw.get("lat"), kw.get("lon"), kw.get("dur"), kw.get("vcard")))
        return len(media)

    def add(session, when, text=None, from_me=0, mtype=0, stanza="auto", member=None,
            media_pk=None, starred=0, event=None, status=None):
        pk = len(msgs) + 1
        msgs.append((pk, session, when, from_me, None, None, mtype, text,
                     f"3EB0{pk:08X}" if stanza == "auto" else stanza,
                     member, media_pk, starred, event, status))
        return pk

    # Ordinary traffic, so the density rail and pagination have real volume.
    for year, month, n in [(2022, m, 3) for m in range(1, 13)] + \
                          [(2023, m, 4) for m in range(1, 13)] + \
                          [(2024, m, 5) for m in range(1, 13)] + \
                          [(2025, m, 6) for m in range(1, 7)]:
        for i in range(n):
            out = i % 3 == 0
            sess = DM if i % 3 != 2 else GROUP
            add(sess, _ts(year, month, (i % 27) + 1, 8 + i % 12),
                text="Sudah dikirim ya" if not out else "Nanti saya kirim",
                from_me=int(out), member=None if (out or sess == DM) else (i % 3) + 1,
                status=5 if out else None)

    ids = {}
    ids["photo"] = add(DM, _ts(2024, 3, 12, 10, 15), "Ini foto lokasinya ya", mtype=1,
                       media_pk=add_media(path=f"{MEDIA_ROOT}/1/0/IMG-20240312-WA0001.jpg",
                                          title="Foto lokasi cold storage", size=n_photo))
    ids["photo2"] = add(DM, _ts(2024, 5, 15, 11), None, from_me=1, mtype=1, starred=1, status=6,
                        media_pk=add_media(path=f"{MEDIA_ROOT}/2/1/IMG-20240515-WA0007.jpg", size=n_photo2))
    ids["video"] = add(DM, _ts(2024, 7, 18, 16, 30), "Video survei lapangan", mtype=2,
                       media_pk=add_media(path=f"{MEDIA_ROOT}/3/2/VID-20240718-WA0003.mp4",
                                          title="Video survei", size=n_video, dur=3.0))
    ids["voice"] = add(DM, _ts(2024, 9, 2, 8, 5), None, mtype=3,
                       media_pk=add_media(path=f"{MEDIA_ROOT}/4/3/AUDIO-20240902-WA0002.m4a",
                                          size=n_voice, dur=4.0))
    ids["sticker"] = add(DM, _ts(2024, 11, 1, 20, 10), None, from_me=1, mtype=11, status=5,
                         media_pk=add_media(path=f"{MEDIA_ROOT}/5/4/STK-20241101-WA0009.webp", size=n_stk))
    ids["doc"] = add(GROUP, _ts(2024, 4, 8, 14, 22), "Draft prospektus terlampir", mtype=8, member=2,
                     media_pk=add_media(path=f"{MEDIA_ROOT}/6/5/Prospektus-Aceh-2024.pdf",
                                        title="Prospektus-Aceh-2024.pdf", size=n_doc))
    ids["dup"] = add(GROUP, _ts(2024, 3, 13, 9), "Diteruskan dari pak Budi", mtype=1, member=1,
                     media_pk=add_media(path=f"Media/{GROUP_JID}/7/6/IMG-20240312-WA0044.jpg",
                                        title="Forward", size=n_dup))
    ids["missing"] = add(DM, _ts(2023, 1, 1, 12), "Foto lama", mtype=1,
                         media_pk=add_media(path=f"{MEDIA_ROOT}/9/9/IMG-20230101-WA0000.jpg",
                                            title="Hilang", size=48213))
    ids["location"] = add(DM, _ts(2024, 6, 6, 13, 45), None, from_me=1, mtype=5, status=5,
                          media_pk=add_media(lat=5.548290, lon=95.323753, title="Pelabuhan Malahayati"))
    ids["contact"] = add(DM, _ts(2024, 2, 2, 9, 30), None, mtype=4,
                         media_pk=add_media(title="Siti Rahmah", vcard=VCARD))

    ids["event_created"] = add(GROUP, _ts(2022, 1, 5, 8), mtype=6, event=1, member=1)
    ids["event_added"] = add(GROUP, _ts(2022, 1, 5, 8, 1), mtype=6, event=2, member=1)
    ids["event_unmapped"] = add(GROUP, _ts(2024, 8, 1, 10), mtype=6, event=77, member=2)

    # Real devices set ZGROUPEVENTTYPE on ORDINARY messages, not just system
    # rows — plain text, photos and documents all carry it. These must stay
    # readable messages with their attachments intact.
    ids["text_with_event_code"] = add(
        GROUP, _ts(2024, 5, 2, 9), "Pesan biasa yang punya kode event", member=1,
        mtype=0, event=2)
    ids["image_with_event_code"] = add(
        GROUP, _ts(2024, 5, 3, 9), "Foto dengan kode event", member=2, mtype=1, event=2,
        media_pk=add_media(path=f"{MEDIA_ROOT}/1/0/IMG-20240312-WA0001.jpg",
                           title="Foto rapat", size=n_photo))
    ids["doc_with_event_code"] = add(
        GROUP, _ts(2024, 5, 4, 9), "Dokumen dengan kode event", member=1, mtype=8, event=5,
        media_pk=add_media(path=f"{MEDIA_ROOT}/6/5/Prospektus-Aceh-2024.pdf",
                           title="Lampiran.pdf", size=n_doc))

    ids["deleted"] = add(DM, _ts(2024, 10, 10, 10, 10), None, mtype=14)
    ids["unknown_type"] = add(DM, _ts(2024, 10, 11, 11, 11), "tipe baru dari update WhatsApp", mtype=99)
    ids["unknown_status"] = add(DM, _ts(2024, 10, 12, 12, 12), "status aneh", from_me=1, status=42)

    ids["xss"] = add(DM, _ts(2025, 1, 3, 9), XSS_BODY)
    ids["unicode"] = add(DM, _ts(2025, 1, 4, 9), UNICODE_BODY, from_me=1, status=6)
    ids["nostanza1"] = add(DM, _ts(2025, 1, 5, 9), "Pesan tanpa stanza id", stanza=None)
    ids["nostanza2"] = add(DM, _ts(2025, 1, 5, 9, 1), "Pesan tanpa stanza id kedua", stanza=None)
    ids["multiline"] = add(DM, _ts(2025, 1, 6, 9), "Baris satu\nbaris dua", from_me=1, status=3)
    ids["broadcast"] = add(BCAST, _ts(2024, 12, 25, 7), "Selamat hari raya")

    # Must be skipped without crashing: no such chat session, and no date.
    add(999, _ts(2024, 1, 1, 1, 1), "orphan")
    add(DM, None, "no date")

    con.executemany("INSERT INTO ZWAMEDIAITEM VALUES (?,?,?,?,?,?,?,?)", media)
    con.executemany("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", msgs)
    con.commit()
    con.close()

    build_chatstorage.ids = ids
    build_chatstorage.expected_messages = len(msgs) - 2      # orphan + dateless are dropped
    build_chatstorage.expected_media = len(media)
    return db, root / "media"


def append_messages(db: Path, rows: list[tuple]) -> None:
    """Add messages to an existing fixture, to simulate a later backup.

    Each row is (chat_pk, coredata_ts, text, from_me, stanza_id).
    """
    con = sqlite3.connect(db)
    mx = con.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGE").fetchone()[0]
    con.executemany(
        "INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(mx + i + 1, chat, ts, me, None, None, 0, text, stanza, None, None, 0, None, None)
         for i, (chat, ts, text, me, stanza) in enumerate(rows)])
    con.commit()
    con.close()


def build_backup(fixture_root: Path, backups_root: Path) -> Path:
    """Wrap a fixture in an unencrypted iOS backup folder. Returns the backup dir."""
    fixture_root, backups_root = Path(fixture_root), Path(backups_root)
    backup = backups_root / UDID
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)

    (backup / "Manifest.plist").write_bytes(plistlib.dumps(
        {"IsEncrypted": False, "Version": "10.0", "Date": dt.datetime.now()}))
    (backup / "Info.plist").write_bytes(plistlib.dumps(
        {"Device Name": "iPhone 13", "Product Version": "18.1",
         "Last Backup Date": dt.datetime.now()}))

    con = sqlite3.connect(backup / "Manifest.db")
    con.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                "relativePath TEXT, flags INTEGER, file BLOB)")

    def stash(src: Path, relative_path: str, domain: str = DOMAIN):
        file_id = hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()
        dest = backup / file_id[:2] / file_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        con.execute("INSERT OR REPLACE INTO Files VALUES (?,?,?,?,?)",
                    (file_id, domain, relative_path, 1, None))

    stash(fixture_root / "ChatStorage.sqlite", "ChatStorage.sqlite")
    media_root = fixture_root / "media" / "Message"
    for f in sorted(media_root.rglob("*")):
        if f.is_file():
            stash(f, f"Message/{f.relative_to(media_root).as_posix()}")

    # Foreign-domain files, so pruning and filtering have something to reject.
    junk = backup / "_tmp_junk"
    junk.mkdir(exist_ok=True)
    for name in ("Photos.sqlite", "sms.db", "CallHistory.storedata"):
        p = junk / name
        p.write_bytes(b"\x00" * 2048)
        stash(p, f"Library/{name}", domain="HomeDomain")
    shutil.rmtree(junk)

    # A directory row, which must not be mistaken for a file.
    con.execute("INSERT OR REPLACE INTO Files VALUES (?,?,?,?,?)",
                (hashlib.sha1(f"{DOMAIN}-Message/Media".encode()).hexdigest(),
                 DOMAIN, "Message/Media", 2, None))
    con.commit()
    con.close()
    return backup

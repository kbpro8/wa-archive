"""End-to-end tests: backup -> extract -> archive -> reader -> export.

Run with:   python -m unittest discover -s tests -v

Standard library only, and no iPhone required. Everything below the device
layer is covered; what a phone would add is the transfer itself, which is
libimobiledevice's job rather than this project's.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import fixture                                          # noqa: E402
from wa_archive.backup import BackupSource, discover_backups       # noqa: E402
from wa_archive.export import export_chat, media_paths             # noqa: E402
from wa_archive.extract import ingest, scan_codes                  # noqa: E402
from wa_archive.query import build_fts_query                       # noqa: E402
from wa_archive.server import Handler, Server, content_type_for    # noqa: E402
from wa_archive.store import Archive                               # noqa: E402


class Base(unittest.TestCase):
    """Builds a fixture, wraps it in a backup, and ingests it once for the class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wa-test-"))
        cls.fixture_root = cls.tmp / "fixture"
        cls.fixture_root.mkdir()
        cls.db, _ = fixture.build_chatstorage(cls.fixture_root)
        cls.ids = fixture.build_chatstorage.ids
        cls.expected_messages = fixture.build_chatstorage.expected_messages
        cls.expected_media = fixture.build_chatstorage.expected_media
        cls.backup_dir = fixture.build_backup(cls.fixture_root, cls.tmp / "backups")
        cls.archive_root = cls.tmp / "archive"
        cls.staging = cls.tmp / "staging"
        cls._ingest()

    @classmethod
    def _ingest(cls):
        archive = Archive(cls.archive_root)
        try:
            source = BackupSource(cls.backup_dir, passphrase=None)
            chat = source.extract_chatstorage(cls.staging / "ChatStorage.sqlite")
            media = source.extract_media(cls.staging / "media")
            cls.last_result = ingest(chat, media, archive)
        finally:
            archive.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def archive(self) -> Archive:
        return Archive(self.archive_root, read_only=True)


class TestBackupDiscovery(Base):
    def test_backup_is_recognised(self):
        found = discover_backups([self.backup_dir.parent])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_name, "iPhone 13")
        self.assertEqual(found[0].ios_version, "18.1")
        self.assertFalse(found[0].encrypted)

    def test_layout_detected_from_manifest(self):
        source = BackupSource(self.backup_dir)
        self.assertEqual(source.detect_layout(),
                         (fixture.DOMAIN, "", "Message/Media/"))

    def test_only_whatsapp_media_is_extracted(self):
        """Foreign-domain files in the manifest must not be pulled out."""
        out = self.tmp / "media-only"
        BackupSource(self.backup_dir).extract_media(out)
        names = {p.name for p in out.rglob("*") if p.is_file()}
        self.assertIn("IMG-20240312-WA0001.jpg", names)
        for foreign in ("sms.db", "Photos.sqlite", "CallHistory.storedata"):
            self.assertNotIn(foreign, names)


class TestIngest(Base):
    def test_counts(self):
        n_msg, n_media = self.last_result
        self.assertEqual(n_msg, self.expected_messages)
        self.assertEqual(n_media, self.expected_media)

    def test_orphan_and_dateless_rows_are_skipped_not_crashed(self):
        with self.archive() as a:
            self.assertIsNone(a.conn.execute(
                "SELECT 1 FROM messages WHERE body = 'orphan'").fetchone())
            self.assertIsNone(a.conn.execute(
                "SELECT 1 FROM messages WHERE body = 'no date'").fetchone())

    def test_reingest_is_idempotent(self):
        before = self._snapshot()
        self._ingest()
        self.assertEqual(self.last_result, (0, 0), "re-ingest added rows")
        self.assertEqual(self._snapshot(), before, "re-ingest changed the archive")

    def _snapshot(self):
        with self.archive() as a:
            return tuple(a.conn.execute(
                "SELECT (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM media),"
                "       (SELECT COUNT(*) FROM chats),    (SELECT COUNT(*) FROM contacts)"
            ).fetchone())

    def test_media_is_deduplicated_by_content(self):
        with self.archive() as a:
            rows = a.conn.execute(
                "SELECT sha256, COUNT(*) n FROM media WHERE sha256 IS NOT NULL "
                "GROUP BY sha256 HAVING n > 1").fetchall()
            self.assertTrue(rows, "fixture should contain duplicate bytes")
            for r in rows:
                matches = list((self.archive_root / "media").rglob(f"{r['sha256']}*"))
                self.assertEqual(len(matches), 1,
                                 f"{r['n']} references stored {len(matches)} times")

    def test_absent_attachment_recorded_as_missing(self):
        with self.archive() as a:
            row = a.conn.execute(
                "SELECT * FROM media WHERE message_id = ?", (self._mid("missing"),)).fetchone()
            self.assertEqual(row["missing"], 1)
            self.assertIsNone(row["path"])

    def test_contact_card_is_kept(self):
        """Regression: contact rows have no ZMEDIALOCALPATH and were dropped."""
        with self.archive() as a:
            row = a.conn.execute(
                "SELECT * FROM media WHERE message_id = ?", (self._mid("contact"),)).fetchone()
            self.assertIsNotNone(row, "contact card was dropped during ingest")
            self.assertEqual(row["kind"], "contact")
            self.assertIn("BEGIN:VCARD", row["vcard"])
            self.assertIsNone(row["mime"], "vCard text leaked into the mime column")

    def test_ordinary_messages_are_not_swallowed_as_group_events(self):
        """Regression: ZGROUPEVENTTYPE is set on ordinary messages too.

        Treating it alone as the event marker turned 55% of a real 1.2M-message
        archive into system notices, hid the text, and dropped every attachment
        on those rows.
        """
        with self.archive() as a:
            for key, expect_type in (("text_with_event_code", "text"),
                                     ("image_with_event_code", "image"),
                                     ("doc_with_event_code", "document")):
                mid = self._mid(key)
                row = a.conn.execute(
                    "SELECT type, event, body FROM messages WHERE id = ?", (mid,)).fetchone()
                self.assertIsNone(row["event"], f"{key} was misfiled as a group event")
                self.assertEqual(row["type"], expect_type)
                self.assertTrue(row["body"], f"{key} lost its text")

            # ...and their attachments must survive.
            for key in ("image_with_event_code", "doc_with_event_code"):
                n = a.conn.execute("SELECT COUNT(*) FROM media WHERE message_id = ?",
                                   (self._mid(key),)).fetchone()[0]
                self.assertEqual(n, 1, f"{key} lost its attachment")

    def test_real_system_events_are_still_recognised(self):
        with self.archive() as a:
            for key, expect in (("event_created", "group_created"),
                                ("event_added", "participants_added"),
                                ("event_unmapped", "unknown:77")):
                row = a.conn.execute("SELECT type, event FROM messages WHERE id = ?",
                                     (self._mid(key),)).fetchone()
                self.assertEqual(row["event"], expect)
                self.assertEqual(row["type"], "event")

    def test_unmapped_codes_preserved_verbatim(self):
        with self.archive() as a:
            self.assertEqual(self._field(a, "unknown_type", "type"), "unknown:99")
            self.assertEqual(self._field(a, "unknown_status", "status"), "unknown:42")
            self.assertEqual(self._field(a, "event_unmapped", "event"), "unknown:77")

    def test_known_codes_are_labelled(self):
        with self.archive() as a:
            self.assertEqual(self._field(a, "event_created", "event"), "group_created")
            self.assertEqual(self._field(a, "photo", "type"), "image")
            self.assertEqual(self._field(a, "voice", "type"), "audio")
            self.assertEqual(self._field(a, "deleted", "is_deleted"), 1)

    def test_null_stanza_ids_get_distinct_synthetic_keys(self):
        with self.archive() as a:
            keys = [r[0] for r in a.conn.execute(
                "SELECT source_key FROM messages WHERE source_key LIKE '%synth:%'")]
            self.assertGreaterEqual(len(keys), 2)
            self.assertEqual(len(keys), len(set(keys)), "synthetic keys collided")

    def test_text_survives_round_trip(self):
        with self.archive() as a:
            self.assertEqual(self._field(a, "xss", "body"), fixture.XSS_BODY)
            self.assertEqual(self._field(a, "unicode", "body"), fixture.UNICODE_BODY)

    def test_timestamps_converted_from_coredata_epoch(self):
        import datetime as dt
        with self.archive() as a:
            ts = self._field(a, "photo", "ts_utc")
            self.assertEqual(dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), "2024-03-12")

    def test_chat_kinds(self):
        with self.archive() as a:
            kinds = dict(a.conn.execute("SELECT jid, kind FROM chats").fetchall())
            self.assertEqual(kinds[fixture.DM_JID], "dm")
            self.assertEqual(kinds[fixture.GROUP_JID], "group")
            self.assertEqual(kinds["status@broadcast"], "broadcast")

    def test_group_sender_resolution(self):
        with self.archive() as a:
            row = a.conn.execute(
                "SELECT c.display_name, c.jid FROM messages m JOIN contacts c ON c.id = m.sender_id "
                "WHERE m.id = ?", (self._mid("doc"),)).fetchone()
            self.assertEqual(row["display_name"], "Danil Akbar")

    def test_scan_codes_finds_the_unmapped_values(self):
        codes = dict(scan_codes(self.db, "ZMESSAGETYPE"))
        self.assertIn(99, codes)

    # helpers ---------------------------------------------------------------
    def _mid(self, key):
        """Map a fixture Z_PK to the archive's message id via source_key."""
        with self.archive() as a:
            row = a.conn.execute(
                "SELECT id FROM messages WHERE source_key LIKE ?",
                (f"%3EB0{self.ids[key]:08X}",)).fetchone()
            if row:
                return row["id"]
        raise AssertionError(f"no archived message for fixture {key!r}")

    def _field(self, a, key, column):
        return a.conn.execute(f"SELECT {column} FROM messages WHERE id = ?",
                              (self._mid(key),)).fetchone()[0]


class TestIncremental(Base):
    def test_later_backup_adds_only_new_rows(self):
        before = self.last_result
        fixture.append_messages(self.db, [
            (fixture.DM, fixture._ts(2025, 7, 1, 9), "Pesan baru Juli", 0, "3EB0NEW1"),
            (fixture.DM, fixture._ts(2025, 7, 2, 10), "Balasan Juli", 1, "3EB0NEW2"),
            # Backfill: an OLD message discovered late. It gets a high row id.
            (fixture.DM, fixture._ts(2021, 12, 25, 8), "Pesan lama baru ditemukan", 0, "3EB0OLD1"),
        ])
        fixture.build_backup(self.fixture_root, self.tmp / "backups")
        shutil.rmtree(self.staging, ignore_errors=True)
        self._ingest()
        self.assertEqual(self.last_result[0], 3)

        with self.archive() as a:
            self.assertEqual(
                a.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                self.expected_messages + 3)
            # The backfilled row must have the highest id but the earliest time,
            # which is exactly the case that id-ordered paging would get wrong.
            newest_id = a.conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
            oldest = a.conn.execute(
                "SELECT id FROM messages ORDER BY ts_utc, id LIMIT 1").fetchone()[0]
            backfill = a.conn.execute(
                "SELECT id, ts_utc FROM messages WHERE body = 'Pesan lama baru ditemukan'"
            ).fetchone()
            self.assertGreater(backfill["id"], self.expected_messages)
            self.assertEqual(oldest, backfill["id"])
            self.assertLessEqual(backfill["id"], newest_id)


class TestSearchQuery(unittest.TestCase):
    def test_prefix_matching_is_the_default(self):
        self.assertEqual(build_fts_query("kirim"), "kirim*")
        self.assertEqual(build_fts_query("dua kata"), "dua* kata*")

    def test_exact_disables_it(self):
        self.assertEqual(build_fts_query("kirim", exact=True), "kirim")

    def test_explicit_fts_syntax_is_left_alone(self):
        """Quotes, wildcards and parens mean the user is writing FTS themselves."""
        for raw in ['"exact phrase"', "foo*", "NEAR(a b)", "col:value"]:
            self.assertEqual(build_fts_query(raw), raw)

    def test_operators_are_preserved_but_terms_still_get_prefixes(self):
        # Boolean operators alone are not "FTS syntax" — a bare AND/OR query
        # should still benefit from prefix matching on its terms.
        self.assertEqual(build_fts_query("foo and bar"), "foo* AND bar*")
        self.assertEqual(build_fts_query("a AND b OR c"), "a* AND b* OR c*")

    def test_punctuation_is_stripped(self):
        self.assertEqual(build_fts_query("halo!!!"), "halo*")

    def test_empty_input_does_not_produce_invalid_sql(self):
        self.assertEqual(build_fts_query("!!!"), "!!!")


class TestMimeTypes(unittest.TestCase):
    def test_voice_note_formats_are_browser_playable(self):
        """Python maps .m4a to audio/mp4a-latm, which no browser will play."""
        self.assertEqual(content_type_for("x.m4a"), "audio/mp4")
        self.assertEqual(content_type_for("x.opus"), "audio/ogg")
        self.assertEqual(content_type_for("x.aac"), "audio/aac")

    def test_common_types_still_resolve(self):
        self.assertEqual(content_type_for("x.jpg"), "image/jpeg")
        self.assertEqual(content_type_for("x.mp4"), "video/mp4")
        self.assertEqual(content_type_for("x.webp"), "image/webp")
        self.assertEqual(content_type_for("x.pdf"), "application/pdf")

    def test_unknown_extension_falls_back(self):
        self.assertEqual(content_type_for("x.zzz"), "application/octet-stream")


class TestExport(Base):
    def _chat_id(self, jid):
        with self.archive() as a:
            return a.conn.execute("SELECT id FROM chats WHERE jid = ?", (jid,)).fetchone()[0]

    def test_every_format_produces_output(self):
        with self.archive() as a:
            cid = self._chat_id(fixture.DM_JID)
            for fmt in ("html", "csv", "txt", "vcf"):
                text, mime = export_chat(a.conn, cid, fmt)
                self.assertTrue(text.strip(), f"{fmt} export was empty")
                self.assertIn("/", mime)

    def test_vcf_contains_the_shared_card(self):
        with self.archive() as a:
            text, mime = export_chat(a.conn, self._chat_id(fixture.DM_JID), "vcf")
            self.assertIn("BEGIN:VCARD", text)
            self.assertIn("Siti Rahmah", text)
            self.assertTrue(mime.startswith("text/vcard"))

    def test_html_escapes_markup(self):
        with self.archive() as a:
            text, _ = export_chat(a.conn, self._chat_id(fixture.DM_JID), "html")
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("<script>alert", text)

    def test_html_media_base_is_applied(self):
        with self.archive() as a:
            cid = self._chat_id(fixture.DM_JID)
            text, _ = export_chat(a.conn, cid, "html", media_base="/media")
            self.assertIn('src="/media/', text)
            self.assertIn("not in backup", text)      # the absent attachment

    def test_csv_row_count_matches_messages(self):
        import csv, io
        with self.archive() as a:
            cid = self._chat_id(fixture.DM_JID)
            text, _ = export_chat(a.conn, cid, "csv")
            rows = list(csv.reader(io.StringIO(text)))
            n = a.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                               (cid,)).fetchone()[0]
            self.assertEqual(len(rows) - 1, n)

    def test_txt_marks_non_text_payloads(self):
        with self.archive() as a:
            text, _ = export_chat(a.conn, self._chat_id(fixture.DM_JID), "txt")
            self.assertIn("<location", text)
            self.assertIn("<message deleted>", text)
            self.assertIn("missing from backup", text)

    def test_media_paths_are_listed_for_copying(self):
        with self.archive() as a:
            paths = media_paths(a.conn, self._chat_id(fixture.DM_JID))
            self.assertTrue(paths)
            for p in paths:
                self.assertTrue((self.archive_root / "media" / p).is_file())

    def test_unknown_format_rejected(self):
        with self.archive() as a:
            with self.assertRaises(ValueError):
                export_chat(a.conn, self._chat_id(fixture.DM_JID), "exe")


class TestReader(Base):
    """Drives the HTTP API the way the reader does."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.httpd = Server(("127.0.0.1", 0), Handler)
        cls.httpd.archive = Archive(cls.archive_root, read_only=True)
        cls.httpd.index_html = (Path(__file__).resolve().parent.parent
                                / "wa_archive" / "web" / "index.html").read_bytes()
        cls.httpd.verbose = False
        cls.httpd.transcode_dir = Path(tempfile.mkdtemp(prefix="wa-audio-test-"))
        cls.httpd.transcode_lock = threading.Lock()
        cls.addClassCleanup(shutil.rmtree, cls.httpd.transcode_dir, ignore_errors=True)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.archive.close()
        super().tearDownClass()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def status(self, path, method="GET", headers=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    # --- content ---------------------------------------------------------
    def test_index_is_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            body = r.read()
        self.assertEqual(r.status, 200)
        self.assertIn(b"<title>Archive</title>", body)

    def test_summary_matches_the_database(self):
        s = self.get("/api/summary")
        with self.archive() as a:
            self.assertEqual(s["messages"],
                             a.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def test_chats_carry_counts_and_previews(self):
        chats = self.get("/api/chats")
        self.assertEqual(len(chats), 3)
        for c in chats:
            self.assertIn(c["kind"], ("dm", "group", "broadcast"))
            self.assertGreaterEqual(c["message_count"], 1)
        self.assertTrue(any(c["preview"] for c in chats))

    def test_pagination_covers_every_message_exactly_once(self):
        cid = self.get("/api/chats")[0]["id"]
        page = self.get(f"/api/chats/{cid}/messages")
        seen = list(page)
        while page:
            page = self.get(f"/api/chats/{cid}/messages?before={page[0]['id']}")
            seen = page + seen
        ids = [m["id"] for m in seen]
        self.assertEqual(len(ids), len(set(ids)), "pages overlapped")
        with self.archive() as a:
            expected = [r[0] for r in a.conn.execute(
                "SELECT id FROM messages WHERE chat_id = ? ORDER BY ts_utc, id", (cid,))]
        self.assertEqual(ids, expected, "paged order != chronological order")

    def test_around_centres_on_the_requested_message(self):
        cid = self.get("/api/chats")[0]["id"]
        target = self.get(f"/api/chats/{cid}/messages")[-1]["id"]
        window = self.get(f"/api/chats/{cid}/messages?around={target}")
        self.assertIn(target, [m["id"] for m in window])

    def test_reading_order_can_start_from_the_beginning(self):
        """`oldest=1` plus `after=` lets a conversation be read forwards, which
        is the only sane way to work through a chat that starts in 2012."""
        cid = self.get("/api/chats")[0]["id"]
        newest = self.get(f"/api/chats/{cid}/messages")
        oldest = self.get(f"/api/chats/{cid}/messages?oldest=1")
        self.assertTrue(oldest and newest)
        self.assertLessEqual(oldest[0]["ts_utc"], newest[0]["ts_utc"])

        with self.archive() as a:
            true_first = a.conn.execute(
                "SELECT id FROM messages WHERE chat_id = ? ORDER BY ts_utc, id LIMIT 1",
                (cid,)).fetchone()[0]
        self.assertEqual(oldest[0]["id"], true_first)

        # Walking forwards must cover the chat exactly once, in order.
        seen = list(oldest)
        while True:
            nxt = self.get(f"/api/chats/{cid}/messages?after={seen[-1]['id']}")
            if not nxt:
                break
            seen += nxt
        ids = [m["id"] for m in seen]
        with self.archive() as a:
            expected = [r[0] for r in a.conn.execute(
                "SELECT id FROM messages WHERE chat_id = ? ORDER BY ts_utc, id", (cid,))]
        self.assertEqual(ids, expected)
        self.assertEqual(len(ids), len(set(ids)), "forward paging repeated messages")

    def test_attachment_browser_counts_and_lists(self):
        kinds = self.get("/api/media/kinds")
        self.assertTrue(kinds)
        with self.archive() as a:
            for k in kinds:
                real = a.conn.execute(
                    "SELECT COUNT(*) FROM media WHERE kind = ? AND missing = 0",
                    (k["kind"],)).fetchone()[0]
                self.assertEqual(k["n"], real, f"wrong count for {k['kind']}")
                # A tab must never open onto attachments that aren't on disk.
                self.assertGreater(k["n"], 0)

        kind = kinds[0]["kind"]
        items = self.get(f"/api/media?kind={kind}")
        self.assertTrue(items)
        for it in items:
            self.assertEqual(it["kind"], kind)
            self.assertIsNotNone(it["media_path"])
            # Each item must carry enough context to jump back to its message.
            self.assertIsNotNone(it["message_id"])
            self.assertIsNotNone(it["chat_id"])
        ts = [i["ts_utc"] for i in items]
        self.assertEqual(ts, sorted(ts, reverse=True), "attachments not newest-first")

    def test_attachment_browser_can_scope_and_page(self):
        cid = self.get("/api/chats")[0]["id"]
        kinds = self.get(f"/api/media/kinds?chat_id={cid}")
        for k in kinds:
            scoped = self.get(f"/api/media?kind={k['kind']}&chat_id={cid}")
            self.assertTrue(all(i["chat_id"] == cid for i in scoped))
        kind = kinds[0]["kind"]
        page1 = self.get(f"/api/media?kind={kind}&offset=0")
        page2 = self.get(f"/api/media?kind={kind}&offset={len(page1)}")
        self.assertFalse({i["id"] for i in page1} & {i["id"] for i in page2},
                         "pages overlap")

    def test_attachment_browser_rejects_a_bad_kind(self):
        self.assertEqual(self.status("/api/media?kind=../etc"), 400)

    def test_attachment_search_filters_counts_and_items_alike(self):
        """Regression: the query reached the tab counts but not the listing, so
        the header read '22 documents' above 120 unfiltered ones."""
        term = "Prospektus"
        kinds = self.get(f"/api/media/kinds?q={term}")
        self.assertTrue(kinds, "fixture should have a matching document")
        for k in kinds:
            items = self.get(f"/api/media?kind={k['kind']}&q={term}")
            self.assertEqual(len(items), k["n"],
                             "listing and tab count disagree")
            for it in items:
                # A hit may come from the caption, the message text, or the
                # original filename — all three are searched, so all three
                # must be returned or the match looks inexplicable.
                haystack = " ".join(str(it.get(f) or "")
                                    for f in ("caption", "body", "orig_path")).lower()
                self.assertIn(term.lower(), haystack)

    def test_attachment_search_narrows_the_available_kinds(self):
        all_kinds = {k["kind"] for k in self.get("/api/media/kinds")}
        hit = {k["kind"] for k in self.get("/api/media/kinds?q=Prospektus")}
        self.assertTrue(hit)
        self.assertTrue(hit <= all_kinds)
        self.assertLess(sum(k["n"] for k in self.get("/api/media/kinds?q=Prospektus")),
                        sum(k["n"] for k in self.get("/api/media/kinds")))

    def test_attachment_search_treats_wildcards_literally(self):
        """A caption containing % must not behave like a LIKE wildcard."""
        everything = sum(k["n"] for k in self.get("/api/media/kinds"))
        pct = sum(k["n"] for k in self.get("/api/media/kinds?q=%25"))
        self.assertLess(pct, everything, "'%' matched everything — not escaped")
        underscore = sum(k["n"] for k in self.get("/api/media/kinds?q=_"))
        self.assertLess(underscore, everything, "'_' matched everything — not escaped")

    def test_attachment_search_with_no_matches_is_empty_not_an_error(self):
        self.assertEqual(self.get("/api/media/kinds?q=zzzznotarealthing"), [])
        self.assertEqual(self.get("/api/media?kind=image&q=zzzznotarealthing"), [])

    def test_timeline_totals_match_the_chat(self):
        cid = self.get("/api/chats")[0]["id"]
        tl = self.get(f"/api/chats/{cid}/timeline")
        with self.archive() as a:
            n = a.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                               (cid,)).fetchone()[0]
        self.assertEqual(sum(m["n"] for m in tl), n)
        for m in tl:                       # every bar must be clickable
            self.assertIsNotNone(m["first_id"])

    def test_search_uses_prefix_matching_and_marks_hits(self):
        res = self.get("/api/search?q=kirim")
        self.assertTrue(res["hits"])
        self.assertTrue(any("\x01" in h["excerpt"] for h in res["hits"]))

    def test_search_can_be_scoped_to_one_chat(self):
        cid = self.get("/api/chats")[0]["id"]
        res = self.get(f"/api/search?q=kirim&chat_id={cid}")
        self.assertTrue(all(h["chat_id"] == cid for h in res["hits"]))
        self.assertLessEqual(len(res["hits"]), res["total"])

    def test_search_reports_the_true_total_when_capped(self):
        """A capped page must not read as the whole answer."""
        res = self.get("/api/search?q=kirim")
        with self.archive() as a:
            real = a.conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                ("kirim*",)).fetchone()[0]
        self.assertEqual(res["total"], real)
        self.assertEqual(res["limit"], 300)
        self.assertLessEqual(len(res["hits"]), res["limit"])

    def test_scoped_search_total_counts_only_that_chat(self):
        cid = self.get("/api/chats")[0]["id"]
        scoped = self.get(f"/api/search?q=kirim&chat_id={cid}")
        everywhere = self.get("/api/search?q=kirim")
        self.assertLessEqual(scoped["total"], everywhere["total"])
        with self.archive() as a:
            real = a.conn.execute(
                "SELECT COUNT(*) FROM messages_fts f JOIN messages m ON m.id = f.rowid"
                " WHERE messages_fts MATCH ? AND m.chat_id = ?", ("kirim*", cid)).fetchone()[0]
        self.assertEqual(scoped["total"], real)

    def test_malformed_search_is_a_client_error(self):
        self.assertEqual(self.status('/api/search?q=%22unclosed'), 400)

    def test_vcard_endpoint_serves_the_card(self):
        with self.archive() as a:
            mid = a.conn.execute(
                "SELECT message_id FROM media WHERE vcard IS NOT NULL").fetchone()[0]
        with urllib.request.urlopen(f"{self.base}/api/messages/{mid}/vcard") as r:
            body = r.read().decode()
        self.assertIn("BEGIN:VCARD", body)
        self.assertEqual(r.headers["Content-Type"], "text/vcard; charset=utf-8")

    def test_export_endpoint_serves_each_format(self):
        # Pin to the DM by jid rather than taking chats[0]: list order is a
        # presentation choice that changes, and only this chat has a vCard.
        with self.archive() as a:
            cid = a.conn.execute("SELECT id FROM chats WHERE jid = ?",
                                 (fixture.DM_JID,)).fetchone()[0]
        for fmt in ("html", "csv", "txt", "vcf"):
            with urllib.request.urlopen(f"{self.base}/api/chats/{cid}/export?format={fmt}") as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(r.read(), f"{fmt} export was empty")

    def test_empty_chats_are_hidden_and_pins_float_to_the_top(self):
        """WhatsApp keeps thousands of empty sessions, and pins a chat by adding
        a ~1000-year offset to its date — which reads as the year 9026."""
        chats = self.get("/api/chats")
        with self.archive() as a:
            with_messages = a.conn.execute(
                "SELECT COUNT(*) FROM chats c WHERE EXISTS"
                "(SELECT 1 FROM messages m WHERE m.chat_id = c.id)").fetchone()[0]
        self.assertEqual(len(chats), with_messages)
        for c in chats:
            self.assertGreater(c["message_count"], 0)
            # Dates must be real, never WhatsApp's pin-sorting sentinel.
            self.assertLess(c["last_message_at"], 4_102_444_800)
        ordering = [(not c["pinned"], -(c["last_message_at"] or 0)) for c in chats]
        self.assertEqual(ordering, sorted(ordering), "pinned chats are not first")

    # --- media -----------------------------------------------------------
    def _a_media_path(self, kind=None):
        with self.archive() as a:
            sql = "SELECT path FROM media WHERE path IS NOT NULL"
            args = ()
            if kind:
                sql += " AND kind = ?"
                args = (kind,)
            return a.conn.execute(sql + " LIMIT 1", args).fetchone()[0]

    def test_media_is_served_with_range_support(self):
        path = self._a_media_path()
        with urllib.request.urlopen(f"{self.base}/media/{path}") as r:
            full = r.read()
            self.assertEqual(r.headers["Accept-Ranges"], "bytes")

        req = urllib.request.Request(f"{self.base}/media/{path}",
                                     headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.read(), full[10:20])

        req = urllib.request.Request(f"{self.base}/media/{path}",
                                     headers={"Range": "bytes=-5"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.read(), full[-5:])

    def test_voice_note_conversion_is_validated(self):
        """Safari cannot play Opus-in-Ogg, so `?as=m4a` converts on demand.

        Only that one conversion exists, and only for formats that need it.
        """
        path = self._a_media_path()          # an image — no conversion needed
        self.assertEqual(self.status(f"/media/{path}?as=exe"), 400)
        self.assertEqual(self.status(f"/media/{path}?as=m4a"), 400)

    def test_conversion_cache_lives_outside_the_archive(self):
        cache = self.httpd.transcode_dir
        self.assertTrue(cache.is_dir())
        self.assertFalse(cache.is_relative_to(self.archive_root),
                         "converted audio must not be written into the archive")

    def test_unsatisfiable_range_is_416(self):
        path = self._a_media_path()
        self.assertEqual(
            self.status(f"/media/{path}", headers={"Range": "bytes=99999999-"}), 416)

    def test_voice_note_content_type_is_playable(self):
        path = self._a_media_path("audio")
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("HEAD", f"/media/{path}")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.headers["Content-Type"], "audio/mp4")
        c.close()

    # --- safety ----------------------------------------------------------
    def test_media_path_traversal_is_refused(self):
        for attempt in ("../../archive.db", "..%2f..%2farchive.db",
                        "%2e%2e/%2e%2e/etc/passwd", "....//archive.db"):
            self.assertEqual(self.status("/media/" + attempt), 404, attempt)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.status("/api/summary", headers={"Host": "evil.example.com"}), 403)
        self.assertEqual(self.status("/api/summary", headers={"Host": "localhost"}), 200)

    def test_write_methods_are_unavailable(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertGreaterEqual(self.status("/api/summary", method=method), 400)

    def test_archive_is_opened_read_only(self):
        with self.archive() as a:
            with self.assertRaises(sqlite3.OperationalError):
                a.conn.execute("DELETE FROM messages")

    def test_unknown_routes_and_ids_are_404(self):
        self.assertEqual(self.status("/api/nope"), 404)
        self.assertEqual(self.status("/api/chats/99999/messages"), 404)

    def test_database_connections_do_not_leak(self):
        """Regression: a thread-local cache leaked one connection per request.

        ThreadingHTTPServer runs each request on a fresh thread, so anything
        cached per thread is never reused and never closed. Left unfixed, a
        long reading session exhausts the process's file descriptors.
        """
        import gc

        def open_connections():
            gc.collect()
            return sum(1 for o in gc.get_objects()
                       if isinstance(o, sqlite3.Connection))

        for _ in range(15):                       # warm up, settle any caches
            self.get("/api/summary")
        before = open_connections()
        for _ in range(60):
            self.get("/api/summary")
            self.get("/api/chats")
        after = open_connections()
        self.assertLessEqual(after - before, 2,
                             f"leaked {after - before} sqlite connections over 120 requests")


class TestStreamingEngine(unittest.TestCase):
    """Drive the backup engine against a stand-in for pymobiledevice3.

    This is the one code path that touches a real iPhone, so it is also the one
    that silently rotted when pymobiledevice3 went async in 10.x:
    `create_using_usbmux` became a coroutine and the old synchronous call site
    handed a coroutine object to the service, which failed only once a device
    was actually plugged in. Faking the library exercises the call sequence
    without hardware, in both the async and the legacy synchronous shape.
    """

    UDID = "00008110-000A4D2E1E38801E"

    def _install(self, async_api: bool, dest: Path):
        import types
        from dataclasses import dataclass

        calls = {"awaited_backup": False, "closed": [], "kwargs": None}

        @dataclass(frozen=True)
        class BackupSelectionRule:
            domain: str
            relative_path: str
            recursive: bool = False

        udid = self.UDID

        class Lockdown:
            def __init__(self):
                self.udid = udid

            def close(self):
                calls["closed"].append("lockdown")
                if async_api:
                    async def _c():
                        return None
                    return _c()

        class Mobilebackup2Service:
            def __init__(self, lockdown):
                # The bug: a coroutine object was passed here instead of a
                # connected client, so this assertion is the regression guard.
                assert isinstance(lockdown, Lockdown), \
                    f"expected a connected lockdown client, got {type(lockdown).__name__}"
                self.lockdown = lockdown

            @staticmethod
            def selection_filter_callback(rules):
                def keep(backup_file):
                    for r in rules:
                        if backup_file.domain != r.domain:
                            continue
                        if r.recursive:
                            if backup_file.relative_path.startswith(r.relative_path):
                                return True
                        elif backup_file.relative_path == r.relative_path:
                            return True
                    return False
                return keep

            async def backup(self, **kwargs):
                calls["awaited_backup"] = True
                calls["kwargs"] = kwargs
                d = Path(kwargs["backup_directory"]) / udid
                d.mkdir(parents=True, exist_ok=True)
                (d / "Manifest.db").write_bytes(b"fake")
                kwargs["progress_callback"](50)
                kwargs["progress_callback"](100)

            def close(self):
                calls["closed"].append("service")
                if async_api:
                    async def _c():
                        return None
                    return _c()

        def create_using_usbmux(serial=None, **kw):
            calls["serial"] = serial
            if async_api:
                async def _c():
                    return Lockdown()
                return _c()
            return Lockdown()

        pkg = types.ModuleType("pymobiledevice3")
        services = types.ModuleType("pymobiledevice3.services")
        mb = types.ModuleType("pymobiledevice3.services.mobilebackup2")
        ld = types.ModuleType("pymobiledevice3.lockdown")
        mb.Mobilebackup2Service = Mobilebackup2Service
        mb.BackupSelectionRule = BackupSelectionRule
        ld.create_using_usbmux = create_using_usbmux

        saved = {k: sys.modules.get(k) for k in
                 ("pymobiledevice3", "pymobiledevice3.services",
                  "pymobiledevice3.services.mobilebackup2", "pymobiledevice3.lockdown")}
        sys.modules.update({
            "pymobiledevice3": pkg,
            "pymobiledevice3.services": services,
            "pymobiledevice3.services.mobilebackup2": mb,
            "pymobiledevice3.lockdown": ld,
        })

        def restore():
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.addCleanup(restore)
        return calls

    def _run(self, async_api: bool):
        import warnings
        from wa_archive import engine_pmd3

        tmp = Path(tempfile.mkdtemp(prefix="wa-engine-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        calls = self._install(async_api, tmp)

        seen = []
        with warnings.catch_warnings():
            # An un-awaited coroutine is exactly how the real failure showed up.
            warnings.simplefilter("error", RuntimeWarning)
            out = engine_pmd3.backup_whatsapp_only(
                tmp, "hunter2", udid=self.UDID, business=False,
                with_media=True, full=False, on_progress=seen.append)

        self.assertEqual(out, tmp / self.UDID)
        self.assertTrue((out / "Manifest.db").is_file())
        self.assertTrue(calls["awaited_backup"], "backup() was never awaited")
        self.assertEqual(calls["serial"], self.UDID)
        self.assertEqual(seen, [50, 100], "progress was not reported")
        self.assertIn("service", calls["closed"])
        self.assertIn("lockdown", calls["closed"])
        return calls

    def test_async_api_is_awaited(self):
        """pymobiledevice3 10.x: connection and teardown are coroutines."""
        calls = self._run(async_api=True)
        self.assertEqual(calls["kwargs"]["password"], "hunter2")
        self.assertFalse(calls["kwargs"]["full"])

    def test_legacy_sync_api_still_works(self):
        """pymobiledevice3 4.x: the same calls are plain functions."""
        self._run(async_api=False)

    def test_filter_passed_to_backup_selects_whatsapp_only(self):
        from dataclasses import dataclass

        calls = self._run(async_api=True)
        keep = calls["kwargs"]["filter_callback"]

        @dataclass
        class F:
            domain: str
            relative_path: str

        self.assertTrue(keep(F("AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
                               "ChatStorage.sqlite")))
        self.assertTrue(keep(F("AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
                               "Message/Media/x/1/IMG.jpg")))
        self.assertFalse(keep(F("HomeDomain", "Library/SMS/sms.db")))
        self.assertFalse(keep(F("CameraRollDomain", "Media/DCIM/IMG_0001.HEIC")))

    def test_purge_request_does_not_abort_when_space_is_available(self):
        """Regression: iOS asks the host to free space as part of the normal
        handshake, and pymobiledevice3 aborted the whole backup rather than
        answering — even with 100 GB free."""
        import asyncio as aio
        from pymobiledevice3.services import device_link
        from wa_archive.engine_pmd3 import install_purge_disk_space_handler

        install_purge_disk_space_handler()

        class Link:
            def __init__(self, root):
                self.root_path = Path(root)
                self.sent = None

            async def status_response(self, code, s="", d=None):
                self.sent = (code, s, d)

        link = Link(tempfile.gettempdir())
        aio.run(device_link.DeviceLink.purge_disk_space(link, ["DLMessagePurgeDiskSpace", 10 ** 10]))
        self.assertIsNotNone(link.sent, "no reply was sent to the device")
        self.assertEqual(link.sent[1], "Operation not supported")

    def test_purge_request_still_refuses_a_genuinely_full_disk(self):
        import asyncio as aio
        from unittest import mock
        from pymobiledevice3.exceptions import NotEnoughDiskSpaceError
        from pymobiledevice3.services import device_link
        from wa_archive.engine_pmd3 import install_purge_disk_space_handler

        install_purge_disk_space_handler()

        class Link:
            root_path = Path(tempfile.gettempdir())

            async def status_response(self, *a, **kw):
                raise AssertionError("should not have replied on a full disk")

        tiny = type("U", (), {"free": 50 * 1024 ** 2})()
        with mock.patch.object(shutil, "disk_usage", return_value=tiny):
            with self.assertRaises(NotEnoughDiskSpaceError):
                aio.run(device_link.DeviceLink.purge_disk_space(Link(), []))

    def test_missing_manifest_is_reported(self):
        from wa_archive import engine_pmd3

        tmp = Path(tempfile.mkdtemp(prefix="wa-engine-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        calls = self._install(True, tmp)
        import pymobiledevice3.services.mobilebackup2 as mb

        async def empty_backup(self, **kwargs):
            calls["awaited_backup"] = True

        mb.Mobilebackup2Service.backup = empty_backup
        with self.assertRaises(RuntimeError):
            engine_pmd3.backup_whatsapp_only(tmp, "pw", udid=self.UDID)


if __name__ == "__main__":
    unittest.main(verbosity=2)

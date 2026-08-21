# wa-archive

A private, searchable archive of your WhatsApp history, built from an encrypted
iPhone backup and read in your browser. Everything stays on your own machine.

Built and proven against a real archive: **1.2 million messages across 6,028
chats and 14 years**, with 83,577 attachments.

---

## Starting the reader

Once an archive exists, reading it has nothing to do with your iPhone. You do
not need the phone connected, and nothing is backed up or changed.

**Double-click `Open WhatsApp Archive.command` on your Desktop.** That is the
whole thing — it opens your browser and serves the archive. Create it once
with:

```bash
wa-archive --archive /path/to/WhatsAppArchive install-launcher --reader-only
```

macOS blocks unsigned `.command` files the first time: right-click → Open →
Open. After that a normal double-click works.

Prefer the terminal? Two lines:

```bash
cd /path/to/WA-Archive-App && source .venv/bin/activate
wa-archive --archive /path/to/WhatsAppArchive serve
```

Then open **http://127.0.0.1:8730**. Press `Ctrl-C` in the terminal to stop it.

Drop `--archive` entirely if your archive is at the default `~/WhatsAppArchive`.
If it lives on an external drive, that drive has to be plugged in — the
launcher says so plainly rather than failing with a stack trace.

Use **Chrome** if you can. Safari works, but voice notes there rely on
on-demand conversion (see below).

## First-time setup

Only needed once, to create the archive in the first place:

```bash
brew install libimobiledevice        # talks to the iPhone over USB
pip install -e .
wa-archive sync                      # full backup, then extract — 30-90 min
```

Connect the iPhone by cable, unlock it, and set **Auto-Lock → Never** before
starting: a phone that locks mid-transfer ends the session.

---

## Why this exists

WhatsApp's own export is per-chat, capped, and drops media. Commercial viewers
want a subscription and your data on their machine. This keeps the archive as
**plain SQLite plus a folder of files** — readable with or without this tool,
in ten years, by anything that speaks SQL.

## How it works

```
iPhone ──USB──> encrypted backup ──decrypt──> ChatStorage.sqlite ──normalise──> archive.db
                                            + Message/Media/**                 + media/
                                                                                    │
                                                                    reader ─────────┘
                                                                    export
```

1. **Back up** — `idevicebackup2` or pymobiledevice3 pulls an encrypted backup
   over the cable. WhatsApp data is only reliably present in *encrypted*
   backups, so `sync` turns encryption on if it is off.
2. **Extract** — the WhatsApp database and media tree are decrypted out of the
   backup into a staging folder.
3. **Normalise** — `ChatStorage.sqlite` (Core Data) becomes a clean schema:
   chats, contacts, messages, media, with the phone's numeric codes translated.
4. **Read** — a local, read-only HTTP reader; or export to html / csv / txt / vcf.

## Commands

| | |
|---|---|
| `sync` | back up the iPhone and fold new messages into the archive |
| `serve` | open the reader in a browser |
| `ingest` | extract from a backup that already exists (no phone needed) |
| `list` | show the local iOS backups it can see |
| `stats` | message, chat, contact and media totals |
| `search` | full-text search from the terminal |
| `export` | write chats out as html / csv / txt / vcf |
| `prune` | delete non-WhatsApp files from a device backup to reclaim space |
| `audit` | check the inferred code mappings against your own database |
| `install-launcher` | Desktop shortcut — `--reader-only` to just open the archive |
| `forget-passphrase` | remove the saved passphrase from the Keychain |

Every command takes `--archive PATH` if the archive isn't at `~/WhatsAppArchive`.

## The reader

`wa-archive serve` opens a three-pane reader at `127.0.0.1:8730`.

- **Chat list** — ordered like WhatsApp, pinned chats first (📌). Chats with no
  messages are hidden; WhatsApp keeps thousands of empty sessions.
- **Conversation** — day separators, per-sender colours, read receipts,
  inline photos, video, voice notes and documents. Deleted messages and
  attachments missing from the backup are shown as such rather than hidden.
- **Density rail** — message volume per month down the right edge. Click a bar
  to jump to that month. For a 14-year chat this beats scrolling.
- **Reading order** — `↓ newest first` reads like a messenger; `↑ oldest first`
  starts at the conversation's beginning and pages forward.
- **Search** — press `/`. Prefix matching by default, so `kirim` finds
  `mengirimkan`. Scope it to the open chat with the `all` / `this chat` toggle.
  Results say how many matches exist, not just how many are shown.
- **Attachments** — tabs for images, video, audio, documents, links, stickers,
  contacts and locations, with counts. Opens scoped to the chat you are reading,
  like WhatsApp's "Media, links and docs". Searchable by filename and caption.
- **Back** — `←` or `Esc` returns you to the search results or attachment tab
  you came from, at the scroll position you left.

### Voice notes and Safari

WhatsApp records voice notes as Opus in an Ogg container. Chrome and Firefox
play it; **Safari does not**. If ffmpeg is installed the reader offers an AAC
conversion as a second source and Safari picks it up automatically — converted
on demand, cached outside the archive, originals untouched. Without ffmpeg,
use Chrome or the download link under each player.

## Where everything lives

```
<archive>/
├── archive.db        every message, chat and contact — plain SQLite
├── media/            attachments, named by content hash
│   └── ab/abc123….jpg
└── device-backup/    the raw iOS backup (only needed for incremental syncs)
```

Default `<archive>` is `~/WhatsAppArchive`. Keeping it on an external drive
works — pass `--archive /Volumes/YourDrive/WhatsAppArchive` — but the reader
cannot start when that drive is unplugged.

Also on the system:

| What | Where |
|---|---|
| Backup passphrase | login Keychain, item `wa-archive-backup-passphrase` |
| Staging (temporary) | system temp, or `ingest --staging PATH` |
| Converted voice notes | system temp, swept at startup and on exit |

## Disk space

A large account needs room in three places at once. On a laptop with a small
SSD and a big external drive, put staging and the archive on different volumes:

```bash
wa-archive --archive /Volumes/Drive/WhatsAppArchive ingest \
  --backup /Volumes/Drive/WhatsAppArchive/device-backup/<UDID> \
  --staging ~/wa-staging
```

Staging holds a decrypted copy of every attachment — tens of GB — and is
deleted at the end. Reading a backup and writing staging on the *same* USB
drive can drop the connection under sustained load; splitting them avoids it.

Once the archive exists, the raw backup is only there to make the next sync
incremental. `wa-archive prune <backup-dir>` strips it to WhatsApp only, which
on a real 139 GB backup reclaims most of the space. It asks first.

## Re-syncing

Every message carries a unique natural key, so re-ingesting is **additive**:
new messages are added, existing ones untouched, nothing duplicated. Run `sync`
every few months for years. The first backup takes 30–90 minutes; later ones
are incremental.

Ordering is by `(timestamp, id)`, never row id — a later sync gives newly
discovered *old* messages fresh high ids, and id order would put a 2021 message
after a 2026 one.

## What WhatsApp's schema does that surprises you

The awkward parts, all found against real data:

- **`ZGROUPEVENTTYPE` is set on ordinary messages**, not just system ones.
  Treating it alone as the marker filed 55% of a 1.2M-message archive as
  "participants added", hid the text, and dropped every attachment on those
  rows. An event needs a system *message type* as well.
- **Pinned chats get a ~1000-year offset** added to their sort date, producing
  chats dated 9026. The reader derives dates from messages and reads the offset
  as the pin flag it is.
- **Contact cards have no file** — the payload is a vCard string with no
  `ZMEDIALOCALPATH`, so a media-path check drops them entirely.
- **Message-type and status codes drift between releases.** Unmapped values are
  stored verbatim as `unknown:<n>`; nothing is silently discarded.

```bash
wa-archive audit ~/path/to/ChatStorage.sqlite
```

lists every code in *your* database and flags the unmapped ones.

## Privacy and safety

- Nothing leaves your machine. No network calls, no telemetry, no accounts.
- The reader binds `127.0.0.1`, opens the database read-only, serves only GET,
  refuses non-loopback `Host` headers, and cannot escape the media folder.
- The passphrase is never passed as a command-line argument — argv is visible
  to every process on the machine. It goes through the Keychain or an
  environment variable.
- **The archive is plaintext on disk and contains other people's messages.**
  Keep FileVault on and think before syncing it to cloud storage.
- A pruned or filtered backup **cannot be restored to a phone**. It is an
  archive, not a backup — keep using Finder if you want a restorable copy.

## Tests

```bash
python -m unittest discover -s tests -t .
```

76 tests, no iPhone and no network required. The suite builds a synthetic
`ChatStorage.sqlite`, wraps it in a fake encrypted-style backup, and runs the
whole pipeline through to the HTTP API — including the cases that actually
break in the field: unmapped codes, attachments referenced but absent,
duplicate bytes across chats, NULL stanza ids, contact cards with no file,
ordinary messages carrying event codes, and a backup that arrives holding
*older* messages than the ones already archived.

What it cannot cover is the transfer itself: `idevicebackup2` talking to a
phone, and decryption of a genuinely encrypted backup.

## Layout

```
wa_archive/
  selection.py     which domains and paths count as WhatsApp
  engine_pmd3.py   streaming-filter backup engine
  device.py        libimobiledevice control, Keychain, pruning
  sync.py          the one-click path, launcher generation
  backup.py        backup discovery, decryption, Manifest.db resolution
  extract.py       ChatStorage.sqlite -> canonical model  (the interesting file)
  store.py         archive writes, media vault, dedup, migrations
  schema.sql       canonical schema + FTS5
  query.py         plain text -> FTS5, shared by CLI and reader
  server.py        read-only localhost API
  web/index.html   the reader — one file, no build step, no CDN
  export.py        html / csv / txt / vcf
  cli.py           command line
tests/
  fixture.py       synthetic ChatStorage + fake backup
  test_archive.py  end-to-end coverage
```

The reader is **standard library only** — `http.server` and `sqlite3`. The two
dependencies are for getting data off the phone; once an archive exists,
reading it needs nothing but Python.

## Troubleshooting

**No iPhone detected** — unlock it, tap Trust, and check the cable carries data.
`idevice_id -l` should print an identifier.

**The connection drops mid-backup** — almost always the phone locking. iOS
re-prompts for the passcode *on the phone's screen*, never in the terminal, and
ends the session if it goes unanswered. Set **Auto-Lock → Never** before a full
sync. Re-running resumes rather than starting over.

**Backups live on an external drive** — macOS lets you symlink
`~/Library/Application Support/MobileSync/Backup` elsewhere. `wa-archive list`
detects this and names the volume instead of reporting "no backups found".

**The launcher won't open** — macOS blocks unsigned `.command` files the first
time: right-click → Open → Open.

## Known gaps

- **Contact names for `@lid` identifiers.** WhatsApp's newer privacy ids carry
  no phone number. Resolving them needs `ContactsV2.sqlite`, which is already
  in the backup but not yet read.
- **Reply threading.** `reply_to_id` exists in the schema but is never filled;
  the link lives in a protobuf blob that isn't parsed.
- **Reactions.** Table exists, never populated — same protobuf.
- **Links tab uses WhatsApp's own classification** (`type = 'link'`). URLs
  pasted into plain messages that WhatsApp never flagged — about 15% more —
  are not listed, because finding them costs a full table scan.
- **Attachment search is a substring match**, not the prefix-matching full-text
  search used for messages. Captions aren't in the FTS index.
- **Thumbnails.** Media is served at full size, so an image-heavy grid is
  heavier to load than it needs to be.
- **Narrow windows.** Below 760px the chat list is hidden with no way back, so
  the reader is effectively desktop-only.

## Licence

MIT.

-- Canonical archive schema.
--
-- Design rules that the rest of the codebase depends on:
--
--   * messages.source_key is UNIQUE. That is what makes re-ingesting a newer
--     backup additive instead of duplicating everything. INSERT OR IGNORE plus
--     this constraint is the whole idempotency story.
--   * Nothing is ever UPDATEd destructively or DELETEd during ingest. An
--     archive only grows.
--   * media.sha256 is the vault key: identical bytes are stored once, however
--     many messages reference them.
--   * messages_fts is an external-content FTS5 index over messages.body, kept
--     in step by triggers.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY,
    jid          TEXT NOT NULL UNIQUE,
    display_name TEXT,
    phone        TEXT,
    is_me        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chats (
    id              INTEGER PRIMARY KEY,
    jid             TEXT NOT NULL UNIQUE,
    name            TEXT,
    kind            TEXT NOT NULL,            -- dm | group | broadcast
    archived        INTEGER NOT NULL DEFAULT 0,
    last_message_at INTEGER
);

CREATE TABLE IF NOT EXISTS chat_participants (
    chat_id    INTEGER NOT NULL REFERENCES chats(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    PRIMARY KEY (chat_id, contact_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES chats(id),
    sender_id   INTEGER REFERENCES contacts(id),
    ts_utc      INTEGER NOT NULL,             -- seconds, UTC
    direction   TEXT NOT NULL,                -- in | out
    type        TEXT NOT NULL,                -- text | image | ... | unknown:<n>
    body        TEXT,
    is_starred  INTEGER NOT NULL DEFAULT 0,
    is_deleted  INTEGER NOT NULL DEFAULT 0,
    status      TEXT,                         -- outgoing delivery state
    event       TEXT,                         -- group event label, if any
    reply_to_id INTEGER REFERENCES messages(id),   -- always NULL for now
    source_key  TEXT NOT NULL UNIQUE,
    raw         TEXT                          -- JSON: original numeric codes
);

CREATE INDEX IF NOT EXISTS ix_messages_chat_ts ON messages(chat_id, ts_utc);
CREATE INDEX IF NOT EXISTS ix_messages_ts      ON messages(ts_utc);
CREATE INDEX IF NOT EXISTS ix_messages_sender  ON messages(sender_id);
-- Drives the attachment browser's Links tab.
CREATE INDEX IF NOT EXISTS ix_messages_type    ON messages(type);

CREATE TABLE IF NOT EXISTS media (
    id         INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    kind       TEXT NOT NULL,                 -- image | video | audio | document | sticker | contact | location
    sha256     TEXT,                          -- NULL when the file wasn't in the backup
    path       TEXT,                          -- relative to <archive>/media/
    orig_path  TEXT,                          -- ZMEDIALOCALPATH, for tracing
    mime       TEXT,
    vcard      TEXT,                          -- shared contact cards, for vcf export
    bytes      INTEGER,
    duration_s REAL,
    latitude   REAL,
    longitude  REAL,
    caption    TEXT,
    missing    INTEGER NOT NULL DEFAULT 0     -- referenced by a message, absent from the backup
);

CREATE INDEX IF NOT EXISTS ix_media_message ON media(message_id);
CREATE INDEX IF NOT EXISTS ix_media_sha     ON media(sha256);
-- Drives the attachment browser's per-kind tabs and listings.
CREATE INDEX IF NOT EXISTS ix_media_kind    ON media(kind, missing);

-- Populated by a future ZWAMESSAGEINFO protobuf parser; the shape is fixed now
-- so that adding it later is an ingest change, not a migration.
CREATE TABLE IF NOT EXISTS reactions (
    id         INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    sender_id  INTEGER REFERENCES contacts(id),
    emoji      TEXT NOT NULL,
    ts_utc     INTEGER,
    UNIQUE (message_id, sender_id, emoji)
);

CREATE TABLE IF NOT EXISTS ingests (
    id           INTEGER PRIMARY KEY,
    udid         TEXT,
    device_name  TEXT,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    new_messages INTEGER,
    new_media    INTEGER
);

-- Full-text search. External content: the index stores only the tokens and
-- points back at messages.id, so the body text is not duplicated on disk.
-- remove_diacritics 2 folds accents without breaking non-Latin scripts.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body,
    content='messages',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

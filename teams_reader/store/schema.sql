CREATE TABLE IF NOT EXISTS chats (
    chat_id       TEXT PRIMARY KEY,
    display_name  TEXT,
    kind          TEXT,
    last_msg_ts   INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    chat_id      TEXT,
    sender       TEXT,
    ts           INTEGER,
    text         TEXT,
    quoted_text  TEXT,
    source       TEXT,
    version      INTEGER,
    edited       INTEGER NOT NULL DEFAULT 0,
    deleted      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
    text,
    sender,
    content='messages',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text, sender) VALUES (new.rowid, new.text, new.sender);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text, sender) VALUES ('delete', old.rowid, old.text, old.sender);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text, sender ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text, sender) VALUES ('delete', old.rowid, old.text, old.sender);
    INSERT INTO messages_fts (rowid, text, sender) VALUES (new.rowid, new.text, new.sender);
END;

CREATE TABLE IF NOT EXISTS attachments (
    msg_id      TEXT,
    url         TEXT,
    local_path  TEXT,
    PRIMARY KEY (msg_id, url)
);

CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments (msg_id);

CREATE TABLE IF NOT EXISTS links (
    msg_id  TEXT,
    url     TEXT,
    PRIMARY KEY (msg_id, url)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id  INTEGER PRIMARY KEY,
    url            TEXT NOT NULL UNIQUE,
    chat_id        TEXT,
    call_id        TEXT,
    title          TEXT,
    started_ts     INTEGER,
    duration_s     INTEGER,
    status         TEXT NOT NULL DEFAULT 'pending',
    text           TEXT,
    speakers       TEXT,
    fetched_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_transcripts_chat ON transcripts (chat_id, started_ts);

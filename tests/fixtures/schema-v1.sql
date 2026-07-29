-- Frozen populated fixture for AIQ journal schema version 1.
-- Do not regenerate this file from the current production schema.
--
-- Before execution, test loaders must replace these literal tokens:
--   __AIQ_SCOPE_KIND__  -> the fixture JournalScope.kind
--   __AIQ_SCOPE_ROOT__  -> the fixture JournalScope.root
--   __AIQ_SCOPE_ID__    -> the fixture JournalScope.scope_id

BEGIN TRANSACTION;

CREATE TABLE journal_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE messages (
  message_id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL,
  source TEXT NOT NULL,
  content TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  session_id TEXT,
  turn_id TEXT,
  cwd TEXT
) STRICT;

CREATE TABLE events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  occurred_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message_id TEXT REFERENCES messages(message_id),
  task_id TEXT,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT;

CREATE INDEX events_message_sequence
  ON events(message_id, sequence);

CREATE TRIGGER messages_no_update
BEFORE UPDATE ON messages
BEGIN
  SELECT RAISE(ABORT, 'messages are append-only');
END;

CREATE TRIGGER messages_no_delete
BEFORE DELETE ON messages
BEGIN
  SELECT RAISE(ABORT, 'messages are append-only');
END;

CREATE TRIGGER events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;

INSERT INTO journal_metadata(key, value) VALUES
  ('schema_version', '1'),
  ('scope_kind', '__AIQ_SCOPE_KIND__'),
  ('scope_root', '__AIQ_SCOPE_ROOT__'),
  ('scope_id', '__AIQ_SCOPE_ID__');

INSERT INTO messages(
  message_id,
  received_at,
  source,
  content,
  content_sha256,
  idempotency_key,
  session_id,
  turn_id,
  cwd
) VALUES (
  'msg_existing',
  '2026-01-01T00:00:00.000000+00:00',
  'user',
  'preserve exactly',
  '783708352c1d00ef8c629f084f15add96afc3e574eb3900b8e26864b4569cd5b',
  'fixture:schema-v1:existing',
  'fixture-session',
  'fixture-turn',
  '__AIQ_SCOPE_ROOT__'
);

INSERT INTO events(
  sequence,
  event_id,
  occurred_at,
  event_type,
  message_id,
  task_id,
  payload_json
) VALUES (
  1,
  'evt_existing',
  '2026-01-01T00:00:00.000000+00:00',
  'message.received',
  'msg_existing',
  NULL,
  '{}'
);

COMMIT;

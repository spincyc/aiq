-- Frozen populated fixture for AIQ journal schema version 4.
-- Do not regenerate this file from the current production schema.
--
-- Schema 4 is the version every deployed journal sits at, so this file is
-- the on-disk shape the 4 -> 5 migration actually has to accept. It was
-- captured once, by `.dump`, from a journal whose ledger the public API
-- wrote -- ingest, enqueue, claim, reader lease -- and whose container was
-- then downgraded to schema 4 by copying those rows into a schema-4
-- database without the two claim columns schema 5 adds. Generated
-- identifiers were renamed to readable ones; nothing else was edited.
--
-- It is frozen deliberately. Building the baseline by calling
-- SCHEMA_V2/V3/V4_STATEMENTS from the test instead would let a later bug
-- in those helpers migrate a journal no user has ever had, and the test
-- would still pass.
--
-- The claims deliberately carry no holder locator: that is what every
-- claim taken before schema 5 looks like. The reader lease does carry
-- one, because schema 4 already gave it columns to record one in, and
-- the 4 -> 5 ALTER must leave that row untouched.
--
-- `.dump` emits table data before triggers and views, so the insert
-- validators are absent while the rows land and present afterwards.
--
-- Before execution, test loaders must replace these literal tokens:
--   __AIQ_SCOPE_KIND__  -> the fixture JournalScope.kind
--   __AIQ_SCOPE_ROOT__  -> the fixture JournalScope.root
--   __AIQ_SCOPE_ID__    -> the fixture JournalScope.scope_id

BEGIN TRANSACTION;
CREATE TABLE claim_releases (
      claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
      event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      disposition TEXT NOT NULL CHECK (
        disposition IN (
          'released', 'applied', 'completed', 'revoked', 'expired',
          'needs_input', 'failed'
        )
      ),
      released_at_us INTEGER NOT NULL
    ) STRICT
    ;
INSERT INTO "claim_releases" VALUES('clm_enqueue_applied',6,'applied',1785443668584924);
CREATE TABLE claims (
      claim_id TEXT PRIMARY KEY,
      resource_kind TEXT NOT NULL CHECK (
        resource_kind IN ('message', 'task')
      ),
      resource_id TEXT NOT NULL,
      owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 200),
      fence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      basis_revision INTEGER CHECK (
        basis_revision IS NULL OR basis_revision > 0
      ),
      acquired_at_us INTEGER NOT NULL,
      expires_at_us INTEGER NOT NULL CHECK (expires_at_us > acquired_at_us),
      CHECK (
        (resource_kind = 'message' AND basis_revision IS NULL)
        OR
        (resource_kind = 'task' AND basis_revision IS NOT NULL)
      )
    ) STRICT
    ;
INSERT INTO "claims" VALUES('clm_enqueue_applied','message','msg_enqueue_request','legacy-worker',3,NULL,1785443668583803,1785444568583803);
INSERT INTO "claims" VALUES('clm_message','message','msg_claimed','legacy-worker',8,NULL,1785443668585421,1785447268585421);
INSERT INTO "claims" VALUES('clm_task','task','TASK-1','legacy-worker',10,1,1785443668586478,1785447268586478);
CREATE TABLE events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  occurred_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message_id TEXT REFERENCES messages(message_id),
  task_id TEXT,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT;
INSERT INTO "events" VALUES(1,'evt_01','2026-07-30T20:34:28.583634+00:00','message.received','msg_claimed',NULL,'{}');
INSERT INTO "events" VALUES(2,'evt_02','2026-07-30T20:34:28.584475+00:00','message.received','msg_enqueue_request',NULL,'{}');
INSERT INTO "events" VALUES(3,'evt_03','2026-07-30T20:34:28.584538+00:00','claim.acquired','msg_enqueue_request',NULL,'{"claim_id":"clm_enqueue_applied","expires_at_us":1785444568583803,"owner_id":"legacy-worker"}');
INSERT INTO "events" VALUES(4,'evt_04','2026-07-30T20:34:28.584603+00:00','message.processing','msg_enqueue_request',NULL,'{"claim_id":"clm_enqueue_applied"}');
INSERT INTO "events" VALUES(5,'evt_05','2026-07-30T20:34:28.584787+00:00','task.created','msg_enqueue_request','TASK-1','{"effect":["create","$task",{"objective":"prove a v4 task ledger crosses to v5 intact","priority":0,"title":"survive the migration"}],"operation":"create"}');
INSERT INTO "events" VALUES(6,'evt_06','2026-07-30T20:34:28.584928+00:00','claim.consumed','msg_enqueue_request',NULL,'{"claim_id":"clm_enqueue_applied","disposition":"applied"}');
INSERT INTO "events" VALUES(7,'evt_07','2026-07-30T20:34:28.584988+00:00','message.applied','msg_enqueue_request',NULL,'{"effects_sha256":"da325ef2db24d930512cb3eb1200d683d51757eb63722028a01db9302c9a9cd2"}');
INSERT INTO "events" VALUES(8,'evt_08','2026-07-30T20:34:28.586236+00:00','claim.acquired','msg_claimed',NULL,'{"claim_id":"clm_message","expires_at_us":1785447268585421,"owner_id":"legacy-worker"}');
INSERT INTO "events" VALUES(9,'evt_09','2026-07-30T20:34:28.586309+00:00','message.processing','msg_claimed',NULL,'{"claim_id":"clm_message"}');
INSERT INTO "events" VALUES(10,'evt_10','2026-07-30T20:34:28.587255+00:00','claim.acquired',NULL,'TASK-1','{"basis_revision":1,"claim_id":"clm_task","expires_at_us":1785447268586478,"owner_id":"legacy-worker"}');
CREATE TABLE journal_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
INSERT INTO "journal_metadata" VALUES('schema_version','4');
INSERT INTO "journal_metadata" VALUES('scope_kind','__AIQ_SCOPE_KIND__');
INSERT INTO "journal_metadata" VALUES('scope_root','__AIQ_SCOPE_ROOT__');
INSERT INTO "journal_metadata" VALUES('scope_id','__AIQ_SCOPE_ID__');
INSERT INTO "journal_metadata" VALUES('project_label','fixture-project');
CREATE TABLE message_applications (
      message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
      claim_id TEXT NOT NULL UNIQUE REFERENCES claims(claim_id),
      effects_sha256 TEXT NOT NULL CHECK (
        length(effects_sha256) = 64
        AND effects_sha256 NOT GLOB '*[^0-9a-f]*'
      ),
      applied_at TEXT NOT NULL,
      applied_event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      effect_count INTEGER NOT NULL CHECK (effect_count >= 0),
      document_json TEXT NOT NULL CHECK (
        json_valid(document_json) AND json_type(document_json) = 'object'
      ),
      result_json TEXT NOT NULL CHECK (
        json_valid(result_json) AND json_type(result_json) = 'object'
      )
    ) STRICT
    ;
INSERT INTO "message_applications" VALUES('msg_enqueue_request','clm_enqueue_applied','da325ef2db24d930512cb3eb1200d683d51757eb63722028a01db9302c9a9cd2','2026-07-30T20:34:28.584988+00:00',7,1,'{"effects":[["create","$task",{"objective":"prove a v4 task ledger crosses to v5 intact","priority":0,"title":"survive the migration"}]],"expect":{},"v":1}','{"aliases":{"$task":"TASK-1"},"applied_sequence":7,"effects_sha256":"da325ef2db24d930512cb3eb1200d683d51757eb63722028a01db9302c9a9cd2","message_id":"msg_enqueue_request","replayed":false,"status":"applied","tasks":[{"blocked_by":[],"claim":null,"created_at":"2026-07-30T20:34:28.584760+00:00","created_by_message_id":"msg_enqueue_request","dependencies":[],"last_sequence":5,"objective":"prove a v4 task ledger crosses to v5 intact","parent_task_id":null,"priority":0,"reason":null,"recorded_state":"queued","revision":1,"state":"ready","superseded_by_task_id":null,"task_id":"TASK-1","title":"survive the migration","waiting_on":[]}]}');
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
INSERT INTO "messages" VALUES('msg_claimed','2026-07-30T20:34:28.583634+00:00','user','claimed before v5','a633ce03ff25b455583dc0caeaf2e08562b3549c074cc66f4d60b97e19cd12c0','fixture:schema-v4:claimed','fixture-session','fixture-turn','__AIQ_SCOPE_ROOT__');
INSERT INTO "messages" VALUES('msg_enqueue_request','2026-07-30T20:34:28.584475+00:00','cli','{"action":"enqueue","spec":{"objective":"prove a v4 task ledger crosses to v5 intact","priority":0,"title":"survive the migration"},"v":1}','79270fb807609cfd954fd46ef96746b0ca2655d6af3c81721707411eaf97d35a',NULL,NULL,NULL,'__AIQ_SCOPE_ROOT__');
CREATE TABLE reader_leases (
      lease_scope INTEGER PRIMARY KEY CHECK (lease_scope = 0),
      lease_id TEXT NOT NULL,
      epoch INTEGER NOT NULL CHECK (epoch > 0),
      owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 200),
      reader_id TEXT NOT NULL CHECK (length(reader_id) BETWEEN 1 AND 200),
      holder_host TEXT,
      holder_sid INTEGER CHECK (holder_sid IS NULL OR holder_sid > 0),
      acquired_at_us INTEGER NOT NULL,
      renewed_at_us INTEGER NOT NULL CHECK (renewed_at_us >= acquired_at_us),
      expires_at_us INTEGER NOT NULL CHECK (expires_at_us > acquired_at_us),
      released_at_us INTEGER CHECK (
        released_at_us IS NULL OR released_at_us >= acquired_at_us
      )
    ) STRICT
    ;
INSERT INTO "reader_leases" VALUES(0,'rdl_fixture',1,'legacy-worker','rdr_fixture','fixture-host',4242,1785443668585421,1785443668586478,1785445468586478,NULL);
CREATE TABLE schema_migrations (
      migration_id INTEGER PRIMARY KEY,
      from_version INTEGER NOT NULL,
      to_version INTEGER NOT NULL,
      migrated_at TEXT NOT NULL,
      backup_name TEXT
    ) STRICT
    ;
INSERT INTO "schema_migrations" VALUES(1,0,4,'2026-01-01T00:00:00.000000+00:00',NULL);
CREATE TABLE task_effects (
      message_id TEXT NOT NULL REFERENCES messages(message_id),
      effect_index INTEGER NOT NULL CHECK (effect_index >= 0),
      event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      operation TEXT NOT NULL CHECK (
        operation IN ('create', 'update', 'transition', 'require', 'unrequire')
      ),
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      payload_json TEXT NOT NULL CHECK (
        json_valid(payload_json) AND json_type(payload_json) = 'object'
      ),
      PRIMARY KEY (message_id, effect_index)
    ) STRICT
    ;
INSERT INTO "task_effects" VALUES('msg_enqueue_request',0,5,'create','TASK-1','{"effect":["create","$task",{"objective":"prove a v4 task ledger crosses to v5 intact","priority":0,"title":"survive the migration"}],"operation":"create"}');
CREATE TABLE task_numbers (
      task_number INTEGER PRIMARY KEY AUTOINCREMENT
    ) STRICT
    ;
INSERT INTO "task_numbers" VALUES(1);
CREATE TABLE task_revisions (
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      revision INTEGER NOT NULL CHECK (revision > 0),
      event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      state TEXT NOT NULL CHECK (
        state IN (
          'queued', 'ready', 'active', 'blocked',
          'done', 'canceled', 'superseded'
        )
      ),
      title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
      objective TEXT CHECK (objective IS NULL OR length(objective) <= 2000),
      priority INTEGER NOT NULL CHECK (priority BETWEEN -1000000 AND 1000000),
      parent_task_id TEXT REFERENCES tasks(task_id),
      dependencies_json TEXT NOT NULL CHECK (
        json_valid(dependencies_json)
        AND json_type(dependencies_json) = 'array'
      ),
      reason TEXT CHECK (reason IS NULL OR length(reason) <= 1000),
      superseded_by_task_id TEXT REFERENCES tasks(task_id),
      PRIMARY KEY (task_id, revision),
      CHECK (parent_task_id IS NULL OR parent_task_id <> task_id),
      CHECK (
        (state = 'superseded' AND superseded_by_task_id IS NOT NULL)
        OR
        (state <> 'superseded' AND superseded_by_task_id IS NULL)
      )
    ) STRICT
    ;
INSERT INTO "task_revisions" VALUES('TASK-1',1,5,'queued','survive the migration','prove a v4 task ledger crosses to v5 intact',0,NULL,'[]',NULL,NULL);
CREATE TABLE tasks (
      task_id TEXT PRIMARY KEY,
      task_number INTEGER NOT NULL UNIQUE
        REFERENCES task_numbers(task_number),
      created_at TEXT NOT NULL,
      created_by_message_id TEXT NOT NULL REFERENCES messages(message_id),
      created_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      CHECK (task_id = 'TASK-' || task_number)
    ) STRICT
    ;
INSERT INTO "tasks" VALUES('TASK-1',1,'2026-07-30T20:34:28.584760+00:00','msg_enqueue_request',5);
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
CREATE TRIGGER tasks_validate_insert
    BEFORE INSERT ON tasks
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.created_sequence
          AND event_type = 'task.created'
          AND message_id = NEW.created_by_message_id
          AND task_id = NEW.task_id
      )
      THEN RAISE(ABORT, 'task creation event mismatch')
      END;
    END;
CREATE TRIGGER task_effects_validate_insert
    BEFORE INSERT ON task_effects
    BEGIN
      SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM message_applications
        WHERE message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application is sealed')
      END;

      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.event_sequence
          AND message_id = NEW.message_id
          AND task_id = NEW.task_id
          AND event_type = CASE NEW.operation
            WHEN 'create' THEN 'task.created'
            WHEN 'update' THEN 'task.revised'
            WHEN 'transition' THEN 'task.state_changed'
            WHEN 'require' THEN 'task.dependency_added'
            WHEN 'unrequire' THEN 'task.dependency_removed'
          END
          AND payload_json = NEW.payload_json
      )
      THEN RAISE(ABORT, 'task effect event mismatch')
      END;
    END;
CREATE TRIGGER task_revisions_validate_insert
    BEFORE INSERT ON task_revisions
    BEGIN
      SELECT CASE WHEN NEW.revision <> COALESCE(
        (
          SELECT MAX(revision) + 1
          FROM task_revisions
          WHERE task_id = NEW.task_id
        ),
        1
      )
      THEN RAISE(ABORT, 'task revision is not contiguous')
      END;

      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM task_effects
        WHERE event_sequence = NEW.event_sequence
          AND task_id = NEW.task_id
      )
      THEN RAISE(ABORT, 'task revision effect mismatch')
      END;
    END;
CREATE TRIGGER message_applications_validate_insert
    BEFORE INSERT ON message_applications
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.applied_event_sequence
          AND event_type = 'message.applied'
          AND message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application event mismatch')
      END;

      SELECT CASE WHEN NEW.effect_count <> (
        SELECT COUNT(*)
        FROM task_effects
        WHERE message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application effect count mismatch')
      END;

      SELECT CASE WHEN NEW.effect_count > 0 AND (
        SELECT MIN(effect_index) <> 0
          OR MAX(effect_index) <> NEW.effect_count - 1
        FROM task_effects
        WHERE message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application effects are not contiguous')
      END;

      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN claim_releases AS release
          ON release.claim_id = claim.claim_id
        WHERE claim.claim_id = NEW.claim_id
          AND claim.resource_kind = 'message'
          AND claim.resource_id = NEW.message_id
          AND release.disposition = 'applied'
      )
      THEN RAISE(ABORT, 'message application claim mismatch')
      END;
    END;
CREATE TRIGGER claims_validate_insert
    BEFORE INSERT ON claims
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.fence
          AND event_type = 'claim.acquired'
          AND (
            (
              NEW.resource_kind = 'message'
              AND message_id = NEW.resource_id
              AND task_id IS NULL
            )
            OR
            (
              NEW.resource_kind = 'task'
              AND task_id = NEW.resource_id
              AND message_id IS NULL
            )
          )
      )
      THEN RAISE(ABORT, 'claim event mismatch')
      END;
    END;
CREATE TRIGGER claim_releases_validate_insert
    BEFORE INSERT ON claim_releases
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN events AS event ON event.sequence = NEW.event_sequence
        WHERE claim.claim_id = NEW.claim_id
          AND event.event_type = CASE NEW.disposition
            WHEN 'released' THEN 'claim.released'
            WHEN 'applied' THEN 'claim.consumed'
            WHEN 'completed' THEN 'claim.consumed'
            WHEN 'needs_input' THEN 'claim.consumed'
            WHEN 'failed' THEN 'claim.consumed'
            WHEN 'revoked' THEN 'claim.revoked'
            WHEN 'expired' THEN 'claim.expired'
          END
          AND json_extract(event.payload_json, '$.claim_id') = NEW.claim_id
          AND json_extract(event.payload_json, '$.disposition') = NEW.disposition
          AND (
            (
              claim.resource_kind = 'message'
              AND event.message_id = claim.resource_id
            )
            OR
            (
              claim.resource_kind = 'task'
              AND event.task_id = claim.resource_id
            )
          )
      )
      THEN RAISE(ABORT, 'claim release event mismatch')
      END;
    END;
CREATE VIEW current_tasks AS
    SELECT revision.*
    FROM task_revisions AS revision
    WHERE NOT EXISTS (
      SELECT 1
      FROM task_revisions AS newer
      WHERE newer.task_id = revision.task_id
        AND newer.revision > revision.revision
    );
CREATE INDEX task_revisions_latest
      ON task_revisions(task_id, revision DESC)
    ;
CREATE INDEX task_current_queue_order
      ON task_revisions(state, priority DESC, event_sequence, task_id)
    ;
CREATE INDEX events_task_sequence
      ON events(task_id, sequence)
      WHERE task_id IS NOT NULL
    ;
CREATE TRIGGER schema_migrations_no_update
            BEFORE UPDATE ON schema_migrations
            BEGIN
              SELECT RAISE(ABORT, 'schema_migrations is append-only');
            END;
CREATE TRIGGER schema_migrations_no_delete
            BEFORE DELETE ON schema_migrations
            BEGIN
              SELECT RAISE(ABORT, 'schema_migrations is append-only');
            END;
CREATE TRIGGER task_numbers_no_update
            BEFORE UPDATE ON task_numbers
            BEGIN
              SELECT RAISE(ABORT, 'task_numbers is append-only');
            END;
CREATE TRIGGER task_numbers_no_delete
            BEFORE DELETE ON task_numbers
            BEGIN
              SELECT RAISE(ABORT, 'task_numbers is append-only');
            END;
CREATE TRIGGER tasks_no_update
            BEFORE UPDATE ON tasks
            BEGIN
              SELECT RAISE(ABORT, 'tasks is append-only');
            END;
CREATE TRIGGER tasks_no_delete
            BEFORE DELETE ON tasks
            BEGIN
              SELECT RAISE(ABORT, 'tasks is append-only');
            END;
CREATE TRIGGER task_revisions_no_update
            BEFORE UPDATE ON task_revisions
            BEGIN
              SELECT RAISE(ABORT, 'task_revisions is append-only');
            END;
CREATE TRIGGER task_revisions_no_delete
            BEFORE DELETE ON task_revisions
            BEGIN
              SELECT RAISE(ABORT, 'task_revisions is append-only');
            END;
CREATE TRIGGER task_effects_no_update
            BEFORE UPDATE ON task_effects
            BEGIN
              SELECT RAISE(ABORT, 'task_effects is append-only');
            END;
CREATE TRIGGER task_effects_no_delete
            BEFORE DELETE ON task_effects
            BEGIN
              SELECT RAISE(ABORT, 'task_effects is append-only');
            END;
CREATE TRIGGER message_applications_no_update
            BEFORE UPDATE ON message_applications
            BEGIN
              SELECT RAISE(ABORT, 'message_applications is append-only');
            END;
CREATE TRIGGER message_applications_no_delete
            BEFORE DELETE ON message_applications
            BEGIN
              SELECT RAISE(ABORT, 'message_applications is append-only');
            END;
CREATE TRIGGER claims_no_update
            BEFORE UPDATE ON claims
            BEGIN
              SELECT RAISE(ABORT, 'claims is append-only');
            END;
CREATE TRIGGER claims_no_delete
            BEFORE DELETE ON claims
            BEGIN
              SELECT RAISE(ABORT, 'claims is append-only');
            END;
CREATE TRIGGER claim_releases_no_update
            BEFORE UPDATE ON claim_releases
            BEGIN
              SELECT RAISE(ABORT, 'claim_releases is append-only');
            END;
CREATE TRIGGER claim_releases_no_delete
            BEFORE DELETE ON claim_releases
            BEGIN
              SELECT RAISE(ABORT, 'claim_releases is append-only');
            END;
CREATE TRIGGER messages_no_replace
            BEFORE INSERT ON messages
            WHEN 
      EXISTS (
        SELECT 1 FROM messages WHERE message_id = NEW.message_id
      )
      OR (
        NEW.idempotency_key IS NOT NULL
        AND EXISTS (
          SELECT 1
          FROM messages
          WHERE idempotency_key = NEW.idempotency_key
        )
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'messages is append-only');
            END;
CREATE TRIGGER events_no_replace
            BEFORE INSERT ON events
            WHEN 
      EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.sequence OR event_id = NEW.event_id
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'events is append-only');
            END;
CREATE TRIGGER schema_migrations_no_replace
            BEFORE INSERT ON schema_migrations
            WHEN 
      EXISTS (
        SELECT 1
        FROM schema_migrations
        WHERE migration_id = NEW.migration_id
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'schema_migrations is append-only');
            END;
CREATE TRIGGER task_numbers_no_replace
            BEFORE INSERT ON task_numbers
            WHEN 
      EXISTS (
        SELECT 1
        FROM task_numbers
        WHERE task_number = NEW.task_number
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'task_numbers is append-only');
            END;
CREATE TRIGGER tasks_no_replace
            BEFORE INSERT ON tasks
            WHEN 
      EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = NEW.task_id
          OR task_number = NEW.task_number
          OR created_sequence = NEW.created_sequence
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'tasks is append-only');
            END;
CREATE TRIGGER task_revisions_no_replace
            BEFORE INSERT ON task_revisions
            WHEN 
      EXISTS (
        SELECT 1
        FROM task_revisions
        WHERE (task_id = NEW.task_id AND revision = NEW.revision)
          OR event_sequence = NEW.event_sequence
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'task_revisions is append-only');
            END;
CREATE TRIGGER task_effects_no_replace
            BEFORE INSERT ON task_effects
            WHEN 
      EXISTS (
        SELECT 1
        FROM task_effects
        WHERE (message_id = NEW.message_id AND effect_index = NEW.effect_index)
          OR event_sequence = NEW.event_sequence
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'task_effects is append-only');
            END;
CREATE TRIGGER message_applications_no_replace
            BEFORE INSERT ON message_applications
            WHEN 
      EXISTS (
        SELECT 1
        FROM message_applications
        WHERE message_id = NEW.message_id
          OR applied_event_sequence = NEW.applied_event_sequence
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'message_applications is append-only');
            END;
CREATE TRIGGER claims_no_replace
            BEFORE INSERT ON claims
            WHEN 
      EXISTS (
        SELECT 1
        FROM claims
        WHERE claim_id = NEW.claim_id OR fence = NEW.fence
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'claims is append-only');
            END;
CREATE TRIGGER claim_releases_no_replace
            BEFORE INSERT ON claim_releases
            WHEN 
      EXISTS (
        SELECT 1
        FROM claim_releases
        WHERE claim_id = NEW.claim_id OR event_sequence = NEW.event_sequence
      )
    
            BEGIN
              SELECT RAISE(ABORT, 'claim_releases is append-only');
            END;
CREATE INDEX claims_resource_lookup
      ON claims(resource_kind, resource_id)
    ;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',10);
INSERT INTO "sqlite_sequence" VALUES('task_numbers',1);
COMMIT;

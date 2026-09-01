-- 001_initial — the v1 schema.
--
-- Byte-identical to data/schema.sql except that the connection PRAGMAs
-- (journal_mode, synchronous, foreign_keys, busy_timeout) are omitted: they are
-- per-connection settings applied by data/db.py::open_rw(), and journal_mode
-- cannot be changed from inside a transaction, which is where a migration runs.
--
-- FROZEN. A shipped migration is never edited — schema changes land in a new
-- 00N_*.sql and bump schema_meta.schema_version.

-- ─── writer: data/db.py ───────────────────────────────────────────────────────
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- ─── writer: sync/accounts.py ─────────────────────────────────────────────────
CREATE TABLE accounts (
  id            TEXT PRIMARY KEY,
  remote        TEXT NOT NULL UNIQUE,
  kind          TEXT NOT NULL DEFAULT 'personal',   -- personal | business
  display_name  TEXT,
  email         TEXT,
  drive_id      TEXT,
  drive_type    TEXT,
  sync_root     TEXT NOT NULL,
  avatar_png    BLOB,
  enabled       INTEGER NOT NULL DEFAULT 1,
  added_at      TEXT NOT NULL,
  last_ok_at    TEXT,
  quota_total   INTEGER, quota_used INTEGER, quota_trashed INTEGER, quota_at TEXT
);

-- ─── writer: data/repo_sync.py (set by sync/supervisor.py) ────────────────────
-- Hazards that must survive a SIGKILL. Feed ladder rungs 5-7.
CREATE TABLE latches (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,   -- needs_resync|bisync_critical|quota_exceeded|mount_failed|orphan_cache
  set_at     TEXT NOT NULL,
  detail     TEXT,
  counter    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, name)
);

-- ─── writer: sync/activity.py ─────────────────────────────────────────────────
-- core/transferred keeps only 100 entries, is wiped by core/stats-reset, and is
-- lost on any daemon restart. History must live here.
CREATE TABLE activity (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path     TEXT NOT NULL,
  name         TEXT NOT NULL,
  is_dir       INTEGER NOT NULL DEFAULT 0,
  verb         TEXT NOT NULL,   -- uploaded|downloaded|modified|created|deleted|renamed|moved
                                -- |shared|restored|pinned|freed
  direction    TEXT,            -- up|down|local|remote
  state        TEXT NOT NULL,   -- inflight|done|error|cancelled|interrupted
  bytes        INTEGER NOT NULL DEFAULT 0,
  size         INTEGER NOT NULL DEFAULT 0,
  started_at   TEXT NOT NULL,
  completed_at TEXT,
  error        TEXT,
  error_kind   TEXT,
  job_group    TEXT,
  run_id       TEXT,
  src_fs       TEXT,
  dst_fs       TEXT,
  dedupe_key   TEXT             -- sha1(group|name|completed_at)
);
CREATE INDEX        ix_activity_recent ON activity(account_id, started_at DESC);
CREATE INDEX        ix_activity_path   ON activity(account_id, rel_path);
CREATE UNIQUE INDEX ux_activity_dedupe ON activity(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE TRIGGER trg_activity_cap AFTER INSERT ON activity BEGIN
  DELETE FROM activity WHERE account_id = NEW.account_id AND id NOT IN (
    SELECT id FROM activity WHERE account_id = NEW.account_id ORDER BY id DESC LIMIT 5000);
END;

-- ─── writer: sync/issues.py ───────────────────────────────────────────────────
CREATE TABLE issues (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  code          TEXT NOT NULL,                      -- IssueCode
  severity      TEXT NOT NULL,                      -- blocking|error|warning|info
  rel_path      TEXT,
  title         TEXT NOT NULL,                      -- already user-worded
  detail        TEXT,
  raw_error     TEXT,                               -- diagnostics only, never shown raw
  actions       TEXT NOT NULL DEFAULT '[]',         -- JSON list[RecoveryAction]
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  occurrences   INTEGER NOT NULL DEFAULT 1,
  resolved_at   TEXT,
  resolution    TEXT,                               -- retried|renamed|ignored|deleted|auto
  muted         INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_issue_open  ON issues(account_id, code, IFNULL(rel_path,'')) WHERE resolved_at IS NULL;
CREATE INDEX        ix_issues_open ON issues(account_id, severity, last_seen_at DESC) WHERE resolved_at IS NULL;

-- ─── writer: sync/pinner.py ───────────────────────────────────────────────────
-- rclone has NO pin concept and its LRU evictor ignores us. This table is
-- authoritative and is replayed whenever an eviction is detected.
CREATE TABLE pins (
  account_id    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path      TEXT NOT NULL,       -- POSIX, relative to sync_root. NEVER an inode.
  mode          TEXT NOT NULL,       -- pinned|online_only|auto
  is_dir        INTEGER NOT NULL DEFAULT 0,
  requested_at  TEXT NOT NULL,
  satisfied_at  TEXT,
  bytes_total   INTEGER,
  bytes_local   INTEGER,
  last_error    TEXT,
  generation    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, rel_path)
);
CREATE INDEX ix_pins_todo ON pins(account_id) WHERE mode = 'pinned' AND satisfied_at IS NULL;

-- ─── writer: sync/filestate.py ────────────────────────────────────────────────
-- Materialised view of the vfsMeta sidecars, written in GENERATIONS so a partial
-- scan never deletes valid rows. This is what the Nautilus IPC answers from.
CREATE TABLE cache_index (
  account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path        TEXT NOT NULL,
  state           TEXT NOT NULL,     -- online_only|partial|local|pinned|dirty|syncing|error|excluded
  size            INTEGER,
  bytes_local     INTEGER,
  dirty           INTEGER NOT NULL DEFAULT 0,
  shared          INTEGER NOT NULL DEFAULT 0,
  atime           TEXT,              -- the sidecar's ATime = rclone's LRU key
  mtime           TEXT,
  fingerprint     TEXT,              -- "<size>,<mtime UTC>,<quickxor>"
  scan_generation INTEGER NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (account_id, rel_path)
);
CREATE INDEX ix_cache_gen   ON cache_index(account_id, scan_generation);
CREATE INDEX ix_cache_state ON cache_index(account_id, state);
CREATE INDEX ix_cache_dirty ON cache_index(account_id) WHERE dirty = 1;

-- ─── writer: sync/conflicts.py ────────────────────────────────────────────────
CREATE TABLE conflicts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path     TEXT NOT NULL,
  loser_path   TEXT NOT NULL,
  winner_side  TEXT,                 -- local|remote
  detected_at  TEXT NOT NULL,
  run_id       TEXT,
  resolved_at  TEXT,
  resolution   TEXT,                 -- keep_both|keep_local|keep_cloud
  local_size   INTEGER,  local_mtime  TEXT,
  remote_size  INTEGER,  remote_mtime TEXT
);
CREATE UNIQUE INDEX ux_conflict_open ON conflicts(account_id, loser_path) WHERE resolved_at IS NULL;

-- ─── writer: data/repo_sync.py (via rc/bisync.py, sync/pinner.py, sync/kfm.py) ─
CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,
  account_id        TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  kind              TEXT NOT NULL,  -- bisync|resync|verify|pin_all|prune|kfm
  argv              TEXT NOT NULL,
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  exit_code         INTEGER,
  verdict           TEXT,           -- RunVerdict
  log_path          TEXT,
  log_offset        INTEGER NOT NULL DEFAULT 0,   -- the tailer resume point
  unit              TEXT,
  session           TEXT,
  listing1          TEXT, listing2 TEXT,
  files_transferred INTEGER, bytes INTEGER, deletes INTEGER, renames INTEGER, errors INTEGER,
  summary           TEXT
);
CREATE INDEX ix_runs_recent ON runs(account_id, started_at DESC);

-- ─── writer: sync/decisions.py ────────────────────────────────────────────────
-- Blocking user decisions that survive a crash and a reboot. Expiry means
-- DO NOT DELETE, matching Microsoft's 7-day policy.
CREATE TABLE decisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,   -- mass_delete|first_delete|resync_confirm|all_changed
                               -- |force_unlock|kfm_optout|quota_full
  payload     TEXT NOT NULL,   -- JSON
  created_at  TEXT NOT NULL,
  expires_at  TEXT,
  answered_at TEXT,
  answer      TEXT,
  run_id      TEXT
);
CREATE INDEX ix_decisions_pending ON decisions(account_id, created_at) WHERE answered_at IS NULL;

-- ─── writer: sync/versions.py ─────────────────────────────────────────────────
CREATE TABLE versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path     TEXT NOT NULL,
  backup_path  TEXT NOT NULL,   -- local dir or onedrive:.onedriveui-versions/<ts>/...
  side         TEXT NOT NULL,   -- local|remote
  captured_at  TEXT NOT NULL,
  size         INTEGER,
  quickxor     TEXT,
  reason       TEXT,            -- overwrite|delete
  run_id       TEXT
);
CREATE INDEX ix_versions_path ON versions(account_id, rel_path, captured_at DESC);

-- ─── writer: sync/trashbin.py ─────────────────────────────────────────────────
-- Only for deletions made THROUGH OUR UI. Deletions through the mount land in
-- Microsoft's own cloud recycle bin (see ARCHITECTURE §2 D6).
CREATE TABLE trashbin (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path     TEXT NOT NULL,
  trash_path   TEXT NOT NULL,   -- .onedriveui-trash/<ts>/<rel_path>
  is_dir       INTEGER NOT NULL DEFAULT 0,
  size         INTEGER,
  deleted_at   TEXT NOT NULL,
  purge_after  TEXT NOT NULL,   -- +30d personal, +93d business
  restored_at  TEXT
);
CREATE INDEX ix_trash_recent ON trashbin(account_id, deleted_at DESC);
CREATE INDEX ix_trash_purge  ON trashbin(purge_after);

-- ─── writer: sync/sharing.py ──────────────────────────────────────────────────
CREATE TABLE share_links (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path     TEXT NOT NULL,
  url          TEXT NOT NULL,
  scope        TEXT, link_type TEXT,
  has_password INTEGER NOT NULL DEFAULT 0,
  expires_at   TEXT,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT             -- LOCAL BOOKKEEPING ONLY: rclone --unlink is a no-op on OneDrive
);
CREATE INDEX ix_share_path ON share_links(account_id, rel_path);

-- ─── writer: sync/selective.py ────────────────────────────────────────────────
CREATE TABLE folder_selection (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  rel_path   TEXT NOT NULL,
  selected   INTEGER NOT NULL,
  size_bytes INTEGER, item_count INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_id, rel_path)
);

-- ─── writer: sync/kfm.py ──────────────────────────────────────────────────────
CREATE TABLE kfm_folder (
  account_id    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  folder        TEXT NOT NULL,   -- desktop|documents|pictures|music|videos
  enabled       INTEGER NOT NULL,
  original_path TEXT, target_path TEXT,
  journal_path  TEXT,
  moved_at      TEXT,
  PRIMARY KEY (account_id, folder)
);

-- ─── writer: platform/notify.py ───────────────────────────────────────────────
CREATE TABLE notifications (
  key             TEXT PRIMARY KEY,
  account_id      TEXT,
  dbus_id         INTEGER,
  last_shown_at   TEXT NOT NULL,
  suppressed_until TEXT,
  payload         TEXT
);

-- ─── writer: ui/dialogs/base.py ───────────────────────────────────────────────
CREATE TABLE dialog_seen (key TEXT PRIMARY KEY, seen_at TEXT NOT NULL);

-- ─── writer: anyone, via repo helpers only ────────────────────────────────────
CREATE TABLE kv (
  account_id TEXT NOT NULL DEFAULT '',
  key        TEXT NOT NULL,
  value      TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_id, key)
);

INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1');

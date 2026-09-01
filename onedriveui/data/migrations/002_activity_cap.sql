-- The activity cap, made cheap.
--
-- `trg_activity_cap` ran `DELETE ... WHERE id NOT IN (SELECT ... LIMIT 5000)`
-- on **every single INSERT**: a 5 000-row anti-join per row written. That was
-- tolerable while nothing wrote activity rows, and stopped being tolerable the
-- moment the transfer poller began draining completed transfers into this table
-- at up to several per second.
--
-- Two changes. The sweep now runs once every 500 inserts instead of every one,
-- and it deletes by primary-key range rather than by anti-join. The range is
-- approximate when two accounts interleave — it over-keeps, which is the safe
-- direction — because the exact per-account trim is `db.vacuum_and_prune()`,
-- which the supervisor's hourly `prune` job now runs.
DROP TRIGGER IF EXISTS trg_activity_cap;

CREATE TRIGGER trg_activity_cap AFTER INSERT ON activity
WHEN NEW.id % 500 = 0
BEGIN
  DELETE FROM activity
   WHERE account_id = NEW.account_id
     AND id <= NEW.id - 5000;
END;

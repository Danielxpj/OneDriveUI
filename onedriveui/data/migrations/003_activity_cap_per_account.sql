-- 003_activity_cap_per_account — cap by rows-per-account again, still cheap.
--
-- 002 replaced the per-INSERT anti-join with a primary-key range, and was right
-- that the anti-join on every row was unaffordable. The range it chose is wrong
-- in a way its own comment gets backwards: it says the approximation "over-keeps,
-- which is the safe direction", but `id <= NEW.id - 5000` measures the window in
-- GLOBAL rowids while the DELETE is scoped to one account. With two accounts
-- interleaving, an account's rows inside the last 5 000 global ids number far
-- fewer than 5 000, so the trigger deletes its history *early* — the quieter the
-- account, the more of it is lost. ARCHITECTURE section 10 and
-- `constants.ACTIVITY_CAP_ROWS` both still specify the newest 5 000 rows
-- per account.
--
-- So: restore 001's exact per-account anti-join, and keep 002's amortisation by
-- gating it instead of rewriting it. `abs(random()) % 500` samples about one
-- insert in five hundred independently of which account wrote it, so no account
-- can fall off the sweep the way a global counter allows, and the table
-- overshoots the cap by at most a few hundred rows between sweeps.
DROP TRIGGER IF EXISTS trg_activity_cap;

CREATE TRIGGER trg_activity_cap AFTER INSERT ON activity
WHEN abs(random()) % 500 = 0
BEGIN
  DELETE FROM activity
   WHERE account_id = NEW.account_id
     AND id NOT IN (SELECT id FROM activity
                     WHERE account_id = NEW.account_id
                     ORDER BY id DESC LIMIT 5000);
END;

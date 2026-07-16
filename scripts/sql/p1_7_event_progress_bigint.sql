-- P1-7 (events scale-readiness audit, 2026-07-16): widen the two columns that
-- fold RAW GP for loot_value tasks from INT to BIGINT.
--
-- Why: EventProgress.progress and EventCompletion.quantity accumulate raw GP
-- for loot_value tasks. Signed INT tops out at 2,147,483,647 (~2.1B GP); a
-- large multi-day loot event plausibly exceeds it, after which — under MySQL
-- strict mode — every subsequent matching drop errors and its whole envelope
-- (including credit for OTHER events on the same envelope) is dropped. The ORM
-- models already declare BigInteger (db/models/events.py); this brings the live
-- schema in line.
--
-- These ALTERs rewrite / lock the tables, so run them in the COORDINATED
-- maintenance window (stop droptracker-events + droptracker-webapi first to
-- avoid an MDL pileup on the busy tables), not during an active event.

ALTER TABLE web_event_completions MODIFY COLUMN quantity BIGINT NOT NULL DEFAULT 1;
ALTER TABLE web_event_progress    MODIFY COLUMN progress BIGINT NOT NULL DEFAULT 0;

-- After applying, `alembic stamp head` is NOT needed (no alembic revision was
-- authored — alembic/versions is gitignored in this repo; this is a manual DDL
-- change tracked only by the model + this script).

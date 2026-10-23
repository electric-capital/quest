-- The skill_loads ledger: one row per time a skill's body was put in front
-- of the model (analytics/ledger.py record_skill_load). Auto-loads land in
-- the system prompt on every turn, so a conversation with an auto-loaded
-- skill contributes one row per turn; the reports count distinct
-- conversations where that matters.
--
-- Deliberately no primary key and no indexes: rows are inserted in time
-- order and the reports filter on created_at, which DuckDB prunes with its
-- per-block min/max statistics. Discriminators are plain strings (the
-- values are the SKILL_LOAD_SOURCES tuple in analytics/ledger.py), so a
-- new source needs no migration. Ids are plain values with no FK: rows
-- outlive the conversation, user and skill they name.
CREATE TABLE skill_loads (
    created_at      TIMESTAMP NOT NULL,   -- UTC
    conversation_id VARCHAR   NOT NULL,
    user_id         INTEGER   NOT NULL,
    skill_id        VARCHAR   NOT NULL,   -- skills UUID or system:* id
    source          VARCHAR   NOT NULL,   -- autoload_user / composer / tool_get_skill / ...
    project_id      VARCHAR,              -- NULL when the recording site did not know it
    routine_id      VARCHAR               -- NULL when the recording site did not know it
);

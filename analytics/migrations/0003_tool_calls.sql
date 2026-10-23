-- The tool_calls ledger: one row per tool call the model (or a sandbox
-- script) issued (analytics/ledger.py record_tool_call), recorded when the
-- call is dispatched -- not when its result lands, so a call that suspends
-- the conversation (an action request) still counts. Names and ids only:
-- arguments and outputs are never stored.
--
-- Same shape rules as skill_loads: no primary key, no indexes, plain-string
-- discriminators (TOOL_CALL_TYPES / TOOL_CALL_SOURCES in analytics/ledger.py),
-- ids with no FK so rows outlive what they name, NULL for "not captured".
CREATE TABLE tool_calls (
    created_at      TIMESTAMP NOT NULL,   -- UTC
    conversation_id VARCHAR,              -- NULL for a script call whose lease has no conversation
    user_id         INTEGER   NOT NULL,
    tool_name       VARCHAR   NOT NULL,   -- the effective tool: the inner name for a tool_call meta-tool call
    dynamic         BOOLEAN   NOT NULL,   -- issued through the tool_call meta-tool
    call_type       VARCHAR   NOT NULL,   -- top_level / sub_agent / script
    source          VARCHAR   NOT NULL,   -- live / backfill
    tool_id         VARCHAR,              -- provider tool-use id (joins to the tool_result / wait handle); NULL for script calls
    agent_name      VARCHAR,              -- sub_agent rows: the sub-agent's name
    sub_agent_level INTEGER,              -- sub_agent rows: 1 or 2
    parent_tool_id  VARCHAR,              -- sub_agent rows: the agent_task* tool_use that spawned the agent
    project_id      VARCHAR,              -- NULL when the recording site did not know it
    routine_id      VARCHAR               -- NULL when the recording site did not know it
);

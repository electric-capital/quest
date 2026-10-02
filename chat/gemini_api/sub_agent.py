"""Sub-agent execution for the LLM integration.

Handles running single and parallel sub-agent tasks. Uses the LLMProvider
interface so sub-agents can use any provider (Gemini, Anthropic).
"""

import asyncio
import json
import time
import logging
import uuid
from typing import Any, Callable, Awaitable

from db.llm_call_store import record_api_call
from db.models import ApiCallType
from chat.gemini_api.constants import (
    SUB_AGENT_CONTEXT_WARNING_THRESHOLD,
    MAX_PARALLEL_TASKS,
    MAX_PARALLEL_TEMPLATE_TASKS,
    NESTED_SUB_AGENT_ALLOWED_MODELS,
    SUB_AGENT_DISALLOWED_MODELS,
    TEMPLATE_BATCH_ALLOWED_MODELS,
    _DEPRECATED_MODELS,
    get_proxy_base_url,
    get_sub_agent_turn_limits,
)
from chat.llm.base import compute_new_input_tokens, compute_total_context_tokens
from chat.llm.config import (
    get_provider_for_model,
    get_provider_instance,
    get_backend_for_model,
    get_max_input_tokens,
    model_instance_id,
)
from chat.llm.tool_schemas import SUB_AGENT_TOOLS, SUB_AGENT_TOOLS_NESTED
from chat.gemini_api.system_prompt import get_sub_agent_system_prompt
from chat.gemini_api.tool_dispatch import (
    _dispatch_tool_call,
    invalid_tool_arguments_result,
)

logger = logging.getLogger(__name__)


async def _run_sub_agent(
    app,
    provider,
    user: dict[str, Any],
    conversation_id: str,
    timezone: str,
    model: str,
    agent_name: str,
    prompt: str,
    custom_prompt: str | None = None,
    project_id: str | None = None,
    project_guide: str = "",
    skills_content: str = "",
    usage_accumulator: dict | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    parent_tool_id: str | None = None,
    nested_enabled: bool = False,
    level: int = 1,
    nested_parent_id: str | None = None,
) -> str:
    """Run a sub-agent task and return its response.

    Creates an ephemeral session and runs it to completion. Sub-agent messages
    are NOT emitted to the frontend.

    By default the sub-agent uses SUB_AGENT_TOOLS (base tools +
    agent_task_response, NO spawning tools). When ``nested_enabled`` is True AND
    this is a 1st-level sub-agent (``level == 1``), the session instead uses
    SUB_AGENT_TOOLS_NESTED, which adds a single ``agent_task_nested`` spawner so
    this sub-agent can delegate cheap leaf work to one 2nd-level sub-agent. A
    2nd-level sub-agent (``level == 2``) always uses SUB_AGENT_TOOLS (no
    spawner) -- there is no 3rd level.

    The sub-agent runs until it calls agent_task_response, at which point
    the response string is extracted and returned. If the sub-agent finishes
    without calling agent_task_response (runs out of things to do), its
    accumulated text output is returned instead as a fallback.

    Args:
        app: FastAPI app instance.
        provider: LLMProvider instance for the sub-agent's model.
        user: Authenticated user dict.
        conversation_id: Conversation ID (for workspace access).
        timezone: User's timezone string.
        model: Model name string.
        agent_name: Display name for this sub-agent.
        prompt: The task prompt to send to the sub-agent.
        custom_prompt: Optional user custom system prompt.
        project_id: Optional project UUID for project-aware workspace resolution.
        project_guide: Optional project-specific instructions.
        usage_accumulator: Optional dict to accumulate usage across turns.
        nested_enabled: Whether the conversation's ``nested_subagents`` flag is
            on. Only meaningful at ``level == 1``; a 1st-level sub-agent then
            gets the nested-spawn tool and the can-nest prompt copy.
        level: Sub-agent nesting depth. 1 = spawned by the top-level agent;
            2 = spawned by a 1st-level sub-agent (a leaf agent that cannot
            spawn further and is restricted to NESTED_SUB_AGENT_ALLOWED_MODELS).
        nested_parent_id: Set only for a 2nd-level (``level == 2``) sub-agent.
            It is the id of the nested-agent node (the ``agent_task_nested``
            tool call) under which this grandchild's ``sub_agent_*`` events are
            grouped in the UI. When present it is attached to every
            ``sub_agent_tool_use`` / ``sub_agent_tool_result`` /
            ``sub_agent_finished`` event this agent emits (alongside the
            1st-level ``parent_tool_id``) so the FE can render the grandchild's
            tool calls one extra indent level deep, under the nested-agent node
            rather than flat under the 1st-level agent row.

    Returns:
        The sub-agent's response string.
    """
    sub_agent_text_parts = []
    max_turns, warning_threshold = get_sub_agent_turn_limits(model)

    # A 1st-level sub-agent may spawn one 2nd-level sub-agent only when the
    # conversation flag is on. 2nd-level sub-agents are always leaves.
    can_nest = bool(nested_enabled) and level == 1

    async def _emit_finished(status: str, error: str | None = None) -> None:
        """Emit a sub_agent_finished event so the UI can flip the per-agent
        badge from RUNNING to COMPLETED/ERRORED once the sub-agent has truly
        finished (i.e. after it calls agent_task_response, or after it hits
        a fallback/early-return path)."""
        if not on_event or not parent_tool_id:
            return
        try:
            payload: dict[str, Any] = {
                "type": "sub_agent_finished",
                "parent_tool_id": parent_tool_id,
                "agent_name": agent_name,
                "status": status,
            }
            if nested_parent_id:
                payload["nested_parent_id"] = nested_parent_id
            if error is not None:
                payload["error"] = error
            await on_event(payload)
        except Exception:
            logger.debug("Failed to emit sub_agent_finished event", exc_info=True)

    # Defense-in-depth: refuse disallowed sub-agent models before any session
    # creation or API calls, so no tokens are spent. The spawn arms normally
    # reject these earlier with a clean tool result, so this guard should never
    # fire in normal operation -- it exists to make _run_sub_agent() safe by
    # construction for any future caller. We return an error-shaped string
    # (rather than raise) so the parent receives a clear message.
    if model in SUB_AGENT_DISALLOWED_MODELS:
        logger.warning(
            "Sub-agent '%s' requested disallowed model '%s' (conversation=%s, user=%s)",
            agent_name, model, conversation_id, user["email"],
        )
        await _emit_finished("error", error=f"Model '{model}' is not permitted for sub-agents.")
        return f"(Sub-agent error: model '{model}' is not permitted for sub-agents; choose a different model)"

    # Defense-in-depth: a 2nd-level (nested) sub-agent must use one of the two
    # cheapest models (NESTED_SUB_AGENT_ALLOWED_MODELS). The nested-spawn
    # dispatch branch normally rejects a disallowed model earlier with a clean
    # tool result, so this guard should never fire in normal operation -- it
    # makes _run_sub_agent(level=2) safe by construction.
    if level == 2 and model not in NESTED_SUB_AGENT_ALLOWED_MODELS:
        allowed = ", ".join(sorted(NESTED_SUB_AGENT_ALLOWED_MODELS))
        logger.warning(
            "Nested (2nd-level) sub-agent '%s' requested non-allowed model '%s' "
            "(conversation=%s, user=%s)",
            agent_name, model, conversation_id, user["email"],
        )
        await _emit_finished(
            "error",
            error=f"Model '{model}' is not permitted for 2nd-level sub-agents.",
        )
        return (
            f"(Sub-agent error: model '{model}' is not permitted for 2nd-level "
            f"sub-agents; choose one of: {allowed})"
        )

    # Build sub-agent system prompt (use passed custom_prompt from parent's
    # resolved guide, falling back to user settings for backward compat)
    if custom_prompt is None:
        custom_prompt = user.get("settings", {}).get("custom_system_prompt", "")
    from api.instructions import get_user_connected_services
    connected_services = get_user_connected_services(user)
    system_prompt = get_sub_agent_system_prompt(
        agent_name, user["api_key"],
        base_url=get_proxy_base_url(),
        custom_system_prompt=custom_prompt,
        connected_services=connected_services,
        user_name=user.get("name", ""),
        user_email=user["email"],
        project_guide=project_guide,
        skills_content=skills_content,
        has_project=bool(project_id),
        can_nest=can_nest,
    )

    # Choose the tool tier: only a 1st-level sub-agent with the flag on gets the
    # nested-spawn tool. 2nd-level sub-agents (and all sub-agents when the flag
    # is off) get the plain SUB_AGENT_TOOLS with no spawner.
    sub_agent_tools = SUB_AGENT_TOOLS_NESTED if can_nest else SUB_AGENT_TOOLS

    # Create ephemeral session -- NOT stored in _active_chats
    session = provider.create_session(
        model=model,
        system_prompt=system_prompt,
        tools=sub_agent_tools,
    )

    current_message: Any = prompt
    turn_count = 0

    while True:
        turn_count += 1
        sub_turn_start = time.time()
        if turn_count > max_turns:
            logger.warning("Sub-agent '%s' exceeded maximum turns (%d) (user=%s, conversation=%s)", agent_name, max_turns, user["email"], conversation_id)
            await _emit_finished("success")
            if sub_agent_text_parts:
                return "\n".join(sub_agent_text_parts)
            return "(Sub-agent exceeded maximum number of turns without completing)"

        text_parts = []
        function_calls = []

        # Stream the sub-agent's response (NOT emitted to frontend)
        async for event in provider.send_message_stream(session, current_message):
            if event.type == "text":
                text_parts.append(event.text)
            elif event.type == "tool_call":
                function_calls.append(event)

        # Extract usage for this turn
        sub_turn_usage = provider.get_usage(session)
        sub_turn_input = sub_turn_usage.input_tokens
        sub_turn_output = sub_turn_usage.output_tokens
        sub_turn_cached = sub_turn_usage.cached_tokens
        sub_turn_cache_creation = sub_turn_usage.cache_creation_tokens
        sub_turn_cache_read = sub_turn_usage.cache_read_tokens
        sub_turn_duration_ms = int((time.time() - sub_turn_start) * 1000)

        # Compute new_input_tokens using the sub-agent's own provider
        # (which may differ from the parent's provider).
        try:
            sub_provider_name = get_provider_for_model(model)
        except ValueError:
            sub_provider_name = "gemini"  # fallback
        sub_turn_new_input = compute_new_input_tokens(sub_turn_usage, sub_provider_name)

        try:
            await record_api_call(
                conversation_id=conversation_id,
                user_id=user["id"],
                model=model,
                call_type=ApiCallType.SUB_AGENT,
                input_tokens=sub_turn_input,
                output_tokens=sub_turn_output,
                duration_ms=sub_turn_duration_ms,
                agent_name=agent_name,
                cached_tokens=sub_turn_cached,
                provider=sub_provider_name,
                backend=get_backend_for_model(model),
                level=level,
                raw_usage=sub_turn_usage.raw_usage,
            )
        except Exception:
            logger.warning("Failed to record sub-agent API call usage", exc_info=True)

        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = usage_accumulator.get("input_tokens", 0) + sub_turn_input
            usage_accumulator["output_tokens"] = usage_accumulator.get("output_tokens", 0) + sub_turn_output
            usage_accumulator["cached_tokens"] = usage_accumulator.get("cached_tokens", 0) + sub_turn_cached
            usage_accumulator["cache_creation_tokens"] = usage_accumulator.get("cache_creation_tokens", 0) + sub_turn_cache_creation
            usage_accumulator["cache_read_tokens"] = usage_accumulator.get("cache_read_tokens", 0) + sub_turn_cache_read
            usage_accumulator["new_input_tokens"] = usage_accumulator.get("new_input_tokens", 0) + sub_turn_new_input
            usage_accumulator["call_count"] = usage_accumulator.get("call_count", 0) + 1

        # Accumulate text (for fallback if agent_task_response is never called)
        if text_parts:
            full_text = "".join(text_parts)
            if full_text.strip():
                sub_agent_text_parts.append(full_text)

        # No more function calls -> sub-agent finished without calling agent_task_response
        if not function_calls:
            break

        # Execute each function call
        tool_results = []
        agent_response = None  # Will be set if agent_task_response is called

        for fc_event in function_calls:
            args = dict(fc_event.tool_args) if fc_event.tool_args else {}

            # Extract intent_message before dispatch (it's not a tool parameter)
            intent_message = args.pop("intent_message", "")

            if fc_event.tool_args_error:
                # Malformed arguments from the model: nothing to dispatch
                # (not even agent_task_response -- its response text is
                # inside the unparseable payload). Return the parse error
                # so the sub-agent can re-issue the call.
                logger.info(
                    "[sub-agent:%s] Malformed arguments for tool call %s "
                    "(conversation=%s, user=%s)",
                    agent_name, fc_event.tool_name, conversation_id, user["email"],
                )
                result = invalid_tool_arguments_result(
                    fc_event.tool_name, fc_event.tool_args_error,
                )
                # Same FE event pair as a dispatched call so the failed
                # attempt shows in the sub-agent tree.
                sub_tool_id = f"sub_{fc_event.tool_id or uuid.uuid4().hex[:8]}"
                if on_event and parent_tool_id:
                    try:
                        for event in (
                            {
                                "type": "sub_agent_tool_use",
                                "tool_name": fc_event.tool_name,
                                "tool_input": args,
                                "intent_message": intent_message,
                            },
                            {"type": "sub_agent_tool_result", "tool_output": result},
                        ):
                            event.update({
                                "parent_tool_id": parent_tool_id,
                                "agent_name": agent_name,
                                "tool_id": sub_tool_id,
                            })
                            if nested_parent_id:
                                event["nested_parent_id"] = nested_parent_id
                            await on_event(event)
                    except Exception:
                        logger.debug(
                            "Failed to emit malformed-arguments sub-agent events",
                            exc_info=True,
                        )
                tool_results.append({
                    "name": fc_event.tool_name,
                    "result": result,
                    "tool_id": fc_event.tool_id,
                    "extra_parts": [],
                })
                continue

            # Check for agent_task_response -- this terminates the sub-agent
            if fc_event.tool_name == "agent_task_response":
                agent_response = args.get("response", "")
                logger.info(
                    "[sub-agent:%s] agent_task_response called (conversation=%s, user=%s, response_length=%d)",
                    agent_name, conversation_id, user["email"], len(agent_response),
                )
                # Build a tool result acknowledging receipt
                tool_results.append({
                    "name": fc_event.tool_name,
                    "result": "Response received. Task complete.",
                    "tool_id": fc_event.tool_id,
                    "extra_parts": [],
                })
                break  # Stop processing further function calls

            # Nested spawn: a 1st-level sub-agent (can_nest) may delegate one
            # 2nd-level sub-agent via agent_task_nested. This branch mirrors the
            # top-level agent_task dispatch but hard-restricts the model to the
            # 2nd-level allow-list and pins level=2 so the grandchild gets
            # SUB_AGENT_TOOLS (no further spawner).
            #
            # UI surfacing: the agent_task_nested call is emitted as a
            # sub_agent_tool_use event (keyed by this 1st-level agent's
            # parent_tool_id and agent_name) that ALSO carries a new
            # ``nested_agent_id`` field marking it as a nested-agent NODE. The
            # 2nd-level agent's own sub_agent_* events are tagged with
            # ``nested_parent_id == nested_agent_id`` so the FE can nest the
            # grandchild's tool calls one indent level under this node instead
            # of rendering them flat under the 1st-level row. A matching
            # sub_agent_tool_result for the node flips it from RUNNING to
            # COMPLETED/ERRORED once the nested agent returns.
            if fc_event.tool_name == "agent_task_nested":
                nested_name = args.get("name", "Nested Sub-Agent")
                # Stable id for the nested-agent node + its grandchild events.
                nested_agent_id = f"nested_{fc_event.tool_id or uuid.uuid4().hex[:8]}"

                if not can_nest:
                    # Defense-in-depth: the tool isn't offered to this agent, so
                    # this only fires if a provider hallucinated the call.
                    nested_result = json.dumps({
                        "error": "agent_task_nested is not available to this sub-agent."
                    })
                    nested_status = "error"
                else:
                    nested_prompt = args.get("prompt", "")
                    nested_model = args.get("model", "")
                    nested_model = _DEPRECATED_MODELS.get(nested_model, nested_model)

                    # Emit the nested-agent node (RUNNING). It is a
                    # sub_agent_tool_use under THIS 1st-level agent's row,
                    # distinguished by nested_agent_id so the FE renders it as
                    # a child agent node rather than a leaf tool call.
                    if on_event and parent_tool_id:
                        try:
                            await on_event({
                                "type": "sub_agent_tool_use",
                                "parent_tool_id": parent_tool_id,
                                "agent_name": agent_name,
                                "tool_name": fc_event.tool_name,
                                "tool_input": args,
                                "tool_id": nested_agent_id,
                                "intent_message": intent_message,
                                "nested_agent_id": nested_agent_id,
                                "nested_agent_name": nested_name,
                                "nested_agent_model": nested_model,
                            })
                        except Exception:
                            logger.debug("Failed to emit nested-agent node sub_agent_tool_use", exc_info=True)

                    if nested_model not in NESTED_SUB_AGENT_ALLOWED_MODELS:
                        allowed = ", ".join(sorted(NESTED_SUB_AGENT_ALLOWED_MODELS))
                        logger.info(
                            "[sub-agent:%s] Rejected non-allowed nested model '%s' for "
                            "'%s' (conversation=%s, user=%s)",
                            agent_name, nested_model, nested_name, conversation_id, user["email"],
                        )
                        nested_result = json.dumps({
                            "error": (
                                f"Model '{nested_model}' is not permitted for 2nd-level "
                                f"sub-agents. Specify a `model` of one of: {allowed}."
                            )
                        })
                        nested_status = "error"
                    else:
                        # Resolve the nested sub-agent's provider (may differ).
                        try:
                            nested_provider_name = get_provider_for_model(nested_model)
                            nested_provider = get_provider_instance(
                                nested_provider_name, model_instance_id(nested_model),
                            )
                        except ValueError:
                            nested_provider = provider

                        logger.info(
                            "[sub-agent:%s] Spawning nested (2nd-level) sub-agent '%s' "
                            "(model=%s, conversation=%s, user=%s)",
                            agent_name, nested_name, nested_model, conversation_id, user["email"],
                        )
                        nested_usage: dict = {}
                        nested_status = "success"
                        try:
                            nested_result = await _run_sub_agent(
                                app=app,
                                provider=nested_provider,
                                user=user,
                                conversation_id=conversation_id,
                                timezone=timezone,
                                model=nested_model,
                                agent_name=nested_name,
                                prompt=nested_prompt,
                                custom_prompt=custom_prompt,
                                project_id=project_id,
                                project_guide=project_guide,
                                skills_content=skills_content,
                                usage_accumulator=nested_usage,
                                on_event=on_event,
                                parent_tool_id=parent_tool_id,
                                nested_enabled=nested_enabled,
                                level=2,
                                nested_parent_id=nested_agent_id,
                            )
                        except Exception as e:
                            logger.exception(
                                "[sub-agent:%s] Nested sub-agent '%s' failed "
                                "(conversation=%s, user=%s)",
                                agent_name, nested_name, conversation_id, user["email"],
                            )
                            nested_result = json.dumps({
                                "error": f"Nested sub-agent '{nested_name}' failed: {str(e)}"
                            })
                            nested_status = "error"

                        # Roll the nested sub-agent's usage into this agent's
                        # accumulator so the top-level totals capture it too.
                        if usage_accumulator is not None and nested_usage:
                            for k, v in nested_usage.items():
                                usage_accumulator[k] = usage_accumulator.get(k, 0) + v

                # Flip the nested-agent node to its terminal state. Emitting a
                # sub_agent_tool_result (keyed by nested_agent_id) mirrors a
                # normal leaf tool-call completion so the FE node can resolve
                # its RUNNING/COMPLETED badge even when the grandchild never
                # got to spawn (e.g. a rejected model).
                if on_event and parent_tool_id:
                    try:
                        await on_event({
                            "type": "sub_agent_tool_result",
                            "parent_tool_id": parent_tool_id,
                            "agent_name": agent_name,
                            "tool_id": nested_agent_id,
                            "tool_output": nested_result,
                            "nested_agent_id": nested_agent_id,
                            "nested_agent_status": nested_status,
                        })
                    except Exception:
                        logger.debug("Failed to emit nested-agent node sub_agent_tool_result", exc_info=True)

                tool_results.append({
                    "name": fc_event.tool_name,
                    "result": nested_result,
                    "tool_id": fc_event.tool_id,
                    "extra_parts": [],
                })
                continue  # Move to the next function call in this turn

            # Execute tool via shared dispatch (the top-level agent_task* tools
            # are impossible here because they're not in SUB_AGENT_TOOLS /
            # SUB_AGENT_TOOLS_NESTED)
            logger.info(
                "[sub-agent:%s] Tool call: %s (turn=%d, conversation=%s, user=%s)",
                agent_name, fc_event.tool_name, turn_count, conversation_id, user["email"],
            )

            # Emit sub_agent_tool_use event for frontend display
            sub_tool_id = f"sub_{fc_event.tool_id or uuid.uuid4().hex[:8]}"
            if on_event and parent_tool_id:
                try:
                    tool_use_event: dict[str, Any] = {
                        "type": "sub_agent_tool_use",
                        "parent_tool_id": parent_tool_id,
                        "agent_name": agent_name,
                        "tool_name": fc_event.tool_name,
                        "tool_input": args,
                        "tool_id": sub_tool_id,
                        "intent_message": intent_message,
                    }
                    # A 2nd-level agent tags its tool calls with the nested-agent
                    # node id so the FE nests them under that node.
                    if nested_parent_id:
                        tool_use_event["nested_parent_id"] = nested_parent_id
                    await on_event(tool_use_event)
                except Exception:
                    logger.debug("Failed to emit sub_agent_tool_use event", exc_info=True)

            tool_start = time.time()

            result, extra_parts = await _dispatch_tool_call(
                app, provider, user, conversation_id, timezone,
                fc_event.tool_name, args, project_id=project_id,
                model=model, is_sub_agent=True, agent_name=agent_name,
            )

            tool_duration_ms = int((time.time() - tool_start) * 1000)
            logger.info(
                "[sub-agent:%s] Tool result: %s completed in %dms (conversation=%s, user=%s, result_length=%d)",
                agent_name, fc_event.tool_name, tool_duration_ms, conversation_id, user["email"], len(result),
            )

            # Emit sub_agent_tool_result event for frontend display
            if on_event and parent_tool_id:
                try:
                    tool_result_event: dict[str, Any] = {
                        "type": "sub_agent_tool_result",
                        "parent_tool_id": parent_tool_id,
                        "agent_name": agent_name,
                        "tool_id": sub_tool_id,
                        "tool_output": result,
                    }
                    if nested_parent_id:
                        tool_result_event["nested_parent_id"] = nested_parent_id
                    await on_event(tool_result_event)
                except Exception:
                    logger.debug("Failed to emit sub_agent_tool_result event", exc_info=True)

            tool_results.append({
                "name": fc_event.tool_name,
                "result": result,
                "tool_id": fc_event.tool_id,
                "extra_parts": extra_parts,
            })

        # Format tool results for the provider
        formatted_results = provider.format_tool_results(session, tool_results)

        # Inject approaching-limit warning when nearing the turn cap
        if turn_count >= warning_threshold:
            remaining = max_turns - turn_count
            warning_text = (
                f"SYSTEM WARNING: You have used {turn_count} of {max_turns} "
                f"allowed turns ({remaining} remaining). You MUST call agent_task_response "
                f"NOW with whatever findings you have gathered so far. Do not begin new "
                f"tool calls. Summarize your progress and return immediately."
            )
            provider.inject_turn_warning(formatted_results, warning_text)
            logger.info(
                "[sub-agent:%s] Turn limit warning injected (turn=%d/%d, conversation=%s, user=%s)",
                agent_name, turn_count, max_turns, conversation_id, user["email"],
            )

        # Inject context window usage warning when nearing the model's limit
        context_tokens = compute_total_context_tokens(sub_turn_usage, sub_provider_name)
        max_context = get_max_input_tokens(model)
        if max_context > 0 and context_tokens / max_context >= SUB_AGENT_CONTEXT_WARNING_THRESHOLD:
            pct = int(context_tokens / max_context * 100)
            context_warning_text = (
                f"SYSTEM WARNING: Your context window is {pct}% full "
                f"({context_tokens} of {max_context} tokens). "
                f"You are running low on context space. You MUST call agent_task_response "
                f"NOW with whatever findings you have gathered so far. Do not begin new "
                f"tool calls. Summarize your progress and return immediately."
            )
            provider.inject_turn_warning(formatted_results, context_warning_text)
            logger.info(
                "[sub-agent:%s] Context window warning injected (context=%d/%d, %d%%, conversation=%s, user=%s)",
                agent_name, context_tokens, max_context, pct, conversation_id, user["email"],
            )

        # If agent_task_response was called, send the acknowledgment back
        # to the model and drain its final response before returning.
        if agent_response is not None:
            drain_start = time.time()
            try:
                async for _event in provider.send_message_stream(session, formatted_results):
                    # Consume all events -- we expect the model to produce
                    # little or no output after the task-complete acknowledgment.
                    pass

                drain_usage = provider.get_usage(session)
                drain_input = drain_usage.input_tokens
                drain_output = drain_usage.output_tokens
                drain_cached = drain_usage.cached_tokens
                drain_cache_creation = drain_usage.cache_creation_tokens
                drain_cache_read = drain_usage.cache_read_tokens
                drain_duration_ms = int((time.time() - drain_start) * 1000)
                drain_new_input = compute_new_input_tokens(drain_usage, sub_provider_name)
                logger.info(
                    "[sub-agent:%s] Drained final response after agent_task_response "
                    "(conversation=%s, user=%s, input_tokens=%d, output_tokens=%d)",
                    agent_name, conversation_id, user["email"],
                    drain_input, drain_output,
                )

                # Record drain call usage
                try:
                    await record_api_call(
                        conversation_id=conversation_id,
                        user_id=user["id"],
                        model=model,
                        call_type=ApiCallType.SUB_AGENT,
                        input_tokens=drain_input,
                        output_tokens=drain_output,
                        duration_ms=drain_duration_ms,
                        agent_name=agent_name,
                        cached_tokens=drain_cached,
                        provider=sub_provider_name,
                        backend=get_backend_for_model(model),
                        level=level,
                        raw_usage=drain_usage.raw_usage,
                    )
                except Exception:
                    logger.warning("Failed to record sub-agent drain API call usage", exc_info=True)

                if usage_accumulator is not None:
                    usage_accumulator["input_tokens"] = usage_accumulator.get("input_tokens", 0) + drain_input
                    usage_accumulator["output_tokens"] = usage_accumulator.get("output_tokens", 0) + drain_output
                    usage_accumulator["cached_tokens"] = usage_accumulator.get("cached_tokens", 0) + drain_cached
                    usage_accumulator["cache_creation_tokens"] = usage_accumulator.get("cache_creation_tokens", 0) + drain_cache_creation
                    usage_accumulator["cache_read_tokens"] = usage_accumulator.get("cache_read_tokens", 0) + drain_cache_read
                    usage_accumulator["new_input_tokens"] = usage_accumulator.get("new_input_tokens", 0) + drain_new_input
                    usage_accumulator["call_count"] = usage_accumulator.get("call_count", 0) + 1

            except Exception:
                logger.debug(
                    "[sub-agent:%s] Error draining final response after agent_task_response "
                    "(conversation=%s, user=%s), continuing with captured response",
                    agent_name, conversation_id, user["email"],
                    exc_info=True,
                )
            await _emit_finished("success")
            return agent_response

        # Send function responses back to the sub-agent for the next turn
        current_message = formatted_results

    # Fallback: sub-agent finished without calling agent_task_response
    await _emit_finished("success")
    if sub_agent_text_parts:
        return "\n".join(sub_agent_text_parts)
    return "(Sub-agent completed without producing a response)"


async def _run_parallel_sub_agents(
    app,
    provider,
    user: dict[str, Any],
    conversation_id: str,
    timezone: str,
    parent_model: str,
    tasks: list[dict[str, Any]],
    custom_prompt: str | None = None,
    project_id: str | None = None,
    project_guide: str = "",
    skills_content: str = "",
    usage_accumulator: dict | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    parent_tool_id: str | None = None,
    nested_enabled: bool = False,
) -> str:
    """Run multiple sub-agent tasks in parallel and return combined results.

    Launches all tasks concurrently using asyncio.gather(). Each task runs
    an independent _run_sub_agent() call. Results are tagged with the
    caller-specified 'id' from each task specification.

    Individual task failures are captured (not propagated) -- the result for
    a failed task will have status="error" instead of status="success".

    Args:
        app: FastAPI app instance.
        provider: LLMProvider instance (parent's provider; sub-agents may
            use different providers based on their model selection).
        user: Authenticated user dict.
        conversation_id: Conversation ID (for workspace access).
        timezone: User's timezone string.
        parent_model: The parent agent's model name (used as fallback).
        tasks: List of task specification dicts, each with 'id', 'name',
               'prompt', 'description', and optional 'model'.
        custom_prompt: Optional user custom system prompt.
        project_id: Optional project UUID for project-aware workspace resolution.
        project_guide: Optional project-specific instructions.
        usage_accumulator: Optional dict to accumulate usage.

    Returns:
        JSON string with results keyed by task ID.
    """
    # Validate tasks
    if not tasks:
        return json.dumps({"error": "No tasks provided"})

    if len(tasks) > MAX_PARALLEL_TASKS:
        return json.dumps({
            "error": f"Too many parallel tasks: {len(tasks)} (maximum is {MAX_PARALLEL_TASKS})"
        })

    # Check for duplicate IDs
    task_ids = [t.get("id", "") for t in tasks]
    seen_ids: set[str] = set()
    for tid in task_ids:
        if not tid:
            return json.dumps({"error": "Each task must have a non-empty 'id' field"})
        if tid in seen_ids:
            return json.dumps({"error": f"Duplicate task id: '{tid}'"})
        seen_ids.add(tid)

    # Build coroutines for each task
    async def run_one_task(task_spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Run a single sub-agent task and return (id, result_dict)."""
        task_id = task_spec["id"]
        agent_name = task_spec.get("name", "Sub-Agent")
        agent_prompt = task_spec.get("prompt", "")
        agent_model = task_spec.get("model") or parent_model
        agent_model = _DEPRECATED_MODELS.get(agent_model, agent_model)

        # Reject disallowed sub-agent models (e.g. Gemini 3.1 Pro) with a clean,
        # per-task error result so the model can re-spawn with an allowed model.
        # Runs after the _DEPRECATED_MODELS remap so the deprecated alias is also
        # blocked, and before the spawn so no tokens are spent. Mirrors the
        # except-block's sub_agent_finished(error) emit so the UI badge resolves.
        if agent_model in SUB_AGENT_DISALLOWED_MODELS:
            logger.info(
                "Rejected disallowed sub-agent model '%s' for task '%s' (id=%s, user=%s)",
                agent_model, agent_name, task_id, user["email"],
            )
            error_msg = (
                f"Model '{agent_model}' is not permitted for sub-agents. "
                f"Specify a different `model` for this task."
            )
            if on_event and parent_tool_id:
                try:
                    await on_event({
                        "type": "sub_agent_finished",
                        "parent_tool_id": parent_tool_id,
                        "agent_name": agent_name,
                        "status": "error",
                        "error": error_msg,
                    })
                except Exception:
                    logger.debug("Failed to emit sub_agent_finished (error) event", exc_info=True)
            return task_id, {
                "status": "error",
                "name": agent_name,
                "error": error_msg,
                "_usage": {},
            }

        # Resolve the provider for the sub-agent's model.
        # This allows cross-provider sub-agents (e.g., Gemini parent
        # spawning a Claude sub-agent and vice versa).
        try:
            sub_provider_name = get_provider_for_model(agent_model)
            sub_provider = get_provider_instance(
                sub_provider_name, model_instance_id(agent_model),
            )
        except ValueError:
            # Unknown model -- fall back to parent's provider
            sub_provider = provider

        task_usage = {}
        try:
            response = await _run_sub_agent(
                app=app,
                provider=sub_provider,
                user=user,
                conversation_id=conversation_id,
                timezone=timezone,
                model=agent_model,
                agent_name=agent_name,
                prompt=agent_prompt,
                custom_prompt=custom_prompt,
                project_id=project_id,
                project_guide=project_guide,
                skills_content=skills_content,
                usage_accumulator=task_usage,
                on_event=on_event,
                parent_tool_id=parent_tool_id,
                nested_enabled=nested_enabled,
                level=1,
            )
            return task_id, {
                "status": "success",
                "name": agent_name,
                "response": response,
                "_usage": task_usage,
            }
        except Exception as e:
            logger.exception("Error in parallel sub-agent '%s' (id=%s, user=%s)", agent_name, task_id, user["email"])
            # Emit a sub_agent_finished event so the UI can flip this
            # particular sub-agent's row to the errored terminal state.
            # _run_sub_agent raised before it could emit its own event.
            if on_event and parent_tool_id:
                try:
                    await on_event({
                        "type": "sub_agent_finished",
                        "parent_tool_id": parent_tool_id,
                        "agent_name": agent_name,
                        "status": "error",
                        "error": str(e),
                    })
                except Exception:
                    logger.debug("Failed to emit sub_agent_finished (error) event", exc_info=True)
            return task_id, {
                "status": "error",
                "name": agent_name,
                "error": f"Sub-agent '{agent_name}' failed: {str(e)}",
                "_usage": task_usage,
            }

    # Launch all tasks concurrently
    coroutines = [run_one_task(task_spec) for task_spec in tasks]
    completed = await asyncio.gather(*coroutines)

    # Assemble results dict keyed by task ID
    results = {}
    for task_id, result_dict in completed:
        # Aggregate usage into the parent accumulator
        if usage_accumulator is not None:
            task_usage = result_dict.pop("_usage", {})
            usage_accumulator["input_tokens"] = usage_accumulator.get("input_tokens", 0) + task_usage.get("input_tokens", 0)
            usage_accumulator["output_tokens"] = usage_accumulator.get("output_tokens", 0) + task_usage.get("output_tokens", 0)
            usage_accumulator["cached_tokens"] = usage_accumulator.get("cached_tokens", 0) + task_usage.get("cached_tokens", 0)
            usage_accumulator["cache_creation_tokens"] = usage_accumulator.get("cache_creation_tokens", 0) + task_usage.get("cache_creation_tokens", 0)
            usage_accumulator["cache_read_tokens"] = usage_accumulator.get("cache_read_tokens", 0) + task_usage.get("cache_read_tokens", 0)
            usage_accumulator["new_input_tokens"] = usage_accumulator.get("new_input_tokens", 0) + task_usage.get("new_input_tokens", 0)
            usage_accumulator["call_count"] = usage_accumulator.get("call_count", 0) + task_usage.get("call_count", 0)
        else:
            result_dict.pop("_usage", None)
        results[task_id] = result_dict

    return json.dumps({"results": results})


async def _run_parallel_sub_agents_template(
    app,
    provider,
    user: dict[str, Any],
    conversation_id: str,
    timezone: str,
    prompt_template: str,
    model: str,
    agents: list[dict[str, Any]],
    custom_prompt: str | None = None,
    project_id: str | None = None,
    project_guide: str = "",
    skills_content: str = "",
    usage_accumulator: dict | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    parent_tool_id: str | None = None,
    nested_enabled: bool = False,
) -> str:
    """Run template-based parallel sub-agents and return combined results.

    Accepts a prompt template with {var}-style placeholders and a list of
    agent dicts containing template variables. Renders each prompt and
    delegates to _run_parallel_sub_agents() for execution.

    The model is restricted to cheaper models (TEMPLATE_BATCH_ALLOWED_MODELS)
    and is shared across all agents in the batch.

    Args:
        app: FastAPI app instance.
        provider: LLMProvider instance (parent's provider).
        user: Authenticated user dict.
        conversation_id: Conversation ID (for workspace access).
        timezone: User's timezone string.
        prompt_template: Prompt template with {var}-style placeholders.
        model: Model name (must be in TEMPLATE_BATCH_ALLOWED_MODELS).
        agents: List of agent dicts, each with 'name' and template variables.
        custom_prompt: Optional user custom system prompt.
        project_id: Optional project UUID.
        project_guide: Optional project-specific instructions.
        skills_content: Optional skills content.
        usage_accumulator: Optional dict to accumulate usage.
        on_event: Optional event callback.
        parent_tool_id: Optional parent tool ID for event tracking.

    Returns:
        JSON string with results keyed by agent name.
    """
    # Validate model
    if model not in TEMPLATE_BATCH_ALLOWED_MODELS:
        allowed = ", ".join(sorted(TEMPLATE_BATCH_ALLOWED_MODELS))
        return json.dumps({
            "error": f"Model '{model}' is not allowed for template batching. "
                     f"Allowed models: {allowed}"
        })

    # Validate agents list is non-empty
    if not agents:
        return json.dumps({"error": "No agents provided"})

    # Validate agents count
    if len(agents) > MAX_PARALLEL_TEMPLATE_TASKS:
        return json.dumps({
            "error": f"Too many agents: {len(agents)} (maximum is {MAX_PARALLEL_TEMPLATE_TASKS})"
        })

    # Validate each agent has a non-empty name and check for duplicates
    seen_names: set[str] = set()
    for i, agent in enumerate(agents):
        name = agent.get("name", "")
        if not name:
            return json.dumps({
                "error": f"Agent at index {i} is missing a non-empty 'name' field"
            })
        if name in seen_names:
            return json.dumps({"error": f"Duplicate agent name: '{name}'"})
        seen_names.add(name)

    # Render prompts via template and build tasks list
    tasks = []
    for agent in agents:
        agent_vars = {k: v for k, v in agent.items() if k != "name"}
        name = agent["name"]
        try:
            rendered_prompt = prompt_template.format(**agent_vars)
        except KeyError as e:
            return json.dumps({
                "error": f"Template variable {e} not found in agent '{name}'. "
                         f"Available variables: {sorted(agent_vars.keys())}"
            })
        except (ValueError, IndexError) as e:
            return json.dumps({
                "error": f"Template rendering error for agent '{name}': {e}"
            })
        tasks.append({
            "id": name,
            "name": name,
            "prompt": rendered_prompt,
            "model": model,
        })

    # Delegate to _run_parallel_sub_agents for execution
    return await _run_parallel_sub_agents(
        app=app,
        provider=provider,
        user=user,
        conversation_id=conversation_id,
        timezone=timezone,
        parent_model=model,
        tasks=tasks,
        custom_prompt=custom_prompt,
        project_id=project_id,
        project_guide=project_guide,
        skills_content=skills_content,
        usage_accumulator=usage_accumulator,
        on_event=on_event,
        parent_tool_id=parent_tool_id,
        nested_enabled=nested_enabled,
    )

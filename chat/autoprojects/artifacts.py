"""The Quest Docs and Quest Tables an AutoProject owns, at delete time.

A doc or table with ``autoproject_id`` set belongs to that AutoProject
(``docs.autoproject_id`` / ``data_tables.autoproject_id``, both ON DELETE
CASCADE). The access rule, creation and listing live with the two
features (chat/artifact_access.py, chat/docs/service.py,
chat/tables/service.py); this module is only the AutoProject delete's
part, the twin of the project delete's sweep in chat/project_routes.py:
the rows go with the AutoProject row, so their ids and share rosters are
captured before it and their directories and events handled after it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from chat.docs import events as doc_events
from chat.docs import files as doc_files
from chat.docs import note_suggestions
from chat.tables import events as table_events
from chat.tables import service as table_service
from db import doc_store, table_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapturedArtifacts:
    """An AutoProject's docs and tables (dicts with shares) and the pending
    doc-note suggestion pairs on those docs, read before the delete."""

    docs: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    pending_note_pairs: list = field(default_factory=list)


async def capture(autoproject_id: str) -> CapturedArtifacts:
    """Read what the AutoProject's CASCADE is about to remove: call BEFORE
    ``autoproject_store.delete_autoproject``."""
    docs = await doc_store.list_docs_for_autoproject(autoproject_id)
    tables = await table_store.list_tables_for_autoproject(autoproject_id)
    pairs = await note_suggestions.capture_pending_pairs([doc["id"] for doc in docs])
    return CapturedArtifacts(docs=docs, tables=tables, pending_note_pairs=pairs)


def _delete_doc_dirs(doc_ids: list[str]) -> None:
    for doc_id in doc_ids:
        try:
            doc_files.delete_doc_dir(doc_id)
        except Exception:
            logger.warning(
                "[docs] could not remove the directory of doc %s", doc_id, exc_info=True,
            )


async def sweep(captured: CapturedArtifacts, user_id: int) -> None:
    """Remove the directories of the deleted docs and tables and tell their
    audiences: call AFTER the rows are gone. Best-effort: a failure is
    logged, never raised (the AutoProject delete has already succeeded)."""
    if captured.docs:
        try:
            await asyncio.to_thread(_delete_doc_dirs, [d["id"] for d in captured.docs])
            for doc in captured.docs:
                doc_events.publish_doc_gone_or_access_changed_to(
                    doc_events.doc_audience(doc), doc["id"],
                )
            doc_events.publish_doc_list_changed_to(
                doc_events.docs_audience(captured.docs, user_id),
            )
            await note_suggestions.publish_changed_for_pairs(captured.pending_note_pairs)
        except Exception:
            logger.exception("Sweeping the docs of a deleted AutoProject failed")
    if captured.tables:
        try:
            await asyncio.to_thread(
                table_service.delete_table_dirs, [t["id"] for t in captured.tables],
            )
            for table in captured.tables:
                table_events.publish_table_gone_to(
                    table_events.table_audience(table), table["id"],
                )
            table_events.publish_table_list_changed_to(
                table_events.tables_audience(captured.tables, user_id),
            )
        except Exception:
            logger.exception("Sweeping the tables of a deleted AutoProject failed")

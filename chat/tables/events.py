"""Realtime publishing for Quest Tables.

Two per-user globals on the persistent WebSocket, built by
chat/realtime/events.py: ``table_list_changed`` (list views re-fetch their
window) and ``table_changed {table_id, updated_at, schema_version}`` (an
open viewer of that table re-fetches its grid, and its schema when
``schema_version`` moved, unless it already shows that version).
``updated_at: null`` means "this table's existence or the receiver's
access to it changed": always re-fetch; ``schema_version`` is then null
too (sent on delete, project delete and share changes). Both are
transient per-user events like the Quest Docs pair (no catch-up; a tab
that missed one re-fetches on reconnect), see
docs/architecture/realtime.md.

Audience: everyone who can see the table in the UI -- its owner plus
every direct share recipient; a table with an "everyone" share row
reaches every user connected right now (``bus.connected_user_ids()``).
User ids are deduped, the owner first. :func:`table_audience` captures it
from a table dict that carries its ``shares``; a deletion captures it
BEFORE the rows go (the cascade takes the share rows with them) and
publishes to the captured audience afterwards. Install-wide fan-out is not
filtered per recipient (the ``tables`` gate is not consulted): a connected
user who cannot open the table still receives its id, ``updated_at`` and
``schema_version`` -- never content or a title; the follow-up fetch goes
through the access rule and 404s.

Who publishes what:

- chat/tables/service.py: every row write, schema change and restore
  (:func:`publish_table_write`: ``table_changed`` then
  ``table_list_changed`` to the audience of the bumped row); create
  (``table_list_changed`` to the owner: a new table has no shares);
  delete (:func:`publish_table_gone_to` then
  :func:`publish_table_list_changed_to`, audience captured under the lock
  before the delete).
- chat/project_routes.py: after a project delete's directory sweep, a
  null ``table_changed`` per deleted table to that table's audience, then
  one ``table_list_changed`` to the combined audience (all captured before
  the delete: :func:`table_audience`, :func:`tables_audience`).

Not covered: an account deletion does not notify the recipients of the
deleted user's tables.

Publishing is best-effort: a failure is logged at debug and never undoes
the write. Event-loop thread only (``bus.publish_to_user`` is not
thread-safe) -- never call from ``asyncio.to_thread`` blocks.
"""

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from chat.realtime import bus, events as realtime_events

logger = logging.getLogger(__name__)

__all__ = [
    "TableAudience",
    "audience_user_ids",
    "publish_table_changed_to",
    "publish_table_gone_to",
    "publish_table_list_changed",
    "publish_table_list_changed_to",
    "publish_table_write",
    "table_audience",
    "tables_audience",
]


@dataclass(frozen=True)
class TableAudience:
    """Who hears about a table: explicit user ids (owner first, deduped)
    and whether every connected user does too (an "everyone" share row).

    Resolved to concrete user ids only at publish time
    (:func:`audience_user_ids`), so a captured audience still reaches the
    users connected when the event goes out.
    """

    user_ids: tuple[int, ...]
    everyone: bool = False


def _dedupe(ids: Iterable[Optional[int]]) -> tuple[int, ...]:
    seen: dict[int, None] = {}
    for uid in ids:
        if isinstance(uid, int) and not isinstance(uid, bool):
            seen.setdefault(uid, None)
    return tuple(seen)


def table_audience(table: Mapping) -> TableAudience:
    """The owner + every direct share recipient of ``table``; ``everyone``
    when it has an everyone row (``user_id`` None). A dict without
    ``shares`` is treated as unshared (events never fail a write)."""
    shares = table.get("shares") or []
    return TableAudience(
        user_ids=_dedupe([table.get("owner_id"), *(s.get("user_id") for s in shares)]),
        everyone=any(s.get("user_id") is None for s in shares),
    )


def tables_audience(tables: Iterable[Mapping], *extra_user_ids: int) -> TableAudience:
    """The combined audience of several tables, plus ``extra_user_ids``."""
    ids: list[Optional[int]] = list(extra_user_ids)
    everyone = False
    for table in tables:
        audience = table_audience(table)
        ids.extend(audience.user_ids)
        everyone = everyone or audience.everyone
    return TableAudience(user_ids=_dedupe(ids), everyone=everyone)


def audience_user_ids(audience: TableAudience) -> list[int]:
    """Concrete user ids: the explicit ones, then (for ``everyone``) every
    other connected user. Deduped, order stable."""
    ids = list(audience.user_ids)
    if audience.everyone:
        try:
            connected = bus.connected_user_ids()
        except Exception:
            logger.debug("[tables] listing connected users failed", exc_info=True)
            connected = []
        ids.extend(connected)
    return list(_dedupe(ids))


def _publish(user_id: int, event: dict) -> None:
    try:
        bus.publish_to_user(user_id, event)
    except Exception:
        logger.debug(
            "[tables] publish %s failed (user_id=%s)",
            event.get("type"), user_id, exc_info=True,
        )


def publish_table_list_changed(user_id: int) -> None:
    """Best-effort: tell one user's tabs their table list may have changed."""
    _publish(user_id, realtime_events.make_table_list_changed())


def publish_table_list_changed_to(audience: TableAudience) -> None:
    """``table_list_changed`` to every user of a (captured) audience."""
    for user_id in audience_user_ids(audience):
        publish_table_list_changed(user_id)


def publish_table_changed_to(
    audience: TableAudience,
    table_id: str,
    updated_at: Optional[str],
    schema_version: Optional[int],
) -> None:
    """``table_changed`` to every user of a (captured) audience."""
    event = realtime_events.make_table_changed(table_id, updated_at, schema_version)
    for user_id in audience_user_ids(audience):
        _publish(user_id, dict(event))


def publish_table_write(table: Mapping) -> None:
    """After a row write, schema change or restore: ``table_changed`` then
    ``table_list_changed`` to the audience of ``table`` -- the row returned
    by the quest.db bump, with its shares."""
    audience = table_audience(table)
    publish_table_changed_to(
        audience, table["id"], table.get("updated_at"), table.get("schema_version"),
    )
    publish_table_list_changed_to(audience)


def publish_table_gone_to(audience: TableAudience, table_id: str) -> None:
    """``table_changed {table_id, updated_at: null, schema_version: null}``
    to a (captured) audience: the table was deleted or the receivers'
    access to it changed."""
    publish_table_changed_to(audience, table_id, None, None)

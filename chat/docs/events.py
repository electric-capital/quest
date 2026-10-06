"""Realtime publishing for Quest Docs.

Two per-user globals on the persistent WebSocket, built by
chat/realtime/events.py: ``doc_list_changed`` (list views and open viewers
re-fetch) and ``doc_changed {doc_id, updated_at}`` (an open viewer of that
doc re-fetches unless it already shows that version).

Audience: everyone who can see the doc in the UI -- its owner plus every
direct share recipient; a doc with an "everyone" share row reaches every
user connected right now (``bus.connected_user_ids()``, in-process and
cheap). User ids are deduped, the owner first. :func:`doc_audience`
captures it from a doc dict that carries its ``shares``; a deletion
captures it BEFORE the rows go (the cascade takes the share rows with
them) and publishes to the captured audience afterwards.

Install-wide fan-out (an everyone share) is not filtered per recipient:
the recipients' ``docs`` / ``public_projects`` gates are not consulted, so
a connected user who cannot open the doc still receives its id and
``updated_at`` -- never content or a title; the client's follow-up fetch
goes through the access rule and 404s. Cost: every write to such a doc
triggers one refetch per connected viewer -- each open list view and doc
viewer of every connected user re-fetches on ``doc_list_changed`` (a tab
viewing that very doc may also re-fetch on ``doc_changed`` when its
``updated_at`` is stale).

Who publishes what:

- chat/docs/service.py: every body/asset write (:func:`publish_doc_write`:
  ``doc_changed`` then ``doc_list_changed`` to the audience), UI create and
  delete; the model's ``create_doc`` (a new doc has no shares, so the owner
  is the whole audience).
- chat/docs/routes.py: the metadata-only rename (audience helpers).
- chat/docs/share_routes.py: share add / update / remove send
  ``doc_list_changed`` to the owner and the affected recipient, or to every
  connected user for the everyone row (:func:`publish_share_changed`). The
  owner's open viewer and a recipient's open viewer both re-fetch on it
  (revocation turns into the viewer's 404 state).
- chat/project_routes.py: one ``doc_list_changed`` after a project
  delete's directory sweep, to the combined audience of the project's docs
  captured before the delete (:func:`docs_audience`).

Not covered: an account deletion does not notify the recipients of the
deleted user's docs; their lists refresh on their next event or reload.

Publishing is best-effort: a failure is logged at debug and never undoes
the write. Event-loop thread only (``bus.publish_to_user`` is not
thread-safe) -- never call from ``asyncio.to_thread`` blocks. Tools never
publish.
"""

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from chat.realtime import bus, events as realtime_events

logger = logging.getLogger(__name__)

__all__ = [
    "DocAudience",
    "doc_audience",
    "docs_audience",
    "publish_doc_changed",
    "publish_doc_changed_for",
    "publish_doc_changed_to",
    "publish_doc_list_changed",
    "publish_doc_list_changed_for",
    "publish_doc_list_changed_to",
    "publish_doc_write",
    "publish_share_changed",
]


@dataclass(frozen=True)
class DocAudience:
    """Who hears about a doc: explicit user ids (owner first, deduped) and
    whether every connected user does too (an "everyone" share row).

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


def doc_audience(doc: Mapping) -> DocAudience:
    """The owner + every direct share recipient of ``doc``; ``everyone``
    when it has an everyone row (``user_id`` None).

    ``doc`` is the doc_store dict; a dict without ``shares`` is treated as
    unshared (owner only) -- events are best-effort, never a reason to
    fail a write.
    """
    shares = doc.get("shares") or []
    return DocAudience(
        user_ids=_dedupe([doc.get("owner_id"), *(s.get("user_id") for s in shares)]),
        everyone=any(s.get("user_id") is None for s in shares),
    )


def docs_audience(docs: Iterable[Mapping], *extra_user_ids: int) -> DocAudience:
    """The combined audience of several docs, plus ``extra_user_ids``."""
    ids: list[Optional[int]] = list(extra_user_ids)
    everyone = False
    for doc in docs:
        audience = doc_audience(doc)
        ids.extend(audience.user_ids)
        everyone = everyone or audience.everyone
    return DocAudience(user_ids=_dedupe(ids), everyone=everyone)


def audience_user_ids(audience: DocAudience) -> list[int]:
    """Concrete user ids: the explicit ones, then (for ``everyone``) every
    other connected user. Deduped, order stable."""
    ids = list(audience.user_ids)
    if audience.everyone:
        try:
            connected = bus.connected_user_ids()
        except Exception:
            logger.debug("[docs] listing connected users failed", exc_info=True)
            connected = []
        ids.extend(connected)
    return list(_dedupe(ids))


# ---------------------------------------------------------------------------
# One user (the building blocks; still used directly, e.g. by History's
# copy, which creates a doc only its caller can see)
# ---------------------------------------------------------------------------


def publish_doc_list_changed(user_id: int) -> None:
    """Best-effort: tell the user's tabs their doc list may have changed."""
    try:
        bus.publish_to_user(user_id, realtime_events.make_doc_list_changed())
    except Exception:
        logger.debug(
            "[docs] publish doc_list_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )


def publish_doc_changed(
    user_id: int, doc_id: str, updated_at: Optional[str],
) -> None:
    """Best-effort: tell the user's tabs a doc's body or assets changed."""
    try:
        bus.publish_to_user(
            user_id, realtime_events.make_doc_changed(doc_id, updated_at),
        )
    except Exception:
        logger.debug(
            "[docs] publish doc_changed failed (user_id=%s, doc_id=%s)",
            user_id, doc_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Audiences
# ---------------------------------------------------------------------------


def publish_doc_list_changed_to(audience: DocAudience) -> None:
    """``doc_list_changed`` to every user of a (captured) audience."""
    for user_id in audience_user_ids(audience):
        publish_doc_list_changed(user_id)


def publish_doc_changed_to(
    audience: DocAudience, doc_id: str, updated_at: Optional[str],
) -> None:
    """``doc_changed`` to every user of a (captured) audience."""
    for user_id in audience_user_ids(audience):
        publish_doc_changed(user_id, doc_id, updated_at)


def publish_doc_list_changed_for(doc: Mapping) -> None:
    """``doc_list_changed`` to everyone who can see ``doc`` (with shares)."""
    publish_doc_list_changed_to(doc_audience(doc))


def publish_doc_changed_for(doc: Mapping) -> None:
    """``doc_changed`` (the row's ``updated_at``) to everyone who can see
    ``doc``."""
    publish_doc_changed_to(doc_audience(doc), doc["id"], doc.get("updated_at"))


def publish_doc_write(doc: Mapping) -> None:
    """After a body/asset write: ``doc_changed`` then ``doc_list_changed``
    (a write bumps ``updated_at`` and so the list order) to the audience of
    ``doc`` -- the row returned by the DB bump, with its shares."""
    audience = doc_audience(doc)
    publish_doc_changed_to(audience, doc["id"], doc.get("updated_at"))
    publish_doc_list_changed_to(audience)


def publish_share_changed(owner_id: int, recipient_user_id: Optional[int]) -> None:
    """After a share row was added, changed or removed: ``doc_list_changed``
    to the owner and the affected recipient, or to every connected user when
    the row is the everyone row (``recipient_user_id`` None). Other
    recipients' view of the doc is unchanged, so they are not notified."""
    publish_doc_list_changed_to(DocAudience(
        user_ids=_dedupe([owner_id, recipient_user_id]),
        everyone=recipient_user_id is None,
    ))

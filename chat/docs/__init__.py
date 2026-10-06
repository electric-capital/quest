"""Quest Docs: user- and project-owned markdown documents (see
docs/architecture/quest-docs.md).

Modules:

- ``constants`` -- size caps, retention, and the single not-found text.
- ``access`` -- :func:`resolve_doc_access`, the ONE place the read/write
  matrix lives.
- ``files`` -- on-disk layout under ``DOCS_DIR/<doc_id>/`` (body, assets,
  revisions) with atomic writes and a per-doc lock.
- ``events`` -- the ``doc_list_changed`` / ``doc_changed`` realtime globals.
- ``service`` -- the high-level operations shared by the model-facing tools,
  the HTTP routes, and the ``write_doc`` action request.
- ``routes`` -- the ``/app/api/docs`` HTTP API.
"""

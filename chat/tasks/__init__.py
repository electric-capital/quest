"""Quest Tasks: long-lived units of work answered by fresh-context runs over
an accumulated brief and work log (spec issue #213).

Not to be confused with ``api/tasks.py`` (Google Tasks) or the
``agent_task`` sub-agent tool; model-facing text says "Quest Task" and
"task run".

Modules:

- ``constants`` -- size caps, the connected-services pseudo-key and the
  shared error texts.

The rows (``tasks``, ``task_runs``, ``task_brief_versions``) are written
only through ``db/task_store.py``; the task workspace path is resolved only
through ``ChatStorage.get_task_dir`` / ``get_task_workspace_root``.
"""

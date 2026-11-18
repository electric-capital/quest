"""AutoProjects: long-lived units of work answered by fresh-context runs over
an accumulated brief and work log.

Model-facing text says "AutoProject" and "AutoProject run".

Modules:

- ``constants`` -- size caps, the connected-services pseudo-key and the
  shared error texts.
- ``context`` -- the ``## AutoProject`` system-prompt section of a run.
- ``service`` -- the AutoProject lifecycle (create, edit, brief, archive, read,
  delete AutoProject or run), starting a run, answering a run's question, the
  startup sweeps.
- ``routes`` -- the ``/app/api/autoprojects`` HTTP routes; ``http`` -- their
  error mapping and route guard; ``views`` -- their response shapes (AutoProject
  detail, brief versions, the pages of runs).
- ``runs`` -- ``complete_autoproject_run`` and the run driver (run statuses).
- ``questions`` -- ``ask_autoproject_question``: the ``autoproject_question`` wait
  handle, its validation, transcript message and AutoProject page view.
- ``artifacts`` -- the delete-time sweep of the Quest Docs and Quest Tables
  an AutoProject owns.
- ``events`` -- the ``autoproject_changed`` / ``autoproject_list_changed`` publishes.

The rows (``autoprojects``, ``autoproject_runs``, ``autoproject_brief_versions``) are written
only through ``db/autoproject_store.py``; the AutoProject workspace path is resolved only
through ``ChatStorage.get_autoproject_dir`` / ``get_autoproject_workspace_root``.
"""

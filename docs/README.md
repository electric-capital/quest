# Quest Documentation

Quest is a chat application for multiple LLM providers. It is a FastAPI backend that serves a React + TypeScript single-page app, runs the conversation loop with tools, and keeps user data in SQLite and chat histories as JSON files. Conversations can be grouped into projects with shared workspaces, routines and schedules; agents act on external services through approval-gated action requests and plugin integrations, and run scripts in Podman sandboxes. Models come from Vertex AI (`MODEL_REGISTRY` in `chat/llm/config.py`) and from admin-configured provider instances.

These docs are written for AI agents: they say which files to read and why the system is built the way it is, and point at the code for everything else.

## Where to Start

- [index.md](index.md) -- the one-line-per-doc index of every doc, including plugin docs. It is also pulled into every agent session through `CLAUDE.md`.
- [Architecture Overview](architecture/overview.md) -- the code map: each top-level directory, its role, and the doc that owns it.
- [Development Setup](setup/development.md) and [Development Workflows](setup/development-workflows.md) -- running Quest locally, branching, checks and tests.
- [Style Guide](STYLE_GUIDE.md) -- how to write and update docs; `tests/test_docs.py` enforces its mechanical rules.

## Layout

- `architecture/` -- how each subsystem works and why.
- `api/` -- endpoint and integration references.
- `setup/` -- development, workflow and deployment guides.
- Plugin docs live with their plugin under `plugins/<dir>/docs/`.

# ALWAYS rebase onto origin/main BEFORE starting any work

**This is a required first step for every task, not optional.** Before writing any code, creating a
branch, or making any changes:

```
git fetch origin
git rebase origin/main
```

Other agents and humans push to `main` frequently; starting from a stale base causes avoidable
conflicts and rework. Do this at the start of every task, even if the working tree looks clean or
you recently synced.

# Development basics

This folder uses uv for python dependency management. All python commands should be run using uv.

Developer workflow for this repo is documented in docs/setup/development-workflows.md. If you're doing development
in this repo, follow the workflow steps.

Work on this repo is PR-based: create a feature branch, commit there, push it, and open a PR against
`main` with `gh pr create`. Do not commit straight to `main` -- PRs are reviewed and merged by humans.

Documentation conventions are in docs/STYLE_GUIDE.md. Follow these when writing or updating docs.

## Local server port and process hygiene

The local server port comes from the `dev_port` key in the checkout's gitignored `dev_config.json`
(read by run.py in local mode; default 9000 if the file is missing). If several checkouts of this
repo live side by side on one machine, give each its own port and check `dev_config.json` before
starting an instance.

Stop a local server by the PID you started it with, or by its port (`fuser -k -TERM <port>/tcp`).
Never kill by a broad command-line pattern such as `pkill -f quest`: other checkouts and the
sessions hosting them can match too.


# Project Documentation

The docs/ folder contains comprehensive project documentation. Start with docs/README.md for a table of contents.

A quick-reference index of all doc files with short summaries is auto-included below:

@docs/index.md


# Rules for avoiding unnecessary confirmation prompts
- don't use sed -n to read file contents for single file reads. Use the Read tool.
- don't use the -C argument on git commands unnecessarily. Only use them if you're really running a command outside of the main directory.

# Environment note

You are running in an extension inside of code-server, where file:// in your output don't work. When referencing files, use workspace-relative paths like
src/components/Button.tsx (optionally with a line number). Never use absolute paths or file:// URLs.

When linking the user to a server running on this machine (e.g. a local Quest instance), use the machine's
raw internal IP address (10.x.x.x, from `hostname -I`) in the URL. Do not use `localhost` (the user is
remote) or the machine's internal DNS name (it does not resolve from the user's machine).
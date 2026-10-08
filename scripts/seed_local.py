"""Seed a fresh local-mode database with canned accounts and demo data.

Run by ``run.py --local`` after migrations whenever the database file is
newly created (i.e. on every fresh throwaway run directory). Safe to run
manually too::

    uv run python scripts/seed_local.py

The script is idempotent: it skips seeding when any of the canned users
already exist. It goes through the same store/storage layers as the app
(``db.*_store``, ``chat.storage.ChatStorage``) so the DB rows and the
on-disk chat/workspace artifacts stay coupled exactly like real data.

The canned admin email must stay in sync with
``config.environment.LOCAL_CANNED_ADMIN_EMAIL`` -- that address is the
default ``admin_emails`` entry in local mode.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import environment


CANNED_USERS = [
    {
        "email": environment.LOCAL_CANNED_ADMIN_EMAIL,
        "name": "Ada Admin",
    },
    {
        "email": "alice@quest.local",
        "name": "Alice Chen",
    },
    {
        "email": "bob@quest.local",
        "name": "Bob Rivera",
    },
    {
        # High-volume account for exercising the paged sidebar list and the
        # server-side origin/project filters (see _seed_heavy_user).
        "email": "heidi@quest.local",
        "name": "Heidi Highvolume",
    },
]

MEMORIES = [
    "Alice prefers concise answers with bullet points over long prose.",
    "Alice's team ships a weekly investor update every Friday morning.",
    "Timezone: US/Pacific. Schedule suggestions accordingly.",
]

GUIDE_NAME = "Concise analyst"
GUIDE_CONTENT = (
    "You are a concise analyst. Lead with the answer, use short bullet "
    "points, and quantify claims whenever possible. Ask at most one "
    "clarifying question, and only when truly blocked."
)

SKILL_NAME = "weekly-update-format"
SKILL_DESCRIPTION = "Format for the weekly team status update."
SKILL_CONTENT = (
    "# Weekly update format\n\n"
    "When asked to draft the weekly update, produce three sections:\n\n"
    "1. **Shipped** - bullets of what landed, each with a one-line impact.\n"
    "2. **In flight** - work started but not finished, with expected dates.\n"
    "3. **Risks** - anything that could slip, with a proposed mitigation.\n\n"
    "Keep the whole update under 200 words.\n"
)

PROJECT_NAME = "Demo Project"
PROJECT_GUIDE = (
    "This project tracks the Quest local-mode demo. Keep analysis "
    "grounded in the files in the project workspace."
)

PROJECT_CHAT_NOTES = """# Chat notes

This file lives in this conversation's own workspace, not in the shared
project files. Sibling conversations in the project do not see it.
"""

WORKSPACE_README = """# Demo Project workspace

This file was created by scripts/seed_local.py so the file browser has
something to show on a fresh local run. Feel free to delete it.
"""

# Small viewable files that render inline in the file browser, plus one file
# well over the 5 MB inline-preview cap (chat/file_storage.py:MAX_VIEW_SIZE)
# so the FileViewerModal "too large to preview" error card is demonstrable on
# a fresh local run. Written into a `preview-samples/` subfolder of the
# Demo Project workspace.
SMALL_TXT = (
    "This is a small text file seeded by scripts/seed_local.py.\n"
    "It is well under the 5 MB inline-preview limit, so it renders inline.\n"
)
SMALL_JSON = json.dumps(
    {
        "note": "Small JSON file — renders inline via the JSON tree viewer.",
        "under_limit": True,
        "items": [1, 2, 3],
    },
    indent=2,
)
SMALL_CSV = "name,role,team\nAlice,Engineer,Platform\nBob,Designer,Product\n"


def _build_large_json() -> str:
    """Return a JSON string comfortably over the 5 MB inline-preview cap."""
    # ~8 MB: 120k rows of ~68 bytes each keeps this safely above MAX_VIEW_SIZE
    # regardless of small serialization differences.
    payload = {
        "note": "Large JSON file seeded to exercise the oversized-preview path.",
        "rows": [{"id": i, "value": "x" * 40} for i in range(120_000)],
    }
    return json.dumps(payload)

# (user message, assistant reply) pairs; each becomes one conversation.
CONVERSATIONS = [
    (
        "What can you help me with in this local demo environment?",
        "This is a seeded local-mode conversation, so the transcript UI has "
        "real content to render.\n\n"
        "A few things to try:\n"
        "- Open the **Demo Project** to see a project with a workspace file.\n"
        "- Check **Settings → Data Connections**: with no credentials "
        "configured, every connector shows as cleanly disconnected.\n"
        "- Sending a new message only works once an LLM credential "
        "(e.g. a Gemini API key) is configured.",
    ),
    (
        "Draft the weekly update from my notes: shipped run modes, testing in flight.",
        "**Shipped**\n"
        "- Three run modes (local/staging/prod) - local boots with zero credentials.\n\n"
        "**In flight**\n"
        "- End-to-end testing of the canned-account login flow.\n\n"
        "**Risks**\n"
        "- Staging deploy automation not designed yet; mitigation: keep prod "
        "process unchanged until it is.",
    ),
]

PROJECT_CONVERSATION = (
    "Summarize what's in this project's workspace.",
    "The workspace currently contains a single seeded file, "
    "**README.md**, which describes the Demo Project workspace itself. "
    "Upload files through the right-panel file browser and I can work "
    "with them here.",
)

# --- High-volume user (heidi@quest.local) ------------------------------------
#
# Volumes chosen to be several pages past the sidebar's page size so "Load
# more" is exercised, with enough Slack / Inference API / archived / project
# rows that every filter toggle visibly changes the list.
HEAVY_WEB_CONVERSATIONS = 140
HEAVY_ARCHIVED = 10  # oldest N of the web conversations get archived
HEAVY_SLACK_CONVERSATIONS = 12
HEAVY_INFERENCE_CONVERSATIONS = 12
HEAVY_PROJECT_CONVERSATIONS = 8
HEAVY_PROJECT_NAME = "Heidi's Research"

HEAVY_TOPICS = [
    "Quarterly revenue model sanity check",
    "Draft reply to the diligence questionnaire",
    "Summarize this week's portfolio news",
    "Compare cloud hosting cost estimates",
    "Clean up the LP contact spreadsheet",
    "Outline the offsite agenda",
    "Debug the failing data pipeline run",
    "Research competitor pricing pages",
    "Rewrite the onboarding email sequence",
    "Prep notes for the Tuesday board call",
]


def _heavy_reply(topic: str) -> str:
    return (
        f"Seeded assistant reply for **{topic.lower()}** — this conversation "
        "exists so the sidebar has a realistic high-volume account to page "
        "through."
    )


async def _seed_heavy_user(user: dict) -> None:
    """Give heidi@quest.local a realistic high-volume account.

    Creates well over a sidebar page of standalone web conversations plus
    Slack-origin, Inference-API-origin, archived, and project conversations,
    with last_message_at spread over the past ~3 months so the paged list
    has a meaningful order to walk through.
    """
    from datetime import datetime, timedelta, timezone

    from chat.storage import ChatStorage
    from db.conversation_store import archive_conversation, update_last_message_at
    from db.project_store import create_project

    now = datetime.now(timezone.utc)
    user_id = user["id"]

    async def _make_conversation(title_seed: str, ts, origin: str = "web") -> str:
        topic = HEAVY_TOPICS[title_seed_index % len(HEAVY_TOPICS)]
        user_message = f"{topic} ({title_seed})"
        if origin == "slack":
            conv_id = await ChatStorage.create_slack_conversation(
                user_id,
                slack_channel_id="D00SEEDED",
                slack_thread_ts=f"{ts.timestamp():.6f}",
                slack_user_id="U00SEEDED",
            )
        elif origin == "inference_api":
            conv_id = await ChatStorage.create_inference_api_conversation(user_id)
        else:
            conv_id, _ = await ChatStorage.create_conversation(user_id)
        await ChatStorage.append_message(conv_id, "user", user_message)
        await ChatStorage.append_message(conv_id, "assistant", _heavy_reply(topic))
        # append_message bumps last_message_at to "now"; rewrite it with the
        # synthetic timestamp so the list has a spread instead of one instant.
        await update_last_message_at(conv_id, ts)
        return conv_id

    title_seed_index = 0
    web_ids: list[str] = []
    for i in range(HEAVY_WEB_CONVERSATIONS):
        ts = now - timedelta(hours=15 * i, minutes=i % 47)
        web_ids.append(await _make_conversation(f"chat {i + 1}", ts))
        title_seed_index += 1
    print(f"  {HEAVY_WEB_CONVERSATIONS} web conversations for {user['email']}")

    for conv_id in web_ids[-HEAVY_ARCHIVED:]:
        await archive_conversation(user_id, conv_id)
    print(f"  {HEAVY_ARCHIVED} of them archived")

    for i in range(HEAVY_SLACK_CONVERSATIONS):
        ts = now - timedelta(hours=11 * i + 3, minutes=(i * 7) % 53)
        await _make_conversation(f"slack {i + 1}", ts, origin="slack")
        title_seed_index += 1
    print(f"  {HEAVY_SLACK_CONVERSATIONS} Slack-origin conversations")

    for i in range(HEAVY_INFERENCE_CONVERSATIONS):
        ts = now - timedelta(hours=13 * i + 5, minutes=(i * 11) % 59)
        await _make_conversation(f"inference {i + 1}", ts, origin="inference_api")
        title_seed_index += 1
    print(f"  {HEAVY_INFERENCE_CONVERSATIONS} Inference-API-origin conversations")

    project = await create_project(
        user_id,
        HEAVY_PROJECT_NAME,
        guide="Seeded high-volume demo project for heidi@quest.local.",
    )
    ChatStorage.create_project_workspace(project["id"])
    for i in range(HEAVY_PROJECT_CONVERSATIONS):
        topic = HEAVY_TOPICS[i % len(HEAVY_TOPICS)]
        conv_id, _ = await ChatStorage.create_project_conversation(
            user_id, project["id"]
        )
        await ChatStorage.append_message(
            conv_id, "user", f"{topic} (project chat {i + 1})"
        )
        await ChatStorage.append_message(conv_id, "assistant", _heavy_reply(topic))
        await update_last_message_at(
            conv_id, now - timedelta(hours=17 * i + 2)
        )
    print(
        f"  project: {HEAVY_PROJECT_NAME} "
        f"({HEAVY_PROJECT_CONVERSATIONS} conversations)"
    )


async def seed() -> bool:
    """Create the canned users and demo data. Returns False when skipped."""
    from auth.config import generate_api_key
    from chat.storage import ChatStorage
    from db.guide_store import create_guide
    from db.memory_store import create_memory
    from db.project_store import create_project
    from db.skill_store import create_skill
    from db.user_store import create_user, get_user_by_email

    if await get_user_by_email(CANNED_USERS[0]["email"]):
        print("Seed skipped: canned users already exist.")
        return False

    from auth.dev_login import _generate_dev_google_sub

    users = {}
    for spec in CANNED_USERS:
        users[spec["email"]] = await create_user(
            email=spec["email"],
            name=spec["name"],
            api_key=generate_api_key(),
            google_oauth=None,
            settings={},
            google_sub=_generate_dev_google_sub(spec["email"]),
        )
        print(f"  user: {spec['email']} ({spec['name']})")

    alice = users["alice@quest.local"]

    for content in MEMORIES:
        await create_memory(alice["id"], content)
    print(f"  {len(MEMORIES)} memories for alice@quest.local")

    await create_guide(alice["id"], GUIDE_NAME, content=GUIDE_CONTENT)
    print(f"  guide: {GUIDE_NAME}")

    await create_skill(
        creator_id=alice["id"],
        name=SKILL_NAME,
        description=SKILL_DESCRIPTION,
        content=SKILL_CONTENT,
    )
    print(f"  skill: {SKILL_NAME}")

    project = await create_project(alice["id"], PROJECT_NAME, guide=PROJECT_GUIDE)
    # Project files shared by every conversation of the project live in the
    # project workspace root (projects/{id}/workspace/workspace/).
    browsable = ChatStorage.create_project_workspace(project["id"])
    (browsable / "README.md").write_text(WORKSPACE_README)
    print(f"  project: {PROJECT_NAME} (workspace README.md)")

    samples = browsable / "preview-samples"
    samples.mkdir(exist_ok=True)
    (samples / "small.txt").write_text(SMALL_TXT)
    (samples / "small.json").write_text(SMALL_JSON)
    (samples / "small.csv").write_text(SMALL_CSV)
    large_json = _build_large_json()
    (samples / "large.json").write_text(large_json)
    large_mb = len(large_json.encode("utf-8")) / (1024 * 1024)
    print(
        f"  preview-samples/: small.txt, small.json, small.csv, "
        f"large.json (~{large_mb:.1f} MB, over the 5 MB view cap)"
    )

    for user_message, assistant_message in CONVERSATIONS:
        conversation_id, _ = await ChatStorage.create_conversation(alice["id"])
        await ChatStorage.append_message(conversation_id, "user", user_message)
        await ChatStorage.append_message(conversation_id, "assistant", assistant_message)
    print(f"  {len(CONVERSATIONS)} conversations for alice@quest.local")

    user_message, assistant_message = PROJECT_CONVERSATION
    conversation_id, _ = await ChatStorage.create_project_conversation(
        alice["id"], project["id"]
    )
    await ChatStorage.append_message(conversation_id, "user", user_message)
    await ChatStorage.append_message(conversation_id, "assistant", assistant_message)
    # Each project conversation also has its own conversation workspace
    # (chats/{id}/workspace/), separate from the shared project files.
    chat_root = ChatStorage.get_conversation_workspace_root(conversation_id)
    chat_root.mkdir(parents=True, exist_ok=True)
    (chat_root / "chat-notes.md").write_text(PROJECT_CHAT_NOTES)
    print("  1 project conversation (conversation workspace chat-notes.md)")

    await _seed_heavy_user(users["heidi@quest.local"])

    return True


def main() -> None:
    if not environment.is_local():
        print(
            "Refusing to seed: QUEST_ENV is "
            f"'{environment.get_quest_env()}', not 'local'."
        )
        sys.exit(1)

    from config.paths import DATA_DIR, DATABASE_PATH

    if not DATABASE_PATH.exists():
        print(
            f"Refusing to seed: no database at {DATABASE_PATH}. "
            "Run migrations first (run.py does this automatically)."
        )
        sys.exit(1)

    print(f"Seeding local demo data into {DATA_DIR} ...")
    asyncio.run(seed())


if __name__ == "__main__":
    main()

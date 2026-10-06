"""Caps, retention, and shared error text for Quest Docs.

Every limit the spec leaves tunable lives here so the tools, routes, the
``write_doc`` action request, and the tests agree on one number.
"""

# Body of doc.md, in bytes (1 MB).
DOC_MAX_CONTENT_SIZE = 1_048_576

# Embedded raster images under ``assets/``.
DOC_MAX_IMAGE_SIZE = 5 * 1024 * 1024
DOC_MAX_ASSETS = 200
DOC_MAX_ASSETS_TOTAL_BYTES = 100 * 1024 * 1024
DOC_ASSET_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# read_doc without a line range returns at most this many characters and
# tells the model to page with start_line / end_line.
DOC_READ_MAX_CHARS = 200_000

# Revision snapshots older than this are pruned at write time; the newest
# snapshot is always kept so an idle doc keeps one restore point.
DOC_REVISION_RETENTION_DAYS = 30
# Hard cap on snapshots per doc inside the window (oldest pruned first, the
# newest always kept): bounds the disk a writer looping on edit/append can
# consume -- each snapshot is up to DOC_MAX_CONTENT_SIZE.
DOC_REVISION_MAX_COUNT = 200

DOC_TITLE_MAX_LEN = 200
DOC_DESCRIPTION_MAX_LEN = 500

# search_docs: bounded body scan.
DOC_SEARCH_MAX_SCAN_BYTES = 50 * 1024 * 1024
DOC_SEARCH_SNIPPETS_PER_DOC = 3
DOC_SEARCH_SNIPPET_CHARS = 200

DOC_MODES = ("private", "public")
DOC_SHARE_PERMISSIONS = ("read", "write")

# The feature gate (config/feature_gates.py FEATURE_DOCS) and the
# connected-services pseudo-key that hides the tools and the system skill
# from prompts while the gate is closed for the user.
DOCS_SERVICE_KEY = "docs"


def doc_not_found_message(doc_id: str) -> str:
    """The ONE not-found text.

    Used verbatim for a doc that does not exist AND for a doc the caller
    may not see (invariant 2 in the spec: a hidden doc behaves exactly like
    a nonexistent one). Never build a not-found message any other way; a
    test pins byte-for-byte equality.
    """
    return f"Doc not found: {doc_id}"


def public_docs_disabled_message() -> str:
    """UI-facing text (400 ``public_projects_disabled``) when a user asks
    for a public user doc while the ``public_projects`` gate is closed for
    them: creating one, or switching a private doc to public."""
    return (
        "Public docs need public projects, which are not available to your "
        "account."
    )


def docs_disabled_message() -> str:
    """Model/user-facing text when the ``docs`` feature gate is closed."""
    return (
        "Quest Docs is not enabled for this user. An admin can enable it "
        "in Settings > Features."
    )

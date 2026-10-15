"""Ramp API instructions and token helper.

Ramp read operations are handled via the ``authed_get`` tool, which makes
authenticated GET requests directly to the Ramp developer API at
``https://api.ramp.com/developer/v1/...``.  The service entry (credential
loader, Bearer auth injector, and the allowed-endpoints regex list) lives
in ``chat/gemini_api/authed_get.py``.

Unlike GitHub, Ramp access tokens expire and refresh tokens rotate on every
use, so ``get_ramp_token`` proactively refreshes near-expiry tokens (same
pattern as ``plugins/twitter/upstream.py``) and the registry entry sets
``retry_on_401`` as a backstop.
"""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException

logger = logging.getLogger(__name__)

RAMP_API_BASE = "https://api.ramp.com"

# Safety margin: refresh the token if it expires within 5 minutes
_REFRESH_MARGIN_SECONDS = 300


async def get_ramp_token(user: dict) -> str:
    """Get a valid Ramp access token, refreshing if expired.

    Args:
        user: User dict from the database.

    Returns:
        A valid access token string.

    Raises:
        HTTPException(401): If Ramp is not connected or token refresh fails.
    """
    ramp_oauth = user.get("ramp_oauth")
    if not ramp_oauth or not ramp_oauth.get("access_token"):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "ramp_oauth_required",
                "message": (
                    "Ramp not connected. "
                    "Please connect Ramp in Settings > Data Connections "
                    "(only a Ramp Admin or Business Owner can connect)."
                ),
            },
        )

    expires_at_str = ramp_oauth.get("expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            # Make timezone-aware if not already
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if (expires_at - now).total_seconds() < _REFRESH_MARGIN_SECONDS:
                # Token is expired or will expire soon — refresh it
                from auth.ramp import refresh_ramp_token
                return await refresh_ramp_token(user)
        except (ValueError, TypeError):
            pass  # If we can't parse the expiry, proceed and try the existing token

    return ramp_oauth["access_token"]


# ---------------------------------------------------------------------------
# Instruction text for the Ramp API
# ---------------------------------------------------------------------------

def get_instructions(base_url: str) -> str:
    """Ramp API documentation section (accessed via authed_get)."""
    return f"""## Ramp API (via authed_get)

Access the Ramp developer API (corporate cards, spend, bills, reimbursements) using `authed_get` with the full Ramp API URL. Authentication is handled automatically -- the user's Ramp OAuth token is injected as a Bearer header and refreshed when expired.

**Ramp admins only:** Ramp lets only Admins and Business Owners of the Ramp business connect, and the token sees the whole business's data (every employee's transactions, cards, bills, ...), not just the connected user's own. Treat results accordingly and do not assume "my transactions" means the connected user's unless filtered by `user_id`.

**Base URL:** `{RAMP_API_BASE}/developer/v1`

**Key API Paths:**

| Path | Description |
|------|-------------|
| `/business` | Get the business profile |
| `/business/balance` | Get the business's current balance |
| `/users` | List users (employees) |
| `/users/{{user_id}}` | Get a single user |
| `/transactions` | List card transactions (filterable; excludes declined unless `state` set) |
| `/transactions/{{transaction_id}}` | Get a single transaction |
| `/cards/physical` | List physical cards |
| `/cards/virtual` | List virtual cards |
| `/limits` | List spend limits |
| `/limits/{{spend_limit_id}}` | Get a single spend limit |
| `/spend-programs` | List spend programs |
| `/bills` | List bills (AP) |
| `/bills/{{bill_id}}` | Get a single bill |
| `/reimbursements` | List reimbursements |
| `/reimbursements/{{reimbursement_id}}` | Get a single reimbursement |
| `/receipts` | List receipts |
| `/receipts/{{receipt_id}}` | Get a single receipt |
| `/memos` | List transaction memos |
| `/vendors` | List vendors |
| `/vendors/{{vendor_id}}` | Get a single vendor |
| `/purchase-orders` | List purchase orders |
| `/merchants` | List merchants the business has transacted with |
| `/statements` | List statements |
| `/statements/{{statement_id}}` | Get a single statement |
| `/transfers` | List transfers (payments to Ramp) |
| `/cashbacks` | List cashback payments |
| `/departments` | List departments |
| `/locations` | List locations |
| `/entities` | List business entities |
| `/accounting/accounts` | List general-ledger accounts |
| `/accounting/fields` | List accounting fields |
| `/accounting/vendors` | List accounting vendors |
| `/audit-logs/events` | List audit-log events |

**Pagination:**
- List endpoints use cursor pagination: pass `page_size` (results per page) and `start` (cursor id from a previous page).
- List responses include a `page.next` field holding the complete URL of the next page -- pass it straight back to `authed_get` until it is null.

**Common filters (vary by endpoint):**
- `from_date` / `to_date`: ISO 8601 window for transactions and other dated resources
- `state`: lifecycle filter (e.g. transaction `CLEARED`, `PENDING`, `DECLINED`)
- `user_id`, `department_id`, `location_id`, `entity_id`, `merchant_id`: ownership filters on list endpoints
- `min_amount` / `max_amount`: amount range on transactions (amounts are in cents)

**Example tool calls:**

```
# Get the business profile and balance
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/business"}})
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/business/balance"}})

# List recent card transactions
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/transactions?page_size=25&from_date=2026-07-01T00:00:00Z"}})

# Get a single transaction
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/transactions/3e5a2b8c-...-id"}})

# List users, then a user's transactions
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/users?page_size=50"}})
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/transactions?user_id=<user_uuid>"}})

# List virtual cards and open bills
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/cards/virtual?page_size=25"}})
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/bills?page_size=25"}})

# List reimbursements
tool_call(tool_name="authed_get", arguments={{"url": "{RAMP_API_BASE}/developer/v1/reimbursements?page_size=25"}})
```

**Important Notes:**
- Requires Ramp to be connected in Settings > Data Connections by a Ramp Admin or Business Owner; regular Ramp employees cannot connect.
- Read-only: only GET paths on the allow-list in the `api.ramp.com` entry of `_SERVICE_REGISTRY` are reachable via `authed_get`. Write operations are not exposed.
- Card-vault endpoints (full card numbers, `/cards/vault/...`) are intentionally NOT accessible.
- Monetary amounts are integers in the currency's minor unit (cents for USD).
- IDs are UUIDs. Resolve names to IDs via the list endpoints (e.g. `/users`, `/vendors`) before filtering.
- Large result sets: prefer narrow date windows and `page_size` over pulling everything; use the `output_file` argument of `authed_get` to save big responses to the workspace."""

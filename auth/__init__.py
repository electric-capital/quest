"""Auth submodule - OAuth flows and authentication for Quest."""

# Re-export the symbols quest.py imports from the package root. Everything
# else is imported from its defining module (auth.config, auth.session,
# auth.google_credentials, auth.popup_helpers).
from auth.config import generate_api_key
from auth.session import get_current_user_cookie_or_apikey

# Routers for registration in the main app. The Slack OAuth router and
# the Telegram login router are plugin-provided (plugins/slack/oauth.py,
# plugins/telegram/auth.py) and mounted by mount_plugin_oauth_routers()
# in quest.py.
from auth.google_login import router as google_login_router
from auth.google_services import router as google_services_router
from auth.airtable import router as airtable_router
from auth.ramp import router as ramp_router
from auth.service_key import router as service_key_router
from auth.connections import router as connections_router
from auth.dev_login import router as dev_login_router
from auth.password_login import router as password_login_router

"""
Supabase client setup.

Env vars expected (set these in Railway → Variables):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY   # server-side key, never exposed to the client app
"""
import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set. "
                "Add them as environment variables before starting the server."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client

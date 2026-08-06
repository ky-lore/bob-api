"""
Settings for the adspend service. Deliberately self-contained (own Settings
class, own .env reads) rather than importing bob-master's app/config.py, even
though both currently live in one repo — this service is meant to be reused
by other projects later, so it shouldn't assume bob-master's app package is
importable from wherever it ends up running.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Google Ads API (REST, not the grpc-based google-ads SDK — same thin-
    # httpx-client style as the rest of this codebase's integrations) ---
    google_ads_developer_token: str
    google_ads_client_id: str
    google_ads_client_secret: str
    google_ads_refresh_token: str
    google_ads_login_customer_id: str  # the MCC's customer ID, digits only, no dashes
    # v18 (originally assumed) 404s -- confirmed against the live API 2026-08-06
    # that v20 is reachable but rejects this client's query shape, and v21 is
    # the first version that returns a clean 200. Bump this if Google
    # deprecates v21 later; there's no way to auto-detect the "current" version.
    google_ads_api_version: str = "v21"

    # --- Atlas (source of truth for the account universe + per-account
    # googleMccId/metaAdAccountId — see ../app/integrations/atlas_client.py for
    # the sibling copy used by the LLM+write service; duplicated here on
    # purpose rather than imported, for the same reuse-elsewhere reason as
    # above) ---
    atlas_api_key: str
    atlas_base_url: str = "https://server-production-3d76.up.railway.app"


@lru_cache
def get_settings() -> Settings:
    return Settings()

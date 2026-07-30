"""
Settings sourced from docs/SECRETS_AND_INTEGRATIONS_MAP.md. Non-secret system IDs
(list IDs, channel IDs, pipeline/stage IDs) are safe to keep as code defaults per
that doc's §3 — they identify records, they don't grant access on their own.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Persistence ---
    database_url: str

    # --- GHL (direct REST, no MCP connector — see integrations map §2) ---
    ghl_api_key: str
    ghl_location_id: str
    ghl_base_url: str = "https://services.leadconnectorhq.com"
    ghl_api_version: str = "2021-07-28"
    # WAF 403s any request missing a User-Agent header — do not drop this.
    ghl_user_agent: str = "AM-QA/1.0"

    # --- ClickUp ---
    clickup_api_token: str
    clickup_base_url: str = "https://api.clickup.com/api/v2"
    clickup_workspace_id: str = "10552018"
    clickup_go_live_list_id: str = "901417990784"       # "Go-Live Pipeline (last 90 days)"
    clickup_web_build_list_id: str = "901413623955"
    clickup_retention_list_id: str = "901417897821"     # current — see known drift re: old ID below
    clickup_retention_list_id_deprecated: str = "901417799940"  # do NOT use; drift artifact only
    clickup_chris_action_items_list_id: str = "901417226802"

    # --- Slack ---
    slack_bot_token: str
    slack_workspace: str = "advancedmarketers.slack.com"
    slack_christian_user_id: str = "U01GYV63X9D"
    slack_jaime_user_id: str = "U0B0XG32YLV"

    # --- Google Drive (service account) ---
    google_service_account_json: str  # path to the service account key file, or raw JSON
    drive_google_ads_heartbeat_file_id: str = "1PjcXsvoFmSP8nVM_C_Qa7MeEVF6yUEAv6xH_LLKV-KQ"
    drive_google_ads_heartbeat_tab: str = "Heartbeat"
    drive_meta_heartbeat_file_id: str = "1rOsGhG4_vyTRBhR62LD2f6Xhqmg636WqYdvGLx5ZamA"
    drive_meta_heartbeat_tab: str = "Heartbeat-Meta"
    drive_tv_board_feed_file_id: str = "1mtDzvPkxcYXKjlRcWTC37yoTx0jaiaxbfI9H8_nUYdA"

    # --- GHL system IDs used by this task specifically ---
    ghl_adv_master_pipeline_id: str = "1rySFshGqxtuO5hF2z2f"
    ghl_closed_won_stage_id: str = "9fff7088-7251-470b-99b5-dc2374630cde"
    ghl_field_slack_channel_id: str = "vcSOMOQUD0U6LzoLg2oy"
    ghl_field_package_type: str = "XMXL75J1u9Tu55IDHG4D"
    ghl_field_sales_notes_doc: str = "PrWBcqWMlGqWBVwfFY3a"

    # --- Scheduler ---
    # Original cadence per docs/SCHEDULED_TASKS_REGISTRY_SNAPSHOT.md: "0 7 * * 1-5" (weekdays 7:01 AM).
    daily_go_live_audit_cron: str = "0 7 * * 1-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()

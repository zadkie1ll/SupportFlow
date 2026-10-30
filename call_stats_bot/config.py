from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration isolated from the existing Bitrix application."""

    model_config = SettingsConfigDict(
        env_file=".env.calls",
        env_file_encoding="utf-8",
        env_prefix="CALLBOT_",
        extra="ignore",
    )

    megafon_crm_token: str
    telegram_bot_token: str
    telegram_chat_ids: str
    telegram_admin_ids: str = ""
    telegram_webhook_secret: str
    employees: str
    megafon_api_url: str = ""
    megafon_api_key: str = ""
    megafon_api_auth_header: str = "X-API-KEY"
    megafon_api_relay_url: str = ""
    report_time: str = "19:00"
    timezone: str = "Europe/Moscow"
    database_path: str = "data/call_stats.sqlite3"
    bitrix_webhook_url: str = ""
    bitrix_gateway_url: str = ""
    bitrix_gateway_token: str = ""
    bitrix_gateway_profile: str = "default"
    bitrix_user_map: str = "{}"
    substitution_poll_seconds: int = 60
    employee_poll_seconds: int = 300
    telegram_polling_enabled: bool = False
    miniapp_public_url: str = ""
    miniapp_auth_max_age_seconds: int = 86400
    openai_api_key: str = ""
    analysis_enabled: bool = True
    transcription_model: str = "gpt-4o-transcribe-diarize"
    call_summary_model: str = "gpt-5-mini"
    analysis_concurrency: int = 2
    analysis_download_timeout_seconds: int = 120
    analysis_max_audio_bytes: int = 104857600
    recording_relay_url: str = ""

    @property
    def chat_ids(self) -> list[int]:
        return [int(item.strip()) for item in self.telegram_chat_ids.split(",") if item.strip()]

    @property
    def admin_ids(self) -> set[int]:
        configured = self.telegram_admin_ids or self.telegram_chat_ids
        return {int(item.strip()) for item in configured.split(",") if item.strip()}

    @property
    def employee_map(self) -> dict[str, str]:
        import json

        parsed = json.loads(self.employees)
        if not isinstance(parsed, dict):
            raise ValueError("CALLBOT_EMPLOYEES must be a JSON object")
        return {str(key): str(name) for key, name in parsed.items()}

    @property
    def telephony_bitrix_map(self) -> dict[str, int]:
        import json

        parsed = json.loads(self.bitrix_user_map)
        if not isinstance(parsed, dict):
            raise ValueError("CALLBOT_BITRIX_USER_MAP must be a JSON object")
        result = {str(extension): int(user_id) for extension, user_id in parsed.items()}
        configured = set(self.employee_map)
        if set(result) != configured:
            missing = sorted(configured - set(result))
            unknown = sorted(set(result) - configured)
            raise ValueError(
                f"Bitrix mapping must exactly match telephony employees; missing={missing}, unknown={unknown}"
            )
        if len(set(result.values())) != len(result):
            raise ValueError("Every telephony employee must map to a unique Bitrix user")
        return result

    @property
    def normalized_miniapp_url(self) -> str:
        return self.miniapp_public_url.strip().rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()

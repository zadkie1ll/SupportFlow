from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://call_queue:call_queue@postgres:5432/call_queue"
    db_schema: str = "call_queue"

    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 12
    refresh_token_expires_days: int = 30

    monolith_base_url: str = "http://host.docker.internal:8000"
    monolith_internal_token: str = ""
    sales_manager_cache_ttl_seconds: int = 60

    bitrix_gateway_base_url: str = "http://host.docker.internal:8002"
    bitrix_gateway_token: str = ""
    bitrix_gateway_profile: str = "default"

    # Используется только для построения ссылок на сделки/лиды (deal_service.build_entity_url),
    # сами запросы к Bitrix теперь идут через bitrix-gateway.
    bitrix_webhook_url: str = ""
    bitrix_base_url: str = ""
    call_queue_bitrix_time_zone: str = "Europe/Moscow"
    call_queue_bitrix_deal_unanswered_stage_id: str = "PREPARATION"
    call_queue_bitrix_lead_unanswered_status_id: str = "IN_PROCESS"
    call_queue_max_desktop_url: str = "max://"
    deal_duplication_source_category_id: int = 2
    deal_duplication_target_category_id: int = 10

    megafon_vats_api_url: str = ""
    megafon_vats_api_key: str = ""
    megafon_vats_auth_header: str = "X-API-KEY"
    megafon_vats_auth_mode: str = "header"
    megafon_vats_crm_auth_key: str = ""

    # Пока монолит и этот сервис работают параллельно, MegaFon настроен слать вебхук
    # только сюда — форвардим копию каждого события в монолит, чтобы его собственная
    # (пока не отключённая) очередь звонков тоже продолжала получать статусы.
    # Пусто = форвардинг выключен.
    megafon_webhook_forward_url: str = ""
    # Независимая best-effort копия для локального сервиса статистики звонков.
    # Ошибка этого получателя не влияет на основной webhook и монолит.
    megafon_stats_forward_url: str = ""


settings = Settings()

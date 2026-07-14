from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    database_dsn: str = "postgresql://jhkang:jhkang@127.0.0.1:5432/jhkang_network"

    window_size_ms: int = 200
    window_flush_interval_ms: int = 50
    node_timeout_seconds: float = 5.0
    timestamp_max_skew_ms: int = 600_000
    rssi_min: int = -100
    rssi_max: int = -10

settings = Settings()

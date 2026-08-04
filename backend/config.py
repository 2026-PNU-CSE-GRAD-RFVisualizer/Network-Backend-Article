from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 프로젝트 루트 (backend/ 의 상위). 저장 경로의 기준점이다.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_path(value: str) -> Path:
    """절대 경로면 그대로, 상대 경로면 프로젝트 루트 기준으로 해석한다."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    database_dsn: str = "postgresql://jhkang:jhkang@127.0.0.1:5432/jhkang_network"

    enable_realtime: bool = False

    window_size_ms: int = 200
    window_flush_interval_ms: int = 50
    node_timeout_seconds: float = 5.0
    timestamp_max_skew_ms: int = 600_000
    rssi_min: int = -100
    rssi_max: int = -10

    experiment_data_dir: str = "data"
    export_root: str = "experiments"
    default_session_seconds: int = 30
    expected_samples_per_point: int = 30
    rssi_filtered_scale: float = 1.0

    @property
    def experiment_data_path(self) -> Path:
        return resolve_path(self.experiment_data_dir)

    @property
    def export_root_path(self) -> Path:
        return resolve_path(self.export_root)


settings = Settings()
"""Settings, read from environment variables."""
import os
import secrets
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_url = os.environ.get("DATABASE_URL", f"sqlite:///{self.data_dir / 'app.db'}")
        self.public_url = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
        self.cookie_secure = self.public_url.startswith("https://")
        self.mqtt_enabled = os.environ.get("MQTT_ENABLED", "1") == "1"
        self.mqtt_host = os.environ.get("MQTT_HOST", "localhost")
        self.mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
        self.default_tz = os.environ.get("DEFAULT_TZ", "UTC")
        self.cert_days = int(os.environ.get("CERT_DAYS", "30"))
        self.session_hours = int(os.environ.get("SESSION_HOURS", "12"))
        self.admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
        self.admin_password = os.environ.get("ADMIN_PASSWORD")
        self.secret_key = os.environ.get("SECRET_KEY") or self._local_secret()

    def _local_secret(self) -> str:
        """Development fallback: a random key kept beside the database."""
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48))
        return path.read_text().strip()


settings = Settings()

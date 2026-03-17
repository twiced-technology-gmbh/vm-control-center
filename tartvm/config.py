"""Application configuration and settings."""
import secrets
from pathlib import Path
from typing import Optional

import yaml

from pydantic import Field
from pydantic_settings import BaseSettings


def _default_token_file() -> Path:
    return Path.home() / ".vm-control-center" / "token"


class Settings(BaseSettings):
    """Application settings."""
    
    # Server settings
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    DEBUG: bool = False
    
    # Security
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    TOKEN_FILE: Path = Field(default_factory=_default_token_file)
    
    # Task settings
    MAX_TASK_LOGS: int = 1000

    # Tart binary path
    TART_PATH: str = "tart"

    # Orchard API timeout (seconds)
    ORCHARD_TIMEOUT: int = 30

    # GitHub API settings
    GITHUB_TOKEN: Optional[str] = None
    GITHUB_TOKEN_FILE: Path = Field(default_factory=lambda: Path.home() / ".vm-control-center" / "github_token")

    # Caddy admin API (on ddmini) for dynamic service URL discovery
    CADDY_ADMIN_URL: Optional[str] = None

    # Pipeline controller URL
    PIPELINE_CONTROLLER_URL: str = "http://infra-pipeline:3200"

    # Orchard controller
    ORCHARD_URL: Optional[str] = None
    ORCHARD_USER: Optional[str] = None
    ORCHARD_TOKEN: Optional[str] = None
    
    class Config:
        """Pydantic config."""
        env_file = ".env"
        env_prefix = "VMCC_"


def ensure_token_file(settings: Settings) -> None:
    """Ensure the token file exists with proper permissions."""
    settings.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings.TOKEN_FILE.parent.chmod(0o700)
    except Exception:
        pass
    if not settings.TOKEN_FILE.exists():
        settings.TOKEN_FILE.write_text(settings.SECRET_KEY)
        settings.TOKEN_FILE.chmod(0o600)


def _ensure_token_file_perms(token_file: Path) -> None:
    try:
        mode = token_file.stat().st_mode & 0o777
        if mode != 0o600:
            token_file.chmod(0o600)
    except Exception:
        pass


def _maybe_migrate_legacy_token(legacy_token_file: Path, new_token_file: Path) -> None:
    if new_token_file.exists() or not legacy_token_file.exists():
        return
    try:
        new_token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            new_token_file.parent.chmod(0o700)
        except Exception:
            pass
        new_token_file.write_text(legacy_token_file.read_text().strip())
        new_token_file.chmod(0o600)
    except Exception:
        pass


# Initialize settings
settings = Settings()

# Ensure token file exists
legacy_token_file = Path(".token")
_maybe_migrate_legacy_token(legacy_token_file, settings.TOKEN_FILE)

if not settings.TOKEN_FILE.exists():
    ensure_token_file(settings)
else:
    _ensure_token_file_perms(settings.TOKEN_FILE)
    settings.SECRET_KEY = settings.TOKEN_FILE.read_text().strip()

# Load Orchard config from ~/.orchard/orchard.yml
_orchard_cfg_path = Path.home() / ".orchard" / "orchard.yml"
if _orchard_cfg_path.exists() and not settings.ORCHARD_URL:
    try:
        _orchard_cfg = yaml.safe_load(_orchard_cfg_path.read_text())
        _ctx_name = _orchard_cfg.get("default-context")
        _ctx = (_orchard_cfg.get("contexts") or {}).get(_ctx_name, {})
        if _ctx.get("url"):
            settings.ORCHARD_URL = _ctx["url"]
            settings.ORCHARD_USER = _ctx.get("serviceAccountName")
            settings.ORCHARD_TOKEN = _ctx.get("serviceAccountToken")
    except Exception:
        pass

# Load GitHub token if it exists
if settings.GITHUB_TOKEN_FILE.exists():
    try:
        _ensure_token_file_perms(settings.GITHUB_TOKEN_FILE)
        settings.GITHUB_TOKEN = settings.GITHUB_TOKEN_FILE.read_text().strip()
    except Exception:
        pass

# Auto-detect Caddy admin URL from Orchard controller host
if not settings.CADDY_ADMIN_URL and settings.ORCHARD_URL:
    try:
        from urllib.parse import urlparse
        _orchard_host = urlparse(settings.ORCHARD_URL).hostname
        if _orchard_host:
            settings.CADDY_ADMIN_URL = f"http://{_orchard_host}:2019"
    except Exception:
        pass

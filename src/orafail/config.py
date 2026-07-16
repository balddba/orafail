"""Configuration models for the Oracle login failure monitor."""

from orafail.models.app_config import AppConfig
from orafail.models.database_config import DatabaseConfig

__all__ = ["DatabaseConfig", "AppConfig"]

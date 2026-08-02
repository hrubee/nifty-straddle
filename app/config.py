"""Minimal config for standalone nifty-straddle — reads from environment."""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    # Tradejini OAuth (for client order execution — not needed for data-only mode)
    tradejini_app_key: str = os.getenv("TRADEJINI_APP_KEY", "")
    tradejini_redirect_uri: str = os.getenv("TRADEJINI_REDIRECT_URI", "")
    tradejini_app_secret: str = os.getenv("TRADEJINI_APP_SECRET", "")
    tradejini_base_url: str = os.getenv("TRADEJINI_BASE_URL", "https://api.tradejini.com")

    # Tradejini data account (for premium WS feed)
    tradejini_data_api_key: str = os.getenv("TRADEJINI_DATA_API_KEY", "")
    tradejini_data_password: str = os.getenv("TRADEJINI_PASSWORD", "")  # same as podcast TOTP var
    tradejini_data_totp_secret: str = os.getenv("TRADEJINI_TOTP", "")    # same as podcast TOTP var

    # Market protection % (default 0.5%)
    tradejini_mkt_prot_pct: float = float(os.getenv("TRADEJINI_MKT_PROT_PCT", "0.5"))

    # Straddle product type
    straddle_product: str = os.getenv("STRADDLE_PRODUCT", "normal")


# Module-level instance (mimics aiprosperity's config.settings)
settings = Settings()

# For convenience: allow env override of individual fields
def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


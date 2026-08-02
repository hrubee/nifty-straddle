"""Minimal config stub for the standalone NIFTY straddle repo."""
import os
from dataclasses import dataclass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # Tradejini OAuth - NOT USED in standalone (credentials per-client via env)
    tradejini_base_url: str = _get("TRADEJINI_BASE_URL", "https://api.tradejini.com/v2")
    tradejini_app_key: str = _get("TRADEJINI_APP_KEY", "")
    tradejini_app_secret: str = _get("TRADEJINI_APP_SECRET", "")
    tradejini_redirect_uri: str = _get("TRADEJINI_REDIRECT_URI", "")
    tradejini_mkt_prot_pct: float = float(_get("TRADEJINI_MKT_PROT_PCT", "5"))
    tradejini_data_api_key: str = _get("TRADEJINI_DATA_API_KEY", "")
    tradejini_data_password: str = _get("TRADEJINI_DATA_PASSWORD", "")
    tradejini_data_totp_secret: str = _get("TRADEJINI_DATA_TOTP_SECRET", "")

    # Kite - NOT USED (Tradejini is data source)
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_user_id: str = ""
    kite_password: str = ""
    kite_totp_secret: str = ""
    kite_instrument_token: str = "256265"

    # Straddle settings
    straddle_live: bool = _get("STRADDLE_LIVE", "0") == "1"
    straddle_dry_run: bool = _get("STRADDLE_DRY_RUN", "0") == "1"
    straddle_canary_email: str = _get("STRADDLE_CANARY_EMAIL", "")
    straddle_capital_per_lot_inr: float = float(_get("STRADDLE_CAPITAL_PER_LOT_INR", "25000"))
    straddle_max_lots: int = int(_get("STRADDLE_MAX_LOTS", "500"))
    straddle_daily_loss_cap_inr: float = float(_get("STRADDLE_DAILY_LOSS_CAP_INR", "0"))
    straddle_guardian_enabled: bool = _get("STRADDLE_GUARDIAN_ENABLED", "1") == "1"
    straddle_heartbeat_stale_secs: int = int(_get("STRADDLE_HEARTBEAT_STALE_SECS", "180"))
    straddle_product: str = _get("STRADDLE_PRODUCT", "normal")
    straddle_ioc_buffer_pct: float = float(_get("STRADDLE_IOC_BUFFER_PCT", "0.01"))


settings = Settings()

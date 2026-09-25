"""Settings. Secrets come from /etc/gatekeeper.env (never from the repo).

Every strategy number lives in STRATEGY_DEFAULTS so the live bot and the
backtester always run the exact same rules. Override any of them in the env
file as GK_<NAME>=value (for example GK_STOP_LOSS_PCT=25).
"""
import os
from pathlib import Path

ENV_FILE = Path(os.environ.get("GATEKEEPER_ENV", "/etc/gatekeeper.env"))
DATA_DIR = Path(os.environ.get("GATEKEEPER_DATA", "/var/lib/gatekeeper"))
DB_PATH = DATA_DIR / "gatekeeper.db"
TIMEZONE = "America/Detroit"


def _load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PUMPPORTAL_API_KEY = os.environ.get("PUMPPORTAL_API_KEY", "")  # optional, not needed for free feeds

STRATEGY_DEFAULTS = {
    # --- which coins get watched ---
    "WATCH_HOURS": 24,              # stop recording a coin after this long
    "DEAD_LIQ_USD": 1000,           # stop recording once the pool is this small
    # --- safety gates (hard) ---
    "MIN_LIQ_USD": 15000,
    "MIN_LP_LOCKED_PCT": 90,
    "MAX_TOP10_PCT": 30,
    "MAX_INSIDERS": 10,
    # --- timing ---
    "MIN_AGE_MIN": 20,              # minutes since graduation before any entry
    "MAX_AGE_MIN": 360,
    "MIN_RUNUP_X": 1.5,             # peak must be this multiple of the graduation price
    "PULLBACK_MIN_PCT": 15,         # entry zone: this far below the recent peak...
    "PULLBACK_MAX_PCT": 40,         # ...but not further
    "PEAK_WITHIN_MIN": 45,          # the peak must be recent
    "MIN_M5_TXNS": 10,              # the coin must still be trading
    "LIQ_HOLD_PCT": 80,             # liquidity now vs 10 min ago
    # --- position + exits ---
    "POSITION_USD": 100,
    "MAX_OPEN": 5,
    "TAKE_HALF_X": 2.0,
    "STOP_LOSS_PCT": 30,
    "TRAIL_PCT": 35,                # after taking half, trail the rest this far below its peak
    "LIQ_PULL_PCT": 20,             # exit if liquidity falls this much within 5 min
    "MAX_HOLD_MIN": 360,
    # --- realistic costs ---
    "FEE_PCT": 1.0,                 # swap + platform fees per side
    "PENALTY_PCT": 2.0,             # slower fills and front-running bots, per side
    "PANIC_PENALTY_PCT": 5.0,       # extra cost when exiting into a liquidity pull
    # --- optional top-trader bonus (off until wallet tracking is added) ---
    "SMART_BONUS": 0,
}


def strategy_params(overrides=None):
    p = dict(STRATEGY_DEFAULTS)
    for k, v in STRATEGY_DEFAULTS.items():
        env = os.environ.get("GK_" + k)
        if env is not None and env != "":
            p[k] = type(v)(float(env)) if isinstance(v, (int, float)) else env
    if overrides:
        for k, v in overrides.items():
            k = k.upper()
            if k not in p:
                raise KeyError("Unknown setting: " + k)
            p[k] = type(p[k])(float(v))
    return p

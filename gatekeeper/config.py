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
    "WATCH_HOURS": 12,              # stop recording a coin after this long
    "DEAD_LIQ_USD": 1000,           # stop recording once the pool is this small
    # --- safety gates (hard) ---
    "MIN_LIQ_USD": 15000,
    "MIN_LP_LOCKED_PCT": 90,
    "MAX_TOP10_PCT": 30,
    "MAX_INSIDERS": 10,
    "MAX_DEV_PREV_COINS": 999,      # skip devs who launched more than this many earlier coins (999 = off until tested)
    "MAX_DEV_DEAD_COINS": 999,      # skip devs with more than this many dead earlier coins
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
    # how a strategy decides to buy: "pullback" (its own rules) or "signal" (Fomo clusters)
    "ENTRY_MODE": "pullback",
    # signal strategies: 1 = sell as soon as a trader who got us in sells (tested in /testfollow)
    "SELL_WITH_TRADERS": 0,
    # --- in-trade protection (0 = off; /test compares them) ---
    "LOCK_START_PCT": 0,            # once up this much, a trailing stop starts protecting the gain
    "LOCK_TRAIL_PCT": 20,           # ...sell if price falls this far from its best since entry
    "BREAKEVEN_AT_PCT": 0,          # once up this much, never let it turn into a loss (stop moves to entry + costs)
    "SELL_PRESSURE_EXIT": 0,        # 1 = exit when sellers swamp buyers (2x sells, 15+ sells in 5 min) and price is off its high
    "LIQ_DRAIN_PCT": 0,             # exit if the pool shrinks this much from its biggest size since entry (slow rug)
    "RECHECK_EXIT": 1,              # live only: re-run the safety check every 10 min while holding; exit on new red flags
    # --- moonbag: after taking profit, keep a small slice to catch the rare huge runner ---
    "MOONBAG_PCT": 0,               # % of the original position kept when the rest is sold (0 = off)
    "MOON_TRAIL_PCT": 50,           # the moonbag sells only if price falls this far from its best
    "MOON_MAX_HOURS": 72,           # ...or after this long
    # --- momentum entries (ENTRY_MODE "momentum"): buy strength, not dips ---
    "MOM_MIN_PCT": 15,              # price up at least this much in the last 5 min
    "MOM_MAX_PCT": 150,             # ...but not more (don't buy a vertical candle)
    "MOM_NEAR_HIGH_PCT": 10,        # and within this much of its high (breaking out, not fading)
    "MOM_BUY_RATIO": 1.3,           # buys at least 1.3x sells in the last 5 min
    # --- take-profit and rug-risk exits ---
    "TAKE_PROFIT_PCT": 0,           # sell everything once up this much (0 = off; half-at-2x rules apply instead)
    "RUG_RISK_MIN": 50,             # if the rug-risk score at entry is this high or more...
    "RUG_TP_PCT": 40,               # ...sell everything once up this much (0 = off). Quick profit, then leave
    # --- rug-risk sizing: smaller bets on riskier coins, skip the worst ---
    "RISK_SKIP": 45,                # don't buy if the rug-risk score is this high or more (100 = never skip)
    "RISK_SIZING": 1,               # 1 = bet less on riskier coins (under 20%: full size, 20-35%: 75%, 35-50%: 50%, 50%+: 30%)
    "ADAPTIVE_TARGETS": 1,          # 1 = profit target per trade picked from data for its rug-risk level (updates hourly)
    "INSIDER_SKIP": 2,              # skip coins this many known insider (trench) wallets have bought (0 = off)
    "WIN_BOOST": 0,                 # 1 = bet 1.5x on coins in the win score's top 40% (only once the model is trusted)
    "REENTER_MIN": 0,               # buy a coin again this many minutes after a winning trade in it, if it sets up again (0 = never)
    "REENTER_MAX": 2,               # ...at most this many times per coin
    "WIN_FILTER": 0,                # 1 = only buy coins whose win score is in the top 40% (only once the model is trusted)
    "STOP_CONFIRM_SEC": 0,          # stop loss only fires if price stays below it this long (0 = immediately)
    # --- survivor entries (ENTRY_MODE "survivor"): coins that lived through the dangerous hours ---
    "SURV_MIN_PCT": 5,              # price up at least this much over the last hour (a steady climb)
    "SURV_MAX_PCT": 40,             # ...but not more (not a spike)
    # --- moonshot entries (ENTRY_MODE "moonshot"): young coins with the two early signs /moonshots found ---
    "MOON_NEED_FIRST_DEV": 1,       # 1 = only coins that are the dev's first launch (4.2x more likely to go 10x)
    "MOON_NEED_X": 1,               # 1 = only coins with an X account (2.2x more likely to go 10x)
}


PRESETS = {
    # the strict rules; sends Telegram alerts
    "main": {"TAKE_HALF_X": 1.4, "TAKE_PROFIT_PCT": 30, "RISK_SKIP": 35},   # Oct 4: skip rug risk 35%+ (best total in three tests running)
    #   # Oct 2: sell ALL at +30% (7-day test: 65% wins, +$471, vs 54% and +$448 for half at +40%)
    # looser rules on the same coins; trades silently so it reaches 100 trades faster
    "wide": {
        "MIN_AGE_MIN": 10, "MIN_RUNUP_X": 1.3, "PULLBACK_MIN_PCT": 10, "PULLBACK_MAX_PCT": 45,
        "PEAK_WITHIN_MIN": 60, "MIN_LIQ_USD": 10000, "MAX_TOP10_PCT": 35, "MAX_INSIDERS": 15,
        "MIN_M5_TXNS": 6, "MAX_OPEN": 8,
    },
    # buys when 2+ of your Fomo traders buy the same coin and it passes safety; same exits
    "follow": {"ENTRY_MODE": "signal", "MAX_OPEN": 8, "MIN_LIQ_USD": 10000, "MAX_TOP10_PCT": 35},
    # buys coins 4 to 48 hours old that survived, have a real pool and are climbing steadily; aims for +30%
    "survivor": {
        "ENTRY_MODE": "survivor", "MIN_AGE_MIN": 240, "MAX_AGE_MIN": 2880, "MIN_LIQ_USD": 25000,
        "MAX_TOP10_PCT": 30, "MAX_INSIDERS": 10, "MIN_M5_TXNS": 10, "LIQ_HOLD_PCT": 95, "MAX_OPEN": 8,
        "TAKE_PROFIT_PCT": 30, "TAKE_HALF_X": 99, "STOP_LOSS_PCT": 15, "MAX_HOLD_MIN": 720,
        "LOCK_START_PCT": 20, "LOCK_TRAIL_PCT": 10, "SELL_PRESSURE_EXIT": 1, "LIQ_DRAIN_PCT": 15,
    },
    # small bets on young coins showing the early signs of a 10x-50x runner; sells half at 3x, lets the rest ride
    "moonshot": {
        "ENTRY_MODE": "moonshot", "MIN_AGE_MIN": 20, "MAX_AGE_MIN": 60, "MIN_LIQ_USD": 10000, "POSITION_USD": 20,
        "MAX_TOP10_PCT": 30, "MAX_INSIDERS": 10, "MIN_M5_TXNS": 10, "LIQ_HOLD_PCT": 85, "MAX_OPEN": 15,
        "TAKE_HALF_X": 3.0, "TRAIL_PCT": 50, "STOP_LOSS_PCT": 50, "MAX_HOLD_MIN": 660, "LIQ_PULL_PCT": 35,
        "RUG_TP_PCT": 0, "ADAPTIVE_TARGETS": 0, "RISK_SKIP": 100, "RISK_SIZING": 0,
    },
    # active day-trader style: buys new coins breaking out, takes profit fast, cuts losers fast, keeps a small moonbag
    "momentum": {
        "ENTRY_MODE": "momentum", "MIN_AGE_MIN": 5, "MAX_AGE_MIN": 240, "MIN_LIQ_USD": 12000,
        "MAX_TOP10_PCT": 30, "MAX_INSIDERS": 10, "MIN_M5_TXNS": 25, "LIQ_HOLD_PCT": 90, "MAX_OPEN": 10,
        "TAKE_HALF_X": 1.4, "STOP_LOSS_PCT": 15, "TRAIL_PCT": 15, "MAX_HOLD_MIN": 90,
        "LOCK_START_PCT": 15, "LOCK_TRAIL_PCT": 12, "BREAKEVEN_AT_PCT": 20,
        "SELL_PRESSURE_EXIT": 1, "LIQ_DRAIN_PCT": 12, "MOONBAG_PCT": 20, "MOON_TRAIL_PCT": 40,
    },
}
# Experiments: copies of Main with one exit or filter changed, trading silently on paper so the best version
# shows up in live conditions. Kept apart from Main and Follow: no alerts, own dashboard tab, not in Telegram summaries.
SHADOW = {
    "x_all40": {"TAKE_PROFIT_PCT": 40},       # sell everything at +40%
    "x_trail10": {"LOCK_TRAIL_PCT": 10},      # tighter profit lock (10% trail once up 20%)
    # best of: every change that beat Main in the Oct 5 test at once (Main already has skip 35%+ and sell all at +30%)
    "x_best": {"STOP_LOSS_PCT": 40, "MIN_LIQ_USD": 15000},
    "x_stop40": {"STOP_LOSS_PCT": 40},        # wider stop: fewer shake-outs (best recent half in the Oct 4 test)
    "x_stopwait": {"STOP_CONFIRM_SEC": 180},  # stop only sells if price stays under it for 3 minutes
    "x_age60": {"MIN_AGE_MIN": 60},           # buys from 60 minutes old: more trades, strong recently, failed before
    "x_skip35": {"RISK_SKIP": 35},            # stricter rug skip
    "x_scalp20": {"TAKE_PROFIT_PCT": 20},     # quick scalp: sell everything at +20% (about +13% after costs)
    "x_scalp30": {"TAKE_PROFIT_PCT": 30},     # quick scalp: sell everything at +30%
    "x_insider1": {"INSIDER_SKIP": 1},        # the strict version: skip a coin if even one insider wallet bought it
    "x_noinsider": {"INSIDER_SKIP": 0},
    "x_winscore": {"WIN_FILTER": 1},
    # smart aggressive: every tested winner at once. Same strict safety and rug rules as Main, but it takes more
    # setups (coins up to 24h old, up to 10 open), sells everything fast at +30% with a tight 10% trail, buys a coin
    # again after it pays, and bets 1.5x on the win score's top picks once that model is trusted.
    "x_aggro": {"MAX_AGE_MIN": 1440, "MAX_OPEN": 10, "TAKE_PROFIT_PCT": 30, "LOCK_TRAIL_PCT": 10, "MAX_HOLD_MIN": 240,
                "WIN_BOOST": 1},          # buying a coin again after a win cut profit in the 7-day test, so it is off
    # swing: buys coins that already survived a full day with a big pool and are climbing steadily, then holds for
    # days. No profit target: sells half at 2x and trails the rest, with a wide lock once it is up 30%. Up to 7 days.
    "x_swing": {"ENTRY_MODE": "survivor", "MIN_AGE_MIN": 1440, "MAX_AGE_MIN": 2880, "MIN_LIQ_USD": 50000, "LIQ_HOLD_PCT": 95,
                "TAKE_PROFIT_PCT": 0, "TAKE_HALF_X": 2.0, "TRAIL_PCT": 35, "LOCK_START_PCT": 30, "LOCK_TRAIL_PCT": 30,
                "BREAKEVEN_AT_PCT": 40, "MAX_HOLD_MIN": 10080, "ADAPTIVE_TARGETS": 0,
                "SELL_PRESSURE_EXIT": 0},      # a 5-minute burst of selling is noise on a multi-day hold,
}
SHADOW_PRESETS = [x.strip() for x in os.environ.get("GK_SHADOW_STRATEGIES", "x_age60,x_insider1,x_winscore").split(",") if x.strip() in SHADOW]
ALERT_PRESETS = [x.strip() for x in os.environ.get("GK_ALERT_STRATEGIES", "main,wide,follow").split(",") if x.strip()]
ENABLED_PRESETS = [x.strip() for x in os.environ.get("GK_STRATEGIES", "main,follow,survivor").split(",") if x.strip() in PRESETS]


def strategy_params(overrides=None, preset="main"):
    if preset in SHADOW:
        p = strategy_params(preset="main")    # same as Main, including your config-file settings for Main
        p.update(SHADOW[preset])
        for k, v in (overrides or {}).items():
            p[k.upper()] = type(p[k.upper()])(float(v))
        return p
    p = dict(STRATEGY_DEFAULTS)
    p.update(PRESETS.get(preset, {}))
    for k, v in STRATEGY_DEFAULTS.items():
        env = os.environ.get("GK_" + ("%s_" % preset.upper() if preset != "main" else "") + k)
        if env is not None and env != "":
            p[k] = type(v)(float(env)) if isinstance(v, (int, float)) else env
    if overrides:
        for k, v in overrides.items():
            k = k.upper()
            if k not in p:
                raise KeyError("Unknown setting: " + k)
            p[k] = type(p[k])(float(v))
    return p

# Gatekeeper bot

A paper-trading bot for newly graduated Solana meme coins. It trades fake money by fixed rules, records market history for backtesting, and sends Telegram alerts. **It never connects to a wallet and never places a real trade.**

## What it does

1. **Recorder.** Listens to PumpPortal's free feed for pump.fun graduations. Every coin that graduates gets a price, liquidity and buy/sell snapshot from DexScreener every 30 seconds for 24 hours (or until it dies).
2. **Safety check.** Once a coin is 10+ minutes old and has a real pool, RugCheck is checked for mint and freeze authority, LP lock, top-10 holder share (pool excluded) and insider wallets.
3. **Paper trader.** Buys a fixed fake amount when a coin that passed safety pulls back 15 to 40% from a recent peak after running at least 1.5x, while buyers still outnumber sellers and liquidity is holding. Exits: half at 2x, stop at -30%, trailing stop on the rest, immediate exit if liquidity drops 20% in 5 minutes, 6-hour time limit.
4. **Realistic fills.** Every fake order is filled against the pool's constant-product math (your own trade moves the price), plus 1% fees and a 2% penalty per side for slow fills and front-running bots.
5. **Two strategies side by side.** `main` (the strict rules above, sends alerts) and `wide` (looser: coins 10+ minutes old, 1.3x run-up, 10 to 45% pullback, $10K pool, top 10 up to 35%, up to 15 insiders; trades silently). Both paper trade the same coins so you can compare them daily.
6. **Alerts and stats.** Telegram message on every `main` buy, partial sell and exit. Daily summary at 9pm Eastern with results per strategy and a coin funnel (launched, graduated, died, passed safety, traded), plus a weekly recap on Sundays. Send the bot `/status`, `/today`, `/week` or `/all`.

7. **Fomo tracking** (needs `FOMO_API_KEY` from fomoapi.io, an unofficial service). Listens to the live Fomo trade feed (free) and alerts when 2+ traders on your list buy the same coin within 30 minutes (CLUSTER) or 5+ Fomo traders pile into one coin within 15 minutes (TRENDING). Solana coins get an instant safety check and are added to the recorder. `/fomo` in Telegram shows the last 24 hours by theme; `/scan` runs a full trend scan of your traders' positions (about 10,000 of the 250,000 free monthly credits). The list of traders is `GK_FOMO_TRADERS` in `gatekeeper config`.

## Install (fresh Ubuntu server, as root)

```
curl -fsSL https://raw.githubusercontent.com/cbabs789-ops/gatekeeper-bot/main/install.sh | sudo bash
```

It asks for your Telegram bot token and Helius key. They are stored only in `/etc/gatekeeper.env` on the server.

## Commands

```
gatekeeper status                    feed health and open paper trades
gatekeeper report [--days 7]         paper results
gatekeeper backtest                  replay all recorded history through the rules
gatekeeper backtest --split          tune on the first half, test on the second
gatekeeper backtest --strategy wide  replay using the wide rules
gatekeeper backtest --set STOP_LOSS_PCT=25 --set PULLBACK_MAX_PCT=35
gatekeeper settings                  every rule and its current value
gatekeeper config                    edit keys and rule overrides (GK_NAME=value for main, GK_WIDE_NAME=value for wide,
                                     GK_ALERT_STRATEGIES=main,wide to get alerts from both)
gatekeeper logs | restart | update
```

## Data sources

- PumpPortal free websocket methods (`subscribeNewToken`, `subscribeMigration`)
- DexScreener public API (no key)
- RugCheck public API (no key)
- Helius: stored for the next version, which tracks chosen trader wallets

Not financial advice. Paper results overstate real results.

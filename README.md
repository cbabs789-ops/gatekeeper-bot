# Gatekeeper bot

A paper-trading bot for newly graduated Solana meme coins. It trades fake money by fixed rules, records market history for backtesting, and sends Telegram alerts. **It never connects to a wallet and never places a real trade.**

## What it does

1. **Recorder.** Listens to PumpPortal's free feed for pump.fun graduations. Every coin that graduates gets a price, liquidity and buy/sell snapshot from DexScreener every 30 seconds for 24 hours (or until it dies).
2. **Safety check.** Once a coin is 10+ minutes old and has a real pool, RugCheck is checked for mint and freeze authority, LP lock, top-10 holder share (pool excluded) and insider wallets.
3. **Paper trader.** Buys a fixed fake amount when a coin that passed safety pulls back 15 to 40% from a recent peak after running at least 1.5x, while buyers still outnumber sellers and liquidity is holding. Exits: half at 2x, stop at -30%, trailing stop on the rest, immediate exit if liquidity drops 20% in 5 minutes, 6-hour time limit.
4. **Realistic fills.** Every fake order is filled against the pool's constant-product math (your own trade moves the price), plus 1% fees and a 2% penalty per side for slow fills and front-running bots.
5. **Alerts.** Telegram message on every buy, partial sell and exit, plus a daily summary at 9pm Eastern. Send the bot `/status`, `/today` or `/week`.

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
gatekeeper backtest --set STOP_LOSS_PCT=25 --set PULLBACK_MAX_PCT=35
gatekeeper settings                  every rule and its current value
gatekeeper config                    edit keys and rule overrides (GK_NAME=value)
gatekeeper logs | restart | update
```

## Data sources

- PumpPortal free websocket methods (`subscribeNewToken`, `subscribeMigration`)
- DexScreener public API (no key)
- RugCheck public API (no key)
- Helius: stored for the next version, which tracks chosen trader wallets

Not financial advice. Paper results overstate real results.

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

8. **Robinhood Chain.** Coins your Fomo traders buy on Robinhood Chain get a GoPlus safety check (honeypot, taxes, mint, blacklist, owner tricks, holder concentration; LP lock is skipped for Uniswap v4 pools) and DexScreener price tracking, same as Solana.
9. **Follow strategy.** A third paper strategy that buys when 2+ of your Fomo traders buy the same coin and it passes safety, with the same exits. Compare it against Main and Wide in the daily summary.
10. **Rule test.** `/test` in Telegram (or `gatekeeper sweep`) replays the last 7 days of recorded coins through 15 rule variations in one pass and ranks them, split into first and second half so you can spot rules that only fit the past.
11. **Disk saver.** Coins with pools under $7K are checked every 5 minutes instead of every 30 seconds, coins are watched for 12 hours, and history is kept 14 days. `/status` shows free disk.

12. **Trader scorecard.** Every Fomo buy of $200+ on Solana or Robinhood Chain (any trader, not just your list) is priced when it happens and again 1, 6 and 24 hours later. `/traders` ranks your list by what their coins did after they bought, and shows the best Fomo traders not on your list (10+ scored buys) so you can add them. Free: uses the live feed and DexScreener.
13. **Dev history.** RugCheck's list of the creator's earlier coins is stored with each safety check, with optional gates (`MAX_DEV_PREV_COINS`, `MAX_DEV_DEAD_COINS`) that `/test` evaluates.

14. **Follow test.** `/testfollow` in Telegram (or `gatekeeper followtest`) replays the stored Fomo feed: when would each Follow rule have bought (2+ or 3+ of your traders, any 1 trader, $1K+ buys, waiting 10 or 30 minutes, trending crowds) and how would the same exits have done, split into first and second half. Every coin a trader on your list buys is now price-tracked for 12 hours so this test has data. Runs with the Sunday recap too.

15. **Live dashboard.** The bot serves a read-only web page at `http://<server-ip>:8080/?k=<key>`: profit and loss per strategy with equity curves, open paper trades valued at what they'd sell for right now, every buy and sell as it happens, Fomo alerts and closed trades. Refreshes every 10 seconds. Send `/site` in Telegram for your link. P/L counts from the last time a strategy's rules changed (detected automatically; `/reset main` starts a fresh count by hand, history is kept). Coins with open paper trades are priced every 10 seconds instead of 30. Set `GK_WEB_PORT` or `GK_WEB_KEY` in `gatekeeper config` to change the port or key.

16. **Trader-sold alerts.** When a trader on your list sells a coin you got a cluster or trending alert on (or a paper trade holds), you get a 🟠 message. `SELL_WITH_TRADERS=1` makes a signal strategy exit when a trader who got it in sells; `/testfollow` tests that rule.
17. **Research report.** `/research` (and every Sunday): which coin themes rose after Fomo traders bought, whether coins with an X account, website or paid boost did better, and which safety warnings actually predicted dead coins.
18. **Real vs paper.** After a real trade, `/fill SYMBOL PRICE` (price per coin, or market cap like `250k`) logs your fill next to the paper bot's. `/fills` shows how far real results run from paper.

19. **In-trade protection.** While holding, the bot can lock in gains (trailing stop once up 20 to 30%), refuse to let a 25% gain turn into a loss, exit when sellers swamp buyers, and exit when the pool slowly drains. `/test` compares each one on newer coins. Separately, coins with an open trade get their safety re-checked every 10 minutes, and the bot exits if new red flags appear (mint or freeze turned on, LP unlocked, holders or insiders jumping).

20. **Instant updates.** Every strategy sends a Telegram message the moment it buys, sells half or exits (set `GK_ALERT_STRATEGIES` to limit which). Open dashboards get each trade pushed instantly with a pop-up, instead of waiting for the next refresh.

21. **Live charts and Fomo links.** Each open trade on the dashboard shows its price chart since entry with the entry, stop, take-profit and profit-lock lines and every buy and sell marked. Tap a closed trade to see how it played out. Every coin name, on the dashboard and in Telegram alerts, opens the coin in the Fomo app.

22. **Moonbag.** `MOONBAG_PCT` keeps a slice of a winning trade (after it has taken profit) with no 6-hour limit and a wide 50% trailing stop, to catch the rare coin that runs 10x to 100x. `/test` and `/testfollow` compare it. Recorded data only covers 12 hours per coin, so the test understates multi-day runs.

23. **Momentum strategy (day-trader style).** Buys safe new coins (5 min to 4 hours old) that are breaking out: up 15 to 150% in 5 minutes, within 10% of their high, buyers 1.3x sellers, 25+ trades in 5 minutes, pool $12K+ and holding. Takes half at 1.4x, stops at -15%, locks gains once up 15%, never lets a 20% gain turn into a loss, exits on heavy selling or a draining pool, 90-minute limit, keeps a 20% moonbag. `/test` includes six Momentum variations.

24. **Rug-risk score.** Every coin's vital signs (pool, holders, insiders, LP lock, socials, dev history, buying pressure, momentum) are logged at 30, 60, 120 and 240 minutes old, and the bot tracks whether each one drained within 12 hours. Hourly, it relearns which signs predict a rug and scores every buy ("rug risk 62%"). If a trade's risk is 50%+ it takes a quick profit at +40% and leaves (`RUG_RISK_MIN`, `RUG_TP_PCT`). Rough rule-based estimates until 300 coins have finished. `/risk` shows what it has learned.
25. **Survivor strategy.** Coins that lived 4 to 48 hours with a $25K+ pool, climbing 5 to 40% over the last hour with buyers ahead: sells everything at +30%, stop at -15%, profit lock, 12h limit. Coins with a $25K+ pool keep being watched for up to 48 hours so there's data on them. `/test` includes four Survivor variations.

26. **Smarter exits and rug sizing.** Liquidity exits now compare the pool's depth to what the price move explains (a pool's dollar value shrinks on its own when price dips), so normal dips no longer trigger "liquidity pulled" or "pool draining"; only real removals do. Every buy is scored for rug risk first: 60%+ is skipped (`RISK_SKIP`), and riskier coins get smaller bets (`RISK_SIZING`: full size under 20%, then 75%, 50%, 30%). `STOP_CONFIRM_SEC` lets a stop ride out a brief wick. `/exits` shows, for each exit rule, how often the coin went on to rise after the bot sold versus kept falling.

27. **Data-picked profit targets.** Every hour the bot works out, for each rug-risk band, which profit target (1.2x to 8x) would have made the most across the coins it tracked, counting rugs and stop-outs. Each new trade gets the target for its band ("data target 1.4x"). Turn off with `ADAPTIVE_TARGETS=0`. The bot also now records each coin's lowest point after each checkpoint, so stop levels can be picked from data next.

28. **Trends.** Every 5 minutes the bot pulls Trump's Truth Social posts (trumpstruth.org's public feed), Google News top stories and crypto/meme-coin searches, DexScreener's new coin profiles and boosts, and what Fomo traders piled into over the last 3 hours. It finds the words trending in the news, matches them to coin names, safety-checks the candidates, scores their rug risk, and picks a few worth a look. Dashboard Trends tab (pictures, links, headlines, posts), `/trends` in Telegram, a 📣 alert when Trump posts (with any matching coins), and a 📰 alert for strong picks. The bot's picks are graded 1h, 6h and 24h later like a trader in the scorecard.

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

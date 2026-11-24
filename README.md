# 🎯 Meme Coin Sniper Bot (Solana)

A Solana meme-coin trading bot with a web control panel. It:

- **Finds coins early.** It scans new token profiles, boosted tokens, newly created pools and trending pools every 30 s, using DexScreener and GeckoTerminal.
- **Researches each coin before buying.** It runs a RugCheck safety report (mint/freeze authority, LP, holder risks), then reads the 1-minute chart: EMA 9/21/50, RSI, VWAP, Bollinger bands, volume spikes, and buy/sell pressure.
- **Trades four setups**, each scored 0–100. It buys when the best score reaches your threshold.
  | Strategy | What it looks for |
  |---|---|
  | **Early** | New launch (<90 min) with strong buy/sell ratio, high volume vs. liquidity, rising price. Parabolic candles are penalised so it doesn't chase. |
  | **Momentum** | Price above EMA9 above EMA21, RSI 55–78, rising volume, mostly green candles, above VWAP. |
  | **Dip** | Pullback of 12–40% inside an uptrend. RSI was oversold and is now turning up, with a reversal candle or a lower-Bollinger reclaim. Moves deeper than 50% are rejected as possible rugs. |
  | **Breakout** | Close above the 20-bar high on 2× volume, ideally after a Bollinger squeeze. |
- **Sells on its own.** Exits are:
  - a **10% stop loss** (adjustable)
  - two take-profit levels: sell half at +40%, the rest at +100%
  - a trailing stop, plus a break-even stop after the first take-profit
  - a maximum hold time
  - chart exits: dump detection, an EMA trend break, or RSI exhaustion
- **Trades in USD or SOL amounts.** You set the size per trade, a cap on total capital deployed, and a maximum number of open positions.
- **Has a risk kill switch.** It halts all new buys when any of these happens: the daily loss limit is hit, equity falls a set % from its peak, or it has N losing trades in a row. You get a push notification when it trips. Trading stays off until you press **Reset** in the dashboard or send `/resume` on Telegram. Open positions keep their stop losses the whole time.
- **Has a web control panel** that you can install on your phone. Use it to:
  - turn the bot on or off, or panic-sell everything
  - see wins and losses, win rate, P&L, an equity curve, open positions, and the full trade history
  - see the "radar" of every coin it researched and why
  - paste any token mint to have the bot investigate it, or buy it manually
  - change every setting live
- **Sends notifications** to Telegram, Discord, and ntfy (phone push) for every buy, sell, win or loss, error, and risk trip. On Telegram you can also control it with `/status /on /off /resume /panic`.

> ⚠️ **Read this first.** Meme coins are extremely risky. Most go to zero, and many are scams. No bot can guarantee profits. A 10% stop loss can fill much worse than −10% when price gaps down or liquidity disappears. The bot starts in **paper mode** (fake money) with trading **OFF**. Run it in paper mode for days and study the results before you consider live mode. In live mode, use a **dedicated hot wallet** holding only what you can afford to lose.

---

## Quick start

```bash
git clone <this repo> && cd Meme-coin-sniperbot-
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env and set DASHBOARD_TOKEN
python main.py
```

Open **http://localhost:8000** and sign in with your `DASHBOARD_TOKEN`. Press **Turn ON** to start paper trading.

### Use it from your phone ("app")
1. Run the bot on an always-on machine, such as a $5/month VPS (Ubuntu) or a spare PC.
2. Open `http://<server-ip>:8000` on your phone. For anything beyond your home network, put it behind HTTPS, for example with a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) or Caddy.
3. Add it to your home screen: **Share → Add to Home Screen** on iPhone, or **⋮ → Install app** on Android. It opens full-screen like a native app.

### Notifications (pick any)
| Channel | Setup |
|---|---|
| **ntfy** (easiest phone push) | Install the ntfy app, subscribe to a hard-to-guess topic name, and put that name in `NTFY_TOPIC`. Risk trips arrive as *urgent*. |
| **Telegram** (push + remote control) | Create a bot with @BotFather and set `TELEGRAM_BOT_TOKEN`. Message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id and set `TELEGRAM_CHAT_ID`. The bot only obeys commands from that chat. |
| **Discord** | Server settings → Integrations → Webhooks → copy the URL into `DISCORD_WEBHOOK_URL`. |

Use the **Send test** button in the dashboard to check that notifications work.

### Run it unattended on a Mac (auto-restart)
`deploy/com.sniperbot.paper.plist` and `deploy/com.sniperbot.recorder.plist` are launchd templates that restart the
bot/recorder if they crash and keep the Mac awake. Edit the paths inside, copy them to `~/Library/LaunchAgents/` and
`launchctl load` them. Set `HEARTBEAT_URL` (e.g. a free healthchecks.io check) to get alerted if the bot stops.

### Market recorder (research data)
`python -m sniperbot.recorder --db market-data.db` records every coin the scanner can see (not just the ones the bot
buys) and follows each for 6 hours. It never trades. `python research/universe_study.py market-data.db` turns that
into base rates and forward outcomes. Research results are logged in `research/RESULTS.md`.

### Going live (real money)
1. Create a **new** Solana wallet just for the bot and fund it with a small amount of SOL.
2. In `.env`, set `MODE=live`, `LIVE_TRADING_CONFIRM=I_ACCEPT_REAL_FUNDS_RISK`, hard caps `LIVE_MAX_TRADE_SOL` and
   `LIVE_MAX_DEPLOYED_SOL` (the bot refuses to start live without all of them), set `SOLANA_PRIVATE_KEY` to that wallet's base58 key (or its JSON byte array), and set a **paid RPC** in `SOLANA_RPC_URL`, such as Helius, QuickNode, or Triton. The public RPC is rate-limited and slow.
3. Restart. The header shows a red **LIVE** badge. Swaps go through the Jupiter aggregator and are signed locally. Your key never leaves the machine.

---

## How a trade happens

```
discover (DexScreener + GeckoTerminal feeds)
  → filter: liquidity, 5m volume, pair age, buy/sell ratio, FDV, not already held / traded in last 2h
  → rank by activity, deep-research the top 12:
        RugCheck safety → 1m candles → score early / momentum / dip / breakout
  → score ≥ min_entry_score and under capital/position limits → BUY
monitor every 5s:
  stop loss → take profits → trailing → max hold → chart exit signals → SELL
  equity check → risk kill switch → halt + notify
```

## Settings (all editable live in the dashboard)

| Group | Key settings (defaults) |
|---|---|
| Size | `trade_size` 25, `trade_unit` USD/SOL, `max_capital_usd` 250, `max_open_positions` 5 |
| Exits | `stop_loss_pct` **10**, `take_profit_pct` 40 (sells half), `take_profit_2_pct` 100, `trailing_stop_pct` 15, `max_hold_minutes` 240 |
| Kill switch | `max_daily_loss_usd` 75, `max_drawdown_pct` 25, `max_consecutive_losses` 5 |
| Entry | `min_entry_score` 65, strategy toggles, `require_safety_check` on |
| Filters | min liquidity $15k, min 5m volume $2k, pair age 3 min–72 h, buy/sell ≥ 1.1 |

Turning the bot **OFF** stops new buys. Open positions stay protected by their stop loss and take-profits. Use **Panic sell all** to exit everything.

## Project layout

```
main.py                       start bot + web server
sniperbot/config.py           settings & secrets
sniperbot/data/market.py      DexScreener / GeckoTerminal / RugCheck / Jupiter price clients
sniperbot/analysis/           indicators + strategy scoring + exit signals
sniperbot/trading/broker.py   PaperBroker (simulated) and JupiterBroker (live swaps)
sniperbot/risk.py             stop loss / TP / trailing + kill switch
sniperbot/engine.py           scan & monitor loops
sniperbot/notify.py           Telegram / Discord / ntfy + Telegram commands
sniperbot/api.py              REST API (token-protected)
sniperbot/web/index.html      dashboard
tests/                        pytest suite (`python -m pytest`)
```

## REST API
Every request needs the header `Authorization: Bearer <DASHBOARD_TOKEN>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Everything the dashboard shows |
| POST | `/api/toggle` `{"enabled": true}` | Turn trading on/off (refused while risk-locked) |
| POST | `/api/settings` `{...}` | Update settings |
| POST | `/api/risk/reset` | Clear the kill switch |
| POST | `/api/positions/{id}/sell` `{"fraction": 0.5}` | Manual sell |
| POST | `/api/sell-all` | Turn off and sell everything |
| POST | `/api/investigate` `{"mint": "..."}` | Research any token |
| POST | `/api/buy` `{"mint": "..."}` | Manual buy |
| POST | `/api/test-notification` | Send a test notification |

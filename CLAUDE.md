# CLAUDE.md: ZebPay Rule Bot handoff

Context for Claude Code picking up this project. Read this first, then `README.md` (user-facing setup) and `config.yaml` (all tunable rules).

## What this is

A personal learning project: a rule-based crypto trading bot for **ZebPay** (Indian exchange), trading BTC/INR and ETH/INR. Owner is Aman, a product manager learning crypto and building this partly as a portfolio/LinkedIn project. Budget is tiny (about Rs 1,000 "fun money"), so the priority is **correct, safe behaviour and honest paper results**, not returns.

Current stage: **paper trading only**. It has never placed a live order. The live code path exists but is untested against the real ZebPay API.

## Hard rules (do not break these)

1. **Paper by default.** `config.yaml` ships with `mode: paper`. Never flip it to live, and never add an API key anywhere, unless Aman explicitly asks in the session.
2. **The GitHub Actions workflow must stay paper-only.** It runs `bot.py --once --paper-only`. Never add ZebPay API secrets to the repo or the workflow.
3. **Never commit secrets.** `.env` is gitignored. Telegram token and chat id live in GitHub Actions secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
4. **API keys, if ever created, are trading-only.** Docs and code must never suggest enabling withdrawal permission.
5. **The bot only sells what it bought.** Positions are tracked in state; never sell from the full exchange balance (Aman holds manual BTC/ETH on the same account).
6. **Run `python simulate.py` after any logic change.** All scenarios must pass (currently 7/7).
7. **Writing style for anything user-facing (README, Telegram messages, STATUS.md): no em dashes.** Aman has asked for this. Plain, simple wording; he is not a full-time developer.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Everything: config loading, state, brokers, rules, Telegram commands, report, CLI |
| `telegram.py` | Tiny stdlib-only Telegram client (`send`, `read_commands` via `getUpdates` polling). Ignores any chat id that isn't the owner's |
| `config.yaml` | All rules and limits. Change numbers here, not in code |
| `simulate.py` | Offline test harness: scripted price paths, safety-brake checks, catch-up check, fake-Telegram session |
| `.github/workflows/paper-bot.yml` | Runs one paper check every 30 min (`13,43 * * * *`), commits `STATUS.md`, `trades.csv`, `state_paper.json`, `bot.log` back to the repo |
| `README.md` | User-facing setup guide (laptop, cloud, Telegram, going live) |

Generated at runtime (gitignored locally, force-added by the workflow in the cloud): `state_<mode>.json`, `trades.csv`, `bot.log`, `STATUS.md`.

## Architecture in `bot.py`

- **`State`**: JSON-persisted memory (`state_paper.json` / `state_live.json`): cash (paper), positions, hourly price history per pair, cooldowns, daily counters, realised gains/losses, `paused`, `pending` approvals, Telegram `/set` `overrides`, `tg_offset`. Saved atomically after every `step`.
- **Brokers** share an interface: `price`, `recent_prices`, `buy(pair, inr)`, `sell(pair, qty, at_price=None)`.
  - `PaperBroker`: simulates fills at the given price, charges `fee_pct` per side and 1% TDS on sells. Has `candles_since` for catch-up.
  - `LiveBroker`: ccxt `zebpay`, market orders, checks min order limits, sells `min(qty, free balance)`. `at_price` ignored.
  - `LivePrices`: public read-only ZebPay prices (no key) used by paper mode.
- **Pure rule functions** (easy to test): `decide_exit` (stop, target, trailing), `decide_exit_in_candle` (paper catch-up, checks the worst case first), `entry_blocker` (returns the reason a buy is blocked, or `None`).
- **`Bot.step(now)`**: roll the day (sends daily summary), handle Telegram commands, expire pending approvals, then for each pair: catch-up (paper) -> exit check -> approval / entry logic -> buy. Every buy and sell is appended to `trades.csv` and sent to Telegram.
- **`write_report`**: builds `STATUS.md` (phone dashboard on GitHub).
- **CLI**: `--once`, `--status`, `--sell-all`, `--paper-only`, `--report`, `--yes` (skip the live confirmation prompt).

## Strategy as configured

Buy Rs 200 when price is 3% below its 24h high. Exits: BTC stop 5% / target 10% / trail from +5% with 3% gap; ETH stop 7% / target 14% / trail from +7% with 4% gap. The trailing stop never drops below the entry price. Brakes: max 2 open positions, max Rs 600 invested, max 3 buys/day, Rs 100 daily loss limit, 6h cooldown after a stop loss. Optional Telegram approval mode (`require_approval`, 60 min expiry, cancelled if price drifts over 1.5%).

## Key decisions and why

- **ccxt** rather than ZebPay's raw API: ccxt supports ZebPay spot (BTC/INR, ETH/INR) with signing handled, and makes swapping exchanges easy.
- **Bot-managed exits, not exchange orders**: ZebPay doesn't appear to support OCO, so a resting take-profit and stop can't coexist on the same coins. Trade-off: exits only fire while the bot runs. README tells the user to keep a manual stop-limit in the app as backup for live money.
- **Catch-up from 5m candles** exists because GitHub cron runs are late or skipped; without it paper results would miss stops touched between runs and look better than reality.
- **TDS is not counted as a loss** in P&L (it's a tax credit). Realised losses are tracked separately because Indian rules don't allow offsetting them against gains. Tax estimate = gains x 30% x 1.04 cess.
- **Telegram over WhatsApp**: WhatsApp needs Meta's paid business API; Telegram is free and simple. Polling (`getUpdates`) instead of a webhook, since the cloud runner isn't a server.
- **Private repo, every 30 min**: GitHub Free gives 2,000 Actions minutes/month for private repos; 30 min cadence uses about 1,440. Don't increase frequency.

## Open items / next steps

1. **Verify ZebPay is reachable from GitHub's US runners.** Run the workflow manually and read `bot.log`. If every run logs `price check failed`, ZebPay is blocking the runners: move to an Oracle Cloud Always Free VM or a cheap VPS with a systemd service.
2. **Look up real ZebPay fees** and set `fee_pct` (0.25 is a placeholder).
3. **Check ZebPay minimum order sizes** for BTC/INR and ETH/INR. Rs 200 trades may be too small; `LiveBroker._check_min` reads ccxt market limits but this is unverified.
4. **Live path is untested.** Before any live use: test `fetch_ohlcv`, `create_order` market buy semantics (ccxt may expect base quantity vs quote cost on ZebPay), `fetch_order` fill fields, and balance keys with a real key in read-only first.
5. **Possible features Aman may ask for:** a server deploy (systemd unit + setup guide), weekly DCA rule alongside the dip rule, ATR-based stops, a small web dashboard from `trades.csv`, comparing strategy variants in paper mode.

## Commands

```bash
pip install -r requirements.txt
python simulate.py                 # offline tests, must print 7/7 scenarios passed
python bot.py --once --report      # one paper check on live prices + STATUS.md
python bot.py --status             # positions, P&L, tax estimate
python bot.py                      # loop forever (checks every 60s)
touch STOP                         # kill switch, bot exits on next check
```

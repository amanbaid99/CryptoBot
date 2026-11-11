# ZebPay Rule Bot

A small Python bot that trades BTC and ETH on ZebPay using fixed rules you set in `config.yaml`. It starts in **paper mode** (pretend trades at real prices) so you can watch it before any real money moves.

## What it does

| Rule | Default |
|---|---|
| **Buy** when price is 3% below its 24 hour high | ₹200 per trade |
| **Stop loss** | BTC 5%, ETH 7% below buy price |
| **Take profit** | BTC 10%, ETH 14% above buy price |
| **Trailing stop** | once up 5% (BTC) / 7% (ETH), sells if price drops 3% / 4% from its peak, never below the buy price |

Safety brakes (any one of these blocks a buy):
- Max 2 open trades, max ₹600 invested at once
- Max 3 buys a day
- Stops buying for the day after losing ₹100
- Waits 6 hours before re-buying a coin that just hit its stop loss

The bot only sells coins **it** bought. Anything you bought yourself in the app is left alone.

## Setup (once)

You need Python 3.9 or newer.

```bash
cd zebpay-bot
pip install -r requirements.txt
```

## Step 1: Check the rules work (no internet needed)

```bash
python simulate.py
```

Runs the bot through scripted price moves (a rally, a crash, a peak and fall) and checks it buys, stops out, takes profit and trails correctly. You should see `7/7 scenarios passed`.

## Step 2: Paper trade on live prices (1 to 2 weeks)

Paper mode reads ZebPay's public prices. No API key needed.

```bash
python bot.py
```

Leave it running (it checks every 60 seconds). In another terminal:

```bash
python bot.py --status
```

Everything is logged:
- `trades.csv`: every buy and sell, with fees, TDS, P&L and the reason
- `bot.log`: everything the bot did
- `state_paper.json`: open positions and history (lets it resume after a restart)

Note: the dip rule needs about 12 hours of price history before it trusts the "24h high". It tries to backfill this from ZebPay's hourly candles on first run.

## Run paper trading in the cloud (no laptop needed)

The repo includes a GitHub Actions workflow (`.github/workflows/paper-bot.yml`) that runs the bot in **paper mode** every 30 minutes on GitHub's servers. It never uses an API key, so it cannot place real orders.

**Setup (one time, about 10 minutes, easiest on a laptop):**
1. Sign in at github.com, tap **+ > New repository**. Name it `zebpay-bot`, choose **Private**, create it.
2. On the new repo page, click **uploading an existing file**, then drag in **everything inside** the unzipped `zebpay-bot` folder (including the `.github` folder). Commit.
   - If the `.github` folder didn't upload (some browsers skip hidden folders): **Add file > Create new file**, type `.github/workflows/paper-bot.yml` as the name, paste that file's contents, commit.
3. Go to the **Actions** tab, enable workflows if asked, open **Paper trading bot**, tap **Run workflow** once to test.
4. When it finishes (about a minute), open `STATUS.md` in the repo. Bookmark it on your phone: that's your dashboard.

**How it behaves:**
- Each run checks prices, replays the 5 minute price candles since the last run (so a stop or target touched while it wasn't running still counts), then saves `STATUS.md`, `trades.csv` and its memory back into the repo.
- GitHub's scheduled runs can start 5 to 20 minutes late and are occasionally skipped. Fine for paper trading, not good enough for real stop losses.
- A private repo gets 2,000 free Actions minutes a month. Every 30 minutes uses about 1,440, so stay at 30 minutes or slower.
- GitHub's servers are in the US. If the log shows "price check failed" on every run, ZebPay is blocking them and you'd need a small server instead (an Oracle free VM or a cheap VPS).

## Telegram alerts and commands (optional)

The bot can message you on every buy, sell and daily summary, and take commands back.

**Setup:**
1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. Copy the **token** it gives you.
2. Open your new bot and send it any message (e.g. `hi`).
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (paste your token). Find `"chat":{"id":123456789` and copy that number: it's your **chat id**.
4. In your GitHub repo: **Settings > Secrets and variables > Actions > New repository secret**. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   (For laptop runs, put the same two lines in `.env`.)

**Commands you can send:**

| Command | What it does |
|---|---|
| `/status` | open trades, cash and P&L |
| `/pause` / `/resume` | stop or allow new buys (stops and targets keep working) |
| `/set approval on` | the bot asks before every buy: reply `/yes` or `/no` |
| `/set dip 2.5` | buy when price is 2.5% below the recent high |
| `/set amount 300` | INR per trade |
| `/sellall` | close everything the bot holds |
| `/help` | list of commands |

The bot only obeys your chat id; messages from anyone else are ignored. In the cloud it reads your commands on its next run, so replies take up to 30 minutes. A buy signal you approve is cancelled if the price moved more than 1.5% by then, or if you don't reply within an hour.

## Step 3: Going live (only if paper results look good)

1. **Create an API key** at https://api.zebpay.com/ (log in, API Trading, Create New API Key).
   - Permissions: **trading only. Never enable withdrawals.**
   - Turn on the **IP whitelist** with your home IP if offered.
   - The secret is shown once. Save it.
2. Copy `.env.example` to `.env` and paste your key and secret. Never share or upload this file.
3. Check ZebPay's fee page and update `fee_pct` in `config.yaml`.
4. In `config.yaml`, change `mode: paper` to `mode: live`.
5. Make sure your ZebPay wallet has the INR the bot needs (at least `max_total_invested_inr`).
6. Run `python bot.py` and type `LIVE` when asked.

## Stopping

- `Ctrl+C`, or create an empty file called `STOP` in this folder. Open trades are saved and resume next time.
- `python bot.py --sell-all` sells everything the bot is holding right now.

## Important limits

- **Exits only work while the bot is running.** If your laptop sleeps, no stop loss fires. For real money, either keep it running on an always-on machine, or also place a stop-limit order in the ZebPay app as a backup.
- Uses market orders, so the fill price can be slightly different from the price it checked.
- ZebPay has minimum order sizes. If a trade is too small, the bot logs it and skips.
- **Tax:** every sell has 1% TDS (claimable in your ITR), gains are taxed at 30% + cess, and losses cannot be offset. `--status` shows an estimate. Download ZebPay's tax report at year end.
- This is a learning tool. Rules that look good in a simulation can lose money in a real market.

## Ideas to try next

- Change `dip_from_high_pct` and `lookback_hours` and compare paper results
- Add a weekly DCA rule alongside the dip rule
- Build a small dashboard from `trades.csv`

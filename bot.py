"""
ZebPay rule-based trading bot.

Rules (all set in config.yaml):
  BUY  when price is X% below its recent high, if every safety brake allows it.
  SELL when the stop loss, take profit or trailing stop is hit.

Run:
  python bot.py              run the bot (paper mode unless config says live)
  python bot.py --once       run one check and exit
  python bot.py --status     show open trades, P&L and tax estimate
  python bot.py --sell-all   close every position the bot opened
Kill switch: create a file named STOP in this folder and the bot exits on its next check.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta

import yaml

from telegram import Telegram

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES_CSV = os.path.join(HERE, "trades.csv")
LOG_FILE = os.path.join(HERE, "bot.log")
STOP_FILE = os.path.join(HERE, "STOP")


# ---------------------------------------------------------------- utilities

def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def inr(x):
    return f"Rs {x:,.2f}"


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if cfg["mode"] not in ("paper", "live"):
        sys.exit("config.yaml: mode must be 'paper' or 'live'")
    for p in cfg["pairs"]:
        if p not in cfg["exits"]:
            sys.exit(f"config.yaml: no exit rules for {p}")
    return cfg


# ---------------------------------------------------------------- state

class State:
    """Everything the bot remembers between runs, saved to state_<mode>.json."""

    def __init__(self, path, starting_cash):
        self.path = path
        self.data = {
            "cash": starting_cash,          # used in paper mode only
            "positions": {},                # pair -> position dict
            "history": {},                  # pair -> [[timestamp, price], ...]
            "cooldown_until": {},           # pair -> timestamp
            "day": "",
            "buys_today": 0,
            "pnl_today": 0.0,
            "realized_gains": 0.0,          # sum of profitable trades (taxable)
            "realized_losses": 0.0,         # sum of losing trades (cannot offset in India)
            "paused": False,                # set by /pause: no new buys, exits still run
            "pending": {},                  # pair -> buy signal waiting for your /yes
            "overrides": {},                # settings changed from Telegram with /set
            "tg_offset": 0,
        }
        if os.path.exists(path):
            with open(path) as f:
                self.data.update(json.load(f))

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def roll_day(self, now):
        """Start a new day. Returns yesterday's (buys, pnl) if the day changed, else None."""
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.data["day"] != today:
            prev = (self.data["buys_today"], self.data["pnl_today"]) if self.data["day"] else None
            self.data.update(day=today, buys_today=0, pnl_today=0.0)
            return prev
        return None


# ---------------------------------------------------------------- brokers

class PaperBroker:
    """Pretends to trade at the live price, charging fee and TDS like the real thing."""

    def __init__(self, cfg, state, price_source):
        self.fee = cfg["safety"]["fee_pct"] / 100
        self.tds = cfg["safety"]["tds_pct"] / 100
        self.state = state
        self.prices = price_source

    def price(self, pair):
        return self.prices.price(pair)

    def recent_prices(self, pair, hours):
        return self.prices.recent_prices(pair, hours)

    def candles_since(self, pair, since_ts):
        return self.prices.candles_since(pair, since_ts)

    def buy(self, pair, inr_amount):
        if self.state.data["cash"] < inr_amount:
            raise RuntimeError("not enough paper cash")
        price = self.price(pair)
        fee = inr_amount * self.fee
        qty = (inr_amount - fee) / price
        self.state.data["cash"] -= inr_amount
        return qty, price, fee

    def sell(self, pair, qty, at_price=None):
        price = at_price or self.price(pair)
        gross = qty * price
        fee = gross * self.fee
        tds = gross * self.tds
        self.state.data["cash"] += gross - fee - tds
        return gross, price, fee, tds


class LiveBroker:
    """Real orders on ZebPay through the ccxt library."""

    def __init__(self, cfg):
        import ccxt
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, ".env"))
        key, secret = os.getenv("ZEBPAY_API_KEY"), os.getenv("ZEBPAY_API_SECRET")
        if not key or not secret:
            sys.exit("Live mode needs ZEBPAY_API_KEY and ZEBPAY_API_SECRET in a .env file.")
        self.ex = ccxt.zebpay({"apiKey": key, "secret": secret, "enableRateLimit": True})
        self.ex.load_markets()
        self.fee = cfg["safety"]["fee_pct"] / 100
        self.tds = cfg["safety"]["tds_pct"] / 100

    def price(self, pair):
        return float(self.ex.fetch_ticker(pair)["last"])

    def recent_prices(self, pair, hours):
        candles = self.ex.fetch_ohlcv(pair, "1h", limit=max(int(hours), 1))
        return [[c[0] / 1000, c[2]] for c in candles]   # use each hour's high

    def _check_min(self, pair, qty, price):
        lim = self.ex.markets[pair]["limits"]
        min_amt = (lim.get("amount") or {}).get("min")
        min_cost = (lim.get("cost") or {}).get("min")
        if min_amt and qty < min_amt:
            raise RuntimeError(f"{pair}: quantity {qty} below ZebPay minimum {min_amt}")
        if min_cost and qty * price < min_cost:
            raise RuntimeError(f"{pair}: order value below ZebPay minimum {min_cost}")

    def _filled(self, order, fallback_price):
        try:
            o = self.ex.fetch_order(order["id"], order["symbol"])
            return float(o.get("filled") or 0), float(o.get("average") or fallback_price)
        except Exception:
            return float(order.get("filled") or order.get("amount") or 0), fallback_price

    def buy(self, pair, inr_amount):
        price = self.price(pair)
        qty = float(self.ex.amount_to_precision(pair, inr_amount / price))
        self._check_min(pair, qty, price)
        order = self.ex.create_order(pair, "market", "buy", qty)
        time.sleep(2)
        filled, avg = self._filled(order, price)
        if filled <= 0:
            raise RuntimeError(f"buy order {order.get('id')} did not fill")
        return filled, avg, filled * avg * self.fee

    def sell(self, pair, qty, at_price=None):   # at_price is ignored: live sells at market
        price = self.price(pair)
        free = float(self.ex.fetch_balance()["free"].get(pair.split("/")[0], 0))
        qty = float(self.ex.amount_to_precision(pair, min(qty, free)))
        self._check_min(pair, qty, price)
        order = self.ex.create_order(pair, "market", "sell", qty)
        time.sleep(2)
        filled, avg = self._filled(order, price)
        gross = filled * avg
        return gross, avg, gross * self.fee, gross * self.tds


class LivePrices:
    """Read-only public prices from ZebPay (no API key needed). Used by paper mode."""

    def __init__(self):
        import ccxt
        self.ex = ccxt.zebpay({"enableRateLimit": True})

    def price(self, pair):
        return float(self.ex.fetch_ticker(pair)["last"])

    def recent_prices(self, pair, hours):
        candles = self.ex.fetch_ohlcv(pair, "1h", limit=max(int(hours), 1))
        return [[c[0] / 1000, c[2]] for c in candles]

    def candles_since(self, pair, since_ts):
        """5-minute candles [ts, open, high, low, close] since a time, oldest first."""
        candles = self.ex.fetch_ohlcv(pair, "5m", since=int(since_ts * 1000), limit=500)
        return [[c[0] / 1000, c[1], c[2], c[3], c[4]] for c in candles]


# ---------------------------------------------------------------- the rules

def decide_exit(pos, price, rules):
    """Return a sell reason, or None to keep holding. Also updates the trailing high."""
    entry = pos["entry_price"]
    pos["highest"] = max(pos["highest"], price)
    change = (price / entry - 1) * 100

    if change <= -rules["stop_loss_pct"]:
        return f"stop loss ({change:+.2f}%)"
    if change >= rules["take_profit_pct"]:
        return f"take profit ({change:+.2f}%)"
    peak_gain = (pos["highest"] / entry - 1) * 100
    if peak_gain >= rules["trail_start_pct"]:
        trail_price = max(pos["highest"] * (1 - rules["trail_gap_pct"] / 100), entry)
        if price <= trail_price:
            return f"trailing stop ({change:+.2f}%, peak {peak_gain:+.2f}%)"
    return None


def decide_exit_in_candle(pos, high, low, rules):
    """Paper mode only: did a stop, trailing stop or target get hit inside this candle?
    Returns (reason, exit_price) or None. Checks the worst case (stops) first."""
    entry = pos["entry_price"]
    stop = entry * (1 - rules["stop_loss_pct"] / 100)
    target = entry * (1 + rules["take_profit_pct"] / 100)
    if low <= stop:
        return f"stop loss between checks ({-rules['stop_loss_pct']:+.2f}%)", stop
    peak_gain = (pos["highest"] / entry - 1) * 100
    if peak_gain >= rules["trail_start_pct"]:
        trail = max(pos["highest"] * (1 - rules["trail_gap_pct"] / 100), entry)
        if low <= trail:
            return f"trailing stop between checks ({(trail / entry - 1) * 100:+.2f}%)", trail
    if high >= target:
        return f"take profit between checks ({rules['take_profit_pct']:+.2f}%)", target
    pos["highest"] = max(pos["highest"], high)
    return None


def entry_blocker(pair, price, recent_high, state, cfg, now):
    """Return why we must NOT buy, or None if buying is allowed."""
    s, d = cfg["safety"], state.data
    if pair in d["positions"]:
        return "already holding"
    if d["cooldown_until"].get(pair, 0) > now:
        return "cooling down after a stop loss"
    if len(d["positions"]) >= cfg["max_open_positions"]:
        return "max open positions reached"
    invested = sum(p["cost_inr"] for p in d["positions"].values())
    if invested + cfg["amount_per_trade_inr"] > cfg["max_total_invested_inr"]:
        return "max total invested reached"
    if d["buys_today"] >= s["max_buys_per_day"]:
        return "max buys per day reached"
    if d["pnl_today"] <= -s["daily_loss_limit_inr"]:
        return "daily loss limit hit"
    if recent_high is None:
        return "not enough price history yet"
    dip = (1 - price / recent_high) * 100
    if dip < cfg["entry"]["dip_from_high_pct"]:
        return f"no dip yet ({dip:.2f}% below high, need {cfg['entry']['dip_from_high_pct']}%)"
    return None


# ---------------------------------------------------------------- bot

OVERRIDABLE = {   # what /set can change from Telegram, with safe ranges
    "dip":    (("entry", "dip_from_high_pct"), 0.5, 20),
    "amount": (("amount_per_trade_inr",), 100, 5000),
}


def apply_overrides(cfg, overrides):
    for key, value in overrides.items():
        if key == "approval":
            cfg["require_approval"] = bool(value)
            continue
        path = OVERRIDABLE[key][0]
        node = cfg
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = value


class Bot:
    def __init__(self, cfg, broker, state, mode, tg=None):
        self.cfg, self.broker, self.state, self.mode = cfg, broker, state, mode
        self.tg = tg or Telegram()
        apply_overrides(self.cfg, self.state.data.get("overrides", {}))

    def notify(self, text):
        tag = "PAPER" if self.mode == "paper" else "LIVE"
        self.tg.send(f"[{tag}] {text}")

    # ------------------------------------------------ telegram commands
    HELP = ("Commands:\n"
            "/status  open trades and P&L\n"
            "/pause   stop new buys (stops and targets still work)\n"
            "/resume  allow buys again\n"
            "/yes     approve waiting buy signals (/yes BTC for one)\n"
            "/no      reject waiting buy signals\n"
            "/sellall close every bot trade now\n"
            "/set dip 2.5     buy when 2.5% below the recent high\n"
            "/set amount 300  INR per trade\n"
            "/set approval on|off  ask you before every buy")

    def handle_commands(self, now):
        d = self.state.data
        cmds, d["tg_offset"] = self.tg.read_commands(d.get("tg_offset", 0))
        for raw in cmds:
            parts = raw.split()
            cmd, args = parts[0].lower().split("@")[0], parts[1:]
            try:
                reply = self.run_command(cmd, args, now)
            except Exception as e:
                reply = f"Could not do that: {e}"
            if reply:
                self.notify(reply)

    def run_command(self, cmd, args, now):
        d = self.state.data
        if cmd in ("/help", "/start"):
            return self.HELP
        if cmd == "/status":
            return self.status_text()
        if cmd == "/pause":
            d["paused"] = True
            return "Paused. No new buys. Stop losses and targets still run."
        if cmd == "/resume":
            d["paused"] = False
            return "Resumed. Buying is allowed again."
        if cmd == "/sellall":
            if not d["positions"]:
                return "Nothing to sell."
            for pair in list(d["positions"]):
                self.close(pair, "sell-all from Telegram", now)
            return "Sold everything the bot was holding."
        if cmd in ("/yes", "/no"):
            targets = [p for p in d["pending"] if not args or p.startswith(args[0].upper())]
            if not targets:
                return "No buy signals are waiting."
            for p in targets:
                if cmd == "/yes":
                    d["pending"][p]["approved"] = True
                else:
                    del d["pending"][p]
            return (f"Approved {', '.join(targets)}. It buys on the next check if the price is still close."
                    if cmd == "/yes" else f"Rejected {', '.join(targets)}.")
        if cmd == "/set":
            if len(args) != 2:
                return "Use: /set dip 2.5, /set amount 300 or /set approval on"
            key, val = args[0].lower(), args[1].lower()
            if key == "approval":
                if val not in ("on", "off"):
                    return "Use /set approval on or /set approval off"
                d["overrides"]["approval"] = val == "on"
            elif key in OVERRIDABLE:
                lo, hi = OVERRIDABLE[key][1], OVERRIDABLE[key][2]
                num = float(val)
                if not lo <= num <= hi:
                    return f"{key} must be between {lo} and {hi}"
                d["overrides"][key] = num
            else:
                return f"Can't change '{key}'. Options: dip, amount, approval"
            apply_overrides(self.cfg, d["overrides"])
            return f"Done: {key} is now {val}"
        return "Unknown command. Send /help"

    def status_text(self):
        d = self.state.data
        lines = []
        if self.mode == "paper":
            lines.append(f"Paper cash {inr(d['cash'])}")
        if d["paused"]:
            lines.append("Status: PAUSED")
        for pair, p in d["positions"].items():
            try:
                chg = f"{(self.broker.price(pair) / p['entry_price'] - 1) * 100:+.2f}%"
            except Exception:
                chg = "price unavailable"
            lines.append(f"{pair} bought @ {inr(p['entry_price'])}, now {chg}")
        if not d["positions"]:
            lines.append("No open trades")
        for pair in d["pending"]:
            lines.append(f"{pair} buy signal waiting for /yes")
        lines.append(f"Gains {inr(d['realized_gains'])}, losses {inr(d['realized_losses'])}")
        lines.append(f"Today: {d['buys_today']} buys, P&L {inr(d['pnl_today'])}")
        return "\n".join(lines)

    def record(self, row):
        new = not os.path.exists(TRADES_CSV)
        with open(TRADES_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)

    def recent_high(self, pair, price, now):
        hours = self.cfg["entry"]["lookback_hours"]
        hist = self.state.data["history"].setdefault(pair, [])
        if not hist:   # first run: backfill from hourly candles
            try:
                hist.extend(self.broker.recent_prices(pair, hours))
            except Exception as e:
                log(f"{pair}: could not backfill history ({e}); building it from live checks")
        hist.append([now, price])
        cutoff = now - hours * 3600
        hist[:] = [h for h in hist if h[0] >= cutoff]
        span = (now - hist[0][0]) / 3600 if hist else 0
        if span < hours * 0.5:     # need at least half the window before trusting the "high"
            return None
        return max(h[1] for h in hist)

    def close(self, pair, reason, now, at_price=None):
        d = self.state.data
        pos = d["positions"][pair]
        gross, price, fee, tds = self.broker.sell(pair, pos["qty"], at_price)
        pnl = gross - fee - pos["cost_inr"]    # TDS is excluded: it is a tax credit, not a loss
        d["pnl_today"] += pnl
        if pnl >= 0:
            d["realized_gains"] += pnl
        else:
            d["realized_losses"] += -pnl
        if reason.startswith("stop loss"):
            d["cooldown_until"][pair] = now + self.cfg["safety"]["cooldown_after_stop_minutes"] * 60
        del d["positions"][pair]
        self.record(dict(time=datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                         mode=self.mode, pair=pair, side="SELL", price=round(price, 2),
                         qty=pos["qty"], inr=round(gross, 2), fee=round(fee, 2),
                         tds=round(tds, 2), pnl=round(pnl, 2), reason=reason))
        log(f"SELL {pair} @ {inr(price)}  {reason}  P&L {inr(pnl)}  (TDS {inr(tds)})")
        self.notify(f"SOLD {pair} at {inr(price)}\nReason: {reason}\nP&L {inr(pnl)} (TDS {inr(tds)} held)")

    def catch_up(self, pair, now):
        """Paper mode: replay 5-minute candles since the last check, so a stop or target that
        was touched while the bot was not running still counts. Returns True if it sold."""
        pos = self.state.data["positions"][pair]
        since = pos.get("checked_until", pos["opened"])
        try:
            candles = self.broker.candles_since(pair, since)
        except Exception as e:
            log(f"{pair}: could not fetch candles for catch-up ({e})")
            return False
        for ts, _o, high, low, _c in candles:
            if ts < since or ts + 300 > now:     # skip old and still-forming candles
                continue
            hit = decide_exit_in_candle(pos, high, low, self.cfg["exits"][pair])
            pos["checked_until"] = ts + 300
            if hit:
                reason, price = hit
                try:
                    self.close(pair, reason, ts + 300, at_price=price)
                    return True
                except Exception as e:
                    log(f"{pair}: catch-up sell failed ({e})")
                    return False
        return False

    def step(self, now=None):
        now = now or time.time()
        d = self.state.data
        prev = self.state.roll_day(now)
        if prev is not None:
            self.notify(f"Daily summary: {prev[0]} buys, P&L {inr(prev[1])}\n" + self.status_text())
        self.handle_commands(now)
        expiry = self.cfg.get("approval_expiry_minutes", 60) * 60
        for pair in list(d["pending"]):
            if now - d["pending"][pair]["created"] > expiry:
                del d["pending"][pair]
                self.notify(f"Buy signal for {pair} expired without approval.")
        for pair in self.cfg["pairs"]:
            try:
                price = self.broker.price(pair)
            except Exception as e:
                log(f"{pair}: price check failed ({e}), skipping")
                continue

            high = self.recent_high(pair, price, now)

            if pair in d["positions"] and self.mode == "paper" and hasattr(self.broker, "candles_since"):
                if self.catch_up(pair, now):
                    continue

            if pair in d["positions"]:
                reason = decide_exit(d["positions"][pair], price, self.cfg["exits"][pair])
                if reason:
                    try:
                        self.close(pair, reason, now)
                    except Exception as e:
                        log(f"{pair}: SELL FAILED ({e}). Will retry next check.")
                continue

            pending = d["pending"].get(pair)
            if pending and pending.get("approved"):
                drift = abs(price / pending["price"] - 1) * 100
                blocker = entry_blocker(pair, price, pending["high"], self.state, self.cfg, now)
                if blocker and not blocker.startswith("no dip"):
                    del d["pending"][pair]
                    self.notify(f"Approved {pair} buy cancelled: {blocker}")
                    continue
                if drift > self.cfg.get("approval_max_drift_pct", 1.5):
                    del d["pending"][pair]
                    self.notify(f"Approved {pair} buy cancelled: price moved {drift:.1f}% since the signal.")
                    continue
                del d["pending"][pair]
                high = pending["high"]
            elif pending:
                continue          # still waiting for your /yes
            else:
                if d["paused"]:
                    continue
                blocker = entry_blocker(pair, price, high, self.state, self.cfg, now)
                if blocker:
                    continue
                if self.cfg.get("require_approval"):
                    d["pending"][pair] = dict(price=price, high=high, created=now)
                    r = self.cfg["exits"][pair]
                    self.notify(f"BUY SIGNAL {pair} at {inr(price)} "
                                f"({(1 - price / high) * 100:.1f}% below recent high)\n"
                                f"Would spend {inr(self.cfg['amount_per_trade_inr'])}, "
                                f"stop {inr(price * (1 - r['stop_loss_pct'] / 100))}, "
                                f"target {inr(price * (1 + r['take_profit_pct'] / 100))}\n"
                                f"Reply /yes or /no")
                    continue
            amount = self.cfg["amount_per_trade_inr"]
            try:
                qty, fill, fee = self.broker.buy(pair, amount)
            except Exception as e:
                log(f"{pair}: BUY FAILED ({e})")
                continue
            d["positions"][pair] = dict(qty=qty, entry_price=fill, cost_inr=amount,
                                        highest=fill, opened=now)
            d["buys_today"] += 1
            self.record(dict(time=datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                             mode=self.mode, pair=pair, side="BUY", price=round(fill, 2),
                             qty=qty, inr=amount, fee=round(fee, 2), tds=0, pnl="",
                             reason=f"dip {(1 - price / high) * 100:.2f}% below {self.cfg['entry']['lookback_hours']}h high"))
            r = self.cfg["exits"][pair]
            msg = (f"BUY  {pair} @ {inr(fill)} for {inr(amount)}  "
                   f"stop {inr(fill * (1 - r['stop_loss_pct'] / 100))}  "
                   f"target {inr(fill * (1 + r['take_profit_pct'] / 100))}")
            log(msg)
            self.notify("BOUGHT " + msg[5:])
        self.state.save()

    def status(self):
        d = self.state.data
        print(f"\nMode: {self.mode.upper()}")
        if self.mode == "paper":
            print(f"Paper cash: {inr(d['cash'])}")
        if not d["positions"]:
            print("Open trades: none")
        for pair, p in d["positions"].items():
            try:
                now_price = self.broker.price(pair)
                chg = f"{(now_price / p['entry_price'] - 1) * 100:+.2f}%"
            except Exception:
                chg = "price unavailable"
            print(f"  {pair}: {p['qty']:.8f} bought @ {inr(p['entry_price'])}  now {chg}")
        tax = d["realized_gains"] * 0.30 * 1.04   # 30% + 4% cess
        print(f"Realised gains: {inr(d['realized_gains'])}   losses: {inr(d['realized_losses'])}")
        print(f"Estimated tax on gains (30% + cess, losses not offset): {inr(tax)}")
        print(f"Today: {d['buys_today']} buys, P&L {inr(d['pnl_today'])}\n")


# ---------------------------------------------------------------- report

def write_report(bot, path=None):
    """Write STATUS.md: a phone-friendly summary you can open on GitHub."""
    path = path or os.path.join(HERE, "STATUS.md")
    d = bot.state.data
    start = bot.cfg["paper_starting_balance_inr"]
    lines = [f"# Bot status ({bot.mode} mode)",
             f"Last check: {datetime.now().strftime('%d %b %Y, %H:%M')} UTC", ""]

    held_value = 0.0
    rows = []
    for pair, p in d["positions"].items():
        try:
            px = bot.broker.price(pair)
        except Exception:
            px = p["entry_price"]
        held_value += p["qty"] * px
        r = bot.cfg["exits"][pair]
        rows.append(f"| {pair} | {inr(p['entry_price'])} | {inr(px)} | {(px / p['entry_price'] - 1) * 100:+.2f}% "
                    f"| {inr(p['entry_price'] * (1 - r['stop_loss_pct'] / 100))} "
                    f"| {inr(p['entry_price'] * (1 + r['take_profit_pct'] / 100))} |")

    if bot.mode == "paper":
        total = d["cash"] + held_value
        lines += [f"**Paper account value:** {inr(total)} ({(total / start - 1) * 100:+.2f}% vs {inr(start)} start)",
                  f"Cash {inr(d['cash'])}, in open trades {inr(held_value)}", ""]

    lines += ["## Open trades"]
    if rows:
        lines += ["| Pair | Bought at | Now | Change | Stop | Target |",
                  "|---|---|---|---|---|---|"] + rows
    else:
        lines += ["None right now."]

    tax = d["realized_gains"] * 0.30 * 1.04
    lines += ["", "## Results so far",
              f"- Realised gains: {inr(d['realized_gains'])}",
              f"- Realised losses: {inr(d['realized_losses'])} (cannot be offset against gains in India)",
              f"- Estimated tax on gains (30% + cess): {inr(tax)}",
              f"- Today: {d['buys_today']} buys, P&L {inr(d['pnl_today'])}", ""]

    lines += ["## Last 10 trades"]
    if os.path.exists(TRADES_CSV):
        with open(TRADES_CSV) as f:
            trades = list(csv.DictReader(f))[-10:][::-1]
        lines += ["| Time (UTC) | Pair | Side | Price | P&L | Reason |", "|---|---|---|---|---|---|"]
        for t in trades:
            pnl = inr(float(t["pnl"])) if t["pnl"] else ""
            lines.append(f"| {t['time'].replace('T', ' ')} | {t['pair']} | {t['side']} "
                         f"| {inr(float(t['price']))} | {pnl} | {t['reason']} |")
    else:
        lines += ["No trades yet. The bot waits for a dip below the recent high."]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- main

def build(cfg_path, force_mode=None, price_source=None, tg=None):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(cfg_path)), ".env"))
    except ImportError:
        pass
    cfg = load_config(cfg_path)
    mode = force_mode or cfg["mode"]
    state = State(os.path.join(HERE, f"state_{mode}.json"), cfg["paper_starting_balance_inr"])
    if mode == "live":
        broker = LiveBroker(cfg)
    else:
        broker = PaperBroker(cfg, state, price_source or LivePrices())
    return Bot(cfg, broker, state, mode, tg), cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--sell-all", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the live-mode confirmation")
    ap.add_argument("--paper-only", action="store_true",
                    help="force paper mode whatever config.yaml says (used by the cloud runner)")
    ap.add_argument("--report", action="store_true", help="also write STATUS.md after running")
    args = ap.parse_args()

    bot, cfg = build(args.config, force_mode="paper" if args.paper_only else None)

    if args.status:
        bot.status()
        return
    if bot.mode == "live" and not args.yes:
        if input("LIVE mode places REAL orders with real money. Type LIVE to continue: ") != "LIVE":
            sys.exit("Cancelled.")
    if args.sell_all:
        for pair in list(bot.state.data["positions"]):
            bot.close(pair, "manual sell-all", time.time())
        bot.state.save()
        return

    log(f"Bot started in {bot.mode.upper()} mode watching {', '.join(cfg['pairs'])}")
    while True:
        if os.path.exists(STOP_FILE):
            log("STOP file found, shutting down. Open positions are kept and resume next run.")
            break
        bot.step()
        if args.report:
            write_report(bot)
        if args.once:
            break
        time.sleep(cfg["check_every_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by you (Ctrl+C). Open positions are saved.")

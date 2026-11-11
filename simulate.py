"""
Test the bot's rules on made-up price moves. No internet, no real money.

  python simulate.py

Runs three scripted scenarios and checks the bot did the right thing:
  1. BTC dips, then rallies past the target    -> buy, then take profit
  2. ETH dips, then keeps crashing             -> buy, stop loss, then cooldown blocks re-entry
  3. BTC dips, rallies, then falls back        -> buy, trailing stop locks in profit
"""

import os
import shutil
import tempfile

import bot as botmod


class ScriptedPrices:
    def __init__(self):
        self.now_prices = {}

    def price(self, pair):
        return self.now_prices[pair]

    def recent_prices(self, pair, hours):
        return []          # history builds from the simulated checks

    def candles_since(self, pair, since_ts):
        return [c for c in getattr(self, "candles", {}).get(pair, []) if c[0] >= since_ts]


def path(start, moves):
    """moves = list of (hours, target_price). Linear steps, one check per hour."""
    out, p = [], start
    for hours, target in moves:
        for i in range(1, hours + 1):
            out.append(p + (target - p) * i / hours)
        p = target
    return out


def run(name, series, expect):
    tmp = tempfile.mkdtemp()
    botmod.HERE = tmp
    botmod.TRADES_CSV = os.path.join(tmp, "trades.csv")
    botmod.LOG_FILE = os.path.join(tmp, "bot.log")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), tmp)

    src = ScriptedPrices()
    b, cfg = botmod.build(os.path.join(tmp, "config.yaml"), force_mode="paper", price_source=src)
    cfg["pairs"] = list(series)
    t0 = 1_760_000_000
    n = max(len(s) for s in series.values())
    print(f"\n=== {name} ===")
    for i in range(n):
        for pair, s in series.items():
            src.now_prices[pair] = s[min(i, len(s) - 1)]
        b.step(now=t0 + i * 3600)

    import csv
    rows = list(csv.DictReader(open(botmod.TRADES_CSV))) if os.path.exists(botmod.TRADES_CSV) else []
    got = [(r["pair"], r["side"], r["reason"].split(" (")[0].split(" below")[0]) for r in rows]
    got = [(p, s, "dip" if r.startswith("dip") else r) for p, s, r in got]
    ok = got == expect
    print(f"Trades: {got}")
    print(f"Cash after: Rs {b.state.data['cash']:.2f}")
    print("PASS" if ok else f"FAIL, expected {expect}")
    shutil.rmtree(tmp)
    return ok


BTC = 7_424_718
ETH = 207_547
results = [
    run("Take profit",
        {"BTC/INR": path(BTC, [(24, BTC), (6, BTC * 0.96), (48, BTC * 0.96 * 1.11)])},
        [("BTC/INR", "BUY", "dip"), ("BTC/INR", "SELL", "take profit")]),
    run("Stop loss + cooldown",
        {"ETH/INR": path(ETH, [(24, ETH), (6, ETH * 0.96), (12, ETH * 0.96 * 0.92), (3, ETH * 0.80)])},
        [("ETH/INR", "BUY", "dip"), ("ETH/INR", "SELL", "stop loss")]),
    run("Trailing stop",
        {"BTC/INR": path(BTC, [(24, BTC), (6, BTC * 0.96), (24, BTC * 0.96 * 1.08), (12, BTC * 0.96 * 1.03)])},
        # after the trailing stop, price keeps falling to 3%+ below the new high, so the
        # dip rule fires again: a fresh buy is the correct behaviour
        [("BTC/INR", "BUY", "dip"), ("BTC/INR", "SELL", "trailing stop"), ("BTC/INR", "BUY", "dip")]),
    run("Two coins at once",
        {"BTC/INR": path(BTC, [(24, BTC), (6, BTC * 0.96), (40, BTC * 0.96 * 1.11)]),
         "ETH/INR": path(ETH, [(24, ETH), (6, ETH * 0.96), (12, ETH * 0.96 * 0.92)])},
        [("BTC/INR", "BUY", "dip"), ("ETH/INR", "BUY", "dip"),
         # ETH stops out, waits out the 6h cooldown, then re-enters since it is still in a dip
         ("ETH/INR", "SELL", "stop loss"), ("ETH/INR", "BUY", "dip"),
         ("BTC/INR", "SELL", "take profit")]),
]


def brakes():
    """Check each safety brake blocks a buy on its own."""
    import yaml
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")))
    print("\n=== Safety brakes ===")
    ok = True

    def fresh():
        st = botmod.State(os.path.join(tempfile.mkdtemp(), "s.json"), 1000)
        st.data["day"] = "x"
        return st

    checks = []
    st = fresh(); checks.append(("dip triggers buy", st, None))
    st = fresh(); st.data["pnl_today"] = -100; checks.append(("daily loss limit", st, "daily loss limit hit"))
    st = fresh(); st.data["buys_today"] = 3; checks.append(("max buys per day", st, "max buys per day reached"))
    st = fresh(); st.data["positions"] = {"A": {"cost_inr": 200}, "B": {"cost_inr": 200}}
    checks.append(("max open positions", st, "max open positions reached"))
    st = fresh(); st.data["positions"] = {"A": {"cost_inr": 500}}
    checks.append(("max total invested", st, "max total invested reached"))
    st = fresh(); st.data["cooldown_until"]["BTC/INR"] = 2e9
    checks.append(("cooldown", st, "cooling down after a stop loss"))
    for label, st, want in checks:
        got = botmod.entry_blocker("BTC/INR", 96, 100, st, cfg, 1.9e9)
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {label}: {got}")
    got = botmod.entry_blocker("BTC/INR", 98, 100, fresh(), cfg, 1.9e9)
    good = got is not None and got.startswith("no dip yet")
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  small dip ignored: {got}")
    return ok


def catch_up():
    """Bot is offline for 30 minutes; price spikes below the stop and recovers before the
    next check. Paper mode must still record the stop loss, at the stop price."""
    print("\n=== Catch-up between checks ===")
    tmp = tempfile.mkdtemp()
    botmod.HERE = tmp
    botmod.TRADES_CSV = os.path.join(tmp, "trades.csv")
    botmod.LOG_FILE = os.path.join(tmp, "bot.log")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), tmp)
    src = ScriptedPrices()
    b, cfg = botmod.build(os.path.join(tmp, "config.yaml"), force_mode="paper", price_source=src)
    cfg["pairs"] = ["BTC/INR"]
    t0 = 1_760_000_000
    for i, px in enumerate(path(BTC, [(24, BTC), (6, BTC * 0.96)])):
        src.now_prices["BTC/INR"] = px
        b.step(now=t0 + i * 3600)
    pos = b.state.data["positions"]["BTC/INR"]
    entry, t_buy = pos["entry_price"], pos["opened"]
    # six 5-minute candles while "offline": one dips 6% below entry
    src.candles = {"BTC/INR": [[t_buy + k * 300, entry, entry * 1.01,
                                entry * (0.94 if k == 3 else 0.995), entry] for k in range(6)]}
    src.now_prices["BTC/INR"] = entry * 1.002          # back to normal at the next check
    b.step(now=t_buy + 1800)
    import csv
    rows = list(csv.DictReader(open(botmod.TRADES_CSV)))
    sell = rows[-1]
    want_px = entry * 0.95
    ok = sell["side"] == "SELL" and "stop loss between checks" in sell["reason"] \
        and abs(float(sell["price"]) - want_px) < 1
    print(f"  sold at {sell['price']} (stop was {want_px:.2f}) reason: {sell['reason']}")
    botmod.write_report(b, os.path.join(tmp, "STATUS.md"))
    report = open(os.path.join(tmp, "STATUS.md")).read()
    ok &= "stop loss between checks" in report and "Paper account value" in report
    print("  STATUS.md written:", "yes" if "Paper account value" in report else "no")
    print("PASS" if ok else "FAIL")
    shutil.rmtree(tmp)
    return ok


class FakeTelegram:
    """Stands in for Telegram: records what the bot sends, feeds it your commands."""
    enabled = True

    def __init__(self):
        self.sent, self.inbox = [], []

    def send(self, text):
        self.sent.append(text)

    def read_commands(self, offset):
        cmds, self.inbox = self.inbox, []
        return cmds, offset + len(cmds)


def telegram_flow():
    print("\n=== Telegram: alerts, approval, commands ===")
    tmp = tempfile.mkdtemp()
    botmod.HERE = tmp
    botmod.TRADES_CSV = os.path.join(tmp, "trades.csv")
    botmod.LOG_FILE = os.path.join(tmp, "bot.log")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), tmp)
    src, tg = ScriptedPrices(), FakeTelegram()
    b, cfg = botmod.build(os.path.join(tmp, "config.yaml"), force_mode="paper", price_source=src, tg=tg)
    cfg["pairs"] = ["BTC/INR"]
    t0, i = 1_760_000_000, 0
    ok = True

    def tick(px, cmds=()):
        nonlocal i
        tg.inbox = list(cmds)
        src.now_prices["BTC/INR"] = px
        b.step(now=t0 + i * 1800)
        i += 1

    def expect(label, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")

    for _ in range(48):                        # 24h of flat prices, half-hourly
        tick(BTC)
    tick(BTC, ["/set approval on"])
    expect("/set approval on is confirmed", any("approval is now on" in m for m in tg.sent))
    tick(BTC * 0.96)                           # dip: should ask, not buy
    expect("dip sends a BUY SIGNAL and waits", any("BUY SIGNAL" in m for m in tg.sent)
           and "BTC/INR" not in b.state.data["positions"])
    tick(BTC * 0.961, ["/yes"])               # approve; buys on this same check
    expect("/yes leads to a buy", "BTC/INR" in b.state.data["positions"]
           and any(m.startswith("[PAPER] BOUGHT") for m in tg.sent))
    tick(BTC * 0.962, ["/status"])
    expect("/status replies with the open trade", any("bought @" in m for m in tg.sent))
    tick(BTC * 0.962, ["/pause"])
    expect("/pause sets paused", b.state.data["paused"])
    tick(BTC * 0.962, ["/sellall"])
    expect("/sellall closes the trade and alerts", not b.state.data["positions"]
           and any(m.startswith("[PAPER] SOLD") for m in tg.sent))
    tick(BTC * 0.95)
    expect("paused bot sends no new signal", sum("BUY SIGNAL" in m for m in tg.sent) == 1)
    tick(BTC * 0.95, ["/set dip 99"])
    expect("out-of-range /set is refused", any("must be between" in m for m in tg.sent))
    tick(BTC * 0.95, ["/resume", "/set approval off"])
    expect("resume + approval off then buys automatically", "BTC/INR" in b.state.data["positions"])
    shutil.rmtree(tmp)
    print("PASS" if ok else "FAIL")
    return ok


results.append(brakes())
results.append(catch_up())
results.append(telegram_flow())
print(f"\n{sum(results)}/{len(results)} scenarios passed")

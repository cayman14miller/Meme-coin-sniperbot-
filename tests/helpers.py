import time

from sniperbot.data.market import Candle, PairInfo


def make_pair(mint="MINT1111111111111111111111111111111111111", price=0.001, **kw) -> PairInfo:
    base = dict(
        mint=mint, symbol="TEST", name="Test", pair_address="PAIR" + mint[:20], dex="raydium",
        price_usd=price, price_native=price / 150, liquidity_usd=80_000, fdv=500_000, market_cap=500_000,
        created_at=time.time() - 3 * 3600,
        volume={"m5": 20_000, "h1": 150_000}, price_change={"m5": 6, "h1": 30, "h6": 80},
        buys={"m5": 120, "h1": 900}, sells={"m5": 50, "h1": 500}, url="https://dexscreener.com/x", has_socials=True,
    )
    base.update(kw)
    return PairInfo(**base)


def candles_from_closes(closes, vols=None, wick=0.002):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(i * 60, o, max(o, c) * (1 + wick), min(o, c) * (1 - wick), c, (vols or [1000] * len(closes))[i]))
        prev = c
    return out


def uptrend(n=80, start=1.0):
    closes, p = [], start
    for i in range(n):
        p *= 1 + (0.012, -0.006, 0.003)[i % 3]
        closes.append(p)
    vols = [1000 + i * 30 for i in range(n)]
    return candles_from_closes(closes, vols)


def breakout(n=60):
    closes = [1.0 + (0.01 if i % 2 else -0.01) for i in range(n - 1)] + [1.08]
    vols = [1000] * (n - 1) + [5000]
    cs = candles_from_closes(closes, vols, wick=0.001)
    last = cs[-1]
    cs[-1] = Candle(last.ts, last.open, 1.082, last.open * 0.999, 1.08, 5000)
    return cs

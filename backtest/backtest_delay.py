"""
backtest_delay.py
-----------------
Historisk backtest af skip-reglen: "spring et køb over, hvis kursen allerede er steget
mere end X % siden medlemmets handel". Et kursfald giver aldrig skip.

For hvert købssignal i house_trades.csv:
  1. Indgang  = lukkekurs første handelsdag EFTER indleveringsdatoen (filing_date).
               Det er den tidligste dag, offentligheden kunne kende handlen.
  2. Drift    = kursændring fra medlemmets handelsdag til vores indgang.
  3. Afkast   = afkast over 21/63/126 handelsdage minus SPY's afkast i samme periode
               (= overnormalt afkast), minus handelsomkostninger.
  4. Signalerne deles i grupper efter drift. Grænsen X findes på data FØR TEST_START
     og kontrolleres bagefter på data fra TEST_START og frem (ren test).

Kør (fra projektmappen, kræver ALPACA_KEY_ID og ALPACA_SECRET_KEY i .env):
    python3 backtest/backtest_delay.py

Første kørsel henter dagskurser for alle aktier og gemmer dem i prices_cache/.
Senere kørsler bruger cachen og går hurtigt.

Resultater:
    backtest_report.txt       - oversigt og anbefalet grænse
    backtest_signals.csv      - hvert signal med drift og afkast (til videre analyse)

Forbehold (står også i rapporten):
  - Aktier, Alpaca ikke har kurser på (fx afnoterede), udelades. Det kan give skævhed.
  - Signaler i samme aktie på samme dag hænger sammen, så t-værdierne er for optimistiske.
  - Medlemmer, der kun indleverer på papir (scannede PDF'er), er ikke med.
"""

from __future__ import annotations

import bisect
import csv
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # projektmappen
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
TRADES_CSV = DATA / "house_trades.csv"
CACHE_DIR = DATA / "prices_cache"
REPORT_TXT = REPORTS / "backtest_report.txt"
SIGNALS_CSV = DATA / "backtest_signals.csv"

BENCHMARK = "SPY"
HORIZONS = (21, 63, 126)        # handelsdage: ca. 1, 3 og 6 måneder
MAIN_HORIZON = 63               # grænsen vælges ud fra 3-måneders afkast
COST_ROUND_TRIP = 0.0020        # 10 bps ind + 10 bps ud (spread + slippage)
TEST_START = date(2025, 11, 1)  # signaler indleveret herefter er ren test
PRICE_START = "2018-06-01"
MIN_GROUP = 30                  # færre signaler end dette i en gruppe -> for usikkert

DRIFT_BUCKETS = [               # (navn, nedre grænse, øvre grænse) i decimaler
    ("under -10%", -math.inf, -0.10),
    ("-10% til -5%", -0.10, -0.05),
    ("-5% til 0%", -0.05, 0.0),
    ("0% til 5%", 0.0, 0.05),
    ("5% til 10%", 0.05, 0.10),
    ("10% til 20%", 0.10, 0.20),
    ("over 20%", 0.20, math.inf),
]


# ----------------------------------------------------------------- kursdata

class PriceStore:
    """Dagslukkekurser pr. ticker: sorterede datoer og kurser."""

    def __init__(self) -> None:
        self.dates: dict[str, list[date]] = {}
        self.closes: dict[str, list[float]] = {}

    def add(self, ticker: str, rows: list[tuple[date, float]]) -> None:
        rows = sorted(rows)
        self.dates[ticker] = [d for d, _ in rows]
        self.closes[ticker] = [c for _, c in rows]

    def has(self, ticker: str) -> bool:
        return bool(self.dates.get(ticker))

    def index_on_or_before(self, ticker: str, d: date) -> int | None:
        i = bisect.bisect_right(self.dates[ticker], d) - 1
        return i if i >= 0 else None

    def index_after(self, ticker: str, d: date) -> int | None:
        i = bisect.bisect_right(self.dates[ticker], d)
        return i if i < len(self.dates[ticker]) else None


def load_cache(ticker: str) -> list[tuple[date, float]] | None:
    path = CACHE_DIR / f"{ticker.replace('/', '_')}.csv"
    if not path.exists():
        return None
    with path.open() as fh:
        return [(date.fromisoformat(r["date"]), float(r["close"])) for r in csv.DictReader(fh)]


def save_cache(ticker: str, rows: list[tuple[date, float]]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{ticker.replace('/', '_')}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "close"])
        w.writerows((d.isoformat(), c) for d, c in rows)


def fetch_alpaca_bars(symbols: list[str], feed: str) -> dict[str, list[tuple[date, float]]]:
    """Henter justerede dagskurser (splits og udbytter) for op til ~100 symboler ad gangen."""
    import requests

    headers = {
        "APCA-API-KEY-ID": os.getenv("ALPACA_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }
    params = {
        "symbols": ",".join(symbols), "timeframe": "1Day", "start": PRICE_START,
        "end": (date.today() - timedelta(days=1)).isoformat(),
        "adjustment": "all", "feed": feed, "limit": 10000,
    }
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    while True:
        resp = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                            headers=headers, params=params, timeout=60)
        if resp.status_code == 429:          # for mange kald - vent og prøv igen
            time.sleep(5)
            continue
        resp.raise_for_status()
        data = resp.json()
        for sym, bars in (data.get("bars") or {}).items():
            out[sym].extend((date.fromisoformat(b["t"][:10]), float(b["c"])) for b in bars)
        token = data.get("next_page_token")
        if not token:
            return out
        params["page_token"] = token
        time.sleep(0.3)


def build_price_store(tickers: set[str]) -> tuple[PriceStore, list[str]]:
    """Fylder PriceStore fra cache og henter det manglende fra Alpaca."""
    from dotenv import load_dotenv

    load_dotenv()
    store = PriceStore()
    missing = []
    for t in sorted(tickers):
        rows = load_cache(t)
        if rows is None:
            missing.append(t)
        elif rows:
            store.add(t, rows)

    if missing:
        print(f"Henter kurser for {len(missing)} tickere fra Alpaca (gemmes i {CACHE_DIR}/) ...")
        feed = "sip"
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            try:
                got = fetch_alpaca_bars(batch, feed)
            except Exception as exc:
                if feed == "sip" and "403" in str(exc):
                    print("  Din Alpaca-plan giver ikke SIP-data. Skifter til IEX-data.")
                    feed = "iex"
                    got = fetch_alpaca_bars(batch, feed)
                else:
                    print(f"  Fejl ved hentning: {exc}")
                    continue
            for t in batch:
                rows = got.get(t, [])
                save_cache(t, rows)          # også tomme, så vi ikke spørger igen
                if rows:
                    store.add(t, rows)
            print(f"  {min(i + 100, len(missing))}/{len(missing)} ... ({feed.upper()}-data)")

    no_data = sorted(t for t in tickers if not store.has(t))
    return store, no_data


# ----------------------------------------------------------------- signaler

def load_signals(path: Path) -> tuple[list[dict], dict]:
    """Købssignaler: køb af aktier/ETF'er, ikke short-dækning, én pr. (medlem, ticker, handelsdag)."""
    stats = defaultdict(int)
    seen = set()
    signals = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            stats["rækker"] += 1
            if r["side"] != "buy":
                continue
            if r.get("short_related") == "True":
                stats["fravalgt: short-dækning"] += 1
                continue
            tx, filed = date.fromisoformat(r["transaction_date"]), date.fromisoformat(r["filing_date"])
            if filed < tx:
                stats["fravalgt: indleveret før handel (datofejl)"] += 1
                continue
            key = (r["member"], r["ticker"], tx)
            if key in seen:
                stats["fravalgt: samme køb i flere konti"] += 1
                continue
            seen.add(key)
            signals.append({"member": r["member"], "ticker": r["ticker"], "transaction_date": tx,
                            "filing_date": filed, "amount_low": r["amount_low"]})
    stats["købssignaler"] = len(signals)
    return signals, dict(stats)


def evaluate(signals: list[dict], store: PriceStore) -> list[dict]:
    """Beregner drift og overnormalt afkast for hvert signal."""
    out = []
    for s in signals:
        t = s["ticker"]
        if not store.has(t):
            continue
        i_tx = store.index_on_or_before(t, s["transaction_date"])
        i_in = store.index_after(t, s["filing_date"])
        if i_tx is None or i_in is None:
            continue
        p_tx, p_in = store.closes[t][i_tx], store.closes[t][i_in]
        entry_day = store.dates[t][i_in]
        b_in = store.index_on_or_before(BENCHMARK, entry_day)
        if b_in is None or p_tx <= 0 or p_in <= 0:
            continue
        row = dict(s, entry_date=entry_day, delay_days=(s["filing_date"] - s["transaction_date"]).days,
                   drift=p_in / p_tx - 1)
        for h in HORIZONS:
            i_out = i_in + h
            if i_out >= len(store.closes[t]):
                row[f"excess_{h}"] = None
                continue
            exit_day = store.dates[t][i_out]
            b_out = store.index_on_or_before(BENCHMARK, exit_day)
            r_stock = store.closes[t][i_out] / p_in - 1
            r_bench = store.closes[BENCHMARK][b_out] / store.closes[BENCHMARK][b_in] - 1
            row[f"excess_{h}"] = r_stock - r_bench - COST_ROUND_TRIP
        out.append(row)
    return out


# ----------------------------------------------------------------- rapport

def summarize(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    return {
        "n": n, "mean": mean, "median": statistics.median(values),
        "hit": sum(v > 0 for v in values) / n,
        "t": mean / (sd / math.sqrt(n)) if sd > 0 else 0.0,
    }


def bucket_table(rows: list[dict], horizon: int) -> list[tuple[str, float, float, dict]]:
    table = []
    for name, lo, hi in DRIFT_BUCKETS:
        vals = [r[f"excess_{horizon}"] for r in rows
                if lo < r["drift"] <= hi and r[f"excess_{horizon}"] is not None]
        table.append((name, lo, hi, summarize(vals)))
    return table


def fmt_stats(s: dict) -> str:
    if s["n"] == 0:
        return f"{'0':>6}  {'-':>9}  {'-':>9}  {'-':>8}  {'-':>6}"
    flag = "  (få)" if s["n"] < MIN_GROUP else ""
    return (f"{s['n']:>6}  {s['mean'] * 100:>8.2f}%  {s['median'] * 100:>8.2f}%  "
            f"{s['hit'] * 100:>7.1f}%  {s['t']:>6.2f}{flag}")


def recommend_threshold(table: list[tuple[str, float, float, dict]]) -> tuple[float | None, str]:
    """Grænsen = nedre grænse for den første positive drift-gruppe, hvor det overnormale
    afkast efter omkostninger ikke længere er positivt. Grupper med for få signaler springes over."""
    for name, lo, hi, s in table:
        if lo < 0 or s["n"] < MIN_GROUP:
            continue
        if s["mean"] <= 0:
            return lo, (f"Fra gruppen '{name}' er det gennemsnitlige overnormale afkast efter "
                        f"omkostninger ikke længere positivt.")
    return None, "Ingen positiv drift-gruppe med nok signaler mister sit overnormale afkast."


def write_report(stats: dict, rows: list[dict], no_data: list[str], n_tickers: int) -> str:
    train = [r for r in rows if r["filing_date"] < TEST_START]
    test = [r for r in rows if r["filing_date"] >= TEST_START]
    lines = []
    add = lines.append
    add("BACKTEST AF SKIP-REGLEN (kursstigning siden medlemmets køb)")
    add("=" * 72)
    add(f"Kørt: {date.today().isoformat()}")
    add(f"Indgang: lukkekurs første handelsdag efter indleveringsdato. Benchmark: {BENCHMARK}.")
    add(f"Afkast er overnormalt (aktie minus {BENCHMARK}) og efter {COST_ROUND_TRIP * 1e4:.0f} bps omkostninger.")
    add("")
    add("DATAGRUNDLAG")
    for k, v in stats.items():
        add(f"  {k}: {v}")
    add(f"  tickere uden kursdata hos Alpaca: {len(no_data)} af {n_tickers}")
    add(f"  signaler med kursdata: {len(rows)}  (kalibrering: {len(train)}, ren test fra {TEST_START}: {len(test)})")
    add("")

    head = f"  {'drift før indgang':<16} {'antal':>6}  {'gns.':>9}  {'median':>9}  {'positive':>8}  {'t':>6}"
    for label, subset in (("KALIBRERING (indleveret før " + TEST_START.isoformat() + ")", train),
                          ("REN TEST (indleveret fra " + TEST_START.isoformat() + ")", test)):
        for h in HORIZONS:
            add(f"{label} - overnormalt afkast over {h} handelsdage")
            add(head)
            for name, _, _, s in bucket_table(subset, h):
                add(f"  {name:<16} {fmt_stats(s)}")
            alls = summarize([r[f"excess_{h}"] for r in subset if r[f"excess_{h}"] is not None])
            add(f"  {'ALLE':<16} {fmt_stats(alls)}")
            add("")

    base = summarize([r[f"excess_{MAIN_HORIZON}"] for r in train
                      if r["drift"] <= 0 and r[f"excess_{MAIN_HORIZON}"] is not None])
    add("SLÅR STRATEGIEN OVERHOVEDET MARKEDET? (kalibrering, signaler uden kursstigning)")
    add(f"  {fmt_stats(base)}")
    if base["n"] and (base["mean"] <= 0 or base["t"] < 2):
        add("  ADVARSEL: Selv købssignaler uden kursstigning før indgang giver ikke et klart positivt")
        add("  overnormalt afkast. Så handler spørgsmålet ikke om grænsen, men om strategien har en")
        add("  fordel overhovedet. Grænsen nedenfor skal læses med det forbehold.")
    add("")

    threshold, why = recommend_threshold(bucket_table(train, MAIN_HORIZON))
    add("ANBEFALET GRÆNSE (ud fra kalibrering, " + str(MAIN_HORIZON) + " handelsdage)")
    add(f"  {why}")
    if threshold is not None:
        add(f"  Regel: spring et køb over, hvis kursen er steget mere end {threshold:.0%} siden medlemmets handel.")
        kept = [r[f"excess_{MAIN_HORIZON}"] for r in test
                if r["drift"] <= threshold and r[f"excess_{MAIN_HORIZON}"] is not None]
        skipped = [r[f"excess_{MAIN_HORIZON}"] for r in test
                   if r["drift"] > threshold and r[f"excess_{MAIN_HORIZON}"] is not None]
        add(f"  Kontrol på ren test:  beholdte signaler {fmt_stats(summarize(kept))}")
        add(f"                        frasorterede       {fmt_stats(summarize(skipped))}")
        add("  Reglen holder i testen, hvis de beholdte signaler klarer sig bedre end de frasorterede.")
    add("")

    late = [r[f"excess_{MAIN_HORIZON}"] for r in train if r["delay_days"] > 45 and r[f"excess_{MAIN_HORIZON}"] is not None]
    ontime = [r[f"excess_{MAIN_HORIZON}"] for r in train if r["delay_days"] <= 45 and r[f"excess_{MAIN_HORIZON}"] is not None]
    add("FORSINKELSE (kalibrering, " + str(MAIN_HORIZON) + " handelsdage)")
    add(f"  indleveret inden for 45 dage  {fmt_stats(summarize(ontime))}")
    add(f"  indleveret efter 45 dage      {fmt_stats(summarize(late))}")
    add("")
    add("FORBEHOLD")
    add("  - Aktier uden kursdata hos Alpaca (fx afnoterede) er udeladt. Det kan gøre resultatet for pænt.")
    add("  - Signaler i samme aktie samme dag hænger sammen, så t-værdierne er for optimistiske.")
    add("    Tommelfingerregel: tag kun en forskel alvorligt, hvis |t| er klart over 2.")
    add("  - Medlemmer, der kun indleverer på papir (scannede PDF'er), er ikke med.")
    add("  - Grupper markeret '(få)' har under " + str(MIN_GROUP) + " signaler og bør ikke bruges alene.")
    report = "\n".join(lines)
    REPORT_TXT.write_text(report, encoding="utf-8")
    return report


def write_signals(rows: list[dict]) -> None:
    fields = ["member", "ticker", "transaction_date", "filing_date", "entry_date", "delay_days",
              "amount_low", "drift"] + [f"excess_{h}" for h in HORIZONS]
    with SIGNALS_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    if not TRADES_CSV.exists():
        sys.exit(f"Fandt ikke {TRADES_CSV}. Kør først build_house_trades.py.")
    signals, stats = load_signals(TRADES_CSV)
    tickers = {s["ticker"] for s in signals} | {BENCHMARK}
    print(f"{len(signals)} købssignaler i {len(tickers) - 1} aktier.")
    store, no_data = build_price_store(tickers)
    if not store.has(BENCHMARK):
        sys.exit(f"Ingen kursdata for {BENCHMARK} - tjek Alpaca-nøglerne i .env.")
    rows = evaluate(signals, store)
    write_signals(rows)
    print()
    print(write_report(stats, rows, no_data, len(tickers) - 1))
    print(f"\nGemt: {REPORT_TXT} og {SIGNALS_CSV}")


if __name__ == "__main__":
    main()

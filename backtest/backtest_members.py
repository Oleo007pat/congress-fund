"""
backtest_members.py
-------------------
Backtest af den nye strategi: følg kun de bedst præsterende medlemmer, og test om det hjælper
at kræve, at aktien hører til en branche, som medlemmets udvalg har indflydelse på.

Undgår fremadskuende snyd (look-ahead bias):
  - Hver måned vælges top-medlemmerne på ny, KUN ud fra signaler hvis afkast var kendt
    før månedens start (indgang + ca. 3 måneder < månedsstart).
  - Udvalgsposter er de poster, medlemmet havde i handelsmåneden (historiske versioner fra
    github.com/unitedstates/congress-legislators), ikke dagens poster.

Input (i samme mappe):
  backtest_signals.csv       fra backtest_delay.py (drift og overnormalt afkast pr. signal)
  member_ids.csv             medlemsnavn -> officielt ID (bioguide)
  committee_assignments.csv  udvalgsposter pr. måned 2021-2026
  ticker_sectors.csv         sektor og branche pr. ticker

Kør:  python3 backtest/backtest_members.py        -> skriver members_report.txt
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # projektmappen
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
SIGNALS = DATA / "backtest_signals.csv"
MEMBER_IDS = DATA / "member_ids.csv"
COMMITTEES = DATA / "committee_assignments.csv"
SECTORS = DATA / "ticker_sectors.csv"
REPORT = REPORTS / "members_report.txt"

HORIZON = 63                    # overnormalt afkast over 63 handelsdage (ca. 3 måneder)
KNOWN_AFTER_DAYS = 100          # 63 handelsdage er ca. 91 kalenderdage. 100 giver luft.
SHRINK_K = 10                   # trækker scoren mod 0 for medlemmer med få handler
MIN_HISTORY = 8                 # mindst så mange afsluttede signaler for at kunne rangeres
TEST_START = date(2025, 11, 1)  # samme rene testperiode som backtest_delay.py
FIRST_MONTH = date(2022, 1, 1)  # første måned med nok historik til at rangere
GRID_TOP_N = (5, 10, 20)
GRID_LOOKBACK_MONTHS = (12, 24)

LEADERSHIP_TITLES = ("chair", "ranking")   # Chair, Chairman, Vice Chair, Ranking Member

# Udvalg -> brancher, udvalget har direkte lovgivnings- eller tilsynsmagt over.
# Bevidst smal: brede udvalg (Appropriations, Rules, Oversight m.fl.) er ikke med.
COMMITTEE_SCOPE = {
    "HSBA": {"name": "Financial Services",
             "sectors": {"Finance", "Real Estate"}, "industries": set()},
    "HSAS": {"name": "Armed Services", "sectors": set(),
             "industries": {"Aerospace", "Military/Government/Technical", "Ordnance And Accessories"}},
    "HSIF": {"name": "Energy and Commerce",
             "sectors": {"Energy", "Utilities", "Health Care", "Telecommunications"},
             "industries": {"Motor Vehicles", "Auto Manufacturing", "Broadcasting",
                            "Cable & Other Pay Television Services"}},
    "HSAG": {"name": "Agriculture", "sectors": set(),
             "industries": {"Agricultural Chemicals", "Farming/Seeds/Milling", "Meat/Poultry/Fish",
                            "Construction/Ag Equipment/Trucks", "Packaged Foods", "Food Distributors"}},
    "HSPW": {"name": "Transportation and Infrastructure", "sectors": set(),
             "industries": {"Air Freight/Delivery Services", "Railroads", "Trucking Freight/Courier Services",
                            "Marine Transportation", "Integrated Freight & Logistics", "Transportation Services",
                            "Engineering & Construction", "Water Sewer Pipeline Comm & Power Line Construction"}},
    "HSII": {"name": "Natural Resources", "sectors": set(),
             "industries": {"Oil & Gas Production", "Integrated oil Companies", "Coal Mining", "Metal Mining",
                            "Precious Metals", "Other Metals and Minerals", "Forest Products",
                            "Mining & Quarrying of Nonmetallic Minerals (No Fuels)", "Oilfield Services/Equipment"}},
    "HSSY": {"name": "Science, Space, and Technology", "sectors": set(),
             "industries": {"Aerospace", "Semiconductors", "Military/Government/Technical"}},
    "HSHM": {"name": "Homeland Security", "sectors": set(),
             "industries": {"Military/Government/Technical", "Aerospace"}},
    "HLIG": {"name": "Intelligence", "sectors": set(),
             "industries": {"Military/Government/Technical", "Aerospace"}},
    "HSWM": {"name": "Ways and Means", "sectors": set(),
             "industries": {"Accident &Health Insurance", "Hospital/Nursing Management", "Medical/Nursing Services"}},
}


# ------------------------------------------------------------------ indlæsning

def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def load_signals() -> list[dict]:
    rows = []
    with SIGNALS.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            v = r.get(f"excess_{HORIZON}")
            if not v:
                continue
            rows.append({
                "member": r["member"], "ticker": r["ticker"],
                "transaction_date": date.fromisoformat(r["transaction_date"]),
                "filing_date": date.fromisoformat(r["filing_date"]),
                "entry_date": date.fromisoformat(r["entry_date"]),
                "drift": float(r["drift"]), "excess": float(v),
            })
    return rows


def load_member_ids() -> dict[str, str]:
    with MEMBER_IDS.open(encoding="utf-8") as fh:
        return {r["member"]: r["bioguide"] for r in csv.DictReader(fh)}


def load_committees() -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(måned 'YYYY-MM', bioguide) -> [(udvalg, titel), ...]"""
    out = defaultdict(list)
    with COMMITTEES.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out[(r["month"], r["bioguide"])].append((r["committee"], r["title"]))
    return out


def load_sectors() -> dict[str, tuple[str, str]]:
    with SECTORS.open(encoding="utf-8") as fh:
        return {r["ticker"]: (r["sector"], r["industry"]) for r in csv.DictReader(fh)}


def annotate_committee_relevance(signals, ids, committees, sectors) -> None:
    """Markerer om aktien hører til en branche, som medlemmets udvalg havde indflydelse på
    i handelsmåneden, og om medlemmet var formand/ledende mindretalsmedlem i det udvalg."""
    for s in signals:
        s["bioguide"] = ids.get(s["member"], "")
        sector, industry = sectors.get(s["ticker"], ("", ""))
        s["has_sector"] = bool(sector)
        s["relevant"] = s["leader_relevant"] = False
        s["committees"] = ""
        posts = committees.get((s["transaction_date"].isoformat()[:7], s["bioguide"]), [])
        hits = []
        for cid, title in posts:
            scope = COMMITTEE_SCOPE.get(cid)
            if scope and (sector in scope["sectors"] or industry in scope["industries"]):
                hits.append(scope["name"])
                s["relevant"] = True
                if any(t in title.lower() for t in LEADERSHIP_TITLES):
                    s["leader_relevant"] = True
        s["committees"] = "; ".join(sorted(set(hits)))


# ------------------------------------------------------------------ udvælgelse

def select_members(signals, month: date, top_n: int, lookback: int) -> list[tuple[str, float, int]]:
    """Top-medlemmer ved månedens start, kun ud fra signaler med kendt udfald."""
    window_start = add_months(month, -lookback)
    hist = defaultdict(list)
    for s in signals:
        if window_start <= s["entry_date"] and s["entry_date"] + timedelta(days=KNOWN_AFTER_DAYS) < month:
            hist[s["member"]].append(s["excess"])
    scored = [(m, sum(v) / (len(v) + SHRINK_K), len(v)) for m, v in hist.items() if len(v) >= MIN_HISTORY]
    scored = [x for x in scored if x[1] > 0]       # kun medlemmer med positiv historik
    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]


def run_strategy(signals, top_n: int, lookback: int) -> list[dict]:
    """Signaler, strategien ville have fulgt: indleveret i måneden af et top-medlem."""
    by_month = defaultdict(list)
    for s in signals:
        by_month[month_start(s["filing_date"])].append(s)
    last = max(by_month)
    followed = []
    month = FIRST_MONTH
    while month <= last:
        chosen = {m for m, _, _ in select_members(signals, month, top_n, lookback)}
        followed.extend(s for s in by_month.get(month, []) if s["member"] in chosen)
        month = add_months(month, 1)
    return followed


# ------------------------------------------------------------------ statistik

def stats(rows: list[dict]) -> dict:
    v = [r["excess"] for r in rows]
    n = len(v)
    if n < 2:
        return {"n": n}
    mean = statistics.fmean(v)
    t = mean / (statistics.stdev(v) / math.sqrt(n))
    # t-værdi på månedsgennemsnit: tager højde for, at signaler i samme måned hænger sammen
    months = defaultdict(list)
    for r in rows:
        months[month_start(r["filing_date"])].append(r["excess"])
    mm = [statistics.fmean(x) for x in months.values()]
    t_m = (statistics.fmean(mm) / (statistics.stdev(mm) / math.sqrt(len(mm)))) if len(mm) > 2 and statistics.stdev(mm) > 0 else 0.0
    return {"n": n, "mean": mean, "hit": sum(x > 0 for x in v) / n, "t": t, "t_m": t_m, "months": len(mm)}


def fmt(s: dict) -> str:
    if s["n"] < 2:
        return f"{s['n']:>6}  {'-':>8}  {'-':>8}  {'-':>6}  {'-':>8}"
    return (f"{s['n']:>6}  {s['mean'] * 100:>7.2f}%  {s['hit'] * 100:>7.1f}%  "
            f"{s['t']:>6.2f}  {s['t_m']:>8.2f}")


HEAD = f"  {'':<44} {'antal':>6}  {'gns.':>8}  {'positive':>8}  {'t':>6}  {'t (mdr.)':>8}"


def split(rows):
    return ([r for r in rows if r["filing_date"] < TEST_START], [r for r in rows if r["filing_date"] >= TEST_START])


def main() -> None:
    signals = load_signals()
    annotate_committee_relevance(signals, load_member_ids(), load_committees(), load_sectors())
    universe = [s for s in signals if s["filing_date"] >= FIRST_MONTH]
    lines = []
    add = lines.append
    add("BACKTEST: FØLG DE BEDSTE MEDLEMMER + UDVALGSRELEVANS")
    add("=" * 78)
    add(f"Mål: overnormalt afkast over {HORIZON} handelsdage (aktie minus SPY, efter omkostninger).")
    add("Top-medlemmer vælges hver måned ud fra signaler med kendt udfald (ingen fremadskuen).")
    add(f"Kalibrering: indleveret {FIRST_MONTH} til {TEST_START}. Ren test: fra {TEST_START}.")
    add("'t (mdr.)' er t-værdien på månedsgennemsnit og er den mest ærlige. Kræv klart over 2.")
    add("")

    cal_all, test_all = split(universe)
    add("1. SAMMENLIGNINGSGRUNDLAG: ALLE KØBSSIGNALER")
    add(HEAD)
    add(f"  {'kalibrering':<44} {fmt(stats(cal_all))}")
    add(f"  {'ren test':<44} {fmt(stats(test_all))}")
    add("")

    add("2. FØLG TOP-MEDLEMMER (rangeret på historisk overnormalt afkast)")
    add(HEAD)
    results = {}
    for n in GRID_TOP_N:
        for lb in GRID_LOOKBACK_MONTHS:
            cal, test = split(run_strategy(signals, n, lb))
            results[(n, lb)] = (cal, test)
            add(f"  {f'top {n}, historik {lb} mdr. - kalibrering':<44} {fmt(stats(cal))}")
            add(f"  {f'top {n}, historik {lb} mdr. - ren test':<44} {fmt(stats(test))}")
    add("  Alle 6 varianter vises, så man kan se, om resultatet er robust eller hænger på ét valg.")
    add("")

    add("3. UDVALGSRELEVANS (alle medlemmer, kun aktier med kendt sektor)")
    add(HEAD)
    with_sector = [s for s in universe if s["has_sector"]]
    for label, subset in (("aktie i udvalgets branche", [s for s in with_sector if s["relevant"]]),
                          ("  heraf formand/ledende mindretalsmedlem", [s for s in with_sector if s["leader_relevant"]]),
                          ("aktie uden for udvalgets brancher", [s for s in with_sector if not s["relevant"]])):
        cal, test = split(subset)
        add(f"  {label + ' - kalibrering':<44} {fmt(stats(cal))}")
        add(f"  {label + ' - ren test':<44} {fmt(stats(test))}")
    add("")
    add("   Fordeling af relevante signaler på udvalg:")
    per = defaultdict(list)
    for s in with_sector:
        for c in filter(None, s["committees"].split("; ")):
            per[c].append(s)
    for c, rows in sorted(per.items(), key=lambda x: -len(x[1])):
        add(f"     {c:<40} {fmt(stats(rows))}")
    add("")

    add("4. KOMBINATION: TOP-MEDLEMMER OG KUN AKTIER I DERES UDVALGS BRANCHER")
    add(HEAD)
    for (n, lb), (cal, test) in results.items():
        c2 = [s for s in cal if s["relevant"]]
        t2 = [s for s in test if s["relevant"]]
        add(f"  {f'top {n}, {lb} mdr. + udvalg - kalibrering':<44} {fmt(stats(c2))}")
        add(f"  {f'top {n}, {lb} mdr. + udvalg - ren test':<44} {fmt(stats(t2))}")
    add("")

    # Hvem ville strategien følge i dag?
    today = month_start(date.today())
    add(f"5. TOP 10 MEDLEMMER VED {today} (historik 24 mdr.) - kandidater til at blive fulgt")
    for m, score, k in select_members(signals, today, 10, 24):
        add(f"   {m:<32} score {score * 100:>6.2f}%   afsluttede signaler: {k}")
    add("")
    add("FORBEHOLD")
    add("  - Medlemmer, der kun indleverer på papir (fx Khanna, McCaul), er ikke med.")
    add("  - Koblingen mellem udvalg og brancher er en vurdering (se COMMITTEE_SCOPE i scriptet).")
    add("  - Sektor findes kun for aktier, der er børsnoteret i dag. Afnoterede aktier mangler.")
    add("  - Seks varianter er testet. Ved så mange forsøg kan én se god ud ved tilfældighed,")
    add("    så kig efter et mønster, der går igen på tværs af varianterne og i den rene test.")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nGemt: {REPORT}")


if __name__ == "__main__":
    main()

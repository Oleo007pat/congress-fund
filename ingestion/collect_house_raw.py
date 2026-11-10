"""
collect_house_raw.py
--------------------
Henter RÅ data fra Repræsentanternes Hus (House Clerk): årsindeks og alle PTR-PDF'er
(Periodic Transaction Reports). PDF'erne bliver IKKE læst her, kun gemt på disken.

Kør (fra projektmappen, eller hvor som helst):
    python3 ingestion/collect_house_raw.py 2016-2026       # alle år
    python3 ingestion/collect_house_raw.py 2024            # ét år
    python3 ingestion/collect_house_raw.py 2016 20         # prøvekørsel: max 20 PDF'er pr. år

Lang kørsel på Mac, så computeren ikke går i dvale:
    caffeinate -i python3 ingestion/collect_house_raw.py 2016-2026

Rart at vide:
  - Kan stoppes (Ctrl-C) og køres igen: PDF'er, der allerede er hentet og er gyldige, springes over.
  - Én PDF hentes ad gangen med ca. 0,3 s pause, så Husets server ikke belastes.
  - Filer: data/raw/index/ (årsindeks), data/raw/ptr_index.csv (samlet indeks),
    data/raw/ptr/{år}/{doc_id}.pdf (PDF'erne), data/raw/download_log.csv (alt der manglede/fejlede).
  - 2021-2026 har bekræftede webadresser. 2016-2020 er ikke afprøvet; afvigelser (fx 404) logges,
    og kørslen fortsætter.

Kræver:  pip3 install requests
"""

from __future__ import annotations

import csv
import io
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

BASE = "https://disclosures-clerk.house.gov/public_disc"
ROOT = Path(__file__).resolve().parent.parent   # projektmappen

USER_AGENT = "congress-fund-research/0.1"
PAUSE_SECONDS = 0.3        # pause efter hver download
TIMEOUT = 60               # sekunder pr. forespørgsel
ATTEMPTS = 3               # forsøg pr. fil
BACKOFF_SECONDS = 2.0      # ventetid før forsøg 2; fordobles derefter (2 s, 4 s)
PROGRESS_EVERY = 50

INDEX_FIELDS = ["year", "doc_id", "first", "last", "prefix", "suffix",
                "state_dst", "filing_date", "pdf_path", "status"]
LOG_FIELDS = ["year", "doc_id", "url", "http_status_or_error", "timestamp"]

RETRY_STATUS = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- stier

class Paths:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.raw = self.root / "data" / "raw"
        self.index_dir = self.raw / "index"
        self.ptr_dir = self.raw / "ptr"
        self.ptr_index = self.raw / "ptr_index.csv"
        self.log = self.raw / "download_log.csv"

    def pdf(self, year: int, doc_id: str) -> Path:
        return self.ptr_dir / str(year) / f"{doc_id}.pdf"

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()


def index_url(year: int) -> str:
    return f"{BASE}/financial-pdfs/{year}FD.zip"


def pdf_url(year: int, doc_id: str) -> str:
    return f"{BASE}/ptr-pdfs/{year}/{doc_id}.pdf"


# --------------------------------------------------------------------------- indeks

def iso_date(value: str | None) -> str:
    """Indeksets FilingDate står som M/D/YYYY (fx '1/2/2024'). Returnerer 'YYYY-MM-DD' eller ''."""
    try:
        m, d, y = (int(x) for x in (value or "").strip().split("/"))
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def parse_index_xml(xml_bytes: bytes) -> list[dict]:
    """Returnerer kun PTR-indberetninger (FilingType 'P'), uden dubletter af doc_id."""
    root = ET.fromstring(xml_bytes)
    out, seen = [], set()
    for m in root.iter("Member"):
        def t(tag: str) -> str:
            return (m.findtext(tag) or "").strip()
        if t("FilingType") != "P":
            continue
        doc_id = t("DocID")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append({
            "doc_id": doc_id, "first": t("First"), "last": t("Last"),
            "prefix": t("Prefix"), "suffix": t("Suffix"), "state_dst": t("StateDst"),
            "filing_date": iso_date(t("FilingDate")),
        })
    return out


def extract_xml(zip_bytes: bytes, year: int) -> tuple[str, bytes]:
    """Finder XML-filen i zippen. Kaster en fejl, hvis zippen er ugyldig."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise ValueError("ingen XML i zip-filen")
        name = next((n for n in names if n.lower() == f"{year}fd.xml"), names[0])
        return name, zf.read(name)


# --------------------------------------------------------------------------- filer

def atomic_write(path: Path, data: bytes) -> None:
    """Skriver til midlertidig fil og omdøber, så en afbrudt skrivning ikke efterlader en halv fil."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def valid_pdf(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            return fh.read(4) == b"%PDF"
    except OSError:
        return False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DownloadLog:
    def __init__(self, path: Path):
        self.path = path

    def add(self, year: int | str, doc_id: str, url: str, what: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(LOG_FIELDS)
            w.writerow([year, doc_id, url, what, now_iso()])


# --------------------------------------------------------------------------- hentning

def get_with_retry(session: requests.Session, url: str) -> tuple[bytes | None, str]:
    """Returnerer (indhold, "200") ved succes, ellers (None, httpstatus eller fejltype).
    Prøver op til ATTEMPTS gange med stigende ventetid ved netværksfejl, 429 og 5xx.
    Øvrige fejl (fx 404, 403) prøves ikke igen."""
    result = "ukendt_fejl"
    for attempt in range(1, ATTEMPTS + 1):
        retry = False
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.content, "200"
            result = str(resp.status_code)
            retry = resp.status_code in RETRY_STATUS
        except requests.RequestException as exc:
            result = type(exc).__name__
            retry = True
        if not retry or attempt == ATTEMPTS:
            break
        time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    return None, result


def load_year_index(session, paths: Paths, log: DownloadLog, year: int) -> list[dict] | None:
    """Henter årsindekset (og gemmer zip + XML). Falder tilbage på en tidligere hentet kopi.
    Returnerer None, hvis der hverken kan hentes eller findes et indeks."""
    url = index_url(year)
    zip_path = paths.index_dir / f"{year}FD.zip"
    content, status = get_with_retry(session, url)
    time.sleep(PAUSE_SECONDS)

    if content is not None:
        try:
            xml_name, xml_bytes = extract_xml(content, year)
            parse_index_xml(xml_bytes)          # tjek at XML kan læses, før noget gemmes
            atomic_write(zip_path, content)
            atomic_write(paths.index_dir / f"{year}FD.xml", xml_bytes)
            return parse_index_xml(xml_bytes)
        except (zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
            status = f"ugyldigt_indeks:{type(exc).__name__}"

    log.add(year, "INDEX", url, status)
    if zip_path.is_file():
        try:
            _, xml_bytes = extract_xml(zip_path.read_bytes(), year)
            print(f"  {year}: kunne ikke hente indekset ({status}); bruger tidligere gemt kopi.")
            return parse_index_xml(xml_bytes)
        except (zipfile.BadZipFile, ET.ParseError, ValueError, OSError):
            pass
    return None


def read_ptr_index(path: Path) -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows[(r["year"], r["doc_id"])] = r
    return rows


def write_ptr_index(path: Path, rows: dict[tuple[str, str], dict]) -> None:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=INDEX_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for key in sorted(rows, key=lambda k: (int(k[0]), k[1])):
        w.writerow(rows[key])
    atomic_write(path, buf.getvalue().encode("utf-8"))


def collect_year(session, paths: Paths, log: DownloadLog, year: int, filings: list[dict],
                 limit: int | None, index_rows: dict) -> dict:
    stats = {"indeks": len(filings), "hentet": 0, "fandtes": 0, "fejlet": 0}

    # Indekset opdateres for alle PTR'er i året (ingen dubletter: nøgle = år + doc_id)
    for f in filings:
        p = paths.pdf(year, f["doc_id"])
        key = (str(year), f["doc_id"])
        old = index_rows.get(key, {})
        status = "ok" if valid_pdf(p) else (old.get("status") if str(old.get("status", "")).startswith("fejlet")
                                            else "ikke_hentet")
        index_rows[key] = {"year": year, **f, "pdf_path": paths.rel(p), "status": status}

    todo = filings[:limit] if limit else filings
    for i, f in enumerate(todo, 1):
        doc_id = f["doc_id"]
        p = paths.pdf(year, doc_id)
        row = index_rows[(str(year), doc_id)]
        if valid_pdf(p):
            stats["fandtes"] += 1
            row["status"] = "ok"
        else:
            url = pdf_url(year, doc_id)
            content, status = get_with_retry(session, url)
            if content is not None and not content.startswith(b"%PDF"):
                content, status = None, "ikke_pdf"
            if content is None:
                stats["fejlet"] += 1
                row["status"] = "fejlet"
                log.add(year, doc_id, url, status)
            else:
                try:
                    atomic_write(p, content)
                    stats["hentet"] += 1
                    row["status"] = "ok"
                except OSError as exc:
                    stats["fejlet"] += 1
                    row["status"] = "fejlet"
                    log.add(year, doc_id, url, f"skrivefejl:{type(exc).__name__}")
            time.sleep(PAUSE_SECONDS)
        if i % PROGRESS_EVERY == 0:
            print(f"  {year}: {i}/{len(todo)} behandlet ({stats['hentet']} hentet, "
                  f"{stats['fandtes']} fandtes, {stats['fejlet']} fejlet)")
    return stats


def run(years: list[int], limit: int | None = None, root: Path = ROOT,
        session: requests.Session | None = None) -> dict[int, dict | None]:
    paths = Paths(root)
    log = DownloadLog(paths.log)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
    index_rows = read_ptr_index(paths.ptr_index)
    results: dict[int, dict | None] = {}
    try:
        for year in years:
            print(f"{year}: henter indeks ...")
            filings = load_year_index(session, paths, log, year)
            if filings is None:
                print(f"  {year}: intet indeks tilgængeligt, springer året over (se {paths.log.name}).")
                results[year] = None
                continue
            print(f"  {year}: {len(filings)} PTR-indberetninger i indekset.")
            results[year] = collect_year(session, paths, log, year, filings, limit, index_rows)
            write_ptr_index(paths.ptr_index, index_rows)   # gem løbende efter hvert år
    except KeyboardInterrupt:
        print("\nAfbrudt. Kør kommandoen igen for at fortsætte, hvor du slap.")
    finally:
        if index_rows:
            write_ptr_index(paths.ptr_index, index_rows)
    return results


def print_summary(results: dict[int, dict | None], paths: Paths) -> None:
    print("\nOversigt pr. år")
    print(f"{'År':<6}{'I indeks':>10}{'Hentet nu':>11}{'Fandtes':>9}{'Fejlet':>8}")
    tot = {"indeks": 0, "hentet": 0, "fandtes": 0, "fejlet": 0}
    for year, s in results.items():
        if s is None:
            print(f"{year:<6}{'indeks mangler':>38}")
            continue
        print(f"{year:<6}{s['indeks']:>10}{s['hentet']:>11}{s['fandtes']:>9}{s['fejlet']:>8}")
        for k in tot:
            tot[k] += s[k]
    print(f"{'Ialt':<6}{tot['indeks']:>10}{tot['hentet']:>11}{tot['fandtes']:>9}{tot['fejlet']:>8}")
    if tot["fejlet"] or any(s is None for s in results.values()):
        print(f"\nNogle filer mangler eller fejlede. Se {paths.log}")
    print(f"Samlet indeks: {paths.ptr_index}")


def parse_years(arg: str) -> list[int]:
    if "-" in arg:
        a, b = (int(x) for x in arg.split("-"))
        if a > b:
            raise ValueError
        return list(range(a, b + 1))
    return [int(arg)]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    usage = ("Brug: python3 ingestion/collect_house_raw.py 2016-2026   (eller ét år, fx 2024)\n"
             "     Valgfrit maks-antal PDF'er pr. år til prøvekørsel: ... 2016 20")
    if not argv or len(argv) > 2:
        print(usage)
        return 2
    try:
        years = parse_years(argv[0])
        limit = int(argv[1]) if len(argv) > 1 else None
        if limit is not None and limit < 1:
            raise ValueError
    except ValueError:
        print(usage)
        return 2
    results = run(years, limit)
    print_summary(results, Paths(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

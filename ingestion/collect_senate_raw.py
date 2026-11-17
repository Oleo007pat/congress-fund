"""
collect_senate_raw.py
---------------------
Henter RÅ data fra Senatets offentlige eFD-system (https://efdsearch.senate.gov): alle
Periodic Transaction Reports (PTR, rapporttype 11). Siderne bliver IKKE tolket her, kun gemt.

Kør (fra projektmappen, eller hvor som helst):
    python3 ingestion/collect_senate_raw.py --probe            # først: lille test (år 2024)
    python3 ingestion/collect_senate_raw.py --probe 2022       # lille test for et andet år
    python3 ingestion/collect_senate_raw.py 2016-2026          # alle år
    python3 ingestion/collect_senate_raw.py 2024               # ét år
    python3 ingestion/collect_senate_raw.py 2016 20            # prøvekørsel: max 20 rapporter pr. år

Lang kørsel på Mac, så computeren ikke går i dvale:
    caffeinate -i python3 ingestion/collect_senate_raw.py 2016-2026

VIGTIGT - brugsbetingelser:
  eFD kræver, at man erklærer: "I understand the prohibitions on obtaining and use of financial
  disclosure reports." Scriptet viser forbuddene (dansk + engelsk) og sender KUN aftalen, hvis du
  selv skriver "ja". Der findes intet flag, der springer det over. Vurdér selv, om din brug er tilladt.

Rart at vide:
  - Kan stoppes (Ctrl-C) og køres igen: filer, der allerede er hentet, springes over.
  - Én forespørgsel ad gangen med 1 s pause. Ved HTTP 429/503 ventes længere.
  - Filer: data/raw/senate/ptr/{år}/{uuid}.html (elektroniske rapporter),
    data/raw/senate/paper/{år}/{uuid}.html + data/raw/senate/paper/{år}/{uuid}/page_N.ext
    (scannede papirrapporter), data/raw/senate_index.csv (samlet indeks).
  - Sidestrukturen er skrevet efter dokumentationen og er ikke afprøvet mod det rigtige site.
    Afviger svarene, stopper scriptet med en tydelig fejl og de første 500 tegn af svaret.

Kræver:  pip3 install requests
"""

from __future__ import annotations

import csv
import html as htmllib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://efdsearch.senate.gov"
HOME_PATH = "/search/home/"
SEARCH_PAGE_PATH = "/search/"
DATA_PATH = "/search/report/data/"
ROOT = Path(__file__).resolve().parent.parent   # projektmappen

USER_AGENT = "congress-fund-research/0.1"
PAUSE_SECONDS = 1.0        # pause efter hver forespørgsel
TIMEOUT = 60               # sekunder pr. forespørgsel
ATTEMPTS = 3               # forsøg pr. forespørgsel
BACKOFF_SECONDS = 2.0      # ventetid før forsøg 2; fordobles derefter
LONG_WAIT_SECONDS = 30.0   # ventetid efter 429/503 (Retry-After respekteres, max MAX_WAIT_SECONDS)
MAX_WAIT_SECONDS = 300.0
PAGE_SIZE = 100
PROGRESS_EVERY = 50
PROBE_REPORTS = 5
PROBE_DEFAULT_YEAR = 2024
PTR_REPORT_TYPE = 11

INDEX_FIELDS = ["year", "uuid", "kind", "first", "last", "office", "title", "is_amendment",
                "filing_date", "url", "html_path", "n_images", "status"]

RETRY_STATUS = {429, 500, 502, 503, 504}
SLOW_DOWN_STATUS = {429, 503}

AGREEMENT_STATEMENT = ("I understand the prohibitions on obtaining and use of financial "
                       "disclosure reports.")

PROHIBITIONS_EN = [
    "any commercial purpose, other than by news and communications media for dissemination "
    "to the general public;",
    "determining or establishing the credit rating of any individual; or",
    "use, directly or indirectly, in the solicitation of money for any political, charitable, "
    "or other purpose.",
]
PROHIBITIONS_DA = [
    "ethvert kommercielt formål, bortset fra nyheds- og kommunikationsmedier, der videregiver "
    "oplysningerne til offentligheden;",
    "at fastsætte eller fastslå en persons kreditvurdering; eller",
    "direkte eller indirekte brug til at anmode om penge til politiske, velgørende eller andre formål.",
]


# --------------------------------------------------------------------------- fejltyper

class SiteError(Exception):
    """Sitet svarer anderledes end forventet (eller blokerer). Kørslen stopper."""

    def __init__(self, message: str, snippet: str = ""):
        super().__init__(message)
        self.message = message
        self.snippet = snippet


class FetchError(Exception):
    """En enkelt forespørgsel fejlede efter alle forsøg (netværk eller HTTP-status)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- stier

class Paths:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.raw = self.root / "data" / "raw"
        self.senate = self.raw / "senate"
        self.index = self.raw / "senate_index.csv"

    def html(self, kind: str, year: int, uuid: str) -> Path:
        return self.senate / kind / str(year) / f"{uuid}.html"

    def images_dir(self, year: int, uuid: str) -> Path:
        return self.senate / "paper" / str(year) / uuid

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()


# --------------------------------------------------------------------------- tekst og parsing

TAG_RE = re.compile(r"<[^>]*>")
CSRF_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ANCHOR_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
VIEW_RE = re.compile(r"/search/view/(ptr|paper)/([0-9A-Za-z-]+)/?", re.I)
IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I | re.S)
IMAGE_EXTS = {".gif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def strip_tags(value: object) -> str:
    return " ".join(htmllib.unescape(TAG_RE.sub(" ", str(value or ""))).split())


def iso_date(value: str | None) -> str:
    """eFD skriver datoer som MM/DD/YYYY (evt. med klokkeslæt efter). Returnerer 'YYYY-MM-DD' eller ''."""
    try:
        m, d, y = (int(x) for x in (value or "").strip().split()[0].split("/"))
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except (ValueError, AttributeError, IndexError):
        return ""


def extract_csrf_token(html: str) -> str | None:
    """Finder værdien af <input name="csrfmiddlewaretoken" value="..."> uanset attributrækkefølge."""
    for tag in CSRF_INPUT_RE.findall(html or ""):
        if re.search(r"""\bname\s*=\s*["']csrfmiddlewaretoken["']""", tag, re.I):
            m = re.search(r"""\bvalue\s*=\s*["']([^"']*)["']""", tag, re.I)
            if m and m.group(1):
                return m.group(1)
    return None


def parse_row(row: object) -> dict:
    """Én række fra søgesvaret -> dict. Kaster SiteError, hvis formen ikke er som forventet."""
    if not isinstance(row, (list, tuple)) or len(row) < 5 or not isinstance(row[3], str):
        raise SiteError("En række i søgesvaret har ikke den forventede form "
                        "[fornavn, efternavn, kontor, link, dato].", repr(row)[:500])
    anchor = ANCHOR_RE.search(row[3])
    view = VIEW_RE.search(anchor.group(1)) if anchor else None
    if not anchor or not view:
        raise SiteError("Rækkens link (/search/view/ptr/... eller /search/view/paper/...) kunne ikke "
                        "findes.", repr(row)[:500])
    kind = view.group(1).lower()
    uuid = view.group(2)
    title = strip_tags(anchor.group(2))
    return {
        "uuid": uuid,
        "kind": kind,
        "first": strip_tags(row[0]),
        "last": strip_tags(row[1]),
        "office": strip_tags(row[2]),
        "title": title,
        "is_amendment": "true" if re.search(r"\(\s*amendment", title, re.I) else "false",
        "filing_date": iso_date(strip_tags(row[4])),
        "url": f"{BASE}/search/view/{kind}/{uuid}/",
    }


def parse_search_json(text: str) -> tuple[int, list[dict]]:
    """Søgesvar (JSON) -> (recordsFiltered, rækker). Kaster SiteError ved afvigende form."""
    try:
        obj = json.loads(text)
    except ValueError:
        raise SiteError("Søgesvaret er ikke JSON.", (text or "")[:500])
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
        raise SiteError("Søgesvaret mangler feltet 'data' (en liste).", (text or "")[:500])
    total = obj.get("recordsFiltered", obj.get("recordsTotal"))
    if not isinstance(total, int) or isinstance(total, bool):
        raise SiteError("Søgesvaret mangler 'recordsFiltered'/'recordsTotal' (heltal).", (text or "")[:500])
    return total, [parse_row(r) for r in obj["data"]]


def extract_image_urls(html: str, page_url: str) -> list[str]:
    """Sidebilleder på en papirrapport: <img src> under eFD's /media/-sti, i rækkefølge, uden dubletter."""
    base_host = urlparse(BASE).netloc
    out: list[str] = []
    for src in IMG_RE.findall(html or ""):
        full = urljoin(page_url, htmllib.unescape(src.strip()))
        u = urlparse(full)
        if u.netloc != base_host or "/media/" not in u.path:
            continue
        if full not in out:
            out.append(full)
    return out


def image_ext(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in IMAGE_EXTS else ".gif"


def looks_like_image(content: bytes, content_type: str = "") -> bool:
    head = content[:16].lstrip().lower()
    if not content or head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return False
    if (content_type or "").lower().startswith("image/"):
        return True
    return content.startswith((b"GIF8", b"\xff\xd8", b"\x89PNG", b"II*\x00", b"MM\x00*"))


def is_agreement_page(resp) -> bool:
    """True, hvis svaret er aftalesiden (sessionen er udløbet eller aftalen mangler)."""
    path = urlparse(getattr(resp, "url", "") or "").path.rstrip("/")
    if path.endswith("/search/home"):
        return True
    ctype = (resp.headers.get("Content-Type", "") or "").lower()
    if ctype and "html" not in ctype and "json" not in ctype and "text" not in ctype:
        return False
    return "prohibition_agreement" in (resp.text or "")


# --------------------------------------------------------------------------- filer

def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def valid_html(path: Path) -> bool:
    """Gyldig = findes, ikke tom og er ikke aftalesiden."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        return "prohibition_agreement" not in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def existing_images(images_dir: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    if images_dir.is_dir():
        for p in images_dir.iterdir():
            m = re.fullmatch(r"page_(\d+)\.[A-Za-z0-9]+", p.name)
            if m and p.is_file() and p.stat().st_size > 0:
                found[int(m.group(1))] = p
    return found


# --------------------------------------------------------------------------- indeks

def read_index(path: Path) -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows[(r["year"], r["uuid"])] = r
    return rows


def write_index(path: Path, rows: dict[tuple[str, str], dict]) -> None:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=INDEX_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for key in sorted(rows, key=lambda k: (int(k[0]), rows[k].get("filing_date", ""), k[1])):
        w.writerow(rows[key])
    atomic_write(path, buf.getvalue().encode("utf-8"))


# --------------------------------------------------------------------------- aftale (kræver "ja")

def ask_agreement(input_fn=input, print_fn=print) -> bool:
    """Viser forbuddene og returnerer True KUN hvis brugeren selv skriver 'ja'."""
    print_fn("\n" + "=" * 78)
    print_fn("Senatets eFD kræver, at du erklærer følgende:")
    print_fn(f'  "{AGREEMENT_STATEMENT}"')
    print_fn("\nForbudt brug af indberetningerne (lovbestemt):")
    print_fn("  På dansk:")
    for t in PROHIBITIONS_DA:
        print_fn(f"    - {t}")
    print_fn("  In English:")
    for t in PROHIBITIONS_EN:
        print_fn(f"    - {t}")
    print_fn("\nScriptet accepterer ikke automatisk. Det sender kun aftalen til eFD på dine vegne,")
    print_fn("hvis du selv skriver ja. Vurdér selv, om din brug er tilladt.")
    print_fn("=" * 78)
    try:
        answer = input_fn('Skriv "ja" for at bekræfte, at du forstår og vil overholde forbuddene '
                          '(alt andet afbryder): ')
    except (EOFError, KeyboardInterrupt):
        return False
    return str(answer).strip().lower() == "ja"


# --------------------------------------------------------------------------- klient

class Client:
    """Holder sessionen. consented bliver kun True, når brugeren selv har skrevet 'ja'."""

    def __init__(self, session):
        self.session = session
        self.consented = False
        self.form_token = ""

    # -- lavniveau med forsøg
    def request(self, method: str, url: str, **kw):
        last = "ukendt_fejl"
        for attempt in range(1, ATTEMPTS + 1):
            wait = BACKOFF_SECONDS * 2 ** (attempt - 1)
            try:
                resp = self.session.request(method, url, timeout=TIMEOUT, **kw)
            except requests.RequestException as exc:
                last = type(exc).__name__
            else:
                if resp.status_code == 200:
                    time.sleep(PAUSE_SECONDS)
                    return resp
                if resp.status_code == 403:
                    raise SiteError(
                        "Sitet svarede HTTP 403 (forbudt). Enten blokerer eFD forespørgslerne, eller også "
                        "er aftale-/CSRF-flowet anderledes end antaget. Scriptet stopper.",
                        (resp.text or "")[:500])
                last = str(resp.status_code)
                if resp.status_code not in RETRY_STATUS:
                    break
                if resp.status_code in SLOW_DOWN_STATUS:
                    try:
                        ra = float(resp.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        ra = LONG_WAIT_SECONDS
                    wait = max(wait, min(ra, MAX_WAIT_SECONDS))
            if attempt == ATTEMPTS:
                break
            if last in ("429", "503"):
                print(f"  Sitet beder os vente ({last}); venter {wait:.0f} s ...")
            time.sleep(wait)
        raise FetchError(last)

    def csrf_cookie(self) -> str:
        try:
            return self.session.cookies.get("csrftoken") or ""
        except Exception:
            return ""

    # -- aftalen
    def agree(self) -> None:
        """Henter aftalesiden og sender aftalen. Må KUN kaldes, når consented er True."""
        if not self.consented:
            raise RuntimeError("agree() kaldt uden at brugeren har skrevet ja")
        home = BASE + HOME_PATH
        resp = self.request("GET", home)
        token = extract_csrf_token(resp.text)
        if not token:
            raise SiteError("Fandt ikke 'csrfmiddlewaretoken' på aftalesiden.", (resp.text or "")[:500])
        if not self.csrf_cookie():
            raise SiteError("Sitet satte ingen 'csrftoken'-cookie.", (resp.text or "")[:500])
        resp = self.request("POST", home, data={"prohibition_agreement": "1",
                                                "csrfmiddlewaretoken": token},
                            headers={"Referer": home})
        if is_agreement_page(resp):
            raise SiteError("Aftalen blev ikke accepteret af sitet (vi er stadig på aftalesiden).",
                            (resp.text or "")[:500])
        self.form_token = extract_csrf_token(resp.text) or token

    def start_interactive(self, input_fn=input) -> bool:
        """Spørger brugeren. Returnerer False (og sender intet) uden et skrevet 'ja'."""
        if not ask_agreement(input_fn):
            return False
        self.consented = True
        self.agree()
        return True

    # -- forespørgsler med automatisk gen-aftale (kun efter brugerens 'ja' i denne kørsel)
    def checked(self, method: str, url: str, build=None):
        for attempt in (1, 2):
            kw = build() if build else {}
            resp = self.request(method, url, **kw)
            if not is_agreement_page(resp):
                return resp
            if attempt == 2 or not self.consented:
                raise SiteError("Sessionen er udløbet, og aftalen kunne ikke fornyes.",
                                (resp.text or "")[:500])
            print("  Sessionen udløb; fornyer aftalen (du har allerede skrevet ja i denne kørsel) ...")
            self.agree()
        raise AssertionError  # pragma: no cover

    def search_page(self, year: int, start: int) -> tuple[int, list[dict]]:
        def build():
            tok = self.csrf_cookie()
            return {
                "data": {
                    "start": str(start), "length": str(PAGE_SIZE),
                    "report_types": f"[{PTR_REPORT_TYPE}]", "filer_types": "[]",
                    "submitted_start_date": f"01/01/{year} 00:00:00",
                    "submitted_end_date": f"12/31/{year} 23:59:59",
                    "candidate_state": "", "senator_state": "", "office_id": "",
                    "first_name": "", "last_name": "",
                    "csrfmiddlewaretoken": self.form_token or tok,
                },
                "headers": {"Referer": BASE + SEARCH_PAGE_PATH, "X-CSRFToken": tok,
                            "X-Requested-With": "XMLHttpRequest"},
            }
        resp = self.checked("POST", BASE + DATA_PATH, build)
        return parse_search_json(resp.text)


def start_client(session, input_fn=input) -> Client | None:
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
    client = Client(session)
    return client if client.start_interactive(input_fn) else None


def search_year(client: Client, year: int, first_page_only: bool = False) -> tuple[int, list[dict]]:
    """Alle PTR'er indleveret i året (eller kun første side). Returnerer (antal ifølge sitet, rapporter)."""
    reports: list[dict] = []
    seen: set[str] = set()
    start, total = 0, 0
    while True:
        total, rows = client.search_page(year, start)
        for r in rows:
            if r["uuid"] not in seen:
                seen.add(r["uuid"])
                reports.append({"year": year, **r})
        if first_page_only or not rows:
            break
        start += len(rows)
        if start >= total:
            break
    return total, reports


# --------------------------------------------------------------------------- hentning

def download_report(client: Client, paths: Paths, r: dict) -> dict:
    """Henter én rapport (HTML, og for papir også billederne). Springer eksisterende filer over.
    Returnerer {'outcome': hentet|fandtes|fejlet, 'status': ok|failed:..., 'n_images': int}."""
    year, uuid, kind = int(r["year"]), r["uuid"], r["kind"]
    p = paths.html(kind, year, uuid)
    downloaded = False
    if not valid_html(p):
        try:
            resp = client.checked("GET", r["url"])
            if not resp.content:
                return {"outcome": "fejlet", "status": "failed:tomt_svar", "n_images": 0}
            atomic_write(p, resp.content)
            downloaded = True
        except FetchError as exc:
            return {"outcome": "fejlet", "status": f"failed:{exc.reason}", "n_images": 0}
        except OSError as exc:
            return {"outcome": "fejlet", "status": f"failed:skrivefejl_{type(exc).__name__}", "n_images": 0}

    if kind != "paper":
        return {"outcome": "hentet" if downloaded else "fandtes", "status": "ok", "n_images": 0}

    text = p.read_text(encoding="utf-8", errors="replace")
    urls = extract_image_urls(text, r["url"])
    if not urls:
        return {"outcome": "fejlet", "status": "failed:ingen_billeder_fundet", "n_images": 0}
    img_dir = paths.images_dir(year, uuid)
    first_error = ""
    for n, url in enumerate(urls, 1):
        if n in existing_images(img_dir):
            continue
        try:
            resp = client.checked("GET", url)
            if not looks_like_image(resp.content, resp.headers.get("Content-Type", "")):
                first_error = first_error or f"billede_{n}_ikke_et_billede"
                continue
            atomic_write(img_dir / f"page_{n}{image_ext(url)}", resp.content)
            downloaded = True
        except FetchError as exc:
            first_error = first_error or f"billede_{n}_{exc.reason}"
        except OSError as exc:
            first_error = first_error or f"billede_{n}_skrivefejl_{type(exc).__name__}"
    n_images = len(existing_images(img_dir))
    if first_error:
        return {"outcome": "fejlet", "status": f"failed:{first_error}", "n_images": n_images}
    return {"outcome": "hentet" if downloaded else "fandtes", "status": "ok", "n_images": n_images}


def baseline_row(paths: Paths, r: dict, old: dict) -> dict:
    """Indeksrække for en rapport, før/uden at den hentes i denne kørsel."""
    year, uuid, kind = int(r["year"]), r["uuid"], r["kind"]
    p = paths.html(kind, year, uuid)
    n_images = len(existing_images(paths.images_dir(year, uuid))) if kind == "paper" else 0
    if kind == "ptr" and valid_html(p):
        status = "ok"
    else:
        old_status = old.get("status", "")
        status = old_status if old_status else "ikke_hentet"
    return {**{k: r.get(k, "") for k in INDEX_FIELDS}, "year": year,
            "html_path": paths.rel(p), "n_images": n_images, "status": status}


def collect_year(client: Client, paths: Paths, year: int, reports: list[dict], limit: int | None,
                 index_rows: dict) -> dict:
    stats = {"rapporter": len(reports),
             "elektroniske": sum(1 for r in reports if r["kind"] == "ptr"),
             "papir": sum(1 for r in reports if r["kind"] == "paper"),
             "hentet": 0, "fandtes": 0, "fejlet": 0}
    for r in reports:
        key = (str(year), r["uuid"])
        index_rows[key] = baseline_row(paths, r, index_rows.get(key, {}))

    todo = reports[:limit] if limit else reports
    for i, r in enumerate(todo, 1):
        res = download_report(client, paths, r)
        stats[res["outcome"]] += 1
        row = index_rows[(str(year), r["uuid"])]
        row["status"], row["n_images"] = res["status"], res["n_images"]
        if i % PROGRESS_EVERY == 0:
            print(f"  {year}: {i}/{len(todo)} behandlet ({stats['hentet']} hentet, "
                  f"{stats['fandtes']} fandtes, {stats['fejlet']} fejlet)")
            write_index(paths.index, index_rows)
    return stats


def report_site_error(exc: SiteError) -> None:
    print(f"\nFEJL: {exc.message}")
    if exc.snippet:
        print("Det rå svar (de første 500 tegn):")
        print(exc.snippet[:500])
    print("Scriptet stopper her og gætter ikke videre. Send ovenstående til den, der vedligeholder scriptet.")


def run(years: list[int], limit: int | None = None, root: Path = ROOT, session=None,
        input_fn=input) -> tuple[dict[int, dict | None], bool]:
    """Returnerer (resultater pr. år, ok). ok=False hvis brugeren sagde nej, eller kørslen blev stoppet af en fejl."""
    paths = Paths(root)
    results: dict[int, dict | None] = {}
    index_rows = read_index(paths.index)
    ok = True
    try:
        try:
            client = start_client(session, input_fn)
        except SiteError as exc:
            report_site_error(exc)
            return results, False
        except FetchError as exc:
            print(f"\nFEJL: kunne ikke nå eFD ({exc.reason}). Prøv igen senere.")
            return results, False
        if client is None:
            print("\nIngen aftale (du skrev ikke ja). Der er ikke sendt noget til eFD, og intet er hentet.")
            return results, False
        for year in years:
            print(f"{year}: henter søgeresultater ...")
            try:
                total, reports = search_year(client, year)
            except FetchError as exc:
                print(f"\nFEJL: søgningen for {year} fejlede ({exc.reason}). Stopper; kør igen for at fortsætte.")
                results[year] = None
                ok = False
                break
            except SiteError as exc:
                report_site_error(exc)
                results[year] = None
                ok = False
                break
            print(f"  {year}: {len(reports)} PTR-indberetninger fundet (sitet siger {total}).")
            try:
                results[year] = collect_year(client, paths, year, reports, limit, index_rows)
            except SiteError as exc:
                report_site_error(exc)
                results[year] = None
                ok = False
                break
            write_index(paths.index, index_rows)
    except KeyboardInterrupt:
        print("\nAfbrudt. Kør kommandoen igen for at fortsætte, hvor du slap.")
    finally:
        if index_rows:
            write_index(paths.index, index_rows)
    return results, ok


def print_summary(results: dict[int, dict | None], paths: Paths) -> None:
    print("\nOversigt pr. år")
    print(f"{'År':<6}{'Rapporter':>10}{'Elektr.':>9}{'Papir':>7}{'Hentet nu':>11}{'Fandtes':>9}{'Fejlet':>8}")
    keys = ["rapporter", "elektroniske", "papir", "hentet", "fandtes", "fejlet"]
    tot = {k: 0 for k in keys}
    for year, s in results.items():
        if s is None:
            print(f"{year:<6}{'søgning mislykkedes':>30}")
            continue
        print(f"{year:<6}{s['rapporter']:>10}{s['elektroniske']:>9}{s['papir']:>7}"
              f"{s['hentet']:>11}{s['fandtes']:>9}{s['fejlet']:>8}")
        for k in keys:
            tot[k] += s[k]
    print(f"{'Ialt':<6}{tot['rapporter']:>10}{tot['elektroniske']:>9}{tot['papir']:>7}"
          f"{tot['hentet']:>11}{tot['fandtes']:>9}{tot['fejlet']:>8}")
    if tot["fejlet"] or any(s is None for s in results.values()):
        print(f"\nNogle rapporter fejlede eller mangler. Se kolonnen 'status' i {paths.index}")
    print(f"Samlet indeks: {paths.index}")


# --------------------------------------------------------------------------- probe

def probe(year: int, root: Path = ROOT, session=None, input_fn=input) -> bool:
    """Lille test: aftale, første søgeside for ét år og de første 5 rapporter. Skriver intet indeks."""
    paths = Paths(root)
    try:
        client = start_client(session, input_fn)
        if client is None:
            print("\nIngen aftale (du skrev ikke ja). Der er ikke sendt noget til eFD.")
            return False
        print(f"\nPROBE for {year}: henter første side af søgeresultaterne ...")
        total, reports = search_year(client, year, first_page_only=True)
        kinds = {}
        for r in reports:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        amend = sum(1 for r in reports if r["is_amendment"] == "true")
        print(f"  Sitet siger {total} rapporter i alt for {year}; første side gav {len(reports)} "
              f"({kinds}, heraf {amend} rettelser).")
        for r in reports[:PROBE_REPORTS]:
            print(f"\n  - {r['first']} {r['last']} | {r['office']} | {r['title']}")
            print(f"    type={r['kind']} rettelse={r['is_amendment']} indleveret={r['filing_date'] or '?'} "
                  f"uuid={r['uuid']}")
            res = download_report(client, paths, {"year": year, **r})
            p = paths.html(r["kind"], year, r["uuid"])
            print(f"    resultat: {res['outcome']} / {res['status']}")
            if p.is_file():
                t = p.read_text(encoding="utf-8", errors="replace")
                print(f"    gemt: {paths.rel(p)} ({p.stat().st_size} bytes, {t.lower().count('<table')} tabeller, "
                      f"{t.lower().count('<tr')} rækker)")
                if r["kind"] == "paper":
                    print(f"    billeder fundet på siden: {len(extract_image_urls(t, r['url']))}, "
                          f"hentet: {res['n_images']}")
                else:
                    hits = [w for w in ("Ticker", "Transaction Date", "Asset", "Amount") if w in t]
                    print(f"    nøgleord fundet: {hits or 'ingen'}")
        print("\nProbe færdig. Ser ovenstående rigtigt ud, så kør hele hentningen.")
        return True
    except SiteError as exc:
        report_site_error(exc)
    except FetchError as exc:
        print(f"\nFEJL: kunne ikke nå eFD ({exc.reason}).")
    return False


# --------------------------------------------------------------------------- kommandolinje

def parse_years(arg: str) -> list[int]:
    if "-" in arg:
        a, b = (int(x) for x in arg.split("-"))
        if a > b:
            raise ValueError
        return list(range(a, b + 1))
    return [int(arg)]


USAGE = ("Brug: python3 ingestion/collect_senate_raw.py 2016-2026   (eller ét år, fx 2024)\n"
         "     Valgfrit maks-antal rapporter pr. år til prøvekørsel: ... 2016 20\n"
         "     Lille test først:  python3 ingestion/collect_senate_raw.py --probe [år]")


def main(argv: list[str] | None = None, input_fn=input, session=None, root: Path = ROOT) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--probe":
        try:
            if len(argv) > 2:
                raise ValueError
            year = int(argv[1]) if len(argv) > 1 else PROBE_DEFAULT_YEAR
        except ValueError:
            print(USAGE)
            return 2
        return 0 if probe(year, root, session, input_fn) else 1
    if not argv or len(argv) > 2:
        print(USAGE)
        return 2
    try:
        years = parse_years(argv[0])
        limit = int(argv[1]) if len(argv) > 1 else None
        if limit is not None and limit < 1:
            raise ValueError
    except ValueError:
        print(USAGE)
        return 2
    results, ok = run(years, limit, root, session, input_fn)
    if results:
        print_summary(results, Paths(root))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

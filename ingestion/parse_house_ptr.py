"""
parse_house_ptr.py
------------------
Læser de rå PTR-PDF'er (data/raw/ptr/{år}/{doc_id}.pdf, indeks: data/raw/ptr_index.csv) og laver én
tabel med handler.

Kør (fra projektmappen):
    python3 ingestion/parse_house_ptr.py                    # alle år i indekset
    python3 ingestion/parse_house_ptr.py 2016               # ét år
    python3 ingestion/parse_house_ptr.py 2016-2020          # interval
    python3 ingestion/parse_house_ptr.py 2016-2020 --max-seconds 140

Kan køres i bidder: tekstudtræk (det langsomme) caches pr. PDF, og --max-seconds stopper udtrækket pænt,
når tiden er brugt. Næste kørsel fortsætter, hvor den slap (cachede PDF'er springes over). Selve
parsingen af cachet tekst er hurtig (ca. 0,5 ms pr. indberetning) og køres altid for alle valgte år.
--force-text tvinger nyt udtræk af tekst (ignorerer cachen).

Cache (tekst):
    data/interim/text/{år}/{doc_id}.txt          pypdf's almindelige tekst (samme slags som data/legacy_text)
    data/interim/text_layout/{år}/{doc_id}.txt   pypdf's layout-tekst; kun for PDF'er, hvis skabelon ikke er
                                                 2021- (t2016 og ukendte skabeloner), se house_ptr_parser.py

Resultater (skrives ved hver kørsel; rækker for år, der IKKE er valgt, bevares uændret):
    data/processed/trades.csv         alle fundne handler
    data/processed/scanned.csv        PDF'er uden (næsten) tekstlag: scannet/håndskrevet
    data/processed/parse_report.csv   én række pr. indberetning i indekset (status, skabelon, advarsler)

Kolonner i trades.csv (nøgle: year + doc_id):
    year, doc_id, member (indeks: first + last), state_dst (indeks), owner (self/spouse/joint/dependent child),
    asset, ticker, asset_type, asset_type_source (code = kode i PDF'en, inferred = udledt af parseren,
    none), type (buy/sell/partial_sale/exchange), transaction_date, notification_date,
    filing_date (indekset: den dato, offentligheden tidligst kunne kende handlen), amount_low, amount_high,
    description, sub_holding, filing_status, option_like, short_related, include_v1, include_reason, template.

parse_report.csv: status = parsed / scanned / pdf_missing / not_processed (tiden gik, kør igen); 'sider' udfyldes kun for
scannede; 'warnings' samler parserens advarsler, kontrol mod indekset (navn, dato) og beløb uden øvre grænse.

include_v1 er sand for aktier og ETF'er med ticker (også ægtefællers og børns handler) og falsk for
optioner, obligationer, krypto og alt andet. Når skabelonen har en aktivtype-kode (2021-), bruges koden
(ST/ET) som før. Når den ikke har (2016), udledes typen af navn og ticker efter reglen i
house_ptr_parser.infer_asset_type; ved tvivl er handlen ikke med (include_reason indeholder 'tvivl').

Kræver: Python >= 3.10 og pypdf >= 4.0 (layout-udtræk; testet med 4.0.0 - 7.0.1). Kun standardbiblioteket ellers.
"""

from __future__ import annotations

import csv
import logging
import unicodedata
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # projektmappen
sys.path.insert(0, str(Path(__file__).resolve().parent))
import house_ptr_parser as hp  # noqa: E402

INDEX_PATH = ROOT / "data" / "raw" / "ptr_index.csv"
RAW_DIR = ROOT / "data" / "raw" / "ptr"
TEXT_DIR = ROOT / "data" / "interim" / "text"
LAYOUT_DIR = ROOT / "data" / "interim" / "text_layout"
OUT_DIR = ROOT / "data" / "processed"

SCANNED_MAX_CHARS = 150        # færre tegn (uden mellemrum) i hele PDF'en = scannet/håndskrevet
DATE_MISMATCH_WARN_DAYS = 30   # indeksets filing_date vs underskrevet dato i PDF'en

TRADE_FIELDS = ["year", "doc_id", "member", "state_dst", "owner", "asset", "ticker", "asset_type",
                "asset_type_source", "type", "transaction_date", "notification_date", "filing_date",
                "amount_low", "amount_high", "amount_flag", "description", "sub_holding", "location", "comments",
                "filing_status", "option_like", "short_related", "date_suspect", "duplicate_of", "deleted_by",
                "include_v1", "include_reason", "template", "parse_source"]
SCANNED_FIELDS = ["year", "doc_id", "member", "filing_date", "pdf_path", "sider"]
REPORT_FIELDS = ["year", "doc_id", "member", "filing_date", "signed_date", "status", "template", "parse_source",
                 "sider", "n_trades", "n_include_v1", "n_doubt", "n_date_suspect", "n_duplicate", "warnings", "pdf_path"]

logging.getLogger("pypdf").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- hjælpefunktioner

def fold(name: str) -> str:
    """Navn uden accenter og med små bogstaver (Sánchez = Sanchez), til sammenligning."""
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)).casefold()


def parse_years(arg: str) -> list[int]:
    m = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", arg.strip())
    if not m:
        raise ValueError(arg)
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if a > b:
        raise ValueError(arg)
    return list(range(a, b + 1))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8", errors="replace", newline="")
    os.replace(tmp, path)


def valid_pdf(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            return fh.read(4) == b"%PDF"
    except OSError:
        return False


def read_index() -> list[dict]:
    if not INDEX_PATH.is_file():
        raise SystemExit(f"Indekset mangler: {INDEX_PATH}")
    with INDEX_PATH.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if re.fullmatch(r"\d{4}", r.get("year", "")) and
                re.fullmatch(r"\d+", r.get("doc_id", ""))]
    rows.sort(key=lambda r: (int(r["year"]), r["doc_id"]))
    return rows


def pdf_path(row: dict) -> Path:
    return RAW_DIR / row["year"] / f"{row['doc_id']}.pdf"


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def require_layout_support() -> str:
    """Stopper med en klar besked, hvis pypdf mangler eller er for gammel til layout-udtræk. Returnerer versionen."""
    try:
        import inspect
        import pypdf
        ok = "extraction_mode" in inspect.signature(pypdf.PageObject.extract_text).parameters
    except ImportError:
        raise SystemExit("pypdf mangler (pip install pypdf). Tekstudtræk er ikke muligt; kun cachet tekst kan parses.")
    if not ok:
        raise SystemExit(f"pypdf {pypdf.__version__} er for gammel: layout-udtræk kræver pypdf >= 4.0 "
                         "(testet med 4.0.0, 4.3.1, 5.0.0, 5.9.0, 6.17.0 og 7.0.1).")
    return pypdf.__version__


def _layout_text(reader) -> str:
    parts = []
    for pg in reader.pages:
        try:
            parts.append(pg.extract_text(extraction_mode="layout") or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)


def extract_plain_and_layout(pdf: Path, want_layout: bool) -> tuple[str, str | None, int]:
    """pypdf-udtræk. Kaster en undtagelse, hvis PDF'en ikke kan åbnes."""
    import pypdf
    reader = pypdf.PdfReader(str(pdf))
    pages = len(reader.pages)
    plain_parts = []
    for pg in reader.pages:
        try:
            plain_parts.append(pg.extract_text() or "")
        except Exception as exc:  # én ødelagt side må ikke vælte resten
            plain_parts.append("")
            logging.getLogger(__name__).warning("%s: side kunne ikke læses (%s)", pdf.name, type(exc).__name__)
    return "\n".join(plain_parts), (_layout_text(reader) if want_layout else None), pages


def extract_layout_only(pdf: Path) -> str:
    """Kun layout-teksten (bruges, når den almindelige tekst allerede ligger i cachen)."""
    import pypdf
    return _layout_text(pypdf.PdfReader(str(pdf)))


def page_count(pdf: Path) -> int | str:
    try:
        import pypdf
        return len(pypdf.PdfReader(str(pdf)).pages)
    except Exception:
        return ""


# --------------------------------------------------------------------------- fase 1: tekstudtræk

LAYOUT_TEMPLATES = ("t2016", "unknown", "old_2021")   # skabeloner, hvor layout-teksten gemmes og bruges


def extract_phase(rows: list[dict], force: bool, deadline: float | None) -> dict:
    """
    Udtrækker og cacher tekst for PDF'er uden cache, indtil tiden er brugt. Returnerer statistik.
    1. PDF uden almindelig tekst i cachen: udtræk almindelig tekst (+ layout, hvis skabelonen bruger den).
    2. PDF med almindelig tekst, men uden layout-tekst, og med en skabelon der bruger den (t2016, ukendt, old_2021):
       udtræk KUN layout-teksten. Det er præcis de PDF'er, hvor en ældre cache (fra før old_2021 fik layout-parsing)
       mangler layout; 'layout_efterhentet' tæller dem. En tom layout-fil tæller som udført (ingen gentagelse).
    """
    st = {"udtrukket": 0, "layout_efterhentet": 0, "layout_mangler": 0, "i_cache": 0, "pdf_mangler": 0, "fejl": {},
          "ikke_naaet": 0}
    checked = False
    for row in rows:
        year, doc = row["year"], row["doc_id"]
        plain_cache = TEXT_DIR / year / f"{doc}.txt"
        layout_cache = LAYOUT_DIR / year / f"{doc}.txt"
        pdf = pdf_path(row)
        need_plain = force or not plain_cache.is_file()
        need_layout_only = False
        if not need_plain:
            tpl = hp.detect_template(plain_cache.read_text(encoding="utf-8", errors="replace"))
            need_layout_only = tpl in LAYOUT_TEMPLATES and not layout_cache.is_file()
            if not need_layout_only:
                st["i_cache"] += 1
                continue
        if not valid_pdf(pdf):
            if need_plain:
                st["pdf_mangler"] += 1
            else:
                st["i_cache"] += 1
                st["layout_mangler"] += 1
            continue
        if deadline is not None and time.monotonic() >= deadline:
            st["ikke_naaet"] += 1
            continue
        if not checked:
            require_layout_support()
            checked = True
        try:
            if need_layout_only:
                atomic_write_text(layout_cache, extract_layout_only(pdf))
                st["layout_efterhentet"] += 1
                continue
            plain, layout, _pages = extract_plain_and_layout(pdf, want_layout=True)
            tpl = hp.detect_template(plain)
            atomic_write_text(plain_cache, plain)
            if tpl in LAYOUT_TEMPLATES:
                atomic_write_text(layout_cache, layout or "")
            elif layout_cache.is_file():
                layout_cache.unlink()
            st["udtrukket"] += 1
        except Exception as exc:
            st["fejl"][(year, doc)] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return st


# --------------------------------------------------------------------------- fase 2: parsing

def trade_type(t: dict) -> str:
    if t["side"] == "sell" and t["partial"]:
        return "partial_sale"
    return t["side"]


def include_decision(t: dict) -> tuple[bool, str]:
    """include_v1 + begrundelse. Konservativ: manglende ticker/dato eller tvivl -> ikke med."""
    src = t.get("asset_type_source", "")
    atype = t["asset_type"]
    if not t["ticker"]:
        return False, "ingen ticker"
    if t["option_like"]:
        return False, "option-lignende"
    if hp.option_like_name(t["asset"]):
        return False, "option/warrant i navnet"
    if not t["transaction_date"] or t["amount_low"] is None:
        return False, "mangler handelsdato eller beløb"
    if atype in hp.STOCK_TYPES:
        return True, ("kode " + atype) if src == "code" else ("udledt " + atype)
    if src == "inferred":
        if atype == "?":
            return False, "tvivl: " + t.get("asset_type_reason", "")
        return False, f"udledt {atype}"
    return False, f"kode {atype or '(ingen)'}"


def amount_warnings(trades: list[dict]) -> list[str]:
    """Beløb, parseren ikke kunne læse fuldt ud. 'open_over' (Over $1.000.000) og 'exact' er gyldige og giver ingen advarsel."""
    n_inc = sum(1 for t in trades if t.get("amount_flag") == "incomplete_high")
    n_unr = sum(1 for t in trades if t.get("amount_flag") == "unreadable")
    out = []
    if n_inc:
        out.append(f"{n_inc} handel(er) har et delt beløb uden øvre grænse (amount_high er tom, der gættes ikke)")
    if n_unr:
        out.append(f"{n_unr} handel(er) uden læsbart beløb")
    return out


def parse_doc(row: dict) -> dict:
    """Parser én indberetning. Returnerer {'status', 'template', 'trades', 'warnings', 'signed_date', 'pages', 'parse_source'}."""
    year, doc = row["year"], row["doc_id"]
    plain_cache = TEXT_DIR / year / f"{doc}.txt"
    layout_cache = LAYOUT_DIR / year / f"{doc}.txt"
    pdf = pdf_path(row)
    out = {"status": "", "template": "", "trades": [], "warnings": [], "signed_date": "", "pages": "",
           "parse_source": ""}
    if not plain_cache.is_file():
        out["status"] = "pdf_missing" if not valid_pdf(pdf) else "not_processed"
        return out
    plain = plain_cache.read_text(encoding="utf-8", errors="replace")
    layout = layout_cache.read_text(encoding="utf-8", errors="replace") if layout_cache.is_file() else None
    nonws = len(re.sub(r"\s", "", hp.normalize(plain)))
    if nonws < SCANNED_MAX_CHARS:
        out["status"] = "scanned"
        out["template"] = "empty"
        out["pages"] = page_count(pdf) if valid_pdf(pdf) else ""
        out["warnings"].append(f"scannet/håndskrevet: kun {nonws} tegn tekst i PDF'en")
        return out
    res = hp.parse_ptr_text(plain, layout)
    out["status"] = "parsed"
    out["template"] = res["template"]
    out["parse_source"] = res.get("parse_source") or ("layout" if res["template"] == "t2016" else "plain")
    out["trades"] = res["trades"]
    out["warnings"] = list(res["warnings"]) + amount_warnings(res["trades"])
    out["signed_date"] = res["header"].get("signed_date") or ""
    # kontrol: indekset og PDF'en skal handle om samme person/dato (accenter ignoreres)
    hdr_name = fold(res["header"].get("member") or "")
    last = fold(row.get("last", "")).strip()
    if hdr_name and last and last not in hdr_name:
        out["warnings"].append(f"navn i PDF ({res['header']['member']!r}) matcher ikke indekset ({row.get('first')} {row.get('last')})")
    if out["signed_date"] and row.get("filing_date"):
        try:
            d = abs((datetime.fromisoformat(out["signed_date"]) - datetime.fromisoformat(row["filing_date"])).days)
            if d > DATE_MISMATCH_WARN_DAYS:
                out["warnings"].append(f"indeksets filing_date {row['filing_date']} afviger {d} dage fra underskrevet dato {out['signed_date']}")
        except ValueError:
            pass
    return out


def fmt_num(v) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)


def build_outputs(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Parser alle valgte indberetninger. Dublet- og slettemarkering sker bagefter i finalize_trades (på tværs af filer)."""
    trades_out: list[dict] = []
    scanned_out: list[dict] = []
    report_out: list[dict] = []
    for row in rows:
        member = f"{row.get('first', '').strip()} {row.get('last', '').strip()}".strip()
        pdf = pdf_path(row)
        res = parse_doc(row)
        n_doubt = 0
        for t in res["trades"]:
            inc, why = include_decision(t)
            n_doubt += why.startswith("tvivl")
            trades_out.append({
                "year": row["year"], "doc_id": row["doc_id"], "member": member, "state_dst": row.get("state_dst", ""),
                "owner": t["owner"], "asset": t["asset"], "ticker": t["ticker"], "asset_type": t["asset_type"],
                "asset_type_source": t.get("asset_type_source", ""), "type": trade_type(t),
                "transaction_date": t["transaction_date"] or "", "notification_date": t["notification_date"] or "",
                "filing_date": row.get("filing_date", ""), "amount_low": fmt_num(t["amount_low"]),
                "amount_high": fmt_num(t["amount_high"]), "amount_flag": t.get("amount_flag", ""),
                "description": t["description"], "sub_holding": t["sub_holding"],
                "location": t.get("location", ""), "comments": t.get("comments", ""),
                "filing_status": t.get("filing_status", ""),
                "option_like": t["option_like"], "short_related": t["short_related"],
                "date_suspect": "", "duplicate_of": "", "deleted_by": "",
                "include_v1": inc, "include_reason": why, "template": t.get("template", res["template"]),
                "parse_source": res["parse_source"],
            })
        if res["status"] == "scanned":
            scanned_out.append({"year": row["year"], "doc_id": row["doc_id"], "member": member,
                                "filing_date": row.get("filing_date", ""), "pdf_path": rel(pdf), "sider": res["pages"]})
        report_out.append({
            "year": row["year"], "doc_id": row["doc_id"], "member": member, "filing_date": row.get("filing_date", ""),
            "signed_date": res["signed_date"], "status": res["status"], "template": res["template"],
            "parse_source": res["parse_source"], "sider": res["pages"], "n_trades": len(res["trades"]),
            "n_include_v1": 0, "n_doubt": n_doubt, "n_date_suspect": 0, "n_duplicate": 0,
            "warnings": " | ".join(res["warnings"]), "pdf_path": rel(pdf),
        })
    return trades_out, scanned_out, report_out


# --------------------------------------------------------------------------- dubletter, slettede og datoer (på tværs af filer)

EXTRA_REASON_PREFIXES = ("datomistanke", "slettet", "dublet")
BASE_SEP = " | grundregel: "      # "dublet af 2000123 | grundregel: kode ST": grundreglens begrundelse bevares bag den ekstra


def _is_true(v) -> bool:
    return str(v) == "True"


def trade_key(t: dict) -> tuple | None:
    """(medlem, ticker eller aktivnavn, type, handelsdato, beløb nedre, beløb øvre). None, hvis dato eller beløb mangler.
    Ejer og konto er IKKE med (en ændringsindberetning kan rette dem)."""
    if not t["transaction_date"] or t["amount_low"] in ("", None):
        return None
    ident = t["ticker"].upper() if t["ticker"] else re.sub(r"\s+", " ", fold(t["asset"])).strip()
    if not ident:
        return None
    return (fold(t["member"]), ident, t["type"], t["transaction_date"], str(t["amount_low"]), str(t["amount_high"]))


def finalize_trades(rows: list[dict]) -> dict:
    """
    Sætter date_suspect, duplicate_of, deleted_by og den endelige include_v1/include_reason på ALLE rækker (de valgte års
    nye rækker + bevarede rækker for andre år). Deterministisk og idempotent: 'include_core' (inden disse regler) kan
    genfindes af rækken selv (include_v1 sand, eller en begrundelse, der begynder med datomistanke/slettet/dublet).
    Rækkefølgen af regler (første træf bestemmer begrundelsen, men alle kolonner udfyldes uafhængigt):
      date_suspect  handelsdato efter filing_date, mere end 3 år før, umulig dato/år (se house_ptr_parser.date_suspect_reason)
      deleted_by    en senere indberetning har en række med Filing Status 'Deleted' for præcis samme handel
      slettet       selve rækken har Filing Status 'Deleted' (en sletteanmodning, ikke en handel)
      duplicate_of  samme handel (nøgle: se trade_key) står allerede i en TIDLIGERE indberetning (første doc_id)
    Ens rækker inden for SAMME indberetning markeres ikke (kan være ægte, fx forskellige konti); er den første indberetning
    c0 ens rækker, og en senere har c, markeres højst min(c, c0) rækker i den senere som dubletter.
    Returnerer tællere.
    """
    for t in rows:
        reason = str(t["include_reason"])
        core = _is_true(t["include_v1"]) or reason.startswith(EXTRA_REASON_PREFIXES)
        t["_core"] = core
        if core and BASE_SEP in reason:
            t["include_reason"] = reason.split(BASE_SEP, 1)[1]       # tilbage til grundreglens begrundelse
        t["include_v1"] = _is_true(t["include_v1"])
        t["date_suspect"] = hp.date_suspect_reason(t["transaction_date"] or None, t["filing_date"] or None)
        t["duplicate_of"] = ""
        t["deleted_by"] = ""
    order = sorted(range(len(rows)), key=lambda i: (rows[i]["filing_date"] or "", int(rows[i]["year"]), str(rows[i]["doc_id"]), i))
    # 1. slettede: find den tidligere række, der slettes
    by_key: dict[tuple, list[int]] = {}
    for i in order:
        t = rows[i]
        k = trade_key(t)
        if k is not None and t["filing_status"] != "Deleted":
            by_key.setdefault(k, []).append(i)
    n_del_unmatched = 0
    for i in order:
        t = rows[i]
        if t["filing_status"] != "Deleted":
            continue
        k = trade_key(t)
        hit = None
        for j in by_key.get(k, []) if k is not None else []:
            r = rows[j]
            if r["doc_id"] != t["doc_id"] and not r["deleted_by"] and (r["filing_date"], int(r["year"]), str(r["doc_id"])) <= (
                    t["filing_date"], int(t["year"]), str(t["doc_id"])):
                hit = j
                break
        if hit is None:
            n_del_unmatched += 1
        else:
            rows[hit]["deleted_by"] = t["doc_id"]
    # 2. dubletter: første indberetning (efter filing_date, doc_id) med nøglen er originalen
    groups: dict[tuple, list[tuple]] = {}
    for i in order:
        t = rows[i]
        if t["filing_status"] == "Deleted":
            continue
        k = trade_key(t)
        if k is None:
            continue
        per_doc = groups.setdefault(k, [])
        if per_doc and per_doc[-1][0] == t["doc_id"]:
            per_doc[-1][1].append(i)
        else:
            per_doc.append((t["doc_id"], [i]))
    n_dup = 0
    for k, per_doc in groups.items():
        if len(per_doc) < 2:
            continue
        first_doc, first_rows = per_doc[0]
        for doc, idxs in per_doc[1:]:
            for i in idxs[:len(first_rows)]:
                rows[i]["duplicate_of"] = first_doc
                n_dup += 1
    # 3. endelig include_v1
    cnt = {"date_suspect": 0, "duplicate": 0, "deleted_by": 0, "deleted_rows": 0, "deleted_unmatched": n_del_unmatched,
           "amended_not_dup": 0}
    for t in rows:
        reasons = []
        if t["filing_status"] == "Deleted":
            reasons.append("slettet: rækken er en sletteanmodning (Filing Status: Deleted)")
            cnt["deleted_rows"] += 1
        if t["deleted_by"]:
            reasons.append(f"slettet af senere indberetning {t['deleted_by']}")
            cnt["deleted_by"] += 1
        if t["date_suspect"]:
            reasons.append(f"datomistanke: {t['date_suspect']}")
            cnt["date_suspect"] += 1
        if t["duplicate_of"]:
            reasons.append(f"dublet af {t['duplicate_of']}")
            cnt["duplicate"] += 1
        if t["filing_status"] == "Amended" and not t["duplicate_of"]:
            cnt["amended_not_dup"] += 1
        if t.pop("_core"):
            if reasons:
                t["include_v1"] = False
                t["include_reason"] = reasons[0] + BASE_SEP + t["include_reason"]
            else:
                t["include_v1"] = True
    return cnt


def merge_write(path: Path, fields: list[str], new_rows: list[dict], years: set[int], finalize: bool = False,
                fix=None) -> dict:
    """Skriver new_rows og bevarer eksisterende rækker for år uden for 'years'. Med finalize=True køres finalize_trades
    over alle rækker (nye + bevarede), så dubletter på tværs af år altid er konsistente."""
    kept: list[dict] = []
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("year", "").isdigit() and int(r["year"]) not in years:
                    kept.append({f: r.get(f, "") for f in fields})
    allrows = kept + new_rows
    allrows.sort(key=lambda r: (int(r["year"]), str(r["doc_id"])))   # stabil: bevarer rækkefølgen inden for en indberetning
    stats: dict = {}
    if finalize:
        stats = finalize_trades(allrows)
    if fix is not None:
        fix(allrows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(allrows)
    os.replace(tmp, path)
    stats["rows"] = allrows
    return stats


def print_summary(report: list[dict], trades: list[dict], extract_stats: dict, elapsed: float, fin: dict | None = None) -> None:
    years = sorted({int(r["year"]) for r in report})
    print("\nOversigt pr. år (handler: include_v1 er efter dato-, slette- og dubletreglerne)")
    head = (f"{'År':<6}{'Indberetn.':>11}{'Mangler PDF':>12}{'Ikke nået':>10}{'Parset':>8}{'Scannet':>9}"
            f"{'Advarsler':>10}{'Handler':>9}{'Heraf v1':>9}{'Tvivl':>7}{'Datomist.':>10}{'Dubletter':>10}")
    print(head)
    keys = ["n", "miss", "todo", "ok", "scan", "warn", "tr", "inc", "doubt", "dsus", "dup"]
    tot = dict.fromkeys(keys, 0)

    def line(label, v):
        print(f"{label:<6}{v['n']:>11}{v['miss']:>12}{v['todo']:>10}{v['ok']:>8}{v['scan']:>9}"
              f"{v['warn']:>10}{v['tr']:>9}{v['inc']:>9}{v['doubt']:>7}{v['dsus']:>10}{v['dup']:>10}")

    for y in years:
        rs = [r for r in report if int(r["year"]) == y]
        ts = [t for t in trades if int(t["year"]) == y]
        vals = {
            "n": len(rs), "miss": sum(r["status"] == "pdf_missing" for r in rs),
            "todo": sum(r["status"] == "not_processed" for r in rs),
            "ok": sum(r["status"] == "parsed" for r in rs), "scan": sum(r["status"] == "scanned" for r in rs),
            "warn": sum(bool(r["warnings"]) and r["status"] == "parsed" for r in rs), "tr": len(ts),
            "inc": sum(str(t["include_v1"]) == "True" for t in ts),
            "doubt": sum(str(t["include_reason"]).startswith("tvivl") or "grundregel: tvivl" in str(t["include_reason"]) for t in ts),
            "dsus": sum(bool(t["date_suspect"]) for t in ts), "dup": sum(bool(t["duplicate_of"]) for t in ts),
        }
        for k, v in vals.items():
            tot[k] += v
        line(str(y), vals)
    if len(years) > 1:
        line("Ialt", tot)
    parsed = [r for r in report if r["status"] == "parsed"]
    clean = sum(1 for r in parsed if not r["warnings"])
    if parsed:
        print(f"\nTekstbaserede indberetninger uden advarsler: {clean} af {len(parsed)} ({100 * clean / len(parsed):.1f} %)")
    no_layout = sum(1 for r in parsed if r["template"] == "old_2021" and r["parse_source"] == "plain")
    if no_layout:
        print(f"{no_layout} old_2021-indberetninger er læst uden layout-tekst (ejer, aktivnavn og beskrivelse er mindre "
              "pålidelige); kør kommandoen igen med PDF'erne til stede for at hente layout-teksten.")
    if fin:
        print("Dato/dubletter/slettede (alle år i trades.csv): "
              f"date_suspect {fin['date_suspect']}, dubletter {fin['duplicate']}, slettet via 'Deleted' {fin['deleted_by']} "
              f"(slettemarkeringer {fin['deleted_rows']}, uden fundet original {fin['deleted_unmatched']}), "
              f"ændringsrækker uden fundet original {fin['amended_not_dup']}.")
    print(f"\nTekstudtræk i denne kørsel: {extract_stats['udtrukket']} nye, {extract_stats['layout_efterhentet']} med "
          f"layout efterhentet, {extract_stats['i_cache']} fra cache, {extract_stats['pdf_mangler']} uden PDF, "
          f"{extract_stats['layout_mangler']} uden PDF til layout, {len(extract_stats['fejl'])} fejlede, "
          f"{extract_stats['ikke_naaet']} ikke nået (tiden gik). Tid: {elapsed:.1f} s.")
    for (y, d), msg in list(extract_stats["fejl"].items())[:10]:
        print(f"  FEJL {y}/{d}: {msg}")
    if extract_stats["ikke_naaet"]:
        print("Tiden er brugt. Kør kommandoen igen for at fortsætte; cachede PDF'er springes over.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    usage = ("Brug: python3 ingestion/parse_house_ptr.py [år eller interval, fx 2016 eller 2016-2020] "
             "[--max-seconds N] [--force-text]")
    max_seconds: float | None = None
    force = False
    years_arg: str | None = None
    i = 0
    try:
        while i < len(argv):
            a = argv[i]
            if a == "--max-seconds":
                max_seconds = float(argv[i + 1])
                if max_seconds <= 0:
                    raise ValueError
                i += 2
            elif a.startswith("--max-seconds="):
                max_seconds = float(a.split("=", 1)[1])
                if max_seconds <= 0:
                    raise ValueError
                i += 1
            elif a == "--force-text":
                force = True
                i += 1
            elif a in ("-h", "--help"):
                print(usage)
                return 0
            elif years_arg is None and not a.startswith("-"):
                years_arg = a
                i += 1
            else:
                raise ValueError(a)
        years_sel = parse_years(years_arg) if years_arg else None
    except (ValueError, IndexError):
        print(usage)
        return 2

    t0 = time.monotonic()
    deadline = t0 + max_seconds if max_seconds else None
    index = read_index()
    rows = [r for r in index if years_sel is None or int(r["year"]) in years_sel]
    if not rows:
        print("Ingen indberetninger i indekset for de valgte år.")
        return 1
    years = {int(r["year"]) for r in rows}

    stats = extract_phase(rows, force, deadline)
    trades, scanned, report = build_outputs(rows)
    tstats = merge_write(OUT_DIR / "trades.csv", TRADE_FIELDS, trades, years, finalize=True)
    final = tstats.pop("rows")
    per_doc: dict[tuple, dict] = {}
    for t in final:
        c = per_doc.setdefault((t["year"], t["doc_id"]), {"inc": 0, "dsus": 0, "dup": 0})
        c["inc"] += _is_true(t["include_v1"])
        c["dsus"] += bool(t["date_suspect"])
        c["dup"] += bool(t["duplicate_of"])

    def fix(allrows):
        for r in allrows:
            c = per_doc.get((r["year"], r["doc_id"]))
            if c:
                r["n_include_v1"], r["n_date_suspect"], r["n_duplicate"] = c["inc"], c["dsus"], c["dup"]

    merge_write(OUT_DIR / "scanned.csv", SCANNED_FIELDS, scanned, years)
    merge_write(OUT_DIR / "parse_report.csv", REPORT_FIELDS, report, years, fix=fix)
    print_summary(report, [t for t in final if int(t["year"]) in years], stats, time.monotonic() - t0, tstats)
    print(f"\nSkrevet: {rel(OUT_DIR / 'trades.csv')}, {rel(OUT_DIR / 'scanned.csv')}, {rel(OUT_DIR / 'parse_report.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

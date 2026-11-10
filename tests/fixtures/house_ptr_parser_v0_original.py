"""
house_ptr_parser.py
-------------------
Læser tekst (udtrukket med pypdf) fra en PTR-indberetning (Periodic Transaction Report)
fra Repræsentanternes Hus og returnerer handlerne.

Håndterer to skabeloner, testet på rigtige indberetninger:

  Ny (ca. 2022-):   SP Charles Schwab Corporation (SCHW) [ST] P 12/14/2023 01/08/2024 $50,001 - $100,000
                    F S: New
                    S O: R.W. Allen & Associates, Inc.
                    D: Part of my spouse's retirement portfolio.

  Ældre (ca. 2021): 8x8 Inc (EgHT) [ST] P 03/10/2020 04/01/2020 $15,001 - $50,000 g
                    fedcF
                    IlINg  S TATuS : New D
                    ESCRIPTION : covered short Amber Road, Inc. (AMBR) ...
                    (etiketter med mærkelige store/små bogstaver, afkrydsningsfelter som 'g'/'fedc',
                     små bogstaver i tickere og typekoder, og beskrivelsen flyder sammen med næste aktie)

Fremgangsmåde: Hver handel afsluttes af en "filing status"-linje. Teksten deles ved dem,
og i hver del findes den faste kerne "type dato dato" (fx "P 03/10/2020 04/01/2020").
Aktietype ([ST]) og ticker søges før kernen, og ellers lige efter den (ved sideskift).

Datoer:
  transaction_date  = handelsdatoen
  notification_date = datoen medlemmet fik besked om handlen (står i tabellen)
  filing_date       = datoen indberetningen blev underskrevet/indleveret. Det er den tidligste dato,
                      offentligheden kunne kende handlen, og den skal bruges i backtesten.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

OWNER_CODES = {"SP": "spouse", "JT": "joint", "DC": "dependent child"}
STOCK_TYPES = {"ST", "ET"}          # aktier og ETF'er - det vi kan købe via Alpaca
SIDE = {"P": "buy", "S": "sell", "E": "exchange"}

CORE_RE = re.compile(
    r"(?<![A-Za-z])(?P<ttype>[PSE])(?:\s*\((?P<partial>partial)\))?\s+"
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+(?P<ndate>\d{2}/\d{2}/\d{4})"
)
AMOUNT_RE = re.compile(
    r"(?P<over>Over\s+)?\$(?P<low>[\d,]+)(?:\s*-\s*(?:\[[A-Za-z]{2}\]\s*)?(?:\$(?P<high>[\d,]+))?)?"
)
TAG_RE = re.compile(r"\[([A-Za-z]{2})\]")
TICKER_RE = re.compile(r"\(\s*([A-Za-z][A-Za-z0-9.\-]{0,6})\s*\)")
OWNER_RE = re.compile(r"(?:^|\s)(SP|JT|DC)\s+(?=\S)")

# Etiketter. Ny skabelon: korte etiketter i starten af en linje.
NEW_FS = re.compile(r"^F\s*S\s*:")
NEW_SO = re.compile(r"^S\s*O\s*:\s*")
NEW_D = re.compile(r"^D\s*:\s*")
NEW_C = re.compile(r"^C\s*:\s*")
# Ældre skabelon: lange etiketter, der kan stå midt i en linje.
OLD_FS = re.compile(r"F\s*I\s*L\s*I\s*N\s*G\s*S\s*T\s*A\s*T\s*U\s*S\s*:\s*[A-Za-z]+", re.I)
OLD_SO = re.compile(r"S\s*U\s*B\s*H\s*O\s*L\s*D\s*I\s*N\s*G\s*O\s*F\s*:", re.I)
OLD_D = re.compile(r"(?<![A-Za-z])D\s*ESCRIPTION\s*:", re.I)
HEADER_ROW = re.compile(r"\bID\s+Owner\s+Asset\s+Transaction\b.*?\$200\?", re.I)

OPTION_WORDS = re.compile(r"\b(calls?|puts?)\b|\bstrike\b|\bexpir", re.I)
SHORT_WORDS = re.compile(r"short\s+(sale|cover)|covered\s+short", re.I)


def normalize(text: str) -> str:
    """Fjerner NUL- og andre kontroltegn, som pypdf kan efterlade."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _iso(d: str) -> Optional[str]:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(d.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_header(text: str) -> dict:
    name = re.search(r"name:\s*(.+?)\s+Status:", text, re.I | re.S)
    state = re.search(r"State/District:\s*(\S+)", text, re.I)
    filing = re.search(r"Filing ID\s*#\s*(\d+)", text)
    signed = re.search(r"Digitally Signed:.*?,\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.S)
    clean_name = ""
    if name:
        clean_name = re.sub(r"\s+", " ", name.group(1)).strip()
        clean_name = re.sub(r"^(Hon\.|Mr\.|Mrs\.|Ms\.|Dr\.)\s*", "", clean_name)
    return {
        "member": clean_name,
        "state_dst": state.group(1).upper() if state else "",
        "filing_id": filing.group(1) if filing else "",
        "signed_date": _iso(signed.group(1)) if signed else None,
    }


def _table_text(text: str) -> str:
    """Skærer ned til transaktionstabellen, fjerner støj og markerer etiketter."""
    lines = [ln.strip() for ln in text.splitlines()]
    start = next((i for i, ln in enumerate(lines) if "$200?" in ln), None)
    if start is None:
        return ""
    out: list[str] = []
    for ln in lines[start + 1:]:
        end = ln.find("* For the complete list")
        if end >= 0:
            ln = ln[:end].strip()
        ln = re.sub(r"^fedcb?", "", ln)          # afkrydsningsfelt i starten
        ln = re.sub(r"\s+g$", "", ln)            # afkrydsningsfelt i slutningen
        ln = re.sub(r"Filing ID\s*#\s*\d+", " ", ln).strip()
        if ln and ln != "g":
            if NEW_FS.match(ln):
                ln = "§FS§"
            elif NEW_SO.match(ln):
                ln = "§SO:" + NEW_SO.sub("", ln) + "§"
            elif NEW_D.match(ln):
                ln = "§D:" + NEW_D.sub("", ln) + "§"
            elif NEW_C.match(ln):
                ln = "§X§"
            out.append(ln)
        if end >= 0:
            break
    joined = " ".join(out)
    joined = HEADER_ROW.sub(" ", joined)
    joined = OLD_FS.sub(" §FS§ ", joined)
    joined = OLD_SO.sub(" §SO> ", joined)
    joined = OLD_D.sub(" §D> ", joined)
    return re.sub(r"\s+", " ", joined)


def _split_pre(pre: str) -> tuple[dict, str, str]:
    """
    Deler teksten før en handels kerne i: afslutningsinfo for den FORRIGE handel
    (konto og beskrivelse), ejerkode og aktienavn for DENNE handel.
    """
    trailer = {"sub_holding": [], "description": []}
    for kind, txt in re.findall(r"§(SO|D):(.*?)§", pre):
        trailer["sub_holding" if kind == "SO" else "description"].append(txt.strip())
    pre = re.sub(r"§(SO|D):.*?§", " ", pre)
    pre = re.sub(r"§X§|§FS§", " ", pre)

    parts = re.split(r"(§SO>|§D>)", pre)
    asset_text = parts[0]
    if len(parts) > 1:
        # Ældre skabelon: etikettens tekst flyder sammen med næste aktie
        pairs = list(zip(parts[1::2], parts[2::2]))
        for i, (tok, txt) in enumerate(pairs):
            key = "sub_holding" if tok == "§SO>" else "description"
            if i < len(pairs) - 1:
                trailer[key].append(txt.strip())
                continue
            owners = list(OWNER_RE.finditer(txt))
            if owners:
                o = owners[-1]
                trailer[key].append(txt[:o.start()].strip())
                asset_text = txt[o.start():]
            else:
                trailer[key].append(txt.strip())  # kan indeholde næste aktienavn
                asset_text = txt
        if parts[0].strip():
            asset_text = parts[0] + " " + asset_text if not asset_text.strip() else asset_text

    asset_text = re.sub(r"\s+", " ", asset_text).strip()
    owner = "self"
    m = OWNER_RE.match(" " + asset_text)
    if m:
        owner = OWNER_CODES[m.group(1)]
        asset_text = asset_text[m.end() - 1:].strip()
    return trailer, owner, asset_text


def _parse_amount(m: Optional[re.Match]) -> tuple[Optional[float], Optional[float]]:
    if not m:
        return None, None
    low = float(m.group("low").replace(",", ""))
    if m.group("over"):
        return low, None
    high = m.group("high")
    return low, (float(high.replace(",", "")) if high else None)


def parse_ptr_text(text: str) -> dict:
    """Returnerer {'header': {...}, 'trades': [...], 'warnings': [...]}."""
    text = normalize(text)
    header = parse_header(text)
    table = _table_text(text)
    warnings: list[str] = []
    records: list[dict] = []

    chunks = table.split("§FS§")
    n_markers = len(chunks) - 1
    for chunk in chunks:
        cores = list(CORE_RE.finditer(chunk))
        prev_end = 0
        for k, core in enumerate(cores):
            pre = chunk[prev_end:core.start()]
            post_end = cores[k + 1].start() if k + 1 < len(cores) else len(chunk)
            post = chunk[core.end():post_end]

            trailer, owner, asset = _split_pre(pre)
            if records:
                if trailer["sub_holding"]:
                    records[-1]["sub_holding"] = " ".join(trailer["sub_holding"])
                if trailer["description"]:
                    records[-1]["description"] = " ".join(trailer["description"])

            amount_m = AMOUNT_RE.search(post)
            prev_end = core.end() + (amount_m.end() if amount_m else 0)

            tags = TAG_RE.findall(asset)
            tag = tags[-1] if tags else (TAG_RE.findall(post) or [""])[0]
            tickers = TICKER_RE.findall(asset)
            if not tickers:  # ved sideskift står tickeren efter kernen
                before_tag = post.split("[")[0] if "[" in post else post
                tickers = TICKER_RE.findall(before_tag)
            low, high = _parse_amount(amount_m)
            records.append({
                "owner": owner,
                "asset": TAG_RE.sub("", asset).strip(),
                "ticker": tickers[-1].upper() if tickers else "",
                "asset_type": tag.upper(),
                "side": SIDE[core.group("ttype")],
                "partial": bool(core.group("partial")),
                "transaction_date": _iso(core.group("tdate")),
                "notification_date": _iso(core.group("ndate")),
                "amount_low": low,
                "amount_high": high,
                "sub_holding": "",
                "description": "",
            })

    # Afslutningsinfo efter den sidste handel
    if records and chunks:
        trailer, _, _ = _split_pre(chunks[-1][prev_end:] if len(chunks) == 1 else chunks[-1])
        if trailer["sub_holding"] and not records[-1]["sub_holding"]:
            records[-1]["sub_holding"] = " ".join(trailer["sub_holding"])
        if trailer["description"] and not records[-1]["description"]:
            records[-1]["description"] = " ".join(trailer["description"])

    if n_markers and n_markers != len(records):
        warnings.append(f"{len(records)} handler fundet, men {n_markers} 'filing status'-markører")
    if not text.strip():
        warnings.append("ingen tekst (sandsynligvis scannet PDF)")
    elif not records:
        warnings.append("ingen handler fundet i teksten")

    for r in records:
        r["member"] = header["member"]
        r["state_dst"] = header["state_dst"]
        r["filing_id"] = header["filing_id"]
        r["filing_date"] = header["signed_date"]
        r["option_like"] = bool(OPTION_WORDS.search(r["description"]))
        r["short_related"] = bool(SHORT_WORDS.search(r["description"]))
        # Optioner holdes ude. Short-handler beholdes, men er markeret til backtesten.
        r["is_stock"] = r["asset_type"] in STOCK_TYPES and bool(r["ticker"]) and not r["option_like"]

    return {"header": header, "trades": records, "warnings": warnings}


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        result = parse_ptr_text(open(path, encoding="utf-8").read())
        print(f"== {path}: {result['header']}")
        for t in result["trades"]:
            flag = "AKTIE" if t["is_stock"] else "andet"
            extra = (" SHORT" if t["short_related"] else "") + (" OPTION" if t["option_like"] else "")
            print(f"  [{flag}{extra}] {t['side']:<5} {t['ticker'] or '-':<6} {t['transaction_date']} "
                  f"(besked {t['notification_date']}) {t['amount_low']}-{t['amount_high']} "
                  f"ejer={t['owner']} konto={t['sub_holding']!r} beskr={t['description'][:40]!r}")
        for w in result["warnings"]:
            print("  ADVARSEL:", w)

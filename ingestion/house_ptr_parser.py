"""
house_ptr_parser.py
-------------------
Læser tekst (udtrukket med pypdf) fra en PTR-indberetning (Periodic Transaction Report)
fra Repræsentanternes Hus og returnerer handlerne.

Håndterer tre skabeloner. Template-navnet står i hver handel (felt 'template'):

  new_2022  Ny (ca. 2022-):   SP Charles Schwab Corporation (SCHW) [ST] P 12/14/2023 01/08/2024 $50,001 - $100,000
                    F S: New
                    S O: R.W. Allen & Associates, Inc.
                    D: Part of my spouse's retirement portfolio.

  old_2021  Ældre (ca. 2021): 8x8 Inc (EgHT) [ST] P 03/10/2020 04/01/2020 $15,001 - $50,000 g
                    fedcF
                    IlINg  S TATUS : New D
                    ESCRIPTION : covered short Amber Road, Inc. (AMBR) ...
                    (etiketter med mærkelige store/små bogstaver, afkrydsningsfelter som 'g'/'fedc',
                     små bogstaver i tickere og typekoder, og beskrivelsen flyder sammen med næste aktie)

  t2016     2016-skabelonen: ingen "$200?"-kolonne, ingen aktivtype-kode som [ST], datoer med encifret
            dag ("01/4/2016"), ejerkode i en egen kolonne (ofte tom = medlemmet selv), "Filing Status"/
            "Subholding Of"/"Description" under hver handel og en sektion "Asset Class Details".
            pypdf's almindelige tekstudtræk er uanvendeligt til denne skabelon (kolonnerne blandes
            sammen, tickere kan blive splittet som "M DT", ejerkoden havner i slutningen af forrige
            handel). Den læses derfor fra pypdf's LAYOUT-tekst (extract_text(extraction_mode="layout"),
            kræver pypdf >= 4.0), hvor hver tabelrække er sin egen linje. Får parseren kun almindelig
            tekst for en 2016-skabelon, læses rækkerne (kun kernen) fra den almindelige tekst med en advarsel.
            2018-2022 bruger t2016-skabelonen eller old_2021; 2017-2020 er set og tjekket mod PDF i runde 2.

  old_2021 med layout-tekst læses også af layout-parseren (_parse_t2016 med template="old_2021"): almindelig tekst
            klistrer ejerkoden til forrige handel og lader beskrivelsen flyde ind i næste aktienavn. Mangler layout-teksten,
            bruges almindelig tekst (parse_source = "plain"); ejer, aktienavn, subholding og beskrivelse er så upålidelige.
            Kendt, uændret fejl i new_2022 (ren tekst): en beskrivelse over to linjer flyder ind i næste handels aktienavn
            (ejerkoden står så midt i navnet og ejer bliver "self"); se KNOWN_PARSER_DEVIATIONS i testen.

Er teksten hverken de to første skabeloner eller t2016, er template = 'unknown', og der gives en
tydelig advarsel i stedet for stille at returnere 0 handler.

Fremgangsmåde (new_2022 / old_2021): Hver handel afsluttes af en "filing status"-linje. Teksten deles
ved dem, og i hver del findes den faste kerne "type dato dato" (fx "P 03/10/2020 04/01/2020").
Aktietype ([ST]) og ticker søges før kernen, og ellers lige efter den (ved sideskift).

Fremgangsmåde (t2016): se _parse_t2016. Aktivtype står ikke i skabelonen og udledes af navn/ticker
(infer_asset_type); er der tvivl, er handlen IKKE med (is_stock/include_v1 = False) og tælles.

Datoer:
  transaction_date  = handelsdatoen
  notification_date = datoen medlemmet fik besked om handlen (står i tabellen)
  filing_date       = datoen indberetningen blev underskrevet/indleveret. Det er den tidligste dato,
                      offentligheden kunne kende handlen, og den skal bruges i backtesten.
                      (parse_ptr_text returnerer den underskrevne dato; parse_house_ptr.py bruger
                      indeksets filing_date.)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

OWNER_CODES = {"SP": "spouse", "JT": "joint", "DC": "dependent child"}
STOCK_TYPES = {"ST", "ET"}          # aktier og ETF'er - det vi kan købe via Alpaca
SIDE = {"P": "buy", "S": "sell", "E": "exchange"}

CORE_RE = re.compile(
    r"(?P<ttype>[PSEpse])(?:\s*\((?P<partial>partial)\))?\s+"
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+(?P<ndate>\d{2}/\d{2}/\d{4})"
)
AMOUNT_RE = re.compile(
    r"(?P<over>Over\s+)?\$(?P<low>[\d,]+(?:\.\d+)?|\.\d+)"
    r"(?:(?P<dash>\s*-\s*)(?:\[[A-Za-z]{2}\]\s*)?(?:\$(?P<high>[\d,]+(?:\.\d+)?|\.\d+))?)?"
)
# Husets faste beloebsintervaller (nedre grænse -> øvre grænse). Bruges til at kontrollere/reparere beløb, der er delt af et
# sideskift, uden at gætte: kun en øvre grænse, der PRÆCIS passer til den nedre, accepteres.
STANDARD_RANGES = {1001: 15000, 15001: 50000, 50001: 100000, 100001: 250000, 250001: 500000, 500001: 1000000,
                   1000001: 5000000, 5000001: 25000000, 25000001: 50000000}
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


FS_VALUES = ("new", "amended", "deleted")


def _fs_value(run: str) -> str:
    """'NewD' (pypdf klistrer næste etiket på) -> 'New'. Ukendt værdi returneres som den er."""
    low = run.lower()
    for v in FS_VALUES:
        if low.startswith(v):
            return v.capitalize()
    return run


def _table_text(text: str, statuses: Optional[list] = None) -> str:
    """Skærer ned til transaktionstabellen, fjerner støj og markerer etiketter. Er 'statuses' en liste, får den
    værdien af hver 'Filing Status' i den rækkefølge, markørerne står i."""
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
                if statuses is not None:
                    statuses.append(_fs_value(re.sub(r"^F\s*S\s*:\s*", "", ln)))
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
    if statuses is None:
        joined = OLD_FS.sub(" §FS§ ", joined)
    else:
        joined = OLD_FS.sub(lambda m: statuses.append(_fs_value(m.group(0).split(":")[-1].strip())) or " §FS§ ", joined)
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


def _num(x: str) -> float:
    return float(x.replace(",", ""))


def _parse_amount(m: Optional[re.Match], rest: str = "") -> tuple[Optional[float], Optional[float], str, int]:
    """
    -> (low, high, amount_flag, forbrugt) hvor amount_flag er:
      ''                normalt interval
      'open_over'       "Over $1,000,000" / "Spouse/DC Over $1,000,000": åbent interval, high er tom
      'exact'           præcist beløb ("$360.00"): low = high = beløbet
      'repaired_split'  intervallet var delt af et sideskift/linjeskift; øvre grænse hentet fra 'rest' og kun accepteret,
                        hvis den passer præcist til Husets standardinterval for den nedre grænse
      'incomplete_high' intervallet er delt, og øvre grænse blev ikke fundet (high er tom; der gættes ikke)
      'unreadable'      intet beløb fundet
    'forbrugt' = antal tegn af 'rest', som hører til beløbet (ved repaired_split).
    """
    if not m:
        return None, None, "unreadable", 0
    low = _num(m.group("low"))
    if m.group("over"):
        return low, None, "open_over", 0
    if m.group("high"):
        return low, _num(m.group("high")), "", 0
    std = STANDARD_RANGES.get(int(low)) if low == int(low) else None
    if m.group("dash") or std is not None:
        cand = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", rest) if rest else None
        if std is not None and cand and _num(cand.group(1)) == std:
            return low, float(std), "repaired_split", cand.end()
        return low, None, "incomplete_high", 0
    return low, low, "exact", 0


def _parse_legacy(text: str, header: dict) -> dict:
    """new_2022 og old_2021 (uændret logik fra før t2016 blev tilføjet). text er normaliseret."""
    statuses: list[str] = []
    table = _table_text(text, statuses)
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
            low, high, aflag, used = _parse_amount(amount_m, post[amount_m.end():].split("§")[0] if amount_m else "")
            prev_end = core.end() + (amount_m.end() + used if amount_m else 0)

            tags = TAG_RE.findall(asset)
            tag = tags[-1] if tags else (TAG_RE.findall(post) or [""])[0]
            tickers = TICKER_RE.findall(asset)
            if not tickers:  # ved sideskift står tickeren efter kernen
                before_tag = post.split("[")[0] if "[" in post else post
                tickers = TICKER_RE.findall(before_tag)
            records.append({
                "owner": owner,
                "asset": TAG_RE.sub("", asset).strip(),
                "ticker": tickers[-1].upper() if tickers else "",
                "asset_type": tag.upper(),
                "side": SIDE[core.group("ttype").upper()],
                "partial": bool(core.group("partial")),
                "transaction_date": _iso(core.group("tdate")),
                "notification_date": _iso(core.group("ndate")),
                "amount_low": low,
                "amount_high": high,
                "amount_flag": aflag,
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

    template = "new_2022" if any(NEW_FS.match(ln.strip()) for ln in text.splitlines()) else "old_2021"
    for r in records:
        r["member"] = header["member"]
        r["state_dst"] = header["state_dst"]
        r["filing_id"] = header["filing_id"]
        r["filing_date"] = header["signed_date"]
        r["option_like"] = bool(OPTION_WORDS.search(r["description"]))
        r["short_related"] = bool(SHORT_WORDS.search(r["description"]))
        # Optioner holdes ude. Short-handler beholdes, men er markeret til backtesten.
        r["is_stock"] = r["asset_type"] in STOCK_TYPES and bool(r["ticker"]) and not r["option_like"]
        r["template"] = template
        r["asset_type_source"] = "code" if r["asset_type"] else "none"
        r["filing_status"] = ""
    if records and len(statuses) == len(records):      # status kun, hvis hver markør har præcis én handel
        for r, st in zip(records, statuses):
            r["filing_status"] = st

    return {"header": header, "trades": records, "warnings": warnings, "template": template}


# --------------------------------------------------------------------------- template-genkendelse

# 2016-skabelonens tabelhoved (uden "$200?"): "ID Owner Asset Transaction Type Date Notification Date Amount"
# (ældre pypdf-versioner klistrer ord sammen: "Datenotification Date", "amountWalt Disney" -> \s* mellem ordene)
T16_HEADER_PLAIN = re.compile(
    r"owner\s+asset\s+transaction\s+type\s+date\s*notification\s+date\s*amount", re.I)


def detect_template(text: str, layout_text: Optional[str] = None) -> str:
    """Gætter skabelonen ud fra den almindelige tekst (og layout-teksten, hvis den findes):
    new_2022, old_2021, t2016, unknown eller empty (ingen tekst = scannet)."""
    text = normalize(text)
    if not text.strip():
        return "empty"
    if "$200?" in text:
        return "new_2022" if any(NEW_FS.match(ln.strip()) for ln in text.splitlines()) else "old_2021"
    if T16_HEADER_PLAIN.search(text):
        return "t2016"
    if layout_text and T16_HEADER_L1.search(normalize(layout_text)):
        return "t2016"
    return "unknown"


# --------------------------------------------------------------------------- t2016 (layout-tekst)

T16_HEADER_L1 = re.compile(r"owner\s+asset\s+transaction\s+date\s*notification\s+amount", re.I)
T16_HEADER_L2 = re.compile(r"^\s*type\s+date\s*$", re.I)
T16_HEADER_ANY = re.compile(r"\bowner\s+asset\s+transaction\b", re.I)    # første linje af tabelhovedet (også i almindelig tekst)
# Sektionsoverskrifter sammenlignes uden mellemrum og uden store/små bogstaver (pypdf versioner indsætter
# mellemrum midt i små-versal-ord: "a SSet   c laSS   DetailS")
# "comments" uden kolon er en rapport-overskrift (generelle kommentarer efter tabellen), ikke en kommentar til en handel
T16_END_WORDS = {"assetclassdetails", "initialpublicofferings", "initialpublicoffering", "certificationandsignature",
                 "comments", "comment"}
T16_DETAIL_END_WORDS = {"initialpublicofferings", "initialpublicoffering", "certificationandsignature",
                        "comments", "comment"}


HEADER_WORDS = {"type", "date", "notification", "amount", "cap.", "cap", "gains", ">", "$200?", "gains>"}
CHECKBOX_ONLY = re.compile(r"^\s*g?fedcb?\s*$")
CHECKBOX_TAIL = re.compile(r"\s+g?fedcb?\s*$")


def _is_header_cont(line: str) -> bool:
    """Linje, der kun består af tabelhovedets ord (fortsættelse af hovedet i layout-teksten)."""
    toks = CHECKBOX_TAIL.sub("", line.strip()).lower().split()
    toks = [re.sub(r"^(\$200\?)g?fedcb?$", r"\1", t) for t in toks]      # "$200?gfedcb": kolonnenavn og checkbox klistret sammen
    return bool(toks) and all(t in HEADER_WORDS for t in toks)


def _compact(line: str) -> str:
    return re.sub(r"\s+", "", line).casefold()


T16_FOOTER = re.compile(r"Filing\s+ID\s*#\s*\d+", re.I)
T16_CORE = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s{2,})?(?P<asset>\S.*?)\s*(?P<ttype>[PSE])"
    r"(?:\s*\((?P<partial>partial)\))?\s+(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<ndate>\d{1,2}/\d{1,2}/\d{4})\s+(?P<amount>\S.*?)\s*$", re.I)
T16_AMOUNT_OPEN = re.compile(r"(?:-|\bover)\s*$", re.I)          # beløb, der mangler sin sidste del
T16_AMOUNT_FRAG = re.compile(r"^\s*(\$\s*[\d,]+(?:\.\d+)?)\s*$")
TWO_DATES = re.compile(r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}/\d{1,2}/\d{4}")
T16_FS = re.compile(r"^\s*filing\s*status\s*:\s*(.*?)\s*$", re.I)
T16_SUB = re.compile(r"^\s*subholding\s*of\s*:\s*(.*?)\s*$", re.I)
T16_DESC = re.compile(r"^\s*description\s*:\s*(.*?)\s*$", re.I)
T16_LOC = re.compile(r"^\s*location\s*:\s*(.*?)\s*$", re.I)
T16_COM = re.compile(r"^\s*comments?\s*:\s*(.*?)\s*$", re.I)
T16_OTHER_LABEL = re.compile(r"^\s*[A-Za-z][A-Za-z ]{2,24}:(?:\s|$)")
T16_DETAIL_OWNER = re.compile(r"\(\s*owner\s*:\s*(SP|JT|DC)\s*\)\s*$", re.I)
T16_TICKER = re.compile(r"\(\s*([A-Za-z][A-Za-z0-9.\-]{0,6})\s*\)")
T16_TICKER_OK = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z])?$")


def _t16_core_region(lines: list[str], template: str = "t2016") -> tuple[Optional[list[str]], list[str], list[str]]:
    """Returnerer (tabellinjer, detalje-linjer, advarsler). Tabellinjer er uden tabelhoveder og sidefødder."""
    warnings: list[str] = []
    start = next((i for i, ln in enumerate(lines) if T16_HEADER_ANY.search(ln)), None)
    if start is None:
        return None, [], [f"{template}: tabelhoved ikke fundet i layout-teksten"]
    end = next((i for i in range(start + 1, len(lines)) if _compact(lines[i]) in T16_END_WORDS), None)
    if end is None:
        warnings.append(f"{template}: ingen slutmarkør efter tabellen (Asset Class Details/IPO/Certification)")
        end = len(lines)
    body: list[str] = []
    in_header = True                         # linjerne lige efter tabelhovedet ("type Date", "Gains >", "$200?") er hovedets
    for ln in lines[start + 1:end]:
        if T16_HEADER_ANY.search(ln):
            in_header = True                 # gentaget tabelhoved ved sideskift
            continue
        if in_header:
            if not ln.strip() or _is_header_cont(ln):
                continue
            in_header = False
        if re.match(r"^\s*\*\s*For the complete list of asset type", ln, re.I):
            continue                         # fodnoten om aktivtype-koder (old_2021)
        if CHECKBOX_ONLY.match(ln):
            continue
        body.append(T16_FOOTER.sub("", ln).rstrip())
    details = lines[end + 1:]
    # detaljerne slutter ved IPO/Certification
    for i, ln in enumerate(details):
        if _compact(ln) in T16_DETAIL_END_WORDS:
            details = details[:i]
            break
    return body, details, warnings


def _t16_accounts(details: list[str]) -> dict[str, set[str]]:
    """Asset Class Details -> {konto (små bogstaver): mængde af ejere}. Samme kontonavn kan stå flere gange med
    forskellig ejer (fx medlemmets egen konto og en fælles konto med samme navn); så er alle ejere tilladt."""
    out: dict[str, set[str]] = {}
    for ln in details:
        t = re.sub(r"\s+", " ", ln).strip()
        if not t or re.match(r"^(location|description)\s*:", t, re.I) or _compact(t) == "assetclassdetails":
            continue
        m = T16_DETAIL_OWNER.search(t)
        name = T16_DETAIL_OWNER.sub("", t).strip() if m else t
        name = re.sub(r"\s*\(\s*\d+\s*%\s*interest\s*\)\s*$", "", name, flags=re.I).strip()
        out.setdefault(name.lower(), set()).add(OWNER_CODES[m.group(1).upper()] if m else "self")
    return out


def infer_asset_type(asset: str, ticker: str, description: str = "") -> tuple[str, str]:
    """
    Udleder aktivtype, når skabelonen ikke har en kode (t2016). Returnerer (type, begrundelse).
    type: ST (aktie), ET (ETF/ETN/fond med børsticker), MF (investeringsforening/mutual fund),
          OP (option/warrant), BD (obligation/note), CR (krypto), OT (præference-aktie),
          ? (ticker men ingen sikker evidens),
          '' (ingen ticker). Kun ST og ET er med i v1. Reglen er konservativ: er der tvivl, er den ikke med.
    Rækkefølgen er vigtig (første træf vinder):
      1. ingen gyldig ticker                    -> ''
      2. option-ord (call/put/option/strike/warrant/expir) i navn eller beskrivelse -> OP
      3. krypto-ord                              -> CR
      4. investeringsforening: ticker på 5 bogstaver der ender på X, eller 'mutual fund'/'Admiral'/
         'Institutional'/'Investor Shares'       -> MF
      5. ETF/ETN/exchange-traded eller rene ETF-udbydere (SPDR, iShares, PowerShares, WisdomTree,
         ProShares, RevenueShares, Direxion, VanEck, Global X) i navnet -> ET
      6. obligations-ord (bond/note/debenture/treasury/municipal)  -> BD
      7. 'Fund' i navnet (børsnoterede fonde har ikke X-tickere)   -> ET
      8. præference-aktie (preferred/pfd)       -> OT (ikke med)
      9. selskabsord (Inc, Corp, Company, Ltd, plc, Holdings, Group, Trust, Bank, 'Class A', 'Ordinary
         Shares', Financial, Energy ... )        -> ST
     10. ellers                                  -> ?  (tvivl, ikke med)
    """
    if not ticker:
        return "", "ingen ticker"
    if option_like_name(asset) or OPTION_WORDS.search(description):
        return "OP", "option-ord"
    if CRYPTO_NAME.search(asset):
        return "CR", "krypto-ord"
    if re.fullmatch(r"[A-Z]{4}X", ticker) or MF_NAME.search(asset):
        return "MF", "investeringsforening (ticker på 5 bogstaver med X, eller mutual-fund-ord)"
    if ETF_NAME.search(asset):
        return "ET", "ETF-ord i navnet"
    if BOND_NAME.search(asset):
        return "BD", "obligations-ord"
    if FUND_NAME.search(asset):
        return "ET", "'Fund' i navnet og ingen X-ticker"
    if OTHER_NAME.search(asset):
        return "OT", "præference-aktie (holdes ude)"
    if CORP_NAME.search(asset):
        return "ST", "selskabsord i navnet"
    return "?", "ticker, men intet i navnet bekræfter aktie/ETF (tvivl -> ikke med)"


OPTION_NAME = re.compile(r"\b(calls?|puts?|options?|strike|warrants?)\b|\bexpir", re.I)
ETF_WORD = re.compile(r"\b(etfs?|etns?)\b", re.I)


def option_like_name(asset: str) -> bool:
    """Option/warrant-ord i aktivnavnet. ETF'er som 'Covered Call ETF' er fonde og undtages."""
    return bool(OPTION_NAME.search(asset)) and not ETF_WORD.search(asset)

CRYPTO_NAME = re.compile(r"bitcoin|ethereum|crypto|grayscale", re.I)
MF_NAME = re.compile(r"mutual\s+fund|\badmiral\b|\binstitutional\b|\binvestor\s+shares\b", re.I)
ETF_NAME = re.compile(
    r"\b(etfs?|etns?|exchange[- ]traded|spdr|ishares|powershares|wisdomtree|proshares|revenueshares|direxion|"
    r"vaneck|van\s+eck|global\s+x)\b", re.I)
BOND_NAME = re.compile(r"\b(bonds?|notes?|debentures?|treasur(?:y|ies)|t-?bills?|municipal|munis?)\b", re.I)
FUND_NAME = re.compile(r"\bfund\b", re.I)
CORP_NAME = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|n\.?v|s\.?a|ag|holdings?|"
    r"group|bancorp|bancshares|bank|trust|ordinary\s+shares|common\s+(?:shares|stock)|adr|ads|"
    # generiske selskabsord (målt mod 2021-2026-data: hæver dækning uden at øge falske positive)
    r"financial|services|laboratories|energy|partners|products|insurance|properties|systems|technologies|"
    r"industries|pharmaceuticals|international|resources|communications|brands|stores|airlines|capital|"
    r"enterprises?|solutions|therapeutics|networks|motors|foods?|oil|gas|electric|water|realty|reit)\b|"
    r"\bclass\s+[a-c]\b", re.I)
# Præference-aktier handles ikke som almindelige aktier -> holdes ude (konservativt)
OTHER_NAME = re.compile(r"\b(preferred|pfd)\b", re.I)


def _parse_t2016(text: str, layout_text: Optional[str], header: dict, template: str = "t2016", core_only: bool = False) -> dict:
    """Layout-parser. template = t2016 (aktivtype udledes) eller old_2021 (aktivtype = kode [ST] i aktivteksten)."""
    warnings: list[str] = []
    records: list[dict] = []
    if not layout_text or not normalize(layout_text).strip():
        return {"header": header, "trades": [], "template": template, "warnings": [
            "2016-skabelon genkendt, men layout-tekst mangler (kræver pypdf extract_text(extraction_mode='layout')); "
            "0 handler læst"]}
    lines = normalize(layout_text).splitlines()
    body, details, w = _t16_core_region(lines, template)
    warnings += w
    if body is None:
        return {"header": header, "trades": [], "template": template, "warnings": warnings}
    accounts = _t16_accounts(details)

    cur: Optional[dict] = None
    field = "asset"          # hvilket felt en fortsættelseslinje hører til
    blank_before = False
    n_date_lines = 0
    for ln in body:
        if not ln.strip():
            blank_before = True
            continue
        core = T16_CORE.match(ln)
        if core and TWO_DATES.search(ln):
            n_date_lines += 1
            cur = {
                "owner": OWNER_CODES.get((core.group("owner") or "").upper(), "self"),
                "asset_parts": [core.group("asset")],
                "ttype": core.group("ttype").upper(), "partial": bool(core.group("partial")),
                "tdate": core.group("tdate"), "ndate": core.group("ndate"),
                "amount": CHECKBOX_TAIL.sub("", core.group("amount")),
                "filing_status": [], "sub_holding": [], "description": [], "location": [], "comments": [],
            }
            records.append(cur)
            field = "asset"
            blank_before = False
            continue
        if TWO_DATES.search(ln):
            n_date_lines += 1
            warnings.append(f"linje med to datoer kunne ikke læses som handel: {ln.strip()[:80]!r}")
            blank_before = False
            continue
        if cur is None:
            warnings.append(f"tekst før første handel ignoreret: {ln.strip()[:60]!r}")
            continue
        if core_only:
            continue                         # kun tabelrækken læses (resten af linjerne er ikke til at stole på)
        if field == "asset" and T16_AMOUNT_OPEN.search(cur["amount"]):
            # beløbet er delt over to linjer ("Spouse/DC Over" + "$1,000,000", eller "$5,000,001 -" + "$25,000,000")
            frag = T16_AMOUNT_FRAG.match(ln)
            if frag:
                cur["amount"] += " " + frag.group(1)
                blank_before = False
                continue
            parts = re.split(r"\s{2,}", ln.strip())
            frag = T16_AMOUNT_FRAG.match(parts[-1]) if len(parts) > 1 else None
            if frag:   # linjen er både en fortsættelse af aktivnavnet og sidste del af beløbet
                cur["amount"] += " " + frag.group(1)
                cur["asset_parts"].append(" ".join(parts[:-1]))
                blank_before = False
                continue
        m = T16_FS.match(ln)
        if m:
            cur["filing_status"].append(m.group(1)); field = "filing_status"; blank_before = False; continue
        m = T16_SUB.match(ln)
        if m:
            cur["sub_holding"].append(m.group(1)); field = "sub_holding"; blank_before = False; continue
        m = T16_DESC.match(ln)
        if m:
            cur["description"].append(m.group(1)); field = "description"; blank_before = False; continue
        m = T16_LOC.match(ln)
        if m:
            cur["location"].append(m.group(1)); field = "location"; blank_before = False; continue
        m = T16_COM.match(ln)
        if m:
            cur["comments"].append(m.group(1)); field = "comments"; blank_before = False; continue
        if field != "asset" and T16_OTHER_LABEL.match(ln):
            warnings.append(f"ukendt etiket under handel {len(records)}: {ln.strip()[:60]!r}")
            blank_before = False
            continue
        if blank_before and field != "asset":
            warnings.append(f"uventet linje efter tom linje under handel {len(records)}: {ln.strip()[:60]!r}")
        else:
            cur["asset_parts" if field == "asset" else field].append(ln.strip())
        blank_before = False

    if records and not core_only and not any(r["filing_status"] for r in records):
        warnings.append("ingen 'Filing Status'-linjer fundet; skabelonen afviger måske")
    out: list[dict] = []
    for i, c in enumerate(records, 1):
        asset = re.sub(r"\s+", " ", " ".join(c["asset_parts"])).strip()
        sub = re.sub(r"\s+", " ", " ".join(c["sub_holding"])).strip()
        desc = re.sub(r"\s+", " ", " ".join(c["description"])).strip()
        status = _fs_value(re.sub(r"\s+", " ", " ".join(c["filing_status"])).strip())
        location = re.sub(r"\s+", " ", " ".join(c["location"])).strip()
        comments = re.sub(r"\s+", " ", " ".join(c["comments"])).strip()
        if not c["filing_status"] and not core_only:
            warnings.append(f"handel {i} ({asset[:30]!r}) mangler 'Filing Status'")
        code = ""
        if template == "old_2021" or TAG_RE.search(asset):
            tags = TAG_RE.findall(asset)
            code = tags[-1].upper() if tags else ""
            asset = re.sub(r"\s+", " ", TAG_RE.sub("", asset)).strip()
            cands = TICKER_RE.findall(asset)
            ticker = cands[-1].upper() if cands else ""
        else:
            cands = T16_TICKER.findall(asset)
            ticker = ""
            if cands:
                t = cands[-1].upper()
                if T16_TICKER_OK.match(t):
                    ticker = t
                else:
                    warnings.append(f"handel {i}: '({cands[-1]})' ligner ikke en ticker og er ignoreret")
        low, high, aflag, _ = _parse_amount(AMOUNT_RE.search(c["amount"]))
        if low is None:
            warnings.append(f"handel {i}: beløb kunne ikke læses: {c['amount']!r}")
        tdate, ndate = _iso(c["tdate"]), _iso(c["ndate"])
        if tdate is None or ndate is None:
            warnings.append(f"handel {i}: ugyldig dato {c['tdate']} / {c['ndate']}")
        exp = accounts.get(sub.lower()) if sub else None
        if exp is not None and c["owner"] not in exp:
            warnings.append(f"handel {i}: ejer '{c['owner']}' stemmer ikke med Asset Class Details "
                            f"({sorted(exp)} for konto {sub!r})")
        if code:
            atype, reason = code, "kode i PDF'en"
        elif template == "old_2021":
            atype, reason = "", "ingen kode i PDF'en"
        else:
            atype, reason = infer_asset_type(asset, ticker, desc)
        # option/short-markeringen læser beskrivelse OG kommentar (konservativt: begge kan forklare en option/short-handel)
        option_like = bool(OPTION_WORDS.search(desc + " " + comments)) or atype == "OP"
        out.append({
            "owner": c["owner"],
            "asset": asset,
            "ticker": ticker,
            "asset_type": atype,
            "side": SIDE[c["ttype"]],
            "partial": c["partial"],
            "transaction_date": tdate,
            "notification_date": ndate,
            "amount_low": low,
            "amount_high": high,
            "amount_flag": aflag,
            "sub_holding": sub,
            "description": desc,
            "member": header["member"],
            "state_dst": header["state_dst"],
            "filing_id": header["filing_id"],
            "filing_date": header["signed_date"],
            "option_like": option_like,
            "short_related": bool(SHORT_WORDS.search(desc + " " + comments)),
            "is_stock": atype in STOCK_TYPES and not option_like,
            "location": location,
            "comments": comments,
            "template": template,
            "asset_type_source": "code" if code else ("none" if not atype else "inferred"),
            "asset_type_reason": reason,
            "filing_status": status,
        })
    n_fs = sum(1 for c in records if c["filing_status"])
    if n_fs != len(records) and not core_only:
        warnings.append(f"{len(records)} handler fundet, men {n_fs} 'Filing Status'-linjer")
    if n_date_lines != len(records):
        warnings.append(f"{n_date_lines} linjer med to datoer, men {len(records)} handler læst")
    if not out:
        warnings.append(f"ingen handler fundet i {template}-skabelonens tabel")
    return {"header": header, "trades": out, "template": template, "warnings": warnings,
            "structure_ok": bool(out) and n_fs == len(records) and n_date_lines == len(records)}


DATE_MAX_BEFORE_DAYS = 3 * 365 + 1      # "mere end ca. 3 år før filing_date"
YEAR_MIN, YEAR_MAX = 1990, 2100


def date_suspect_reason(tdate: Optional[str], filing_date: Optional[str]) -> str:
    """
    Tom streng, hvis handelsdatoen er plausibel, ellers en kort årsag (første træf vinder):
      ugyldig_dato                 handelsdatoen kunne ikke læses som dato (fx 02/30/2018) eller mangler
      umuligt_aar                  årstallet er før 1990 eller efter 2100 (tastefejl)
      filing_date_mangler          indekset har ingen filing_date at sammenligne med
      efter_filing_date            handelsdatoen ligger EFTER indberetningsdatoen
      over_3_aar_foer_filing_date  handelsdatoen ligger mere end 3 år før indberetningsdatoen
    Datoer rettes aldrig ved at gætte.
    """
    if not tdate:
        return "ugyldig_dato"
    try:
        t = datetime.fromisoformat(tdate)
    except ValueError:
        return "ugyldig_dato"
    if not YEAR_MIN <= t.year <= YEAR_MAX:
        return "umuligt_aar"
    if not filing_date:
        return "filing_date_mangler"
    try:
        gap = (datetime.fromisoformat(filing_date) - t).days
    except ValueError:
        return "filing_date_mangler"
    if gap < 0:
        return "efter_filing_date"
    if gap > DATE_MAX_BEFORE_DAYS:
        return "over_3_aar_foer_filing_date"
    return ""


def parse_ptr_text(text: str, layout_text: Optional[str] = None) -> dict:
    """
    Returnerer {'header': {...}, 'trades': [...], 'warnings': [...], 'template': str}.
    text = pypdf's almindelige tekst. layout_text = pypdf's layout-tekst (kun nødvendig for t2016;
    ignoreres for de øvrige skabeloner).
    """
    text = normalize(text)
    header = parse_header(text)
    template = detect_template(text, layout_text)
    if template == "t2016":
        if layout_text and normalize(layout_text).strip():
            return _parse_t2016(text, layout_text, header)
        # Ingen layout-tekst (fx en PDF fra en anden producent, hvor pypdf's layout-udtræk er tomt): prøv den almindelige
        # tekst, som i de PDF'er har én tabelrække pr. linje. Kun rækken læses; subholding/beskrivelse læses ikke.
        fb = _parse_t2016(text, text.replace("\xa0", " ").replace("\xad", "-"), header, core_only=True)
        if fb["trades"] and not any("linjer med to datoer" in w for w in fb["warnings"]):
            fb["warnings"].insert(0, "layout-tekst mangler: tabelrækkerne er læst fra den almindelige tekst; "
                                     "subholding, beskrivelse og Filing Status er ikke læst")
            fb["parse_source"] = "plain"
            return fb
        return _parse_t2016(text, layout_text, header)
    if template == "old_2021" and layout_text and normalize(layout_text).strip() and T16_HEADER_L1.search(normalize(layout_text)):
        lay = _parse_t2016(text, layout_text, header, "old_2021")
        plain = _parse_legacy(text, header)
        if lay.get("structure_ok"):
            lay["parse_source"] = "layout"
            lay["plain_n_trades"] = len(plain["trades"])
            return lay
        # layout-læsningen er ikke intern konsistent: brug den almindelige tekst og sig det
        plain["warnings"].append("layout-tekst fandtes, men blev ikke brugt (" +
                                 ("; ".join(lay["warnings"])[:150] or "0 handler i layout-teksten") + ")")
        plain["parse_source"] = "plain"
        return plain
    result = _parse_legacy(text, header)
    if template == "unknown":
        result["warnings"].append(
            "ukendt skabelon: hverken '$200?'-kolonnen (2021-) eller 2016-tabelhovedet blev fundet")
    result["template"] = template
    return result


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

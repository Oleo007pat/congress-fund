"""Strategy configuration for the live fund (live/strategy.json).

The rule is swappable without code changes: exit rule, weighting, skip threshold X,
sizing and an optional member filter.  The v1 eligibility rules (stocks/ETFs, all
owners, exclusions) are NOT configurable here; they are implemented by the backtest
functions so that live behaviour matches the backtest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.backtest import config as bt_config

STRATEGY_PATH = Path(__file__).resolve().parent / "strategy.json"
ORDER_MODES = ("auto", "whole_shares", "notional")
FILTER_RULES = ("score_gt_0", "top_percentile")


class StrategyError(ValueError):
    pass


def _num(d: dict, key: str, lo: float | None = None, hi: float | None = None, allow_none: bool = False):
    v = d.get(key)
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise StrategyError(f"strategy.json: '{key}' skal være et tal")
    if lo is not None and v < lo:
        raise StrategyError(f"strategy.json: '{key}' skal være >= {lo}")
    if hi is not None and v > hi:
        raise StrategyError(f"strategy.json: '{key}' skal være <= {hi}")
    return float(v)


def validate(cfg: dict) -> dict:
    """Returns cfg (unchanged) or raises StrategyError with a Danish message."""
    if not isinstance(cfg, dict):
        raise StrategyError("strategy.json skal være et JSON-objekt")
    if cfg.get("eligibility", {}).get("version") != "v1":
        raise StrategyError("strategy.json: kun eligibility.version 'v1' er implementeret")
    rule = cfg.get("exit", {}).get("rule")
    if rule not in bt_config.EXIT_RULES:
        raise StrategyError(f"strategy.json: exit.rule skal være en af {bt_config.EXIT_RULES}")
    if cfg.get("weighting") not in bt_config.WEIGHTINGS:
        raise StrategyError(f"strategy.json: weighting skal være en af {bt_config.WEIGHTINGS}")
    _num(cfg, "skip_rise_pct", lo=0, allow_none=True)      # null = never skip
    _num(cfg.get("entry", {}), "max_entry_lag_days", lo=1)
    s = cfg.get("sizing", {})
    for k, lo in (("base_pct_of_equity", 0.0), ("clip_min", 0.0), ("clip_max", 0.0),
                  ("median_midpoint_usd", 0.0), ("max_position_pct_of_equity", 0.0)):
        if _num(s, k, lo=lo) <= 0:
            raise StrategyError(f"strategy.json: sizing.{k} skal være > 0")
    if s["clip_min"] > s["clip_max"]:
        raise StrategyError("strategy.json: sizing.clip_min må ikke være større end clip_max")
    if s["max_position_pct_of_equity"] > 100 or s["base_pct_of_equity"] > s["max_position_pct_of_equity"]:
        raise StrategyError("strategy.json: ugyldige procentgrænser i sizing")
    o = cfg.get("orders", {})
    if o.get("mode") not in ORDER_MODES:
        raise StrategyError(f"strategy.json: orders.mode skal være en af {ORDER_MODES}")
    _num(o, "gap_buffer_pct", lo=0, hi=50)
    mf = cfg.get("member_filter", {})
    if not isinstance(mf.get("enabled"), bool):
        raise StrategyError("strategy.json: member_filter.enabled skal være true/false")
    if mf["enabled"]:
        if mf.get("rule") not in FILTER_RULES:
            raise StrategyError(f"strategy.json: member_filter.rule skal være en af {FILTER_RULES}")
        _num(mf, "trailing_window_days", lo=1)
        _num(mf, "min_completed_trades", lo=1)
        _num(mf, "shrinkage_k", lo=0)
        _num(mf, "top_percentile", lo=0.0001, hi=100)
        if mf.get("on_insufficient_history") not in ("skip", "allow"):
            raise StrategyError("strategy.json: member_filter.on_insufficient_history skal være 'skip' eller 'allow'")
    return cfg


def load(path: Path | str = STRATEGY_PATH) -> dict:
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StrategyError(f"strategy.json kunne ikke læses ({type(exc).__name__})") from exc
    return validate(cfg)


def strategy_hash(cfg: dict) -> str:
    """Hash of the rule-relevant content (descriptive notes excluded), stable under key order."""
    core = {k: v for k, v in cfg.items() if k not in ("locked_note",)}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def hold_months(cfg: dict) -> int | None:
    """Fixed holding period in calendar months, or None for the member_sells rule."""
    return bt_config.HOLD_MONTHS.get(cfg["exit"]["rule"])


def describe_da(cfg: dict) -> str:
    """Strategy description in Danish, generated from the active configuration."""
    rule = cfg["exit"]["rule"]
    x = cfg.get("skip_rise_pct")
    s = cfg["sizing"]
    parts = ["Fonden kopierer køb af aktier og ETF'er, som medlemmer af USA's Repræsentanternes Hus indberetter "
             "(Periodic Transaction Reports), uanset hvem i familien der ejer papiret. Optioner, short-relaterede handler, "
             "handler der ikke var medlemmets eget valg (fx tildelte aktier og geninvestering), dubletter og handler med "
             "mistænkelig dato indgår ikke."]
    parts.append("Når en indberetning er offentliggjort, sendes en market-on-open-ordre på Alpacas papirkonto til "
                 "næste børsåbning.")
    if x is None:
        parts.append("Der springes ikke over på grund af kursudviklingen.")
    else:
        parts.append(f"Købet springes kun over, hvis kursen er steget mere end {x:g} % siden medlemmets handelsdag "
                     "(et kursfald giver aldrig et skip).")
    if rule == "member_sells":
        parts.append("Positionen sælges ved næste åbning efter, at medlemmet selv har indberettet salg af papiret.")
    else:
        m = hold_months(cfg)
        parts.append(f"Positionen holdes i {m} måneder og sælges derefter til lukkekursen (market-on-close).")
    if cfg["weighting"] == "amount":
        med = f"{s['median_midpoint_usd']:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
        parts.append(f"Positionens størrelse følger beløbet i indberetningen: {s['base_pct_of_equity']:g} % af kapitalen "
                     f"gange beløbets midtpunkt i forhold til medianen ({med} USD), begrænset til "
                     f"{str(s['clip_min']).replace('.', ',')}-{s['clip_max']:g} gange.")
    else:
        parts.append(f"Alle køb vægtes ens: {s['base_pct_of_equity']:g} % af kapitalen pr. køb.")
    parts.append(f"Ingen position over {s['max_position_pct_of_equity']:g} % af kapitalen; kun kontanter (ingen gearing, "
                 "ingen shortsalg).")
    if cfg.get("member_filter", {}).get("enabled"):
        parts.append("Et medlemsfilter baseret på medlemmets tidligere afsluttede, kopierede handler er slået til.")
    parts.append("Reglen er låst på forhånd og er den samme som i backtesten (hold 6 mdr., beløbsvægtet, X = 10 %), "
                 "men handler i realtid med fiktive penge." if (rule == "hold_6m" and cfg["weighting"] == "amount"
                                                              and x == 10.0) else
                 "Reglen er låst på forhånd (se live/strategy.json).")
    return " ".join(parts)

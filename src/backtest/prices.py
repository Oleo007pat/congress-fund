"""Price data: trading calendar, valid bars, segments.

PriceBook holds dense (n_days x n_symbols) matrices aligned to the benchmark
(SPY) trading calendar.  Nothing in here looks at trades; every lookup that the
signal / exit logic needs is a pure index function documented below.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


class PriceBook:
    """Aligned price matrices.

    Attributes (n = number of calendar days, S = number of symbols):
      cal         datetime64[D] array, the trading calendar (SPY's dates).
      open, close float (n, S); NaN where the symbol has no VALID bar that day.
      valid       bool (n, S): bar exists, volume > 0, open > 0, close > 0.
      seg         int32 (n, S): piece ("segment") id for days between the first and last
                  valid bar of a segment (incl. invalid days inside it); -1 outside.
      seg_end     int32 (n, S): index of the last valid bar of the day's segment.
      close_ff    float (n, S): last valid close carried forward INSIDE a segment
                  (used only to value open positions on days without a valid bar).
      last_valid  int32 (n, S): index of the last valid bar on/before day t (-1 if none).
      next_valid  int32 (n, S): index of the first valid bar on/after day t (n if none).
      alive       bool (S): the symbol's final segment reaches the end of data
                  (last valid bar within GAP_BREAK trading days of the last calendar day).
      extreme_cum int32 (n, S): cumulative count of "extreme" one-day moves (flag only).
    """

    def __init__(self, cal, frames: dict[str, pd.DataFrame], gap_break: int = config.GAP_BREAK):
        self.cal = np.asarray(cal, dtype="datetime64[D]")
        n = len(self.cal)
        self.symbols = list(frames)
        self.col = {s: i for i, s in enumerate(self.symbols)}
        S = len(self.symbols)
        self.open = np.full((n, S), np.nan)
        self.close = np.full((n, S), np.nan)
        self.valid = np.zeros((n, S), dtype=bool)
        cal_index = pd.Index(self.cal)
        for s, df in frames.items():
            j = self.col[s]
            d = np.asarray(pd.to_datetime(df["date"]).values, dtype="datetime64[D]")
            pos = cal_index.get_indexer(d)
            if (pos < 0).any():
                raise ValueError(f"{s}: dates outside the benchmark calendar")
            o = df["open"].to_numpy(float)
            c = df["close"].to_numpy(float)
            v = df["volume"].to_numpy(float)
            ok = (v > 0) & (o > 0) & (c > 0) & np.isfinite(o) & np.isfinite(c)
            # Zero-volume bars are treated as "no trade happened" (stale filler).
            self.open[pos[ok], j] = o[ok]
            self.close[pos[ok], j] = c[ok]
            self.valid[pos[ok], j] = True
        self._build_index(gap_break)

    # ------------------------------------------------------------------
    def _build_index(self, gap_break: int):
        """Split each symbol's valid bars into PIECES (round 2, F6).

        A new piece starts after a run of invalid days only if the run is longer
        than ``gap_break`` trading days AND either the run is longer than
        PIECE_MAX_GAP trading days or the price on resumption is not continuous
        (first open / last close outside [1/PIECE_RATIO, PIECE_RATIO]).  So a
        trading halt or an illiquid stretch with a continuous price stays in the
        same piece: a held position waits through the gap and can only exit on the
        first bar after it (no knowledge of the gap's length is needed).  A piece
        that is never continued "truly ends": positions exit at its last close.
        """
        n, S = self.valid.shape
        self.seg = np.full((n, S), -1, dtype=np.int32)
        self.seg_end = np.full((n, S), -1, dtype=np.int32)
        self.close_ff = np.full((n, S), np.nan)
        self.last_valid = np.full((n, S), -1, dtype=np.int32)
        self.next_valid = np.full((n, S), n, dtype=np.int32)
        self.alive = np.zeros(S, dtype=bool)
        self.n_segments = np.zeros(S, dtype=np.int32)
        self.n_long_gaps_within_piece = np.zeros(S, dtype=np.int32)
        ar = np.arange(n)
        for j in range(S):
            p = np.flatnonzero(self.valid[:, j])
            if len(p) == 0:
                continue
            lv = np.where(self.valid[:, j], ar, -1)
            self.last_valid[:, j] = np.maximum.accumulate(lv)
            nv = np.where(self.valid[:, j], ar, n)
            self.next_valid[:, j] = np.minimum.accumulate(nv[::-1])[::-1]
            gaps = np.diff(p) - 1  # invalid trading days between consecutive valid bars
            jump = self.open[p[1:], j] / self.close[p[:-1], j]
            long_gap = gaps > gap_break
            discontinuous = (jump > config.PIECE_RATIO) | (jump < 1.0 / config.PIECE_RATIO)
            brk = np.flatnonzero(long_gap & (discontinuous | (gaps > config.PIECE_MAX_GAP)))
            self.n_long_gaps_within_piece[j] = int(long_gap.sum()) - len(brk)
            starts = np.r_[p[0], p[brk + 1]]
            ends = np.r_[p[brk], p[-1]]
            for k, (a, b) in enumerate(zip(starts, ends)):
                self.seg[a:b + 1, j] = k
                self.seg_end[a:b + 1, j] = b
                c = self.close[a:b + 1, j]
                self.close_ff[a:b + 1, j] = pd.Series(c).ffill().to_numpy()
            self.n_segments[j] = len(starts)
            self.alive[j] = ends[-1] >= n - 1 - gap_break
        self._jump_cache = {}
        self.extreme_cum = self.jump_cum(config.EXTREME_UP, config.EXTREME_DOWN)

    def jump_cum(self, up: float, down: float) -> np.ndarray:
        """Cumulative count (n+1, S) of one-day close/close moves on valid bars inside a
        piece with ratio > up or < down.  Data-quality FLAG ONLY (uses the whole
        series), never used by a trading rule: flag(window) = cum[x] - cum[e] > 0."""
        key = (up, down)
        if key not in self._jump_cache:
            S = self.valid.shape[1]
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = self.close_ff[1:] / self.close_ff[:-1]
            same = (self.seg[1:] == self.seg[:-1]) & (self.seg[1:] >= 0)
            ext = same & self.valid[1:] & ((ratio > up) | (ratio < down))
            self._jump_cache[key] = np.vstack([np.zeros((1, S), np.int32), np.cumsum(ext, axis=0, dtype=np.int32)])
        return self._jump_cache[key]

    # ------------------------------------------------------------------
    # Calendar helpers (R2).  The calendar is the exchange schedule; using it to
    # find "the next trading day" uses no price information.
    def first_day_after(self, date) -> int:
        """Index of the first trading day STRICTLY after `date` (n if none)."""
        return int(np.searchsorted(self.cal, np.datetime64(pd.Timestamp(date).date(), "D"), side="right"))

    def first_day_on_or_after(self, date) -> int:
        return int(np.searchsorted(self.cal, np.datetime64(pd.Timestamp(date).date(), "D"), side="left"))

    def last_day_on_or_before(self, date) -> int:
        """Index of the last trading day on/before `date` (-1 if none)."""
        return int(np.searchsorted(self.cal, np.datetime64(pd.Timestamp(date).date(), "D"), side="right")) - 1

    def date(self, i: int) -> str:
        return str(self.cal[i])


def load_pricebook(symbols, price_dir=config.PRICE_DIR, bench=config.BENCH_SYMBOL) -> PriceBook:
    """Load the benchmark (defines the calendar) plus the given symbols."""
    bench_df = pd.read_csv(price_dir / f"{bench}.csv")
    cal = pd.to_datetime(bench_df["date"]).values.astype("datetime64[D]")
    frames = {bench: bench_df}
    missing = []
    for s in sorted(set(symbols) - {bench}):
        f = price_dir / f"{s}.csv"
        if not f.exists():
            missing.append(s)
            continue
        frames[s] = pd.read_csv(f)
    if missing:
        # Essential input broken: a mapped symbol without its price file. Stop.
        raise FileNotFoundError(f"price files missing for mapped symbols: {missing[:20]} (n={len(missing)})")
    return PriceBook(cal, frames)

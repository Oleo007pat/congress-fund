"""Round-2 statistics: significance vs SPY (F4), calendar-year table (F1)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def newey_west_ols(y: np.ndarray, X: np.ndarray, lags: int = config.NW_LAGS):
    """OLS coefficients and Newey-West (Bartlett kernel) standard errors."""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    n = len(y)
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    u = y - X @ beta
    Xu = X * u[:, None]
    S = Xu.T @ Xu
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        G = Xu[lag:].T @ Xu[:-lag]
        S += w * (G + G.T)
    V = XtX_inv @ S @ XtX_inv
    return beta, np.sqrt(np.diag(V)), u, n


def significance(r: np.ndarray, rb: np.ndarray, lags: int = config.NW_LAGS) -> dict:
    """Daily strategy returns r vs benchmark rb (same days).
    alpha/beta from r = a + b*rb + e (NW s.e.), mean daily excess with NW t-stat,
    information ratio (annualised mean excess / tracking error), and Memmel (2003)
    test of the Sharpe-ratio difference (rf = 0, iid assumption)."""
    r = np.asarray(r, float)
    rb = np.asarray(rb, float)
    T = len(r)
    X = np.column_stack([np.ones(T), rb])
    (a, b), (sa, sb), _, _ = newey_west_ols(r, X, lags)
    ex = r - rb
    (m,), (sm,), _, _ = newey_west_ols(ex, np.ones((T, 1)), lags)
    te = ex.std(ddof=1) * np.sqrt(config.ANNUALIZATION)
    sr1, sr2 = r.mean() / r.std(ddof=1), rb.mean() / rb.std(ddof=1)
    rho = np.corrcoef(r, rb)[0, 1]
    theta = 2 - 2 * rho + 0.5 * (sr1 ** 2 + sr2 ** 2 - 2 * sr1 * sr2 * rho ** 2)
    z = (sr1 - sr2) / np.sqrt(theta / T)
    return {"n_days": T,
            "alpha_daily": a, "alpha_annual": a * config.ANNUALIZATION, "alpha_t_nw": a / sa, "beta": b, "beta_t_nw": b / sb,
            "beta_minus_1_t_nw": (b - 1) / sb,
            "mean_excess_annual": m * config.ANNUALIZATION, "mean_excess_t_nw": m / sm,
            "tracking_error": te, "information_ratio": m * config.ANNUALIZATION / te if te > 0 else np.nan,
            "sharpe_strategy": sr1 * np.sqrt(config.ANNUALIZATION), "sharpe_spy": sr2 * np.sqrt(config.ANNUALIZATION),
            "sharpe_diff_memmel_z": z, "corr_with_spy": rho, "nw_lags": lags}


def yearly_table(dates: np.ndarray, r: np.ndarray, rb: np.ndarray) -> pd.DataFrame:
    """Calendar-year compounded returns of strategy and benchmark (same days)."""
    df = pd.DataFrame({"year": pd.DatetimeIndex(dates).year, "r": r, "rb": rb})
    g = df.groupby("year")
    out = pd.DataFrame({"n_days": g.size(),
                        "strategy_return": g["r"].apply(lambda x: float(np.prod(1 + x) - 1)),
                        "spy_return": g["rb"].apply(lambda x: float(np.prod(1 + x) - 1))})
    out["excess"] = out["strategy_return"] - out["spy_return"]
    out["period"] = np.where(out.index <= int(config.IS_END[:4]), "in_sample_years", "out_of_sample_years")
    return out.reset_index()

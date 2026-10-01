"""Weekly demand forecasting per product, backtested with a rolling origin.

Two targets are forecast for every product and week t, using only weeks before t:
  * `units`      - demand in week t (accuracy table);
  * `units_next` - demand over weeks t .. t+PROTECTION-1 (what a replenishment order has to cover).

One gradient-boosting model is shared by all products and refitted every REFIT_EVERY weeks on the
history available at that point.
"""
import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from retail.config import SEED

TEST_WEEKS = 26
REFIT_EVERY = 4
PROTECTION = 3          # weeks an order must cover: 2 weeks lead time + 1 week until the next review
MIN_ACTIVE_WEEKS = 80   # a product needs sales in at least this many weeks to be forecast
LAGS = [1, 2, 3, 4, 8, 13, 26, 50, 51, 52, 53]
FEATURES = (
    [f"lag{k}" for k in LAGS]
    + ["mean4", "mean8", "mean13", "mean26", "median8", "median13", "std8", "max13", "zero_share13", "trend",
       "last_year_next", "last_year_lift", "history_mean", "orders_lag1", "orders_mean4", "orders_mean13",
       "units_per_order13", "store_lag1", "store_mean4", "store_last_year_lift", "week_of_year", "weeks_of_history"]
)


def weekly_panel(conn: duckdb.DuckDBPyConnection, min_active_weeks: int = MIN_ACTIVE_WEEKS) -> pd.DataFrame:
    """Products x complete weeks, zero-filled. The final, partial week of the data is dropped."""
    weekly = conn.execute("SELECT week_start, stock_code, units, orders FROM mart_product_weekly").df()
    weekly["week_start"] = pd.to_datetime(weekly["week_start"])
    last_day = pd.Timestamp(conn.execute("SELECT max(invoice_date) FROM fact_sales").fetchone()[0])
    last_complete = last_day - pd.Timedelta(days=last_day.weekday() + 7) if last_day.weekday() < 6 else last_day - pd.Timedelta(days=6)
    weeks = pd.date_range(weekly["week_start"].min(), last_complete, freq="W-MON")
    weekly = weekly[weekly["week_start"] <= last_complete]
    active = weekly.groupby("stock_code")["week_start"].nunique()
    keep = active[active >= min_active_weeks].index
    grid = pd.MultiIndex.from_product([sorted(keep), weeks], names=["stock_code", "week_start"])
    panel = weekly.set_index(["stock_code", "week_start"])[["units", "orders"]].reindex(grid, fill_value=0.0)
    return panel.astype(float).reset_index()


def add_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Every feature of week t is built from weeks before t (shift >= 1), so nothing leaks from the future."""
    out = panel.copy()
    g = out.groupby("stock_code", sort=False)["units"]
    for k in LAGS:
        out[f"lag{k}"] = g.shift(k)
    past = g.shift(1)
    by = out["stock_code"]
    for n in (4, 8, 13, 26):
        out[f"mean{n}"] = past.groupby(by, sort=False).rolling(n, min_periods=1).mean().reset_index(level=0, drop=True)
    for n in (8, 13):
        out[f"median{n}"] = past.groupby(by, sort=False).rolling(n, min_periods=1).median().reset_index(level=0, drop=True)
    out["zero_share13"] = (past == 0).astype(float).where(past.notna()).groupby(by, sort=False).rolling(13, min_periods=1).mean().reset_index(level=0, drop=True)
    out["std8"] = past.groupby(by, sort=False).rolling(8, min_periods=2).std().reset_index(level=0, drop=True)
    out["max13"] = past.groupby(by, sort=False).rolling(13, min_periods=1).max().reset_index(level=0, drop=True)
    out["trend"] = out["mean4"] / (out["mean13"] + 1.0)
    # Same weeks one year ago: what the coming PROTECTION weeks sold then, and how that compared with
    # the quarter before them. This is how the model sees the run-up to Christmas.
    out["last_year_next"] = sum(g.shift(52 - k) for k in range(PROTECTION))
    year_ago_base = g.shift(53).groupby(by, sort=False).rolling(13, min_periods=4).mean().reset_index(level=0, drop=True)
    out["last_year_lift"] = (out["last_year_next"] / PROTECTION) / (year_ago_base + 1.0)
    out["history_mean"] = past.groupby(by, sort=False).expanding(min_periods=1).mean().reset_index(level=0, drop=True)

    # Order counts are steadier than units, which a single bulk order can multiply.
    past_orders = out.groupby("stock_code", sort=False)["orders"].shift(1)
    out["orders_lag1"] = past_orders
    for n in (4, 13):
        out[f"orders_mean{n}"] = past_orders.groupby(by, sort=False).rolling(n, min_periods=1).mean().reset_index(level=0, drop=True)
    out["units_per_order13"] = out["mean13"] / (out["orders_mean13"] + 0.1)

    store = out.groupby("week_start")["units"].sum().sort_index()
    store_features = pd.DataFrame({
        "store_lag1": store.shift(1),
        "store_mean4": store.shift(1).rolling(4, min_periods=1).mean(),
        "store_last_year_lift": store.shift(52) / (store.shift(53).rolling(13, min_periods=4).mean() + 1.0),
    })
    out = out.join(store_features, on="week_start")
    out["week_of_year"] = out["week_start"].dt.isocalendar().week.astype(int)
    out["weeks_of_history"] = out.groupby("stock_code", sort=False).cumcount()
    # Demand over the protection period starting at week t (NaN where the period runs past the data).
    out["units_next"] = sum(g.shift(-k) for k in range(PROTECTION))
    return out


def make_model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="poisson", learning_rate=0.05, max_iter=300, max_leaf_nodes=31, min_samples_leaf=30,
        l2_regularization=1.0, random_state=SEED,
    )


def backtest(features: pd.DataFrame, test_weeks: int = TEST_WEEKS, refit_every: int = REFIT_EVERY) -> pd.DataFrame:
    """Rolling-origin forecasts for the last `test_weeks` weeks. Returns one row per product and test week."""
    weeks = np.sort(features["week_start"].unique())
    test = weeks[-test_weeks:]
    usable = features[features["weeks_of_history"] >= 13]  # skip the cold-start weeks of each series
    rows = []
    for start in range(0, test_weeks, refit_every):
        block = test[start:start + refit_every]
        origin = block[0]
        target_weeks = features[features["week_start"].isin(block)].copy()
        # A row's target must be fully observed before the origin: week < origin for `units`,
        # week + PROTECTION - 1 < origin for `units_next`.
        train_1 = usable[usable["week_start"] < origin]
        cutoff_n = weeks[np.searchsorted(weeks, origin) - PROTECTION + 1]
        train_n = usable[usable["week_start"] < cutoff_n]
        target_weeks["gbm"] = make_model().fit(train_1[FEATURES], train_1["units"]).predict(target_weeks[FEATURES])
        target_weeks["gbm_next"] = make_model().fit(train_n[FEATURES], train_n["units_next"]).predict(target_weeks[FEATURES])
        rows.append(target_weeks)
    out = pd.concat(rows, ignore_index=True)
    out["last_week"] = out["lag1"]
    out["average4"] = out["mean4"]
    out["last_year"] = out["lag52"]
    out["last_week_next"] = out["lag1"] * PROTECTION
    out["average4_next"] = out["mean4"] * PROTECTION
    out["last_year_next_forecast"] = out["last_year_next"]
    keep = ["stock_code", "week_start", "units", "units_next", "gbm", "last_week", "average4", "last_year",
            "gbm_next", "last_week_next", "average4_next", "last_year_next_forecast"]
    return out[keep]


def wape(actual: np.ndarray, forecast: np.ndarray) -> float:
    """Weighted absolute percentage error: total absolute error over total demand."""
    return float(np.abs(forecast - actual).sum() / actual.sum())


def bias(actual: np.ndarray, forecast: np.ndarray) -> float:
    return float((forecast - actual).sum() / actual.sum())


MODELS = {
    "Last week": ("last_week", "last_week_next"),
    "Average of last 4 weeks": ("average4", "average4_next"),
    "Same week last year": ("last_year", "last_year_next_forecast"),
    "Gradient boosting": ("gbm", "gbm_next"),
}


def accuracy(forecasts: pd.DataFrame) -> list[dict]:
    rows = []
    for name, (one, multi) in MODELS.items():
        a1 = forecasts.dropna(subset=["units", one])
        an = forecasts.dropna(subset=["units_next", multi])
        per_product = a1.groupby("stock_code").apply(
            lambda d, c=one: np.abs(d[c] - d["units"]).sum() / max(d["units"].sum(), 1.0), include_groups=False)
        rows.append({
            "model": name,
            "wape": round(wape(a1["units"].to_numpy(), a1[one].to_numpy()), 6),
            "bias": round(bias(a1["units"].to_numpy(), a1[one].to_numpy()), 6),
            "median_product_wape": round(float(per_product.median()), 6),
            "wape_next": round(wape(an["units_next"].to_numpy(), an[multi].to_numpy()), 6),
            "bias_next": round(bias(an["units_next"].to_numpy(), an[multi].to_numpy()), 6),
        })
    return rows


def run(conn: duckdb.DuckDBPyConnection, min_active_weeks: int = MIN_ACTIVE_WEEKS,
        test_weeks: int = TEST_WEEKS) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    panel = weekly_panel(conn, min_active_weeks)
    features = add_features(panel)
    forecasts = backtest(features, test_weeks)
    table = accuracy(forecasts)
    best_baseline = min((r for r in table if r["model"] != "Gradient boosting"), key=lambda r: r["wape"])
    gbm = next(r for r in table if r["model"] == "Gradient boosting")
    total = conn.execute("SELECT sum(revenue) FROM mart_product").fetchone()[0]
    covered = conn.execute(
        "SELECT sum(revenue) FROM mart_product WHERE stock_code IN (SELECT DISTINCT stock_code FROM panel)").fetchone()[0]
    weekly_total = forecasts.groupby("week_start")[["units", "gbm", "average4", "last_year"]].sum().round(0)
    summary = {
        "products": int(panel["stock_code"].nunique()), "weeks": int(panel["week_start"].nunique()),
        "first_week": str(panel["week_start"].min().date()), "last_week": str(panel["week_start"].max().date()),
        "test_weeks": test_weeks, "test_start": str(forecasts["week_start"].min().date()),
        "refit_every": REFIT_EVERY, "protection_weeks": PROTECTION, "min_active_weeks": min_active_weeks,
        "revenue_share": round(float(covered / total), 6),
        "accuracy": table,
        "best_baseline": best_baseline["model"],
        "wape_reduction_vs_best_baseline": round(1 - gbm["wape"] / best_baseline["wape"], 6),
        "weekly_total": [{"week": str(w.date()), **{k: float(v) for k, v in r.items()}} for w, r in weekly_total.iterrows()],
    }
    return summary, forecasts, panel

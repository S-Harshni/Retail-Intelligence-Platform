"""Replenishment simulation: does a better forecast buy the same service with less stock?

Weekly periodic review with an order-up-to level. An order placed at the start of week t arrives
LEAD_TIME weeks later, so it has to cover demand for LEAD_TIME + 1 weeks (the protection period):

    order-up-to level = forecast of protection-period demand + z x sigma

Both policies use the same sigma (how much the product's protection-period demand varied before the
test), so the only difference between them is the forecast. Unmet demand is lost. Sweeping z traces
each policy's trade-off between fill rate and stock held.
"""
import numpy as np
import pandas as pd

from retail.forecasting import PROTECTION

LEAD_TIME = PROTECTION - 1
Z_VALUES = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
POLICIES = {"Gradient boosting forecast": "gbm_next", "Average of last 4 weeks": "average4_next"}


def simulate(demand: np.ndarray, forecast: np.ndarray, sigma: np.ndarray, z: float, price: np.ndarray,
             lead_time: int = LEAD_TIME) -> dict:
    """demand, forecast: products x weeks. Returns value-weighted fill rate and average stock value."""
    products, weeks = demand.shape
    level = np.maximum(forecast + z * sigma[:, None], 0.0)
    on_hand = level[:, 0].copy()
    arrivals = np.zeros((products, weeks + lead_time + 1))
    served = np.zeros(products)
    stock = np.zeros(products)
    for t in range(weeks):
        on_hand += arrivals[:, t]
        position = on_hand + arrivals[:, t + 1:].sum(axis=1)
        arrivals[:, t + lead_time] += np.maximum(level[:, t] - position, 0.0)
        sold = np.minimum(on_hand, demand[:, t])
        on_hand -= sold
        served += sold
        stock += on_hand
    return {
        "z": z,
        "fill_rate": float((served * price).sum() / (demand.sum(axis=1) * price).sum()),
        "average_stock_value": float((stock / weeks * price).sum()),
    }


def stock_for_fill_rate(points: list[dict], target: float) -> float | None:
    """Stock value needed to reach `target` fill rate, interpolated along a policy's frontier."""
    fill = np.array([p["fill_rate"] for p in points])
    stock = np.array([p["average_stock_value"] for p in points])
    if target < fill.min() or target > fill.max():
        return None
    return float(np.interp(target, fill, stock))


def run(forecasts: pd.DataFrame, panel: pd.DataFrame, prices: pd.Series) -> dict:
    test = forecasts.dropna(subset=["units_next"]).sort_values(["stock_code", "week_start"])
    products = test["stock_code"].unique()
    shape = (len(products), test["week_start"].nunique())
    demand = test["units"].to_numpy().reshape(shape)
    price = prices.reindex(products).to_numpy()

    # Sigma from history only: spread of rolling protection-period demand before the first test week.
    history = panel[panel["week_start"] < test["week_start"].min()].sort_values(["stock_code", "week_start"])
    rolling = history.groupby("stock_code")["units"].rolling(PROTECTION).sum().reset_index(level=0, drop=True)
    sigma = rolling.groupby(history["stock_code"]).std().reindex(products).to_numpy()

    frontier = {
        name: [simulate(demand, test[col].to_numpy().reshape(shape), sigma, z, price) for z in Z_VALUES]
        for name, col in POLICIES.items()
    }
    model, baseline = frontier["Gradient boosting forecast"], frontier["Average of last 4 weeks"]
    comparison = []
    for target in (0.90, 0.95, 0.98):
        a, b = stock_for_fill_rate(model, target), stock_for_fill_rate(baseline, target)
        if a is not None and b is not None:
            comparison.append({"fill_rate": target, "model_stock": round(a), "baseline_stock": round(b),
                               "stock_reduction": round(1 - a / b, 6)})
    same_z = [
        {"z": z, "model_fill": round(m["fill_rate"], 6), "baseline_fill": round(b["fill_rate"], 6),
         "model_stock": round(m["average_stock_value"]), "baseline_stock": round(b["average_stock_value"])}
        for z, m, b in zip(Z_VALUES, model, baseline, strict=True)
    ]
    return {
        "products": int(shape[0]), "weeks": int(shape[1]), "lead_time_weeks": LEAD_TIME, "protection_weeks": PROTECTION,
        "demand_value": round(float((demand.sum(axis=1) * price).sum())),
        "frontier": {k: [{"z": p["z"], "fill_rate": round(p["fill_rate"], 6),
                          "average_stock_value": round(p["average_stock_value"])} for p in v] for k, v in frontier.items()},
        "comparison": comparison, "same_z": same_z,
    }

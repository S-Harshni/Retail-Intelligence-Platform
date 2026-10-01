"""Forecasting, inventory, recommendations and the customer model."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from retail import customer_model, forecasting, inventory, recommender


def synthetic_panel(products: int = 3, weeks: int = 70, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.MultiIndex.from_product(
        [[f"P{i}" for i in range(products)], pd.date_range("2010-01-04", periods=weeks, freq="W-MON")],
        names=["stock_code", "week_start"])
    return pd.DataFrame({"units": rng.poisson(20, len(index)).astype(float),
                         "orders": rng.poisson(5, len(index)).astype(float)}, index=index).reset_index()


def test_features_use_only_the_past():
    panel = synthetic_panel()
    changed = panel.copy()
    cut = panel["week_start"].unique()[40]
    future = changed["week_start"] >= cut
    changed.loc[future, ["units", "orders"]] = changed.loc[future, ["units", "orders"]] * 7 + 3
    a, b = forecasting.add_features(panel), forecasting.add_features(changed)
    past = a["week_start"] <= cut  # the row for week `cut` itself may only see weeks before it
    pd.testing.assert_frame_equal(a.loc[past, forecasting.FEATURES], b.loc[past, forecasting.FEATURES])


def test_protection_period_target():
    features = forecasting.add_features(synthetic_panel(products=1, weeks=30))
    units = features["units"].to_numpy()
    assert features["units_next"].iloc[5] == units[5:5 + forecasting.PROTECTION].sum()
    assert features["units_next"].iloc[-(forecasting.PROTECTION - 1):].isna().all()


def test_error_metrics():
    actual, forecast = np.array([10.0, 0.0, 30.0]), np.array([12.0, 4.0, 24.0])
    assert forecasting.wape(actual, forecast) == pytest.approx(12 / 40)
    assert forecasting.bias(actual, forecast) == pytest.approx(0.0)


def test_backtest_on_real_data(models):
    summary, forecasts = models["forecast"], models["forecasts"]
    assert forecasts["week_start"].nunique() == 8 and forecasts[["gbm", "average4", "last_week"]].notna().all().all()
    assert (forecasts["gbm"] >= 0).all()
    table = {r["model"]: r for r in summary["accuracy"]}
    assert table["Gradient boosting"]["wape"] < table["Last week"]["wape"]
    assert table["Gradient boosting"]["wape_next"] < table["Average of last 4 weeks"]["wape_next"]


def test_inventory_simulation():
    rng = np.random.default_rng(1)
    demand = rng.poisson(30, size=(4, 20)).astype(float)
    price, sigma = np.ones(4), np.full(4, 9.0)
    padded = np.concatenate([demand, np.zeros((4, inventory.PROTECTION))], axis=1)
    perfect = sum(padded[:, k:k + 20] for k in range(inventory.PROTECTION))  # exact demand over each protection period
    assert inventory.simulate(demand, perfect, sigma, 0.0, price)["fill_rate"] == pytest.approx(1.0)
    assert inventory.simulate(demand, np.zeros_like(demand), sigma, 0.0, price)["fill_rate"] == 0.0
    low = inventory.simulate(demand, perfect * 0.5, sigma, 0.0, price)
    high = inventory.simulate(demand, perfect * 0.5, sigma, 3.0, price)
    assert low["fill_rate"] < high["fill_rate"] and low["average_stock_value"] < high["average_stock_value"]


def test_stock_for_fill_rate_interpolates():
    points = [{"fill_rate": 0.8, "average_stock_value": 100.0}, {"fill_rate": 0.9, "average_stock_value": 200.0}]
    assert inventory.stock_for_fill_rate(points, 0.85) == pytest.approx(150.0)
    assert inventory.stock_for_fill_rate(points, 0.95) is None


def test_recommender_on_toy_baskets():
    baskets = pd.DataFrame(
        [(f"o{i}", "TEA") for i in range(6)] + [(f"o{i}", "CUP") for i in range(6)]
        + [(f"o{i}", "SOAP") for i in range(4, 8)] + [("o6", "TOWEL"), ("o7", "TOWEL"), ("o8", "TOWEL"), ("o8", "SOAP")],
        columns=["invoice", "stock_code"])
    items, similarity, together = recommender.fit(baskets, min_item_orders=1)
    assert similarity[items.get_loc("TEA"), items.get_loc("CUP")] == pytest.approx(1.0)
    assert together[items.get_loc("TEA"), items.get_loc("CUP")] == 6
    assert recommender.recommend(["TEA"], items, similarity, k=1)[0][0] == "CUP"
    assert recommender.recommend(["TOWEL"], items, similarity, k=1)[0][0] == "SOAP"
    assert "TEA" not in [code for code, _ in recommender.recommend(["TEA"], items, similarity, k=5)]
    assert recommender.recommend(["UNKNOWN"], items, similarity) == []


def test_recommender_beats_best_sellers(models):
    model, baseline = models["recommender"]["models"]
    assert model["hit_rate_at_10"] > 3 * baseline["hit_rate_at_10"]
    assert models["recommender"]["test_baskets"] > 5000


def test_customer_features_stop_at_the_cutoff(conn):
    cutoff = date(2011, 6, 1)
    features = customer_model.features_at(conn, cutoff)
    first_orders = conn.execute("SELECT customer_id, min(invoice_date) FROM fact_sales WHERE line_type = 'sale' "
                                "AND customer_id IS NOT NULL GROUP BY 1").df().set_index("customer_id").iloc[:, 0]
    assert (pd.to_datetime(first_orders.loc[features["customer_id"]]) < pd.Timestamp(cutoff)).all()
    assert features["recency_days"].min() >= 1 and (features["tenure_days"] >= features["recency_days"]).all()
    known = conn.execute("SELECT count(DISTINCT customer_id) FROM fact_sales WHERE line_type = 'sale' AND invoice_date >= ? "
                         "AND invoice_date < ? AND customer_id IN (SELECT customer_id FROM fact_sales WHERE line_type = 'sale' "
                         "AND invoice_date < ?)", [cutoff, date(2011, 8, 30), cutoff]).fetchone()[0]
    assert features["will_order"].sum() == known


def test_customer_model_beats_recency(models):
    summary = models["customer"]
    table = {m["model"]: m for m in summary["models"]}
    assert max(summary["train_cutoffs"]) < summary["test_cutoff"]
    assert table[summary["best_model"]]["roc_auc"] > table["Baseline: most recent buyers first"]["roc_auc"]
    assert [d["decile"] for d in summary["deciles"]] == list(range(1, 11))
    assert summary["deciles"][0]["actual"] > summary["deciles"][-1]["actual"]

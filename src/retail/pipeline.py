"""Run everything: warehouse -> analytics -> models -> tables for the API -> JSON for the dashboard.

    python -m retail.pipeline
"""
import json

import duckdb
import pandas as pd

from retail import analytics, customer_model, forecasting, inventory, recommender, warehouse
from retail.config import EXPORT, SOURCE_URL

FORECAST_PRODUCTS_SHOWN = 40
RECOMMENDATION_PRODUCTS_SHOWN = 150
NEIGHBOURS_SHOWN = 6


def write_model_tables(conn: duckdb.DuckDBPyConnection, scores: pd.DataFrame, forecasts: pd.DataFrame,
                       neighbours: pd.DataFrame) -> None:
    """Persist model outputs next to the marts, so the API serves them with plain SQL."""
    conn.register("scores_frame", scores)
    conn.register("forecasts_frame", forecasts)
    conn.register("neighbours_frame", neighbours)
    conn.execute("CREATE OR REPLACE TABLE ml_customer_score AS SELECT customer_id, repeat_probability FROM scores_frame")
    conn.execute("""
        CREATE OR REPLACE TABLE ml_forecast AS
        SELECT stock_code, CAST(week_start AS DATE) AS week_start, units AS actual_units,
               round(gbm, 1) AS forecast_units, round(average4, 1) AS baseline_units
        FROM forecasts_frame""")
    conn.execute("CREATE OR REPLACE TABLE ml_item_neighbour AS SELECT * FROM neighbours_frame")
    for name in ("scores_frame", "forecasts_frame", "neighbours_frame"):
        conn.unregister(name)


def forecast_examples(conn, forecasts: pd.DataFrame, panel: pd.DataFrame) -> list[dict]:
    top = conn.execute(f"""
        SELECT stock_code, description, revenue FROM mart_product
        WHERE stock_code IN (SELECT DISTINCT stock_code FROM ml_forecast)
        ORDER BY revenue DESC LIMIT {FORECAST_PRODUCTS_SHOWN}""").df()
    out = []
    for row in top.itertuples():
        history = panel[panel["stock_code"] == row.stock_code]
        test = forecasts[forecasts["stock_code"] == row.stock_code]
        actual = test["units"].to_numpy()
        out.append({
            "stock_code": row.stock_code, "description": row.description,
            "weeks": [str(w.date()) for w in history["week_start"]],
            "units": [int(u) for u in history["units"]],
            "test_from": str(test["week_start"].min().date()),
            "forecast": [round(float(v), 1) for v in test["gbm"]],
            "baseline": [round(float(v), 1) for v in test["average4"]],
            "wape": round(forecasting.wape(actual, test["gbm"].to_numpy()), 3),
            "baseline_wape": round(forecasting.wape(actual, test["average4"].to_numpy()), 3),
        })
    return out


def recommendation_examples(conn) -> list[dict]:
    frame = conn.execute(f"""
        WITH shown AS (
            SELECT stock_code, description, orders FROM mart_product
            WHERE stock_code IN (SELECT DISTINCT stock_code FROM ml_item_neighbour)
            ORDER BY orders DESC LIMIT {RECOMMENDATION_PRODUCTS_SHOWN}
        )
        SELECT s.stock_code, s.description, s.orders, n.rank, n.neighbour, p.description AS neighbour_description,
               n.similarity, n.orders_together
        FROM shown s
        JOIN ml_item_neighbour n USING (stock_code)
        JOIN dim_product p ON p.stock_code = n.neighbour
        WHERE n.rank <= {NEIGHBOURS_SHOWN}
        ORDER BY s.orders DESC, s.stock_code, n.rank""").df()
    out = []
    for (code, description, orders), group in frame.groupby(["stock_code", "description", "orders"], sort=False):
        out.append({
            "stock_code": code, "description": description, "orders": int(orders),
            "neighbours": [{"stock_code": g.neighbour, "description": g.neighbour_description,
                            "similarity": round(float(g.similarity), 3), "orders_together": int(g.orders_together)}
                           for g in group.itertuples()],
        })
    return out


def main() -> None:
    conn = warehouse.build()
    quality = warehouse.profile(conn)
    customer_summary, scores = customer_model.run(conn)
    forecast_summary, forecasts, panel = forecasting.run(conn)
    prices = conn.execute("SELECT stock_code, median_price FROM mart_product").df().set_index("stock_code")["median_price"]
    inventory_summary = inventory.run(forecasts, panel, prices)
    recommender_summary, neighbours = recommender.run(conn)
    write_model_tables(conn, scores, forecasts, neighbours)

    customers = analytics.customers(conn)
    customers["model"] = customer_summary
    forecast_summary["examples"] = forecast_examples(conn, forecasts, panel)
    total = panel.groupby("week_start")["units"].sum()
    forecast_summary["total_history"] = {"weeks": [str(w.date()) for w in total.index], "units": [int(u) for u in total]}
    recommender_summary["examples"] = recommendation_examples(conn)
    data = {
        "source": {
            "name": "Online Retail II", "publisher": "UCI Machine Learning Repository", "licence": "CC BY 4.0",
            "url": SOURCE_URL, "doi": "10.24432/C5CG6D", "currency": "GBP",
            "description": "Every transaction of a UK-based online gift-ware retailer, 1 December 2009 to 9 December 2011",
        },
        "quality": quality,
        "overview": analytics.overview(conn),
        "returns": analytics.returns(conn),
        "customers": customers,
        "forecast": forecast_summary,
        "inventory": inventory_summary,
        "recommender": recommender_summary,
    }
    conn.close()
    EXPORT.mkdir(parents=True, exist_ok=True)
    (EXPORT / "data.json").write_text(json.dumps(data, separators=(",", ":"), default=str, sort_keys=True))

    best = next(m for m in customer_summary["models"] if m["model"] == customer_summary["best_model"])
    print(json.dumps({
        "rows": quality["raw_rows"], "gross_sales": data["overview"]["totals"]["gross_sales"],
        "checks_passed": len(quality["checks"]),
        "repeat_purchase": {"model": customer_summary["best_model"], "roc_auc": best["roc_auc"],
                            "top_decile_rate": best["top_decile_rate"], "base_rate": customer_summary["test_repeat_rate"]},
        "forecast": {r["model"]: [r["wape"], r["wape_next"]] for r in forecast_summary["accuracy"]},
        "inventory": inventory_summary["comparison"],
        "recommender": recommender_summary["models"],
        "export_kb": round((EXPORT / "data.json").stat().st_size / 1024),
    }, indent=1))


if __name__ == "__main__":
    main()

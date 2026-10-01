"""Which customers will order again in the next 90 days?

Features describe a customer using only orders placed before a cutoff date; the label is whether they
order in the 90 days after it. Training cutoffs all end before the test cutoff, so the test is a
genuine look forward, not a random split of the same period.
"""
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from retail.config import SEED

HORIZON_DAYS = 90
TRAIN_CUTOFFS = 4  # 90, 180, 270 and 360 days before the test cutoff; the last one matches its season
FEATURES = [
    "recency_days", "tenure_days", "orders", "net_spend", "average_order_value", "distinct_products",
    "units", "return_line_share", "orders_last_90d", "spend_last_90d", "days_between_orders", "is_uk",
]
LABELS = {
    "recency_days": "Days since last order", "tenure_days": "Days since first order", "orders": "Orders so far",
    "net_spend": "Net spend", "average_order_value": "Average order value", "distinct_products": "Distinct products bought",
    "units": "Units bought", "return_line_share": "Share of lines returned", "orders_last_90d": "Orders in last 90 days",
    "spend_last_90d": "Spend in last 90 days", "days_between_orders": "Typical gap between orders", "is_uk": "UK customer",
}

FEATURE_SQL = """
WITH history AS (
    SELECT * FROM fact_sales WHERE customer_id IS NOT NULL AND invoice_date < $cutoff
),
per_customer AS (
    SELECT
        customer_id,
        date_diff('day', max(invoice_date) FILTER (WHERE line_type = 'sale'), $cutoff)   AS recency_days,
        date_diff('day', min(invoice_date) FILTER (WHERE line_type = 'sale'), $cutoff)   AS tenure_days,
        count(DISTINCT invoice) FILTER (WHERE line_type = 'sale')                         AS orders,
        -- Rounded to pence: parallel float sums differ in the last bits from run to run, and the
        -- model's split points would differ with them.
        round(sum(amount), 2)                                                             AS net_spend,
        count(DISTINCT stock_code) FILTER (WHERE line_type = 'sale')                      AS distinct_products,
        sum(quantity) FILTER (WHERE line_type = 'sale')                                   AS units,
        count(*) FILTER (WHERE line_type = 'return') / count(*)                           AS return_line_share,
        count(DISTINCT invoice) FILTER (
            WHERE line_type = 'sale' AND invoice_date >= $cutoff - INTERVAL 90 DAY)       AS orders_last_90d,
        round(coalesce(sum(amount) FILTER (
            WHERE line_type = 'sale' AND invoice_date >= $cutoff - INTERVAL 90 DAY), 0), 2) AS spend_last_90d,
        CAST(count(*) FILTER (WHERE country = 'United Kingdom') * 2 > count(*) AS INTEGER) AS is_uk
    FROM history
    GROUP BY customer_id
    HAVING count(*) FILTER (WHERE line_type = 'sale') > 0
),
future AS (
    SELECT DISTINCT customer_id
    FROM fact_sales
    WHERE line_type = 'sale' AND invoice_date >= $cutoff AND invoice_date < $cutoff + INTERVAL 90 DAY
)
SELECT
    p.*,
    round(p.net_spend / p.orders, 2)                                                     AS average_order_value,
    CASE WHEN p.orders > 1 THEN (p.tenure_days - p.recency_days) / (p.orders - 1)
         ELSE p.tenure_days END                                                          AS days_between_orders,
    CAST(f.customer_id IS NOT NULL AS INTEGER)                                           AS will_order
FROM per_customer p
LEFT JOIN future f USING (customer_id)
ORDER BY p.customer_id
"""


def features_at(conn: duckdb.DuckDBPyConnection, cutoff: date) -> pd.DataFrame:
    frame = conn.execute(FEATURE_SQL, {"cutoff": cutoff}).df()
    frame.insert(1, "cutoff", pd.Timestamp(cutoff))
    return frame


def _signed_log(x):
    return np.sign(x) * np.log1p(np.abs(x))


def make_models() -> dict:
    return {
        "Logistic regression": make_pipeline(
            FunctionTransformer(_signed_log), StandardScaler(), LogisticRegression(max_iter=1000)),
        "Gradient boosting": HistGradientBoostingClassifier(
            learning_rate=0.05, max_iter=200, max_leaf_nodes=15, min_samples_leaf=40, l2_regularization=1.0,
            random_state=SEED),
    }


def _metrics(y: np.ndarray, score: np.ndarray, probability: bool) -> dict:
    order = np.argsort(-score, kind="stable")
    top = order[: max(1, len(y) // 10)]
    out = {
        "roc_auc": round(float(roc_auc_score(y, score)), 6),
        "average_precision": round(float(average_precision_score(y, score)), 6),
        "top_decile_rate": round(float(y[top].mean()), 6),
        "top_decile_lift": round(float(y[top].mean() / y.mean()), 2),
    }
    if probability:
        out["brier"] = round(float(brier_score_loss(y, score)), 6)
    return out


def run(conn: duckdb.DuckDBPyConnection) -> tuple[dict, pd.DataFrame]:
    """Evaluate on the last 90 days, then refit on everything and score today's customers."""
    end = conn.execute("SELECT max(invoice_date) + 1 FROM fact_sales").fetchone()[0]
    test_cutoff = end - timedelta(days=HORIZON_DAYS)
    train_cutoffs = [test_cutoff - timedelta(days=HORIZON_DAYS * k) for k in range(1, TRAIN_CUTOFFS + 1)]
    train = pd.concat([features_at(conn, c) for c in train_cutoffs], ignore_index=True)
    test = features_at(conn, test_cutoff)
    X, y = train[FEATURES], train["will_order"].to_numpy()
    Xt, yt = test[FEATURES], test["will_order"].to_numpy()

    results = [
        {"model": "Baseline: most recent buyers first", **_metrics(yt, -Xt["recency_days"].to_numpy(float), False)},
        {"model": "Baseline: most frequent buyers first", **_metrics(yt, Xt["orders"].to_numpy(float), False)},
    ]
    fitted = {}
    for name, model in make_models().items():
        fitted[name] = model.fit(X, y)
        results.append({"model": name, **_metrics(yt, model.predict_proba(Xt)[:, 1], True)})
    best_name = max(fitted, key=lambda n: next(r["roc_auc"] for r in results if r["model"] == n))
    best = fitted[best_name]
    prob = best.predict_proba(Xt)[:, 1]

    deciles = pd.DataFrame({"p": prob, "y": yt})
    deciles["decile"] = pd.qcut(deciles["p"].rank(method="first", ascending=False), 10, labels=False) + 1
    by_decile = deciles.groupby("decile").agg(customers=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))

    importance = permutation_importance(best, Xt, yt, scoring="roc_auc", n_repeats=10, random_state=SEED)
    ranked = sorted(zip(FEATURES, importance.importances_mean, strict=True), key=lambda kv: -kv[1])

    # Deployment: every cutoff whose outcome is known becomes training data, then score customers as of today.
    everything = pd.concat([train, test], ignore_index=True)
    final = make_models()[best_name].fit(everything[FEATURES], everything["will_order"])
    today = features_at(conn, end)
    scores = today[["customer_id", "recency_days", "orders", "net_spend"]].copy()
    scores["repeat_probability"] = final.predict_proba(today[FEATURES])[:, 1].round(4)

    summary = {
        "horizon_days": HORIZON_DAYS,
        "test_cutoff": str(test_cutoff), "test_customers": int(len(test)), "test_repeat_rate": round(float(yt.mean()), 6),
        "train_cutoffs": [str(c) for c in sorted(train_cutoffs)], "train_rows": int(len(train)),
        "train_repeat_rate": round(float(y.mean()), 6),
        "models": results, "best_model": best_name,
        "deciles": [{"decile": int(d), "customers": int(r.customers), "predicted": round(float(r.predicted), 6),
                     "actual": round(float(r.actual), 6)} for d, r in by_decile.iterrows()],
        "importance": [{"feature": f, "label": LABELS[f], "auc_drop": round(float(v), 6)} for f, v in ranked[:8]],
        "scored_customers": int(len(scores)),
        "expected_repeat_customers": round(float(scores["repeat_probability"].sum())),
    }
    return summary, scores

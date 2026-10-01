""""Frequently bought together": item-to-item similarity from order baskets.

Similarity is the cosine of two products' basket vectors: orders containing both, over the geometric
mean of the orders containing each. It is evaluated on later orders than it was built from: one
product is hidden from each test basket and the recommender has to bring it back in its top k.
"""
import duckdb
import numpy as np
import pandas as pd
from scipy import sparse

from retail.config import SEED

TEST_FROM = "2011-09-01"
MIN_ITEM_ORDERS = 20
MAX_BASKET = 100       # larger orders are wholesale restocks: they link everything to everything
TOP_K = (5, 10)
NEIGHBOURS_STORED = 20


def baskets(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = conn.execute("""
        SELECT invoice, invoice_date, stock_code,
               count(*) OVER (PARTITION BY invoice) AS basket_size
        FROM mart_basket""").df()
    frame["invoice_date"] = pd.to_datetime(frame["invoice_date"])
    return frame[(frame["basket_size"] >= 2) & (frame["basket_size"] <= MAX_BASKET)]


def _matrix(frame: pd.DataFrame, items: pd.Index) -> sparse.csr_matrix:
    orders = frame["invoice"].astype("category")
    cols = items.get_indexer(frame["stock_code"])
    keep = cols >= 0
    return sparse.csr_matrix(
        (np.ones(keep.sum(), dtype=np.float32), (orders.cat.codes[keep], cols[keep])),
        shape=(orders.cat.categories.size, len(items)),
    )


def fit(frame: pd.DataFrame, min_item_orders: int = MIN_ITEM_ORDERS) -> tuple[pd.Index, np.ndarray, np.ndarray]:
    """Returns the item index, the cosine-similarity matrix and the co-purchase counts."""
    counts = frame.groupby("stock_code")["invoice"].nunique()
    items = pd.Index(sorted(counts[counts >= min_item_orders].index))
    x = _matrix(frame, items)
    together = np.asarray((x.T @ x).todense(), dtype=np.float32)
    alone = np.sqrt(np.diag(together))
    similarity = together / np.outer(alone, alone)
    np.fill_diagonal(similarity, 0.0)
    return items, similarity, together


def recommend(basket: list[str], items: pd.Index, similarity: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
    idx = items.get_indexer(basket)
    idx = idx[idx >= 0]
    if idx.size == 0:
        return []
    scores = similarity[idx].sum(axis=0)
    scores[idx] = -np.inf
    top = np.argsort(-scores)[:k]
    return [(items[i], float(scores[i])) for i in top if scores[i] > 0]


def evaluate(train: pd.DataFrame, test: pd.DataFrame, min_item_orders: int = MIN_ITEM_ORDERS) -> dict:
    items, similarity, together = fit(train, min_item_orders)
    popularity = np.diag(together).copy()
    x = _matrix(test, items).tolil()
    rng = np.random.default_rng(SEED)
    hidden, rows = [], []
    for row, cols in enumerate(x.rows):
        if len(cols) >= 2:
            hide = int(rng.choice(cols))
            x[row, hide] = 0
            hidden.append(hide)
            rows.append(row)
    x = x.tocsr()[rows]
    hidden = np.array(hidden)
    in_basket = x.toarray() > 0

    def hit_rates(scores: np.ndarray) -> dict:
        scores = np.where(in_basket, -np.inf, scores)
        rank = (scores > scores[np.arange(len(hidden)), hidden][:, None]).sum(axis=1)  # items ranked above the hidden one
        return {f"hit_rate_at_{k}": round(float((rank < k).mean()), 6) for k in TOP_K} | {
            "mean_reciprocal_rank": round(float((1.0 / (rank + 1)).mean()), 6)}

    return {
        "items": int(len(items)), "train_orders": int(train["invoice"].nunique()), "test_baskets": int(len(hidden)),
        "models": [
            {"model": "Item-to-item similarity", **hit_rates(x @ similarity)},
            {"model": "Baseline: best sellers", **hit_rates(np.tile(popularity, (len(hidden), 1)))},
        ],
    }


def run(conn: duckdb.DuckDBPyConnection, min_item_orders: int = MIN_ITEM_ORDERS) -> tuple[dict, pd.DataFrame]:
    frame = baskets(conn)
    cutoff = pd.Timestamp(TEST_FROM)
    summary = evaluate(frame[frame["invoice_date"] < cutoff], frame[frame["invoice_date"] >= cutoff], min_item_orders)
    summary["test_from"] = TEST_FROM
    summary["max_basket"] = MAX_BASKET
    model, baseline = summary["models"]
    summary["lift_at_10"] = round(model["hit_rate_at_10"] / baseline["hit_rate_at_10"], 2)

    # Production table: refit on every order, keep each product's strongest neighbours.
    items, similarity, together = fit(frame, min_item_orders)
    top = np.argsort(-similarity, axis=1)[:, :NEIGHBOURS_STORED]
    src = np.repeat(np.arange(len(items)), NEIGHBOURS_STORED)
    dst = top.ravel()
    neighbours = pd.DataFrame({
        "stock_code": items[src], "neighbour": items[dst],
        "rank": np.tile(np.arange(1, NEIGHBOURS_STORED + 1), len(items)),
        "similarity": similarity[src, dst].round(4), "orders_together": together[src, dst].astype(int),
    })
    return summary, neighbours[neighbours["similarity"] > 0].reset_index(drop=True)

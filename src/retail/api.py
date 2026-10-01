"""Read-only HTTP API over the warehouse and the model tables.

    uvicorn retail.api:app --reload        # after `python -m retail.pipeline`
"""
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from retail import assistant
from retail.config import WAREHOUSE


class BasketRequest(BaseModel):
    items: list[str] = Field(min_length=1, max_length=50, description="Stock codes already in the basket")
    k: int = Field(default=10, ge=1, le=50)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=300, description="A question about sales, products or customers")


def _rows(cursor: duckdb.DuckDBPyConnection) -> list[dict]:
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def create_app(source: Path | str | duckdb.DuckDBPyConnection = WAREHOUSE, llm=None) -> FastAPI:
    """`source` is the warehouse file, or an already open connection (used by the tests).

    `llm` answers POST /ask; by default it is the model named by LLM_MODEL on the server at LLM_BASE_URL.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(source, duckdb.DuckDBPyConnection):
            app.state.conn = source
            yield
            return
        if not Path(source).exists():
            raise RuntimeError(f"{source} not found: run `python -m retail.pipeline` first")
        app.state.conn = assistant.read_only_connection(source)
        yield
        app.state.conn.close()

    app = FastAPI(title="Retail Intelligence Platform", version="1.0.0", lifespan=lifespan)

    def db(request: Request) -> duckdb.DuckDBPyConnection:
        return request.app.state.conn.cursor()

    @app.get("/health")
    def health(request: Request) -> dict:
        lines, last = db(request).execute("SELECT count(*), max(invoice_date) FROM fact_sales").fetchone()
        return {"status": "ok", "fact_rows": lines, "last_date": str(last)}

    @app.get("/kpis")
    def kpis(request: Request) -> dict:
        return _rows(db(request).execute("""
            SELECT round(sum(revenue), 2) AS gross_sales, sum(orders) AS orders,
                   round(sum(returned_value), 2) AS returned_value,
                   round(sum(returned_value) / sum(revenue), 4) AS return_rate,
                   (SELECT count(*) FROM mart_customer_rfm) AS customers
            FROM mart_monthly"""))[0]

    @app.get("/products")
    def products(request: Request, search: str = Query(default="", max_length=60),
                 limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
        return _rows(db(request).execute("""
            SELECT stock_code, description, round(revenue, 2) AS revenue, orders
            FROM mart_product
            WHERE description ILIKE '%' || $search || '%' OR stock_code ILIKE $search || '%'
            ORDER BY revenue DESC LIMIT $limit""", {"search": search, "limit": limit}))

    @app.get("/products/{stock_code}")
    def product(request: Request, stock_code: str) -> dict:
        code = stock_code.upper()
        found = _rows(db(request).execute("""
            SELECT stock_code, description, round(revenue, 2) AS revenue, units_sold, orders,
                   round(return_lines / sale_lines, 4) AS return_line_rate, median_price
            FROM mart_product WHERE stock_code = $code""", {"code": code}))
        if not found:
            raise HTTPException(status_code=404, detail=f"unknown product {code}")
        return found[0] | {
            "bought_together": _rows(db(request).execute("""
                SELECT n.neighbour AS stock_code, p.description, n.similarity, n.orders_together
                FROM ml_item_neighbour n JOIN dim_product p ON p.stock_code = n.neighbour
                WHERE n.stock_code = $code ORDER BY n.rank LIMIT 5""", {"code": code})),
            "forecast": _rows(db(request).execute("""
                SELECT CAST(week_start AS VARCHAR) AS week_start, actual_units, forecast_units, baseline_units
                FROM ml_forecast WHERE stock_code = $code ORDER BY week_start DESC LIMIT 8""", {"code": code})),
        }

    @app.get("/customers/{customer_id}")
    def customer(request: Request, customer_id: int) -> dict:
        found = _rows(db(request).execute("""
            SELECT r.customer_id, r.segment, r.recency_days, r.frequency AS orders, round(r.monetary, 2) AS net_spend,
                   r.r, r.f, r.m, s.repeat_probability
            FROM mart_customer_rfm r LEFT JOIN ml_customer_score s USING (customer_id)
            WHERE r.customer_id = $id""", {"id": customer_id}))
        if not found:
            raise HTTPException(status_code=404, detail=f"unknown customer {customer_id}")
        return found[0]

    @app.post("/recommendations")
    def recommendations(request: Request, basket: BasketRequest) -> dict:
        items = sorted({code.upper() for code in basket.items})
        rows = _rows(db(request).execute("""
            SELECT n.neighbour AS stock_code, p.description, round(sum(n.similarity), 4) AS score
            FROM ml_item_neighbour n JOIN dim_product p ON p.stock_code = n.neighbour
            WHERE list_contains($items, n.stock_code) AND NOT list_contains($items, n.neighbour)
            GROUP BY n.neighbour, p.description
            ORDER BY score DESC, n.neighbour LIMIT $k""", {"items": items, "k": basket.k}))
        return {"basket": items, "recommendations": rows}

    @app.post("/ask")
    def ask(request: Request, body: AskRequest) -> dict:
        """Plain-English question -> one checked, read-only SQL query -> rows. The query is always returned."""
        try:
            answer = assistant.ask(body.question, llm or assistant.ChatModel(), db(request))
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"language model not reachable: {exc}") from exc
        if answer.declined:
            return {"question": body.question, "answered": False, "reason": "the question cannot be answered from the warehouse"}
        if answer.error:
            return {"question": body.question, "answered": False, "sql": answer.sql, "reason": answer.error}
        return {"question": body.question, "answered": True, "sql": answer.sql, "attempts": answer.attempts,
                "columns": answer.columns, "rows": [list(row) for row in answer.rows]}

    return app


app = create_app()

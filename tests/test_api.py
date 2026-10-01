import pytest
from fastapi.testclient import TestClient

from retail.api import create_app


@pytest.fixture(scope="module")
def client(conn, models):
    with TestClient(create_app(conn)) as test_client:
        yield test_client


def test_health_and_kpis(client):
    assert client.get("/health").json()["status"] == "ok"
    kpis = client.get("/kpis").json()
    assert kpis["orders"] == 39516 and 0.03 < kpis["return_rate"] < 0.04


def test_product_search_and_detail(client):
    found = client.get("/products", params={"search": "cakestand", "limit": 5}).json()
    assert found and all("CAKESTAND" in p["description"] for p in found) and len(found) <= 5
    detail = client.get("/products/22423").json()
    assert detail["description"] == "REGENCY CAKESTAND 3 TIER"
    assert len(detail["bought_together"]) == 5 and "22423" not in [n["stock_code"] for n in detail["bought_together"]]
    assert client.get("/products/85123a").json()["stock_code"] == "85123A"   # codes are case-insensitive
    assert client.get("/products/NOPE").status_code == 404
    assert client.get("/products", params={"limit": 0}).status_code == 422


def test_customer(client):
    body = client.get("/customers/12347").json()
    assert body["segment"] in {"Champions", "Loyal", "Promising", "New", "At risk", "Lost"}
    assert 0 <= body["repeat_probability"] <= 1
    assert client.get("/customers/1").status_code == 404


def test_recommendations(client):
    body = client.post("/recommendations", json={"items": ["22423", "22699"], "k": 5}).json()
    codes = [r["stock_code"] for r in body["recommendations"]]
    assert len(codes) == 5 and not {"22423", "22699"} & set(codes)
    scores = [r["score"] for r in body["recommendations"]]
    assert scores == sorted(scores, reverse=True)
    assert client.post("/recommendations", json={"items": []}).status_code == 422
    assert client.post("/recommendations", json={"items": ["UNKNOWN"]}).json()["recommendations"] == []


def test_ask_runs_a_checked_query_and_reports_refusals(conn, models):
    class Scripted:
        def __init__(self, *replies):
            self.replies = list(replies)

        def chat(self, messages):
            return self.replies.pop(0)

    def post(llm, question="How many customers are Champions?"):
        with TestClient(create_app(conn, llm=llm)) as c:
            return c.post("/ask", json={"question": question})

    ok = post(Scripted("```sql\nSELECT count(*) AS n FROM mart_customer_rfm WHERE segment = 'Champions'\n```")).json()
    assert ok["answered"] and ok["columns"] == ["n"] and ok["rows"][0][0] > 0 and "mart_customer_rfm" in ok["sql"]
    blocked = post(Scripted("```sql\nDROP TABLE fact_sales\n```", "```sql\nDELETE FROM fact_sales\n```")).json()
    assert not blocked["answered"] and "allowed" in blocked["reason"]
    assert conn.execute("SELECT count(*) FROM fact_sales").fetchone()[0] > 1_000_000
    assert not post(Scripted("CANNOT ANSWER"), "What is the weather?").json()["answered"]
    assert post(Scripted(""), "x").status_code == 422

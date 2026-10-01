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

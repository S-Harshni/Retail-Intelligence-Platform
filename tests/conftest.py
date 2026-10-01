import pytest

from retail import customer_model, forecasting, pipeline, recommender, warehouse


@pytest.fixture(scope="session")
def conn(tmp_path_factory):
    """The real warehouse, built once from the committed Parquet file."""
    path = tmp_path_factory.mktemp("warehouse") / "test.duckdb"
    connection = warehouse.build(path)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def models(conn):
    """Model outputs on a reduced setting (fewer products and test weeks) so the suite stays quick."""
    customer_summary, scores = customer_model.run(conn)
    forecast_summary, forecasts, panel = forecasting.run(conn, min_active_weeks=100, test_weeks=8)
    recommender_summary, neighbours = recommender.run(conn)
    pipeline.write_model_tables(conn, scores, forecasts, neighbours)
    return {"customer": customer_summary, "forecast": forecast_summary, "forecasts": forecasts, "panel": panel,
            "recommender": recommender_summary}

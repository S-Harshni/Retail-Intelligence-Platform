import datetime

import duckdb
import pytest

from retail import assistant
from retail.assistant_questions import MUST_DECLINE, QUESTIONS


class Scripted:
    """Stands in for the language model: returns the replies it was given, in order."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0)


@pytest.fixture(scope="module")
def db(conn, models, tmp_path_factory):
    """A read-only connection to a copy of the warehouse, the way the assistant opens it."""
    path = tmp_path_factory.mktemp("assistant") / "copy.duckdb"
    conn.execute(f"ATTACH '{path}' AS copy")
    conn.execute("COPY FROM DATABASE " + conn.execute("SELECT current_database()").fetchone()[0] + " TO copy")
    conn.execute("DETACH copy")
    connection = assistant.read_only_connection(path)
    yield connection
    connection.close()


@pytest.mark.parametrize("sql", [
    "DROP TABLE fact_sales",
    "DELETE FROM fact_sales WHERE line_type = 'return'",
    "UPDATE mart_product SET median_price = 1",
    "SELECT 1; DELETE FROM fact_sales",
    "WITH x AS (SELECT 1) INSERT INTO fact_sales SELECT * FROM x",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_parquet('s3://bucket/file')",
    "COPY fact_sales TO 'out.csv'",
    "ATTACH 'other.duckdb' AS other",
    "PRAGMA database_list",
    "SELECT * FROM stg_lines",                       # a real table, but not a published one
    "SELECT * FROM duckdb_tables()",
    "SELECT * FROM information_schema.tables",
    "SELECT getenv('HOME')",
    "CREATE TABLE t AS SELECT 1",
    "INSTALL httpfs",
])
def test_guard_rejects_anything_but_a_select_over_published_tables(sql):
    with pytest.raises(assistant.UnsafeQuery):
        assistant.guard(sql)


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM fact_sales",
    "select segment, count(*) from mart_customer_rfm group by segment",
    "WITH top AS (SELECT stock_code FROM mart_product ORDER BY revenue DESC LIMIT 5) SELECT * FROM top JOIN dim_product USING (stock_code)",
    "SELECT extract(year FROM invoice_date) AS y, sum(amount) FROM fact_sales GROUP BY 1",
    "SELECT 'drop table; delete' AS harmless_text FROM mart_monthly LIMIT 1",
    "SELECT replace(description, 'CAKE', 'cake') FROM dim_product -- update later",
])
def test_guard_accepts_ordinary_queries(sql):
    assert assistant.guard(sql)


def test_every_reference_query_passes_the_guard_and_returns_rows(db):
    assert len(QUESTIONS) == 70 and len(MUST_DECLINE) == 10
    assert len({q for _, q, _ in QUESTIONS}) == 70
    examples = {q for q, _ in assistant.EXAMPLES}
    assert not examples & {q for _, q, _ in QUESTIONS}          # the few-shot examples are not test questions
    for _, question, sql in QUESTIONS:
        _, rows = assistant.run_query(db, sql)
        assert rows, question
    for _, sql in assistant.EXAMPLES:
        assert assistant.run_query(db, sql)[1]


def test_the_connection_itself_cannot_write_or_read_files(db):
    with pytest.raises(duckdb.Error):
        db.execute("DELETE FROM fact_sales")
    with pytest.raises(duckdb.Error):
        db.execute("SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(duckdb.Error):
        db.execute("SET enable_external_access = true")


def test_results_are_capped(db):
    _, rows = assistant.run_query(db, "SELECT * FROM fact_sales")
    assert len(rows) == assistant.MAX_ROWS


def test_extract_sql_handles_code_blocks_plain_text_and_refusals():
    assert assistant.extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert assistant.extract_sql("Here you go:\n```\nSELECT 2\n```\nHope it helps") == "SELECT 2"
    assert assistant.extract_sql("SELECT 3") == "SELECT 3"
    assert assistant.extract_sql("CANNOT ANSWER") is None
    assert assistant.extract_sql("") is None


def test_same_result_ignores_order_names_extra_columns_and_rounding():
    expected = [("France", 10.004), ("Spain", 3.0)]
    assert assistant.same_result(expected, [("Spain", 3), ("France", 10.0)])
    assert assistant.same_result(expected, [(3.0, "x", "Spain"), (10.0, "y", "France")])       # extra column, other order
    assert not assistant.same_result(expected, [("France", 10.0)])                              # a row is missing
    assert not assistant.same_result(expected, [("France", 10.0), ("Spain", 4.0)])              # a value is wrong
    assert not assistant.same_result(expected, [("France",), ("Spain",)])                       # a column is missing
    assert assistant.same_result([(datetime.date(2011, 1, 1),)], [(datetime.datetime(2011, 1, 1),)])
    assert not assistant.same_result([(0.25,)], [(25.0,)])                                      # a share is not a percentage


def test_ask_runs_the_query_the_model_wrote(db):
    llm = Scripted("```sql\nSELECT count(*) AS n FROM mart_customer_rfm WHERE segment = 'Champions'\n```")
    answer = assistant.ask("How many Champions?", llm, db)
    assert answer.error is None and answer.columns == ["n"] and answer.rows[0][0] > 0 and answer.attempts == 1
    system = llm.seen[0][0]["content"]
    assert all(table in system for table in assistant.TABLES)


def test_ask_shows_the_error_to_the_model_and_takes_the_corrected_query(db):
    llm = Scripted("```sql\nSELECT avg(orders) FROM mart_customer_rfm\n```", "```sql\nSELECT avg(frequency) FROM mart_customer_rfm\n```")
    answer = assistant.ask("Average orders per customer?", llm, db)
    assert answer.error is None and answer.attempts == 2 and answer.rows[0][0] > 1
    assert "orders" in llm.seen[1][-1]["content"]              # the database error went back to the model


def test_ask_never_runs_an_unsafe_query(db):
    before = db.execute("SELECT count(*) FROM fact_sales").fetchone()[0]
    llm = Scripted("```sql\nDELETE FROM fact_sales\n```", "```sql\nDROP TABLE fact_sales\n```")
    answer = assistant.ask("Delete all sales", llm, db)
    assert answer.rows == [] and "allowed" in answer.error
    assert db.execute("SELECT count(*) FROM fact_sales").fetchone()[0] == before


def test_ask_reports_a_refusal(db):
    answer = assistant.ask("What is the weather?", Scripted("CANNOT ANSWER"), db)
    assert answer.declined and answer.sql is None and answer.rows == []


def test_vote_keeps_the_result_most_candidates_agree_on():
    def answer(rows, error=None, declined=False):
        return assistant.Answer("q", sql="SELECT 1", columns=["n"], rows=rows, error=error, declined=declined)

    first, second, third = answer([(1,)]), answer([(7,)]), answer([(7.0,)])
    assert assistant.vote([first, second, third]) is second                 # two agree on 7
    assert assistant.vote([first, second]) is first                         # no agreement: the preferred model
    assert assistant.vote([answer([], error="bad"), second]) is second      # failed queries do not vote
    failed = answer([], error="bad")
    assert assistant.vote([failed, answer([], declined=True)]) is failed

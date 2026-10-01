"""Ask the warehouse a question in plain English.

A language model writes one DuckDB query from the question and a description of the tables. The query
is checked before it runs (one read-only SELECT over the published tables), executed on a read-only
connection with a row limit, and, if it fails, the error is shown to the model once so it can fix it.

The model is any OpenAI-compatible chat API. By default that is a local Ollama server, so open-source
models run on the laptop with no key; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL to use another.
"""
import datetime
import decimal
import itertools
import math
import os
import re
from dataclasses import dataclass, field

import duckdb
import httpx

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5-coder:3b"
MAX_ROWS = 200

# The tables a question may touch, described the way an analyst would explain them to a new colleague.
SCHEMA = """\
fact_sales(invoice, invoice_date DATE, stock_code, customer_id, country, quantity, unit_price, amount, line_type, is_reversed)
  -- one row per product line of an invoice. line_type is 'sale' or 'return'.
  -- amount = quantity * unit_price in GBP; return lines have negative quantity and amount.
  -- customer_id is NULL for guest orders. An order is one distinct invoice that has sale lines.
dim_product(stock_code, description, first_sold DATE, last_sold DATE)
  -- one row per product; description is in upper case, e.g. 'REGENCY CAKESTAND 3 TIER'.
dim_customer(customer_id, country, first_order_date DATE, last_order_date DATE)
  -- one row per identified customer.
mart_monthly(month DATE, revenue, orders, customers, units, returned_value)
  -- one row per calendar month (month is its first day). revenue is gross sales; returned_value is positive.
mart_country(country, revenue, orders, customers, returned_value)
  -- one row per country, whole period.
mart_product(stock_code, description, revenue, units_sold, orders, units_returned, returned_value, median_price)
  -- one row per product, whole period.
mart_customer_rfm(customer_id, recency_days, frequency, monetary, r, f, m, segment)
  -- one row per customer. frequency = number of orders, monetary = net spend in GBP,
  -- recency_days = days since the last order. r, f, m are scores from 1 to 5.
  -- segment is one of 'Champions', 'Loyal', 'Promising', 'New', 'At risk', 'Lost'.
ml_customer_score(customer_id, repeat_probability)
  -- model output: probability that the customer orders again in the next 90 days.
ml_forecast(stock_code, week_start DATE, actual_units, forecast_units, baseline_units)
  -- model output: weekly demand forecast per product in the backtest weeks, next to what was sold.
"""
TABLES = frozenset(re.findall(r"^(\w+)\(", SCHEMA, flags=re.M))

RULES = """You translate questions about a retailer into one DuckDB SQL query.

Tables:
{schema}
Rules:
- Reply with a single SELECT statement in a ```sql block and nothing else.
- Use only the tables and columns listed. The data covers 2009-12-01 to 2011-12-09.
- Sales or revenue means sum(amount) over rows with line_type = 'sale', unless a mart already has the column.
- Prefer a mart table when it already holds the answer.
- A column exists only in the table it is listed under. When the columns you need are in two tables, JOIN them
  on customer_id or stock_code.
- Counting orders means count(DISTINCT invoice) over sale lines, never count(*).
- Copy codes, names and numbers from the question exactly.
- Return only the columns the question asks for.
- If the question cannot be answered from these tables, or asks to change data, reply with exactly: CANNOT ANSWER"""

# Worked examples for the few-shot prompt. None of them is in the evaluation set.
EXAMPLES = [
    ("How many products are there?", "SELECT count(*) AS products FROM dim_product"),
    ("Which 3 countries have the most customers?",
     "SELECT country, customers FROM mart_country ORDER BY customers DESC LIMIT 3"),
    ("What were total sales in France in 2011?",
     "SELECT sum(amount) AS sales FROM fact_sales\nWHERE line_type = 'sale' AND country = 'France' AND year(invoice_date) = 2011"),
    ("What is the average repeat probability of customers in each segment?",
     "SELECT r.segment, avg(s.repeat_probability) AS avg_probability\nFROM mart_customer_rfm r JOIN ml_customer_score s USING (customer_id)\nGROUP BY r.segment"),
    ("Which month had the highest revenue?", "SELECT month FROM mart_monthly ORDER BY revenue DESC LIMIT 1"),
    ("What share of products have never been returned?",
     "SELECT avg(CASE WHEN units_returned = 0 THEN 1.0 ELSE 0.0 END) AS share FROM mart_product"),
]

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|attach|detach|copy|export|import|install|load|pragma|"
    r"set|reset|call|grant|revoke|vacuum|checkpoint|execute|prepare|begin|commit|rollback|use|merge)\b", re.I)
EXTERNAL = re.compile(r"\b(read_\w+|\w+_scan|glob|getenv|system|shell|http\w*|current_setting|duckdb_\w+|pragma_\w+)\s*\(", re.I)
STRING = re.compile(r"'(?:[^']|'')*'")
RELATION = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", re.I)
CTE = re.compile(r"(?:\bwith\b|,)\s*([a-zA-Z_]\w*)\s+as\s*\(", re.I)


class UnsafeQuery(ValueError):
    """The query is not a single read-only SELECT over the published tables."""


def extract_sql(reply: str) -> str | None:
    """Pull the query out of a model reply; None when the model declined."""
    block = re.search(r"```(?:sql)?\s*(.*?)```", reply, flags=re.S | re.I)
    text = (block.group(1) if block else reply).strip().rstrip(";").strip()
    if not text or "CANNOT ANSWER" in reply.upper():
        return None
    return text


def guard(sql: str) -> str:
    """Raise UnsafeQuery unless `sql` is one SELECT that reads only the published tables."""
    bare = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
    code = STRING.sub("''", bare)                       # keywords inside string literals do not count
    if ";" in code:
        raise UnsafeQuery("only one statement is allowed")
    if not re.match(r"\s*(select|with)\b", code, flags=re.I):
        raise UnsafeQuery("only SELECT queries are allowed")
    if word := FORBIDDEN.search(code):
        raise UnsafeQuery(f"'{word.group(1).lower()}' is not allowed")
    if call := EXTERNAL.search(code):
        raise UnsafeQuery(f"function '{call.group(1).lower()}' is not allowed")
    code = re.sub(r"\b(extract|substring|trim)\s*\(([^()]*?)\bfrom\b", r"\1(\2,", code, flags=re.I)   # not a table reference
    local = {name.lower() for name in CTE.findall(code)}
    for name in RELATION.findall(code):
        if name.lower() not in TABLES and name.lower() not in local:
            raise UnsafeQuery(f"table '{name}' is not available")
    return bare.strip()


def read_only_connection(path) -> duckdb.DuckDBPyConnection:
    """A second line of defence: even a query that slipped past `guard` cannot write or reach outside the file."""
    conn = duckdb.connect(str(path), read_only=True)
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")
    return conn


def run_query(conn: duckdb.DuckDBPyConnection, sql: str, limit: int = MAX_ROWS) -> tuple[list[str], list[tuple]]:
    cursor = conn.cursor().execute(f"SELECT * FROM ({guard(sql)}) AS answer LIMIT {int(limit)}")
    return [c[0] for c in cursor.description], cursor.fetchall()


def build_messages(question: str, few_shot: bool = True) -> list[dict]:
    messages = [{"role": "system", "content": RULES.format(schema=SCHEMA)}]
    if few_shot:
        for q, sql in EXAMPLES:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": f"```sql\n{sql}\n```"}]
    return messages + [{"role": "user", "content": question}]


class ChatModel:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "local")
        self.client = httpx.Client(timeout=180)

    def chat(self, messages: list[dict]) -> str:
        response = self.client.post(
            f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": 0, "seed": 0, "max_tokens": 400})
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


@dataclass
class Answer:
    question: str
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: str | None = None          # why the last query failed, if it did
    declined: bool = False            # the model said the question cannot be answered
    attempts: int = 0                 # queries the model wrote


def ask(question: str, llm, conn: duckdb.DuckDBPyConnection, few_shot: bool = True, repairs: int = 1) -> Answer:
    """Question -> SQL -> guard -> execute; on failure the model sees the error and may try again."""
    messages = build_messages(question, few_shot)
    answer = Answer(question)
    for _ in range(repairs + 1):
        reply = llm.chat(messages)
        answer.sql = extract_sql(reply)
        if answer.sql is None:
            answer.declined, answer.error = True, None
            return answer
        answer.attempts += 1
        try:
            answer.columns, answer.rows = run_query(conn, answer.sql)
            answer.error = None
            return answer
        except (UnsafeQuery, duckdb.Error) as exc:
            answer.error = " ".join(str(exc).split())[:400]      # DuckDB names the columns it could have meant
            messages = messages + [{"role": "assistant", "content": reply}, {"role": "user", "content": (
                f"That query failed: {answer.error}\nCheck which table lists each column you used, join another table "
                "if you need one, and write a corrected query.")}]
    return answer


# ---- scoring ---------------------------------------------------------------------------------------------------------

def _value(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int | float | decimal.Decimal):
        return float(v)
    if isinstance(v, datetime.datetime):
        return str(v.date()) if v.time() == datetime.time(0) else str(v)
    return str(v)


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-4, abs_tol=0.006)     # a rounded answer is still the same answer
    return a == b


def _sort_key(row: tuple) -> tuple:
    return tuple((0, round(v, 1)) if isinstance(v, float) else (1, str(v)) for v in row)


def same_result(expected: list[tuple], got: list[tuple]) -> bool:
    """Execution match: some choice of the returned columns equals the expected table, row order ignored.

    Extra columns are tolerated (a model that also returns the product name is not wrong); missing or
    different values are not.
    """
    if len(expected) != len(got):
        return False
    if not expected:
        return True
    want = sorted((tuple(_value(v) for v in row) for row in expected), key=_sort_key)
    have = [tuple(_value(v) for v in row) for row in got]
    width, n = len(have[0]), len(want[0])
    if width < n or width > 8:
        return False
    for columns in itertools.permutations(range(width), n):
        picked = sorted((tuple(row[c] for c in columns) for row in have), key=_sort_key)
        if all(_same(a, b) for x, y in zip(want, picked, strict=True) for a, b in zip(x, y, strict=True)):
            return True
    return False


def vote(answers: list[Answer]) -> Answer:
    """Execution-guided voting: run every candidate query and keep the result most candidates agree on.

    Two models rarely make the same mistake, so agreement between their result tables is evidence of a right
    answer. Ties, and questions where nobody agrees, go to the first candidate (the preferred model).
    """
    usable = [a for a in answers if a.error is None and not a.declined]
    if not usable:
        return answers[0]
    groups: list[list[Answer]] = []
    for answer in usable:
        for group in groups:
            if same_result(group[0].rows, answer.rows) and same_result(answer.rows, group[0].rows):
                group.append(answer)
                break
        else:
            groups.append([answer])
    best = max(len(g) for g in groups)
    return next(g for g in groups if len(g) == best)[0]

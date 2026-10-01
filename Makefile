PY ?= .venv/bin/python
export PYTHONPATH := src

install:
	python3 -m venv .venv && $(PY) -m pip install -r requirements.txt

data:        ## rebuild data/online_retail_ii.parquet from the UCI archive (the file is already in the repo)
	$(PY) -m retail.ingest

pipeline:    ## warehouse -> analytics -> models -> docs/data/data.json
	$(PY) -m retail.pipeline

lakehouse:   ## PySpark + Delta Lake bronze/silver/gold tables in data/lakehouse (needs Java 17)
	$(PY) -m pip install -q -r requirements-lakehouse.txt && $(PY) -m retail.lakehouse

assistant:   ## measure the text-to-SQL assistant with local models (needs Ollama); writes docs/data/assistant.json
	$(PY) -m retail.assistant_eval

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

api:         ## http://127.0.0.1:8000/docs (run `make pipeline` first)
	$(PY) -m uvicorn retail.api:app --reload

dashboard:   ## http://127.0.0.1:8080
	$(PY) -m http.server 8080 -d docs

.PHONY: install data pipeline lakehouse assistant test lint api dashboard

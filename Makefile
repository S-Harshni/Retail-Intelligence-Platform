PY ?= .venv/bin/python
export PYTHONPATH := src

install:
	python3 -m venv .venv && $(PY) -m pip install -r requirements.txt

data:        ## rebuild data/online_retail_ii.parquet from the UCI archive (the file is already in the repo)
	$(PY) -m retail.ingest

pipeline:    ## warehouse -> analytics -> models -> docs/data/data.json
	$(PY) -m retail.pipeline

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

api:         ## http://127.0.0.1:8000/docs (run `make pipeline` first)
	$(PY) -m uvicorn retail.api:app --reload

dashboard:   ## http://127.0.0.1:8080
	$(PY) -m http.server 8080 -d docs

.PHONY: install data pipeline test lint api dashboard

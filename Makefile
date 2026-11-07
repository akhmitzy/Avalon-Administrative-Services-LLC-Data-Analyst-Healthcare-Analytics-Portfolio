PY ?= python

.PHONY: all data pipeline analysis test site clean

all: data pipeline analysis test site

data:
	$(PY) src/generate_data.py

pipeline:
	$(PY) src/pipeline/run_pipeline.py

analysis:
	cd src/analysis && $(PY) completion_factors.py && $(PY) program_impact.py && $(PY) forecast.py && $(PY) pricing_model.py
	$(PY) src/ai/verify_semantic_queries.py

test:
	$(PY) -m pytest -q

site:
	$(PY) src/export_site_data.py
	$(PY) site/build_site.py

clean:
	rm -rf warehouse outputs data/raw

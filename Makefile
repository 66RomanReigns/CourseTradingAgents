PYTHONPATH := src

.PHONY: sample test backtest experiment api docker-build

sample:
	python3 scripts/generate_sample_data.py

test: sample
	PYTHONPATH=$(PYTHONPATH) python3 -m unittest discover -s tests -v
	PYTHONPATH=$(PYTHONPATH) python3 -m compileall -q src

backtest: sample
	PYTHONPATH=$(PYTHONPATH) python3 -m tradinglab_agents.cli backtest \
		--csv data/sample/demo.csv \
		--news data/sample/demo_news.jsonl \
		--config config/default.yaml

experiment: sample
	PYTHONPATH=$(PYTHONPATH) python3 -m tradinglab_agents.cli experiment \
		--csv data/sample/demo.csv \
		--news data/sample/demo_news.jsonl \
		--config config/default.yaml

api:
	PYTHONPATH=$(PYTHONPATH) TRADINGLAB_ROOT=$$(pwd) uvicorn tradinglab_agents.api.app:app --host 0.0.0.0 --port 8000

docker-build:
	docker build -t tradinglab-agent:course .

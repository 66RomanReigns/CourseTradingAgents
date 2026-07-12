PYTHONPATH := src

.PHONY: doctor sample scenarios test backtest experiment benchmark runs api docker-build clean-artifacts

doctor:
	python3 scripts/doctor.py

sample:
	python3 scripts/generate_sample_data.py

scenarios:
	python3 scripts/generate_scenario_suite.py

test: sample scenarios
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

benchmark: scenarios
	PYTHONPATH=$(PYTHONPATH) python3 -m tradinglab_agents.cli benchmark \
		--scenario-dir data/scenarios \
		--config config/default.yaml

runs:
	PYTHONPATH=$(PYTHONPATH) python3 -m tradinglab_agents.cli runs

api:
	PYTHONPATH=$(PYTHONPATH) TRADINGLAB_ROOT=$$(pwd) uvicorn tradinglab_agents.api.app:app --host 0.0.0.0 --port 8000

docker-build:
	docker build -t tradinglab-agent:course .

clean-artifacts:
	find artifacts -mindepth 1 ! -name '.gitkeep' -delete

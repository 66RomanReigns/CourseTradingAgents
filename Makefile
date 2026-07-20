UV ?= uv
PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
PYTHONPATH := src

.PHONY: sync doctor secrets-configure secrets-token secrets-check api-budget workflow-plan workflow-dry-run workflow-offline sample multi-sample scenarios test lint check backtest multi-backtest experiment benchmark runs research paper-init paper-next paper-account paper-dashboard paper-orders paper-approve-all api docker-build clean-artifacts

sync:
	$(UV) venv --python 3.11 --clear .venv
	$(UV) pip sync --python $(PYTHON) requirements.lock

doctor:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/doctor.py

secrets-configure:
	./scripts/configure_api_keys.sh

secrets-token:
	$(PYTHON) scripts/ensure_api_token.py

secrets-check:
	./scripts/with_api_keys.sh

api-budget:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/api_budget_report.py

workflow-plan:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli workflow-plan \
		--config config/default.yaml

workflow-dry-run: multi-sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli workflow-run \
		--mode dry_run \
		--config config/default.yaml

workflow-offline: multi-sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli workflow-run \
		--mode offline \
		--config config/default.yaml

sample:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/generate_sample_data.py

multi-sample:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/generate_multi_asset_data.py

scenarios:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/generate_scenario_suite.py

test: sample multi-sample scenarios
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m compileall -q src tests

lint:
	$(RUFF) check src tests scripts

check: test lint

backtest: sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli backtest \
		--csv data/sample/demo.csv \
		--news data/sample/demo_news.jsonl \
		--config config/default.yaml

multi-backtest: multi-sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli multi-backtest \
		--data-dir data/multi_sample \
		--symbols SPY QQQ AAPL MSFT NVDA \
		--config config/default.yaml

experiment: sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli experiment \
		--csv data/sample/demo.csv \
		--news data/sample/demo_news.jsonl \
		--config config/default.yaml

benchmark: scenarios
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli benchmark \
		--scenario-dir data/scenarios \
		--config config/default.yaml

research: sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli research \
		--csv data/sample/demo.csv \
		--news data/sample/demo_news.jsonl \
		--config config/default.yaml

paper-init: multi-sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli paper-init \
		--account demo-paper \
		--name "TradeLab Demo Paper" \
		--symbols SPY QQQ AAPL MSFT NVDA \
		--config config/default.yaml

paper-next: multi-sample
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli paper-next \
		--account demo-paper \
		--config config/default.yaml

paper-account:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli paper-account \
		--account demo-paper \
		--config config/default.yaml

paper-dashboard:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli paper-dashboard \
		--account demo-paper \
		--config config/default.yaml

paper-orders:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli paper-orders \
		--account demo-paper \
		--config config/default.yaml

paper-approve-all:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli paper-approve-all \
		--account demo-paper \
		--reviewer local-user \
		--config config/default.yaml

runs:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tradinglab_agents.cli runs

api:
	./scripts/with_api_keys.sh env PYTHONPATH=$(PYTHONPATH) TRADINGLAB_ROOT=$$(pwd) \
		$(PYTHON) -m uvicorn tradinglab_agents.api.app:app \
		--host 127.0.0.1 --port 8000

docker-build:
	docker build -t tradinglab-agent:course .

clean-artifacts:
	find artifacts -mindepth 1 ! -name '.gitkeep' -delete

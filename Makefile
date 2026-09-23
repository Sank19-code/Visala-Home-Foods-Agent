.PHONY: setup seed run mcp demo eval test lint

setup:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]" && $(MAKE) seed

seed:
	python -m src.db.seed

run:            ## FastAPI app: webhooks, approval page, payment callback, order status
	uvicorn src.payments.webhooks:app --reload --port 8000

mcp:            ## commerce MCP server
	python -m src.commerce_mcp.server

demo:           ## one end-to-end order placed by the buyer agent (Claude) - run `make run` too
	python -m src.buyer_agent.cli "order 2 mango pickles and 1 podi under 600 rupees"

eval:
	python -m evals.run_evals

test:
	pytest -q

lint:
	ruff check src tests evals

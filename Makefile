.PHONY: install dev test lint run clean css

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements-dev.txt

test:
	PYTHONPATH=. .venv/bin/pytest

lint:
	.venv/bin/ruff check app/ tests/ run.py

css:
	/tmp/tailwindcss --input static/css/input.css --output static/css/app.css --minify

run:
	python run.py

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache *.db
	find . -type d -name __pycache__ -exec rm -rf {} +

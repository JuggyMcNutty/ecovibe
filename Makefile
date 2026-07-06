.PHONY: install dev test lint run clean

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements-dev.txt

test:
	pytest

lint:
	ruff check app/ tests/ run.py

run:
	python run.py

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache *.db
	find . -type d -name __pycache__ -exec rm -rf {} +

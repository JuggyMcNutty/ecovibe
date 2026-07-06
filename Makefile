.PHONY: install dev test lint run build clean

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

build:
	./build.sh python3.12

clean:
	rm -rf build dist venv_build __pycache__ .pytest_cache .ruff_cache *.db

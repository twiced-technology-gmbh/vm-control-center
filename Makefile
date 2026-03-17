.PHONY: install dev test clean

install:
	python3 -m venv venv
	. venv/bin/activate && \
	pip install --upgrade pip && \
	pip install -r requirements.txt

dev:
	. venv/bin/activate && \
	uvicorn tartvm.main:app --reload --host 127.0.0.1 --port 8000

test:
	. venv/bin/activate && \
	pytest -v

clean:
	rm -rf venv
	find . -type d -name "__pycache__" -exec rm -r {} +
	find . -type f -name "*.py[co]" -delete

.PHONY: install dev prod test clean

install:
	python3 -m venv venv
	. venv/bin/activate && \
	pip install --upgrade pip && \
	pip install -r requirements.txt

dev:
	. venv/bin/activate && \
	VMCC_DEBUG=true uvicorn tartvm.main:app --reload --host 127.0.0.1 --port 8000

prod:
	. venv/bin/activate && \
	uvicorn tartvm.main:app --host 0.0.0.0 --port 8200

test:
	. venv/bin/activate && \
	pytest -v

clean:
	rm -rf venv
	find . -type d -name "__pycache__" -exec rm -r {} +
	find . -type f -name "*.py[co]" -delete

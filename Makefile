VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
HOST ?= 0.0.0.0
PORT ?= 8096

.PHONY: run serve tidal-login install clean

## run: start the dev server (Flask, reloader on)
run: install
	$(PY) main.py

## serve: start under gunicorn (set HOST/PORT to override)
serve: install
	$(VENV)/bin/gunicorn -b $(HOST):$(PORT) server:app

## tidal-login: one-time interactive Tidal sign-in, writes tidal_creds.json
tidal-login: install
	$(PY) tidal_client.py

## install: create the venv and install requirements (runs once, then when requirements.txt changes)
install: $(VENV)/.stamp

$(VENV)/.stamp: requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install -r requirements.txt
	touch $@

## clean: remove the venv and Python caches
clean:
	rm -rf $(VENV) __pycache__ */__pycache__

include make/platform.mk

SHELL := /bin/sh

.PHONY: install-packages sanity-check test verify public-audit build ci

sanity-check:
	./tools/sanity-check

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(CURDIR)/src" python3 -m unittest discover -s tests

verify:
	./tools/verify

public-audit:
	./tools/public-audit

build:
	PYTHONDONTWRITEBYTECODE=1 pyproject-build --no-isolation

ci:
	$(MAKE) verify
	$(MAKE) public-audit
	$(MAKE) build
	./tools/acceptance-install

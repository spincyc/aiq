include make/platform.mk

SHELL := /bin/sh

.PHONY: install-packages sanity-check test verify public-audit release-check build ci

sanity-check:
	./tools/sanity-check

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(CURDIR)/src" python3 -m unittest discover -s tests

verify:
	./tools/verify

public-audit:
	./tools/public-audit

# Pass the tag being cut to compare it too: make release-check TAG=v0.3.0a1
release-check:
	./tools/release-check --tag "$(TAG)"

build:
	PYTHONDONTWRITEBYTECODE=1 pyproject-build --no-isolation

ci:
	$(MAKE) verify
	$(MAKE) public-audit
	$(MAKE) release-check
	$(MAKE) build
	./tools/acceptance-install

SHELL := /bin/sh

ARCH_PACKAGES := \
  git \
  gitleaks \
  make \
  python \
  python-build \
  python-pipx

.PHONY: install-packages sanity-check test verify public-audit build

install-packages:
	sudo pacman -S --needed -- $(ARCH_PACKAGES)

sanity-check:
	./tools/sanity-check

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(CURDIR)/src" python3 -m unittest discover -s tests

verify: sanity-check
	./tools/verify

public-audit:
	./tools/public-audit

build:
	PYTHONDONTWRITEBYTECODE=1 python3 -m build --no-isolation

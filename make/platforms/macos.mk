AIQ_PACKAGES := \
  git \
  gitleaks \
  pipx \
  python-build \
  python-setuptools \
  python@3.14

install-packages:
	brew install -- $(AIQ_PACKAGES)

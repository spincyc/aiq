AIQ_PACKAGES := \
  git \
  gitleaks \
  make \
  python \
  python-build \
  python-pipx \
  python-setuptools

install-packages:
	sudo pacman -S --needed -- $(AIQ_PACKAGES)

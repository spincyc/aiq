install-packages:
	@printf 'Unsupported platform: %s\n' '$(AIQ_UNSUPPORTED_PLATFORM)' >&2
	@printf 'Set AIQ_PLATFORM to a supported fragment in make/platforms/.\n' >&2
	@exit 2

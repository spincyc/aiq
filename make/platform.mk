AIQ_SYSTEM := $(shell uname -s)

ifeq ($(AIQ_SYSTEM),Darwin)
AIQ_DETECTED_PLATFORM := macos
else ifeq ($(AIQ_SYSTEM),Linux)
AIQ_LINUX_IDS := $(shell sed -n -e 's/^ID=//p' -e 's/^ID_LIKE=//p' /etc/os-release 2>/dev/null | tr -d '"')
ifneq ($(filter arch,$(AIQ_LINUX_IDS)),)
AIQ_DETECTED_PLATFORM := arch
else
AIQ_DETECTED_PLATFORM := $(or $(firstword $(AIQ_LINUX_IDS)),linux)
endif
else
AIQ_DETECTED_PLATFORM := unsupported
endif

AIQ_PLATFORM ?= $(AIQ_DETECTED_PLATFORM)

ifeq ($(AIQ_PLATFORM),arch)
include make/platforms/arch.mk
else ifeq ($(AIQ_PLATFORM),macos)
include make/platforms/macos.mk
else
AIQ_UNSUPPORTED_PLATFORM := $(AIQ_PLATFORM)
include make/platforms/unsupported.mk
endif

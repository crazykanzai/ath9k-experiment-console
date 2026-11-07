CC ?= gcc
CPPFLAGS += -D_GNU_SOURCE -Isrc
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic
LDFLAGS ?=

TARGET := ath9k-edca
SRC := \
	src/main.c \
	src/common.c \
	src/process.c \
	src/progress.c \
	src/prompt.c \
	src/hostapd_ctrl.c \
	src/dhcp_ctrl.c \
	src/wifi_state.c \
	src/wifi_nm.c \
	src/wifi_mode.c \
	src/driver_ctrl.c \
	src/edca_ctrl.c
OBJ := $(SRC:.c=.o)

KERNEL_RELEASE := $(shell uname -r)
MODULE_DIR ?= modules/$(KERNEL_RELEASE)
ATH9K_BUILD_DIR ?=
MAC80211_BUILD_DIR ?=

.PHONY: all clean stage-modules
all: $(TARGET)

$(TARGET): $(OBJ)
	$(CC) $(LDFLAGS) -o $@ $(OBJ)

stage-modules:
	@test -n "$(ATH9K_BUILD_DIR)" || { \
		echo "Usage: make stage-modules ATH9K_BUILD_DIR=/path/to/ath9k MAC80211_BUILD_DIR=/path/to/net/mac80211"; \
		exit 2; \
	}
	@test -n "$(MAC80211_BUILD_DIR)" || { \
		echo "04 HoL requires the matching mac80211.ko from the same patched tree"; \
		exit 2; \
	}
	mkdir -p "$(MODULE_DIR)"
	install -m 0644 "$(MAC80211_BUILD_DIR)/mac80211.ko" "$(MODULE_DIR)/"
	install -m 0644 "$(ATH9K_BUILD_DIR)/ath9k_hw.ko" "$(MODULE_DIR)/"
	install -m 0644 "$(ATH9K_BUILD_DIR)/ath9k_common.ko" "$(MODULE_DIR)/"
	install -m 0644 "$(ATH9K_BUILD_DIR)/ath9k.ko" "$(MODULE_DIR)/"
	@echo "Staged custom modules in $(MODULE_DIR)"

clean:
	rm -f $(OBJ) $(TARGET)

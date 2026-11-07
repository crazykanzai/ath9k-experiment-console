# Matching module bundles

No binary modules are committed. Build the matched set for the exact target kernel:
mac80211.ko, ath9k_hw.ko, ath9k_common.ko, ath9k.ko. Use the Makefile stage-modules
target with explicit source build paths. Validate hashes, srcversion and vermagic
before any unload/load. A retry-07 manifest is not a FIFO20 candidate manifest.

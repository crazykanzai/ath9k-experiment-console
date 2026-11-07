# Driver source, patches and licensing

Source baseline: Ubuntu linux-hwe-5.15 package 5.15.0-139.149~20.04.1,
runtime kernel 5.15.0-139-generic. Imported source is a subtree, not a full kernel.
Use an exact matching full source/configuration/Module.symvers environment to build.
Do not copy this subtree into an unrelated kernel and assume ABI compatibility.

Patch order is recorded in patches/series through Basic retry-07. HoL changes TX
metadata shared by mac80211 and ath9k: rebuild/load the four matched modules
mac80211, ath9k_hw, ath9k_common, ath9k together. Never mix patched ath9k with
stock mac80211. Preserve exact hashes, srcversion, vermagic, build provenance,
rollback materials and live ABI verification.

The FIFO20 runtime candidate used in the source lab is separate from the
persisted retry-07 bundle. This repository does not include a verified FIFO20
binary release. Reboot must not be assumed to retain a temporary module or CW.

RTS cross-series counts remain approximate/unavailable as documented; retry-08
and single-series experimental deployment scripts are not shipped as production.
Basic ABI2 terminal-failure denominator restrictions remain, not recalibrated here.

Imported source retains its copyright/SPDX identifiers and COPYING notice.
Application source license status is recorded in the root LICENSE.md.

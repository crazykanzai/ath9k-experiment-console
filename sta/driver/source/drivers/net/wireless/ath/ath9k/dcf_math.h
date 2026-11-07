/* SPDX-License-Identifier: ISC */
#ifndef ATH9K_DCF_MATH_H
#define ATH9K_DCF_MATH_H

#include <linux/types.h>

enum ath_dcf_capture_mode {
	ATH_DCF_CAPTURE_OFF,
	ATH_DCF_CAPTURE_DIAGNOSTIC,
	ATH_DCF_CAPTURE_MEASUREMENT,
};

#define ATH_DCF_CAPTURE_MODE_MASK 3U

/* Publish mode and epoch together; every control transition advances epoch. */
static inline u32 ath_dcf_capture_next(u32 state, u32 mode)
{
	return ((state + 4) & ~ATH_DCF_CAPTURE_MODE_MASK) | mode;
}

struct ath_dcf_attempt_delta {
	u32 exchange_attempts;
	u32 data_attempts;
	u32 rts_attempts;
	u32 rts_successes;
	u64 modeled_exchange_us;
};

static inline struct ath_dcf_attempt_delta
ath_dcf_attempt_delta(bool rts_cts, bool acked, u8 short_retries,
		      u8 long_retries, u32 success_us, u32 failure_us)
{
	struct ath_dcf_attempt_delta delta = {0};

	delta.data_attempts = (u32)long_retries + 1;
	delta.exchange_attempts = delta.data_attempts;
	if (rts_cts) {
		/* Preserve ABI 02's provisional terminal-stage mapping. Target
		 * chipset monitor validation is still required for retry failures.
		 */
		delta.exchange_attempts += short_retries;
		delta.rts_attempts = delta.exchange_attempts;
		delta.rts_successes = delta.data_attempts;
	}
	delta.modeled_exchange_us = (u64)delta.exchange_attempts * failure_us;
	if (acked)
		delta.modeled_exchange_us += success_us - failure_us;

	return delta;
}

/* Basic DATA retries can span several hardware series at the same bitrate.
 * ts_longretry belongs to the terminal series; earlier series used their
 * configured counts. Keep the provisional RTS mapping above unchanged.
 */
static inline struct ath_dcf_attempt_delta
ath_dcf_basic_attempt_delta(bool acked, u8 long_retries,
			    u32 prior_series_attempts,
			    u32 success_us, u32 failure_us)
{
	struct ath_dcf_attempt_delta delta;

	delta = ath_dcf_attempt_delta(false, acked, 0, long_retries,
				      success_us, failure_us);
	delta.data_attempts += prior_series_attempts;
	delta.exchange_attempts += prior_series_attempts;
	delta.modeled_exchange_us += (u64)prior_series_attempts * failure_us;
	return delta;
}

#endif /* ATH9K_DCF_MATH_H */

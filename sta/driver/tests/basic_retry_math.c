/* Compile against the real dcf_math.h with a userspace linux/types.h shim. */
#include <assert.h>
#include <stdio.h>
#include "dcf_math.h"

int main(void)
{
	unsigned int checks = 0;
	unsigned int prior, retries, short_retries, acked;
	struct ath_dcf_attempt_delta d;

	for (prior = 0; prior <= 93; prior++)
		for (retries = 0; retries <= 15; retries++)
			for (acked = 0; acked <= 1; acked++) {
				d = ath_dcf_basic_attempt_delta(acked, retries,
							 prior, 700, 450);
				assert(d.data_attempts == prior + retries + 1);
				assert(d.exchange_attempts == d.data_attempts);
				assert(!d.rts_attempts && !d.rts_successes);
				assert(d.modeled_exchange_us ==
				       (u64)(prior + retries + 1) * 450 + acked * 250);
				checks++;
			}
	/* Wide prior-series sum and multiplication must not truncate to u8/u32. */
	d = ath_dcf_basic_attempt_delta(true, 15, 300, 10000000, 9000000);
	assert(d.data_attempts == 316);
	assert(d.modeled_exchange_us == (u64)316 * 9000000 + 1000000);
	checks++;
	/* RTS/CTS remains the old, explicitly provisional mapping. */
	for (short_retries = 0; short_retries <= 15; short_retries++)
		for (retries = 0; retries <= 15; retries++)
			for (acked = 0; acked <= 1; acked++) {
				d = ath_dcf_attempt_delta(true, acked, short_retries,
							  retries, 700, 450);
				assert(d.data_attempts == retries + 1);
				assert(d.exchange_attempts == short_retries + retries + 1);
				assert(d.rts_attempts == d.exchange_attempts);
				assert(d.rts_successes == d.data_attempts);
				assert(d.modeled_exchange_us ==
				       (u64)(short_retries + retries + 1) * 450 + acked * 250);
				checks++;
			}
	printf("PASS: %u Basic/RTS retry-math cases\n", checks);
	return 0;
}

/*
 * Copyright (c) 2008-2011 Atheros Communications Inc.
 *
 * Permission to use, copy, modify, and/or distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#ifndef DEBUG_H
#define DEBUG_H

#include "hw.h"
#include "dfs_debug.h"
#include "dcf_math.h"

struct ath_txq;
struct ath_buf;
struct ath_node;
struct fft_sample_tlv;

#ifdef CONFIG_ATH9K_DEBUGFS
#define TX_STAT_INC(sc, q, c)	 do { (sc)->debug.stats.txstats[q].c++; } while (0)
#define RX_STAT_INC(sc, c)	 do { (sc)->debug.stats.rxstats.c++; } while (0)
#define RESET_STAT_INC(sc, type) do { (sc)->debug.stats.reset[type]++; } while (0)
#define ANT_STAT_INC(sc, i, c)	 do { (sc)->debug.stats.ant_stats[i].c++; } while (0)
#define ANT_LNA_INC(sc, i, c)	 do { (sc)->debug.stats.ant_stats[i].lna_recv_cnt[c]++; } while (0)
#else
#define TX_STAT_INC(sc, q, c)	 do { (void)(sc); } while (0)
#define RX_STAT_INC(sc, c)	 do { (void)(sc); } while (0)
#define RESET_STAT_INC(sc, type) do { (void)(sc); } while (0)
#define ANT_STAT_INC(sc, i, c)	 do { (void)(sc); } while (0)
#define ANT_LNA_INC(sc, i, c)	 do { (void)(sc); } while (0)
#endif

enum ath_reset_type {
	RESET_TYPE_BB_HANG,
	RESET_TYPE_BB_WATCHDOG,
	RESET_TYPE_FATAL_INT,
	RESET_TYPE_TX_ERROR,
	RESET_TYPE_TX_GTT,
	RESET_TYPE_TX_HANG,
	RESET_TYPE_PLL_HANG,
	RESET_TYPE_MAC_HANG,
	RESET_TYPE_BEACON_STUCK,
	RESET_TYPE_MCI,
	RESET_TYPE_CALIBRATION,
	RESET_TX_DMA_ERROR,
	RESET_RX_DMA_ERROR,
	__RESET_TYPE_MAX
};

#ifdef CONFIG_ATH9K_DEBUGFS

/**
 * struct ath_interrupt_stats - Contains statistics about interrupts
 * @total: Total no. of interrupts generated so far
 * @rxok: RX with no errors
 * @rxlp: RX with low priority RX
 * @rxhp: RX with high priority, uapsd only
 * @rxeol: RX with no more RXDESC available
 * @rxorn: RX FIFO overrun
 * @txok: TX completed at the requested rate
 * @txurn: TX FIFO underrun
 * @mib: MIB regs reaching its threshold
 * @rxphyerr: RX with phy errors
 * @rx_keycache_miss: RX with key cache misses
 * @swba: Software Beacon Alert
 * @bmiss: Beacon Miss
 * @bnr: Beacon Not Ready
 * @cst: Carrier Sense TImeout
 * @gtt: Global TX Timeout
 * @tim: RX beacon TIM occurrence
 * @cabend: RX End of CAB traffic
 * @dtimsync: DTIM sync lossage
 * @dtim: RX Beacon with DTIM
 * @bb_watchdog: Baseband watchdog
 * @tsfoor: TSF out of range, indicates that the corrected TSF received
 * from a beacon differs from the PCU's internal TSF by more than a
 * (programmable) threshold
 * @local_timeout: Internal bus timeout.
 * @mci: MCI interrupt, specific to MCI based BTCOEX chipsets
 * @gen_timer: Generic hardware timer interrupt
 */
struct ath_interrupt_stats {
	u32 total;
	u32 rxok;
	u32 rxlp;
	u32 rxhp;
	u32 rxeol;
	u32 rxorn;
	u32 txok;
	u32 txeol;
	u32 txurn;
	u32 mib;
	u32 rxphyerr;
	u32 rx_keycache_miss;
	u32 swba;
	u32 bmiss;
	u32 bnr;
	u32 cst;
	u32 gtt;
	u32 tim;
	u32 cabend;
	u32 dtimsync;
	u32 dtim;
	u32 bb_watchdog;
	u32 tsfoor;
	u32 mci;
	u32 gen_timer;

	/* Sync-cause stats */
	u32 sync_cause_all;
	u32 sync_rtc_irq;
	u32 sync_mac_irq;
	u32 eeprom_illegal_access;
	u32 apb_timeout;
	u32 pci_mode_conflict;
	u32 host1_fatal;
	u32 host1_perr;
	u32 trcv_fifo_perr;
	u32 radm_cpl_ep;
	u32 radm_cpl_dllp_abort;
	u32 radm_cpl_tlp_abort;
	u32 radm_cpl_ecrc_err;
	u32 radm_cpl_timeout;
	u32 local_timeout;
	u32 pm_access;
	u32 mac_awake;
	u32 mac_asleep;
	u32 mac_sleep_access;
};


/**
 * struct ath_tx_stats - Statistics about TX
 * @tx_pkts_all:  No. of total frames transmitted, including ones that
	may have had errors.
 * @tx_bytes_all:  No. of total bytes transmitted, including ones that
	may have had errors.
 * @queued: Total MPDUs (non-aggr) queued
 * @completed: Total MPDUs (non-aggr) completed
 * @a_aggr: Total no. of aggregates queued
 * @a_queued_hw: Total AMPDUs queued to hardware
 * @a_completed: Total AMPDUs completed
 * @a_retries: No. of AMPDUs retried (SW)
 * @a_xretries: No. of AMPDUs dropped due to xretries
 * @txerr_filtered: No. of frames with TXERR_FILT flag set.
 * @fifo_underrun: FIFO underrun occurrences
	Valid only for:
		- non-aggregate condition.
		- first packet of aggregate.
 * @xtxop: No. of frames filtered because of TXOP limit
 * @timer_exp: Transmit timer expiry
 * @desc_cfg_err: Descriptor configuration errors
 * @data_urn: TX data underrun errors
 * @delim_urn: TX delimiter underrun errors
 * @puttxbuf: Number of times hardware was given txbuf to write.
 * @txstart:  Number of times hardware was told to start tx.
 * @txprocdesc:  Number of times tx descriptor was processed
 * @txfailed:  Out-of-memory or other errors in xmit path.
 */
struct ath_tx_stats {
	u32 tx_pkts_all;
	u32 tx_bytes_all;
	u32 queued;
	u32 completed;
	u32 xretries;
	u32 a_aggr;
	u32 a_queued_hw;
	u32 a_completed;
	u32 a_retries;
	u32 a_xretries;
	u32 txerr_filtered;
	u32 fifo_underrun;
	u32 xtxop;
	u32 timer_exp;
	u32 desc_cfg_err;
	u32 data_underrun;
	u32 delim_underrun;
	u32 puttxbuf;
	u32 txstart;
	u32 txprocdesc;
	u32 txfailed;
};

/*
 * Various utility macros to print TX/Queue counters.
 */
#define PR_QNUM(_n) sc->tx.txq_map[_n]->axq_qnum
#define TXSTATS sc->debug.stats.txstats
#define PR(str, elem)							\
	do {								\
		seq_printf(file, "%s%13u%11u%10u%10u\n", str,		\
			   TXSTATS[PR_QNUM(IEEE80211_AC_BE)].elem,\
			   TXSTATS[PR_QNUM(IEEE80211_AC_BK)].elem,\
			   TXSTATS[PR_QNUM(IEEE80211_AC_VI)].elem,\
			   TXSTATS[PR_QNUM(IEEE80211_AC_VO)].elem); \
	} while(0)

struct ath_rx_rate_stats {
	struct {
		u32 ht20_cnt;
		u32 ht40_cnt;
		u32 sgi_cnt;
		u32 lgi_cnt;
	} ht_stats[24];

	struct {
		u32 ofdm_cnt;
	} ofdm_stats[8];

	struct {
		u32 cck_lp_cnt;
		u32 cck_sp_cnt;
	} cck_stats[4];
};

struct ath_airtime_stats {
	u32 rx_airtime;
	u32 tx_airtime;
};

#define ANT_MAIN 0
#define ANT_ALT  1

struct ath_antenna_stats {
	u32 recv_cnt;
	u32 rssi_avg;
	u32 lna_recv_cnt[4];
	u32 lna_attempt_cnt[4];
};

struct ath_stats {
	struct ath_interrupt_stats istats;
	struct ath_tx_stats txstats[ATH9K_NUM_TX_QUEUES];
	struct ath_rx_stats rxstats;
	struct ath_dfs_stats dfs_stats;
	struct ath_antenna_stats ant_stats[2];
	u32 reset[__RESET_TYPE_MAX];
};

/*
 * Last eligible, non-aggregated legacy data transmission for one AC.
 * All time values are integer microseconds.  The model values describe one
 * virtual-slot outcome; they are not host timestamp deltas.
 */
struct ath_dcf_timing_sample {
	bool valid;
	bool acked;
	bool fixed_rate;
	bool dynack;
	u8 qnum;
	u8 band;
	u8 protection;
	u8 channel_width;
	u8 active_series;
	u8 final_series;
	u8 configured_tries;
	u8 short_retries;
	u8 long_retries;
	u16 frame_len;
	int coverage_class;
	u16 data_rate_100kbps;
	u16 control_rate_100kbps;
	u16 sifs_us;
	u16 slot_us;
	u16 aifsn;
	u32 cwmin;
	u32 cwmax;
	u32 aifs_us;
	u32 descriptor_duration_us;
	u32 data_airtime_us;
	u32 ack_airtime_us;
	u32 rts_airtime_us;
	u32 cts_airtime_us;
	u32 success_time_us;
	u32 failure_time_us;
	u32 hw_send_timestamp;
	u64 hol_enqueue_ns;
	u64 hol_done_ns;
	u8 hol_tid;
	u16 hol_seq;
	u64 samples;
	u64 successes;
	u64 failures;
};

struct ath_dcf_timing_debug {
	/* Protects the per-AC last-sample snapshots. */
	spinlock_t lock;
	struct ath_dcf_timing_sample ac[IEEE80211_NUM_ACS];
};

#define ATH_DCF_MAX_PEERS 32

/*
 * Attempt counters are kept as integers in the driver.  User space derives
 * probabilities from the numerator/denominator pairs and can therefore pick
 * its own precision and confidence-interval method.
 */
struct ath_dcf_attempt_stats {
	u64 packets;
	u64 packet_successes;
	u64 packet_failures;
	u64 exchange_attempts;
	u64 exchange_successes;
	u64 rts_attempts;
	u64 rts_successes;
	u64 data_attempts;
	u64 data_successes;
	u64 short_retries;
	u64 long_retries;
	u64 modeled_exchange_us;
	u64 config_mismatches;
};

struct ath_dcf_measure_signature {
	bool valid;
	bool dynack;
	u8 band;
	u8 protection;
	u8 channel_width;
	u16 frame_len;
	int coverage_class;
	u16 data_rate_100kbps;
	u16 control_rate_100kbps;
	u16 sifs_us;
	u16 slot_us;
	u16 aifsn;
	u32 cwmin;
	u32 cwmax;
};

/*
 * Saturated HOL service-delay samples for one peer and access category.
 *
 * AR_SendTimestamp marks the final on-air transmission of a completed
 * descriptor.  With a fixed measurement signature, the constant tail from
 * that timestamp to the end of the successful virtual slot cancels between
 * consecutive successful departures.  A sample is accepted only when the
 * previous success left another descriptor in the hardware queue and no
 * terminal drop or excluded/config-mismatched frame broke the interval.
 */
struct ath_dcf_delay_stats {
	bool anchor_valid;
	u32 anchor_tstamp;
	u64 samples;
	u64 sum_us;
	u64 sum_sq_us2;
	u32 min_us;
	u32 max_us;
	u64 queue_empty_breaks;
	u64 terminal_drop_breaks;
	u64 config_breaks;
	u64 excluded_breaks;
	u64 invalid_deltas;
	u64 overflow_samples;
};

/* Queue-head service time, measured on the host monotonic clock. For a
 * single FIFO TID, a packet becomes HOL at max(its mac80211 TXQ arrival,
 * the preceding packet's final TX status). TX-status callback latency is
 * included and must be checked against the TSF departure estimator.
 */
struct ath_dcf_hol_stats {
	bool anchor_valid;
	u8 last_tid;
	u16 last_seq;
	u64 last_enqueue_ns;
	u64 last_done_ns;
	u64 samples;
	u64 sum_us;
	u64 sum_sq_us2;
	u32 min_us;
	u32 max_us;
	u64 anchor_skips;
	u64 missing_enqueue;
	u64 order_breaks;
	u64 terminal_drop_breaks;
	u64 config_breaks;
	u64 excluded_breaks;
	u64 invalid_deltas;
	u64 overflow_samples;
};

struct ath_dcf_peer_stats {
	bool valid;
	u8 addr[ETH_ALEN];
	struct ath_dcf_attempt_stats ac[IEEE80211_NUM_ACS];
	struct ath_dcf_delay_stats delay[IEEE80211_NUM_ACS];
	struct ath_dcf_hol_stats hol[IEEE80211_NUM_ACS];
};

struct ath_dcf_measurement {
	/* Process access uses spin_lock_bh(), TX tasklet access spin_lock().
	 * Lock order: measurement.lock -> dcf_timing.lock. No hard-IRQ users.
	 */
	spinlock_t lock;
	u32 capture_state;
	u16 peer_count;
	struct list_head peers;
	bool running;
	u32 generation;
	u64 start_ns;
	u64 stop_ns;
	u64 excluded_samples;
	u64 config_mismatches;
	u64 peer_overflow;
	struct ath_dcf_measure_signature signature[IEEE80211_NUM_ACS];
	struct ath_dcf_attempt_stats ac[IEEE80211_NUM_ACS];
};

struct ath9k_debug {
	struct dentry *debugfs_phy;
	u32 regidx;
	/* Experiment-only override: 1 disables protection for legacy QoS data. */
	u32 basic_access_override;
	struct ath_stats stats;
	struct ath_dcf_timing_debug dcf_timing;
	struct ath_dcf_measurement dcf_measurement;
};

int ath9k_init_debug(struct ath_hw *ah);
void ath9k_deinit_debug(struct ath_softc *sc);

void ath_debug_stat_interrupt(struct ath_softc *sc, enum ath9k_int status);
void ath_debug_stat_tx(struct ath_softc *sc, struct ath_buf *bf,
		       struct ath_tx_status *ts, struct ath_txq *txq,
		       unsigned int flags);
void ath_debug_dcf_timing_tx(struct ath_softc *sc, struct ath_buf *bf,
			     const struct ath_tx_status *ts,
			     const struct ath_txq *txq,
			     struct ath_node *an);
u64 ath_debug_dcf_hol_enqueue_time(struct ath_softc *sc, struct sk_buff *skb);
void ath_debug_dcf_hol_exclude_tx(struct ath_softc *sc,
				  const struct ath_txq *txq,
				  struct ath_node *an);
void ath_debug_dcf_peer_add(struct ath_softc *sc, struct ath_node *an);
void ath_debug_dcf_peer_remove(struct ath_softc *sc, struct ath_node *an);
void ath_debug_stat_rx(struct ath_softc *sc, struct ath_rx_status *rs);
int ath9k_get_et_sset_count(struct ieee80211_hw *hw,
			    struct ieee80211_vif *vif, int sset);
void ath9k_get_et_stats(struct ieee80211_hw *hw,
			struct ieee80211_vif *vif,
			struct ethtool_stats *stats, u64 *data);
void ath9k_get_et_strings(struct ieee80211_hw *hw,
			  struct ieee80211_vif *vif,
			  u32 sset, u8 *data);
void ath9k_sta_add_debugfs(struct ieee80211_hw *hw,
			   struct ieee80211_vif *vif,
			   struct ieee80211_sta *sta,
			   struct dentry *dir);
void ath9k_debug_stat_ant(struct ath_softc *sc,
			  struct ath_hw_antcomb_conf *div_ant_conf,
			  int main_rssi_avg, int alt_rssi_avg);
void ath9k_debug_sync_cause(struct ath_softc *sc, u32 sync_cause);

#else

static inline int ath9k_init_debug(struct ath_hw *ah)
{
	return 0;
}

static inline void ath9k_deinit_debug(struct ath_softc *sc)
{
}

static inline void ath_debug_stat_interrupt(struct ath_softc *sc,
					    enum ath9k_int status)
{
}
static inline void ath_debug_stat_tx(struct ath_softc *sc,
				     struct ath_buf *bf,
				     struct ath_tx_status *ts,
				     struct ath_txq *txq,
				     unsigned int flags)
{
}
static inline void ath_debug_dcf_timing_tx(struct ath_softc *sc,
					    struct ath_buf *bf,
					    const struct ath_tx_status *ts,
					    const struct ath_txq *txq,
					    struct ath_node *an)
{
}
static inline u64 ath_debug_dcf_hol_enqueue_time(struct ath_softc *sc,
						   struct sk_buff *skb)
{
	return 0;
}
static inline void ath_debug_dcf_hol_exclude_tx(struct ath_softc *sc,
						 const struct ath_txq *txq,
						 struct ath_node *an)
{
}
static inline void ath_debug_dcf_peer_add(struct ath_softc *sc,
					  struct ath_node *an)
{
}
static inline void ath_debug_dcf_peer_remove(struct ath_softc *sc,
					     struct ath_node *an)
{
}
static inline void ath_debug_stat_rx(struct ath_softc *sc,
				     struct ath_rx_status *rs)
{
}
static inline void ath9k_debug_stat_ant(struct ath_softc *sc,
					struct ath_hw_antcomb_conf *div_ant_conf,
					int main_rssi_avg, int alt_rssi_avg)
{

}

static inline void
ath9k_debug_sync_cause(struct ath_softc *sc, u32 sync_cause)
{
}

#endif /* CONFIG_ATH9K_DEBUGFS */

#ifdef CONFIG_ATH9K_STATION_STATISTICS
void ath_debug_rate_stats(struct ath_softc *sc,
			  struct ath_rx_status *rs,
			  struct sk_buff *skb);
#else
static inline void ath_debug_rate_stats(struct ath_softc *sc,
					struct ath_rx_status *rs,
					struct sk_buff *skb)
{
}
#endif /* CONFIG_ATH9K_STATION_STATISTICS */

#endif /* DEBUG_H */

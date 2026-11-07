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

#include <linux/slab.h>
#include <linux/vmalloc.h>
#include <linux/export.h>
#include <linux/ktime.h>
#include <linux/overflow.h>
#include <linux/string.h>
#include <asm/unaligned.h>

#include "ath9k.h"

#define REG_WRITE_D(_ah, _reg, _val) \
	ath9k_hw_common(_ah)->ops->write((_ah), (_val), (_reg))
#define REG_READ_D(_ah, _reg) \
	ath9k_hw_common(_ah)->ops->read((_ah), (_reg))

void ath9k_debug_sync_cause(struct ath_softc *sc, u32 sync_cause)
{
	if (sync_cause)
		sc->debug.stats.istats.sync_cause_all++;
	if (sync_cause & AR_INTR_SYNC_RTC_IRQ)
		sc->debug.stats.istats.sync_rtc_irq++;
	if (sync_cause & AR_INTR_SYNC_MAC_IRQ)
		sc->debug.stats.istats.sync_mac_irq++;
	if (sync_cause & AR_INTR_SYNC_EEPROM_ILLEGAL_ACCESS)
		sc->debug.stats.istats.eeprom_illegal_access++;
	if (sync_cause & AR_INTR_SYNC_APB_TIMEOUT)
		sc->debug.stats.istats.apb_timeout++;
	if (sync_cause & AR_INTR_SYNC_PCI_MODE_CONFLICT)
		sc->debug.stats.istats.pci_mode_conflict++;
	if (sync_cause & AR_INTR_SYNC_HOST1_FATAL)
		sc->debug.stats.istats.host1_fatal++;
	if (sync_cause & AR_INTR_SYNC_HOST1_PERR)
		sc->debug.stats.istats.host1_perr++;
	if (sync_cause & AR_INTR_SYNC_TRCV_FIFO_PERR)
		sc->debug.stats.istats.trcv_fifo_perr++;
	if (sync_cause & AR_INTR_SYNC_RADM_CPL_EP)
		sc->debug.stats.istats.radm_cpl_ep++;
	if (sync_cause & AR_INTR_SYNC_RADM_CPL_DLLP_ABORT)
		sc->debug.stats.istats.radm_cpl_dllp_abort++;
	if (sync_cause & AR_INTR_SYNC_RADM_CPL_TLP_ABORT)
		sc->debug.stats.istats.radm_cpl_tlp_abort++;
	if (sync_cause & AR_INTR_SYNC_RADM_CPL_ECRC_ERR)
		sc->debug.stats.istats.radm_cpl_ecrc_err++;
	if (sync_cause & AR_INTR_SYNC_RADM_CPL_TIMEOUT)
		sc->debug.stats.istats.radm_cpl_timeout++;
	if (sync_cause & AR_INTR_SYNC_LOCAL_TIMEOUT)
		sc->debug.stats.istats.local_timeout++;
	if (sync_cause & AR_INTR_SYNC_PM_ACCESS)
		sc->debug.stats.istats.pm_access++;
	if (sync_cause & AR_INTR_SYNC_MAC_AWAKE)
		sc->debug.stats.istats.mac_awake++;
	if (sync_cause & AR_INTR_SYNC_MAC_ASLEEP)
		sc->debug.stats.istats.mac_asleep++;
	if (sync_cause & AR_INTR_SYNC_MAC_SLEEP_ACCESS)
		sc->debug.stats.istats.mac_sleep_access++;
}

static ssize_t ath9k_debugfs_read_buf(struct file *file, char __user *user_buf,
				      size_t count, loff_t *ppos)
{
	u8 *buf = file->private_data;
	return simple_read_from_buffer(user_buf, count, ppos, buf, strlen(buf));
}

static int ath9k_debugfs_release_buf(struct inode *inode, struct file *file)
{
	vfree(file->private_data);
	return 0;
}

#ifdef CONFIG_ATH_DEBUG

static ssize_t read_file_debug(struct file *file, char __user *user_buf,
			     size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	char buf[32];
	unsigned int len;

	len = sprintf(buf, "0x%08x\n", common->debug_mask);
	return simple_read_from_buffer(user_buf, count, ppos, buf, len);
}

static ssize_t write_file_debug(struct file *file, const char __user *user_buf,
			     size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	unsigned long mask;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &mask))
		return -EINVAL;

	common->debug_mask = mask;
	return count;
}

static const struct file_operations fops_debug = {
	.read = read_file_debug,
	.write = write_file_debug,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

#endif

#define DMA_BUF_LEN 1024


static ssize_t read_file_ani(struct file *file, char __user *user_buf,
			     size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	struct ath_hw *ah = sc->sc_ah;
	unsigned int len = 0;
	const unsigned int size = 1024;
	ssize_t retval = 0;
	char *buf;
	int i;
	struct {
		const char *name;
		unsigned int val;
	} ani_info[] = {
		{ "ANI RESET", ah->stats.ast_ani_reset },
		{ "OFDM LEVEL", ah->ani.ofdmNoiseImmunityLevel },
		{ "CCK LEVEL", ah->ani.cckNoiseImmunityLevel },
		{ "SPUR UP", ah->stats.ast_ani_spurup },
		{ "SPUR DOWN", ah->stats.ast_ani_spurdown },
		{ "OFDM WS-DET ON", ah->stats.ast_ani_ofdmon },
		{ "OFDM WS-DET OFF", ah->stats.ast_ani_ofdmoff },
		{ "MRC-CCK ON", ah->stats.ast_ani_ccklow },
		{ "MRC-CCK OFF", ah->stats.ast_ani_cckhigh },
		{ "FIR-STEP UP", ah->stats.ast_ani_stepup },
		{ "FIR-STEP DOWN", ah->stats.ast_ani_stepdown },
		{ "INV LISTENTIME", ah->stats.ast_ani_lneg_or_lzero },
		{ "OFDM ERRORS", ah->stats.ast_ani_ofdmerrs },
		{ "CCK ERRORS", ah->stats.ast_ani_cckerrs },
	};

	buf = kzalloc(size, GFP_KERNEL);
	if (buf == NULL)
		return -ENOMEM;

	len += scnprintf(buf + len, size - len, "%15s: %s\n", "ANI",
			 common->disable_ani ? "DISABLED" : "ENABLED");

	if (common->disable_ani)
		goto exit;

	for (i = 0; i < ARRAY_SIZE(ani_info); i++)
		len += scnprintf(buf + len, size - len, "%15s: %u\n",
				 ani_info[i].name, ani_info[i].val);

exit:
	if (len > size)
		len = size;

	retval = simple_read_from_buffer(user_buf, count, ppos, buf, len);
	kfree(buf);

	return retval;
}

static ssize_t write_file_ani(struct file *file,
			      const char __user *user_buf,
			      size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	unsigned long ani;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &ani))
		return -EINVAL;

	if (ani > 1)
		return -EINVAL;

	common->disable_ani = !ani;

	if (common->disable_ani) {
		clear_bit(ATH_OP_ANI_RUN, &common->op_flags);
		ath_stop_ani(sc);
	} else {
		ath_check_ani(sc);
	}

	return count;
}

static const struct file_operations fops_ani = {
	.read = read_file_ani,
	.write = write_file_ani,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

#ifdef CONFIG_ATH9K_BTCOEX_SUPPORT

static ssize_t read_file_bt_ant_diversity(struct file *file,
					  char __user *user_buf,
					  size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	char buf[32];
	unsigned int len;

	len = sprintf(buf, "%d\n", common->bt_ant_diversity);
	return simple_read_from_buffer(user_buf, count, ppos, buf, len);
}

static ssize_t write_file_bt_ant_diversity(struct file *file,
					   const char __user *user_buf,
					   size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	struct ath9k_hw_capabilities *pCap = &sc->sc_ah->caps;
	unsigned long bt_ant_diversity;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	if (!(pCap->hw_caps & ATH9K_HW_CAP_BT_ANT_DIV))
		goto exit;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &bt_ant_diversity))
		return -EINVAL;

	common->bt_ant_diversity = !!bt_ant_diversity;
	ath9k_ps_wakeup(sc);
	ath9k_hw_set_bt_ant_diversity(sc->sc_ah, common->bt_ant_diversity);
	ath_dbg(common, CONFIG, "Enable WLAN/BT RX Antenna diversity: %d\n",
		common->bt_ant_diversity);
	ath9k_ps_restore(sc);
exit:
	return count;
}

static const struct file_operations fops_bt_ant_diversity = {
	.read = read_file_bt_ant_diversity,
	.write = write_file_bt_ant_diversity,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

#endif

void ath9k_debug_stat_ant(struct ath_softc *sc,
			  struct ath_hw_antcomb_conf *div_ant_conf,
			  int main_rssi_avg, int alt_rssi_avg)
{
	struct ath_antenna_stats *as_main = &sc->debug.stats.ant_stats[ANT_MAIN];
	struct ath_antenna_stats *as_alt = &sc->debug.stats.ant_stats[ANT_ALT];

	as_main->lna_attempt_cnt[div_ant_conf->main_lna_conf]++;
	as_alt->lna_attempt_cnt[div_ant_conf->alt_lna_conf]++;

	as_main->rssi_avg = main_rssi_avg;
	as_alt->rssi_avg = alt_rssi_avg;
}

static ssize_t read_file_antenna_diversity(struct file *file,
					   char __user *user_buf,
					   size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	struct ath9k_hw_capabilities *pCap = &ah->caps;
	struct ath_antenna_stats *as_main = &sc->debug.stats.ant_stats[ANT_MAIN];
	struct ath_antenna_stats *as_alt = &sc->debug.stats.ant_stats[ANT_ALT];
	struct ath_hw_antcomb_conf div_ant_conf;
	unsigned int len = 0;
	const unsigned int size = 1024;
	ssize_t retval = 0;
	char *buf;
	static const char *lna_conf_str[4] = {
		"LNA1_MINUS_LNA2", "LNA2", "LNA1", "LNA1_PLUS_LNA2"
	};

	buf = kzalloc(size, GFP_KERNEL);
	if (buf == NULL)
		return -ENOMEM;

	if (!(pCap->hw_caps & ATH9K_HW_CAP_ANT_DIV_COMB)) {
		len += scnprintf(buf + len, size - len, "%s\n",
				 "Antenna Diversity Combining is disabled");
		goto exit;
	}

	ath9k_ps_wakeup(sc);
	ath9k_hw_antdiv_comb_conf_get(ah, &div_ant_conf);
	len += scnprintf(buf + len, size - len, "Current MAIN config : %s\n",
			 lna_conf_str[div_ant_conf.main_lna_conf]);
	len += scnprintf(buf + len, size - len, "Current ALT config  : %s\n",
			 lna_conf_str[div_ant_conf.alt_lna_conf]);
	len += scnprintf(buf + len, size - len, "Average MAIN RSSI   : %d\n",
			 as_main->rssi_avg);
	len += scnprintf(buf + len, size - len, "Average ALT RSSI    : %d\n\n",
			 as_alt->rssi_avg);
	ath9k_ps_restore(sc);

	len += scnprintf(buf + len, size - len, "Packet Receive Cnt:\n");
	len += scnprintf(buf + len, size - len, "-------------------\n");

	len += scnprintf(buf + len, size - len, "%30s%15s\n",
			 "MAIN", "ALT");
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "TOTAL COUNT",
			 as_main->recv_cnt,
			 as_alt->recv_cnt);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA1",
			 as_main->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA1],
			 as_alt->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA1]);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA2",
			 as_main->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA2],
			 as_alt->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA2]);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA1 + LNA2",
			 as_main->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA1_PLUS_LNA2],
			 as_alt->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA1_PLUS_LNA2]);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA1 - LNA2",
			 as_main->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA1_MINUS_LNA2],
			 as_alt->lna_recv_cnt[ATH_ANT_DIV_COMB_LNA1_MINUS_LNA2]);

	len += scnprintf(buf + len, size - len, "\nLNA Config Attempts:\n");
	len += scnprintf(buf + len, size - len, "--------------------\n");

	len += scnprintf(buf + len, size - len, "%30s%15s\n",
			 "MAIN", "ALT");
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA1",
			 as_main->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA1],
			 as_alt->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA1]);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA2",
			 as_main->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA2],
			 as_alt->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA2]);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA1 + LNA2",
			 as_main->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA1_PLUS_LNA2],
			 as_alt->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA1_PLUS_LNA2]);
	len += scnprintf(buf + len, size - len, "%-14s:%15d%15d\n",
			 "LNA1 - LNA2",
			 as_main->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA1_MINUS_LNA2],
			 as_alt->lna_attempt_cnt[ATH_ANT_DIV_COMB_LNA1_MINUS_LNA2]);

exit:
	if (len > size)
		len = size;

	retval = simple_read_from_buffer(user_buf, count, ppos, buf, len);
	kfree(buf);

	return retval;
}

static const struct file_operations fops_antenna_diversity = {
	.read = read_file_antenna_diversity,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

static int read_file_dma(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;
	struct ath_hw *ah = sc->sc_ah;
	u32 val[ATH9K_NUM_DMA_DEBUG_REGS];
	int i, qcuOffset = 0, dcuOffset = 0;
	u32 *qcuBase = &val[0], *dcuBase = &val[4];

	ath9k_ps_wakeup(sc);

	REG_WRITE_D(ah, AR_MACMISC,
		  ((AR_MACMISC_DMA_OBS_LINE_8 << AR_MACMISC_DMA_OBS_S) |
		   (AR_MACMISC_MISC_OBS_BUS_1 <<
		    AR_MACMISC_MISC_OBS_BUS_MSB_S)));

	seq_puts(file, "Raw DMA Debug values:\n");

	for (i = 0; i < ATH9K_NUM_DMA_DEBUG_REGS; i++) {
		if (i % 4 == 0)
			seq_puts(file, "\n");

		val[i] = REG_READ_D(ah, AR_DMADBG_0 + (i * sizeof(u32)));
		seq_printf(file, "%d: %08x ", i, val[i]);
	}

	seq_puts(file, "\n\n");
	seq_puts(file, "Num QCU: chain_st fsp_ok fsp_st DCU: chain_st\n");

	for (i = 0; i < ATH9K_NUM_QUEUES; i++, qcuOffset += 4, dcuOffset += 5) {
		if (i == 8) {
			qcuOffset = 0;
			qcuBase++;
		}

		if (i == 6) {
			dcuOffset = 0;
			dcuBase++;
		}

		seq_printf(file, "%2d          %2x      %1x     %2x           %2x\n",
			   i, (*qcuBase & (0x7 << qcuOffset)) >> qcuOffset,
			   (*qcuBase & (0x8 << qcuOffset)) >> (qcuOffset + 3),
			   (val[2] & (0x7 << (i * 3))) >> (i * 3),
			   (*dcuBase & (0x1f << dcuOffset)) >> dcuOffset);
	}

	seq_puts(file, "\n");

	seq_printf(file, "qcu_stitch state:   %2x    qcu_fetch state:        %2x\n",
		   (val[3] & 0x003c0000) >> 18, (val[3] & 0x03c00000) >> 22);
	seq_printf(file, "qcu_complete state: %2x    dcu_complete state:     %2x\n",
		   (val[3] & 0x1c000000) >> 26, (val[6] & 0x3));
	seq_printf(file, "dcu_arb state:      %2x    dcu_fp state:           %2x\n",
		   (val[5] & 0x06000000) >> 25, (val[5] & 0x38000000) >> 27);
	seq_printf(file, "chan_idle_dur:     %3d    chan_idle_dur_valid:     %1d\n",
		   (val[6] & 0x000003fc) >> 2, (val[6] & 0x00000400) >> 10);
	seq_printf(file, "txfifo_valid_0:      %1d    txfifo_valid_1:          %1d\n",
		   (val[6] & 0x00000800) >> 11, (val[6] & 0x00001000) >> 12);
	seq_printf(file, "txfifo_dcu_num_0:   %2d    txfifo_dcu_num_1:       %2d\n",
		   (val[6] & 0x0001e000) >> 13, (val[6] & 0x001e0000) >> 17);

	seq_printf(file, "pcu observe: 0x%x\n", REG_READ_D(ah, AR_OBS_BUS_1));
	seq_printf(file, "AR_CR: 0x%x\n", REG_READ_D(ah, AR_CR));

	ath9k_ps_restore(sc);

	return 0;
}

void ath_debug_stat_interrupt(struct ath_softc *sc, enum ath9k_int status)
{
	if (status)
		sc->debug.stats.istats.total++;
	if (sc->sc_ah->caps.hw_caps & ATH9K_HW_CAP_EDMA) {
		if (status & ATH9K_INT_RXLP)
			sc->debug.stats.istats.rxlp++;
		if (status & ATH9K_INT_RXHP)
			sc->debug.stats.istats.rxhp++;
		if (status & ATH9K_INT_BB_WATCHDOG)
			sc->debug.stats.istats.bb_watchdog++;
	} else {
		if (status & ATH9K_INT_RX)
			sc->debug.stats.istats.rxok++;
	}
	if (status & ATH9K_INT_RXEOL)
		sc->debug.stats.istats.rxeol++;
	if (status & ATH9K_INT_RXORN)
		sc->debug.stats.istats.rxorn++;
	if (status & ATH9K_INT_TX)
		sc->debug.stats.istats.txok++;
	if (status & ATH9K_INT_TXURN)
		sc->debug.stats.istats.txurn++;
	if (status & ATH9K_INT_RXPHY)
		sc->debug.stats.istats.rxphyerr++;
	if (status & ATH9K_INT_RXKCM)
		sc->debug.stats.istats.rx_keycache_miss++;
	if (status & ATH9K_INT_SWBA)
		sc->debug.stats.istats.swba++;
	if (status & ATH9K_INT_BMISS)
		sc->debug.stats.istats.bmiss++;
	if (status & ATH9K_INT_BNR)
		sc->debug.stats.istats.bnr++;
	if (status & ATH9K_INT_CST)
		sc->debug.stats.istats.cst++;
	if (status & ATH9K_INT_GTT)
		sc->debug.stats.istats.gtt++;
	if (status & ATH9K_INT_TIM)
		sc->debug.stats.istats.tim++;
	if (status & ATH9K_INT_CABEND)
		sc->debug.stats.istats.cabend++;
	if (status & ATH9K_INT_DTIMSYNC)
		sc->debug.stats.istats.dtimsync++;
	if (status & ATH9K_INT_DTIM)
		sc->debug.stats.istats.dtim++;
	if (status & ATH9K_INT_TSFOOR)
		sc->debug.stats.istats.tsfoor++;
	if (status & ATH9K_INT_MCI)
		sc->debug.stats.istats.mci++;
	if (status & ATH9K_INT_GENTIMER)
		sc->debug.stats.istats.gen_timer++;
}

static int read_file_interrupt(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;

#define PR_IS(a, s)						\
	do {							\
		seq_printf(file, "%21s: %10u\n", a,		\
			   sc->debug.stats.istats.s);		\
	} while (0)

	if (sc->sc_ah->caps.hw_caps & ATH9K_HW_CAP_EDMA) {
		PR_IS("RXLP", rxlp);
		PR_IS("RXHP", rxhp);
		PR_IS("WATCHDOG", bb_watchdog);
	} else {
		PR_IS("RX", rxok);
	}
	PR_IS("RXEOL", rxeol);
	PR_IS("RXORN", rxorn);
	PR_IS("TX", txok);
	PR_IS("TXURN", txurn);
	PR_IS("MIB", mib);
	PR_IS("RXPHY", rxphyerr);
	PR_IS("RXKCM", rx_keycache_miss);
	PR_IS("SWBA", swba);
	PR_IS("BMISS", bmiss);
	PR_IS("BNR", bnr);
	PR_IS("CST", cst);
	PR_IS("GTT", gtt);
	PR_IS("TIM", tim);
	PR_IS("CABEND", cabend);
	PR_IS("DTIMSYNC", dtimsync);
	PR_IS("DTIM", dtim);
	PR_IS("TSFOOR", tsfoor);
	PR_IS("MCI", mci);
	PR_IS("GENTIMER", gen_timer);
	PR_IS("TOTAL", total);

	seq_puts(file, "SYNC_CAUSE stats:\n");

	PR_IS("Sync-All", sync_cause_all);
	PR_IS("RTC-IRQ", sync_rtc_irq);
	PR_IS("MAC-IRQ", sync_mac_irq);
	PR_IS("EEPROM-Illegal-Access", eeprom_illegal_access);
	PR_IS("APB-Timeout", apb_timeout);
	PR_IS("PCI-Mode-Conflict", pci_mode_conflict);
	PR_IS("HOST1-Fatal", host1_fatal);
	PR_IS("HOST1-Perr", host1_perr);
	PR_IS("TRCV-FIFO-Perr", trcv_fifo_perr);
	PR_IS("RADM-CPL-EP", radm_cpl_ep);
	PR_IS("RADM-CPL-DLLP-Abort", radm_cpl_dllp_abort);
	PR_IS("RADM-CPL-TLP-Abort", radm_cpl_tlp_abort);
	PR_IS("RADM-CPL-ECRC-Err", radm_cpl_ecrc_err);
	PR_IS("RADM-CPL-Timeout", radm_cpl_timeout);
	PR_IS("Local-Bus-Timeout", local_timeout);
	PR_IS("PM-Access", pm_access);
	PR_IS("MAC-Awake", mac_awake);
	PR_IS("MAC-Asleep", mac_asleep);
	PR_IS("MAC-Sleep-Access", mac_sleep_access);

	return 0;
}

static int read_file_xmit(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;

	seq_printf(file, "%30s %10s%10s%10s\n\n", "BE", "BK", "VI", "VO");

	PR("MPDUs Queued:    ", queued);
	PR("MPDUs Completed: ", completed);
	PR("MPDUs XRetried:  ", xretries);
	PR("Aggregates:      ", a_aggr);
	PR("AMPDUs Queued HW:", a_queued_hw);
	PR("AMPDUs Completed:", a_completed);
	PR("AMPDUs Retried:  ", a_retries);
	PR("AMPDUs XRetried: ", a_xretries);
	PR("TXERR Filtered:  ", txerr_filtered);
	PR("FIFO Underrun:   ", fifo_underrun);
	PR("TXOP Exceeded:   ", xtxop);
	PR("TXTIMER Expiry:  ", timer_exp);
	PR("DESC CFG Error:  ", desc_cfg_err);
	PR("DATA Underrun:   ", data_underrun);
	PR("DELIM Underrun:  ", delim_underrun);
	PR("TX-Pkts-All:     ", tx_pkts_all);
	PR("TX-Bytes-All:    ", tx_bytes_all);
	PR("HW-put-tx-buf:   ", puttxbuf);
	PR("HW-tx-start:     ", txstart);
	PR("HW-tx-proc-desc: ", txprocdesc);
	PR("TX-Failed:       ", txfailed);

	return 0;
}

static void print_queue(struct ath_softc *sc, struct ath_txq *txq,
			struct seq_file *file)
{
	ath_txq_lock(sc, txq);

	seq_printf(file, "%s: %d ", "qnum", txq->axq_qnum);
	seq_printf(file, "%s: %2d ", "qdepth", txq->axq_depth);
	seq_printf(file, "%s: %2d ", "ampdu-depth", txq->axq_ampdu_depth);
	seq_printf(file, "%s: %3d\n", "pending", txq->pending_frames);

	ath_txq_unlock(sc, txq);
}

static int read_file_queues(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;
	struct ath_txq *txq;
	int i;
	static const char *qname[4] = {
		"VO", "VI", "BE", "BK"
	};

	for (i = 0; i < IEEE80211_NUM_ACS; i++) {
		txq = sc->tx.txq_map[i];
		seq_printf(file, "(%s):  ", qname[i]);
		print_queue(sc, txq, file);
	}

	seq_puts(file, "(CAB): ");
	print_queue(sc, sc->beacon.cabq, file);

	return 0;
}


/*
 * Runtime EDCA control for the four mac80211 access categories.
 *
 * Write format:
 *   <AC> <AIFS> <CWmin> <CWmax>
 * Example:
 *   echo "BE 3 15 63" > edca_ctrl
 *
 * TXOP, readyTime and all other queue properties are preserved.
 */
static const char * const ath9k_edca_ac_names[IEEE80211_NUM_ACS] = {
	[IEEE80211_AC_VO] = "VO",
	[IEEE80211_AC_VI] = "VI",
	[IEEE80211_AC_BE] = "BE",
	[IEEE80211_AC_BK] = "BK",
};

static int ath9k_edca_ac_from_name(const char *name)
{
	int ac;

	for (ac = 0; ac < IEEE80211_NUM_ACS; ac++) {
		if (!strcasecmp(name, ath9k_edca_ac_names[ac]))
			return ac;
	}

	return -EINVAL;
}

static bool ath9k_edca_cw_is_valid(u32 cw)
{
	switch (cw) {
	case 1:
	case 3:
	case 7:
	case 15:
	case 31:
	case 63:
	case 127:
	case 255:
	case 511:
	case 1023:
		return true;
	default:
		return false;
	}
}

static bool ath9k_edca_aifs_is_valid(u32 aifs)
{
	return aifs >= 1 && aifs <= 15;
}

#define ATH9K_EDCA_CTRL_BUFSIZE 1024

struct ath9k_edca_readback {
	u32 effective_aifs;
	u32 effective_cwmin;
	u32 effective_cwmax;
	u32 hw_aifs;
	u32 hw_cwmin;
	u32 hw_cwmax;
	bool valid;
};

static const char *ath9k_edca_status_name(int status)
{
	switch (status) {
	case 0:
		return "verified";
	case 1:
		return "rolled_back";
	case 2:
		return "rollback_failed";
	case 3:
		return "readback_error";
	default:
		return "unknown";
	}
}

static void ath9k_edca_readback_locked(struct ath_softc *sc, int ac,
				       struct ath9k_edca_readback *rb)
{
	struct ath9k_tx_queue_info qi;
	struct ath_txq *txq;
	u32 regval;

	memset(rb, 0, sizeof(*rb));
	txq = sc->tx.txq_map[ac];
	if (!txq || !ath9k_hw_get_txq_props(sc->sc_ah, txq->axq_qnum, &qi))
		return;
	rb->effective_aifs = qi.tqi_aifs;
	rb->effective_cwmin = qi.tqi_cwmin;
	rb->effective_cwmax = qi.tqi_cwmax;
	regval = REG_READ(sc->sc_ah, AR_DLCL_IFS(txq->axq_qnum));
	rb->hw_cwmin = regval & AR_D_LCL_IFS_CWMIN;
	rb->hw_cwmax = (regval & AR_D_LCL_IFS_CWMAX) >> AR_D_LCL_IFS_CWMAX_S;
	rb->hw_aifs = (regval & AR_D_LCL_IFS_AIFS) >> AR_D_LCL_IFS_AIFS_S;
	rb->valid = true;
}

static int ath9k_edca_verify_locked(struct ath_softc *sc, int ac)
{
	struct ath9k_edca_readback rb;

	ath9k_edca_readback_locked(sc, ac, &rb);
	if (!rb.valid)
		return -ENODEV;
	if (rb.effective_aifs != rb.hw_aifs ||
	    rb.effective_cwmin != rb.hw_cwmin ||
	    rb.effective_cwmax != rb.hw_cwmax)
		return -EIO;
	return 0;
}

static ssize_t read_file_edca_ctrl(struct file *file, char __user *user_buf,
				   size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath9k_edca_override *override;
	struct ath9k_edca_readback rb;
	struct ath_txq *txq;
	char *buf;
	unsigned int len = 0;
	ssize_t ret;
	int ac;

	buf = kmalloc(ATH9K_EDCA_CTRL_BUFSIZE, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;
	ath9k_ps_wakeup(sc);
	mutex_lock(&sc->mutex);

	for (ac = 0; ac < IEEE80211_NUM_ACS; ac++) {
		override = &sc->edca_override[ac];
		txq = sc->tx.txq_map[ac];
		if (!txq) {
			len += scnprintf(buf + len, ATH9K_EDCA_CTRL_BUFSIZE - len,
					 "AC=%s unavailable\n",
					 ath9k_edca_ac_names[ac]);
			continue;
		}

		ath9k_edca_readback_locked(sc, ac, &rb);
		if (!rb.valid) {
			len += scnprintf(buf + len, ATH9K_EDCA_CTRL_BUFSIZE - len,
					 "AC=%s qnum=%u inactive\n",
					 ath9k_edca_ac_names[ac],
					 txq->axq_qnum);
			continue;
		}

		len += scnprintf(buf + len, ATH9K_EDCA_CTRL_BUFSIZE - len,
				 "AC=%s qnum=%u aifs=%u cwmin=%u cwmax=%u "
				 "override_enabled=%u base_valid=%u "
				 "base_aifs=%u base_cwmin=%u base_cwmax=%u "
				 "override_valid=%u override_aifs=%u "
				 "override_cwmin=%u override_cwmax=%u "
				 "hw_aifs=%u hw_cwmin=%u hw_cwmax=%u "
				 "status=%d status_name=%s\n",
				 ath9k_edca_ac_names[ac], txq->axq_qnum,
				 rb.effective_aifs, rb.effective_cwmin,
				 rb.effective_cwmax,
				 override->enabled, override->base_valid,
				 override->base.tqi_aifs,
				 override->base.tqi_cwmin,
				 override->base.tqi_cwmax,
				 override->override_valid, override->aifs,
				 override->cwmin, override->cwmax,
				 rb.hw_aifs, rb.hw_cwmin, rb.hw_cwmax,
				 override->last_status,
				 ath9k_edca_status_name(override->last_status));
	}

	mutex_unlock(&sc->mutex);
	ath9k_ps_restore(sc);

	ret = simple_read_from_buffer(user_buf, count, ppos, buf, len);
	kfree(buf);
	return ret;
}

static ssize_t write_file_edca_ctrl(struct file *file,
				    const char __user *user_buf,
				    size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	struct ath9k_edca_override saved;
	struct ath_txq *txq;
	char buf[64];
	char ac_name[8];
	char action[16];
	char extra;
	unsigned int aifs, cwmin, cwmax;
	bool set_values;
	int ac, ret;

	if (!count || count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, user_buf, count))
		return -EFAULT;
	buf[count] = '\0';

	/* Trailing conversions reject extra non-whitespace arguments. */
	set_values = sscanf(buf, "%7s %u %u %u %c",
			    ac_name, &aifs, &cwmin, &cwmax, &extra) == 4;
	if (!set_values &&
	    sscanf(buf, "%7s %15s %c", ac_name, action, &extra) != 2)
		return -EINVAL;

	ac = ath9k_edca_ac_from_name(ac_name);
	if (ac < 0)
		return ac;

	if (set_values) {
		if (!ath9k_edca_aifs_is_valid(aifs) ||
		    !ath9k_edca_cw_is_valid(cwmin) ||
		    !ath9k_edca_cw_is_valid(cwmax) || cwmin > cwmax)
			return -EINVAL;
	} else if (strcmp(action, "enable") && strcmp(action, "disable") &&
		   strcmp(action, "reset")) {
		return -EINVAL;
	}

	txq = sc->tx.txq_map[ac];
	if (!txq)
		return -ENODEV;

	ath9k_ps_wakeup(sc);
	mutex_lock(&sc->mutex);

	saved = sc->edca_override[ac];
	if (set_values)
		ret = ath9k_edca_override_set(sc, ac, aifs, cwmin, cwmax);
	else if (!strcmp(action, "enable"))
		ret = ath9k_edca_override_enable(sc, ac);
	else
		ret = ath9k_edca_override_disable(sc, ac,
						 !strcmp(action, "reset"));

	/* Verify the write against both the cached effective queue properties
	 * and the hardware register while still holding sc->mutex. A mismatch is
	 * rolled back to the pre-write state and re-verified before reporting.
	 */
	if (ret == 0) {
		int ver = ath9k_edca_verify_locked(sc, ac);

		if (ver == 0) {
			sc->edca_override[ac].last_status = 0;
		} else if (ver == -ENODEV) {
			sc->edca_override[ac].last_status = 3;
			ret = -ENODEV;
		} else {
			sc->edca_override[ac] = saved;
			if (ath9k_edca_apply_current(sc, ac) == 0) {
				int ver2 = ath9k_edca_verify_locked(sc, ac);

				if (ver2 == 0) {
					sc->edca_override[ac].last_status = 1;
					ret = -EIO;
				} else {
					sc->edca_override[ac].last_status = 2;
					ret = -EPROTO;
				}
			} else {
				sc->edca_override[ac].last_status = 2;
				ret = -EPROTO;
			}
		}
	}

	if (!ret && set_values)
		ath_info(common,
			 "EDCA override: AC=%s qnum=%u aifs=%u cwmin=%u cwmax=%u\n",
			 ath9k_edca_ac_names[ac], txq->axq_qnum,
			 aifs, cwmin, cwmax);
	else if (!ret)
		ath_info(common, "EDCA override: AC=%s qnum=%u action=%s\n",
			 ath9k_edca_ac_names[ac], txq->axq_qnum, action);
	else
		ath_err(common,
			"EDCA override: AC=%s qnum=%u verify failed, status=%s\n",
			ath9k_edca_ac_names[ac], txq->axq_qnum,
			ath9k_edca_status_name(sc->edca_override[ac].last_status));

	mutex_unlock(&sc->mutex);
	ath9k_ps_restore(sc);

	return ret ? ret : count;
}

static const struct file_operations fops_edca_ctrl = {
	.read = read_file_edca_ctrl,
	.write = write_file_edca_ctrl,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

#define ATH_DCF_ACK_FRAME_LEN 14
#define ATH_DCF_CTS_FRAME_LEN 14
#define ATH_DCF_RTS_FRAME_LEN 20

enum ath_dcf_channel_width {
	ATH_DCF_CHANNEL_NORMAL,
	ATH_DCF_CHANNEL_HALF,
	ATH_DCF_CHANNEL_QUARTER,
};

static const char *ath9k_dcf_protection_name(u8 protection)
{
	switch (protection) {
	case ATH_DCF_PROTECTION_BASIC:
		return "basic";
	case ATH_DCF_PROTECTION_RTS_CTS:
		return "rts-cts";
	case ATH_DCF_PROTECTION_CTS_SELF:
		return "cts-self";
	default:
		return "unknown";
	}
}

static u16 ath9k_dcf_channel_sifs(struct ath_hw *ah)
{
	if (ah->curchan && IS_CHAN_QUARTER_RATE(ah->curchan))
		return OFDM_SIFS_TIME_QUARTER;
	if (ah->curchan && IS_CHAN_HALF_RATE(ah->curchan))
		return OFDM_SIFS_TIME_HALF;
	if (ah->curchan && IS_CHAN_5GHZ(ah->curchan))
		return OFDM_SIFS_TIME;

	return CCK_SIFS_TIME;
}

static u16 ath9k_dcf_phy_sifs(struct ath_hw *ah, int phy)
{
	if (phy == WLAN_RC_PHY_CCK)
		return CCK_SIFS_TIME;
	if (ah->curchan && IS_CHAN_QUARTER_RATE(ah->curchan))
		return OFDM_SIFS_TIME_QUARTER;
	if (ah->curchan && IS_CHAN_HALF_RATE(ah->curchan))
		return OFDM_SIFS_TIME_HALF;

	return OFDM_SIFS_TIME;
}

static u16 ath9k_dcf_slot_time(struct ath_hw *ah)
{
	u32 slot;

	if (ah->dynack.enabled && ah->dynack.ackto > 3)
		return min_t(u32, (ah->dynack.ackto - 3) / 2, U16_MAX);
	if (ah->curchan && IS_CHAN_QUARTER_RATE(ah->curchan))
		slot = 21;
	else if (ah->curchan && IS_CHAN_HALF_RATE(ah->curchan))
		slot = 13;
	else
		slot = ah->slottime;

	slot += 3 * ah->coverage_class;
	return min_t(u32, slot, U16_MAX);
}

static u8 ath9k_dcf_channel_width(struct ath_hw *ah)
{
	if (ah->curchan && IS_CHAN_QUARTER_RATE(ah->curchan))
		return ATH_DCF_CHANNEL_QUARTER;
	if (ah->curchan && IS_CHAN_HALF_RATE(ah->curchan))
		return ATH_DCF_CHANNEL_HALF;

	return ATH_DCF_CHANNEL_NORMAL;
}

static const char *ath9k_dcf_channel_width_name(u8 width)
{
	switch (width) {
	case ATH_DCF_CHANNEL_NORMAL:
		return "normal";
	case ATH_DCF_CHANNEL_HALF:
		return "half";
	case ATH_DCF_CHANNEL_QUARTER:
		return "quarter";
	default:
		return "unknown";
	}
}

static int ath9k_dcf_rate_phy(enum nl80211_band band,
			      const struct ieee80211_rate *rate)
{
	if (band == NL80211_BAND_2GHZ &&
	    !(rate->flags & IEEE80211_RATE_ERP_G))
		return WLAN_RC_PHY_CCK;

	return WLAN_RC_PHY_OFDM;
}

static const struct ieee80211_rate *
ath9k_dcf_rate_by_hw(struct ath_common *common, enum nl80211_band band,
		     u8 hw_rate, u16 *rate_index, bool *short_preamble)
{
	struct ieee80211_supported_band *sband;
	const struct ieee80211_rate *rate;
	int i;

	if (band != NL80211_BAND_2GHZ && band != NL80211_BAND_5GHZ)
		return NULL;
	sband = &common->sbands[band];
	if (!sband->bitrates)
		return NULL;

	for (i = 0; i < sband->n_bitrates; i++) {
		rate = &sband->bitrates[i];
		if (hw_rate == rate->hw_value) {
			*rate_index = i;
			*short_preamble = false;
			return rate;
		}
		if (rate->hw_value_short && hw_rate == rate->hw_value_short) {
			*rate_index = i;
			*short_preamble = true;
			return rate;
		}
	}

	return NULL;
}

static u32 ath9k_dcf_control_airtime(struct ath_hw *ah,
				     enum nl80211_band band,
				     const struct ieee80211_rate *rate,
				     u16 rate_index, bool short_preamble,
				     u32 frame_len)
{
	u32 duration;
	int phy = ath9k_dcf_rate_phy(band, rate);
	u16 phy_sifs = ath9k_dcf_phy_sifs(ah, phy);

	duration = ath9k_hw_computetxtime(ah, phy, rate->bitrate * 100,
					 frame_len, rate_index, short_preamble);
	if (duration <= phy_sifs)
		return 0;

	return duration - phy_sifs;
}

static bool
ath9k_dcf_signature_matches(const struct ath_dcf_measure_signature *signature,
			     const struct ath_dcf_timing_sample *sample)
{
	return signature->dynack == sample->dynack &&
	       signature->band == sample->band &&
	       signature->protection == sample->protection &&
	       signature->channel_width == sample->channel_width &&
	       signature->frame_len == sample->frame_len &&
	       signature->coverage_class == sample->coverage_class &&
	       signature->data_rate_100kbps == sample->data_rate_100kbps &&
	       signature->control_rate_100kbps == sample->control_rate_100kbps &&
	       signature->sifs_us == sample->sifs_us &&
	       signature->slot_us == sample->slot_us &&
	       signature->aifsn == sample->aifsn &&
	       signature->cwmin == sample->cwmin &&
	       signature->cwmax == sample->cwmax;
}

static void
ath9k_dcf_signature_save(struct ath_dcf_measure_signature *signature,
			 const struct ath_dcf_timing_sample *sample)
{
	signature->valid = true;
	signature->dynack = sample->dynack;
	signature->band = sample->band;
	signature->protection = sample->protection;
	signature->channel_width = sample->channel_width;
	signature->frame_len = sample->frame_len;
	signature->coverage_class = sample->coverage_class;
	signature->data_rate_100kbps = sample->data_rate_100kbps;
	signature->control_rate_100kbps = sample->control_rate_100kbps;
	signature->sifs_us = sample->sifs_us;
	signature->slot_us = sample->slot_us;
	signature->aifsn = sample->aifsn;
	signature->cwmin = sample->cwmin;
	signature->cwmax = sample->cwmax;
}

static void
ath9k_dcf_accumulate_attempts(struct ath_dcf_attempt_stats *stats,
			      const struct ath_dcf_timing_sample *sample,
			      const struct ath_dcf_attempt_delta *delta)
{
	stats->packets++;
	stats->packet_successes += sample->acked;
	stats->packet_failures += !sample->acked;
	stats->short_retries += sample->short_retries;
	stats->long_retries += sample->long_retries;

	stats->exchange_attempts += delta->exchange_attempts;
	stats->exchange_successes += sample->acked;
	stats->rts_attempts += delta->rts_attempts;
	stats->rts_successes += delta->rts_successes;
	stats->data_attempts += delta->data_attempts;
	stats->data_successes += sample->acked;
	stats->modeled_exchange_us += delta->modeled_exchange_us;
}

static void ath9k_dcf_peer_clear(struct ath_node *an)
{
	memset(&an->dcf_stats, 0, sizeof(an->dcf_stats));
	an->dcf_stats.valid = true;
	ether_addr_copy(an->dcf_stats.addr, an->sta->addr);
}

void ath_debug_dcf_peer_add(struct ath_softc *sc, struct ath_node *an)
{
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;

	INIT_LIST_HEAD(&an->dcf_list);
	ath9k_dcf_peer_clear(an);
	spin_lock_bh(&measurement->lock);
	list_add_tail(&an->dcf_list, &measurement->peers);
	measurement->peer_count++;
	spin_unlock_bh(&measurement->lock);
}

void ath_debug_dcf_peer_remove(struct ath_softc *sc, struct ath_node *an)
{
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;

	spin_lock_bh(&measurement->lock);
	if (!list_empty(&an->dcf_list)) {
		list_del_init(&an->dcf_list);
		measurement->peer_count--;
	}
	an->dcf_stats.valid = false;
	spin_unlock_bh(&measurement->lock);
}

static void
ath9k_dcf_delay_accumulate(struct ath_dcf_delay_stats *delay,
			   const struct ath_dcf_timing_sample *sample,
			   bool queue_continuous)
{
	u64 delta_sq, sum_us, sum_sq_us2;
	u32 delta;

	/* A terminal drop ends the chain: the next successful departure is not
	 * the service completion of the packet that became HOL here.
	 */
	if (!sample->acked) {
		delay->anchor_valid = false;
		delay->terminal_drop_breaks++;
		return;
	}

	if (delay->anchor_valid) {
		/* AR_SendTimestamp is a wrapping 32-bit TSF-domain counter. */
		delta = sample->hw_send_timestamp - delay->anchor_tstamp;
		delta_sq = (u64)delta * delta;
		if (!delta) {
			delay->invalid_deltas++;
		} else if (check_add_overflow(delay->sum_us, (u64)delta,
					      &sum_us) ||
			   check_add_overflow(delay->sum_sq_us2, delta_sq,
					      &sum_sq_us2)) {
			delay->overflow_samples++;
		} else {
			delay->sum_us = sum_us;
			delay->sum_sq_us2 = sum_sq_us2;
			if (!delay->samples || delta < delay->min_us)
				delay->min_us = delta;
			if (delta > delay->max_us)
				delay->max_us = delta;
			delay->samples++;
		}
	}

	delay->anchor_tstamp = sample->hw_send_timestamp;
	delay->anchor_valid = queue_continuous;
	if (!queue_continuous)
		delay->queue_empty_breaks++;
}

static void
ath9k_dcf_hol_accumulate(struct ath_dcf_hol_stats *hol,
			const struct ath_dcf_timing_sample *sample)
{
	u64 start_ns, delta_us, delta_sq, sum_us, sum_sq_us2;

	if (!sample->acked) {
		hol->anchor_valid = false;
		hol->terminal_drop_breaks++;
		return;
	}
	if (!sample->hol_enqueue_ns) {
		hol->anchor_valid = false;
		hol->missing_enqueue++;
		return;
	}
	if (sample->hol_enqueue_ns >= sample->hol_done_ns) {
		hol->anchor_valid = false;
		hol->invalid_deltas++;
		return;
	}

	if (hol->anchor_valid) {
		/* A second TID or non-FIFO completion cannot identify this
		 * packet's queue-head transition from the previous departure.
		 */
		if (sample->hol_tid != hol->last_tid ||
		    sample->hol_seq != ((hol->last_seq + 1) & 0xfff) ||
		    sample->hol_enqueue_ns < hol->last_enqueue_ns) {
			hol->order_breaks++;
			goto anchor;
		}
		start_ns = max(sample->hol_enqueue_ns, hol->last_done_ns);
		if (sample->hol_done_ns <= start_ns) {
			hol->invalid_deltas++;
			goto anchor;
		}
		delta_us = div_u64(sample->hol_done_ns - start_ns,
				    NSEC_PER_USEC);
		if (!delta_us || delta_us > U32_MAX) {
			hol->invalid_deltas++;
			goto anchor;
		}
		delta_sq = delta_us * delta_us;
		if (check_add_overflow(hol->sum_us, delta_us, &sum_us) ||
		    check_add_overflow(hol->sum_sq_us2, delta_sq,
				   &sum_sq_us2)) {
			hol->overflow_samples++;
			goto anchor;
		}
		hol->sum_us = sum_us;
		hol->sum_sq_us2 = sum_sq_us2;
		if (!hol->samples || delta_us < hol->min_us)
			hol->min_us = delta_us;
		if (delta_us > hol->max_us)
			hol->max_us = delta_us;
		hol->samples++;
	} else {
		hol->anchor_skips++;
	}
anchor:
	hol->last_enqueue_ns = sample->hol_enqueue_ns;
	hol->last_done_ns = sample->hol_done_ns;
	hol->last_tid = sample->hol_tid;
	hol->last_seq = sample->hol_seq;
	hol->anchor_valid = true;
}

static void
ath9k_dcf_measurement_tx_locked(struct ath_softc *sc, struct ath_node *an,
				const struct ath_txq *txq,
				const struct ath_dcf_timing_sample *sample,
				const struct ath_dcf_attempt_delta *delta)
{
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;
	struct ath_dcf_measure_signature *signature;
	struct ath_dcf_peer_stats *peer;
	int ac = txq->mac80211_qnum;

	lockdep_assert_held(&measurement->lock);

	peer = an && an->dcf_stats.valid ? &an->dcf_stats : NULL;

	signature = &measurement->signature[ac];
	if (!signature->valid) {
		ath9k_dcf_signature_save(signature, sample);
	} else if (!ath9k_dcf_signature_matches(signature, sample)) {
		measurement->config_mismatches++;
		measurement->ac[ac].config_mismatches++;
		if (peer) {
			peer->delay[ac].anchor_valid = false;
			peer->delay[ac].config_breaks++;
			peer->hol[ac].anchor_valid = false;
			peer->hol[ac].config_breaks++;
		}
		return;
	}

	ath9k_dcf_accumulate_attempts(&measurement->ac[ac], sample, delta);
	if (peer) {
		ath9k_dcf_accumulate_attempts(&peer->ac[ac], sample, delta);
		ath9k_dcf_delay_accumulate(&peer->delay[ac], sample,
					   txq->axq_depth > 0);
		ath9k_dcf_hol_accumulate(&peer->hol[ac], sample);
	}
}

static void ath9k_dcf_measurement_exclude_locked(struct ath_softc *sc,
						 struct ath_node *an,
						 const struct ath_txq *txq)
{
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;
	struct ath_dcf_peer_stats *peer;

	lockdep_assert_held(&measurement->lock);
	if (measurement->running) {
		measurement->excluded_samples++;
		peer = an && an->dcf_stats.valid ? &an->dcf_stats : NULL;
		if (peer) {
			peer->delay[txq->mac80211_qnum].anchor_valid = false;
			peer->delay[txq->mac80211_qnum].excluded_breaks++;
			peer->hol[txq->mac80211_qnum].anchor_valid = false;
			peer->hol[txq->mac80211_qnum].excluded_breaks++;
		}
	}
}

static void ath9k_dcf_timing_tx(struct ath_softc *sc, struct ath_buf *bf,
				const struct ath_tx_status *ts,
				const struct ath_txq *txq, struct ath_node *an,
				u32 capture_state)
{
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;
	struct ieee80211_tx_info *tx_info = IEEE80211_SKB_CB(bf->bf_mpdu);
	struct ieee80211_hdr *hdr = (void *)bf->bf_mpdu->data;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	struct ath_hw *ah = sc->sc_ah;
	struct ieee80211_supported_band *sband;
	const struct ieee80211_rate *data_rate, *control_rate;
	const struct ieee80211_tx_rate *final_rate;
	struct ath_dcf_timing_sample sample = {};
	struct ath_dcf_timing_sample *saved;
	struct ath_dcf_attempt_delta delta;
	enum nl80211_band band = tx_info->band;
	bool control_short = false;
	bool fixed_rate = true;
	bool measure_sample;
	u16 control_index = 0;
	u16 data_phy_sifs;
	u32 error_status;
	u32 prior_series_attempts = 0;
	int data_phy;
	int first_rate = -1;
	int i;

	if (txq->mac80211_qnum < 0 ||
	    txq->mac80211_qnum >= IEEE80211_NUM_ACS ||
	    txq->axq_qnum >= ATH9K_NUM_TX_QUEUES)
		return;
	if (!ieee80211_is_data_present(hdr->frame_control) ||
	    is_multicast_ether_addr(hdr->addr1) ||
	    (tx_info->flags & IEEE80211_TX_CTL_NO_ACK))
		goto exclude;
	if (bf_isampdu(bf))
		goto exclude;
	if (ieee80211_has_morefrags(hdr->frame_control) ||
	    (le16_to_cpu(hdr->seq_ctrl) & IEEE80211_SCTL_FRAG))
		goto exclude;

	error_status = ts->ts_status & ATH9K_TXERR_MASK;
	if (error_status & ~ATH9K_TXERR_XRETRY)
		goto exclude;
	if (ts->ts_flags & (ATH9K_TX_DESC_CFG_ERR |
			    ATH9K_TX_DATA_UNDERRUN |
			    ATH9K_TX_DELIM_UNDERRUN))
		goto exclude;

	if (band != NL80211_BAND_2GHZ && band != NL80211_BAND_5GHZ)
		goto exclude;
	sband = &common->sbands[band];
	if (!sband->bitrates || ts->ts_rateindex >= ARRAY_SIZE(bf->rates))
		goto exclude;

	final_rate = &bf->rates[ts->ts_rateindex];
	if (!final_rate->count || final_rate->idx < 0 ||
	    final_rate->idx >= sband->n_bitrates ||
	    (final_rate->flags & (IEEE80211_TX_RC_MCS |
				 IEEE80211_TX_RC_VHT_MCS)))
		goto exclude;
	data_rate = &sband->bitrates[final_rate->idx];

	control_rate = ath9k_dcf_rate_by_hw(common, band,
				bf->bf_state.bfs_dcf_control_rate,
				&control_index, &control_short);
	if (!control_rate)
		goto exclude;

	for (i = 0; i < ARRAY_SIZE(bf->rates); i++) {
		const struct ieee80211_tx_rate *rate = &bf->rates[i];

		if (!rate->count || rate->idx < 0)
			continue;
		sample.active_series++;
		sample.configured_tries += rate->count;
		if (i < ts->ts_rateindex)
			prior_series_attempts += rate->count;
		if (first_rate < 0)
			first_rate = rate->idx;
		if (rate->idx != first_rate ||
		    (rate->flags & (IEEE80211_TX_RC_MCS |
				    IEEE80211_TX_RC_VHT_MCS)))
			fixed_rate = false;
	}
	if (!sample.active_series)
		goto exclude;

	sample.valid = true;
	sample.acked = !(ts->ts_status & ATH9K_TXERR_XRETRY);
	sample.fixed_rate = fixed_rate;
	sample.dynack = ah->dynack.enabled;
	sample.qnum = txq->axq_qnum;
	sample.band = band == NL80211_BAND_2GHZ ? 2 : 5;
	sample.protection = bf->bf_state.bfs_dcf_protection;
	sample.channel_width = ath9k_dcf_channel_width(ah);
	sample.final_series = ts->ts_rateindex;
	sample.short_retries = ts->ts_shortretry;
	sample.long_retries = ts->ts_longretry;
	sample.frame_len = bf->bf_state.bfs_dcf_frame_len;
	sample.coverage_class = ah->coverage_class;
	sample.data_rate_100kbps = data_rate->bitrate;
	sample.control_rate_100kbps = control_rate->bitrate;
	sample.sifs_us = ath9k_dcf_channel_sifs(ah);
	sample.slot_us = ath9k_dcf_slot_time(ah);
	sample.aifsn = READ_ONCE(ah->txq[txq->axq_qnum].tqi_aifs);
	sample.cwmin = READ_ONCE(ah->txq[txq->axq_qnum].tqi_cwmin);
	sample.cwmax = READ_ONCE(ah->txq[txq->axq_qnum].tqi_cwmax);
	sample.aifs_us = sample.sifs_us + sample.aifsn * sample.slot_us;
	sample.descriptor_duration_us = ts->duration;

	data_phy = ath9k_dcf_rate_phy(band, data_rate);
	data_phy_sifs = ath9k_dcf_phy_sifs(ah, data_phy);
	if (sample.descriptor_duration_us <= data_phy_sifs)
		goto exclude;
	sample.data_airtime_us = sample.descriptor_duration_us - data_phy_sifs;
	sample.ack_airtime_us = ath9k_dcf_control_airtime(ah, band,
					control_rate, control_index,
					control_short, ATH_DCF_ACK_FRAME_LEN);
	sample.rts_airtime_us = ath9k_dcf_control_airtime(ah, band,
					control_rate, control_index,
					control_short, ATH_DCF_RTS_FRAME_LEN);
	/* ACK and CTS have the same length and all other inputs are identical. */
	BUILD_BUG_ON(ATH_DCF_ACK_FRAME_LEN != ATH_DCF_CTS_FRAME_LEN);
	sample.cts_airtime_us = sample.ack_airtime_us;
	if (!sample.ack_airtime_us || !sample.rts_airtime_us ||
	    !sample.cts_airtime_us)
		goto exclude;

	if (sample.protection == ATH_DCF_PROTECTION_BASIC) {
		sample.success_time_us = sample.data_airtime_us + sample.sifs_us +
					 sample.ack_airtime_us + sample.aifs_us;
		sample.failure_time_us = sample.data_airtime_us + sample.aifs_us;
	} else if (sample.protection == ATH_DCF_PROTECTION_RTS_CTS) {
		sample.success_time_us = sample.rts_airtime_us + sample.sifs_us +
					 sample.cts_airtime_us + sample.sifs_us +
					 sample.data_airtime_us + sample.sifs_us +
					 sample.ack_airtime_us + sample.aifs_us;
		sample.failure_time_us = sample.rts_airtime_us + sample.aifs_us;
	} else {
		/* CTS-to-self is observable but not one of the paper's two models. */
		sample.success_time_us = 0;
		sample.failure_time_us = 0;
	}
	sample.hw_send_timestamp = ts->ts_tstamp;
	sample.hol_enqueue_ns = bf->bf_state.bfs_hol_enqueue_ns;
	sample.hol_done_ns = ktime_get_ns();
	sample.hol_tid = ieee80211_is_data_qos(hdr->frame_control) ?
			 (*ieee80211_get_qos_ctl(hdr) & IEEE80211_QOS_CTL_TID_MASK) :
			 0xff;
	sample.hol_seq = le16_to_cpu(hdr->seq_ctrl) >> 4;

	measure_sample = (capture_state & ATH_DCF_CAPTURE_MODE_MASK) ==
			 ATH_DCF_CAPTURE_MEASUREMENT && sample.success_time_us &&
			 sample.failure_time_us && sample.fixed_rate;
	if (measure_sample) {
		if (sample.protection == ATH_DCF_PROTECTION_BASIC)
			delta = ath_dcf_basic_attempt_delta(sample.acked,
						    sample.long_retries,
						    prior_series_attempts,
						    sample.success_time_us,
						    sample.failure_time_us);
		else
			delta = ath_dcf_attempt_delta(true, sample.acked,
						 sample.short_retries,
						 sample.long_retries,
						 sample.success_time_us,
						 sample.failure_time_us);
	}

	/* Commit both consumers before a control transition can freeze/reset. */
	spin_lock(&measurement->lock);
	if (measurement->capture_state != capture_state)
		goto unlock;
	if (measurement->running) {
		if (measure_sample)
			ath9k_dcf_measurement_tx_locked(sc, an, txq,
							&sample, &delta);
		else
			ath9k_dcf_measurement_exclude_locked(sc, an, txq);
	}

	spin_lock(&sc->debug.dcf_timing.lock);
	saved = &sc->debug.dcf_timing.ac[txq->mac80211_qnum];
	sample.samples = saved->samples + 1;
	sample.successes = saved->successes + sample.acked;
	sample.failures = saved->failures + !sample.acked;
	*saved = sample;
	spin_unlock(&sc->debug.dcf_timing.lock);
unlock:
	spin_unlock(&measurement->lock);
	return;

exclude:
	spin_lock(&measurement->lock);
	if (measurement->capture_state == capture_state)
		ath9k_dcf_measurement_exclude_locked(sc, an, txq);
	spin_unlock(&measurement->lock);
}

void ath_debug_dcf_timing_tx(struct ath_softc *sc, struct ath_buf *bf,
			     const struct ath_tx_status *ts,
			     const struct ath_txq *txq,
			     struct ath_node *an)
{
	u32 state = READ_ONCE(sc->debug.dcf_measurement.capture_state);

	/* The disabled path does not build a sample, calculate or acquire locks. */
	if (!(state & ATH_DCF_CAPTURE_MODE_MASK))
		return;
	ath9k_dcf_timing_tx(sc, bf, ts, txq, an, state);
}

u64 ath_debug_dcf_hol_enqueue_time(struct ath_softc *sc, struct sk_buff *skb)
{
	if (!READ_ONCE(sc->debug.dcf_measurement.running))
		return 0;
	return ieee80211_hol_enqueue_time_ns(skb);
}

void ath_debug_dcf_hol_exclude_tx(struct ath_softc *sc,
				  const struct ath_txq *txq,
				  struct ath_node *an)
{
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;

	if (!READ_ONCE(measurement->running) ||
	    txq->mac80211_qnum < 0 ||
	    txq->mac80211_qnum >= IEEE80211_NUM_ACS)
		return;
	spin_lock(&measurement->lock);
	ath9k_dcf_measurement_exclude_locked(sc, an, txq);
	spin_unlock(&measurement->lock);
}

static const char *ath9k_dcf_capture_name(u32 state)
{
	switch (state & ATH_DCF_CAPTURE_MODE_MASK) {
	case ATH_DCF_CAPTURE_DIAGNOSTIC:
		return "diagnostic";
	case ATH_DCF_CAPTURE_MEASUREMENT:
		return "measurement";
	default:
		return "off";
	}
}

struct ath_dcf_timing_snapshot {
	u32 rts_threshold;
	u32 capture_state;
	bool dynack;
	int coverage_class;
	u8 channel_width;
	struct ath_dcf_timing_sample ac[IEEE80211_NUM_ACS];
};

struct ath_dcf_stats_snapshot {
	u64 elapsed_us;
	struct ath_dcf_measurement data;
	struct ath_dcf_peer_stats peers[ATH_DCF_MAX_PEERS];
};

/* Both seq_file show callbacks format immutable, per-open snapshots only. */
static int read_file_dcf_timing(struct seq_file *file, void *unused)
{
	const struct ath_dcf_timing_snapshot *snapshot = file->private;
	int ac;

	seq_printf(file,
		   "version=1 units=us model=paper-virtual-slot capture=%s ",
		   ath9k_dcf_capture_name(snapshot->capture_state));
	if (snapshot->rts_threshold == (u32)-1)
		seq_printf(file,
			   "rts_threshold=off ");
	else
		seq_printf(file,
			   "rts_threshold=%u ",
			   snapshot->rts_threshold);
	seq_printf(file,
		   "dynack=%u coverage_class=%d channel_width=%s\n",
		   snapshot->dynack, snapshot->coverage_class,
		   ath9k_dcf_channel_width_name(snapshot->channel_width));
	seq_printf(file,
		   "note=descriptor_duration_includes_phy_sifs;hw_timestamp_is_raw_not_elapsed\n");

	for (ac = 0; ac < IEEE80211_NUM_ACS; ac++) {
		const struct ath_dcf_timing_sample *s = &snapshot->ac[ac];

		if (!s->valid) {
			seq_printf(file,
				   "AC=%s ready=0\n",
				   ath9k_edca_ac_names[ac]);
			continue;
		}

		seq_printf(file,
			   "AC=%s ready=1 qnum=%u band=%u mode=%s outcome=%s "
			   "dynack=%u coverage_class=%d channel_width=%s "
			   "samples=%llu successes=%llu failures=%llu "
			   "fixed_rate=%u rate_series=%u final_series=%u tries=%u "
			   "frame_len=%u data_rate_100kbps=%u control_rate_100kbps=%u "
			   "short_retries=%u long_retries=%u hw_timestamp=%u "
			   "sifs_us=%u slot_us=%u aifsn=%u cwmin=%u cwmax=%u "
			   "aifs_us=%u "
			   "descriptor_duration_us=%u data_airtime_us=%u "
			   "ack_airtime_us=%u rts_airtime_us=%u cts_airtime_us=%u "
			   "success_time_us=%u failure_time_us=%u\n",
			   ath9k_edca_ac_names[ac], s->qnum,
			   s->band, ath9k_dcf_protection_name(s->protection),
			   s->acked ? "success" : "failure",
			   s->dynack, s->coverage_class,
			   ath9k_dcf_channel_width_name(s->channel_width),
			   (unsigned long long)s->samples,
			   (unsigned long long)s->successes,
			   (unsigned long long)s->failures,
			   s->fixed_rate, s->active_series, s->final_series,
			   s->configured_tries, s->frame_len,
			   s->data_rate_100kbps, s->control_rate_100kbps,
			   s->short_retries, s->long_retries,
			   s->hw_send_timestamp, s->sifs_us, s->slot_us,
			   s->aifsn, s->cwmin, s->cwmax, s->aifs_us,
			   s->descriptor_duration_us,
			   s->data_airtime_us, s->ack_airtime_us,
			   s->rts_airtime_us, s->cts_airtime_us,
			   s->success_time_us, s->failure_time_us);
	}

	return 0;
}

static int open_file_dcf_timing(struct inode *inode, struct file *file)
{
	struct ath_softc *sc = inode->i_private;
	struct ath_hw *ah = sc->sc_ah;
	struct ath_dcf_timing_snapshot *snapshot;
	int ret;

	snapshot = kzalloc(sizeof(*snapshot), GFP_KERNEL);
	if (!snapshot)
		return -ENOMEM;

	snapshot->rts_threshold = READ_ONCE(sc->hw->wiphy->rts_threshold);
	snapshot->dynack = READ_ONCE(ah->dynack.enabled);
	snapshot->coverage_class = READ_ONCE(ah->coverage_class);
	snapshot->channel_width = ath9k_dcf_channel_width(ah);
	spin_lock_bh(&sc->debug.dcf_timing.lock);
	snapshot->capture_state = READ_ONCE(sc->debug.dcf_measurement.capture_state);
	memcpy(snapshot->ac, sc->debug.dcf_timing.ac, sizeof(snapshot->ac));
	spin_unlock_bh(&sc->debug.dcf_timing.lock);

	ret = single_open(file, read_file_dcf_timing, snapshot);
	if (ret)
		kfree(snapshot);
	return ret;
}

static int release_file_dcf_snapshot(struct inode *inode, struct file *file)
{
	struct seq_file *seq = file->private_data;

	if (!seq)
		return 0;
	kvfree(seq->private);
	return single_release(inode, file);
}

static const struct file_operations fops_dcf_timing = {
	.read = seq_read,
	.open = open_file_dcf_timing,
	.release = release_file_dcf_snapshot,
	.owner = THIS_MODULE,
	.llseek = seq_lseek,
};

static void
ath9k_dcf_measurement_clear_locked(struct ath_dcf_measurement *measurement)
{
	struct ath_node *an;

	lockdep_assert_held(&measurement->lock);
	memset(measurement->signature, 0, sizeof(measurement->signature));
	memset(measurement->ac, 0, sizeof(measurement->ac));
	list_for_each_entry(an, &measurement->peers, dcf_list)
		ath9k_dcf_peer_clear(an);
	measurement->excluded_samples = 0;
	measurement->config_mismatches = 0;
	measurement->peer_overflow = 0;
	measurement->start_ns = 0;
	measurement->stop_ns = 0;
}

static int read_file_dcf_stats(struct seq_file *file, void *unused)
{
	const struct ath_dcf_stats_snapshot *view = file->private;
	const struct ath_dcf_measurement *snapshot = &view->data;
	int ac, i;

	seq_puts(file, "version=2 units=us scope=tx-attempt-window ");
	seq_printf(file,
		   "running=%u generation=%u elapsed_us=%llu capture=%s hol_abi=1 "
		   "basic_retry_abi=2 rts_retry_abi=1 "
		   "peer_capacity=%u peer_overflow=%llu "
		   "config_mismatches=%llu excluded_samples=%llu\n",
		   snapshot->running, snapshot->generation,
		   (unsigned long long)view->elapsed_us,
		   ath9k_dcf_capture_name(snapshot->capture_state),
		   ATH_DCF_MAX_PEERS,
		   (unsigned long long)snapshot->peer_overflow,
		   (unsigned long long)snapshot->config_mismatches,
		   (unsigned long long)snapshot->excluded_samples);
	seq_printf(file,
		   "note=probabilities_are_user_space_ratios;"
		   "modeled_exchange_excludes_backoff_and_busy_freeze;"
		   "hol_delay_is_per_peer_saturated_tsf_departure_delta;"
		   "hol_delay_requires_fixed_signature_and_queue_continuity;"
		   "hol_service_start=max(mac80211_txq_enqueue,previous_tx_status);"
		   "hol_service_end=ath9k_acked_tx_status;"
		   "hol_service_condition=ack_success;"
		   "hol_service_clock=host_monotonic;"
		   "hol_service_requires_one_fifo_tid_and_callback_calibration;"
		   "hol_service_is_not_hardware_tsf_exact;"
		   "basic_attempts_include_prior_rate_series;"
		   "rts_retry_stage_mapping_requires_monitor_validation\n");

	for (ac = 0; ac < IEEE80211_NUM_ACS; ac++) {
		const struct ath_dcf_attempt_stats *s = &snapshot->ac[ac];
		const struct ath_dcf_measure_signature *signature =
			&snapshot->signature[ac];

		if (!signature->valid) {
			seq_printf(file,
				   "AC=%s ready=0 packets=0\n",
				   ath9k_edca_ac_names[ac]);
			continue;
		}

		seq_printf(file,
			   "AC=%s ready=1 band=%u mode=%s dynack=%u "
			   "coverage_class=%d channel_width=%s frame_len=%u "
			   "data_rate_100kbps=%u control_rate_100kbps=%u "
			   "sifs_us=%u slot_us=%u aifsn=%u cwmin=%u cwmax=%u "
			   "packets=%llu packet_successes=%llu packet_failures=%llu "
			   "exchange_attempts=%llu exchange_successes=%llu "
			   "rts_attempts=%llu rts_successes=%llu "
			   "data_attempts=%llu data_successes=%llu "
			   "short_retries=%llu long_retries=%llu "
			   "modeled_exchange_us=%llu config_mismatches=%llu\n",
			   ath9k_edca_ac_names[ac], signature->band,
			   ath9k_dcf_protection_name(signature->protection),
			   signature->dynack, signature->coverage_class,
			   ath9k_dcf_channel_width_name(signature->channel_width),
			   signature->frame_len, signature->data_rate_100kbps,
			   signature->control_rate_100kbps, signature->sifs_us,
			   signature->slot_us, signature->aifsn,
			   signature->cwmin, signature->cwmax,
			   (unsigned long long)s->packets,
			   (unsigned long long)s->packet_successes,
			   (unsigned long long)s->packet_failures,
			   (unsigned long long)s->exchange_attempts,
			   (unsigned long long)s->exchange_successes,
			   (unsigned long long)s->rts_attempts,
			   (unsigned long long)s->rts_successes,
			   (unsigned long long)s->data_attempts,
			   (unsigned long long)s->data_successes,
			   (unsigned long long)s->short_retries,
			   (unsigned long long)s->long_retries,
			   (unsigned long long)s->modeled_exchange_us,
			   (unsigned long long)s->config_mismatches);
	}

	for (i = 0; i < snapshot->peer_count; i++) {
		const struct ath_dcf_peer_stats *peer = &view->peers[i];

		if (!peer->valid)
			continue;
		for (ac = 0; ac < IEEE80211_NUM_ACS; ac++) {
			const struct ath_dcf_attempt_stats *s = &peer->ac[ac];
			const struct ath_dcf_delay_stats *delay = &peer->delay[ac];
			const struct ath_dcf_hol_stats *hol = &peer->hol[ac];

			if (!s->packets)
				continue;
			seq_printf(file,
				   "peer=%pM AC=%s packets=%llu "
				   "packet_successes=%llu packet_failures=%llu "
				   "exchange_attempts=%llu exchange_successes=%llu "
				   "rts_attempts=%llu rts_successes=%llu "
				   "data_attempts=%llu data_successes=%llu "
				   "short_retries=%llu long_retries=%llu "
				   "modeled_exchange_us=%llu "
				   "delay_samples=%llu delay_sum_us=%llu "
				   "delay_sum_sq_us2=%llu delay_min_us=%u "
				   "delay_max_us=%u "
				   "delay_queue_empty_breaks=%llu "
				   "delay_drop_breaks=%llu "
				   "delay_config_breaks=%llu "
				   "delay_excluded_breaks=%llu "
				   "delay_invalid_deltas=%llu "
				   "delay_overflow_samples=%llu "
				   "hol_service_samples=%llu "
				   "hol_service_sum_us=%llu "
				   "hol_service_sum_sq_us2=%llu "
				   "hol_service_min_us=%u "
				   "hol_service_max_us=%u "
				   "hol_service_anchor_skips=%llu "
				   "hol_service_missing_enqueue=%llu "
				   "hol_service_order_breaks=%llu "
				   "hol_service_drop_breaks=%llu "
				   "hol_service_config_breaks=%llu "
				   "hol_service_excluded_breaks=%llu "
				   "hol_service_invalid_deltas=%llu "
				   "hol_service_overflow_samples=%llu\n",
				   peer->addr, ath9k_edca_ac_names[ac],
				   (unsigned long long)s->packets,
				   (unsigned long long)s->packet_successes,
				   (unsigned long long)s->packet_failures,
				   (unsigned long long)s->exchange_attempts,
				   (unsigned long long)s->exchange_successes,
				   (unsigned long long)s->rts_attempts,
				   (unsigned long long)s->rts_successes,
				   (unsigned long long)s->data_attempts,
				   (unsigned long long)s->data_successes,
				   (unsigned long long)s->short_retries,
				   (unsigned long long)s->long_retries,
				   (unsigned long long)s->modeled_exchange_us,
				   (unsigned long long)delay->samples,
				   (unsigned long long)delay->sum_us,
				   (unsigned long long)delay->sum_sq_us2,
				   delay->min_us, delay->max_us,
				   (unsigned long long)delay->queue_empty_breaks,
				   (unsigned long long)delay->terminal_drop_breaks,
				   (unsigned long long)delay->config_breaks,
				   (unsigned long long)delay->excluded_breaks,
				   (unsigned long long)delay->invalid_deltas,
				   (unsigned long long)delay->overflow_samples,
				   (unsigned long long)hol->samples,
				   (unsigned long long)hol->sum_us,
				   (unsigned long long)hol->sum_sq_us2,
				   hol->min_us, hol->max_us,
				   (unsigned long long)hol->anchor_skips,
				   (unsigned long long)hol->missing_enqueue,
				   (unsigned long long)hol->order_breaks,
				   (unsigned long long)hol->terminal_drop_breaks,
				   (unsigned long long)hol->config_breaks,
				   (unsigned long long)hol->excluded_breaks,
				   (unsigned long long)hol->invalid_deltas,
				   (unsigned long long)hol->overflow_samples);
		}
	}

	return 0;
}

static int open_file_dcf_stats(struct inode *inode, struct file *file)
{
	struct ath_softc *sc = inode->i_private;
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;
	struct ath_dcf_stats_snapshot *view;
	struct ath_dcf_measurement *snapshot;
	struct ath_node *an;
	u64 end_ns;
	int peer = 0;
	int ret;

	/* Writes use inode->i_private; they do not allocate a read snapshot. */
	if (!(file->f_mode & FMODE_READ)) {
		file->private_data = NULL;
		return 0;
	}
	view = kvzalloc(sizeof(*view), GFP_KERNEL);
	if (!view)
		return -ENOMEM;
	snapshot = &view->data;

	spin_lock_bh(&measurement->lock);
	end_ns = measurement->running ? ktime_get_ns() : measurement->stop_ns;
	snapshot->capture_state = measurement->capture_state;
	snapshot->running = measurement->running;
	snapshot->generation = measurement->generation;
	snapshot->peer_count = min_t(u16, measurement->peer_count,
				     ATH_DCF_MAX_PEERS);
	snapshot->peer_overflow = measurement->peer_overflow;
	if (measurement->peer_count > ATH_DCF_MAX_PEERS)
		snapshot->peer_overflow += measurement->peer_count -
					   ATH_DCF_MAX_PEERS;
	snapshot->config_mismatches = measurement->config_mismatches;
	snapshot->excluded_samples = measurement->excluded_samples;
	if (measurement->start_ns && end_ns >= measurement->start_ns)
		view->elapsed_us = end_ns - measurement->start_ns;
	memcpy(snapshot->signature, measurement->signature,
	       sizeof(snapshot->signature));
	memcpy(snapshot->ac, measurement->ac, sizeof(snapshot->ac));
	list_for_each_entry(an, &measurement->peers, dcf_list) {
		if (peer == ATH_DCF_MAX_PEERS)
			break;
		view->peers[peer++] = an->dcf_stats;
	}
	spin_unlock_bh(&measurement->lock);
	view->elapsed_us = div_u64(view->elapsed_us, NSEC_PER_USEC);

	ret = single_open(file, read_file_dcf_stats, view);
	if (ret)
		kvfree(view);
	return ret;
}

static ssize_t write_file_dcf_stats(struct file *file,
				    const char __user *user_buf,
				    size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file_inode(file)->i_private;
	struct ath_dcf_measurement *measurement = &sc->debug.dcf_measurement;
	char buf[32];
	char *command;
	u64 now_ns;
	u32 mode = ATH_DCF_CAPTURE_OFF;
	bool diagnostic;

	if (!count || count >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, user_buf, count))
		return -EFAULT;
	buf[count] = '\0';
	command = strim(buf);
	diagnostic = !strcmp(command, "diagnostic");
	if (strcmp(command, "start") && strcmp(command, "stop") &&
	    strcmp(command, "reset") && !diagnostic && strcmp(command, "off"))
		return -EINVAL;

	spin_lock_bh(&measurement->lock);
	if (measurement->running && (diagnostic || !strcmp(command, "off"))) {
		spin_unlock_bh(&measurement->lock);
		return -EBUSY;
	}
	/* State publication and sample clearing share the timing reader's lock. */
	spin_lock(&sc->debug.dcf_timing.lock);
	now_ns = ktime_get_ns();
	if (!strcmp(command, "start")) {
		ath9k_dcf_measurement_clear_locked(measurement);
		measurement->generation++;
		memset(sc->debug.dcf_timing.ac, 0,
		       sizeof(sc->debug.dcf_timing.ac));
		measurement->running = true;
		measurement->start_ns = now_ns;
		mode = ATH_DCF_CAPTURE_MEASUREMENT;
	} else if (!strcmp(command, "stop")) {
		if (measurement->running)
			measurement->stop_ns = now_ns;
		measurement->running = false;
	} else if (!strcmp(command, "reset")) {
		ath9k_dcf_measurement_clear_locked(measurement);
		measurement->generation++;
		measurement->running = false;
		memset(sc->debug.dcf_timing.ac, 0,
		       sizeof(sc->debug.dcf_timing.ac));
	} else if (diagnostic) {
		memset(sc->debug.dcf_timing.ac, 0,
		       sizeof(sc->debug.dcf_timing.ac));
		mode = ATH_DCF_CAPTURE_DIAGNOSTIC;
	}
	WRITE_ONCE(measurement->capture_state,
		   ath_dcf_capture_next(measurement->capture_state, mode));
	spin_unlock(&sc->debug.dcf_timing.lock);
	spin_unlock_bh(&measurement->lock);

	return count;
}

static loff_t seek_file_dcf_stats(struct file *file, loff_t offset, int whence)
{
	if (!(file->f_mode & FMODE_READ))
		return -ESPIPE;
	return seq_lseek(file, offset, whence);
}

static const struct file_operations fops_dcf_stats = {
	.read = seq_read,
	.write = write_file_dcf_stats,
	.open = open_file_dcf_stats,
	.release = release_file_dcf_snapshot,
	.owner = THIS_MODULE,
	.llseek = seek_file_dcf_stats,
};

static int read_file_misc(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;
	struct ath_common *common = ath9k_hw_common(sc->sc_ah);
	struct ath9k_vif_iter_data iter_data;
	struct ath_chanctx *ctx;
	unsigned int reg;
	u32 rxfilter, i;

	seq_printf(file, "BSSID: %pM\n", common->curbssid);
	seq_printf(file, "BSSID-MASK: %pM\n", common->bssidmask);
	seq_printf(file, "OPMODE: %s\n",
		   ath_opmode_to_string(sc->sc_ah->opmode));

	ath9k_ps_wakeup(sc);
	rxfilter = ath9k_hw_getrxfilter(sc->sc_ah);
	ath9k_ps_restore(sc);

	seq_printf(file, "RXFILTER: 0x%x", rxfilter);

	if (rxfilter & ATH9K_RX_FILTER_UCAST)
		seq_puts(file, " UCAST");
	if (rxfilter & ATH9K_RX_FILTER_MCAST)
		seq_puts(file, " MCAST");
	if (rxfilter & ATH9K_RX_FILTER_BCAST)
		seq_puts(file, " BCAST");
	if (rxfilter & ATH9K_RX_FILTER_CONTROL)
		seq_puts(file, " CONTROL");
	if (rxfilter & ATH9K_RX_FILTER_BEACON)
		seq_puts(file, " BEACON");
	if (rxfilter & ATH9K_RX_FILTER_PROM)
		seq_puts(file, " PROM");
	if (rxfilter & ATH9K_RX_FILTER_PROBEREQ)
		seq_puts(file, " PROBEREQ");
	if (rxfilter & ATH9K_RX_FILTER_PHYERR)
		seq_puts(file, " PHYERR");
	if (rxfilter & ATH9K_RX_FILTER_MYBEACON)
		seq_puts(file, " MYBEACON");
	if (rxfilter & ATH9K_RX_FILTER_COMP_BAR)
		seq_puts(file, " COMP_BAR");
	if (rxfilter & ATH9K_RX_FILTER_PSPOLL)
		seq_puts(file, " PSPOLL");
	if (rxfilter & ATH9K_RX_FILTER_PHYRADAR)
		seq_puts(file, " PHYRADAR");
	if (rxfilter & ATH9K_RX_FILTER_MCAST_BCAST_ALL)
		seq_puts(file, " MCAST_BCAST_ALL");
	if (rxfilter & ATH9K_RX_FILTER_CONTROL_WRAPPER)
		seq_puts(file, " CONTROL_WRAPPER");

	seq_puts(file, "\n");

	reg = sc->sc_ah->imask;

	seq_printf(file, "INTERRUPT-MASK: 0x%x", reg);

	if (reg & ATH9K_INT_SWBA)
		seq_puts(file, " SWBA");
	if (reg & ATH9K_INT_BMISS)
		seq_puts(file, " BMISS");
	if (reg & ATH9K_INT_CST)
		seq_puts(file, " CST");
	if (reg & ATH9K_INT_RX)
		seq_puts(file, " RX");
	if (reg & ATH9K_INT_RXHP)
		seq_puts(file, " RXHP");
	if (reg & ATH9K_INT_RXLP)
		seq_puts(file, " RXLP");
	if (reg & ATH9K_INT_BB_WATCHDOG)
		seq_puts(file, " BB_WATCHDOG");

	seq_puts(file, "\n");

	i = 0;
	ath_for_each_chanctx(sc, ctx) {
		if (list_empty(&ctx->vifs))
			continue;
		ath9k_calculate_iter_data(sc, ctx, &iter_data);

		seq_printf(file,
			   "VIFS: CTX %i(%i) AP: %i STA: %i MESH: %i",
			   i++, (int)(ctx->assigned), iter_data.naps,
			   iter_data.nstations,
			   iter_data.nmeshes);
		seq_printf(file, " ADHOC: %i OCB: %i TOTAL: %hi BEACON-VIF: %hi\n",
			   iter_data.nadhocs, iter_data.nocbs, sc->cur_chan->nvifs,
			   sc->nbcnvifs);
	}

	return 0;
}

static int read_file_reset(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;
	static const char * const reset_cause[__RESET_TYPE_MAX] = {
		[RESET_TYPE_BB_HANG] = "Baseband Hang",
		[RESET_TYPE_BB_WATCHDOG] = "Baseband Watchdog",
		[RESET_TYPE_FATAL_INT] = "Fatal HW Error",
		[RESET_TYPE_TX_ERROR] = "TX HW error",
		[RESET_TYPE_TX_GTT] = "Transmit timeout",
		[RESET_TYPE_TX_HANG] = "TX Path Hang",
		[RESET_TYPE_PLL_HANG] = "PLL RX Hang",
		[RESET_TYPE_MAC_HANG] = "MAC Hang",
		[RESET_TYPE_BEACON_STUCK] = "Stuck Beacon",
		[RESET_TYPE_MCI] = "MCI Reset",
		[RESET_TYPE_CALIBRATION] = "Calibration error",
		[RESET_TX_DMA_ERROR] = "Tx DMA stop error",
		[RESET_RX_DMA_ERROR] = "Rx DMA stop error",
	};
	int i;

	for (i = 0; i < ARRAY_SIZE(reset_cause); i++) {
		if (!reset_cause[i])
		    continue;

		seq_printf(file, "%17s: %2d\n", reset_cause[i],
			   sc->debug.stats.reset[i]);
	}

	return 0;
}

void ath_debug_stat_tx(struct ath_softc *sc, struct ath_buf *bf,
		       struct ath_tx_status *ts, struct ath_txq *txq,
		       unsigned int flags)
{
	int qnum = txq->axq_qnum;

	TX_STAT_INC(sc, qnum, tx_pkts_all);
	sc->debug.stats.txstats[qnum].tx_bytes_all += bf->bf_mpdu->len;

	if (bf_isampdu(bf)) {
		if (flags & ATH_TX_ERROR)
			TX_STAT_INC(sc, qnum, a_xretries);
		else
			TX_STAT_INC(sc, qnum, a_completed);
	} else {
		if (ts->ts_status & ATH9K_TXERR_XRETRY)
			TX_STAT_INC(sc, qnum, xretries);
		else
			TX_STAT_INC(sc, qnum, completed);
	}

	if (ts->ts_status & ATH9K_TXERR_FILT)
		TX_STAT_INC(sc, qnum, txerr_filtered);
	if (ts->ts_status & ATH9K_TXERR_FIFO)
		TX_STAT_INC(sc, qnum, fifo_underrun);
	if (ts->ts_status & ATH9K_TXERR_XTXOP)
		TX_STAT_INC(sc, qnum, xtxop);
	if (ts->ts_status & ATH9K_TXERR_TIMER_EXPIRED)
		TX_STAT_INC(sc, qnum, timer_exp);
	if (ts->ts_flags & ATH9K_TX_DESC_CFG_ERR)
		TX_STAT_INC(sc, qnum, desc_cfg_err);
	if (ts->ts_flags & ATH9K_TX_DATA_UNDERRUN)
		TX_STAT_INC(sc, qnum, data_underrun);
	if (ts->ts_flags & ATH9K_TX_DELIM_UNDERRUN)
		TX_STAT_INC(sc, qnum, delim_underrun);
}

void ath_debug_stat_rx(struct ath_softc *sc, struct ath_rx_status *rs)
{
	ath9k_cmn_debug_stat_rx(&sc->debug.stats.rxstats, rs);
}

static ssize_t read_file_regidx(struct file *file, char __user *user_buf,
				size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	char buf[32];
	unsigned int len;

	len = sprintf(buf, "0x%08x\n", sc->debug.regidx);
	return simple_read_from_buffer(user_buf, count, ppos, buf, len);
}

static ssize_t write_file_regidx(struct file *file, const char __user *user_buf,
				 size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	unsigned long regidx;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &regidx))
		return -EINVAL;

	sc->debug.regidx = regidx;
	return count;
}

static const struct file_operations fops_regidx = {
	.read = read_file_regidx,
	.write = write_file_regidx,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

static ssize_t read_file_regval(struct file *file, char __user *user_buf,
				size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	char buf[32];
	unsigned int len;
	u32 regval;

	ath9k_ps_wakeup(sc);
	regval = REG_READ_D(ah, sc->debug.regidx);
	ath9k_ps_restore(sc);
	len = sprintf(buf, "0x%08x\n", regval);
	return simple_read_from_buffer(user_buf, count, ppos, buf, len);
}

static ssize_t write_file_regval(struct file *file, const char __user *user_buf,
				 size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	unsigned long regval;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &regval))
		return -EINVAL;

	ath9k_ps_wakeup(sc);
	REG_WRITE_D(ah, sc->debug.regidx, regval);
	ath9k_ps_restore(sc);
	return count;
}

static const struct file_operations fops_regval = {
	.read = read_file_regval,
	.write = write_file_regval,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

#define REGDUMP_LINE_SIZE	20

static int open_file_regdump(struct inode *inode, struct file *file)
{
	struct ath_softc *sc = inode->i_private;
	unsigned int len = 0;
	u8 *buf;
	int i, j = 0;
	unsigned long num_regs, regdump_len, max_reg_offset;
	static const struct reg_hole {
		u32 start;
		u32 end;
	} reg_hole_list[] = {
		{0x0200, 0x07fc},
		{0x0c00, 0x0ffc},
		{0x2000, 0x3ffc},
		{0x4100, 0x6ffc},
		{0x705c, 0x7ffc},
		{0x0000, 0x0000}
	};

	max_reg_offset = AR_SREV_9300_20_OR_LATER(sc->sc_ah) ? 0x8800 : 0xb500;
	num_regs = max_reg_offset / 4 + 1;
	regdump_len = num_regs * REGDUMP_LINE_SIZE + 1;
	buf = vmalloc(regdump_len);
	if (!buf)
		return -ENOMEM;

	ath9k_ps_wakeup(sc);
	for (i = 0; i < num_regs; i++) {
		if (reg_hole_list[j].start == i << 2) {
			i = reg_hole_list[j].end >> 2;
			j++;
			continue;
		}

		len += scnprintf(buf + len, regdump_len - len,
			"0x%06x 0x%08x\n", i << 2, REG_READ(sc->sc_ah, i << 2));
	}
	ath9k_ps_restore(sc);

	file->private_data = buf;

	return 0;
}

static const struct file_operations fops_regdump = {
	.open = open_file_regdump,
	.read = ath9k_debugfs_read_buf,
	.release = ath9k_debugfs_release_buf,
	.owner = THIS_MODULE,
	.llseek = default_llseek,/* read accesses f_pos */
};

static int read_file_dump_nfcal(struct seq_file *file, void *data)
{
	struct ieee80211_hw *hw = dev_get_drvdata(file->private);
	struct ath_softc *sc = hw->priv;
	struct ath_hw *ah = sc->sc_ah;
	struct ath9k_nfcal_hist *h = sc->cur_chan->caldata.nfCalHist;
	struct ath_common *common = ath9k_hw_common(ah);
	struct ieee80211_conf *conf = &common->hw->conf;
	u32 i, j;
	u8 chainmask = (ah->rxchainmask << 3) | ah->rxchainmask;
	u8 nread;

	seq_printf(file, "Channel Noise Floor : %d\n", ah->noise);
	seq_puts(file, "Chain | privNF | # Readings | NF Readings\n");
	for (i = 0; i < NUM_NF_READINGS; i++) {
		if (!(chainmask & (1 << i)) ||
		    ((i >= AR5416_MAX_CHAINS) && !conf_is_ht40(conf)))
			continue;

		nread = AR_PHY_CCA_FILTERWINDOW_LENGTH - h[i].invalidNFcount;
		seq_printf(file, " %d\t %d\t %d\t\t", i, h[i].privNF, nread);
		for (j = 0; j < nread; j++)
			seq_printf(file, " %d", h[i].nfCalBuffer[j]);
		seq_puts(file, "\n");
	}

	return 0;
}

#ifdef CONFIG_ATH9K_BTCOEX_SUPPORT
static ssize_t read_file_btcoex(struct file *file, char __user *user_buf,
				size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	u32 len = 0, size = 1500;
	char *buf;
	size_t retval;

	buf = kzalloc(size, GFP_KERNEL);
	if (buf == NULL)
		return -ENOMEM;

	if (!sc->sc_ah->common.btcoex_enabled) {
		len = scnprintf(buf, size, "%s\n",
				"BTCOEX is disabled");
		goto exit;
	}

	len = ath9k_dump_btcoex(sc, buf, size);
exit:
	retval = simple_read_from_buffer(user_buf, count, ppos, buf, len);
	kfree(buf);

	return retval;
}

static const struct file_operations fops_btcoex = {
	.read = read_file_btcoex,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};
#endif

#ifdef CONFIG_ATH9K_DYNACK
static ssize_t read_file_ackto(struct file *file, char __user *user_buf,
			       size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	char buf[32];
	unsigned int len;

	len = sprintf(buf, "%u %c\n", ah->dynack.ackto,
		      (ah->dynack.enabled) ? 'A' : 'S');

	return simple_read_from_buffer(user_buf, count, ppos, buf, len);
}

static const struct file_operations fops_ackto = {
	.read = read_file_ackto,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};
#endif

#ifdef CONFIG_ATH9K_WOW

static ssize_t read_file_wow(struct file *file, char __user *user_buf,
			     size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	unsigned int len = 0, size = 32;
	ssize_t retval;
	char *buf;

	buf = kzalloc(size, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	len += scnprintf(buf + len, size - len, "WOW: %s\n",
			 sc->force_wow ? "ENABLED" : "DISABLED");

	if (len > size)
		len = size;

	retval = simple_read_from_buffer(user_buf, count, ppos, buf, len);
	kfree(buf);

	return retval;
}

static ssize_t write_file_wow(struct file *file, const char __user *user_buf,
			      size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	unsigned long val;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &val))
		return -EINVAL;

	if (val != 1)
		return -EINVAL;

	if (!sc->force_wow) {
		sc->force_wow = true;
		ath9k_init_wow(sc->hw);
	}

	return count;
}

static const struct file_operations fops_wow = {
	.read = read_file_wow,
	.write = write_file_wow,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

#endif

static ssize_t read_file_tpc(struct file *file, char __user *user_buf,
			     size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	unsigned int len = 0, size = 32;
	ssize_t retval;
	char *buf;

	buf = kzalloc(size, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	len += scnprintf(buf + len, size - len, "%s\n",
			 ah->tpc_enabled ? "ENABLED" : "DISABLED");

	if (len > size)
		len = size;

	retval = simple_read_from_buffer(user_buf, count, ppos, buf, len);
	kfree(buf);

	return retval;
}

static ssize_t write_file_tpc(struct file *file, const char __user *user_buf,
			      size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	unsigned long val;
	char buf[32];
	ssize_t len;
	bool tpc_enabled;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (kstrtoul(buf, 0, &val))
		return -EINVAL;

	if (val > 1)
		return -EINVAL;

	tpc_enabled = !!val;

	if (tpc_enabled != ah->tpc_enabled) {
		ah->tpc_enabled = tpc_enabled;

		mutex_lock(&sc->mutex);
		ath9k_set_txpower(sc, NULL);
		mutex_unlock(&sc->mutex);
	}

	return count;
}

static const struct file_operations fops_tpc = {
	.read = read_file_tpc,
	.write = write_file_tpc,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

static ssize_t read_file_nf_override(struct file *file,
				     char __user *user_buf,
				     size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	char buf[32];
	unsigned int len;

	if (ah->nf_override == 0)
		len = sprintf(buf, "off\n");
	else
		len = sprintf(buf, "%d\n", ah->nf_override);

	return simple_read_from_buffer(user_buf, count, ppos, buf, len);
}

static ssize_t write_file_nf_override(struct file *file,
				      const char __user *user_buf,
				      size_t count, loff_t *ppos)
{
	struct ath_softc *sc = file->private_data;
	struct ath_hw *ah = sc->sc_ah;
	long val;
	char buf[32];
	ssize_t len;

	len = min(count, sizeof(buf) - 1);
	if (copy_from_user(buf, user_buf, len))
		return -EFAULT;

	buf[len] = '\0';
	if (strncmp("off", buf, 3) == 0)
		val = 0;
	else if (kstrtol(buf, 0, &val))
		return -EINVAL;

	if (val > 0)
		return -EINVAL;

	if (val < -120)
		return -EINVAL;

	ah->nf_override = val;

	if (ah->curchan) {
		ath9k_ps_wakeup(sc);
		ath9k_hw_loadnf(ah, ah->curchan);
		ath9k_ps_restore(sc);
	}

	return count;
}

static const struct file_operations fops_nf_override = {
	.read = read_file_nf_override,
	.write = write_file_nf_override,
	.open = simple_open,
	.owner = THIS_MODULE,
	.llseek = default_llseek,
};

/* Ethtool support for get-stats */

#define AMKSTR(nm) #nm "_BE", #nm "_BK", #nm "_VI", #nm "_VO"
static const char ath9k_gstrings_stats[][ETH_GSTRING_LEN] = {
	"tx_pkts_nic",
	"tx_bytes_nic",
	"rx_pkts_nic",
	"rx_bytes_nic",
	AMKSTR(d_tx_pkts),
	AMKSTR(d_tx_bytes),
	AMKSTR(d_tx_mpdus_queued),
	AMKSTR(d_tx_mpdus_completed),
	AMKSTR(d_tx_mpdu_xretries),
	AMKSTR(d_tx_aggregates),
	AMKSTR(d_tx_ampdus_queued_hw),
	AMKSTR(d_tx_ampdus_completed),
	AMKSTR(d_tx_ampdu_retries),
	AMKSTR(d_tx_ampdu_xretries),
	AMKSTR(d_tx_fifo_underrun),
	AMKSTR(d_tx_op_exceeded),
	AMKSTR(d_tx_timer_expiry),
	AMKSTR(d_tx_desc_cfg_err),
	AMKSTR(d_tx_data_underrun),
	AMKSTR(d_tx_delim_underrun),
	"d_rx_crc_err",
	"d_rx_decrypt_crc_err",
	"d_rx_phy_err",
	"d_rx_mic_err",
	"d_rx_pre_delim_crc_err",
	"d_rx_post_delim_crc_err",
	"d_rx_decrypt_busy_err",

	"d_rx_phyerr_radar",
	"d_rx_phyerr_ofdm_timing",
	"d_rx_phyerr_cck_timing",

};
#define ATH9K_SSTATS_LEN ARRAY_SIZE(ath9k_gstrings_stats)

void ath9k_get_et_strings(struct ieee80211_hw *hw,
			  struct ieee80211_vif *vif,
			  u32 sset, u8 *data)
{
	if (sset == ETH_SS_STATS)
		memcpy(data, ath9k_gstrings_stats,
		       sizeof(ath9k_gstrings_stats));
}

int ath9k_get_et_sset_count(struct ieee80211_hw *hw,
			    struct ieee80211_vif *vif, int sset)
{
	if (sset == ETH_SS_STATS)
		return ATH9K_SSTATS_LEN;
	return 0;
}

#define AWDATA(elem)							\
	do {								\
		data[i++] = sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_BE)].elem; \
		data[i++] = sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_BK)].elem; \
		data[i++] = sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_VI)].elem; \
		data[i++] = sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_VO)].elem; \
	} while (0)

#define AWDATA_RX(elem)						\
	do {							\
		data[i++] = sc->debug.stats.rxstats.elem;	\
	} while (0)

void ath9k_get_et_stats(struct ieee80211_hw *hw,
			struct ieee80211_vif *vif,
			struct ethtool_stats *stats, u64 *data)
{
	struct ath_softc *sc = hw->priv;
	int i = 0;

	data[i++] = ((u64)sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_BE)].tx_pkts_all +
		     sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_BK)].tx_pkts_all +
		     sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_VI)].tx_pkts_all +
		     sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_VO)].tx_pkts_all);
	data[i++] = ((u64)sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_BE)].tx_bytes_all +
		     sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_BK)].tx_bytes_all +
		     sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_VI)].tx_bytes_all +
		     sc->debug.stats.txstats[PR_QNUM(IEEE80211_AC_VO)].tx_bytes_all);
	AWDATA_RX(rx_pkts_all);
	AWDATA_RX(rx_bytes_all);

	AWDATA(tx_pkts_all);
	AWDATA(tx_bytes_all);
	AWDATA(queued);
	AWDATA(completed);
	AWDATA(xretries);
	AWDATA(a_aggr);
	AWDATA(a_queued_hw);
	AWDATA(a_completed);
	AWDATA(a_retries);
	AWDATA(a_xretries);
	AWDATA(fifo_underrun);
	AWDATA(xtxop);
	AWDATA(timer_exp);
	AWDATA(desc_cfg_err);
	AWDATA(data_underrun);
	AWDATA(delim_underrun);

	AWDATA_RX(crc_err);
	AWDATA_RX(decrypt_crc_err);
	AWDATA_RX(phy_err);
	AWDATA_RX(mic_err);
	AWDATA_RX(pre_delim_crc_err);
	AWDATA_RX(post_delim_crc_err);
	AWDATA_RX(decrypt_busy_err);

	AWDATA_RX(phy_err_stats[ATH9K_PHYERR_RADAR]);
	AWDATA_RX(phy_err_stats[ATH9K_PHYERR_OFDM_TIMING]);
	AWDATA_RX(phy_err_stats[ATH9K_PHYERR_CCK_TIMING]);

	WARN_ON(i != ATH9K_SSTATS_LEN);
}

void ath9k_deinit_debug(struct ath_softc *sc)
{
	ath9k_cmn_spectral_deinit_debug(&sc->spec_priv);
}

int ath9k_init_debug(struct ath_hw *ah)
{
	struct ath_common *common = ath9k_hw_common(ah);
	struct ath_softc *sc = (struct ath_softc *) common->priv;

	spin_lock_init(&sc->debug.dcf_timing.lock);
	spin_lock_init(&sc->debug.dcf_measurement.lock);
	INIT_LIST_HEAD(&sc->debug.dcf_measurement.peers);

	sc->debug.debugfs_phy = debugfs_create_dir("ath9k",
						   sc->hw->wiphy->debugfsdir);

#ifdef CONFIG_ATH_DEBUG
	debugfs_create_file("debug", 0600, sc->debug.debugfs_phy,
			    sc, &fops_debug);
#endif

	ath9k_dfs_init_debug(sc);
	ath9k_tx99_init_debug(sc);
	ath9k_cmn_spectral_init_debug(&sc->spec_priv, sc->debug.debugfs_phy);

	debugfs_create_devm_seqfile(sc->dev, "dma", sc->debug.debugfs_phy,
				    read_file_dma);
	debugfs_create_devm_seqfile(sc->dev, "interrupt", sc->debug.debugfs_phy,
				    read_file_interrupt);
	debugfs_create_devm_seqfile(sc->dev, "xmit", sc->debug.debugfs_phy,
				    read_file_xmit);
	debugfs_create_devm_seqfile(sc->dev, "queues", sc->debug.debugfs_phy,
				    read_file_queues);
	debugfs_create_file("edca_ctrl", 0600, sc->debug.debugfs_phy,
			    sc, &fops_edca_ctrl);
	debugfs_create_u32("basic_access_override", 0600,
			   sc->debug.debugfs_phy,
			   &sc->debug.basic_access_override);
	debugfs_create_file("dcf_timing", 0400, sc->debug.debugfs_phy,
			    sc, &fops_dcf_timing);
	debugfs_create_file("dcf_stats", 0600, sc->debug.debugfs_phy,
			    sc, &fops_dcf_stats);
	debugfs_create_devm_seqfile(sc->dev, "misc", sc->debug.debugfs_phy,
				    read_file_misc);
	debugfs_create_devm_seqfile(sc->dev, "reset", sc->debug.debugfs_phy,
				    read_file_reset);

	ath9k_cmn_debug_recv(sc->debug.debugfs_phy, &sc->debug.stats.rxstats);
	ath9k_cmn_debug_phy_err(sc->debug.debugfs_phy, &sc->debug.stats.rxstats);

	debugfs_create_u8("rx_chainmask", 0400, sc->debug.debugfs_phy,
			  &ah->rxchainmask);
	debugfs_create_u8("tx_chainmask", 0400, sc->debug.debugfs_phy,
			  &ah->txchainmask);
	debugfs_create_file("ani", 0600,
			    sc->debug.debugfs_phy, sc, &fops_ani);
	debugfs_create_bool("paprd", 0600, sc->debug.debugfs_phy,
			    &sc->sc_ah->config.enable_paprd);
	debugfs_create_file("regidx", 0600, sc->debug.debugfs_phy,
			    sc, &fops_regidx);
	debugfs_create_file("regval", 0600, sc->debug.debugfs_phy,
			    sc, &fops_regval);
	debugfs_create_bool("ignore_extcca", 0600,
			    sc->debug.debugfs_phy,
			    &ah->config.cwm_ignore_extcca);
	debugfs_create_file("regdump", 0400, sc->debug.debugfs_phy, sc,
			    &fops_regdump);
	debugfs_create_devm_seqfile(sc->dev, "dump_nfcal",
				    sc->debug.debugfs_phy,
				    read_file_dump_nfcal);

	ath9k_cmn_debug_base_eeprom(sc->debug.debugfs_phy, sc->sc_ah);
	ath9k_cmn_debug_modal_eeprom(sc->debug.debugfs_phy, sc->sc_ah);

	debugfs_create_u32("gpio_mask", 0600,
			   sc->debug.debugfs_phy, &sc->sc_ah->gpio_mask);
	debugfs_create_u32("gpio_val", 0600,
			   sc->debug.debugfs_phy, &sc->sc_ah->gpio_val);
	debugfs_create_file("antenna_diversity", 0400,
			    sc->debug.debugfs_phy, sc, &fops_antenna_diversity);
#ifdef CONFIG_ATH9K_BTCOEX_SUPPORT
	debugfs_create_file("bt_ant_diversity", 0600,
			    sc->debug.debugfs_phy, sc, &fops_bt_ant_diversity);
	debugfs_create_file("btcoex", 0400, sc->debug.debugfs_phy, sc,
			    &fops_btcoex);
#endif

#ifdef CONFIG_ATH9K_WOW
	debugfs_create_file("wow", 0600, sc->debug.debugfs_phy, sc, &fops_wow);
#endif

#ifdef CONFIG_ATH9K_DYNACK
	debugfs_create_file("ack_to", 0400, sc->debug.debugfs_phy,
			    sc, &fops_ackto);
#endif
	debugfs_create_file("tpc", 0600, sc->debug.debugfs_phy, sc, &fops_tpc);

	debugfs_create_file("nf_override", 0600,
			    sc->debug.debugfs_phy, sc, &fops_nf_override);

	return 0;
}

# 07：Basic 前序硬件重试段计数修正

## 原因与公式

`ts_longretry` 是最终硬件速率段的重试数，不包含此前已耗尽的段。
固定 legacy 24 Mbps 时，minstrel 仍可能配置多个相同速率的重试段。
06 的 Basic 计数只用了 `ts_longretry + 1`，因此漏计。

07 使用与 ath9k 原生 `ath_tx_count_airtime` / TX status 一致的分段规则：

```text
Basic DATA attempts = ts_longretry + 1
                    + sum(bf->rates[i].count, i < ts_rateindex)
```

仅修改 Basic 的 `data_attempts`、`exchange_attempts`，以及由次数派生的
`modeled_exchange_us`。原始 `long_retries` 仍是 terminal hardware retry
字段的累计，不能再把 `packets + long_retries` 当作完整 Basic 尝试数。
前序段之和及乘法使用 u32/u64，不写回硬件状态或发送描述符。

RTS/CTS 保留 06 的 provisional 公式，本次不声称已经校准 RTS 的失败映射。
HoL 时间戳、FIFO/队列连续性、条件成功样本与捕获窗口锁均未修改。
实际 AP ACK 速率与描述符控制速率的差异也不在此次修复范围内。

统计头保持 `version=2 hol_abi=1`，增加：

```text
basic_retry_abi=2 rts_retry_abi=1
```

用户态解析器保留这些标记；旧数据缺省为 Basic ABI 1，并提示前序段漏计。
同步 worker 在强制 Basic 时必须读到 ABI 2，否则拒绝通过准备检查。

## 测试与部署

补丁 `patches/ath9k-basic-retry-07.patch` 接在 06 后，只需构建并重载 ath9k；
保留已有修改版 mac80211 与 ath9k_hw。不要误用系统默认 modprobe 替换 HoL
依赖模块。源文件备份、构建日志和回退模块都保存在 STA1：

```text
/home/EXPERIMENT_USER/ath9k-kernel/basic-retry-07/original/
/home/EXPERIMENT_USER/ath9k-kernel/basic-retry-07/build.log
/home/EXPERIMENT_USER/ath9k-kernel/basic-retry-07/ath9k-basic-retry.ko
/home/EXPERIMENT_USER/ath9k-kernel/basic-retry-07/rollback-ath9k.ko
```

模块：Ubuntu `5.15.0-139-generic`，srcversion
`C6E744C117E5F6ABADF932F`，SHA256
`270d53c6d955b7451983b6b97f0627b98b596a44e582164fe4313ae8c1a9dee1`。
`activate_basic_retry.py` 先核对原模块、备份回退，再仅重载 ath9k，动态解析
新的 PHY，恢复并核对 24 Mbps、功率、节能、RTS/Basic 开关、硬件 BE EDCA
和关联状态。未覆盖发行版模块、应用自带模块或系统启动配置；**重启或由旧
应用重新加载模块后须重新核对 srcversion 与 basic_retry_abi，不能假定持久生效**。

直接编译实际 `dcf_math.h` 的 C 测试通过 3521 组，包含 Basic 多段/失败、
宽计数与时长乘法，以及 RTS 旧公式不变。源码和类型 shim 在 `tests/`。

STA1 三组 5/30/5 s、单发 5 Mbps/1200 B、24 Mbps、Basic 的现场回归：

| 重复 | 包数 | 前序段次数 | 新驱动累计 | 独立逐包重建 |
|---|---:|---:|---:|---:|
| 1 | 15625 | 364 | 18494 | 18494 |
| 2 | 15625 | 415 | 18784 | 18784 |
| 3 | 15627 | 313 | 18510 | 18510 |

三组都完全闭合，raw long_retries 不变；trace/kprobe 无漏记。空口 AP 与
其余三个 STA 监听核验 Basic 无发送 STA RTS。边界事件只用包数和 raw
long_retries 选择合法时间窗，再独立比较新尝试数，避免以待验计数反向拟合。
本轮不是四 STA 并发，也不把带追踪的 HoL 用于正式曲线。

全部证据在
`archive://2026-10-17_Basic_CW15-1023_retry_fix_regression_01`。
STA2～4 的激活与逐台冒烟结果以该目录 `controller/activation_*.json`、
`controller/deployment_smoke.json` 为准；不能从 STA1 回归推断其他节点状态。

## 尚未解决

修复是计数修正，不改变空口成功率。ACK 已发出但发送 STA 仍重试的原因
尚不明确；RTS 映射、ACK 模型时长还需独立校准。用户目前无法提供安静或
屏蔽对照条件，后续只能明确限定为现有射频环境下的机制对比，不能声称
已经获得纯 collision model；也不能用单 STA 成功率简单除掉干扰影响。

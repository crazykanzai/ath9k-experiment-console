# 实验驱动持久部署

适用于当前四台 STA 的 `5.15.0-139-generic` 内核，AP 不在此次更换范围内。
持久部署不改变 EDCA、速率控制、发射功率、节能或打流参数。

## 加载路径

四个相互匹配的模块安装在：

```text
/lib/modules/5.15.0-139-generic/updates/apstaselect/
  mac80211.ko
  ath9k_hw.ko
  ath9k_common.ko
  ath9k.ko
  bundle.json
  bundle.sha256
```

Ubuntu 当前 depmod 搜索顺序为 `updates ubuntu built-in`。安装工具执行
`depmod -a`，核对 modinfo/modprobe 为四个模块都选择该目录，再更新当前
内核 initramfs。发行版 `kernel/` 下的模块文件不被覆盖；若启动镜像含有
这四个模块，工具会拒绝留下其发行版旧副本。本平台启动镜像不含无线
模块，系统挂载根文件系统后由正常 kmod 路径加载。

`/etc/modules-load.d/apstaselect-ath9k.conf` 声明启动时加载 ath9k，依赖由
kmod 自动解析。`apstaselect-driver-verify.service` 在模块加载后核验磁盘
SHA256、内核版本、全部模块 srcversion 和实时 `hol_abi=1 basic_retry_abi=2`。
它只检查，不自动卸载网卡、不改无线参数；失败记入 systemd/journal。
实验脚本的版本、配置和时间窗门禁仍必须执行。

APSTASELECT 应用的默认目录选择顺序：

1. 显式 `--module-dir` / 环境变量 `ATH9K_MODULE_DIR`（保留人工控制）。
2. 当前内核的 `updates/apstaselect`，要求存在 bundle.json。
3. 没有持久安装时才回退到 `./modules/$(uname -r)`。

有 bundle.sha256 的目录在卸载无线设备前进行完整校验。现有四台 STA
项目自带模块目录内的旧 ath9k.ko 也已备份更新，配套三个模块保持原匹配版本。
应用内核匹配检查仍保留；SHA 不匹配会在改变网卡状态前失败。

## 安装、检查

先准备四个已验证且匹配内核的模块；仅有仓库源码不等于有可加载二进制。
当前精确哈希清单为 `sta/app/config/driver-bundle-5.15.0-139.json`。
升级驱动或内核必须重新构建/验证清单，不能仅修改清单里的哈希绕过测试。

从本仓库 app 目录安装（使用 sudo；首次安装才执行）：

```bash
sudo python3 scripts/driver_persist.py install \
  --bundle ./modules/$(uname -r) \
  --manifest ./config/driver-bundle-5.15.0-139.json \
  --app-dir "$PWD"
```

重复安装遇到已存在的持久目录会拒绝覆盖；先检查现有清单或按备份回退。
安装只变更磁盘与启动配置，不重启、不重载正在运行的模块。

安装后的只读检查：

```bash
sudo python3 /usr/local/libexec/apstaselect-driver-persist.py verify --live
systemctl status apstaselect-driver-verify.service --no-pager
modprobe --show-depends ath9k
```

## 备份与回退

每个节点的精确备份目录在部署报告中，路径形式为：

```text
/var/lib/apstaselect/driver-backups/<kernel>-<timestamp>/record.json
```

备份包括被替换的应用模块、旧启动镜像、已有同名配置（如有）及原权限，
写入前落盘记录。失败自动恢复已记录文件并重新 depmod；回退不删除发行版
模块，也不递归清理目录。程序 C 源文件和二进制的额外备份位于部署 stage
的 backup-app/，从未覆盖其他未提交的源文件。

只有确实需要撤销此次持久部署时才执行：

```bash
sudo python3 /usr/local/libexec/apstaselect-driver-persist.py rollback \
  --backup /var/lib/apstaselect/driver-backups/<实际目录>
```

回退改变后续模块选择，不替换当前内存里的模块；切换运行版本必须另行
在有线控制和无实验流量条件下操作，保持模块集匹配。

## 验收与限制

2026-10-17 部署记录：`打流分析/2026-10-17_driver_persistence_01`。
Python 安装器测试和 C 默认路径测试通过；STA1 完整卸载四模块后，通过
系统 modprobe 重新加载通过；应用从 `/tmp` 使用默认目录执行 reload 也通过。
错误 SHA 在卸载前被拒绝。关联、24 Mbps、14 dBm、原 EDCA/节能状态恢复。

用户明确要求不重启，因此**没有进行整机重启验收**；通过 boot ID 对照确认。
其余三台安装时不卸载正在运行的驱动。模块持久化只覆盖这个内核版本：
内核升级后须重新构建部署，校验失败不能继续正式实验。
驱动持久化不保证 CW/PHY 参数跨重启保持；这些参数仍由实验准备流程设置和读回。

## 已核验重试修订的升级

08 的 RTS 修复也必须更新持久包、bundle.json/checksum 和应用模块槽，
不能只 insmod 新版。安装器现在支持显式
`--replace-manifest-sha256 <现有 bundle.json 的完整 SHA256>`；升级前校验
旧包全部模块、新包全部模块及当前运行 srcversion，不允许在重试升级中
改变另外三个依赖模块。所有替换项及 initrd 仍先备份；失败按记录回退。
候选 RTS ABI 2 未通过核准，当前安装器明确拒绝该 ABI，不能靠清单和
二进制字符串匹配绕过现场校验。

RTS 候选 08 在 STA1 回归时未通过空口核准，已撤回；**四台持久目录仍
为 07，没有进行上述升级**。新增升级保护代码不是候选获准部署的证据。
审计与剩余限制见 `../driver/08-rts-retry.md`；没有授权重启。

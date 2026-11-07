# 实时观察与动态CW接入

此功能是观察通道，不是实验执行器或CW控制器。网页的真实实验启动仍锁定。
0.3.0已做本机离线验证；SSH只读适配器和真实节点时钟/采样开销尚未现场验收。
本次开发没有连接节点、启动iperf、修改节点配置或旧数据。Work设置禁用了本地浏览器自动化，因此本次没有浏览器视觉验收截图；API、数据流与脚本验证不替代现场验收。

## 操作

启动总控后进入 **08 实时观察**。接入一个明确的、由采集器正在追加的本机`.jsonl`文件。

1. 独立采集器/算法先创建本次新的遥测文件。
2. 填观察名称、数据类型和文件绝对路径，点击“接入观察文件”。接入只读文件，不发SSH命令。
3. 查看AP接收速率、区间HoL、ACK代理、实际CWmin四图和逐节点状态表。
4. “暂停画面”只冻结浏览器当前画面；服务器继续跟随文件。“停止跟随文件”只结束观察，不停止业务、不杀外部采集器。
5. 浏览器刷新后，从会话列表重新打开；同一文件不重复建立活动观察。网页关闭也不停止服务器采样缓存。服务器停止后重新启动可读取已登记源；不自动连接节点。
6. 导出的是诊断快照，不是新的正式测量报告。源JSONL由采集器保存，界面不会改写它。

显示最近最多300秒，内存保留最多6000条节点采样和500个事件；这只是观察缓存，长期原始记录仍在源JSONL。源文件初始大小上限128MiB，每次最多读取2MiB，末尾不完整行留待下次读取；格式错误记录到接入错误，不据此改旧实验判定。文件被截断/替换时停止跟随，禁止自动把两次实验拼在一起。

## 不开设备的离线验证

在项目目录运行下面命令，只生成明确标记的合成本地测试数据：

```powershell
python -m labconsole.live_demo --output "D:\演练数据\offline_telemetry.jsonl" --seconds 60
```

网页选择“离线测试数据”，接入该文件。每10秒模拟切换一个CW，验证虚线、区间断点和图表刷新。所有数值均为合成测试，不能作为实验结果、真实动态CW效果或算法性能。

## 只读SSH适配器（已实现，现场待验收）

控制机的可选适配器需要Paramiko；实验核心和历史回放仍只依赖Python标准库。适配器由用户单独运行，网页不会自动启动它。
先导出平台档案，确认接口提示或让探针发现唯一无线接口；有多接口且无法唯一识别时拒绝。SSH主机公钥必须已经核验，默认严格拒绝陌生主机，不自动接受。
在获准进行现场只读验收之后，示例命令为：

```powershell
python -m labconsole.observe --platform "D:\实验配置\platform.json" --output "D:\观察数据\session01.jsonl" --known-hosts "D:\实验配置\known_hosts" --sudo --duration 1800
```

- 只连AP及STA1～4，MON15不参与、不切换monitor。管理地址/用户名从档案读；接口、PHY和MAC在现场发现，不猜业务地址，不开iperf。
- 每节点建立一个SSH读流；探针以`python3 -c`在内存执行，不部署文件、服务或驱动。读`iw dev info/link/station dump`、`dcf_stats`、`edca_ctrl`。
- CW来自已登记驱动`edca_ctrl`的`hw_cwmin/hw_cwmax/hw_aifs`和`status_name=verified`，不是缓存参数或请求值。读取使用**已存在**的共同锁，0.25秒争用即跳过；不创建另一个锁、不写regidx/regval、不读clear-on-read MIB、不回退到旧无锁路径。缺少硬件读回字段保留错误和NA。
- 需要root读取时由用户显式加`--sudo`，只在内存输入密码；不存密码、不改sudo配置。SSH凭据按credential_ref分组提示，空密码使用已有密钥/agent。当前sudo模式要求五节点相同sudo密码，否则应分别扩展适配后验收，不能默认为可用。
- 开始及约每10秒只读测时；相对控制机偏差+往返不确定度超过50ms即停止观察，不校时。区间展示还计入探针读耗时；时钟/读取跨度包络超50ms时断线NA。不是连续硬件时间测量，不触发任何业务T0。
- `--interval`默认1秒，允许1～10秒；区间计算仅接受连续间隔≤5秒，更慢会保持NA而不推断。`--duration`默认1800秒、上限3600秒。接口/权限/通道异常停止本次适配，不自动重试其他配置。
- Ctrl+C只结束本次只读SSH通道，远端有界探针会退出，不停止正在运行的实验业务；不得通过该程序停止旧监控。

注意：不能仅凭本机测试宣称这条现场采集路径通过。真实实验前须有界检查权限、锁、驱动字段、跨节点时间、采样额外开销和既有采集兼容；不为显示曲线而放宽任何正式门禁。

## 指标与变化标记

|图/字段|计算及边界|
|---|---|
|AP接收速率|AP逐STA station dump接收字节差分/实际采样间隔；MAC映射来自各STA实际接口。不是正式AP UDP载荷/iperf应用吞吐，可能含该计数范围的其他流量|
|区间HoL|`Δhol_service_sum_us / Δhol_service_samples`；按TX完成回调落入相邻采样间隔，含该成功路径服务时长。不是窗口内完整服务包集合，不与正式HoL混用|
|区间ACK代理|Basic ABI2、模式不变、尝试有效且区间无未核准终态失败时`Δexchange_successes/Δexchange_attempts`；RTS/失败/无尝试/计数回退/停窗/代次变化时NA，不删除失败后硬算|
|实际CW|直接硬件读回。发现相邻读回值变化时画竖虚线，真实生效时刻仅知在两次读回之间；同一间隔横跨CW变化的HoL/概率不分配给任何一档|
|过期/中断|超过5秒无新样本显示过期，表中当前值NA；曲线保留历史，后续间隔>5秒重新建基线。不持有最后值伪装当前值|

观察器**不**调用驱动start/stop/reset。旧采集流程尚未启动驱动窗口时HoL/概率会NA；显示NA正确，不通过网页私自清零开窗来补图。快速CW来回切换可能发生在两次读回之间，不能仅凭采样声称捕获了所有变化；需要算法事件旁证。

## 未来算法的接入协议

每行一个UTF-8 JSON对象、以换行结束。`schema`固定`apstaselect.telemetry.v1`；同角色`seq`严格递增，采样时间为控制机墙钟域的整数纳秒`sample_ns`，须另给`clock_uncertainty_ms`。算法可复用自己的数据获取机制，不必启动额外SSH观察器；按协议发布即可。

STA样本示例（数值仅说明字段）：

```json
{"schema":"apstaselect.telemetry.v1","kind":"sample","role":"STA1","seq":1,"sample_ns":1794000000000000000,"clock_uncertainty_ms":2,"source":"算法现有遥测","data":{"cw":{"cwmin":31,"cwmax":255,"aifsn":3,"basis":"hardware_readback"},"dcf":{"generation":1,"running":true,"mode":"basic","basic_retry_abi":2,"hol_samples":100,"hol_sum_us":80000,"attempts":120,"successes":100,"packets":100,"failures":0}}}
```

AP样本使用`role:"AP"`、`data:{"rx_bytes":{"STA1":1000000,"STA2":500000}}`。字段来源必须与实际匹配；若是别的吞吐源，需要增加独立适配，不将应用字节与station dump计数拼接。

算法事件示例（不执行写入，仅记录）：

```json
{"schema":"apstaselect.telemetry.v1","kind":"event","role":"algorithm","seq":1,"sample_ns":1794000000000000000,"clock_uncertainty_ms":2,"source":"algorithm-v1","target":"STA1","event":"cw_requested","requested":{"cwmin":63,"cwmax":511},"message":"算法决策；尚未证明生效"}
```

事件类型：`cw_requested`、`cw_applied`、`cw_verified`、`note`；`requested`、`readback`分别记录请求和读回，不把前者冒充后者。算法写入CW的控制权限、事务、失败回退和验证是后续独立工作，本版没有这些动作。建议未来记录decision_id及请求/调用/读回时刻，当前观察通道只提供记录与展示。

运行中改变CW将形成动态控制诊断；不能再把整段结果当作原固定CW方案的一次正式重复。未来需单独预登记动态协议、切换稳定期和分析规则，不追改历史固定方案或有效性。图上变化前后对应关系是时间关联，不自动证明因果改善。

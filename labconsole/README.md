# 控制机总控

从仓库根目录运行`python start_console.py --open`，Windows也可双击start_console.bat。
Python3.10以上，网页/模拟/回放/本机JSONL跟随只需标准库。数据根可用`--workspace`指定。

页面：开始使用、平台配置、实验方案、环境检查、任务运行、结果与历史、历史回放、实时观察。
正式节点执行仍锁定；角色管理、初始化向导、任意节点登记待实现。当前平台校验为固定六角色。

平台配置不存密码/密钥正文。离线检查的“通过”仅指本机，节点项未现场检查。
模拟采用压缩时间，正式有效始终0、数值NA；停止/故障演练不会触发业务。

历史回放：选择四批中任一份发布用样例，读取后看批次整窗结果，或选择单窗播放真实归档吞吐桶。
HoL和概率只有整窗值，时序只在测量结束处显示结果点。失败、NA和缺失保持。图例可勾选，大字模式便于演示。
这些样例已去掉个人目录，不带原始pcap，不用于重新证明采集有效性。

离线报告导入只支持明确批次metrics的单STA rows，不递归找窗口，不自动合并批次；导入创建新版本，不改原始文件。

实时观察见[LIVE_TELEMETRY](LIVE_TELEMETRY.md)。独立SSH观察器需要可选Paramiko及单独现场授权，目前未现场验收。
画面暂停不停止后台文件跟随或业务；源JSONL不是正式实验报告。驱动窗口未运行时HoL显示NA，不自动开启窗口。

默认工作区：Windows的LOCALAPPDATA/APSTASELECT/LabConsole，其他系统为~/.local/share/apstaselect-console。
历史兼容名称保留；可用命令行换目录。迁移工作区前先等待任务结束，不在SQLite写入时只拷数据库文件。
后台模拟独立于浏览器；Ctrl+C只关网页服务，先在任务页停止并等待终态，再关闭服务。

## 测试

`python -m unittest labconsole.tests.test_console labconsole.tests.test_replay labconsole.tests.test_live -v`

本机模拟/HTTP及数据流测试不是硬件验证。此版本不承诺其他OS的现场采集能力、图形视觉验收或驱动永久稳定。

# ath9k Experiment Console

由控制机统一配置、观察和分析ath9k无线实验的平台。源自APSTASELECT，保留必要节点程序和测量方法，去掉每台节点上的旧STA/AP选择网页与旧上位机界面。

**当前版本：0.3.0迁移版。控制机总控可离线运行；正式实验执行、角色管理和新节点初始化尚未接入网页。** 不会因为打开页面而连接节点、配置无线或启动iperf。

## 快速开始

要求Python 3.10以上。离线总控、历史演示与JSONL观察使用标准库，无需安装Streamlit。

```bash
python start_console.py --open
```

Windows也可双击`start_console.bat`。默认地址为`http://127.0.0.1:8790`。端口被占用时指定`--port 8791`。

自选数据目录：

```bash
python start_console.py --workspace ./workspace --open
```

控制台保持运行。默认工作区在用户本机应用数据目录，不在Git仓库中；切换电脑无需复制旧虚拟环境。

## 现在能做什么

|功能|状态|
|---|---|
|平台档案、固定方案预览、模拟执行与停止|可用；模拟不产生测量数据|
|历史回放|可用；含四批85窗发布用快照，路径已去除，数值/失败/NA保留|
|实时观察|可用；只读跟随本机JSONL，显示区间速率/HoL/ACK代理/CW变化|
|独立SSH只读采集适配器|已编写、未现场验收；须显式启动，不由网页自动调用|
|正式实验网页执行|锁定；保留参考采集/分析引擎，尚未完成配置化接入|
|新增任意节点、AP/STA角色分配、驱动初始化向导|已设计，待实现；当前配置仍为六角色、最多四业务STA|
|实验中自动修改CW/智能算法|未实现控制动作；预留遥测事件协议|

角色选择是日常操作；新机器首次驱动初始化是独立流程。普通STA关联无需本项目补丁，CW/HoL实验能力需要匹配的定制驱动。实验节点仍须先具备Linux、有线网络、SSH与必要权限。

## 使用入口

- **历史回放**：左侧07，选实验→读取→批次总览或单窗播放。HoL/概率只有整窗结果时只显示结果点，不伪造逐秒曲线。
- **实时观察**：左侧08，接入独立采集器或未来算法正在追加的本机JSONL。暂停画面不停止业务；跨CW/计数重置/中断区间保持NA。详见[LIVE_TELEMETRY](labconsole/LIVE_TELEMETRY.md)。
- **离线演练**：选择实验模板、生成封存计划、启动模拟；正式有效数始终0。
- **节点源码与实验方法**：见[节点部署](docs/DEPLOYMENT.md)、[参考引擎](experiments/README.md)、[指标定义](docs/METRICS.md)。不要直接运行旧批次或把模拟通过当现场通过。

## 项目结构

```text
labconsole/       控制机网页、离线任务、报告、回放、只读观察
experiments/      已解耦的参考采集/分析模块及相关离线测试
sta/app/          Linux节点C命令程序、驱动安装/校验支持
sta/tools/        DCF解析（不含旧页面服务）
sta/driver/       匹配内核源码片段、补丁、许可证与C测试
configs/          无凭据的配置示例
docs/             架构、部署、指标、迁移和状态
tools/            发布前离线检查
```

旧Web页面/启动器、一次性构建/修复/回退和历史批次入口没有整体搬入。必要共用函数已提取，原项目与实验数据保持原处。迁移不是对既有驱动和理论方法进行重新硬件认证。

## 开发与验收

```bash
python -m unittest labconsole.tests.test_console labconsole.tests.test_replay labconsole.tests.test_live -v
python tools/check_repository.py
```

参考分析测试需可选依赖：`python -m pip install -r requirements-analysis.txt`，随后`python -m unittest discover -s experiments -p "test_*.py" -v`。Linux专属flock/模块/C构建检查与Windows离线测试分开记录。CI不连接节点、不sudo安装模块、不打流。

详细边界见[STATUS](docs/STATUS.md)。更多设备、角色管理和驱动向导见[ROADMAP](docs/ROADMAP.md)。正式实验必须遵守[EXPERIMENT_RULES](EXPERIMENT_RULES.md)。

## 来源与许可证

源项目：[tangshier47-collab/APSTASELECT](https://github.com/tangshier47-collab/APSTASELECT)。本次包含旧commit之后的工作区改动，逐文件来源和变换记录见[迁移说明](docs/MIGRATION.md)。新仓库不改旧远端或实验有效性。

本项目**没有为所有原创代码统一指定MIT/Apache等许可证**，公开可见不等于已授予任意再分发许可。保留原项目的[原创代码授权状态](LICENSES/ORIGINAL_APPLICATION_NOTICE.md)；Linux导入部分的SPDX、版权和[COPYING](sta/driver/COPYING)继续适用。详见[LICENSE](LICENSE.md)。

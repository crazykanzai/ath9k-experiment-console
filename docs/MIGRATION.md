# 迁移记录

来源为tangshier47-collab/APSTASELECT及其未提交工作区，不是只导出旧commit。旧完整实验日志、原始数据、未提交改动、旧远端保持；新目录为独立clone。
逐文件来源/源SHA/变换/目标见migration_manifest.json；来源工作区版本不等同于旧Git基准提交。
source_sha256按来源工作区原始字节；published_sha256按Git暂存区规范化内容（LF）计算，避免跨平台换行差异。GitHub克隆的Windows启动BAT可能按属性签出CRLF。上游内核源码和补丁已有空白保持，不对第三方导入作无关格式改写。

## 排除

旧STA/AP选择页、STA上位机、AP Streamlit页面与根launcher.py没有迁入。
不带AP页面专属依赖；不复制.venv、tmp、验收日志/机器截图、数据库、原始PCAP/追踪/账本、编译模块或实际凭据。
单段/RTS候选部署、一次性修复/回退、旧批次runner/finalizer没有整体迁入。

## 适配

- build_single_series_sta1中的历史CANONICAL常量移到driver_profile.py，没有构建单段的入口。
- calibrate_collision_model和power_step_check中被核心引用的共用函数提取到reference_helpers.py；不保留整套旧功率/校准扫描。
- 固定四STA参考引擎连接器默认拒绝网络连接；不因新仓库而自动获得真实执行能力。其余配置化和任意节点扩展按ROADMAP推进。
- 四份回放JSON的个人绝对路径替换为archive://标识，来源哈希键改为编号。所有测量数值、日期、完整失败、原NA、原判定保持；这不是对原始数据原地脱敏。
- 测试中的动态旧模块引用随共用函数迁移更新；微秒时间戳夹具数字不属于密码，保留原值。
- 新README/规则/依赖/CI明确区分当前功能与待实现项，保留版权、第三方SPDX和原授权状态。

## 验收

离线脚本检查、总控/参考分析/DCF/驱动安装器测试分开记录；Windows跳过Linux flock，Linux构建由CI单独执行。
发布检查是有限文本模式和文件范围审查，不宣称能证明所有内容绝无敏感信息。
本轮不连接实验节点、不打流、不构建部署驱动。公开回放只含源汇总和时间桶，不含原始证据。

## 日常入口

新同学从根README启动控制机总控；不要从experiments挑一个旧脚本尝试“一键实验”。
需要更多节点、角色组网或新机器初始化时先实现对应适配和资源验收，再按实验授权执行。

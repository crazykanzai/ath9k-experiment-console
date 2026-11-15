# 演示核心

运行仓库根目录start_console.bat，或 `python start_console.py`。仅环境准备和真实CW演示。默认不连接节点。详情见根目录README和docs/LIVE_CW_DEMO.md。

核心模块：server（本机HTTP与请求认证）、storage（平台/占用）、workspace_lock（单实例）、preparation/prepare_node（准备/恢复）、clock_probe/time_reference/ntp_relay（时间）、real_demo/demo_node/demo_analysis（采集与调节）、cw_policy（策略）。

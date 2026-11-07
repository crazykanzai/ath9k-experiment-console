# 参考采集与分析引擎

保留来源平台中绝对T0调度、节点worker、完整服务包账本、空口解析、概率及理论计算的必要模块和离线测试。
这是**固定四STA、尚未完全配置化的参考引擎**，没有自动接入网页正式启动。

- 默认禁止sync_run.Remote建立连接。不得为了跑测试而解除限制。
- `sync_run.py`/`sync_node.py`/`passive_listener*.py`是待配置化接入的控制/worker参考；不能用新README当作运行授权。
- `analyze_*.py`、`cohort_trace.py`、`bianchi_dai_*.py`等提供分析函数；原输入显式指定，不能递归混入历史数据。
- 旧单段构建、功率扫描、历史诊断campaign没有迁入。必要函数提取到reference_helpers.py，CANONICAL模块位置独立为driver_profile.py；没有单段构建入口。
- 尚有旧四节点、远端路径和内核假设；/home/EXPERIMENT_USER是占位符，不可直接部署。以后由平台档案接入，不让用户改一堆源码。

离线测试需安装requirements-analysis.txt后执行：

```bash
python -m unittest discover -s experiments -p 'test_*.py' -v
```

本目录测试不得访问真实节点。Linux flock等专属测试在Windows跳过，不把跳过称为通过。

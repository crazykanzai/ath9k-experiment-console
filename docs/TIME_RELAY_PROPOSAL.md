# UDP123冲突及AP有线时间中继

2026-11-11：Windows W32Time正在0.0.0.0/:: UDP123监听，系统NtpServer提供器未启用。准备会话cfd5640d5796490db5e9053e8a0a13fe在启动本机参考时WinError10013，尚未修改节点时间覆盖、未打流；状态failed_clean保留。

不停止W32Time、不修改Windows注册表、防火墙或服务启动方式。systemd249的timesyncd源码使用固定123服务端口，不能简单把NTP地址改成`:1123`。

建议显式选择“经AP有线中继”：控制机参考仅绑定管理IP的UDP1123；AP以本次SSH内存进程占用其空闲UDP123，只接受登记六节点的请求，转发到控制机1123并原样返回响应。六节点本次/run时间覆盖NTP改为AP有线地址；授时戳仍由控制机参考生成，AP不生成/拟合时钟。持续50ms门禁、90/300秒验收保持。

中继有短租约，SSH断开或控制心跳超时自行退出。结束先恢复六节点原时间配置，再停止本次中继和控制机参考；不安装服务、不改持久配置、不停止原有服务。AP端口不空闲/源码版本或中继就绪/时钟验收失败均停止，不抢占、不自动换其他路径。

用户已授权临时中继。现场已完成90秒稳定等待与300秒持时验收，并核对清理。网页04可显式选择中继，原直连失败会话保留。

依据：systemd v249 src/timesync/timesyncd-manager.c 的 manager_connect 使用resolve_getaddrinfo(...,"123",...)。
https://github.com/systemd/systemd/blob/v249/src/timesync/timesyncd-manager.c

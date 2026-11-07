# 控制机与节点部署边界

## 控制机

Python3.10以上，运行start_console.py。核心不需要Streamlit、Linux网卡或旧节点Web服务。
仅在需要独立只读SSH观察时安装requirements-observer.txt；参考分析需requirements-analysis.txt。
实际平台文件与known_hosts保存在仓库外，不提交密码、私钥和原始数据。

## Linux节点

基础条件：Linux、有线网络可达、SSH启用、有效登录/sudo权限。插交换机本身不会给未配置的裸系统建立SSH。
候选环境来自5.15.0-139-generic与AR928x平台，其他内核/硬件须另查，不自动升级或换驱动。

节点C程序：在目标Linux上`make -C sta/app`；用于角色、关联、hostapd/DHCP、驱动/EDCA等命令，不依赖旧GUI。
驱动源码片段不是完整内核树，不能直接在Windows编译或把某台.ko无条件复制到另一内核。
HoL需要同源匹配的mac80211、ath9k_hw、ath9k_common、ath9k四模块；检查vermagic、ABI、源版本和SHA。

生产补丁顺序见sta/driver/patches/series；Basic计数ABI2与HoL ABI1保留。RTS概率及Basic终态失败分母限制不变。
**FIFO20候选不是当前持久07资源包的同义词。** 07持久资源清单不代表FIFO候选已进入开机加载；重启后仍需核对。FIFO候选构建证据留在来源项目，不把本仓库源码自动当成已发布FIFO二进制。

驱动持久安装/校验/回退代码保留在sta/app/scripts/driver_persist.py；相关模块目录须提供真实验证过的资源，仓库没有内核二进制。
该安装器不自动重启或重载；是否加载和中断无线要按有线管理下的独立授权进行。
匹配驱动源码有许可证约束，保留SPDX和COPYING。参见[驱动说明](../sta/driver/README.md)。

## 尚未提供

网页角色切换、初始化向导和远端正式执行尚未完成；不能将此文当作逐条自动执行新节点的脚本。
旧部署指南仅编译ath9k EDCA且含过时环境操作，本仓库不复制执行。不要为新项目默认替换软件源、降级Python或禁用安全设置。

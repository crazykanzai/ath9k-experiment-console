# Linux节点命令程序

在目标Linux运行`make -C sta/app`，生成ath9k-edca。不是Windows GUI，不需要旧节点网页。
保留AP/STA角色、关联、hostapd/DHCP、EDCA及驱动管理命令，具体参数见程序帮助与源码main.c。
构建/安装不等于授权执行无线变更；只能在有线可控、无冲突任务且有对应授权时操作。

config为模板，接口和业务地址必须发现后填写，不将模板当现场事实。
modules只放匹配当前内核的四模块资源，源仓库不含.ko。scripts/driver_persist.py
校验、备份、安装和回退支持保留；需要真实资源清单，不能改哈希强行通过。
driver reload会中断无线，和mode sta/ap是不同动作；日常角色切换不应每次重编驱动。
控制机网页的初始化/角色编排尚未接入。详见../../docs/DEPLOYMENT.md。

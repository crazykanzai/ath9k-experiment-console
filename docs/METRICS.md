# 数据与指标

- 正式吞吐：AP接收公共绝对窗口的业务载荷，不以STA发送设置速率代替。
- 正式HoL：成功条件、窗口内完整服务包、主机服务起点代理；失败/跨界/起点未知独立保留。
- 成功概率：逐次交换ACK代理、CTS代理、最终MAC交付率、应用丢失分别报告。未核准分母NA；包含失败包的辅助近似单独标明。
- 重复单位：预登记独立窗口；全部点、有效数、窗口级t95%CI。缺失概率不能用已知子集代表整组。
- 历史回放：源汇总/归档时间桶的展示，不重新证明有效性，不对整窗HoL/概率插值成逐秒曲线。
- 实时HoL：累计量差分按TX完成回调归属相邻采样区间；不等同正式完整服务包集合。
- 实时AP速率：station dump字节增量/实际间隔，不冒充iperf应用载荷吞吐。
- CW标记：两次硬件读回之间发现变化；不是精确写入时刻。跨CW区间不归给任一档，算法请求不能代替读回。
- 模型：物理slot与sigma_eff、Bianchi有限n与Dai–Sun原近似、经验外推分开。Bianchi自身nL/S；HoL与饱和吞吐并非独立验证。

回放快照schema：apstaselect.history-replay.v1；遥测schema：apstaselect.telemetry.v1。完整遥测字段见labconsole/LIVE_TELEMETRY.md。

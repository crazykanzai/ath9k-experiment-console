# Current architecture

```text
CLI
 ├─ driver_ctrl ── modinfo/rmmod/insmod ── ath9k modules
 ├─ wifi_mode   ── NetworkManager/iw/ip ── AP/STA state
 ├─ hostapd_ctrl ──────────────────────── AP process
 └─ edca_ctrl ── debugfs ── ath9k TXQ ── hardware register
```

The application owns orchestration and verification. The driver owns parameter
validation close to the hardware, queue updates and kernel-side concurrency.

## EDCA state layers

Future experiments must distinguish:

1. the policy/requested EDCA parameters;
2. the ath9k software queue properties;
3. the hardware register values;
4. for AP experiments, the WMM parameters advertised to stations.

An experiment is not considered reproducible unless the relevant layers are
recorded together with the kernel, module, hardware and traffic metadata.

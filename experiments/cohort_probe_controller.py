"""Generate DWARF-verified, read-only cohort probe specs on the controller."""
from retry_audit import offsets,event_definition,input_event_definition,HW_MODULE
from cohort_trace import definitions


def prepare(remote,module):
    import shlex
    iface=shlex.quote(getattr(remote,'iface','wlp2s0'))
    pci=remote.run('cat /sys/class/net/'+iface+'/device/vendor /sys/class/net/'+iface+'/device/device')
    identity=pci['stdout'].splitlines()
    # AR5416-format descriptors only; do not read AR9003 descriptors using
    # an AR9002 layout just because both families share the ath9k module.
    if pci['code'] or len(identity)!=2 or identity[0]!='0x168c' or identity[1] not in {
        '0x0023','0x0024','0x0027','0x0029','0x002a','0x002b','0x002c','0x002d','0x002e'}:
        raise RuntimeError('wireless PCI device not verified for AR9002 descriptor observer: '+repr(pci))
    o,raw=offsets(remote,module)
    extra,_=offsets(remote,module,{'bf_desc':'(long)&((struct ath_buf *)0)->bf_desc'})
    hw={**{'ds_ctl%d'%i:'(long)&((struct ar5416_desc *)0)->'+('ds_ctl%d'%i if i<2 else 'u.tx.ctl%d'%i) for i in range(12)},
        **{'ds_status%d'%i:'(long)&((struct ar5416_desc *)0)->u.tx.status%d'%i for i in range(10)}}
    h,hwraw=offsets(remote,HW_MODULE,hw,'ath9k_hw');o.update(extra);o.update(h)
    events,proof=definitions(remote,module,o);o['cohort']=True
    events.update(txdone=event_definition(o),txinput=input_event_definition(o))
    return {'events':events,'proof':proof,'offsets':o,'DWARF':raw,'hardware_DWARF':hwraw,'wireless_PCI_identity':pci,
        'expected_srcversion':remote.run('cat /sys/module/ath9k/srcversion')['stdout'].strip()}

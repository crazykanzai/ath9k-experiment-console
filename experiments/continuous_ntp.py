"""Explicit continuous-NTP profile: observe source, never step/recenter clocks."""
import ipaddress,re
def source_address(value):
    address=ipaddress.IPv4Address(value)
    if not address.is_private:raise ValueError('experiment NTP reference must be a private wired IPv4 address')
    return str(address)
def check(remote,source):
    source=source_address(source)
    receipt=remote.run('timedatectl show; timedatectl timesync-status')
    output=receipt.get('stdout','')
    matched=re.search(r'^\s*Server:\s+(\S+)',output,re.M)
    return {'source':source,'receipt':receipt,'pass':receipt['code']==0 and 'NTPSynchronized=yes' in output.splitlines()
            and bool(matched and matched.group(1)==source),
            'policy':'continuous daemon frequency/phase slew; no controller manual clock steps/recentering; hard 50ms gate unchanged'}
def require(remote,source):
    result=check(remote,source)
    if not result['pass']:raise RuntimeError(remote.name+' continuous NTP source/status gate: '+repr(result))
    return result

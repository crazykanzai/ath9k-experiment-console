"""Explicit expected radio state; validation only, never changes hardware."""
import re
STAS=('STA1','STA2','STA3','STA4')
FREQUENCIES={1:2412,6:2437,11:2462}
def validate_radio_plan(value):
    if not isinstance(value,dict) or set(value)!={'channel','frequency_MHz','AP_txpower_dBm','STA_txpower_dBm'}:
        raise ValueError('radio plan keys differ from registered schema')
    channel=value['channel']
    if type(channel)!=int or channel not in FREQUENCIES or type(value['frequency_MHz'])!=int or value['frequency_MHz']!=FREQUENCIES[channel]:
        raise ValueError('unregistered/mismatched radio channel')
    if type(value['AP_txpower_dBm'])!=int or value['AP_txpower_dBm'] not in (5,16):raise ValueError('unregistered AP power')
    powers=value['STA_txpower_dBm']
    if not isinstance(powers,dict) or set(powers)!=set(STAS) or any(type(v)!=int or v not in (5,14) for v in powers.values()):raise ValueError('incomplete/unregistered per-STA power')
    return value
def power_matches(text,expected):
    match=re.search(r'\btxpower\s+(-?\d+(?:\.\d+)?)\s+dBm\b',text)
    return bool(match and float(match.group(1))==expected)
def matches(text,radio,name):
    validate_radio_plan(radio)
    expected=radio['AP_txpower_dBm'] if name=='AP' else radio['STA_txpower_dBm'][name]
    return (power_matches(text,expected) and
            f"channel {radio['channel']} ({radio['frequency_MHz']} MHz)" in text and '20 MHz (no HT)' in text)

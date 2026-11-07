# EDCA debugfs interface (current prototype)

Status: experimental and not versioned yet.

## Location

```text
/sys/kernel/debug/ieee80211/<phy>/ath9k/edca_ctrl
```

The application resolves `<phy>` through the selected network interface; it
must not assume `phy0`.

## Read format

One line is returned for each available mac80211 access category:

```text
AC=VO qnum=0 aifs=2 cwmin=3 cwmax=7
AC=VI qnum=1 aifs=2 cwmin=7 cwmax=15
AC=BE qnum=2 aifs=3 cwmin=15 cwmax=1023
AC=BK qnum=3 aifs=7 cwmin=15 cwmax=1023
```

The queue numbers above are examples only. Consumers must parse the runtime
`qnum` value.

## Write format

```text
<VO|VI|BE|BK> <AIFS> <CWmin> <CWmax>
```

Example:

```text
BE 4 15 63
```

Current validation:

- AIFS: 1 through 15;
- CWmin/CWmax: one of 1, 3, 7, 15, 31, 63, 127, 255, 511, 1023;
- CWmin must not exceed CWmax;
- extra non-whitespace input is rejected.

## Semantics and limitations

- A successful write changes the local ath9k transmit queue.
- Existing TXOP, ready time and unrelated queue properties are preserved.
- The operation is one-shot; a later mac80211 `conf_tx()` or hardware reset
  may overwrite it.
- The interface does not update the WMM parameters advertised by an AP.
- `debugfs` is an experimental control mechanism, not a stable public ABI.

The next incompatible format change must introduce an explicit capability and
ABI version before the application starts relying on it.

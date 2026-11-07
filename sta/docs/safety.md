# Safety notes

The project performs privileged operations that can disconnect networking or
leave a partially loaded module stack after failure.

Before a driver load, unload or reload:

- use a separate management interface using a different driver;
- verify the target interface, PHY, PCI device and bound kernel driver;
- enumerate every device bound to ath9k because module removal is global;
- validate all staged modules before changing network state;
- keep the distribution module path and a local recovery procedure available;
- save logs from the console rather than relying on the interface being changed.

Never run privileged hardware tests automatically for an untrusted pull
request. Unit and fixture tests may run in shared CI; module load/unload tests
belong on a dedicated, physically recoverable runner.

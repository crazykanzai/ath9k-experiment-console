# Development workflow

## The iteration unit

An iteration should solve one observable problem. Write down:

```text
Problem:
Current evidence:
Expected behavior:
Out of scope:
Acceptance tests:
Risk and recovery:
```

For a small private project this can be a GitHub issue. It prevents an
iteration from becoming a collection of unrelated edits.

## Solo workflow

1. Update `main` and create a topic branch.
2. Reproduce the problem and save a small fixture or command transcript.
3. Add or update a test where practical.
4. Implement the smallest coherent change.
5. Review `git diff` before staging.
6. Run `make check`, the relevant unit tests and explicitly selected hardware
   tests.
7. Commit logical steps instead of one end-of-week snapshot.
8. Merge only after the documented acceptance criteria pass.

Do not create a new directory for every iteration. Use branches and tags:

```text
main                         Current integrated state
feat/... or fix/...          Temporary iteration branch
v0.1.0, v0.2.0              Reproducible released states
```

## Driver change workflow

1. Identify the exact baseline and call path being changed.
2. Keep the diff minimal and follow Linux coding style.
3. Define debugfs input, output, error and lifecycle semantics before changing
   the application parser.
4. Test invalid input, concurrent access, interface reconfiguration, reset and
   module unload as applicable.
5. Regenerate the driver patch and verify it applies to a clean matching tree.
6. Build modules, record `vermagic` and hashes, then run a recoverable hardware
   test.

## Application change workflow

1. Separate pure parsing/state logic from privileged side effects.
2. Add a fixture or fake-backend test for each new failure path.
3. Treat every multi-step operation as preconditions, actions,
   compensations and postconditions.
4. Preserve the original error and verify rollback instead of printing success
   unconditionally.
5. If the driver interface changes, update compatibility tests and interface
   documentation in the same branch.

## Releases

Create a release only when its stated scope is reproducible. Before tagging:

- working tree is clean;
- application checks pass;
- driver patch applies to a clean baseline;
- supported kernel/hardware and known limitations are documented;
- `CHANGELOG.md` is updated;
- generated binaries and secrets are absent from Git.

Use development and pre-release versions before claiming stability:

```text
0.1.0-dev       Active local development
v0.1.0-alpha.1  First reproducible private/public preview
v0.1.0          Supported control-path milestone
```

# Contributor instructions

Read EXPERIMENT_RULES.md before planning/changing any measurement or node action.
This repository's default workflow is offline. Do not run the reference acquisition engine,
connect to nodes, change radio/driver/time settings, or start traffic without explicit authorization.
Do not enable the fixed-four reference controller to make tests pass.
Preserve original experiment inputs, failures, NA and validity. Never recursively infer input windows.
Inspect git status before editing. No credentials, private keys, local workspaces or raw traces in commits.
Keep physical node identity separate from radio resources, assigned roles, and active sender sets.
Adding a node in the design does not imply arbitrary-n or multi-AP hardware acceptance.
Record status accurately: offline tests are not visual QA or live hardware validation.
Run offline tests appropriate to modified components. Linux-only skips must be reported as skips.

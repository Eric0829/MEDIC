# Contributing to MEDIC

Thank you for helping improve MEDIC. The project is an early alpha, so clear evidence and small reversible changes are more valuable than broad rewrites.

## Before opening an issue

- Reproduce the behavior with a minimal command or fixture.
- Include the target type, Python version, operating system, and the relevant trace or error summary.
- Remove secrets, tokens, personal data, and generated runtime logs before posting.
- Do not include customer infrastructure details in a public issue.

## Local checks

From the repository root, run the smallest relevant checks first:

```powershell
python medic_control.py --role-contract
python medic_control.py --diagnostic-smoke
python medic_control.py --benchmark-suite
```

If you change a connector, add or update a focused seed case under `benchmarks/`. Generated output directories are ignored and should not be committed.

## Pull requests

Please explain:

1. What behavior changed.
2. Why the change is safe.
3. How it was tested.
4. Whether the role contract, approval path, audit trail, or public documentation changed.

Keep automatic execution disabled by default. Changes that can restart, patch, roll back, quarantine, or alter a target need an explicit policy and approval-path test.

## Scope

MEDIC is not a medical device and does not provide clinical advice. Contributions should stay within software operations, agent safety, observability, policy, approval, and auditability.

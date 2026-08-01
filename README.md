# MEDIC

MEDIC is an observe-only safety control layer for monitoring, diagnosing, approving, and auditing actions across services and AI agents.

> MEDIC is a software-operations project, not a clinical or medical device.

## What it does

- Collects service and agent health signals.
- Produces diagnosis and treatment candidates.
- Routes high-risk actions through policy review, second opinion, approval, and audit records.
- Tracks incidents and provides concise operator briefs.

## Safety model

MEDIC defaults to `observe_only`. It does not automatically patch code, restart services, alter configuration, or remove data. Such actions require explicit approval and are recorded for review.

## Status

Early alpha. Use in controlled environments and validate against your own systems before relying on it operationally.

## Quick start

```powershell
python medic_control.py --daily-check
python medic_control.py --operator-brief
python medic_control.py --benchmark-suite
```

See [the documentation](docs/README.md) for the architecture, operations guide, benchmarks, and current limitations.

## License

MIT.

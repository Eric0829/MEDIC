# MEDIC

[![MEDIC checks](https://github.com/Eric0829/MEDIC/actions/workflows/ci.yml/badge.svg)](https://github.com/Eric0829/MEDIC/actions/workflows/ci.yml)

### A safety control plane for systems that can take action

MEDIC is an **observe-only safety control layer** for services and AI agents. It turns health signals into explainable diagnosis and treatment candidates, then keeps risky actions behind policy review, second opinion, human approval, and an audit trail.

> MEDIC is a software-operations project, not a clinical or medical device.

## Why it exists

Monitoring tools can tell an operator that something is wrong. MEDIC focuses on the operational decision boundary that follows: **what is safe to do, who approved it, and what evidence explains the decision?**

That makes MEDIC useful as an external control plane around an AI agent, router, Python service, or local workload. The monitored system can keep doing its job while MEDIC observes it from the outside and records the reasoning path.

```text
target -> vitals -> diagnosis -> prescription
       -> second opinion -> policy -> approval -> controlled runner
       -> audit log -> trace -> incident queue -> operator brief
```

## Safety model

The default contract is:

```text
observe -> judge -> approve
```

`observe_only` is the default and automatic execution is disabled. MEDIC does not silently patch code, restart processes, change configuration, delete evidence, or quarantine a target. High-risk actions must pass through the approval and audit path.

## What is included

- Observe-only health checks for the MEDIC process, local systems, and Python services.
- Diagnostic and prescription pipelines with trace IDs.
- Policy checks, second-opinion review, approval queues, and controlled execution guards.
- Incident triage, operator briefs, audit logs, and storage-health checks.
- Connectors for selected local, remote, Python-service, database, Ollama, model, and workload targets.
- Seed benchmark cases for internal regression, external-style evaluation, adversarial policy checks, and local smoke tests.

Some adapters are experimental. Validate the adapter and permissions for your own environment before using MEDIC in operations.

## Verification snapshot

The current repository includes a staged benchmark harness. The latest local snapshot matched 36/36 internal diagnostic cases, 12/12 external-style seed cases, and 10/10 adversarial policy cases. The real local-service stage matched 1/2 cases, and the long-running stage was a short two-cycle probe. These are useful regression signals, not independent certification or a production guarantee.

## A good fit when

- an AI agent or automation can make changes, but every risky change needs a clear approval boundary;
- operators need diagnosis, proposed treatment, and execution evidence in one trace;
- a team wants a small, inspectable safety layer that can sit outside an existing service;
- contributors want to add target adapters and benchmark cases without changing the safety contract.

MEDIC is not a replacement for metrics, logs, tracing, incident paging, or a security program. It is the control and evidence layer that connects those signals to an accountable action path.

## Quick start

Requirements: Python 3.10+ and Windows PowerShell for the helper scripts. The core smoke commands use the Python standard library.

From this repository root:

```powershell
python medic_control.py --role-contract
python medic_control.py --version
python medic_control.py --diagnostic-smoke
python medic_control.py --daily-check
python medic_control.py --benchmark-suite
```

See [`docs/DEMO.md`](docs/DEMO.md) for a two-command walkthrough of observation, approval queuing, and pre-execution blocking. For the repeated observer, see [`scripts/README.md`](scripts/README.md). Runtime evidence is written to ignored directories such as `control_state/`, `observe_runs/`, and `benchmark_runs/`; do not commit those generated files.

## Documentation

- [`docs/MEDIC_OVERVIEW.md`](docs/MEDIC_OVERVIEW.md) — architecture and safety contract.
- [`docs/DEMO.md`](docs/DEMO.md) — a short local walkthrough with representative output.
- [`docs/STATUS.md`](docs/STATUS.md) — current maturity and known limitations.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — daily checks and daemon operations.
- [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) — benchmark stages and interpretation.
- [`docs/INCIDENT_GUIDE.md`](docs/INCIDENT_GUIDE.md) — incident triage and resolution.
- [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) — how to make a safe contribution.
- [`docs/SECURITY.md`](docs/SECURITY.md) — responsible vulnerability reporting.

## Current status

MEDIC is an early alpha with a working observe-only control path. The repository contains internal and seed benchmark harnesses, not an independent certification or a guarantee of production reliability. Long-running soak tests, independent blind cases, installer hardening, and broader target coverage are still in progress.

## Contributing

Bug reports, adapter improvements, benchmark cases, and documentation fixes are welcome. Please keep changes observable, reversible, and covered by a focused test or reproducible example. Read [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) before opening an issue or pull request.

## License

MIT. See [`LICENSE`](LICENSE).

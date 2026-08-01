# MEDIC in two short runs

This is a small, local demonstration of the control path. It does not touch a real customer service and it does not enable automatic treatment execution.

Run these commands from the repository root:

```powershell
python medic_control.py --diagnostic-smoke --json
python medic_control.py --second-opinion-smoke --json
```

## 1. Observe and explain

The diagnostic smoke run collects vitals, explains the diagnosis, proposes a treatment, and sends the proposal through the gateway.

Representative result from a local run:

```text
status: observed
severity: LOW
root cause: no_issue_detected
confidence: 0.96
gateway: observed
policy: allow (observe_only)
treatment execution: not called
```

The important behavior is the last line: the runner records the decision and evidence without applying a treatment.

## 2. Queue or block a risky change

The second smoke run sends two high-risk patch examples through the same policy boundary:

| Example | Second opinion | Policy result | Execution |
| --- | --- | --- | --- |
| Safe patch shape | `APPROVE` | queued for human approval | not executed |
| Patch containing `eval(` | `REJECT` | blocked | not executed |

This is a policy-path demonstration, not a claim that one pattern catches every dangerous change. Review the source, add your own cases, and run the benchmark suite against the systems you control.

## What to look for

- One trace ID connects the diagnosis, policy decision, and audit event.
- High-risk actions do not jump directly to the target.
- A rejected second opinion blocks the prescription before execution.
- Generated state and evidence stay in ignored runtime directories.

For the full operating model, see [`MEDIC_OVERVIEW.md`](MEDIC_OVERVIEW.md) and [`BENCHMARKS.md`](BENCHMARKS.md).

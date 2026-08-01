# Security policy

MEDIC can observe systems and represent potentially destructive treatment candidates. Treat deployments, credentials, audit records, and target configuration as sensitive.

## Reporting a vulnerability

Please do not publish an exploitable vulnerability, credential, customer log, or private infrastructure detail in a public issue. Contact the maintainer privately through the GitHub profile and include:

- affected commit or version;
- a short impact description;
- safe reproduction steps;
- a suggested mitigation, if known.

Do not test MEDIC or Codex Security against systems or repositories that you do not own or have explicit permission to review.

## Current security boundaries

- `observe_only` is the default execution mode.
- Risky actions require policy review and approval.
- Direct treatment bypasses are intended to be blocked by the control layer.
- Runtime evidence and local state must remain outside public commits.

These are design goals and tested behaviors, not a security certification. Review the code and deployment permissions for your environment before use.

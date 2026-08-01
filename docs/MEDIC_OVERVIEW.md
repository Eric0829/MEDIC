# MEDIC overview

MEDIC은 외부에서 다른 서비스나 에이전트 상태를 지켜보고, 문제가 보이면 원인과 조치 후보를 판단한 뒤, 승인과 감사 기록을 남기는 감시/판정 에이전트입니다.

쉽게 말하면 지금의 MEDIC은 "수리 기사"라기보다 "검사관 + 기록관 + 승인 창구"에 가깝습니다.

## MEDIC이 하는 일

- 대상의 상태를 관찰합니다.
- 증상을 모아 진단합니다.
- 원인 후보와 처방 후보를 만듭니다.
- 위험한 처방은 `ControlGateway`, `PolicyEngine`, `ApprovalQueue`, `SecondOpinionGate`를 거치게 합니다.
- 모든 흐름을 `AuditLog`와 `PipelineTrace`에 남깁니다.
- 반복 감시 결과를 `IncidentQueue`로 올리고 운영자가 볼 수 있게 `OperatorBrief`로 정리합니다.

## MEDIC이 아직 하지 않는 일

- 마음대로 코드를 고치지 않습니다.
- 마음대로 프로세스를 재시작하지 않습니다.
- evidence log를 임의로 삭제하거나 축소하지 않습니다.
- 승인 없이 위험한 처방을 실행하지 않습니다.
- 백신처럼 운영체제 전체를 실시간 차단하지 않습니다.

## 기본 실행 철학

현재 계약은 아래와 같습니다.

```text
observe -> judge -> approve
```

즉 MEDIC은 먼저 봅니다. 그다음 판단합니다. 실행이 필요한 경우에는 승인 기록을 남겨야 합니다.

## 주요 파이프라인

```text
Target
-> collect_vitals
-> diagnose
-> prescribe
-> second_opinion
-> control_gateway
-> policy_review
-> approval_queue if needed
-> controlled_runner
-> audit_log
-> pipeline_trace
-> incident_queue
-> operator_brief
```

각 단계가 따로 있는 이유는 단순합니다. 나중에 "왜 이런 판단이 나왔지?", "어디서 막혔지?", "누가 승인했지?", "실제로 실행됐나?"를 추적할 수 있어야 하기 때문입니다.

## 핵심 구성 요소

| 구성 | 역할 |
| --- | --- |
| `ControlGateway` | 처방이 바로 실행되지 않도록 통제합니다. |
| `PolicyEngine` | 처방의 위험도와 허용 여부를 판단합니다. |
| `ApprovalQueue` | 승인이 필요한 조치를 대기열에 둡니다. |
| `AuditLog` | 판단, 승인, 실행 결과를 기록합니다. |
| `PipelineTrace` | 한 케이스의 흐름을 trace id로 연결합니다. |
| `SecondOpinionGate` | 위험한 처방에 2차 검토를 붙입니다. |
| `IncidentQueue` | 반복 감시에서 나온 경고를 사건으로 관리합니다. |
| `OperatorBrief` | 지금 사람이 무엇을 봐야 하는지 한 화면으로 요약합니다. |
| `ObserveDaemon` | 감시를 반복 실행합니다. |
| `RoleContract` | MEDIC이 해도 되는 일과 안 되는 일을 선언합니다. |

## 안전 계약

현재 role contract의 핵심은 다음입니다.

```text
default execution: observe_only
auto execute: false
approval required:
  patch_code
  restart
  rollback
  config_change
  quarantine
  fine_tune_trigger
  manual_intervention
```

이 말은 MEDIC이 경고를 보고 `restart`를 추천하더라도, 지금 단계에서는 자동으로 재시작하지 않는다는 뜻입니다.

## 왜 독립 에이전트로 두는가

MEDIC을 다른 에이전트 내부에 넣으면 감시 대상과 감시자가 섞일 수 있습니다. 그러면 자기 판단을 자기 자신이 통과시키는 문제가 생깁니다.

그래서 현재 방향은 MEDIC을 외부 제어 레이어로 두는 것입니다.

```text
다른 에이전트 / 서비스
        |
        v
   MEDIC observes
        |
        v
  policy + approval + audit
```

이 구조가 완벽하다는 뜻은 아닙니다. 다만 첫 단계로는 더 안전합니다.

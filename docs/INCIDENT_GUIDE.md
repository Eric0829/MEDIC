# MEDIC incident guide

Incident는 MEDIC이 감시 중 발견한 "사람이 봐야 할 사건"입니다.

중요한 점은 incident가 생겼다고 해서 MEDIC이 자동으로 고친다는 뜻은 아닙니다. 현재 MEDIC은 observe-only가 기본이므로, incident는 판단과 기록의 시작점입니다.

## incident 상태

| 상태 | 뜻 |
| --- | --- |
| `open` | 아직 사람이 처리하지 않은 열린 사건입니다. |
| `acknowledged` | 사람이 봤고, 일단 지켜보는 상태입니다. |
| `resolved` | 확인 후 처리 완료로 닫은 상태입니다. |
| `rejected` | 오탐 또는 처리 불필요로 닫은 상태입니다. |

## priority 의미

| priority | 의미 |
| --- | --- |
| `P1` | 가장 먼저 봐야 합니다. 보통 critical이거나 오래 방치된 stale incident입니다. |
| `P2` | 주의가 필요합니다. warning급 active incident가 여기에 들어갑니다. |
| `P3` | 낮은 우선순위입니다. 참고 또는 낮은 위험 항목입니다. |

## 기본 확인 순서

1. 전체 요약을 봅니다.

```powershell
python MEDIC\medic_control.py --operator-brief
```

2. incident triage를 봅니다.

```powershell
python MEDIC\medic_control.py --incident-triage
```

3. incident 상세를 봅니다.

```powershell
python MEDIC\medic_control.py --incident-show INCIDENT_ID
```

4. daemon 상태를 봅니다.

```powershell
python MEDIC\medic_control.py --observe-daemon-status
```

5. 필요한 경우 한 번 다시 관찰합니다.

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1 -MaxCycles 1 -Interval 0
```

## 처리 기준

### acknowledge

아직 원인을 확정하지 못했지만 사람이 봤고 지켜볼 때 사용합니다.

```powershell
python MEDIC\medic_control.py --incident-ack INCIDENT_ID --incident-by human --incident-note "reviewed, watching"
```

사용 예:

- 일시적인 CPU 상승처럼 보입니다.
- 현재는 정상으로 보이지만 조금 더 봐야 합니다.
- 외부 서비스 상태 확인이 더 필요합니다.

### resolve

현재 상태가 정상이고, 해당 incident가 더 이상 조치 필요 없다고 확인했을 때 사용합니다.

```powershell
python MEDIC\medic_control.py --incident-resolve INCIDENT_ID --incident-by human --incident-note "rechecked, current status healthy"
```

사용 예:

- 새 관찰 결과가 healthy입니다.
- 원인이 일시적이었고 반복되지 않았습니다.
- 별도 조치를 완료했습니다.

### reject

오탐 또는 MEDIC이 볼 대상이 아닌 것으로 판단할 때 사용합니다.

```powershell
python MEDIC\medic_control.py --incident-reject INCIDENT_ID --incident-by human --incident-note "false alarm"
```

사용 예:

- config가 잘못되어 잘못 잡힌 경고입니다.
- 일부러 테스트한 경고입니다.
- 관찰 대상이 아닌 프로세스를 보고 있었습니다.

## 현재 열린 incident 예시

최근 정리한 stale incident:

```text
incident id: 1ec814c5-7d88-4cac-9b89-9d40a833a7c8
target: local-system
severity: warning
status: resolved
message: local-system observe status=healthy, patient_status=attention
suggested action: review_attention_target
```

이 케이스는 `local-system`에서 한 번 `cpu_overload`로 진단된 신호입니다. MEDIC은 `restart` 처방 후보를 만들었지만 observe-only 모드라 실행하지 않았습니다.

비슷한 incident가 다시 열리면 운영자가 할 수 있는 현실적인 순서:

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1 -MaxCycles 1 -Interval 0
python MEDIC\medic_control.py --operator-brief
python MEDIC\medic_control.py --incident-show 1ec814c5-7d88-4cac-9b89-9d40a833a7c8
```

새 관찰도 healthy라면 resolve를 고려합니다.

```powershell
python MEDIC\medic_control.py --incident-resolve 1ec814c5-7d88-4cac-9b89-9d40a833a7c8 --incident-by human --incident-note "rechecked, current status healthy"
```

다만 이 결정은 운영 판단입니다. 자동으로 닫지 않는 것이 현재 MEDIC의 안전한 기본값입니다.

## 기억할 점

- incident를 닫는 것은 "이 기록을 지운다"가 아닙니다. 상태를 `resolved` 또는 `rejected`로 바꾸고, audit/trace에 판단을 남기는 것입니다.
- stale incident는 오래 열린 사건이라는 뜻입니다. 반드시 더 위험하다는 뜻은 아니지만, 오래 방치되었으므로 우선 확인해야 합니다.
- 경고가 한 번 사라졌다고 무조건 삭제하지 않습니다. 확인하고 상태 전이를 남깁니다.

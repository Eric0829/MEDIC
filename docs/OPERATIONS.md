# MEDIC operations

이 문서는 MEDIC을 평소에 어떻게 확인하면 되는지 설명합니다.

## 매번 먼저 볼 것

가장 먼저 이 명령을 봅니다.

```powershell
python MEDIC\medic_control.py --daily-check
```

이 명령은 매일 볼 항목만 간단히 묶어 보여줍니다.

조금 더 자세히 보려면 이 명령을 봅니다.

```powershell
python MEDIC\medic_control.py --operator-brief
```

`operator-brief`는 MEDIC의 대시보드 역할입니다.

주요 의미:

| 항목 | 뜻 |
| --- | --- |
| `status: clear` | 지금 당장 볼 큰 항목이 없습니다. |
| `status: attention_required` | 사람이 확인해야 할 항목이 있습니다. |
| `self-control: healthy` | MEDIC 자체 점검이 통과했습니다. |
| `self-control: warning` | MEDIC 자체 또는 운영 상태에 주의가 필요합니다. |
| `daemon heartbeat: stale=False` | 감시 daemon이 최근에 움직였습니다. |
| `daemon heartbeat: stale=True` | 감시 daemon이 멈췄거나 오래 갱신되지 않았습니다. |
| `daemon_process: running` | 상시 감시 daemon 프로세스가 실제로 떠 있습니다. |
| `daemon_process: missing` | heartbeat 기록은 있어도 현재 daemon 프로세스는 없습니다. |
| `approvals: pending=0` | 승인 대기 조치가 없습니다. |
| `incidents: active=0` | 열린 incident가 없습니다. |

## 기본 점검 명령

```powershell
python MEDIC\medic_control.py --daily-check
python MEDIC\medic_control.py --operator-brief
python MEDIC\medic_control.py --incident-triage
python MEDIC\medic_control.py --observe-daemon-status
python MEDIC\medic_control.py --causal-report
python MEDIC\medic_control.py --storage-health
python MEDIC\medic_control.py --benchmark-suite
```

만약 `python` 명령이 인식되지 않으면, `daily-check`가 출력하는 전체 Python 경로 명령을 사용합니다. 예시는 아래 형태입니다.

```powershell
C:\Users\shini\AppData\Local\Programs\Python\Python313\python.exe MEDIC\medic_control.py --daily-check
```

## 벤치마크 평가

1~5단계 평가를 한 번에 보려면:

```powershell
.\MEDIC\scripts\run_benchmark_suite.ps1
```

직접 CLI로 실행하려면:

```powershell
python MEDIC\medic_control.py --benchmark-suite
```

기본값의 5단계는 짧은 probe입니다. 장시간 운영 검증은 아래처럼 시간을 늘려 실행합니다.

```powershell
python MEDIC\medic_control.py --benchmark-suite --benchmark-observe-cycles 360 --benchmark-observe-interval 60
```

## 감시 daemon 실행

한 번만 실행해서 상태를 새로고침하려면:

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1 -MaxCycles 1 -Interval 0
```

현재 창에서 계속 실행하려면:

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1
```

숨김 백그라운드로 시작하려면:

```powershell
.\MEDIC\scripts\start_observe_daemon_hidden.ps1
```

이 스크립트는 이미 daemon이 떠 있으면 새로 띄우지 않습니다.

현재 사용자 로그인 시 자동 시작을 미리 보려면:

```powershell
.\MEDIC\scripts\install_user_startup.ps1
```

실제 등록:

```powershell
.\MEDIC\scripts\install_user_startup.ps1 -Apply
```

짧은 Windows 래퍼로 실행하려면:

```powershell
.\MEDIC\scripts\run_observe_daemon.cmd
```

daemon 상태 확인:

```powershell
python MEDIC\medic_control.py --observe-daemon-status
```

짧은 soak 검증:

```powershell
.\MEDIC\scripts\run_observe_soak.ps1 -Cycles 3 -Interval 1
```

같은 검증을 CLI로 직접 실행하려면:

```powershell
C:\Users\shini\AppData\Local\Programs\Python\Python313\python.exe MEDIC\medic_control.py --observe-soak --observe-soak-cycles 3 --observe-soak-interval 1 --observe-daemon-config MEDIC\config\observe_daemon.example.json
```

## Windows 예약 작업

예약 작업 상태 확인:

```powershell
.\MEDIC\scripts\show_observe_task.ps1
```

등록 미리보기:

```powershell
.\MEDIC\scripts\install_observe_daemon_task.ps1
```

실제 등록:

```powershell
.\MEDIC\scripts\install_observe_daemon_task.ps1 -Apply
```

주의: Windows 권한이 부족하면 `Access is denied`가 날 수 있습니다. 이 경우 MEDIC 코드가 고장난 것이 아니라, 현재 사용자 권한으로 Task Scheduler 등록이 막힌 것입니다.

## 열린 incident 처리

열린 incident 목록:

```powershell
python MEDIC\medic_control.py --incident-list
```

상세 보기:

```powershell
python MEDIC\medic_control.py --incident-show INCIDENT_ID
```

사람이 봤고 아직 보류하려면:

```powershell
python MEDIC\medic_control.py --incident-ack INCIDENT_ID --incident-by human --incident-note "reviewed, watching"
```

현재 상태가 정상으로 돌아왔고 더 처리할 일이 없으면:

```powershell
python MEDIC\medic_control.py --incident-resolve INCIDENT_ID --incident-by human --incident-note "rechecked, current status healthy"
```

오탐 또는 처리 불필요로 판단하면:

```powershell
python MEDIC\medic_control.py --incident-reject INCIDENT_ID --incident-by human --incident-note "false alarm"
```

## 절대 가볍게 하지 말 것

- evidence log 삭제
- evidence log 축소
- 승인 없이 실행 모드 변경
- `auto_execute_enabled` 켜기
- incident를 보지 않고 일괄 resolve 처리
- stale heartbeat를 무시하고 운영 중이라고 판단하기

## 운영자가 기억할 한 문장

MEDIC은 지금 "고치는 도구"보다 "보는 도구"입니다. 좋은 감시자는 조용히 보고, 이유를 남기고, 위험한 조치 앞에서는 멈춥니다.

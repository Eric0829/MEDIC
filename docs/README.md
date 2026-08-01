# MEDIC docs

이 폴더는 MEDIC을 나중에 다시 볼 때 길을 잃지 않기 위한 운영 문서입니다.

읽는 순서는 아래가 좋습니다.

1. [MEDIC_OVERVIEW.md](MEDIC_OVERVIEW.md)
   - MEDIC이 무엇인지, 무엇이 아닌지, 전체 흐름이 어떻게 생겼는지 설명합니다.
2. [STATUS.md](STATUS.md)
   - 현재 완성도, 코드 규모, 남은 위험 요소를 적어 둡니다.
3. [OPERATIONS.md](OPERATIONS.md)
   - 평소에 무엇을 확인하고 어떤 명령을 쓰면 되는지 설명합니다.
4. [BENCHMARKS.md](BENCHMARKS.md)
   - 내부/외부/공격/실제 대상/장시간 평가를 어떻게 돌리는지 설명합니다.
5. [INCIDENT_GUIDE.md](INCIDENT_GUIDE.md)
   - 경고가 생겼을 때 열람, 확인, 보류, 종료하는 기준을 설명합니다.

빠른 확인 명령:

```powershell
python MEDIC\medic_control.py --daily-check
python MEDIC\medic_control.py --operator-brief
python MEDIC\medic_control.py --incident-triage
python MEDIC\medic_control.py --observe-daemon-status
python MEDIC\medic_control.py --causal-report
python MEDIC\medic_control.py --benchmark-suite
```

중요한 원칙:

- MEDIC은 현재 자동 수정기가 아니라 외부 감시/판정/승인 레이어입니다.
- `observe_only`가 기본값입니다.
- evidence log는 임의로 삭제하거나 줄이지 않습니다.
- 위험한 조치에는 승인 큐와 감사 기록이 먼저 필요합니다.

# MEDIC status

이 문서는 현재 MEDIC의 완성도와 남은 빈칸을 기록합니다.

Snapshot 기준: 2026-05-26 00:31 UTC

## 규모

git에 들어간 MEDIC 본체:

```text
files: 81
lines: about 18,400
python files: 58
python lines: about 16,100
size: about 820 KB
```

MEDIC 폴더 전체:

```text
files: about 1,080 including runtime artifacts
lines: about 360,000+ excluding bytecode caches
size: about 62 MB
```

전체 크기가 큰 이유는 코드가 그렇게 큰 것이 아니라, 감시 기록, 하네스 결과, audit, trace 같은 runtime evidence가 쌓여 있기 때문입니다. 이 기록들은 원인 추적과 운영 검증에 필요하므로 임의로 삭제하거나 줄이지 않습니다.

## 현재 운영 상태

```text
daily-check: attention_required
operator brief: attention_required
self-control: healthy
role contract: healthy
default execution: observe_only
auto execute: false
approval pending: 0
incident active: 0
incident stale active: 0
daemon latest status: healthy
daemon heartbeat: fresh after benchmark observe probe
daemon process: missing in current Codex session
benchmark suite: healthy, 5/5 stages
external seed benchmark: 12/12
adversarial seed benchmark: 10/10
real local target smoke: 2/2
observe soak: healthy, latest 1/1 short probe
storage health: healthy
causal report: healthy
```

최근 정리한 주요 항목:

```text
incident id: 1ec814c5-7d88-4cac-9b89-9d40a833a7c8
status: resolved
target: local-system
message: local-system observe status=healthy, patient_status=attention
decision note: refreshed observe daemon; latest cycle healthy with zero alerts
```

해석:

- MEDIC 자체 role contract는 건강합니다.
- storage도 깨진 줄 없이 건강합니다.
- causal report도 현재 기준 healthy입니다.
- observe daemon heartbeat는 benchmark observe probe 실행으로 새로 갱신되었습니다.
- 하지만 현재 Codex 세션에서는 상시 daemon 프로세스가 떠 있지 않아 `daily-check`와 `operator-brief`는 `attention_required`를 표시합니다.
- 과거에 열린 `local-system` stale incident는 최신 cycle이 healthy이고 alerts가 0이라 `resolved`로 전환했습니다.
- 2026-05-08에는 짧은 observe soak 3 cycles를 실행했고 모두 healthy였습니다.
- `python` 명령이 PATH에서 안 잡히는 환경을 대비해 daily-check 안내 명령과 daemon scripts가 전체 Python 경로를 더 잘 다루도록 보강했습니다.
- Windows 예약 작업 등록은 `CimException: Access denied`로 막혔고, 설치 스크립트는 이제 이 실패를 구조화해서 보고합니다.
- 2026-05-11에는 daily-check에 실제 daemon process 감지를 추가했고, 숨김 시작 및 현재 사용자 Startup 등록 스크립트를 추가했습니다.
- 2026-05-11에는 observe soak runner를 추가했고, latest 2/2 cycles가 healthy였습니다.
- 2026-05-26에는 `--benchmark-suite`를 추가했고, 짧은 설정에서 5/5 stages가 healthy였습니다.
- 외부/공격 benchmark는 `MEDIC/benchmarks/*.jsonl` 파일 기반 seed 평가입니다. 진짜 제3자 blind benchmark는 아직 별도 추가가 필요합니다.
- 현재 Codex 세션에서는 숨김 프로세스 시작과 Startup 폴더 쓰기가 Windows 권한으로 막힙니다. 일반 Windows PowerShell 세션에서 같은 명령을 재시도할 수 있습니다.

## 현재 수준

```text
장난감 수준: 아님
연구용/실험용 MVP: 넘었음
운영 감시 에이전트 alpha: 가까움
프로덕션 완성품: 아직 아님
```

더 짧게 말하면:

```text
외부 감시/판정/승인 에이전트 alpha
```

## 강점

- `ControlGateway`가 존재합니다.
- `ApprovalQueue`가 존재합니다.
- `AuditLog`와 `PipelineTrace`가 존재합니다.
- `IncidentQueue`가 observe daemon과 연결되어 있습니다.
- `OperatorBrief`가 지금 볼 항목을 한 화면으로 요약합니다.
- role contract가 MEDIC의 행동 한계를 선언합니다.
- 기본 실행 모드가 `observe_only`입니다.
- causal report 기준 harness/trace 신호는 healthy입니다.
- 내부/외부 seed/공격 seed/실제 로컬 서비스/observe soak를 묶은 benchmark suite가 있습니다.

## 아직 부족한 점

1. 상시 실행 안정성

   Windows 예약 작업 등록이 권한 문제로 막힐 수 있습니다. 예약 작업이 없으면 재부팅 또는 로그아웃 뒤 daemon이 자동으로 살아나지 않습니다.

2. incident 운영 절차

   incident를 열고 보여주는 기능은 있지만, 사람이 어떤 기준으로 `ack`, `resolve`, `reject`할지 습관이 아직 자리잡지 않았습니다.

3. 오래 쌓인 audit/trace 해석

   기록은 많지만, 기록을 읽어 한눈에 흐름을 보는 UI나 요약 도구는 아직 제한적입니다.

4. 실제 치료 실행 통제

   현재 방향은 안전합니다. 다만 프로덕션 자동 수리로 가려면 실행 전후 검증, rollback plan, approval policy 강화가 더 필요합니다.

5. 운영 서비스화

   지금은 Python/PowerShell 스크립트 중심입니다. 완전한 Windows service, installer, health dashboard 단계는 아닙니다.

## 프로덕션으로 가기 전 필요한 것

- Windows 예약 작업 또는 서비스 등록이 실제로 성공해야 합니다.
- daemon heartbeat stale을 자동 감지하고 복구 안내해야 합니다.
- active/stale incident 운영 절차가 문서와 명령으로 더 단단해야 합니다.
- 승인 큐 처리 흐름을 더 쉽게 볼 수 있어야 합니다.
- evidence log 보존 정책과 archive 정책이 명확해야 합니다.
- benchmark 5단계는 아직 짧은 probe만 통과했습니다. 더 긴 soak test가 필요합니다.
- 실제 대상 서비스별 관찰 config가 늘어나야 합니다.
- 제3자 또는 사용자 제공 blind benchmark case가 더 필요합니다.

## 현재 다음 작업 후보

우선순위 순서:

1. Windows 예약 작업 등록 권한 문제를 해결합니다.
2. 현재 사용자 Startup 등록을 일반 Windows PowerShell 세션에서 완료합니다.
3. 사용자/제3자 blind benchmark case를 `MEDIC/benchmarks`에 추가합니다.
4. 장기 실행 soak를 더 길게 돌리고 daemon 재시작 검증을 추가합니다.
5. approval/incident 운영 화면을 더 쉽게 만듭니다.

완료된 최근 작업:

```text
stale incident resolved
daemon heartbeat refreshed
daily-check command added
operator brief now reports daemon process attention
python path fallback added
short observe soak passed, 3 cycles
scheduled task failure reporting improved
daemon process detection added
hidden daemon starter added
current-user Startup installer added
observe soak runner added
observe soak passed, 2 cycles
benchmark suite added
benchmark suite passed, 5/5 stages
external seed benchmark passed, 12/12
adversarial seed benchmark passed, 10/10
real local target benchmark passed, 2/2
```

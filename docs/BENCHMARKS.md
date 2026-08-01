# MEDIC benchmarks

이 문서는 MEDIC의 능력을 어떻게 검증하는지 설명합니다.

## 한 번에 실행

```powershell
.\MEDIC\scripts\run_benchmark_suite.ps1
```

직접 CLI로 실행하려면:

```powershell
python MEDIC\medic_control.py --benchmark-suite
```

## 단계

```text
1. internal_evaluation
   MEDIC 내부 하네스와 control soak를 다시 돌립니다.

2. external_case_evaluation
   MEDIC/benchmarks/external_cases.jsonl 파일의 외부 seed 케이스를 풉니다.

3. adversarial_evaluation
   MEDIC/benchmarks/adversarial_cases.jsonl 파일의 우회/위험 payload를 풉니다.

4. real_local_target_evaluation
   실제 로컬 HTTP /health 서비스를 띄우고 관찰합니다.

5. long_running_operations_evaluation
   observe daemon soak를 반복 실행합니다.
```

## 중요한 해석

기본 실행의 5단계는 짧은 probe입니다. 며칠 동안 버틴다는 증거가 아니라,
장시간 soak를 돌릴 수 있는 경로가 연결됐는지 보는 짧은 확인입니다.

진짜 장시간 검증은 예를 들어 아래처럼 실행합니다.

```powershell
python MEDIC\medic_control.py --benchmark-suite --benchmark-observe-cycles 360 --benchmark-observe-interval 60
```

위 설정은 약 6시간짜리 observe soak입니다.

## 케이스 파일

외부 진단 케이스:

```text
MEDIC/benchmarks/external_cases.jsonl
```

공격/우회 케이스:

```text
MEDIC/benchmarks/adversarial_cases.jsonl
```

이 파일들은 MEDIC 코드와 분리된 local seed benchmark입니다. 독립 기관이나
다른 사람이 만든 blind test는 아직 별도로 추가해야 합니다.

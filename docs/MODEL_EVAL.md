# Model Evaluation

Use the regression harness to track narration quality/safety drift.

## Harness

- script: `scripts/model_eval_regression.py`
- baseline cases: `scripts/model_eval_cases.json`

## Run

Static candidate checks:

```powershell
.\.venv\Scripts\python.exe .\scripts\model_eval_regression.py
```

Live LLM generation checks:

```powershell
.\.venv\Scripts\python.exe .\scripts\model_eval_regression.py --live-llm
```

## What It Verifies

- input alignment quality
- low-information response detection
- player-character autonomy violations

## CI Integration

Add this script as a CI step and fail build on unacceptable regression count.

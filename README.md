# workstack-runner

**Verified parallel execution for planning tasks — powered by [Daytona](https://daytona.io) sandboxes.**

A planning task says *what* you want. An LLM will happily produce five different
answers for it, all confident, some wrong. `workstack-runner` closes that gap: it
fans out N candidate solutions into N isolated Daytona sandboxes, runs a real
executable oracle in each, and returns **only the candidates that actually passed**.

No candidate is trusted because it looks right. It is trusted because it ran.

---

## Why a sandbox per candidate

Candidate code is untrusted by definition — it was just generated. Running five
variants on one machine means they fight over the same filesystem, the same port,
the same installed packages, and one bad `rm` takes the run with it.

Daytona gives a clean, disposable, ~2 second sandbox per candidate. So the natural
unit becomes *one candidate, one machine*, and the whole set verifies in the time
the slowest single candidate takes.

Measured on 2026-09-19: **7 candidates, 3.6 s wall clock vs 18.5 s sequential (5.2x).**

## The control group is the point

Every run includes a candidate known to be wrong. If it passes, the oracle is not
discriminating and the entire green result means nothing.

This is not hypothetical — it happened during development. A deliberately broken
`median()` that returned the **mean** passed the original test suite, because every
test input happened to have `mean == median`:

```
median([3,1,2])     median 2     mean 2      <- cannot tell them apart
median([4,1,3,2])   median 2.5   mean 2.5    <- cannot tell them apart
```

Adding `median([1, 2, 100])` (median 2, mean 34.33) made it fail correctly. So
`02_generate.py` now always appends the original buggy source as `original-buggy`,
and a run where it survives is a **failed run**, not a clean one.

---

## Quick start

```bash
pip install daytona
export DAYTONA_API_KEY=...            # https://app.daytona.io
export DAYTONA_API_URL=https://app.daytona.io/api
export GROQ_API_KEY=...               # or any OpenAI-compatible provider

python 00_smoke.py                    # round trip: create -> run -> serve -> delete
python 02_generate.py 6               # generate 6 candidate patches
python 01_fanout.py                   # verify all of them in parallel
```

Expected tail of a healthy run:

```
survivors 6/7   -- original-buggy FAILED (control held)
wall 3.6s  sequential 18.5s  speedup 5.2x
```

## Files

| File | Role |
|---|---|
| `00_smoke.py` | Round trip proof: create -> `code_run` -> shell -> public preview URL -> delete |
| `02_generate.py` | Generates N candidates in parallel at varied temperature; always appends the control |
| `01_fanout.py` | Fan-out verification. Reads `candidates.json` if present, else built-in candidates |
| `llm.py` | Zero-dependency provider shim (stdlib `urllib` only) |
| `NOTES.md` | Every trap hit during development, with the measured numbers (Korean) |

## Provider swap

`llm.py` speaks the OpenAI-compatible `/chat/completions` shape, so switching
inference providers is environment variables — no code change:

```bash
LLM_PROVIDER=nosana
NOSANA_BASE_URL=...
NOSANA_API_KEY=...
NOSANA_MODEL=...
```

Groq, Nosana and OpenAI are wired in `PROVIDERS`.

## Operational notes

- **Concurrency ceiling.** Default sandboxes are 1 vCPU and org tiers cap total
  vCPU (10 on the free tier). `01_fanout.py` gates on `asyncio.Semaphore`; tune
  with `DAYTONA_MAX_CONCURRENT`. Exceeding it fails sandboxes mid-run.
- **Transient 502s.** The API returns `DaytonaBadGatewayError` under load. Both
  scripts retry with exponential backoff — for fan-out this is not optional.
- **Always delete.** Sandboxes are deleted in `finally`, with
  `auto_stop_interval` / `auto_delete_interval` as a second line of defence.
- **Preview tokens authenticate every port** of a sandbox, not just the one you
  asked for. `00_smoke.py` prints only the token's length.

## Status

The verification engine above is working and measured. Wiring it to the Work Stack
planning SSOT — so a planning task becomes a fan-out run and its outcome is written
back as evidence — is in progress.

## License

MIT

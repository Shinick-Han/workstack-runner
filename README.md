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

## The execution lane

`03_lane.py` takes a planning task and returns a verified implementation:

```bash
python 03_lane.py T-101 --n 5
```

```
[1/3] oracle + control generated          2.6s
[2/3] 5/5 candidates parsed               3.4s
[3/3] fan-out: 5 candidates + 1 control

cand-02                 PASS    2.56
cand-04                 PASS    2.71
cand-00                 FAIL    2.54   ValueError: invalid separator
cand-01                 FAIL    2.72   TypeError: cannot unpack non-iterable
cand-03                 FAIL    2.20   AssertionError: expected ValueError
CONTROL-known-wrong     FAIL    2.42   caught by the oracle (as it must be)

oracle gate passed - the known-wrong candidate was rejected.
survivors 2/5 | wall 3.3s vs sequential 15.2s (4.6x)
adopted cand-02 -> solution_T-101.py
```

The oracle is written by the model too. That is exactly why the control matters:
a generated test suite can be vacuous, and a vacuous suite makes every candidate
look correct. The control and the candidates run in the **same batch, against the
same oracle, in identical sandboxes** — so the gate result and the candidate
results are comparable by construction.

If the control survives, the run reports **the oracle as invalid and every green
result as meaningless**, rather than adopting a winner.

Outcomes are written back to the planning task as evidence — survivors, what was
adopted, wall time, speedup — so the plan records not just that the task is done
but what proved it.

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
| `03_lane.py` | The execution lane: planning task -> oracle -> gate -> fan-out -> evidence |
| `demo_backlog.json` | Demo workspace of planning tasks (`T-101`..`T-103`) |
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

- **Generated code gets truncated, and reasoning leaks into it.** Reasoning models
  spend tokens before emitting content, so a fenced code block can end mid-function
  with no closing fence. Worse, where a model puts its reasoning is provider-specific:
  some return it in a separate field, while DeepSeek R1 style models inline it in the
  content as `<think>...</think>`. Checking only that the text contains `def foo` then
  accepts English prose as a candidate — it parses as nothing, fails in the sandbox,
  and looks like a bad implementation rather than a broken pipeline. `llm.py`'s
  `extract_code` strips reasoning (closed and unterminated), takes the code block, and
  validates it with `ast.parse`; callers retry with a larger token budget.
- **An underspecified task produces zero survivors.** A generated oracle will
  invent requirements the candidates never saw, and everything fails. That is a
  real signal about the task, not a bug — but it means the planning task's detail
  is the actual contract, and vagueness there shows up as a red run.

## Status

The lane runs end to end against the bundled demo workspace. It reads from a
local `demo_backlog.json`; reading and writing the real Work Stack SSOT is next.

## License

MIT

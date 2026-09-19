"""실행 레인 — 계획 태스크 하나를 검증된 구현으로 바꾼다.

    python 03_lane.py T-101          # 태스크 하나 실행
    python 03_lane.py T-101 --n 6    # 후보 6개

파이프라인:

    1. 태스크 읽기        Work Stack 계획 태스크 (title + detail + entrypoint)
    2. 오라클 생성        LLM 이 이 태스크의 실행 가능한 테스트를 쓴다
    3. 오라클 게이트 ***  LLM 이 '그럴듯하지만 틀린' 구현을 쓴다.
                          그게 오라클을 통과하면 오라클을 버린다.
    4. 후보 생성          LLM 이 서로 다른 온도로 N개 구현을 쓴다
    5. 팬아웃 검증        N개 샌드박스에서 동시에 오라클을 돌린다
    6. 되돌려쓰기         생존자 + 증거를 태스크에 기록한다

3번이 이 도구의 존재 이유다. LLM 이 테스트를 쓰면 그 테스트가 무엇도
구분하지 못할 수 있다 — 통과가 아무 의미도 없는 초록불이 된다.
알려진 오답이 떨어지는 걸 먼저 보여야 나머지 초록불을 믿을 수 있다.
"""

import argparse
import ast
import asyncio
import concurrent.futures as cf
import json
import pathlib
import random
import re
import sys
import time

from daytona import (
    AsyncDaytona,
    CreateSandboxFromSnapshotParams,
    DaytonaBadGatewayError,
    DaytonaRateLimitError,
    DaytonaServiceUnavailableError,
)

from llm import LLMError, chat, provider_info

TRANSIENT = (DaytonaBadGatewayError, DaytonaServiceUnavailableError, DaytonaRateLimitError)
MAX_CONCURRENT = 10
HERE = pathlib.Path(__file__).parent
CONTROL = "CONTROL-known-wrong"

CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
OPEN_FENCE = re.compile(r"```(?:python)?\s*\n(.*)", re.S)


def extract(text: str, must_contain: str) -> str | None:
    """코드블록을 꺼내고 실제로 파싱되는지까지 확인한다.

    reasoning 모델은 content 앞에 추론 토큰을 태우기 때문에 max_tokens 에 걸려
    코드블록이 '닫히기 전에' 끊기는 일이 잦다 (실측). 닫는 펜스가 없으면
    정규식이 통째로 실패하고, must_contain 만 보면 잘린 코드를 통과시킨다.
    그래서 여는 펜스만 있는 경우도 받아주되 ast.parse 로 절단을 잡는다.
    """
    m = CODE_BLOCK.search(text) or OPEN_FENCE.search(text)
    code = (m.group(1) if m else text).strip()
    if must_contain not in code:
        return None
    try:
        ast.parse(code)
    except SyntaxError:
        return None  # 대개 토큰 상한에 걸려 잘린 응답이다
    return code


# --- 1. 태스크 ---------------------------------------------------------------

def load_task(task_id: str) -> dict:
    path = HERE / "demo_backlog.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))["tasks"]
    for t in tasks:
        if t["id"] == task_id:
            return t
    sys.exit(f"태스크 {task_id} 없음. 가능: {', '.join(t['id'] for t in tasks)}")


# --- 2~4. 프롬프트 -----------------------------------------------------------

ORACLE_PROMPT = """You are writing an executable test oracle for this task.

TITLE: {title}
DETAIL: {detail}

The implementation will be in `solution.py` exposing `{entry}`.

Write a single Python code block that:
- starts with `from solution import {entry}`
- uses plain `assert` statements only (no pytest, no unittest)
- covers the boundary cases DETAIL states, including cases where a naive or
  partially-correct implementation would give the wrong answer
- uses try/except to assert that invalid input raises, but ONLY for inputs DETAIL
  explicitly calls invalid
- ends with `print("ALL TESTS PASSED")`

CRITICAL: test only behaviour DETAIL states. Do not invent requirements, do not
test inputs whose correct result DETAIL leaves unspecified, and do not assume a
particular exception message. An independent correct implementation that read
only DETAIL must pass every assertion you write.

Output only the code block. No prose."""

CONTROL_PROMPT = """Write a DELIBERATELY INCORRECT implementation of `{entry}` for this task.

TITLE: {title}
DETAIL: {detail}

It must look plausible and run without syntax errors, but be subtly wrong: handle
the common case correctly and get an edge case, an error case, or the ordering wrong.
This is a control sample used to prove a test suite can actually detect bugs.

Output only a single Python code block defining `{entry}`. No prose."""

CANDIDATE_PROMPT = """Implement this task in Python.

TITLE: {title}
DETAIL: {detail}

Output only a single Python code block defining `{entry}` (plus any stdlib imports
and helpers it needs). No prose, no tests, no example usage."""


def gen(prompt: str, task: dict, must: str, temp: float, tries: int = 3) -> str | None:
    """실패하면 토큰 상한을 늘려 다시 묻는다.

    절단은 토큰 부족이 원인이라 같은 조건으로 재시도해봐야 또 잘린다.
    무대에서 후보 하나가 비는 건 괜찮지만 오라클·대조군이 비면 데모가 멈춘다.
    """
    msg = [{"role": "user", "content": prompt.format(
        title=task["title"], detail=task["detail"], entry=task["entrypoint"])}]
    for i in range(tries):
        try:
            code = extract(chat(msg, temperature=temp, max_tokens=2500 + 1500 * i), must)
            if code:
                return code
        except LLMError as e:
            print(f"    LLM 오류: {str(e)[:90]}")
            return None
    return None


# --- 3/5. 샌드박스 실행 ------------------------------------------------------

async def create_with_retry(daytona, params, tries=4):
    for i in range(tries):
        try:
            return await daytona.create(params)
        except TRANSIENT:
            if i == tries - 1:
                raise
            await asyncio.sleep(2**i + random.random())


async def run_one(daytona, name: str, code: str, oracle: str, gate: asyncio.Semaphore) -> dict:
    t0 = time.time()
    sandbox = None
    async with gate:
        try:
            sandbox = await create_with_retry(daytona, CreateSandboxFromSnapshotParams(
                labels={"event": "hacksprint", "candidate": name},
                auto_stop_interval=5, auto_delete_interval=10,
            ))
            await sandbox.fs.upload_file(code.encode(), "/tmp/w/solution.py")
            await sandbox.fs.upload_file(oracle.encode(), "/tmp/w/test.py")
            r = await sandbox.process.exec("cd /tmp/w && python3 test.py", timeout=60)
            out = (r.result or "").strip()
            return {"name": name, "passed": r.exit_code == 0,
                    "detail": out.splitlines()[-1][:120] if out else "",
                    "seconds": round(time.time() - t0, 2)}
        except Exception as e:
            return {"name": name, "passed": False,
                    "detail": str(e).strip().splitlines()[0][:120],
                    "seconds": round(time.time() - t0, 2)}
        finally:
            if sandbox is not None:
                await sandbox.delete()


# --- 레인 -------------------------------------------------------------------

async def lane(task: dict, n: int) -> dict:
    p = provider_info()
    print(f"\n태스크 {task['id']}  {task['title']}")
    print(f"  {task['detail'][:96]}...")
    print(f"  provider={p['name']}  model={p['model']}\n")

    print("[1/3] 오라클 + 대조군 생성")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_oracle = ex.submit(gen, ORACLE_PROMPT, task,
                             f"from solution import {task['entrypoint']}", 0.3)
        f_control = ex.submit(gen, CONTROL_PROMPT, task,
                              f"def {task['entrypoint']}", 0.9)
        oracle, control = f_oracle.result(), f_control.result()
    if not oracle:
        sys.exit("오라클 생성 실패 — 다시 실행하거나 LLM_MODEL 을 바꿔라")
    if not control:
        sys.exit("대조군 생성 실패 — 대조군 없이는 오라클을 믿을 수 없다")
    print(f"      오라클 {len(oracle.splitlines())}줄 / 대조군 확보  ({time.time() - t0:.1f}s)")

    print(f"[2/3] 후보 {n}개 생성")
    t0 = time.time()
    temps = [round(0.2 + 0.9 * i / max(n - 1, 1), 2) for i in range(n)]
    with cf.ThreadPoolExecutor(max_workers=min(n, 8)) as ex:
        codes = list(ex.map(
            lambda t: gen(CANDIDATE_PROMPT, task, f"def {task['entrypoint']}", t), temps))
    candidates = {f"cand-{i:02d}": c for i, c in enumerate(codes) if c}
    print(f"      {len(candidates)}/{n} 파싱 성공  ({time.time() - t0:.1f}s)")
    if not candidates:
        sys.exit("후보가 하나도 안 나왔다")

    # 대조군을 같은 배치에 섞는다. 따로 돌리지 않는 이유는
    # 오라클 게이트와 후보 검증이 완전히 동일한 조건에서 돌았음을 보이기 위해서다.
    print(f"[3/3] 팬아웃 검증 — 후보 {len(candidates)}개 + 대조군 1개")
    batch = dict(candidates)
    batch[CONTROL] = control
    t0 = time.time()
    gate = asyncio.Semaphore(MAX_CONCURRENT)
    async with AsyncDaytona() as daytona:
        results = await asyncio.gather(*(
            run_one(daytona, name, code, oracle, gate) for name, code in batch.items()
        ))
    wall = time.time() - t0

    ctrl = next(r for r in results if r["name"] == CONTROL)
    cands = [r for r in results if r["name"] != CONTROL]
    survivors = [r for r in cands if r["passed"]]
    serial = sum(r["seconds"] for r in results)

    print(f"\n{'후보':<24}{'결과':<8}{'초':>6}  비고")
    print("-" * 74)
    for r in sorted(results, key=lambda r: (r["name"] == CONTROL, not r["passed"], r["name"])):
        if r["name"] == CONTROL:
            note = "오라클이 걸러냄 (정상)" if not r["passed"] else "!! 오라클이 못 걸렀다"
        else:
            note = "" if r["passed"] else r["detail"][:40]
        print(f"{r['name']:<24}{'PASS' if r['passed'] else 'FAIL':<8}{r['seconds']:>6.2f}  {note}")

    oracle_valid = not ctrl["passed"]
    print()
    if oracle_valid:
        print("오라클 게이트 통과 — 알려진 오답이 떨어졌다. 나머지 초록불은 의미가 있다.")
        print(f"생존 {len(survivors)}/{len(cands)}"
              f"  |  벽시계 {wall:.1f}s vs 순차 {serial:.1f}s ({serial / wall:.1f}x)")
    else:
        print("오라클 게이트 실패 — 알려진 오답이 통과했다.")
        print("이 오라클은 무엇도 구분하지 못한다. 후보 결과는 전부 무효다.")
        print(f"(참고: {len(survivors)}/{len(cands)} 가 '통과'했지만 그 초록불은 증거가 아니다)")

    return {
        "task_id": task["id"],
        "oracle_valid": oracle_valid,
        "oracle": oracle,
        "control_passed": ctrl["passed"],
        "survivors": [r["name"] for r in survivors],
        "adopted": survivors[0]["name"] if survivors and oracle_valid else None,
        "solution": candidates[survivors[0]["name"]] if survivors and oracle_valid else None,
        "results": results,
        "wall_seconds": round(wall, 2),
        "serial_seconds": round(serial, 2),
        "speedup": round(serial / wall, 1) if wall else 0,
    }


def write_back(task: dict, outcome: dict) -> None:
    """계획 태스크에 실행 증거를 붙인다 — 계획 자체는 건드리지 않는다.

    Work Stack 은 계획(무엇을 할 것인가)을 소유하고 실행은 소유하지 않는다.
    그래서 실행 결과는 planning status 를 덮어쓰지 않고 read-only 증거로만 붙는다.
    검증이 통과했다고 태스크가 스스로 done 이 되지 않는다 — 완료 판단은 사람이 한다.

    여기서는 데모 백로그에 쓰지만 형태는 그대로 SSOT 로 옮길 수 있다.
    """
    path = HERE / "demo_backlog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for t in data["tasks"]:
        if t["id"] == task["id"]:
            t["evidence"] = {
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "oracle_valid": outcome["oracle_valid"],
                "candidates": len(outcome["results"]) - 1,
                "survivors": outcome["survivors"],
                "adopted": outcome["adopted"],
                "wall_seconds": outcome["wall_seconds"],
                "speedup": outcome["speedup"],
            }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    status = task.get("status", "open")
    if outcome["solution"]:
        out = HERE / f"solution_{task['id']}.py"
        out.write_text(outcome["solution"], encoding="utf-8")
        print(f"\n채택: {outcome['adopted']}  ->  {out.name}")
        print(f"태스크 {task['id']} 에 증거 기록됨 (status={status} 유지 — 완료 판단은 사람이)")
    else:
        print(f"\n채택 없음 — 태스크 {task['id']} 에 실패 증거만 기록됨 (status={status} 유지)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--n", type=int, default=5)
    a = ap.parse_args()
    task = load_task(a.task_id)
    outcome = asyncio.run(lane(task, a.n))
    write_back(task, outcome)


if __name__ == "__main__":
    main()

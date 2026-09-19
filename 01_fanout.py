"""팬아웃 골격 — N개 후보를 N개 샌드박스에서 동시에 빌드·테스트하고 생존자만 남긴다.

해커톤 데모의 코어 루프다. 지금은 후보가 '작은 파이썬 패치'지만,
현장에서는 CANDIDATES 만 LLM이 만든 진짜 패치로 갈아끼우면 된다.

    python 01_fanout.py          # 기본 4개
    python 01_fanout.py 12       # 12개 동시
"""

import asyncio
import json
import os
import pathlib
import random
import sys
import time

from daytona import (
    AsyncDaytona,
    CreateSandboxFromSnapshotParams,
    DaytonaBadGatewayError,
    DaytonaRateLimitError,
    DaytonaServiceUnavailableError,
)

# 일시적 서버측 오류. 2026-09-19 실측: create 가 502 를 뱉다가 재시도하니 붙었다.
# N개를 동시에 때리면 확률이 올라가므로 팬아웃에서는 재시도가 선택이 아니다.
TRANSIENT = (DaytonaBadGatewayError, DaytonaServiceUnavailableError, DaytonaRateLimitError)

# 조직 tier 상한. 2026-09-19 실측: 12개를 한 번에 띄우니
#   "Total CPU limit exceeded. Maximum allowed: 10" 으로 2개가 생성 실패했다.
# 기본 샌드박스가 1 vCPU 라 동시 10개가 천장이다. 무대에서 이걸 모르고 돌리면
# 데모 중에 절반이 빨갛게 뜬다. 올리려면 app.daytona.io/dashboard/limits 에서 tier 상향.
MAX_CONCURRENT = int(os.environ.get('DAYTONA_MAX_CONCURRENT', '10'))


async def create_with_retry(daytona, params, tries=4):
    for i in range(tries):
        try:
            return await daytona.create(params)
        except TRANSIENT:
            if i == tries - 1:
                raise
            await asyncio.sleep(2**i + random.random())  # jitter - 동시 재시도 뭉침 방지

# --- 고쳐야 할 버그: median() 이 짝수 길이에서 틀린다 -------------------------
BUGGY = '''
def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]
'''

TESTS = '''
from solution import median
assert median([3, 1, 2]) == 2
assert median([4, 1, 3, 2]) == 2.5      # 짝수 길이 - 원본(BUGGY)이 틀리는 지점
assert median([1]) == 1
assert median([2, 2, 2, 2]) == 2
# 판별용: 평균(34.33)과 중앙값(2)이 갈라지는 입력.
# 이게 없으면 "평균을 리턴하는 가짜 패치"가 위 4줄을 전부 통과한다 - 실제로 겪었다.
assert median([1, 2, 100]) == 2
assert median([1, 2, 3, 1000]) == 2.5
print("ALL TESTS PASSED")
'''

# 현장에서는 이 리스트가 LLM 생성 패치 N개가 된다.
CANDIDATES = {
    "original": BUGGY,
    "patch-a-midpoint": '''
def median(xs):
    xs = sorted(xs)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2
''',
    "patch-b-statistics": '''
import statistics
def median(xs):
    return statistics.median(xs)
''',
    "patch-c-wrong": '''
def median(xs):
    xs = sorted(xs)
    return sum(xs) / len(xs)          # 평균을 중앙값이라 우김 - 떨어져야 정상
''',
}


async def verify(daytona, name: str, code: str, idx: int, gate: asyncio.Semaphore) -> dict:
    """샌드박스 하나를 띄워 후보 하나를 검증한다.

    gate 가 동시 실행 수를 조직 tier 상한 아래로 묶는다.
    """
    t0 = time.time()
    sandbox = None
    async with gate:
        try:
            sandbox = await create_with_retry(
                daytona,
                CreateSandboxFromSnapshotParams(
                    labels={"event": "hacksprint", "candidate": name},
                    auto_stop_interval=5,
                    auto_delete_interval=10,
                ),
            )
            print(f"  [{idx:>2}] {name:<22} up     {(time.time() - t0) * 1000:6.0f}ms  {sandbox.id[:8]}")

            await sandbox.fs.upload_file(code.encode(), "/tmp/w/solution.py")
            await sandbox.fs.upload_file(TESTS.encode(), "/tmp/w/test.py")

            r = await sandbox.process.exec("cd /tmp/w && python3 test.py", timeout=60)
            passed = r.exit_code == 0
            print(f"  [{idx:>2}] {name:<22} {'PASS' if passed else 'FAIL'}   {time.time() - t0:5.2f}s")
            return {
                "name": name,
                "passed": passed,
                "exit_code": r.exit_code,
                "detail": (r.result or "").strip().splitlines()[-1] if r.result else "",
                "seconds": round(time.time() - t0, 2),
            }
        except Exception as e:
            first = str(e).strip().splitlines()[0][:70]
            print(f"  [{idx:>2}] {name:<22} ERROR  {first}")
            return {
                "name": name,
                "passed": False,
                "exit_code": -1,
                "detail": first,
                "seconds": round(time.time() - t0, 2),
            }
        finally:
            if sandbox is not None:
                await sandbox.delete()


def load_candidates() -> dict:
    """02_generate.py 가 만든 candidates.json 이 있으면 그걸 쓴다.

    없으면 위의 하드코딩 후보로 돈다 — LLM 없이도 데모 골격이 항상 돌아가야 한다.
    """
    f = pathlib.Path(__file__).with_name("candidates.json")
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        print(f"candidates.json 로드: LLM 생성 후보 {len(data)}개")
        return data
    print("candidates.json 없음 -> 내장 후보로 실행 (python 02_generate.py 로 LLM 후보 생성)")
    return CANDIDATES


async def main():
    if not os.environ.get("DAYTONA_API_KEY"):
        sys.exit("DAYTONA_API_KEY 없음")

    items = list(load_candidates().items())
    # 인자를 주면 그 개수만큼 순환 복제한다(부하 테스트용). 안 주면 후보 전부를 돈다.
    # 기본값을 하드코딩 CANDIDATES 길이로 두면 LLM 후보가 잘려나간다 - 실제로 당했다.
    # 특히 맨 뒤의 original-buggy 대조군이 사라지면 오라클 판별력을 증명할 수 없다.
    want = int(sys.argv[1]) if len(sys.argv) > 1 else len(items)
    if want > len(items):
        items = [(f"{n}#{i // len(items)}" if i >= len(items) else n, c)
                 for i, (n, c) in enumerate((items * (want // len(items) + 1))[:want])]
    elif want < len(items):
        print(f"주의: 후보 {len(items)}개 중 {want}개만 돈다 - 대조군이 잘릴 수 있다")
        items = items[:want]

    print(f"\n{len(items)}개 후보 동시 검증 (동시 상한 {MAX_CONCURRENT})\n")
    t0 = time.time()

    gate = asyncio.Semaphore(MAX_CONCURRENT)
    async with AsyncDaytona() as daytona:
        results = await asyncio.gather(
            *(verify(daytona, name, code, i, gate) for i, (name, code) in enumerate(items))
        )

    wall = time.time() - t0
    survivors = [r for r in results if r["passed"]]
    serial = sum(r["seconds"] for r in results)

    print(f"\n{'후보':<24}{'결과':<8}{'초':>6}")
    print("-" * 40)
    for r in sorted(results, key=lambda r: (not r["passed"], r["name"])):
        print(f"{r['name']:<24}{'PASS' if r['passed'] else 'FAIL':<8}{r['seconds']:>6.2f}")

    print(f"\n생존 {len(survivors)}/{len(results)}"
          f"  |  벽시계 {wall:.1f}s  vs  순차 합계 {serial:.1f}s  ({serial / wall:.1f}x)")
    if survivors:
        print(f"채택: {survivors[0]['name']}")


if __name__ == "__main__":
    asyncio.run(main())

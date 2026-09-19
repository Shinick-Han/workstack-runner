"""Daytona 왕복 스모크 테스트 — 생성 -> 실행 -> 셸 -> 공개 프리뷰 URL -> 삭제.

이 4단계가 해커톤 데모의 전부다. 여기까지 통과하면 현장에서 설치로 시간 날릴 일은 없다.

    python 00_smoke.py
"""

import os
import sys
import time
import urllib.request

from daytona import (
    CreateSandboxFromSnapshotParams,
    Daytona,
    DaytonaBadGatewayError,
    DaytonaRateLimitError,
    DaytonaServiceUnavailableError,
    SessionExecuteRequest,
)

TRANSIENT = (DaytonaBadGatewayError, DaytonaServiceUnavailableError, DaytonaRateLimitError)


def create_with_retry(client, params=None, tries=4):
    """해커톤 당일 API가 502를 뱉는다. 실측함 - 재시도하면 붙는다."""
    for i in range(tries):
        try:
            return client.create(params) if params else client.create()
        except TRANSIENT as e:
            if i == tries - 1:
                raise
            wait = 2 ** i
            print(f"    (일시적 {type(e).__name__}, {wait}s 후 재시도 {i + 2}/{tries})")
            time.sleep(wait)

if not os.environ.get("DAYTONA_API_KEY"):
    sys.exit("DAYTONA_API_KEY 가 없다. setx DAYTONA_API_KEY \"...\" 후 새 터미널에서 실행할 것.")

daytona = Daytona()
sandbox = None
t0 = time.time()

try:
    # public=True 여야 프리뷰 URL이 토큰 없이 열린다 (무대에서 남의 브라우저로 보여줄 때 필수).
    # ttl_minutes / auto_delete_interval 은 크레딧 방어용 안전장치.
    sandbox = create_with_retry(
        daytona,
        CreateSandboxFromSnapshotParams(
            public=True,
            labels={"event": "hacksprint", "role": "smoke"},
            auto_stop_interval=5,     # 5분 유휴 시 정지
            auto_delete_interval=15,  # 정지 15분 후 삭제
        ),
    )
    print(f"[1/4] created  id={sandbox.id}  ({time.time() - t0:.2f}s)")

    # --- 코드 실행
    r = sandbox.process.code_run("print('sum:', 3 + 4)")
    print(f"[2/4] code_run exit={r.exit_code} out={r.result.strip()!r}")
    assert r.exit_code == 0, r.result

    # --- 진짜 머신인지 확인 (패키지 설치 + git + 프로세스)
    r = sandbox.process.exec("python -V && git --version && nproc && free -m | head -2")
    print(f"[3/4] exec exit={r.exit_code}")
    for line in r.result.strip().splitlines():
        print("        ", line)

    # --- 웹서버 띄우고 공개 URL 받기 (데모에서 심사위원이 실제로 클릭할 것)
    sandbox.process.exec(
        "mkdir -p /tmp/web && echo '<h1>Daytona sandbox alive</h1>' > /tmp/web/index.html"
    )
    sandbox.process.create_session("web")
    sandbox.process.execute_session_command(
        "web",
        SessionExecuteRequest(
            command="python3 -m http.server 3000 --directory /tmp/web",
            run_async=True,
        ),
    )
    time.sleep(2)

    preview = sandbox.get_preview_link(3000)
    print(f"[4/4] preview url = {preview.url}")
    # token 은 일부러 출력하지 않는다 - 그 샌드박스의 모든 포트를 인증한다.
    print(f"        token     = <{len(preview.token)}자, 미출력>")

    try:
        with urllib.request.urlopen(preview.url, timeout=15) as resp:
            body = resp.read().decode()
        print(f"        HTTP {resp.status}: {body.strip()}")
    except Exception as e:  # 네트워크/전파 지연이면 브라우저로 직접 열어볼 것
        print(f"        (fetch 실패: {e} — 브라우저로 직접 열어볼 것)")

    print(f"\nOK — 총 {time.time() - t0:.2f}s")

finally:
    if sandbox is not None:
        sandbox.delete()
        print("cleanup: deleted")

# Daytona 해커톤 킷

**2026-09-19 실환경에서 전 구간 실행 검증 완료.**
Python 3.12.10 / `daytona` SDK **0.214.0** / CLI **v0.190.0** / API **v0.215.0** (Windows)

## 실행 — 전체 파이프라인 (2026-09-19 실측 완주)

```
python 00_smoke.py            # 왕복 4단계          - 6.5s
python 02_generate.py 6       # Groq 로 패치 6개 생성 - 각 ~1s (병렬)
python 01_fanout.py           # 7개(대조군 포함) 검증 - 벽시계 3.6s, 순차 18.5s (5.2x)
```

마지막 실행 결과: **생존 6/7 — `original-buggy` 대조군만 FAIL.**
오라클이 살아있다는 증거가 매 실행마다 화면에 찍힌다.

`DAYTONA_API_KEY` 는 사용자 환경변수에 있다. **새 터미널부터 보인다.**

### 7. Groq 3연속 함정 (전부 실측으로 뚫음)

| 증상 | 원인 | 해결 |
|---|---|---|
| `403 error code: 1010` | Cloudflare 가 `Python-urllib` UA 를 차단 | `User-Agent` 헤더를 채운다 |
| `404 model_not_found` | `llama-3.3-70b-versatile` 이 이 계정에 없음 | `/models` 로 실제 목록 조회 |
| `content=''` (빈 응답) | `gpt-oss` 는 reasoning 모델 — content 앞에 추론 토큰을 태운다 | `max_tokens` 를 넉넉히 (1800) |

이 계정에서 쓸 수 있는 chat 모델: `openai/gpt-oss-120b`(기본, 추론형),
`openai/gpt-oss-20b`, `qwen/qwen3.8-27b`(추론 안 씀 — 빠른 대안), `groq/compound`.

### 8. 후보 잘림 — 대조군이 사라진다

`01_fanout.py` 가 인자 없이 돌 때 하드코딩 후보 개수(4)로 잘라서
LLM 후보 7개 중 4개만 돌았고, **맨 뒤의 `original-buggy` 대조군이 날아갔다.**
전원 PASS 로 보여서 오히려 그럴듯했던 게 더 위험하다. 지금은 후보 전부를 돌고,
인자로 줄이면 경고를 찍는다.

## 파일

| 파일 | 역할 |
|---|---|
| `00_smoke.py` | 생성 → `code_run` → 셸 → 공개 프리뷰 URL → 삭제. 통과하면 셋업 끝 |
| `02_generate.py` | LLM 으로 후보 패치 N개 생성 → `candidates.json` (원본 대조군 자동 포함) |
| `01_fanout.py` | 팬아웃 검증. `candidates.json` 있으면 그걸, 없으면 내장 후보를 쓴다 |
| `llm.py` | 프로바이더 한 겹. **Groq→Nosana 는 환경변수 2개 교체로 끝난다** |

### 현장에서 Nosana 로 갈아끼우기

```
LLM_PROVIDER=nosana
NOSANA_BASE_URL=<현장에서 받는 엔드포인트>
NOSANA_API_KEY=<크레딧 키>
NOSANA_MODEL=<모델명>
```

셋 다 OpenAI 호환 `/chat/completions` 라 코드는 한 줄도 안 바꾼다.

---

## 실측 결과 (2026-09-19)

```
[1/4] created  id=e5fa8f05...  (2.07s)
[2/4] code_run exit=0 out='sum: 7'
[3/4] exec exit=0
         Python 3.14.4 / git 2.53.0 / 2 vCPU / 1996MB RAM
[4/4] preview url = https://3000-e5fa8f05....daytonaproxy01.net
        HTTP 200: <h1>Daytona sandbox alive</h1>
OK — 총 6.49s
```

- 샌드박스 부팅 **1.3~2.2s** (문서의 90~200ms 는 런타임 준비 시간이고, API 왕복 포함 실측은 이 정도)
- 기본 이미지에 Python 3.14.4 + git 이 이미 들어 있다
- 실측 스펙 2 vCPU / 2GB (문서상 기본값 1 vCPU / 1GB 와 다름)
- `public=True` 로 만든 프리뷰 URL은 **토큰 없이 HTTP 200** — 무대에서 남의 브라우저로 열린다

## 함정 — 전부 직접 부딪힌 것들

### 1. 조직 tier 상한: 총 10 vCPU 동시

12개를 한 번에 띄우니 2개가 이걸로 죽었다:

```
Failed to create sandbox: Total CPU limit exceeded. Maximum allowed: 10.
```

기본 샌드박스가 1 vCPU라 **동시 10개가 천장**이다. `01_fanout.py` 는
`asyncio.Semaphore(MAX_CONCURRENT)` 로 묶어서 해결했다 (`DAYTONA_MAX_CONCURRENT` 로 조절).
무대에서 이걸 모르고 20개를 돌리면 **화면 절반이 빨갛게 뜬다.**
상향: app.daytona.io/dashboard/limits

### 2. create 가 502 Bad Gateway 를 뱉는다

첫 시도가 `DaytonaBadGatewayError (502)` 로 실패했다. 키 문제가 아니었다 —
`daytona.list()` 는 같은 키로 멀쩡히 통했다. **재시도하니 붙었다.**
두 스크립트 모두 지수 백오프 + jitter 재시도를 넣어뒀다. 해커톤 당일 동시 접속이 몰리면
확률이 올라가므로 팬아웃에서 재시도는 선택이 아니다.

### 3. 판별력 없는 테스트가 가짜 패치를 통과시킨다

일부러 넣은 오답 후보(`patch-c-wrong`, 중앙값 대신 **평균**을 리턴)가 **PASS 했다.**
원래 테스트 4줄이 우연히 평균과 중앙값이 같은 입력들뿐이었다:

```
median([3,1,2])    중앙값 2    평균 2      <- 구분 못 함
median([4,1,3,2])  중앙값 2.5  평균 2.5    <- 구분 못 함
```

`median([1, 2, 100])` (중앙값 2 / 평균 34.33) 를 넣자 정상적으로 FAIL 했다.
**검증 하네스를 만들 때 오라클의 판별력을 먼저 증명해야 한다** — 알려진 오답이
떨어지는 걸 확인하지 않은 통과는 아무것도 보장하지 않는다.
데모에서 `patch-c-wrong` 이 빨갛게 죽는 그림 자체가 "우리 게이트는 진짜로 거른다"의 증거다.

### 4. 프리뷰 토큰은 포트 전용이 아니다

`get_preview_link()` 가 주는 토큰은 **그 샌드박스의 모든 포트**를 인증한다.
화면 공유 중에 출력하지 말 것. `00_smoke.py` 는 길이만 찍는다.

### 5. CLI 와 SDK 가 서로 다른 API 를 본다 — `daytona login` 은 필요 없었다

`daytona list` 가 계속 이렇게 죽었다:

```
Forbidden: Please provide a valid email address.
 - check that your API key has sufficient permissions for this action
```

로그인 문제로 보이지만 아니다. **기본 엔드포인트가 다르다:**

| | 기본 API URL |
|---|---|
| CLI v0.190.0 | `https://api.daytona.work` |
| Python SDK 0.214.0 | `https://app.daytona.io/api` |

CLI 에 SDK 와 같은 주소를 물리면 바로 통한다. **브라우저 로그인 불필요:**

```
DAYTONA_API_URL=https://app.daytona.io/api
```

사용자 환경변수에 넣어뒀다. MCP 의 `create_sandbox` 도 이걸로 403 → 정상 생성이 됐다.
CLI 릴리스는 v0.190.0(2026-06-23)이 최신이라 API v0.215.0 과의 skew 는 우회 불가다 —
경고는 무시하고 URL 만 맞추면 된다.

### 6. 삭제 누락 = 크레딧 증발

`finally: sandbox.delete()` + `auto_stop_interval` / `auto_delete_interval` 이중으로.

---

## 0.214.0 실측 API

```python
from daytona import (Daytona, AsyncDaytona, CreateSandboxFromSnapshotParams,
                     SessionExecuteRequest)

daytona = Daytona()                              # DAYTONA_API_KEY 자동 인식
sandbox = daytona.create(CreateSandboxFromSnapshotParams(
    public=True,              # 토큰 없이 열리는 프리뷰 URL - 무대 데모 필수
    auto_stop_interval=5,     # 분
    auto_delete_interval=10,  # 분
    labels={"event": "hacksprint"},
))

sandbox.process.code_run("print(1+1)")           # -> ExecuteResponse(exit_code, result)
sandbox.process.exec("pytest -q", cwd="/tmp/w", timeout=60)
sandbox.fs.upload_file(b"...", "/tmp/w/a.py")    # bytes 또는 로컬 경로
sandbox.get_preview_link(3000).url               # PortPreviewUrl(url, token, sandbox_id)
sandbox.delete()
```

백그라운드 서버:

```python
sandbox.process.create_session("web")
sandbox.process.execute_session_command("web",
    SessionExecuteRequest(command="python3 -m http.server 3000", run_async=True))
```

팬아웃은 반드시 `AsyncDaytona` — 동기 클라이언트로 루프 돌리면 직렬이다.

```python
gate = asyncio.Semaphore(10)                     # tier 상한
async with AsyncDaytona() as d:
    results = await asyncio.gather(*(verify(d, c, gate) for c in candidates))
```

## MCP (Claude Code 연결)

등록 완료 — `claude mcp get daytona` 가 **Connected**. 툴 12개:
`create_sandbox` `destroy_sandbox` `execute_command` `file_upload` `file_download`
`git_clone` `list_files` `create_folder` `move_file` `delete_file` `get_file_info` `preview_link`

```
claude mcp add daytona --scope user --env DAYTONA_API_URL=https://app.daytona.io/api -- "%APPDATA%\bin\daytona\daytona.exe" mcp start
```

**API 키는 설정 파일에 안 넣는다** — 키는 환경변수에서만 읽는다.
사용자 환경변수 `DAYTONA_API_KEY` 를 상속받는 구조라, 키를 설정한 뒤 시작된
Claude Code 프로세스에서만 인증된다 — **한 번 재시작 필요.**

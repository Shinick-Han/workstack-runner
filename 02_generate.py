"""후보 패치 N개를 LLM 으로 생성한다 — 데모의 'AI' 쪽 절반.

    python 02_generate.py 8        # 8개 생성 -> candidates.json
    python 01_fanout.py            # candidates.json 이 있으면 그걸 검증한다

N번을 서로 다른 온도로 병렬 호출한다. 같은 프롬프트라도 흩어지게 해야
'여러 후보 중 살아남는 것을 고른다'는 서사가 성립한다.
"""

import concurrent.futures as cf
import json
import pathlib
import re
import sys
import time

from llm import LLMError, chat, provider_info

BUGGY = '''def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]
'''

FAILING = "median([4, 1, 3, 2]) 가 2 를 반환한다. 기대값은 2.5."

PROMPT = """아래 파이썬 함수에 버그가 있다.

```python
{buggy}
```

증상: {failing}

고친 함수 전체를 파이썬 코드블록 하나로만 답하라.
설명·주석·테스트는 쓰지 말고 `def median(xs):` 로 시작하는 함수 본문만 출력한다."""

CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str | None:
    m = CODE_BLOCK.search(text)
    code = (m.group(1) if m else text).strip()
    return code if "def median" in code else None


def one(i: int, temp: float) -> tuple[str, str | None, str]:
    t0 = time.time()
    try:
        out = chat(
            [{"role": "user", "content": PROMPT.format(buggy=BUGGY, failing=FAILING)}],
            temperature=temp,
        )
        code = extract_code(out)
        note = "OK" if code else "코드블록 파싱 실패"
    except LLMError as e:
        code, note = None, str(e)[:80]
    return f"llm-{i:02d}", code, f"{note}  ({time.time() - t0:.1f}s)"


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    p = provider_info()
    print(f"provider={p['name']}  model={p['model']}  후보 {n}개 생성\n")

    temps = [round(0.2 + 0.9 * i / max(n - 1, 1), 2) for i in range(n)]
    with cf.ThreadPoolExecutor(max_workers=min(n, 8)) as ex:
        results = list(ex.map(lambda a: one(*a), enumerate(temps)))

    candidates = {}
    for name, code, note in results:
        print(f"  {name}  {note}")
        if code:
            candidates[name] = code

    # 대조군: 원본(고장난 채로)을 항상 섞는다. 이게 FAIL 해야 오라클이 살아있다는 증거다.
    candidates["original-buggy"] = BUGGY

    out = pathlib.Path(__file__).with_name("candidates.json")
    out.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(candidates)}개 저장 -> {out.name}  (원본 대조군 1개 포함)")
    print("다음: python 01_fanout.py")


if __name__ == "__main__":
    main()

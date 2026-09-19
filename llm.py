"""LLM 프로바이더 한 겹 — Groq / Nosana / OpenAI 를 같은 코드로 부른다.

현장에서 Nosana 크레딧을 받으면 환경변수 두 개만 바꾸면 된다.
셋 다 OpenAI 호환 /chat/completions 라서 코드 경로는 하나다.

    LLM_PROVIDER=groq    (기본)
    LLM_PROVIDER=nosana  + NOSANA_BASE_URL, NOSANA_API_KEY, NOSANA_MODEL
    LLM_PROVIDER=openai

의존성 없음 (urllib). 현장에서 pip 사고 안 난다.
"""

import json
import os
import urllib.error
import urllib.request

PROVIDERS = {
    # Groq 계정에서 실제로 쓸 수 있던 chat 모델 (2026-09-19 /models 조회):
    #   openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.8-27b,
    #   groq/compound, groq/compound-mini, allam-2-7b
    # llama-3.3-70b-versatile 은 404 - 문서에 흔히 보이지만 이 계정엔 없다.
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "openai/gpt-oss-120b",  # 2026-09-19 계정에서 실제 조회한 목록 기준
    },
    "nosana": {
        # 현장에서 받는 엔드포인트로 NOSANA_BASE_URL 만 채우면 된다.
        "base_url": os.environ.get("NOSANA_BASE_URL", ""),
        "key_env": "NOSANA_API_KEY",
        "model": os.environ.get("NOSANA_MODEL", "llama-3.3-70b"),
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
}


class LLMError(RuntimeError):
    pass


def provider_info(name: str | None = None) -> dict:
    name = (name or os.environ.get("LLM_PROVIDER", "groq")).lower()
    if name not in PROVIDERS:
        raise LLMError(f"모르는 프로바이더: {name} (가능: {', '.join(PROVIDERS)})")
    p = dict(PROVIDERS[name])
    p["name"] = name
    p["api_key"] = os.environ.get(p["key_env"], "")
    p["model"] = os.environ.get("LLM_MODEL", p["model"])
    return p


# gpt-oss 계열은 reasoning 모델이라 content 앞에 추론 토큰을 먼저 태운다.
# max_tokens 가 작으면 finish_reason=length 로 content 가 '' 인 채 끊긴다 (실측).
# 빠르고 추론을 안 쓰는 대안: LLM_MODEL=qwen/qwen3.8-27b
def chat(messages: list[dict], *, temperature: float = 0.8, max_tokens: int = 1800,
         provider: str | None = None, timeout: int = 60) -> str:
    p = provider_info(provider)
    if not p["api_key"]:
        raise LLMError(f"{p['key_env']} 가 비어 있다. 환경변수를 설정할 것.")
    if not p["base_url"]:
        raise LLMError(f"{p['name']} 의 base_url 이 비어 있다.")

    req = urllib.request.Request(
        f"{p['base_url']}/chat/completions",
        data=json.dumps({
            "model": p["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode(),
        headers={
            "Authorization": f"Bearer {p['api_key']}",
            "Content-Type": "application/json",
            # Cloudflare 가 기본 Python-urllib UA 를 막는다 (403 error code: 1010).
            # 2026-09-19 Groq 에서 실측. UA 만 채우면 통과한다.
            "User-Agent": "daytona-hacksprint/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise LLMError(f"{p['name']} HTTP {e.code}: {e.read().decode()[:200]}") from e


def selftest() -> None:
    p = provider_info()
    print(f"provider = {p['name']}  model = {p['model']}  key = {'있음' if p['api_key'] else '없음'}")
    print("응답:", chat([{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=300).strip())


if __name__ == "__main__":
    selftest()

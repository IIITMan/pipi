"""OpenAI 兼容接口的 LLM Provider。

适用：通义千问(Qwen) / DeepSeek / 豆包 / Kimi / OpenAI / 本地 vLLM / Ollama
—— 只要兼容 /chat/completions，这个类就能用。

只用标准库 urllib（异步包装到线程池），避免强制依赖 openai SDK。
装了 aiohttp/httpx 会自动优先使用，延迟更低。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, AsyncIterator

from pipi.providers.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OpenAICompatLLM:
    """OpenAI 兼容接口客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "qwen3-flash",
        cheap_model: str | None = None,
        *,
        name: str = "openai_compat",
        timeout_s: float = 30.0,
        max_retries: int = 2,
        extra_body: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.cheap_model = cheap_model or model
        self.name = name
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.extra_body = extra_body or {}
        # 调试统计：最近一次请求延迟 + 累计
        self.last_latency_ms = 0
        self.calls = 0
        self.total_ms = 0

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 150,
        use_cheap: bool = False,
    ) -> LLMResponse:
        payload = {
            "model": self.cheap_model if use_cheap else self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "presence_penalty": 0.4,
            "frequency_penalty": 0.3,
            **self.extra_body,
        }
        start = time.time()
        data = await self._post("/chat/completions", payload)
        latency = int((time.time() - start) * 1000)
        self.last_latency_ms = latency
        self.calls += 1
        self.total_ms += latency

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResponse(
            text=(msg.get("content") or "").strip(),
            model=data.get("model", payload["model"]),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            latency_ms=latency,
            finish_reason=choice.get("finish_reason", "stop"),
        )

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        use_cheap: bool = False,
        max_tokens: int = 400,
        temperature: float = 0.3,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = await self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, use_cheap=use_cheap
        )
        return resp.text

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 150,
        use_cheap: bool = False,
    ) -> AsyncIterator[str]:
        """真·SSE 流式生成：token 级增量输出。

        SSE 连接在独立线程里读（urllib 是阻塞 IO），
        通过 asyncio.Queue 桥接到异步侧；失败时降级为非流式分句输出。
        """
        payload = {
            "model": self.cheap_model if use_cheap else self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "presence_penalty": 0.4,
            "frequency_penalty": 0.3,
            "stream": True,
            **self.extra_body,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def _worker() -> None:
            try:
                req = urllib.request.Request(url, data=body, headers=headers,
                                             method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = (chunk.get("choices") or [{}])[0] \
                                .get("delta", {}).get("content")
                        except (json.JSONDecodeError, IndexError, AttributeError):
                            continue
                        if delta:
                            loop.call_soon_threadsafe(queue.put_nowait, ("delta", delta))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        import threading

        threading.Thread(target=_worker, daemon=True).start()

        got_any = False
        error: Exception | None = None
        start = time.time()
        while True:
            kind, item = await queue.get()
            if kind == "delta":
                if not got_any:
                    got_any = True
                    self.last_latency_ms = int((time.time() - start) * 1000)
                    self.calls += 1
                    self.total_ms += self.last_latency_ms
                yield item
            elif kind == "done":
                return
            else:
                error = item
                break

        # 流式失败且一个 token 都没拿到 → 降级非流式
        if not got_any:
            logger.warning("SSE 流式失败，降级非流式：%s", error)
            resp = await self.chat(
                messages, temperature=temperature, max_tokens=max_tokens,
                use_cheap=use_cheap,
            )
            for chunk in _split_sentences(resp.text):
                yield chunk

    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.to_thread(
                    self._sync_request, url, body, headers
                )
            except Exception as exc:
                last_err = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"LLM 请求失败（已重试 {self.max_retries} 次）：{last_err}")

    def _sync_request(self, url: str, body: bytes, headers: dict[str, str]) -> dict:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def _split_sentences(text: str, max_len: int = 40) -> list[str]:
    """把长文本按句切分，模拟流式。"""
    import re
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?…\n])", text)
    out: list[str] = []
    for p in parts:
        if not p.strip():
            continue
        while len(p) > max_len:
            out.append(p[:max_len])
            p = p[max_len:]
        if p.strip():
            out.append(p.strip())
    return out


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------

PRESETS = {
    # 选型依据见 LLM_SELECTION.md（全云端方案）。
    # 闲聊走 Flash 档：≤32K 输入 ¥0.2 / 输出 ¥0.8（每百万 Token），
    # 实测约 ¥0.15/千轮。成本不是约束，延迟与人格稳定性才是。
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-flash",
        "cheap_model": "qwen3-flash",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "cheap_model": "deepseek-chat",
    },
    # 豆包：中文拟人度（"人话感"）公认最强，语音生态也是国内最完整的一套。
    # 若实测 qwen 的人格不稳定，首选换它。
    # 注意：方舟模型全名带日期后缀，可在 /models 接口查账号已开通的版本
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-2-1-pro-260915",
        "cheap_model": "doubao-seed-2-1-pro-260915",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "cheap_model": "gpt-4o-mini",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "cheap_model": "moonshot-v1-8k",
    },
}


def create_llm(provider: str, api_key: str, base_url: str = "",
               model: str = "", cheap_model: str = "",
               extra_body: dict | None = None, **kwargs) -> LLMProvider:
    """按名称创建 LLM。未知 provider 时按 OpenAI 兼容处理。"""
    preset = PRESETS.get(provider, {})
    return OpenAICompatLLM(
        base_url=base_url or preset.get("base_url", ""),
        api_key=api_key,
        model=model or preset.get("model", "qwen3-flash"),
        cheap_model=cheap_model or preset.get("cheap_model"),
        name=provider,
        extra_body=extra_body,
        **kwargs,
    )

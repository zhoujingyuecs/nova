"""云端大语言模型适配层。

把"OpenAI 兼容的 chat completions HTTP 接口"包装成和 LocalLLM 一样的接口：
只暴露一个 `chat(system, user, max_tokens) -> str`。

绝大多数主流厂商都提供 OpenAI 兼容端点，只是 base_url / model 名不同：

| Provider             | base_url                                              | 推荐 model            |
| -------------------- | ----------------------------------------------------- | --------------------- |
| DeepSeek             | https://api.deepseek.com/v1                           | deepseek-chat         |
| 阿里云百炼 (通义千问) | https://dashscope.aliyuncs.com/compatible-mode/v1     | qwen-plus             |
| 火山方舟 (豆包)       | https://ark.cn-beijing.volces.com/api/v3              | doubao-pro-32k        |
| Moonshot (Kimi)      | https://api.moonshot.cn/v1                            | moonshot-v1-32k       |
| 智谱 GLM             | https://open.bigmodel.cn/api/paas/v4                  | glm-4-plus            |
| SiliconFlow (硅基流动)| https://api.siliconflow.cn/v1                         | Qwen/Qwen2.5-72B-Instruct |
| OpenRouter           | https://openrouter.ai/api/v1                          | deepseek/deepseek-chat |
| OpenAI               | https://api.openai.com/v1                             | gpt-4o-mini           |
| Anthropic (兼容层)    | https://api.anthropic.com/v1/                         | claude-sonnet-4-5     |

只依赖标准库 `urllib`，**不引入 requests / openai 这种额外依赖**——这样
"零本地模型部署"那条最轻量路径完全装得下：32MB venv 起步，能跑就行。

把 `<think>...</think>` 推理块剥掉（DeepSeek-R1 / Qwen3-thinking 系列会带），
这样 nova 在主循环里看到的永远是干净的最终输出，不会被冒泡的内省搞混。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from .config import NovaConfig


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r"</think>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    """剥掉 reasoning 模型留下的 <think>...</think> 块。"""
    if not text:
        return text or ""
    text = _THINK_BLOCK_RE.sub("", text)
    m = _OPEN_THINK_RE.search(text)
    if m:
        # 残缺的 <think> 没闭合——后面整段都当成 think 丢掉
        text = text[: m.start()]
    m = _CLOSE_THINK_RE.search(text)
    if m:
        # 残缺的 </think> 没开头——前面是 think，剥掉
        text = text[m.end():]
    return text.strip()


class OpenAICompatLLM:
    """对 OpenAI 兼容的 `/chat/completions` 端点做最薄的封装。

    chat(system, user) 一次往返。简单、可重试、不流式——nova 的主循环
    一次就要一整段，没必要分块。

    重试：网络抖动是常态。失败后 backoff 重试 `cfg.llm_api_retries` 次。
    """

    def __init__(self, cfg: NovaConfig):
        self.cfg = cfg
        self.api_base = (cfg.llm_api_base or "https://api.openai.com/v1").rstrip("/")
        self.api_key = (cfg.llm_api_key or "").strip()
        self.model = (cfg.llm_api_model or "gpt-4o-mini").strip()
        self.timeout = float(cfg.llm_api_timeout)
        self.retries = max(0, int(cfg.llm_api_retries))

        # 自定义 header（部分厂商需要，比如 OpenRouter 推荐 HTTP-Referer）
        self.extra_headers: dict = dict(cfg.llm_api_extra_headers or {})

        if not self.api_key:
            print(
                "⚠️ 云端 LLM 没配 API key。"
                "请设置环境变量 NOVA_LLM_API_KEY 或写到 .env 里，"
                "否则每次调用都会 401。"
            )

    # ----------------------------------------------------------
    def chat(self, system: str, user: str,
             max_tokens: Optional[int] = None) -> str:
        url = self.api_base + "/chat/completions"

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or ""},
                {"role": "user", "content": user or ""},
            ],
            "temperature": float(self.cfg.temperature),
            "top_p": float(self.cfg.top_p),
            "max_tokens": int(max_tokens or self.cfg.max_tokens),
            "stream": False,
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "nova/1.2 (https://github.com/zhoujingyuecs/nova)",
        }
        headers.update(self.extra_headers)

        last_err = None
        for attempt in range(self.retries + 1):
            try:
                raw = self._post_json(url, body, headers)
                return _strip_think(self._extract_text(raw))
            except _Retryable as e:
                last_err = e
                if attempt < self.retries:
                    delay = min(2 ** attempt, 8)
                    print(f"  [LLM 重试 {attempt + 1}/{self.retries}：{e}（{delay}s 后再试）]")
                    time.sleep(delay)
                    continue
                break
            except Exception as e:
                # 非可重试错误（4xx 业务报错 / 鉴权失败 / 模型不存在）
                raise RuntimeError(f"云端 LLM 调用失败：{e}") from e

        raise RuntimeError(f"云端 LLM 多次重试后仍失败：{last_err}")

    # ----------------------------------------------------------
    def _post_json(self, url: str, body: dict, headers: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", errors="ignore")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            # 5xx 与 429 视为可重试；4xx 一般是配置/鉴权错误，直接抛
            if e.code in (429, 500, 502, 503, 504):
                raise _Retryable(f"HTTP {e.code}：{err_body[:200]}") from e
            raise RuntimeError(f"HTTP {e.code}：{err_body[:500]}") from e
        except urllib.error.URLError as e:
            raise _Retryable(f"网络错误：{e.reason}") from e
        except TimeoutError as e:
            raise _Retryable(f"超时（{self.timeout}s）：{e}") from e

    @staticmethod
    def _extract_text(payload: dict) -> str:
        """从 OpenAI 兼容响应里抠出文本内容。

        正常路径：choices[0].message.content。
        某些 reasoning 接口会单独给一个 reasoning_content，我们只取最终
        message.content，不要 reasoning 段。
        """
        if not payload:
            return ""
        choices = payload.get("choices") or []
        if not choices:
            # 有些厂商在错误时返回 {"error": {...}}
            err = payload.get("error") or {}
            if err:
                msg = err.get("message") or err.get("type") or str(err)
                raise RuntimeError(f"API 错误：{msg}")
            return ""
        msg = (choices[0] or {}).get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            # 兼容部分多模态返回的 content 是 list of parts
            buf = []
            for part in text:
                if isinstance(part, dict) and part.get("type") == "text":
                    buf.append(part.get("text") or "")
            text = "\n".join(buf)
        return (text or "").strip()


class _Retryable(Exception):
    """内部信号：这次失败值得重试。"""

"""nova 的大语言模型访问层。

历史上这个类叫 `LocalLLM`——v1.1 之前它只支持 `llama_cpp` 跑本地 GGUF。
v1.2 起它变成一个"路由器"：

    NovaConfig.llm_backend = "local"   →  llama_cpp 跑本地 GGUF
    NovaConfig.llm_backend = "openai"  →  OpenAI 兼容的 HTTP 端点
                                            （DeepSeek / 通义 / 豆包 /
                                             Moonshot / 智谱 / OpenAI / ...）

对调用方（mind.py / executive.py）来说接口完全一样：

    llm = LocalLLM(cfg)
    text = llm.chat(system, user, max_tokens=...)

这意味着 v1.0/v1.1 写的所有 nova 代码不用改一行就能直接换到云端。

不预先 import llama_cpp 也不预先 import urllib 路径——按需 lazy import，
"只用云端的人不必装 llama-cpp-python，反之亦然"。
"""
from __future__ import annotations

from typing import Optional

from .config import NovaConfig


# ============================================================
# 本地 GGUF（llama.cpp）实现
# ============================================================
class _LlamaCppLLM:
    """原 v1.1 的 LocalLLM 实现，原封不动搬过来。"""

    def __init__(self, cfg: NovaConfig):
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise ImportError(
                "本地模型后端需要 llama-cpp-python。\n"
                "  pip install -r requirements-local.txt\n"
                "或者切换到云端后端：NOVA_LLM_BACKEND=openai。"
            ) from e

        if not cfg.model_path:
            raise RuntimeError(
                "NOVA_LLM_BACKEND=local 但 NOVA_MODEL_PATH 没设。\n"
                "请把环境变量指向一个本地 GGUF 文件，或者运行 launcher.py 的配置向导。"
            )

        self.cfg = cfg
        self.llm = Llama(
            model_path=cfg.model_path,
            n_ctx=cfg.n_ctx,
            n_gpu_layers=cfg.n_gpu_layers,
            flash_attn=cfg.flash_attn,
            verbose=False,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            top_k=cfg.top_k,
            min_p=cfg.min_p,
            presence_penalty=cfg.presence_penalty,
        )

    def chat(self, system: str, user: str,
             max_tokens: Optional[int] = None) -> str:
        """ChatML 格式调用一次。"""
        prompt = (
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        out = self.llm(
            prompt=prompt,
            max_tokens=max_tokens or self.cfg.max_tokens,
            stop=list(self.cfg.stop_tokens),
            echo=False,
        )
        return out["choices"][0]["text"].strip()


# ============================================================
# 对外的 LocalLLM —— 路由器
# ============================================================
class LocalLLM:
    """nova 的 LLM 访问入口。

    名字保留是为了向后兼容：v1.0/v1.1 的代码 `from .llm import LocalLLM`
    依然能用。内部根据 cfg.llm_backend 选实际实现。

    Backend 取值（不区分大小写，前后空格忽略）：
      * "local" / "llama" / "llama_cpp" / "gguf"  → 本地 GGUF
      * "openai" / "cloud" / "api" / "http"        → OpenAI 兼容 HTTP
    """

    def __init__(self, cfg: NovaConfig):
        self.cfg = cfg
        backend = (cfg.llm_backend or "local").strip().lower()

        if backend in {"openai", "cloud", "api", "http", "remote"}:
            from .cloud_llm import OpenAICompatLLM
            self._impl = OpenAICompatLLM(cfg)
            self.backend = "openai"
            print(
                f"🌐 LLM 后端：云端 {cfg.llm_api_base}"
                f"（model={cfg.llm_api_model}）"
            )
        elif backend in {"local", "llama", "llama_cpp", "llamacpp", "gguf"}:
            self._impl = _LlamaCppLLM(cfg)
            self.backend = "local"
            print(f"💻 LLM 后端：本地 GGUF（{cfg.model_path}）")
        else:
            raise ValueError(
                f"未知 llm_backend：{cfg.llm_backend!r}。"
                "支持的值：'local' 或 'openai'。"
            )

    def chat(self, system: str, user: str,
             max_tokens: Optional[int] = None) -> str:
        return self._impl.chat(system, user, max_tokens=max_tokens)


# 工厂函数（推荐新代码用）
def make_llm(cfg: NovaConfig) -> LocalLLM:
    """显式工厂——语义比 `LocalLLM(cfg)` 更准。"""
    return LocalLLM(cfg)

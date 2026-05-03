"""本地大模型包装器。

完全照搬 llama_gateway.py 里的加载方式，唯一区别是支持 system / user
两段式提示词，方便给 nova 注入"她是谁"。
"""
from __future__ import annotations

from typing import Optional

from .config import NovaConfig


class LocalLLM:
    def __init__(self, cfg: NovaConfig):
        from llama_cpp import Llama
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
        """ChatML 格式调用一次。

        <|im_start|>system
        ...
        <|im_end|>
        <|im_start|>user
        ...
        <|im_end|>
        <|im_start|>assistant
        """
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

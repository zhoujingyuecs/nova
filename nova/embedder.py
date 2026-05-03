"""嵌入器（Embedder）：把文字翻译成空间中的形状向量。

我们不自己造一个嵌入模型，直接借用 sentence-transformers 上现成的
轻量模型。BGE-small-zh 中文表现非常好，仅 100MB 左右，跑在 CPU
上每条句子毫秒级，不会和 llama_cpp 抢显存。

如果你需要中英混合，把 NovaConfig.embedding_model 换成
'BAAI/bge-m3' 即可（约 2GB，多语言）。
"""
from __future__ import annotations

import numpy as np

from .config import NovaConfig


class Embedder:
    def __init__(self, cfg: NovaConfig):
        # 延迟导入，sentence_transformers 启动较慢
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(cfg.embedding_model, device=cfg.embedding_device)
        self.dim = self.model.get_sentence_embedding_dimension()

    def embed(self, text: str) -> np.ndarray:
        v = self.model.encode(
            text if text.strip() else " ",
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return v.astype(np.float32)

    def embed_batch(self, texts: list) -> np.ndarray:
        safe = [t if t.strip() else " " for t in texts]
        vs = self.model.encode(
            safe,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=32,
        )
        return vs.astype(np.float32)

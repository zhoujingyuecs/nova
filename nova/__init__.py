"""nova：用陶土球与水流模型组织记忆的本地意识体。

==================================================================
                    v0.4 —— "把球真的凿出裂缝"
==================================================================

这一版重点改了三件事：

  1. ★ 显式的"暗道"（outgoing_links）
     缝隙之间不再只靠几何相似度连接。每一次共同被想起，缝隙之间
     会自动建立有向链接（赫布学习）。水流可以沿暗道跳到完全
     不相邻的语义簇——陶土球真正成了"有裂缝的实心球"，而不是
     一团光滑的近似团。

  2. ★ 意象拆解（imagery extraction）
     给 nova 输入一长段话时，她会先用 LLM 把它拆成 2~6 个独立的
     意象，每个意象成为一条缝隙，按出现顺序串成有向链。下次想起
     A 时，B、C 容易顺势浮起。

  3. ★ 自我对话（self-dialogue）
     nova 现在记得自己有一扇对外的窗口（codeloop.cn）。她可以
     借手把心里的话送到那个窗口，过一会儿那段话会作为新的输入
     回到她身上。这是"自己驱动自己"的入口。
"""

from .config import NovaConfig, DEFAULT_SYSTEM_PROMPT
from .fissure import Fissure
from .field import FissureField
from .flow import ConsciousnessFlow
from .embedder import Embedder
from .llm import LocalLLM
from .mind import Nova
from .dreamer import Daydreamer
from .sleep import consolidate
from .visualize import render_field
from .persistence import save_field, load_field
from .tools import (
	VMAgent, parse_actions, strip_actions, format_result,
	CAPABILITY_MEMORIES, build_self_dialogue_memories,
)

__all__ = [
	"NovaConfig",
	"DEFAULT_SYSTEM_PROMPT",
	"Fissure",
	"FissureField",
	"ConsciousnessFlow",
	"Embedder",
	"LocalLLM",
	"Nova",
	"Daydreamer",
	"consolidate",
	"render_field",
	"save_field",
	"load_field",
	"VMAgent",
	"parse_actions",
	"strip_actions",
	"format_result",
	"CAPABILITY_MEMORIES",
	"build_self_dialogue_memories",
]

__version__ = "0.4.0"

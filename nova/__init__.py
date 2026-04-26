"""nova：用陶土球与水流模型组织记忆的本地意识体。"""

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
]

__version__ = "0.2.0"

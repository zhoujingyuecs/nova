"""nova：用陶土球与水流模型组织记忆的本地意识体。v0.8 Self Loop。"""
from .config import NovaConfig, DEFAULT_SYSTEM_PROMPT
from .fissure import Fissure
from .field import FissureField
from .flow import ConsciousnessFlow
from .embedder import Embedder
from .llm import LocalLLM
from .notes import Note, NotesBook
from .mind import Nova
from .dreamer import Daydreamer
from .sleep import consolidate
from .visualize import render_field
from .persistence import save_field, load_field
from .tools import (
    VMAgent,
    parse_actions,
    strip_actions,
    format_result,
    CAPABILITY_MEMORIES,
)
from .self_field import SelfField, SelfFissure
from .drives import DriveSystem, Drive
from .metacognition import Metacognition, InternalAction
from .skills import SkillBook, Skill
from .self_modification import SelfModificationLog, SelfPatch

__all__ = [
    "NovaConfig", "DEFAULT_SYSTEM_PROMPT", "Fissure", "FissureField",
    "ConsciousnessFlow", "Embedder", "LocalLLM", "Note", "NotesBook", "Nova",
    "Daydreamer", "consolidate", "render_field", "save_field", "load_field",
    "VMAgent", "parse_actions", "strip_actions", "format_result", "CAPABILITY_MEMORIES",
    "SelfField", "SelfFissure", "DriveSystem", "Drive", "Metacognition",
    "InternalAction", "SkillBook", "Skill", "SelfModificationLog", "SelfPatch",
]

__version__ = "0.8.0"

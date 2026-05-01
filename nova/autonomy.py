"""Self Loop: 自主行动模式选择。

这里不给 nova 无限行动权，只把 dream_step 从“随机走神”改成由 drive
和 self field 选择的内向活动：自我维护、目标推进、好奇探针或自由联想。
"""
from __future__ import annotations


def choose_autonomy_mode(drive_system, self_field=None) -> str:
    try:
        return drive_system.choose_mode()
    except Exception:
        return "free_dream"


def build_dream_header(mode: str) -> str:
    if mode == "self_refresh":
        return "[这次不是随便飘走，而是自我维护：确认我是谁、我刚才在做什么、下一步该守住什么。]\n\n"
    if mode == "goal_pursuit":
        return "[这次内向活动要推进一个未完成目标：先想清下一步可执行动作，不要空泛抒情。]\n\n"
    if mode == "curiosity_probe":
        return "[这次内向活动来自好奇：围绕一个没弄清的问题探一下，但不要无限发散。]\n\n"
    if mode == "skill_consolidation":
        return "[这次内向活动用于沉淀技能：从刚才的成功或失败里抽出下次可复用的做法。]\n\n"
    return "[这次是自由走神，但主意识仍在：念头可以飘，不能把我带散。]\n\n"

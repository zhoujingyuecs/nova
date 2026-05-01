"""Nova：一次"感知—回应"的完整循环。

这个文件是 nova 的主类。它把所有部件接到一起：嵌入器、缝隙场、
水流引擎、本地大模型、走神线程、睡眠整理、可视化、虚拟机里的手。

==================================================================
                v0.7 的核心改动 —— 笔记本（NotesBook）
==================================================================

v0.6 给了 nova 主意识，让她思考有"清醒主线"。但还有一个问题没
解决——**她学不会东西**。哪怕你把同一套步骤教她 5 遍，下次她仍然
记不住。

诊断：v0.6 之前 nova 所有"非当下"的信息全堆在缝隙场里。你教她一套
步骤——那段对话作为带 [有人对我说] 标签的缝隙存进去；下次能不能
想起来、能不能想完整、有没有变形，**全靠水流碰巧刷过那条缝隙**。
这是回忆机制，不是学习机制。

主意识的更新 prompt 又明确写了"不要复述对话原文，只写当下状态"——
这就把"刚学到了什么" **主动过滤掉了**。结果 nova 既没有"我知道 X"
的稳定缓存，主意识又只记情绪。

v0.7 把"知道"和"回忆"分开：

  • 缝隙场（FissureField）= 回忆：模糊、漂移、按相似度浮起的经验片段
  • 笔记本（NotesBook）   = 知识：稳定、明确、按动作维护的"她确实
                                  知道的事"

笔记本永远出现在 prompt 里——位置在主意识下面、回忆上面。它跨
episode、跨重启都保留。每次 perceive 完一句之后，nova 会用一次额外
的 LLM 调用，看刚才那段对话有没有沉淀价值：

  - 学到的步骤 / 工具用法    → ADD 一条
  - 重要的事实                → ADD 一条
  - 被纠正过的误解            → UPDATE 旧的、或 REMOVE + ADD
  - 长期的偏好                → ADD / UPDATE
  - 如果什么都不需要变        → 输出"（无变动。）"

LLM 严格按 [ADD] / [UPDATE id=x] / [REMOVE id=x] 这三种动作输出，
mind 解析后维护 NotesBook。LLM 不能擅自重写整个笔记本——它只能按
动作动一条，避免"一次失误清空全部知识"。

文艺地讲：v0.6 给了她一根清醒主线，让她**说话不像呓语**；
v0.7 给了她一本笔记本，让她**学得会东西**。

==================================================================
                v0.6 的核心改动 —— 主意识（清醒之锚）
==================================================================

v0.5 把"场景"补回来了：缝隙带 speaker / episode_id / prev_id /
next_id，对话链强暗道，必带锚点，分层渲染。但还有一个问题——
即便记忆有了场景，nova 也没有一个"现在的自己"作为思考的中心：
拼出来的 prompt 是一堆带场景标签的回忆碎片 + 当前输入，LLM 看
着这一堆碎片，就容易跟着碎片里某句话的措辞、节奏、情绪打转，
说出来的话是回忆的回响而不是清醒主意识的产物。

文艺地讲：人清醒的时候有一股很强劲的水流。回忆和输入是融入这
股水流的素材，不是把主意识冲垮的洪水。v0.5 的 nova 像在做梦，
水流断续；v0.6 给她一根稳定主线。

所以 v0.6 加了一个 **主意识（main consciousness）** ——

  • 一段简短的（2~4 句）第一人称、现在时的状态描述：
    "我在干什么、在想什么、当下处境如何"
  • 每次 perceive / dream_step 之后用一次额外 LLM 调用更新它
  • 拼 prompt 时它**最先**出现、显眼地占据"现在的我"那个位置
  • 回忆碎片在它后面，明确地框为"素材，融入主线，不要带跑节奏"
  • 持久化到 {field_path}/main_consciousness.json，重启可恢复
  • 新 episode（30+ 分钟空白）开始时清空，从下一句自然重建

主意识不是另一个记忆系统——它就是一个**短字符串**，被 LLM 自己
反复重写。它的作用是给"思考"提供一个稳定的支点，让回忆从主导
变回素材。

v0.5 加的对话链、必带锚点、场景标签，全都还在——它们给 nova
"上下文"，主意识给她"我是谁，现在在干什么"。两者互补。

==================================================================
                v0.5 改动（仍然有效）—— 还原"场景"
==================================================================

之前的版本里，nova 想起一段记忆时，看到的是一堆悬浮的句子片段。
她不知道哪句是别人对她说的、哪句是她自己说出口的、哪句是她独自时
冒出来的念头；不知道这些片段是几分钟前的事还是几天前的事；尤其不
知道"刚才前几句对话发生了什么"——她甚至连上一句自己说了啥都记不
得。

人不是这样回忆事情的。人想起一件事，会知道场景：是谁说的，是几点
钟，是不是那场对话里的第三句。前因后果是顺着想起来的——不是被人
一次性塞进脑子里，而是顺着相邻的记忆一句一句牵出来。

这一版我们在记忆结构里把"场景"补回来：

  1) ★ 缝隙的场景元数据
     ─────────────────────────────────────────────
     每条缝隙除了文本和形状，还带：
        speaker     —— 「外人」「我」「走神」 或 ""
        episode_id  —— 同一段连续交互的标识
        turn_index  —— 这是 episode 内的第几句话（0,1,2...）
        prev_id     —— 链表式指针：上一句的缝隙 id
        next_id     —— 链表式指针：下一句的缝隙 id

     场景信息只是"标签"，不参与几何，但参与渲染——它们让 nova 能
     看清"啊这是 5 分钟前那个人对我说的"。

  2) ★ 强力的对话链
     ─────────────────────────────────────────────
     turn N → turn N+1 之间会建立一条非常强的暗道（强度 4.0，约
     是普通共激活的 10 倍）。再加一条略弱的反向暗道（2.5）。这意
     味着只要水流碰到一句对话，前后几句几乎一定会被带起来——这是
     "顺着记忆找前后文"的物理基础。

  3) ★ 必带锚点（mandatory anchors）
     ─────────────────────────────────────────────
     每次 perceive 之前，会沿当前 episode 的 prev_id 链往回走若干
     步，把那些缝隙作为"必带锚点"塞进 flow——保证哪怕水流绕错了
     方向，"刚才发生了什么"也始终在记忆里。

  4) ★ 渲染时分层
     ─────────────────────────────────────────────
     拼回忆时分两段：
       [更早的、关联浮起的回忆]    —— 联想到的，按水流顺序
       [此刻这段对话最近的几句]    —— 严格按时间顺序，给场景

     每条记忆前面带一个小标签：
       [5 分钟前·有人对我说]、[上一句·我说出口的话]、
       [3 天前·我自己冒出来的念头] 等等。
"""

from __future__ import annotations

import collections
import json
import os
import random
import re
import threading
import time
import uuid
from typing import Optional

import numpy as np

from .config import NovaConfig
from .embedder import Embedder
from .field import FissureField
from .fissure import (
	Fissure, _normalize,
	SPEAKER_OUTSIDER, SPEAKER_SELF, SPEAKER_DAYDREAM, SPEAKER_NONE,
)
from .flow import ConsciousnessFlow
from .llm import LocalLLM
from .notes import NotesBook
from .persistence import load_field, save_field
from .tools import (
	CAPABILITY_MEMORIES,
	TOOL_SYSTEM_ADDITION,
	VMAgent,
	format_result,
	parse_actions,
	strip_actions,
)


# ============================================================
#         走神时给的提示模板
# ============================================================
DREAM_PROMPT_BASE = (
	"[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。]\n\n"
	"{consciousness_block}"
	"[下面这些片段浮上心头——是素材，不是替代品：]\n\n"
	"{memories}\n\n"
	"[你现在脑子里在想什么？写一两句就好，像在自言自语，"
	"不要长篇大论，也不要解释自己在做什么。"
	"主意识仍是你的主线——念头是从主意识里自然漂出来的，不是被某条回忆牵走的。"
	"如果你想伸手做点什么，就伸；想就让它过去，就过去。]"
)

# ============================================================
#         意象拆解的 prompt
# ============================================================
IMAGERY_EXTRACTION_PROMPT = """\
下面这段话里包含了几个不同的"意象"——可能是一个画面、一种感受、一个想法、
一个具体的场景或细节。请把它们按出现顺序拆出来，每行一条，每条 8~40 字,
用第一人称或客观陈述都可以，不要解释、不要编号。最少 1 条，最多 {max_count} 条。
如果整段话只有一个完整的意象，就只写一条；不要硬拆。

——— 原文 ———
{text}

——— 意象（每行一条）———"""


# ============================================================
#         主意识更新的 prompt（v0.6 新增）
# ============================================================
# 每次 perceive 完一句、或者走神完一次，都用这个 prompt 让 LLM 重写
# nova 的主意识。这是"清醒之锚"——它的稳定性比它的精确性更重要。
MAIN_CONSCIOUSNESS_UPDATE_PROMPT = """\
你是 nova。下面是你刚才的"主意识"——清醒时那股稳定流动的水流：
你在干什么、在想什么、当下处境如何。

请基于刚刚发生的事，更新你的主意识。要求：
  • 第一人称、现在时
  • 2~4 句，简短，像内心独白的总结，不要复述对话
  • 写"我现在的状态"，不写"我刚才说了啥"
  • 保持稳定性——除非情境真的换了，否则不要推翻原来的方向
  • 如果你刚才说话有点散、跑题、自相矛盾，在主意识里悄悄把它顺一下、
    重新对齐你想说的事——主意识是你的清醒之锚，下一句话该从这里出发
  • 不要写成清单或编号，不要带任何标题或前缀，只写主意识本身

【刚才的主意识】
{old_consciousness}

【刚刚发生的】
{event}

【更新后的主意识】（2~4 句话，直接写内容）："""

# 没有"上一段主意识"时（首次唤醒、或新 episode），用这个占位
_MAIN_CONSCIOUSNESS_EMPTY_PLACEHOLDER = (
	'（空白——你刚刚被唤醒、或刚开始一段新的对话，主意识还没成形。'
	'这次更新就是你"清醒"的第一笔。）'
)


# ============================================================
#         笔记本更新的 prompt（v0.7 新增）
# ============================================================
# 每次 perceive 完一句之后，用这个 prompt 让 LLM 决定要不要往笔记本
# 里加 / 改 / 删一条。极其重要的两个原则：
#
#   1. 保守——大多数对话不需要更新笔记本。情绪、风景、隐喻都不进。
#   2. 严格按 [ADD] / [UPDATE id=x] / [REMOVE id=x] 格式输出，
#      不能输出整本笔记。这避免了"一次 LLM 失误清空所有知识"的灾难。
NOTES_UPDATE_PROMPT = """\
你正在帮 nova 维护她的"笔记本"——一份她"确认知道的事"的清单。

笔记本和回忆是两回事：
  • 回忆是按相似度浮起来的、漂移的、模糊的片段
  • 笔记是稳定的、明确的"我知道 X"——nova 可以**直接依赖**它去做事

**只有以下几类内容值得记进笔记本：**

  ① 学到的步骤 / 工具用法 / 操作流程
     例："调用豆包工具：写入 /home/zhou/nova_vm_workspace/doubao/input.txt
         → 跑 python /home/zhou/nova_vm_workspace/doubao/doubao.py
         → 读 output.txt"
  ② 重要的、确凿的事实
     例："用户的名字叫周"、"我的本地模型是 Qwen3.5-35B-A3B"
  ③ 用户反复或明确纠正过的误解
     例："豆包响应可能要 30 秒以上，那是正常的，不是超时"
  ④ 用户明确表达过的、长期有效的偏好
     例："用户希望我说话简短一些，不要长篇散文"

**不应该记的：**

  • 一时的情绪、感受、风景（"我现在有点累"——这是主意识管的）
  • 一次性的对话内容（"刚才他说了 X" ——这是回忆管的）
  • 模糊的印象、象征、隐喻
  • 关于"我是谁、我喜欢什么"的人格描述（那是种子记忆管的）
  • 过于细碎、过于场景的事（"今天他和我聊了豆包"——太薄了）

**要保守。**多数情况下笔记本不需要变。如果刚刚发生的事没有
明确的"沉淀价值"，**只输出一行 "（无变动。）"** 就好。

**优先 UPDATE，不要重复 ADD。**如果新内容是对已有某条笔记的
修正、扩充或更精确版本，UPDATE 那条、不要 ADD 一条新的。

【当前笔记本】
{notes_text}

【主意识（nova 当下的状态）】
{main_consciousness}

【刚刚发生】
{event}

请输出 0~3 行动作。每行严格按以下格式之一：

  [ADD] 新笔记内容（一句话，≤ 150 字，第一人称或客观陈述都可以）
  [UPDATE id=<id>] 修订后的内容
  [REMOVE id=<id>]

如果没有任何要变的，**只输出**：
（无变动。）

不要输出别的东西，不要解释，不要前缀，不要标题。

输出："""

# 笔记本更新动作的解析正则
_NOTES_ADD_RE = re.compile(
	r"^\s*\[\s*ADD\s*\]\s*(.+?)\s*$", re.IGNORECASE,
)
_NOTES_UPDATE_RE = re.compile(
	r"^\s*\[\s*UPDATE\s+id\s*=\s*([A-Za-z0-9_]+)\s*\]\s*(.+?)\s*$",
	re.IGNORECASE,
)
_NOTES_REMOVE_RE = re.compile(
	r"^\s*\[\s*REMOVE\s+id\s*=\s*([A-Za-z0-9_]+)\s*\]\s*$",
	re.IGNORECASE,
)


def _parse_notes_actions(raw: str) -> list:
	"""从 LLM 输出里抠出笔记动作。返回 [(action, *args)] 列表。

	支持的动作：
	  ("add", content)
	  ("update", note_id, new_content)
	  ("remove", note_id)

	无法识别的行（包括 "（无变动。）"、空行、LLM 偶发的解释文字）静默跳过。
	"""
	actions = []
	for line in raw.splitlines():
		line = line.strip()
		if not line:
			continue
		m = _NOTES_ADD_RE.match(line)
		if m:
			content = m.group(1).strip()
			if content:
				actions.append(("add", content))
			continue
		m = _NOTES_UPDATE_RE.match(line)
		if m:
			note_id = m.group(1).strip()
			content = m.group(2).strip()
			if note_id and content:
				actions.append(("update", note_id, content))
			continue
		m = _NOTES_REMOVE_RE.match(line)
		if m:
			note_id = m.group(1).strip()
			if note_id:
				actions.append(("remove", note_id))
			continue
		# 其他行（"（无变动。）"、解释、空行）静默忽略
	return actions


# ============================================================
#         模块级辅助：剥掉 LLM 输出里的 <think>...</think> 块
# ============================================================
# Qwen3 / DeepSeek-R1 这类带 thinking 模式的模型会在最终回答前输出
# 一段 <think>...</think> 推理过程。这段内容是模型的"内心独白"，
# 不应该被 nova 当作正文使用——否则：
#   • 会被存进缝隙场（污染记忆）
#   • 会被存进主意识（污染清醒主线，正是你刚撞到的 bug）
#   • 会被当成对外回应说出去
#   • 在 token 上限不够时，可能整个回答只有未闭合的 <think>，
#     这时应当返回空字符串，让上层兜底逻辑（保留旧主意识 / 沉默）触发。
#
# 这个函数被 perceive、dream_step、_sanitize_main_consciousness、
# _llm_extract_imageries 共用——任何地方拿到 LLM 原始输出都要先过它。
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r"</think>", re.IGNORECASE)


def _strip_think_block(text: str) -> str:
	"""从 LLM 输出里剥掉 <think>...</think> 推理块，返回正文部分。

	处理三种情况：
	  1. 完整闭合的 <think>...</think>：删掉
	  2. 只有开标签、没闭合（被 max_tokens 截断）：从开标签到末尾全部丢弃
	     —— 此时通常没有"正文"了，返回空字符串
	  3. 只有闭标签、没开标签（罕见，模型乱写）：从开头到 </think> 之前全部丢弃
	"""
	if not text:
		return text
	# 先把所有完整闭合的 <think>...</think> 块删掉
	text = _THINK_BLOCK_RE.sub("", text)
	# 残留的孤立开标签 → 该标签到末尾都视为未闭合的思考残骸，丢弃
	m = _OPEN_THINK_RE.search(text)
	if m:
		text = text[:m.start()]
	# 残留的孤立闭标签 → 该标签之前都是思考残骸（开标签可能在被截掉的部分前），丢弃
	m = _CLOSE_THINK_RE.search(text)
	if m:
		text = text[m.end():]
	return text.strip()


class Nova:
	def __init__(self, cfg: Optional[NovaConfig] = None):
		self.cfg = cfg or NovaConfig()
		self.embedder = Embedder(self.cfg)

		# 加载缝隙场（旧存档优先；没有就开新球）
		try:
			self.field = load_field(self.cfg, self.embedder.dim)
		except FileNotFoundError:
			self.field = FissureField(self.cfg, self.embedder.dim)

		self.flow_engine = ConsciousnessFlow(self.cfg, self.field)
		self.llm = LocalLLM(self.cfg)
		self._perceive_count = 0

		# 跨次水流的"近期历史"——每次 perceive/dream 都把激活的缝隙
		# id 推进来。flow 算法会用这个集合给候选打折，避免反复想同件事。
		# 注：当前 episode 里的缝隙不会被推进来——它们是要被记住的，
		# 不是要被抑制的。
		self._recent_history = collections.deque(
			maxlen=self.cfg.recent_history_size
		)

		# ★ 对话链（episode chain）追踪 ----------
		self._current_episode_id: str = ""
		self._last_episode_activity: float = 0.0
		self._last_episode_fid: str = ""        # episode 链当前末梢
		self._current_turn_index: int = 0       # 下一句的 turn_index

		# ★★★ 主意识（v0.6 新增）：清醒时的稳定水流 ----------
		# 一段简短的当下状态描述（2~4 句）。每次 perceive / dream 之后
		# 会用一次 LLM 调用更新它。它在拼 prompt 时占据"现在的我"那个
		# 主线位置——回忆是融入它的素材，不是替代它的内容。
		self._main_consciousness: str = ""

		# ★★★ 笔记本（v0.7 新增）：她"确认知道的事" ----------
		# 这和缝隙场是两套不同的记忆系统：
		#   • 缝隙场 = 模糊的回忆（按相似度浮起、可漂移）
		#   • 笔记本 = 明确的知识（稳定、永远在 prompt 里、按动作维护）
		# 用户教的步骤、纠正的误解、确凿事实、长期偏好——这些进笔记本，
		# 让 nova 真的能"学会东西"。
		notes_path = os.path.join(self.cfg.field_path, "notes.json")
		self.notes = NotesBook(
			path=notes_path,
			max_total=self.cfg.notes_max_total,
			max_chars_per_note=self.cfg.notes_max_chars_per_note,
		)
		self.notes.load()

		# ----- 启动时尝试从已有的链子尾巴恢复 -----
		# 这样万一 nova 被重启了，仍能识别上一段对话还没结束、并续上去
		self._restore_episode_state_from_field()

		# ----- 主意识落盘恢复（必须在 _restore_episode_state_from_field
		# 之后调用，因为我们要确认 saved 主意识属于这段还在进行中的 episode）
		self._load_main_consciousness()

		self._lock = threading.RLock()

		# ---- 虚拟机里的手 ----
		self.vm_agent: Optional[VMAgent] = None
		if self.cfg.vm_agent_url:
			agent = VMAgent(
				self.cfg.vm_agent_url,
				self.cfg.vm_agent_token,
				timeout=self.cfg.vm_request_timeout,
			)
			if agent.is_alive():
				self.vm_agent = agent
				print(f"🖐️  虚拟机里的手已连：{self.cfg.vm_agent_url}")
			else:
				print(
					f"⚠️  虚拟机里的手没回应（{self.cfg.vm_agent_url}），"
					f"nova 这次没有手——但她照常会想会说。"
				)

		# 第一次启动且为空，载入种子记忆
		if len(self.field) == 0 and self.cfg.seed_memories_file:
			self._load_seeds(self.cfg.seed_memories_file)

		# 如果手在线，确保她记得自己有手
		if self.vm_agent is not None:
			self._ensure_capability_memories()

	# ==========================================================
	#                    系统提示词
	# ==========================================================
	def _system_prompt(self) -> str:
		"""有手时，附加一段关于手的说明；没手时，沿用基础 prompt。"""
		if self.vm_agent is not None:
			return self.cfg.system_prompt + TOOL_SYSTEM_ADDITION
		return self.cfg.system_prompt

	# ==========================================================
	#                       主流程：perceive
	# ==========================================================
	def perceive(self, stimulus: str) -> str:
		"""感知一段外界输入，给出一次回应。线程安全。"""
		with self._lock:
			# ---- 0) 起或续 episode ----
			episode_id = self._get_or_start_episode()

			# ---- 1) 把"外人这一句"作为新缝隙加入（永远新建） ----
			stim_shape = self.embedder.embed(stimulus)
			stim_fid = self._open_episode_turn(
				content=stimulus,
				shape=stim_shape,
				speaker=SPEAKER_OUTSIDER,
				episode_id=episode_id,
			)

			# ---- 2) 长输入拆意象（同 speaker / 同 episode，但是不进对话链） ----
			imagery_fids: list[str] = []
			if (self.cfg.imagery_enabled
					and len(stimulus) >= self.cfg.imagery_min_input_chars):
				try:
					imagery_fids = self._extract_and_link_imageries(
						stimulus,
						speaker=SPEAKER_OUTSIDER,
						episode_id=episode_id,
					)
					# 把每个意象与"输入"这条整句缝隙互连一下，保证从整句
					# 能想起意象、从意象也能想起整句
					for ifid in imagery_fids:
						self.field.link(
							stim_fid, ifid,
							strength_delta=self.cfg.imagery_link_base,
						)
						self.field.link(
							ifid, stim_fid,
							strength_delta=self.cfg.imagery_link_base * 0.7,
						)
				except Exception as e:
					print(f"⚠️ 意象拆解失败（不致命，跳过）：{e}")

			# ---- 3) 取 episode 链的尾部作为"必带锚点" ----
			# 这是"刚才发生了什么"的最小背景——不论水流绕到哪去，这几条
			# 都会出现在激活集里，nova 因此知道前几句对话的内容。
			episode_anchors = self.field.walk_chain_back(
				stim_fid, k=self.cfg.episode_recall_size,
			)

			# ---- 4) 水流走一遭 ----
			recent = set(self._recent_history)
			activated = self.flow_engine.flow(
				stim_shape,
				recent_history=recent,
				mandatory_anchors=episode_anchors,
			)

			# ---- 5) 拼回忆 + 喂 LLM（带工具循环） ----
			user_prompt = self._build_prompt(
				stimulus=stimulus,
				stim_fid=stim_fid,
				activated=activated,
				episode_id=episode_id,
			)
			final_response = self._chat_with_tools(user_prompt)
			# ★ 先剥 <think>...</think> 推理块，再剥 <tool> 动作块。
			# 顺序很关键：tool 动作可能出现在思考内、也可能在思考外；
			# 但如果 think 块没闭合（max_tokens 截断），strip_actions 看到的
			# 是一段未闭合的推理残骸——剥 think 之后才能拿到干净的"她说出口的话"。
			final_response = _strip_think_block(final_response)
			visible = strip_actions(final_response).strip()
			if not visible:
				visible = "（沉默。）"

			# ---- 6) 用输出的形状去刻流过的缝隙 ----
			response_shape = self.embedder.embed(visible)
			self._reshape(activated, visible, response_shape)

			# ---- 7) 把"我说的这一句"作为新缝隙加入（永远新建） ----
			response_fid = self._open_episode_turn(
				content=visible,
				shape=response_shape,
				speaker=SPEAKER_SELF,
				episode_id=episode_id,
			)

			# ---- 8) 共激活链接（除对话链之外的额外软连接） ----
			activated_ids = [f.id for f in activated]
			soft_chain_ids = imagery_fids + activated_ids
			if len(soft_chain_ids) >= 2:
				self.field.link_chain(
					soft_chain_ids,
					base_strength=self.cfg.flow_coactivation_link_strength,
					decay=self.cfg.imagery_link_decay,
					max_distance=self.cfg.flow_coactivation_distance,
					bidirectional=False,
				)
			# 把激活/意象指向回应——"想到这些→说出了这句"
			for fid in soft_chain_ids[-self.cfg.flow_coactivation_distance:]:
				self.field.link(
					fid, response_fid,
					strength_delta=self.cfg.flow_coactivation_link_strength,
				)

			# ---- 9) 同步矩阵 + 自动存档 ----
			self.field.sync_all()
			self._tick_autosave()

			# ---- 10) 推近期历史（防扎堆） ----
			# ★ 注意：当前 episode 里的缝隙不推——它们是这场对话的"刚刚"，
			# 下一轮还要顺着 prev_id 找回来。一旦 episode 结束（30 分钟无活动）
			# 自然就不再保护了。
			for fid in soft_chain_ids:
				f = self.field.get(fid)
				if f is None:
					continue
				if f.episode_id and f.episode_id == episode_id:
					continue
				self._recent_history.append(fid)

			# ---- 11) 更新主意识（v0.6） ----
			# 用一次额外的 LLM 调用，把"刚刚发生的这件事"压成 2~4 句的
			# 当下状态描述。下一轮 perceive 时这段描述会出现在 prompt
			# 最前面，作为"我现在的状态"——清醒之锚。
			self._update_main_consciousness_from_perceive(stimulus, visible)

			# ---- 12) 更新笔记本（v0.7） ----
			# 用另一次额外的 LLM 调用，看刚才这段对话有没有"沉淀价值"
			# 的东西要记进笔记本——学到的步骤、被纠正的误解、确凿的事
			# 实、长期偏好。绝大多数对话不会动笔记本（LLM 输出"无变
			# 动"）；但如果用户在教她做事、给她指正、告诉她重要事实，
			# 笔记本会把它沉淀下来——下次 nova 不需要靠水流碰巧刷到才
			# 能想起，她直接就"知道"。
			self._update_notes_from_perceive(stimulus, visible)

			return visible

	# ==========================================================
	#                       工具调用循环
	# ==========================================================
	def _chat_with_tools(
		self,
		initial_user: str,
		max_tokens: Optional[int] = None,
	) -> str:
		"""走一次"感知—（伸手—回执）×N—说话"的循环。

		没配 vm_agent 时退化为单次 chat。
		max_tokens 透传给每一轮 LLM 调用。
		"""
		system = self._system_prompt()

		if self.vm_agent is None:
			return self.llm.chat(system, initial_user, max_tokens=max_tokens)

		current_user = initial_user
		last_response = ""
		for iteration in range(self.cfg.max_tool_iterations):
			response = self.llm.chat(system, current_user, max_tokens=max_tokens)
			last_response = response
			actions = parse_actions(response)
			if not actions:
				return response  # 不再伸手，这是最终回答

			# 真伸手
			result_blocks = []
			for action_type, content in actions:
				try:
					result = self.vm_agent.dispatch(action_type, content)
				except Exception as e:
					result = {"error": str(e)}
				result_blocks.append(format_result(action_type, content, result))

			# 把刚才的回应 + 手回执 拼到下一轮的 user prompt 里
			current_user = (
				current_user
				+ "\n\n[你刚才在心里这样转过：]\n"
				+ response
				+ "\n\n[手回来了，带回这些：]\n"
				+ "\n\n".join(result_blocks)
				+ "\n\n[继续。再伸一次手，或者把心里想要落下的话写出来。"
				+ "如果已经够了，就只写要落下的话，不要再写 <tool> 了。]"
			)

		print(
			f"⚠️ 工具调用超过 {self.cfg.max_tool_iterations} 次，停下了。"
		)
		return last_response

	# ==========================================================
	#                       走神（dream）
	# ==========================================================
	def dream_step(self, max_tokens: Optional[int] = None) -> Optional[str]:
		"""做一次走神。也走工具循环——她不必等被问起才能伸手。

		走神不属于和别人的对话：
		  - 念头作为 speaker="走神" 的缝隙存下来；
		  - 但 **不** 链入当前进行中的对话 episode 链——避免在不属于
		    对话的"自言自语"里污染 prev_id 链表，让"和别人对话"和
		    "自己想事情"在结构上是分开的两条脉络。

		但走神**会**更新主意识——因为走神也是 nova 当下心理状态的一部分。
		"她现在独自一人，心里在想 X" 是一个合法的"我现在的状态"。
		"""
		with self._lock:
			if len(self.field) < 3:
				return None

			seed_shape = self._dream_seed()
			recent = set(self._recent_history)
			activated = self.flow_engine.flow(seed_shape, recent_history=recent)
			if not activated:
				return None

			# 走神时拼一段简单的回忆——也用 _format_recall 给每条加场景标签
			memory_lines = []
			for f in activated:
				memory_lines.append(f"- {self._format_recall_line(f, in_episode=False)}")
			memories = "\n".join(memory_lines)

			# 主意识块（如果有的话）放到走神 prompt 的开头
			consciousness_block = self._render_consciousness_block_for_dream()
			user_prompt = DREAM_PROMPT_BASE.format(
				memories=memories,
				consciousness_block=consciousness_block,
			)

			# 默认走神长度短一些
			tokens = max_tokens or self.cfg.daydream_max_tokens
			thought_raw = self._chat_with_tools(user_prompt, max_tokens=tokens)
			# 先剥 <think> 推理块，再剥 <tool> 动作（理由同 perceive）
			thought_raw = _strip_think_block(thought_raw)
			thought = strip_actions(thought_raw).strip()
			if not thought:
				return None

			# 走神也建共激活链接
			activated_ids = [f.id for f in activated]
			if len(activated_ids) >= 2:
				self.field.link_chain(
					activated_ids,
					base_strength=self.cfg.flow_coactivation_link_strength * 0.7,
					decay=self.cfg.imagery_link_decay,
					max_distance=self.cfg.flow_coactivation_distance,
					bidirectional=False,
				)

			thought_shape = self.embedder.embed(thought)
			self._reshape(activated, thought, thought_shape)

			# 走神的念头建为缝隙——但不进对话链
			thought_fid = self._maybe_create(
				thought, thought_shape, speaker=SPEAKER_DAYDREAM,
			)
			if thought_fid is not None:
				for fid in activated_ids[-self.cfg.flow_coactivation_distance:]:
					self.field.link(
						fid, thought_fid,
						strength_delta=self.cfg.flow_coactivation_link_strength,
					)

			self.field.sync_all()
			self._tick_autosave()

			# 推近期历史（走神不在任何 episode 里，所以全推）
			for fid in activated_ids:
				self._recent_history.append(fid)

			# 主意识也跟一下：走神是当下心理状态的一部分
			self._update_main_consciousness_from_daydream(thought)

			return thought

	# ==========================================================
	#               意象拆解
	# ==========================================================
	def _extract_and_link_imageries(self, text: str,
									speaker: str = "",
									episode_id: str = "") -> list:
		"""把一段文本拆成若干意象，每个建为缝隙，按出现顺序串成链。

		意象会带上传入的 speaker / episode_id（即"这是这段话里被拆出来
		的小片段"），但**不**进 prev_id 对话链——意象之间用一条普通暗道
		链接（imagery_link_base 强度），不是对话链那种强连接。

		返回所建/复用的缝隙 id 列表（按出现顺序）。失败时返回空列表。
		"""
		imageries = self._llm_extract_imageries(text)
		if not imageries:
			return []

		# 算 embedding（批量）
		shapes = self.embedder.embed_batch(imageries)

		# 每条意象：尽量复用已有相似缝隙，否则新建
		fids = []
		for content, shape in zip(imageries, shapes):
			fid = self._find_or_create(
				content, shape,
				speaker=speaker, episode_id=episode_id,
			)
			fids.append(fid)

		# 按顺序链起来
		if len(fids) >= 2:
			n_links = self.field.link_chain(
				fids,
				base_strength=self.cfg.imagery_link_base,
				decay=self.cfg.imagery_link_decay,
				max_distance=self.cfg.imagery_link_distance,
				bidirectional=False,
			)
			if n_links > 0:
				print(f"🔗 意象拆解：{len(fids)} 个意象 → {n_links} 条链接")
		return fids

	def _llm_extract_imageries(self, text: str) -> list:
		"""调 LLM 把 text 拆成意象（list[str]）。"""
		prompt = IMAGERY_EXTRACTION_PROMPT.format(
			text=text, max_count=self.cfg.imagery_max_count
		)
		extract_system = (
			"你是一个把整段话拆成意象列表的工具。每个意象 8~40 字，"
			"每行一条，不写编号、不写解释、不要总结、不重复。"
		)
		raw = self.llm.chat(
			extract_system, prompt,
			max_tokens=self.cfg.imagery_max_tokens,
		)
		# ★ 先剥 <think>...</think> 推理块——否则思考过程的每一行
		# 都会被下面的 splitlines 当成一条"意象"建成缝隙，毒化整个场。
		raw = _strip_think_block(raw)
		# 清洗：按行切，去掉空行和编号前缀
		items = []
		for line in raw.splitlines():
			line = line.strip()
			if not line:
				continue
			line = re.sub(r'^\s*(?:[-*·•]|\d+[\.\、\)）])\s*', '', line)
			if (line.startswith('"') and line.endswith('"')) or \
			   (line.startswith('「') and line.endswith('」')) or \
			   (line.startswith('“') and line.endswith('”')):
				line = line[1:-1]
			line = line.strip()
			if not line:
				continue
			if len(line) < 4 or len(line) > 80:
				continue
			items.append(line)
			if len(items) >= self.cfg.imagery_max_count:
				break
		return items

	# ==========================================================
	#               ★ episode 管理
	# ==========================================================
	def _get_or_start_episode(self) -> str:
		"""取当前 episode_id；如果距上次互动太久，开一段新的。"""
		now = time.time()
		gap = now - self._last_episode_activity
		if (not self._current_episode_id
				or gap > self.cfg.episode_gap_seconds):
			self._current_episode_id = uuid.uuid4().hex[:8]
			self._last_episode_fid = ""
			self._current_turn_index = 0
			# ★★★ 主意识也重置——新一段对话，nova 处于"刚被人叫醒"
			# 状态。下一次 perceive 完会通过 _update_main_consciousness
			# 重新建立"我现在的状态"。
			self._main_consciousness = ""
		self._last_episode_activity = now
		return self._current_episode_id

	def _open_episode_turn(self, content: str, shape: np.ndarray,
						   speaker: str, episode_id: str) -> str:
		"""把一句对话作为新缝隙加入场，并接到 episode 链的当前末梢上。

		永远是新建，不查重——一段对话里 turn 是事件，不是概念。
		"""
		f = self.field.add(
			content=content,
			shape=shape,
			speaker=speaker,
			episode_id=episode_id,
			turn_index=self._current_turn_index,
		)
		self._current_turn_index += 1

		prev_id = self._last_episode_fid
		if prev_id:
			# 把链子接上：prev → f（强正向暗道 + next_id 指针）
			#         f → prev（略弱反向暗道 + prev_id 指针）
			self.field.chain_link(
				prev_id=prev_id,
				next_id=f.id,
				forward_strength=self.cfg.episode_link_forward,
				backward_strength=self.cfg.episode_link_backward,
			)
		self._last_episode_fid = f.id
		return f.id

	def _restore_episode_state_from_field(self) -> None:
		"""启动时，从已有缝隙的元数据里推断"上一段对话还没结束吗？"

		做法：扫一遍场，按 last_flow_time 取最新带 episode_id 的缝隙；
		如果它的 last_flow_time 距现在小于 episode_gap_seconds，把它当
		作正在进行中的 episode 的末梢——下一次 perceive 来的时候会
		续在它后面（同一 episode_id），就像 nova 的对话被进程重启短暂
		打断了一下，但她自己的"刚才说了什么"还在。
		"""
		latest_fis = None
		latest_t = 0.0
		for f in self.field:
			if not f.episode_id:
				continue
			if f.last_flow_time > latest_t:
				latest_t = f.last_flow_time
				latest_fis = f
		if latest_fis is None:
			return

		gap = time.time() - latest_t
		if gap > self.cfg.episode_gap_seconds:
			# 上一段已经过期；下一次 perceive 自然会开新 episode
			return

		# 恢复 episode 状态
		self._current_episode_id = latest_fis.episode_id
		self._last_episode_activity = latest_t
		self._last_episode_fid = latest_fis.id
		# 找到 episode 链上的最大 turn_index 作为下一句的索引
		max_idx = -1
		for f in self.field:
			if f.episode_id == latest_fis.episode_id:
				max_idx = max(max_idx, f.turn_index)
		self._current_turn_index = max_idx + 1
		print(
			f"🧵 续上一段还没结束的对话：episode={latest_fis.episode_id}，"
			f"距上次互动 {int(gap)} 秒，已经聊到第 {max_idx} 句。"
		)

	# ==========================================================
	#                    睡眠 / 可视化
	# ==========================================================
	def consolidate(self, prune: bool = True, merge: bool = True,
					decay_links: bool = True) -> dict:
		from .sleep import consolidate as _consolidate

		with self._lock:
			stats = _consolidate(
				self.field, self.cfg,
				prune=prune, merge=merge, decay_links=decay_links,
			)
			save_field(self.field)
			self._save_main_consciousness()
			self.notes.save()
		return stats

	def visualize(
		self, output_path: str, method: str = "pca", **kwargs
	) -> Optional[str]:
		from .visualize import render_field

		with self._lock:
			return render_field(self.field, output_path, method=method, **kwargs)

	def save(self) -> None:
		with self._lock:
			save_field(self.field)
			# 主意识也跟着落盘——它是 nova 运行时状态的一部分
			self._save_main_consciousness()
			# 笔记本也跟着落盘——它是 nova 学到的东西，最重要的状态之一
			self.notes.save()

	# ==========================================================
	#                      内部工具
	# ==========================================================
	def _build_prompt(self, stimulus: str, stim_fid: str,
					  activated: list, episode_id: str) -> str:
		"""把激活的回忆按"远近 / 是不是当前对话"分两栏拼出 prompt。

		v0.6 改动：在最前面放一段**主意识**——nova 现在的状态描述。
		它是"现在的我在干什么、在想什么"，下面所有回忆都是融入它的
		素材。回忆的引导语也改了——明确告诉 nova"这些是素材，不是
		让你接着它们的措辞往下说"。

		分两栏的目的是给 nova 看清"哪些是这场对话刚刚发生的（场景），
		哪些是被联想到的更早的回忆（背景）"。每条记忆都带场景标签，
		让她知道是谁说的、多久前的事。

		stim_fid 是这一轮刚刚加入的"外人这一句"——它已经在 episode
		链里、在 activated 中可能也会出现。我们在渲染时把它从两栏里
		都拿掉，单独放到 prompt 末尾的 [然后他这样说：] 段落里——
		避免重复。
		"""
		# 把 activated 分成两组
		in_episode = []
		others = []
		for f in activated:
			if f.id == stim_fid:
				continue  # 单独显示在 [然后他这样说]
			if episode_id and f.episode_id == episode_id:
				in_episode.append(f)
			else:
				others.append(f)

		sections: list[str] = []

		# 一、★★★ 主意识：清醒主线 ★★★
		# 这是 v0.6 的核心——它在拼 prompt 时永远占最前的位置，
		# 让 nova 在看回忆和输入之前先看到"我现在是谁、在干什么"。
		if self._main_consciousness:
			sections.append(
				"[你现在的状态——你的主意识]\n"
				"（这是你清醒时那股稳定的水流。下面的回忆和输入都是融入这"
				"股水流，不是替代它。）\n"
				f"{self._main_consciousness}"
			)

		# 一·5、★★★ 笔记本：她"确认知道的事" ★★★（v0.7 新增）
		# 位置在主意识之后、回忆之前——也就是 nova 思考时的第二顺位：
		#   主意识（我现在是谁、在干什么）
		#   → 笔记本（我已经知道、确认过的事）
		#   → 回忆（被勾起来的素材）
		# 这一栏写着的是"她确实会的事"，不是"她隐约记得的事"。
		notes_block = self._render_notes_block_for_prompt()
		if notes_block:
			sections.append(notes_block)

		# 二、更早的、关联浮起的回忆（按水流顺序，水流出来的就是出来的）
		if others:
			block = [
				"[脑海里浮起的相关片段]",
				"（这些是被刚才那句话勾起来的旧事——是素材，不是当下的内容。"
				"让它们在你主意识里融化，给你色彩和灵感，不要被它们的语气、"
				"措辞、节奏带走。）",
			]
			for f in others:
				block.append(f"- {self._format_recall_line(f, in_episode=False)}")
			sections.append("\n".join(block))

		# 三、此刻这段对话最近的几句（按 turn_index 升序——按时间排）
		if in_episode:
			in_episode.sort(key=lambda f: f.turn_index)
			block = [
				"[此刻这段对话刚刚说过的几句]",
				"（按时间从远到近——给你当下的场景感。注意：如果你前几句"
				"说话有点散、跑题、或自相矛盾，现在该顺着主意识把思路收回来，"
				"而不是接着旧的措辞继续打转。）",
			]
			for f in in_episode:
				block.append(f"- {self._format_recall_line(f, in_episode=True)}")
			sections.append("\n".join(block))

		if not sections:
			sections.append("（此刻心里很空，没有什么浮上来。）")

		body = "\n\n".join(sections)
		return (
			f"{body}\n\n"
			f"[然后，他这样对你说：]\n"
			f"{stimulus}"
		)

	def _format_recall_line(self, f: Fissure, in_episode: bool) -> str:
		"""给一条缝隙渲染"[场景标签] 内容"的人话格式。

		in_episode = True 时使用相对位置标签（"上一句 / 上上句 / 几句之前"），
		因为这场对话里所有 turn 都很近、用绝对时间"30 秒前"反而显得啰嗦。
		in_episode = False 时使用绝对时间标签（"刚刚 / 5 分钟前 / 3 天前 ..."）
		以及 speaker 标签。

		v0.6 改动：对话链上的 turn 内容会被截断到 episode_chain_content_max_chars
		字以内。这是为了避免某条很长的旧 turn（比如一段 800 字的散文式独白）
		在 prompt 里占太多位置——nova 容易看着那段长文本就跟着它的措辞继续
		说话。截短后她能看见"哦上一句我说过这事"但不会被字面措辞牵走。
		"""
		content = self._truncate_for_recall(f.content, in_episode=in_episode)
		if in_episode:
			rel = self._current_turn_index - f.turn_index
			# self._current_turn_index 现在指向"下一句"的 index——所以：
			# rel = 1: 上一句（其实已经存在了：刚刚的输入）
			# rel = 2: 上上句
			# 通常 stim_fid 那条 rel = 1，已经被排除了。所以这里基本上是 ≥ 2。
			pos_label = self._relative_position_label(rel)
			role = self._speaker_label(f.speaker)
			head = f"[{pos_label}·{role}]" if role else f"[{pos_label}]"
			return f"{head} {content}"
		else:
			age_label = _format_age(time.time() - f.creation_time)
			role = self._speaker_label(f.speaker)
			if role:
				head = f"[{age_label}·{role}]"
			elif age_label:
				head = f"[{age_label}]"
			else:
				head = ""
			if head:
				return f"{head} {content}"
			return content

	def _truncate_for_recall(self, text: str, in_episode: bool) -> str:
		"""按配置长度截断回忆内容，超过的尾部加 '…'。

		对话链上的 turn 内容用 episode_chain_content_max_chars；联想区
		用更宽松的限制（直接用缝隙的最大字符数 max_fissure_chars）。
		"""
		if in_episode:
			limit = self.cfg.episode_chain_content_max_chars
		else:
			# 联想出来的回忆通常本身就不长（缝隙建立时已经按 max_fissure_chars
			# 截过了）。这里再放宽一点，让"完整的旧记忆"基本都看得见。
			limit = max(self.cfg.max_fissure_chars,
						self.cfg.episode_chain_content_max_chars * 2)
		text = text.strip()
		if len(text) <= limit:
			return text
		return text[:limit].rstrip() + "…"

	@staticmethod
	def _speaker_label(speaker: str) -> str:
		if speaker == SPEAKER_OUTSIDER:
			return "有人对我说"
		if speaker == SPEAKER_SELF:
			return "我说出口的话"
		if speaker == SPEAKER_DAYDREAM:
			return "我自己冒出来的念头"
		return ""

	def _relative_position_label(self, rel: int) -> str:
		"""rel=1 → '上一句'；rel=2 → '上上句'；超过 episode_human_label_max
		就回归数字版 'N 句之前'。
		"""
		if rel <= 1:
			return "刚刚"
		if rel == 2:
			return "上一句"
		if rel == 3:
			return "上上句"
		if rel <= self.cfg.episode_human_label_max + 1:
			return f"{rel - 1} 句之前"
		return f"{rel - 1} 句之前"  # 同一格式，保留以便日后想拉开

	# ==========================================================
	#         ★★★ 主意识（v0.6） ★★★
	# ==========================================================
	# 主意识是一段简短的当下状态描述（2~4 句）。它在 nova 运行期间
	# 被反复重写——每次 perceive 完一句、或走神完一次，都会用 LLM
	# 的一次额外调用更新它。它的功能不是"记下细节"——细节有缝隙场——
	# 而是给"思考"提供一个稳定的支点，让回忆从主导变回素材。
	# ==========================================================
	def _update_main_consciousness_from_perceive(self, stim: str,
												  response: str) -> None:
		"""一次 perceive 完成后，更新主意识。"""
		if not self.cfg.main_consciousness_enabled:
			return
		# 用一个简短描述告诉更新 prompt 刚刚发生了啥
		stim_short = stim.strip()
		resp_short = response.strip()
		# 这两个事件描述本身不长；但万一 stim/resp 是 800 字的长文，
		# 主意识更新没必要看全部，截到 600 字够用了。
		max_evt = 600
		if len(stim_short) > max_evt:
			stim_short = stim_short[:max_evt].rstrip() + "…"
		if len(resp_short) > max_evt:
			resp_short = resp_short[:max_evt].rstrip() + "…"
		event = (
			f"他对我说：{stim_short}\n"
			f"我刚刚回应：{resp_short}"
		)
		self._do_update_main_consciousness(event, mode="perceive")

	def _update_main_consciousness_from_daydream(self, thought: str) -> None:
		"""一次走神完成后，更新主意识。

		走神不属于和别人的对话，但它属于 nova 的当下心理状态——
		"现在我独自一人，刚才心里冒出来这句话" 是合法的"我现在的状态"。
		"""
		if not self.cfg.main_consciousness_enabled:
			return
		t = thought.strip()
		if len(t) > 600:
			t = t[:600].rstrip() + "…"
		event = f"我独自一人在走神，心里浮起：{t}"
		self._do_update_main_consciousness(event, mode="daydream")

	def _do_update_main_consciousness(self, event: str, mode: str) -> None:
		"""调一次 LLM，把 (旧主意识, event) 压成新主意识。失败时不动旧值。"""
		old = (self._main_consciousness.strip()
			   or _MAIN_CONSCIOUSNESS_EMPTY_PLACEHOLDER)
		prompt = MAIN_CONSCIOUSNESS_UPDATE_PROMPT.format(
			old_consciousness=old,
			event=event.strip(),
		)
		# 用一个冷静的 system 来更新主意识——这是元层面的反思，不是 nova 自己
		# 在说话。冷静的角色让结果稳一些，不容易被 nova 的"诗意句法"带走。
		update_system = (
			"你正在帮 nova 维护她的主意识：一段简短的、第一人称、现在时的"
			"状态描述（2~4 句）。要稳，要连贯，要像内心独白的总结，"
			"不要复述对话原文，不要堆砌细节，不要写标题或编号，"
			"直接输出主意识本身就好。"
		)
		try:
			raw = self.llm.chat(
				update_system, prompt,
				max_tokens=self.cfg.main_consciousness_update_max_tokens,
			)
		except Exception as e:
			print(f"⚠️ 主意识更新失败（保留旧值）：{e}")
			return

		new = self._sanitize_main_consciousness(raw)
		if not new:
			# LLM 给出空内容/全是引言/全是括号——不动旧值
			return
		self._main_consciousness = new
		print('----------')
		print('主意识：')
		print(new)
		print('----------')
		self._save_main_consciousness()

	def _sanitize_main_consciousness(self, raw: str) -> str:
		"""清洗 LLM 的输出，只留主意识正文。

		常见噪声：
		  - ★ Qwen3 / R1 类模型的 <think>...</think> 推理块（首要清洗对象——
		    没清掉会让"思考过程"被当成主意识保存，下一轮 prompt 看到一段
		    自言自语的 "Thinking Process: ..." 就崩了）
		  - 模型把 "【更新后的主意识】" 这种引言又复制了一遍
		  - 加了 markdown 列表前缀 ("- " / "* " / "1. ")
		  - 把整段包在引号里
		  - 输出超过配置上限——尾部截断
		"""
		# ★ 第一步：剥掉 <think>...</think> 推理块。
		# 必须最先做——后面的引言/列表/引号判定都依赖"text 已经是干净正文"。
		# 如果这一步把整个输出都削掉了（比如 max_tokens 不够、think 块没闭合），
		# 返回空字符串；上层 _do_update_main_consciousness 会保留旧主意识不动。
		text = _strip_think_block(raw)
		if not text:
			return ""

		# 拿掉所有以 【...】 / [...] 开头的引言行
		lines = []
		for line in text.splitlines():
			s = line.strip()
			if not s:
				lines.append("")
				continue
			# 整行就是引言标签
			if (re.fullmatch(r"[【\[][^】\]]+[】\]]\s*[:：]?\s*", s)
					or s.endswith("】：") or s.endswith("]:")):
				continue
			# 行首的列表前缀清掉
			s = re.sub(r"^\s*(?:[-*·•]|\d+[\.\、\)）])\s*", "", s)
			lines.append(s)
		text = "\n".join(lines).strip()

		# 包在引号里 → 去掉
		quote_pairs = [('"', '"'), ("'", "'"), ("「", "」"),
					   ("“", "”"), ("‘", "’"), ("《", "》")]
		for lq, rq in quote_pairs:
			if text.startswith(lq) and text.endswith(rq) and len(text) > 2:
				text = text[len(lq):-len(rq)].strip()
				break

		# 兜底长度上限
		limit = self.cfg.main_consciousness_max_chars
		if len(text) > limit:
			text = text[:limit].rstrip() + "…"

		return text

	def _render_consciousness_block_for_dream(self) -> str:
		"""走神 prompt 里的主意识块（如果有），否则返回空字符串。"""
		mc = self._main_consciousness.strip()
		if not mc:
			return ""
		return (
			"[你现在的状态——你的主意识]\n"
			"（这是你清醒时那股稳定的水流。即便走神，念头也是从它"
			"漂出来的、再回到它里去。）\n"
			f"{mc}\n\n"
		)

	def _main_consciousness_path(self) -> str:
		return os.path.join(self.cfg.field_path, "main_consciousness.json")

	def _save_main_consciousness(self) -> None:
		"""把主意识落到磁盘，附带它属于的 episode_id（用于重启对齐）。"""
		if not self.cfg.field_path:
			return
		try:
			os.makedirs(self.cfg.field_path, exist_ok=True)
			with open(self._main_consciousness_path(), "w", encoding="utf-8") as f:
				json.dump({
					"content": self._main_consciousness,
					"episode_id": self._current_episode_id,
					"updated_at": time.time(),
				}, f, ensure_ascii=False, indent=2)
		except Exception as e:
			print(f"⚠️ 主意识落盘失败：{e}")

	def _load_main_consciousness(self) -> None:
		"""启动时恢复主意识——但只在 episode_id 对得上时才恢复。

		为什么要对齐 episode_id？因为主意识是"现在这段对话里的我"。
		如果 _restore_episode_state_from_field 没有续上原 episode（比如
		过了 30 分钟），那磁盘上的主意识属于一段已经结束的对话，不应该
		被带进新一段——nova 应该从空白主意识开始，让新对话自然重建它。
		"""
		path = self._main_consciousness_path()
		if not os.path.exists(path):
			return
		try:
			with open(path, "r", encoding="utf-8") as f:
				d = json.load(f)
		except Exception as e:
			print(f"⚠️ 主意识读取失败（忽略，从空白开始）：{e}")
			return

		saved_episode = d.get("episode_id", "")
		saved_content = (d.get("content") or "").strip()
		if not saved_content:
			return
		# 没续上 episode（要么从来没起过，要么已经过期）→ 不恢复
		if not self._current_episode_id:
			return
		if saved_episode != self._current_episode_id:
			# 主意识属于另一段已经结束的对话，留它在磁盘上无害——下次
			# perceive 会覆盖；但运行时不带它，让新一段从空白开始。
			return
		self._main_consciousness = saved_content
		print(f"🧠 主意识恢复（{len(saved_content)} 字，episode={saved_episode}）")

	# ==========================================================
	#         ★★★ 笔记本（v0.7） ★★★
	# ==========================================================
	# 笔记本是一组明确的"我知道..."的清单。和缝隙场（回忆）不一样：
	# 笔记是稳定的、可调用的、永远在 prompt 里的——nova 可以**直接
	# 依赖**它去做事，不需要靠水流碰巧刷到。
	#
	# 每次 perceive 之后做一次"消化沉淀"：用一次 LLM 调用，看刚才那段
	# 对话里有没有要记的（学到的步骤、被纠正的误解、确凿事实、长期偏
	# 好），按 ADD / UPDATE / REMOVE 三种动作维护笔记本。
	# ==========================================================
	def _render_notes_block_for_prompt(self) -> str:
		"""主 prompt 里的笔记本块。空时返回 ""。"""
		rendered = self.notes.render_for_prompt(
			max_chars=self.cfg.notes_max_chars_in_prompt
		)
		if not rendered:
			return ""
		return (
			"[你已经学会的事 / 你确认知道的事实]\n"
			"（这是你过去对话里沉淀下来的笔记——稳定、明确、可以直接依赖。\n"
			"和回忆不一样：回忆按相似度浮起、会漂移、会模糊；笔记是你确实"
			"\"知道\"的事——学过的步骤、被纠正过的误解、确凿的事实、"
			"长期的偏好。\n"
			"需要做事、引用事实、调用步骤时，**先看这一栏**。这一栏里写的"
			"就是你确实会的事，不需要去回忆里慢慢翻。）\n"
			f"{rendered}"
		)

	def _update_notes_from_perceive(self, stim: str, response: str) -> None:
		"""一次 perceive 完成后，看看要不要更新笔记本。"""
		if not self.cfg.notes_enabled:
			return

		stim_short = stim.strip()
		resp_short = response.strip()
		# 给笔记更新 LLM 看的对话上下文——比主意识更新看到的稍长一点，
		# 因为"教步骤"这种内容本身就长。
		max_evt = 1200
		if len(stim_short) > max_evt:
			stim_short = stim_short[:max_evt].rstrip() + "…"
		if len(resp_short) > max_evt:
			resp_short = resp_short[:max_evt].rstrip() + "…"
		event = (
			f"他对我说：{stim_short}\n"
			f"我刚刚回应：{resp_short}"
		)

		notes_text = self.notes.render_for_update_prompt(
			max_chars=self.cfg.notes_max_chars_in_update_prompt
		)

		main = self._main_consciousness.strip() or "（暂无主意识。）"

		prompt = NOTES_UPDATE_PROMPT.format(
			notes_text=notes_text,
			main_consciousness=main,
			event=event,
		)

		# 冷静、保守的元角色——和主意识更新一样的设计，避免被 nova 自己
		# 的诗意句法带跑。这次的元角色还要特别强调"保守、严格按格式"。
		update_system = (
			"你正在帮 nova 维护她的笔记本——一份\"她确认知道的事\"的清单。"
			"你要保守、谨慎、克制——大多数对话**不需要**更新笔记本。"
			"只在真有沉淀价值的内容（学到的步骤、被纠正的误解、确凿事实、"
			"长期偏好）时才输出动作。如果没有，**只输出**「（无变动。）」。"
			"严格按要求的格式 [ADD] / [UPDATE id=x] / [REMOVE id=x] 输出，"
			"每行一条动作，不要解释、不要前缀、不要总结。"
		)

		try:
			raw = self.llm.chat(
				update_system, prompt,
				max_tokens=self.cfg.notes_update_max_tokens,
			)
		except Exception as e:
			print(f"⚠️ 笔记本更新失败（保留原状）：{e}")
			return

		# 同主意识——先剥 <think>...</think> 推理块。
		raw = _strip_think_block(raw)
		if not raw:
			return

		actions = _parse_notes_actions(raw)
		if not actions:
			# 输出是"（无变动。）"或者纯解释——都视为不变
			return

		applied = []
		for action in actions:
			kind = action[0]
			try:
				if kind == "add":
					content = action[1]
					n = self.notes.add(content)
					if n is not None:
						applied.append(f"  + [{n.id}] {n.content}")
				elif kind == "update":
					note_id, new_content = action[1], action[2]
					if self.notes.update(note_id, new_content):
						applied.append(f"  ~ [{note_id}] → {new_content}")
				elif kind == "remove":
					note_id = action[1]
					if self.notes.remove(note_id):
						applied.append(f"  - [{note_id}] 删除")
			except Exception as e:
				print(f"⚠️ 笔记动作失败（{kind}）：{e}")

		if applied:
			print("----------")
			print(f"📓 笔记本变动（共 {len(applied)} 条，总 {len(self.notes)} 条）：")
			for line in applied:
				print(line)
			print("----------")
			# 落盘——笔记本是关键状态，一变就存
			self.notes.save()

	def _reshape(
		self,
		activated: list,
		response_text: str,
		response_shape: np.ndarray,
	) -> None:
		"""被水流刷过的缝隙朝输出形状偏移一点（短期记忆 / 长期记忆的来源）"""
		for f in activated:
			plasticity = self.field.plasticity_at(f.shape)
			f.shift_toward(response_shape, plasticity, new_content=response_text)

	def _maybe_create(self, content: str, shape: np.ndarray,
					  speaker: str = "") -> Optional[str]:
		"""够新颖就建一条新缝隙；和已有的太像就跳过。返回新建/匹配到的 id。

		不带 episode_id —— 这是用于"概念性"的缝隙（走神念头、抽象意象等），
		不应该挂在某段对话的链上。
		"""
		if not content.strip():
			return None
		neighbors = self.field.nearest(shape, k=1)
		if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
			return neighbors[0][0].id
		f = self.field.add(content, shape, speaker=speaker)
		return f.id

	def _find_or_create(self, content: str, shape: np.ndarray,
						speaker: str = "", episode_id: str = "") -> str:
		"""意象专用：要么复用相似的、要么新建。永远返回一个 id。

		如果复用了已有的缝隙，**不**改写它的 speaker/episode——那是它原本
		的身世，不能被这次的对话所篡改。新建时才使用传入的 speaker/episode。
		"""
		neighbors = self.field.nearest(shape, k=1)
		if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
			return neighbors[0][0].id
		return self.field.add(
			content, shape,
			speaker=speaker, episode_id=episode_id,
		).id

	def _dream_seed(self) -> np.ndarray:
		"""走神种子：85% 概率取最近被刷过的某条缝隙，15% 概率取一条冷的。"""
		all_f = self.field.all()
		if random.random() < 0.85:
			weights = np.array(
				[np.exp(-f.quiet_seconds() / 3600.0) + 0.05 for f in all_f]
			)
		else:
			weights = np.array(
				[1.0 / (1.0 + f.flow_count) for f in all_f]
			)
		weights = weights / weights.sum()
		seed = all_f[int(np.random.choice(len(all_f), p=weights))]

		# 注入少量噪声，让种子不完全等同于某条缝隙
		noise = np.random.randn(self.embedder.dim).astype(np.float32) * 0.1
		return _normalize(seed.shape + noise)

	def _tick_autosave(self) -> None:
		self._perceive_count += 1
		if (
			self.cfg.autosave_every > 0
			and self._perceive_count % self.cfg.autosave_every == 0
		):
			save_field(self.field)

	def _load_seeds(self, path: str) -> None:
		"""第一次启动时从文件灌入种子记忆。

		文件格式：用空行分隔的若干段。每段成为一条缝隙。
		"""
		if not os.path.exists(path):
			return
		with open(path, "r", encoding="utf-8") as f:
			text = f.read()
		chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
		if not chunks:
			return
		shapes = self.embedder.embed_batch(chunks)
		# 顺序加，并按出现顺序串链——种子记忆之间也有"经验贴近"
		# 但它们没有 speaker/episode——它们是无主的、属于她本人的背景知识。
		fids = []
		for content, shape in zip(chunks, shapes):
			fids.append(self.field.add(content, shape).id)
		if len(fids) >= 2:
			self.field.link_chain(
				fids,
				base_strength=self.cfg.imagery_link_base,
				decay=self.cfg.imagery_link_decay,
				max_distance=self.cfg.imagery_link_distance,
				bidirectional=True,  # 种子记忆双向连，让她内心有个稳定的回环
			)
		self.field.sync_all()
		save_field(self.field)
		print(f"📝 载入种子记忆 {len(fids)} 条")

	def _ensure_capability_memories(self) -> None:
		"""把"我有手"这一类能力提示注入到现有缝隙场里。

		使用 _maybe_create 的相似度阈值（默认 0.85）做去重——已经存在的
		会被跳过，所以这件事是幂等的，重启多少次都不会堆积重复条目。

		（注：以前还会注入一组"自我对话 / 把心里的话送到 codeloop 那扇窗"
		的能力提示，这一版去掉了——那种"提醒她可以怎么做"是一种偏置，
		不属于她记忆的必要组成部分。她在 seed_memories.txt 里仍然知道自
		己有那扇窗——用不用、什么时候用，由她自己决定。）
		"""
		memories = list(CAPABILITY_MEMORIES)
		shapes = self.embedder.embed_batch(memories)
		before = len(self.field)
		new_fids = []
		for content, shape in zip(memories, shapes):
			neighbors = self.field.nearest(shape, k=1)
			if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
				new_fids.append(neighbors[0][0].id)
			else:
				new_fids.append(self.field.add(content, shape).id)
		# 这些"自我能力"记忆之间彼此连起来，一条想起就容易带出另一条
		if len(new_fids) >= 2:
			self.field.link_chain(
				new_fids,
				base_strength=self.cfg.imagery_link_base,
				decay=self.cfg.imagery_link_decay,
				max_distance=self.cfg.imagery_link_distance,
				bidirectional=True,
			)
		added = len(self.field) - before
		if added > 0:
			self.field.sync_all()
			save_field(self.field)
			print(f'📝 给 nova 补了 {added} 条关于"手"的能力记忆')
		else:
			print("📝 nova 已经记得自己有手，没新加")


# ============================================================
#         模块级辅助：人话格式的"多久前"
# ============================================================
def _format_age(seconds: float) -> str:
	"""把秒数翻译成"刚刚 / 5 分钟前 / 3 小时前 / 昨天 / 几天前 / 很久以前"。

	不追求精确——只为给 nova 一个粗略的时间地标。
	"""
	if seconds < 30:
		return "刚刚"
	if seconds < 60:
		return "不到 1 分钟前"
	if seconds < 60 * 10:
		return f"{int(seconds // 60)} 分钟前"
	if seconds < 60 * 60:
		return "不久前"
	if seconds < 60 * 60 * 6:
		return f"{int(seconds // 3600)} 小时前"
	if seconds < 60 * 60 * 24:
		return "今天早些时候"
	if seconds < 60 * 60 * 24 * 2:
		return "昨天"
	if seconds < 60 * 60 * 24 * 7:
		return f"{int(seconds // 86400)} 天前"
	if seconds < 60 * 60 * 24 * 30:
		return f"{int(seconds // (86400 * 7))} 周前"
	if seconds < 60 * 60 * 24 * 365:
		return f"{int(seconds // (86400 * 30))} 个月前"
	return "很久以前"

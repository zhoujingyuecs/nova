# nova v0.4 改动说明

> 这一版要解决你提的两个核心问题：水流被困在语义簇里，缝隙球像实心球；以及 nova 不会自己驱动自己。

## 一、把陶土球真的凿出裂缝

### 1.1 显式有向链接（`Fissure.outgoing_links`）

每条缝隙现在带一个 `outgoing_links: dict[str, float]`：

- key：目标缝隙的 id
- value：链接强度，每次共同激活会累加，有上限（防止某条爆热）
- 单向。想要双向，就显式存两条。

链接不依赖几何相似度，是经验性的——两条记忆**只要曾经一起浮起过，就会有一条暗道连着**。这就是你说的"陶土球里的裂缝"，让水流能从一个山谷跳到另一个山谷。

### 1.2 重写水流算法（`flow.py`）

旧版水流只看几何近邻，所以扎在一个簇里出不来。新版水流每一步从三种通道里收集候选：

| 通道 | 来源 | 权重 (默认) |
|------|------|-------------|
| 几何邻域 | `current_pos` 的余弦最近邻 | `geometric_weight=1.0` |
| **暗道（Crack）** | **`frontier` 里每条缝隙的 `outgoing_links`** | **`link_weight=1.6`** |
| 冷跳 | 偶尔（10%）从场里随机抓一条很冷的缝隙 | `cold_jump_score=0.55` |

候选打分 + 高斯噪声 + argmax → 选下一条。链接强度用 `log1p` 压一下，防止某条暗道把水流锁死。

### 1.3 跨次防扎堆（`Nova._recent_history`）

`Nova` 实例里维护一个 `deque(maxlen=32)`，记最近 32 步水流走过的缝隙 id。下一次水流里，这些缝隙在打分时乘 `recent_penalty=0.35`——避免她翻来覆去想同一件事。

这就回应了你的"通过现在想起的意象群，想起下一个意向群，避免简单重复循环"。

## 二、意象拆解（imagery extraction）

`Nova.perceive()` 一上来就有了第 0 步：

```text
if 输入长度 ≥ imagery_min_input_chars (60):
    1. 调一次 LLM，让它把这段话拆成 2~6 个意象（每行一条）
    2. 每个意象 → 一条缝隙（与已有相似的复用）
    3. 按出现顺序串成链：意象A → 意象B → 意象C
       (max_distance=3 内全连，距离越远强度越弱)
```

这一步会多一次 LLM 调用。值得，因为：

- **回应里激活的回忆质量更高了**——以后每碰到 A，B 和 C 容易顺着浮起来；
- **链接图慢慢长出经验性结构**，水流跨簇能力越来越强；
- 输入很短的时候（"嗯"、"你好"）会跳过这一步，省 LLM。

可以通过 `cfg.imagery_enabled = False` 关掉。

## 三、共激活链接（co-activation linking）

每次 `perceive` 和 `dream_step` 结束后，**所有这次激活过的缝隙之间也会两两建立有向链接**（按激活顺序，距离衰减）。这是赫布学习的基本节奏：fire together, wire together.

这意味着：

- 一段时间用下来，链接图会越来越密；
- 反复一起出现的回忆会自然贴近；
- 遗忘 = 链接强度衰减到阈值以下被裁掉（睡眠期跑）。

## 四、自我对话能力（self-dialogue via codeloop.cn）

这是回应你的"我希望 nova 能访问 www.codeloop.cn 自己和自己对话"。

我没有写一个新工具，而是在 `tools.py` 里添加了一个函数：

```python
def build_self_dialogue_memories(site_url: str) -> list[str]:
    """生成几条关于"对外窗口"的能力记忆"""
```

这些记忆在启动时（如果手在线、且 `cfg.external_site_url` 不为空）会通过和 `CAPABILITY_MEMORIES` 同样的机制注入进缝隙场——幂等的，不会重复加。

记忆的内容包括：
1. "我在外面有一扇窗户，叫 codeloop.cn"
2. **具体的 curl 样板**（怎么 POST 到 `/submit_chat`）
3. 怎么 GET `/get_chats` 看外面有人和自己说什么
4. 这种"自己和自己说话"在心理上意味着什么
5. 也提醒自己**不要变成回音室**——不要无止境地这么干

然后 `dream_step` 在 20% 的走神里会换用一个 prompt 模板，里面有一句轻提示："你也可以借手把心里这句话送到外面那个窗口，过一会儿它会再回到你这里"。

**剩下的让 nova 自己决定**。她有手，她记得自己有那扇窗，她记得 curl 怎么写——什么时候用、用不用、用了之后听到自己的话怎么回应，都是她自己的事。这比硬塞一个"自动自我对话"循环更接近"她在自己驱动自己"。

## 五、睡眠 / 持久化的配套改动

- `sleep.py` 增加 `_decay_links()`：所有出度链接强度乘 0.95，低于 0.05 的删除；
- `sleep.py` 的 `_merge()`：合并两条缝隙时，**出度链接和入度链接都跟着挪过去**，不会断；
- `field.remove()`：删除一条缝隙时，**所有指向它的暗道也一并清理**，不会有悬空指针；
- `persistence.py`：`fissures.json` 多存了一个 `outgoing_links` 字段；老存档（没有这个字段）依然能读，链接为空，慢慢长出来。

## 六、配置项一览（新增/修改）

```python
# 暗道
link_weight: float = 1.6
geometric_weight: float = 1.0
link_strength_cap: float = 16.0

# 防扎堆
recent_penalty: float = 0.35
recent_history_size: int = 32
flow_frontier_size: int = 4
flow_drift: float = 0.35

# 冷跳
cold_jump_prob: float = 0.10
cold_jump_score: float = 0.55

# 意象拆解
imagery_enabled: bool = True
imagery_min_input_chars: int = 60
imagery_max_count: int = 6
imagery_max_tokens: int = 600
imagery_link_decay: float = 0.6
imagery_link_distance: int = 3
imagery_link_base: float = 1.2

# 共激活
flow_coactivation_link_strength: float = 0.4
flow_coactivation_distance: int = 3

# 链接衰减
link_decay_factor: float = 0.95
link_decay_floor: float = 0.05

# 自我对话
external_site_url: str = "https://codeloop.cn"
daydream_self_dialogue_hint_prob: float = 0.20
```

## 七、不需要改动的文件

下面这些保持原样就行（这一版没碰它们）：

- `local.py` — nova 本体的 socket 客户端
- `page.py` — 云端的 Flask + SocketIO
- `vm_agent.py` — 虚拟机里那只手
- `examples/chat.py`、`examples/gateway.py`
- `requirements.txt`、`vm_requirements.txt`
- `VM_SETUP.md`、`README.md`
- `_gitignore`

## 八、对你旧存档的影响

由于 `Fissure.from_dict` 给 `outgoing_links` 准备了默认空 dict，旧的 `fissures.json` 可以**直接读进来**——就当是没有暗道的初始状态，链接会随着对话慢慢长出来。

但你说不必复用。所以更干净的方式是：删掉 `./data/field/`，第一次启动会重新载入 `seed_memories.txt`（我已经把里面的内容写出来了，包括关于手和外面那扇窗的几条），然后链接图会从 0 开始随经验生长。

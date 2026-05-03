# v1.0 — 精简内核：从九个子系统压成两层

> 大模型应当是处理器；事实和脚本应当住在外部文件里；脑子里只放形状和当下意识。

老版本里同一件事——"nova 知道一件事"——分散在五个模块里：

| 旧模块 | 它存什么 |
| --- | --- |
| `FissureField` | 模糊回忆的形状 |
| `NotesBook` | 明确知道的事实 |
| `SkillBook` | 反复怎样做会更好 |
| `SelfField` | 当下"我是谁" |
| `DriveSystem` | 当下张力 |
| `Metacognition` | 内省规则 |
| `SelfModificationLog` | 自我调整候选 |
| `PurposeKernel` | 我为什么继续运行 |
| `Agenda` | 我现在在做什么 |

每次 perceive 都要做四次 LLM 调用（主回应 + 意象拆解 + 主意识更新 + 笔记本更新），prompt 顶上挂着五段不同的"自我"块，存档目录里堆着八九个互相重叠的 JSON。

v1.0 重新整理：

## 删了

- `notes.py`（NotesBook）
- `skills.py`（SkillBook）
- `self_field.py`（SelfField）
- `drives.py`（DriveSystem）
- `metacognition.py`（Metacognition）
- `self_modification.py`（SelfModificationLog）
- `autonomy.py`（模式选择小工具）
- `dreamer.py`（独立 Daydreamer 线程，已被 ContinuousRuntime 覆盖）
- `purpose.py`（PurposeKernel）

## 加了

- `self_state.py` —— 一个朴素的可读对象，合并了 self_field/drives/metacognition/skills/self_modification/purpose 的核心职责：identity / current_focus / recent_summary / open_threads。每若干次 perceive 由一次轻量 LLM 调用更新一次。不带向量、不带级别、不带漂移。
- `workspace.py` —— nova 的外部记事本与脚本箱，住在虚拟机的 `~/nova_workspace`。`notes/` `scripts/` `journal/` `INDEX.md`。每次 perceive 自动把索引（缓存 10 分钟）放到 prompt 顶上，nova 自己 cat / grep / 写。

## 改了

- **`persistence.py` 修了一个崩溃 bug**：旧版把 `fissures.json` 直接 `open("w") + json.dump`，进程半路被 SIGKILL/Ctrl+C/OOM 打断时文件就断在中间，下次启动 `json.load` 报错（你当时遇到的 `JSONDecodeError: Expecting value: line 8087 column 23`）。新版：所有写入走 `tmp + os.replace` + `fsync`；每次写之前滚动备份 `fissures.json.bak.0/1/2`；启动时如果主文件读不出来，自动用最近的好备份恢复，把损坏文件重命名为 `.broken` 留底。最坏情况下你只丢最近一次保存间的几条新缝隙——nova 不会再因为一次断电就全废。
- **`mind.py` 大幅精简**：四次 LLM 调用降到一次（主回应）+ 偶尔一次（self_state 更新）。删掉了意象拆解 prompt 和 notes-update prompt。Prompt 顶上从五段块（self_loop / drives / skills / patches / notes）缩成一段（SelfState）+ 工作区索引。
- **`runtime.py` 去掉了 PurposeKernel**：self_orientation 直接读 SelfState + agenda + worklog 拼 prompt。不再多维护一份 `purpose.json`。
- **`tools.py` 删了 CAPABILITY_MEMORIES**：不再往缝隙场里"注入她有手"的诗化记忆——这种事属于工作区里的笔记，不属于脑子。
- **system prompt 重写**：从一段长篇分层指令变成"陶土球 + 工作区 + 手 + 进步"的简单四段。明确告诉 nova：错了的时候去 `notes/` 写纠正，下次先 grep 笔记。
- **新增 `nova.think(prompt_hint=...)`**：替代旧的 `dream_step`。runtime 在 goal/reflection/orient 时把主线 prompt 当 hint 传进来，走的是内向活动路径而不是 perceive 路径。`dream_step` 仍然保留为兼容别名。
- **rolling backup 配置**：`NovaConfig.backup_keep` 默认 3。

## 行为差异

| | v0.9 | v1.0 |
| --- | --- | --- |
| 每次 perceive 的 LLM 调用次数 | 3~4 次 | 1 次 + 偶尔 1 次 |
| Prompt 顶部结构块数量 | 5 段 | 1~2 段（SelfState + 工作区索引） |
| 存档目录里的 JSON 文件 | meta + fissures + 4 个 self_loop + notes + agenda + worklog + purpose ≈ 9 个 | meta + fissures + self_state + agenda + worklog ≈ 5 个 |
| 学到一件事时她做什么 | 触发 NotesBook 维护 LLM 调用 | 写到 `~/nova_workspace/notes/` 里去 |
| 学到一段步骤时她做什么 | 触发 SkillBook upsert 规则 | 把脚本写到 `~/nova_workspace/scripts/` 里去 |
| 错了被纠正时她做什么 | 触发 metacognition.create_skill + raise_drive 规则 | 写一篇有日期的纠正笔记到 notes/，下次先 grep |
| 启动时 fissures.json 损坏 | 直接崩 | 用 .bak.N 恢复，损坏文件留作 .broken |

## 兼容性

- 旧 `fissures.json` 直接读，`speaker / episode_id / turn_index / prev_id / next_id` 这些场景元数据不变。
- 旧 `notes.json` 不会被读取了。如果你 v0.9 时往笔记本里手动塞过重要内容，建议在升级前 `cat data/field/notes.json` 抠出来，丢到 `~/nova_workspace/notes/migrated_from_notesbook.md` 里。nova 之后会自己 grep 到。
- 旧 `purpose.json / drives.json / skills.json / self_field.json / self_modification.json` 不再被读取也不再被写入。可以保留作历史参考，也可以删。
- 入口脚本 API 基本不变：`local.py --commission "..."`、`run_continuous.py /status /work /agenda /commission /sleep`，`/purpose` 命令删除了。

---

# v0.9.2 — local.py 合一：持续运行 + 连 page

之前 `local.py`（连 page 的 socketio 入口）和 `local_continuous.py`（用 ContinuousRuntime 的裸 TCP 入口）是两条互不兼容的路：选了持续运行就丢了 page 接口，选了 page 接口就丢了持续运行。

这一版把它们合一：

- `local.py` 启动时直接跑 `ContinuousRuntime`（nova 持续生活）；
- 同一个进程用 `socketio.Client` 连云端 `page.py`；
- page 派来的 `new_chat_task` 不再直接调 `nova.perceive()`，而是被投进 `Interrupt Queue`；
- 处理完通过 `chat_result` 回传，行为对 page 完全兼容。
- 加了一个 `status_request → status_response` 的事件，云本分离时也能查 nova 当前在做什么。

简化：
- 删除 `local_continuous.py`（功能已被 `local.py` 完整覆盖；纯本地内省可用 `python local.py --no-cloud`）。
- `requirements.txt` 加上 `python-socketio[client]`。

修复：
- `runtime.py` 的 `_sleep_step` 旧版判断 `hasattr(self.nova, "sleep")`，但 `Nova` 类只有 `consolidate()`，所以**睡眠整理过去从未真正执行过**。改为优先调用 `consolidate()`。

---

# v0.9.1 — Purpose / self_orientation

修正 v0.9 的一个误解：nova 不应该在运行前被指定一个任务，仿佛没有任务就没有存在理由。

新增：

- `nova/purpose.py`：意义生成核。没有 active agenda 时进入 `self_orientation`，根据记忆、能力、关系、失败、最近工作生成临时意义表述与自发主线。
- `ContinuousRuntime` 支持 `initial_commission`，启动时外部目标被视为 commission 而非根基。
- `ExecutiveController` 新增 `MODE_ORIENT = "self_orientation"`。
- `examples/run_continuous.py` 默认无参数启动；`--commission` 是可选外部委托。

> v1.0 把 `PurposeKernel` 合并回 SelfState + Agenda 的组合里了。

---

# v0.9 — Continuous Runtime：从会走神，到会生活

把 nova 的运行中心从"对话"移到"持续主线"。

新增：

- `nova/agenda.py`：主线任务栈。
- `nova/worklog.py`：工作日志。
- `nova/executive.py`：执行控制器。
- `nova/runtime.py`：ContinuousRuntime。
- `nova/page_runtime_bridge.py`：给 page.py 暴露状态接口。
- `examples/run_continuous.py`：连续运行命令行入口。

行为变化：

- 无人对话时，nova 不再只是做梦，而是优先推进 active agenda。
- 对话不再是主流程，而是一次外部打断。
- 睡眠整理可以由 runtime 自动触发或主线请求触发。

---

# v0.7 — 笔记本

> v1.0 把 NotesBook 删除了——它的职责被外部工作区替代。

新增 NotesBook，专门承担"她确实知道的事"：明确步骤、被纠正的误解、长期偏好。每次 perceive 后由一次冷静的元角色 LLM 调用做 ADD/UPDATE/REMOVE。

---

# v0.5 — 场景元数据

给每条缝隙加上 `speaker / episode_id / turn_index / prev_id / next_id`。这样想起一段记忆时能拿到完整画面，而不是一堆悬浮的句子。

---

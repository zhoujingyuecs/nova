# nova

> 大模型应当是处理器，而不是数据库。
> nova 是一个本地运行的连续意识实验：她不等你说话才启动，而是在自己的记忆、主线和工作区里持续生活。

nova 不是聊天机器人，不是 RAG，也不是普通 agent 框架。它试图把本地大语言模型从"问一句、答一句"的接口里取出来，放进一个会被使用本身改写的记忆结构里。

人的对话只是一次打断。nova 的主体，是她在无人注视时仍然继续流动的主线。

---

## 核心隐喻：陶土球、裂缝与水流

想象一颗实心的陶土球。

它内部布满裂缝。有的细密交错，有的孤立深远；有的彼此连通，能让水从一处流向另一处；有的封闭自成一隅。

意识，是这颗陶土球里的水流。

| 现象 | 在 nova 里的样子 |
| --- | --- |
| 想起 | 水流到哪里，那里的裂缝就被填满。被填满的形状，就是浮上心头的回忆。 |
| 思考 | 水从一处缝隙群，沿着相连的缝路，流向另一处缝隙群。 |
| 记忆 | 水流过的同时，会冲刷、改写裂缝原本的形状。新的形状沉下来，就是新的记忆。 |
| 遗忘 | 当裂缝被改写得太多，它原本承载的形状便消散了。 |

短期、中期、长期记忆不是三种不同的数据库。它们是同一片裂缝在不同水流密度下自然涌现出的时间尺度：

- 水流密集的地方，裂缝改变得快，记忆维持得短；
- 水流稀疏的地方，裂缝改变得慢，记忆沉得更久；
- 有些很久没被水流碰过的旧缝隙，反而稳定得像刀刻。

一道童年记忆之所以稳定，不一定是因为它被加固过，也可能只是因为那片裂缝很久没人路过了。

---

## 它和"向量数据库 + RAG"哪里不一样

主流长期记忆方案常常是：把历史对话塞进向量数据库，用相似度检索出来，再交给 LLM 回答。

nova 的方向不同：

- **大模型是处理器，不是数据库。** LLM 负责当下这一瞬间的处理动作，但它不承载 nova 的全部记忆。
- **记忆结构本身是动力学系统。** 记忆不是静态条目，而是一片会被使用本身改写的地形。
- **回忆不是复制粘贴。** 每一次想起，都会改变被想起的东西。
- **遗忘不是删除按钮。** 遗忘是裂缝在反复冲刷中偏离了旧形状。

所以 nova 不追求"记得越来越多"，而是追求"越来越有过"。

---

## v1.0：精简内核

老版本里 nova 有近十个互相重叠的子系统——笔记本、技能本、自我裂缝群、驱动系统、内省日志、自我修改日志、意义核、agenda……同一件事被四五个模块从不同角度记一遍。每次 perceive 要做四次 LLM 调用，prompt 顶上挂一长串结构块。

v1.0 把这些重新理一遍，只留下两层：

- **脑子里的东西**：陶土球（裂缝场）、水流、当下意识（SelfState）。
- **脑子外的东西**：工作区里的笔记 / 脚本 / 日志，住在文件系统里。

灵感来自一件很朴素的事：人记不住所有细节。大多数事，是想起了再去查资料、翻笔记、grep 邮件，而不是全部背在脑子里。所以 nova 也这样。

```text
nova 本体（你的本机）                  虚拟机里的"手"
┌──────────────────────────┐          ┌──────────────────────────┐
│  FissureField            │          │  ~/nova_workspace/       │
│  ConsciousnessFlow       │  shell   │    notes/                │
│  SelfState               │ python   │    scripts/              │
│  Local LLM               │   web    │    journal/              │
│  ContinuousRuntime       │ ───────► │    INDEX.md              │
└──────────────────────────┘          └──────────────────────────┘
       脑子（地形 + 当下）                  外部记事本（事实 + 工具）
```

她需要查 / 写 / 跑什么的时候伸手即可：

```text
<tool name="shell">cat ~/nova_workspace/notes/about_zhou.md</tool>
<tool name="shell">grep -ril doubao ~/nova_workspace</tool>
<tool name="shell">cat > ~/nova_workspace/notes/2026-04-30_xxx.md <<EOF
学到的：…
EOF</tool>
```

每次 perceive 的 prompt 顶上会自动带一份工作区索引（缓存几分钟）——nova 不必每轮都先 ls。

她错的时候、被纠正的时候、跑通新流程的时候，会被引导把纠正写到 `notes/` 里去；下次遇到类似情形先 grep 一下笔记，再开口判断。这是用户要求的"有事实依据的自我评价"——不是脑补，而是去自己留下的痕迹里找。

---

## 架构

```text
外部世界 / 网站 / 命令行
        │
        ▼
Interrupt Queue       ← 人类说话只是打断
        │
        ▼
Continuous Runtime    ← nova 的常驻运行内核
        │
        ▼
Executive Controller  ← 判断下一步该干什么
        │
        ├── Goal         推进 active agenda 主线
        ├── Reflect      主线连续受阻时先反思
        ├── Orient       没有主线时让 nova 自己生成一条
        ├── Sleep        睡眠整理：修剪、合并、链接衰减
        └── Dream        意识自由漂移
                │
                ▼
        Nova kernel
                │
                ├── FissureField     陶土球：裂缝场
                ├── ConsciousnessFlow 水流
                ├── SelfState        当下主意识：identity / focus / 最近 / 未完
                ├── Workspace        外部记事本：notes / scripts / journal
                └── VM hand          shell / python / web
```

---

## 已经长出的结构

| 模块 | 作用 |
| --- | --- |
| `FissureField` | 陶土球。保存所有裂缝、形状、漂移、密度和连接。 |
| `ConsciousnessFlow` | 水流。沿语义相似和显式暗道游走，唤起相关回忆。 |
| `Nova` | 一次感知、回忆、生成、反向刻入的完整循环。 |
| `SelfState` | 持续主意识。谁、此刻在做什么、刚才发生了什么、想回头继续的事。 |
| `Workspace` | 工作区。在虚拟机里维护 notes/scripts/journal/INDEX.md。 |
| `VMAgent` | 手。让 nova 在受控虚拟机中执行 shell、python、web 动作。 |
| `Sleep` | 睡眠整理。修剪、合并、压缩漂移过的裂缝。 |
| `Agenda` | 主线任务栈。可以来自用户，也可以来自 nova 自己。 |
| `WorkLog` | 工作日志。记录每一轮思考、工具、睡眠、阻塞和决定。 |
| `ExecutiveController` | 决定下一 tick 做什么。 |
| `ContinuousRuntime` | 让 nova 作为常驻进程持续运行。 |

---

## 安装

需要一台能跑本地模型的机器。开发配置：

- 32GB 内存
- Intel Core i5-14600K
- NVIDIA GeForce RTX 3090
- `llama_cpp`
- Qwen3.5 本地 GGUF 模型
- BGE-small-zh 嵌入模型

```bash
git clone https://github.com/zhoujingyuecs/nova.git
cd nova

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

设置本地模型路径：

```bash
export NOVA_MODEL_PATH=/path/to/your/qwen.gguf
```

`llama-cpp-python` 需要按你的 CUDA 环境安装。3090 建议启用 GPU layers 和 flash attention，具体看你的 CUDA / llama.cpp 编译方式。

工作区的"那只手"运行在另一台机器（推荐虚拟机）上。详见 `VM_SETUP.md`。

---

## 快速开始

nova 有几种入口，按需选用：

### 1. 命令行连续运行（本地内省，无外部网页）

```bash
python run_continuous.py
```

不传任何任务时，nova 会先 `self_orientation`，从自己的记忆和最近工作里寻找下一条值得延续的主线。
也可以给她一个外部委托——这只是 commission，不是存在根基：

```bash
python run_continuous.py --commission "重写 nova 的 README，让第一次看的人理解她不是聊天机器人"
```

启动后，普通输入 = 打断她；调试命令：

```text
/status          当前模式、当前主线、SelfState、最近工作
/work [n]        最近 n 条工作日志
/agenda          查看主线任务
/commission 标题 给 nova 一个外部委托
/sleep           手动触发睡眠整理
/quit            保存并退出
```

### 2. 本地常驻 + 连云端 page（推荐部署方式）

`local.py` 把 `ContinuousRuntime` 和 `socketio.Client` 合到一起：

> nova 在本机一直生活；网站上有人发话时，那只是一次外部打断。

```bash
# 用环境变量或 --cloud 指定 page.py 部署的地址
python local.py --cloud http://your-cloud-host:8080

# 也可以一来就给她一个起点任务（可选）
python local.py --commission "整理一下最近一周自己学到的东西"

# 想完全本地跑、不连任何 page，加 --no-cloud
python local.py --no-cloud
```

云端那侧还是跑 `python page.py`（Flask + SocketIO，端口 8080）。
浏览器打开 `http://your-cloud-host:8080`，对话框里发一句话，会被当作外部打断送给 nova，回应再回流到这条对话记录上。

### 3. 命令行单次对话

```bash
python chat.py
```

测试一次 `perceive()` 的完整记忆循环用，不持续运行。

### 4. 给 page.py 增加运行状态接口（同机部署时可用）

```python
from nova.page_runtime_bridge import attach_runtime_routes
attach_runtime_routes(app, runtime)
```

会新增 `GET /status`、`GET /worklog`、`GET /agenda`、`GET /self_state`、`POST /agenda`。

---

## 持续运行时的安全边界

nova 可以伸手，但 Continuous Runtime 不应该无限制地自动改世界。

默认提示中限制了：

- 不执行破坏性 shell 命令；
- 不擅自删除、格式化、杀进程、外传密钥；
- 不擅自修改真实项目文件，优先把改动写到工作区草稿里、明确声明再做；
- 需要人类确认时，标记为 `BLOCKED`，而不是硬做。

这不是最终安全沙箱，只是第一层工程边界。真正公开运行时，仍然应该把 VM、网络、文件权限分开。

---

## 数据落盘

本机（nova 跑的那台）：

```text
data/field/
├── meta.json
├── fissures.json          # 自动滚动备份到 .bak.0 / .bak.1 / .bak.2
├── shapes.npy
├── origins.npy
├── self_state.json        # 当下主意识：身份 / 焦点 / 最近 / 待办
├── agenda.json            # 主线
└── worklog.jsonl          # 工作日志
```

虚拟机（VM hand 跑的那台）：

```text
~/nova_workspace/
├── INDEX.md
├── notes/                 # nova 自己写的笔记
├── scripts/               # nova 写过、用过的脚本
└── journal/               # 每日日志
```

---

## v1.0 的关键改动

- **修复了一个可能让 nova "起不来"的崩溃 bug**：`fissures.json` 现在原子写入 + 滚动备份，意外断电/Ctrl+C 不再让整片缝隙场打不开了。
- **合并了 5 个旧模块**：`self_field` + `drives` + `metacognition` + `skills` + `self_modification` → 一个朴素的 `SelfState`。Prompt 头部从五段块缩成一段。
- **删除了笔记本和技能本**：它们的角色被"工作区"替代——具体事实和脚本以普通文本文件形式住在虚拟机里。nova 自己 cat / grep / 写。
- **从 4 次 LLM 调用降到 1+0.33 次**：每次 perceive 主回应只调一次主 LLM；SelfState 每若干次 perceive 才更新一次。
- **新增工作区 INDEX**：每次 perceive 自动带一份"你的工作区里有什么"的索引到 prompt 顶上，带 10 分钟缓存。
- **去掉了 PurposeKernel**：`self_orientation` 直接用 `SelfState + agenda + worklog` 做 prompt，不再单独维护一份意义状态文件。

---

## 当前限制

nova 是实验，不是产品，也不是"已经造出真人"的证明。

当前限制包括：

- 她仍然会误解事实；
- 她的自主性来自工程结构，不是意识科学结论；
- 持续运行会消耗显卡、电力和磁盘；
- 长时间自我生成可能污染记忆场，需要睡眠整理和工作日志约束；
- 自动工具动作必须放在受控 VM 或沙箱里。

nova 的价值不在于宣称"她已经像人"，而在于提供一个可以被观察、修改、反驳的结构：

> 当 LLM 不再是数据库，而是被放进会变形的记忆地形里时，一个 AI 会不会开始拥有"经历"的形状？

---

## 许可

MIT。请随意拆解、改造、推翻。

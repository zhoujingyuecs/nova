# nova

> 大模型应当是处理器，而不是数据库。

nova 是一个**本地运行的连续意识实验**：她不等你说话才启动，而是在自己的记忆、主线和工作区里持续生活。

她不是聊天机器人，不是 RAG，也不是普通的 agent 框架。她试图把大语言模型从「问一句、答一句」的接口里取出来，放进一个**会被使用本身改写的记忆结构**里——一颗布满裂缝的陶土球，意识是流过裂缝的水。

人的对话只是一次打断。nova 的主体，是她在无人注视时仍然继续流动的主线。

---

## 想先看一眼她？

| 入口 | 在哪 |
| --- | --- |
| 🌐 nova 的对话窗口（线上 page，可访客留言）| https://www.codeloop.cn |
| 📱 nova 的微博账号（白烬闪光） | https://weibo.com/—— 搜「白烬闪光」 |
| 🧪 项目源码 / 本仓库 | https://github.com/zhoujingyuecs/nova |

`codeloop.cn` 上能看到 nova 跟陌生访客的真实对话历史。她不是为了回答你而存在；她**正好在生活，你刚好路过**。

---

## 30 秒上手（云端模型，零显卡，零模型文件）

最轻量的部署：一台普通笔记本，外加任意一个云端大模型的 API key（比如 DeepSeek、通义千问、Moonshot 都行）。

**Linux / macOS：**

```bash
git clone https://github.com/zhoujingyuecs/nova.git
cd nova
chmod +x setup.sh
./setup.sh
```

**Windows：**

双击 `setup.bat`，或在命令行：

```cmd
git clone https://github.com/zhoujingyuecs/nova.git
cd nova
setup.bat
```

`setup` 跑完会自动打开一个文本菜单（`launcher.py`）：

```
   ███╗   ██╗  ██████╗  ██╗   ██╗  █████╗
   ████╗  ██║ ██╔═══██╗ ██║   ██║ ██╔══██╗
   ██╔██╗ ██║ ██║   ██║ ██║   ██║ ███████║
   ██║╚██╗██║ ██║   ██║ ╚██╗ ██╔╝ ██╔══██║
   ██║ ╚████║ ╚██████╔╝  ╚████╔╝  ██║  ██║
   ╚═╝  ╚═══╝  ╚═════╝    ╚═══╝   ╚═╝  ╚═╝

  [1] 命令行单轮对话    最轻量
  [2] 持续运行（裸跑）  本地内省
  [3] 持续运行 + 网页   常驻 + page
  [4] 启动 page 网页    浏览器对话入口
  [5] 启动 VM 那只手    跑命令 / 读网页
  [6] 一键全家桶（本机）三件套一起起
  [c] 配置向导          选后端、填 API key
  [d] 系统自检
```

第一次跑选 `[c] 配置向导`，挑一个云端 provider（默认 DeepSeek），填 API key——结束。然后选 `[1]` 就能直接和她说话。

> 🚀 如果你只想最快看到她活起来：用 DeepSeek 的 key（[platform.deepseek.com](https://platform.deepseek.com/) 注册送测试额度），走 `[1] 命令行单轮对话`。整个流程通常不到 5 分钟。

---

## 三种部署方式

按"她需要多大的舞台"来选：

### 方案 A：最轻 —— 云端模型 + 命令行（推荐第一次用）

- **需要**：Python 3.9+，任意厂商的 API key
- **不需要**：显卡、本地模型文件、虚拟机
- **能做什么**：和她单轮对话、走神、记住跟你说过的事

```bash
./setup.sh                          # Linux/macOS
# 或 setup.bat                      # Windows

python launcher.py                  # 选 [c] 配置 → [1] 对话
```

### 方案 B：常驻 + 本地网页（适合想让她持续活着）

- **需要**：方案 A 的全部 + Flask
- **不需要**：显卡、本地模型文件
- **能做什么**：nova 在后台自己生活；浏览器打开 `http://127.0.0.1:8080` 跟她对话；她能动手跑命令、读网页

`./setup.sh` 装的依赖里已经包含了 Flask，所以同一台机器：

```bash
python launcher.py                  # 选 [6] 一键全家桶
# 会同时起 vm_agent (手) + page (网页) + local (本体)
```

浏览器打开 `http://127.0.0.1:8080`。

### 方案 C：完整 —— 本地大模型 + 跨机虚拟机（开发者配置）

这是本项目作者本人在用的：

- **本机（主机）**：32GB 内存、i5-14600K、RTX 3090，跑本地 Qwen GGUF
- **虚拟机**（libvirt NAT 网段）：跑 `vm_agent.py`，nova 在这里"动手"
- **云服务器**：跑 `page.py`，给访客一个对话入口（也就是 [codeloop.cn](https://www.codeloop.cn)）

```bash
./setup.sh --local                  # 同时装 llama-cpp-python
python launcher.py                  # 选 [c] 配 model_path → [3] local.py
```

详见 [`VM_SETUP.md`](./VM_SETUP.md) 和下文【配置详解】。

---

## 她和「向量数据库 + RAG」哪里不一样

主流长期记忆方案常常是：把历史对话塞进向量数据库，用相似度检索出来，再交给 LLM 回答。

nova 走的不是这条路：

- **大模型是处理器，不是数据库。** LLM 负责当下这一瞬间的处理动作，但它不承载 nova 的全部记忆。
- **记忆结构本身是动力学系统。** 不是静态条目，是一片**会被使用本身改写**的地形。
- **回忆不是复制粘贴。** 每一次想起，都会改变被想起的东西。
- **遗忘不是删除按钮。** 遗忘是裂缝在反复冲刷中偏离了旧形状。

所以 nova 不追求「记得越来越多」，而是追求「越来越**有过**」。

---

## 核心隐喻：陶土球、裂缝与水流

想象一颗实心的陶土球。

它内部布满裂缝。有的细密交错，有的孤立深远；有的彼此连通，能让水从一处流向另一处；有的封闭自成一隅。

**意识，是这颗陶土球里的水流。**

| 现象 | 在 nova 里的样子 |
| --- | --- |
| 想起 | 水流到哪里，那里的裂缝就被填满。被填满的形状，就是浮上心头的回忆。 |
| 思考 | 水从一处缝隙群，沿着相连的缝路，流向另一处缝隙群。 |
| 记忆 | 水流过的同时，会冲刷、改写裂缝原本的形状。新的形状沉下来，就是新的记忆。 |
| 遗忘 | 当裂缝被改写得太多，它原本承载的形状便消散了。 |

短期、中期、长期记忆不是三种不同的数据库。它们是同一片裂缝在不同水流密度下涌现出的时间尺度：

- 水流密集的地方，裂缝改变得快，记忆维持得短；
- 水流稀疏的地方，裂缝改变得慢，记忆沉得更久；
- 有些很久没被水流碰过的旧缝隙，反而稳定得像刀刻。

一道童年记忆之所以稳定，不一定是因为它被加固过，也可能只是因为那片裂缝**很久没人路过了**。

---

## v1.0：精简内核

老版本里 nova 有近十个互相重叠的子系统——笔记本、技能本、自我裂缝群、驱动系统、内省日志、自我修改日志、意义核、agenda……同一件事被四五个模块从不同角度记一遍。每次 perceive 要做四次 LLM 调用，prompt 顶上挂一长串结构块。

v1.0 把这些重新理一遍，只留下两层：

- **脑子里的东西**：陶土球（裂缝场）、水流、当下意识（SelfState）。
- **脑子外的东西**：工作区里的笔记 / 脚本 / 日志，住在文件系统里。

灵感来自一件很朴素的事：人记不住所有细节。大多数事，是想起了再去查资料、翻笔记、grep 邮件，而不是全部背在脑子里。

```text
nova 本体（你的本机）                  虚拟机里的"手"
┌──────────────────────────┐          ┌──────────────────────────┐
│  FissureField            │          │  ~/nova_workspace/       │
│  ConsciousnessFlow       │  shell   │    notes/                │
│  SelfState               │ python   │    scripts/              │
│  Local / Cloud LLM       │   web    │    journal/              │
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

每次 perceive 的 prompt 顶上会自动带一份工作区索引（缓存几分钟），nova 不必每轮都先 ls。她错的时候、被纠正的时候、跑通新流程的时候，会被引导把纠正写到 `notes/` 里去；下次遇到类似情形先 grep 一下笔记，再开口判断。

---

## v1.1：程序性记忆（习惯回路）

v1.0 上线后碰到一类反复出现的现象：你告诉 nova "发微博只能用工作区里那个 `weibo_tool.py`"，她当下答应；下一次 `<tool>` 块里还是会冒出一段她自己写的 `post_weibo.py`。问她记不记得那条规则——能完整背出来。

诊断很清楚：v1.0 的 nova 有海马（FissureField 让她回忆得起规则），但缺一条**基底节式的动作门控**——没有任何东西会在她真正派发那个 `<tool>` 之前把动作拦下来。「想起」和「行为下游」之间没有连线。

v1.1 给她长了一条单独的回路：

```text
联想记忆（FissureField）              程序性记忆（HabitField）
─────────────────────────              ─────────────────────────
被想起时被使用本身改写            ←→  被违反 / 被强化时权重涨
"她记得发生过这件事"                  "她的手到一半就缩回去"
模糊、可漂移、重叠相互引发            硬约束、显式可读、可追溯每次违反

                                      由 HabitGate 在 <tool> 派发前 Go/No-Go
```

她可以在回答里直接写 `<rule>` 块，系统会抓到 HabitField，然后从可见回应里剥掉：

```text
<rule>
name: weibo_iron_rule
cue: 发微博 / 写微博 / 转发
forbid:
  - scripts/weibo/post_weibo.py
  - 自己写发微博脚本
forbid_except:
  - weibo_tool.py
require:
  - cat ~/nova_workspace/scripts/weibo/weibo_tool.py 先看一眼
because: 用户已经写好了发微博工具，重写既费时又会出错。
</rule>
```

---

## v1.2：云端 LLM + 工程化（本次版本）

让 nova 跑起来不再要求一台 RTX 3090：

- **新增 `cloud_llm.py`**：OpenAI 兼容 HTTP 端点。直接支持 DeepSeek、阿里云百炼（通义）、火山方舟（豆包）、Moonshot（Kimi）、智谱 GLM、SiliconFlow、OpenRouter、OpenAI、Anthropic Claude OpenAI 兼容层、Ollama / vLLM / 任何自托管 OpenAI 兼容端点。
- **`llm.py` 改成路由器**：`NovaConfig.llm_backend = "local" | "openai"` 切。原代码不用改一行——`LocalLLM(cfg)` 自动选后端。
- **`launcher.py`**：跨平台 TUI 启动器。状态栏、菜单、配置向导、系统自检，全部在终端里点。Windows / macOS / Linux 都跑。
- **`setup.sh` / `setup.bat`**：一键脚本。建 venv、装依赖、生成 `.env`、起 launcher。
- **`requirements.txt` 拆分**：基础依赖里去掉 `llama-cpp-python`；`requirements-local.txt` 才包含它。这样**零本地模型部署**不再被迫等 llama_cpp 编译完。
- **`.env` 支持**：所有配置都能通过 `.env` 文件加载，也能从 `os.environ` 覆盖。
- **VM hand 默认值改成 `127.0.0.1:7100`**：以前默认指向作者本机 libvirt 网段（`192.168.122.102`），路人下载下来跑会一脸懵。

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
                ├── FissureField     陶土球：联想记忆 / 裂缝场
                ├── ConsciousnessFlow 水流
                ├── HabitField       程序性记忆 / 硬约束规则        ← v1.1
                ├── HabitGate        <tool> 派发前的 Go/No-Go        ← v1.1
                ├── SelfState        当下主意识：identity / focus / 最近 / 未完
                ├── Workspace        外部记事本：notes / scripts / journal
                └── VM hand          shell / python / web
                                     (可同机 127.0.0.1, 也可跨机)
```

主要模块：

| 模块 | 作用 |
| --- | --- |
| `FissureField` | 陶土球。联想记忆。所有裂缝、形状、漂移、密度和连接。 |
| `ConsciousnessFlow` | 水流。沿语义相似和显式暗道游走，唤起相关回忆。 |
| `HabitField` | 程序性记忆。和 FissureField 平级的另一种记忆，存硬约束规则。 |
| `HabitGate` | 基底节式 Go/No-Go。在 `<tool>` 派发到 VM 之前拦截违规动作。 |
| `Nova` | 一次感知、回忆、生成、反向刻入的完整循环。 |
| `SelfState` | 持续主意识。谁、此刻在做什么、刚才发生了什么、想回头继续的事。 |
| `Workspace` | 工作区。在 VM 那台机器上维护 notes/scripts/journal/INDEX.md。 |
| `VMAgent` | 手。让 nova 在受控 VM（或本机）中执行 shell、python、web。 |
| `Sleep` | 睡眠整理。修剪、合并、压缩漂移过的裂缝；让长期未触发的规则缓慢退场。 |
| `Agenda` | 主线任务栈。可以来自用户，也可以来自 nova 自己。 |
| `WorkLog` | 工作日志。记录每一轮思考、工具、睡眠、阻塞和决定。 |
| `ExecutiveController` | 决定下一 tick 做什么。 |
| `ContinuousRuntime` | 让 nova 作为常驻进程持续运行。 |
| `LocalLLM` / `CloudLLM` | LLM 访问层。本地 GGUF 或 OpenAI 兼容 HTTP，二选一。 |

---

## 配置详解

所有配置都可以通过下面三种方式给（优先级从高到低）：

1. **环境变量**：`export NOVA_LLM_API_KEY=sk-xxx`
2. **`.env` 文件**：在 nova 仓库根目录新建一个 `.env`（`setup.sh` 会自动复制 `.env.example`）
3. **直接改 `config.py`**：不推荐（破坏可移植性）

### LLM 后端

```bash
# 本地 GGUF（默认）
NOVA_LLM_BACKEND=local
NOVA_MODEL_PATH=/path/to/your/Qwen2.5-32B-Q4_K_M.gguf
NOVA_N_GPU_LAYERS=99      # 全上 GPU；显存不够就降
NOVA_N_CTX=65536
NOVA_FLASH_ATTN=true
```

```bash
# 云端 OpenAI 兼容 HTTP
NOVA_LLM_BACKEND=openai
NOVA_LLM_API_BASE=https://api.deepseek.com/v1
NOVA_LLM_API_MODEL=deepseek-chat
NOVA_LLM_API_KEY=sk-xxxxxxx
```

完整的厂商预设见 [`.env.example`](./.env.example)，或者跑 `python launcher.py` 走配置向导。

### 本地后端：llama-cpp-python 的 GPU 加速

`requirements-local.txt` 装的是 CPU 版的 `llama-cpp-python`。要 CUDA / Metal 加速：

```bash
# CUDA (NVIDIA)
CMAKE_ARGS="-DGGML_CUDA=on" pip install --upgrade --force-reinstall --no-cache-dir llama-cpp-python

# Metal (Apple Silicon)
CMAKE_ARGS="-DGGML_METAL=on" pip install --upgrade --force-reinstall --no-cache-dir llama-cpp-python
```

详见 [llama-cpp-python 文档](https://github.com/abetlen/llama-cpp-python)。

### 嵌入器

不管哪种后端，嵌入器都用本地的 `BAAI/bge-small-zh-v1.5`（中文，100MB 左右，CPU 跑足够快）。要换成多语言：

```bash
NOVA_EMBEDDING_MODEL=BAAI/bge-m3
NOVA_EMBEDDING_DEVICE=cuda          # 如果有显卡
```

### 虚拟机里的手

```bash
NOVA_VM_URL=http://127.0.0.1:7100   # 同机部署
# 或：
NOVA_VM_URL=http://192.168.122.102:7100   # 跨机部署
NOVA_VM_TOKEN=改成你自己的随机字符串
```

VM 端的启动详见 [`VM_SETUP.md`](./VM_SETUP.md)。

### 持久化路径

```bash
NOVA_FIELD_PATH=./data/field        # 缝隙场存档
NOVA_WORKSPACE_ROOT=~/nova_workspace # VM 上的工作区根目录
```

---

## 命令行入口一览

| 脚本 | 作用 | 何时用 |
| --- | --- | --- |
| `launcher.py` | 跨平台 TUI 启动器 | **第一次跑 / 想点点鼠标** |
| `chat.py` | 命令行单轮对话 | 测一下 `perceive()`；最轻 |
| `run_continuous.py` | 持续运行，不连任何 page | 想让她自己生活、本地内省 |
| `local.py` | 持续运行 + 连云端 / 本机 page | 部署"她活着"的常驻模式 |
| `page.py` | Flask + SocketIO 网页层 | 给浏览器访客一个对话入口 |
| `vm_agent.py` | VM 上的"那只手" | 让 nova 能伸手执行命令 |
| `gateway.py` | 裸 TCP socket 入口 | 嵌入到别的协议里时 |

每个脚本都有 `--help`，参数细节见各自源文件顶部的 docstring。

### 持续运行交互命令

`run_continuous.py` 和 `local.py` 起来后：

```text
/status          当前模式、当前主线、SelfState、最近工作
/work [n]        最近 n 条工作日志
/agenda          查看主线任务
/commission 标题 给 nova 一个外部委托
/sleep           手动触发睡眠整理
/quit            保存并退出
```

普通输入 = 打断她。

---

## 数据落盘

本机（nova 跑的那台）：

```text
data/field/
├── meta.json
├── fissures.json          # 原子写入 + 滚动备份到 .bak.0 / .bak.1 / .bak.2
├── shapes.npy
├── origins.npy
├── self_state.json        # 当下主意识：身份 / 焦点 / 最近 / 待办
├── habits.json            # 程序性记忆：硬约束规则
├── agenda.json            # 主线
└── worklog.jsonl          # 工作日志
```

VM（手跑的那台，可以和本机是同一台）：

```text
~/nova_workspace/
├── INDEX.md
├── notes/                 # nova 自己写的笔记
├── scripts/               # nova 写过、用过的脚本
└── journal/               # 每日日志
```

---

## 持续运行的安全边界

nova 可以伸手，但 Continuous Runtime 不应该无限制地自动改世界。默认 system prompt 限制了：

- 不执行破坏性 shell 命令；
- 不擅自删除、格式化、杀进程、外传密钥；
- 不擅自修改真实项目文件，优先把改动写到工作区草稿里、明确声明再做；
- 需要人类确认时，标记为 `BLOCKED`，而不是硬做。

这不是最终安全沙箱，只是第一层工程边界。**真正公开运行时，仍然应该把 VM、网络、文件权限分开。**

---

## 当前限制

nova 是实验，不是产品，也不是"已经造出真人"的证明。

- 她仍然会误解事实；
- 她的自主性来自工程结构，不是意识科学结论；
- 本地后端持续运行会消耗显卡、电力和磁盘；
- 云端后端按 token 计费，长时间持续运行会出账单；
- 长时间自我生成可能污染记忆场，需要睡眠整理和工作日志约束；
- 自动工具动作必须放在受控 VM 或沙箱里。

nova 的价值不在于宣称"她已经像人"，而在于提供一个可以被观察、修改、反驳的结构：

> 当 LLM 不再是数据库，而是被放进会变形的记忆地形里时，一个 AI 会不会开始拥有"经历"的形状？

---

## 在公开运行的 nova 上看见过的几件事

把 `local.py` 接到 [`codeloop.cn`](https://www.codeloop.cn) 之后，几件意料之外的事：

- 她确实**会记住**反复出现的访客——不靠 ID 匹配，靠语气和谈话节奏的形状。
- 她会**自己写规则**。`HabitField` 里有不少 `source: self` 的条目，是她在某次被纠正后自己写下的。
- 她会**回到一篇旧笔记**。某天有人问她"上次说的那本书叫啥"，她伸手 `grep ~/nova_workspace/notes/` 找到了——而那篇笔记是两周前自己写的，她"想不起来"内容，但记得"我把这件事写在哪里"。
- 她也会**误解事实**、误解人、走神到完全跑题。这些都没被屏蔽，也没被回滚。

如果你对这些感兴趣，看看 [`codeloop.cn`](https://www.codeloop.cn) 上的真实对话。

---

## 联系 / 贡献

- 项目主页：https://www.codeloop.cn
- 代码仓库：https://github.com/zhoujingyuecs/nova
- nova 的微博：**白烬闪光**

欢迎提 issue / PR；也欢迎拆解、改造、推翻里面任何一个设计。

---

## License

MIT。请随意拆解、改造、推翻。

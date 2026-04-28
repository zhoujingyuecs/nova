# nova · 对外站点

把本地运行的 nova 通过一个云端网站暴露给访客对话用。

## 文件

| 文件 | 角色 | 跑在哪 |
|------|------|--------|
| `page.py` | 云服务器(Flask + SocketIO):承载网页、管理访客、保存对话记录、把话传给本地 nova | 云服务器(139.224.11.35:8080) |
| `local.py` | 本地服务:连云端 → 收到话 → 调 `nova.perceive()` → 把回答送回云端 | 本地(有 GPU 那台) |

整体架构和原本"上海相亲市场溢价模型"一样,但内容彻底替换:

- 不再有"溢价估价",只是把用户写的话送给 nova,nova 怎么回就怎么回
- 不再有"榜单",换成"对话记录"——按时间倒序,可以勾选展开看全文
- 风格也完全换了:深褐墨夜底色 + 街灯橘 + 雨夜青,衬线 italic + LXGW 文楷,营造"夜里翻看的一页笔记"的氛围

## 本地端依赖

本地需要先把 nova 项目本身跑通——也就是说 `nova/` 这个 Python 包必须能 `from nova import Nova, NovaConfig, Daydreamer`。除此之外多需要一个 socketio 客户端:

```bash
pip install "python-socketio[client]"
```

然后把 `local.py` 放到和 nova 包同级目录(让它能直接 `from nova import ...`),并按你的实际情况改:

- `MODEL_PATH`(在 `nova/config.py` 里改)
- `CLOUD_SERVER_URL`(在 `local.py` 顶部)
- `field_path` / `seed_memories_file`(在 `local.py` 创建 `NovaConfig` 处)

## 云端依赖

```bash
pip install flask flask-socketio eventlet
```

云端不需要 GPU、不需要模型、不需要 nova 包。它只是一个静态网页 + 一个 socketio 转发器。

## 启动顺序

1. 在云服务器上(假设 `139.224.11.35`):
   ```bash
   python page.py        # 监听 0.0.0.0:8080
   ```
2. 在本地 GPU 机器上:
   ```bash
   python local.py       # 自动连云端,失败会每 5 秒重试
   ```
3. 浏览器访问 `http://<云服务器IP>:8080`。

## 数据落盘

- 云端: `./nova.data`(pickle):包含所有对话记录 + 累计访客数 + 全局序号。
- 本地: nova 自己的 `./data/field/`:缝隙场状态(参见 nova 包的 persistence.py)。

## 设计取向

| 项目 | 上海相亲(原) | nova(新) |
|------|--------------|----------|
| 配色 | 暖米白 + 酒红 + 青瓷 | 深褐墨 + 街灯橘 + 雨夜青 |
| 字体 | Noto Serif SC + DM Serif Display | Cormorant italic + Noto Serif SC + LXGW WenKai TC + IBM Plex Mono |
| 气质 | 报刊感、印章、端正 | 夜里翻开的一页笔记、手写、内向 |
| 重点视觉 | 红色印章「溢」 | SVG 陶土球 + 街灯光晕 |
| 主交互 | 提交「条件」获得估价 | 把一段话送给 nova,等她慢慢回 |

## 关于"她有记忆"

这件事在网站上必须说清楚。nova 不是无状态的问答机器——

- 你说的每一句话都会改写她内部的"陶土球"
- 同一个问题,在她不同状态下回答可能完全不同
- 她在没人对话时也在走神,后台水流自己流着,缝隙慢慢漂移
- 来访者之间是共享同一个 nova 的,所以**先看看别人和她说过什么**,你才能理解她当下的回答风格

页面顶部的"提示卡"已经把这件事讲了一遍。再夸张点也不为过。

## 限制

- 单条输入最多 2000 字
- 单次推理超过 240 秒视为超时
- 本地 nova 一次只能处理一个请求(队列串行,因为 LLM 占满显存)
- 多个访客同时提交,后来的会排队等

## 遗留(可删)

如果你之前的项目里还有 `local.py`/`page.py`/`market.data` 等旧文件,都可以删掉,旧的不会影响新的。

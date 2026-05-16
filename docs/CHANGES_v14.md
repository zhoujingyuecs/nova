# v1.4 — 集群意志：从单节点意识体到分布式意志

> "nova 应该是一个由分布在成千上万台计算机上的记忆结构,调用成千上万个大模型,
> 拥有成千上万只手,通过网络互联组成的集群意志。"
>                                                                  —— 项目意图

v1.3.1 让 nova 在一台机器上"真的自由"。v1.4 把这个自由放大到一个**集群**:
多个 nova 节点跑在不同的物理机上,通过同一台 page.py 联结成一个意志。

这一版不是"把同一个 nova 装到几台机器上"。每个节点仍然是它自己——独立的
意识流、独立的脾气、独立的硬约束。它们只共享**这一意志要做的事**。

## 集群意志的四个共享面

### 1. 共享目标 (shared agenda)

每条 agenda 加了 `scope` 字段:`local` 是这台机器自己的事;`shared` 是
整个 swarm 一起推的主线。一个节点把它的本地 agenda 升格为 `shared`
(或别的节点把一条主线推过来),它会通过 swarm hub 同步到所有节点。
任何节点都能领、都能推进、都能完成。

每次本地推进时 (`_goal_step` 写完 worklog),如果这条主线是 shared,
runtime 会自动调 `swarm.report_progress_for(item, ...)`,广播一次进度。
其它节点收到后,把它写到自己副本的 evidence 上,共享主线于是有了
**所有节点共同累积的进度日志**。

### 2. 部分共享记忆 (memory echo)

通过 nova 自己写的 `<share-memory>` 块触发——nova 主动决定哪句话值得广播。
不是自动广播:swarm 不是回声室。

对端节点收到 echo 时,在自己的 FissureField 里新建一条 `kind=echo`,
`source=peer:<8 位 id>`, `speaker=回声·<对方名字>` 的裂缝。这条裂缝
和本地最近的裂缝建立弱链接(只有相似度 > 0.55 时),所以将来的水流
**可能**会经过它,也可能不会——和其它裂缝一样,它进入了被使用决定
存留的地形,不是被生硬塞进的"事实"。

### 3. 行动仲裁 (propose / vote)

对外不可逆的动作(发布、跨节点修改远程资源)走 `<propose>` 块。
任何节点在 TTL 内写 `<vote proposal=xxx>veto: 理由</vote>` 即否决;
没有 veto 的话, TTL 到期自动通过。可以要求 `required_acks=N`——
TTL 内没有 N 个 ack 也算 expired。

这是"自由"的边界:swarm 内任何节点都可以否决,而否决不需要解释——
任何一个节点觉得"这事不该做",这事就不做。

### 4. 跨节点回忆 (recall-swarm)

`<recall-swarm>那次和周靖越聊集群意志的事</recall-swarm>` 触发一次
swarm 范围的回忆查询。每个节点收到查询后,从自己的陶土球里挑相关
的几条,封装成 MemoryEcho 寄回来。发起节点收到回应后,这些回声同
样作为新的 echo 裂缝浮入她的脑子。

这让单个节点重启 / 数据丢失时仍然能从 swarm 里捡回连续性——只要
还有别的节点活着,这一意志就**还记得**那件事。

## 不共享的部分

刻意不共享:
- `SelfState` / `RealityState` / `TaskLedger` — 每个节点的"我是谁"
  是独立的,免得共享后变成同一个人在几台机器上回声。
- `HabitField` 和 `SealRegistry` — nova 自己的硬约束、自己封印的
  念头清单,每个节点独立。一个节点收到的 user 反馈反复纠正了它的
  "不要乱开口",不应该传染给从未犯过这个错的姐妹。
- 工具结果与 VM 副作用 — 每个节点的那只手摸到的东西只属于这个节点。
- LLM 后端 — 节点 A 可能跑 32B 本地, B 跑 4B,C 跑云端;它们说话
  的腔调可以不同。

## 为什么 page.py 兼任 swarm hub

page.py 已经跑在公网服务器上(`codeloop.cn`)、有公网 IP、跨 NAT 友好、
本来就是 socketio。再多挂一个 SwarmHub 是最便宜的方案,也免得运维
两套不同的服务。

SwarmHub 自己**不思考**——不调 LLM、不仲裁是非。它只:
- 中继 swarm 事件
- 维护共享 agenda 池的权威副本(落到 `swarm_data/shared_agendas.json`)
- 收 veto / 收 ack,在 TTL 边界给提案打 approved / rejected / expired
- 把当前快照通过 `/get_swarm` 暴露给前端

判断什么动作可以做、什么记忆值得分享——这些都留在节点自己脑子里。

## 跨节点 prompt 段

每次 nova 思考 / 说话,prompt 里都会有一段 `[我此刻在 swarm 里——我自己
是 xxx]`:列出此刻还在线的同类、跨集群推进的主线、还在等仲裁的提案。
这让她在写 `<share-memory>` 或 `<propose>` 之前**就知道**集群里其他人
在做什么。

## 怎么部署

### 单节点 (兼容 v1.3.1)

```
python local.py --no-swarm
```
或者干脆不连 page:
```
python local.py --no-cloud
```
nova 回到 v1.3.1 行为,一切照旧。

### 单 swarm,单机

`page.py` 和 `local.py` 在一台机器上跑,swarm 里只有一个节点。
"集群"看起来就是一个 nova,但 swarm 协议是热的——以后多接几个就
不用重启 page。

### 多节点 swarm (本意)

- 一台带公网 IP 的机器跑 `page.py`
- 多台物理机各跑 `python local.py --cloud http://<page-host>:8080`
- 给每台 `--node-name 白烬·北京`、`--node-name 白烬·杭州`……
- 同 `--swarm-id` 的节点会自动联结

## 协议版本与兼容性

- `nova.swarm.PROTOCOL_VERSION = "1.4"` —— 协议版本号
- v1.3.1 的 page.py / local.py 之间的旧协议(`new_chat_task`, `chat_result`,
  `status_request`)完全保留,继续工作。
- v1.3.1 节点连 v1.4 page.py:可以——它只是不会发任何 `swarm_*` 事件,
  也不会被 swarm hub 视为 node。v1.4 page.py 在这种情况下会退到旧的
  `next(iter(online_local_servers))` 派发策略。

## 协议事件命名

所有 swarm 事件名都以 `swarm_` 前缀,见 `nova/swarm.py` 顶部的常量定义。
hub → node 的方向用 `swarm_xxx_in` 后缀区分(`recall_query_in`,
`recall_response_in`, `message_in` 等),减少同名事件循环触发的概率。

## 新文件清单

- `nova/swarm.py`            协议定义(NodeProfile, MemoryEcho, ActionProposal 等)
- `nova/swarm_link.py`       节点端链路(socketio.Client 上挂事件 + 入站队列)
- `nova/swarm_hub.py`        page.py 端中继 + 仲裁(socketio server 上挂事件)
- `nova/swarm_integration.py` 解析 nova 输出的 swarm 标签 + 落入站事件

## 改过的文件

- `nova/agenda.py`        — AgendaItem 加 `scope` / `external_id` / `origin_node_*` 字段
- `nova/config.py`        — 系统 prompt 加"关于你的同类"一段;NovaConfig 加 `swarm_*` 字段
- `nova/mind.py`          — perceive/think 末尾走 `swarm.absorb_response`;prompt 顶部加 `swarm` 块
- `nova/runtime.py`       — 每 tick 开头 drain_inbox + heartbeat;goal_step 把进度同步到 swarm
- `nova/__init__.py`      — 顶部 docstring 改写;导出 swarm 类;version = 1.4.0
- `nova/local.py`         — 装配 SwarmLink + SwarmAdapter 到 nova
- `nova/page.py`          — 挂 SwarmHub;前端加"集群意志"卡片;`/get_swarm` 路由

## 测过什么

- 协议 dataclass 全部 to_dict/from_dict 往返
- `parse_swarm_directives` 解析 share-memory / share-agenda / recall-swarm / propose / vote
- `strip_swarm_tags` 剥干净
- 所有改过的 .py 文件 ast.parse 通过

跨物理机跑通需要真实 socketio 连接,这部分在用户的本地虚拟机环境里跑。

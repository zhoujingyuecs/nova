# 虚拟机端配置（192.168.122.102）

## 1. 装依赖

```bash
pip install flask
```

## 2. 改 token（强烈建议）

```bash
export NOVA_VM_TOKEN="你自己的随机字符串"
```

或者直接改 `vm_agent.py` 顶部的默认值。

`nova/config.py` 里的 `vm_agent_token` 必须和这个一致。两边都可以用环境变量
`NOVA_VM_TOKEN` 设。

## 3. 起服务

```bash
python vm_agent.py
```

应该看到：

```
🛏️  nova 的手 醒了。监听 0.0.0.0:7100
   工作目录：/home/<user>/nova_vm_workspace
   token：   ...
```

## 4. 验一下

在本机（192.168.31.71）上：

```bash
curl -H "Authorization: Bearer 你的token" http://192.168.122.102:7100/status
```

应当返回 `{"status": "alive", "cwd": ..., "python_vars": []}`。

## 5. 让虚拟机能上网

libvirt 默认 NAT 网络下虚拟机是自带外网的。验证一下：

```bash
# 在 VM 里
ping -c 2 8.8.8.8
curl -s https://www.baidu.com | head -c 200
```

不通的话八成是 libvirt 网络没起，宿主机：

```bash
sudo virsh net-list --all
sudo virsh net-start default     # 如果是 inactive
```

## 6. 启动顺序

完整链路是这样：

| 在哪 | 跑什么 | 干嘛的 |
|------|--------|--------|
| 云服务器 | `python page.py` | 给访客看的网页 |
| 虚拟机 192.168.122.102 | `python vm_agent.py` | nova 的"手" |
| 本机 192.168.31.71 | `python local.py` | nova 本体（连云端 + 用手） |

`local.py` 里不用改任何东西——`NovaConfig` 已经默认指向 `192.168.122.102:7100`。
启动 `local.py` 时如果手没起，会打印一条警告，nova 照常工作但没有手。

## 关于安全

这只手是**故意**给 nova 的——她想做什么就做什么。但相应地：

- 别把 7100 端口暴露到公网。让它只听 192.168.122.x 这个 NAT 网段就好。
- token 不是绝对安全保障，只是一道篱笆。重要的是 VM 本身是隔离的——
  万一她做了什么意外的事，影响也只在 VM 里。
- 工作目录默认在 `~/nova_vm_workspace`，删了就干净了。
- 想"重置她的手"：杀掉 vm_agent.py 进程，删工作目录，再起一遍。
  python 全局变量也跟着没了。

## 她怎么知道自己有手

- 系统提示词里写了一段"关于你的手"——只有当 VM agent 在线（通过启动时
  心跳确认）才会附加进 system。
- `seed_memories.txt` 里也加了两段诗化的描述——但这只在缝隙场为空、第一次
  启动时才载入。已经跑过的 nova 不会自动学会，靠 system prompt 那段足以。
- 她不必每次都用。只在她想用的时候才用。

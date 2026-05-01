# 豆包网页版自动化工具 v2

## 关于验证码

使用 cookie 注入的方式（v1 的做法）很容易触发字节系的 RMC 验证码（`rmc.bytedance.com/verifycenter`）。原因：

- 浏览器的 `localStorage` / `IndexedDB` / 设备指纹和你 cookie 来源那台浏览器对不上
- 虚拟机的 IP 跟你日常登录的 IP 不一致
- headless Chromium 的指纹本身就有特征

**正确做法**：在虚拟机上**直接登录一次**，把整个浏览器配置（profile）持久化保存下来，之后自动化复用同一个 profile。这样豆包看到的就是一个真实、连续的浏览器会话，跟人正常用没区别。

虚拟机没有图形界面也没关系——脚本自带的 `login` 子命令会启动一个带远程调试端口的浏览器，你从本地电脑通过 SSH 隧道连过去操作。

---

## 一、环境准备（一次性）

```bash
pip install playwright
playwright install chromium
sudo playwright install-deps
```

如果第三步失败，手动装：

```bash
sudo apt-get update
sudo apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0
```

---

## 二、第一次登录（建立持久化 profile）

### Step 1：在虚拟机上启动 login 模式

```bash
python3 doubao_chat.py login --profile-dir ./doubao-profile
```

启动后脚本会打印操作指引。这个进程会一直挂着，等你登录完按 Ctrl+C 才退出。

### Step 2：在你本地电脑做 SSH 隧道

新开一个终端：

```bash
ssh -L 9222:127.0.0.1:9222  你的用户名@虚拟机IP
```

> 把 9222 端口转发到本地。这条 SSH 连接保持着不要断。

### Step 3：用本地 Chrome 远程控制虚拟机的浏览器

1. 打开本地的 Chrome 或 Edge，地址栏访问：
   ```
   chrome://inspect/#devices
   ```
2. 点 **Configure...** 按钮，添加一行 `localhost:9222`，确认。
3. 等几秒，**Remote Target** 区会出现一个豆包页面，点它下面的 **inspect**。
4. 弹出的 DevTools 窗口里就是远程那个浏览器的页面。在里面：
   - 完成豆包登录（扫码 / 验证码 / 短信都行）
   - 如果有滑块验证码，正常拖
   - 登录完后停留几秒确认状态正常

### Step 4：保存退出

回到虚拟机的终端，按 **Ctrl+C**。脚本会保存 profile 到 `./doubao-profile/`。

> 这一步只用做一次。profile 一般能用很久，被风控了再做一次。

### 可选：方式 B / C

如果你本地有 X server（Linux/Mac），可以走 X 转发：

```bash
# 本地：
ssh -X 你的用户名@虚拟机IP
# 虚拟机上：
python3 doubao_chat.py login --profile-dir ./doubao-profile --no-headless
```

会直接弹出真实浏览器窗口转发到本地。

或者装 VNC，用 `--no-headless` 启动后 VNC 连过来操作。

---

## 三、日常使用

```bash
# 把要发的内容写到 input.txt
echo "用一句话解释什么是傅里叶变换" > input.txt

# 跑 chat 子命令，复用之前建立的 profile
python3 doubao_chat.py chat \
    -i input.txt \
    -o output.txt \
    --profile-dir ./doubao-profile

cat output.txt
```

完整参数：

```
chat 子命令：
  -i, --input            输入文件（要发送的内容）            [必填]
  -o, --output           输出文件                             [必填]
  --profile-dir          持久化浏览器配置目录（推荐）
  -c, --cookies          cookie 文件（备用方式，容易触发验证码）
  -u, --url              豆包对话 URL
  --stable-seconds       回复多少秒不变就视为完成（默认 4）
  --max-wait             最长等待秒数（默认 600）
  --debug-dir            出错时保存截图和 HTML 到这里

login 子命令：
  --profile-dir          profile 目录                         [必填]
  --cdp-port             远程调试端口（默认 9222）
  --url                  登录入口（默认 doubao.com 首页）
  --no-headless          非无头模式（需要 X 转发或 VNC）
```

---

## 四、注意事项

- **不要把 profile 目录通过 git / scp 公开传播**——里面包含完整的登录态。
- **远程调试端口不要直接暴露公网**。脚本绑了 0.0.0.0 是为了 SSH 隧道方便（127.0.0.1 也能转发，但部分配置下 SSH 隧道用 0.0.0.0 更稳）。如果担心，可以改成 127.0.0.1 然后 SSH 隧道照样能转。
- **profile 失效的信号**：跑 chat 时报 "找不到输入框" 或 "弹出验证码"，重新跑 login 即可。
- **不要高频调用**：模拟用户的脚本被高频触发依然会触发风控。需要批量跑就在每次之间 `sleep` 几秒。
- **同账号多设备登录**：在你本地浏览器和虚拟机上同时登录通常没问题。某些情况下可能互相挤掉，挤掉就重新 login。

---

## 五、出错排查

加 `--debug-dir ./debug` 跑一次，出错时会保存截图和页面 HTML：

```bash
python3 doubao_chat.py chat -i in.txt -o out.txt \
    --profile-dir ./doubao-profile --debug-dir ./debug
```

把 `debug/` 下的文件 scp 回本地查看。

| 现象 | 处理 |
|---|---|
| 弹出 captcha | 重新跑 login，过验证码 |
| "找不到输入框" 且 URL 含 login/passport | profile 登录态没了，重新 login |
| "找不到输入框" 但页面看起来正常 | 选择器没命中，看 debug HTML，把对应选择器加到脚本 `INPUT_SELECTORS` |
| 回复被截断 | `--stable-seconds 8 --max-wait 1200` |
| 浏览器启动报 .so 错误 | `sudo playwright install-deps` |

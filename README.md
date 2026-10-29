<div align="center">
    <a href="https://v2.nonebot.dev/store">
    <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="logo"></a>

## ✨ nonebot-plugin-parsehub ✨

[![LICENSE](https://img.shields.io/github/license/ByteColtX/nonebot-plugin-parsehub.svg)](./LICENSE)
[![pypi](https://img.shields.io/pypi/v/nonebot-plugin-parsehub.svg)](https://pypi.org/project/nonebot-plugin-parsehub/)
[![python](https://img.shields.io/badge/python-3.12|3.13-blue.svg)](https://www.python.org)
[![uv](https://img.shields.io/badge/package%20manager-uv-black?style=flat-square&logo=uv)](https://github.com/astral-sh/uv)
<br/>
[![ruff](https://img.shields.io/badge/code%20style-ruff-black?style=flat-square&logo=ruff)](https://github.com/astral-sh/ruff)
[![CI](https://github.com/ByteColtX/nonebot-plugin-parsehub/actions/workflows/ci.yml/badge.svg)](https://github.com/ByteColtX/nonebot-plugin-parsehub/actions/workflows/ci.yml)

</div>

## 📖 介绍

基于 [ParseHub](https://github.com/z-mio/ParseHub) 的社交媒体链接解析插件：群里出现受支持的分享链接时自动解析，发回视频 / 图集 / 图文，并配一张 App Store「Today」风格的信息卡片。

> 仅支持 **OneBot v11** 适配器（NapCat / SnowLuma / LuckyLilliaBot 等）。

- **自动解析**：发链接、分享文案或 B 站 BV 号即可，也可用 `/解析` 命令手动触发
- **按会话开关**：`解析开关` 控制某个群 / 私聊，超管和群管理可操作
- **24 个平台**：Bilibili、抖音、小红书、微博、快手、知乎、贴吧、豆瓣、今日头条、微信公众号、微信视频号、酷安、皮皮虾、最右、小黑盒、Bangumi、X/Twitter、Instagram、YouTube、TikTok、Threads、Facebook、Snapchat、Medium
- **卡片 + 媒体**：先发信息卡片，再发视频 / 图集；2 个以上媒体合并转发，单个作品最多 20 个
- **直播间封面卡片**：B 站直播间链接（含 b23 短链）只发一张封面卡片——直播中取当前画面帧，未开播取静态封面；出不了卡就走常规失败路径
- **音频提取**：`bm` / `ym` 指令下载 B 站 / YouTube 视频音频，默认发 record 语音，`-f` 以文件发送（文件名用作品标题）
- **表情回应**：给触发消息点表情（收到 / 成功 / 失败），不引用原消息
- **成本控制**：体积 / 时长上限、会话内去重、并发限制、临时文件自动清理
- **缓存与投递**：同一作品 24 小时内直接发本地缓存；投递方式 `file` / `http` / `base64` 三选一

## 🎨 效果图

| B 站视频卡片 | 抖音图集卡片 |
| :---: | :---: |
| <img src="https://raw.githubusercontent.com/ByteColtX/nonebot-plugin-parsehub/main/docs/card-bilibili.jpg" width="420" alt="B 站卡片"> | <img src="https://raw.githubusercontent.com/ByteColtX/nonebot-plugin-parsehub/main/docs/card-douyin.jpg" width="420" alt="抖音卡片"> |

## 💿 安装

使用 nb-cli 安装

在 nonebot2 项目的根目录下打开命令行, 输入以下指令即可安装

```bash
nb plugin install nonebot-plugin-parsehub --upgrade
```

使用 uv 安装

在 nonebot2 项目的根目录下, 打开命令行, 输入以下指令即可安装

```bash
uv add nonebot-plugin-parsehub
```

安装仓库 main 分支

```bash
uv add git+https://github.com/ByteColtX/nonebot-plugin-parsehub@main
```

用 uv 安装后还需要在 nonebot2 项目根目录的 `pyproject.toml` 里注册插件
（nb-cli 会自动完成这一步）：

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_parsehub"]
```

## ⚙️ 配置

在 nonebot2 项目的 `.env` 文件中添加下表中的配置, 全部可选（零配置即可加载运行）

| 配置项 | 必填 | 默认值 | 说明 |
| :---  | :---: | :---  | :--- |
| `PARSEHUB_AUTO` | 否 | `true` | 群里出现链接时自动解析（总开关） |
| `PARSEHUB_MAX_FILE_MB` | 否 | `100` | 单个媒体文件体积上限（MB），超过直接跳过；`0` = 不限 |
| `PARSEHUB_MAX_SECONDS` | 否 | `300` | 视频时长上限（秒），超过只发封面 + 一句提示、不下载；`0` = 不限 |
| `PARSEHUB_PLATFORMS` | 否 | 无 | 平台白名单：只解析这些平台，留空 = 全部。可写平台 id（`xhs`）或中文名（`小红书`） |
| `PARSEHUB_DEDUPE_SECONDS` | 否 | `120` | 同一会话内同一链接的去重窗口（秒），`0` = 不去重 |
| `PARSEHUB_CONCURRENCY` | 否 | `2` | 同时处理的任务数 |
| `PARSEHUB_PROXY` | 否 | 无 | 解析 / 下载代理，如 `http://127.0.0.1:7890` |
| `PARSEHUB_PROXY_PLATFORMS` | 否 | 海外 8 平台 | 只对这些平台走代理，留空 = 全部走（列表要用合法 JSON） |
| `PARSEHUB_COOKIE` | 否 | 无 | 兜底 cookie（优先读 parsehub 自己的 `cookies.toml`） |
| `PARSEHUB_TIMEOUT` | 否 | `180` | 解析 + 下载总超时（秒） |
| `PARSEHUB_CACHE_TTL_HOURS` | 否 | `24` | 媒体缓存保留小时数，命中就直接发本地那份；`0` = 关闭缓存 |
| `PARSEHUB_CACHE_MAX_MB` | 否 | `1024` | 媒体缓存总容量上限（MB），超了按最久没用过的删 |
| `PARSEHUB_CARD` | 否 | `true` | 是否发信息卡片图（失败自动退回文字） |
| `PARSEHUB_CARD_FONT` | 否 | 无 | 中文字体路径，留空则自动找系统字体 / 下载 |
| `PARSEHUB_FORWARD_TIMEOUT` | 否 | `60` | 合并转发等待协议端的秒数，超时才逐条发 |
| `PARSEHUB_SEND_TEXT` | 否 | `true` | 是否发送标题 / 正文 |
| `PARSEHUB_MAX_CHARS` | 否 | `300` | 正文最多发送多少字符 |
| `PARSEHUB_FAIL_NOTICE` | 否 | `true` | 视频发送失败时是否额外发文字提示 |
| `PARSEHUB_DELIVERY` | 否 | `file` | 媒体交给协议端：`file` = 本机路径（要求同机）/ `http` = 中转地址 / `base64` = 内容内联 |
| `PARSEHUB_RELAY_HOST` | 否 | 无 | 中转服务对外地址，留空自动探测内网 IP（仅 `http` 模式） |
| `PARSEHUB_RELAY_PORT` | 否 | `21473` | 中转服务端口（仅 `http` 模式） |
| `PARSEHUB_TASK_TTL_MINUTES` | 否 | `30` | 临时文件保留分钟数 |

> 列表类配置必须是合法 JSON（如 `PARSEHUB_PROXY_PLATFORMS=["twitter", "youtube"]`）；
> 数值项有下界校验（如 `PARSEHUB_TIMEOUT >= 5`）：低于下界（多数为负数）会在启动时直接报错；
> 标了 `0` = 不限 / 关闭的项除外。
> 哪些群 / 私聊解析由命令 `/解析开关` 控制，没有对应的配置项。

需要登录态的平台（X、Instagram、TikTok、豆瓣、微信视频号 等），cookie 优先从 ParseHub 自己的配置读取
（`ph set cookie <平台>`），也可以用 `PARSEHUB_COOKIE` 兜底。

## 🎉 使用

### 指令表

| 指令 | 权限 | 需要@ | 范围 | 说明 |
| `/解析 <链接、BV 号或分享文案>`（别名 `/ph`、`/解析链接`） | 群员 | 否 | 群聊/私聊 | 手动解析，不做去重拦截 |
| `/bm <BV 号或 B 站链接>` | 群员 | 否 | 群聊/私聊 | 下载 B 站视频音频，发 record 语音 |
| `/ym <YouTube 链接或视频 ID>` | 群员 | 否 | 群聊/私聊 | 下载 YouTube 视频音频，发 record 语音 |
| `/解析平台` | 群员 | 否 | 群聊/私聊 | 列出支持的平台 |
| `/解析开关 [开\|关]`、`/解析开`、`/解析关` | 主人/群管 | 否 | 群聊/私聊 | 按会话开关解析（私聊仅主人） |
| `/解析状态` | 群员 | 否 | 群聊/私聊 | 查看当前会话的开关状态 |

> 命令按 NoneBot 的 `COMMAND_START` 匹配（默认只有 `/`）。想让无前缀写法也算命令，把 `COMMAND_START` 加上 `""`。
> 命令名后必须是空白、标点或消息结尾：`php是世界上最好的语言`、`解析一下` 这类普通文本不会被当成命令。

### 触发规则

- 群里发链接或分享文案即自动解析，一条消息最多处理 **3 个**链接
- 只有 B 站 BV 号（如 `BV1xx411c7mD`）也会自动解析，和完整链接共用去重与缓存
- `/解析关` 后该会话不再自动解析；手动 `/解析` 只对超管 / 群管理有效
- 默认配置下（`COMMAND_START` 只有 `/`），不带前缀的「解析 <链接>」**不是**命令，会当作普通消息自动解析（带去重、不额外提示）

### 解析开关

- 以**会话**为维度（群按群号，私聊按对方 QQ），群和私聊**默认都开**
- 关掉后该会话不再自动解析，手动解析仅超管 / 群管可用；状态所有人可看
- 超管与群主 / 管理员可修改；状态存在 `sessions.json`，只记录与默认值不同的会话

### 媒体缓存

- **按作品跨会话共享**：A 群发过的视频，B 群再发直接发本地那份
- **命中即跳过**：不解析、不下载、不画卡（卡片图也一起缓存）
- **保留 24 小时**（命中续期），总量上限 1GB，超了按最久没用过的删
- **不同写法归一**：`b23.tv/xxx`、`bilibili.com/video/BVxxx` 与纯 `BVxxx` 是同一个作品
- 时间线：120 秒内同会话重复 → 静默；120 秒 ~ 24 小时 → 直接发缓存；超过 24 小时 → 重新解析
- 手动 `/解析 <链接>` 同样吃缓存（只是不做去重拦截）

### 音频指令（bm / ym）

- `bm` 认 B 站完整链接（含 `?p=N` 分P）、`b23.tv` 短链、分享文案里的链接和裸 BV 号；**不支持 AV 号**
- `ym` 认 `youtube.com`（含 music / shorts）、`youtu.be` 链接和单独的 11 位视频 ID；链接带 `list=` 等参数没关系，只取单个视频
- 默认发 record 语音段（QQ 客户端会转成语音消息）；加 `-f` / `--file` 改以群文件 / 私聊文件发送，文件名是作品标题（分P带 `- PN` 后缀），取不到标题回退 BV 号 / 视频 ID
- 与 `/解析` 共用：会话开关（关掉后仅超管 / 群管可用）、平台白名单、代理设置（YouTube 走 `PARSEHUB_PROXY`）、体积上限（B 站按码率 × 时长预估，yt-dlp 下载中还会拦一次）、超时与并发、媒体缓存（独立 key，24 小时内直接发本地那份）
- 时长上限对音频**不生效**（那是防大视频的）；直播 / 预告 / 番剧不给提取音频，会提示原因
- 音质说明：B 站免登录最高 192kbps AAC（大会员的杜比 / Hi-Res 不做）；QQ 语音本身是有损编码，要原文件请用 `-f`

### 说明

- B 站链接带 `?p=N` 时解析第 N P，不带时默认 P1；时长按该 P 计算；纯 BV 号按 P1
- 视频被协议端拒绝时只点失败表情，**不会**退回群文件
- 投递三选一、**互不兜底**：`file` 发本机路径（协议端同机）/ `http` 发中转地址（跨机）/ `base64` 内容内联（请求体约 +1/3）
- parsehub 全程同步阻塞，插件把所有调用放进 `asyncio.to_thread`；首次导入懒加载，不影响启动
- 卡片用 Pillow 画，封面三级兜底（`thumb_url` → 图片媒体 → 视频抽帧），渲染异常时自动退回文字（Pillow 是硬依赖，正常安装不会缺）；
  字体优先 `PARSEHUB_CARD_FONT` → 缓存目录 → 系统字体 → 下载（sha256 校验），缺字用回退字体补，emoji 用 Twemoji 位图字体

## 🛠️ 开发

```bash
uv sync --all-groups            # 安装依赖（含测试）
uv run poe check                # 代码检查 + 格式检查 + 测试（CI 同款）
uv run pytest -q                # 只跑测试
uv run poe live                 # 真实链路自检（解析 → 下载 → 中转取回）
```

架构与不变量见 [`ARCHITECTURE.md`](ARCHITECTURE.md)；需求 / 模块 spec 已归档在
`archive/specs` 分支，不再随 main 发布。

### 代码结构

```
src/nonebot_plugin_parsehub/
├── triggers.py         # matcher / 命令：只决定「处理谁」
├── service.py          # 编排：每个结局都是 Outcome 里的一个值
├── audio.py            # bm / ym 音频提取（B 站 playurl / yt-dlp）
├── delivery.py         # 三种上传模式 + 合并转发/逐条 + 统一失败记账
├── feedback.py         # 表情回应与用户提示文案
├── cache.py            # 作品级媒体缓存（哈希目录、原子写）
├── dedupe.py           # 会话级去重（claim / commit / release）
├── relay.py            # 只读中转服务（只服务 relay/）
├── parsehub_client.py  # ParseHub 懒加载 + 线程池封装
├── platforms.py        # 平台元数据（展示名 / 颜色 / logo / 时长单位）
├── keys.py             # 链接识别与作品 key 归一
├── switches.py         # 按会话的解析开关
├── media.py            # parsehub 结果 → MediaRef / MediaFile
├── errors.py           # 异常分类与用户文案
├── paths.py            # localstore 目录约定
├── budget.py           # 解析 / 下载共享的超时预算
├── config.py           # 配置读取与归一化
├── card/               # fonts / cover / layout + CardRenderer
└── app.py              # 装配与生命周期
```

模块级单例已全部移除：`App` 持有设置、客户端、缓存、去重、开关、中转、投递和卡片，
测试直接 `App.build(...)` 造独立实例。

### 代码风格

遵循 [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)：行宽 80，Google 风格文档字符串，
ruff 强制 `pydocstyle(google)` + `pep8-naming` + `isort`。

有意保留的偏差：包内用相对导入（本地插件加载时绝对自引用会 ImportError）；NoneBot 按名字导入；
中文文档字符串以「。」结尾（关闭 `D415`）；测试不强制文档字符串。

## 📄 许可

[MIT](LICENSE)

## 🙏 致谢

- [z-mio/ParseHub](https://github.com/z-mio/ParseHub)：本插件的解析能力全部来自这个项目

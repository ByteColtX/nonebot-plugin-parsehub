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

- **自动解析**：发链接、分享文案或 BV / AV 号即可，支持 B 站 / 抖音 / 小红书 / 微博 等 21 个平台（`/解析平台` 查全量）
- **信息卡片**：先发卡片、再发视频 / 图集；多个媒体或长正文平台的正文与资源合并成一条转发
- **直播间封面卡片**：B 站直播间（含 b23 短链）发一张封面——直播中取当前画面帧，未开播取静态封面
- **音频提取**：`bm` / `ym` 下载 B 站 / YouTube 音频
- **表情回应**：收到 / 成功 / 失败贴在原消息上

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
| `PARSEHUB_PROXY_PLATFORMS` | 否 | 海外 6 平台 | 只对这些平台走代理，留空 = 全部走（列表要用合法 JSON） |
| `PARSEHUB_COOKIE` | 否 | 无 | 兜底 cookie（优先读 parsehub 自己的 `cookies.toml`） |
| `PARSEHUB_TIMEOUT` | 否 | `180` | 解析 + 下载总超时（秒） |
| `PARSEHUB_CACHE_TTL_HOURS` | 否 | `24` | 媒体缓存保留小时数，命中就直接发本地那份；`0` = 关闭缓存 |
| `PARSEHUB_CACHE_MAX_MB` | 否 | `1024` | 媒体缓存总容量上限（MB），超了按最久没用过的删 |
| `PARSEHUB_CARD` | 否 | `true` | 是否发信息卡片图（失败自动退回文字） |
| `PARSEHUB_CARD_FONT` | 否 | 无 | 中文字体路径，留空则自动找系统字体 / 下载 |
| `PARSEHUB_FORWARD_TIMEOUT` | 否 | `60` | 合并转发等待协议端的秒数，超时才逐条发；同时决定每批媒体数量（60s ≈ 20 项） |
| `PARSEHUB_SEND_TEXT` | 否 | `true` | 是否发送标题 / 正文 |
| `PARSEHUB_MAX_CHARS` | 否 | `300` | 正文最多发送多少字符 |
| `PARSEHUB_FAIL_NOTICE` | 否 | `true` | 媒体发送失败时是否额外发文字提示 |
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
微信视频号必须用腾讯元宝（`yuanbao.tencent.com`）的登录 Cookie：
`ph set cookie weixin_channels`。

## 🎉 使用

### 指令

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `/解析 <链接、BV / AV 号或分享文案>`（别名 `/ph`、`/解析链接`） | 群员 | 手动解析，不做去重拦截 |
| `/bm <BV / AV 号或 B 站链接>` | 群员 | 下载 B 站视频音频，发 record 语音（`-f` 以文件发送） |
| `/ym <YouTube 链接或视频 ID>` | 群员 | 下载 YouTube 视频音频，发 record 语音（`-f` 以文件发送） |
| `/解析平台` | 群员 | 列出支持的平台 |
| `/解析开关 [开\|关]`、`/解析开`、`/解析关` | 主人/群管 | 按会话开关解析（私聊仅主人） |
| `/解析状态` | 群员 | 查看当前会话的开关状态 |

> 命令按 NoneBot 的 `COMMAND_START` 匹配（默认只有 `/`）；命令名后必须是空白、标点或消息结尾——
> 「解析一下」「php是世界上最好的语言」这类普通文本不会被当成命令。

### 行为要点

- 发链接、分享文案或裸 BV / AV 号即自动解析，一条消息最多 3 个链接；`/解析关` 后仅超管 / 群管能手动解析
- 解析开关按会话（群 / 私聊），默认全开，只把改过的会话写进 `sessions.json`
- 同作品跨会话共享缓存：命中直接发本地那份（含卡片）；`b23.tv` / 完整链接 / 裸 BV / 裸 AV（换算成 BV）算同一作品；
  同会话 120 秒内重复不发内容，解析后才认出是同一作品的只补一个成功表情，超过 24 小时重新解析
  （手动 `/解析` 吃缓存、不去重）
- B 站链接带 `?p=N` 解析第 N P（默认 P1）；`bm` 认完整链接 / 短链 / 分享文案 / 裸 BV / 裸 AV（等价换算成 BV）；
  `ym` 认 `youtube.com`（含 music / shorts）、`youtu.be`、单独 11 位视频 ID
- `music.youtube.com` 自动按音乐处理：卡片标「音乐」，只下载音频并发 `record` 语音，不下载视频
- YouTube 视频自带 ffmpeg 合并音视频（系统已装则优先用系统），没装 ffmpeg 也不会再发无声视频
- 音频默认发 record 语音，B 站免登录最高 192kbps AAC；与 `/解析` 共用开关、白名单、
  代理与体积上限；时长上限对音频不生效；直播 / 预告 / 番剧不给提取
- 解析结果没给时长（视频号 / Threads 必现，其它平台偶发）：下载前用 ffmpeg 只读容器头补出真实时长
  （不落盘、不拉完整文件），时长上限照常生效；探测失败按未知时长继续，由体积上限与总超时兜底
- 单个作品最多发 **100 项**，按单次转发容量分批（默认每批 20 项）；超出 100 项的部分不发并附提示
- 长正文平台（微博 / 酷安 / 知乎 / 贴吧 / 公众号等）：卡片后把正文和资源放进同一个合并转发块，不再单独补文字；卡片只放三行摘要
- 视频消息段被协议端拒绝时自动改发群文件；投递三选一、互不兜底（`file` 同机 / `http` 跨机 / `base64` 内联）
- 卡片封面兜底（`thumb_url` → 图片媒体 → 本地图片 / 视频抽帧），渲染失败自动退回文字

## 🛠️ 开发

```bash
uv sync --all-groups            # 安装依赖（含测试）
uv run poe check                # lint + 格式 + 测试（CI 同款）
uv run pytest -q                # 只跑测试
```

架构、不变量与模块清单见 [`ARCHITECTURE.md`](ARCHITECTURE.md)；需求 / 模块 spec 已归档在
`archive/specs` 分支，不随 main 发布。代码风格遵循
[Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)（ruff 强制）。

## 📄 许可

[MIT](LICENSE)

## 🙏 致谢

- [z-mio/ParseHub](https://github.com/z-mio/ParseHub)：本插件的解析能力全部来自这个项目

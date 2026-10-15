## ✨ nonebot-plugin-parsehub ✨

[![LICENSE](https://img.shields.io/github/license/ByteColtX/nonebot-plugin-parsehub.svg)](./LICENSE)
[![pypi](https://img.shields.io/pypi/v/nonebot-plugin-parsehub.svg)](https://pypi.python.org/pypi/nonebot-plugin-parsehub)
[![python](https://img.shields.io/badge/python-3.12|3.13-blue.svg)](https://www.python.org)
[![uv](https://img.shields.io/badge/package%20manager-uv-black?style=flat-square&logo=uv)](https://github.com/astral-sh/uv)

[![ruff](https://img.shields.io/badge/code%20style-ruff-black?style=flat-square&logo=ruff)](https://github.com/astral-sh/ruff)
[![CI](https://github.com/ByteColtX/nonebot-plugin-parsehub/actions/workflows/ci.yml/badge.svg)](https://github.com/ByteColtX/nonebot-plugin-parsehub/actions/workflows/ci.yml)

## 📖 介绍

基于 [ParseHub](https://github.com/z-mio/ParseHub) 的社交媒体链接解析插件：群里出现受支持的分享链接时自动解析，
把视频 / 图集 / 图文发出来，并配一张 App Store「Today」风格的信息卡片。

- **自动解析**：群消息里出现受支持的链接时自动处理，也可用命令手动触发
- **按会话开关**：`解析开关` 可以把某个群 / 某个私聊的解析关掉，超管和群管理可操作
- **24 个平台**：Bilibili、抖音、小红书、微博、快手、知乎、贴吧、豆瓣、今日头条、微信公众号、微信视频号、酷安、皮皮虾、最右、小黑盒、Bangumi、X/Twitter、Instagram、YouTube、TikTok、Threads、Facebook、Snapchat、Medium
- **多媒体合并转发**：一个帖子有 2 个以上媒体时合并成一条转发消息（标题那条文字不算）；
  转发卡片上的名字取自 `NICKNAME` 的**第一项**（读原始配置保证顺序稳定，没配才退回 `ParseHub`）
- **信息卡片**：先发一张 App Store「Today」风格的信息卡（封面铺满 + 底部渐变 + 标题 / 时长），再发媒体；
  平台图标与「平台名」同一行（左边缘对齐正文），带品牌色的图标保留原色，只有纯黑白的标在深色封面上才转成白色剪影
- **实况照片**：图片 + 短视频一起发
- **视频消息段**：直接发视频，不占用群文件
- **表情回应**：不引用原消息，改为给触发消息点表情（收到 / 成功 / 失败）
- **代理按平台分流**：只给海外平台走代理，国内平台直连
- **体积 / 时长保护 / 去重 / 并发限制 / 临时文件自动清理**（去重只作用于同一会话，跨会话不限制）
- **时长保护**：视频超过 `PARSEHUB_MAX_SECONDS`（默认 5 分钟）时不下载，只发封面卡片 + 一句「视频太长啦，还是去 xx 看吧」
- **媒体缓存**：同一作品 24 小时内再出现，直接发本地那份，不再解析下载

## 🎨 效果图

B 站视频卡片：

![B 站卡片](https://raw.githubusercontent.com/ByteColtX/nonebot-plugin-parsehub/main/docs/card-bilibili.jpg)

抖音图集卡片：

![抖音卡片](https://raw.githubusercontent.com/ByteColtX/nonebot-plugin-parsehub/main/docs/card-douyin.jpg)

## 💿 安装

使用 nb-cli 安装

在 nonebot2 项目的根目录下打开命令行, 输入以下指令即可安装

```bash
nb plugin install nonebot-plugin-parsehub --upgrade
```

使用 **pypi** 源安装

```bash
nb plugin install nonebot-plugin-parsehub --upgrade -i "https://pypi.org/simple"
```

使用**清华源**安装

```bash
nb plugin install nonebot-plugin-parsehub --upgrade -i "https://pypi.tuna.tsinghua.edu.cn/simple"
```

使用包管理器安装

在 nonebot2 项目的插件目录下, 打开命令行, 根据你使用的包管理器, 输入相应的安装命令

uv

```bash
uv add nonebot-plugin-parsehub
```

安装仓库 main 分支

```bash
uv add git+https://github.com/ByteColtX/nonebot-plugin-parsehub@main
```

pdm

```bash
pdm add nonebot-plugin-parsehub
```

安装仓库 main 分支

```bash
pdm add git+https://github.com/ByteColtX/nonebot-plugin-parsehub@main
```

poetry

```bash
poetry add nonebot-plugin-parsehub
```

安装仓库 main 分支

```bash
poetry add git+https://github.com/ByteColtX/nonebot-plugin-parsehub@main
```

打开 nonebot2 项目根目录下的 `pyproject.toml` 文件, 在 `[tool.nonebot]` 部分追加写入

```toml
plugins = ["nonebot_plugin_parsehub"]
```

使用 nbr 安装(使用 uv 管理依赖可用)

[nbr](https://github.com/fllesser/nbr) 是一个基于 uv 的 nb-cli，可以方便地管理 nonebot2

```bash
nbr plugin install nonebot-plugin-parsehub
```

使用 **pypi** 源安装

```bash
nbr plugin install nonebot-plugin-parsehub -i "https://pypi.org/simple"
```

使用**清华源**安装

```bash
nbr plugin install nonebot-plugin-parsehub -i "https://pypi.tuna.tsinghua.edu.cn/simple"
```

## ⚙️ 配置

在 nonebot2 项目的`.env`文件中添加下表中的配置, 全部可选（零配置即可加载运行）

| 配置项 | 必填 | 默认值 | 说明 |
| :---: | :---: | :---: | :--- |
| `PARSEHUB_AUTO` | 否 | `true` | 群里出现链接时自动解析（总开关） |
| `PARSEHUB_MAX_MB` | 否 | `100` | 单个媒体文件体积上限（MB），超过直接跳过；`0` = 不限 |
| `PARSEHUB_MAX_SECONDS` | 否 | `300` | 视频时长上限（秒），超过只发封面 + 一句提示、不下载；`0` = 不限 |
| `PARSEHUB_PLATFORMS` | 否 | 无 | 平台白名单：只解析这些平台，留空 = 全部。可写平台 id（`xhs`）或中文名（`小红书`） |
| `PARSEHUB_DEDUPE_SECONDS` | 否 | `120` | 同一会话内同一链接的去重窗口（秒），`0` = 不去重 |
| `PARSEHUB_CONCURRENCY` | 否 | `2` | 同时处理的任务数 |
| `PARSEHUB_PROXY` | 否 | 无 | 解析 / 下载代理，如 `http://127.0.0.1:7890` |
| `PARSEHUB_PROXY_PLATFORMS` | 否 | 海外 8 平台 | 只对这些平台走代理，留空 = 全部走（列表要用合法 JSON） |
| `PARSEHUB_COOKIE` | 否 | 无 | 兜底 cookie（优先读 parsehub 自己的 `cookies.toml`） |
| `PARSEHUB_TIMEOUT` | 否 | `180` | 解析 + 下载总超时（秒） |
| `PARSEHUB_CACHE_HOURS` | 否 | `24` | 媒体缓存保留小时数，命中就直接发本地那份；`0` = 关闭缓存 |
| `PARSEHUB_CACHE_MAX_MB` | 否 | `1024` | 媒体缓存总容量上限（MB），超了按最久没用过的删 |
| `PARSEHUB_CARD` | 否 | `true` | 是否发信息卡片图（失败自动退回文字） |
| `PARSEHUB_CARD_FONT` | 否 | 无 | 中文字体路径，留空则自动找系统字体 / 下载 |
| `PARSEHUB_FORWARD_TIMEOUT` | 否 | `60` | 合并转发等待协议端的秒数，超时才逐条发 |
| `PARSEHUB_SEND_TEXT` | 否 | `true` | 是否发送标题 / 正文 |
| `PARSEHUB_MAX_CHARS` | 否 | `300` | 正文最多发送多少字符 |
| `PARSEHUB_FAIL_NOTICE` | 否 | `true` | 视频发送失败时是否额外发文字提示 |
| `PARSEHUB_FILES_HOST` | 否 | 无 | 媒体中转服务对外地址，留空自动探测内网 IP |
| `PARSEHUB_FILES_PORT` | 否 | `21473` | 媒体中转服务端口 |
| `PARSEHUB_KEEP_MINUTES` | 否 | `30` | 临时文件保留分钟数 |

> 列表类配置必须是合法 JSON，例如 `PARSEHUB_PROXY_PLATFORMS=["twitter", "youtube"]`。
> 哪些群 / 私聊解析由命令 `解析开关` 控制（见下），没有对应的配置项。

需要登录态的平台（X、Instagram、TikTok、豆瓣、微信视频号 等），cookie 优先从 ParseHub 自己的配置读取
（`ph set cookie <平台>`），也可以直接用 `PARSEHUB_COOKIE` 兜底。

## 🎉 使用

### 指令表

| 指令 | 权限 | 需要@ | 范围 | 说明 |
| :---: | :---: | :---: | :---: | :--- |
| `解析 <链接或分享文案>`（别名 `ph`、`解析链接`） | 群员 | 否 | 群聊/私聊 | 手动解析，不做去重拦截 |
| `解析平台` | 群员 | 否 | 群聊/私聊 | 列出支持的平台 |
| `解析开关 [开\|关]`、`解析开`、`解析关` | 主人/群管 | 否 | 群聊/私聊 | 按会话开关解析（私聊仅主人） |
| `解析状态` | 群员 | 否 | 群聊/私聊 | 查看当前会话的开关状态 |

### 触发规则

- **自动**：群里发一条分享链接（或直接粘贴分享文案）即可，插件从文本里挑出受支持的链接
- 一条消息最多处理 **3 个**链接
- 会话被 `解析关` 关掉后，自动解析停止，手动 `解析` 也只对主人 / 群管理有效
- 命令前缀支持空前缀与 `/`（`COMMAND_START` 需包含 `""`）

### 解析开关

以**会话**为维度（群按群号，私聊按对方 QQ），只影响该会话：

- **默认值**：群和私聊**都默认开**
- 关掉之后：该会话里的链接**不再自动解析**；手动 `解析` 也只有超管和群管理能用
- **谁能操作**：`SUPERUSERS` 里的超管，以及群主 / 群管理员（私聊只有超管）
- **谁能看状态**：所有人（`解析状态`）
- 状态存在插件数据目录的 `sessions.json`（`nonebot-plugin-localstore` 的 data dir），
  只记录和默认值不同的会话（关掉的群、打开的私聊），文件一直很小

### 媒体缓存

下载好的媒体按**作品**存一份，同一个作品再出现时直接发本地那份：

- **按作品存，不按会话**：A 群发过的视频，B 群再发也是直接发本地那份
- **命中即跳过**：不解析、不下载、不画卡（卡片图也一起缓存）
- **识别不同写法**：`b23.tv/xxx` 与 `bilibili.com/video/BVxxx`、`v.douyin.com/xxx`
  与 `iesdouyin.com/share/video/<id>` 是同一个作品
- **保留 24 小时**（用过的会续期），总量上限 1GB，超了按最久没用过的删
- 时间线上的三种情况：**120 秒内**同一会话重复 → 静默（去重）；**120 秒后 ~ 24 小时内**
  → 直接发缓存；**超过 24 小时** → 重新解析下载
- 手动 `解析 <链接>` 同样吃缓存（只是不做去重拦截）

### 说明

- 视频走 `video` 消息段，被协议端拒绝时只回应失败表情，**不会**退回群文件
- 媒体先落到本机临时目录，再通过内置的只读 HTTP 服务给协议端一个 `http://` 地址
  （协议端常在另一台机器上，读不到本机 `file://` 路径），过期自动清理
- parsehub 全程是同步阻塞的，插件把所有调用放进 `asyncio.to_thread`，不会阻塞事件循环
- 解析器首次导入需要几秒（numpy + opencv），插件采用懒加载，不影响机器人启动
- 信息卡片用 Pillow 画，封面三级兜底：`thumb_url` → 本身就是图片的媒体 → 本地视频抽首帧；
  渲染走 `asyncio.to_thread`（低端设备上单张约 0.3s），没装 Pillow 或没有中文字体时自动退回纯文字
- 中文字体优先用 `PARSEHUB_CARD_FONT`，其次是插件缓存目录里的
  `HarmonyOS_Sans_SC_Regular.ttf`，再是系统字体，都没有才下载（首次下载约 8MB，
  之后走缓存；下载源钉在固定提交上并做 sha256 校验）
- 彩色 emoji 用 Twemoji 的 TrueType 构建（`Twemoji-15.0.2.ttf`，3.2MB，CBDT 位图，
  图形 CC-BY 4.0）：位图字体只有一档（61px），所以贴图先按 61px 画再缩到目标大小。
  不用 Twemoji Mozilla 是因为那是 COLR 彩色字体，FreeType 按基字形 bbox 裁彩色图层
  （Pillow issue #9146），emoji 下半截连嘴巴一起被切掉
- 主字体缺的字（颜文字那类符号：`˶` `⚈` `Ɛ` `˵` `ʕ` `ɞ`）用回退字体补：
  `DejaVuSans.ttf`（0.7MB，补符号和 IPA）→ `NotoSans.ttf`（2MB，补修饰符字母），
  同样钉版本 + sha256；下不到就退回方块，不影响出卡

## 🛠️ 开发

```bash
uv sync --all-groups            # 安装依赖（含测试）
uv run pytest -q                # 跑测试
uv run ruff check .             # 代码检查
uv run ruff format --check .    # 格式检查
uv run python live_check.py     # 真实链路自检（解析 → 下载 → 中转取回）
```

### 代码风格

遵循 [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)：

- 行宽 80，模块 / 类 / 函数一律写 Google 风格文档字符串
  （`Args` / `Returns` / `Raises`，类属性写在 `Attributes` 段）
- 常量用 `UPPER_SNAKE_CASE`，私有成员以 `_` 开头
- 第三方与框架模块用绝对导入，由 ruff 的 `isort` 排序
- 由 ruff 强制：`pydocstyle(google)` + `pep8-naming` + `isort`

有意保留的偏差（都写在代码注释或 `pyproject.toml` 里）：

- **包内用相对导入**（`from .config import ...`）：插件既可能作为已安装的包加载，
  也可能作为 `src/plugins` 下的本地插件加载（模块名带前缀），绝对自引用在后一种
  情况下会直接 ImportError
- 框架惯用法：`from nonebot import on_message` 这类按名字导入无法避免
  （Google 只允许导入模块，而 NoneBot 的插件 API 就是按名字导入的）
- 中文文档字符串以「。」结尾，因此关闭 `D415`
- 测试函数名本身就是说明，测试目录不强制文档字符串

## 📄 许可

[MIT](LICENSE) · 解析能力来自 [ParseHub](https://github.com/z-mio/ParseHub)

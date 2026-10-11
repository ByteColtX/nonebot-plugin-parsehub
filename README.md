<div align="center">

# nonebot-plugin-parsehub

**基于 [ParseHub](https://github.com/z-mio/ParseHub) 的社交媒体链接解析插件**

群里出现分享链接时自动解析，并把视频 / 图集 / 正文发回群

</div>

## 特性

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
- **体积保护 / 去重 / 并发限制 / 临时文件自动清理**（去重只作用于同一会话，跨会话不限制）
- **信息卡片**：先发一张封面铺满的信息卡（标题 / 平台 / 时长），再发媒体
- **媒体缓存**：同一作品 24 小时内再出现，直接发本地那份，不再解析下载

## 安装

使用 nb-cli（推荐）：

```bash
nb plugin install nonebot-plugin-parsehub
```

或直接安装包：

```bash
pip install nonebot-plugin-parsehub
```

## 配置

在 `.env` / `.env.prod` 中填写，全部可选（零配置即可加载）。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PARSEHUB_AUTO` | bool | `true` | 群里出现链接时自动解析（总开关） |
| `PARSEHUB_MAX_MB` | int | `100` | 单个媒体文件体积上限，超过直接跳过 |
| `PARSEHUB_CACHE_HOURS` | int | `24` | 媒体缓存保留小时数，命中就直接发本地那份；`0` = 关闭缓存 |
| `PARSEHUB_CACHE_MAX_MB` | int | `1024` | 媒体缓存总容量上限（MB），超了按最久没用过的删 |
| `PARSEHUB_CARD` | bool | `true` | 是否发信息卡片图（失败自动退回文字） |
| `PARSEHUB_CARD_FONT` | str | 空 | 中文字体路径，留空则自动找系统字体 / 下载 |
| `PARSEHUB_FORWARD_TIMEOUT` | int | `60` | 合并转发等协议端的秒数，超时才逐条发 |
| `PARSEHUB_SEND_TEXT` | bool | `true` | 是否发送标题 / 正文 |
| `PARSEHUB_MAX_CHARS` | int | `300` | 正文最多发送多少字符 |
| `PARSEHUB_FAIL_NOTICE` | bool | `true` | 视频发送失败时是否额外发文字提示 |
| `PARSEHUB_PROXY` | str | 空 | 解析 / 下载代理，如 `http://127.0.0.1:7890` |
| `PARSEHUB_PROXY_PLATFORMS` | list[str] | 海外 8 平台 | 只对这些平台走代理，留空 = 全部走 |
| `PARSEHUB_COOKIE` | str | 空 | 兜底 cookie（优先读 parsehub 自己的 cookies.toml） |
| `PARSEHUB_TIMEOUT` | int | `180` | 解析 + 下载总超时（秒） |
| `PARSEHUB_FILES_HOST` | str | 空 | 媒体中转服务对外地址，留空自动探测内网 IP |
| `PARSEHUB_FILES_PORT` | int | `21473` | 媒体中转服务端口 |
| `PARSEHUB_KEEP_MINUTES` | int | `30` | 临时文件保留分钟数 |
| `PARSEHUB_DEDUPE_SECONDS` | int | `120` | 同一会话内同一链接的去重窗口（秒），0 = 不去重 |
| `PARSEHUB_CONCURRENCY` | int | `2` | 同时处理的任务数 |

> 列表类配置必须是合法 JSON，例如 `PARSEHUB_PROXY_PLATFORMS=["twitter", "youtube"]`。
> 哪些群 / 私聊解析由命令 `解析开关` 控制（见下），没有对应的配置项。

需要登录态的平台（知乎 / 小红书 / 微博类之外的 X、Instagram、TikTok、豆瓣、微信视频号 等），
cookie 优先从 ParseHub 自己的配置读取（`ph set cookie <平台>`），也可以直接用 `PARSEHUB_COOKIE` 兜底。

## 使用

- **自动**：群里发一条分享链接（或直接粘贴分享文案）即可
- **命令**：
  - `解析 <链接或分享文案>`（别名 `ph`、`解析链接`）
  - `解析平台`（列出支持的平台）
  - `解析开关 [开|关]` / `解析开` / `解析关` / `解析状态` —— 按会话开关解析

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

## 说明

- 视频走 `video` 消息段，被协议端拒绝时只回应失败表情，**不会**退回群文件
- 媒体先落到本机临时目录，再通过内置的只读 HTTP 服务给协议端一个 `http://` 地址
  （协议端常在另一台机器上，读不到本机 `file://` 路径），过期自动清理
- parsehub 全程是同步阻塞的，插件把所有调用放进 `asyncio.to_thread`，不会阻塞事件循环
- 解析器首次导入需要几秒（numpy + opencv），插件采用懒加载，不影响机器人启动
- 信息卡片用 Pillow 画，封面三级兜底：`thumb_url` → 本身就是图片的媒体 → 本地视频抽首帧；
  渲染走 `asyncio.to_thread`（armv7 上单张约 0.3s），没装 Pillow 或没有中文字体时自动退回纯文字
- 中文字体优先用 `PARSEHUB_CARD_FONT`，其次是插件缓存目录里的 `NotoSansSC.ttf`，
  再是系统字体，都没有才下载（首次下载约 18MB，之后走缓存）

## 开发

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

## 许可

[MIT](LICENSE) · 解析能力来自 [ParseHub](https://github.com/z-mio/ParseHub)

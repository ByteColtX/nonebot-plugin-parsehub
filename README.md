<div align="center">

# nonebot-plugin-parsehub

**基于 [ParseHub](https://github.com/z-mio/ParseHub) 的社交媒体链接解析插件**

群里出现分享链接时自动解析，并把视频 / 图集 / 正文发回群

</div>

## 特性

- **自动解析**：群消息里出现受支持的链接时自动处理，也可用命令手动触发
- **24 个平台**：Bilibili、抖音、小红书、微博、快手、知乎、贴吧、豆瓣、今日头条、微信公众号、微信视频号、酷安、皮皮虾、最右、小黑盒、Bangumi、X/Twitter、Instagram、YouTube、TikTok、Threads、Facebook、Snapchat、Medium
- **多媒体合并转发**：一个帖子有 2 个以上媒体时合并成一条转发消息（标题那条文字不算）
- **实况照片**：图片 + 短视频一起发
- **视频消息段**：直接发视频，不占用群文件
- **表情回应**：不引用原消息，改为给触发消息点表情（收到 / 成功 / 失败）
- **代理按平台分流**：只给海外平台走代理，国内平台直连
- **体积保护 / 去重 / 并发限制 / 临时文件自动清理**

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
| `PARSEHUB_AUTO` | bool | `true` | 群里出现链接时自动解析 |
| `PARSEHUB_GROUPS` | list[int] | `[]` | 自动解析生效的群号，留空 = 所有群 |
| `PARSEHUB_AUTO_PRIVATE` | bool | `false` | 私聊也自动解析 |
| `PARSEHUB_MAX_MB` | int | `100` | 单个媒体文件体积上限，超过直接跳过 |
| `PARSEHUB_VIDEO_SEND` | str | `video` | `video` = 用视频消息段；`file` = 上传群文件 |
| `PARSEHUB_SEND_TEXT` | bool | `true` | 是否发送标题 / 正文 |
| `PARSEHUB_MAX_CHARS` | int | `300` | 正文最多发送多少字符 |
| `PARSEHUB_FAIL_NOTICE` | bool | `true` | 视频发送失败时是否额外发文字提示 |
| `PARSEHUB_PROXY` | str | 空 | 解析 / 下载代理，如 `http://127.0.0.1:7890` |
| `PARSEHUB_PROXY_PLATFORMS` | list[str] | 海外 8 平台 | 只对这些平台走代理，留空 = 全部走 |
| `PARSEHUB_COOKIE` | str | 空 | 兜底 cookie（优先读 parsehub 自己的 cookies.toml） |
| `PARSEHUB_TIMEOUT` | int | `180` | 解析 + 下载总超时（秒） |
| `PARSEHUB_FILES_HOST` | str | 空 | 媒体中转服务对外地址，留空自动探测内网 IP |
| `PARSEHUB_FILES_PORT` | int | `8899` | 媒体中转服务端口 |
| `PARSEHUB_KEEP_MINUTES` | int | `30` | 临时文件保留分钟数 |
| `PARSEHUB_CONCURRENCY` | int | `2` | 同时处理的任务数 |
| `PARSEHUB_DEDUPE_SECONDS` | int | `120` | 同一链接去重窗口（秒） |

> 列表类配置必须是合法 JSON，例如 `PARSEHUB_GROUPS=[123456]`、`PARSEHUB_PROXY_PLATFORMS=["twitter", "youtube"]`。

需要登录态的平台（知乎 / 小红书 / 微博类之外的 X、Instagram、TikTok、豆瓣、微信视频号 等），
cookie 优先从 ParseHub 自己的配置读取（`ph set cookie <平台>`），也可以直接用 `PARSEHUB_COOKIE` 兜底。

## 使用

- **自动**：群里发一条分享链接（或直接粘贴分享文案）即可
- **命令**：`解析 <链接或分享文案>`（别名 `ph`）、`解析平台`（列出支持的平台）

## 说明

- 视频走 `video` 消息段，被协议端拒绝时只回应失败表情，**不会**退回群文件
- 媒体先落到本机临时目录，再通过内置的只读 HTTP 服务给协议端一个 `http://` 地址
  （协议端常在另一台机器上，读不到本机 `file://` 路径），过期自动清理
- parsehub 全程是同步阻塞的，插件把所有调用放进 `asyncio.to_thread`，不会阻塞事件循环
- 解析器首次导入需要几秒（numpy + opencv），插件采用懒加载，不影响机器人启动

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

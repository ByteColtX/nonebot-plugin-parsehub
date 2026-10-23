---
id: parsehub-plugin
type: module-design
status: draft
title: nonebot_plugin_parsehub 模块
parent: parsehub-architecture
tags: [nonebot, parsehub, module]
---

# nonebot_plugin_parsehub

## Responsibility

把一个链接变成「发出去的东西」：识别平台、去重、缓存、解析、下载、画卡、投递，
并把每个结局表达成用户看得见的反馈。

## Boundary

- **允许依赖**：nonebot2、onebot v11 适配器、parsehub、Pillow、httpx、localstore、
  numpy、opencv-python-headless（后两者只在 `card` 里用）。
- **禁止**：`triggers` 之外的模块 import matcher / `on_command`；`delivery` 之外的模块
  调用 `sender.send`；`service` import `Bot`。
- **日志**：一律用 `nonebot.logger`（官方指导），领域模块也不例外；除 logger 之外，
  `cache`、`dedupe`、`relay`、`keys`、`platforms`、`budget`、`media`、`card` 不碰
  nonebot 的 matcher / `Bot` / 事件类型。
- 包内一律相对 import：插件既可能作为已安装包加载，也可能作为本地插件加载。

## Module map

| 模块 | 角色 |
| :--- | :--- |
| `app.py` | `App` 装配、启动（中转 / 字体 / 预热 / 清扫）、关闭 |
| `config.py` | `Config`（校验）+ `Settings`（归一化只读快照） |
| `triggers.py` | matcher 注册、自动规则、四个命令 |
| `service.py` | 编排流水线与 `Outcome` |
| `delivery.py` | `Sender` 协议、三种上传模式、合并转发、`MediaReport` |
| `feedback.py` | `Reaction` 与全部提示文案 |
| `parsehub_client.py` | 懒加载客户端、cookie 读取 |
| `cache.py` | 作品级媒体缓存（原子写、TTL、容量） |
| `dedupe.py` | 会话级去重 |
| `relay.py` | 只读中转服务 |
| `paths.py` | cache / relay / data 目录约定 |
| `keys.py` | 域名白名单、链接提取、作品 key 归一 |
| `platforms.py` | 平台表、白名单别名、时长单位 |
| `media.py` | `MediaKind` / `MediaRef` / `MediaFile`、体积探测 |
| `errors.py` | `ErrorKind` / `ParseFailure` / 异常分类 |
| `budget.py` | 单次工作的超时预算 |
| `card/` | 卡片渲染，见 `parsehub-card` |

## Contracts

- `service.handle(app, sender, event, url, manual=False) -> Result`；`Result.ok` 决定终态
  表情，`Result.notice` 由 `handle` 发送。
- `Delivery.send_media(...) -> MediaReport`：`sent` / `failed` / `skipped` 三份明细，
  不抛异常；`MediaReport.ok` 是「至少发出去一个且没有失败」。
- `CardRenderer.render(...) -> Path | None`：任何失败返回 `None`，调用方退回文字。
- `MediaCache.store(keys, files, card=, caption=, seconds=) -> Path | None`：先写
  `.tmp` 再 `os.replace`，过期条目重新缓存不会混入旧文件。
- `ParseHubClient` 的所有方法都是异步的，内部 `to_thread`；异常由 `errors.translate`
  在 `service` 边界分类。

## Decisions

- 不额外包一层 `domain.Work`：`MediaRef` / `MediaFile` / `CacheEntry` 就是领域模型，
  避免复制 parsehub 的结果对象。
- 平台白名单在 `Settings` 里归一成 id 集合，运行期只做集合查找。
- 配置键是唯一入口：`Config` 只负责读取与校验，运行期一律用 `Settings`，
  不再散读 `plugin_config`。
- 命令严格按 `COMMAND_START` 匹配：`should_auto` 不排除命令文本，靠命令匹配器的
  `priority=10` + `block=True` 先收口。裸写的「解析 <链接>」不是命令，按普通消息
  自动解析——默认前缀只有 `/`，若自动规则也跳过它，消息会被静默丢弃。
  命令名后还要求词边界（`triggers.command_boundary`）：`TrieRule` 只做前缀匹配，
  `COMMAND_START` 带空字符串时会把「php是世界上最好的语言」「解析一下」误判成命令，
  必须让普通聊天落到自动规则或什么都不做。
- `paths` 把 localstore 当硬依赖（`require` 后 import），不保留临时目录兜底。
- 日志统一用 `nonebot.logger`：框架的 loguru 已经管住格式、插件标签与 `LOG_LEVEL`，
  在包内再桥接一层 stdlib logging 只是多一个可能失效的中间层。
- 第三方依赖全是**硬依赖**：parsehub / Pillow / numpy / opencv / localstore 缺了就是
  装错了，代码里不写 `except ImportError` 兜底，也不往别的目录写；降级只发生在产品层
  （卡片→文字、字体→系统字体、体积探测→未知）。
- B 站分P交给上游 ParseHub（2.3.14 起按 `p` 选分P并返回对应时长）；插件只在
  `keys.content_key` 里把分P写进作品 key，避免不同 P 撞缓存。

## Known limitations

- `sessions.json` 带 schema 版本（当前 2）；版本不符时按默认值处理。
- 平台白名单里无法表达「除某平台外全部」。

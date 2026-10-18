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

- **允许依赖**：nonebot2、onebot v11 适配器、parsehub、Pillow、httpx、localstore。
- **禁止**：`triggers` 之外的模块 import matcher / `on_command`；`delivery` 之外的模块
  调用 `sender.send`；`service` import `Bot`；`card`、`cache`、`dedupe`、`relay`、
  `keys`、`platforms`、`budget`、`media` import nonebot（`card` 只允许 Pillow/httpx）。
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
- 配置改名不保留旧键（破坏性变更）：`MAX_FILE_MB`、`TASK_TTL_MINUTES`、
  `RELAY_HOST/PORT`、`DELIVERY`、`CACHE_TTL_HOURS`。

## Known limitations

- `sessions.json` 只有 schema 2 会被读取，旧文件直接忽略（不迁移）。
- 平台白名单里无法表达「除某平台外全部」。

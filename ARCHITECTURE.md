---
id: parsehub-architecture
type: architecture-design
status: draft
title: ParseHub 插件：架构与不变量
tags: [nonebot, parsehub, architecture]
---

# 架构与不变量

## Topology

```
triggers ──► service（run / works / audio / download / message / result）──► parsehub_client
                │            cache / dedupe / budget
                │            card（渲染）
                └──► delivery ──► relay（http 模式）
                        │
                        └──► feedback（表情 / 提示）
```

`app.App` 持有全部状态并注入给各层；`__init__` 在导入时构建唯一实例，测试自建实例。
依赖方向自上而下，下层不得反向 import 上层。

## Layering

- **触发层**（`triggers`）：判断该不该处理、从消息里取链接、调用 `service.handle`。
- **编排层**（`service` 包）：固定顺序的流水线，每个结局是 `Outcome` 的一个值；
  `run` 收尾（白名单 / 去重 / 表情）、`works` 作品、`audio` 音频、`download`
  下载、`message` 卡片或文字、`result` 结局。
- **能力层**：`parsehub_client`（解析 / 下载）、`bilibili_live`（B 站直播间封面卡片）、
  `audio`（bm / ym 音频提取）、`bilibili`（B 站常量）、`cache`、`dedupe`、`relay`、
  `card`（渲染）。
- **投递层**（`delivery`）：三种上传模式、合并转发/逐条、失败记账。
- **反馈层**（`feedback`）：表情与用户提示文案。
- **装配层**（`app`）：构建、启动、清扫、关闭。

## 模块清单

```
src/nonebot_plugin_parsehub/
├── triggers.py         # matcher / 命令：只决定「处理谁」
├── service/            # 编排：works / audio / download / message / run / result
├── audio.py            # bm / ym 音频提取（B 站 playurl / yt-dlp）
├── bilibili.py         # B 站常量（域名 / BV 正则，单一来源）
├── bilibili_live.py    # B 站直播间封面卡片
├── delivery.py         # 三种上传模式 + 合并转发/逐条 + 统一失败记账
├── feedback.py         # 表情回应与用户提示文案
├── cache.py            # 作品级媒体缓存（哈希目录、原子写）
├── dedupe.py           # 会话级去重（claim / commit / release）
├── relay.py            # 只读中转服务（只服务 relay/）
├── parsehub_client.py  # ParseHub 懒加载 + 线程池封装
├── platforms.py        # 平台元数据（展示名 / 颜色 / logo / 时长单位）
├── keys.py             # 链接识别 / 域名→平台 / 作品 key 归一
├── switches.py         # 按会话的解析开关
├── media.py            # parsehub 结果 → MediaRef / MediaFile
├── errors.py           # 异常分类与用户文案
├── paths.py            # localstore 目录约定
├── budget.py           # 解析 / 下载共享的超时预算
├── config.py           # 配置读取与归一化
├── card/               # fonts / cover / text / compose / layout + CardRenderer
├── assets/logos/       # 平台 logo PNG（card 渲染的运行时资源）
└── app.py              # 装配与生命周期
```

## Invariants

- **发送只有一个出口**：除 `delivery` 外不允许出现 `bot.send` / `call_api` 发送消息；
  `service` 不接触 `Bot`，只依赖 `Sender` 协议。
- **结局穷举**：`service.handle` 一定返回 `Result`；提示放在 `Result.notice`，由
  `handle` 统一发送。START 表情是后台任务，收尾前 join，保证终态表情排在它后面；
  解析前就命中去重的重复不点表情，解析后才发现重复的点成功表情。
- **同消息多链接并发**：`triggers` 对一条消息里的链接 `gather` 扇出；整条流水线的
  并发上限仍由 `App.semaphore`（`PARSEHUB_CONCURRENCY`）兜底。
- **媒体分批**：单个作品硬上限 100 项，发送按 `forward_timeout / 3s` 分批
  （默认 20 项/批）；正文只进第一批，截断提示在所有批次之后以独立文本发送。
- **去重三态**：`claim`（进入时，含处理中集合）→ 成功 `commit` → 失败 `release`。
  手动命令不参与去重拦截，也不记入窗口。
- **缓存只收发出去的东西**：体积超限被跳过的媒体不写缓存；缓存条目按作品跨会话共享。
- **直播不走作品缓存**：直播间的标题 / 封面 / 状态随时在变，只靠会话去重，
  不读写 ``cache/media``；出不了卡就退回常规失败路径。
- **缓存与中转分离**：缓存在 `cache/media`，中转只服务 `cache/relay`；投递时缓存文件
  硬链接进任务目录，缓存淘汰不会让消息发到一半文件消失。
- **超时预算共享**：解析与下载共用一个 deadline；超时不重试，只有网络类失败才重试。
- **阻塞活进线程**：parsehub、字体、封面、缓存磁盘 IO、清扫都在 `asyncio.to_thread`。
- **配置启动时校验**：`Config` 用 pydantic 下界校验，`Settings` 是归一化只读快照。
- **音频不设时长上限**：纯音频远小于视频，只按体积上限（`max_file_bytes`）；
  但取数与下载都吃操作超时预算（`PARSEHUB_TIMEOUT`），和常规视频下载同等待遇。

## Directory contract

| 目录 | 内容 | 生命周期 |
| :--- | :--- | :--- |
| `cache/media` | 作品级媒体缓存（`h_<sha256(key)[:20]>` + `meta.json`） | TTL / 容量淘汰，可重建 |
| `cache/relay/<uuid>` | 待投递的任务目录 | `PARSEHUB_TASK_TTL_MINUTES` 清扫 |
| `data` | `sessions.json`（会话开关） | 持久 |

## Delivery modes

`file`（默认，本机 `file://` 路径）/ `http`（中转地址）/ `base64`（内容内联）。
三者**互不兜底**：选了哪种就是哪种，发不出去就点失败表情并提示。

`http` 模式固定绑定 `0.0.0.0`；`PARSEHUB_RELAY_HOST` 只是告诉协议端「来这个地址」，
可以是内网 IP 或公网域名，不参与 bind。

## Known limitations

- parsehub 是同步库，线程无法取消：下载超时后那次下载仍会跑完，只是不再重试。
- 中转服务无鉴权；任务目录名随机不可猜，默认绑 `0.0.0.0` 是跨机投递的前提。
- `sessions.json` 带 schema 版本（当前 2）；版本不符时按默认值处理。
- 平台白名单只能表达「只解析这些」，无法表达「除某平台外全部」。

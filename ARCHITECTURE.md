---
id: parsehub-architecture
type: architecture-design
status: draft
title: ParseHub 插件：架构与不变量
parent: parsehub-goal
tags: [nonebot, parsehub, architecture]
---

# 架构与不变量

## Topology

```
triggers ──► service ──► parsehub_client
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
- **编排层**（`service`）：固定顺序的流水线，每个结局是 `Outcome` 的一个值。
- **能力层**：`parsehub_client`（解析 / 下载）、`bilibili`（分P归一 / 单P时长）、
  `cache`、`dedupe`、`relay`、`card`。
- **投递层**（`delivery`）：三种上传模式、合并转发/逐条、失败记账。
- **反馈层**（`feedback`）：表情与用户提示文案。
- **装配层**（`app`）：构建、启动、清扫、关闭。

## Invariants

- **发送只有一个出口**：除 `delivery` 外不允许出现 `bot.send` / `call_api` 发送消息；
  `service` 不接触 `Bot`，只依赖 `Sender` 协议。
- **结局穷举**：`service.handle` 一定返回 `Result`；提示放在 `Result.notice`，由
  `handle` 统一发送，终态表情在提示之后（重复除外，重复是静默的）。
- **去重三态**：`claim`（进入时，含处理中集合）→ 成功 `commit` → 失败 `release`。
  手动命令不参与去重拦截，但成功后会记入窗口。
- **缓存只收发出去的东西**：体积超限被跳过的媒体不写缓存；缓存条目按作品跨会话共享。
- **缓存与中转分离**：缓存在 `cache/media`，中转只服务 `cache/relay`；投递时缓存文件
  硬链接进任务目录，缓存淘汰不会让消息发到一半文件消失。
- **超时预算共享**：解析与下载共用一个 deadline；超时不重试，只有网络类失败才重试。
- **阻塞活进线程**：parsehub、字体、封面、缓存磁盘 IO、清扫都在 `asyncio.to_thread`。
- **配置启动时校验**：`Config` 用 pydantic 下界校验，`Settings` 是归一化只读快照。

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

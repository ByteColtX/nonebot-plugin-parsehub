---
id: parsehub-card
type: submodule-design
status: draft
title: 卡片渲染子模块
parent: parsehub-plugin
tags: [nonebot, parsehub, card]
---

# card

## Responsibility

把解析结果画成一张 App Store「Today」风格的信息卡：封面铺满、底部渐变、平台图标、
标题、时长。任何一步失败都退回纯文字，绝不让解析白做。

## Boundary

- 只依赖 Pillow、httpx 与包内的 `platforms`；**不 import nonebot**。
- 所有渲染是同步阻塞的 CPU 活，调用方必须放进线程池（`CardRenderer` 负责）。

## Layout

| 模块 | 角色 |
| :--- | :--- |
| `fonts.py` | 中文字体 / 彩色 emoji / 回退字体的获取与 sha256 校验 |
| `cover.py` | 封面三级兜底：`thumb_url` → 图片媒体 → 本地视频抽帧 |
| `layout.py` | 版式、折行、绘制、`render`、时长与平台展示 |
| `__init__.py` | `CardRenderer` + 公开 API 再导出 |

## Contracts

- `CardRenderer.render(result, files, task_dir, proxy="") -> Path | None`；
  `task_dir` 为 `None` 时用注入的目录工厂新建一个。
- `render` 的输出固定为 `<task_dir>/card.jpg`。
- `ensure_font` 找不到字体时返回 `None`（关闭卡片，不抛异常）。

## Decisions

- 字体源钉在固定提交 / release 上并做 sha256 校验；下载失败只是少一项能力。
- 彩色 emoji 用 Twemoji 的 TrueType（CBDT 位图）构建：COLR 彩色字体会被 FreeType
  按基字形 bbox 裁掉下半截（Pillow issue #9146）。位图只有 61px 一档，所以先按档位
  画再缩到目标字号。
- 主字体缺字（颜文字符号）用 DejaVu → Noto Sans 回退，拿不到就显示方块。
- logo 资源在包根目录 `assets/logos`（`card` 包比原单文件深一层，路径按 `parent.parent`）。

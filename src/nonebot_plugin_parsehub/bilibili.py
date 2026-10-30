# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""B 站相关常量的单一来源。

keys / audio / bilibili_live 三个模块都认同一套域名与 BV 号规则，
规则只在这里写一份，改短链域名之类的调整不需要再对三处。
"""

from __future__ import annotations

import re

# 直播间链接的主机名。
LIVE_HOST = "live.bilibili.com"

# 短链域名：路径没有作品信息，展开后才知道指向什么。
SHORT_HOSTS: tuple[str, ...] = ("b23.tv", "bili2233.cn")

# B 站相关主机（含子域名），短链排在末尾。
HOSTS: tuple[str, ...] = ("bilibili.com", *SHORT_HOSTS)

# 裸 BV 号拼成的标准链接，和用户直接发完整链接走同一套 key / 缓存。
VIDEO_URL = "https://www.bilibili.com/video/{}"

# 裸 BV 号：前后不能紧贴字母数字，免得把 ``xBV...`` 或更长的串切一段出来。
BV_PATTERN: re.Pattern[str] = re.compile(
    r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])"
)

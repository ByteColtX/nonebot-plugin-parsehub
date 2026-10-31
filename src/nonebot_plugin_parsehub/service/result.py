# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""结局与结果对象。

流水线各阶段都用 :class:`Result` 交接：阶段自己能终结的就地带上结局与
提示返回，交给统一的收尾逻辑发通知、点表情、记去重。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..delivery import MediaReport


class Outcome(StrEnum):
    """一次处理的结局。"""

    DELIVERED = "delivered"
    FROM_CACHE = "from_cache"
    DUPLICATE = "duplicate"
    PLATFORM_OFF = "platform_off"
    UNSUPPORTED = "unsupported"
    PARSE_FAILED = "parse_failed"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"
    NO_MEDIA = "no_media"
    DOWNLOAD_FAILED = "download_failed"
    DELIVERY_FAILED = "delivery_failed"
    INTERNAL_ERROR = "internal_error"


# 这些结局算「处理成功」，点成功表情。
_OK_OUTCOMES = frozenset(
    {
        Outcome.DELIVERED,
        Outcome.FROM_CACHE,
        Outcome.NO_MEDIA,
        Outcome.TOO_LONG,
    }
)


@dataclass
class Result:
    """一次处理的完整结果。

    Attributes:
        outcome: 结局。
        notice: 要补发给用户的一句话；没有为 ``None``。
        report: 媒体投递明细（没走到投递时为 ``None``）。
        keys: 这次处理涉及的作品 key（用于去重提交）。
    """

    outcome: Outcome
    notice: str | None = None
    report: MediaReport | None = None
    keys: tuple[str, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        """这次处理算不算成功。

        Returns:
            成功结局且媒体没有失败时为 True。
        """
        if self.outcome not in _OK_OUTCOMES:
            return False
        if self.report is not None and not self.report.ok:
            return False
        return True

    @classmethod
    def fail(
        cls,
        outcome: Outcome,
        *,
        notice: str | None = None,
        keys: tuple[str, ...] = (),
    ) -> Result:
        """不带投递明细的结局（失败 / 跳过 / 提示类）。

        Args:
            outcome: 结局。
            notice: 要补发给用户的一句话；没有为 ``None``。
            keys: 这次处理涉及的作品 key。

        Returns:
            对应的 :class:`Result`。
        """
        return cls(outcome, notice=notice, keys=keys)

    @classmethod
    def delivered(
        cls,
        report: MediaReport,
        keys: tuple[str, ...],
        *,
        from_cache: bool = False,
    ) -> Result:
        """投递类结局：媒体全部发出才算成功。

        Args:
            report: 媒体投递明细。
            keys: 这次处理涉及的作品 key。
            from_cache: 命中缓存发出的（结局记 ``FROM_CACHE``）。

        Returns:
            按投递明细定好结局的 :class:`Result`。
        """
        if not report.ok:
            return cls(Outcome.DELIVERY_FAILED, report=report, keys=keys)
        outcome = Outcome.FROM_CACHE if from_cache else Outcome.DELIVERED
        return cls(outcome, report=report, keys=keys)

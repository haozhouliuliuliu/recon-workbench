"""策略配置：金额容差、发票号规则、字段长度等。

原则：默认值可用，配置项宁少勿多；未知配置键一律报错，避免拼错键却没生效。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import ConfigError
from .models import PolicySnapshot

POLICY_KEYS = {
    "amount_tolerance",
    "invoice_pattern",
    "invoice_min_length",
    "invoice_max_length",
    "counterparty_max_length",
    "note_max_length",
    "allow_negative_amount",
    "max_decimal_places",
    "date_formats",
    "allowed_currencies",
}


@dataclass(frozen=True, slots=True)
class Policy:
    """对账策略。不可变，改参数用 :meth:`with_overrides` 生成新实例。"""

    amount_tolerance: Decimal = Decimal("0.00")
    invoice_pattern: str = r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$"
    invoice_min_length: int = 5
    invoice_max_length: int = 32
    counterparty_max_length: int = 80
    note_max_length: int = 200
    allow_negative_amount: bool = False
    max_decimal_places: int = 2
    date_formats: tuple[str, ...] = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y年%m月%d日",
        "%Y%m%d",
    )
    allowed_currencies: tuple[str, ...] = ("CNY", "USD", "EUR")

    def __post_init__(self) -> None:
        if self.amount_tolerance < 0:
            raise ConfigError("amount_tolerance 不能为负数")
        if not 0 <= self.max_decimal_places <= 6:
            raise ConfigError("max_decimal_places 必须在 0..6 之间")
        if self.invoice_min_length <= 0 or self.invoice_max_length < self.invoice_min_length:
            raise ConfigError("发票号长度区间不合法")
        if not self.date_formats:
            raise ConfigError("date_formats 不能为空")
        if not self.allowed_currencies:
            raise ConfigError("allowed_currencies 不能为空")
        try:
            re.compile(self.invoice_pattern)
        except re.error as exc:  # pragma: no cover - 由配置校验测试覆盖
            raise ConfigError(f"invoice_pattern 不是合法正则：{exc}") from exc

    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(
            amount_tolerance=self.amount_tolerance,
            invoice_pattern=self.invoice_pattern,
            invoice_min_length=self.invoice_min_length,
            invoice_max_length=self.invoice_max_length,
            counterparty_max_length=self.counterparty_max_length,
            note_max_length=self.note_max_length,
            allow_negative_amount=self.allow_negative_amount,
            max_decimal_places=self.max_decimal_places,
            date_formats=self.date_formats,
            allowed_currencies=self.allowed_currencies,
        )

    def with_overrides(self, **overrides: object) -> Policy:
        unknown = set(overrides) - POLICY_KEYS
        if unknown:
            raise ConfigError(f"未知策略键：{', '.join(sorted(unknown))}")
        return replace(self, **overrides)  # type: ignore[arg-type]


def _as_decimal(key: str, value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{key} 必须是数字，收到 {value!r}") from exc


def _as_int(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数，收到 {value!r}")
    return value


def _as_bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是布尔值，收到 {value!r}")
    return value


def _as_str_tuple(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} 必须是字符串数组，收到 {value!r}")
    return tuple(value)


def load_policy(path: Path | str) -> Policy:
    """从 TOML 的 ``[policy]`` 表加载策略；只覆盖显式给出的键。"""

    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"配置文件不是合法的 TOML/UTF-8：{exc}") from exc

    table = data.get("policy")
    if table is None:
        raise ConfigError("配置文件缺少 [policy] 段")
    if not isinstance(table, dict):
        raise ConfigError("[policy] 必须是一个表")

    unknown = set(table) - POLICY_KEYS
    if unknown:
        raise ConfigError(f"[policy] 中存在未知键：{', '.join(sorted(unknown))}")

    overrides: dict[str, object] = {}
    if "amount_tolerance" in table:
        tolerance = _as_decimal("amount_tolerance", table["amount_tolerance"])
        if tolerance < 0:
            raise ConfigError("amount_tolerance 不能为负数")
        overrides["amount_tolerance"] = tolerance
    if "invoice_pattern" in table:
        value = table["invoice_pattern"]
        if not isinstance(value, str):
            raise ConfigError("invoice_pattern 必须是字符串")
        overrides["invoice_pattern"] = value
    for key in (
        "invoice_min_length",
        "invoice_max_length",
        "counterparty_max_length",
        "note_max_length",
        "max_decimal_places",
    ):
        if key in table:
            overrides[key] = _as_int(key, table[key])
    if "allow_negative_amount" in table:
        overrides["allow_negative_amount"] = _as_bool("allow_negative_amount", table["allow_negative_amount"])
    if "date_formats" in table:
        overrides["date_formats"] = _as_str_tuple("date_formats", table["date_formats"])
    if "allowed_currencies" in table:
        currencies = _as_str_tuple("allowed_currencies", table["allowed_currencies"])
        overrides["allowed_currencies"] = tuple(c.strip().upper() for c in currencies)

    try:
        return Policy(**overrides)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ConfigError(f"策略参数不合法：{exc}") from exc

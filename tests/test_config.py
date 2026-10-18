"""策略配置测试。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from recon_workbench.config import Policy, load_policy
from recon_workbench.errors import ConfigError
from recon_workbench.pipeline import run_reconciliation

from .helpers import write_csv


def _write_toml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_defaults(tmp_path: Path) -> None:
    policy = load_policy(_write_toml(tmp_path / "p.toml", "[policy]\namount_tolerance = 0.01\n"))
    assert policy.amount_tolerance == Decimal("0.01")
    assert policy.allowed_currencies == ("CNY", "USD", "EUR")


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_policy(_write_toml(tmp_path / "p.toml", "[policy]\nnot_a_key = 1\n"))
    assert "not_a_key" in str(excinfo.value)


def test_missing_section_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_policy(_write_toml(tmp_path / "p.toml", "amount_tolerance = 0.01\n"))


def test_invalid_toml_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_policy(_write_toml(tmp_path / "p.toml", "[policy\nbroken"))


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_policy(tmp_path / "missing.toml")


def test_negative_tolerance_is_rejected() -> None:
    with pytest.raises(ConfigError):
        Policy(amount_tolerance=Decimal("-0.01"))


def test_tolerance_changes_match_outcome(tmp_path: Path) -> None:
    ledger = write_csv(
        tmp_path / "ledger.csv",
        [["INV-2099-0001", "辰光金属（虚构）", "2026-08-03", "CNY", "100.00"]],
    )
    statement = write_csv(
        tmp_path / "statement.csv",
        [["INV-2099-0001", "辰光金属（虚构）", "2026-08-03", "CNY", "100.01"]],
    )

    strict = run_reconciliation(ledger, statement, Policy())
    assert strict.report.counts.amount_mismatch == 1

    tolerance = run_reconciliation(ledger, statement, Policy(amount_tolerance=Decimal("0.01")))
    assert tolerance.report.counts.amount_mismatch == 0
    assert tolerance.report.counts.matched == 1
    assert strict.digest != tolerance.digest

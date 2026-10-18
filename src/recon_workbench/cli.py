"""命令行入口：``recon run``。

退出码约定（便于接入 CI/定时任务）：
0 完成且无差异；1 完成但存在差异（仅在 --fail-on-difference 时）；
2 参数错误；3 输入或配置不可用。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Policy, load_policy
from .errors import ConfigError, InputError, ReconError
from .export import write_excel, write_report_json
from .pipeline import run_reconciliation, summarize

EXIT_OK = 0
EXIT_DIFFERENCE = 1
EXIT_INPUT = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recon",
        description="对账工作台：导入台账与对账单，做字段校验、金额核对、重复检测并导出 Excel。",
    )
    parser.add_argument("--version", action="version", version=f"recon-workbench {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="执行一次对账")
    run.add_argument("--ledger", required=True, type=Path, help="内部应付台账（CSV / XLSX）")
    run.add_argument("--statement", required=True, type=Path, help="供应商对账单（CSV / XLSX）")
    run.add_argument("--out-dir", type=Path, default=Path("out"), help="输出目录，默认 ./out")
    run.add_argument("--ledger-sheet", default=None, help="台账工作表名（仅 XLSX）")
    run.add_argument("--statement-sheet", default=None, help="对账单工作表名（仅 XLSX）")
    run.add_argument("--config", type=Path, default=None, help="TOML 策略文件，例如 [policy] amount_tolerance = 0.01")
    run.add_argument(
        "--fail-on-difference",
        action="store_true",
        help="存在金额不一致/单边记录时以退出码 1 结束，便于 CI 卡控",
    )
    run.add_argument("--now", default=None, help="覆盖 Excel 中的生成时间（ISO 8601），用于可复现输出")
    run.add_argument("--print-json", action="store_true", help="把规范化 JSON 打印到标准输出")
    run.add_argument("--quiet", action="store_true", help="不打印摘要")
    return parser


def _resolve_policy(path: Path | None) -> Policy:
    return load_policy(path) if path is not None else Policy()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command != "run":  # pragma: no cover - argparse 已保证
        parser.print_help()
        return EXIT_INPUT

    try:
        generated_at = _parse_now(args.now)
        policy = _resolve_policy(args.config)
        result = run_reconciliation(
            args.ledger,
            args.statement,
            policy,
            ledger_sheet=args.ledger_sheet,
            statement_sheet=args.statement_sheet,
        )
    except (InputError, ConfigError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    except ReconError as exc:  # pragma: no cover - 兜底
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT

    out_dir: Path = args.out_dir
    json_path = write_report_json(result.canonical_json, out_dir / "recon_report.json")
    excel_path = write_excel(result.report, out_dir / "recon_result.xlsx", generated_at=generated_at)

    if not args.quiet:
        print(summarize(result.report))
        print(f"输出：{json_path}")
        print(f"      {excel_path}")

    if args.print_json:
        sys.stdout.write(result.canonical_json.decode("utf-8") + "\n")

    if args.fail_on_difference:
        counts = result.report.counts
        if counts.amount_mismatch or counts.only_in_ledger or counts.only_in_statement:
            return EXIT_DIFFERENCE
    return EXIT_OK


def _parse_now(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"--now 需要 ISO 8601 时间，收到 {raw!r}") from exc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

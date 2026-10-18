"""命令行入口：``recon run`` / ``recon review``。

退出码约定（便于接入 CI/定时任务）：
0 成功；1 存在差异（run 需 --fail-on-difference）或审核调用失败；
2 参数错误；3 输入、配置或环境变量不可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import Policy, load_policy
from .errors import ConfigError, InputError, ReconError
from .export import write_excel, write_report_json
from .models import Report
from .pipeline import run_reconciliation, summarize
from .review import (
    LLMSettings,
    OpenAICompatibleClient,
    ReviewPolicy,
    ReviewRequest,
    StubClient,
    build_batch,
    build_requests,
    review_all,
)
from .review import (
    file_json as review_file_json,
)

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

    review = subparsers.add_parser("review", help="用 LLM 复核对账差异（阶段 2）")
    review.add_argument("--report", required=True, type=Path, help="由 recon run 生成的 recon_report.json")
    review.add_argument("--out-dir", type=Path, default=Path("out"), help="输出目录，默认 ./out")
    review.add_argument("--limit", type=int, default=20, help="最多复核多少条差异，默认 20")
    review.add_argument("--threshold", type=float, default=None, help="覆盖置信度阈值（默认取环境变量或 0.85）")
    review.add_argument("--include-duplicates", action="store_true", help="把重复单据也纳入复核")
    review.add_argument("--stub", action="store_true", help="使用本地假响应演示，不发起真实 API 调用")
    review.add_argument("--dry-run", action="store_true", help="只打印将要复核的请求，不调用模型")
    review.add_argument("--now", default=None, help="覆盖报告生成时间（ISO 8601）")
    review.add_argument("--quiet", action="store_true", help="不打印逐条结论")
    return parser


def _resolve_policy(path: Path | None) -> Policy:
    return load_policy(path) if path is not None else Policy()


def _parse_now(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"--now 需要 ISO 8601 时间，收到 {raw!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "review":
        return _cmd_review(args)

    parser.print_help()  # pragma: no cover - argparse 已保证
    return EXIT_INPUT


def _cmd_run(args: argparse.Namespace) -> int:
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


def _cmd_review(args: argparse.Namespace) -> int:
    report_path = Path(args.report)
    if not report_path.is_file():
        print(f"错误：报告文件不存在：{report_path}", file=sys.stderr)
        return EXIT_INPUT
    if args.threshold is not None and not 0 <= args.threshold <= 1:
        print("错误：--threshold 必须在 0 到 1 之间", file=sys.stderr)
        return EXIT_INPUT

    try:
        report = Report.model_validate_json(report_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        print(f"错误：{report_path.name} 不是有效的对账报告（{exc.error_count()} 处不合法）", file=sys.stderr)
        return EXIT_INPUT

    requests = build_requests(
        report,
        include_duplicates=args.include_duplicates,
        limit=args.limit,
    )

    if args.dry_run:
        return _print_dry_run(requests)

    generated_at = _parse_now(args.now) or datetime.now()
    if not requests:
        batch = build_batch(
            [],
            provider="none",
            model="-",
            policy=ReviewPolicy(confidence_threshold=args.threshold or 0.85, max_attempts=1),
            generated_at=generated_at,
        )
        output_path = _write_review_batch(batch, Path(args.out_dir))
        if not args.quiet:
            _print_review_summary(batch, requests, output_path)
        return EXIT_OK

    try:
        policy, client = _build_review_client(args, requests)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT

    try:
        outcomes = review_all(requests, client, policy)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    batch = build_batch(
        outcomes,
        provider=client.name,
        model=client.model,
        policy=policy,
        generated_at=generated_at,
    )

    output_path = _write_review_batch(batch, Path(args.out_dir))
    if not args.quiet:
        _print_review_summary(batch, requests, output_path)
    return EXIT_DIFFERENCE if batch.summary.failed else EXIT_OK


def _write_review_batch(batch, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "review_report.json"
    output_path.write_bytes(review_file_json(batch))
    return output_path


def _build_review_client(args: argparse.Namespace, requests: list[ReviewRequest]):
    if args.stub:
        client = StubClient([_demo_verdict(request) for request in requests])
        threshold = args.threshold if args.threshold is not None else 0.85
        return ReviewPolicy(confidence_threshold=threshold, max_attempts=1), client

    settings = LLMSettings.from_env()
    threshold = args.threshold if args.threshold is not None else settings.confidence_threshold
    policy = ReviewPolicy(confidence_threshold=threshold, max_attempts=settings.max_attempts)
    return policy, OpenAICompatibleClient(settings)


def _print_dry_run(requests: list[ReviewRequest]) -> int:
    print(f"待复核 {len(requests)} 条（dry-run，不调用模型）")
    for request in requests:
        print(f"  - {request.item_id}  {request.status}  {request.invoice_no}  {request.counterparty}")
    if requests:
        print("首条请求事实：")
        print(json.dumps(requests[0].to_facts(), ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


def _print_review_summary(batch, requests: list[ReviewRequest], output_path: Path) -> None:
    summary = batch.summary
    print(f"复核 {summary.total} 条差异（{batch.provider} / {batch.model}，阈值 {batch.policy.confidence_threshold}）")
    print(f"  建议可直接采纳  {summary.auto_suggested}")
    print(f"  转人工复核      {summary.manual_review}")
    print(f"  审核失败        {summary.failed}")
    for outcome, request in zip(batch.outcomes, requests, strict=False):
        if outcome.verdict is None:
            print(f"  - {request.invoice_no}  {outcome.status.label_zh}  {outcome.error_code}: {outcome.error_detail}")
            continue
        verdict = outcome.verdict
        print(
            f"  - {request.invoice_no}  {outcome.status.label_zh}  "
            f"{verdict.verdict.value} {verdict.confidence:.2f}  {verdict.explanation}"
        )
    print(f"报告摘要：{batch.digest}")
    print(f"输出：{output_path}")


def _demo_verdict(request: ReviewRequest) -> str:
    """``--stub`` 演示用的确定性结论，只引用请求里已有的事实。"""

    delta = Decimal(request.delta) if request.delta else None
    base = {
        "verdict": "needs_review",
        "category": "other",
        "confidence": 0.5,
        "explanation": "仅凭当前对账事实无法判断差异原因。",
        "recommended_action": "请人工核对合同、送货单与发票。",
        "evidence": [f"状态 {request.status}"],
    }

    if request.status == "amount_mismatch" and delta is not None and abs(delta) <= Decimal("300"):
        base.update(
            {
                "verdict": "approve",
                "category": "amount_diff",
                "confidence": 0.91,
                "explanation": (
                    f"台账 {request.ledger_amount} 与对账单 {request.statement_amount} "
                    f"相差 {request.delta}，差异金额很小，可能为运费或四舍五入尾差。"
                ),
                "recommended_action": "按对账单金额确认差异，并要求供应商补充差异说明。",
                "evidence": [
                    f"台账金额 {request.ledger_amount}",
                    f"对账单金额 {request.statement_amount}",
                    f"差额 {request.delta}",
                ],
            }
        )
    elif request.status == "only_in_ledger":
        base.update(
            {
                "verdict": "needs_review",
                "category": "missing_in_statement",
                "confidence": 0.58,
                "explanation": (
                    f"台账记录了 {request.invoice_no}，但供应商对账单中没有这张票，可能对方漏记或本期未结算。"
                ),
                "recommended_action": "向供应商确认该发票是否已开具、是否计入本期对账单。",
                "evidence": [f"发票号 {request.invoice_no}", f"台账金额 {request.ledger_amount}"],
            }
        )
    elif request.status == "only_in_statement":
        base.update(
            {
                "verdict": "needs_review",
                "category": "missing_in_ledger",
                "confidence": 0.55,
                "explanation": f"对账单出现了 {request.invoice_no}，但内部台账没有对应记录，可能未入账或重复开票。",
                "recommended_action": "核对采购入库单与发票原件，确认是否补入台账。",
                "evidence": [f"发票号 {request.invoice_no}", f"对账单金额 {request.statement_amount}"],
            }
        )
    elif request.status == "duplicate":
        base.update(
            {
                "verdict": "needs_review",
                "category": "duplicate",
                "confidence": 0.52,
                "explanation": "同一发票号在来源文件中出现多次，需要确认是否为重复录入。",
                "recommended_action": "人工确认后删除重复行，并检查导入流程。",
                "evidence": [f"发票号 {request.invoice_no}"],
            }
        )

    return json.dumps(base, ensure_ascii=False)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

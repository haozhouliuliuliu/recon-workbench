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
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import Policy, load_policy
from .errors import ConfigError, InputError, ReconError
from .export import write_excel, write_report_json
from .models import Report, Status
from .pipeline import run_reconciliation, summarize
from .review import (
    LLMSettings,
    OpenAICompatibleClient,
    ReviewPolicy,
    ReviewRequest,
    StubClient,
    build_batch,
    build_requests,
    demo_verdict,
    review_all,
)
from .review import (
    file_json as review_file_json,
)
from .settings import AppSettings
from .workflow import (
    HumanDecision,
    JobNotFound,
    ReviewSettings,
    StageName,
    StageStatus,
    WorkflowError,
    WorkflowStatus,
    connect,
    get_status,
    list_jobs,
    record_decision,
    resume_job,
    start_job,
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

    workflow = subparsers.add_parser("workflow", help="阶段 3：任务化对账、断点恢复与人工复核")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)

    workflow_run = workflow_sub.add_parser("run", help="创建（或复用）任务并推进")
    workflow_run.add_argument("--ledger", required=True, type=Path, help="内部应付台账（CSV / XLSX）")
    workflow_run.add_argument("--statement", required=True, type=Path, help="供应商对账单（CSV / XLSX）")
    workflow_run.add_argument("--db", type=Path, default=Path("data/recon.db"), help="SQLite 路径")
    workflow_run.add_argument("--storage", type=Path, default=Path("data/jobs"), help="产物目录")
    workflow_run.add_argument("--config", type=Path, default=None, help="TOML 策略文件")
    workflow_run.add_argument("--no-review", action="store_true", help="跳过 LLM 复核阶段")
    workflow_run.add_argument("--stub", action="store_true", help="用本地假响应复核（无需密钥）")
    workflow_run.add_argument("--threshold", type=float, default=None, help="覆盖置信度阈值")
    workflow_run.add_argument("--limit", type=int, default=20, help="最多复核多少条差异")
    workflow_run.add_argument("--force", action="store_true", help="清空建议与人工决策后重跑")
    workflow_run.add_argument("--quiet", action="store_true", help="只打印任务号")
    workflow_run.add_argument("--json", action="store_true", help="以 JSON 打印任务状态")

    workflow_status = workflow_sub.add_parser("status", help="查看任务状态")
    workflow_status.add_argument("job_id", help="任务号")
    workflow_status.add_argument("--db", type=Path, default=Path("data/recon.db"))
    workflow_status.add_argument("--json", action="store_true", help="以 JSON 输出")
    workflow_status.add_argument("--events", type=int, default=5, help="显示最近多少条事件")

    workflow_list = workflow_sub.add_parser("list", help="列出任务")
    workflow_list.add_argument("--db", type=Path, default=Path("data/recon.db"))
    workflow_list.add_argument("--limit", type=int, default=20)
    workflow_list.add_argument("--json", action="store_true")

    workflow_resume = workflow_sub.add_parser("resume", help="从断点恢复任务")
    workflow_resume.add_argument("job_id", help="任务号")
    workflow_resume.add_argument("--db", type=Path, default=Path("data/recon.db"))
    workflow_resume.add_argument("--storage", type=Path, default=Path("data/jobs"))
    workflow_resume.add_argument("--stub", action="store_true", help="强制用假响应（任务原本是 stub 时自动使用）")
    workflow_resume.add_argument("--quiet", action="store_true")
    workflow_resume.add_argument("--json", action="store_true")

    workflow_decide = workflow_sub.add_parser("decide", help="记录人工复核结论")
    workflow_decide.add_argument("job_id", help="任务号")
    workflow_decide.add_argument("item_id", help="差异条目 ID")
    workflow_decide.add_argument(
        "--decision",
        required=True,
        choices=[value.value for value in HumanDecision],
        help="accepted 认可 / rejected 驳回 / needs_info 退回补充",
    )
    workflow_decide.add_argument("--by", required=True, dest="decided_by", help="操作人")
    workflow_decide.add_argument("--note", default=None, help="备注")
    workflow_decide.add_argument("--db", type=Path, default=Path("data/recon.db"))
    workflow_decide.add_argument("--quiet", action="store_true")
    workflow_decide.add_argument("--json", action="store_true")

    config = subparsers.add_parser("config", help="查看生效配置（不含密钥）")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show", help="打印当前配置")
    config_show.add_argument("--json", action="store_true", help="JSON 输出")

    api = subparsers.add_parser("api", help="启动 HTTP 接口（阶段 4）")
    api.add_argument("--host", default=None, help="监听地址，默认取 RECON_HOST")
    api.add_argument("--port", type=int, default=None, help="端口，默认取 RECON_PORT")

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
    if args.command == "workflow":
        return _cmd_workflow(args)
    if args.command == "config":
        return _cmd_config(args)
    if args.command == "api":
        return _cmd_api(args)

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
        client = StubClient([demo_verdict(request) for request in requests])
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


def _cmd_workflow(args: argparse.Namespace) -> int:
    if args.workflow_command == "run":
        return _workflow_run(args)
    if args.workflow_command == "status":
        return _workflow_status(args)
    if args.workflow_command == "list":
        return _workflow_list(args)
    if args.workflow_command == "resume":
        return _workflow_resume(args)
    if args.workflow_command == "decide":
        return _workflow_decide(args)
    print("未知的 workflow 子命令", file=sys.stderr)
    return EXIT_INPUT


def _workflow_review_settings(args: argparse.Namespace):
    if args.no_review:
        return ReviewSettings(enabled=False), None

    if args.stub:
        review = ReviewSettings(
            enabled=True,
            provider="stub",
            model="stub-model",
            threshold=args.threshold if args.threshold is not None else 0.85,
            max_attempts=1,
            limit=args.limit,
        )
        return review, lambda requests: StubClient([demo_verdict(request) for request in requests])

    settings = LLMSettings.from_env()
    review = ReviewSettings(
        enabled=True,
        provider="openai-compatible",
        model=settings.model,
        threshold=args.threshold if args.threshold is not None else settings.confidence_threshold,
        max_attempts=settings.max_attempts,
        limit=args.limit,
    )
    return review, lambda requests: OpenAICompatibleClient(settings)


def _workflow_run(args: argparse.Namespace) -> int:
    try:
        review, provider = _workflow_review_settings(args)
        policy = _resolve_policy(args.config)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT

    connection = connect(args.db)
    try:
        status = start_job(
            connection,
            ledger_path=args.ledger,
            statement_path=args.statement,
            storage_dir=args.storage,
            policy=policy,
            review=review,
            client_provider=provider,
            force=args.force,
        )
    except (InputError, WorkflowError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    finally:
        connection.close()

    return _emit_workflow_status(status, args)


def _workflow_status(args: argparse.Namespace) -> int:
    connection = connect(args.db)
    try:
        status = get_status(connection, args.job_id)
    except JobNotFound as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    finally:
        connection.close()

    if args.json:
        print(status.model_dump_json(indent=2))
    else:
        print(_format_workflow(status, events=args.events))
    return EXIT_OK


def _workflow_list(args: argparse.Namespace) -> int:
    connection = connect(args.db)
    try:
        jobs = list_jobs(connection, args.limit)
    finally:
        connection.close()

    if args.json:
        payload = [job.model_dump(mode="json") for job in jobs]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not jobs:
        print("暂无任务")
        return EXIT_OK
    print(f"{'任务号':<18} {'状态':<16} {'待人工':>5}  {'台账':<24} {'对账单':<24}")
    for job in jobs:
        print(
            f"{job.job_id:<18} {job.status:<16} {job.pending_count:>5}  "
            f"{job.ledger_filename:<24} {job.statement_filename:<24}"
        )
    return EXIT_OK


def _workflow_resume(args: argparse.Namespace) -> int:
    connection = connect(args.db)
    try:
        current = get_status(connection, args.job_id)
        if not current.review_enabled:
            provider = None
        elif args.stub or current.review_provider == "stub":
            provider = lambda requests: StubClient([demo_verdict(request) for request in requests])  # noqa: E731
        else:
            provider = lambda requests: OpenAICompatibleClient(LLMSettings.from_env())  # noqa: E731

        status = resume_job(
            connection,
            args.job_id,
            storage_dir=args.storage,
            client_provider=provider,
        )
    except (JobNotFound, InputError, WorkflowError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    finally:
        connection.close()

    return _emit_workflow_status(status, args)


def _workflow_decide(args: argparse.Namespace) -> int:
    connection = connect(args.db)
    try:
        status = record_decision(
            connection,
            job_id=args.job_id,
            item_id=args.item_id,
            decision=args.decision,
            decided_by=args.decided_by,
            note=args.note,
        )
    except (JobNotFound, WorkflowError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    finally:
        connection.close()

    return _emit_workflow_status(status, args)


def _emit_workflow_status(status: WorkflowStatus, args: argparse.Namespace) -> int:
    if args.json:
        print(status.model_dump_json(indent=2))
    elif args.quiet:
        print(status.job_id)
    else:
        print(_format_workflow(status))
    return EXIT_DIFFERENCE if status.status == "failed" else EXIT_OK


JOB_STATUS_LABELS = {
    "pending": "待执行",
    "running": "执行中",
    "awaiting_human": "待人工复核",
    "completed": "已完成",
    "failed": "失败",
}


def _format_workflow(status: WorkflowStatus, *, events: int = 5) -> str:
    lines = [
        f"任务 {status.job_id}  状态：{JOB_STATUS_LABELS.get(status.status, status.status)}",
        f"输入：{status.ledger_filename} / {status.statement_filename}",
        "阶段：",
    ]
    for stage in status.stages:
        name = StageName(stage.stage).label_zh
        line = f"  - {name:<8} {StageStatus(stage.status).label_zh}（尝试 {stage.attempts}）"
        if stage.error:
            line += f" 错误：{stage.error}"
        lines.append(line)

    if status.counts:
        counts = " / ".join(f"{_status_label(name)} {value}" for name, value in sorted(status.counts.items()))
        lines.append(f"对账结果：{counts}")

    if status.review_enabled:
        auto = sum(1 for item in status.suggestions if item.status == "auto_suggested")
        manual = sum(1 for item in status.suggestions if item.status == "manual_review")
        failed = sum(1 for item in status.suggestions if item.status == "failed")
        lines.append(
            f"复核建议：建议采纳 {auto} / 转人工 {manual} / 失败 {failed}"
            f"（{status.review_provider} / {status.review_model}，阈值 {status.review_threshold}）"
        )

    decided = len(status.decisions)
    lines.append(f"人工决策：{decided} / {decided + len(status.pending)} 条差异")

    if status.pending:
        suggestions = {item.item_id: item for item in status.suggestions}
        lines.append(f"待人工决策 {len(status.pending)} 条：")
        for item in status.pending[:10]:
            suggestion = suggestions.get(item.item_id)
            extra = ""
            if suggestion is not None:
                if suggestion.verdict:
                    extra = f"  建议 {suggestion.verdict}({suggestion.confidence or 0:.2f})"
                elif suggestion.error_code:
                    extra = f"  建议失败 {suggestion.error_code}"
            lines.append(
                f"  - {item.item_id}  {item.invoice_no}  {_status_label(item.status)}  差额 {item.delta or '—'}{extra}"
            )
        if len(status.pending) > 10:
            lines.append(f"  …还有 {len(status.pending) - 10} 条")

    if status.error:
        lines.append(f"错误：{status.error}")

    if events and status.events:
        lines.append("最近事件：")
        for event in status.events[:events]:
            detail = f"  {event.detail}" if event.detail else ""
            lines.append(f"  {event.at}  {event.event}{detail}")
    return "\n".join(lines)


def _status_label(value: str) -> str:
    try:
        return Status(value).label_zh
    except ValueError:
        return value


def _cmd_config(args: argparse.Namespace) -> int:
    try:
        settings = AppSettings.from_env()
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT

    payload = settings.describe()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK
    for key, value in payload.items():
        print(f"{key}: {value}")
    return EXIT_OK


def _cmd_api(args: argparse.Namespace) -> int:
    try:
        settings = AppSettings.from_env()
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT

    host = args.host or settings.api_host
    port = args.port or settings.api_port
    if not 1 <= port <= 65535:
        print("错误：--port 必须在 1..65535 之间", file=sys.stderr)
        return EXIT_INPUT

    try:
        import uvicorn
    except ImportError:
        print('错误：未安装服务端依赖，请执行 pip install -e ".[server]"', file=sys.stderr)
        return EXIT_INPUT

    from .api import create_app

    application = create_app(settings)
    print(f"API 启动：http://{host}:{port}  文档：/docs")
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

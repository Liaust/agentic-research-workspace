"""Non-interactive CLI for deterministic research-map coordination."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from research_map.audit import AuditCoordinator, AuditValidationError
from research_map.calibration import (
    CalibrationCoordinator,
    CalibrationExecutionError,
    CalibrationValidationError,
)
from research_map.compiler import CompilationCoordinator, CompilationError
from research_map.corpus_completion import (
    CorpusCompletionCoordinator,
    CorpusCompletionExecutionError,
    CorpusCompletionValidationError,
    quality_source_summaries,
)
from research_map.corpus_run import (
    CorpusRunCoordinator,
    CorpusRunExecutionError,
    CorpusRunValidationError,
)
from research_map.cross_reference import (
    RELATIONSHIP_TYPES,
    CrossReferenceValidationError,
    validate_schema,
)
from research_map.cross_reference_benchmark import (
    DEFAULT_AUDIT_SAMPLE_SIZE,
    CrossReferenceBenchmarkCoordinator,
    CrossReferenceBenchmarkError,
    CrossReferenceBenchmarkExecutionError,
)
from research_map.cross_reference_loop import (
    CrossReferenceCoordinator,
    CrossReferenceExecutionError,
)
from research_map.cross_reference_recovery import (
    CrossReferenceRecoveryCoordinator,
    CrossReferenceRecoveryExecutionError,
    CrossReferenceRecoveryValidationError,
)
from research_map.deterministic_salvage import (
    DeterministicSalvageCoordinator,
    DeterministicSalvageValidationError,
)
from research_map.exploration import (
    GraphExplorationError,
    GraphExplorer,
    SearchFilters,
    UnresolvedGraphObject,
)
from research_map.focused_cross_reference_calibration import (
    FocusedCrossReferenceCalibrationCoordinator,
    FocusedCrossReferenceCalibrationError,
    FocusedCrossReferenceCalibrationExecutionError,
)
from research_map.map_first import MAP_FIRST_READER_MODE, MapFirstReadingCoordinator
from research_map.multi_surface_cross_reference_calibration import (
    MultiSurfaceCrossReferenceCalibrationCoordinator,
    MultiSurfaceCrossReferenceCalibrationError,
    MultiSurfaceCrossReferenceCalibrationExecutionError,
)
from research_map.proposals import ManualReviewRequired, ProposalValidationError
from research_map.public_release import (
    PublicReleaseBuilder,
    PublicReleaseValidationError,
    verify_public_release,
)
from research_map.reading import (
    ReadingCoordinator,
    ReadingValidationError,
    WorkerExecutionError,
    validate_source_wip,
)
from research_map.receipts import read_receipt
from research_map.results import CommandResult, ResultClassification
from research_map.source import (
    SourceResolutionError,
    SourceValidationError,
    find_repository_root,
    resolve_source,
    verify_asset,
)
from research_map.state import IdentityMismatch, MissingIdentity, StateRepository
from research_map.transitions import InvalidTransition
from research_map.validation import DossierValidationError
from research_map.visual import VisualTransportError

DEFAULT_STATE_PATH = Path(".cache/research-map/state.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-map")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="register one immutable corpus source")
    _source_argument(register, required=True)
    _common_arguments(register)

    status = subparsers.add_parser("status", help="inspect durable source or batch state")
    status_selector = status.add_mutually_exclusive_group()
    status_selector.add_argument("--source")
    status_selector.add_argument("--batch")
    _common_arguments(status)

    validate = subparsers.add_parser("validate", help="validate coordinator capabilities")
    _source_argument(validate, required=False)
    _common_arguments(validate)

    run = subparsers.add_parser("run", help="run the source-local deterministic pipeline")
    _source_argument(run, required=True)
    run.add_argument(
        "--through",
        required=True,
        choices=("source_complete", "audit_passed", "graph_inserted"),
    )
    run.add_argument("--model", required=True)
    run.add_argument(
        "--reader-mode",
        choices=("staged", MAP_FIRST_READER_MODE),
        default="staged",
        help="select the source-local reader topology; default: staged",
    )
    run.add_argument(
        "--reviewed-finding",
        action="append",
        default=[],
        help="open latest-third-audit finding approved for one reviewed resume",
    )
    run.add_argument(
        "--review-rationale",
        help="explicit source-review rationale required with --reviewed-finding",
    )
    run.add_argument(
        "--replace-artifacts",
        action="store_true",
        help="replace a complete fingerprint-verified prior projection after audit",
    )
    _common_arguments(run)

    calibrate = subparsers.add_parser(
        "calibrate-corpus",
        help="run an isolated fail-continuing provisional corpus calibration",
    )
    calibrate.add_argument("--calibration", required=True)
    calibrate.add_argument("--source", action="append", required=True)
    calibrate.add_argument("--through", required=True, choices=("source-maps", "cross-reference"))
    calibrate.add_argument("--model", required=True)
    calibrate.add_argument("--source-timeout-seconds", type=float, default=5400)
    calibrate.add_argument("--cross-reference-timeout-seconds", type=float, default=5400)
    calibrate.add_argument("--json", action="store_true", dest="as_json")

    run_corpus = subparsers.add_parser(
        "run-corpus",
        help="preflight or execute one hash-approved manifest-driven corpus run",
    )
    run_corpus.add_argument("--manifest", type=Path, required=True)
    run_corpus.add_argument("--preflight-only", action="store_true")
    run_corpus.add_argument("--approved-launch-plan-sha256")
    run_corpus.add_argument("--json", action="store_true", dest="as_json")

    complete_corpus = subparsers.add_parser(
        "complete-corpus",
        help=(
            "operate one isolated corpus-completion run; see the tracked feature "
            "operator_runbook.md"
        ),
    )
    complete_corpus.add_argument(
        "completion_action", choices=("preflight", "run", "status", "report")
    )
    complete_corpus.add_argument("--manifest", type=Path, required=True)
    complete_corpus.add_argument("--source-run-root", type=Path, required=True)
    complete_corpus.add_argument("--prior-completion-root", type=Path)
    complete_corpus.add_argument("--destination-run-root", type=Path, required=True)
    complete_corpus.add_argument("--through", choices=("audit", "quality", "cross-reference"))
    complete_corpus.add_argument("--approved-launch-plan-sha256")
    complete_corpus.add_argument(
        "--resume-started-audits",
        action="store_true",
        help=(
            "explicitly continue audit sources whose retained worker receipts prove "
            "that no semantic process is still live"
        ),
    )
    complete_corpus.add_argument("--json", action="store_true", required=True, dest="as_json")

    recover_cross_reference = subparsers.add_parser(
        "recover-cross-reference",
        help="preflight or run one bounded retained failed-shard recovery",
    )
    recover_cross_reference.add_argument(
        "recovery_action", choices=("preflight", "run", "status", "report")
    )
    recover_cross_reference.add_argument("--manifest", type=Path, required=True)
    recover_cross_reference.add_argument("--origin-run-root", type=Path, required=True)
    recover_cross_reference.add_argument("--destination-run-root", type=Path, required=True)
    recover_cross_reference.add_argument("--approved-launch-plan-sha256")
    recover_cross_reference.add_argument("--json", action="store_true", dest="as_json")

    salvage_recovery = subparsers.add_parser(
        "salvage-recovery",
        help="deterministically admit already-validator-accepted rows from one failed shard",
    )
    salvage_recovery.add_argument("salvage_action", choices=("run", "report"))
    salvage_recovery.add_argument("--origin-recovery-root", type=Path, required=True)
    salvage_recovery.add_argument("--destination-run-root", type=Path, required=True)
    salvage_recovery.add_argument("--shard-id", required=True)
    salvage_recovery.add_argument("--json", action="store_true", dest="as_json")

    cross_reference = subparsers.add_parser(
        "cross-reference", help="discover and inspect cross-source relationships"
    )
    cross_reference.add_argument("--source", action="append", required=True)
    cross_reference.add_argument(
        "--source-group",
        action="append",
        default=[],
        metavar="SOURCE[,SOURCE...]",
        help=(
            "repeat exactly three times to run cyclic source-sharded discovery; "
            "groups must be disjoint and cover every --source"
        ),
    )
    cross_reference.add_argument(
        "--through", required=True, choices=("discovered", "inspected", "compiled")
    )
    cross_reference.add_argument("--model", required=True)
    cross_reference.add_argument(
        "--calibration-root",
        type=Path,
        help="explicit isolated calibration root containing state, cache, vault, and build",
    )
    cross_reference.add_argument(
        "--revalidate-retained",
        action="store_true",
        help="reprocess only existing terminal holistic proposals without invoking Codex",
    )
    _common_arguments(cross_reference)

    benchmark = subparsers.add_parser(
        "benchmark-cross-reference",
        help="evaluate frozen cross-reference quality, bounded coverage, and cost",
    )
    benchmark.add_argument("--benchmark", required=True)
    benchmark.add_argument("--batch", required=True)
    benchmark.add_argument("--calibration-root", type=Path, required=True)
    benchmark.add_argument(
        "--output-root",
        type=Path,
        default=Path(".cache/research-map/benchmarks"),
    )
    benchmark.add_argument("--model", required=True)
    benchmark.add_argument("--coverage-source", action="append", required=True)
    benchmark.add_argument(
        "--source-family",
        action="append",
        default=[],
        metavar="SOURCE,SOURCE[,SOURCE...]",
    )
    benchmark.add_argument("--generation-effective-tokens", type=int, required=True)
    benchmark.add_argument("--audit-sample-size", type=int, default=DEFAULT_AUDIT_SAMPLE_SIZE)
    benchmark.add_argument("--timeout-seconds", type=float, default=5400)
    benchmark.add_argument("--json", action="store_true", dest="as_json")

    focused = subparsers.add_parser(
        "calibrate-focused-cross-reference",
        help="run a read-only graph-blind five-source mapper calibration",
    )
    focused.add_argument("--calibration", required=True)
    focused.add_argument("--batch", required=True)
    focused.add_argument("--calibration-root", type=Path, required=True)
    focused.add_argument("--reference-benchmark-root", type=Path, required=True)
    focused.add_argument(
        "--output-root",
        type=Path,
        default=Path(".cache/research-map/calibrations"),
    )
    focused.add_argument("--model", required=True)
    focused.add_argument("--source", action="append", required=True)
    focused.add_argument("--timeout-seconds", type=float, default=5400)
    focused.add_argument("--json", action="store_true", dest="as_json")

    multi_surface = subparsers.add_parser(
        "calibrate-multi-surface-cross-reference",
        help="run a read-only graph-blind variable-surface five-source calibration",
    )
    multi_surface.add_argument("--calibration", required=True)
    multi_surface.add_argument("--batch", required=True)
    multi_surface.add_argument("--calibration-root", type=Path, required=True)
    multi_surface.add_argument("--reference-benchmark-root", type=Path, required=True)
    multi_surface.add_argument(
        "--output-root",
        type=Path,
        default=Path(".cache/research-map/calibrations"),
    )
    multi_surface.add_argument("--model", required=True)
    multi_surface.add_argument("--source", action="append", required=True)
    multi_surface.add_argument("--timeout-seconds", type=float, default=5400)
    multi_surface.add_argument("--json", action="store_true", dest="as_json")

    build_public = subparsers.add_parser(
        "build-public-release",
        help="build one deterministic aggregate-only public repository candidate",
    )
    build_public.add_argument("--repository-root", type=Path, required=True)
    build_public.add_argument("--source-registration-root", type=Path, required=True)
    build_public.add_argument("--asset-root", type=Path, required=True)
    build_public.add_argument("--graph", type=Path, required=True)
    build_public.add_argument("--graph-manifest", type=Path, required=True)
    build_public.add_argument("--private-report", type=Path, required=True)
    build_public.add_argument("--policy", type=Path, required=True)
    build_public.add_argument("--input-lock", type=Path, required=True)
    build_public.add_argument("--output", type=Path, required=True)
    build_public.add_argument("--json", action="store_true", dest="as_json")

    verify_public = subparsers.add_parser(
        "verify-public-release",
        help="verify one public candidate without private repository context",
    )
    verify_public.add_argument("--candidate-root", type=Path, required=True)
    verify_public.add_argument("--json", action="store_true", dest="as_json")

    search = subparsers.add_parser("search", help="search one explicit compiled research graph")
    search.add_argument("query")
    search.add_argument("--graph", type=Path, required=True)
    search.add_argument("--source", action="append", default=[])
    search.add_argument(
        "--record-type",
        action="append",
        default=[],
        choices=("evidence", "atom", "move", "thread", "relationship"),
    )
    search.add_argument("--kind", action="append", default=[])
    search.add_argument(
        "--relationship-type",
        action="append",
        default=[],
        choices=tuple(sorted(RELATIONSHIP_TYPES)),
    )
    search.add_argument("--connectable", choices=("true", "false"))
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true", dest="as_json")

    explore = subparsers.add_parser(
        "explore", help="resolve a stable graph ID with bounded one-hop context"
    )
    explore.add_argument("--id", required=True, dest="object_id")
    explore.add_argument("--graph", type=Path, required=True)
    explore.add_argument("--neighbor-limit", type=int, default=25)
    explore.add_argument("--json", action="store_true", dest="as_json")

    inspect = subparsers.add_parser("inspect", help="inspect a durable operational object")
    inspect_selector = inspect.add_mutually_exclusive_group(required=True)
    inspect_selector.add_argument("--job")
    inspect_selector.add_argument("--candidate")
    inspect_selector.add_argument("--relationship")
    _common_arguments(inspect)
    return parser


def _source_argument(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--source", required=required)


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--json", action="store_true", dest="as_json")


def dispatch(args: argparse.Namespace) -> CommandResult:
    if args.command == "build-public-release":
        try:
            public_result = PublicReleaseBuilder(
                repository_root=args.repository_root,
                source_registration_root=args.source_registration_root,
                asset_root=args.asset_root,
                graph_path=args.graph,
                graph_manifest_path=args.graph_manifest,
                private_report_path=args.private_report,
                policy_path=args.policy,
                input_lock_path=args.input_lock,
            ).build(args.output)
        except (PublicReleaseValidationError, OSError) as error:
            return CommandResult(
                command="build-public-release",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"output": str(args.output)},
                errors=(str(error),),
            )
        return CommandResult(
            command="build-public-release",
            ok=True,
            identifiers={
                "output": str(public_result.candidate_root),
                "tree_sha256": public_result.tree_sha256,
            },
            current_state="candidate_built",
            artifacts=(
                {
                    "kind": "public_release_manifest",
                    "path": str(public_result.manifest_path),
                },
                {
                    "kind": "public_release_report",
                    "path": str(public_result.report_path),
                },
            ),
            data=public_result.to_dict(),
        )

    if args.command == "verify-public-release":
        try:
            public_verification = verify_public_release(args.candidate_root)
        except (PublicReleaseValidationError, OSError) as error:
            return CommandResult(
                command="verify-public-release",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"candidate": str(args.candidate_root)},
                errors=(str(error),),
            )
        return CommandResult(
            command="verify-public-release",
            ok=True,
            identifiers={
                "candidate": str(public_verification.candidate_root),
                "tree_sha256": public_verification.tree_sha256,
            },
            current_state="candidate_verified",
            data=public_verification.to_dict(),
        )

    if args.command in {"search", "explore"}:
        try:
            explorer = GraphExplorer(graph_path=args.graph)
            if args.command == "search":
                connectable = None if args.connectable is None else args.connectable == "true"
                exploration_data = explorer.search(
                    args.query,
                    filters=SearchFilters(
                        source_ids=tuple(args.source),
                        record_types=tuple(args.record_type),
                        kinds=tuple(args.kind),
                        relationship_types=tuple(args.relationship_type),
                        connectable=connectable,
                    ),
                    limit=args.limit,
                )
                identifiers = {
                    "graph": explorer.graph_fingerprint.sha256,
                    "query": args.query,
                }
            else:
                exploration_data = explorer.explore(
                    args.object_id, neighbor_limit=args.neighbor_limit
                )
                identifiers = {
                    "graph": explorer.graph_fingerprint.sha256,
                    "id": args.object_id,
                }
            exploration_data = _with_source_quality(explorer, exploration_data)
        except UnresolvedGraphObject as error:
            return CommandResult(
                command=args.command,
                ok=False,
                classification=ResultClassification.UNRESOLVED_IDENTITY,
                identifiers={"id": args.object_id},
                errors=(str(error),),
            )
        except (GraphExplorationError, OSError) as error:
            return CommandResult(
                command=args.command,
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                errors=(str(error),),
            )
        return CommandResult(
            command=args.command,
            ok=True,
            identifiers=identifiers,
            current_state="read_only",
            artifacts=(
                {
                    "kind": f"{explorer.graph_kind}_graph",
                    "path": str(explorer.graph_path),
                    "sha256": explorer.graph_fingerprint.sha256,
                    "size_bytes": explorer.graph_fingerprint.size_bytes,
                },
            ),
            data=exploration_data,
        )

    if args.command == "calibrate-multi-surface-cross-reference":
        try:
            multi_surface_result = MultiSurfaceCrossReferenceCalibrationCoordinator(
                repository_root=find_repository_root(),
                calibration_root=args.calibration_root,
                reference_benchmark_root=args.reference_benchmark_root,
                output_root=args.output_root,
                timeout_seconds=args.timeout_seconds,
            ).run(
                calibration_id=args.calibration,
                batch_id=args.batch,
                model=args.model,
                source_ids=tuple(args.source),
            )
        except MultiSurfaceCrossReferenceCalibrationExecutionError as error:
            return CommandResult(
                command="calibrate-multi-surface-cross-reference",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                identifiers={"calibration": args.calibration, "batch": args.batch},
                errors=(str(error),),
            )
        except (MultiSurfaceCrossReferenceCalibrationError, OSError) as error:
            return CommandResult(
                command="calibrate-multi-surface-cross-reference",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"calibration": args.calibration, "batch": args.batch},
                errors=(str(error),),
            )
        return CommandResult(
            command="calibrate-multi-surface-cross-reference",
            ok=True,
            identifiers={
                "calibration": multi_surface_result.calibration_id,
                "batch": multi_surface_result.batch_id,
            },
            current_state="calibration_complete",
            artifacts=(
                {
                    "kind": "multi_surface_calibration_metrics",
                    "path": str(multi_surface_result.metrics_path),
                },
                {
                    "kind": "multi_surface_calibration_report",
                    "path": str(multi_surface_result.report_path),
                },
            ),
            data=multi_surface_result.to_dict(),
        )

    if args.command == "calibrate-focused-cross-reference":
        try:
            focused_result = FocusedCrossReferenceCalibrationCoordinator(
                repository_root=find_repository_root(),
                calibration_root=args.calibration_root,
                reference_benchmark_root=args.reference_benchmark_root,
                output_root=args.output_root,
                timeout_seconds=args.timeout_seconds,
            ).run(
                calibration_id=args.calibration,
                batch_id=args.batch,
                model=args.model,
                source_ids=tuple(args.source),
            )
        except FocusedCrossReferenceCalibrationExecutionError as error:
            return CommandResult(
                command="calibrate-focused-cross-reference",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                identifiers={"calibration": args.calibration, "batch": args.batch},
                errors=(str(error),),
            )
        except (FocusedCrossReferenceCalibrationError, OSError) as error:
            return CommandResult(
                command="calibrate-focused-cross-reference",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"calibration": args.calibration, "batch": args.batch},
                errors=(str(error),),
            )
        return CommandResult(
            command="calibrate-focused-cross-reference",
            ok=True,
            identifiers={
                "calibration": focused_result.calibration_id,
                "batch": focused_result.batch_id,
            },
            current_state="calibration_complete",
            artifacts=(
                {"kind": "focused_calibration_metrics", "path": str(focused_result.metrics_path)},
                {"kind": "focused_calibration_report", "path": str(focused_result.report_path)},
            ),
            data=focused_result.to_dict(),
        )

    if args.command == "benchmark-cross-reference":
        try:
            source_families = tuple(
                tuple(source.strip() for source in group.split(",")) for group in args.source_family
            )
            benchmark_result = CrossReferenceBenchmarkCoordinator(
                repository_root=find_repository_root(),
                calibration_root=args.calibration_root,
                output_root=args.output_root,
                timeout_seconds=args.timeout_seconds,
            ).run(
                batch_id=args.batch,
                benchmark_id=args.benchmark,
                model=args.model,
                coverage_source_ids=tuple(args.coverage_source),
                source_families=source_families,
                generation_effective_tokens=args.generation_effective_tokens,
                audit_sample_size=args.audit_sample_size,
            )
        except CrossReferenceBenchmarkExecutionError as error:
            return CommandResult(
                command="benchmark-cross-reference",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                identifiers={"benchmark": args.benchmark, "batch": args.batch},
                errors=(str(error),),
            )
        except (CrossReferenceBenchmarkError, OSError) as error:
            return CommandResult(
                command="benchmark-cross-reference",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"benchmark": args.benchmark, "batch": args.batch},
                errors=(str(error),),
            )
        return CommandResult(
            command="benchmark-cross-reference",
            ok=True,
            identifiers={
                "benchmark": benchmark_result.benchmark_id,
                "batch": benchmark_result.batch_id,
            },
            current_state="benchmark_complete",
            artifacts=(
                {"kind": "benchmark_metrics", "path": str(benchmark_result.metrics_path)},
                {"kind": "benchmark_report", "path": str(benchmark_result.report_path)},
            ),
            data=benchmark_result.to_dict(),
        )

    if args.command == "calibrate-corpus":
        try:
            calibration_result = CalibrationCoordinator(
                repository_root=find_repository_root(),
                calibration_id=args.calibration,
                source_timeout_seconds=args.source_timeout_seconds,
                cross_reference_timeout_seconds=args.cross_reference_timeout_seconds,
            ).run(tuple(args.source), through=args.through, model=args.model)
        except CalibrationValidationError as error:
            return CommandResult(
                command="calibrate-corpus",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"calibration": args.calibration},
                errors=(str(error),),
            )
        except CalibrationExecutionError as error:
            return CommandResult(
                command="calibrate-corpus",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                identifiers={"calibration": args.calibration},
                errors=(str(error),),
            )
        source_failures = tuple(
            item.source_id
            for item in calibration_result.source_results
            if item.outcome == "source_failed"
        )
        warnings = calibration_result.warnings
        if source_failures:
            warnings += (
                "calibration completed with isolated source failures: "
                + ", ".join(source_failures),
            )
        return CommandResult(
            command="calibrate-corpus",
            ok=True,
            identifiers={"calibration": calibration_result.calibration_id},
            current_state=(
                "cross_reference_complete"
                if calibration_result.cross_reference is not None
                else "source_maps_complete"
            ),
            artifacts=(
                {
                    "kind": "calibration_summary",
                    "path": str(calibration_result.root / "summary.json"),
                },
            ),
            warnings=warnings,
            data=calibration_result.to_dict(),
        )

    if args.command == "run-corpus":
        try:
            corpus_result = CorpusRunCoordinator(
                repository_root=find_repository_root(),
                manifest_path=args.manifest,
            ).run(
                preflight_only=args.preflight_only,
                approved_launch_plan_sha256=args.approved_launch_plan_sha256,
            )
        except CorpusRunValidationError as error:
            return CommandResult(
                command="run-corpus",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                errors=(str(error),),
            )
        except CorpusRunExecutionError as error:
            return CommandResult(
                command="run-corpus",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                errors=(str(error),),
            )
        return CommandResult(
            command="run-corpus",
            ok=True,
            identifiers={"run": corpus_result.run_id},
            current_state=corpus_result.status,
            artifacts=(
                {
                    "kind": "corpus_run_launch_plan",
                    "path": str(corpus_result.launch_plan_path),
                    "sha256": corpus_result.launch_plan_sha256,
                },
                {
                    "kind": "corpus_run_preflight_receipt",
                    "path": str(corpus_result.preflight_receipt_path),
                },
                *(
                    ()
                    if corpus_result.summary_path is None
                    else (
                        {
                            "kind": "corpus_run_summary",
                            "path": str(corpus_result.summary_path),
                        },
                    )
                ),
            ),
            data=corpus_result.to_dict(),
        )

    if args.command == "complete-corpus":
        completion_artifacts: tuple[dict[str, Any], ...]
        try:
            repository_root = find_repository_root()
            coordinator = CorpusCompletionCoordinator(
                repository_root=repository_root,
                manifest_path=args.manifest,
            )
            explicit_source_root = (
                args.source_run_root
                if args.source_run_root.is_absolute()
                else repository_root / args.source_run_root
            ).resolve()
            explicit_destination_root = (
                args.destination_run_root
                if args.destination_run_root.is_absolute()
                else repository_root / args.destination_run_root
            ).resolve()
            if (
                explicit_source_root != coordinator.manifest.source_run_root
                or explicit_destination_root != coordinator.manifest.destination_run_root
            ):
                raise CorpusCompletionValidationError(
                    "explicit completion roots differ from the tracked manifest"
                )
            quality_labelled = bool(getattr(coordinator.manifest, "quality_labelled", False))
            explicit_prior_root = (
                None
                if args.prior_completion_root is None
                else (
                    args.prior_completion_root
                    if args.prior_completion_root.is_absolute()
                    else repository_root / args.prior_completion_root
                ).resolve()
            )
            if quality_labelled:
                if explicit_prior_root is None:
                    raise CorpusCompletionValidationError(
                        "quality-labelled completion requires --prior-completion-root"
                    )
                if explicit_prior_root != coordinator.manifest.prior_completion_run_root:
                    raise CorpusCompletionValidationError(
                        "explicit prior completion root differs from the tracked manifest"
                    )
            elif explicit_prior_root is not None:
                raise CorpusCompletionValidationError(
                    "v1 completion does not accept --prior-completion-root"
                )
            if args.completion_action == "preflight":
                if (
                    args.through is not None
                    or args.approved_launch_plan_sha256 is not None
                    or args.resume_started_audits
                ):
                    raise CorpusCompletionValidationError(
                        "completion preflight does not accept run-only arguments"
                    )
                completion_result = coordinator.preflight()
                data = completion_result.to_dict()
                completion_artifacts = (
                    {
                        "kind": "corpus_completion_launch_plan",
                        "path": str(completion_result.launch_plan_path),
                        "sha256": completion_result.launch_plan_sha256,
                    },
                    {
                        "kind": "corpus_completion_preflight_receipt",
                        "path": str(completion_result.preflight_receipt_path),
                    },
                )
                completion_state = completion_result.state
            elif args.completion_action == "run":
                if quality_labelled and args.resume_started_audits:
                    raise CorpusCompletionValidationError(
                        "quality-labelled completion never resumes source audits"
                    )
                allowed_through = (
                    {"quality", "cross-reference"}
                    if quality_labelled
                    else {"audit", "cross-reference"}
                )
                if args.through not in allowed_through:
                    raise CorpusCompletionValidationError(
                        "quality-labelled completion run requires --through quality or "
                        "cross-reference"
                        if quality_labelled
                        else "completion run requires --through audit or cross-reference"
                    )
                if args.approved_launch_plan_sha256 is None:
                    raise CorpusCompletionValidationError(
                        "completion run requires --approved-launch-plan-sha256"
                    )
                if args.through == "quality":
                    completion_result = coordinator.run_through_quality(
                        approved_launch_plan_sha256=args.approved_launch_plan_sha256
                    )
                elif args.through == "audit":
                    completion_result = coordinator.run_through_audit(
                        approved_launch_plan_sha256=args.approved_launch_plan_sha256,
                        resume_started_audits=args.resume_started_audits,
                    )
                else:
                    completion_result = coordinator.run_through_cross_reference(
                        approved_launch_plan_sha256=args.approved_launch_plan_sha256,
                        resume_started_audits=args.resume_started_audits,
                    )
                data = completion_result.to_dict()
                completion_artifacts = tuple(
                    {"kind": kind, "path": str(path)}
                    for kind, path in (
                        (
                            "corpus_eligibility_manifest",
                            completion_result.eligibility_manifest_path,
                        ),
                        (
                            "corpus_source_selection_plan",
                            completion_result.selection_plan_path,
                        ),
                        (
                            "corpus_source_quality_manifest",
                            completion_result.quality_manifest_path,
                        ),
                        (
                            "corpus_cross_reference_plan",
                            completion_result.cross_reference_plan_path,
                        ),
                        ("corpus_graph", completion_result.corpus_graph_path),
                    )
                    if path is not None
                )
                completion_state = completion_result.state
            elif args.completion_action == "status":
                if (
                    args.through is not None
                    or args.approved_launch_plan_sha256 is not None
                    or args.resume_started_audits
                ):
                    raise CorpusCompletionValidationError(
                        "completion status does not accept run-only arguments"
                    )
                data = coordinator.status()
                completion_artifacts = tuple(data["artifacts"])
                completion_state = str(data["state"])
            else:
                if (
                    args.through is not None
                    or args.approved_launch_plan_sha256 is not None
                    or args.resume_started_audits
                ):
                    raise CorpusCompletionValidationError(
                        "completion report does not accept run-only arguments"
                    )
                data = coordinator.report()
                completion_artifacts = (
                    {
                        "kind": "corpus_completion_report",
                        "path": str(coordinator.report_path),
                    },
                )
                completion_state = str(data["state"])
        except CorpusCompletionValidationError as error:
            return CommandResult(
                command="complete-corpus",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                errors=(str(error),),
            )
        except (CorpusCompletionExecutionError, OSError, sqlite3.Error) as error:
            return CommandResult(
                command="complete-corpus",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                errors=(str(error),),
            )
        return CommandResult(
            command="complete-corpus",
            ok=True,
            identifiers={"run": coordinator.manifest.run_id},
            current_state=completion_state,
            artifacts=completion_artifacts,
            data=data,
        )

    if args.command == "recover-cross-reference":
        try:
            repository_root = find_repository_root()
            origin_root = (
                args.origin_run_root
                if args.origin_run_root.is_absolute()
                else repository_root / args.origin_run_root
            ).resolve()
            destination_root = (
                args.destination_run_root
                if args.destination_run_root.is_absolute()
                else repository_root / args.destination_run_root
            ).resolve()
            recovery = CrossReferenceRecoveryCoordinator(
                repository_root=repository_root,
                manifest_path=args.manifest,
                origin_run_root=origin_root,
                destination_run_root=destination_root,
            )
            if args.recovery_action == "preflight":
                if args.approved_launch_plan_sha256 is not None:
                    raise CrossReferenceRecoveryValidationError(
                        "recovery preflight does not accept an approved launch-plan hash"
                    )
                recovery_result = recovery.preflight()
                data = recovery_result.to_dict()
            elif args.recovery_action == "run":
                if args.approved_launch_plan_sha256 is None:
                    raise CrossReferenceRecoveryValidationError(
                        "recovery run requires --approved-launch-plan-sha256"
                    )
                recovery_result = recovery.run(
                    approved_launch_plan_sha256=args.approved_launch_plan_sha256
                )
                data = recovery_result.to_dict()
            elif args.recovery_action == "status":
                if args.approved_launch_plan_sha256 is not None:
                    raise CrossReferenceRecoveryValidationError(
                        "recovery status does not accept an approved launch-plan hash"
                    )
                data = recovery.status()
            else:
                if args.approved_launch_plan_sha256 is not None:
                    raise CrossReferenceRecoveryValidationError(
                        "recovery report does not accept an approved launch-plan hash"
                    )
                data = recovery.report()
            recovery_state = str(data["state"])
        except CrossReferenceRecoveryValidationError as error:
            return CommandResult(
                command="recover-cross-reference",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                errors=(str(error),),
            )
        except (CrossReferenceRecoveryExecutionError, OSError) as error:
            return CommandResult(
                command="recover-cross-reference",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                errors=(str(error),),
            )
        artifacts = []
        for kind, path in (
            ("recovery_launch_plan", recovery.launch_plan_path),
            ("recovery_preflight_receipt", recovery.preflight_receipt_path),
            ("recovery_status", recovery.status_path),
            ("recovery_report", recovery.report_path),
            ("recovery_graph", recovery.graph_path),
            ("recovery_graph_manifest", recovery.graph_manifest_path),
        ):
            if path.is_file():
                artifacts.append({"kind": kind, "path": str(path)})
        return CommandResult(
            command="recover-cross-reference",
            ok=True,
            identifiers={"recovery": recovery.manifest.recovery_id},
            current_state=recovery_state,
            artifacts=tuple(artifacts),
            data=data,
        )

    if args.command == "salvage-recovery":
        try:
            repository_root = find_repository_root()
            origin_root = (
                args.origin_recovery_root
                if args.origin_recovery_root.is_absolute()
                else repository_root / args.origin_recovery_root
            ).resolve()
            destination_root = (
                args.destination_run_root
                if args.destination_run_root.is_absolute()
                else repository_root / args.destination_run_root
            ).resolve()
            salvage = DeterministicSalvageCoordinator(
                repository_root=repository_root,
                origin_recovery_root=origin_root,
                destination_run_root=destination_root,
                shard_id=args.shard_id,
            )
            if args.salvage_action == "run":
                salvage_result = salvage.run()
                data = salvage_result.to_dict()
                salvage_state = salvage_result.state
            else:
                data = salvage.report()
                salvage_state = str(data["state"])
        except DeterministicSalvageValidationError as error:
            return CommandResult(
                command="salvage-recovery",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                errors=(str(error),),
            )
        except OSError as error:
            return CommandResult(
                command="salvage-recovery",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                errors=(str(error),),
            )
        artifacts = [
            {"kind": kind, "path": str(path)}
            for kind, path in (
                ("salvage_report", salvage.report_path),
                ("salvage_graph", salvage.graph_path),
                ("salvage_graph_manifest", salvage.graph_manifest_path),
                ("salvage_admission", salvage.admission_path),
            )
            if path.is_file()
        ]
        return CommandResult(
            command="salvage-recovery",
            ok=True,
            identifiers={"salvage": salvage.salvage_id, "shard": salvage.shard_id},
            current_state=salvage_state,
            artifacts=tuple(artifacts),
            data=data,
        )

    state_path = args.state_path
    if args.command == "cross-reference" and args.calibration_root is not None:
        state_path = args.calibration_root.resolve() / "state.sqlite3"
    repository = StateRepository(state_path)
    try:
        repository.initialize()
    except (OSError, sqlite3.Error) as error:
        return CommandResult(
            command=args.command,
            ok=False,
            classification=ResultClassification.VALIDATION_FAILURE,
            errors=(str(error),),
        )

    if args.command == "status":
        if args.batch:
            batch = repository.cross_reference_batch_record(args.batch)
            return CommandResult(
                command="status",
                ok=batch is not None,
                classification=(
                    ResultClassification.SUCCESS
                    if batch is not None
                    else ResultClassification.UNRESOLVED_IDENTITY
                ),
                identifiers={"batch": args.batch},
                current_state=None if batch is None else str(batch["state"]),
                errors=() if batch is not None else ("batch is not registered",),
                data={
                    "batch": batch,
                    "jobs": repository.cross_reference_jobs_for_batch(args.batch),
                    "candidates": repository.cross_reference_candidates_for_batch(args.batch),
                    "relationships": repository.cross_reference_relationships_for_batch(args.batch),
                },
            )
        source_record = repository.source_record(args.source) if args.source else None
        state = None if source_record is None else str(source_record["state"])
        identifiers = {"source": args.source} if args.source else {}
        missing_source = args.source is not None and state is None
        return CommandResult(
            command="status",
            ok=not missing_source,
            classification=(
                ResultClassification.UNRESOLVED_IDENTITY
                if missing_source
                else ResultClassification.SUCCESS
            ),
            identifiers=identifiers,
            current_state=state,
            errors=("source is not registered",) if missing_source else (),
            data={
                "schema_versions": repository.schema_versions(),
                "source": source_record,
                "assets": repository.assets_for(args.source) if source_record is not None else (),
                "wip_record_count": (
                    len(repository.latest_wip_records(args.source))
                    if source_record is not None
                    else 0
                ),
                "coverage_count": (
                    len(repository.coverage_for(args.source)) if source_record is not None else 0
                ),
            },
        )

    if args.command == "register":
        try:
            source = resolve_source(args.source)
            source_record = source.to_record()
            outcome = repository.register_source(
                source.source_id,
                metadata={
                    "schema_version": source.schema_version,
                    "source_yaml_path": source_record["source_yaml_path"],
                    "source_yaml_sha256": source.source_yaml_sha256,
                    "metadata": dict(source.metadata),
                },
                assets=[asset.to_record() for asset in source.assets],
                receipt={
                    "kind": "source_registration",
                    "schema_version": source.schema_version,
                    "source_yaml_sha256": source.source_yaml_sha256,
                    "asset_sha256": [asset.sha256 for asset in source.assets],
                },
            )
        except SourceResolutionError as error:
            return CommandResult(
                command="register",
                ok=False,
                classification=ResultClassification.UNRESOLVED_IDENTITY,
                identifiers={"source": args.source},
                errors=(str(error),),
            )
        except (SourceValidationError, IdentityMismatch, OSError, sqlite3.Error) as error:
            return CommandResult(
                command="register",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"source": args.source},
                errors=(str(error),),
            )
        return CommandResult(
            command="register",
            ok=True,
            identifiers={"source": source.source_id},
            current_state=outcome.current_state,
            artifacts=tuple(asset.to_record() for asset in source.assets),
            data={"applied": outcome.applied, "transition_id": outcome.transition_id},
        )

    if args.command == "inspect":
        if args.candidate:
            candidate = repository.cross_reference_candidate_record(args.candidate)
            return CommandResult(
                command="inspect",
                ok=candidate is not None,
                classification=(
                    ResultClassification.SUCCESS
                    if candidate is not None
                    else ResultClassification.UNRESOLVED_IDENTITY
                ),
                identifiers={"candidate": args.candidate},
                current_state=None if candidate is None else str(candidate["state"]),
                errors=() if candidate is not None else ("candidate is not registered",),
                data={"candidate": candidate},
            )
        if args.relationship:
            relationship = repository.cross_reference_relationship_record(args.relationship)
            return CommandResult(
                command="inspect",
                ok=relationship is not None,
                classification=(
                    ResultClassification.SUCCESS
                    if relationship is not None
                    else ResultClassification.UNRESOLVED_IDENTITY
                ),
                identifiers={"relationship": args.relationship},
                current_state=None if relationship is None else str(relationship["state"]),
                errors=(() if relationship is not None else ("relationship is not registered",)),
                data={"relationship": relationship},
            )
        job = repository.job_record(args.job)
        cross_reference_job = repository.cross_reference_job_record(args.job)
        if job is None and cross_reference_job is not None:
            cross_reference_receipt = None
            receipt_path = cross_reference_job.get("receipt_path")
            if isinstance(receipt_path, str) and Path(receipt_path).is_file():
                cross_reference_receipt = read_receipt(Path(receipt_path))
            return CommandResult(
                command="inspect",
                ok=True,
                identifiers={
                    "job": args.job,
                    "batch": str(cross_reference_job["batch_id"]),
                },
                current_state=str(cross_reference_job["state"]),
                artifacts=(
                    ({"kind": "job_receipt", "path": receipt_path},) if receipt_path else ()
                ),
                data={"job": cross_reference_job, "receipt": cross_reference_receipt},
            )
        if job is None:
            return CommandResult(
                command="inspect",
                ok=False,
                classification=ResultClassification.UNRESOLVED_IDENTITY,
                identifiers={"job": args.job},
                errors=("job is not registered",),
            )
        receipt: dict[str, object] | None = None
        receipt_path = job.get("receipt_path")
        if isinstance(receipt_path, str) and Path(receipt_path).is_file():
            receipt = read_receipt(Path(receipt_path))
        return CommandResult(
            command="inspect",
            ok=True,
            identifiers={"job": args.job, "source": str(job["source_id"])},
            current_state=str(job["state"]),
            artifacts=(({"kind": "job_receipt", "path": receipt_path},) if receipt_path else ()),
            data={"job": job, "receipt": receipt},
        )

    if args.command == "cross-reference":
        try:
            source_groups = (
                tuple(
                    tuple(source.strip() for source in group.split(","))
                    for group in args.source_group
                )
                if args.source_group
                else None
            )
            root = find_repository_root()
            coordinator_arguments: dict[str, Any] = {
                "repository_root": root,
                "state_path": state_path,
                "allow_failed_proposal_revalidation": bool(args.revalidate_retained),
            }
            if args.calibration_root is not None:
                calibration_root = args.calibration_root.resolve()
                coordinator_arguments.update(
                    {
                        "cache_root": calibration_root / "cache",
                        "vault_root": calibration_root / "vault" / "sources",
                        "relationship_root": calibration_root / "vault" / "relationships",
                        "build_root": calibration_root / "build",
                        "eligible_source_states": frozenset({"calibration_graph_inserted"}),
                    }
                )
            xref_result = CrossReferenceCoordinator(**coordinator_arguments).run(
                tuple(args.source),
                through=args.through,
                model=args.model,
                source_groups=source_groups,
                revalidate_retained=bool(args.revalidate_retained),
            )
        except CrossReferenceExecutionError as error:
            return CommandResult(
                command="cross-reference",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                errors=(str(error),),
            )
        except (
            CrossReferenceValidationError,
            IdentityMismatch,
            MissingIdentity,
            OSError,
            sqlite3.Error,
        ) as error:
            return CommandResult(
                command="cross-reference",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                errors=(str(error),),
            )
        return CommandResult(
            command="cross-reference",
            ok=True,
            identifiers={"batch": xref_result.batch_id},
            current_state=xref_result.state,
            warnings=xref_result.warnings,
            data=xref_result.to_dict(),
        )

    if args.command == "run":
        reviewed_finding_ids = tuple(args.reviewed_finding)
        has_rationale = args.review_rationale is not None
        review_pair_invalid = (bool(reviewed_finding_ids) != has_rationale) or (
            has_rationale and not args.review_rationale.strip()
        )
        if review_pair_invalid or (
            (reviewed_finding_ids or has_rationale) and args.through != "audit_passed"
        ):
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"source": args.source},
                current_state=repository.source_state(args.source),
                errors=(
                    "--reviewed-finding and a non-empty --review-rationale must be "
                    "supplied together and only with --through audit_passed",
                ),
            )
        if reviewed_finding_ids and args.reader_mode != "staged":
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"source": args.source},
                current_state=repository.source_state(args.source),
                errors=("reviewed audit repair currently requires --reader-mode staged",),
            )
        if args.replace_artifacts and args.through != "graph_inserted":
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"source": args.source},
                current_state=repository.source_state(args.source),
                errors=("--replace-artifacts requires --through graph_inserted",),
            )
        try:
            root = find_repository_root()
            reading: ReadingCoordinator | MapFirstReadingCoordinator
            if args.reader_mode == MAP_FIRST_READER_MODE:
                reading = MapFirstReadingCoordinator(
                    repository_root=root,
                    state_path=args.state_path,
                )
            else:
                reading = ReadingCoordinator(
                    repository_root=root,
                    state_path=args.state_path,
                )
            result: Any
            current_state = repository.source_state(args.source)
            audit_repair_pending = (
                current_state == "reading"
                and args.through in {"audit_passed", "graph_inserted"}
                and bool(repository.findings_for(args.source, status="open"))
            )
            if args.through == "source_complete" or (
                current_state
                not in {
                    "source_complete",
                    "audit_failed",
                    "audit_passed",
                    "graph_inserted",
                }
                and not audit_repair_pending
            ):
                result = reading.run_through_source_complete(args.source, model=args.model)
            else:
                result = None
            if args.through in {"audit_passed", "graph_inserted"} and repository.source_state(
                args.source
            ) not in {"audit_passed", "graph_inserted"}:
                result = AuditCoordinator(
                    repository_root=root,
                    state_path=args.state_path,
                ).run_through_audit_passed(
                    args.source,
                    model=args.model,
                    reviewed_finding_ids=(
                        reviewed_finding_ids if args.through == "audit_passed" else ()
                    ),
                    review_rationale=(
                        args.review_rationale if args.through == "audit_passed" else None
                    ),
                    command_context={
                        "command": "run",
                        "source": args.source,
                        "through": args.through,
                        "model": args.model,
                        "reader_mode": args.reader_mode,
                        "reviewed_finding": list(reviewed_finding_ids),
                        "review_rationale": args.review_rationale,
                        "replace_artifacts": bool(args.replace_artifacts),
                        "json": bool(args.as_json),
                    },
                )
            if args.through == "graph_inserted":
                result = CompilationCoordinator(
                    repository_root=root,
                    state_path=args.state_path,
                ).run_through_graph_inserted(
                    args.source,
                    model=args.model,
                    replace_existing=bool(args.replace_artifacts),
                )
            if result is None:
                raise ReadingValidationError("pipeline did not produce a result")
        except SourceResolutionError as error:
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.UNRESOLVED_IDENTITY,
                identifiers={"source": args.source},
                errors=(str(error),),
            )
        except WorkerExecutionError as error:
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.WORKER_FAILURE,
                identifiers={"source": args.source},
                current_state=repository.source_state(args.source),
                errors=(str(error),),
            )
        except InvalidTransition as error:
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.INVALID_TRANSITION,
                identifiers={"source": args.source},
                current_state=repository.source_state(args.source),
                errors=(str(error),),
            )
        except MissingIdentity as error:
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.UNRESOLVED_IDENTITY,
                identifiers={"source": args.source},
                errors=(str(error),),
            )
        except (
            SourceValidationError,
            IdentityMismatch,
            ManualReviewRequired,
            ProposalValidationError,
            ReadingValidationError,
            AuditValidationError,
            CompilationError,
            DossierValidationError,
            VisualTransportError,
            OSError,
            sqlite3.Error,
        ) as error:
            return CommandResult(
                command="run",
                ok=False,
                classification=ResultClassification.VALIDATION_FAILURE,
                identifiers={"source": args.source},
                current_state=repository.source_state(args.source),
                errors=(str(error),),
            )
        return CommandResult(
            command="run",
            ok=True,
            identifiers={"source": result.source_id, "run": result.run_id},
            current_state=result.state,
            artifacts=tuple(getattr(result, "artifacts", ())),
            warnings=result.warnings,
            data=result.to_dict(),
        )

    state = repository.source_state(args.source) if args.source else None
    identifiers = {"source": args.source} if args.source else {}
    errors = ("source is not registered",) if args.source is not None and state is None else ()
    validation_data: dict[str, object] = {}
    if args.source is not None and not errors:
        try:
            source = resolve_source(args.source)
            for asset in source.assets:
                verify_asset(asset)
            if state in {
                "source_complete",
                "calibration_ready",
                "calibration_graph_inserted",
                "audit_failed",
                "audit_passed",
                "graph_inserted",
            }:
                coverage, dossier = validate_source_wip(
                    repository,
                    source,
                    schema_directory=find_repository_root() / "schemas" / "research-map" / "v1",
                )
                validation_data = {
                    "coverage": coverage.to_dict(),
                    "dossier": {
                        "ok": dossier.ok,
                        "errors": list(dossier.errors),
                        "type_gaps": list(dossier.type_gaps),
                    },
                }
        except (SourceResolutionError, SourceValidationError) as error:
            errors = (str(error),)
        except (ReadingValidationError, DossierValidationError, ProposalValidationError) as error:
            errors = (str(error),)
    return CommandResult(
        command="validate",
        ok=not errors,
        classification=(
            (
                ResultClassification.UNRESOLVED_IDENTITY
                if state is None
                else ResultClassification.VALIDATION_FAILURE
            )
            if errors
            else ResultClassification.SUCCESS
        ),
        identifiers=identifiers,
        current_state=state,
        errors=errors,
        data={
            "capabilities": {
                "python": True,
                "sqlite": True,
                "schema_version": repository.schema_versions()[-1],
            },
            **validation_data,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = dispatch(args)
    output = result.to_json() if args.as_json else _human_output(result)
    print(output)
    return result.exit_code


def _human_output(result: CommandResult) -> str:
    if not result.ok:
        return result.to_human()
    if result.command in {"build-public-release", "verify-public-release"}:
        lines = result.to_human().splitlines()
        lines.extend(
            (
                f"file_count: {result.data.get('file_count')}",
                f"tree_sha256: {result.data.get('tree_sha256')}",
            )
        )
        checks = result.data.get("checks")
        if isinstance(checks, list):
            lines.extend(f"check: {check}" for check in checks)
        return "\n".join(lines)
    if result.command not in {"search", "explore"}:
        return result.to_human()
    lines = result.to_human().splitlines()
    graph = result.data.get("graph")
    if isinstance(graph, dict):
        lines.extend(
            (
                f"graph_path: {graph.get('path')}",
                f"graph_sha256: {graph.get('sha256')}",
                f"manifest_verified: {str(graph.get('manifest_verified')).lower()}",
            )
        )
    if result.command == "search":
        lines.extend(
            (
                f"total_matches: {result.data.get('total_matches')}",
                f"returned_count: {result.data.get('returned_count')}",
                f"truncated: {str(result.data.get('truncated')).lower()}",
            )
        )
        values = result.data.get("results")
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                sources = ",".join(str(item) for item in value.get("source_ids", []))
                lines.append(
                    f"result: {value.get('object_id')} | {value.get('record_type')} | "
                    f"sources={sources} | score={value.get('score')}"
                )
                fields = value.get("matched_fields")
                if isinstance(fields, list):
                    names = [str(field.get("field")) for field in fields if isinstance(field, dict)]
                    lines.append(f"matched_fields: {','.join(names)}")
                evidence = value.get("evidence_ids")
                if isinstance(evidence, list):
                    lines.append("evidence_ids: " + ",".join(str(item) for item in evidence))
                lines.append(f"snippet: {value.get('snippet')}")
                quality = value.get("source_quality")
                if isinstance(quality, list):
                    lines.append(
                        "source_quality: "
                        + ",".join(
                            f"{item.get('source_id')}={item.get('quality_label')}"
                            for item in quality
                            if isinstance(item, dict)
                        )
                    )
        return "\n".join(lines)

    lines.extend(
        (
            f"object_type: {result.data.get('object_type')}",
            f"requested_id: {result.data.get('requested_id')}",
        )
    )
    selected = result.data.get("object")
    if isinstance(selected, dict):
        if result.data.get("object_type") == "node":
            lines.append(
                f"object: {selected.get('id')} | {selected.get('record_type')} | "
                f"source={selected.get('source_id')}"
            )
            payload = selected.get("payload")
            if isinstance(payload, dict):
                evidence_ids = payload.get("evidence_ids")
                if isinstance(evidence_ids, list):
                    lines.append(
                        "object_evidence_ids: " + ",".join(str(item) for item in evidence_ids)
                    )
        else:
            lines.extend(
                (
                    f"relationship_type: {selected.get('relation_type')}",
                    f"direction: {selected.get('direction')}",
                    f"comparison_surface: {selected.get('comparison_surface')}",
                    f"rationale: {selected.get('rationale')}",
                )
            )
            for side in ("left", "right"):
                evidence_ids = selected.get(f"{side}_evidence_ids")
                if isinstance(evidence_ids, list):
                    lines.append(
                        f"{side}_evidence_ids: " + ",".join(str(item) for item in evidence_ids)
                    )
    context = result.data.get("context")
    if isinstance(context, dict):
        _append_context_lines(lines, context, prefix="")
    endpoints = result.data.get("endpoints")
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            if isinstance(endpoint, dict):
                lines.append(
                    f"endpoint: {endpoint.get('id')} | {endpoint.get('record_type')} | "
                    f"source={endpoint.get('source_id')}"
                )
    endpoint_contexts = result.data.get("endpoint_contexts")
    if isinstance(endpoint_contexts, list):
        for index, endpoint_context in enumerate(endpoint_contexts, start=1):
            if isinstance(endpoint_context, dict):
                _append_context_lines(lines, endpoint_context, prefix=f"endpoint_{index}_")
    quality = result.data.get("source_quality")
    if isinstance(quality, list):
        for item in quality:
            if isinstance(item, dict):
                lines.append(
                    f"source_quality: {item.get('source_id')} | "
                    f"{item.get('quality_label')} | "
                    f"open_findings={item.get('open_finding_count')}"
                )
    return "\n".join(lines)


def _with_source_quality(explorer: GraphExplorer, data: dict[str, Any]) -> dict[str, Any]:
    manifest = explorer.manifest
    if manifest is None or "source_quality" not in manifest:
        return data
    raw = manifest.get("source_quality")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise GraphExplorationError("graph manifest source quality is invalid")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or explorer.namespace_root is None:
        raise GraphExplorationError("quality graph manifest inputs are invalid")
    quality_artifacts = [
        item
        for item in inputs
        if isinstance(item, dict) and item.get("kind") == "corpus_source_quality_manifest"
    ]
    if len(quality_artifacts) == 1:
        quality_path = _graph_input_path(explorer, quality_artifacts[0])
        try:
            quality_manifest = read_receipt(quality_path)
            validate_schema(
                quality_manifest,
                schema_directory=explorer.schema_directory,
                schema_name="corpus-source-quality-manifest.schema.json",
            )
        except (OSError, ValueError) as error:
            raise GraphExplorationError(
                f"quality graph source-quality manifest is invalid: {error}"
            ) from error
        if (
            manifest.get("quality_manifest_sha256") != quality_artifacts[0].get("sha256")
            or raw != quality_source_summaries(quality_manifest)
            or manifest.get("quality_label_counts") != quality_manifest.get("quality_label_counts")
            or manifest.get("selected_origin_counts")
            != quality_manifest.get("selected_origin_counts")
            or manifest.get("structurally_invalid_sources")
            != quality_manifest.get("structurally_invalid_sources")
        ):
            raise GraphExplorationError(
                "graph manifest quality context differs from its verified input"
            )
    elif not _recovery_quality_context_is_verified(explorer, raw, inputs):
        raise GraphExplorationError("quality graph must identify one source-quality manifest")
    quality = {str(item.get("source_id")): dict(item) for item in raw}
    if len(quality) != len(raw) or any(
        source_id not in explorer.graph["sources"] for source_id in quality
    ):
        raise GraphExplorationError("graph manifest source quality membership is invalid")
    enriched = dict(data)
    if "results" in enriched:
        results = enriched.get("results")
        if not isinstance(results, list):
            raise GraphExplorationError("search results are invalid")
        enriched["results"] = [
            {
                **dict(item),
                "source_quality": [
                    quality[source_id]
                    for source_id in item.get("source_ids", [])
                    if source_id in quality
                ],
            }
            for item in results
            if isinstance(item, dict)
        ]
        return enriched
    source_ids: set[str] = set()
    selected = enriched.get("object")
    if isinstance(selected, dict) and isinstance(selected.get("source_id"), str):
        source_ids.add(str(selected["source_id"]))
    endpoints = enriched.get("endpoints")
    if isinstance(endpoints, list):
        source_ids.update(
            str(item["source_id"])
            for item in endpoints
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        )
    enriched["source_quality"] = [quality[source_id] for source_id in sorted(source_ids)]
    return enriched


def _recovery_quality_context_is_verified(
    explorer: GraphExplorer,
    source_quality: list[dict[str, Any]],
    inputs: list[Any],
) -> bool:
    manifest = explorer.manifest
    if manifest is None:
        return False
    if manifest.get("kind") == "deterministic_recovery_salvage_graph_manifest":
        return _salvage_quality_context_is_verified(explorer, source_quality, inputs)
    if manifest.get("kind") != "cross_reference_recovery_graph_manifest":
        return False
    origin_manifests = [
        item
        for item in inputs
        if isinstance(item, dict)
        and item.get("kind") == "recovery_lineage"
        and Path(str(item.get("path", ""))).as_posix() == "lineage/origin-graph-manifest.json"
    ]
    if len(origin_manifests) != 1:
        return False
    try:
        origin_manifest = read_receipt(_graph_input_path(explorer, origin_manifests[0]))
    except (OSError, ValueError) as error:
        raise GraphExplorationError(
            f"recovery graph origin manifest is invalid: {error}"
        ) from error
    origin_inputs = origin_manifest.get("inputs")
    if not isinstance(origin_inputs, list):
        return False
    quality_artifacts = [
        item
        for item in origin_inputs
        if isinstance(item, dict) and item.get("kind") == "corpus_source_quality_manifest"
    ]
    origin_graph = origin_manifest.get("corpus_graph")
    return (
        len(quality_artifacts) == 1
        and isinstance(origin_graph, dict)
        and manifest.get("origin_graph_sha256") == origin_graph.get("sha256")
        and origin_manifest.get("quality_manifest_sha256") == quality_artifacts[0].get("sha256")
        and source_quality == origin_manifest.get("source_quality")
        and manifest.get("quality_label_counts") == origin_manifest.get("quality_label_counts")
        and manifest.get("selected_origin_counts") == origin_manifest.get("selected_origin_counts")
        and manifest.get("structurally_invalid_sources")
        == origin_manifest.get("structurally_invalid_sources")
    )


def _salvage_quality_context_is_verified(
    explorer: GraphExplorer,
    source_quality: list[dict[str, Any]],
    inputs: list[Any],
) -> bool:
    manifest = explorer.manifest
    if manifest is None:
        return False

    def lineage(name: str) -> dict[str, Any] | None:
        matches = [
            item
            for item in inputs
            if isinstance(item, dict)
            and item.get("kind") == "salvage_lineage"
            and Path(str(item.get("path", ""))).as_posix() == f"lineage/{name}"
        ]
        return matches[0] if len(matches) == 1 else None

    recovery_descriptor = lineage("origin-graph-manifest.json")
    quality_descriptor = lineage("origin-quality-graph-manifest.json")
    if recovery_descriptor is None or quality_descriptor is None:
        return False
    try:
        recovery_manifest = read_receipt(_graph_input_path(explorer, recovery_descriptor))
        quality_manifest = read_receipt(_graph_input_path(explorer, quality_descriptor))
    except (OSError, ValueError) as error:
        raise GraphExplorationError(
            f"salvage graph transitive origin manifest is invalid: {error}"
        ) from error
    recovery_graph = recovery_manifest.get("corpus_graph")
    quality_graph = quality_manifest.get("corpus_graph")
    quality_inputs = quality_manifest.get("inputs")
    if not isinstance(recovery_graph, dict) or not isinstance(quality_graph, dict):
        return False
    if not isinstance(quality_inputs, list):
        return False
    quality_artifacts = [
        item
        for item in quality_inputs
        if isinstance(item, dict) and item.get("kind") == "corpus_source_quality_manifest"
    ]
    shared_fields = (
        "source_quality",
        "quality_label_counts",
        "selected_origin_counts",
        "structurally_invalid_sources",
    )
    return (
        len(quality_artifacts) == 1
        and manifest.get("origin_graph_sha256") == recovery_graph.get("sha256")
        and recovery_manifest.get("origin_graph_sha256") == quality_graph.get("sha256")
        and quality_manifest.get("quality_manifest_sha256") == quality_artifacts[0].get("sha256")
        and source_quality == recovery_manifest.get("source_quality")
        and all(
            manifest.get(field) == recovery_manifest.get(field) == quality_manifest.get(field)
            for field in shared_fields
        )
    )


def _graph_input_path(explorer: GraphExplorer, artifact: dict[str, Any]) -> Path:
    if explorer.namespace_root is None:
        raise GraphExplorationError("graph manifest namespace is unavailable")
    recorded = Path(str(artifact.get("path", "")))
    path = (
        recorded.resolve()
        if recorded.is_absolute()
        else (explorer.namespace_root / recorded).resolve()
    )
    try:
        path.relative_to(explorer.namespace_root.resolve())
    except ValueError as error:
        raise GraphExplorationError(
            f"graph manifest artifact escapes its namespace: {recorded}"
        ) from error
    return path


def _append_context_lines(lines: list[str], context: dict[str, Any], *, prefix: str) -> None:
    lines.extend(
        (
            f"{prefix}total_incident_edges: {context.get('total_incident_edges')}",
            f"{prefix}returned_edge_count: {context.get('returned_edge_count')}",
            f"{prefix}omitted_edge_count: {context.get('omitted_edge_count')}",
            f"{prefix}truncated: {str(context.get('truncated')).lower()}",
        )
    )
    edges = context.get("edges")
    if isinstance(edges, list):
        for edge in edges:
            if isinstance(edge, dict):
                lines.append(
                    f"{prefix}edge: {edge.get('source')} -[{edge.get('kind')}]-> "
                    f"{edge.get('target')} | relationship={edge.get('relationship_id')}"
                )
    edge_groups = context.get("edge_groups")
    if isinstance(edge_groups, dict):
        for name, values in sorted(edge_groups.items()):
            if isinstance(values, list):
                lines.append(f"{prefix}edge_group_{name}: {len(values)}")
    evidence = context.get("evidence")
    if isinstance(evidence, list):
        for value in evidence:
            if isinstance(value, dict):
                lines.append(f"{prefix}evidence: {value.get('id')}")
    for key, label, identifier in (
        ("moves", "move", "id"),
        ("threads", "thread", "id"),
        ("cross_source_relationships", "relationship", "relationship_id"),
    ):
        values = context.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    lines.append(f"{prefix}{label}: {value.get(identifier)}")


if __name__ == "__main__":
    sys.exit(main())

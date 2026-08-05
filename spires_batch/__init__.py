"""Plan and operate SPIReS retrievals over many scientific work units."""

__version__ = "0.1.0"

from spires_batch.backends import DryRunBackend, SerialBackend
from spires_batch.discovery import (
    CurcPathDiscoveryAdapter,
    ExplicitFileDiscoveryAdapter,
    discover_inputs,
    parse_path_identity,
)
from spires_batch.models import (
    AlbedoScienceConfig,
    ClusteringScienceConfig,
    InvertScienceConfig,
    MetadataCheck,
    OperationalAdvanceRecord,
    OperationalRunRecord,
    ProductContents,
    R0BuildScienceConfig,
    R0Recipe,
    RequestConfig,
    ResolvedPlan,
    ReservationSet,
    ResourceProfile,
    SchedulerSubmissionRecord,
    SchedulerTestRecord,
    ScenePreparationConfig,
    Stage,
    SubmissionRecord,
    Task,
    TaskAttempt,
)
from spires_batch.planner import plan_request
from spires_batch.operational import (
    advance_operational_run,
    load_operational_advance,
    load_operational_run,
    start_operational_run,
    summarize_operational_run,
)
from spires_batch.reservations import (
    ReservationStore,
    WorkerReservationError,
    WorkerReservationGuard,
)
from spires_batch.scheduler import (
    load_scheduler_submission_record,
    load_scheduler_test_record,
    submit_scheduler_submission,
    test_scheduler_submission,
)
from spires_batch.science import ScientificExecutor, validate_scientific_outputs
from spires_batch.serialization import load_plan, load_request, write_plan
from spires_batch.submission import (
    acquire_submission_reservations,
    load_reservation_set,
    load_submission_record,
    prepare_submission,
    rollback_submission_reservations,
)

__all__ = [
    "__version__",
    "AlbedoScienceConfig",
    "ClusteringScienceConfig",
    "CurcPathDiscoveryAdapter",
    "DryRunBackend",
    "ExplicitFileDiscoveryAdapter",
    "MetadataCheck",
    "OperationalAdvanceRecord",
    "OperationalRunRecord",
    "ProductContents",
    "R0BuildScienceConfig",
    "R0Recipe",
    "RequestConfig",
    "ReservationStore",
    "ReservationSet",
    "WorkerReservationError",
    "WorkerReservationGuard",
    "ResolvedPlan",
    "ResourceProfile",
    "SchedulerSubmissionRecord",
    "SchedulerTestRecord",
    "ScenePreparationConfig",
    "ScientificExecutor",
    "SerialBackend",
    "Stage",
    "SubmissionRecord",
    "Task",
    "TaskAttempt",
    "InvertScienceConfig",
    "discover_inputs",
    "advance_operational_run",
    "acquire_submission_reservations",
    "load_plan",
    "load_operational_advance",
    "load_operational_run",
    "load_reservation_set",
    "load_request",
    "load_scheduler_submission_record",
    "load_scheduler_test_record",
    "load_submission_record",
    "parse_path_identity",
    "plan_request",
    "prepare_submission",
    "rollback_submission_reservations",
    "submit_scheduler_submission",
    "start_operational_run",
    "summarize_operational_run",
    "test_scheduler_submission",
    "validate_scientific_outputs",
    "write_plan",
]

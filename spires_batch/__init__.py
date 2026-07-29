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
    ProductContents,
    R0BuildScienceConfig,
    RequestConfig,
    ResolvedPlan,
    ResourceProfile,
    ScenePreparationConfig,
    Stage,
    Task,
    TaskAttempt,
)
from spires_batch.planner import plan_request
from spires_batch.reservations import ReservationStore
from spires_batch.science import ScientificExecutor, validate_scientific_outputs
from spires_batch.serialization import load_plan, load_request, write_plan

__all__ = [
    "__version__",
    "AlbedoScienceConfig",
    "ClusteringScienceConfig",
    "CurcPathDiscoveryAdapter",
    "DryRunBackend",
    "ExplicitFileDiscoveryAdapter",
    "MetadataCheck",
    "ProductContents",
    "R0BuildScienceConfig",
    "RequestConfig",
    "ReservationStore",
    "ResolvedPlan",
    "ResourceProfile",
    "ScenePreparationConfig",
    "ScientificExecutor",
    "SerialBackend",
    "Stage",
    "Task",
    "TaskAttempt",
    "InvertScienceConfig",
    "discover_inputs",
    "load_plan",
    "load_request",
    "parse_path_identity",
    "plan_request",
    "validate_scientific_outputs",
    "write_plan",
]

"""Canonical JSON, stable digests, and immutable artifact serialization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from spires_batch.models import RequestConfig, ResolvedPlan


ModelT = TypeVar("ModelT", bound=BaseModel)


def canonical_data(value: BaseModel | dict[str, Any] | list[Any]) -> Any:
    """Return JSON-compatible data with Pydantic normalization applied."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def canonical_json(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Serialize data deterministically for hashing and durable artifacts."""
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_digest(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    payload = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def load_json_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.suffix.lower() != ".json":
        raise ValueError(f"configuration and manifests must be JSON files: {source}")
    with source.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {source}")
    return value


def load_request(path: str | Path) -> RequestConfig:
    return RequestConfig.model_validate(load_json_object(path))


def load_plan(path: str | Path, *, verify_digest: bool = True) -> ResolvedPlan:
    plan = ResolvedPlan.model_validate(load_json_object(path))
    if verify_digest:
        from spires_batch.planner import deterministic_plan_payload

        actual = sha256_digest(deterministic_plan_payload(plan))
        if actual != plan.plan_digest:
            raise ValueError(
                f"resolved plan digest mismatch for {path}: stored {plan.plan_digest}, "
                f"calculated {actual}"
            )
        config_digest = sha256_digest(plan.request)
        if config_digest != plan.config_digest:
            raise ValueError(
                f"configuration digest mismatch for {path}: stored {plan.config_digest}, "
                f"calculated {config_digest}"
            )
    return plan


def write_immutable_json(path: str | Path, value: BaseModel | dict[str, Any]) -> Path:
    """Atomically create a JSON artifact without replacing an existing file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to replace immutable artifact {destination}"
            ) from exc
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_plan(path: str | Path, plan: ResolvedPlan) -> Path:
    return write_immutable_json(path, plan)

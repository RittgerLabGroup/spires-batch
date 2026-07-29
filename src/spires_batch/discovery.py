"""Explicit-file and configuration-driven CURC path discovery."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from spires_batch.models import (
    CheckLayer,
    CheckSeverity,
    DiscoveryRootConfig,
    InputFileConfig,
    InputRole,
    PreflightIssue,
    RequestConfig,
    ResolvedInput,
)


_REFLECTANCE_PATTERN = re.compile(
    r"^(?P<product>VNP09GA|VJ109GA|VJ209GA|MOD09GA|MYD09GA)"
    r"\.A(?P<year>\d{4})(?P<doy>\d{3})\."
    r"(?P<tile>h\d{2}v\d{2})\.",
    re.IGNORECASE,
)
_RAW_PATTERN = re.compile(
    r"^spires_(?P<product>vnp09ga|vj109ga|vj209ga|mod09ga|myd09ga)_"
    r"(?P<tile>h\d{2}v\d{2})_(?P<date>\d{8})_"
    r"(?P<content>raw|interpolate)\.nc$",
    re.IGNORECASE,
)
_R0_PATTERN = re.compile(
    r"^r0_(?P<start>\d{8})_(?P<end>\d{8})\.nc$",
    re.IGNORECASE,
)
_TILE_COMPONENT = re.compile(r"^h\d{2}v\d{2}$", re.IGNORECASE)


@dataclass(frozen=True)
class DiscoveryResult:
    inputs: tuple[ResolvedInput, ...]
    issues: tuple[PreflightIssue, ...]


class ExplicitFileDiscoveryAdapter:
    """Resolve user-supplied exact paths without site-policy imports."""

    name = "explicit"

    def resolve(
        self,
        configured_files: list[InputFileConfig],
        *,
        request: RequestConfig,
        base_dir: Path,
    ) -> tuple[list[ResolvedInput], list[PreflightIssue]]:
        resolved: list[ResolvedInput] = []
        issues: list[PreflightIssue] = []
        for configured in configured_files:
            if not _matches_selection(configured, request):
                continue
            item, item_issues = _resolved_input(
                configured,
                request=request,
                base_dir=base_dir,
            )
            issues.extend(item_issues)
            if item is not None:
                resolved.append(item)
        return resolved, issues


class CurcPathDiscoveryAdapter:
    """Expand configured CURC roots using public NASA filename conventions."""

    name = "curc"

    def expand(
        self,
        root: DiscoveryRootConfig,
        *,
        request: RequestConfig,
        base_dir: Path,
    ) -> tuple[list[InputFileConfig], list[PreflightIssue]]:
        root_path = _absolute(root.path, base_dir)
        if not root_path.is_dir():
            severity = CheckSeverity.ERROR if root.required else CheckSeverity.WARNING
            return [], [
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=severity,
                    code="missing_discovery_root",
                    message=f"discovery root does not exist as a directory: {root_path}",
                    path=root_path,
                )
            ]

        configured_files: list[InputFileConfig] = []
        matches = sorted(path for path in root_path.glob(root.pattern) if path.is_file())
        for path in matches:
            parsed = parse_path_identity(path)
            parsed_product = parsed.get("product")
            if parsed_product is not None and parsed_product != request.run.product:
                continue
            configured = InputFileConfig(
                role=root.role,
                path=path,
                name=root.name,
                tile=parsed.get("tile"),
                date=parsed.get("date"),
                water_year=parsed.get("water_year"),
                product=parsed_product,
                metadata=parsed,
            )
            if _matches_selection(configured, request):
                configured_files.append(configured)

        issues: list[PreflightIssue] = []
        if root.required and not configured_files:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="empty_discovery",
                    message=(
                        f"discovery root {root_path} with pattern {root.pattern!r} "
                        "resolved to no selected files"
                    ),
                    path=root_path,
                )
            )
        return configured_files, issues


def water_year(acquisition_date: date) -> int:
    return acquisition_date.year + (1 if acquisition_date.month >= 10 else 0)


def parse_path_identity(path: str | Path) -> dict[str, Any]:
    """Parse identity tokens from supported source and SPIReS filenames."""
    candidate = Path(path)
    name = candidate.name

    match = _REFLECTANCE_PATTERN.match(name)
    if match:
        year = int(match.group("year"))
        doy = int(match.group("doy"))
        acquisition_date = date(year, 1, 1) + timedelta(days=doy - 1)
        return {
            "product": match.group("product").lower(),
            "tile": match.group("tile").lower(),
            "date": acquisition_date,
            "water_year": water_year(acquisition_date),
        }

    match = _RAW_PATTERN.match(name)
    if match:
        acquisition_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
        return {
            "product": match.group("product").lower(),
            "tile": match.group("tile").lower(),
            "date": acquisition_date,
            "water_year": water_year(acquisition_date),
            "content": match.group("content").lower(),
        }

    match = _R0_PATTERN.match(name)
    if match:
        result: dict[str, Any] = {
            "start_date": datetime.strptime(match.group("start"), "%Y%m%d").date(),
            "end_date": datetime.strptime(match.group("end"), "%Y%m%d").date(),
        }
        for component in reversed(candidate.parts[:-1]):
            if _TILE_COMPONENT.fullmatch(component):
                result["tile"] = component.lower()
                break
        return result

    return {}


def _absolute(path: Path, base_dir: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve(strict=False)


def _staging_path(
    source: Path,
    *,
    role: InputRole,
    name: str | None,
    tile: str | None,
    product: str,
    request: RequestConfig,
    base_dir: Path,
) -> Path:
    staging = request.execution.staging
    if not staging.enabled or staging.root is None:
        return source
    root = _absolute(staging.root, base_dir)
    role_component = name or role.value
    tile_component = tile or "global"
    return (
        root
        / request.run.sensor
        / request.run.platform
        / product
        / role_component
        / tile_component
        / source.name
    )


def _resolved_input(
    configured: InputFileConfig,
    *,
    request: RequestConfig,
    base_dir: Path,
) -> tuple[ResolvedInput | None, list[PreflightIssue]]:
    issues: list[PreflightIssue] = []
    source = _absolute(configured.path, base_dir)
    parsed = parse_path_identity(source)

    configured_identity = {
        "tile": configured.tile,
        "date": configured.date,
        "water_year": configured.water_year,
        "product": configured.product,
    }
    for key, expected in configured_identity.items():
        found = parsed.get(key)
        if expected is not None and found is not None and expected != found:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="explicit_identity_mismatch",
                    message=(
                        f"explicit {configured.role.value} file declares {key}={expected!r}, "
                        f"but its filename resolves to {found!r}"
                    ),
                    path=source,
                )
            )

    tile = configured.tile or parsed.get("tile")
    acquisition_date = configured.date or parsed.get("date")
    item_water_year = (
        configured.water_year
        or parsed.get("water_year")
        or (water_year(acquisition_date) if acquisition_date else None)
    )
    product = configured.product or parsed.get("product")

    if configured.role in {InputRole.REFLECTANCE, InputRole.RAW}:
        missing = [
            key
            for key, value in {
                "tile": tile,
                "date": acquisition_date,
                "product": product,
            }.items()
            if value is None
        ]
        if missing:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="unresolved_input_identity",
                    message=(
                        f"{configured.role.value} input is missing {missing}; provide the "
                        "values explicitly or use a supported filename"
                    ),
                    path=source,
                )
            )

    if product is not None and product != request.run.product:
        issues.append(
            PreflightIssue(
                layer=CheckLayer.INVENTORY,
                severity=CheckSeverity.ERROR,
                code="product_mismatch",
                message=(
                    f"input product {product!r} does not match declared run product "
                    f"{request.run.product!r}"
                ),
                path=source,
            )
        )

    if not source.is_file():
        issues.append(
            PreflightIssue(
                layer=CheckLayer.INVENTORY,
                severity=CheckSeverity.ERROR,
                code="missing_input",
                message=f"resolved input does not exist as a file: {source}",
                path=source,
            )
        )
        return None, issues
    if not os.access(source, os.R_OK):
        issues.append(
            PreflightIssue(
                layer=CheckLayer.INVENTORY,
                severity=CheckSeverity.ERROR,
                code="unreadable_input",
                message=f"resolved input is not readable: {source}",
                path=source,
            )
        )
        return None, issues

    stat = source.stat()
    effective_product = product or request.run.product
    return (
        ResolvedInput(
            role=configured.role,
            source_path=source,
            execution_path=_staging_path(
                source,
                role=configured.role,
                name=configured.name,
                tile=tile,
                product=effective_product,
                request=request,
                base_dir=base_dir,
            ),
            name=configured.name,
            tile=tile,
            date=acquisition_date,
            water_year=item_water_year,
            product=effective_product,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            metadata={**parsed, **configured.metadata},
        ),
        issues,
    )


def _matches_selection(item: InputFileConfig, request: RequestConfig) -> bool:
    selection = request.selection
    if item.role in {
        InputRole.R0_SOURCE,
        InputRole.ANCILLARY,
        InputRole.LUT,
        InputRole.MASK,
    }:
        if item.tile is not None and selection.tiles and item.tile not in selection.tiles:
            return False
        return True
    if item.tile is not None and selection.tiles and item.tile not in selection.tiles:
        return False
    if item.date is not None and selection.dates and item.date not in selection.dates:
        return False
    item_wy = item.water_year or (water_year(item.date) if item.date else None)
    if item_wy is not None and selection.water_years and item_wy not in selection.water_years:
        return False
    return True


def discover_inputs(request: RequestConfig, *, base_dir: str | Path = ".") -> DiscoveryResult:
    """Resolve explicit files and configured roots into immutable input records."""
    base = Path(base_dir).resolve()
    configured_files = list(request.inputs.files)
    issues: list[PreflightIssue] = []

    adapters = {CurcPathDiscoveryAdapter.name: CurcPathDiscoveryAdapter()}
    for root in request.inputs.roots:
        adapter = adapters.get(root.adapter)
        if adapter is None:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="unknown_discovery_adapter",
                    message=(
                        f"unknown discovery adapter {root.adapter!r}; installed adapters "
                        f"are {sorted(adapters)}"
                    ),
                    path=_absolute(root.path, base),
                )
            )
            continue
        expanded, adapter_issues = adapter.expand(
            root,
            request=request,
            base_dir=base,
        )
        configured_files.extend(expanded)
        issues.extend(adapter_issues)

    resolved, explicit_issues = ExplicitFileDiscoveryAdapter().resolve(
        configured_files,
        request=request,
        base_dir=base,
    )
    issues.extend(explicit_issues)

    by_source: dict[Path, ResolvedInput] = {}
    for item in resolved:
        previous = by_source.get(item.source_path)
        if previous is not None:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="duplicate_input",
                    message=(
                        f"input path was resolved more than once as "
                        f"{previous.role.value!r} and {item.role.value!r}"
                    ),
                    path=item.source_path,
                )
            )
        else:
            by_source[item.source_path] = item

    by_stage_path: dict[Path, ResolvedInput] = {}
    for item in resolved:
        previous = by_stage_path.get(item.execution_path)
        if previous is not None and previous.source_path != item.source_path:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="staging_collision",
                    message=(
                        f"distinct inputs resolve to the same staging destination: "
                        f"{previous.source_path} and {item.source_path}"
                    ),
                    path=item.execution_path,
                )
            )
        else:
            by_stage_path[item.execution_path] = item

    return DiscoveryResult(
        inputs=tuple(
            sorted(
                resolved,
                key=lambda item: (
                    item.role.value,
                    item.tile or "",
                    item.date.isoformat() if item.date else "",
                    str(item.source_path),
                ),
            )
        ),
        issues=tuple(issues),
    )


def staging_key(source_path: Path) -> str:
    return hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()

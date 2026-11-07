"""Validate the draft source snapshot without installing or releasing anything."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib


BATCH_ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("runtime", "cloudmask", "lut")


def git_output(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def check_requirement(requirement: str, components: dict, source: str) -> None:
    name_match = re.match(r"([A-Za-z0-9_-]+)", requirement)
    if name_match is None:
        return
    name = name_match.group(1).lower().replace("_", "-")
    if not (name.startswith("spires-") or name == "viirs-cloudmask-batch"):
        return
    if name not in components:
        raise ValueError(f"{source}: first-party dependency {name} is not in the manifest")
    component = components[name]
    expected = f"git+{component['repository']}@{component['commit']}"
    actual = requirement.partition("@")[2].strip()
    if actual != expected:
        raise ValueError(f"{source}: {name} must reference {expected}, got {requirement!r}")


def validate_manifest(manifest: dict, workspace: Path) -> None:
    if manifest.get("schema_version") != 1 or manifest.get("product") != "spipy":
        raise ValueError("Expected a schema-version 1 spipy manifest")
    if manifest.get("status") != "draft" or manifest.get("target_version") != "1.0.0":
        raise ValueError("This helper only supports the unreleased spipy v1.0.0 draft")
    components = manifest["components"]
    expected_names = {
        "spires-contract", "spires-io", "spires-r0", "spires-inversion",
        "spires-postprocess", "spires-interpolation", "spires-batch",
        "viirs-cloudmask-batch", "spires-lut",
    }
    if set(components) != expected_names:
        raise ValueError("The draft must record all nine spipy components")
    for name, component in components.items():
        if component["repository"] != f"https://github.com/RittgerLabGroup/{name}.git":
            raise ValueError(f"{name}: expected the RittgerLabGroup repository URL")
        if not re.fullmatch(r"[0-9a-f]{40}", component["commit"]):
            raise ValueError(f"{name}: an exact 40-character Git commit is required")
        if not component["groups"] or not set(component["groups"]).issubset(GROUPS):
            raise ValueError(f"{name}: invalid installation groups")

    for name, component in components.items():
        repository = workspace / name
        revision = component["commit"]
        project = tomllib.loads(git_output(repository, "show", f"{revision}:pyproject.toml"))
        if project["project"]["name"] != name:
            raise ValueError(f"{name}: package metadata does not match the manifest")
        optional = project["project"].get("optional-dependencies", {})
        if not set(component.get("extras", [])).issubset(optional):
            raise ValueError(f"{name}: manifest requests an undefined package extra")
        requirements = list(project["project"].get("dependencies", []))
        for extra_requirements in optional.values():
            requirements.extend(extra_requirements)
        for requirement in requirements:
            check_requirement(requirement, components, f"{name}/pyproject.toml")
        for documentation_file in ("docs/requirements.txt", "doc/requirements.txt"):
            if git_output(repository, "ls-tree", "--name-only", revision, documentation_file).strip():
                for requirement in git_output(repository, "show", f"{revision}:{documentation_file}").splitlines():
                    check_requirement(requirement.strip(), components, f"{name}/{documentation_file}")
        print(f"{name}: {revision}")


def requirements_text(manifest: dict, group: str) -> str:
    lines = [
        "# Draft spipy v1.0.0 first-party sources; NOT an environment lock.",
        "# Generated from manifest.json by scripts/check_spipy_candidate.py.",
    ]
    for name, component in manifest["components"].items():
        if group not in component["groups"]:
            continue
        extras = component.get("extras", [])
        suffix = f"[{','.join(extras)}]" if extras else ""
        lines.append(f"{name}{suffix} @ git+{component['repository']}@{component['commit']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path,
        default=BATCH_ROOT / "releases/spipy-v1.0.0/manifest.json",
    )
    parser.add_argument("--workspace", type=Path, default=BATCH_ROOT.parent)
    parser.add_argument("--write-requirements", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        validate_manifest(manifest, args.workspace)
        for group in GROUPS:
            path = args.manifest.parent / f"sources-{group}.txt"
            expected = requirements_text(manifest, group)
            if args.write_requirements:
                path.write_text(expected)
            elif not path.exists() or path.read_text() != expected:
                raise ValueError(f"{path}: stale or missing; use --write-requirements")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Candidate validation failed: {error}\n")
    print("Draft source snapshot is consistent. No environment or release was changed.")


if __name__ == "__main__":
    main()

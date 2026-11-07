"""Validate the draft source snapshot without installing or releasing anything."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys
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


def validate_manifest_header(manifest: dict) -> None:
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


def validate_manifest(manifest: dict, workspace: Path) -> None:
    validate_manifest_header(manifest)
    components = manifest["components"]
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


def validate_installed(manifest: dict, group: str) -> None:
    """Check pip's installed Git provenance without requiring source checkouts."""
    validate_manifest_header(manifest)
    prefix = Path(sys.prefix).resolve()
    print(f"Interpreter: {sys.executable}")
    for name, component in manifest["components"].items():
        if group not in component["groups"]:
            continue
        distribution = metadata.distribution(name)
        if not Path(distribution.locate_file("")).resolve().is_relative_to(prefix):
            raise ValueError(f"{name}: distribution is outside the selected environment")
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        repository = component["repository"]
        accepted_urls = {
            repository,
            repository.replace("https://github.com/", "ssh://git@github.com/"),
            repository.replace("https://github.com/", "git@github.com:"),
        }
        vcs = direct.get("vcs_info", {})
        if (direct.get("url") not in accepted_urls
                or vcs.get("vcs") != "git"
                or vcs.get("commit_id") != component["commit"]
                or direct.get("dir_info", {}).get("editable")):
            raise ValueError(f"{name}: install the non-editable Git revision {component['commit']} from {repository}")
        print(f"{name}: {component['commit']} ({distribution.version})")
    if group == "cloudmask":
        science = sorted({d.metadata["Name"] for d in metadata.distributions()
                          if (d.metadata.get("Name") or "").lower().replace("_", "-").startswith("spires")})
        if science:
            raise ValueError(f"Cloud environment must not contain SPIReS packages: {science}")


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
    parser.add_argument("--installed-group", choices=GROUPS,
                        help="Verify this interpreter's installed Git revisions; no sibling repositories or Git commands required")
    parser.add_argument("--write-requirements", action="store_true")
    args = parser.parse_args()
    if args.installed_group and args.write_requirements:
        parser.error("--installed-group cannot write requirements")
    try:
        manifest = json.loads(args.manifest.read_text())
        if args.installed_group:
            validate_installed(manifest, args.installed_group)
            print("Installed source revisions match the candidate. No environment or release was changed.")
            return
        validate_manifest(manifest, args.workspace)
        for group in GROUPS:
            path = args.manifest.parent / f"sources-{group}.txt"
            expected = requirements_text(manifest, group)
            if args.write_requirements:
                path.write_text(expected)
            elif not path.exists() or path.read_text() != expected:
                raise ValueError(f"{path}: stale or missing; use --write-requirements")
    except (OSError, ValueError, KeyError, TypeError, metadata.PackageNotFoundError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Candidate validation failed: {error}\n")
    print("Draft source snapshot is consistent. No environment or release was changed.")


if __name__ == "__main__":
    main()

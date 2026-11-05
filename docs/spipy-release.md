# spipy v1.0.0 staging

Status: **draft, not released, not approved for operational deployment**.
The target operational version is `1.0.0`. Keep that target while adjusting
the candidate; ordinary commits and updated pins do not require a new release.
There is no new release tag, GitHub release, or PyPI publication.

## Ownership and scope

All nine components are owned by RittgerLabGroup:

| Component | Purpose |
| --- | --- |
| spires-contract | Shared schemas and boundary validation |
| spires-io | Reflectance and ancillary loading, persisted products |
| spires-r0 | In-memory summer background construction and reuse |
| spires-inversion | Spectral inversion |
| spires-postprocess | Albedo and sensor-specific canopy adjustment |
| spires-interpolation | Interpolation and NRT state |
| spires-batch | Submission, staging, orchestration, and release manifest |
| viirs-cloudmask-batch | Cloud-mask inference, with a separately managed runtime |
| spires-lut | LUT generation tooling, not required for processing existing LUTs |

GitHub has no nested repository namespace such as
`RittgerLabGroup/spipy/spires-io`. Repositories stay directly under the
organization. This document and the manifest group them without changing team
membership or repository access permissions.

The upstream `spires` convenience metapackage is not needed by this batch
runtime and is not included. Do not install it to assemble this candidate: its
dependencies are not this controlled source set. MATLAB production, ancillary
generation, and research repositories are also outside this Python release.

For forked repositories, local `origin` points to RittgerLabGroup and `upstream`
retains SPIReS-Organization. Fetching upstream does not authorize merging it.
Upstream tags remain unchanged. Package versions remain independent of the
operational spipy version, including the contract's v2-derived version.
The fork publishing workflows cannot publish to the upstream PyPI package
names, even if a GitHub release is subsequently created.

## Candidate source snapshot

`releases/spipy-v1.0.0/manifest.json` is the editable candidate record. Every
component has an exact Git commit, an owner-controlled URL, and an installation
group. The following source requirements are generated from that record:

- `sources-runtime.txt`: batch, contract, IO, R0, inversion, postprocess, and
  interpolation, including their relevant extras.
- `sources-cloudmask.txt`: the independently managed cloud-mask runtime source.
- `sources-lut.txt`: the LUT tooling and its matching contract source.

These files freeze **first-party source only**, not Python, native libraries,
third-party Python packages, model weights, or data assets. They are staging
inputs, not a claim that an environment is reproducible or production-ready.
Do not install them over the active operational environment yet.

The manifest records a batch **code commit**. A subsequent manifest-only commit
can record that code commit without a circular self-hash. Manifest and generated
requirements changes alone do not require advancing the batch code pin. Changes
to batch code, packaging, or its dependency pins do require advancing it.

In the `spipy14` environment, from the `spires-batch` checkout:

```bash
python scripts/check_spipy_candidate.py
```

This read-only check uses sibling Git repositories to inspect the recorded
commits, verifies their internal dependency pins and documentation dependencies,
and checks the generated source files. It does not install, fetch, change
checkouts, submit jobs, or test scientific results. Run it where Git access is
permitted. Python 3.11 or newer is required for this standalone staging helper.

## Making adjustments before release

1. Edit and validate the relevant RittgerLabGroup component. Do not reset tags
   or change its package version just to match the spipy target version.
2. Commit the component change. Update affected dependency URLs to that exact
   commit, including documentation requirements. Work in dependency order:
   contract, IO and other consumers, R0, then batch. All references to the same
   component must use the same URL and revision.
3. Commit the dependent changes. Update the manifest component commits and any
   validation notes or outstanding work. Keep `status` set to `draft` and
   `target_version` set to `1.0.0`.
4. Regenerate and verify the source lists:

   ```bash
   python scripts/check_spipy_candidate.py --write-requirements
   python scripts/check_spipy_candidate.py
   ```

5. Review and commit the manifest/source-list changes. Push component commits
   before pushing dependent pins and the manifest to RittgerLabGroup.

Never substitute `@main` to get around a pin mismatch. Do not use `--no-deps` to
hide an incompatible dependency resolution. Editable checkouts can be useful
for development, but a release deployment must use the recorded clean commits.

## Work required before release approval

- Resolve and validate complete third-party environments for retrieval,
  cloud-mask inference, and LUT tooling. Record Python, compiler/native-library
  requirements, platform, and exact package builds. The current `spipy14`
  environment is a development environment, not an approved lock.
- Validate LUT packaging and the native DISORT build independently; the new
  upstream layout has not been installed or certified here.
- Exercise the source-pinned stack in an isolated environment, including the
  existing boundary checks, a representative historical tile/date window, and
  NRT resume with scratch staging and the full-history transfer kill switch.
- Record required LUT, ancillary, and cloud-model identities and availability.
  Source ownership does not version those assets automatically.
- Resolve or explicitly disposition archived test failures and skipped coverage.
- Freeze the manifest and environment locks only after explicit release approval.
  Decide then how to label the operational release without confusing existing
  Git-derived Python package versions. Do not create a coordinated version tag
  in each package repository as part of staging.

There is no automatic release action in the staging helper. Promotion to a
release, installation into an operational environment, and changes to NRT
cron/Slurm configuration remain separate, explicitly approved steps.

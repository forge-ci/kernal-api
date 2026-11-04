# Intel macOS test lane

Executes the `x86_64-apple-darwin` test suite. The suite is **built on Linux**
and only **run on macOS**: a GitHub-hosted Linux runner cross-compiles the
archive through Soldr's own toolchain, then boots a real x86_64 macOS Recovery
guest under QEMU/KVM and replays the archive inside it.

Nothing here needs a `macos-*` runner, a baked guest image, an SSH secret, or a
published GHCR package. `.github/workflows/macos-x64-tests.yml` runs nightly and
on demand, and is advisory — never a required check, never a release gate.

## Pieces

| File | Runs on | Job |
|---|---|---|
| `build-archive.sh` | Linux runner | Cross-build the nextest archive |
| `stage-guest-share.sh` | Linux runner | Stage the archive + Mach-O `cargo-nextest` the guest fetches over HTTP |
| `recovery-guest.sh` | macOS guest | Fetch the archive, run it, write evidence to `/tmp/results` |
| `verify-guest-results.py` | Linux runner | Decide pass/fail and assert Intel coverage from the collected evidence |

The guest half is driven by [`zackees/docker-mac-x64`][action], pinned to a
commit in the workflow. It owns the KVM setup, the disk reclaim, the boot, the
HTTP share at `10.0.2.2:8000`, and the collection of `/tmp/results`.

## Why the guest binary is a downloaded Mach-O

The archive is produced on Linux by `soldr cargo nextest archive`, so the
`cargo-nextest` on the runner's `PATH` is a Linux binary and cannot run in the
guest. `stage-guest-share.sh` therefore reads the version from the same nextest
that built the archive and downloads nextest's published
`universal-apple-darwin` build for that exact version, verifying it against the
release's `.sha256` before it can reach the guest.

nextest owns its archive format, so the guest's nextest is matched to the
builder's rather than pinned to a constant here that could drift.

## Why Recovery, not a prebaked image

This lane previously pulled a hand-baked `dockur/macos` image from this
repository's own GHCR namespace. That image was never published — the
`macos-x64-bake.yml` workflow the old comment named has never existed in this
repository — and producing it required a 30–60 minute manual macOS install plus
a 128 GB volume published by hand. Every run since the workflow landed failed at
`guest.sh start` with `manifest unknown`; the scripts and the Dockerfile for
that approach have been removed rather than left to rot.

A Recovery guest has no toolchain of its own, so the archive must be
self-contained. It is: every fixture these tests read is `include_str!`/
`include_bytes!` and therefore already inside the test binaries.

## Why the guest writes a stub `Cargo.toml`

Replaying an archive makes nextest require a workspace root that contains a
`Cargo.toml` (`ReuseWithWorkspaceRemap`), and it exits **96** without one —
before running a single test. The guest has no source tree and does not need
one, because nothing that runs reads it, so `recovery-guest.sh` writes a
minimal empty-workspace manifest and points `--workspace-remap` at it. The stub
satisfies nextest's root check and leaves config discovery empty, which is what
the archive's own `cargo-metadata.json` already implies.

The four source-inspection tests that *would* read the tree are excluded, so
the stub never has to stand in for real source.

## What the guest does not run

- **`#[ignore]`d tests** are skipped by nextest by default. That covers every
  test needing a full macOS userland — the Tauri/WKWebView screenshot proofs,
  the threaded-worker containment proofs, and the archive fixtures — none of
  which belong in a Recovery partition.
- **Four source-inspection policy tests** are excluded by name in
  `recovery-guest.sh`: `daemon_frame_v1`, `daemon_identity`, `version_policy`,
  and `facade_policy`. They read this crate's own tree through
  `env!("CARGO_MANIFEST_DIR")`, a compile-time constant still pointing at the
  Linux builder's path, and `--workspace-remap` cannot rewrite a compile-time
  constant. They assert on source text that is identical on every host and
  ci.yml's Linux `test-run` lanes already run them. This mirrors the exclusion
  the aarch64 lane carries in `ci.yml`.
- **15 further tests**, excluded by name and grouped by cause in
  `recovery-guest.sh`. Each entry is named rather than pattern-matched, so the
  list stays reviewable, and each group carries the evidence observed in the
  guest. None of it is a blanket filter: a test that starts failing for a *new*
  reason is not on the list and will fail the lane.

| Group | N | Why it cannot pass here |
|---|---|---|
| `EXCLUDE_CAP_PRIMITIVES` | 12 | `cap-primitives` 4.0.3 panics converting a negative `st_rdev` — `u64::try_from(stat.st_rdev).unwrap()` at `metadata_ext.rs:171`. The `dev` field two lines above guards the same signedness; only `rdev` was missed. This guest reports negative device numbers; hosted Macs do not, and all 12 pass there. Upstream fix: bytecodealliance/cap-std#428 (we wait rather than `[patch]`, which would not reach our consumers). |
| `EXCLUDE_MACOS_RENAME` | 3 | The guest's filesystem returns `ENOTSUP` (45) for `renameatx_np(RENAME_EXCL)` (readiness marker) and `renamex_np(RENAME_SWAP)` (`install_directory`, which documents returning the filesystem's error when exchange is unsupported). Real APFS supports both, and all three pass on hosted Macs. |

The seven failures from scheduled run 41658083156 are exercised again. Seal
metadata and hardlink identity are checked under Recovery's root identity;
only DAC write-refusal assertions require a non-root owner. The ACL validator
receives an explicitly inherited entry (`chmod +ai`), independent of automatic
filesystem propagation. The real APE image is embedded in the test binary, so
archive replay needs no fixture checkout. Cancellation stress accepts either
cooperative cancellation or forced containment with a cancellation trigger,
and still requires every parent resource counter to return to zero (see #568).

**Every macOS portability finding this lane surfaced is resolved** (#283). The
TLS fixture validity, the `/var` → `/private/var` comparison, and the
invalid-UTF-8 name in `tree_hash` were fixed in the tests. The owner-bound
child, verified-control, and PTY-restore failures were real and were fixed in
#347. Everything still excluded is a property of this guest, not of macOS:
each group passes on hosted Intel and Apple Silicon runners with no exclusions
(exact-SHA full CI run
[36067778414](https://github.com/zackees/kernal-api/actions/runs/36067778414)).

## Coverage assertion

`verify-guest-results.py` fails the job unless the collected log proves the
guest ran **Intel** code. `src/snapshot/unwind.rs`'s `frame_pointer_tests`
module compiles only under `#[cfg(all(test, target_arch = "x86_64"))]`, so
naming those six tests is what distinguishes a real Intel run from a green job
that executed nothing. That failure mode — passing while verifying nothing — is
the one this lane was created to close.

## Local use

The Linux half runs anywhere:

```bash
./build-archive.sh          # ~85 s, produces kernal-x64.tar.zst
./stage-guest-share.sh      # downloads the pinned Mach-O cargo-nextest
```

The guest half needs `/dev/kvm`. On a host that has it, the workflow step is the
only supported entry point — `zackees/docker-mac-x64` is a GitHub Action and its
`run:` input is the guest's one script.

[action]: https://github.com/zackees/docker-mac-x64

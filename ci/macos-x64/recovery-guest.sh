# Run the prebuilt x86_64-apple-darwin nextest archive inside a macOS Recovery
# guest booted by the `zackees/docker-mac-x64` action on a Linux runner.
#
# Recovery gives exactly one boot and one script: there is no per-command
# execution channel, no Rust toolchain, no Xcode CLT and no Homebrew. Everything
# this script needs is fetched over the action's HTTP share at 10.0.2.2:8000.
#
# This script never exits non-zero. A guest boot costs minutes, so a failure
# must still produce collected evidence for the host to read -- the verdict is
# decided by ci/macos-x64/verify-guest-results.py from the files written under
# /tmp/results, not from this process's exit status.
#
# Shell target is Recovery's bash 3.2: no associative arrays, no ${v^^}, no
# `local -n`, and no comments inside the continued `nextest run` command below
# (a prior harness split a continued command on an embedded comment and
# executed an option as if it were a program).

set -u

SHARE=http://10.0.2.2:8000
WORK=/tmp/kernal-x64
COLLECT=/tmp/results
ARCHIVE=kernal-x64.tar.zst
NEXTEST_BIN=cargo-nextest

# Source-inspection policy tests read this crate's own tree through
# `env!("CARGO_MANIFEST_DIR")`, a compile-time constant still pointing at the
# Linux builder's path. `--workspace-remap` cannot rewrite a compile-time
# constant, and none of the guest's files live there. They assert on source
# text that is identical on every host, and ci.yml's Linux `test-run` lanes
# already run them. Mirrors the Linux `test-run` lanes in ci.yml.
POLICY_FILTER='not binary(source_policy)'

# ---------------------------------------------------------------------------
# Tests that cannot pass in this guest, grouped by cause.
#
# Every entry is excluded BY NAME so the list stays visible and reviewable, and
# each group records the evidence observed in the guest. None of this is a
# blanket filter: a test that starts failing for a NEW reason is not on this
# list and will fail the lane.
#
# The macOS portability findings this lane originally surfaced have been fixed
# in the tests themselves and are no longer excluded: the TLS fixture's 3650-day
# validity (now 820, under macOS's 825-day ceiling), the `/var` -> `/private/var`
# canonicalization comparison in context_file_observation, and the invalid-UTF-8
# file name in tree_hash, which APFS rejects with EILSEQ before the hash can see
# it. The remaining entries are all properties of this guest, not of macOS:
# each passes on hosted Intel and Apple Silicon runners with no exclusions
# (exact-SHA full CI run 36067778414, #283). They are the upstream
# cap-primitives panic on the guest's negative device numbers, one rename
# flag the guest's filesystem lacks, and two-core VM timing.
# ---------------------------------------------------------------------------

# cap-primitives 4.0.3 panics converting a negative macOS st_rdev:
# `rdev: u64::try_from(stat.st_rdev).unwrap()` at
# cap-primitives-4.0.3/src/rustix/fs/metadata_ext.rs:171, surfacing as
# `TryFromIntError(())`. The `dev` field two lines above guards the same
# signedness, so `dev_t` is known-signed here and only `rdev` was missed.
# APFS device numbers in this guest are negative; hosted Macs report
# non-negative ones and these tests pass there. The fix is upstream
# (bytecodealliance/cap-std#428); we wait for it rather than fork, because a
# [patch] would not reach kernal-api's own consumers anyway.
EXCLUDE_CAP_PRIMITIVES='commit_error_cleans_staging_after_destination_parent_is_renamed completed_symlink_is_not_published_or_followed missing_completed_output_preserves_destination_and_cleans_staging parent_discard_cleans_staging_after_destination_parent_is_renamed parent_discard_removes_worker_partial_and_completed_files replacement_failure_preserves_existing_directory_and_cleans_staging staged_output_is_invisible_until_parent_commit cancellation_after_parent_sync_preserves_output deadline_after_parent_sync_preserves_output_and_reports_cleanup_failure dispatcher_retry_releases_ownership_and_records_one_forced_reap output_cleanup_failure_reports_whether_publication_occurred parent_output_discards_on_failure_or_stop_and_commits_only_success'

# The guest's filesystem returns ENOTSUP (45) for the atomic rename flags:
# `tempfile`'s renameatx_np(RENAME_EXCL) in the readiness marker's no-clobber
# publish, and renamex_np(RENAME_SWAP) in install_directory, which documents
# returning the filesystem's error when exchange is unsupported. Real APFS
# supports both; all three tests pass on hosted Intel and Apple Silicon runners.
EXCLUDE_MACOS_RENAME='failed_marker_write_is_cleaned_up_and_existing_marker_is_preserved marker_is_invisible_until_payload_is_complete install_directory_replaces_an_existing_tree_and_removes_the_old_one'

# Recovery's Terminal runs as root, and root bypasses POSIX mode bits. The
# write seal is a mode-bit seal (owner-write cleared), so root can still open
# the sealed file for writing. Guest evidence (run 41658083156): even the
# legacy precondition `set_readonly(true); assert!(owner_write_refused)` fails
# at tests/fs/write_seal_facade.rs:99, which only a privileged writer can do.
# The same thing breaks inherited-ACL creation in this guest: the child came out
# ACL-free (`inherited ACL must be rejected: ()`), while the direct-grant ACL
# test still passes. All four pass on hosted macos-15-intel as a normal user
# (ci.yml run 41628848892, job 125920375402).
EXCLUDE_GUEST_ROOT='a_sealed_file_refuses_owner_writes_but_readonly_stays_false a_legacy_readonly_file_upgrades_to_the_seal the_seal_rides_the_file_record_across_hardlinks open_file_acl_rejects_inherited_grants'

# The APE fixture (tests/fixtures/ape-hello/hello.com, an LFS file) is resolved
# through CARGO_MANIFEST_DIR. That points at the stub workspace below, which
# holds no fixtures, so the guest sees ENOENT (`real APE image launched: Os {
# code: 2 ... }`, `assertion failed: ape::is_ape_file(&hello())`). Both tests
# pass on hosted macos-15-intel, which has the LFS checkout.
EXCLUDE_SOURCE_FIXTURE='the_real_fixture_is_an_ape_image command_routes_a_real_image_through_its_loader'

# Two-core VM timing: the cancel races the worker's forced containment, and the
# test sees `ForcedContainment { trigger: Cancelled }` where it expects
# `Stopped(Cancelled)` (tests/wasm_worker_containment.rs:182). It fails in every
# guest run and passes on hosted runners. Root cause is tracked in #568.
EXCLUDE_VM_TIMING='real_worker_sequential_stress_leaves_no_parent_state'

EXCLUDED_TESTS="$EXCLUDE_CAP_PRIMITIVES $EXCLUDE_MACOS_RENAME $EXCLUDE_GUEST_ROOT $EXCLUDE_SOURCE_FIXTURE $EXCLUDE_VM_TIMING"

GUEST_EXCLUDE=''
for name in $EXCLUDED_TESTS; do
  if [ -z "$GUEST_EXCLUDE" ]; then
    GUEST_EXCLUDE="test(~$name)"
  else
    GUEST_EXCLUDE="$GUEST_EXCLUDE or test(~$name)"
  fi
done

# --- end of exclusions ------------------------------------------------------

mkdir -p "$WORK" "$COLLECT"
cd "$WORK" || exit 0

echo "guest kernel: $(uname -a)"
echo "guest arch:   $(uname -m)"

fail() {
  echo "guest staging failed: $1" >&2
  echo "$1" > "$COLLECT/stage-failure.txt"
  exit 0
}

fetch() {
  curl -fsS -o "$WORK/$1" "$SHARE/$1" 2>"$COLLECT/fetch-$1.err"
}

fetch "$ARCHIVE" || fail "could not fetch $ARCHIVE from $SHARE"
fetch "$NEXTEST_BIN" || fail "could not fetch $NEXTEST_BIN from $SHARE"
fetch filter.txt || fail "could not fetch filter.txt from $SHARE"

chmod +x "$WORK/$NEXTEST_BIN"

# Record what actually ran, so the host can prove the guest Nextest matches the
# one that built the archive rather than assuming it.
"$WORK/$NEXTEST_BIN" nextest --version > "$COLLECT/nextest-version.txt" 2>&1 || true

# Replaying an archive makes nextest require a workspace root containing a
# Cargo.toml (`ReuseWithWorkspaceRemap`), and it exits 96 without one. The guest
# has no source tree and does not need one: every fixture these tests read is
# compiled into the binaries, and the four tests that do read the tree are
# excluded above. The stub satisfies the root check and leaves config discovery
# empty, which is what the archive's own cargo-metadata.json already implies.
# Written with printf rather than a heredoc because this script arrives as
# typed input, not as a file with a real shebang.
WORKSPACE="$WORK/workspace"
mkdir -p "$WORKSPACE"
printf '%s\n' '[workspace]' 'members = []' > "$WORKSPACE/Cargo.toml"

FILTER="$POLICY_FILTER and not ($GUEST_EXCLUDE)"
EXTRA_FILTER="$(cat "$WORK/filter.txt" 2>/dev/null || true)"
if [ -n "$EXTRA_FILTER" ]; then
  FILTER="($FILTER) and ($EXTRA_FILTER)"
fi
echo "$GUEST_EXCLUDE" > "$COLLECT/excluded-tests.txt"
echo "$FILTER" > "$COLLECT/filter.txt"

# --no-fail-fast: one guest boot is expensive, so a run must report every
# failure it can find rather than stopping at the first.
"$WORK/$NEXTEST_BIN" nextest run --archive-file "$WORK/$ARCHIVE" --workspace-remap "$WORKSPACE" --no-fail-fast -E "$FILTER" > "$COLLECT/nextest.log" 2>&1
echo $? > "$COLLECT/nextest.rc"

echo "--- nextest log tail ---"
tail -n 40 "$COLLECT/nextest.log" 2>/dev/null || true
echo "--- guest script complete ---"

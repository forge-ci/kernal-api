import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ci.prune_obsolete_cook_caches import (
    BUDGET_BYTES,
    Cache,
    GitHub,
    cache_version,
    checked_out_lock_hash,
    lock_generation_shape,
    over_budget_orphaned_lock_caches,
    prune,
    require_lock_generations_retired,
    require_single_current_generation,
    retired_cross_target_cook_bases,
    retired_old_dylint_outputs,
    stale_cook_bases,
    superseded_lock_generation_caches,
    version_tuple,
)


class CookCacheRetentionTests(unittest.TestCase):
    CURRENT_FIXTURE_LOCK_HASH = "5e524d4298978a25"

    def test_old_dylint_outputs_retire_only_over_budget_with_current_anchor(self):
        current = self.CURRENT_FIXTURE_LOCK_HASH
        prefix = "setup-soldr-dylint-output-v2-linux-x64-"
        now = datetime(2026, 10, 4, 15, tzinfo=timezone.utc)
        entries = [
            Cache(
                1,
                f"{prefix}aaaaaaaaaaaaaaaa-ffffffffffffffff",
                "refs/heads/main",
                6,
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:00Z",
            ),
            Cache(
                2,
                f"{prefix}bbbbbbbbbbbbbbbb-{current}",
                "refs/heads/main",
                6,
                "2026-09-02T00:00:00Z",
                "2026-09-02T00:00:00Z",
            ),
        ]
        self.assertEqual(
            retired_old_dylint_outputs(entries, current, set(), budget=12, now=now), []
        )
        self.assertEqual(
            retired_old_dylint_outputs(entries, current, set(), budget=10, now=now),
            [entries[0]],
        )
        self.assertEqual(
            retired_old_dylint_outputs(entries[:1], current, set(), budget=1, now=now),
            [],
        )

    def test_dylint_retention_protects_unknown_young_pr_and_current_entries(self):
        current = self.CURRENT_FIXTURE_LOCK_HASH
        prefix = "setup-soldr-dylint-output-v2-linux-x64-aaaaaaaaaaaaaaaa-"
        now = datetime(2026, 10, 4, 15, tzinfo=timezone.utc)
        entries = [
            Cache(1, prefix + current, "refs/heads/main", 6),
            Cache(
                2,
                prefix + "ffffffffffffffff",
                "refs/pull/1/merge",
                6,
                created_at="2026-09-01T00:00:00Z",
            ),
            Cache(
                3,
                prefix + "ffffffffffffffff",
                "refs/heads/main",
                6,
                created_at="2026-10-04T14:55:00Z",
            ),
            Cache(
                4,
                prefix + "ffffffffffffffff",
                "refs/heads/main",
                6,
                created_at="2026-10-05T00:00:00Z",
            ),
            Cache(
                5,
                prefix + "ffffffffffffffff",
                "refs/heads/main",
                6,
                created_at="invalid",
            ),
            Cache(
                6,
                prefix + "ffffffffffffffff",
                "refs/heads/main",
                6,
                created_at="2026-09-01T00:00:00",
            ),
            Cache(
                7,
                prefix.removesuffix("aaaaaaaaaaaaaaaa-") + "ffffffffffffffff",
                "refs/heads/main",
                6,
                created_at="2026-09-01T00:00:00Z",
            ),
        ]
        self.assertEqual(
            retired_old_dylint_outputs(entries, current, set(), budget=1, now=now), []
        )

    def test_dylint_retention_accounts_for_exclusions_and_stops_in_lru_order(self):
        current = self.CURRENT_FIXTURE_LOCK_HASH
        prefix = "setup-soldr-dylint-output-v2-linux-arm64-aaaaaaaaaaaaaaaa-"
        now = datetime(2026, 10, 4, 15, tzinfo=timezone.utc)
        entries = [
            Cache(1, prefix + current, "refs/heads/main", 6),
            Cache(
                2,
                prefix + "ffffffffffffffff",
                "refs/heads/main",
                6,
                "2026-09-02T00:00:00Z",
                "2026-09-01T00:00:00Z",
            ),
            Cache(
                3,
                prefix + "eeeeeeeeeeeeeeee",
                "refs/heads/main",
                6,
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:00Z",
            ),
        ]
        self.assertEqual(
            retired_old_dylint_outputs(entries, current, set(), budget=12, now=now),
            [entries[2]],
        )
        self.assertEqual(
            retired_old_dylint_outputs(entries, current, {2}, budget=12, now=now), []
        )
        self.assertEqual(
            retired_old_dylint_outputs(entries, current, {1}, budget=1, now=now), []
        )
        empty_anchor = [Cache(1, prefix + current, "refs/heads/main", 0), entries[1]]
        self.assertEqual(
            retired_old_dylint_outputs(empty_anchor, current, set(), budget=1, now=now),
            [],
        )

    def test_dylint_retention_requires_current_anchor_for_same_architecture(self):
        prefix = "setup-soldr-dylint-output-v2-linux-"
        entries = [
            Cache(
                1,
                prefix + "arm64-aaaaaaaaaaaaaaaa-" + self.CURRENT_FIXTURE_LOCK_HASH,
                "refs/heads/main",
                6,
            ),
            Cache(
                2,
                prefix + "x64-bbbbbbbbbbbbbbbb-ffffffffffffffff",
                "refs/heads/main",
                6,
                created_at="2026-09-01T00:00:00Z",
            ),
        ]
        self.assertEqual(
            retired_old_dylint_outputs(
                entries,
                self.CURRENT_FIXTURE_LOCK_HASH,
                set(),
                budget=1,
                now=datetime(2026, 10, 4, 15, tzinfo=timezone.utc),
            ),
            [],
        )

    def test_lock_generation_shape_removes_only_the_lock_dimension(self):
        cook_old = Cache(
            1,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
            "refs/heads/main",
            100,
        )
        cook_new = Cache(
            2,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
            "refs/heads/main",
            110,
        )
        self.assertEqual(
            lock_generation_shape(cook_old)[:2],
            lock_generation_shape(cook_new)[:2],
        )

        build_old = Cache(
            3,
            "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-f594fff1c7c78900",
            "refs/heads/main",
            100,
        )
        build_new = Cache(
            4,
            "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-5e524d4298978a25",
            "refs/heads/main",
            110,
        )
        self.assertEqual(
            lock_generation_shape(build_old)[:2], lock_generation_shape(build_new)[:2]
        )

        registry_old = Cache(
            5,
            "setup-soldr-cargoregistry-v1-linux-x64-f594fff1c7c78900-1bd06206b2843d4e",
            "refs/heads/main",
            100,
        )
        registry_new = Cache(
            6,
            "setup-soldr-cargoregistry-v1-linux-x64-5e524d4298978a25-1bd06206b2843d4e",
            "refs/heads/main",
            110,
        )
        self.assertEqual(
            lock_generation_shape(registry_old)[:2],
            lock_generation_shape(registry_new)[:2],
        )

    def test_only_older_main_generations_with_exact_replacements_are_retired(self):
        main_native_old = Cache(
            10,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
            "refs/heads/main",
            100,
        )
        main_native_current = Cache(
            20,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
            "refs/heads/main",
            110,
        )
        main_build_old = Cache(
            11,
            "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-f594fff1c7c78900",
            "refs/heads/main",
            100,
        )
        main_build_current = Cache(
            21,
            "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-5e524d4298978a25",
            "refs/heads/main",
            110,
        )
        main_registry_old = Cache(
            12,
            "setup-soldr-cargoregistry-v1-linux-x64-f594fff1c7c78900-1bd06206b2843d4e",
            "refs/heads/main",
            100,
        )
        main_registry_current = Cache(
            22,
            "setup-soldr-cargoregistry-v1-linux-x64-5e524d4298978a25-1bd06206b2843d4e",
            "refs/heads/main",
            110,
        )

        # A separate target/job shape, an unmatched registry digest, and a PR
        # generation are all preserved even though their lock hashes differ.
        unique_target_old = Cache(
            13,
            "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-build-aarch64-unknown-linux-gnu-f594fff1c7c78900",
            "refs/heads/main",
            120,
        )
        unique_registry = Cache(
            14,
            "setup-soldr-cargoregistry-v1-linux-x64-f594fff1c7c78900-cc1ceb1e7de990c6",
            "refs/heads/main",
            130,
        )
        pr_old = Cache(
            15,
            main_native_old.key,
            "refs/pull/361/merge",
            140,
        )

        self.assertEqual(
            superseded_lock_generation_caches(
                [
                    main_native_old,
                    main_native_current,
                    main_build_old,
                    main_build_current,
                    main_registry_old,
                    main_registry_current,
                    unique_target_old,
                    unique_registry,
                    pr_old,
                ],
                self.CURRENT_FIXTURE_LOCK_HASH,
            ),
            [main_native_old, main_build_old, main_registry_old],
        )

    def test_prior_generation_is_kept_until_same_shape_replacement_exists(self):
        old = Cache(
            1,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
            "refs/heads/main",
            100,
        )
        self.assertEqual(
            superseded_lock_generation_caches([old], self.CURRENT_FIXTURE_LOCK_HASH),
            [],
        )

        current = Cache(
            2,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
            "refs/heads/main",
            110,
        )
        self.assertEqual(
            superseded_lock_generation_caches(
                [old, current], self.CURRENT_FIXTURE_LOCK_HASH
            ),
            [old],
        )

    def test_multiple_lock_changes_keep_only_the_latest_same_shape_entry(self):
        entries = [
            Cache(
                10,
                "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-f594fff1c7c78900",
                "refs/heads/main",
                100,
            ),
            Cache(
                20,
                "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-5e524d4298978a25",
                "refs/heads/main",
                110,
            ),
            Cache(
                30,
                "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-a1b2c3d4e5f60718",
                "refs/heads/main",
                120,
            ),
        ]

        # Once a current replacement exists, retaining rollback-specific old
        # lock caches is a bandwidth tradeoff, not correctness state. If a
        # commit reverts Cargo.lock, the exact old cache can be rebuilt.
        self.assertEqual(
            superseded_lock_generation_caches(entries, "a1b2c3d4e5f60718"),
            entries[:2],
        )

    def test_empty_latest_entry_is_not_accepted_as_a_replacement(self):
        entries = [
            Cache(
                1,
                "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
                "refs/heads/main",
                100,
            ),
            Cache(
                2,
                "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
                "refs/heads/main",
                0,
            ),
        ]

        self.assertEqual(
            superseded_lock_generation_caches(entries, self.CURRENT_FIXTURE_LOCK_HASH),
            [],
        )
        with self.assertRaisesRegex(ValueError, "no nonempty checked-out"):
            require_lock_generations_retired(entries, self.CURRENT_FIXTURE_LOCK_HASH)

    def test_checked_out_lock_hash_matches_setup_soldr_short_file_hash(self):
        lockfile = Path(__file__).resolve().parents[1] / "Cargo.lock"
        expected = hashlib.sha256(lockfile.read_bytes()).hexdigest()[:16]
        self.assertEqual(checked_out_lock_hash(), expected)

    def test_stale_producer_with_newer_cache_id_does_not_replace_current_lock(self):
        current = Cache(
            20,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
            "refs/heads/main",
            110,
        )
        stale_late_write = Cache(
            30,
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
            "refs/heads/main",
            100,
        )

        self.assertEqual(
            superseded_lock_generation_caches(
                [current, stale_late_write], self.CURRENT_FIXTURE_LOCK_HASH
            ),
            [stale_late_write],
        )
        require_lock_generations_retired([current], self.CURRENT_FIXTURE_LOCK_HASH)

    def test_only_current_main_build_matrix_cross_cook_bases_are_retired(self):
        targets = (
            "aarch64-unknown-linux-gnu",
            "x86_64-apple-darwin",
            "aarch64-apple-darwin",
            "x86_64-pc-windows-msvc",
            "aarch64-pc-windows-msvc",
        )
        caches = [
            Cache(
                index,
                f"cook-base-v2-linux-x64-rustc1.95-fnone-lf594-soldrv0.9.23-xbuild-{target}",
                "refs/heads/main",
                index,
            )
            for index, target in enumerate(targets, start=1)
        ]
        native = Cache(
            10,
            "cook-base-v2-linux-x64-rustc1.95-fnone-lf594-soldrv0.9.23-xlinux",
            "refs/heads/main",
            10,
        )
        unrelated = Cache(
            11,
            "cook-base-v2-linux-x64-rustc1.95-fnone-lf594-soldrv0.9.23-xbuild-riscv64gc-unknown-linux-gnu",
            "refs/heads/main",
            11,
        )
        old = Cache(
            12,
            "cook-base-v2-linux-x64-rustc1.95-fnone-lf594-soldrv0.9.22-xbuild-x86_64-apple-darwin",
            "refs/heads/main",
            12,
        )
        pr = Cache(
            13,
            caches[0].key,
            "refs/pull/99/merge",
            13,
        )

        self.assertEqual(
            retired_cross_target_cook_bases(
                [*caches, native, unrelated, old, pr], "0.9.23"
            ),
            caches,
        )

    def test_prune_preserves_native_current_cook_before_any_deletes(self):
        class FakeGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1,
                        "cook-base-v2-linux-x64-rustc-fnone-soldrv0.9.23-xbuild-x86_64-apple-darwin",
                        "refs/heads/main",
                        900,
                    )
                ]
                self.deletions = []

            def caches(self):
                return list(self.entries)

            def delete_cache(self, cache_id):
                self.deletions.append(cache_id)
                return True

        api = FakeGitHub()
        with self.assertRaisesRegex(ValueError, "no retained native xlinux cook base"):
            prune(api, "0.9.23", apply=True)
        self.assertEqual(api.deletions, [])

    def test_only_old_main_cook_bases_are_prunable(self):
        caches = [
            Cache(1, "cook-base-v2-linux-x64-soldrv0.9.22", "refs/heads/main", 3),
            Cache(2, "cook-base-v2-linux-x64-soldrv0.9.23", "refs/heads/main", 5),
            Cache(3, "cook-base-v2-linux-arm64-soldrv0.9.22", "refs/pull/99/merge", 7),
            Cache(4, "solo-toolchain-v3-linux-x64-soldrv0.9.22", "refs/heads/main", 11),
            Cache(5, "cook-base-v2-linux-x64-soldrv0.9.24", "refs/heads/main", 13),
        ]

        self.assertEqual(stale_cook_bases(caches, "0.9.23"), [caches[0]])

    def test_current_generation_guard_rejects_mixed_versions(self):
        caches = [
            Cache(1, "cook-base-v2-linux-x64-soldrv0.9.23", "refs/heads/main", 3),
            Cache(2, "cook-base-v2-linux-arm64-soldrv0.9.22", "refs/heads/main", 7),
        ]

        with self.assertRaisesRegex(ValueError, "expected only 0.9.23"):
            require_single_current_generation(caches, "0.9.23")

    def test_current_generation_guard_preserves_distinct_shapes(self):
        caches = [
            Cache(1, "cook-base-v2-linux-x64-soldrv0.9.23", "refs/heads/main", 3),
            Cache(2, "cook-base-v2-linux-arm64-soldrv0.9.23", "refs/heads/main", 7),
        ]

        require_single_current_generation(caches, "0.9.23")

    def test_unparseable_old_cache_is_never_pruned(self):
        cache = Cache(1, "cook-base-v2-linux-x64", "refs/heads/main", 3)

        with self.assertRaisesRegex(ValueError, "cannot safely classify"):
            stale_cook_bases([cache], "0.9.23")

    def test_prune_refuses_to_delete_old_generation_without_current_replacement(self):
        class FakeGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1, "cook-base-v2-linux-x64-soldrv0.9.22", "refs/heads/main", 900
                    )
                ]
                self.deletions = []

            def caches(self):
                return list(self.entries)

            def delete_cache(self, cache_id):
                self.deletions.append(cache_id)
                self.entries = [
                    entry for entry in self.entries if entry.cache_id != cache_id
                ]
                return True

        api = FakeGitHub()
        with self.assertRaisesRegex(ValueError, "no main cook-base exists"):
            prune(api, "0.9.23", apply=True)
        self.assertEqual(api.deletions, [])
        self.assertEqual(api.entries[0].cache_id, 1)

    def test_versions_and_budget_use_gib_units(self):
        self.assertEqual(version_tuple("v0.9.23"), (0, 9, 23))
        self.assertEqual(
            cache_version("cook-base-v2-linux-x64-soldrv0.9.23-xlinux"), "0.9.23"
        )
        self.assertEqual(BUDGET_BYTES, 10_200_547_328)

    def test_prune_deletes_only_obsolete_and_disabled_main_cook_bases(self):
        class FakeGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1, "cook-base-v2-linux-x64-soldrv0.9.22", "refs/heads/main", 900
                    ),
                    Cache(
                        2,
                        "cook-base-v2-linux-x64-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        100,
                    ),
                    Cache(
                        3,
                        "cook-base-v2-linux-x64-soldrv0.9.23-xbuild-x86_64-pc-windows-msvc",
                        "refs/heads/main",
                        200,
                    ),
                    Cache(
                        4,
                        "cook-base-v2-linux-x64-soldrv0.9.22",
                        "refs/pull/99/merge",
                        300,
                    ),
                    Cache(
                        5,
                        "solo-toolchain-v3-linux-x64-soldrv0.9.22",
                        "refs/heads/main",
                        400,
                    ),
                ]

            def caches(self):
                return list(self.entries)

            def delete_cache(self, cache_id):
                self.entries = [
                    entry for entry in self.entries if entry.cache_id != cache_id
                ]
                return True

            def usage_bytes(self):
                return sum(entry.size for entry in self.entries)

        api = FakeGitHub()
        deleted, reclaimed = prune(
            api,
            "0.9.23",
            apply=True,
            current_lock_hash=self.CURRENT_FIXTURE_LOCK_HASH,
        )

        self.assertEqual((deleted, reclaimed), (2, 1_100))
        self.assertEqual([cache.cache_id for cache in api.entries], [2, 4, 5])

    def test_prune_retires_only_lock_generations_with_same_shape_replacement(self):
        class FakeGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1,
                        "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        100,
                    ),
                    Cache(
                        2,
                        "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        110,
                    ),
                    Cache(
                        3,
                        "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-f594fff1c7c78900",
                        "refs/heads/main",
                        120,
                    ),
                    Cache(
                        4,
                        "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-5e524d4298978a25",
                        "refs/heads/main",
                        130,
                    ),
                    Cache(
                        5,
                        "setup-soldr-cargoregistry-v1-linux-x64-f594fff1c7c78900-cc1ceb1e7de990c6",
                        "refs/heads/main",
                        140,
                    ),
                    Cache(
                        6,
                        "setup-soldr-cargoregistry-v1-linux-x64-5e524d4298978a25-1bd06206b2843d4e",
                        "refs/heads/main",
                        150,
                    ),
                    Cache(
                        7,
                        "setup-soldr-cargoregistry-v1-linux-x64-f594fff1c7c78900-23c34377df2e227d",
                        "refs/heads/main",
                        160,
                    ),
                    Cache(
                        8,
                        "cook-base-v2-linux-arm64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        170,
                    ),
                    Cache(
                        9,
                        "cook-base-v2-linux-arm64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        180,
                    ),
                    Cache(
                        10,
                        "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
                        "refs/pull/361/merge",
                        190,
                    ),
                ]
                self.deleted = []

            def caches(self):
                return list(self.entries)

            def delete_cache(self, cache_id):
                self.deleted.append(cache_id)
                self.entries = [
                    entry for entry in self.entries if entry.cache_id != cache_id
                ]
                return True

            def usage_bytes(self):
                return sum(entry.size for entry in self.entries)

        api = FakeGitHub()
        deleted, reclaimed = prune(
            api,
            "0.9.23",
            apply=True,
            current_lock_hash=self.CURRENT_FIXTURE_LOCK_HASH,
        )

        self.assertEqual(api.deleted, [1, 3, 8])
        self.assertEqual((deleted, reclaimed), (3, 390))
        self.assertEqual(
            {entry.cache_id for entry in api.entries}, {2, 4, 5, 6, 7, 9, 10}
        )

    def test_prune_keeps_unique_generation_when_no_same_shape_replacement_exists(self):
        class FakeGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1,
                        "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        100,
                    ),
                    Cache(
                        2,
                        "setup-soldr-buildcache-v2-linux-x64-1bd06206b2843d4e-linux-f594fff1c7c78900",
                        "refs/heads/main",
                        120,
                    ),
                ]
                self.deleted = []

            def caches(self):
                return list(self.entries)

            def delete_cache(self, cache_id):
                self.deleted.append(cache_id)
                return True

            def usage_bytes(self):
                return sum(entry.size for entry in self.entries)

        api = FakeGitHub()
        self.assertEqual(
            prune(
                api,
                "0.9.23",
                apply=True,
                current_lock_hash=self.CURRENT_FIXTURE_LOCK_HASH,
            ),
            (0, 0),
        )
        self.assertEqual(api.deleted, [])

    def test_concurrent_delete_404_is_idempotent_and_inventory_is_rechecked(self):
        from unittest.mock import patch
        from urllib.error import HTTPError

        api = GitHub("zackees/kernal-api", "test-token")
        missing = HTTPError(
            "https://api.github.com/cache/1", 404, "Not Found", None, None
        )
        with patch("ci.prune_obsolete_cook_caches.urlopen", side_effect=missing):
            self.assertFalse(api.delete_cache(1))

        class ConcurrentGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1, "cook-base-v2-linux-x64-soldrv0.9.22", "refs/heads/main", 900
                    ),
                    Cache(
                        2,
                        "cook-base-v2-linux-x64-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        10,
                    ),
                ]
                self.list_count = 0

            def caches(self):
                self.list_count += 1
                return list(self.entries)

            def delete_cache(self, cache_id):
                # Another cleanup already deleted the candidate after our snapshot.
                self.entries = [
                    entry for entry in self.entries if entry.cache_id != cache_id
                ]
                return False

            def usage_bytes(self):
                return sum(entry.size for entry in self.entries)

        concurrent = ConcurrentGitHub()
        with patch("ci.prune_obsolete_cook_caches.time.sleep"):
            deleted, reclaimed = prune(concurrent, "0.9.23", apply=True)
        self.assertEqual((deleted, reclaimed), (1, 900))
        self.assertGreaterEqual(concurrent.list_count, 2)
        self.assertEqual([cache.cache_id for cache in concurrent.entries], [2])

    def test_live_budget_uses_larger_of_usage_and_list(self):
        class FakeGitHub:
            def __init__(self):
                self.entries = [
                    Cache(
                        1,
                        "cook-base-v2-linux-x64-soldrv0.9.23-xlinux",
                        "refs/heads/main",
                        10,
                    ),
                ]

            def caches(self):
                return self.entries

            def usage_bytes(self):
                return BUDGET_BYTES + 1

        # The usage endpoint is authoritative when it is higher than the list.
        from unittest.mock import patch

        with patch("ci.prune_obsolete_cook_caches.time.sleep"):
            from ci.prune_obsolete_cook_caches import settled_usage

            usage, endpoint, listed = settled_usage(
                FakeGitHub(),
                "0.9.23",
                "5e524d4298978a25",
                polls=1,
                interval=0,
            )
        self.assertEqual(
            (usage, endpoint, listed), (BUDGET_BYTES + 1, BUDGET_BYTES + 1, 10)
        )

    def test_live_budget_fails_when_either_source_exceeds_cap(self):
        from unittest.mock import patch

        for endpoint, listed in (
            (BUDGET_BYTES + 1, 10),
            (10, BUDGET_BYTES + 1),
        ):
            with self.subTest(endpoint=endpoint, listed=listed):

                class FakeGitHub:
                    def __init__(self, current_endpoint, listed_bytes):
                        self.current_endpoint = current_endpoint
                        self.listed_bytes = listed_bytes

                    def caches(self):
                        return [
                            Cache(
                                1,
                                "cook-base-v2-linux-x64-soldrv0.9.23-xlinux",
                                "refs/heads/main",
                                self.listed_bytes,
                            )
                        ]

                    def usage_bytes(self):
                        return self.current_endpoint

                with (
                    patch("ci.prune_obsolete_cook_caches.time.sleep"),
                    self.assertRaisesRegex(RuntimeError, "exceeds"),
                ):
                    prune(FakeGitHub(endpoint, listed), "0.9.23", apply=True)

    def test_live_budget_retries_until_deleted_key_disappears_from_listing(self):
        class FakeGitHub:
            def __init__(self):
                self.read_count = 0
                self.current = Cache(
                    2,
                    "cook-base-v2-linux-x64-soldrv0.9.23-xlinux",
                    "refs/heads/main",
                    10,
                )
                self.stale = Cache(
                    1, "cook-base-v2-linux-x64-soldrv0.9.22", "refs/heads/main", 900
                )

            def caches(self):
                self.read_count += 1
                if self.read_count == 1:
                    return [self.current, self.stale]
                if self.read_count == 2:
                    return [self.current, self.stale]  # deletion/list lag
                return [self.current]

            def delete_cache(self, cache_id):
                self.asserted_id = cache_id

            def usage_bytes(self):
                return 910 if self.read_count < 3 else 10

        from unittest.mock import patch

        api = FakeGitHub()
        with patch("ci.prune_obsolete_cook_caches.time.sleep"):
            deleted, reclaimed = prune(api, "0.9.23", apply=True)
        self.assertEqual((deleted, reclaimed, api.asserted_id), (1, 900, 1))

    def test_lock_generation_cleanup_waits_for_listing_and_usage_convergence(self):
        from unittest.mock import patch

        class LaggingGitHub:
            def __init__(self):
                self.read_count = 0
                self.usage_reads = 0
                self.old = Cache(
                    1,
                    "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-lf594fff1c7c78900-soldrv0.9.23-xlinux",
                    "refs/heads/main",
                    BUDGET_BYTES,
                )
                self.current = Cache(
                    2,
                    "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l5e524d4298978a25-soldrv0.9.23-xlinux",
                    "refs/heads/main",
                    10,
                )
                self.deleted = []

            def caches(self):
                self.read_count += 1
                if self.read_count < 4:
                    return [self.old, self.current]  # cache-list deletion lag
                return [self.current]

            def delete_cache(self, cache_id):
                self.deleted.append(cache_id)
                return True

            def usage_bytes(self):
                self.usage_reads += 1
                if self.usage_reads < 3:
                    return BUDGET_BYTES + 10  # usage endpoint deletion lag
                return 10

        api = LaggingGitHub()
        with patch("ci.prune_obsolete_cook_caches.time.sleep"):
            deleted, reclaimed = prune(api, "0.9.23", apply=True)

        self.assertEqual((deleted, reclaimed), (1, BUDGET_BYTES))
        self.assertEqual(api.deleted, [1])
        self.assertEqual((api.read_count, api.usage_reads), (4, 3))

    def test_live_budget_retries_until_disabled_cross_cook_disappears(self):
        class LaggingGitHub:
            def __init__(self):
                self.read_count = 0
                self.native = Cache(
                    2,
                    "cook-base-v2-linux-x64-soldrv0.9.23-xlinux",
                    "refs/heads/main",
                    10,
                )
                self.retired = Cache(
                    1,
                    "cook-base-v2-linux-x64-soldrv0.9.23-xbuild-x86_64-pc-windows-msvc",
                    "refs/heads/main",
                    900,
                )

            def caches(self):
                self.read_count += 1
                if self.read_count < 3:
                    return [self.native, self.retired]  # delete/list convergence lag
                return [self.native]

            def delete_cache(self, cache_id):
                self.asserted_id = cache_id
                return True

            def usage_bytes(self):
                return 10

        from unittest.mock import patch

        api = LaggingGitHub()
        with patch("ci.prune_obsolete_cook_caches.time.sleep"):
            deleted, reclaimed = prune(api, "0.9.23", apply=True)
        self.assertEqual((deleted, reclaimed, api.asserted_id), (1, 900, 1))
        self.assertEqual(api.read_count, 3)

    def test_cleanup_job_runs_after_all_producers_and_uses_their_soldr_version(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        ).read_text()

        guards = (Path(__file__).resolve().parents[1] / "ci/linux_steps.py").read_text()
        self.assertIn("ci/linux_steps.py ci-guards", workflow)
        self.assertIn('"ci.test_prune_obsolete_cook_caches"', guards)
        self.assertIn(
            "soldr_version: ${{ steps.setup_soldr.outputs.soldr-version }}", workflow
        )
        self.assertIn("needs: [linux, build, test, dylints, full-coverage]", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn('--version "${SOLDR_VERSION}" --apply', workflow)

    def test_orphaned_lock_generations_retire_only_over_budget_lru_first(self):
        """Over budget, retire what GitHub's own LRU eviction would, never current."""
        current = self.CURRENT_FIXTURE_LOCK_HASH
        old = "f594fff1c7c78900"
        prefix = "setup-soldr-buildcache-v2-linux-x64-"
        caches = [
            Cache(
                1,
                f"{prefix}aa-build-x86_64-apple-darwin-{old}",
                "refs/heads/main",
                4,
                "2026-09-28T15:03",
            ),
            Cache(
                2,
                f"{prefix}bb-build-aarch64-apple-darwin-{old}",
                "refs/heads/main",
                4,
                "2026-09-27T04:56",
            ),
            Cache(
                3,
                f"{prefix}cc-linux-{current}",
                "refs/heads/main",
                4,
                "2026-09-01T00:00",
            ),
            Cache(
                4,
                f"{prefix}dd-build-x-{old}",
                "refs/pull/9/merge",
                4,
                "2026-09-01T00:00",
            ),
            Cache(
                5,
                "setup-soldr-dylint-output-v2-linux-x64-75fcd49156e5685e",
                "refs/heads/main",
                4,
                "2026-09-01T00:00",
            ),
        ]
        self.assertEqual(
            over_budget_orphaned_lock_caches(caches, current, set(), budget=20), []
        )
        self.assertEqual(
            [
                c.cache_id
                for c in over_budget_orphaned_lock_caches(
                    caches, current, set(), budget=16
                )
            ],
            [2],
        )
        self.assertEqual(
            [
                c.cache_id
                for c in over_budget_orphaned_lock_caches(
                    caches, current, set(), budget=1
                )
            ],
            [2, 1],
        )
        # Entries already chosen by another rule count as reclaimed.
        self.assertEqual(
            over_budget_orphaned_lock_caches(caches, current, {2}, budget=16), []
        )


if __name__ == "__main__":
    unittest.main()

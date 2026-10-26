"""Retire superseded main-branch cache generations.

Cook-base keys include the Soldr version and use exact-only restore. Once all
main-branch producers use a newer Soldr release, older-version cook bases are
unreachable and only consume the repository Actions cache quota. Other cache
families, refs, active current-version shapes, and future-version keys are
never deleted. The five exact cross-target shapes in `ci.yml` are retired after
their producer opts out of cook; the native `xlinux` cook remains a required
current-generation sentinel. For lock-scoped families, only an older cache is
retired when a newer main cache exists for the identical non-lock shape. This
keeps one reusable generation per producer shape without deleting unique
platform, target, feature, or job caches. When still over budget, understood
old-lock Dylint outputs may be evicted in LRU order if a nonempty current-lock
output of the same architecture remains. This budget-pressure eviction can
retire a unique old compiler/manifest identity; it does not assert an equivalent
replacement for that identity. Current-lock outputs, unknown keys, PR refs, and outputs with
unknown creation time or younger than ten minutes are preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GIB = 1024**3
BUDGET_BYTES = 19 * GIB // 2  # 9.5 GiB leaves room for ordinary cache growth.
MAIN_REF = "refs/heads/main"
COOK_BASE_PREFIX = "cook-base-v2-"
BUILD_CACHE_PREFIX = "setup-soldr-buildcache-v2-"
CARGO_REGISTRY_PREFIX = "setup-soldr-cargoregistry-v"
VERSION_RE = re.compile(r"(?:^|-)soldrv(?P<version>\d+\.\d+\.\d+)(?:-|$)")
LOCK_HASH_RE = re.compile(r"[0-9a-f]{16}")
WORKSPACE_LOCKFILE = Path(__file__).resolve().parents[1] / "Cargo.lock"
DISABLED_CROSS_TARGETS = frozenset(
    {
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
    }
)


@dataclass(frozen=True)
class Cache:
    cache_id: int
    key: str
    ref: str
    size: int
    last_accessed: str = ""
    created_at: str = ""


def lock_generation_shape(cache: Cache) -> tuple[str, str, str] | None:
    """Return (family, non-lock shape, lock hash) for known immutable cache keys."""
    key = cache.key
    if key.startswith(COOK_BASE_PREFIX):
        match = re.fullmatch(
            r"(?P<shape>cook-base-v2-.+-f[^-]+)-l(?P<lock>[0-9a-f]{16})(?P<tail>-soldrv.+)",
            key,
        )
        if match is None:
            return None
        return (
            "cook-base",
            match.group("shape") + match.group("tail"),
            match.group("lock"),
        )

    if key.startswith(BUILD_CACHE_PREFIX):
        shape, separator, lock_hash = key.rpartition("-")
        if separator and LOCK_HASH_RE.fullmatch(lock_hash):
            return "buildcache", shape, lock_hash
        return None

    if key.startswith(CARGO_REGISTRY_PREFIX):
        # Registry keys have the stable format/runner prefix, then the lock
        # hash, an optional validation namespace, and a final content digest.
        # Retain the entire suffix after the lock so distinct namespaces and
        # cache-content shapes never collapse together.
        parts = key.split("-")
        if len(parts) < 8 or parts[:3] != ["setup", "soldr", "cargoregistry"]:
            return None
        if not re.fullmatch(r"v[12]", parts[3]) or not LOCK_HASH_RE.fullmatch(parts[-1]):
            return None
        lock_index = next(
            (index for index in range(6, len(parts) - 1) if LOCK_HASH_RE.fullmatch(parts[index])),
            None,
        )
        if lock_index is None:
            return None
        lock_hash = parts[lock_index]
        shape_parts = parts[:lock_index] + parts[lock_index + 1 :]
        return "cargo-registry", "-".join(shape_parts), lock_hash

    return None


def checked_out_lock_hash() -> str:
    """Return setup-soldr's short SHA-256 hash for this checkout's Cargo.lock."""
    try:
        contents = WORKSPACE_LOCKFILE.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read workspace Cargo.lock: {exc}") from exc
    return hashlib.sha256(contents).hexdigest()[:16]


def superseded_lock_generation_caches(caches: list[Cache], current_lock_hash: str) -> list[Cache]:
    """Retire non-current generations only when this checkout's exact shape exists.

    Cache IDs do not identify the current lock generation: a delayed producer
    for an older checkout can write later and receive a greater ID. The
    checked-out Cargo.lock hash is the authority; only a nonempty main entry
    with that hash makes other generations of the same shape replaceable.
    """
    groups: dict[tuple[str, str, str], list[tuple[str, Cache]]] = {}
    for cache in caches:
        if cache.ref != MAIN_REF:
            continue
        parsed = lock_generation_shape(cache)
        if parsed is None:
            continue
        family, shape, lock_hash = parsed
        groups.setdefault((cache.ref, family, shape), []).append((lock_hash, cache))

    candidates = []
    for entries in groups.values():
        current = [cache for lock_hash, cache in entries if lock_hash == current_lock_hash and cache.size > 0]
        if not current:
            continue
        candidates.extend(cache for lock_hash, cache in entries if lock_hash != current_lock_hash)
    return sorted(candidates, key=lambda cache: cache.cache_id)


def over_budget_orphaned_lock_caches(
    caches: list[Cache],
    current_lock_hash: str,
    excluded: set[int],
    budget: int = BUDGET_BYTES,
) -> list[Cache]:
    """Retire non-current lock generations with no replacement, only over budget.

    A shape whose producer has not run since `Cargo.lock` changed -- a
    full-mode cross target, say -- keeps its last generation, which a restore
    can still use as a prefix fallback. That is worth its space until the
    repository is over budget. Past that point GitHub itself evicts the least
    recently used entries, and those orphans are exactly what it would pick,
    so retire them here in the same order: deterministically, and never a
    current-lock entry.
    """
    usage = sum(cache.size for cache in caches if cache.cache_id not in excluded)
    if usage <= budget:
        return []
    orphans = []
    for cache in caches:
        if cache.ref != MAIN_REF or cache.cache_id in excluded:
            continue
        parsed = lock_generation_shape(cache)
        if parsed is not None and parsed[2] != current_lock_hash:
            orphans.append(cache)
    retired = []
    for cache in sorted(orphans, key=lambda c: (c.last_accessed, c.cache_id)):
        if usage <= budget:
            break
        retired.append(cache)
        usage -= cache.size
    return retired


def retired_old_dylint_outputs(
    caches: list[Cache],
    current_lock_hash: str,
    excluded: set[int],
    budget: int = BUDGET_BYTES,
    now: datetime | None = None,
) -> list[Cache]:
    """Reclaim old-lock outputs over budget, preserving a current usable anchor."""
    pattern = re.compile(
        r"setup-soldr-dylint-output-v2-linux-(?P<arch>x64|arm64)-"
        r"[0-9a-f]{16}-(?P<lock>[0-9a-f]{16})"
    )
    remaining = [cache for cache in caches if cache.cache_id not in excluded]
    usage = sum(cache.size for cache in remaining)
    if usage <= budget:
        return []
    anchored_arches = {
        match.group("arch")
        for cache in remaining
        if cache.ref == MAIN_REF
        and cache.size > 0
        and (match := pattern.fullmatch(cache.key)) is not None
        and match.group("lock") == current_lock_hash
    }
    if not anchored_arches:
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=10)
    eligible: list[Cache] = []
    for cache in remaining:
        match = pattern.fullmatch(cache.key)
        if cache.ref != MAIN_REF or match is None:
            continue
        if match.group("lock") == current_lock_hash or match.group("arch") not in anchored_arches:
            continue
        try:
            created = datetime.fromisoformat(cache.created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None or created > cutoff:
            continue
        eligible.append(cache)
    retired: list[Cache] = []
    for cache in sorted(eligible, key=lambda item: (item.last_accessed, item.cache_id)):
        if usage <= budget:
            break
        retired.append(cache)
        usage -= cache.size
    return retired


def require_lock_generations_retired(caches: list[Cache], current_lock_hash: str) -> None:
    groups: dict[tuple[str, str, str], list[tuple[str, Cache]]] = {}
    for cache in caches:
        if cache.ref != MAIN_REF:
            continue
        parsed = lock_generation_shape(cache)
        if parsed is None:
            continue
        family, shape, lock_hash = parsed
        groups.setdefault((cache.ref, family, shape), []).append((lock_hash, cache))

    for entries in groups.values():
        current_rows = [cache for lock_hash, cache in entries if lock_hash == current_lock_hash]
        stale = [cache for lock_hash, cache in entries if lock_hash != current_lock_hash]
        if not stale or not current_rows:
            continue
        current = [cache for cache in current_rows if cache.size > 0]
        if not current:
            raise ValueError(
                "superseded lock-scoped caches have no nonempty checked-out "
                f"Cargo.lock replacement (expected {current_lock_hash}): "
                + ", ".join(f"{cache.cache_id}:{cache.key}" for cache in stale)
            )
        details = ", ".join(f"{cache.cache_id}:{cache.key}" for cache in stale)
        raise ValueError(f"superseded lock-scoped cache generations remain: {details}")


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if match is None:
        raise ValueError(f"invalid Soldr version: {value!r}")
    return tuple(int(part) for part in match.groups())


def cache_version(key: str) -> str | None:
    match = VERSION_RE.search(key)
    return match.group("version") if match else None


def stale_cook_bases(caches: list[Cache], current_version: str) -> list[Cache]:
    current = version_tuple(current_version)
    candidates = []
    for cache in caches:
        if cache.ref != MAIN_REF or not cache.key.startswith(COOK_BASE_PREFIX):
            continue
        found = cache_version(cache.key)
        if found is None:
            raise ValueError(f"cannot safely classify cook-base cache {cache.cache_id}: {cache.key}")
        version = version_tuple(found)
        if version < current:
            candidates.append(cache)
    return candidates


def retired_cross_target_cook_bases(caches: list[Cache], current_version: str) -> list[Cache]:
    """Select only current-version main cook shapes whose producer opted out."""
    current = version_tuple(current_version)
    candidates = []
    for cache in caches:
        if cache.ref != MAIN_REF or not cache.key.startswith(COOK_BASE_PREFIX):
            continue
        found = cache_version(cache.key)
        if found is None:
            raise ValueError(f"cannot safely classify cook-base cache {cache.cache_id}: {cache.key}")
        if version_tuple(found) != current:
            continue
        if any(cache.key.endswith(f"-xbuild-{target}") for target in DISABLED_CROSS_TARGETS):
            candidates.append(cache)
    return candidates


def require_current_generation_present(caches: list[Cache], current_version: str) -> None:
    """Permit old-generation pruning only when a usable current generation exists."""
    current = version_tuple(current_version)
    versions = set()
    for cache in caches:
        if cache.ref != MAIN_REF or not cache.key.startswith(COOK_BASE_PREFIX):
            continue
        found = cache_version(cache.key)
        if found is None:
            raise ValueError(f"cannot safely classify cook-base cache {cache.cache_id}: {cache.key}")
        parsed = version_tuple(found)
        if parsed > current:
            raise ValueError(f"future Soldr cook-base generation {found} exceeds current {current_version}")
        versions.add(parsed)
    if current not in versions:
        raise ValueError(f"refusing to prune old cook bases: no main cook-base exists for Soldr {current_version}")


def require_single_current_generation(caches: list[Cache], current_version: str) -> None:
    current = version_tuple(current_version)
    versions = {
        cache_version(cache.key) for cache in caches if cache.ref == MAIN_REF and cache.key.startswith(COOK_BASE_PREFIX)
    }
    if None in versions:
        raise ValueError("an unparseable main cook-base key remains")
    parsed = {version_tuple(value) for value in versions if value is not None}
    if parsed != {current}:
        actual = sorted(".".join(map(str, version)) for version in parsed)
        raise ValueError(f"main cook-base generations are {actual}, expected only {current_version}")


def require_retained_native_cook_base(caches: list[Cache], current_version: str, candidates: list[Cache]) -> None:
    """Prove a current native Linux producer remains before deleting any cache."""
    current = version_tuple(current_version)
    candidate_ids = {cache.cache_id for cache in candidates}
    for cache in caches:
        if cache.ref != MAIN_REF or cache.cache_id in candidate_ids:
            continue
        if not cache.key.startswith(COOK_BASE_PREFIX):
            continue
        found = cache_version(cache.key)
        if found is None:
            raise ValueError(f"cannot safely classify cook-base cache {cache.cache_id}: {cache.key}")
        if version_tuple(found) == current and cache.key.endswith("-xlinux") and cache.size > 0:
            return
    raise ValueError(
        f"refusing to prune cook bases: no retained native xlinux cook base exists for Soldr {current_version}"
    )


def require_disabled_cross_cooks_retired(caches: list[Cache], current_version: str) -> None:
    remaining = retired_cross_target_cook_bases(caches, current_version)
    if remaining:
        details = ", ".join(f"{cache.cache_id}:{cache.key}" for cache in remaining)
        raise ValueError(f"disabled cross-target cook bases remain in inventory: {details}")


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError(f"invalid repository: {repository!r}")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, path: str, *, method: str = "GET") -> dict:
        request = Request(
            self.base + path,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
        except HTTPError as exc:
            if method == "DELETE" and exc.code == 404:
                return {"already_absent": True}
            raise RuntimeError(f"GitHub API {method} {path} failed: {exc}") from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub API {method} {path} failed: {exc}") from exc
        if not payload:
            return {}
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise TypeError(f"GitHub API returned unexpected data for {path}")
        return value

    def caches(self) -> list[Cache]:
        result = []
        page = 1
        while True:
            response = self.request(f"/actions/caches?per_page=100&page={page}")
            entries = response.get("actions_caches", [])
            if not isinstance(entries, list):
                raise TypeError("GitHub API cache listing had no actions_caches array")
            for entry in entries:
                result.append(
                    Cache(
                        cache_id=int(entry["id"]),
                        key=str(entry["key"]),
                        ref=str(entry["ref"]),
                        size=int(entry["size_in_bytes"]),
                        last_accessed=str(entry.get("last_accessed_at", "")),
                        created_at=str(entry.get("created_at", "")),
                    )
                )
            if len(entries) < 100:
                return result
            page += 1

    def usage_bytes(self) -> int:
        response = self.request("/actions/cache/usage")
        return int(response["active_caches_size_in_bytes"])

    def delete_cache(self, cache_id: int) -> bool:
        response = self.request(f"/actions/caches/{cache_id}", method="DELETE")
        return not response.get("already_absent", False)


def settled_usage(
    api: GitHub,
    current_version: str,
    current_lock_hash: str,
    *,
    polls: int = 6,
    interval: int = 10,
) -> tuple[int, int, int]:
    """Return (max usage, endpoint usage, listed usage) after deletion settles."""
    last = (0, 0, 0)
    last_policy_error: ValueError | None = None
    for attempt in range(polls):
        endpoint = api.usage_bytes()
        caches = api.caches()
        listed = sum(cache.size for cache in caches)
        last = (max(endpoint, listed), endpoint, listed)
        try:
            require_single_current_generation(caches, current_version)
            require_retained_native_cook_base(caches, current_version, [])
            require_disabled_cross_cooks_retired(caches, current_version)
            require_lock_generations_retired(caches, current_lock_hash)
            last_policy_error = None
        except ValueError as exc:
            last_policy_error = exc
        if last[0] <= BUDGET_BYTES and last_policy_error is None:
            return last
        if attempt + 1 < polls:
            time.sleep(interval)
    if last_policy_error is not None:
        raise last_policy_error
    return last


def prune(
    api: GitHub,
    current_version: str,
    *,
    apply: bool,
    current_lock_hash: str | None = None,
) -> tuple[int, int]:
    current_lock_hash = current_lock_hash or checked_out_lock_hash()
    before = api.caches()
    require_current_generation_present(before, current_version)
    candidates_by_id = {
        cache.cache_id: cache
        for cache in (
            stale_cook_bases(before, current_version)
            + retired_cross_target_cook_bases(before, current_version)
            + superseded_lock_generation_caches(before, current_lock_hash)
        )
    }
    for cache in over_budget_orphaned_lock_caches(before, current_lock_hash, set(candidates_by_id)):
        candidates_by_id[cache.cache_id] = cache
    for cache in retired_old_dylint_outputs(before, current_lock_hash, set(candidates_by_id)):
        candidates_by_id[cache.cache_id] = cache
    candidates = sorted(candidates_by_id.values(), key=lambda cache: cache.cache_id)
    require_retained_native_cook_base(before, current_version, candidates)
    reclaimed = sum(cache.size for cache in candidates)
    for cache in candidates:
        if apply:
            deleted = api.delete_cache(cache.cache_id)
            state = "deleted" if deleted else "already absent"
        else:
            state = "would delete"
        print(f"{state} id={cache.cache_id} ref={cache.ref} size={cache.size} key={cache.key}")

    if apply:
        usage, endpoint, listed = settled_usage(api, current_version, current_lock_hash)
    else:
        remaining = [c for c in before if c not in candidates]
        require_single_current_generation(remaining, current_version)
        require_retained_native_cook_base(remaining, current_version, [])
        require_disabled_cross_cooks_retired(remaining, current_version)
        require_lock_generations_retired(remaining, current_lock_hash)
        endpoint = api.usage_bytes()
        listed = sum(cache.size for cache in before)
        usage = max(endpoint - reclaimed, listed - reclaimed)
        print(
            f"dry-run current: max(endpoint={endpoint}, listed={listed}) bytes; projected after deletion={usage} bytes"
        )
    print(
        f"cache budget: max(endpoint={endpoint}, listed={listed})={usage} bytes; "
        f"limit={BUDGET_BYTES} bytes; retired={len(candidates)} entries/{reclaimed} bytes"
    )
    if usage > BUDGET_BYTES:
        raise RuntimeError(f"Actions cache usage {usage} exceeds {BUDGET_BYTES}-byte budget")
    return len(candidates), reclaimed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="Soldr version from the producer action output")
    parser.add_argument("--apply", action="store_true", help="delete superseded main cache entries")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        parser.error("GITHUB_TOKEN and GITHUB_REPOSITORY must be set")
    try:
        prune(GitHub(repository, token), args.version, apply=args.apply)
    except (RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

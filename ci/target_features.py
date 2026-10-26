"""Print the feature list for a kernal-api *target* graph, comma-separated.

    uv run --no-project --python 3.12 ci/target_features.py [--github-env]

That is every feature in Cargo.toml's `[features]` except `default` and the
host-only ones. A host-only feature is a build-script facility: it only ever
runs in an application's `build.rs`, compiled for the build host, so it never
belongs in a graph linked for another target. `build-resources` is one:
`embed-resource` depends on `vswhom-sys` for Windows, whose C++ archive is built
only on a Windows host, so a Linux-hosted cross link of kernal-api's own test
binaries for `*-pc-windows-msvc` fails with undefined `vswhom_*` symbols.

The list is derived rather than hard-coded so a new feature is covered by the
per-target build matrix automatically. Every optional dependency is named by
a `dep:` entry, so there are no implicit features and this list is exactly
`--all-features` minus the host-only set. Host-only features stay covered by
the per-feature isolation check and by `tests/build-resources-consumer`,
which runs `build-resources` as a real build-dependency.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "Cargo.toml"
HOST_ONLY = frozenset({"build-resources"})


def target_features(manifest: Path = MANIFEST) -> list[str]:
    features = tomllib.loads(manifest.read_text(encoding="utf-8"))["features"]
    unknown = HOST_ONLY - features.keys()
    if unknown:
        raise SystemExit(f"host-only features missing from Cargo.toml: {', '.join(sorted(unknown))}")
    return sorted(name for name in features if name != "default" and name not in HOST_ONLY)


def main() -> int:
    features = ",".join(target_features())
    if sys.argv[1:] == ["--github-env"]:
        # Export KERNAL_TARGET_FEATURES to every later step of this job.
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
            env.write(f"KERNAL_TARGET_FEATURES={features}\n")
    print(features)
    return 0


if __name__ == "__main__":
    sys.exit(main())

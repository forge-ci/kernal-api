"""Keep `.config/nextest.toml` pointed at tests that exist.

Adopted from soldr's `tests/test_nextest_timeout_wrapper.py`. nextest silently
ignores a filter that matches nothing, so a renamed or moved test turns its
override into a no-op with no error anywhere. These checks are static -- they
read the config and the source tree -- so they run in the fast guard lane
instead of needing a built archive.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".config/nextest.toml"


def filters() -> list[str]:
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    overrides = config.get("profile", {}).get("default", {}).get("overrides", [])
    return [override["filter"] for override in overrides]


def test_targets() -> set[str]:
    """Every test binary name: a top-level file, or a category directory."""
    return {path.stem for path in ROOT.glob("tests/*.rs")} | {path.parent.name for path in ROOT.glob("tests/*/main.rs")}


def defined_functions() -> set[str]:
    names = set()
    for source in (*ROOT.glob("src/**/*.rs"), *ROOT.glob("tests/**/*.rs")):
        names.update(re.findall(r"\bfn\s+([A-Za-z0-9_]+)", source.read_text(encoding="utf-8")))
    return names


class NextestConfigTests(unittest.TestCase):
    def test_every_test_has_a_bounded_runtime(self):
        """A hung test must free its runner long before the job timeout does."""
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        timeout = config["profile"]["default"]["slow-timeout"]
        period = int(timeout["period"].rstrip("s"))
        budget = period * timeout["terminate-after"]
        self.assertLessEqual(budget, 300, "a test may not hold a runner past five minutes")

    def test_every_named_binary_is_a_real_test_target(self):
        """`binary(NAME)` must resolve, or nextest refuses the whole config."""
        targets = test_targets()
        for expression in filters():
            for name in re.findall(r"binary\(([A-Za-z0-9_-]+)\)", expression):
                with self.subTest(binary=name):
                    self.assertIn(name, targets, f"{name} is not a test binary")

    def test_every_exact_test_filter_names_a_function_that_exists(self):
        """`test(=path::fn)` whose fn is gone silently stops matching."""
        functions = defined_functions()
        for expression in filters():
            for path in re.findall(r"test\(=([A-Za-z0-9_:]+)\)", expression):
                with self.subTest(test=path):
                    self.assertIn(
                        path.rsplit("::", 1)[-1],
                        functions,
                        f"no function named in {path}; the filter matches nothing",
                    )


if __name__ == "__main__":
    unittest.main()

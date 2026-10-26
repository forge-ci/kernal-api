"""Keep the setup-soldr cache footprint inside the 10 GB repository budget.

One pull request once offered about 23 GB of cache entries, 14.6 GB of them
~2.4 GB cook bases (#355, zackees/setup-soldr#528). These guards pin the
properties that keep the footprint bounded:

- every job reaches setup-soldr through the one wrapper,
  `.github/actions/soldr`, which pins one revision and one Soldr runtime, so
  two versions never write two copies of the same key;
- steps that cook the same graph for the same target share one
  `cache-key-suffix` (the suffix is part of the cook-base key), unless a
  listed reason says why the graphs differ;
- no step saves durable caches from a `pull_request` run, whose entries only
  that pull request can restore: every pin is v0.9.78+ with `save-cache: auto`
  unless a listed reason needs `true`;
- no step writes the per-commit cook-delta layer: one `cook-delta-v2-*`
  generation per commit accumulated ~2.8 GB on main with nothing pruning it
  (zackees/setup-soldr#528). Every step pins a release that honors
  `cook-delta` and sets it to `false`; cook bases stay on.
- the five `ci.yml` build-matrix producers explicitly skip the full cook
  layer: the exact-main run 36281990983 showed zero cook reuse in all five,
  while their distinct target bases added 11.31 GB before registry, prepare,
  and build-cache entries. The measured non-cook layers project to a stable
  7–8.5 GB family; native Linux and Dylint cache policy is unchanged.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
WRAPPER = ROOT / ".github/actions/soldr/action.yml"
DIRECT_SETUP_SOLDR = re.compile(r"^(?P<indent> *)(?P<marker>- )?uses: zackees/setup-soldr@(?P<ref>\S+)(?P<comment>.*)$")
SETUP_SOLDR = re.compile(r"^(?P<indent> *)(?P<marker>- )?uses: \./\.github/actions/soldr(?P<comment>.*)$")

# The Soldr runtime is setup-soldr's default -- the one soldr pin -- so no
# step passes `version:`. `@v0` moves only through setup-soldr's gated
# promotion, so every step in a run still resolves one runtime.

# Every setup-soldr step, keyed by (workflow, job): the target it cooks for
# and the profile it compiles. A new step must be classified here, so its
# cook base is weighed against the others before it lands. `matrix` targets
# carry `${{ matrix.target }}` in their suffix, one distinct target per entry.
COOK_SHAPES = {
    ("ci.yml", "linux"): ("x86_64-unknown-linux-gnu", "dev"),
    # These five target builds use the independent target-specific build-cache,
    # prepared toolchain, registry, and toolchain layers; their full cook bases
    # had 0 compile-cache hits in exact-main full run 36281990983.
    ("ci.yml", "build"): ("matrix", "none"),
    ("ci.yml", "dylints"): ("dylint-nightly", "none"),
    # GEN-022 step 5 (forge-ci/ci.yml#432): push-only writers with their
    # validation job's exact inputs, so PRs restore what main saves.
    ("ci.yml", "linux-cache-writer"): ("x86_64-unknown-linux-gnu", "dev"),
    ("ci.yml", "dylint-cache-writer"): ("dylint-nightly", "none"),
    ("release.yml", "validate-and-package"): ("x86_64-unknown-linux-gnu", "dev"),
    ("release.yml", "symbolizer-workers"): ("matrix", "none"),
    ("auto-release.yml", "publish-crates"): ("x86_64-unknown-linux-gnu", "none"),
    ("macos-x64-tests.yml", "intel-macos-tests"): ("x86_64-apple-darwin", "none"),
}

# (target, profile) pairs whose steps may keep different suffixes, with the
# reason their cook graphs genuinely differ. Empty today: every cooking pair
# on one target shares one suffix.
JUSTIFIED_SPLITS: dict[tuple[str, str], str] = {}

# setup-soldr v0.9.78 (zackees/setup-soldr#527) added `save-cache`, whose
# default `auto` skips every durable save on `pull_request`. Older pins save
# unconditionally.
SAVE_POLICY_MIN_VERSION = (0, 9, 78)
# The ci-lint plan's `cache_save` (ci.toml [cache].write-on): "true" only on
# a main push, "false" on pull requests and dispatched candidate SHAs.
PUSH_ONLY_SAVE_POLICIES = {
    "${{ steps.plan.outputs.cache_save }}",
    "${{ needs.linux.outputs.cache_save }}",
}

# The first setup-soldr release whose main action honors `cook-delta`
# (zackees/setup-soldr#528). Older pins always write the delta layer.
COOK_DELTA_MIN_VERSION = (0, 9, 80)

# Steps allowed to keep the cook-delta layer, with the reason (a measured
# warm-build win and a retention bound). Empty today.
COOK_DELTA_JUSTIFICATIONS: dict[tuple[str, str], str] = {}

# Steps allowed `save-cache: "true"` on pull requests, with the reason: only
# when a later job in the same run restores what this step saved. Empty
# today: no job in a pull-request run restores another job's entry (`linux`,
# each `build` target and `dylints` key their own caches; `test` compiles
# nothing).
PR_SAVE_JUSTIFICATIONS: dict[tuple[str, str], str] = {}

# What `prebuild-deps-flags` must be for a job compiling each profile. The
# flags are hashed into the cook key; cooking `--release` for a job that only
# builds the dev profile stores a base nothing reuses.
PROFILE_FLAGS = {"dev": "", "release": "--release"}
DEFAULT_COOK_FLAGS = "--release"


def workflow_triggers(text):
    block = re.search(r"(?ms)^on:\n(.*?)(?=^\S)", text)
    return set(re.findall(r"(?m)^  ([\w-]+):", block.group(1))) if block else set()


def step_inputs(lines, index, step_indent):
    inputs = {}
    for body in lines[index + 1 :]:
        if body.strip() and len(body) - len(body.lstrip()) <= step_indent:
            break
        pair = re.match(r"^\s+([\w-]+):\s*(.*?)\s*$", body)
        if pair and not body.lstrip().startswith("#") and pair.group(1) != "with":
            value = re.sub(r"\s+#.*$", "", pair.group(2))
            inputs[pair.group(1)] = value.strip('"').strip("'")
    return inputs


def action_step_indent(match):
    """Named steps put uses two spaces deeper than the list item."""
    return len(match.group("indent")) - (0 if match.group("marker") else 2)


def wrapper_call():
    """The wrapper's one setup-soldr step: its ref, pin comment and fixed inputs."""
    lines = WRAPPER.read_text(encoding="utf-8").splitlines()
    calls = [(index, match) for index, line in enumerate(lines) if (match := DIRECT_SETUP_SOLDR.match(line))]
    assert len(calls) == 1, "the wrapper calls setup-soldr exactly once"
    index, match = calls[0]
    fixed = {
        name: value
        for name, value in step_inputs(lines, index, action_step_indent(match)).items()
        if "inputs." not in value and name != "id"
    }
    return match.group("ref"), match.group("comment"), fixed


def setup_soldr_steps():
    """Yield one dict per wrapper call across every workflow, with the
    wrapper's fixed inputs merged in."""
    ref, comment, fixed = wrapper_call()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        triggers = workflow_triggers(text)
        lines = text.splitlines()
        job = None
        for index, line in enumerate(lines):
            header = re.match(r"^  ([\w-]+):\s*$", line)
            if header and text.find("\njobs:\n") < sum(len(x) + 1 for x in lines[:index]):
                job = header.group(1)
            match = SETUP_SOLDR.match(line)
            if not match:
                continue
            inputs = step_inputs(lines, index, action_step_indent(match))
            inputs.pop("id", None)
            overlap = set(inputs) & set(fixed)
            assert not overlap, f"the wrapper fixes {sorted(overlap)}"
            yield {
                "workflow": path.name,
                "job": job,
                "ref": ref,
                "comment": comment,
                "inputs": {**inputs, **fixed},
                "triggers": triggers,
            }


def cooks(step):
    inputs = step["inputs"]
    return inputs.get("cache", "true") != "false" and inputs.get("prebuild-deps", "") != "none"


def pin_version(step):
    match = re.search(r"#\s*v(\d+)\.(\d+)\.(\d+)", step["comment"])
    return tuple(int(part) for part in match.groups()) if match else None


def saves_on_pull_request(step):
    """Whether the step would upload durable caches from a pull request."""
    inputs = step["inputs"]
    if all(inputs.get(name, "true") == "false" for name in ("cache", "build-cache", "solo-toolchain-cache")):
        return False
    version = pin_version(step)
    if version is None or version < SAVE_POLICY_MIN_VERSION:
        return True
    save_policy = inputs.get("save-cache", "auto")
    if save_policy == "false":
        return False
    remote_policy = inputs.get("save-cache-remote", "auto")
    if remote_policy in PUSH_ONLY_SAVE_POLICIES or remote_policy == "false":
        # Its expression evaluates to the string "false" on pull_request.
        return False
    if remote_policy == "true":
        return True
    return save_policy not in {"auto", "false"}


class CachePolicyTests(unittest.TestCase):
    def test_remote_plan_does_not_globally_disable_local_ci_saves(self):
        steps = [step for step in self.steps() if step["workflow"] == "ci.yml"]
        self.assertEqual(len(steps), 5)
        for step in steps:
            with self.subTest(job=step["job"]):
                self.assertNotIn("save-cache", step["inputs"])
                self.assertIn(step["inputs"].get("save-cache-remote"), PUSH_ONLY_SAVE_POLICIES)
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("save-cache: ${{ inputs.save-cache }}", wrapper)
        self.assertIn("save-cache-remote: ${{ inputs.save-cache-remote }}", wrapper)
        global_input = wrapper.split("  save-cache:\n", 1)[1].split("  save-cache-remote:\n", 1)[0]
        self.assertIn('default: "auto"', global_input)

    def steps(self):
        steps = list(setup_soldr_steps())
        self.assertTrue(steps, "found no setup-soldr steps")
        return steps

    def test_every_step_is_classified(self):
        """A new setup-soldr step names its target and profile here first."""
        found = {(s["workflow"], s["job"]) for s in self.steps()}
        self.assertEqual(found, set(COOK_SHAPES))

    def test_only_the_wrapper_calls_setup_soldr(self):
        """One call site keeps one revision and one runtime (ci-lint CACHE-009)."""
        for path in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=path.name):
                self.assertFalse(
                    [line for line in path.read_text(encoding="utf-8").splitlines() if DIRECT_SETUP_SOLDR.match(line)],
                    "call ./.github/actions/soldr instead of zackees/setup-soldr",
                )

    def test_one_pinned_revision(self):
        """Mixed setup-soldr versions write duplicate keys."""
        refs = {s["ref"] for s in self.steps()}
        self.assertEqual(len(refs), 1, f"setup-soldr pins differ: {sorted(refs)}")
        (ref,) = refs
        # The floating `v0` major moves only through setup-soldr's gated
        # promotion, so every step still resolves the same revision per run.
        self.assertEqual(ref, "v0", "float every setup-soldr step at the v0 major")

    def test_no_step_pins_the_soldr_runtime(self):
        for step in self.steps():
            with self.subTest(workflow=step["workflow"], job=step["job"]):
                self.assertIsNone(
                    step["inputs"].get("version"),
                    "take setup-soldr's default runtime; only setup-soldr pins soldr",
                )

    def test_cook_flags_match_the_compiled_profile(self):
        for step in self.steps():
            target, profile = COOK_SHAPES[(step["workflow"], step["job"])]
            with self.subTest(workflow=step["workflow"], job=step["job"]):
                if profile == "none":
                    self.assertFalse(cooks(step), "classified as not cooking, but cooks")
                    continue
                self.assertTrue(cooks(step), "classified as cooking, but does not")
                flags = step["inputs"].get("prebuild-deps-flags", DEFAULT_COOK_FLAGS)
                if target == "matrix":
                    self.assertIn("matrix.", step["inputs"].get("cache-key-suffix", ""))
                    continue
                self.assertEqual(flags, PROFILE_FLAGS[profile])

    def test_only_the_cross_build_matrix_skips_the_unreused_cook_layer(self):
        """Keep the measured cook opt-out local to its five matrix targets."""
        by_name = {(s["workflow"], s["job"]): s for s in self.steps()}
        build = by_name[("ci.yml", "build")]
        self.assertFalse(cooks(build))
        self.assertEqual(build["inputs"].get("prebuild-deps"), "none")
        self.assertEqual(build["inputs"].get("cargo-registry-cache"), "true")

        linux = by_name[("ci.yml", "linux")]
        self.assertTrue(cooks(linux))
        self.assertNotEqual(linux["inputs"].get("prebuild-deps"), "none")

        dylints = by_name[("ci.yml", "dylints")]
        self.assertFalse(cooks(dylints))
        self.assertEqual(dylints["inputs"].get("prebuild-deps"), "none")

    def test_one_suffix_per_target_and_graph(self):
        """Each cook base is ~2.4 GB; two suffixes for one graph store it twice."""
        suffixes = {}
        for step in self.steps():
            target, profile = COOK_SHAPES[(step["workflow"], step["job"])]
            if not cooks(step) or target == "matrix":
                continue
            key = (target, profile, step["inputs"].get("toolchain", ""))
            suffixes.setdefault(key, {}).setdefault(step["inputs"].get("cache-key-suffix", ""), []).append(
                f"{step['workflow']}:{step['job']}"
            )
        for (target, profile, _), by_suffix in suffixes.items():
            if (target, profile) in JUSTIFIED_SPLITS:
                continue
            with self.subTest(target=target, profile=profile):
                self.assertEqual(len(by_suffix), 1, f"one graph, several suffixes: {by_suffix}")

    def test_justified_splits_are_live(self):
        for pair, reason in JUSTIFIED_SPLITS.items():
            self.assertTrue(reason.strip(), pair)
            self.assertIn(pair, set(COOK_SHAPES.values()))

    def test_no_step_saves_on_pull_request(self):
        """A pull-request cache is restorable only by that pull request."""
        for step in self.steps():
            if "pull_request" not in step["triggers"] or not saves_on_pull_request(step):
                continue
            where = (step["workflow"], step["job"])
            with self.subTest(workflow=where[0], job=where[1]):
                self.assertIn(
                    where,
                    PR_SAVE_JUSTIFICATIONS,
                    "pin setup-soldr v0.9.78+ and leave `save-cache` unset or `auto`",
                )

    def test_every_step_states_its_save_policy(self):
        """Every step names a planned remote policy or explicit global disable."""
        for step in self.steps():
            with self.subTest(workflow=step["workflow"], job=step["job"]):
                global_policy = step["inputs"].get("save-cache", "auto")
                self.assertIn(global_policy, {"auto", "false"})
                if global_policy != "false":
                    self.assertIn(
                        step["inputs"].get("save-cache-remote"),
                        PUSH_ONLY_SAVE_POLICIES,
                    )

    def test_release_validation_restores_without_racing_main_retention(self):
        """Separate release runs may restore, but cannot save stale lock keys."""
        steps = [
            step for step in self.steps() if (step["workflow"], step["job"]) == ("release.yml", "validate-and-package")
        ]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["inputs"].get("save-cache"), "false")
        self.assertNotEqual(steps[0]["inputs"].get("cache"), "false")

    def test_ci_writers_save_only_on_main_push(self):
        """PR and arbitrary-SHA dispatch runs must not write main-scoped keys."""
        steps = [step for step in self.steps() if step["workflow"] == "ci.yml"]
        self.assertEqual(len(steps), 5)
        for step in steps:
            with self.subTest(job=step["job"]):
                self.assertIn(
                    step["inputs"].get("save-cache-remote"),
                    PUSH_ONLY_SAVE_POLICIES,
                )

    def test_main_writers_are_linux_x64_only(self):
        """ci.toml declares every cache family `per = "none"`: one writer shape.

        A main push runs minimal mode, so the only jobs that can save there
        are the ones that run in every mode: `linux` and `dylints`, both on
        ubuntu-24.04 x64. The full-mode `build` matrix restores but never
        reaches a push. If this fails, re-derive ci.toml [cache.family].
        """
        from ci import ci_mode

        self.assertEqual(ci_mode.select("push", {}, "a" * 40)[0], "minimal")
        text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        for step in self.steps():
            if step["inputs"].get("save-cache") not in PUSH_ONLY_SAVE_POLICIES:
                continue
            with self.subTest(workflow=step["workflow"], job=step["job"]):
                self.assertEqual(step["workflow"], "ci.yml")
                body = re.search(rf"(?ms)^  {step['job']}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text).group(1)
                if step["job"] in {"linux", "dylints", "linux-cache-writer", "dylint-cache-writer"}:
                    self.assertRegex(body, r"(?m)^    runs-on: ubuntu-24\.04$")
                else:
                    self.assertIn("if: needs.linux.outputs.mode == 'full'", body)

    def test_pr_save_justifications_are_live(self):
        """Drop a justification once its step stops saving on pull requests."""
        for where, reason in PR_SAVE_JUSTIFICATIONS.items():
            steps = [s for s in self.steps() if (s["workflow"], s["job"]) == where]
            with self.subTest(workflow=where[0], job=where[1]):
                self.assertTrue(reason.strip())
                self.assertTrue(steps and all(saves_on_pull_request(s) for s in steps))

    def test_no_step_writes_the_cook_delta_layer(self):
        """The delta layer saves one generation per commit and is never pruned."""
        for step in self.steps():
            where = (step["workflow"], step["job"])
            if where in COOK_DELTA_JUSTIFICATIONS:
                continue
            with self.subTest(workflow=where[0], job=where[1]):
                self.assertEqual(
                    step["inputs"].get("cook-delta"),
                    "false",
                    "set `cook-delta: false` (setup-soldr#528)",
                )
                version = pin_version(step)
                self.assertIsNotNone(COOK_DELTA_MIN_VERSION)
                self.assertTrue(
                    version is not None and version >= COOK_DELTA_MIN_VERSION,
                    f"setup-soldr {version} ignores `cook-delta`",
                )

    def test_cook_delta_justifications_are_live(self):
        for where, reason in COOK_DELTA_JUSTIFICATIONS.items():
            self.assertTrue(reason.strip(), where)
            self.assertIn(where, COOK_SHAPES)


if __name__ == "__main__":
    unittest.main()

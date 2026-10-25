"""Run minimal Linux and Dylint checks through bosn's pinned act2 engine.

Both required workflow lanes prove minimal checks. Full and other native platform
jobs remain remote until their complete coverage has local evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .platform_host import NativeHost, native_host
else:
    from platform_host import NativeHost, native_host

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/ci.yml"
CI_LINT = "ci-lint @ git+https://github.com/zackees/ci.yml@29087cb90ddebb84cd5f4b2e0f85380e76c743e5"


def bosn_command(*arguments: str) -> list[str]:
    """Use the latest published bosn and an optional owned state dir.

    Never pin a bosn release here: the machine shares one bosn daemon, and an
    older client is refused by a newer daemon (bosn#509).
    """
    argv = ["uvx", "bosn@latest", *arguments]
    state = os.environ.get("BOSN_GATE_STATE_DIR")
    if state:
        argv.extend(["--state-dir", str(Path(state).resolve())])
    return argv


def verify_pr_base(ref: str) -> None:
    """A feature branch used as its own base can silently omit diff-gated tests."""
    if ref != "refs/remotes/origin/main":
        raise ValueError(
            "origin/HEAD must name main; run git remote set-head origin -a"
        )


def verify_native_host(host: NativeHost) -> None:
    """This lane cannot attest x64 tests through ARM emulation or another OS."""
    if (
        host.system != "Linux"
        or host.machine.lower() not in {"x86_64", "amd64"}
        or host.docker_platform.lower() not in {"linux/x86_64", "linux/amd64"}
    ):
        raise ValueError(
            "minimal Linux attestation requires a native Linux x64 host and Docker daemon"
        )


@dataclass(frozen=True)
class RunProof:
    workspace: Path
    sha: str
    dirty: JsonValue
    engine: str
    act_version: str
    workflow: str
    job: str
    mode: str
    state: str
    conclusion: str
    exit_code: int
    total: int
    completed: int
    failed: int
    git_tree: str
    trigger: str
    event: str
    cleanup: str
    payload_sha256: str

    @classmethod
    def from_json(cls, raw: dict[str, JsonValue]) -> RunProof:
        """Validate the wire document before treating it as evidence."""
        if (
            type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or raw.get("provider") != "github"
            or raw.get("repository") != "zackees/kernal-api"
            or type(raw.get("act_exit_code")) is not int
            or raw["act_exit_code"] != 0
            or not isinstance(raw.get("tree_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(raw["tree_digest"])) is None
        ):
            raise ValueError("bosn record lacks a successful source snapshot identity")
        strings = (
            "workspace",
            "sha",
            "engine",
            "act_version",
            "workflow",
            "job",
            "mode",
            "state",
            "conclusion",
            "git_tree",
            "trigger",
            "event",
            "cleanup",
            "payload_sha256",
        )
        if any(not isinstance(raw.get(key), str) for key in strings):
            raise ValueError("bosn record is missing identity or completion fields")
        jobs = raw.get("jobs")
        if not isinstance(jobs, dict) or "dirty" not in raw:
            raise ValueError("bosn record is missing snapshot or job evidence")
        if type(raw.get("exit_code")) is not int or any(
            type(jobs.get(key)) is not int for key in ("total", "completed", "failed")
        ):
            raise ValueError("bosn record is missing numeric completion evidence")
        return cls(
            Path(str(raw["workspace"])).resolve(),
            str(raw["sha"]),
            raw["dirty"],
            str(raw["engine"]),
            str(raw["act_version"]),
            str(raw["workflow"]),
            str(raw["job"]),
            str(raw["mode"]),
            str(raw["state"]),
            str(raw["conclusion"]),
            int(raw["exit_code"]),
            int(jobs["total"]),
            int(jobs["completed"]),
            int(jobs["failed"]),
            str(raw["git_tree"]),
            str(raw["trigger"]),
            str(raw["event"]),
            str(raw["cleanup"]),
            str(raw["payload_sha256"]),
        )


def verify_run(
    proof: RunProof, workspace: Path, sha: str, git_tree: str, *, job: str = "linux"
) -> None:
    """Reject unrelated, dirty, partial, failed, or upstream-act runs."""
    if (
        proof.workspace != workspace.resolve()
        or proof.sha != sha
        or proof.dirty is not None
        or proof.engine != "act"
        or "-act2." not in proof.act_version
        or proof.workflow != WORKFLOW
        or proof.job != job
        or proof.mode != "minimal"
        or proof.git_tree != git_tree
        or re.fullmatch(r"[0-9a-f]{40}", git_tree) is None
        or proof.trigger != "pr"
        or proof.event != "pull_request"
        or re.fullmatch(r"[0-9a-f]{64}", proof.payload_sha256) is None
    ):
        raise ValueError("bosn run does not prove this clean minimal Linux lane")
    if (
        proof.state != "done"
        or proof.conclusion != "success"
        or proof.exit_code != 0
        or proof.total < 1
        or proof.completed != proof.total
        or proof.failed != 0
        or proof.cleanup != "removed"
    ):
        raise ValueError("bosn run did not complete every selected job successfully")


def output(argv: list[str]) -> str:
    """Temporary-file capture avoids pipe-held EOF and capture-then-wait."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stream:
        subprocess.run(argv, cwd=ROOT, stdout=stream, check=True)
        stream.seek(0)
        return stream.read()


def document(argv: list[str]) -> dict[str, JsonValue]:
    raw = json.loads(output(argv))
    if not isinstance(raw, dict):
        raise TypeError("expected a bosn JSON object")
    return raw


def run_fast_guards() -> None:
    """Catch workflow and source guard failures before paying for an engine."""
    subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--with",
            CI_LINT,
            "--with",
            "pyyaml==6.0.2",
            "--python",
            "3.12",
            "python",
            "-m",
            "unittest",
            "discover",
            "-s",
            "ci",
            "-p",
            "test_*.py",
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", choices=("linux", "dylints"), default="linux")
    args = parser.parse_args()
    verify_pr_base(output(["git", "symbolic-ref", "refs/remotes/origin/HEAD"]).strip())
    if output(["git", "status", "--porcelain", "--untracked-files=normal"]).strip():
        raise ValueError("commit the worktree before running its local gate")
    run_fast_guards()
    verify_native_host(
        native_host(
            output(
                ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]
            ).strip(),
        )
    )
    sha = output(["git", "rev-parse", "HEAD"]).strip()
    git_tree = output(["git", "rev-parse", "HEAD^{tree}"]).strip()
    submitted = document(
        bosn_command(
            "ci",
            "run",
            "--workspace",
            str(ROOT),
            "--workflow",
            WORKFLOW,
            "--job",
            args.job,
            "--trigger",
            "pr",
            "--mode",
            "minimal",
            "--sha",
            sha,
            "--timeout-secs",
            "7200",
            "--json",
        )
    )
    run_id = submitted.get("run")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("bosn did not return a run ID")
    print(f"bosn local gate run: {run_id}", flush=True)
    subprocess.run(bosn_command("ci", "wait", run_id), cwd=ROOT, check=True)
    report = output(bosn_command("ci", "show", run_id, "--json"))
    raw = json.loads(report)
    if not isinstance(raw, dict):
        raise TypeError("expected the original bosn JSON object")
    proof = RunProof.from_json(raw)
    verify_run(proof, ROOT, sha, git_tree, job=args.job)
    if __package__:
        from .replay_evidence import verify_checks
    else:
        from replay_evidence import verify_checks
    verify_checks(raw, ROOT, sha, git_tree, args.job)
    if (
        output(["git", "rev-parse", "HEAD"]).strip() != sha
        or output(["git", "status", "--porcelain", "--untracked-files=normal"]).strip()
    ):
        raise ValueError("worktree changed while the local gate ran")
    report_path = os.environ.get("CI_LINT_GATE_REPLAY_REPORT")
    if report_path:
        Path(report_path).write_text(report, encoding="utf-8")
    print(f"Passed minimal {args.job} on {proof.act_version}: {sha}", flush=True)


if __name__ == "__main__":
    main()

"""The git pre-commit hook (task item 5, brief D11's third enforcement point)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from grison.remote.bootstrap import bootstrap_workspace
from grison.scaffold import precommit


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)


def _commit(path: Path, message: str) -> subprocess.CompletedProcess:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    return subprocess.run(
        ["git", "commit", "-m", message], cwd=path, capture_output=True, text=True
    )


def test_not_a_repo_is_a_silent_noop(tmp_path: Path) -> None:
    result = precommit.install_precommit_hook(tmp_path)
    assert result.installed is False
    assert "not a git repository" in result.reason


def test_installs_an_executable_hook(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = precommit.install_precommit_hook(tmp_path)
    assert result.installed is True
    assert result.hook_path is not None
    assert result.hook_path.exists()
    assert result.hook_path.stat().st_mode & 0o111  # executable


def test_a_commit_with_an_invalid_document_is_refused(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    _init_repo(tmp_path)
    precommit.install_precommit_hook(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial scaffold"], cwd=tmp_path, check=True)

    bad = tmp_path / "findings" / "library" / "bad.md"
    bad.write_text("---\nseverity: not-a-real-severity\nfinding_type: web\n---\n# x\n")

    result = _commit(tmp_path, "add an invalid finding")
    assert result.returncode != 0
    assert "FND-003" in (result.stdout + result.stderr) or "grison: commit blocked" in (
        result.stdout + result.stderr
    )
    log = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "add an invalid finding" not in log


def test_a_clean_commit_succeeds(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path)
    _init_repo(tmp_path)
    precommit.install_precommit_hook(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial scaffold"], cwd=tmp_path, check=True)

    good = tmp_path / "findings" / "library" / "good.md"
    good.write_text(
        "---\nseverity: low\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    result = _commit(tmp_path, "add a clean finding")
    assert result.returncode == 0, result.stderr
    log = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "add a clean finding" in log


def test_grison_missing_from_path_blocks_the_commit_with_a_clear_message(
    tmp_path: Path,
) -> None:
    """Bug fix: the generated hook used to invoke a bare `grison validate` with no
    existence check — if `grison` isn't on the PATH a commit runs under (a fresh
    clone before `uv sync`, a shell that doesn't source the profile a hook runs
    under), that surfaced as a confusing shell "command not found" rather than a
    clear, grison-named reason. It must still refuse the commit (never a silent
    pass) but say plainly that `grison` itself is missing."""
    bootstrap_workspace(tmp_path)
    _init_repo(tmp_path)
    precommit.install_precommit_hook(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial scaffold"], cwd=tmp_path, check=True)

    good = tmp_path / "findings" / "library" / "good.md"
    good.write_text(
        "---\nseverity: low\nfinding_type: web\n---\n"
        "# x\n\n## Description\n\nx\n\n## Impact\n\nx\n\n## Mitigation\n\nx\n\n"
        "## Replication Steps\n\nx\n\n## References\n\nx\n"
    )
    # A minimal PATH with git/sh but deliberately no grison — simulates the real
    # "not installed yet" case without needing to actually uninstall anything.
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "add a clean finding"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "grison" in output and "PATH" in output
    log = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "add a clean finding" not in log


def test_an_existing_foreign_hook_is_left_untouched(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    foreign = hooks_dir / "pre-commit"
    foreign.write_text("#!/bin/sh\necho 'a pre-existing husky-style hook'\nexit 0\n")
    foreign.chmod(0o755)

    result = precommit.install_precommit_hook(tmp_path)
    assert result.installed is False
    assert "foreign" in result.reason
    assert result.instructions is not None
    assert "grison validate" in result.instructions
    assert foreign.read_text() == "#!/bin/sh\necho 'a pre-existing husky-style hook'\nexit 0\n"


def test_reinstalling_grisons_own_hook_updates_it_in_place(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    first = precommit.install_precommit_hook(tmp_path)
    assert first.reason == "installed"
    second = precommit.install_precommit_hook(tmp_path)
    assert second.reason == "already installed"

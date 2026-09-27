"""Territory contracts — the unit of contention is a path, not a name.

The motivating case is real: six repos under ~/repos carry multiple git worktrees sharing one
``.git``. Agents with different names sit in the *same* checkout; one agent holds two sessions
on one branch. Scoping by agent name cannot see either collision.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import territory


# --- canonicalization ------------------------------------------------------

def test_a_relative_path_becomes_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert territory.canonical("sub") == str((tmp_path / "sub").resolve())


def test_trailing_slashes_do_not_create_a_second_territory(tmp_path: Path) -> None:
    a = territory.canonical(str(tmp_path))
    b = territory.canonical(str(tmp_path) + "/")
    assert a == b


def test_a_symlinked_path_resolves_to_its_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert territory.canonical(str(link)) == territory.canonical(str(real))


def test_dot_dot_segments_are_normalized(tmp_path: Path) -> None:
    assert territory.canonical(str(tmp_path / "a" / ".." / "b")) == str((tmp_path / "b").resolve())


def test_a_path_that_does_not_exist_yet_still_canonicalizes(tmp_path: Path) -> None:
    """A fence may name a worktree before it is created."""
    assert territory.canonical(str(tmp_path / "not-yet")) == str((tmp_path / "not-yet").resolve())


# --- the real collision: siblings in one repo are different territories ----

def test_two_worktrees_of_one_repo_are_different_territories(tmp_path: Path) -> None:
    """The NEXUS case. Fencing them together would be opacity in the other direction."""
    main = tmp_path / "NEXUS"
    wt = main / ".worktrees" / "t_abc"
    wt.mkdir(parents=True)
    assert territory.canonical(str(main)) != territory.canonical(str(wt))


def test_agents_in_the_same_checkout_share_one_territory(tmp_path: Path) -> None:
    """The other half: different names, same path, same territory — a real collision."""
    assert territory.canonical(str(tmp_path)) == territory.canonical(str(tmp_path))


# --- resolution from a tool call ------------------------------------------

def test_an_explicit_workdir_argument_wins(tmp_path: Path) -> None:
    got = territory.from_tool_args("terminal", {"workdir": str(tmp_path)})
    assert got == territory.canonical(str(tmp_path))


def test_a_path_argument_is_honoured_for_file_tools(tmp_path: Path) -> None:
    target = tmp_path / "src" / "main.rs"
    got = territory.from_tool_args("write_file", {"path": str(target)})
    assert got == territory.canonical(str(tmp_path / "src"))


def test_a_file_path_territory_is_its_parent_directory(tmp_path: Path) -> None:
    """`write_file` names a file, but the territory is the tree it lives in."""
    target = tmp_path / "src" / "main.rs"
    got = territory.from_tool_args("write_file", {"path": str(target)})
    assert got.endswith("src")


def test_no_arguments_falls_back_to_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert territory.from_tool_args("read_file", {}) == territory.canonical(str(tmp_path))


def test_resolution_never_raises_on_a_junk_argument(tmp_path: Path) -> None:
    assert territory.from_tool_args("write_file", {"path": 12345}) != ""
    assert territory.from_tool_args("write_file", {"path": "   "}) != ""


# --- a fence must not be able to bind the whole host -----------------------

def test_a_bare_wildcard_is_rejected_as_too_broad() -> None:
    assert territory.pattern_is_too_broad("*")
    assert territory.pattern_is_too_broad(".*")
    assert territory.pattern_is_too_broad("")


def test_a_specific_territory_pattern_is_not_too_broad() -> None:
    assert not territory.pattern_is_too_broad(r"/var/home/bruno/repos/NEXUS")


def test_a_wildcard_inside_a_path_is_allowed() -> None:
    assert not territory.pattern_is_too_broad(r"/var/home/bruno/repos/NEXUS/.*")


def test_patterns_match_whole_paths() -> None:
    assert territory.matches(r"/repos/NEXUS", "/repos/NEXUS")
    assert not territory.matches(r"/repos/NEXUS", "/repos/NEXUS/sub")


def test_a_malformed_pattern_degrades_to_a_literal_not_to_everything() -> None:
    assert territory.matches("/repos/[unclosed", "/repos/[unclosed")
    assert not territory.matches("/repos/[unclosed", "/repos/anything")


# --- repo_root is for visibility, not for binding --------------------------

def test_repo_root_finds_the_shared_repo_of_a_worktree(tmp_path: Path) -> None:
    import subprocess
    main = tmp_path / "repo"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    wt = main / ".worktrees" / "t_x"
    wt.mkdir(parents=True)
    assert territory.repo_root(territory.canonical(str(wt))) == territory.canonical(str(main))


def test_an_untracked_directory_is_its_own_repo_root(tmp_path: Path) -> None:
    plain = tmp_path / "scratch"
    plain.mkdir()
    assert territory.repo_root(territory.canonical(str(plain))) == territory.canonical(str(plain))

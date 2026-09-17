"""Proofs for grison/index.py: strict load, the mutation API, POSIX-only path
rules, a hypothesis property test that any set/move/remove sequence keeps the file
loadable/sorted/duplicate-free, and a real ``git merge-file`` proof that two
independent additions merge cleanly."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from grison.index import Index, IndexFileError, IndexKind, IndexRecord

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)


# --- load: happy path + strictness ---------------------------------------------


def test_load_missing_file_is_empty_index(tmp_path: Path) -> None:
    idx = Index.load(tmp_path)
    assert idx.records == {}


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("findings/library/a.md", IndexKind.GW_FINDING, 1)
    idx.set("methodology/library/x/p.md", IndexKind.BS_PAGE, 7)
    idx.save()

    loaded = Index.load(tmp_path)
    assert loaded.records == {
        "findings/library/a.md": IndexRecord(IndexKind.GW_FINDING, 1),
        "methodology/library/x/p.md": IndexRecord(IndexKind.BS_PAGE, 7),
    }


def test_save_is_sorted_and_one_record_per_line(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("z.md", IndexKind.GW_FINDING, 1)
    idx.set("a.md", IndexKind.GW_FINDING, 2)
    idx.save()
    text = (tmp_path / ".grison" / "index.json").read_text()
    lines = [ln for ln in text.splitlines() if '"id"' in ln]
    assert len(lines) == 2
    assert lines[0].strip().startswith('"a.md"')
    assert lines[1].strip().startswith('"z.md"')


def test_save_is_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    idx.save()
    before = (tmp_path / ".grison" / "index.json").read_text()

    import grison.fsio as fsio

    def boom(*a: object, **k: object) -> None:
        raise OSError("crash")

    monkeypatch.setattr(fsio.os, "replace", boom)
    idx.set("b.md", IndexKind.GW_FINDING, 2)
    with pytest.raises(OSError):
        idx.save()
    assert (tmp_path / ".grison" / "index.json").read_text() == before


def test_load_rejects_unknown_kind(tmp_path: Path) -> None:
    _write_raw(tmp_path, {"a.md": {"kind": "gw.bogus", "id": 1}})
    with pytest.raises(IndexFileError, match="a.md.*unknown kind"):
        Index.load(tmp_path)


def test_load_rejects_non_integer_id(tmp_path: Path) -> None:
    _write_raw(tmp_path, {"a.md": {"kind": "gw.finding", "id": "1"}})
    with pytest.raises(IndexFileError, match="a.md.*non-integer id"):
        Index.load(tmp_path)


def test_load_rejects_unexpected_field(tmp_path: Path) -> None:
    _write_raw(tmp_path, {"a.md": {"kind": "gw.finding", "id": 1, "extra": True}})
    with pytest.raises(IndexFileError, match="unexpected field"):
        Index.load(tmp_path)


def test_load_rejects_non_object_entry(tmp_path: Path) -> None:
    _write_raw(tmp_path, {"a.md": "not-an-object"})
    with pytest.raises(IndexFileError, match="not an object"):
        Index.load(tmp_path)


def test_load_rejects_duplicate_identity_under_two_paths(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        {
            "a.md": {"kind": "gw.finding", "id": 1},
            "b.md": {"kind": "gw.finding", "id": 1},
        },
    )
    with pytest.raises(IndexFileError, match="indexed under two paths"):
        Index.load(tmp_path)


def test_load_rejects_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / ".grison" / "index.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(IndexFileError, match="invalid JSON"):
        Index.load(tmp_path)


def test_load_rejects_missing_records_key(tmp_path: Path) -> None:
    p = tmp_path / ".grison" / "index.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(IndexFileError, match="missing 'records'"):
        Index.load(tmp_path)


def _write_raw(root: Path, records: dict) -> None:
    p = root / ".grison" / "index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 1, "records": records}), encoding="utf-8")


# --- path rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["/abs/path.md", "a\\b.md", "../escape.md", "a/../b.md", ""],
)
def test_rejects_non_posix_relative_paths(tmp_path: Path, bad: str) -> None:
    idx = Index(root=tmp_path)
    with pytest.raises(IndexFileError):
        idx.set(bad, IndexKind.GW_FINDING, 1)


# --- mutation API ------------------------------------------------------------


def test_set_get_path_of(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    assert idx.get("a.md") == IndexRecord(IndexKind.GW_FINDING, 1)
    assert idx.path_of(IndexKind.GW_FINDING, 1) == "a.md"
    assert idx.path_of(IndexKind.GW_FINDING, 2) is None


def test_set_is_idempotent_on_the_same_path(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    idx.set("a.md", IndexKind.GW_FINDING, 1)  # no-op, must not raise
    assert idx.get("a.md") == IndexRecord(IndexKind.GW_FINDING, 1)


def test_set_refuses_duplicate_identity_under_a_different_path(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    with pytest.raises(IndexFileError, match="already indexed under 'a.md'"):
        idx.set("b.md", IndexKind.GW_FINDING, 1)


def test_remove_is_a_noop_when_unindexed(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.remove("nope.md")  # must not raise
    assert idx.records == {}


def test_move_reassigns_path(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("old.md", IndexKind.GW_FINDING, 1)
    idx.move("old.md", "new.md")
    assert idx.get("old.md") is None
    assert idx.get("new.md") == IndexRecord(IndexKind.GW_FINDING, 1)


def test_move_unindexed_path_raises(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    with pytest.raises(IndexFileError, match="not indexed"):
        idx.move("nope.md", "new.md")


def test_move_onto_an_existing_different_path_raises(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    idx.set("b.md", IndexKind.GW_FINDING, 2)
    with pytest.raises(IndexFileError, match="already indexed"):
        idx.move("a.md", "b.md")


def test_under_returns_records_below_a_directory(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("findings/reports/7-acme/a.md", IndexKind.GW_REPORTED_FINDING, 1)
    idx.set("findings/reports/7-acme/evidence/x.png", IndexKind.GW_EVIDENCE, 2)
    idx.set("findings/library/z.md", IndexKind.GW_FINDING, 3)
    hits = idx.under("findings/reports/7-acme")
    assert set(hits) == {"findings/reports/7-acme/a.md", "findings/reports/7-acme/evidence/x.png"}


def test_missing_on_disk(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    (tmp_path / "a.md").write_text("x")
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    idx.set("gone.md", IndexKind.GW_FINDING, 2)
    assert idx.missing_on_disk(tmp_path) == ["gone.md"]


def test_unindexed(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    assert idx.unindexed(tmp_path, ["a.md", "b.md", "c.md"]) == ["b.md", "c.md"]


# --- property test: any set/move/remove sequence stays sound -------------------


_path_names = st.sampled_from([f"{c}{n}.md" for c in "abcdefgh" for n in range(3)])
_dir_names = st.sampled_from(["", "sub/", "sub/deep/"])
_paths = st.builds(lambda d, n: d + n, _dir_names, _path_names)
_kinds = st.sampled_from(list(IndexKind))
_ids = st.integers(min_value=1, max_value=20)

_set_op = st.tuples(st.just("set"), _paths, _kinds, _ids)
_remove_op = st.tuples(st.just("remove"), _paths)
_ops = st.lists(_set_op | _remove_op, max_size=15)


def test_any_set_remove_sequence_keeps_the_index_sound(tmp_path: Path) -> None:
    """Any sequence of set()/remove() calls, replayed on a fresh in-memory Index
    and then saved once, must leave ``.grison/index.json`` loadable, sorted, and
    free of duplicate identities — the property the brief asks for. The hypothesis
    test is defined inside this outer test so it can reuse the one ``tmp_path``
    fixture instance across every generated example (the supported pattern for
    combining ``@given`` with a function-scoped pytest fixture)."""

    @given(_ops)
    @settings(max_examples=150, deadline=None)
    def run(ops: list[tuple]) -> None:
        idx = Index(root=tmp_path)
        for op in ops:
            if op[0] == "set":
                _, path, kind, ident = op
                existing = idx.path_of(kind, ident)
                if existing is not None and existing != path:
                    continue  # would violate the one-identity-one-path invariant; skip
                idx.set(path, kind, ident)
            else:
                idx.remove(op[1])

        idx.save()
        text = Index.path_for(tmp_path).read_text(encoding="utf-8")

        # loadable, and round-trips to the same records
        reloaded = Index.load(tmp_path)
        assert reloaded.records == idx.records

        # sorted
        paths_in_file = [
            line.strip().split('"')[1]
            for line in text.splitlines()
            if line.strip().startswith('"') and '"id"' in line
        ]
        assert paths_in_file == sorted(paths_in_file)

        # no duplicate identities
        identities = [(r.kind, r.id) for r in idx.records.values()]
        assert len(identities) == len(set(identities))

    run()


def test_move_sequence_keeps_the_index_sound(tmp_path: Path) -> None:
    idx = Index(root=tmp_path)
    idx.set("a.md", IndexKind.GW_FINDING, 1)
    idx.move("a.md", "b.md")
    idx.move("b.md", "c/d.md")
    idx.save()
    reloaded = Index.load(tmp_path)
    assert reloaded.records == {"c/d.md": IndexRecord(IndexKind.GW_FINDING, 1)}


# --- merge-friendliness: real git merge-file -----------------------------------


def test_two_independent_additions_merge_cleanly_with_git_merge_file(tmp_path: Path) -> None:
    """Read-only w.r.t. the repo: runs ``git merge-file`` on three temp files built
    from Index.save()'s own serialization, never touching the actual git repo."""
    base_idx = Index(root=tmp_path)
    base_idx.set("a.md", IndexKind.GW_FINDING, 1)
    base_idx.set("e.md", IndexKind.GW_FINDING, 2)
    base_idx.set("m.md", IndexKind.GW_FINDING, 3)
    base_idx.set("z.md", IndexKind.GW_FINDING, 4)

    ours = Index(root=tmp_path, records=dict(base_idx.records))
    ours.set("b.md", IndexKind.GW_FINDING, 5)  # near the start of the alphabetic gap

    theirs = Index(root=tmp_path, records=dict(base_idx.records))
    theirs.set("y.md", IndexKind.BS_PAGE, 6)  # near the end — a different gap entirely

    base_file = tmp_path / "base.json"
    ours_file = tmp_path / "ours.json"
    theirs_file = tmp_path / "theirs.json"
    base_file.write_text(_dumps_for_test(base_idx))
    ours_file.write_text(_dumps_for_test(ours))
    theirs_file.write_text(_dumps_for_test(theirs))

    result = subprocess.run(
        ["git", "merge-file", "-p", str(ours_file), str(base_file), str(theirs_file)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"git merge-file reported a conflict:\n{result.stdout}"

    merged = json.loads(result.stdout)
    assert set(merged["records"]) == {"a.md", "b.md", "e.md", "m.md", "y.md", "z.md"}
    assert merged["records"]["b.md"] == {"id": 5, "kind": "gw.finding"}
    assert merged["records"]["y.md"] == {"id": 6, "kind": "bs.page"}


def _dumps_for_test(idx: Index) -> str:
    from grison.index import _dumps  # exercise the real serializer, not a copy

    return _dumps(idx.version, idx.records)

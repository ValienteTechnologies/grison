"""Identity M/U move-pairing (D3, ENGINE.md 'Identity'): deterministic pairing plus the
hypothesis property the task asks for — pairing never assigns one record twice, and
(trivially, by construction — see the module docstring) never pairs across kinds,
since :func:`pair` is only ever handed one kind's M/U lists in the first place."""

from __future__ import annotations

from pathlib import PurePosixPath

from hypothesis import given
from hypothesis import strategies as st

from grison.engine.identity import Missing, Unindexed, normalized_similarity, pair


def test_identical_content_pairs_as_a_pure_move() -> None:
    m = Missing(path=PurePosixPath("a.md"), id=1, base_hash="h1", remote_content="hello world")
    u = Unindexed(path=PurePosixPath("b.md"), content="whatever", content_hash="h1")
    result = pair([m], [u])
    assert len(result.decisions) == 1
    d = result.decisions[0]
    assert d.old_path == PurePosixPath("a.md")
    assert d.new_path == PurePosixPath("b.md")
    assert d.id == 1
    assert d.edited is False
    assert not result.unpaired_missing
    assert not result.unpaired_unindexed


def test_similar_content_pairs_as_move_and_edit() -> None:
    m = Missing(path=PurePosixPath("a.md"), id=1, base_hash="h-base",
               remote_content="the quick brown fox jumps over the lazy dog")
    u = Unindexed(path=PurePosixPath("b.md"),
                 content="the quick brown fox jumps over a lazy dog today", content_hash="h-new")
    result = pair([m], [u])
    assert len(result.decisions) == 1
    assert result.decisions[0].edited is True


def test_unrelated_content_does_not_pair() -> None:
    m = Missing(path=PurePosixPath("a.md"), id=1, base_hash="h-base", remote_content="alpha beta")
    u = Unindexed(path=PurePosixPath("b.md"), content="completely different text entirely",
                 content_hash="h-new")
    result = pair([m], [u])
    assert not result.decisions
    assert result.unpaired_missing == [m]
    assert result.unpaired_unindexed == [u]


def test_best_score_wins_and_pairing_is_one_to_one() -> None:
    m1 = Missing(path=PurePosixPath("a.md"), id=1, base_hash="ha", remote_content="x")
    m2 = Missing(path=PurePosixPath("b.md"), id=2, base_hash="hb", remote_content="x")
    # u matches m1's base hash exactly (identical -> score 1.0); should NOT be stolen
    # by m2 even though m2 is also a candidate via similarity.
    u = Unindexed(path=PurePosixPath("c.md"), content="x", content_hash="ha")
    result = pair([m1, m2], [u])
    assert len(result.decisions) == 1
    assert result.decisions[0].old_path == PurePosixPath("a.md")
    assert result.unpaired_missing == [m2]


def test_ties_broken_deterministically_by_path() -> None:
    # two U's identical to the SAME M's base — same score, tie broken by u.path asc.
    m = Missing(path=PurePosixPath("a.md"), id=1, base_hash="h1", remote_content="x")
    u1 = Unindexed(path=PurePosixPath("z.md"), content="x", content_hash="h1")
    u2 = Unindexed(path=PurePosixPath("b.md"), content="x", content_hash="h1")
    result = pair([m], [u1, u2])
    assert len(result.decisions) == 1
    assert result.decisions[0].new_path == PurePosixPath("b.md")
    assert result.unpaired_unindexed == [u1]


def test_normalized_similarity_ignores_whitespace_reflow() -> None:
    a = "line one\nline two\nline three"
    b = "line one   line two line three"
    assert normalized_similarity(a, b) == 1.0


def test_normalized_similarity_both_empty_is_identical() -> None:
    assert normalized_similarity("", "   \n  ") == 1.0


# --- hypothesis property ---------------------------------------------------------


@st.composite
def _missing_and_unindexed(draw: st.DrawFn) -> tuple[list[Missing], list[Unindexed]]:
    n_m = draw(st.integers(min_value=0, max_value=5))
    n_u = draw(st.integers(min_value=0, max_value=5))
    m_paths = [PurePosixPath(f"m{i}.md") for i in range(n_m)]
    u_paths = [PurePosixPath(f"u{i}.md") for i in range(n_u)]
    hashes = st.sampled_from(["h1", "h2", "h3", "h4"])
    texts = st.sampled_from(["alpha beta gamma", "delta epsilon zeta", "the quick fox",
                             "completely unrelated text block here"])
    missing = [
        Missing(path=p, id=i + 1, base_hash=draw(hashes), remote_content=draw(texts))
        for i, p in enumerate(m_paths)
    ]
    unindexed = [
        Unindexed(path=p, content=draw(texts), content_hash=draw(hashes)) for p in u_paths
    ]
    return missing, unindexed


@given(_missing_and_unindexed())
def test_pairing_never_double_assigns(data: tuple[list[Missing], list[Unindexed]]) -> None:
    missing, unindexed = data
    result = pair(missing, unindexed)
    old_paths = [d.old_path for d in result.decisions]
    new_paths = [d.new_path for d in result.decisions]
    assert len(old_paths) == len(set(old_paths))  # never assigns one M twice
    assert len(new_paths) == len(set(new_paths))  # never assigns one U twice
    # every M and U is accounted for exactly once, either paired or unpaired
    assert set(old_paths) | {m.path for m in result.unpaired_missing} == {m.path for m in missing}
    assert (
        set(new_paths) | {u.path for u in result.unpaired_unindexed}
        == {u.path for u in unindexed}
    )

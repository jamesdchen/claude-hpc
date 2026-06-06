"""Tests for the #294 PR1 checkpoint-aware recovery helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.experiment_kit import checkpoint as ck


@pytest.fixture(autouse=True)
def _reset_interval_timer() -> None:
    ck._reset_should_checkpoint_state()


def test_write_read_round_trip(tmp_path: Path) -> None:
    state = {"weights": [1, 2, 3], "step": 7}
    path = ck.write_checkpoint(state, iteration=7, result_dir=tmp_path)
    assert path == tmp_path / "_checkpoints" / "checkpoint-7.pkl"
    assert path.is_file()
    assert ck.read_checkpoint(path) == state


def test_write_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    ck.write_checkpoint({"x": 1}, iteration=0, result_dir=tmp_path)
    leftovers = list((tmp_path / "_checkpoints").glob("*.tmp"))
    assert leftovers == []


def test_read_latest_fresh_run_returns_none_zero(tmp_path: Path) -> None:
    state, next_iter = ck.read_latest_checkpoint(result_dir=tmp_path)
    assert state is None
    assert next_iter == 0


def test_read_latest_picks_highest_iteration_and_next_index(tmp_path: Path) -> None:
    ck.write_checkpoint({"i": 2}, iteration=2, result_dir=tmp_path)
    ck.write_checkpoint({"i": 10}, iteration=10, result_dir=tmp_path)
    ck.write_checkpoint({"i": 5}, iteration=5, result_dir=tmp_path)
    state, next_iter = ck.read_latest_checkpoint(result_dir=tmp_path)
    assert state == {"i": 10}
    assert next_iter == 11  # resume AFTER the latest checkpointed iteration
    assert ck.latest_checkpoint(result_dir=tmp_path).name == "checkpoint-10.pkl"


def test_result_dir_resolves_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_RESULT_DIR", str(tmp_path))
    ck.write_checkpoint({"a": 1}, iteration=3)  # no explicit result_dir
    assert (tmp_path / "_checkpoints" / "checkpoint-3.pkl").is_file()
    state, next_iter = ck.read_latest_checkpoint()
    assert state == {"a": 1} and next_iter == 4


def test_empty_checkpoint_file_is_ignored(tmp_path: Path) -> None:
    ckdir = tmp_path / "_checkpoints"
    ckdir.mkdir()
    (ckdir / "checkpoint-0.pkl").write_bytes(b"")  # 0-byte (crashed mid-write, pre-atomic)
    assert ck.latest_checkpoint(result_dir=tmp_path) is None
    assert ck.read_latest_checkpoint(result_dir=tmp_path) == (None, 0)


def test_read_latest_skips_corrupt_newest(tmp_path: Path) -> None:
    ck.write_checkpoint({"good": 1}, iteration=1, result_dir=tmp_path)
    # A newer but unreadable checkpoint must not force a from-scratch restart.
    (tmp_path / "_checkpoints" / "checkpoint-2.pkl").write_bytes(b"\x80\x05 not a pickle")
    state, next_iter = ck.read_latest_checkpoint(result_dir=tmp_path)
    assert state == {"good": 1}
    assert next_iter == 2


def test_checkpoint_iteration_parsing() -> None:
    assert ck.checkpoint_iteration("checkpoint-42.pkl") == 42
    assert ck.checkpoint_iteration("/a/b/_checkpoints/checkpoint-0.pkl") == 0
    assert ck.checkpoint_iteration("metrics.json") is None


def test_should_checkpoint_walltime_margin_no_deadline_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HPC_WALLTIME_END_EPOCH", raising=False)
    assert ck.should_checkpoint(strategy="walltime_margin", margin_min=10) is False


def test_should_checkpoint_walltime_margin_fires_within_margin() -> None:
    # deadline 5 min out, margin 10 min → within margin → True.
    assert (
        ck.should_checkpoint(
            strategy="walltime_margin", margin_min=10, deadline_epoch=1000.0, _now_epoch=700.0
        )
        is True
    )
    # deadline 30 min out, margin 10 min → not yet → False.
    assert (
        ck.should_checkpoint(
            strategy="walltime_margin", margin_min=10, deadline_epoch=2500.0, _now_epoch=700.0
        )
        is False
    )


def test_should_checkpoint_walltime_margin_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_WALLTIME_END_EPOCH", "1000")
    assert ck.should_checkpoint(strategy="walltime_margin", margin_min=10, _now_epoch=500.0) is True


def test_should_checkpoint_interval_arms_then_fires() -> None:
    # First call arms the timer (returns False so a loop skips iteration 0).
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=0.0) is False
    # Before the interval elapses → still False.
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=300.0) is False
    # After interval_min (10 min = 600s) → True, and re-arms.
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=600.0) is True
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=900.0) is False
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=1200.0) is True


def test_should_checkpoint_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="unknown checkpoint strategy"):
        ck.should_checkpoint(strategy="every-full-moon")


def test_public_reexport_from_experiment_kit() -> None:
    from hpc_agent import experiment_kit as ek

    for name in (
        "write_checkpoint",
        "read_checkpoint",
        "read_latest_checkpoint",
        "latest_checkpoint",
        "checkpoint_dir",
        "should_checkpoint",
    ):
        assert hasattr(ek, name), f"{name} not re-exported from experiment_kit"

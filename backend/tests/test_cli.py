"""Tests for the CLI explain command."""

from __future__ import annotations

import pytest

from backend.cli import main


def test_explain_valid_duplicate_scenario(capsys):
    """Explain a duplicate payment scenario."""
    ret = main(["explain", "--scenario", "ORD-S6-001"])
    assert ret == 0
    out = capsys.readouterr().out

    assert "ORD-S6-001" in out
    assert "DUPLICATE_PAYMENT" in out
    assert "REFUND_DUPLICATE" in out
    assert "ADVISORY ONLY" in out
    assert "S09_DRY_RUN" in out


def test_explain_valid_human_review_scenario(capsys):
    """Explain a human-review scenario."""
    ret = main(["explain", "--scenario", "ORD-S9-001"])
    assert ret == 0
    out = capsys.readouterr().out

    assert "ORD-S9-001" in out
    assert "HUMAN_REVIEW" in out
    assert "ESCALATE_HUMAN_REVIEW" in out
    assert "ADVISORY ONLY" in out


def test_explain_unknown_scenario(capsys):
    """Unknown scenario ID gives a clear error."""
    ret = main(["explain", "--scenario", "ORD-NONEXISTENT"])
    assert ret != 0
    err = capsys.readouterr().err
    assert "ORD-NONEXISTENT" in err
    assert "not found" in err


def test_explain_shows_resolved_state_and_intervention(capsys):
    """Output contains resolved state and intervention."""
    main(["explain", "--scenario", "ORD-S6-001"])
    out = capsys.readouterr().out

    assert "Resolved state:" in out
    assert "Intervention:" in out


def test_explain_identifies_ai_as_advisory_only(capsys):
    """Output explicitly identifies AI as advisory-only."""
    main(["explain", "--scenario", "ORD-S6-001"])
    out = capsys.readouterr().out

    assert "ADVISORY ONLY" in out
    assert "does not decide financial action" in out


def test_explain_shows_safety_and_dry_run(capsys):
    """Output exposes safety/dry-run information."""
    main(["explain", "--scenario", "ORD-S6-001"])
    out = capsys.readouterr().out

    assert "Safety Gate:" in out
    assert "S09_DRY_RUN" in out
    assert "Dry-run protection: ACTIVE" in out


def test_explain_no_money_movement(capsys):
    """Explain must never move money."""
    main(["explain", "--scenario", "ORD-S6-001"])
    out = capsys.readouterr().out

    assert "Money moved:     no" in out
    assert "Simulated:       True" in out


def test_explain_uses_existing_batch_dataset(capsys):
    """Explain uses the default dataset path."""
    ret = main(["explain", "--scenario", "ORD-S1-001"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "NORMAL_SUCCESS" in out

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_weekly_workflow_is_manual_only() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "weekly_inference.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "intentionally disabled" in workflow


def test_readme_uses_honest_split_language() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # README must disclose the temporal-only split strategy
    assert "year-based holdout" in readme
    # README must be explicit about geographic scope
    assert "four Southern California counties" in readme

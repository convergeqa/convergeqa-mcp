"""Tests for the unified convergeqa-mcp dispatcher and registry metadata."""


import json
import re
from pathlib import Path

import pytest

from convergeqa_mcp import dispatch

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_dispatch_rejects_missing_argument(monkeypatch, capsys):
    assert dispatch.main([]) == 2
    assert "usage: convergeqa-mcp" in capsys.readouterr().err


def test_dispatch_rejects_unknown_argument(capsys):
    assert dispatch.main(["everything"]) == 2
    assert "usage: convergeqa-mcp" in capsys.readouterr().err


def test_dispatch_rejects_extra_arguments(capsys):
    assert dispatch.main(["reviews", "compare"]) == 2
    assert "usage: convergeqa-mcp" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["reviews", "compare"])
def test_dispatch_routes_to_server_main(monkeypatch, target):
    calls = []
    monkeypatch.setitem(dispatch._SERVERS, target, lambda: calls.append(target) or 0)
    assert dispatch.main([target]) == 0
    assert calls == [target]


def test_dispatch_targets_are_the_real_server_mains():
    # Real stdio behavior is covered by test_stdio_smoke.py; here we pin that
    # the dispatcher routes to exactly the same entry points as the dedicated
    # convergeqa-mcp-reviews / convergeqa-mcp-compare console scripts.
    from convergeqa_mcp import compare_due_diligence_mcp, service_account_reviews_mcp

    assert dispatch._SERVERS["reviews"] is service_account_reviews_mcp.main
    assert dispatch._SERVERS["compare"] is compare_due_diligence_mcp.main


def _load_registry_entries():
    return {
        path.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (REPO_ROOT / "registry").glob("*/server.json")
    }


def test_registry_entries_match_package_metadata():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    entries = _load_registry_entries()
    assert set(entries) == {"reviews", "compare"}
    for key, entry in entries.items():
        assert entry["name"] == f"io.github.convergeqa/{key}"
        assert entry["version"] == package_version
        package = entry["packages"][0]
        assert package["identifier"] == "convergeqa-mcp"
        assert package["version"] == package_version
        assert package["packageArguments"] == [{"type": "positional", "value": key}]
        # PyPI ownership verification requires the mcp-name marker in the README.
        assert f"mcp-name: {entry['name']}" in readme

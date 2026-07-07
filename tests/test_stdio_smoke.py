import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_stdio_module(module_name, expected_tools):
    launcher = shutil.which("py")
    command = [launcher, "-m", module_name] if launcher else [os.sys.executable, "-m", module_name]
    env = os.environ.copy()
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    payload = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "",
        ]
    )

    result = subprocess.run(
        command,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=10,
        check=True,
    )

    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["result"]["protocolVersion"] == "2024-11-05"
    names = {tool["name"] for tool in lines[1]["result"]["tools"]}
    assert expected_tools <= names
    assert result.stderr == ""


def test_service_account_reviews_stdio_roundtrip():
    _run_stdio_module(
        "convergeqa_mcp.service_account_reviews_mcp",
        {
            "convergeqa_critique_start",
            "convergeqa_critique_status",
            "convergeqa_iterate_start",
            "convergeqa_iterate_status",
        },
    )


def test_compare_stdio_roundtrip():
    _run_stdio_module(
        "convergeqa_mcp.compare_due_diligence_mcp",
        {
            "convergeqa_compare_submit",
            "convergeqa_compare_status",
            "convergeqa_compare_packet",
            "convergeqa_compare_bundle",
            "convergeqa_compare_run",
        },
    )

import ast
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

import convergeqa_mcp.service_account_reviews_cli as cli
import convergeqa_mcp.service_account_reviews_mcp as mcp


ROOT = Path(__file__).resolve().parents[1]


def _case_dir(name):
    path = ROOT / ".tmp" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class FakeTransport:
    def __init__(self, *, packet=None):
        self.requests = []
        self.downloads = []
        self.packet = packet or _packet("critique")

    def request_json(self, method, url, *, headers, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if method == "POST" and url.endswith("/start"):
            mode = "iterate" if "/iterate/" in url else "critique"
            return {"ok": True, "mode": mode, "session_id": f"{mode}-session-1", "job_id": f"{mode}-job-1", "status": "queued"}
        if method == "GET" and url.endswith("/sessions"):
            return [{"session_id": "review-session-1", "status": "active", "mode": "critique"}]
        if method == "GET" and url.endswith("/templates"):
            return {"templates": {"custom-review": {"description": "Custom review lens"}}}
        if method == "GET" and url.endswith("/packet"):
            return self.packet
        if method == "GET" and "/status/" in url:
            return {"job_id": url.rsplit("/", 1)[-1], "mode": "critique", "status": "complete", "session_id": "critique-session-1"}
        if method == "GET":
            return self.packet
        if method == "POST" and url.endswith("/decide"):
            return {"ok": True, "session_id": "critique-session-1", "status": "completed"}
        raise AssertionError(f"unexpected request {method} {url}")

    def download(self, url, *, headers, destination):
        self.downloads.append({"url": url, "headers": headers, "destination": destination})
        destination.write_bytes(b"PK\x03\x04zip")
        return destination


def _packet(mode):
    return {
        "schema_version": f"convergeqa.{mode}.agent_packet.v1",
        "mode": mode,
        "session_id": f"{mode}-session-1",
        "status": "active",
        "submitted_by": {"type": "service_account", "credential_prefix": "cqa_sa_one"},
    }


def _factory(transport):
    return lambda: transport


def _content(response):
    text = response["result"]["content"][0]["text"]
    return json.loads(text)


def _call(name, arguments, transport):
    return mcp.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        transport_factory=_factory(transport),
    )


@pytest.fixture(autouse=True)
def _isolate_convergeqa_credentials(monkeypatch):
    monkeypatch.delenv("CONVERGEQA_API_KEY", raising=False)
    monkeypatch.delenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", raising=False)


def test_initialize_and_tools_list_shape():
    init = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    tools = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert init["result"]["protocolVersion"] == mcp.MCP_PROTOCOL_VERSION
    assert init["result"]["capabilities"]["tools"] == {}
    names = {tool["name"] for tool in tools["result"]["tools"]}
    assert {
        "convergeqa_critique_start",
        "convergeqa_critique_status",
        "convergeqa_critique_packet",
        "convergeqa_critique_session",
        "convergeqa_critique_sessions",
        "convergeqa_critique_templates",
        "convergeqa_critique_decide",
        "convergeqa_critique_export",
        "convergeqa_iterate_start",
        "convergeqa_iterate_status",
        "convergeqa_iterate_packet",
        "convergeqa_iterate_session",
        "convergeqa_iterate_sessions",
        "convergeqa_iterate_templates",
        "convergeqa_iterate_decide",
        "convergeqa_iterate_export",
    } <= names
    start = next(tool for tool in tools["result"]["tools"] if tool["name"] == "convergeqa_critique_start")
    assert "max_spend_usd" not in start["inputSchema"]["required"]
    assert "model_tier" in start["inputSchema"]["required"]
    assert "service_account_key_env" in start["inputSchema"]["properties"]
    assert "additional_instructions" in start["inputSchema"]["properties"]
    assert start["inputSchema"]["properties"]["model_tier"]["enum"] == sorted(cli.VALID_SERVICE_ACCOUNT_MODEL_TIERS)
    assert "service-secret" not in json.dumps(tools)


def test_start_delegates_to_cli_contract_without_secret_or_prompt_leak(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")

    response = _call(
        "convergeqa_critique_start",
        {
            "prompt": "Synthetic prompt that must not be returned",
            "models": ["m1"],
            "max_spend_usd": "0.20",
            "model_tier": "budget",
            "idempotency_key": "caller-idem",
            "base_url": "https://example.test",
            "credential_kind": "service-account",
            "reference_material": "Reference material",
            "additional_instructions": "Use approved guidance.",
        },
        transport,
    )

    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://example.test/api/v1/critique/start"
    assert request["headers"] == {"X-Service-Account-Key": "service-secret", "Idempotency-Key": "caller-idem"}
    assert request["body"]["model_tier"] == "budget"
    assert request["body"]["max_spend_usd"] == "0.20"
    assert request["body"]["reference_material"] == "Reference material"
    assert request["body"]["additional_instructions"] == "Use approved guidance."
    assert request["body"]["extra_prompt"] == "Use approved guidance."
    serialized = json.dumps(response)
    assert _content(response)["job_id"] == "critique-job-1"
    assert "service-secret" not in serialized
    assert "Synthetic prompt" not in serialized


def test_templates_tool_lists_visible_templates_without_secret(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")

    response = _call(
        "convergeqa_critique_templates",
        {"base_url": "https://example.test", "credential_kind": "service-account"},
        transport,
    )

    request = transport.requests[0]
    assert request["method"] == "GET"
    assert request["url"] == "https://example.test/api/v1/templates"
    assert request["headers"] == {"X-Service-Account-Key": "service-secret"}
    assert _content(response)["templates"]["custom-review"]["description"] == "Custom review lens"
    assert "service-secret" not in json.dumps(response)


def test_start_requires_model_tier_before_transport(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")

    missing_tier = _call(
        "convergeqa_iterate_start",
        {"prompt": "Prompt", "models": ["m1"], "max_spend_usd": "0.20"},
        transport,
    )
    assert missing_tier["error"]["code"] == -32602
    assert "model_tier is required" in missing_tier["error"]["message"]
    assert transport.requests == []


def test_literal_credentials_and_bad_env_names_are_rejected_without_echo(monkeypatch):
    monkeypatch.setenv("SAFE_CQA_SERVICE_KEY", "service-secret")
    transport = FakeTransport()

    literal = _call(
        "convergeqa_critique_status",
        {"job_id": "crit-job-1", "service_account_key": "service-secret"},
        transport,
    )
    invalid_name = _call(
        "convergeqa_critique_status",
        {"job_id": "crit-job-1", "service_account_key_env": "lowercase_name"},
        transport,
    )
    safe_name = _call(
        "convergeqa_critique_status",
        {"job_id": "crit-job-1", "service_account_key_env": "SAFE_CQA_SERVICE_KEY", "base_url": "https://example.test"},
        transport,
    )

    combined = json.dumps([literal, invalid_name, safe_name])
    assert literal["error"]["code"] == -32602
    assert invalid_name["error"]["code"] == -32602
    assert safe_name["result"]
    assert "service-secret" not in combined
    assert "SAFE_CQA_SERVICE_KEY" not in combined


def test_status_packet_session_sessions_decide_and_export_paths(monkeypatch):
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")
    transport = FakeTransport(packet=_packet("iterate"))
    work = _case_dir("reviews_status_packet_session")
    packet_path = work / "packet.json"
    sessions_path = work / "sessions.json"
    bundle_path = work / "bundle.zip"

    service_account_args = {"credential_kind": "service-account"}
    status = _call("convergeqa_iterate_status", {"job_id": "iter-job-1", "base_url": "https://example.test", **service_account_args}, transport)
    packet = _call("convergeqa_iterate_packet", {"session_id": "iter-session-1", "base_url": "https://example.test", "output": str(packet_path), **service_account_args}, transport)
    session = _call("convergeqa_iterate_session", {"session_id": "iter-session-1", "base_url": "https://example.test", **service_account_args}, transport)
    sessions = _call("convergeqa_iterate_sessions", {"base_url": "https://example.test", "output": str(sessions_path), **service_account_args}, transport)
    decided = _call("convergeqa_iterate_decide", {"session_id": "iter-session-1", "base_url": "https://example.test", "action": "finish", "decisions": [{"rec_index": 0, "accepted": True}], **service_account_args}, transport)
    exported = _call("convergeqa_iterate_export", {"session_id": "iter-session-1", "base_url": "https://example.test", "output": str(bundle_path), **service_account_args}, transport)

    assert _content(status)["job_id"] == "iter-job-1"
    assert json.loads(packet_path.read_text(encoding="utf-8"))["session_id"] == "iterate-session-1"
    assert _content(packet)["packet_saved"] == str(packet_path)
    assert _content(session)["schema_version"] == "convergeqa.iterate.agent_packet.v1"
    assert json.loads(sessions_path.read_text(encoding="utf-8"))[0]["session_id"] == "review-session-1"
    assert _content(sessions)["sessions_saved"] == str(sessions_path)
    assert _content(decided)["status"] == "completed"
    assert bundle_path.read_bytes() == b"PK\x03\x04zip"
    assert _content(exported)["export_saved"] == str(bundle_path)
    urls = [request["url"] for request in transport.requests] + [download["url"] for download in transport.downloads]
    assert urls == [
        "https://example.test/api/v1/iterate/status/iter-job-1",
        "https://example.test/api/v1/iterate/iter-session-1/packet",
        "https://example.test/api/v1/iterate/iter-session-1",
        "https://example.test/api/v1/iterate/sessions",
        "https://example.test/api/v1/iterate/iter-session-1/decide",
        "https://example.test/api/v1/iterate/iter-session-1/export",
    ]
    assert all("X-API-Key" not in item["headers"] for item in transport.requests + transport.downloads)


def test_auto_uses_developer_api_key_from_service_account_env(monkeypatch):
    monkeypatch.delenv("CONVERGEQA_API_KEY", raising=False)
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "developer-api-secret")
    transport = FakeTransport()

    response = _call(
        "convergeqa_iterate_sessions",
        {"base_url": "https://example.test"},
        transport,
    )

    assert _content(response)["sessions"][0]["session_id"] == "review-session-1"
    assert transport.requests[0]["headers"] == {"X-API-Key": "developer-api-secret"}


def test_decide_allows_empty_decisions_array(monkeypatch):
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")
    transport = FakeTransport()

    response = _call(
        "convergeqa_critique_decide",
        {
            "session_id": "critique-session-1",
            "base_url": "https://example.test",
            "action": "finish",
            "decisions": [],
        },
        transport,
    )

    assert _content(response)["status"] == "completed"
    assert transport.requests[0]["body"] == {"action": "finish", "decisions": []}


def test_safe_error_message_trims_secret_like_tokens_with_whitespace():
    message = mcp._safe_error_message("service_account_key=secret-value leaked after space")

    assert "secret-value" not in message
    assert "service_account_key" in message


def test_safe_error_message_handles_empty_and_non_secret_session_text():
    assert mcp._safe_error_message("") == "MCP request failed"
    assert "session_id=abc123" in mcp._safe_error_message("session_id=abc123 failed")


def test_malformed_json_stdin_returns_parse_error_without_traceback():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = mcp.serve(stdin=io.StringIO("{bad json}\n"), stdout=stdout, stderr=stderr)

    assert exit_code == 0
    response = json.loads(stdout.getvalue())
    assert response["error"]["code"] == -32700
    assert "Parse error" in response["error"]["message"]
    assert stderr.getvalue() == ""
    assert "Traceback" not in stdout.getvalue()


def test_mcp_source_imports_cli_but_not_runtime_routes_or_helpers():
    tree = ast.parse(Path(mcp.__file__).read_text(encoding="utf-8"))
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)

    assert "convergeqa_mcp" in imported_names
    assert all(not name.startswith("routes.") for name in imported_names)
    assert "app" not in imported_names
    assert "flask" not in imported_names
    assert "psycopg2" not in imported_names


def test_mcp_import_has_no_runtime_modules_loaded():
    script = (
        "import sys; "
        "import convergeqa_mcp.service_account_reviews_mcp; "
        "bad=[m for m in sys.modules if m == 'app' or m == 'flask' or m == 'psycopg2' or m.startswith('routes.')]; "
        "print('|'.join(sorted(bad)))"
    )

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == ""

import ast
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

import convergeqa_mcp.compare_due_diligence_cli as cli
import convergeqa_mcp.compare_due_diligence_mcp as mcp


ROOT = Path(__file__).resolve().parents[1]


def _case_dir(name):
    path = ROOT / ".tmp" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class FakeTransport:
    def __init__(self, *, packet=None, fail_packet=False):
        self.requests = []
        self.downloads = []
        self.status_calls = 0
        self.packet = packet or _packet()
        self.fail_packet = fail_packet

    def request_json(self, method, url, *, headers, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if method == "POST":
            return _status("queued") | {"packet_url": None, "bundle_url": None}
        if url.endswith("/packet"):
            if self.fail_packet:
                return {**_packet(), "raw_model_output": "do not expose"}
            return self.packet
        self.status_calls += 1
        return _status("complete" if self.status_calls > 1 else "processing")

    def download(self, url, *, headers, destination):
        self.downloads.append({"url": url, "headers": headers, "destination": destination})
        destination.write_bytes(b"fake bundle bytes")
        return destination


class NeverCompleteTransport(FakeTransport):
    def request_json(self, method, url, *, headers, body=None):
        if method == "POST":
            return super().request_json(method, url, headers=headers, body=body)
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        return _status("processing")


def _status(status="complete"):
    return {
        "job_id": "ddc_fake",
        "mode": "compare",
        "status": status,
        "certificate_id": "CQA-API-COMPARE-FAKE",
        "packet_url": "https://example.test/api/v1/due-diligence/compare/ddc_fake/packet",
        "bundle_url": "https://example.test/api/v1/due-diligence/compare/ddc_fake/bundle",
        "cost": {"estimated": "0.0100", "actual": "0.0040", "signed_as_authoritative_billing_truth": False},
    }


def _packet():
    return {
        "packet_id": "pkt_fake",
        "schema_version": "convergeqa.compare.internal_packet.v1",
        "mode": "compare",
        "status": "completed",
        "outcome": "compare_process_recorded",
        "privacy": {
            "verify_visibility": "not_public_for_private_api_v1",
            "packet_visibility": "owner_authenticated_api",
        },
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


def test_initialize_and_tools_list_shape():
    init = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    tools = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert init["result"]["protocolVersion"] == mcp.MCP_PROTOCOL_VERSION
    assert init["result"]["capabilities"]["tools"] == {}
    names = {tool["name"] for tool in tools["result"]["tools"]}
    assert {
        "convergeqa_compare_submit",
        "convergeqa_compare_status",
        "convergeqa_compare_packet",
        "convergeqa_compare_bundle",
        "convergeqa_compare_run",
    } <= names
    submit = next(tool for tool in tools["result"]["tools"] if tool["name"] == "convergeqa_compare_submit")
    assert "max_spend_usd" not in submit["inputSchema"]["required"]
    assert submit["inputSchema"]["properties"]["credential_kind"]["enum"] == ["api-key", "service-account"]
    assert "service_account_key_env" in submit["inputSchema"]["properties"]
    assert "model_tier" in submit["inputSchema"]["properties"]
    assert submit["inputSchema"]["properties"]["model_tier"]["enum"] == sorted(
        cli.VALID_SERVICE_ACCOUNT_MODEL_TIERS
    )


def test_submit_delegates_to_cli_contract_with_caller_idempotency(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")

    response = _call(
        "convergeqa_compare_submit",
        {
            "prompt": "Synthetic prompt that must not be returned",
            "models": ["m1", "m2"],
            "max_spend_usd": "0.20",
            "idempotency_key": "caller-idem",
            "base_url": "https://example.test",
        },
        transport,
    )

    body = transport.requests[0]["body"]
    assert transport.requests[0]["headers"]["Idempotency-Key"] == "caller-idem"
    assert body["prompt"] == "Synthetic prompt that must not be returned"
    assert body["synthesize"] is True
    result = _content(response)
    assert result["idempotency_key"] == "caller-idem"
    assert "Synthetic prompt" not in json.dumps(response)
    assert "secret-value" not in json.dumps(response)


def test_submit_can_use_service_account_env_without_api_key_header(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_API_KEY", "api-secret")
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")

    response = _call(
        "convergeqa_compare_submit",
        {
            "credential_kind": "service-account",
            "prompt": "Synthetic prompt that must not be returned",
            "models": ["m1"],
            "max_spend_usd": "0.20",
            "model_tier": "budget",
            "idempotency_key": "caller-idem",
            "base_url": "https://example.test",
        },
        transport,
    )

    headers = transport.requests[0]["headers"]
    assert headers["X-Service-Account-Key"] == "service-secret"
    assert "X-API-Key" not in headers
    assert transport.requests[0]["body"]["model_tier"] == "budget"
    serialized = json.dumps(response)
    assert "service-secret" not in serialized
    assert "api-secret" not in serialized


def test_service_account_submit_requires_model_tier_before_transport(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_SERVICE_ACCOUNT_KEY", "service-secret")

    response = _call(
        "convergeqa_compare_submit",
        {
            "credential_kind": "service-account",
            "prompt": "Synthetic prompt that must not be returned",
            "models": ["m1"],
            "max_spend_usd": "0.20",
            "idempotency_key": "caller-idem",
            "base_url": "https://example.test",
        },
        transport,
    )

    assert response["error"]["code"] == -32000
    assert "model_tier" in response["error"]["message"]
    assert transport.requests == []


def test_submit_generates_idempotency_without_stderr_leak(monkeypatch, capsys):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")
    monkeypatch.setattr(cli, "generate_idempotency_key", lambda now=None: "cqa-cli-20260521T200000Z-abcdef123456")

    response = _call(
        "convergeqa_compare_submit",
        {"prompt": "No leak prompt", "models": ["m1"], "max_spend_usd": "0.20"},
        transport,
    )

    result = _content(response)
    assert result["idempotency_key"] == "cqa-cli-20260521T200000Z-abcdef123456"
    assert result["idempotency_key_generated"] is True
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "No leak prompt" not in captured.out + captured.err
    assert "secret-value" not in captured.out + captured.err


def test_submit_can_omit_max_spend(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")

    response = _call(
        "convergeqa_compare_submit",
        {"prompt": "Prompt", "models": ["m1"]},
        transport,
    )

    assert _content(response)["job_id"] == "ddc_fake"
    assert "max_spend_usd" not in transport.requests[0]["body"]


def test_status_packet_and_bundle_use_owner_scoped_cli_paths(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")
    work = _case_dir("compare_status_packet_bundle")
    packet_path = work / "packet.json"
    bundle_path = work / "bundle.zip"

    status = _call("convergeqa_compare_status", {"job_id": "ddc_fake", "base_url": "https://example.test"}, transport)
    packet = _call("convergeqa_compare_packet", {"job_id": "ddc_fake", "base_url": "https://example.test", "output": str(packet_path)}, transport)
    bundle = _call("convergeqa_compare_bundle", {"job_id": "ddc_fake", "base_url": "https://example.test", "output": str(bundle_path)}, transport)

    assert _content(status)["job_id"] == "ddc_fake"
    assert json.loads(packet_path.read_text(encoding="utf-8"))["packet_id"] == "pkt_fake"
    assert _content(packet)["packet_saved"] == str(packet_path)
    assert bundle_path.read_bytes() == b"fake bundle bytes"
    assert _content(bundle)["bundle_saved"] == str(bundle_path)
    urls = [request["url"] for request in transport.requests] + [download["url"] for download in transport.downloads]
    assert "https://example.test/api/v1/due-diligence/compare/ddc_fake" in urls
    assert "https://example.test/api/v1/due-diligence/compare/ddc_fake/packet" in urls
    assert "https://example.test/api/v1/due-diligence/compare/ddc_fake/bundle" in urls


def test_run_happy_path_is_sanitized_and_writes_outputs(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    work = _case_dir("compare_run_happy")
    packet_path = work / "packet.json"
    bundle_path = work / "bundle.zip"

    response = _call(
        "convergeqa_compare_run",
        {
            "prompt": "Synthetic run prompt should not be returned",
            "models": ["m1"],
            "max_spend_usd": "0.20",
            "packet_output": str(packet_path),
            "bundle_output": str(bundle_path),
            "timeout_seconds": 5,
            "poll_interval_seconds": 1,
        },
        transport,
    )

    result = _content(response)
    assert result["packet"]["packet_id"] == "pkt_fake"
    assert packet_path.exists()
    assert bundle_path.read_bytes() == b"fake bundle bytes"
    serialized = json.dumps(response)
    assert "Synthetic run prompt" not in serialized
    assert "secret-value" not in serialized


def test_run_timeout_and_private_field_failures_are_safe(monkeypatch):
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    timeout_response = _call(
        "convergeqa_compare_run",
        {"prompt": "Timeout prompt", "models": ["m1"], "max_spend_usd": "0.20", "timeout_seconds": 0},
        NeverCompleteTransport(),
    )
    leak_response = _call(
        "convergeqa_compare_run",
        {"prompt": "Private field prompt", "models": ["m1"], "max_spend_usd": "0.20", "timeout_seconds": 5},
        FakeTransport(fail_packet=True),
    )

    combined = json.dumps([timeout_response, leak_response])
    assert timeout_response["error"]["code"] == -32000
    assert leak_response["error"]["code"] == -32000
    assert "Timeout prompt" not in combined
    assert "Private field prompt" not in combined
    assert "raw_model_output" in leak_response["error"]["message"]
    assert "do not expose" not in combined


def test_unknown_method_unknown_tool_and_missing_arguments_are_safe():
    unknown_method = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "bad/method"})
    unknown_tool = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bad_tool", "arguments": {}}})
    missing_args = mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "convergeqa_compare_status"}})

    assert unknown_method["error"]["code"] == -32601
    assert unknown_tool["error"]["code"] == -32601
    assert missing_args["error"]["code"] == -32602
    assert "Traceback" not in json.dumps([unknown_method, unknown_tool, missing_args])


def test_malformed_json_stdin_returns_parse_error_without_traceback(capsys):
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = mcp.serve(stdin=io.StringIO("{bad json}\n"), stdout=stdout, stderr=stderr)

    assert exit_code == 0
    response = json.loads(stdout.getvalue())
    assert response["error"]["code"] == -32700
    assert "Parse error" in response["error"]["message"]
    assert stderr.getvalue() == ""
    assert "Traceback" not in stdout.getvalue()


def test_api_key_env_accepts_only_name_and_never_literal_value(monkeypatch):
    monkeypatch.setenv("SAFE_CQA_KEY", "secret-value")

    literal = _call(
        "convergeqa_compare_status",
        {"job_id": "ddc_fake", "api_key": "secret-value"},
        FakeTransport(),
    )
    invalid_name = _call(
        "convergeqa_compare_status",
        {"job_id": "ddc_fake", "api_key_env": "not allowed=secret-value"},
        FakeTransport(),
    )
    safe_name = _call(
        "convergeqa_compare_status",
        {"job_id": "ddc_fake", "api_key_env": "SAFE_CQA_KEY"},
        FakeTransport(),
    )

    combined = json.dumps([literal, invalid_name, safe_name])
    assert literal["error"]["code"] == -32602
    assert invalid_name["error"]["code"] == -32602
    assert safe_name["result"]
    assert "secret-value" not in combined
    assert "SAFE_CQA_KEY" not in combined


def test_service_account_env_accepts_only_uppercase_name_and_never_literal_value(monkeypatch):
    monkeypatch.setenv("SAFE_CQA_SERVICE_KEY", "service-secret")

    literal = _call(
        "convergeqa_compare_status",
        {"job_id": "ddc_fake", "credential_kind": "service-account", "service_account_key": "service-secret"},
        FakeTransport(),
    )
    invalid_name = _call(
        "convergeqa_compare_status",
        {"job_id": "ddc_fake", "credential_kind": "service-account", "service_account_key_env": "lowercase_name"},
        FakeTransport(),
    )
    safe_name = _call(
        "convergeqa_compare_status",
        {"job_id": "ddc_fake", "credential_kind": "service-account", "service_account_key_env": "SAFE_CQA_SERVICE_KEY"},
        FakeTransport(),
    )

    combined = json.dumps([literal, invalid_name, safe_name])
    assert literal["error"]["code"] == -32602
    assert invalid_name["error"]["code"] == -32602
    assert safe_name["result"]
    assert "service-secret" not in combined
    assert "SAFE_CQA_SERVICE_KEY" not in combined


def test_service_account_submit_rejects_literal_credential_even_with_model_tier(monkeypatch):
    monkeypatch.setenv("SAFE_CQA_SERVICE_KEY", "service-secret")
    transport = FakeTransport()

    response = _call(
        "convergeqa_compare_submit",
        {
            "credential_kind": "service-account",
            "service_account_key": "service-secret",
            "prompt": "Synthetic prompt that must not be returned",
            "models": ["m1"],
            "max_spend_usd": "0.20",
            "model_tier": "budget",
        },
        transport,
    )

    serialized = json.dumps(response)
    assert response["error"]["code"] == -32602
    assert transport.requests == []
    assert "service-secret" not in serialized


def test_output_path_uses_cli_like_direct_write_semantics(monkeypatch):
    monkeypatch.setenv("CONVERGEQA_API_KEY", "secret-value")
    work = _case_dir("compare_output_path")
    packet_path = work / "existing.json"
    packet_path.write_text("old", encoding="utf-8")

    response = _call(
        "convergeqa_compare_packet",
        {"job_id": "ddc_fake", "output": str(packet_path)},
        FakeTransport(),
    )

    assert _content(response)["packet_saved"] == str(packet_path)
    assert json.loads(packet_path.read_text(encoding="utf-8"))["packet_id"] == "pkt_fake"


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
    assert "compare_packet_mapping" not in imported_names
    assert "compare_packet_service" not in imported_names


def test_mcp_import_has_no_runtime_modules_loaded():
    script = (
        "import sys; "
        "import convergeqa_mcp.compare_due_diligence_mcp; "
        "bad=[m for m in sys.modules if m == 'app' or m == 'flask' or m == 'psycopg2' or m.startswith('routes.')]; "
        "print('|'.join(sorted(bad)))"
    )

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == ""


def test_public_readme_uses_safe_claims_and_no_literal_secrets():
    doc = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    lowered = doc.lower()

    assert "multi-model document review" in lowered
    assert "responsible individual decides" in lowered
    assert "reviews consume paid credits" in lowered
    assert "never stores keys" in lowered
    assert "verified truth" not in lowered
    assert "sk-" not in doc
    assert "Bearer " not in doc
    assert "BEGIN PRIVATE KEY" not in doc

"""MCP stdio wrapper for the supported ConvergeQA Compare CLI contract.

This module is agent-client tooling only. It is not imported by Flask startup,
route modules, auth/session code, billing code, or signed-report runtime
helpers. It intentionally delegates request semantics to
compare_due_diligence_cli so MCP does not become a second client contract.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from convergeqa_mcp import compare_due_diligence_cli as cli


JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_API_KEY_ENV = "CONVERGEQA_API_KEY"
DEFAULT_SERVICE_ACCOUNT_KEY_ENV = "CONVERGEQA_SERVICE_ACCOUNT_KEY"
API_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SERVICE_ACCOUNT_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class McpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _tool_schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


COMMON_PROPERTIES = {
    "base_url": {"type": "string", "description": "ConvergeQA base URL.", "default": cli.DEFAULT_BASE_URL},
    "api_key_env": {
        "type": "string",
        "description": "Environment variable name containing the ConvergeQA API key.",
        "default": DEFAULT_API_KEY_ENV,
    },
    "service_account_key_env": {
        "type": "string",
        "description": "Environment variable name containing the ConvergeQA service-account key.",
        "default": DEFAULT_SERVICE_ACCOUNT_KEY_ENV,
    },
    "credential_kind": {
        "type": "string",
        "enum": ["api-key", "service-account"],
        "default": "api-key",
    },
    "redact_private_urls": {"type": "boolean", "default": True},
}

SUBMIT_PROPERTIES = {
    **COMMON_PROPERTIES,
    "prompt": {"type": "string", "description": "Synthetic or user-approved text to review. Not returned."},
    "prompt_file": {"type": "string", "description": "Local file path containing text to review. Not returned."},
    "models": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "max_spend_usd": {"type": "string", "description": "Optional advisory spend cap for this request."},
    "model_tier": {
        "type": "string",
        "description": "Required for service-account requests; omit for api-key credentials.",
        "enum": sorted(cli.VALID_SERVICE_ACCOUNT_MODEL_TIERS),
    },
    "idempotency_key": {"type": "string"},
    "system_prompt": {"type": "string", "default": ""},
    "document_name": {"type": "string", "default": "Agent Compare Review"},
    "template": {"type": "string", "default": "Agent Due Diligence"},
    "intended_use": {"type": "string", "default": "Agent-submitted Compare due-diligence review."},
    "synthesize": {"type": "boolean", "default": True},
    "synthesis_model": {"type": "string"},
}

JOB_PROPERTIES = {
    **COMMON_PROPERTIES,
    "job_id": {"type": "string"},
}

TOOLS = [
    _tool_schema(
        "convergeqa_compare_submit",
        "Submit a private authenticated Compare due-diligence job.",
        SUBMIT_PROPERTIES,
        ["models"],
    ),
    _tool_schema(
        "convergeqa_compare_status",
        "Fetch owner-scoped Compare due-diligence job status.",
        JOB_PROPERTIES,
        ["job_id"],
    ),
    _tool_schema(
        "convergeqa_compare_packet",
        "Fetch owner-authenticated packet JSON and optionally write it to a caller-selected path.",
        {**JOB_PROPERTIES, "output": {"type": "string"}},
        ["job_id"],
    ),
    _tool_schema(
        "convergeqa_compare_bundle",
        "Download owner-authenticated Compare due-diligence bundle to a caller-selected path.",
        {**JOB_PROPERTIES, "output": {"type": "string"}},
        ["job_id", "output"],
    ),
    _tool_schema(
        "convergeqa_compare_run",
        "Submit, poll, fetch packet, and optionally download packet/bundle artifacts.",
        {
            **SUBMIT_PROPERTIES,
            "packet_output": {"type": "string"},
            "bundle_output": {"type": "string"},
            "timeout_seconds": {"type": "integer", "default": 600},
            "poll_interval_seconds": {"type": "integer", "default": 5},
        },
        ["models"],
    ),
]


def _safe_error_message(message: str) -> str:
    redacted = cli.redact_private_urls(str(message))
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "<REDACTED>", redacted)
    redacted = re.sub(r"(?i)(api[_ -]?key|password|cookie|session)[^\\s,;]*", r"\1", redacted)
    return redacted.splitlines()[0][:1000] or "MCP request failed"


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": {"code": code, "message": _safe_error_message(message)}}


def _tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
        "isError": False,
    }


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpError(-32602, f"{label} must be an object")
    return value


def _require_arg(args: dict[str, Any], name: str) -> Any:
    value = args.get(name)
    if value in (None, "", []):
        raise McpError(-32602, f"{name} is required")
    return value


def _env_name(args: dict[str, Any], arg_name: str, default: str, pattern: re.Pattern[str]) -> str:
    name = str(args.get(arg_name) or default)
    if not pattern.fullmatch(name):
        raise McpError(-32602, f"{arg_name} must be an environment variable name")
    return name


def _credential_kind(args: dict[str, Any]) -> str:
    kind = str(args.get("credential_kind") or "api-key")
    if kind not in cli.VALID_CREDENTIAL_KINDS:
        raise McpError(-32602, "credential_kind must be api-key or service-account")
    return kind


def _credentials(args: dict[str, Any]) -> dict[str, str]:
    if {"api_key", "x_api_key", "service_account_key", "x_service_account_key"} & set(args):
        raise McpError(-32602, "literal credentials are not accepted; use environment variable names")
    kind = _credential_kind(args)
    if kind == "service-account":
        key = os.environ.get(
            _env_name(args, "service_account_key_env", DEFAULT_SERVICE_ACCOUNT_KEY_ENV, SERVICE_ACCOUNT_ENV_NAME_RE),
            "",
        )
        if not key:
            raise McpError(-32001, "ConvergeQA service-account key environment variable is not set")
        return {"credential_kind": "service-account", "api_key": "", "service_account_key": key}
    key = os.environ.get(_env_name(args, "api_key_env", DEFAULT_API_KEY_ENV, API_ENV_NAME_RE), "")
    if not key:
        raise McpError(-32001, "ConvergeQA API key environment variable is not set")
    return {"credential_kind": "api-key", "api_key": key, "service_account_key": ""}


def _prompt(args: dict[str, Any]) -> str:
    if args.get("prompt"):
        return str(args["prompt"])
    if args.get("prompt_file"):
        return Path(str(args["prompt_file"])).read_text(encoding="utf-8-sig")
    raise McpError(-32602, "prompt or prompt_file is required")


def _transport(factory: Callable[[], cli.HttpTransport]) -> cli.HttpTransport:
    return factory()


def _submit_args(args: dict[str, Any]) -> dict[str, Any]:
    models = _require_arg(args, "models")
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
        raise McpError(-32602, "models must be an array of model ids")
    return {
        "base_url": str(args.get("base_url") or cli.DEFAULT_BASE_URL),
        **_credentials(args),
        "idempotency_key": args.get("idempotency_key"),
        "prompt": _prompt(args),
        "models": models,
        "max_spend_usd": str(args.get("max_spend_usd") or "") or None,
        "model_tier": str(args.get("model_tier") or ""),
        "system_prompt": str(args.get("system_prompt") or ""),
        "document_name": str(args.get("document_name") or "Agent Compare Review"),
        "template": str(args.get("template") or "Agent Due Diligence"),
        "synthesize": bool(args.get("synthesize", True)),
        "synthesize_model": args.get("synthesis_model"),
        "intended_use": str(args.get("intended_use") or "Agent-submitted Compare due-diligence review."),
    }


def _redact(args: dict[str, Any]) -> bool:
    return bool(args.get("redact_private_urls", True))


def _int_arg(args: dict[str, Any], name: str, default: int) -> int:
    if name not in args or args.get(name) is None:
        return default
    return int(args[name])


def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    transport_factory: Callable[[], cli.HttpTransport] = cli.HttpTransport,
) -> dict[str, Any]:
    args = _require_mapping(arguments, "arguments")
    transport = _transport(transport_factory)
    try:
        if name == "convergeqa_compare_submit":
            stderr = io.StringIO()
            created, idem, generated = cli.submit_compare(
                transport=transport,
                stderr=stderr,
                **_submit_args(args),
            )
            return _tool_result(
                cli.summarize_status(
                    created,
                    idempotency_key=idem,
                    idempotency_key_generated=generated,
                    redact_urls=_redact(args),
                )
            )
        if name == "convergeqa_compare_status":
            job_id = str(_require_arg(args, "job_id"))
            status = cli.get_status(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                **_credentials(args),
                job_id=job_id,
            )
            return _tool_result(cli.summarize_status(status, redact_urls=_redact(args)))
        if name == "convergeqa_compare_packet":
            job_id = str(_require_arg(args, "job_id"))
            packet = cli.get_packet(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                **_credentials(args),
                job_id=job_id,
            )
            result = cli.summarize_status({"job_id": job_id}, packet, redact_urls=_redact(args))
            if args.get("output"):
                Path(str(args["output"])).write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
                result["packet_saved"] = str(args["output"])
            return _tool_result(result)
        if name == "convergeqa_compare_bundle":
            job_id = str(_require_arg(args, "job_id"))
            output = Path(str(_require_arg(args, "output")))
            path = cli.download_bundle(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                **_credentials(args),
                job_id=job_id,
                destination=output,
            )
            return _tool_result({"job_id": job_id, "bundle_saved": str(path)})
        if name == "convergeqa_compare_run":
            stderr = io.StringIO()
            submit = _submit_args(args)
            created, idem, generated = cli.submit_compare(transport=transport, stderr=stderr, **submit)
            job_id = created.get("job_id")
            if not job_id:
                raise cli.CompareApiError("submit response did not include job_id")
            final = cli.poll_until_complete(
                status_getter=lambda jid: cli.get_status(
                    transport=transport,
                    base_url=submit["base_url"],
                    api_key=submit["api_key"],
                    service_account_key=submit["service_account_key"],
                    credential_kind=submit["credential_kind"],
                    job_id=jid,
                ),
                job_id=job_id,
                timeout_seconds=_int_arg(args, "timeout_seconds", 600),
                interval_seconds=_int_arg(args, "poll_interval_seconds", 5),
            )
            if final.get("status") != "complete":
                raise cli.CompareApiError(f"Compare job ended with status {final.get('status')}: {final.get('error')}")
            packet = cli.get_packet(
                transport=transport,
                base_url=submit["base_url"],
                api_key=submit["api_key"],
                service_account_key=submit["service_account_key"],
                credential_kind=submit["credential_kind"],
                job_id=job_id,
            )
            result = cli.summarize_status(
                final,
                packet,
                idempotency_key=idem,
                idempotency_key_generated=generated,
                redact_urls=_redact(args),
            )
            if args.get("packet_output"):
                Path(str(args["packet_output"])).write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
                result["packet_saved"] = str(args["packet_output"])
            if args.get("bundle_output"):
                cli.download_bundle(
                    transport=transport,
                    base_url=submit["base_url"],
                    api_key=submit["api_key"],
                    service_account_key=submit["service_account_key"],
                    credential_kind=submit["credential_kind"],
                    job_id=job_id,
                    destination=Path(str(args["bundle_output"])),
                )
                result["bundle_saved"] = str(args["bundle_output"])
            return _tool_result(cli.redact_private_urls(result) if _redact(args) else result)
    except cli.CompareApiError as exc:
        raise McpError(-32000, str(exc)) from exc
    raise McpError(-32601, f"Unknown tool: {name}")


def handle_request(
    request: Any,
    *,
    transport_factory: Callable[[], cli.HttpTransport] = cli.HttpTransport,
) -> dict[str, Any]:
    req = _require_mapping(request, "request")
    request_id = req.get("id")
    method = req.get("method")
    try:
        if method == "initialize":
            return _response(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "convergeqa-compare-due-diligence-mcp", "version": "1.0.0"},
                },
            )
        if method == "tools/list":
            return _response(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = _require_mapping(req.get("params"), "params")
            name = str(_require_arg(params, "name"))
            arguments = _require_mapping(params.get("arguments", {}), "arguments")
            return _response(request_id, call_tool(name, arguments, transport_factory=transport_factory))
        raise McpError(-32601, f"Unknown method: {method}")
    except McpError as exc:
        return _error(request_id, exc.code, exc.message)


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_error(None, -32700, "Parse error")), file=stdout, flush=True)
            continue
        response = handle_request(request)
        print(json.dumps(response, sort_keys=True), file=stdout, flush=True)
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())

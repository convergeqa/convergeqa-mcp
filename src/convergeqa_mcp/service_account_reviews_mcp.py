"""MCP stdio wrapper for Critique/Iterate agent review CLI.

This module is agent-client tooling only. It is not imported by Flask startup,
route modules, auth/session code, billing code, or signed-report runtime
helpers. It delegates request semantics to service_account_reviews_cli so MCP
does not become a second client contract.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from convergeqa_mcp import service_account_reviews_cli as cli


JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SERVICE_ACCOUNT_KEY_ENV = cli.DEFAULT_SERVICE_ACCOUNT_KEY_ENV
DEFAULT_API_KEY_ENV = cli.DEFAULT_API_KEY_ENV
SERVICE_ACCOUNT_ENV_NAME_RE = cli.SERVICE_ACCOUNT_ENV_NAME_RE


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
        "description": "Uppercase environment variable name containing a ConvergeQA Developer API key.",
        "default": DEFAULT_API_KEY_ENV,
    },
    "service_account_key_env": {
        "type": "string",
        "description": "Uppercase environment variable name containing the ConvergeQA service-account key.",
        "default": DEFAULT_SERVICE_ACCOUNT_KEY_ENV,
    },
    "credential_kind": {
        "type": "string",
        "enum": sorted(cli.VALID_CREDENTIAL_KINDS),
        "default": "auto",
    },
    "redact_private_urls": {"type": "boolean", "default": True},
}

START_PROPERTIES = {
    **COMMON_PROPERTIES,
    "prompt": {"type": "string", "description": "Synthetic or user-approved text to review. Not returned."},
    "prompt_file": {"type": "string", "description": "Local file path containing text to review. Not returned."},
    "models": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "max_spend_usd": {"type": "string", "description": "Optional advisory spend cap for this service-account request."},
    "model_tier": {
        "type": "string",
        "description": "Required for agent review starts.",
        "enum": sorted(cli.VALID_SERVICE_ACCOUNT_MODEL_TIERS),
    },
    "idempotency_key": {"type": "string"},
    "synthesis_model": {"type": "string"},
    "document_name": {"type": "string", "default": "Agent Review"},
    "template": {"type": "string", "default": "General"},
    "system_prompt": {"type": "string", "default": ""},
    "extra_prompt": {"type": "string", "default": ""},
    "additional_instructions": {"type": "string", "default": ""},
    "reference_material": {"type": "string", "default": ""},
}

JOB_PROPERTIES = {
    **COMMON_PROPERTIES,
    "job_id": {"type": "string"},
}

SESSION_PROPERTIES = {
    **COMMON_PROPERTIES,
    "session_id": {"type": "string"},
}

DECIDE_PROPERTIES = {
    **SESSION_PROPERTIES,
    "action": {"type": "string", "enum": ["continue", "finish", "pause"], "default": "finish"},
    "decisions": {"type": "array", "items": {"type": "object"}, "default": []},
}


TOOLS: list[dict[str, Any]] = []
for mode in ("critique", "iterate"):
    TOOLS.extend(
        [
            _tool_schema(
                f"convergeqa_{mode}_start",
                f"Start a private authenticated {mode.title()} agent review.",
                START_PROPERTIES,
                ["models", "model_tier"],
            ),
            _tool_schema(
                f"convergeqa_{mode}_status",
                f"Fetch owner-scoped {mode.title()} job status.",
                JOB_PROPERTIES,
                ["job_id"],
            ),
            _tool_schema(
                f"convergeqa_{mode}_packet",
                f"Fetch owner-authenticated {mode.title()} packet JSON and optionally write it to a path.",
                {**SESSION_PROPERTIES, "output": {"type": "string"}},
                ["session_id"],
            ),
            _tool_schema(
                f"convergeqa_{mode}_session",
                f"Fetch owner-authenticated {mode.title()} session packet.",
                {**SESSION_PROPERTIES, "output": {"type": "string"}},
                ["session_id"],
            ),
            _tool_schema(
                f"convergeqa_{mode}_sessions",
                f"List owner-authenticated {mode.title()} sessions.",
                {**COMMON_PROPERTIES, "output": {"type": "string"}},
                [],
            ),
            _tool_schema(
                f"convergeqa_{mode}_templates",
                "List API-visible ConvergeQA templates for the configured credential.",
                {**COMMON_PROPERTIES, "output": {"type": "string"}},
                [],
            ),
            _tool_schema(
                f"convergeqa_{mode}_decide",
                f"Submit delegated {mode.title()} decisions.",
                DECIDE_PROPERTIES,
                ["session_id", "decisions"],
            ),
            _tool_schema(
                f"convergeqa_{mode}_export",
                f"Download owner-authenticated {mode.title()} export ZIP to a caller-selected path.",
                {**SESSION_PROPERTIES, "output": {"type": "string"}},
                ["session_id", "output"],
            ),
        ]
    )


def _safe_error_message(message: str) -> str:
    redacted = cli.redact_private_urls(str(message))
    redacted = re.sub(r"cqa_sa_[A-Za-z0-9_-]+", "<REDACTED>", redacted)
    redacted = re.sub(
        r"(?i)(service[_ -]?account[_ -]?key|api[_ -]?key|password|cookie|token|secret)[^\s,;]*",
        r"\1",
        redacted,
    )
    first_line = (redacted.splitlines() or [""])[0][:1000]
    return first_line or "MCP request failed"


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


def _mode_from_tool(name: str) -> tuple[str, str]:
    for mode in ("critique", "iterate"):
        prefix = f"convergeqa_{mode}_"
        if name.startswith(prefix):
            return mode, name.removeprefix(prefix)
    raise McpError(-32601, f"Unknown tool: {name}")


def _env_name(args: dict[str, Any], arg_name: str, default: str) -> str:
    name = str(args.get(arg_name) or default)
    if not SERVICE_ACCOUNT_ENV_NAME_RE.fullmatch(name):
        raise McpError(-32602, f"{arg_name} must be an uppercase environment variable name")
    return name


def _credential(args: dict[str, Any]) -> tuple[str, str]:
    if {"api_key", "x_api_key", "service_account_key", "x_service_account_key", "token", "password"} & set(args):
        raise McpError(-32602, "literal credentials are not accepted; use an environment variable name")
    try:
        return cli._resolve_credential(
            credential_kind=str(args.get("credential_kind") or "auto"),
            api_key=os.environ.get(_env_name(args, "api_key_env", DEFAULT_API_KEY_ENV), ""),
            service_account_key=os.environ.get(
                _env_name(args, "service_account_key_env", DEFAULT_SERVICE_ACCOUNT_KEY_ENV),
                "",
            ),
        )
    except cli.ReviewApiError as exc:
        raise McpError(-32001, str(exc)) from exc


def _prompt(args: dict[str, Any]) -> str:
    if args.get("prompt"):
        return str(args["prompt"])
    if args.get("prompt_file"):
        return Path(str(args["prompt_file"])).read_text(encoding="utf-8-sig")
    raise McpError(-32602, "prompt or prompt_file is required")


def _models(args: dict[str, Any]) -> list[str]:
    models = _require_arg(args, "models")
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
        raise McpError(-32602, "models must be an array of model ids")
    return models


def _redact(args: dict[str, Any]) -> bool:
    return bool(args.get("redact_private_urls", True))


def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    transport_factory: Callable[[], cli.HttpTransport] = cli.HttpTransport,
) -> dict[str, Any]:
    args = _require_mapping(arguments, "arguments")
    mode, operation = _mode_from_tool(name)
    transport = transport_factory()
    try:
        credential_kind, credential_value = _credential(args)
        if operation == "start":
            stderr = io.StringIO()
            created, idem, generated = cli.start_review(
                transport=transport,
                stderr=stderr,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                idempotency_key=args.get("idempotency_key"),
                prompt=_prompt(args),
                models=_models(args),
                max_spend_usd=str(args.get("max_spend_usd") or "") or None,
                model_tier=str(_require_arg(args, "model_tier")),
                synthesis_model=args.get("synthesis_model"),
                document_name=str(args.get("document_name") or "Agent Review"),
                template=str(args.get("template") or "General"),
                system_prompt=str(args.get("system_prompt") or ""),
                extra_prompt=str(args.get("extra_prompt") or ""),
                additional_instructions=str(args.get("additional_instructions") or ""),
                reference_material=str(args.get("reference_material") or ""),
            )
            return _tool_result(
                cli.summarize_response(
                    created,
                    mode=mode,
                    idempotency_key=idem,
                    idempotency_key_generated=generated,
                    redact_urls=_redact(args),
                )
            )
        if operation == "status":
            status = cli.get_status(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                job_id=str(_require_arg(args, "job_id")),
            )
            return _tool_result(cli.summarize_response(status, mode=mode, redact_urls=_redact(args)))
        if operation in {"packet", "session"}:
            packet = cli.get_session_or_packet(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                session_id=str(_require_arg(args, "session_id")),
                packet=operation == "packet",
            )
            result = cli.summarize_response(packet, mode=mode, redact_urls=_redact(args))
            if args.get("output"):
                Path(str(args["output"])).write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
                result["packet_saved"] = str(args["output"])
            return _tool_result(result)
        if operation == "sessions":
            sessions = cli.list_sessions(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
            )
            result = cli.summarize_response(sessions, mode=mode, redact_urls=_redact(args))
            if args.get("output"):
                sessions_payload = (
                    cli.redact_private_urls(sessions) if _redact(args) else sessions
                )
                Path(str(args["output"])).write_text(json.dumps(sessions_payload, indent=2, sort_keys=True), encoding="utf-8")
                result["sessions_saved"] = str(args["output"])
            return _tool_result(result)
        if operation == "templates":
            templates = cli.list_templates(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                credential_kind=credential_kind,
                credential_value=credential_value,
            )
            result = cli.redact_private_urls(templates) if _redact(args) else templates
            if args.get("output"):
                # Write the (possibly redacted) result so the redaction flag
                # also governs the file artifact, not just stdout.
                Path(str(args["output"])).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
                result["templates_saved"] = str(args["output"])
            return _tool_result(result)
        if operation == "decide":
            if "decisions" not in args or args.get("decisions") is None:
                raise McpError(-32602, "decisions is required")
            decisions = args.get("decisions")
            if not isinstance(decisions, list):
                raise McpError(-32602, "decisions must be an array")
            decided = cli.decide_review(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                session_id=str(_require_arg(args, "session_id")),
                action=str(args.get("action") or "finish"),
                decisions=decisions,
            )
            return _tool_result(cli.summarize_response(decided, mode=mode, redact_urls=_redact(args)))
        if operation == "export":
            output = Path(str(_require_arg(args, "output")))
            path = cli.export_review(
                transport=transport,
                base_url=str(args.get("base_url") or cli.DEFAULT_BASE_URL),
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                session_id=str(_require_arg(args, "session_id")),
                destination=output,
            )
            return _tool_result({"mode": mode, "session_id": str(args["session_id"]), "export_saved": str(path)})
    except cli.ReviewApiError as exc:
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
                    "serverInfo": {"name": "convergeqa-service-account-reviews-mcp", "version": "1.0.0"},
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

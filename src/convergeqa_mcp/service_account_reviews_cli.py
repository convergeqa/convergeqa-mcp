"""Agent CLI for private Critique and Iterate API v1 routes.

This tooling is inert production-tree client code. It is not imported by Flask
startup, route modules, auth/session code, billing code, or signed-report
runtime helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

try:
    from .packet_secret_scan import (
        scan_path_name_flags,
        scan_text_for_secret_flags,
    )
except ImportError:  # direct script invocation from tools/agent_clients/
    from packet_secret_scan import (
        scan_path_name_flags,
        scan_text_for_secret_flags,
    )


DEFAULT_BASE_URL = "https://convergeqa.net"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_API_KEY_ENV = "CONVERGEQA_API_KEY"
DEFAULT_SERVICE_ACCOUNT_KEY_ENV = "CONVERGEQA_SERVICE_ACCOUNT_KEY"
DEFAULT_USER_AGENT = "ConvergeQA-AgentClient/1.0"
VALID_MODES = {"critique", "iterate"}
VALID_COMMANDS = {"start", "status", "packet", "session", "sessions", "decide", "export", "templates", "models", "catalog"}
VALID_CREDENTIAL_KINDS = {"auto", "api-key", "service-account"}
VALID_SERVICE_ACCOUNT_MODEL_TIERS = {"budget", "premium"}
SERVICE_ACCOUNT_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SERVICE_ACCOUNT_KEY_PREFIX = "cqa_sa_"
PRIVATE_REVIEW_API_PATH_RE = re.compile(r"(/api/v1/(?:critique|iterate)/status/)[A-Za-z0-9._:-]+")
PRIVATE_SESSION_API_PATH_RE = re.compile(
    r"(/api/v1/(?:critique|iterate)/)(?!(?:start|sessions|status)(?:\b|/|$))[A-Za-z0-9._:-]+"
    r"(?=(?:/packet|/decide|/export)?(?:\b|/|$))"
)
SAFE_ERROR_FIELDS = {
    "error",
    "message",
    "estimated_cost",
    "max_spend_usd",
    "remaining_max_spend_usd",
    "spend_cap_scope",
    "reason_code",
    "artifact_status",
    "job_id",
    "session_id",
}
PRIVATE_PACKET_KEYS = {
    "api_key",
    "api_key_value",
    "cookie",
    "credential_hash",
    "password",
    "provider_diagnostics",
    "original_prompt",
    "raw_model_output",
    "raw_document",
    "raw_prompt",
    "service_account_key",
    "session_token",
    "source_document_text",
    "system_prompt",
}


class ReviewApiError(RuntimeError):
    """Raised for safe client-side or API errors."""


class HttpTransport:
    """Small stdlib HTTP transport so the supported CLI has no dependency."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        data = None
        request_headers = _transport_headers(headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReviewApiError(_http_error_message("API request failed", exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise ReviewApiError(f"API request failed: {exc.reason}") from exc
        return json.loads(payload or "{}")

    def download(
        self,
        url: str,
        *,
        headers: dict[str, str],
        destination: Path,
    ) -> Path:
        req = urllib.request.Request(url, headers=_transport_headers(headers), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                destination.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReviewApiError(_http_error_message("Export download failed", exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise ReviewApiError(f"Export download failed: {exc.reason}") from exc
        return destination


def _transport_headers(headers: dict[str, str]) -> dict[str, str]:
    request_headers = dict(headers)
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    return request_headers


def _http_error_message(prefix: str, status_code: int, body_text: str) -> str:
    safe = _safe_error_body(body_text)
    if status_code == 409 and safe.get("error") == "idempotency_conflict":
        safe["retry_guidance"] = "retry with a different idempotency key for a different request"
    return f"{prefix} with HTTP {status_code}: {json.dumps(safe, sort_keys=True)}"


def _safe_error_body(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": "unparseable_api_error"}
    if not isinstance(parsed, dict):
        return {"error": "unexpected_api_error_shape"}
    return {key: parsed.get(key) for key in SAFE_ERROR_FIELDS if key in parsed}


def _api_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _validate_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized not in VALID_MODES:
        raise ReviewApiError("mode must be critique or iterate")
    return normalized


def _validate_env_name(name: str) -> str:
    normalized = str(name or "")
    if not SERVICE_ACCOUNT_ENV_NAME_RE.fullmatch(normalized):
        raise ReviewApiError("service_account_key_env must be an uppercase environment variable name")
    return normalized


def _normalize_credential_kind(value: str) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized not in VALID_CREDENTIAL_KINDS:
        raise ReviewApiError("credential_kind must be auto, api-key, or service-account")
    return normalized


def _looks_like_service_account_key(value: str) -> bool:
    return str(value or "").startswith(SERVICE_ACCOUNT_KEY_PREFIX)


def _resolve_credential(
    *,
    credential_kind: str,
    api_key: str = "",
    service_account_key: str = "",
) -> tuple[str, str]:
    normalized_kind = _normalize_credential_kind(credential_kind)
    if normalized_kind == "api-key":
        if not api_key:
            raise ReviewApiError("CONVERGEQA_API_KEY is required")
        return "api-key", api_key
    if normalized_kind == "service-account":
        if not service_account_key:
            raise ReviewApiError("CONVERGEQA_SERVICE_ACCOUNT_KEY is required")
        return "service-account", service_account_key

    if service_account_key and _looks_like_service_account_key(service_account_key):
        return "service-account", service_account_key
    if api_key and not service_account_key:
        return "api-key", api_key
    if service_account_key and not api_key:
        # Compatibility path for Developer-page API keys pasted into the older
        # service-account env var during beta agent testing.
        return "api-key", service_account_key
    if api_key and service_account_key:
        raise ReviewApiError("Multiple ConvergeQA credentials are set; pass --credential-kind")
    raise ReviewApiError("CONVERGEQA_API_KEY or CONVERGEQA_SERVICE_ACCOUNT_KEY is required")


def _headers(
    *,
    credential_kind: str,
    credential_value: str,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    normalized_kind = _normalize_credential_kind(credential_kind)
    if normalized_kind == "auto":
        normalized_kind = (
            "service-account"
            if _looks_like_service_account_key(credential_value)
            else "api-key"
        )
    if not credential_value:
        if normalized_kind == "service-account":
            raise ReviewApiError("CONVERGEQA_SERVICE_ACCOUNT_KEY is required")
        raise ReviewApiError("CONVERGEQA_API_KEY is required")
    if normalized_kind == "service-account":
        headers = {"X-Service-Account-Key": credential_value}
    else:
        headers = {"X-API-Key": credential_value}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _normalize_model_tier(model_tier: str) -> str:
    normalized = str(model_tier or "").strip().lower()
    if not normalized:
        raise ReviewApiError("model_tier is required for service-account starts")
    if normalized not in VALID_SERVICE_ACCOUNT_MODEL_TIERS:
        raise ReviewApiError("model_tier must be budget or premium")
    return normalized


def generate_idempotency_key(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"cqa-sa-{timestamp}-{uuid.uuid4().hex[:12]}"


def redact_private_urls(value: Any) -> Any:
    if isinstance(value, str):
        value = PRIVATE_REVIEW_API_PATH_RE.sub(r"\1<REDACTED>", value)
        return PRIVATE_SESSION_API_PATH_RE.sub(r"\1<REDACTED>", value)
    if isinstance(value, list):
        return [redact_private_urls(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_private_urls(item) for key, item in value.items()}
    return value


def _emit_generated_key(idempotency_key: str, stderr: TextIO) -> None:
    print(
        f"Generated idempotency key: {idempotency_key}. Save it to retry this same request.",
        file=stderr,
        flush=True,
    )


def _resolve_idempotency_key(idempotency_key: str | None) -> tuple[str, bool]:
    if idempotency_key:
        return idempotency_key, False
    return generate_idempotency_key(), True


def _iter_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_iter_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_iter_keys(item))
    return found


def assert_packet_has_no_private_fields(packet: dict[str, Any]) -> None:
    # This is a structural key-name guard; it is not a content scanner.
    keys = {key.lower() for key in _iter_keys(packet)}
    leaked = sorted(keys & PRIVATE_PACKET_KEYS)
    if leaked:
        raise ReviewApiError(f"Packet failed private-field guard: {', '.join(leaked)}")


def _read_json_array(value: str | None, path: Path | None, *, label: str) -> list[Any]:
    if path:
        raw = path.read_text(encoding="utf-8-sig")
    else:
        raw = value or "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewApiError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise ReviewApiError(f"{label} must be a JSON array")
    return parsed


def start_review(
    *,
    transport: HttpTransport,
    base_url: str,
    mode: str,
    credential_kind: str,
    credential_value: str,
    idempotency_key: str | None,
    prompt: str,
    models: list[str],
    max_spend_usd: str | None = None,
    model_tier: str,
    stderr: TextIO = sys.stderr,
    synthesis_model: str | None = None,
    document_name: str = "Agent Review",
    template: str = "General",
    system_prompt: str = "",
    extra_prompt: str = "",
    reference_material: str = "",
    additional_instructions: str = "",
) -> tuple[dict[str, Any], str, bool]:
    normalized_mode = _validate_mode(mode)
    if not prompt:
        raise ReviewApiError("prompt is required")
    if not models:
        raise ReviewApiError("at least one model id is required")
    normalized_model_tier = _normalize_model_tier(model_tier)
    resolved_idempotency_key, generated = _resolve_idempotency_key(idempotency_key)
    body = {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "models": models,
        "synthesis_model": synthesis_model or models[0],
        "document_name": document_name,
        "template": template,
        "model_tier": normalized_model_tier,
    }
    if max_spend_usd:
        body["max_spend_usd"] = max_spend_usd
    if extra_prompt:
        body["extra_prompt"] = extra_prompt
    if additional_instructions:
        body["additional_instructions"] = additional_instructions
        if body.get("extra_prompt"):
            # The server consumes only extra_prompt; combine both so the
            # additional instructions are not silently dropped when an
            # extra prompt is also supplied.
            body["extra_prompt"] = (
                body["extra_prompt"] + "\n\n" + additional_instructions
            )
        else:
            body["extra_prompt"] = additional_instructions
    if reference_material:
        body["reference_material"] = reference_material
    if generated:
        _emit_generated_key(resolved_idempotency_key, stderr)
    response = transport.request_json(
        "POST",
        _api_url(base_url, f"/api/v1/{normalized_mode}/start"),
        headers=_headers(
            credential_kind=credential_kind,
            credential_value=credential_value,
            idempotency_key=resolved_idempotency_key,
        ),
        body=body,
    )
    if not isinstance(response, dict):
        raise ReviewApiError("start response did not return an object")
    return response, resolved_idempotency_key, generated


def get_status(
    *,
    transport: HttpTransport,
    base_url: str,
    mode: str,
    job_id: str,
    credential_kind: str,
    credential_value: str,
) -> dict[str, Any]:
    response = transport.request_json(
        "GET",
        _api_url(base_url, f"/api/v1/{_validate_mode(mode)}/status/{job_id}"),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
    )
    if not isinstance(response, dict):
        raise ReviewApiError("status response did not return an object")
    return response


def get_session_or_packet(
    *,
    transport: HttpTransport,
    base_url: str,
    mode: str,
    session_id: str,
    credential_kind: str,
    credential_value: str,
    packet: bool,
) -> dict[str, Any]:
    suffix = "/packet" if packet else ""
    response = transport.request_json(
        "GET",
        _api_url(base_url, f"/api/v1/{_validate_mode(mode)}/{session_id}{suffix}"),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
    )
    if not isinstance(response, dict):
        raise ReviewApiError("packet response did not return an object")
    assert_packet_has_no_private_fields(response)
    return response


def list_sessions(
    *,
    transport: HttpTransport,
    base_url: str,
    mode: str,
    credential_kind: str,
    credential_value: str,
) -> list[Any]:
    response = transport.request_json(
        "GET",
        _api_url(base_url, f"/api/v1/{_validate_mode(mode)}/sessions"),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
    )
    if not isinstance(response, list):
        raise ReviewApiError("sessions response did not return a list")
    return response


def decide_review(
    *,
    transport: HttpTransport,
    base_url: str,
    mode: str,
    session_id: str,
    credential_kind: str,
    credential_value: str,
    action: str,
    decisions: list[Any],
) -> dict[str, Any]:
    response = transport.request_json(
        "POST",
        _api_url(base_url, f"/api/v1/{_validate_mode(mode)}/{session_id}/decide"),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
        body={"action": action, "decisions": decisions},
    )
    if not isinstance(response, dict):
        raise ReviewApiError("decide response did not return an object")
    return response


def export_review(
    *,
    transport: HttpTransport,
    base_url: str,
    mode: str,
    session_id: str,
    credential_kind: str,
    credential_value: str,
    destination: Path,
) -> Path:
    return transport.download(
        _api_url(base_url, f"/api/v1/{_validate_mode(mode)}/{session_id}/export"),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
        destination=destination,
    )


def list_templates(
    *,
    transport: HttpTransport,
    base_url: str,
    credential_kind: str,
    credential_value: str,
) -> dict[str, Any]:
    response = transport.request_json(
        "GET",
        _api_url(base_url, "/api/v1/templates"),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
    )
    if not isinstance(response, dict) or not isinstance(response.get("templates"), dict):
        raise ReviewApiError("templates response did not return a template object")
    return response


def list_models(
    *,
    transport: HttpTransport,
    base_url: str,
    credential_kind: str,
    credential_value: str,
    catalog: bool = False,
) -> dict[str, Any]:
    path = "/api/v1/models/catalog" if catalog else "/api/v1/models"
    response = transport.request_json(
        "GET",
        _api_url(base_url, path),
        headers=_headers(credential_kind=credential_kind, credential_value=credential_value),
    )
    if not isinstance(response, dict):
        raise ReviewApiError("models response did not return an object")
    return response


def poll_until_complete(
    *,
    status_getter: Callable[[str], dict[str, Any]],
    job_id: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status: dict[str, Any] = {}
    while time.time() <= deadline:
        last_status = status_getter(job_id)
        if last_status.get("status") in {"complete", "completed", "failed", "timed_out"}:
            return last_status
        time.sleep(interval_seconds)
    raise ReviewApiError(f"Timed out waiting for job {job_id}; last status was {last_status.get('status')}")


def summarize_response(
    value: dict[str, Any] | list[Any],
    *,
    mode: str,
    idempotency_key: str | None = None,
    idempotency_key_generated: bool = False,
    redact_urls: bool = False,
) -> dict[str, Any]:
    if isinstance(value, list):
        summary: dict[str, Any] = {"mode": mode, "sessions": value}
    else:
        summary = {
            "mode": value.get("mode") or mode,
            "job_id": value.get("job_id"),
            "session_id": value.get("session_id"),
            "status": value.get("status"),
            "artifact_status": value.get("artifact_status"),
            "report_id": value.get("report_id"),
            "history_id": value.get("history_id"),
            "schema_version": value.get("schema_version"),
            "cost": value.get("cost"),
        }
        if "result" in value and isinstance(value["result"], dict):
            result = value["result"]
            summary["result"] = {
                "session_id": result.get("session_id"),
                "mode": result.get("mode"),
                "iteration": result.get("iteration"),
                "summary": result.get("summary"),
                "finding_count": result.get("finding_count"),
                "cost": result.get("cost"),
                "cumulative_cost": result.get("cumulative_cost"),
                "history_id": result.get("history_id"),
                "convergence": result.get("convergence"),
            }
    if idempotency_key:
        summary["idempotency_key"] = idempotency_key
        summary["idempotency_key_generated"] = bool(idempotency_key_generated)
    return redact_private_urls(summary) if redact_urls else summary


def _write_json(path: Path, value: dict[str, Any] | list[Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ConvergeQA Critique/Iterate agent API CLI.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--service-account-key-env", default=DEFAULT_SERVICE_ACCOUNT_KEY_ENV)
    parser.add_argument(
        "--credential-kind",
        choices=sorted(VALID_CREDENTIAL_KINDS),
        default="auto",
        help="Credential header to send. auto uses cqa_sa_* keys as service accounts; otherwise API keys.",
    )
    parser.add_argument("--mode", choices=sorted(VALID_MODES), required=True)
    parser.add_argument(
        "--redact-private-urls",
        action="store_true",
        help="Redact private API URLs in stdout and CLI error text.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--idempotency-key")
    start.add_argument("--prompt-file", type=Path, required=True)
    start.add_argument("--model", action="append", dest="models", required=True)
    start.add_argument("--max-spend-usd")
    start.add_argument("--model-tier", required=True)
    start.add_argument("--synthesis-model")
    start.add_argument("--document-name", default="Agent Review")
    start.add_argument("--template", default="General")
    start.add_argument("--system-prompt", default="")
    start.add_argument("--extra-prompt", default="")
    start.add_argument("--additional-instructions-file", type=Path)
    start.add_argument("--reference-material-file", type=Path)
    start.add_argument(
        "--allow-flagged-content",
        action="store_true",
        help="Bypass the secret/high-risk content preflight after manual redaction review.",
    )

    status = sub.add_parser("status")
    status.add_argument("job_id")

    packet = sub.add_parser("packet")
    packet.add_argument("session_id")
    packet.add_argument("--output", type=Path)

    session = sub.add_parser("session")
    session.add_argument("session_id")
    session.add_argument("--output", type=Path)

    sessions = sub.add_parser("sessions")
    sessions.add_argument("--output", type=Path)

    templates = sub.add_parser("templates")
    templates.add_argument("--output", type=Path)

    models = sub.add_parser("models")
    models.add_argument("--catalog", action="store_true")
    models.add_argument("--output", type=Path)

    catalog = sub.add_parser("catalog")
    catalog.add_argument("--output", type=Path)

    decide = sub.add_parser("decide")
    decide.add_argument("session_id")
    decide.add_argument("--action", choices=["continue", "finish", "pause"], default="finish")
    decide.add_argument("--decisions-json")
    decide.add_argument("--decisions-file", type=Path)

    export = sub.add_parser("export")
    export.add_argument("session_id")
    export.add_argument("--output", type=Path, required=True)
    return parser


def _print_summary(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _credential_from_env(name: str) -> str:
    _validate_env_name(name)
    return os.environ.get(name, "")


def _selected_credential(args: argparse.Namespace) -> tuple[str, str]:
    return _resolve_credential(
        credential_kind=args.credential_kind,
        api_key=_credential_from_env(args.api_key_env),
        service_account_key=_credential_from_env(args.service_account_key_env),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transport = HttpTransport()
    try:
        credential_kind, credential_value = _selected_credential(args)
        mode = _validate_mode(args.mode)
        if args.command == "start":
            reference_material = ""
            if args.reference_material_file:
                reference_material = args.reference_material_file.read_text(encoding="utf-8-sig")
            additional_instructions = ""
            if args.additional_instructions_file:
                additional_instructions = args.additional_instructions_file.read_text(encoding="utf-8-sig")
            prompt_text = args.prompt_file.read_text(encoding="utf-8-sig")
            preflight_flags = scan_path_name_flags(args.prompt_file)
            preflight_flags.extend(scan_text_for_secret_flags(prompt_text))
            preflight_flags.extend(scan_text_for_secret_flags(reference_material))
            if preflight_flags and not args.allow_flagged_content:
                raise ReviewApiError(
                    "start preflight blocked likely secret or high-risk source content: "
                    + "; ".join(preflight_flags)
                    + ". Pass --allow-flagged-content only after manual redaction review."
                )
            response, idem, generated = start_review(
                transport=transport,
                base_url=args.base_url,
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                idempotency_key=args.idempotency_key,
                prompt=prompt_text,
                models=args.models,
                max_spend_usd=args.max_spend_usd,
                model_tier=args.model_tier,
                synthesis_model=args.synthesis_model,
                document_name=args.document_name,
                template=args.template,
                system_prompt=args.system_prompt,
                extra_prompt=args.extra_prompt,
                additional_instructions=additional_instructions,
                reference_material=reference_material,
            )
            _print_summary(
                summarize_response(
                    response,
                    mode=mode,
                    idempotency_key=idem,
                    idempotency_key_generated=generated,
                    redact_urls=args.redact_private_urls,
                )
            )
            return 0
        if args.command == "status":
            response = get_status(
                transport=transport,
                base_url=args.base_url,
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                job_id=args.job_id,
            )
            _print_summary(summarize_response(response, mode=mode, redact_urls=args.redact_private_urls))
            return 0
        if args.command == "templates":
            response = list_templates(
                transport=transport,
                base_url=args.base_url,
                credential_kind=credential_kind,
                credential_value=credential_value,
            )
            output = getattr(args, "output", None)
            if output:
                _write_json(output, response)
            _print_summary(redact_private_urls(response) if args.redact_private_urls else response)
            return 0
        if args.command in {"models", "catalog"}:
            response = list_models(
                transport=transport,
                base_url=args.base_url,
                credential_kind=credential_kind,
                credential_value=credential_value,
                catalog=args.command == "catalog" or getattr(args, "catalog", False),
            )
            output = getattr(args, "output", None)
            if output:
                _write_json(output, response)
            _print_summary(redact_private_urls(response) if args.redact_private_urls else response)
            return 0
        if args.command in {"packet", "session"}:
            response = get_session_or_packet(
                transport=transport,
                base_url=args.base_url,
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                session_id=args.session_id,
                packet=args.command == "packet",
            )
            if args.output:
                _write_json(args.output, response)
            _print_summary(summarize_response(response, mode=mode, redact_urls=args.redact_private_urls))
            return 0
        if args.command == "sessions":
            response = list_sessions(
                transport=transport,
                base_url=args.base_url,
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
            )
            if args.output:
                _write_json(args.output, response)
            _print_summary(summarize_response(response, mode=mode, redact_urls=args.redact_private_urls))
            return 0
        if args.command == "decide":
            decisions = _read_json_array(args.decisions_json, args.decisions_file, label="decisions")
            response = decide_review(
                transport=transport,
                base_url=args.base_url,
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                session_id=args.session_id,
                action=args.action,
                decisions=decisions,
            )
            _print_summary(summarize_response(response, mode=mode, redact_urls=args.redact_private_urls))
            return 0
        if args.command == "export":
            path = export_review(
                transport=transport,
                base_url=args.base_url,
                mode=mode,
                credential_kind=credential_kind,
                credential_value=credential_value,
                session_id=args.session_id,
                destination=args.output,
            )
            result = {"mode": mode, "session_id": args.session_id, "export_saved": str(path)}
            _print_summary(redact_private_urls(result) if args.redact_private_urls else result)
            return 0
    except ReviewApiError as exc:
        message = str(exc)
        if getattr(args, "redact_private_urls", False):
            message = redact_private_urls(message)
        print(json.dumps({"error": message}, sort_keys=True), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

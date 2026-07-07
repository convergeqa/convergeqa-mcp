"""Supported CLI client for ConvergeQA private Compare due-diligence API v1.

This client is inert production-tree tooling. It is not imported by Flask
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


DEFAULT_BASE_URL = "https://convergeqa.net"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_API_KEY_ENV = "CONVERGEQA_API_KEY"
DEFAULT_SERVICE_ACCOUNT_KEY_ENV = "CONVERGEQA_SERVICE_ACCOUNT_KEY"
DEFAULT_USER_AGENT = "ConvergeQA-AgentClient/1.0"
VALID_CREDENTIAL_KINDS = {"api-key", "service-account"}
VALID_SERVICE_ACCOUNT_MODEL_TIERS = {"budget", "premium"}
QA_THAT_CODING_PANEL = [
    "claude-opus-4.8",
    "gpt-5.5",
    "gemini-3.1-pro",
    "grok-4.3",
    "openrouter-qwen3-235b",
]
QA_THAT_CODING_SYNTHESIS_MODEL = "claude-opus-4.8"
QA_THAT_GENERAL_ASSESSMENT = """Review the submitted codebase packet for:
- correctness and behavioral regressions
- auth, admin, tenant, state restoration, and session-resume risks
- billing, credits, refunds, spend caps, and account-balance side effects
- export names, ZIP contents, PDFs, manifests, audit records, and user-facing artifacts
- public UI wording, labels, banners, and provider/model cost visibility
- package/deploy risks, ignored files, scratch artifacts, and stale build assets
- missing tests, weak assertions, and tests that only prove the harness rather than the user flow
- mismatch between stated goal and actual implementation"""
QA_THAT_SPECIALISTS = [
    {
        "panel_node_id": "architecture_regression",
        "model": "claude-opus-4.8",
        "role_name": "architecture_regression_reviewer",
        "specialist_focus_prompt": "Focus on architecture, regression surface, product-flow coherence, rollback shape, and whether the change fits the existing codebase boundaries.",
    },
    {
        "panel_node_id": "implementation_correctness",
        "model": "gpt-5.5",
        "role_name": "implementation_correctness_reviewer",
        "specialist_focus_prompt": "Focus on implementation logic, invariants, edge cases, data validation, and whether the patch actually satisfies the stated acceptance criteria.",
    },
    {
        "panel_node_id": "state_data_flow",
        "model": "gemini-3.1-pro",
        "role_name": "state_data_flow_reviewer",
        "specialist_focus_prompt": "Focus on state, data flow, session recovery, async/background behavior, stale metadata, and restore/resume behavior across user paths.",
    },
    {
        "panel_node_id": "adversarial_failure_modes",
        "model": "grok-4.3",
        "role_name": "adversarial_failure_mode_reviewer",
        "specialist_focus_prompt": "Focus on adversarial failure modes, unexpected user paths, brittle assumptions, confusing UI states, and how a real user could still get stuck.",
    },
    {
        "panel_node_id": "tests_and_repair",
        "model": "openrouter-qwen3-235b",
        "role_name": "test_strategy_and_repair_reviewer",
        "specialist_focus_prompt": "Focus on test strategy, code-path completeness, missing assertions, whether tests prove the user flow, and practical repair sequencing.",
    },
]
PRIVATE_API_PATH_RE = re.compile(r"(/api/v1/due-diligence/compare/)[A-Za-z0-9_-]+")
try:
    from .packet_secret_scan import (
        HIGH_RISK_PACKET_NAMES,
        SECRET_PATTERNS,
        scan_path_name_flags,
        scan_text_for_secret_flags,
    )
except ImportError:  # direct script invocation from tools/agent_clients/
    from packet_secret_scan import (
        HIGH_RISK_PACKET_NAMES,
        SECRET_PATTERNS,
        scan_path_name_flags,
        scan_text_for_secret_flags,
    )
PRIVATE_PACKET_KEYS = {
    "raw_prompt",
    "raw_document",
    "raw_model_output",
    "source_document_text",
    "model_output_text",
    "provider_diagnostics",
    "system_prompt",
    "api_key",
    "api_key_value",
    "session_token",
    "password",
    "cookie",
}
SAFE_ERROR_FIELDS = {"error", "message", "estimated_cost", "max_spend_usd"}


class CompareApiError(RuntimeError):
    """Raised for safe client-side or API errors."""


class HttpTransport:
    """Small stdlib HTTP transport so the supported CLI has no package dependency."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            raise CompareApiError(_http_error_message("API request failed", exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise CompareApiError(f"API request failed: {exc.reason}") from exc
        return json.loads(payload or "{}")

    def download(
        self,
        url: str,
        *,
        headers: dict[str, str],
        destination: Path,
    ) -> Path:
        req = urllib.request.Request(url, headers=_transport_headers(headers), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                destination.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CompareApiError(_http_error_message("Bundle download failed", exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise CompareApiError(f"Bundle download failed: {exc.reason}") from exc
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


def _headers(
    api_key: str = "",
    *,
    service_account_key: str = "",
    credential_kind: str = "api-key",
    idempotency_key: str | None = None,
) -> dict[str, str]:
    if credential_kind not in VALID_CREDENTIAL_KINDS:
        raise CompareApiError("credential_kind must be api-key or service-account")
    if api_key and service_account_key:
        raise CompareApiError("Provide only one ConvergeQA credential value")
    if credential_kind == "api-key":
        if not api_key:
            raise CompareApiError("CONVERGEQA_API_KEY is required")
        headers = {"X-API-Key": api_key}
    else:
        if not service_account_key:
            raise CompareApiError("CONVERGEQA_SERVICE_ACCOUNT_KEY is required")
        headers = {"X-Service-Account-Key": service_account_key}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _normalize_model_tier(model_tier: str = "", *, credential_kind: str = "api-key") -> str:
    normalized = str(model_tier or "").strip().lower()
    if credential_kind == "service-account":
        if not normalized:
            raise CompareApiError("model_tier is required when credential_kind is service-account")
        if normalized not in VALID_SERVICE_ACCOUNT_MODEL_TIERS:
            raise CompareApiError("model_tier must be budget or premium")
        return normalized
    if normalized:
        raise CompareApiError("model_tier is only supported with service-account credentials")
    return ""


def generate_idempotency_key(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"cqa-cli-{timestamp}-{uuid.uuid4().hex[:12]}"


def redact_private_urls(value: Any) -> Any:
    if isinstance(value, str):
        return PRIVATE_API_PATH_RE.sub(r"\1<REDACTED>", value)
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
    keys = {key.lower() for key in _iter_keys(packet)}
    leaked = sorted(keys & PRIVATE_PACKET_KEYS)
    if leaked:
        raise CompareApiError(f"Packet failed private-field guard: {', '.join(leaked)}")


def submit_compare(
    *,
    transport: HttpTransport,
    base_url: str,
    idempotency_key: str | None,
    prompt: str,
    models: list[str],
    max_spend_usd: str | None = None,
    api_key: str = "",
    service_account_key: str = "",
    credential_kind: str = "api-key",
    model_tier: str = "",
    stderr: TextIO = sys.stderr,
    system_prompt: str = "",
    document_name: str = "Agent Compare Review",
    template: str = "Agent Due Diligence",
    synthesize: bool = True,
    synthesize_model: str | None = None,
    specialists: list[dict[str, Any]] | None = None,
    no_specialists: bool = False,
    intended_use: str = "Agent-submitted Compare due-diligence review.",
) -> tuple[dict[str, Any], str, bool]:
    if not prompt:
        raise CompareApiError("prompt is required")
    if not models:
        raise CompareApiError("at least one model id is required")
    normalized_model_tier = _normalize_model_tier(model_tier, credential_kind=credential_kind)

    resolved_idempotency_key, generated = _resolve_idempotency_key(idempotency_key)
    body = {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "models": models,
        "document_name": document_name,
        "template": template,
        "synthesize": synthesize,
        "synthesize_model": synthesize_model or models[0],
        "intended_use": intended_use,
    }
    if specialists is not None:
        body["specialists"] = specialists
        body["specialists_enabled"] = True
        first_general = str(specialists[0].get("general_assessment_prompt") or "").strip() if specialists else ""
        if first_general:
            body["general_assessment_prompt"] = first_general
    if no_specialists:
        body["no_specialists"] = True
    if max_spend_usd:
        body["max_spend_usd"] = max_spend_usd
    if normalized_model_tier:
        body["model_tier"] = normalized_model_tier
    if generated:
        _emit_generated_key(resolved_idempotency_key, stderr)
    response = transport.request_json(
        "POST",
        _api_url(base_url, "/api/v1/due-diligence/compare"),
        headers=_headers(
            api_key,
            service_account_key=service_account_key,
            credential_kind=credential_kind,
            idempotency_key=resolved_idempotency_key,
        ),
        body=body,
    )
    return response, resolved_idempotency_key, generated


def get_status(
    *,
    transport: HttpTransport,
    base_url: str,
    job_id: str,
    api_key: str = "",
    service_account_key: str = "",
    credential_kind: str = "api-key",
) -> dict[str, Any]:
    return transport.request_json(
        "GET",
        _api_url(base_url, f"/api/v1/due-diligence/compare/{job_id}"),
        headers=_headers(api_key, service_account_key=service_account_key, credential_kind=credential_kind),
    )


def get_packet(
    *,
    transport: HttpTransport,
    base_url: str,
    job_id: str,
    api_key: str = "",
    service_account_key: str = "",
    credential_kind: str = "api-key",
) -> dict[str, Any]:
    packet = transport.request_json(
        "GET",
        _api_url(base_url, f"/api/v1/due-diligence/compare/{job_id}/packet"),
        headers=_headers(api_key, service_account_key=service_account_key, credential_kind=credential_kind),
    )
    assert_packet_has_no_private_fields(packet)
    return packet


def list_models(
    *,
    transport: HttpTransport,
    base_url: str,
    api_key: str = "",
    service_account_key: str = "",
    credential_kind: str = "api-key",
    catalog: bool = False,
) -> dict[str, Any]:
    path = "/api/v1/models/catalog" if catalog else "/api/v1/models"
    response = transport.request_json(
        "GET",
        _api_url(base_url, path),
        headers=_headers(
            api_key,
            service_account_key=service_account_key,
            credential_kind=credential_kind,
        ),
    )
    if not isinstance(response, dict):
        raise CompareApiError("models response did not return an object")
    return response


def download_bundle(
    *,
    transport: HttpTransport,
    base_url: str,
    job_id: str,
    destination: Path,
    api_key: str = "",
    service_account_key: str = "",
    credential_kind: str = "api-key",
) -> Path:
    return transport.download(
        _api_url(base_url, f"/api/v1/due-diligence/compare/{job_id}/bundle"),
        headers=_headers(api_key, service_account_key=service_account_key, credential_kind=credential_kind),
        destination=destination,
    )


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
        if last_status.get("status") in {"complete", "failed"}:
            return last_status
        time.sleep(interval_seconds)
    raise CompareApiError(f"Timed out waiting for job {job_id}; last status was {last_status.get('status')}")


def summarize_status(
    status: dict[str, Any],
    packet: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    idempotency_key_generated: bool = False,
    redact_urls: bool = False,
) -> dict[str, Any]:
    summary = {
        "job_id": status.get("job_id"),
        "mode": status.get("mode"),
        "status": status.get("status"),
        "certificate_id": status.get("certificate_id"),
        "packet_url": status.get("packet_url"),
        "bundle_url": status.get("bundle_url"),
        "cost": status.get("cost") or {},
    }
    if idempotency_key:
        summary["idempotency_key"] = idempotency_key
        summary["idempotency_key_generated"] = bool(idempotency_key_generated)
    if packet:
        summary["packet"] = {
            "packet_id": packet.get("packet_id"),
            "schema_version": packet.get("schema_version"),
            "outcome": packet.get("outcome"),
            "verify_visibility": (packet.get("privacy") or {}).get("verify_visibility"),
            "packet_visibility": (packet.get("privacy") or {}).get("packet_visibility"),
        }
    if redact_urls:
        return redact_private_urls(summary)
    return summary


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _read_specialists_file(
    path: Path | None, *, allow_flagged_content: bool = False
) -> list[dict[str, Any]] | None:
    if not path:
        return None
    raw_text = path.read_text(encoding="utf-8-sig")
    flags = scan_path_name_flags(path) + scan_text_for_secret_flags(raw_text)
    if flags and not allow_flagged_content:
        raise CompareApiError(
            "specialists-file preflight blocked likely secret or high-risk "
            "source content: " + "; ".join(flags)
            + ". Pass --allow-flagged-content only after manual redaction review."
        )
    payload = json.loads(raw_text)
    if isinstance(payload, dict):
        if payload.get("specialists_enabled") is False:
            return None
        general = payload.get("general_assessment_prompt") or payload.get("general_assessment") or ""
        specialists = payload.get("specialists") or payload.get("roles") or []
        if general:
            specialists = [
                {**item, "general_assessment_prompt": item.get("general_assessment_prompt") or general}
                for item in specialists
                if isinstance(item, dict)
            ]
        return specialists
    if isinstance(payload, list):
        return payload
    raise CompareApiError("specialists file must be a list or an object with specialists")


def _qa_that_specialists() -> list[dict[str, Any]]:
    return [
        {**item, "general_assessment_prompt": QA_THAT_GENERAL_ASSESSMENT}
        for item in QA_THAT_SPECIALISTS
    ]


def _apply_qa_that_defaults(args: argparse.Namespace) -> None:
    if not getattr(args, "qa_that_coding_panel", False):
        return
    if not getattr(args, "models", None):
        args.models = list(QA_THAT_CODING_PANEL)
    if not getattr(args, "synthesis_model", None):
        args.synthesis_model = QA_THAT_CODING_SYNTHESIS_MODEL
    if not getattr(args, "specialists_file", None) and not getattr(args, "no_specialists", False):
        args._qa_that_specialists = _qa_that_specialists()


def _scan_prompt_file(path: Path) -> list[str]:
    flags = scan_path_name_flags(path)
    text = path.read_text(encoding="utf-8-sig")
    flags.extend(scan_text_for_secret_flags(text))
    return flags


def _read_prompt_with_preflight(path: Path, *, allow_flagged_content: bool = False) -> str:
    flags = _scan_prompt_file(path)
    if flags and not allow_flagged_content:
        raise CompareApiError(
            "prompt preflight blocked likely secret or high-risk source content: "
            + "; ".join(flags)
            + ". Pass --allow-flagged-content only after manual redaction review."
        )
    return path.read_text(encoding="utf-8-sig")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ConvergeQA Compare due-diligence API CLI.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--service-account-key-env", default=DEFAULT_SERVICE_ACCOUNT_KEY_ENV)
    parser.add_argument(
        "--credential-kind",
        choices=sorted(VALID_CREDENTIAL_KINDS),
        default="api-key",
        help="Use a legacy user API key or an org-owned service-account key.",
    )
    parser.add_argument(
        "--redact-private-urls",
        action="store_true",
        help="Redact private packet/bundle API URLs in stdout and CLI error text.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit")
    submit.add_argument("--idempotency-key")
    submit.add_argument("--prompt-file", type=Path, required=True)
    submit.add_argument("--model", action="append", dest="models")
    submit.add_argument("--max-spend-usd")
    submit.add_argument("--model-tier", default="")
    submit.add_argument("--system-prompt", default="")
    submit.add_argument("--document-name", default="Agent Compare Review")
    submit.add_argument("--template", default="Agent Due Diligence")
    submit.add_argument("--intended-use", default="Agent-submitted Compare due-diligence review.")
    submit.add_argument("--specialists-file", type=Path)
    submit.add_argument("--no-specialists", action="store_true")
    submit.add_argument("--qa-that-coding-panel", action="store_true")
    submit.add_argument("--allow-flagged-content", action="store_true")
    submit.add_argument("--no-synthesis", action="store_true")
    submit.add_argument("--synthesis-model")

    status = sub.add_parser("status")
    status.add_argument("job_id")

    packet = sub.add_parser("packet")
    packet.add_argument("job_id")
    packet.add_argument("--output", type=Path)

    bundle = sub.add_parser("bundle")
    bundle.add_argument("job_id")
    bundle.add_argument("--output", type=Path, required=True)

    models = sub.add_parser("models")
    models.add_argument("--catalog", action="store_true")
    models.add_argument("--output", type=Path)

    catalog = sub.add_parser("catalog")
    catalog.add_argument("--output", type=Path)

    run = sub.add_parser("run")
    run.add_argument("--idempotency-key")
    run.add_argument("--prompt-file", type=Path, required=True)
    run.add_argument("--model", action="append", dest="models")
    run.add_argument("--max-spend-usd")
    run.add_argument("--model-tier", default="")
    run.add_argument("--system-prompt", default="")
    run.add_argument("--document-name", default="Agent Compare Review")
    run.add_argument("--template", default="Agent Due Diligence")
    run.add_argument("--intended-use", default="Agent-submitted Compare due-diligence review.")
    run.add_argument("--specialists-file", type=Path)
    run.add_argument("--no-specialists", action="store_true")
    run.add_argument("--qa-that-coding-panel", action="store_true")
    run.add_argument("--allow-flagged-content", action="store_true")
    run.add_argument("--synthesis-model")
    run.add_argument("--packet-output", type=Path)
    run.add_argument("--bundle-output", type=Path)
    run.add_argument("--timeout-seconds", type=int, default=600)
    run.add_argument("--poll-interval-seconds", type=int, default=5)
    return parser


def _print_summary(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _credential_from_env(name: str) -> str:
    return os.environ.get(name, "")


def _selected_credentials(args: argparse.Namespace) -> dict[str, str]:
    if args.credential_kind == "service-account":
        return {
            "credential_kind": "service-account",
            "api_key": "",
            "service_account_key": _credential_from_env(args.service_account_key_env),
        }
    return {
        "credential_kind": "api-key",
        "api_key": _credential_from_env(args.api_key_env),
        "service_account_key": "",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    credentials = _selected_credentials(args)
    transport = HttpTransport()
    try:
        if args.command in {"submit", "run"}:
            _apply_qa_that_defaults(args)
            if not getattr(args, "models", None):
                raise CompareApiError("at least one model id is required")
            if getattr(args, "specialists_file", None) and getattr(args, "no_specialists", False):
                raise CompareApiError("--specialists-file cannot be combined with --no-specialists")
        if args.command == "submit":
            specialists = getattr(args, "_qa_that_specialists", None)
            if args.specialists_file:
                specialists = _read_specialists_file(
                    args.specialists_file,
                    allow_flagged_content=args.allow_flagged_content,
                )
            response, idem, generated = submit_compare(
                transport=transport,
                base_url=args.base_url,
                **credentials,
                idempotency_key=args.idempotency_key,
                prompt=_read_prompt_with_preflight(
                    args.prompt_file,
                    allow_flagged_content=args.allow_flagged_content,
                ),
                models=args.models,
                max_spend_usd=args.max_spend_usd,
                model_tier=args.model_tier,
                system_prompt=args.system_prompt,
                document_name=args.document_name,
                template=args.template,
                synthesize=not args.no_synthesis,
                synthesize_model=args.synthesis_model,
                specialists=specialists,
                no_specialists=args.no_specialists,
                intended_use=args.intended_use,
            )
            _print_summary(
                summarize_status(
                    response,
                    idempotency_key=idem,
                    idempotency_key_generated=generated,
                    redact_urls=args.redact_private_urls,
                )
            )
            return 0
        if args.command == "status":
            response = get_status(transport=transport, base_url=args.base_url, **credentials, job_id=args.job_id)
            _print_summary(summarize_status(response, redact_urls=args.redact_private_urls))
            return 0
        if args.command == "packet":
            response = get_packet(transport=transport, base_url=args.base_url, **credentials, job_id=args.job_id)
            if args.output:
                _write_json(args.output, response)
            _print_summary(summarize_status({"job_id": args.job_id}, response, redact_urls=args.redact_private_urls))
            return 0
        if args.command == "bundle":
            path = download_bundle(
                transport=transport,
                base_url=args.base_url,
                **credentials,
                job_id=args.job_id,
                destination=args.output,
            )
            _print_summary(redact_private_urls({"job_id": args.job_id, "bundle_saved": str(path)}) if args.redact_private_urls else {"job_id": args.job_id, "bundle_saved": str(path)})
            return 0
        if args.command in {"models", "catalog"}:
            response = list_models(
                transport=transport,
                base_url=args.base_url,
                **credentials,
                catalog=args.command == "catalog" or getattr(args, "catalog", False),
            )
            if args.output:
                _write_json(args.output, response)
            _print_summary(redact_private_urls(response) if args.redact_private_urls else response)
            return 0
        if args.command == "run":
            specialists = getattr(args, "_qa_that_specialists", None)
            if args.specialists_file:
                specialists = _read_specialists_file(
                    args.specialists_file,
                    allow_flagged_content=args.allow_flagged_content,
                )
            created, idem, generated = submit_compare(
                transport=transport,
                base_url=args.base_url,
                **credentials,
                idempotency_key=args.idempotency_key,
                prompt=_read_prompt_with_preflight(
                    args.prompt_file,
                    allow_flagged_content=args.allow_flagged_content,
                ),
                models=args.models,
                max_spend_usd=args.max_spend_usd,
                model_tier=args.model_tier,
                system_prompt=args.system_prompt,
                document_name=args.document_name,
                template=args.template,
                synthesize=True,
                synthesize_model=args.synthesis_model,
                specialists=specialists,
                no_specialists=args.no_specialists,
                intended_use=args.intended_use,
            )
            job_id = created.get("job_id")
            if not job_id:
                raise CompareApiError("submit response did not include job_id")
            final = poll_until_complete(
                status_getter=lambda jid: get_status(
                    transport=transport,
                    base_url=args.base_url,
                    **credentials,
                    job_id=jid,
                ),
                job_id=job_id,
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.poll_interval_seconds,
            )
            if final.get("status") != "complete":
                raise CompareApiError(f"Compare job ended with status {final.get('status')}: {final.get('error')}")
            packet_response = get_packet(transport=transport, base_url=args.base_url, **credentials, job_id=job_id)
            result = summarize_status(
                final,
                packet_response,
                idempotency_key=idem,
                idempotency_key_generated=generated,
                redact_urls=args.redact_private_urls,
            )
            if args.packet_output:
                _write_json(args.packet_output, packet_response)
                result["packet_saved"] = str(args.packet_output)
            if args.bundle_output:
                download_bundle(
                    transport=transport,
                    base_url=args.base_url,
                    **credentials,
                    job_id=job_id,
                    destination=args.bundle_output,
                )
                result["bundle_saved"] = str(args.bundle_output)
            _print_summary(redact_private_urls(result) if args.redact_private_urls else result)
            return 0
    except CompareApiError as exc:
        message = str(exc)
        if getattr(args, "redact_private_urls", False):
            message = redact_private_urls(message)
        print(json.dumps({"error": message}, sort_keys=True), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

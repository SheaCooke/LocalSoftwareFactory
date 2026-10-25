"""HTTP plumbing shared by the adapters: retries with backoff, error mapping, SSE."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from factory.providers.base import ProviderError
from factory.secrets import redactor

#: Transient statuses worth retrying (529 = Anthropic "overloaded").
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_MAX_BACKOFF_S = 8.0
_MAX_RETRY_AFTER_S = 30.0


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool-call arguments as a dict; unparsable JSON is kept under `_raw` so the
    runtime can report it to the model as an argument error."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def error_from(resp: httpx.Response, label: str, url: str) -> ProviderError:
    try:
        body = resp.read().decode("utf-8", "replace")[:2000]
    except httpx.HTTPError:
        body = ""
    return ProviderError(
        redactor.redact(f"{label} HTTP {resp.status_code} from {url}: {body}"),
        status=resp.status_code,
        retryable=resp.status_code in RETRYABLE,
    )


def _backoff(attempt: int, resp: httpx.Response | None) -> float:
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_RETRY_AFTER_S)
            except ValueError:
                pass
    return min(0.5 * 2.0**attempt, _MAX_BACKOFF_S)


def post_json(
    client: httpx.Client,
    url: str,
    *,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    params: Mapping[str, str] | None,
    max_retries: int,
    label: str,
) -> dict[str, Any]:
    """POST JSON, retrying transient failures; returns the decoded JSON body."""
    attempt = 0
    while True:
        resp: httpx.Response | None = None
        try:
            resp = client.post(url, json=body, headers=dict(headers), params=dict(params or {}))
        except httpx.TransportError as exc:
            error = ProviderError(redactor.redact(f"cannot reach {url}: {exc}"), retryable=True)
        else:
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ProviderError(f"{label}: response from {url} is not JSON") from exc
                if not isinstance(data, dict):
                    raise ProviderError(f"{label}: unexpected response shape from {url}")
                return data
            error = error_from(resp, label, url)
        if not error.retryable or attempt >= max_retries:
            raise error
        attempt += 1
        time.sleep(_backoff(attempt, resp))


def stream_sse(
    client: httpx.Client,
    url: str,
    *,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    params: Mapping[str, str] | None,
    label: str,
) -> Iterator[tuple[str | None, Any]]:
    """POST and yield `(event_name, decoded_data)` for each Server-Sent Event.

    `data: [DONE]` (OpenAI) ends the stream. No retries: a stream may already have
    produced output, so the caller decides.
    """
    try:
        with client.stream(
            "POST", url, json=body, headers=dict(headers), params=dict(params or {})
        ) as resp:
            if resp.status_code >= 400:
                raise error_from(resp, label, url)
            event: str | None = None
            data_lines: list[str] = []
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    if payload == "[DONE]":
                        return
                    yield event, json.loads(payload)
                    event = None
            if data_lines:  # stream ended without a trailing blank line
                payload = "\n".join(data_lines)
                if payload != "[DONE]":
                    yield event, json.loads(payload)
    except httpx.TransportError as exc:
        raise ProviderError(redactor.redact(f"cannot reach {url}: {exc}"), retryable=True) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{label}: malformed stream event from {url}: {exc}") from exc

"""Shared REST client for CLI and MCP. It intentionally never opens SSH or SQLite."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

CONTROL_PLANE_READ_TIMEOUT_SECONDS = 20.0
# Measured 8-card reclaim spent ~8.5s per GPU when each stop re-collected the
# whole host. Apply calls keep that budget with headroom even after one
# collection per endpoint, and never wait more than three minutes.
CONTROL_PLANE_CLAIM_SECONDS_PER_GPU = 15.0
CONTROL_PLANE_CLAIM_TIMEOUT_MAX_SECONDS = 180.0
_CONTROL_PLANE_CLAIM_PATHS = frozenset({"/api/v1/claims", "/api/v1/routine/claims"})


def control_plane_claim_timeout(gpu_count: int) -> float:
    """HTTP budget for one apply/claim; scales with the requested GPU count."""

    count = gpu_count if type(gpu_count) is int and gpu_count >= 1 else 1
    return min(
        CONTROL_PLANE_CLAIM_TIMEOUT_MAX_SECONDS,
        max(
            CONTROL_PLANE_READ_TIMEOUT_SECONDS,
            count * CONTROL_PLANE_CLAIM_SECONDS_PER_GPU,
        ),
    )


def control_plane_request_timeout(
    path: str,
    json_body: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
    default: float = CONTROL_PLANE_READ_TIMEOUT_SECONDS,
) -> float:
    """Read calls keep the default; claim posts scale with ``gpu_count``."""

    if timeout is not None:
        return timeout
    if path in _CONTROL_PLANE_CLAIM_PATHS and isinstance(json_body, dict):
        constraints = json_body.get("constraints")
        if isinstance(constraints, dict):
            gpu_count = constraints.get("gpu_count")
            if type(gpu_count) is int and gpu_count >= 1:
                return control_plane_claim_timeout(gpu_count)
    return default


def control_plane_http_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Issue one HTTP request to the loopback control plane.

    ``trust_env`` stays false so ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``
    and the macOS System Configuration proxy never intercept 127.0.0.1. Python's
    ``urllib.request.getproxies()`` still reads that OS table when the process
    environment has no proxy variables; httpx would honor it by default.
    """
    return httpx.request(method, url, trust_env=False, **kwargs)


def control_plane_async_httpx_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build the MCP process's AsyncClient for the loopback control plane."""
    return httpx.AsyncClient(trust_env=False, **kwargs)


class BrokerClientError(RuntimeError):
    """A broker call that did not return a success envelope.

    ``code`` carries the broker's own error code when the response had one, so
    a caller can tell a business outcome such as ``no_capacity`` apart from a
    transport failure without parsing the message.
    """

    def __init__(
        self, message: str, *, code: str | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BrokerClient:
    def __init__(
        self,
        url: str,
        actor: str = "agent",
        *,
        timeout_seconds: float = CONTROL_PLANE_READ_TIMEOUT_SECONDS,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise BrokerClientError("SERVERPILOT_URL must start with http:// or https://")
        self.url = url.rstrip("/")
        self.actor = actor or "agent"
        self.timeout_seconds = timeout_seconds
        self._last_state_revision: int | None = None

    @classmethod
    def from_env(cls, *, url: str | None = None, actor: str | None = None) -> BrokerClient:
        configured_actor = actor or os.environ.get("SERVERPILOT_ACTOR")
        return cls(
            url or os.environ.get("SERVERPILOT_URL", "http://127.0.0.1:8787"),
            configured_actor or "agent",
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {"X-ServerPilot-Actor": self.actor}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = control_plane_http_request(
                method,
                f"{self.url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=control_plane_request_timeout(
                    path,
                    json_body,
                    timeout=timeout,
                    default=self.timeout_seconds,
                ),
            )
        except httpx.HTTPError as exc:
            raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            raise BrokerClientError(
                f"broker returned non-JSON HTTP {response.status_code} ({content_type})"
            ) from exc
        if response.is_error:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code")
            message = error.get("message", "request failed")
            raise BrokerClientError(
                f"broker HTTP {response.status_code}: {code or 'unknown'}: {message}",
                code=code if isinstance(code, str) else None,
                status_code=response.status_code,
            )
        if not isinstance(payload, dict):
            raise BrokerClientError("broker returned an invalid JSON envelope")
        return payload

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            path,
            json_body=body,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    def patch(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request("PATCH", path, json_body=body, idempotency_key=idempotency_key)

    def delete(self, path: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.request("DELETE", path, idempotency_key=idempotency_key)

    def control_plane_state(
        self,
        *,
        minimum_snapshot_revision: int | None = None,
        timeout_seconds: float = 0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        if minimum_snapshot_revision is not None and (
            isinstance(minimum_snapshot_revision, bool)
            or not isinstance(minimum_snapshot_revision, int)
            or minimum_snapshot_revision < 0
        ):
            raise BrokerClientError("minimum_snapshot_revision must be a non-negative integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
            raise BrokerClientError("timeout_seconds must be a number")
        if isinstance(poll_interval_seconds, bool) or not isinstance(
            poll_interval_seconds, int | float
        ):
            raise BrokerClientError("poll_interval_seconds must be a number")
        timeout_seconds = float(timeout_seconds)
        poll_interval_seconds = float(poll_interval_seconds)
        if not 0 <= timeout_seconds <= 300:
            raise BrokerClientError("timeout_seconds must be between 0 and 300")
        if not 0.05 <= poll_interval_seconds <= 10:
            raise BrokerClientError("poll_interval_seconds must be between 0.05 and 10")

        deadline = time.monotonic() + timeout_seconds
        previous_revision = self._last_state_revision
        while True:
            payload = self.get("/api/v1/state")
            revision = payload.get("snapshot_revision")
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise BrokerClientError("broker state returned an invalid snapshot_revision")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
                raise BrokerClientError("broker state returned an invalid current state")
            if previous_revision is not None and revision < previous_revision:
                raise BrokerClientError(
                    f"broker state revision rolled back from {previous_revision} to {revision}"
                )
            previous_revision = revision
            self._last_state_revision = revision
            if minimum_snapshot_revision is None or revision >= minimum_snapshot_revision:
                return payload
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise BrokerClientError(
                    f"broker state revision {revision} is below required {minimum_snapshot_revision}"
                )
            time.sleep(min(poll_interval_seconds, remaining_seconds))

    def _state_data(
        self, **kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        payload = self.control_plane_state(**kwargs)
        data = payload["data"]
        history = data.get("history")
        if not isinstance(history, dict):
            raise BrokerClientError("broker state returned an invalid history state")
        return payload, data["current"], history

    def _state_current(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        payload, current, _history = self._state_data(**kwargs)
        return payload, current

    def _state_projection(
        self,
        key: str,
        *,
        data: Any | None = None,
        current: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state is None or current is None:
            state, current = self._state_current()
        if data is None:
            if key not in current:
                raise BrokerClientError(f"broker state is missing current.{key}")
            data = current[key]
        return {
            "schema_version": state.get("schema_version", "v1"),
            "snapshot_revision": state["snapshot_revision"],
            "server_time": state.get("server_time"),
            "data": data,
        }

    def snapshot(
        self,
        *,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "compact": compact,
            "only_available": only_available,
        }
        if endpoint_id:
            params["endpoint_id"] = endpoint_id
        if state:
            params["state"] = state
        return self.get("/api/v1/snapshot", params=params)

    def endpoints(self) -> dict[str, Any]:
        return self.get("/api/v1/endpoints")

    def endpoint_history(
        self,
        endpoint_id: str,
        *,
        window_seconds: int = 3600,
        points: int = 120,
    ) -> dict[str, Any]:
        return self.get(
            f"/api/v1/endpoints/{endpoint_id}/history",
            params={"window_seconds": window_seconds, "points": points},
        )

    def gpus(
        self,
        *,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "compact": compact,
            "only_available": only_available,
        }
        if state:
            params["state"] = state
        if endpoint_id:
            params["endpoint_id"] = endpoint_id
        # The REST GPU projection is already revision-consistent. Ask
        # /api/v1/gpus for that list; do not derive GPUs from /api/v1/state.
        return self.get("/api/v1/gpus", params=params)

    def leases(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload = self.get("/api/v1/leases")
        leases = payload.get("data")
        if not isinstance(leases, list):
            raise BrokerClientError("broker leases response is invalid")
        if project_id:
            leases = [lease for lease in leases if lease.get("project_id") == project_id]
        return {**payload, "data": leases}

    def requests(self, *, request_id: str | None = None, queued_only: bool = False) -> dict[str, Any]:
        payload = self.get("/api/v1/requests")
        requests = payload.get("data")
        if not isinstance(requests, list):
            raise BrokerClientError("broker requests response is invalid")
        if request_id:
            requests = [request for request in requests if request.get("id") == request_id]
        if queued_only:
            requests = [
                request
                for request in requests
                if request.get("state") in {"QUEUED", "PENDING_APPROVAL"}
            ]
        return {**payload, "data": requests}

    def reservations(self) -> dict[str, Any]:
        return self.get("/api/v1/reservations")

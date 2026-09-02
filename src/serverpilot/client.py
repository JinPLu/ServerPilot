"""Shared REST client for CLI and MCP. It intentionally never opens SSH or SQLite."""

from __future__ import annotations

import time
from typing import Any

import httpx

from serverpilot.config import control_plane_actor, control_plane_url

CONTROL_PLANE_READ_TIMEOUT_SECONDS = 20.0
# A claim costs whatever the server it lands on costs, and nothing on the
# server side scales with the number of cards: a direct claim stops one host
# once and observes it once, and a delegated claim is bounded by the apply
# budget its plugin declares. Guessing from `gpu_count` produced the one
# outcome a claim must never have -- the caller giving up while the control
# plane went on to commit a lease nobody would release. So the caller waits
# out the largest budget the server can spend instead of predicting it:
# `plugins.MAX_PROFILE_APPLY_SECONDS` bounds the delegated side,
# `adapters.direct_claim_budget_seconds` the direct side, and
# `tests/test_client.py` keeps this value above both.
CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS = 200.0
# Registering a host observes it once before answering, so the same rule
# applies: the caller waits out what that collection can cost the server --
# `plugins.PLUGIN_OBSERVE_TIMEOUT_SECONDS` for a delegated cluster, one SSH
# connect timeout for a direct host -- rather than the plain read budget,
# which a slow cluster would exceed while the endpoint was already created.
CONTROL_PLANE_REGISTER_TIMEOUT_SECONDS = 60.0
# One table, so a path that costs more than a read says so in one place. The
# budget is a ceiling on waiting, not a wait: a healthy loopback call returns
# in milliseconds whichever entry it matches.
_CONTROL_PLANE_PATH_TIMEOUTS = {
    "/api/v1/claims": CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS,
    "/api/v1/routine/claims": CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS,
    "/api/v1/endpoints": CONTROL_PLANE_REGISTER_TIMEOUT_SECONDS,
}


def control_plane_request_timeout(
    path: str,
    json_body: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
    default: float = CONTROL_PLANE_READ_TIMEOUT_SECONDS,
) -> float:
    """Read calls keep the default; a longer path waits out the server's budget."""

    if timeout is not None:
        return timeout
    return _CONTROL_PLANE_PATH_TIMEOUTS.get(path, default)


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
    transport failure without parsing the message. ``unsent`` says whether the
    request provably never reached the control plane, which is the only case
    where replaying it is free of consequence.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        unsent: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.unsent = unsent
        self.details = details or {}


def parse_broker_response(response: httpx.Response) -> dict[str, Any]:
    """Turn one control-plane response into an envelope, or raise for its error.

    The CLI's synchronous client and the MCP session's async broker differ only
    in how they issue a request; everything after the response arrived is the
    same decision. Keeping two copies of it let them disagree: ``details`` was
    attached on the async path and dropped on the sync one, so a documented
    outcome such as ``group_selection_required`` reached an agent complete and
    reached the CLI stripped of the very choices it names.
    """

    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown")
        raise BrokerClientError(
            f"broker returned non-JSON HTTP {response.status_code} ({content_type})"
        ) from exc
    if response.is_error:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        error = error if isinstance(error, dict) else {}
        code = error.get("code")
        details = error.get("details")
        raise BrokerClientError(
            f"broker HTTP {response.status_code}: {code or 'unknown'}: "
            f"{error.get('message', 'request failed')}",
            code=code if isinstance(code, str) else None,
            status_code=response.status_code,
            details=details if isinstance(details, dict) else None,
        )
    if not isinstance(payload, dict):
        raise BrokerClientError("broker returned an invalid JSON envelope")
    return payload


def request_was_never_sent(exc: httpx.HTTPError) -> bool:
    """True when the transport failed before the control plane could see it.

    A connect failure proves nothing was received. A read timeout proves the
    opposite: the request arrived and the server may still be working on it,
    so replaying it only doubles the wait for an answer that is already coming.
    """

    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


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
        return cls(control_plane_url(url), control_plane_actor(actor))

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
            raise BrokerClientError(
                f"broker request failed: {type(exc).__name__}",
                unsent=request_was_never_sent(exc),
            ) from exc
        return parse_broker_response(response)

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

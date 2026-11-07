"""Client for the private ML service (mlengine). The mobile app never talks to it directly."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

import httpx

from app.config import get_settings


class MlServiceError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details or {}

    @property
    def retryable(self) -> bool:
        return self.code == "ML_SERVICE_UNAVAILABLE"


class MlClient:
    def __init__(self, base_url: str, token: str | None, timeout: float, transport: httpx.BaseTransport | None = None):
        headers = {"X-Internal-Token": token} if token else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/internal/v1", headers=headers,
                                  timeout=timeout, transport=transport)

    def _call(self, method: str, path: str, request_id: str | None = None, **kwargs) -> dict:
        headers = {"X-Request-Id": request_id} if request_id else {}
        try:
            response = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise MlServiceError("ML_SERVICE_UNAVAILABLE", f"The forecasting service could not be reached: {error}") from error
        if response.status_code >= 500:
            raise MlServiceError("ML_SERVICE_UNAVAILABLE", "The forecasting service failed.", response.status_code)
        body = response.json()
        if response.status_code >= 400:
            error = body.get("error", {})
            raise MlServiceError(error.get("code", "ML_ERROR"), error.get("message", "Forecasting failed."),
                                 response.status_code, error.get("details"))
        return body

    def health(self) -> dict:
        return self._call("GET", "/health")

    def models(self) -> list[dict]:
        return self._call("GET", "/models")["models"]

    def resolve(self, model_id: str, as_of: dt.date) -> dict:
        return self._call("GET", f"/models/{model_id}/resolve", params={"asOfDate": as_of.isoformat()})

    def card(self, model_id: str, version: str) -> dict:
        return self._call("GET", f"/models/{model_id}/{version}/card")

    def product_backtest(self, model_id: str, version: str, product_id: str) -> dict | None:
        try:
            return self._call("GET", f"/models/{model_id}/{version}/backtest", params={"productId": product_id}).get("product")
        except MlServiceError as error:
            if error.code == "NO_STORED_BACKTEST":
                return None
            raise

    def forecast(self, body: dict, request_id: str | None = None) -> dict:
        return self._call("POST", "/forecast", request_id, json=body)

    def demand_class(self, series: list[dict]) -> list[dict]:
        return self._call("POST", "/demand-class", json={"series": series})["series"]


_override: MlClient | None = None


def set_ml_client(client: MlClient | None) -> None:
    """Tests inject a client with a fake transport."""
    global _override
    _override = client
    get_ml_client.cache_clear()


@lru_cache
def get_ml_client() -> MlClient:
    if _override is not None:
        return _override
    settings = get_settings()
    return MlClient(settings.ml_service_url, settings.ml_service_token, settings.ml_timeout_seconds)

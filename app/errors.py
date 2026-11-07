"""One error shape for every response: {"error": {code, message, details, requestId}}."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details or {}


def not_found(what: str) -> ApiError:
    return ApiError("NOT_FOUND", f"{what} was not found.", 404)


def forbidden(message: str = "You don't have permission to do that.") -> ApiError:
    return ApiError("FORBIDDEN", message, 403)


def _body(request: Request, code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {},
                      "requestId": getattr(request.state, "request_id", None)}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, error: ApiError):
        headers = {"Retry-After": str(error.details["retryAfterSeconds"])} if "retryAfterSeconds" in error.details else None
        return JSONResponse(status_code=error.status, content=_body(request, error.code, error.message, error.details),
                            headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, error: RequestValidationError):
        details = {"errors": [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in error.errors()]}
        return JSONResponse(status_code=422, content=_body(
            request, "INVALID_REQUEST", "Some fields are missing or invalid.", details))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, error: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 401: "UNAUTHORIZED"}.get(error.status_code, "HTTP_ERROR")
        return JSONResponse(status_code=error.status_code, content=_body(request, code, str(error.detail)))

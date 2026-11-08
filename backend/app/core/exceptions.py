"""Application errors and the single error envelope every route returns.

Envelope (see docs/API.md):
    {"detail": {"code": "MACHINE_CODE", "message": "Human text", "field": "optional"}}
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base class for every business-rule failure.

    Subclasses (or callers) supply a stable machine `code` that the SPA can
    branch on; see the code list at the bottom of docs/API.md.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        field: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.field = field
        self.status_code = status_code or self.status_code
        # Headers that must survive the raise. A `Response` injected into the
        # route is thrown away when an exception unwinds past it, so anything
        # that has to reach the browser on a *failure* — clearing a dead
        # session cookie, above all — has to travel on the error itself.
        self.headers = headers

    def as_detail(self) -> dict:
        detail = {"code": self.code, "message": self.message}
        if self.field:
            detail["field"] = self.field
        return detail


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_CREDENTIALS"


class PermissionError_(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN_ROLE"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.as_detail()},
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Pass through details that are already in envelope shape.
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            detail = exc.detail
        else:
            detail = {
                "code": _CODE_BY_STATUS.get(exc.status_code, "HTTP_ERROR"),
                "message": str(exc.detail),
            }
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": {
                    "code": "VALIDATION_ERROR",
                    "message": first.get("msg", "Invalid request"),
                    "field": ".".join(loc) or None,
                    "errors": [
                        {
                            "field": ".".join(
                                str(p) for p in e.get("loc", []) if p not in ("body", "query", "path")
                            ),
                            "message": e.get("msg", ""),
                        }
                        for e in exc.errors()
                    ],
                }
            },
        )


_CODE_BY_STATUS = {
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN_ROLE",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
}

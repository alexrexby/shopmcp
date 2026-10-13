"""Errors an adapter may raise. Anything else becomes `internal_error` without a trace."""

from typing import Any

from shopmcp.i18n import L10n, define


class ShopError(Exception):
    code: str = "internal_error"
    retryable: bool = False
    default_message: L10n = define(
        "error.internal_error",
        ru="Внутренняя ошибка сервера. Попробуйте позже.",
        en="Internal server error. Try again later.",
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # message: verbatim text from the source; None means the localized default by code.
        # details: language-neutral data for the model, e.g. {"missing": ["42"]}.
        super().__init__(message or self.code)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details


class NotFound(ShopError):
    code = "not_found"
    default_message = define("error.not_found", ru="Ничего не найдено.", en="Nothing was found.")


class InvalidInput(ShopError):
    code = "invalid_input"
    default_message = define(
        "error.invalid_input",
        ru="Некорректные параметры запроса.",
        en="Invalid request parameters.",
    )


class Unavailable(ShopError):
    code = "upstream_unavailable"
    retryable = True
    default_message = define(
        "error.upstream_unavailable",
        ru="Источник данных магазина временно недоступен. Повторите запрос позже.",
        en="The store data source is temporarily unavailable. Retry later.",
    )


class NotSupported(ShopError):
    code = "not_supported"
    default_message = define(
        "error.not_supported",
        ru="Этот магазин не поддерживает такую операцию.",
        en="This store does not support this operation.",
    )


class Unauthorized(ShopError):
    code = "unauthorized"
    default_message = define(
        "error.unauthorized",
        ru="Нужен вход в аккаунт магазина.",
        en="Sign-in to the store account is required.",
    )


class InternalError(ShopError):
    pass

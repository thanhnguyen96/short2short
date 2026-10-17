from __future__ import annotations


class Short2ShortError(Exception):
    """Base error. Message must already be redacted by the raiser."""


class ConfigError(Short2ShortError):
    """Configuration error."""


class PathSecurityError(Short2ShortError):
    """Path traversal or security validation error."""


class BudgetExceededError(Short2ShortError):
    """Budget limits exceeded error."""


class PipelineError(Short2ShortError):
    """Pipeline execution error."""


class ProviderError(Short2ShortError):
    """Provider execution error."""

    def __init__(self, message: str, *, provider: str = "") -> None:
        from short2short.security import redact

        super().__init__(redact(message))
        self.provider: str = provider


class ProviderAuthError(ProviderError):
    """Provider authentication error."""


class ProviderPolicyError(ProviderError):
    """Provider policy violation error."""


class ProviderQuotaError(ProviderError):
    """Provider quota exceeded error."""


class ProviderTimeoutError(ProviderError):
    """Provider timeout error."""


class ProviderUnconfirmedJobError(ProviderError):
    """Provider job submitted but confirmation unverified error."""

    def __init__(self, message: str, *, job_id: str | None, provider: str = "") -> None:
        super().__init__(message, provider=provider)
        self.job_id: str | None = job_id

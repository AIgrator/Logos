"""
Custom exception classes for LLM provider interactions.

This module defines a structured hierarchy of exceptions to allow for more
granular error handling, particularly for implementing intelligent retry mechanisms.
"""

class LLMProviderError(Exception):
    """Base class for all custom LLM provider errors."""
    pass

class FatalError(LLMProviderError):
    """
    Represents a non-recoverable error.

    These errors should not be retried, as the request is guaranteed to fail
    again (e.g., invalid API key, malformed request, insufficient quota).
    The operation should be aborted immediately, and the error should be
    reported to the user.
    """
    pass

class RetryableError(LLMProviderError):
    """
    Represents a temporary, potentially recoverable error.

    These errors indicate a transient issue, such as a server-side problem
    (5xx errors) or a temporary network failure. Operations that raise this
    error can be safely retried, ideally with an exponential backoff strategy.
    """
    pass

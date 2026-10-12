import re
import html
from .exceptions import RetryableError, format_wait, is_quota_exhausted

RETRY_LINK = "<br><a href=\"retry:last\">↻ Retry request</a>"
CONTINUE_LINK = "<br><a href=\"continue:last\">▶ Продолжить</a>"


def format_quota_wall(provider_name: str, error: Exception, wait_seconds: float) -> str:
    """Honest card for 'quota exhausted for a long time'.

    Used when the server demands a wait far beyond what auto-retry can
    honor (e.g. RetryInfo retryDelay of hours on a free-tier 429), so
    hammering Retry is pointless. No Retry link on purpose: re-send the
    request manually once quota is restored (or another provider is set).
    """
    provider_display_name = provider_name.replace('-', ' ').replace('_', ' ').title()
    wait_human = format_wait(wait_seconds)
    excerpt = re.sub(r"\s+", " ", str(error)).strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:300] + "..."
    return (f"<b>{provider_display_name}: quota exhausted for ~{wait_human}:</b><br>"
            f"The server asks to wait about {wait_human} (daily/longer-term limit, "
            f"not a momentary overload) — retrying right now is pointless. "
            f"Switch provider/model, check billing, or wait."
            f"<br><i>Server says: {html.escape(excerpt)}</i>")

def format_llm_error(provider_name: str, error: Exception) -> str:
    """
    Formats a raw LLM provider exception into a user-friendly HTML message.
    """
    error_str = str(error).lower()
    provider_display_name = provider_name.replace('-', ' ').replace('_', ' ').title()

    # --- Category 1: Fatal Errors (Immediate Feedback) ---

    # Invalid API Key / Authentication
    api_key_patterns = [
        r'invalid api key', r'incorrect api key', r'api key not valid',
        r'authenticationerror', r'no api key provided', r'invalid x-api-key',
        r'user not found',  # OpenRouter 401
        r'api_key_invalid', # Google
        r'401'
    ]
    if any(re.search(p, error_str) for p in api_key_patterns):
        return (f"<b>Authentication Error with {provider_display_name}:</b><br>"
                f"The API key is incorrect or invalid. Please verify your key in the settings.")

    # Transient upstream throttling: gateways (OpenRouter) answer 429 for a
    # busy shared pool too ("temporarily rate-limited, retry shortly").
    # Without exhaustion markers that is transient, not dead quota.
    # Checked BEFORE quota (whose patterns would also match the 429 code).
    if (("temporarily rate-limited" in error_str or "retry shortly" in error_str)
            and not is_quota_exhausted(error_str)):
        return (f"<b>{provider_display_name} is temporarily rate-limited upstream:</b><br>"
                f"The shared pool is busy right now — the server asks to retry shortly "
                f"(the app already retried automatically)."
                f"{RETRY_LINK}")

    # Quota or Billing Issues (money/account side: 429 = limits, retrying will NOT help).
    # Checked BEFORE overload: original design rule, restored. No Retry link
    # on purpose — re-send manually once quota is restored.
    quota_patterns = [
        r'quota', r'insufficient_quota', r'quota_failure',
        r'resource_exhausted', r'resource exhausted',
        r'rate limit', r'429',
        r'billing', r'402', r'out of credits', r'payment',
        r'exceeded your current quota',
    ]
    if any(re.search(p, error_str) for p in quota_patterns):
        return (f"<b>Quota Error with {provider_display_name}:</b><br>"
                f"You may have exceeded your usage limit or there's a billing issue. "
                f"Please check your account on the {provider_display_name} website.")

    # Temporary overload (provider is busy, NOT a billing problem).
    overload_patterns = [
        r'503', r'529', r'unavailable', r'overloaded', r'overload',
        r'high demand', r'try again',
        r'server error', r'service unavailable',
    ]
    if (any(re.search(p, error_str) for p in overload_patterns)
            or isinstance(error, RetryableError)):
        network_markers = (
            'connection', 'network', 'timeout', 'timed out', 'dns',
            'could not resolve', 'econn', 'socket',
        )
        if any(m in error_str for m in network_markers):
            return (f"<b>Network Error with {provider_display_name}:</b><br>"
                    f"Could not connect to the provider. The application will automatically retry. "
                    f"Please check your internet connection if the issue persists."
                    f"{RETRY_LINK}")
        return (f"<b>{provider_display_name} is temporarily overloaded:</b><br>"
                f"The provider is busy right now — this is not a billing issue. "
                f"Wait a minute and try again (the app already retried automatically)."
                f"{RETRY_LINK}")

    # Bad Request / Invalid Model
    model_patterns = [
        r'model not found', r'model is not supported', r'invalid model',
        r'bad request', r'invalid_request_error', r'400', r'404'
    ]
    if any(re.search(p, error_str) for p in model_patterns):
        return (f"<b>Invalid Request with {provider_display_name}:</b><br>"
                f"The request is invalid. This could be due to an unsupported model or a malformed request. "
                f"Please check your model settings.")

    # --- Category 3: Fallback for Uncategorized Errors ---

    # Sanitize the raw error message for safe HTML display
    try:
        # For OpenAI/Anthropic-like error dicts from requests library
        if hasattr(error, 'response') and hasattr(error.response, 'json'):
            error_details = error.response.json()
            message = error_details.get('error', {}).get('message', '')
            if message:
                error_message = message
            else: # Fallback for different structures
                error_message = str(error_details)
        # For Google-like errors that have a 'message' attribute
        elif hasattr(error, 'message'):
            error_message = error.message
        else:
            error_message = str(error)
    except Exception:
        error_message = str(error)

    sanitized_message = html.escape(error_message)

    return (f"<b>An unexpected error occurred with {provider_display_name}:</b><br>"
            f"<pre style='white-space: pre-wrap; word-wrap: break-word;'>{sanitized_message}</pre>"
            f"<i>For more details, please check the application logs.</i>")

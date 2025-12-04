import re
import html
from .exceptions import RetryableError

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

    # Quota or Billing Issues
    quota_patterns = [
        r'quota', r'rate limit', r'insufficient_quota', r'billing',
        r'402', r'429'
    ]
    if any(re.search(p, error_str) for p in quota_patterns):
        return (f"<b>Quota Error with {provider_display_name}:</b><br>"
                f"You may have exceeded your usage limit or there's a billing issue. "
                f"Please check your account on the {provider_display_name} website.")

    # Bad Request / Invalid Model
    model_patterns = [
        r'model not found', r'model is not supported', r'invalid model',
        r'bad request', r'invalid_request_error', r'400', r'404'
    ]
    if any(re.search(p, error_str) for p in model_patterns):
        return (f"<b>Invalid Request with {provider_display_name}:</b><br>"
                f"The request is invalid. This could be due to an unsupported model or a malformed request. "
                f"Please check your model settings.")

    # --- Category 2: Retryable Errors (Might resolve on its own) ---

    # Network Errors
    if isinstance(error, RetryableError):
        return (f"<b>Network Error with {provider_display_name}:</b><br>"
                f"Could not connect to the provider. The application will automatically retry. "
                f"Please check your internet connection if the issue persists.")

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

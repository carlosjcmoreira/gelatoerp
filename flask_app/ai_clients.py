import os

# Own API keys take priority; Replit's AI Integrations proxy is the fallback while production still runs there.


def _anthropic_credentials():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key, None
    return os.environ.get("AI_INTEGRATIONS_ANTHROPIC_API_KEY"), os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL")


def _openai_credentials():
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key, None
    return os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"), os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")


def anthropic_configured() -> bool:
    key, base_url = _anthropic_credentials()
    return bool(key or base_url)


def anthropic_client():
    key, base_url = _anthropic_credentials()
    if not (key or base_url):
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    return Anthropic(api_key=key, base_url=base_url)


def openai_client():
    key, base_url = _openai_credentials()
    if not (key or base_url):
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    return OpenAI(api_key=key, base_url=base_url)

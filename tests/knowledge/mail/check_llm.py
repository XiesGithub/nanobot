"""Explicit credential-safe provider capability check, not a quality evaluation.

Runs one fictional request, fixed to deepseek-v4-flash. Never emits provider
error bodies, credentials, generated content or HTTP logs.
"""

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from nanobot.knowledge.mail.contracts import GENERATION_MODEL


def load_provider(credentials):
    # Disable optional third-party telemetry before the provider module imports.
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    logging.disable(logging.CRITICAL)
    from loguru import logger
    logger.remove()
    key = credentials.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]+", key):
        raise ValueError("unsupported_credential_format")
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    from nanobot.providers.registry import find_by_name
    return OpenAICompatProvider(api_key=key, default_model=GENERATION_MODEL,
        spec=find_by_name("deepseek"), extra_body={"thinking": {"type": "disabled"}})


async def check(credentials):
    try:
        provider = load_provider(credentials)
    except ValueError:
        return {"available": False, "error": "unsupported_credential_format"}
    try:
        response = await asyncio.wait_for(provider.chat(model=GENERATION_MODEL,
            messages=[{"role": "user", "content": 'Fictional integration check. Return only {"ready":true}.'}],
            tools=None, temperature=0, max_tokens=64), 45)
        if response.finish_reason == "error":
            return {"available": False, "error": "provider_error", "http_status": response.error_status_code}
        return {"available": bool(response.content) and not response.tool_calls,
                "finish_reason": response.finish_reason, "usage": response.usage}
    except Exception:
        return {"available": False, "error": "provider_connection_or_timeout"}
    finally:
        await provider._client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=Path("api.txt"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"kind": "capability_check_not_quality_evaluation", "model": GENERATION_MODEL,
              "timestamp": datetime.now(UTC).isoformat(), **asyncio.run(check(args.credentials))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))

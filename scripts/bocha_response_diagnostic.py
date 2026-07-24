"""Probe Bocha once and persist only a redacted response-shape summary."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SEARCH_URL = "https://api.bochaai.com/v1/web-search"
OUTPUT_PATH = Path("diagnostics/bocha-response-shape.json")
QUERY = "中国商业航天 融资 新闻"


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _safe_message(value: Any, api_key: str) -> str | None:
    if not isinstance(value, str):
        return None
    message = value.replace(api_key, "<redacted>").replace(QUERY, "<query>")
    message = re.sub(r"\b[A-Za-z0-9_-]{24,}\b", "<redacted>", message)
    return message.strip()[:160]


def _shape(payload: Any, *, status: int, content_type: str, api_key: str) -> dict:
    result: dict[str, Any] = {
        "http_status": status,
        "content_type": content_type,
        "root_type": _type_name(payload),
    }
    if not isinstance(payload, dict):
        return result

    result["root_keys"] = sorted(str(key) for key in payload)
    if "code" in payload:
        code = payload["code"]
        result["business_code_type"] = _type_name(code)
        if isinstance(code, (str, int, float, bool)) or code is None:
            result["business_code"] = code

    message = _safe_message(payload.get("msg", payload.get("message")), api_key)
    if message:
        result["business_message"] = message

    data = payload.get("data")
    if isinstance(data, dict):
        result["data_keys"] = sorted(str(key) for key in data)

    web_pages = payload.get("webPages")
    path = "webPages"
    if not isinstance(web_pages, dict) and isinstance(data, dict):
        web_pages = data.get("webPages")
        path = "data.webPages"

    if not isinstance(web_pages, dict):
        result["web_pages_path"] = None
        return result

    result["web_pages_path"] = path
    result["web_pages_keys"] = sorted(str(key) for key in web_pages)
    values = web_pages.get("value")
    result["results_type"] = _type_name(values)
    if isinstance(values, list):
        result["result_count"] = len(values)
        if values and isinstance(values[0], dict):
            result["first_result_keys"] = sorted(str(key) for key in values[0])
    return result


def main() -> int:
    api_key = os.environ.get("BOCHA_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("BOCHA_API_KEY is required")

    request_body = json.dumps(
        {
            "query": QUERY,
            "freshness": "oneMonth",
            "summary": False,
            "count": 3,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_URL,
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        content_type = error.headers.get("Content-Type", "")
        response_body = error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        diagnostic = {
            "network_error": type(error).__name__,
            "web_pages_path": None,
        }
    else:
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            diagnostic = {
                "http_status": status,
                "content_type": content_type,
                "json_valid": False,
                "web_pages_path": None,
            }
        else:
            diagnostic = _shape(
                payload,
                status=status,
                content_type=content_type,
                api_key=api_key,
            )
            diagnostic["json_valid"] = True

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(diagnostic, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

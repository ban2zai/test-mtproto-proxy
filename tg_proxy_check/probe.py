from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import ProxyConfig
from .docker_runner import DockerRunner


@dataclass(frozen=True)
class Measurement:
    timestamp: str
    proxy_name: str
    server: str
    port: int
    success: bool
    response_time_ms: int | None
    status_code: int | None
    error_type: str
    error_message: str


NODE_SCRIPT = r"""
const url = process.argv[1];
const timeoutMs = Number(process.argv[2]);
const started = Date.now();
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), timeoutMs);

(async () => {
  try {
    const response = await fetch(url, { signal: controller.signal });
    const text = await response.text();
    let data = null;
    try {
      data = JSON.parse(text);
    } catch (_) {}
    const telegramOk = data && data.ok === true;
    const success = response.status === 200 && telegramOk;
    const errorType = success ? "" : (data && data.description ? "telegram_error" : "http_error");
    const errorMessage = success ? "" : (data && data.description ? data.description : text.slice(0, 300));
    console.log(JSON.stringify({
      transport: "node",
      status_code: response.status,
      telegram_ok: data ? data.ok : null,
      success,
      response_time_ms: Date.now() - started,
      error_type: errorType,
      error_message: errorMessage
    }));
    process.exit(success ? 0 : 2);
  } catch (error) {
    const timeout = error && error.name === "AbortError";
    console.log(JSON.stringify({
      transport: "node",
      status_code: null,
      telegram_ok: null,
      success: false,
      response_time_ms: Date.now() - started,
      error_type: timeout ? "timeout" : "request_error",
      error_message: String(error && error.message ? error.message : error)
    }));
    process.exit(timeout ? 124 : 1);
  } finally {
    clearTimeout(timer);
  }
})();
""".strip()


def probe_get_me(
    *,
    runner: DockerRunner,
    client_container: str,
    proxy: ProxyConfig,
    proxy_container_name: str,
    bot_token: str,
    timeout_seconds: int,
) -> Measurement:
    timestamp = datetime.now(timezone.utc).isoformat()
    url = f"http://{proxy_container_name}:8081/bot{bot_token}/getMe"
    started = time.perf_counter()

    result = _probe_with_node(runner, client_container, url, timeout_seconds)
    if result is None:
        result = _probe_with_curl(runner, client_container, url, timeout_seconds)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response_time_ms = _coalesce_int(result.get("response_time_ms"), elapsed_ms)
    return Measurement(
        timestamp=timestamp,
        proxy_name=proxy.name,
        server=proxy.server,
        port=proxy.port,
        success=bool(result.get("success")),
        response_time_ms=response_time_ms,
        status_code=_coalesce_int(result.get("status_code"), None),
        error_type=str(result.get("error_type") or ""),
        error_message=str(result.get("error_message") or "")[:500],
    )


def _probe_with_node(
    runner: DockerRunner,
    client_container: str,
    url: str,
    timeout_seconds: int,
) -> dict | None:
    exit_code, output = runner.exec_in_container(
        client_container,
        ["node", "-e", NODE_SCRIPT, url, str(timeout_seconds * 1000)],
    )
    parsed = _parse_json_output(output)
    if parsed and parsed.get("transport") == "node":
        return parsed

    missing_node = exit_code in (126, 127) or "executable file not found" in output.lower()
    if missing_node:
        return None

    return {
        "success": False,
        "status_code": None,
        "response_time_ms": None,
        "error_type": "probe_error",
        "error_message": output.strip() or f"node probe failed with exit code {exit_code}",
    }


def _probe_with_curl(
    runner: DockerRunner,
    client_container: str,
    url: str,
    timeout_seconds: int,
) -> dict:
    exit_code, output = runner.exec_in_container(
        client_container,
        [
            "curl",
            "-sS",
            "--max-time",
            str(timeout_seconds),
            "-w",
            "\nHTTP_STATUS:%{http_code}",
            url,
        ],
    )
    if exit_code in (126, 127) or "executable file not found" in output.lower():
        return {
            "success": False,
            "status_code": None,
            "response_time_ms": None,
            "error_type": "probe_tool_missing",
            "error_message": "В CLIENT_CONTAINER нет ни node, ни curl для HTTP-проверки",
        }
    if exit_code == 28:
        return {
            "success": False,
            "status_code": None,
            "response_time_ms": timeout_seconds * 1000,
            "error_type": "timeout",
            "error_message": "curl request timeout",
        }

    body, status_code = _split_curl_output(output)
    data = _safe_json(body)
    telegram_ok = isinstance(data, dict) and data.get("ok") is True
    success = status_code == 200 and telegram_ok
    error_message = ""
    error_type = ""
    if not success:
        if isinstance(data, dict) and data.get("description"):
            error_type = "telegram_error"
            error_message = str(data["description"])
        else:
            error_type = "http_error" if status_code else "request_error"
            error_message = body.strip()[:300] or output.strip()[:300]

    return {
        "success": success,
        "status_code": status_code,
        "response_time_ms": None,
        "error_type": error_type,
        "error_message": error_message,
    }


def _parse_json_output(output: str) -> dict | None:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        parsed = _safe_json(line)
        if isinstance(parsed, dict):
            return parsed
    return None


def _split_curl_output(output: str) -> tuple[str, int | None]:
    marker = "\nHTTP_STATUS:"
    if marker not in output:
        return output, None
    body, raw_status = output.rsplit(marker, 1)
    try:
        status_code = int(raw_status.strip())
    except ValueError:
        status_code = None
    return body, status_code


def _safe_json(text: str) -> object | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _coalesce_int(value: object, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

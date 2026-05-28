from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Ошибка конфигурации, которую можно показать пользователю."""


@dataclass(frozen=True)
class ProxyConfig:
    name: str
    server: str
    port: int
    secret: str


@dataclass(frozen=True)
class AppConfig:
    telegram_api_id: str
    telegram_api_hash: str
    telegram_bot_token: str
    docker_network: str
    docker_image: str
    client_container: str
    test_container_prefix: str
    check_interval_seconds: int
    request_timeout_seconds: int
    startup_wait_seconds: int
    duration_seconds: int
    proxies: list[ProxyConfig]

    def safe_dict(self) -> dict:
        return {
            "docker_network": self.docker_network,
            "docker_image": self.docker_image,
            "client_container": self.client_container,
            "test_container_prefix": self.test_container_prefix,
            "check_interval_seconds": self.check_interval_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "startup_wait_seconds": self.startup_wait_seconds,
            "duration_seconds": self.duration_seconds,
            "telegram_api_id_set": bool(self.telegram_api_id),
            "telegram_api_hash_set": bool(self.telegram_api_hash),
            "telegram_bot_token_set": bool(self.telegram_bot_token),
            "proxies": [
                {"name": proxy.name, "server": proxy.server, "port": proxy.port}
                for proxy in self.proxies
            ],
        }


def load_config(
    env_file: str,
    *,
    duration: int | None = None,
    interval: int | None = None,
    timeout: int | None = None,
    proxy_name: str | None = None,
    require_runtime: bool = True,
) -> AppConfig:
    env_path = Path(env_file)
    values: dict[str, str] = {}

    if env_path.exists():
        try:
            from dotenv import dotenv_values
        except ImportError as exc:
            raise ConfigError(
                "Не установлен python-dotenv. Установи зависимости: pip install -r requirements.txt"
            ) from exc
        values.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
    elif require_runtime:
        raise ConfigError(f"Файл конфигурации не найден: {env_path}")

    # Переменные окружения могут переопределить .env, это удобно для CI/серверных запусков.
    for key, value in os.environ.items():
        if key.startswith(("TELEGRAM_", "DOCKER_", "CLIENT_", "TEST_", "CHECK_", "REQUEST_", "STARTUP_", "DURATION_", "PROXY_")):
            values[key] = value

    try:
        proxies = _parse_proxies(values)
    except ConfigError:
        if require_runtime:
            raise
        proxies = []
    if proxy_name:
        proxies = [proxy for proxy in proxies if proxy.name == proxy_name]
        if not proxies and require_runtime:
            raise ConfigError(f"Прокси с именем '{proxy_name}' не найден в конфиге")

    config = AppConfig(
        telegram_api_id=_get(values, "TELEGRAM_API_ID"),
        telegram_api_hash=_get(values, "TELEGRAM_API_HASH"),
        telegram_bot_token=_get(values, "TELEGRAM_BOT_TOKEN"),
        docker_network=_get(values, "DOCKER_NETWORK", "tools_default"),
        docker_image=_get(values, "DOCKER_IMAGE", "ghcr.io/avbor/docker-telegram-bot-api:latest"),
        client_container=_get(values, "CLIENT_CONTAINER"),
        test_container_prefix=_get(values, "TEST_CONTAINER_PREFIX", "tg-proxy-check"),
        check_interval_seconds=interval
        if interval is not None
        else _get_int(values, "CHECK_INTERVAL_SECONDS", 10),
        request_timeout_seconds=timeout
        if timeout is not None
        else _get_int(values, "REQUEST_TIMEOUT_SECONDS", 10),
        startup_wait_seconds=_get_int(values, "STARTUP_WAIT_SECONDS", 15),
        duration_seconds=duration if duration is not None else _get_int(values, "DURATION_SECONDS", 600),
        proxies=proxies,
    )

    if require_runtime:
        _validate(config)

    return config


def _get(values: dict[str, str], key: str, default: str = "") -> str:
    value = values.get(key, default)
    return str(value).strip()


def _get_int(values: dict[str, str], key: str, default: int) -> int:
    raw = _get(values, key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} должен быть целым числом, сейчас: {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{key} не может быть отрицательным")
    return value


def _parse_proxies(values: dict[str, str]) -> list[ProxyConfig]:
    indexes = sorted(
        {
            int(match.group(1))
            for key in values
            if (match := re.fullmatch(r"PROXY_(\d+)_NAME", key))
        }
    )

    proxies: list[ProxyConfig] = []
    errors: list[str] = []
    for index in indexes:
        prefix = f"PROXY_{index}_"
        missing = [
            key
            for key in ("NAME", "SERVER", "PORT", "SECRET")
            if not _get(values, prefix + key)
        ]
        if missing:
            errors.append(f"{prefix}: нет {', '.join(missing)}")
            continue

        port_raw = _get(values, prefix + "PORT")
        try:
            port = int(port_raw)
        except ValueError:
            errors.append(f"{prefix}PORT должен быть числом, сейчас: {port_raw!r}")
            continue
        if not 1 <= port <= 65535:
            errors.append(f"{prefix}PORT вне диапазона 1..65535")
            continue

        proxies.append(
            ProxyConfig(
                name=_get(values, prefix + "NAME"),
                server=_get(values, prefix + "SERVER"),
                port=port,
                secret=_get(values, prefix + "SECRET"),
            )
        )

    if errors:
        raise ConfigError("Ошибки в списке прокси:\n" + "\n".join(f"- {error}" for error in errors))

    return proxies


def _validate(config: AppConfig) -> None:
    missing = []
    for key, value in (
        ("TELEGRAM_API_ID", config.telegram_api_id),
        ("TELEGRAM_API_HASH", config.telegram_api_hash),
        ("TELEGRAM_BOT_TOKEN", config.telegram_bot_token),
        ("DOCKER_NETWORK", config.docker_network),
        ("CLIENT_CONTAINER", config.client_container),
    ):
        if not value:
            missing.append(key)

    if not config.proxies:
        missing.append("PROXY_N_NAME/SERVER/PORT/SECRET")

    for key, value in (
        ("CHECK_INTERVAL_SECONDS", config.check_interval_seconds),
        ("REQUEST_TIMEOUT_SECONDS", config.request_timeout_seconds),
        ("STARTUP_WAIT_SECONDS", config.startup_wait_seconds),
        ("DURATION_SECONDS", config.duration_seconds),
    ):
        if value <= 0:
            raise ConfigError(f"{key} должен быть больше нуля")

    if missing:
        raise ConfigError("Не хватает обязательных переменных:\n" + "\n".join(f"- {key}" for key in missing))

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import AppConfig, ProxyConfig


class DockerRunnerError(RuntimeError):
    """Ошибка Docker-операции, которую можно показать в CLI."""


@dataclass(frozen=True)
class StartResult:
    proxy: ProxyConfig
    container_name: str
    success: bool
    error: str = ""


class DockerRunner:
    def __init__(self) -> None:
        try:
            import docker
            from docker.errors import APIError, DockerException, NotFound
        except ImportError as exc:
            raise DockerRunnerError(
                "Не установлен Docker SDK for Python. Установи зависимости: pip install -r requirements.txt"
            ) from exc

        self._docker = docker
        self._api_error = APIError
        self._docker_exception = DockerException
        self._not_found = NotFound
        try:
            self.client = docker.from_env()
        except DockerException as exc:
            raise DockerRunnerError(f"Docker недоступен: {exc}") from exc

    def check_available(self) -> None:
        try:
            self.client.ping()
        except self._docker_exception as exc:
            raise DockerRunnerError(f"Docker daemon не отвечает: {exc}") from exc

    def validate_runtime(self, config: AppConfig) -> None:
        self.check_available()
        try:
            self.client.networks.get(config.docker_network)
        except self._not_found as exc:
            raise DockerRunnerError(f"Docker network не найдена: {config.docker_network}") from exc

        try:
            client_container = self.client.containers.get(config.client_container)
        except self._not_found as exc:
            raise DockerRunnerError(f"CLIENT_CONTAINER не найден: {config.client_container}") from exc

        client_container.reload()
        if client_container.status != "running":
            raise DockerRunnerError(
                f"CLIENT_CONTAINER должен быть running, сейчас: {client_container.status}"
            )

    def cleanup_temp_containers(self, prefix: str) -> list[str]:
        removed: list[str] = []
        for container in self.client.containers.list(all=True, filters={"name": prefix}):
            names = [name.lstrip("/") for name in container.attrs.get("Names", [])]
            if not any(name.startswith(prefix) for name in names):
                continue
            try:
                container.remove(force=True)
                removed.append(container.name)
            except self._docker_exception as exc:
                raise DockerRunnerError(f"Не удалось удалить контейнер {container.name}: {exc}") from exc
        return removed

    def start_proxy_container(self, config: AppConfig, proxy: ProxyConfig) -> StartResult:
        container_name = build_container_name(config.test_container_prefix, proxy.name)
        command = [
            "--local",
            "--verbosity=3",
            "--tdlib-proxy-type=MTPROTO",
            f"--proxy-server={proxy.server}",
            f"--proxy-port={proxy.port}",
            f"--proxy-secret={proxy.secret}",
        ]
        environment = {
            "TELEGRAM_API_ID": config.telegram_api_id,
            "TELEGRAM_API_HASH": config.telegram_api_hash,
        }
        try:
            self.client.containers.run(
                config.docker_image,
                command=command,
                detach=True,
                environment=environment,
                labels={"tg-proxy-check": config.test_container_prefix, "tg-proxy-name": proxy.name},
                name=container_name,
                network=config.docker_network,
            )
            return StartResult(proxy=proxy, container_name=container_name, success=True)
        except self._docker_exception as exc:
            return StartResult(
                proxy=proxy,
                container_name=container_name,
                success=False,
                error=str(exc),
            )

    def stop_and_remove(self, container_names: list[str]) -> list[str]:
        removed: list[str] = []
        for container_name in container_names:
            try:
                container = self.client.containers.get(container_name)
            except self._not_found:
                continue
            try:
                container.remove(force=True)
                removed.append(container_name)
            except self._docker_exception as exc:
                raise DockerRunnerError(f"Не удалось удалить контейнер {container_name}: {exc}") from exc
        return removed

    def exec_in_container(self, container_name: str, command: list[str]) -> tuple[int | None, str]:
        try:
            container = self.client.containers.get(container_name)
            result = container.exec_run(command, stdout=True, stderr=True)
        except self._docker_exception as exc:
            return None, str(exc)

        output = result.output
        if isinstance(output, bytes):
            text = output.decode("utf-8", errors="replace")
        else:
            text = str(output)
        return result.exit_code, text

    def fetch_new_logs(self, container_name: str, start_line: int) -> tuple[list[str], int]:
        try:
            container = self.client.containers.get(container_name)
            raw = container.logs(stdout=True, stderr=True, timestamps=False)
        except self._docker_exception:
            return [], start_line

        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start_line > len(lines):
            start_line = 0
        return lines[start_line:], len(lines)

    def get_container_status(self, container_name: str) -> str:
        try:
            container = self.client.containers.get(container_name)
            container.reload()
            return str(container.status)
        except self._not_found:
            return "not_found"
        except self._docker_exception:
            return "unknown"


def build_container_name(prefix: str, proxy_name: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", proxy_name.strip()).strip("-").lower()
    if not safe_name:
        safe_name = "proxy"
    return f"{prefix}-{safe_name}"[:63]

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .config import AppConfig, ConfigError, load_config
from .docker_runner import DockerRunner, DockerRunnerError
from .live import LiveRenderer
from .probe import Measurement, probe_get_me
from .reports import write_reports
from .stats import ProxyRuntimeStats


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)

    try:
        config = load_config(
            args.env_file,
            duration=args.duration,
            interval=args.interval,
            timeout=args.timeout,
            proxy_name=args.proxy,
            require_runtime=not args.cleanup_only,
        )
    except ConfigError as exc:
        print(f"Ошибка конфигурации:\n{exc}", file=sys.stderr)
        return 2

    try:
        runner = DockerRunner()
        runner.check_available()
    except DockerRunnerError as exc:
        print(f"Ошибка Docker:\n{exc}", file=sys.stderr)
        return 3

    if args.cleanup_only:
        try:
            removed = runner.cleanup_temp_containers(config.test_container_prefix)
        except DockerRunnerError as exc:
            print(f"Ошибка cleanup:\n{exc}", file=sys.stderr)
            return 3
        print(f"Удалено временных контейнеров: {len(removed)}")
        for name in removed:
            print(f"- {name}")
        return 0

    try:
        runner.validate_runtime(config)
    except DockerRunnerError as exc:
        print(f"Ошибка Docker runtime:\n{exc}", file=sys.stderr)
        return 3

    return run_checks(config=config, runner=runner, args=args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверка стабильности MTProto-прокси через временные docker-telegram-bot-api контейнеры.",
    )
    parser.add_argument("--env-file", default=".env", help="Путь к .env файлу")
    parser.add_argument("--duration", type=int, help="Длительность проверки в секундах")
    parser.add_argument("--interval", type=int, help="Интервал между проверками в секундах")
    parser.add_argument("--timeout", type=int, help="HTTP timeout getMe в секундах")
    parser.add_argument("--keep-containers", action="store_true", help="Не удалять временные контейнеры после завершения")
    parser.add_argument("--no-live", action="store_true", help="Не рисовать live-таблицу")
    parser.add_argument("--output-dir", default="reports", help="Папка для отчётов")
    parser.add_argument("--proxy", help="Проверить только один прокси по имени")
    parser.add_argument("--cleanup-only", action="store_true", help="Только удалить старые временные контейнеры и выйти")
    return parser


def run_checks(*, config: AppConfig, runner: DockerRunner, args: argparse.Namespace) -> int:
    started_at = datetime.now(timezone.utc)
    measurements: list[Measurement] = []
    stats = {proxy.name: ProxyRuntimeStats(proxy=proxy) for proxy in config.proxies}
    active: dict[str, str] = {}
    log_offsets: dict[str, int] = {}
    exit_code = 0

    try:
        if not args.keep_containers:
            removed = runner.cleanup_temp_containers(config.test_container_prefix)
            if removed:
                print(f"Удалены старые временные контейнеры: {', '.join(removed)}")

        print(f"Стартуем контейнеры для прокси: {len(config.proxies)}")
        for proxy in config.proxies:
            result = runner.start_proxy_container(config, proxy)
            item = stats[proxy.name]
            if result.success:
                item.record_started(result.container_name)
                active[proxy.name] = result.container_name
                log_offsets[result.container_name] = 0
                print(f"- {proxy.name}: контейнер {result.container_name} запущен")
            else:
                item.record_start_failure(result.container_name, result.error)
                print(f"- {proxy.name}: не стартовал: {result.error}", file=sys.stderr)
                exit_code = 1

        if active:
            print(f"Ждём startup: {config.startup_wait_seconds} сек.")
            time.sleep(config.startup_wait_seconds)

        deadline = time.monotonic() + config.duration_seconds
        with LiveRenderer(enabled=not args.no_live) as live:
            live.update(list(stats.values()))
            while active and time.monotonic() < deadline:
                cycle_started = time.monotonic()
                _collect_logs(runner, stats, active, log_offsets)

                _probe_getme_cycle(config, runner, stats, active, measurements)

                _collect_logs(runner, stats, active, log_offsets)
                live.update(list(stats.values()))

                sleep_for = config.check_interval_seconds - (time.monotonic() - cycle_started)
                if sleep_for > 0 and time.monotonic() + sleep_for < deadline:
                    time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nCtrl+C пойман, сохраняю отчёты и завершаюсь корректно...")
        exit_code = 130
    finally:
        _collect_logs(runner, stats, active, log_offsets)
        ended_at = datetime.now(timezone.utc)
        paths = write_reports(
            config=config,
            stats=stats,
            measurements=measurements,
            output_dir=args.output_dir,
            started_at=started_at,
            ended_at=ended_at,
        )
        print("Отчёты сохранены:")
        for kind, path in paths.items():
            print(f"- {kind}: {path}")

        if args.keep_containers:
            print("Временные контейнеры оставлены для ручного анализа.")
        else:
            try:
                removed = runner.stop_and_remove(list(active.values()))
                print(f"Удалено временных контейнеров после теста: {len(removed)}")
            except DockerRunnerError as exc:
                print(f"Ошибка удаления временных контейнеров: {exc}", file=sys.stderr)
                exit_code = 1 if exit_code == 0 else exit_code

    return exit_code


def _probe_getme_cycle(
    config: AppConfig,
    runner: DockerRunner,
    stats: dict[str, ProxyRuntimeStats],
    active: dict[str, str],
    measurements: list[Measurement],
) -> None:
    with ThreadPoolExecutor(max_workers=max(1, len(active))) as executor:
        futures = {
            executor.submit(
                probe_get_me,
                runner=runner,
                client_container=config.client_container,
                proxy=stats[proxy_name].proxy,
                proxy_container_name=container_name,
                bot_token=config.telegram_bot_token,
                timeout_seconds=config.request_timeout_seconds,
            ): proxy_name
            for proxy_name, container_name in active.items()
        }
        for future in as_completed(futures):
            proxy_name = futures[future]
            try:
                measurement = future.result()
            except Exception as exc:
                proxy = stats[proxy_name].proxy
                measurement = Measurement(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    proxy_name=proxy.name,
                    server=proxy.server,
                    port=proxy.port,
                    success=False,
                    response_time_ms=None,
                    status_code=None,
                    error_type="probe_exception",
                    error_message=str(exc),
                )
            stats[proxy_name].record_measurement(measurement)
            measurements.append(measurement)


def _collect_logs(
    runner: DockerRunner,
    stats: dict[str, ProxyRuntimeStats],
    active: dict[str, str],
    log_offsets: dict[str, int],
) -> None:
    by_container = {container_name: proxy_name for proxy_name, container_name in active.items()}
    for container_name, proxy_name in by_container.items():
        lines, next_offset = runner.fetch_new_logs(container_name, log_offsets.get(container_name, 0))
        log_offsets[container_name] = next_offset
        stats[proxy_name].record_log_lines(lines)


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())

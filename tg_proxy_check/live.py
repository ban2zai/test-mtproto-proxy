from __future__ import annotations

from .stats import ProxyRuntimeStats


class LiveRenderer:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._live = None
        self._console = None

    def __enter__(self) -> "LiveRenderer":
        if not self.enabled:
            return self
        try:
            from rich.console import Console
            from rich.live import Live
        except ImportError:
            print("rich не установлен, live-таблица отключена")
            self.enabled = False
            return self

        self._console = Console()
        self._live = Live(self._build_table([]), console=self._console, refresh_per_second=2)
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live:
            self._live.__exit__(exc_type, exc, tb)

    def update(self, stats: list[ProxyRuntimeStats]) -> None:
        if self._live:
            self._live.update(self._build_table(stats))

    def _build_table(self, stats: list[ProxyRuntimeStats]):
        from rich.table import Table

        table = Table(title="MTProto proxy stability check")
        table.add_column("name")
        table.add_column("server:port")
        table.add_column("attempts", justify="right")
        table.add_column("total", justify="right")
        table.add_column("ok", justify="right")
        table.add_column("fail", justify="right")
        table.add_column("timeout", justify="right")
        table.add_column("rate limit", justify="right")
        table.add_column("success", justify="right")
        table.add_column("avg", justify="right")
        table.add_column("p95", justify="right")
        table.add_column("max", justify="right")
        table.add_column("conn timeout", justify="right")
        table.add_column("failed conn", justify="right")
        table.add_column("last")
        table.add_column("error")

        for item in stats:
            table.add_row(
                item.proxy.name,
                f"{item.proxy.server}:{item.proxy.port}",
                str(item.attempts_total),
                str(item.checks_total),
                str(item.ok_count),
                str(item.fail_count),
                str(item.timeout_count),
                str(item.rate_limited_count),
                f"{item.success_rate:.1f}%",
                _fmt(item.avg_response_ms),
                _fmt(item.p95_response_ms),
                _fmt(item.max_response_ms),
                str(item.log_counters.connection_timeout_count),
                str(item.log_counters.failed_connection_count),
                item.last_status,
                _short(item.last_error),
            )

        return table


def _fmt(value: int | None) -> str:
    return "-" if value is None else str(value)


def _short(value: str, limit: int = 60) -> str:
    value = value.replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ProxyConfig
from .probe import Measurement


LOG_PATTERNS = {
    "connection_timeout_count": "Connection timeout expired",
    "failed_connection_count": "Failed connection",
    "flood_count": "FLOOD",
    "too_many_requests_count": "Too Many Requests",
    "unauthorized_count": "Unauthorized",
    "forbidden_count": "Forbidden",
}


@dataclass
class LogCounters:
    connection_timeout_count: int = 0
    failed_connection_count: int = 0
    flood_count: int = 0
    too_many_requests_count: int = 0
    unauthorized_count: int = 0
    forbidden_count: int = 0
    other_error_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


@dataclass
class ProxyRuntimeStats:
    proxy: ProxyConfig
    container_name: str = ""
    started: bool = False
    start_error: str = ""
    checks_total: int = 0
    ok_count: int = 0
    fail_count: int = 0
    timeout_count: int = 0
    response_times_ms: list[int] = field(default_factory=list)
    log_counters: LogCounters = field(default_factory=LogCounters)
    last_status: str = "pending"
    last_error: str = ""

    def record_start_failure(self, container_name: str, error: str) -> None:
        self.container_name = container_name
        self.started = False
        self.start_error = error
        self.last_status = "start_failed"
        self.last_error = error[:500]

    def record_started(self, container_name: str) -> None:
        self.container_name = container_name
        self.started = True
        self.last_status = "started"

    def record_measurement(self, measurement: Measurement) -> None:
        self.checks_total += 1
        if measurement.success:
            self.ok_count += 1
            self.last_status = "ok"
            self.last_error = ""
        else:
            self.fail_count += 1
            self.last_status = "fail"
            self.last_error = measurement.error_message or measurement.error_type
            if measurement.error_type == "timeout":
                self.timeout_count += 1
        if measurement.response_time_ms is not None:
            self.response_times_ms.append(measurement.response_time_ms)

    def record_log_lines(self, lines: list[str]) -> None:
        for line in lines:
            matched = False
            for field_name, pattern in LOG_PATTERNS.items():
                if pattern in line:
                    setattr(self.log_counters, field_name, getattr(self.log_counters, field_name) + 1)
                    matched = True
            if "Error" in line and not matched:
                self.log_counters.other_error_count += 1

    @property
    def success_rate(self) -> float:
        if self.checks_total == 0:
            return 0.0
        return self.ok_count / self.checks_total * 100

    @property
    def avg_response_ms(self) -> int | None:
        if not self.response_times_ms:
            return None
        return round(sum(self.response_times_ms) / len(self.response_times_ms))

    @property
    def p95_response_ms(self) -> int | None:
        return percentile(self.response_times_ms, 95)

    @property
    def max_response_ms(self) -> int | None:
        if not self.response_times_ms:
            return None
        return max(self.response_times_ms)

    def as_dict(self) -> dict:
        return {
            "name": self.proxy.name,
            "server": self.proxy.server,
            "port": self.proxy.port,
            "container_name": self.container_name,
            "started": self.started,
            "start_error": self.start_error,
            "checks_total": self.checks_total,
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "timeout_count": self.timeout_count,
            "success_rate": round(self.success_rate, 2),
            "avg_response_ms": self.avg_response_ms,
            "p95_response_ms": self.p95_response_ms,
            "max_response_ms": self.max_response_ms,
            "log_counters": self.log_counters.as_dict(),
            "last_status": self.last_status,
            "last_error": self.last_error,
        }


def percentile(values: list[int], percentile_value: int) -> int | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = round((percentile_value / 100) * (len(sorted_values) - 1))
    return sorted_values[index]


def rank_stats(stats: list[ProxyRuntimeStats]) -> list[ProxyRuntimeStats]:
    return sorted(
        stats,
        key=lambda item: (
            item.timeout_count + item.fail_count,
            item.log_counters.connection_timeout_count + item.log_counters.failed_connection_count,
            -item.success_rate,
            item.p95_response_ms if item.p95_response_ms is not None else 10**12,
            item.max_response_ms if item.max_response_ms is not None else 10**12,
        ),
    )

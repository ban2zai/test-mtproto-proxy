from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .probe import Measurement
from .stats import ProxyRuntimeStats, rank_stats


def write_reports(
    *,
    config: AppConfig,
    stats: dict[str, ProxyRuntimeStats],
    measurements: list[Measurement],
    output_dir: str,
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, Path]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%d_%H%M%S")

    json_path = target / f"tg_proxy_check_{stamp}.json"
    csv_path = target / f"tg_proxy_check_{stamp}.csv"
    md_path = target / f"tg_proxy_check_{stamp}.md"

    ordered_stats = list(stats.values())
    ranking = rank_stats(ordered_stats)

    payload = {
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "config": config.safe_dict(),
        "stats": [item.as_dict() for item in ordered_stats],
        "ranking": [item.proxy.name for item in ranking],
        "measurements_count": len(measurements),
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "timestamp",
                "proxy_name",
                "server",
                "port",
                "success",
                "response_time_ms",
                "status_code",
                "error_type",
                "error_message",
            ],
        )
        writer.writeheader()
        for measurement in measurements:
            writer.writerow(asdict(measurement))

    md_path.write_text(
        _render_markdown(config=config, ranking=ranking, started_at=started_at, ended_at=ended_at),
        encoding="utf-8",
    )

    return {"json": json_path, "csv": csv_path, "markdown": md_path}


def _render_markdown(
    *,
    config: AppConfig,
    ranking: list[ProxyRuntimeStats],
    started_at: datetime,
    ended_at: datetime,
) -> str:
    lines = [
        "# MTProto proxy check summary",
        "",
        f"- Started: `{started_at.isoformat()}`",
        f"- Ended: `{ended_at.isoformat()}`",
        f"- Duration config: `{config.duration_seconds}` sec",
        f"- Interval: `{config.check_interval_seconds}` sec",
        f"- Timeout: `{config.request_timeout_seconds}` sec",
        "",
        "## Ranking",
        "",
        "| # | Proxy | Endpoint | Success | Fail | Timeout | Success rate | Avg ms | P95 ms | Max ms | Conn timeout logs | Failed conn logs | Last status |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    for index, item in enumerate(ranking, start=1):
        lines.append(
            "| {index} | {name} | {endpoint} | {ok} | {fail} | {timeout} | {rate:.2f}% | {avg} | {p95} | {max_ms} | {conn_timeout} | {failed_conn} | {last_status} |".format(
                index=index,
                name=item.proxy.name,
                endpoint=f"{item.proxy.server}:{item.proxy.port}",
                ok=item.ok_count,
                fail=item.fail_count,
                timeout=item.timeout_count,
                rate=item.success_rate,
                avg=_fmt(item.avg_response_ms),
                p95=_fmt(item.p95_response_ms),
                max_ms=_fmt(item.max_response_ms),
                conn_timeout=item.log_counters.connection_timeout_count,
                failed_conn=item.log_counters.failed_connection_count,
                last_status=item.last_status,
            )
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Лучший прокси выбирается не только по скорости: сначала учитываются timeout/fail и проблемные строки в логах.",
            "- Секреты и Telegram token в отчёт не записываются.",
            "- Инструмент работает только с временными контейнерами своего prefix.",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value: int | None) -> str:
    return "-" if value is None else str(value)

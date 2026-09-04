"""Probe public Console routes, Cowork, and the website with one-retry debounce.

The endpoints and body markers live in ``config/console-route-probe.json`` so
operators can review the whole coverage contract without reading this module.
Only endpoints that fail the first attempt are retried. A transient failure is
therefore visible in the run log but does not fail the workflow or alert Slack.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "console-route-probe.json"
)
DEFAULT_ALERT_LABEL = "public web probe"
MAX_BODY_BYTES = 64 * 1024
MAX_NETWORK_ERROR_CHARS = 64
MAX_ALERT_LABEL_CHARS = 2_000


@dataclass(frozen=True)
class Environment:
    """One named Console deployment."""

    name: str
    base_url: str


@dataclass(frozen=True)
class Endpoint:
    """One public URL and the body marker it must return."""

    environment: str
    base_url: str
    route: str
    body_marker: str
    marker_label: str

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.route}"


@dataclass(frozen=True)
class ProbeConfig:
    """Validated route-probe configuration."""

    spa_marker: str
    environments: tuple[Environment, ...]
    routes: tuple[str, ...]
    standalone_endpoints: tuple[Endpoint, ...]

    @property
    def console_endpoints(self) -> tuple[Endpoint, ...]:
        return tuple(
            Endpoint(
                environment.name,
                environment.base_url,
                route,
                self.spa_marker,
                "SPA",
            )
            for environment in self.environments
            for route in self.routes
        )

    @property
    def endpoints(self) -> tuple[Endpoint, ...]:
        return (*self.console_endpoints, *self.standalone_endpoints)


@dataclass(frozen=True)
class FetchResult:
    """The response fields needed to decide whether an endpoint is healthy."""

    status: int | None
    body: str = ""
    error: str | None = None


@dataclass(frozen=True)
class Failure:
    """One endpoint that did not return its expected body marker."""

    endpoint: Endpoint
    reason: str

    @property
    def summary(self) -> str:
        return f"{self.endpoint.environment} {self.endpoint.route} {self.reason}"


@dataclass(frozen=True)
class ProbeOutcome:
    """Both attempts, retained so the CLI can explain transient recovery."""

    endpoint_count: int
    first_failures: tuple[Failure, ...]
    final_failures: tuple[Failure, ...]

    @property
    def passed(self) -> bool:
        return not self.final_failures


Fetch = Callable[[Endpoint, float], FetchResult]
Sleeper = Callable[[float], None]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return redirects to the caller instead of following them."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    return value


def _require_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_https_origin(value: object, field: str) -> str:
    origin = _require_string(value, field)
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError(f"{field} must be an HTTPS origin")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{field} must be an HTTPS origin")
    return origin.rstrip("/")


def _require_route(value: object, field: str) -> str:
    route = _require_string(value, field)
    if not route.startswith("/") or "?" in route or "#" in route:
        raise ValueError(
            f"{field} must be an absolute path without a query or fragment"
        )
    return route


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ProbeConfig:
    """Load and validate the operator-owned route matrix."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("probe config must be a JSON object")

    spa_marker = _require_string(raw.get("spa_marker"), "spa_marker")
    raw_environments = _require_list(raw.get("environments"), "environments")
    raw_routes = _require_list(raw.get("routes"), "routes")
    raw_standalone_endpoints = _require_list(
        raw.get("standalone_endpoints"), "standalone_endpoints"
    )

    environments: list[Environment] = []
    for index, value in enumerate(raw_environments):
        if not isinstance(value, dict):
            raise ValueError(f"environments[{index}] must be an object")
        name = _require_string(value.get("name"), f"environments[{index}].name")
        base_url = _require_https_origin(
            value.get("base_url"), f"environments[{index}].base_url"
        )
        environments.append(Environment(name=name, base_url=base_url))

    if len({environment.name for environment in environments}) != len(environments):
        raise ValueError("environment names must be unique")
    if len({environment.base_url for environment in environments}) != len(environments):
        raise ValueError("environment base URLs must be unique")

    routes: list[str] = []
    for index, value in enumerate(raw_routes):
        routes.append(_require_route(value, f"routes[{index}]"))
    if len(set(routes)) != len(routes):
        raise ValueError("routes must be unique")

    standalone_endpoints: list[Endpoint] = []
    for index, value in enumerate(raw_standalone_endpoints):
        field = f"standalone_endpoints[{index}]"
        endpoint = _require_object(value, field)
        standalone_endpoints.append(
            Endpoint(
                environment=_require_string(endpoint.get("name"), f"{field}.name"),
                base_url=_require_https_origin(
                    endpoint.get("base_url"), f"{field}.base_url"
                ),
                route=_require_route(endpoint.get("route"), f"{field}.route"),
                body_marker=_require_string(
                    endpoint.get("body_marker"), f"{field}.body_marker"
                ),
                marker_label=_require_string(
                    endpoint.get("marker_label"), f"{field}.marker_label"
                ),
            )
        )

    endpoint_names = [environment.name for environment in environments]
    endpoint_names.extend(endpoint.environment for endpoint in standalone_endpoints)
    if len(set(endpoint_names)) != len(endpoint_names):
        raise ValueError("endpoint names must be unique")

    config = ProbeConfig(
        spa_marker=spa_marker,
        environments=tuple(environments),
        routes=tuple(routes),
        standalone_endpoints=tuple(standalone_endpoints),
    )
    if len({endpoint.url for endpoint in config.endpoints}) != len(config.endpoints):
        raise ValueError("configured endpoint URLs must be unique")
    return config


def _decode_body(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def fetch_endpoint(endpoint: Endpoint, timeout_seconds: float) -> FetchResult:
    """GET one endpoint with TLS validation and redirects disabled."""

    opener = urllib.request.build_opener(NoRedirectHandler())
    request = urllib.request.Request(
        endpoint.url,
        headers={
            "Accept": "text/html",
            "User-Agent": "MindsHub-public-web-probe/1.0",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return FetchResult(
                status=int(response.getcode()),
                body=_decode_body(response.read(MAX_BODY_BYTES)),
            )
    except urllib.error.HTTPError as error:
        return FetchResult(
            status=int(error.code),
            body=_decode_body(error.read(MAX_BODY_BYTES)),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return FetchResult(status=None, error=_compact(str(error)))


def _compact(value: str, limit: int = 160) -> str:
    compacted = " ".join(value.split()) or "unknown error"
    compacted = compacted.replace("\\", "/").replace('"', "'")
    return compacted[:limit]


def evaluate(endpoint: Endpoint, result: FetchResult) -> Failure | None:
    """Apply the endpoint's exact-status and body-marker contract."""

    if result.error is not None:
        return Failure(
            endpoint,
            f"network error: {_compact(result.error, limit=MAX_NETWORK_ERROR_CHARS)}",
        )
    if result.status != 200:
        status = "unknown" if result.status is None else str(result.status)
        return Failure(endpoint, f"status {status}")
    if endpoint.body_marker not in result.body:
        return Failure(endpoint, f"status 200 missing {endpoint.marker_label} marker")
    return None


def check_endpoints(
    endpoints: Sequence[Endpoint],
    *,
    timeout_seconds: float,
    max_workers: int,
    fetcher: Fetch,
) -> tuple[Failure, ...]:
    """Probe endpoints concurrently while preserving configuration order."""

    if not endpoints:
        return ()
    worker_count = min(max_workers, len(endpoints))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetcher, endpoint, timeout_seconds)
            for endpoint in endpoints
        ]
        results = [future.result() for future in futures]

    failures = (
        failure
        for endpoint, result in zip(endpoints, results, strict=True)
        if (failure := evaluate(endpoint, result)) is not None
    )
    return tuple(failures)


def run_probe(
    config: ProbeConfig,
    *,
    retry_delay_seconds: float,
    timeout_seconds: float,
    max_workers: int,
    fetcher: Fetch = fetch_endpoint,
    sleeper: Sleeper = time.sleep,
) -> ProbeOutcome:
    """Run the full matrix once, then retry only the failed endpoints."""

    endpoints = config.endpoints
    first_failures = check_endpoints(
        endpoints,
        timeout_seconds=timeout_seconds,
        max_workers=max_workers,
        fetcher=fetcher,
    )
    if not first_failures:
        return ProbeOutcome(len(endpoints), (), ())

    sleeper(retry_delay_seconds)
    retry_endpoints = tuple(failure.endpoint for failure in first_failures)
    final_failures = check_endpoints(
        retry_endpoints,
        timeout_seconds=timeout_seconds,
        max_workers=max_workers,
        fetcher=fetcher,
    )
    return ProbeOutcome(len(endpoints), first_failures, final_failures)


def format_alert_label(failures: Sequence[Failure]) -> str:
    """Fit every actionable failure into the notifier's bounded label."""

    if not failures:
        return DEFAULT_ALERT_LABEL

    label_prefix = "public routes: "
    separator = "; "
    full_label = label_prefix + separator.join(failure.summary for failure in failures)
    if len(full_label) <= MAX_ALERT_LABEL_CHARS:
        return full_label

    # A broad DNS or TLS outage can put the maximum-length network detail on all
    # 21 endpoints. The full details remain in the workflow log; the Slack label
    # keeps every endpoint identity and its actionable result category.
    compact_summaries = separator.join(
        f"{failure.endpoint.environment} {failure.endpoint.route} "
        f"{_compact_alert_reason(failure.reason)}"
        for failure in failures
    )
    compact_label = label_prefix + compact_summaries
    if len(compact_label) > MAX_ALERT_LABEL_CHARS:
        raise ValueError("failure identities exceed the alert-label size limit")
    return compact_label


def _compact_alert_reason(reason: str) -> str:
    network_prefix = "network error"
    if reason.startswith(f"{network_prefix}: "):
        return network_prefix
    return reason


def write_github_output(path: str | None, *, alert_label: str) -> None:
    """Expose a single-line label to the terminal notification job."""

    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(
            f"alert_label={_compact(alert_label, limit=MAX_ALERT_LABEL_CHARS)}\n"
        )


def _print_failures(prefix: str, failures: Iterable[Failure]) -> None:
    print(prefix)
    for failure in failures:
        print(f"  - {failure.summary}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=float(os.environ.get("PROBE_RETRY_DELAY_SECONDS", "15")),
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args(argv)
    if args.retry_delay_seconds < 0:
        parser.error("--retry-delay-seconds must be non-negative")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_workers <= 0:
        parser.error("--max-workers must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        label = _compact(f"public routes: configuration error: {error}")
        write_github_output(args.github_output, alert_label=label)
        print(label)
        return 2

    outcome = run_probe(
        config,
        retry_delay_seconds=args.retry_delay_seconds,
        timeout_seconds=args.timeout_seconds,
        max_workers=args.max_workers,
    )
    if outcome.passed:
        write_github_output(args.github_output, alert_label=DEFAULT_ALERT_LABEL)
        if outcome.first_failures:
            _print_failures(
                "First attempt failed; every failed endpoint recovered on retry:",
                outcome.first_failures,
            )
        print(
            f"PASS: {outcome.endpoint_count} public endpoints returned status 200 "
            "and their expected body marker."
        )
        return 0

    label = format_alert_label(outcome.final_failures)
    write_github_output(args.github_output, alert_label=label)
    _print_failures("FAIL: endpoints failed both attempts:", outcome.final_failures)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

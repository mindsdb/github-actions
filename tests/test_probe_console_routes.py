"""Tests for the public Console, Cowork, and website probe contract."""

import importlib.util
import json
import re
import sys
from collections import Counter
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "probe_console_routes.py"
CONFIG_PATH = ROOT / "config" / "console-route-probe.json"
README_PATH = ROOT / "README.md"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "console-route-probe.yml"

_spec = importlib.util.spec_from_file_location("probe_console_routes", SCRIPT_PATH)
probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


EXPECTED_ENVIRONMENTS = (
    ("production", "https://console.mindshub.ai"),
    ("staging", "https://console.staging.mindshub.ai"),
)
EXPECTED_ROUTES = (
    "/",
    "/home",
    "/cowork",
    "/cowork-web",
    "/login",
    "/settings",
    "/billing",
    "/projects",
    "/assets/",
)
CONSOLE_MARKER = '<div id="root"></div>'
EXPECTED_STANDALONE_ENDPOINTS = (
    (
        "production Cowork",
        "https://cowork.mindshub.ai",
        "/",
        CONSOLE_MARKER,
        "SPA",
    ),
    (
        "staging Cowork",
        "https://cowork.staging.mindshub.ai",
        "/",
        CONSOLE_MARKER,
        "SPA",
    ),
    (
        "production website",
        "https://mindshub.ai",
        "/",
        '<meta property="og:site_name" content="MindsHub by MindsDB">',
        "website",
    ),
)
EXPECTED_CONSOLE_ENDPOINT_COUNT = len(EXPECTED_ENVIRONMENTS) * len(EXPECTED_ROUTES)
EXPECTED_ENDPOINT_COUNT = EXPECTED_CONSOLE_ENDPOINT_COUNT + len(
    EXPECTED_STANDALONE_ENDPOINTS
)
SPA_SHELL = """<!doctype html><html><body><div id="root"></div></body></html>"""
WEBSITE_PAGE = (
    "<!doctype html><html><head>"
    '<meta property="og:site_name" content="MindsHub by MindsDB">'
    "</head></html>"
)
NGINX_PAGE = """<html><body><center><h1>404 Not Found</h1></center><hr><center>nginx</center></body></html>"""


def endpoint(route: str = "/home"):
    return probe.Endpoint(
        "staging",
        "https://console.staging.mindshub.ai",
        route,
        CONSOLE_MARKER,
        "SPA",
    )


def standalone_endpoint(base_url):
    return next(
        target
        for target in probe.load_config().standalone_endpoints
        if target.base_url == base_url
    )


def cowork_endpoint():
    return standalone_endpoint("https://cowork.mindshub.ai")


def staging_cowork_endpoint():
    return standalone_endpoint("https://cowork.staging.mindshub.ai")


def website_endpoint():
    return standalone_endpoint("https://mindshub.ai")


def response(status: int = 200, body: str = SPA_SHELL):
    return probe.FetchResult(status=status, body=body)


class RecordedFetcher:
    """Return recorded responses by URL and retain every attempted endpoint."""

    def __init__(self, recordings):
        self.recordings = {url: list(values) for url, values in recordings.items()}
        self.calls = []

    def __call__(self, target, timeout_seconds):
        self.calls.append((target.url, timeout_seconds))
        values = self.recordings.get(target.url)
        if values:
            return values.pop(0)
        return response(body=target.body_marker)


class FakeHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def getcode(self):
        return self.status

    def read(self, limit):
        return self.body[:limit]


class TestResponseContract:
    def test_200_spa_shell_passes(self):
        assert probe.evaluate(endpoint(), response()) is None

    def test_200_cowork_spa_shell_passes(self):
        assert probe.evaluate(cowork_endpoint(), response()) is None

    def test_200_staging_cowork_spa_shell_passes(self):
        assert probe.evaluate(staging_cowork_endpoint(), response()) is None

    def test_200_website_marker_passes(self):
        assert probe.evaluate(website_endpoint(), response(body=WEBSITE_PAGE)) is None

    def test_200_spa_shell_fails_for_the_website(self):
        failure = probe.evaluate(website_endpoint(), response())
        assert (
            failure.summary == "production website / status 200 missing website marker"
        )

    def test_403_fails_with_the_status(self):
        failure = probe.evaluate(endpoint(), response(403, "Forbidden"))
        assert failure.summary == "staging /home status 403"

    def test_301_fails_instead_of_counting_as_the_destination(self):
        failure = probe.evaluate(endpoint(), response(301, "Moved"))
        assert failure.summary == "staging /home status 301"

    def test_200_nginx_page_fails_without_the_spa_marker(self):
        failure = probe.evaluate(endpoint(), response(200, NGINX_PAGE))
        assert failure.summary == "staging /home status 200 missing SPA marker"

    def test_network_error_is_actionable(self):
        failure = probe.evaluate(
            endpoint(),
            probe.FetchResult(status=None, error="timed out"),
        )
        assert failure.summary == "staging /home network error: timed out"

    def test_fetch_installs_the_no_redirect_handler(self, monkeypatch):
        captured = []

        class Opener:
            def open(self, request, timeout):
                return FakeHTTPResponse(200, SPA_SHELL)

        def build_opener(*handlers):
            captured.extend(handlers)
            return Opener()

        monkeypatch.setattr(probe.urllib.request, "build_opener", build_opener)
        assert probe.fetch_endpoint(endpoint(), 4).status == 200
        assert len(captured) == 1
        assert isinstance(captured[0], probe.NoRedirectHandler)
        assert captured[0].redirect_request(None) is None

    def test_fetch_returns_a_redirect_as_a_301_response(self, monkeypatch):
        class RedirectingOpener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 301, "Moved", {}, BytesIO(b"Moved"))

        monkeypatch.setattr(
            probe.urllib.request, "build_opener", lambda *handlers: RedirectingOpener()
        )
        result = probe.fetch_endpoint(endpoint(), 4)
        assert (result.status, result.body) == (301, "Moved")


class TestRetryDebounce:
    def test_a_clean_first_attempt_does_not_sleep_or_retry(self):
        sleeper_calls = []
        fetcher = RecordedFetcher({})
        outcome = probe.run_probe(
            probe.load_config(),
            retry_delay_seconds=7,
            timeout_seconds=4,
            max_workers=1,
            fetcher=fetcher,
            sleeper=sleeper_calls.append,
        )
        assert outcome.passed
        assert outcome.first_failures == ()
        assert len(fetcher.calls) == EXPECTED_ENDPOINT_COUNT
        assert sleeper_calls == []

    def test_one_failed_attempt_then_success_is_green(self):
        target = endpoint().url
        fetcher = RecordedFetcher({target: [response(403, "Forbidden"), response()]})
        sleeper_calls = []
        outcome = probe.run_probe(
            probe.load_config(),
            retry_delay_seconds=7,
            timeout_seconds=4,
            max_workers=1,
            fetcher=fetcher,
            sleeper=sleeper_calls.append,
        )
        assert outcome.passed
        assert [failure.summary for failure in outcome.first_failures] == [
            "staging /home status 403"
        ]
        assert outcome.final_failures == ()
        assert sleeper_calls == [7]
        assert Counter(url for url, _ in fetcher.calls)[target] == 2
        assert len(fetcher.calls) == EXPECTED_ENDPOINT_COUNT + 1

    def test_website_failure_then_success_is_green(self):
        target = website_endpoint().url
        fetcher = RecordedFetcher(
            {target: [response(200, SPA_SHELL), response(200, WEBSITE_PAGE)]}
        )
        outcome = probe.run_probe(
            probe.load_config(),
            retry_delay_seconds=0,
            timeout_seconds=4,
            max_workers=1,
            fetcher=fetcher,
            sleeper=lambda _: None,
        )
        assert outcome.passed
        assert [failure.summary for failure in outcome.first_failures] == [
            "production website / status 200 missing website marker"
        ]
        assert Counter(url for url, _ in fetcher.calls)[target] == 2

    def test_the_retry_contains_only_failed_endpoints(self):
        first = endpoint("/home").url
        second = endpoint("/assets/").url
        fetcher = RecordedFetcher(
            {
                first: [response(403, "Forbidden"), response()],
                second: [response(301, "Moved"), response()],
            }
        )
        outcome = probe.run_probe(
            probe.load_config(),
            retry_delay_seconds=0,
            timeout_seconds=4,
            max_workers=1,
            fetcher=fetcher,
            sleeper=lambda _: None,
        )
        counts = Counter(url for url, _ in fetcher.calls)
        assert outcome.passed
        assert counts[first] == counts[second] == 2
        assert all(
            count == 1 for url, count in counts.items() if url not in {first, second}
        )

    def test_two_failed_attempts_exit_with_the_last_concise_reason(self):
        target = endpoint().url
        fetcher = RecordedFetcher(
            {target: [response(403, "Forbidden"), response(200, NGINX_PAGE)]}
        )
        outcome = probe.run_probe(
            probe.load_config(),
            retry_delay_seconds=0,
            timeout_seconds=4,
            max_workers=1,
            fetcher=fetcher,
            sleeper=lambda _: None,
        )
        assert not outcome.passed
        assert [failure.summary for failure in outcome.final_failures] == [
            "staging /home status 200 missing SPA marker"
        ]
        assert probe.format_alert_label(outcome.final_failures) == (
            "public routes: staging /home status 200 missing SPA marker"
        )

    def test_one_alert_label_names_every_persistent_failure(self, tmp_path):
        failures = tuple(
            probe.Failure(target, "status 503")
            for target in probe.load_config().endpoints
        )
        label = probe.format_alert_label(failures)
        output = tmp_path / "github-output"
        probe.write_github_output(str(output), alert_label=label)

        assert len(label) <= probe.MAX_ALERT_LABEL_CHARS
        for failure in failures:
            assert failure.summary in label
        assert label.count("status 503") == EXPECTED_ENDPOINT_COUNT
        assert output.read_text(encoding="utf-8") == f"alert_label={label}\n"

    def test_network_outage_label_keeps_all_21_endpoints_and_results(self):
        failures = tuple(
            probe.evaluate(
                target,
                probe.FetchResult(
                    status=None,
                    error="x" * (probe.MAX_NETWORK_ERROR_CHARS + 10),
                ),
            )
            for target in probe.load_config().endpoints
        )
        assert all(failures)

        label = probe.format_alert_label(failures)

        assert len(label) <= probe.MAX_ALERT_LABEL_CHARS
        assert label.count("network error") == EXPECTED_ENDPOINT_COUNT
        for failure in failures:
            endpoint_result = (
                f"{failure.endpoint.environment} {failure.endpoint.route} network error"
            )
            assert endpoint_result in label

    def test_transient_outcome_makes_main_exit_green_and_write_a_quiet_label(
        self, tmp_path, monkeypatch
    ):
        output = tmp_path / "github-output"
        failure = probe.Failure(endpoint(), "status 403")
        monkeypatch.setattr(
            probe,
            "run_probe",
            lambda *args, **kwargs: probe.ProbeOutcome(
                EXPECTED_ENDPOINT_COUNT, (failure,), ()
            ),
        )
        assert (
            probe.main(["--github-output", str(output), "--retry-delay-seconds", "0"])
            == 0
        )
        assert output.read_text(encoding="utf-8") == "alert_label=public web probe\n"

    def test_persistent_outcome_makes_main_exit_red_and_write_failure_details(
        self, tmp_path, monkeypatch
    ):
        output = tmp_path / "github-output"
        failure = probe.Failure(endpoint(), "status 403")
        monkeypatch.setattr(
            probe,
            "run_probe",
            lambda *args, **kwargs: probe.ProbeOutcome(
                EXPECTED_ENDPOINT_COUNT, (failure,), (failure,)
            ),
        )
        assert (
            probe.main(["--github-output", str(output), "--retry-delay-seconds", "0"])
            == 1
        )
        assert output.read_text(encoding="utf-8") == (
            "alert_label=public routes: staging /home status 403\n"
        )

    def test_output_is_one_json_safe_line_for_the_slack_payload(self, tmp_path):
        output = tmp_path / "github-output"
        probe.write_github_output(
            str(output), alert_label='bad "host"\ncertificate \\ mismatch'
        )
        assert output.read_text(encoding="utf-8") == (
            "alert_label=bad 'host' certificate / mismatch\n"
        )


class TestConfigurationAndDocumentation:
    def test_config_is_the_exact_console_matrix_plus_standalone_roots(self):
        config = probe.load_config()
        environments = tuple((item.name, item.base_url) for item in config.environments)
        assert environments == EXPECTED_ENVIRONMENTS
        assert config.routes == EXPECTED_ROUTES
        assert len(config.console_endpoints) == EXPECTED_CONSOLE_ENDPOINT_COUNT
        assert (
            len({item.url for item in config.console_endpoints})
            == EXPECTED_CONSOLE_ENDPOINT_COUNT
        )
        standalone = tuple(
            (
                item.environment,
                item.base_url,
                item.route,
                item.body_marker,
                item.marker_label,
            )
            for item in config.standalone_endpoints
        )
        assert standalone == EXPECTED_STANDALONE_ENDPOINTS
        assert len(config.endpoints) == EXPECTED_ENDPOINT_COUNT
        assert len({item.url for item in config.endpoints}) == EXPECTED_ENDPOINT_COUNT

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        (
            (
                "name",
                "",
                "standalone_endpoints[1].name must be a non-empty string",
            ),
            ("name", "production", "endpoint names must be unique"),
            (
                "base_url",
                "http://mindshub.ai",
                "standalone_endpoints[1].base_url must be an HTTPS origin",
            ),
            (
                "base_url",
                "https://console.mindshub.ai",
                "configured endpoint URLs must be unique",
            ),
            (
                "route",
                "/?preview=true",
                "standalone_endpoints[1].route must be an absolute path without a query or fragment",
            ),
            (
                "body_marker",
                "",
                "standalone_endpoints[1].body_marker must be a non-empty string",
            ),
            (
                "marker_label",
                "",
                "standalone_endpoints[1].marker_label must be a non-empty string",
            ),
        ),
    )
    def test_invalid_standalone_contract_is_rejected(
        self, tmp_path, field, value, message
    ):
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw["standalone_endpoints"][1][field] = value
        config_path = tmp_path / "invalid-standalone.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(ValueError, match=re.escape(message)):
            probe.load_config(config_path)

    def test_each_standalone_endpoint_must_be_an_object(self, tmp_path):
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw["standalone_endpoints"][1] = []
        config_path = tmp_path / "invalid-standalone.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(
            ValueError, match=r"standalone_endpoints\[1\] must be an object"
        ):
            probe.load_config(config_path)

    def test_readme_environment_and_route_lists_match_the_config(self):
        readme = README_PATH.read_text(encoding="utf-8")
        environment_block = re.search(
            r"<!-- console-route-probe-environments:start -->(.*?)"
            r"<!-- console-route-probe-environments:end -->",
            readme,
            re.DOTALL,
        ).group(1)
        route_block = re.search(
            r"<!-- console-route-probe-routes:start -->(.*?)"
            r"<!-- console-route-probe-routes:end -->",
            readme,
            re.DOTALL,
        ).group(1)
        documented_environments = tuple(
            re.findall(r"^- `([^`]+)`: `([^`]+)`$", environment_block, re.MULTILINE)
        )
        documented_routes = tuple(
            re.findall(r"^- `([^`]+)`$", route_block, re.MULTILINE)
        )
        assert documented_environments == EXPECTED_ENVIRONMENTS
        assert documented_routes == EXPECTED_ROUTES
        standalone_block = re.search(
            r"<!-- standalone-public-probes:start -->(.*?)"
            r"<!-- standalone-public-probes:end -->",
            readme,
            re.DOTALL,
        ).group(1)
        for name, base_url, route, body_marker, _ in EXPECTED_STANDALONE_ENDPOINTS:
            assert f"- `{name}`: `{base_url}{route}` requires `{body_marker}`" in (
                standalone_block
            )

    def test_readme_records_the_tier_and_cadence_decisions(self):
        readme = README_PATH.read_text(encoding="utf-8")
        for contract in (
            "| Public uptime | 60 seconds | Cloudflare Health Checks |",
            "| Public route matrix | 5 minutes | This GitHub Actions workflow |",
            "| Staging integration | Nightly | Each service repository |",
            "| Production smoke | Nightly | `cowork-server` |",
            "| Certificates | Existing | Prometheus |",
            "An authenticated 15-minute browser journey is not scheduled by this work.",
        ):
            assert contract in readme

    def test_readme_blocks_activation_until_eng_2317_is_live(self):
        readme = README_PATH.read_text(encoding="utf-8")

        assert "ENG-2317 is the activation gate." in readme
        assert "Do not merge or promote this workflow" in readme
        assert "passes all 18 Console environment and route pairs" in readme
        assert f"all {EXPECTED_ENDPOINT_COUNT} public endpoints" in readme

    def test_workflow_runs_every_five_minutes_and_keeps_the_notify_terminal(self):
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        assert workflow["name"] == "Public web probe"
        triggers = workflow.get("on", workflow.get(True))
        assert triggers == {
            "schedule": [{"cron": "*/5 * * * *"}],
            "workflow_dispatch": None,
        }
        assert set(workflow["jobs"]) == {"probe", "notify"}
        notify = workflow["jobs"]["notify"]
        probe_job = workflow["jobs"]["probe"]
        probe_step = next(
            step for step in probe_job["steps"] if step.get("id") == "probe"
        )
        failure_step = next(
            step
            for step in probe_job["steps"]
            if step["name"] == "Mark a persistent route failure"
        )
        assert probe_step["continue-on-error"] is True
        assert probe_step["env"]["PROBE_RETRY_DELAY_SECONDS"] == "15"
        assert failure_step["if"] == "steps.probe.outcome == 'failure'"
        assert (
            probe_job["outputs"]["alert_label"]
            == "${{ steps.probe.outputs.alert_label }}"
        )
        assert notify["needs"] == ["probe"]
        assert notify["permissions"]["actions"] == "read"
        assert notify["uses"] == (
            "mindsdb/github-actions/.github/workflows/notify-main-failure.yml@main"
        )
        assert "needs.probe.outputs.alert_label" in notify["with"]["env-name"]
        assert "needs.*.result" in notify["with"]["status"]
        assert notify["secrets"] == "inherit"

    def test_json_file_contains_no_hidden_route_expansion(self):
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        assert set(raw) == {
            "spa_marker",
            "environments",
            "routes",
            "standalone_endpoints",
        }
        assert (
            len(raw["environments"]) * len(raw["routes"])
            == EXPECTED_CONSOLE_ENDPOINT_COUNT
        )
        assert (
            tuple(
                (
                    item["name"],
                    item["base_url"],
                    item["route"],
                    item["body_marker"],
                    item["marker_label"],
                )
                for item in raw["standalone_endpoints"]
            )
            == EXPECTED_STANDALONE_ENDPOINTS
        )

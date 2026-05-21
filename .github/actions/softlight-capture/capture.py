from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import runtime
import yaml

DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
DEFAULT_COMMAND_TIMEOUTS_BY_LABEL = {
    "auth": 300,
    "service": 300,
    "teardown": 120,
}
DEFAULT_APP_HOST = "127.0.0.1"
UNBOUNDED_POLL_PATTERN = re.compile(r"\b(?:while|until)\b")
TIMEOUT_WRAPPER_PATTERN = re.compile(r"(?:^|[;&|]\s*)timeout\s+[0-9]+")
HIDDEN_TMP_LOG_REDIRECT_PATTERN = re.compile(r"(?:^|\s)(?:\d?>|&>)\s*/tmp/")
TRYCLOUDFLARE_URL_PATTERN = re.compile(r"https://[a-z0-9.-]+\.trycloudflare\.com")
DEFAULT_PREVIEW_HOLD_SECONDS = 1800


@dataclasses.dataclass(frozen=True)
class RunningProcess:
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        _stop_process(self.process)


def main() -> None:
    workspace = pathlib.Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    config_path = workspace / os.environ.get("SOFTLIGHT_CONFIG_PATH", ".softlight/review.yml")
    config = _load_config(config_path)
    working_directory = (workspace / config.get("working_directory", ".")).resolve()
    env = _command_env(config)
    env = runtime.prepare_runtime_env(workspace=working_directory, env=env, config=config)

    for command in _commands(config.get("install")):
        _run_command(
            command,
            cwd=working_directory,
            env=env,
            label="install",
            timeout_seconds=_configured_command_timeout_seconds(config, "install"),
        )
    for command in _commands(config.get("services")):
        _run_command(
            command,
            cwd=working_directory,
            env=env,
            label="service",
            timeout_seconds=_configured_command_timeout_seconds(config, "service"),
        )

    start_command = _required_string(config, "start")
    port = int(config.get("port") or 3000)
    healthcheck = str(config.get("healthcheck") or "/")
    timeout_seconds = float(config.get("timeout_seconds") or 180)
    app_url = _app_url_from_config(config=config, port=port)

    auth_state_path = _auth_state_path(config=config, workspace=workspace)
    env["SOFTLIGHT_APP_URL"] = app_url
    env["SOFTLIGHT_AUTH_STATE"] = str(auth_state_path)

    app = _start_process(start_command, cwd=working_directory, env=env)
    try:
        app_url = _wait_for_app(
            app_url=app_url,
            healthcheck=healthcheck,
            timeout_seconds=timeout_seconds,
            process=app.process,
        )
        env["SOFTLIGHT_APP_URL"] = app_url
        _run_auth_setup(config=config, cwd=working_directory, env=env)
        try:
            capture_targets = _fetch_capture_plan(config=config)
        except Exception:
            if not _capture_targets(config=config):
                raise
            print(
                "softlight: capture plan failed; using configured captures from review.yml",
                file=sys.stderr,
                flush=True,
            )
            capture_targets = []
        if not capture_targets:
            capture_targets = _capture_targets(config=config)
        if not capture_targets:
            capture_targets = _default_capture_targets(config=config)

        capture_artifacts = _capture_screenshots(
            app_url=app_url,
            auth_state_path=auth_state_path,
            config=config,
            capture_targets=capture_targets,
            workspace=workspace,
        )
        payload = _review_payload(config=config, capture_artifacts=capture_artifacts)
        try:
            _post_review(payload)
        except Exception as error:  # noqa: BLE001
            if not _truthy(os.environ.get("SOFTLIGHT_REVIEW_OPTIONAL")):
                raise
            print(
                f"softlight: review post failed (SOFTLIGHT_REVIEW_OPTIONAL set; continuing): {error}",
                file=sys.stderr,
                flush=True,
            )
        _maybe_serve_preview(
            app_url=app_url,
            capture_targets=capture_targets,
            app_process=app.process,
        )
    finally:
        app.stop()
        for command in reversed(_commands(config.get("teardown"))):
            _run_command(
                command,
                cwd=working_directory,
                env=env,
                label="teardown",
                timeout_seconds=_configured_command_timeout_seconds(config, "teardown"),
            )


def _load_config(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Softlight review config not found: {path}")

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Softlight review config must be a mapping: {path}")

    return data


def _command_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    configured_env = config.get("env") or {}
    if not isinstance(configured_env, dict):
        raise ValueError("review.yml env must be a mapping when provided")

    for key, value in configured_env.items():
        env[str(key)] = os.path.expandvars(str(value))

    return env


def _commands(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return [value]

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value

    raise ValueError("review.yml command fields must be a string or list of strings")


def _required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"review.yml must define a non-empty {key!r} command")

    return value


def _run_command(
    command: str,
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    label: str,
    timeout_seconds: float | None = None,
) -> None:
    _validate_bounded_command(command=command, label=label)
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else _default_command_timeout_seconds(label)
    )
    print(f"softlight: running {label} (timeout {timeout:g}s): {command}", flush=True)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=True,
        start_new_session=True,
    )
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _stop_process(process)
        raise TimeoutError(
            f"Timed out after {timeout:g}s running {label}: {command}",
        ) from error
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def _validate_bounded_command(*, command: str, label: str) -> None:
    if UNBOUNDED_POLL_PATTERN.search(command) and not TIMEOUT_WRAPPER_PATTERN.search(command):
        raise ValueError(
            f"review.yml {label} command uses while/until polling without timeout: {command}",
        )


def _configured_command_timeout_seconds(config: dict[str, Any], label: str) -> float:
    specific_key = f"{label}_timeout_seconds"
    value = config.get(specific_key, config.get("command_timeout_seconds"))
    if value is None:
        return _default_command_timeout_seconds(label)
    return float(value)


def _default_command_timeout_seconds(label: str) -> float:
    normalized = label.removeprefix("doctor ").strip()
    return float(
        DEFAULT_COMMAND_TIMEOUTS_BY_LABEL.get(
            normalized,
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        ),
    )


def _app_url_from_config(*, config: dict[str, Any], port: int) -> str:
    configured_url = config.get("app_url") or config.get("base_url")
    if configured_url:
        value = str(configured_url).strip()
        if not value:
            raise ValueError("review.yml app_url/base_url must not be empty")
        if urllib.parse.urlparse(value).scheme not in {"http", "https"}:
            value = f"http://{value}"
        parsed = urllib.parse.urlparse(value)
        if not parsed.netloc:
            raise ValueError(f"review.yml app_url/base_url is not a valid URL: {configured_url}")
        return value.rstrip("/")

    host = str(config.get("host") or DEFAULT_APP_HOST).strip() or DEFAULT_APP_HOST
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _candidate_app_urls(app_url: str) -> list[str]:
    candidates = [app_url.rstrip("/")]
    parsed = urllib.parse.urlparse(candidates[0])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return candidates

    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost"}:
        return candidates

    alternate_host = "localhost" if host == "127.0.0.1" else "127.0.0.1"
    alternate_netloc = alternate_host
    if parsed.port is not None:
        alternate_netloc = f"{alternate_host}:{parsed.port}"
    alternate = urllib.parse.urlunparse(
        (
            parsed.scheme,
            alternate_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ),
    ).rstrip("/")
    if alternate not in candidates:
        candidates.append(alternate)
    return candidates


def _start_process(
    command: str,
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
) -> RunningProcess:
    _validate_streaming_start_command(command)
    print(f"softlight: starting app: {command}", flush=True)
    return RunningProcess(
        process=subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            shell=True,
            start_new_session=True,
        ),
    )


def _wait_for_app(
    *,
    app_url: str | None = None,
    port: int | None = None,
    healthcheck: str,
    process: subprocess.Popen[bytes] | None = None,
    timeout_seconds: float,
) -> str:
    if app_url is None:
        if port is None:
            raise ValueError("_wait_for_app requires app_url or port")
        app_url = _app_url_from_config(config={}, port=port)
    app_urls = _candidate_app_urls(app_url)
    deadline = time.monotonic() + timeout_seconds
    last_error = "not checked yet"
    last_log_at = 0.0
    first_http_error_at_by_status: dict[int, float] = {}
    http_error_retry_seconds = _http_error_retry_seconds()
    while time.monotonic() < deadline:
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(
                    "App start command exited before the app became ready "
                    f"(exit code {return_code})",
                )
        for candidate_app_url in app_urls:
            url = _target_url(app_url=candidate_app_url, route=healthcheck)
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "softlight-capture"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    if _http_status_indicates_app_ready(response.status):
                        print(f"softlight: app is ready at {url} ({response.status})", flush=True)
                        return candidate_app_url
            except urllib.error.HTTPError as error:
                if _http_status_indicates_app_ready(error.code):
                    print(f"softlight: app is ready at {url} ({error.code})", flush=True)
                    return candidate_app_url
                last_error = f"{url}: HTTP {error.code}"
                first_seen_at = first_http_error_at_by_status.setdefault(error.code, time.monotonic())
                if time.monotonic() - first_seen_at >= http_error_retry_seconds:
                    raise TimeoutError(
                        "App returned a stable non-ready HTTP status while waiting for "
                        f"{url}: HTTP {error.code} for at least {http_error_retry_seconds:g}s",
                    ) from error
            except (urllib.error.URLError, OSError) as error:
                last_error = f"{url}: {error}"
                first_http_error_at_by_status.clear()

        now = time.monotonic()
        if now - last_log_at >= 10:
            print(
                "softlight: waiting for app at "
                f"{', '.join(_target_url(app_url=url, route=healthcheck) for url in app_urls)}; "
                f"last error: {last_error}",
                flush=True,
            )
            last_log_at = now

        time.sleep(1)

    urls = ", ".join(_target_url(app_url=url, route=healthcheck) for url in app_urls)
    raise TimeoutError(f"Timed out waiting for app at {urls}: {last_error}")


def _http_status_indicates_app_ready(status: int) -> bool:
    return 200 <= status < 400 or status in {401, 403}


def _http_error_retry_seconds() -> float:
    value = os.environ.get("SOFTLIGHT_HTTP_ERROR_RETRY_SECONDS")
    if value is None:
        return 45.0
    return max(0.0, float(value))


def _validate_streaming_start_command(command: str) -> None:
    if HIDDEN_TMP_LOG_REDIRECT_PATTERN.search(command) and "tee" not in command:
        raise ValueError(
            "review.yml start command redirects logs to /tmp without tee; "
            "stream app logs to stdout/stderr so Softlight can show startup progress",
        )


def _run_auth_setup(
    *,
    config: dict[str, Any],
    cwd: pathlib.Path,
    env: dict[str, str],
) -> None:
    auth = config.get("auth") or {}
    if not isinstance(auth, dict):
        raise ValueError("review.yml auth must be a mapping when provided")

    strategy = str(auth.get("strategy") or "").strip().lower()
    if strategy in {"", "none", "public"}:
        return

    commands = (
        _commands(auth.get("command"))
        or _commands(auth.get("commands"))
        or _commands(auth.get("setup"))
    )
    if not commands:
        raise ValueError(
            "review.yml auth.strategy requires auth.command, auth.commands, or auth.setup",
        )

    for command in commands:
        _run_command(
            command,
            cwd=cwd,
            env=env,
            label="auth",
            timeout_seconds=_configured_command_timeout_seconds(config, "auth"),
        )


def _auth_state_path(
    *,
    config: dict[str, Any],
    workspace: pathlib.Path,
) -> pathlib.Path:
    auth = config.get("auth") or {}
    if not isinstance(auth, dict):
        raise ValueError("review.yml auth must be a mapping when provided")

    configured = auth.get("storage_state") or ".softlight/.auth/reviewer.json"
    path = pathlib.Path(str(configured))
    if not path.is_absolute():
        path = workspace / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_capture_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    if _truthy(os.environ.get("SOFTLIGHT_SKIP_CAPTURE_PLAN")):
        return []

    base_url = (
        os.environ.get("SOFTLIGHT_REVIEW_BASE_URL")
        or os.environ.get("SOFTLIGHT_BASE_URL")
        or "https://softlight.orianna.ai"
    ).rstrip("/")
    endpoint = os.environ.get("SOFTLIGHT_CAPTURE_PLAN_ENDPOINT", "/api/review/capture-plan")
    url = f"{base_url}/{endpoint.lstrip('/')}"
    payload = _plan_payload()
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SOFTLIGHT_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"softlight: requesting capture plan at {url}", flush=True)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(config.get("plan_timeout_seconds") or 600)) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(error.read().decode("utf-8"), file=sys.stderr)
        raise

    targets = data.get("targets") or []
    if not isinstance(targets, list):
        raise ValueError("capture plan response must contain a targets array")

    print(f"softlight: capture plan returned {len(targets)} target(s)", flush=True)
    return targets


def _capture_targets(config: dict[str, Any]) -> list[dict[str, Any]]:
    captures = config.get("captures") or []
    if not captures:
        return []
    if not isinstance(captures, list) or not all(isinstance(item, dict) for item in captures):
        raise ValueError("review.yml captures must be a list of mappings")

    return [_target_from_config_capture(capture, index=index) for index, capture in enumerate(captures, start=1)]


def _default_capture_targets(config: dict[str, Any]) -> list[dict[str, Any]]:
    viewports = config.get("viewports") or [{"width": 1440, "height": 932}]
    if not isinstance(viewports, list):
        raise ValueError("review.yml viewports must be a list when provided")

    targets = []
    for index, viewport in enumerate(viewports, start=1):
        if not isinstance(viewport, dict):
            raise ValueError("review.yml viewports entries must be mappings")
        width = int(viewport.get("width") or 1440)
        height = int(viewport.get("height") or 932)
        targets.append(
            _target(
                name=f"default route {width}x{height}",
                route="/",
                viewport_width=width,
                viewport_height=height,
                steps=[],
            ),
        )
    return targets


def _target_from_config_capture(
    capture: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    viewport = capture.get("viewport") or {}
    if not isinstance(viewport, dict):
        raise ValueError("capture viewport must be a mapping when provided")

    width = int(capture.get("width") or viewport.get("width") or 1440)
    height = int(capture.get("height") or viewport.get("height") or 932)
    route = str(capture.get("route") or capture.get("url") or "/")
    target = _target(
        name=str(capture.get("name") or f"configured capture {index}"),
        route=route,
        viewport_width=width,
        viewport_height=height,
        steps=capture.get("steps") or [],
        wait_for=capture.get("wait_for"),
    )
    for key in (
        "allow_minimal_component",
        "allow_clipped_elements",
        "allow_narrow_viewport",
        "allow_obscuring_overlays",
        "allow_visual_anomalies",
        "expected_terms",
        "min_body_text_chars",
        "min_visible_elements",
        "min_viewport_height",
        "min_viewport_width",
        "review_surface",
        "required_selectors",
        "surface_rationale",
    ):
        if key in capture:
            target[key] = capture[key]
    return target


def _target(
    *,
    name: str,
    route: str,
    viewport_width: int,
    viewport_height: int,
    steps: list[Any],
    wait_for: Any = None,
) -> dict[str, Any]:
    return {
        "auth_and_data": "Use the Softlight reviewer auth state configured during onboarding.",
        "clone_guardrails": "Captured from the real application running in the PR checkout.",
        "current_pr_expectations": "The head checkout should render the PR UI in the real app.",
        "final_state_must_show": str(wait_for or "The requested route renders meaningful UI."),
        "interaction_to_reach_state": "configured Playwright steps" if steps else "none",
        "name": name,
        "route_or_entrypoint": route,
        "ui_change_to_inspect": name,
        "user_state": "Authenticated reviewer state when auth is configured.",
        "viewport": f"{viewport_width}x{viewport_height}",
        "viewport_height": viewport_height,
        "viewport_width": viewport_width,
        "_softlight_steps": steps,
        "_softlight_wait_for": wait_for,
    }


def _capture_screenshots(
    *,
    app_url: str,
    auth_state_path: pathlib.Path,
    config: dict[str, Any],
    capture_targets: list[dict[str, Any]],
    workspace: pathlib.Path,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: PLC0415
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as error:
        raise RuntimeError(
            "Python package 'playwright' is required. The Softlight action installs it by "
            "default; local runs should use `python3 -m pip install playwright` and "
            "`python3 -m playwright install chromium`.",
        ) from error

    artifacts: list[dict[str, Any]] = []
    timeout_ms = int(float(config.get("navigation_timeout_seconds") or 60) * 1000)
    settle_ms = int(
        float(
            config.get("settle_seconds")
            or config.get("capture_settle_seconds")
            or 1,
        )
        * 1000,
    )
    storage_state = str(auth_state_path) if auth_state_path.exists() else None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for index, target in enumerate(capture_targets, start=1):
                target_for_backend = _strip_runner_fields(target)
                width = int(target_for_backend.get("viewport_width") or 1440)
                height = int(target_for_backend.get("viewport_height") or 932)
                route = str(target_for_backend.get("route_or_entrypoint") or "/")
                url = _target_url(app_url=app_url, route=route)
                print(
                    f"softlight: capturing {target_for_backend['name']} at {url} "
                    f"({width}x{height})",
                    flush=True,
                )
                context_options: dict[str, Any] = {
                    "viewport": {"width": width, "height": height},
                }
                if storage_state:
                    context_options["storage_state"] = storage_state
                context = browser.new_context(**context_options)
                _apply_route_mocks(context=context, config=config, workspace=workspace)
                page = context.new_page()
                console_messages: list[str] = []
                page_errors: list[str] = []
                page.on("console", lambda message: console_messages.append(f"{message.type}: {message.text}"))
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    _apply_steps(page=page, steps=target.get("_softlight_steps") or [], timeout_ms=timeout_ms)
                    _wait_for_target(page=page, wait_for=target.get("_softlight_wait_for"), timeout_ms=timeout_ms)
                    with contextlib.suppress(PlaywrightTimeoutError):
                        page.wait_for_load_state("networkidle", timeout=5_000)
                    page.wait_for_timeout(settle_ms)
                    _wait_for_rendered_ui(page=page, timeout_ms=timeout_ms)
                    screenshot = page.screenshot(full_page=True, type="png", timeout=timeout_ms)
                finally:
                    context.close()

                data_url = "data:image/png;base64," + base64.b64encode(screenshot).decode("ascii")
                artifacts.append(
                    {
                        "data_url": data_url,
                        "target": target_for_backend,
                        "console": console_messages[-50:],
                        "page_errors": page_errors[-20:],
                    },
                )
        finally:
            browser.close()

    print(f"softlight: captured {len(artifacts)} screenshot artifact(s)", flush=True)
    return artifacts


def _apply_route_mocks(*, context: Any, config: dict[str, Any], workspace: pathlib.Path) -> None:
    for mock in _route_mocks(config):
        pattern = str(
            mock.get("url")
            or mock.get("pattern")
            or mock.get("glob")
            or mock.get("match")
            or "",
        ).strip()
        if not pattern:
            raise ValueError("route_mocks entries must include url, pattern, glob, or match")
        method = str(mock.get("method") or "").upper().strip()
        status = int(mock.get("status") or 200)
        headers_raw = mock.get("headers")
        headers: dict[str, Any] = headers_raw if isinstance(headers_raw, dict) else {}
        content_type = str(mock.get("content_type") or mock.get("contentType") or "").strip()
        body = _route_mock_body(mock=mock, workspace=workspace)
        if not content_type:
            content_type = "application/json" if "json" in mock else "text/plain"

        def handler(
            route: Any,
            request: Any,
            *,
            route_body: str = body,
            route_content_type: str = content_type,
            route_headers: dict[str, Any] = headers,
            route_method: str = method,
            route_status: int = status,
        ) -> None:
            if route_method and str(request.method).upper() != route_method:
                route.continue_()
                return
            route.fulfill(
                status=route_status,
                headers={str(key): str(value) for key, value in route_headers.items()},
                content_type=route_content_type,
                body=route_body,
            )

        context.route(pattern, handler)


def _route_mocks(config: dict[str, Any]) -> list[dict[str, Any]]:
    route_mocks = config.get("route_mocks") or config.get("network_mocks") or []
    if isinstance(route_mocks, dict):
        route_mocks = [route_mocks]
    if not isinstance(route_mocks, list):
        raise ValueError("route_mocks must be a list of mappings")
    if not all(isinstance(mock, dict) for mock in route_mocks):
        raise ValueError("route_mocks must be a list of mappings")
    return route_mocks


def _route_mock_body(*, mock: dict[str, Any], workspace: pathlib.Path) -> str:
    if "json" in mock:
        return json.dumps(mock["json"])
    if "body" in mock:
        body = mock["body"]
        return body if isinstance(body, str) else json.dumps(body)
    fixture = mock.get("fixture") or mock.get("file") or mock.get("path")
    if fixture:
        fixture_path = pathlib.Path(str(fixture))
        if not fixture_path.is_absolute():
            fixture_path = workspace / fixture_path
        return fixture_path.read_text()
    return ""


def _apply_steps(
    *,
    page: Any,
    steps: list[Any],
    timeout_ms: int,
) -> None:
    if not isinstance(steps, list):
        raise ValueError("capture steps must be a list")

    for step in steps:
        if isinstance(step, str):
            page.locator(step).click(timeout=timeout_ms)
            continue
        if not isinstance(step, dict):
            raise ValueError("capture steps must be strings or mappings")

        if "click" in step:
            page.locator(str(step["click"])).click(timeout=timeout_ms)
        elif "click_role" in step:
            _click_role(page=page, value=step["click_role"], timeout_ms=timeout_ms)
        elif "fill" in step:
            fill = step["fill"]
            if not isinstance(fill, dict) or "selector" not in fill:
                raise ValueError("fill step must contain selector and value")
            page.locator(str(fill["selector"])).fill(str(fill.get("value") or ""), timeout=timeout_ms)
        elif "press" in step:
            press = step["press"]
            if not isinstance(press, dict) or "key" not in press:
                raise ValueError("press step must contain key")
            selector = press.get("selector")
            if selector:
                page.locator(str(selector)).press(str(press["key"]), timeout=timeout_ms)
            else:
                page.keyboard.press(str(press["key"]))
        elif "evaluate" in step:
            expression = step["evaluate"]
            if not isinstance(expression, str) or not expression.strip():
                raise ValueError("evaluate step must contain a non-empty JavaScript expression")
            page.evaluate(expression)
        elif "select" in step:
            select = step["select"]
            if not isinstance(select, dict) or "selector" not in select:
                raise ValueError("select step must contain selector and value")
            page.locator(str(select["selector"])).select_option(str(select.get("value") or ""), timeout=timeout_ms)
        elif "wait_for_selector" in step:
            _wait_for_selector(page=page, selector=str(step["wait_for_selector"]), timeout_ms=timeout_ms)
        elif "wait_for_js" in step:
            _wait_for_js(page=page, expression=str(step["wait_for_js"]), timeout_ms=timeout_ms)
        elif "wait_for_text" in step:
            _wait_for_visible_text(page=page, text=str(step["wait_for_text"]), timeout_ms=timeout_ms)
        elif "wait_for_url" in step:
            page.wait_for_url(str(step["wait_for_url"]), timeout=timeout_ms)
        elif "sleep" in step:
            page.wait_for_timeout(int(float(step["sleep"]) * 1000))
        else:
            raise ValueError(f"Unsupported capture step: {step}")


def _click_role(*, page: Any, value: Any, timeout_ms: int) -> None:
    if isinstance(value, str):
        role = value
        name = None
        exact = None
        nth = None
    elif isinstance(value, dict):
        role = str(value.get("role") or "").strip()
        name = value.get("name")
        exact = value.get("exact")
        nth = value.get("nth")
    else:
        raise ValueError("click_role step must be a role string or mapping")
    if not role:
        raise ValueError("click_role step must define a non-empty role")

    options: dict[str, Any] = {}
    if name is not None:
        options["name"] = str(name)
    if exact is not None:
        options["exact"] = bool(exact)
    locator = page.get_by_role(role, **options)
    if nth is not None:
        locator = locator.nth(int(nth))
    locator.click(timeout=timeout_ms)


def _wait_for_target(
    *,
    page: Any,
    wait_for: Any,
    timeout_ms: int,
) -> None:
    if not wait_for:
        return
    if isinstance(wait_for, str):
        _wait_for_visible_text(page=page, text=wait_for, timeout_ms=timeout_ms)
        return
    if not isinstance(wait_for, dict):
        raise ValueError("capture wait_for must be a string or mapping")
    if "selector" in wait_for:
        _wait_for_selector(page=page, selector=str(wait_for["selector"]), timeout_ms=timeout_ms)
    elif "text" in wait_for:
        _wait_for_visible_text(page=page, text=str(wait_for["text"]), timeout_ms=timeout_ms)
    elif "url" in wait_for:
        page.wait_for_url(str(wait_for["url"]), timeout=timeout_ms)
    elif "js" in wait_for:
        _wait_for_js(page=page, expression=str(wait_for["js"]), timeout_ms=timeout_ms)
    else:
        raise ValueError("capture wait_for must contain selector, text, url, or js")


def _wait_for_selector(*, page: Any, selector: str, timeout_ms: int) -> None:
    if not selector:
        raise ValueError("wait_for selector must not be empty")
    locator: Any = page.locator(selector)
    first = getattr(locator, "first", None)
    if callable(first):
        locator = first()
    locator.wait_for(timeout=timeout_ms)


def _wait_for_js(*, page: Any, expression: str, timeout_ms: int) -> None:
    if not expression.strip():
        raise ValueError("wait_for_js must not be empty")
    page.wait_for_function(expression, timeout=timeout_ms)


def _wait_for_visible_text(*, page: Any, text: str, timeout_ms: int) -> None:
    if not text:
        raise ValueError("wait_for_text must not be empty")
    page.wait_for_function(
        """text => (document.body?.innerText || '').includes(text)""",
        arg=text,
        timeout=timeout_ms,
    )


def _wait_for_rendered_ui(
    *,
    page: Any,
    timeout_ms: int,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = page.evaluate(
            """() => {
                const body = document.body;
                const root = document.querySelector('#root, #__next, [data-reactroot], main, [role="main"]');
                const visible = [...document.querySelectorAll('body *')].filter((element) => {
                  const style = window.getComputedStyle(element);
                  const rect = element.getBoundingClientRect();
                  return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                }).length;
                return {
                  bodyTextLength: (body?.innerText || '').trim().length,
                  readyState: document.readyState,
                  rootElementCount: root ? root.querySelectorAll('*').length : 0,
                  title: document.title,
                  url: window.location.href,
                  visibleElementCount: visible,
                };
            }""",
        )
        if (
            int(last_state.get("bodyTextLength") or 0) >= 20
            or int(last_state.get("visibleElementCount") or 0) >= 8
            or int(last_state.get("rootElementCount") or 0) >= 5
        ):
            return
        page.wait_for_timeout(500)

    raise TimeoutError(f"Timed out waiting for rendered UI; last page state: {last_state}")


def _strip_runner_fields(target: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in target.items() if not key.startswith("_softlight_")}


def _target_url(
    *,
    app_url: str,
    route: str,
) -> str:
    parsed = urllib.parse.urlparse(route)
    if parsed.scheme in {"http", "https"}:
        return route

    if not route or re.search(r"\s", route):
        route = "/"
    if not route.startswith("/"):
        route = f"/{route}"

    return urllib.parse.urljoin(app_url.rstrip("/") + "/", route.lstrip("/"))


def _review_payload(
    *,
    config: dict[str, Any],
    capture_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    owner, repo = _repository()
    return {
        "repository_owner": owner,
        "repository_name": repo,
        "pull_number": _pull_number(),
        "base_sha": _base_sha(),
        "capture_artifacts": [
            {
                "data_url": artifact["data_url"],
                "target": artifact["target"],
            }
            for artifact in capture_artifacts
        ],
        "capture_mode": config.get("capture_mode")
        or os.environ.get("SOFTLIGHT_CAPTURE_MODE")
        or "real",
        "head_sha": _head_sha(),
    }


def _plan_payload() -> dict[str, Any]:
    owner, repo = _repository()
    return {
        "repository_owner": owner,
        "repository_name": repo,
        "pull_number": _pull_number(),
        "base_sha": _base_sha(),
        "head_sha": _head_sha(),
    }


def _post_review(payload: dict[str, Any]) -> None:
    base_url = (
        os.environ.get("SOFTLIGHT_REVIEW_BASE_URL")
        or os.environ.get("SOFTLIGHT_BASE_URL")
        or "https://softlight.orianna.ai"
    ).rstrip("/")
    endpoint = os.environ.get("SOFTLIGHT_REVIEW_ENDPOINT", "/api/review/run")
    url = f"{base_url}/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SOFTLIGHT_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"softlight: triggering review at {url}", flush=True)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            print(response.read().decode("utf-8"), flush=True)
    except urllib.error.HTTPError as error:
        print(error.read().decode("utf-8"), file=sys.stderr)
        raise


def _repository() -> tuple[str, str]:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository or "/" not in repository:
        raise ValueError("GITHUB_REPOSITORY must be set to owner/repo")

    owner, repo = repository.split("/", 1)
    return owner, repo


def _event() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}

    with open(event_path) as file:
        return json.load(file)


def _pull_number() -> int:
    event = _event()
    pull_request = event.get("pull_request") or {}
    number = pull_request.get("number") or event.get("number")
    if number is None:
        raise ValueError("Could not infer pull request number from the GitHub event")

    return int(number)


def _head_sha() -> str | None:
    event = _event()
    pull_request = event.get("pull_request") or {}
    return (pull_request.get("head") or {}).get("sha") or os.environ.get("GITHUB_SHA")


def _base_sha() -> str | None:
    event = _event()
    pull_request = event.get("pull_request") or {}
    return (pull_request.get("base") or {}).get("sha")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _maybe_serve_preview(
    *,
    app_url: str,
    capture_targets: list[dict[str, Any]],
    app_process: subprocess.Popen[bytes],
) -> None:
    """Tunnel the running app and post a clickable preview link on the PR (v0 hack).

    Gated on SOFTLIGHT_PREVIEW. Holds the app + tunnel alive for
    SOFTLIGHT_PREVIEW_HOLD_SECONDS so reviewers can click the link, then tears
    the tunnel down. The link dies when the run ends; this is intentionally a
    throwaway demo path, not a durable preview deploy.
    """
    if not _truthy(os.environ.get("SOFTLIGHT_PREVIEW")):
        return

    try:
        hold_seconds = float(
            os.environ.get("SOFTLIGHT_PREVIEW_HOLD_SECONDS") or DEFAULT_PREVIEW_HOLD_SECONDS,
        )
    except ValueError:
        hold_seconds = float(DEFAULT_PREVIEW_HOLD_SECONDS)

    tunnel: subprocess.Popen[bytes] | None = None
    try:
        tunnel, public_url = _start_tunnel(app_url=app_url)
        if public_url is None:
            print(
                "softlight: cloudflared did not report a public URL; skipping preview",
                file=sys.stderr,
                flush=True,
            )
            return
        links = _preview_links(public_url=public_url, capture_targets=capture_targets)
        _post_pr_preview_comment(links=links, hold_seconds=hold_seconds)
        print(f"softlight: preview live at {public_url} for ~{hold_seconds:g}s", flush=True)
        _hold_preview(hold_seconds=hold_seconds, app_process=app_process, tunnel=tunnel)
    except Exception as error:  # noqa: BLE001 - a preview failure must not fail the review
        print(f"softlight: preview failed: {error}", file=sys.stderr, flush=True)
    finally:
        if tunnel is not None:
            _stop_process(tunnel)


def _start_tunnel(*, app_url: str) -> tuple[subprocess.Popen[bytes], str | None]:
    log_path = pathlib.Path(tempfile.gettempdir()) / "softlight-cloudflared.log"
    log_file = log_path.open("w+b")
    print(f"softlight: starting cloudflared tunnel to {app_url}", flush=True)
    try:
        process = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", app_url, "--no-autoupdate"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        log_text = log_path.read_text(errors="replace")
        match = TRYCLOUDFLARE_URL_PATTERN.search(log_text)
        if match:
            return process, match.group(0)
        if process.poll() is not None:
            print(log_text, file=sys.stderr, flush=True)
            return process, None
        time.sleep(0.5)

    return process, None


def _preview_links(
    *,
    public_url: str,
    capture_targets: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    base = public_url.rstrip("/")
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for target in capture_targets:
        route = str(target.get("route_or_entrypoint") or "/")
        url = _target_url(app_url=base, route=route)
        if url in seen:
            continue
        seen.add(url)
        links.append((str(target.get("name") or route), url))
    if not links:
        links.append(("App", base))
    return links


def _post_pr_preview_comment(
    *,
    links: list[tuple[str, str]],
    hold_seconds: float,
) -> None:
    owner, repo = _repository()
    pull_number = _pull_number()
    body = "\n".join(
        [
            "### ▶ Interactive prototype",
            "",
            f"A live, clickable preview of this PR is running for ~{hold_seconds / 60:.0f} min. "
            "This is a temporary tunnel — the link stops working once the review run ends.",
            "",
            *[f"- [{name}]({url})" for name, url in links],
        ],
    )
    result = subprocess.run(
        [
            "gh",
            "pr",
            "comment",
            str(pull_number),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr comment failed (exit {result.returncode}): {result.stdout}{result.stderr}",
        )
    print(f"softlight: posted preview link comment to {owner}/{repo}#{pull_number}", flush=True)


def _hold_preview(
    *,
    hold_seconds: float,
    app_process: subprocess.Popen[bytes],
    tunnel: subprocess.Popen[bytes],
) -> None:
    deadline = time.monotonic() + hold_seconds
    last_log_at = 0.0
    while time.monotonic() < deadline:
        if app_process.poll() is not None:
            print("softlight: app exited; ending preview early", file=sys.stderr, flush=True)
            return
        if tunnel.poll() is not None:
            print("softlight: cloudflared exited; ending preview early", file=sys.stderr, flush=True)
            return
        now = time.monotonic()
        if now - last_log_at >= 60:
            print(f"softlight: preview live; ~{(deadline - now) / 60:.0f} min remaining", flush=True)
            last_log_at = now
        time.sleep(2)
    print("softlight: preview hold elapsed; tearing down", flush=True)


if __name__ == "__main__":
    main()

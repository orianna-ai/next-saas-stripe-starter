from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tomllib
from typing import Any


@dataclasses.dataclass(frozen=True)
class RuntimeRequirement:
    tool: str
    version: str
    source: str


def prepare_runtime_env(
    *,
    workspace: pathlib.Path,
    env: dict[str, str],
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Provision repo-declared runtimes and return the command environment."""
    prepared_env = dict(env)
    requirements = detect_runtime_requirements(
        workspace=workspace,
        env=prepared_env,
        config=config,
    )
    if requirements:
        prepared_env["SOFTLIGHT_RUNTIME_REQUIREMENTS"] = json.dumps(
            [dataclasses.asdict(requirement) for requirement in requirements],
        )

    ensure_system_packages(packages=_configured_system_packages(config), env=prepared_env)

    for requirement in requirements:
        env_var = _runtime_env_var(requirement.tool)
        if env_var:
            prepared_env[env_var] = requirement.version
        runtime_bin = ensure_runtime(
            requirement=requirement,
            workspace=workspace,
            env=prepared_env,
        )
        if runtime_bin:
            prepared_env["PATH"] = _prepend_path(prepared_env.get("PATH", ""), runtime_bin)
    return prepared_env


def ensure_system_packages(*, packages: list[str], env: dict[str, str]) -> None:
    if not packages:
        return
    apt_get = shutil.which("apt-get", path=env.get("PATH"))
    if not apt_get:
        raise RuntimeError(
            "review.yml requested system packages but apt-get is not available",
        )
    sudo = shutil.which("sudo", path=env.get("PATH"))
    prefix = [sudo] if sudo else []
    _run([*prefix, apt_get, "update"], env=env)
    _run([*prefix, apt_get, "install", "-y", *packages], env=env)


def detect_runtime_requirements(
    *,
    workspace: pathlib.Path,
    env: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> list[RuntimeRequirement]:
    """Return repo-declared runtime requirements in provisioning order."""
    env = env or {}
    requirements: list[RuntimeRequirement] = []
    for tool, detector in (
        ("node", detect_node_version),
        ("python", detect_python_version),
        ("go", detect_go_version),
        ("ruby", detect_ruby_version),
        ("rust", detect_rust_version),
        ("java", detect_java_version),
    ):
        version = _configured_runtime_version(tool=tool, env=env, config=config)
        source = "review.yml" if version else ""
        if not version:
            detected = detector(workspace)
            if detected:
                version, source = detected
        if version:
            requirements.append(
                RuntimeRequirement(
                    tool=tool,
                    version=extract_version(version) or version,
                    source=source,
                ),
            )
    return requirements


def detect_node_version(workspace: pathlib.Path) -> tuple[str, str] | None:
    for filename in (".node-version", ".nvmrc"):
        path = workspace / filename
        if path.exists():
            version = extract_version(path.read_text().strip())
            if version:
                return version, filename

    package_json = workspace / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text())
        except json.JSONDecodeError:
            return None
        engines = package.get("engines") or {}
        if isinstance(engines, dict):
            version = extract_version(str(engines.get("node") or ""))
            if version:
                return version, "package.json engines.node"
    return None


def detect_python_version(workspace: pathlib.Path) -> tuple[str, str] | None:
    python_version = _version_file(workspace / ".python-version")
    if python_version:
        return python_version, ".python-version"

    pyproject = workspace / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text())
        except tomllib.TOMLDecodeError:
            return None
        project = data.get("project")
        if isinstance(project, dict):
            version = extract_version(str(project.get("requires-python") or ""))
            if version:
                return version, "pyproject.toml project.requires-python"
    return None


def detect_go_version(workspace: pathlib.Path) -> tuple[str, str] | None:
    go_mod = workspace / "go.mod"
    if not go_mod.exists():
        return None
    for line in go_mod.read_text().splitlines():
        if match := re.match(r"\s*toolchain\s+go(\d+(?:\.\d+){0,2})\s*$", line):
            return match.group(1), "go.mod toolchain"
    for line in go_mod.read_text().splitlines():
        if match := re.match(r"\s*go\s+(\d+(?:\.\d+){0,2})\s*$", line):
            return match.group(1), "go.mod go"
    return None


def detect_ruby_version(workspace: pathlib.Path) -> tuple[str, str] | None:
    ruby_version = _version_file(workspace / ".ruby-version")
    if ruby_version:
        return ruby_version, ".ruby-version"

    gemfile = workspace / "Gemfile"
    if gemfile.exists():
        match = re.search(
            r"^\s*ruby\s+['\"]([^'\"]+)['\"]",
            gemfile.read_text(),
            flags=re.MULTILINE,
        )
        if match:
            version = extract_version(match.group(1))
            if version:
                return version, "Gemfile ruby"
    return None


def detect_rust_version(workspace: pathlib.Path) -> tuple[str, str] | None:
    rust_toolchain = workspace / "rust-toolchain"
    if rust_toolchain.exists():
        version = extract_version(rust_toolchain.read_text().strip())
        if version:
            return version, "rust-toolchain"

    rust_toolchain_toml = workspace / "rust-toolchain.toml"
    if rust_toolchain_toml.exists():
        try:
            data = tomllib.loads(rust_toolchain_toml.read_text())
        except tomllib.TOMLDecodeError:
            return None
        toolchain = data.get("toolchain")
        if isinstance(toolchain, dict):
            version = extract_version(str(toolchain.get("channel") or ""))
            if version:
                return version, "rust-toolchain.toml toolchain.channel"
    return None


def detect_java_version(workspace: pathlib.Path) -> tuple[str, str] | None:
    java_version = _version_file(workspace / ".java-version")
    if java_version:
        return java_version, ".java-version"

    sdkmanrc = workspace / ".sdkmanrc"
    if sdkmanrc.exists():
        for line in sdkmanrc.read_text().splitlines():
            if line.startswith("java="):
                version = extract_version(line.partition("=")[2])
                if version:
                    return version, ".sdkmanrc java"
    return None


def ensure_runtime(
    *,
    requirement: RuntimeRequirement,
    workspace: pathlib.Path,
    env: dict[str, str],
) -> pathlib.Path | None:
    if runtime_is_satisfied(requirement=requirement, env=env):
        print(
            f"softlight: runtime satisfied: {requirement.tool} {requirement.version}",
            flush=True,
        )
        return None

    if requirement.tool == "node":
        try:
            return ensure_node_runtime(
                version=requirement.version,
                workspace=workspace,
                env=env,
            )
        except FileNotFoundError:
            print(
                "softlight: npm not available for Node provisioning; falling back to mise",
                flush=True,
            )

    return ensure_mise_runtime(
        requirement=requirement,
        workspace=workspace,
        env=env,
    )


def runtime_is_satisfied(
    *,
    requirement: RuntimeRequirement,
    env: dict[str, str],
) -> bool:
    current = current_runtime_version(tool=requirement.tool, env=env)
    if not current:
        return False
    precision = "major" if requirement.tool in {"node", "java"} else "minor"
    return version_matches(current=current, required=requirement.version, precision=precision)


def ensure_node_runtime(
    *,
    version: str,
    workspace: pathlib.Path,
    env: dict[str, str],
) -> pathlib.Path | None:
    current_major = current_node_major(env=env)
    required_major = major_version(version)
    if current_major and required_major and current_major == required_major:
        return None

    cache_dir = _runtime_cache(workspace=workspace, env=env) / "node" / f"node-{version}"
    package_bin = cache_dir / "node_modules" / ".bin"
    node_binary = package_bin / "node"
    corepack_binary = package_bin / "corepack"
    marker = cache_dir / ".softlight-ready"
    if marker.exists() and node_binary.exists() and corepack_binary.exists():
        return package_bin

    cache_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "npm",
            "install",
            "--silent",
            "--prefix",
            str(cache_dir),
            f"node@{version}",
            "corepack",
        ],
        env=env,
    )
    if not node_binary.exists():
        raise FileNotFoundError(f"node was not installed at {node_binary}")
    if not corepack_binary.exists():
        raise FileNotFoundError(f"corepack was not installed at {corepack_binary}")
    marker.write_text("ok\n")
    print(f"softlight: using Node {version} from {node_binary}", flush=True)
    return package_bin


def ensure_mise_runtime(
    *,
    requirement: RuntimeRequirement,
    workspace: pathlib.Path,
    env: dict[str, str],
) -> pathlib.Path:
    mise_env, mise_binary, shim_dir = _mise_env(workspace=workspace, env=env)
    tool_spec = f"{requirement.tool}@{requirement.version}"
    marker = _runtime_cache(workspace=workspace, env=env) / "mise" / "markers" / tool_spec
    if not marker.exists():
        _run([str(mise_binary), "install", tool_spec], env=mise_env)
        _run([str(mise_binary), "use", "--global", tool_spec], env=mise_env)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n")
    print(
        f"softlight: using {requirement.tool} {requirement.version} via mise",
        flush=True,
    )
    env.update(mise_env)
    return shim_dir


def extract_version(value: str) -> str | None:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return None
    parts = [part for part in match.groups() if part is not None]
    return ".".join(parts)


def current_runtime_version(*, tool: str, env: dict[str, str]) -> str | None:
    commands = {
        "node": ["node", "--version"],
        "python": ["python3", "--version"],
        "go": ["go", "version"],
        "ruby": ["ruby", "--version"],
        "rust": ["rustc", "--version"],
        "java": ["java", "-version"],
    }
    command = commands.get(tool)
    if not command:
        return None
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return None
    return extract_version(f"{completed.stdout}\n{completed.stderr}")


def current_node_major(*, env: dict[str, str]) -> int | None:
    version = current_runtime_version(tool="node", env=env)
    return major_version(version or "")


def major_version(version: str) -> int | None:
    match = re.search(r"(\d+)", version)
    return int(match.group(1)) if match else None


def version_matches(*, current: str, required: str, precision: str) -> bool:
    current_parts = _version_parts(current)
    required_parts = _version_parts(required)
    if not current_parts or not required_parts:
        return False
    if precision == "major":
        return current_parts[0] == required_parts[0]
    if len(required_parts) == 1:
        return current_parts[0] == required_parts[0]
    return current_parts[:2] == required_parts[:2]


def _runtime_cache(
    *,
    workspace: pathlib.Path,
    env: dict[str, str],
) -> pathlib.Path:
    if configured := env.get("SOFTLIGHT_RUNTIME_CACHE"):
        return pathlib.Path(configured)
    if runner_temp := env.get("RUNNER_TEMP"):
        return pathlib.Path(runner_temp) / "softlight-runtime"
    return workspace / ".softlight" / ".runtime"


def _configured_runtime_version(
    *,
    tool: str,
    env: dict[str, str],
    config: dict[str, Any] | None,
) -> str | None:
    if env_var := _runtime_env_var(tool):
        if version := env.get(env_var):
            return str(version)

    runtime_config = (config or {}).get("runtime") or {}
    if isinstance(runtime_config, dict):
        configured = runtime_config.get(tool)
        if isinstance(configured, dict):
            configured = configured.get("version")
        if configured:
            return str(configured)
    return None


def _configured_system_packages(config: dict[str, Any] | None) -> list[str]:
    runtime_config = (config or {}).get("runtime") or {}
    if not isinstance(runtime_config, dict):
        return []
    packages = (
        runtime_config.get("apt")
        or runtime_config.get("apt_packages")
        or runtime_config.get("system_packages")
        or []
    )
    if isinstance(packages, str):
        return [packages]
    if isinstance(packages, list) and all(isinstance(package, str) for package in packages):
        return packages
    raise ValueError("review.yml runtime system packages must be a string or list of strings")


def _runtime_env_var(tool: str) -> str | None:
    return {
        "node": "SOFTLIGHT_NODE_VERSION",
        "python": "SOFTLIGHT_PYTHON_VERSION",
        "go": "SOFTLIGHT_GO_VERSION",
        "ruby": "SOFTLIGHT_RUBY_VERSION",
        "rust": "SOFTLIGHT_RUST_VERSION",
        "java": "SOFTLIGHT_JAVA_VERSION",
    }.get(tool)


def _version_file(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    return extract_version(path.read_text().strip())


def _version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def _prepend_path(existing_path: str, path: pathlib.Path) -> str:
    return f"{path}{os.pathsep}{existing_path}" if existing_path else str(path)


def _mise_env(
    *,
    workspace: pathlib.Path,
    env: dict[str, str],
) -> tuple[dict[str, str], pathlib.Path, pathlib.Path]:
    cache = _runtime_cache(workspace=workspace, env=env) / "mise"
    bin_dir = cache / "bin"
    data_dir = cache / "data"
    config_dir = cache / "config"
    cache_dir = cache / "cache"
    shim_dir = data_dir / "shims"
    mise_binary = pathlib.Path(
        shutil.which("mise", path=env.get("PATH")) or bin_dir / "mise",
    )
    mise_env = dict(env)
    mise_env["MISE_DATA_DIR"] = str(data_dir)
    mise_env["MISE_CONFIG_DIR"] = str(config_dir)
    mise_env["MISE_CACHE_DIR"] = str(cache_dir)
    mise_env["PATH"] = _prepend_path(
        _prepend_path(mise_env.get("PATH", ""), shim_dir),
        bin_dir,
    )
    if not mise_binary.exists():
        bin_dir.mkdir(parents=True, exist_ok=True)
        install_command = (
            f"curl -fsSL https://mise.run | "
            f"MISE_INSTALL_PATH={shlex.quote(str(mise_binary))} sh"
        )
        _run(["sh", "-c", install_command], env=mise_env)
    return mise_env, mise_binary, shim_dir


def _run(command: list[str], *, env: dict[str, str]) -> None:
    print(f"softlight: provisioning runtime: {' '.join(command)}", flush=True)
    subprocess.run(command, env=env, check=True)

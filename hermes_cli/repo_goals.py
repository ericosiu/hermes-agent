"""Repo-local /goals command routing for Hermes.

Hermes stays thin here: resolve a goal alias, run the target repo's CLI, and
summarize the repo-generated report. The target repo owns execution, gates,
artifacts, and status.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


DEFAULT_TIMEOUT_SECONDS = 900

DEFAULT_ALIAS_CONFIG = {
    "schemaVersion": "1.0",
    "aliases": [
        {
            "alias": "outbound-autoresearch",
            "repo": "ericosiu/singlegrain-ai-optimization-lab",
            "workdir": ".",
            "template": "outbound-autoresearch-night-crew",
            "input": {
                "mode": "single_read_only_local_export",
                "directory": "artifacts/input",
                "acceptedExtensions": [".csv", ".json"],
            },
            "commands": {
                "runDryRun": "./scripts/goals run outbound-autoresearch-night-crew --dry-run",
                "status": "./scripts/goals status outbound-autoresearch-night-crew",
                "report": "./scripts/goals report outbound-autoresearch-night-crew",
                "pause": "./scripts/goals pause outbound-autoresearch-night-crew",
                "resume": "./scripts/goals resume outbound-autoresearch-night-crew",
                "stop": "./scripts/goals stop outbound-autoresearch-night-crew",
            },
        }
    ],
}


class RepoGoalsError(RuntimeError):
    """User-facing /goals command error."""


@dataclass(frozen=True)
class GoalAlias:
    alias: str
    repo: str
    workdir: str = "."
    template: str = ""
    commands: Mapping[str, str] = field(default_factory=dict)
    input: Mapping[str, object] = field(default_factory=dict)
    repo_path: Optional[Path] = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "GoalAlias":
        repo_path_raw = raw.get("repoPath") or raw.get("repo_path") or raw.get("path")
        repo_path = Path(str(repo_path_raw)).expanduser() if repo_path_raw else None
        commands = raw.get("commands") if isinstance(raw.get("commands"), Mapping) else {}
        input_cfg = raw.get("input") if isinstance(raw.get("input"), Mapping) else {}
        return cls(
            alias=str(raw.get("alias") or "").strip(),
            repo=str(raw.get("repo") or "").strip(),
            workdir=str(raw.get("workdir") or ".").strip() or ".",
            template=str(raw.get("template") or "").strip(),
            commands={str(k): str(v) for k, v in commands.items()},
            input={str(k): v for k, v in input_cfg.items()},
            repo_path=repo_path,
        )


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    runtime_seconds: float


def _default_alias_file() -> Optional[Path]:
    env_path = os.environ.get("HERMES_GOALS_ALIAS_FILE")
    if env_path:
        return Path(env_path).expanduser()
    try:
        from hermes_constants import get_hermes_home

        candidate = Path(get_hermes_home()) / "goals-aliases.json"
    except Exception:
        candidate = Path.home() / ".hermes" / "goals-aliases.json"
    return candidate if candidate.exists() else None


def load_aliases(path: Optional[Path] = None) -> dict[str, GoalAlias]:
    """Load goal aliases from a Hermes fixture-shaped JSON file.

    If no file is configured, Hermes ships with the first production alias:
    ``outbound-autoresearch``.
    """
    config_path = path or _default_alias_file()
    if config_path and config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        data = DEFAULT_ALIAS_CONFIG

    aliases: dict[str, GoalAlias] = {}
    raw_aliases = data.get("aliases", []) if isinstance(data, Mapping) else []
    for raw in raw_aliases:
        if not isinstance(raw, Mapping):
            continue
        alias = GoalAlias.from_dict(raw)
        if alias.alias:
            aliases[alias.alias] = alias
    return aliases


def _repo_basename(repo: str) -> str:
    return repo.rstrip("/").split("/")[-1].removesuffix(".git")


def resolve_repo_path(alias: GoalAlias) -> Path:
    """Find the local checkout for an alias.

    Production installs should prefer ``repoPath`` in the alias JSON. For local
    operator use, ``HERMES_GOALS_REPO_ROOTS`` can point Hermes at one or more
    parent directories.
    """
    if alias.repo_path:
        path = alias.repo_path.expanduser()
        if path.exists():
            return path
        raise RepoGoalsError(f"Repo path for `{alias.alias}` does not exist: {path}")

    repo_name = _repo_basename(alias.repo)
    roots = []
    roots_env = os.environ.get("HERMES_GOALS_REPO_ROOTS") or os.environ.get("HERMES_GOALS_WORKSPACE")
    if roots_env:
        roots.extend(Path(item).expanduser() for item in roots_env.split(os.pathsep) if item.strip())
    roots.append(Path.cwd())
    roots.extend(Path.cwd().parents)

    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        candidates = [root]
        if root.name != repo_name:
            candidates.append(root / repo_name)
        for candidate in candidates:
            if (candidate / ".git").exists() and candidate.name == repo_name:
                return candidate

    raise RepoGoalsError(
        f"Local checkout not found for `{alias.repo}`. Set `repoPath` in the goals alias file "
        "or HERMES_GOALS_REPO_ROOTS to a parent directory containing the repo."
    )


def _command_for_action(alias: GoalAlias, action: str) -> str:
    action_to_key = {
        "start": "runDryRun",
        "run": "runDryRun",
        "status": "status",
        "report": "report",
        "pause": "pause",
        "resume": "resume",
        "stop": "stop",
    }
    key = action_to_key.get(action)
    if not key:
        raise RepoGoalsError(f"Unsupported /goals action `{action}`. Use start, status, report, monitor, pause, resume, or stop.")
    command = alias.commands.get(key)
    if not command and key == "runDryRun":
        command = alias.commands.get("startDryRun")
    if not command:
        raise RepoGoalsError(f"Alias `{alias.alias}` has no `{key}` command configured.")
    return command


def _run_repo_command(alias: GoalAlias, action: str) -> CommandResult:
    repo_path = resolve_repo_path(alias)
    cwd = (repo_path / alias.workdir).resolve()
    if not cwd.exists():
        raise RepoGoalsError(f"Workdir for `{alias.alias}` does not exist: {cwd}")

    command = _command_for_action(alias, action)
    args = shlex.split(command)
    if not args:
        raise RepoGoalsError(f"Alias `{alias.alias}` has an empty command for `{action}`.")

    timeout = int(os.environ.get("HERMES_GOALS_COMMAND_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoGoalsError(f"`/goals {action} {alias.alias}` timed out after {timeout}s.") from exc
    runtime = time.monotonic() - started_at
    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
        runtime_seconds=runtime,
    )


def _parse_input_path(tokens: list[str]) -> Optional[Path]:
    for index, token in enumerate(tokens):
        if token == "--input" and index + 1 < len(tokens):
            return Path(tokens[index + 1]).expanduser()
        if token.startswith("--input="):
            return Path(token.split("=", 1)[1]).expanduser()
        if token.startswith("input="):
            return Path(token.split("=", 1)[1]).expanduser()
    return None


def _stage_input_export(alias: GoalAlias, source: Path) -> Optional[str]:
    if not source.exists():
        raise RepoGoalsError(f"Input export does not exist: {source}")
    accepted = set(alias.input.get("acceptedExtensions") or [".csv", ".json"])
    if source.suffix.lower() not in accepted:
        raise RepoGoalsError(f"Input export must be one of {sorted(accepted)}; got `{source.suffix}`.")

    repo_path = resolve_repo_path(alias)
    input_dir_name = str(alias.input.get("directory") or "artifacts/input")
    input_dir = repo_path / alias.workdir / input_dir_name
    input_dir.mkdir(parents=True, exist_ok=True)
    existing = [path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in accepted]
    target = input_dir / source.name
    if existing and not (len(existing) == 1 and existing[0].resolve() == target.resolve()):
        names = ", ".join(path.name for path in existing)
        raise RepoGoalsError(
            f"Cannot stage `{source.name}` because `{input_dir_name}` already has export file(s): {names}. "
            "Leave exactly one CSV/JSON export for the repo runner."
        )
    if target.exists() and source.resolve() != target.resolve():
        raise RepoGoalsError(
            f"Cannot stage `{source.name}` because `{input_dir_name}/{target.name}` already exists. "
            "Point Hermes at the export already in the repo or clear the input folder manually."
        )
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return str(target)


def extract_report_section(markdown: str, heading: str) -> str:
    marker = f"## {heading}".lower()
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == marker:
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def render_slack_summary(alias: str, report: str, status_text: str = "", start_text: str = "") -> str:
    sections = {
        "Slack Summary": extract_report_section(report, "Slack Summary"),
        "Data Quality Caveats": extract_report_section(report, "Data Quality Caveats"),
        "Blockers": extract_report_section(report, "Blockers"),
        "Approval Gates": extract_report_section(report, "Approval Gates"),
    }
    lines = [f"/goals `{alias}` report"]
    if status_text:
        lines.extend(["", "Status", status_text.strip()])
    if start_text:
        lines.extend(["", "Run", start_text.strip()])
    if sections["Slack Summary"]:
        lines.extend(["", "Slack Summary", sections["Slack Summary"]])
    else:
        lines.extend(["", "Report", report.strip()[:3000] or "No report output."])
    for title in ("Data Quality Caveats", "Blockers", "Approval Gates"):
        if sections[title]:
            lines.extend(["", title, sections[title]])
    return "\n".join(lines).strip()


def _render_failure(alias: str, action: str, result: CommandResult) -> str:
    lines = [
        f"/goals `{action} {alias}` blocked",
        f"Command failed with exit {result.exit_code}: `{result.command}`",
    ]
    if result.stderr:
        lines.extend(["", "stderr", result.stderr])
    if result.stdout:
        lines.extend(["", "stdout", result.stdout])
    return "\n".join(lines)


def _format_alias_list(aliases: Mapping[str, GoalAlias]) -> str:
    if not aliases:
        return "No /goals aliases are configured."
    lines = ["Configured /goals aliases:"]
    for name in sorted(aliases):
        alias = aliases[name]
        lines.append(f"- `{name}` -> `{alias.repo}` (`{alias.template or 'no template'}`)")
    return "\n".join(lines)


def handle_goals_command(command: str, aliases: Optional[Mapping[str, GoalAlias]] = None) -> str:
    aliases = dict(aliases or load_aliases())
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"Invalid /goals command: {exc}"
    if not tokens:
        return _format_alias_list(aliases)
    if tokens[0].lstrip("/") != "goals":
        tokens.insert(0, "/goals")

    action = tokens[1].lower() if len(tokens) > 1 else "list"
    if action in {"list", "ls", "help"}:
        return _format_alias_list(aliases)
    if len(tokens) < 3:
        return "Usage: /goals [start|status|report|monitor|pause|resume|stop] <alias> [--input path/to/export.csv]"

    alias_name = tokens[2]
    alias = aliases.get(alias_name)
    if alias is None:
        supported = ", ".join(f"`{name}`" for name in sorted(aliases)) or "none"
        return f"Unknown goals alias `{alias_name}`. Supported aliases: {supported}."

    try:
        input_path = _parse_input_path(tokens[3:])
        staged = _stage_input_export(alias, input_path) if input_path else None

        if action in {"start", "run"}:
            start = _run_repo_command(alias, "start")
            if start.exit_code != 0:
                return _render_failure(alias.alias, action, start)
            status = _run_repo_command(alias, "status")
            report = _run_repo_command(alias, "report")
            if report.exit_code != 0:
                return _render_failure(alias.alias, "report", report)
            start_text = start.stdout
            if staged:
                start_text = f"Input staged: {staged}\n{start_text}".strip()
            return render_slack_summary(alias.alias, report.stdout, status.stdout, start_text)

        if action == "monitor":
            status = _run_repo_command(alias, "status")
            report = _run_repo_command(alias, "report")
            if report.exit_code != 0:
                return _render_failure(alias.alias, "report", report)
            return render_slack_summary(alias.alias, report.stdout, status.stdout)

        result = _run_repo_command(alias, action)
        if result.exit_code != 0:
            return _render_failure(alias.alias, action, result)
        if action == "report":
            return render_slack_summary(alias.alias, result.stdout)
        return result.stdout or f"/goals `{action} {alias.alias}` completed."
    except RepoGoalsError as exc:
        return f"/goals `{action} {alias_name}` blocked\n{exc}"

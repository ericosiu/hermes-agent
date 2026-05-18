import json
import os
from pathlib import Path

import hermes_cli.repo_goals as repo_goals
from hermes_cli.repo_goals import (
    GoalAlias,
    extract_report_section,
    handle_goals_command,
    load_aliases,
    render_slack_summary,
)


def _write_fake_goals_cli(repo: Path, *, fail_start: bool = False) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    goals = scripts / "goals"
    goals.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import pathlib",
                "import sys",
                "",
                "args = sys.argv[1:]",
                "if args[:2] == ['run', 'outbound-autoresearch-night-crew']:",
                "    if " + repr(fail_start) + ":",
                "        print('missing required export fields: campaign_id', file=sys.stderr)",
                "        raise SystemExit(2)",
                "    run = pathlib.Path('goals/runs/run-1')",
                "    (run / 'reports').mkdir(parents=True, exist_ok=True)",
                "    (run / 'artifacts').mkdir(parents=True, exist_ok=True)",
                "    (run / 'reports' / 'final-report.md').write_text('# Final Report Packet\\n')",
                "    print('Started goal outbound-autoresearch-night-crew (dry_run)')",
                "    print('Input mode: read_only_export')",
                "    print('Run folder: goals/runs/run-1')",
                "    raise SystemExit(0)",
                "if args[:2] == ['status', 'outbound-autoresearch-night-crew']:",
                "    print('Goal: outbound-autoresearch-night-crew')",
                "    print('Status: completed')",
                "    print('Latest run: goals/runs/run-1')",
                "    raise SystemExit(0)",
                "if args[:2] == ['report', 'outbound-autoresearch-night-crew']:",
                "    print('''# Final Report Packet: Outbound Autoresearch Night Crew",
                "",
                "## Slack Summary",
                "",
                "Outbound autoresearch ran in read-only local-export mode for 2 campaigns.",
                "Pipeline signal observed: Yes; positive replies: 9; qualified booked calls: 2; positive reply rate: 0.50%.",
                "",
                "## Data Quality Caveats",
                "",
                "- Optional fields missing from export: owner",
                "",
                "## Blockers",
                "",
                "- Dry run only: no external services were read or written.",
                "",
                "## Approval Gates",
                "",
                "- Gate 1: Read-Only Data Access Approval",
                "- Gate 4: Instantly-Ready Change Packet Approval",
                "''')",
                "    raise SystemExit(0)",
                "print('unexpected args: ' + ' '.join(args), file=sys.stderr)",
                "raise SystemExit(99)",
            ]
        )
        + "\n"
    )
    goals.chmod(0o755)


def _alias(repo: Path) -> GoalAlias:
    return GoalAlias(
        alias="outbound-autoresearch",
        repo="ericosiu/singlegrain-ai-optimization-lab",
        workdir=".",
        template="outbound-autoresearch-night-crew",
        repo_path=repo,
        commands={
            "runDryRun": "./scripts/goals run outbound-autoresearch-night-crew --dry-run",
            "status": "./scripts/goals status outbound-autoresearch-night-crew",
            "report": "./scripts/goals report outbound-autoresearch-night-crew",
        },
        input={
            "directory": "artifacts/input",
            "acceptedExtensions": [".csv", ".json"],
        },
    )


def test_load_aliases_reads_hermes_fixture_shape(tmp_path, monkeypatch):
    config = tmp_path / "aliases.json"
    config.write_text(
        json.dumps(
            {
                "aliases": [
                    {
                        "alias": "outbound-autoresearch",
                        "repo": "ericosiu/singlegrain-ai-optimization-lab",
                        "workdir": ".",
                        "template": "outbound-autoresearch-night-crew",
                        "repoPath": str(tmp_path / "repo"),
                        "commands": {
                            "runDryRun": "./scripts/goals run outbound-autoresearch-night-crew --dry-run",
                            "report": "./scripts/goals report outbound-autoresearch-night-crew",
                        },
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("HERMES_GOALS_ALIAS_FILE", str(config))

    aliases = load_aliases()

    alias = aliases["outbound-autoresearch"]
    assert alias.repo == "ericosiu/singlegrain-ai-optimization-lab"
    assert alias.repo_path == tmp_path / "repo"
    assert alias.commands["runDryRun"].endswith("--dry-run")


def test_handle_goals_start_runs_repo_command_and_returns_report(tmp_path):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    _write_fake_goals_cli(repo)

    output = handle_goals_command(
        "/goals start outbound-autoresearch",
        aliases={"outbound-autoresearch": _alias(repo)},
    )

    assert "outbound-autoresearch" in output
    assert "Slack Summary" in output
    assert "Pipeline signal observed: Yes" in output
    assert "Gate 4: Instantly-Ready Change Packet Approval" in output
    assert "goals/runs/run-1" in output


def test_handle_goals_start_stages_read_only_export(tmp_path):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    _write_fake_goals_cli(repo)
    export = tmp_path / "instant-export.csv"
    export.write_text("campaign_id,delivered,replies\nc1,10,1\n")

    output = handle_goals_command(
        f"/goals start outbound-autoresearch --input {export}",
        aliases={"outbound-autoresearch": _alias(repo)},
    )

    staged = repo / "artifacts" / "input" / "instant-export.csv"
    assert staged.read_text() == export.read_text()
    assert f"Input staged: {staged}" in output


def _point_cron_at_tmp_home(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod
    import cron.scheduler as scheduler_mod

    cron_dir = tmp_path / "cron"
    output_dir = cron_dir / "output"
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(scheduler_mod, "_hermes_home", tmp_path)
    monkeypatch.setattr(repo_goals, "_goals_monitor_scripts_dir", lambda: tmp_path / "scripts")
    return jobs_mod, scheduler_mod


def test_handle_goals_schedule_creates_no_agent_monitor_job(tmp_path, monkeypatch):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    (repo / ".git").mkdir()
    _write_fake_goals_cli(repo)
    jobs_mod, _scheduler_mod = _point_cron_at_tmp_home(tmp_path, monkeypatch)

    output = handle_goals_command(
        "/goals schedule outbound-autoresearch every 15m",
        aliases={"outbound-autoresearch": _alias(repo)},
    )

    jobs = jobs_mod.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == "goals-monitor:outbound-autoresearch"
    assert job["schedule_display"] == "every 15m"
    assert job["no_agent"] is True
    assert job["deliver"] == "local"
    assert job["script"] == "goals-monitor-outbound-autoresearch.py"
    assert "Manual check: /goals monitor outbound-autoresearch" in output
    assert (tmp_path / "scripts" / "goals-monitor-outbound-autoresearch.py").exists()


def test_handle_goals_schedule_preserves_gateway_origin_for_delivery(tmp_path, monkeypatch):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    (repo / ".git").mkdir()
    _write_fake_goals_cli(repo)
    jobs_mod, _scheduler_mod = _point_cron_at_tmp_home(tmp_path, monkeypatch)
    origin = {
        "platform": "slack",
        "chat_id": "C123",
        "thread_id": "1715970000.000100",
        "user_id": "U123",
    }

    handle_goals_command(
        "/goals schedule outbound-autoresearch every 15m",
        aliases={"outbound-autoresearch": _alias(repo)},
        origin=origin,
    )

    job = jobs_mod.list_jobs(include_disabled=True)[0]
    assert job["deliver"] == "origin"
    assert job["origin"] == origin


def test_handle_goals_schedule_updates_existing_monitor_job(tmp_path, monkeypatch):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    (repo / ".git").mkdir()
    _write_fake_goals_cli(repo)
    jobs_mod, _scheduler_mod = _point_cron_at_tmp_home(tmp_path, monkeypatch)
    aliases = {"outbound-autoresearch": _alias(repo)}

    handle_goals_command("/goals watch outbound-autoresearch every 15m", aliases=aliases)
    output = handle_goals_command("/goals watch outbound-autoresearch every 45m", aliases=aliases)

    jobs = jobs_mod.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    assert jobs[0]["schedule_display"] == "every 45m"
    assert jobs[0]["state"] == "scheduled"
    assert "Updated scheduled monitor" in output


def test_scheduled_goals_monitor_script_calls_status_and_report(tmp_path, monkeypatch):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    (repo / ".git").mkdir()
    _write_fake_goals_cli(repo)
    jobs_mod, scheduler_mod = _point_cron_at_tmp_home(tmp_path, monkeypatch)

    handle_goals_command(
        "/goals schedule outbound-autoresearch every 15m",
        aliases={"outbound-autoresearch": _alias(repo)},
    )

    success, _doc, final_response, error = scheduler_mod.run_job(jobs_mod.list_jobs()[0])

    assert success is True
    assert error is None
    assert "Status: completed" in final_response
    assert "Pipeline signal observed: Yes" in final_response
    assert "Gate 4: Instantly-Ready Change Packet Approval" in final_response


def test_handle_goals_start_reports_nonzero_exit_as_blocker(tmp_path):
    repo = tmp_path / "singlegrain-ai-optimization-lab"
    repo.mkdir()
    _write_fake_goals_cli(repo, fail_start=True)

    output = handle_goals_command(
        "/goals start outbound-autoresearch",
        aliases={"outbound-autoresearch": _alias(repo)},
    )

    assert "blocked" in output.lower()
    assert "exit 2" in output
    assert "missing required export fields" in output


def test_handle_goals_unknown_alias_lists_supported_aliases(tmp_path):
    output = handle_goals_command(
        "/goals start unknown",
        aliases={"outbound-autoresearch": _alias(tmp_path)},
    )

    assert "Unknown goals alias" in output
    assert "outbound-autoresearch" in output


def test_extract_report_section_and_summary_renderer():
    report = """# Final Report Packet

## Slack Summary

Ran for 2 campaigns.

## Blockers

- Missing attribution.

## Approval Gates

- Gate 4: Instantly-Ready Change Packet Approval
"""

    assert extract_report_section(report, "Slack Summary") == "Ran for 2 campaigns."

    summary = render_slack_summary("outbound-autoresearch", report, status_text="Status: completed")

    assert "Ran for 2 campaigns." in summary
    assert "Missing attribution" in summary
    assert "Gate 4" in summary


def test_monitor_summary_marks_missing_expected_artifacts_incomplete():
    report = """# Final Report Packet

## Slack Summary

Ran, but artifacts are not complete yet.

## Expected Artifacts

Present:
- `artifacts/baseline-performance.md`

Missing:
- `artifacts/bottleneck-diagnosis.md`

## Blockers

- Monitor found missing expected artifacts.

## Approval Gates

- Gate 4: Instantly-Ready Change Packet Approval
"""

    summary = render_slack_summary("outbound-autoresearch", report, status_text="Status: running")

    assert "Monitor State" in summary
    assert "Incomplete: expected artifacts are missing." in summary
    assert "Expected Artifacts" in summary
    assert "artifacts/bottleneck-diagnosis.md" in summary

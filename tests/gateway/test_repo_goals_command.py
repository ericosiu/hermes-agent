import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_slack_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="C123",
            chat_type="channel",
            user_id="U123",
            thread_id="1715970000.000100",
        ),
        message_id="m1",
    )


@pytest.mark.asyncio
async def test_gateway_repo_goals_command_passes_origin_to_scheduler(monkeypatch):
    from gateway.run import GatewayRunner
    import hermes_cli.repo_goals as repo_goals

    calls = {}

    def fake_handle_goals_command(command, aliases=None, *, origin=None, deliver=None):
        calls["command"] = command
        calls["origin"] = origin
        calls["deliver"] = deliver
        return "scheduled"

    monkeypatch.setattr(repo_goals, "handle_goals_command", fake_handle_goals_command)

    runner = object.__new__(GatewayRunner)

    output = await GatewayRunner._handle_repo_goals_command(
        runner,
        _make_slack_event("/goals schedule outbound-autoresearch every 15m"),
    )

    assert output == "scheduled"
    assert calls["command"] == "/goals schedule outbound-autoresearch every 15m"
    assert calls["origin"]["platform"] == "slack"
    assert calls["origin"]["chat_id"] == "C123"
    assert calls["origin"]["thread_id"] == "1715970000.000100"
    assert calls["origin"]["user_id"] == "U123"

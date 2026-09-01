"""Parser and dispatch tests for the bounded Stage 2 CLI entry point."""

from __future__ import annotations

from copytradeaster.cli import main as cli


def test_parser_exposes_run_bot() -> None:
    args = cli.build_parser().parse_args(["run-bot"])
    assert args.command == "run-bot"


async def test_dispatch_run_bot_builds_and_runs_process(monkeypatch) -> None:
    calls: list[object] = []

    class FakeProcess:
        async def run(self) -> None:
            calls.append("run")

    sentinel_settings = object()

    def build_process(settings):
        calls.append(settings)
        return FakeProcess()

    monkeypatch.setattr(cli, "_load_settings", lambda: sentinel_settings)
    from copytradeaster.application import engine

    monkeypatch.setattr(engine, "build_stage2_process", build_process)

    args = cli.build_parser().parse_args(["run-bot"])
    assert await cli._dispatch(args) == 0
    assert calls == [sentinel_settings, "run"]

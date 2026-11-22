"""Suite-wide test configuration.

Tests marked ``live`` call the real model API and generate real images. They used to run whenever an API key was in
the environment, so a plain ``pytest`` on a developer machine with a key set spent quota on every run (a stage-agent
session, a judge call and a 2K image) without anyone asking for it. A key in the environment is not a request to
spend: live tests now run only when asked for, with ``--run-live`` or ``AGY3D_RUN_LIVE=1``. They still need the key.
"""

from __future__ import annotations

import os

import pytest

RUN_LIVE_ENV = "AGY3D_RUN_LIVE"

# The suite tests the flat path by default: EngineBudget.generated_surfaces takes its default from this variable
# (state.generated_surfaces_default). Tests of the generated surface set the field, or the variable, themselves.
os.environ["AGY3D_GENERATED_SURFACES"] = "0"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-live", action="store_true", default=False,
                     help=f"run tests marked 'live' (real model calls and image generations; also {RUN_LIVE_ENV}=1)")


def live_requested(config: pytest.Config) -> bool:
    return bool(config.getoption("--run-live")) or os.environ.get(RUN_LIVE_ENV) == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if live_requested(config):
        return
    skip = pytest.mark.skip(reason=f"live test: spends model quota; run with --run-live or {RUN_LIVE_ENV}=1")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)

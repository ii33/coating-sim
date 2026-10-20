"""Release unreachable Python wrapper cycles between completed Qt test cases.

No Qt API is imported or changed. Live/reachable objects and within-case
lifecycle assertions remain owned by the test and its fixtures.
"""
from __future__ import annotations

from collections.abc import Iterator
import gc

import pytest


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    yield
    # The protocol has returned: fixture funcargs and unittest's cached test
    # instance have been released, unlike at an ordinary fixture finalizer.
    if item.get_closest_marker("qt") is not None:
        gc.collect()

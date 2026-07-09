"""Tests for the /search and /context wing/room filter pass-through.

Both endpoints accept optional ``wing`` and ``room`` query params. When
supplied they are forwarded into ``mempalace_search``'s arguments;
when omitted the endpoints behave exactly as before (back-compat).

These invoke the endpoint coroutines directly with ``_call`` mocked so
no live daemon, MCP child, or palace is required.

Run with::

    python -m unittest tests.test_search_wing_filter -v
"""
import os
import sys
import unittest
from unittest.mock import patch, AsyncMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class _SearchTestBase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._patches = [
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
            patch.object(main, "_unwrap", side_effect=lambda r: r),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    @staticmethod
    def _captured_args(call_mock):
        """Pull the ``arguments`` dict the endpoint forwarded into _call()."""
        assert call_mock.call_count == 1, "expected exactly one MCP call"
        req = call_mock.call_args.args[0]
        return req["params"]["arguments"]


class TestSearchWingFilter(_SearchTestBase):

    async def test_search_no_filters_back_compat(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.search(q="hello", limit=5, x_api_key=None)
        args = self._captured_args(mock)
        self.assertEqual(args, {"query": "hello", "limit": 5})
        self.assertNotIn("wing", args)
        self.assertNotIn("room", args)

    async def test_search_forwards_wing(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.search(q="hello", limit=5, wing="kit_projects", x_api_key=None)
        args = self._captured_args(mock)
        self.assertEqual(args["wing"], "kit_projects")
        self.assertNotIn("room", args)

    async def test_search_forwards_room(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.search(q="hello", limit=5, room="log", x_api_key=None)
        args = self._captured_args(mock)
        self.assertEqual(args["room"], "log")
        self.assertNotIn("wing", args)

    async def test_search_forwards_both(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.search(
                q="hello", limit=3, wing="kit_projects", room="log", x_api_key=None,
            )
        args = self._captured_args(mock)
        self.assertEqual(args, {
            "query": "hello", "limit": 3,
            "wing": "kit_projects", "room": "log",
        })


class TestContextWingFilter(_SearchTestBase):

    async def test_context_no_filters_back_compat(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.context(topic="rfc-99", limit=5, x_api_key=None)
        args = self._captured_args(mock)
        self.assertEqual(args, {"query": "rfc-99", "limit": 5})
        self.assertNotIn("wing", args)
        self.assertNotIn("room", args)

    async def test_context_forwards_wing(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.context(topic="rfc-99", limit=5, wing="kit_projects", x_api_key=None)
        args = self._captured_args(mock)
        self.assertEqual(args["wing"], "kit_projects")

    async def test_context_forwards_both(self):
        with patch.object(main, "_call", new=AsyncMock(return_value={})) as mock:
            await main.context(
                topic="rfc-99", limit=2, wing="kit_projects", room="log", x_api_key=None,
            )
        args = self._captured_args(mock)
        self.assertEqual(args, {
            "query": "rfc-99", "limit": 2,
            "wing": "kit_projects", "room": "log",
        })


if __name__ == "__main__":
    unittest.main()

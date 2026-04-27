"""Tests for cozempic._validation — generic helpers used by strategies, CLI,
and env-var parsing."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from cozempic._validation import (
    ConfigError,
    coerce_choice,
    coerce_non_negative_int,
    coerce_positive_float,
    coerce_positive_int,
    parse_env_non_negative_int,
    parse_env_positive_int,
)


class TestCoercePositiveInt(unittest.TestCase):
    """Strict > 0. Distinct from coerce_non_negative_int (which allows 0)."""

    def test_returns_default_when_absent(self):
        self.assertEqual(coerce_positive_int({}, "k", default=30), 30)

    def test_returns_value_when_positive(self):
        self.assertEqual(coerce_positive_int({"k": 5}, "k", default=30), 5)

    def test_rejects_zero(self):
        with self.assertRaises(ConfigError) as ctx:
            coerce_positive_int({"k": 0}, "k", default=30)
        self.assertIn("positive", str(ctx.exception))

    def test_rejects_negative(self):
        with self.assertRaises(ConfigError):
            coerce_positive_int({"k": -1}, "k", default=30)

    def test_rejects_float(self):
        with self.assertRaises(ConfigError):
            coerce_positive_int({"k": 5.5}, "k", default=30)

    def test_rejects_string(self):
        with self.assertRaises(ConfigError):
            coerce_positive_int({"k": "5"}, "k", default=30)

    def test_rejects_bool(self):
        """True is an int in Python but almost never intended here."""
        with self.assertRaises(ConfigError):
            coerce_positive_int({"k": True}, "k", default=30)


class TestCoercePositiveFloat(unittest.TestCase):
    """Strict > 0 for MB thresholds. Accepts int in addition to float."""

    def test_returns_default_when_absent(self):
        self.assertEqual(coerce_positive_float({}, "mb", default=50.0), 50.0)

    def test_accepts_int(self):
        """User writes threshold=50 (int) expecting 50.0 MB — must not reject."""
        result = coerce_positive_float({"mb": 50}, "mb", default=10.0)
        self.assertEqual(result, 50.0)
        self.assertIsInstance(result, float)

    def test_accepts_float(self):
        self.assertEqual(coerce_positive_float({"mb": 50.5}, "mb", default=10.0), 50.5)

    def test_rejects_zero(self):
        with self.assertRaises(ConfigError):
            coerce_positive_float({"mb": 0}, "mb", default=10.0)

    def test_rejects_zero_float(self):
        with self.assertRaises(ConfigError):
            coerce_positive_float({"mb": 0.0}, "mb", default=10.0)

    def test_rejects_negative(self):
        with self.assertRaises(ConfigError):
            coerce_positive_float({"mb": -1.0}, "mb", default=10.0)

    def test_rejects_string(self):
        with self.assertRaises(ConfigError):
            coerce_positive_float({"mb": "50"}, "mb", default=10.0)

    def test_rejects_bool(self):
        with self.assertRaises(ConfigError):
            coerce_positive_float({"mb": True}, "mb", default=10.0)


class TestParseEnvPositiveInt(unittest.TestCase):
    """Env var helper: warn+fallback (does NOT raise). Used for
    COZEMPIC_CONTEXT_WINDOW — zero would cause divide-by-zero downstream."""

    def test_returns_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_ENV_POSINT", None)
            self.assertIsNone(parse_env_positive_int("TEST_ENV_POSINT"))

    def test_returns_none_when_empty(self):
        with patch.dict(os.environ, {"TEST_ENV_POSINT": ""}):
            self.assertIsNone(parse_env_positive_int("TEST_ENV_POSINT"))

    def test_returns_value_when_valid(self):
        with patch.dict(os.environ, {"TEST_ENV_POSINT": "1000000"}):
            self.assertEqual(parse_env_positive_int("TEST_ENV_POSINT"), 1000000)

    def test_returns_none_on_zero(self):
        """The falsy-trap bug: `0` currently passes `if val:` test in
        tokens.py and silently ignores the override. We reject it loudly."""
        with patch.dict(os.environ, {"TEST_ENV_POSINT": "0"}):
            self.assertIsNone(parse_env_positive_int("TEST_ENV_POSINT"))

    def test_returns_none_on_negative(self):
        with patch.dict(os.environ, {"TEST_ENV_POSINT": "-100"}):
            self.assertIsNone(parse_env_positive_int("TEST_ENV_POSINT"))

    def test_returns_none_on_non_numeric(self):
        with patch.dict(os.environ, {"TEST_ENV_POSINT": "abc"}):
            self.assertIsNone(parse_env_positive_int("TEST_ENV_POSINT"))

    def test_warns_on_invalid(self):
        """User should see a message on stderr — silent swallow is a UX bug."""
        import io
        import contextlib
        buf = io.StringIO()
        with patch.dict(os.environ, {"TEST_ENV_POSINT": "-100"}):
            with contextlib.redirect_stderr(buf):
                parse_env_positive_int("TEST_ENV_POSINT")
        self.assertIn("TEST_ENV_POSINT", buf.getvalue())
        self.assertIn("-100", buf.getvalue())

    def test_silent_when_unset(self):
        """No warning when the var is simply not set — that's the normal path."""
        import io
        import contextlib
        buf = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_ENV_POSINT", None)
            with contextlib.redirect_stderr(buf):
                parse_env_positive_int("TEST_ENV_POSINT")
        self.assertEqual(buf.getvalue(), "")


class TestParseEnvNonNegativeInt(unittest.TestCase):
    """Like positive-int but accepts 0 (valid for system_overhead_tokens —
    a session with no rules file legitimately has zero overhead)."""

    def test_accepts_zero(self):
        with patch.dict(os.environ, {"TEST_ENV_NNINT": "0"}):
            self.assertEqual(parse_env_non_negative_int("TEST_ENV_NNINT"), 0)

    def test_returns_value_when_positive(self):
        with patch.dict(os.environ, {"TEST_ENV_NNINT": "25000"}):
            self.assertEqual(parse_env_non_negative_int("TEST_ENV_NNINT"), 25000)

    def test_rejects_negative(self):
        with patch.dict(os.environ, {"TEST_ENV_NNINT": "-1"}):
            self.assertIsNone(parse_env_non_negative_int("TEST_ENV_NNINT"))

    def test_rejects_non_numeric(self):
        with patch.dict(os.environ, {"TEST_ENV_NNINT": "xyz"}):
            self.assertIsNone(parse_env_non_negative_int("TEST_ENV_NNINT"))


# ── Backwards compat: re-exports from strategies/_config still work ────────

class TestBackwardsCompatReExport(unittest.TestCase):
    """strategies/_config.py re-exports these — existing strategy imports
    must continue to resolve after the refactor."""

    def test_reexport_coerce_non_negative_int(self):
        from cozempic.strategies._config import coerce_non_negative_int as reexported
        self.assertIs(reexported, coerce_non_negative_int)

    def test_reexport_coerce_choice(self):
        from cozempic.strategies._config import coerce_choice as reexported
        self.assertIs(reexported, coerce_choice)

    def test_reexport_ConfigError(self):
        from cozempic.strategies._config import ConfigError as reexported
        self.assertIs(reexported, ConfigError)


if __name__ == "__main__":
    unittest.main()

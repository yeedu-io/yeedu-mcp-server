"""Test configuration.

The tests here never reach the platform. They exercise the pure helpers that
turn an API response into a table, the confirmation gate, and the tool
registry, so no API key or base URL is needed — but the real `.env` in a
developer's checkout would otherwise be picked up by `pydantic-settings` and
make results depend on whose machine is running. Pinning the settings that
tests read keeps them deterministic.
"""

import os

os.environ.setdefault("YEEDU_API_BASE_URL", "https://yeedu.invalid:8080")
os.environ.setdefault("YEEDU_API_KEY", "not-a-real-token")
os.environ.pop("YEEDU_UI_BASE_URL", None)

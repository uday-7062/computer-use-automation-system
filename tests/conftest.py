from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

FIXTURE_HTML = Path(__file__).parent / "fixtures" / "legacy_app" / "index.html"
FIXTURE_URL = f"file://{FIXTURE_HTML.resolve()}"


@pytest.fixture
def browser():
    # Function-scoped (not session-scoped): src/replay_engine.py and src/llm_agent.py
    # each open their own `with sync_playwright()` block per run, and Playwright's
    # sync API does not support a second one starting while an earlier one is still
    # open in the same thread -- keeping this alive across the whole test session
    # would break the replay-engine tests that run after this fixture is first used.
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1024, "height": 768})
    p = ctx.new_page()
    p.goto(FIXTURE_URL)
    yield p
    ctx.close()


@pytest.fixture
def fixture_url() -> str:
    return FIXTURE_URL

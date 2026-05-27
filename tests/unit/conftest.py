import pytest

import bot


@pytest.fixture(autouse=True)
def clear_bot_state():
    bot._token_cache.clear()
    bot._scrape_state["running"] = False
    bot._scrape_state["mode"] = None
    bot._scrape_state["started_at"] = None
    yield
    bot._token_cache.clear()
    bot._scrape_state["running"] = False
    bot._scrape_state["mode"] = None
    bot._scrape_state["started_at"] = None

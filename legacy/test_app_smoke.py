import os

import pytest
from streamlit.testing.v1 import AppTest

PAGES = ["Action list", "Draft POs", "Exceptions", "Scenario lab", "SKU manager", "Decisions & shadow mode",
         "Data quality", "Assumptions & limitations", "Rules & config"]


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_offline(page, monkeypatch):
    monkeypatch.setenv("STORAGE", "csv")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    at = AppTest.from_file(str(__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py"), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, (page, at.exception)
    assert any("Offline CSV mode" in w.value for w in at.warning)

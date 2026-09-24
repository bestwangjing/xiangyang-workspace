import pytest
from backend import providers


@pytest.fixture(autouse=True)
def _isolated_secret_root(tmp_path, monkeypatch):
    """Never touch the real DPAPI credential directory during tests."""
    monkeypatch.setattr(providers, 'secret_root', lambda: tmp_path / 'secrets')

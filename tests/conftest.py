import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "data")
    monkeypatch.setattr(main, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(main, "DOWNLOADS", tmp_path / "downloads")
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "worker", idle)
    with TestClient(main.app) as test_client:
        yield test_client

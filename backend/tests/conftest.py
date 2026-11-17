"""pytest 夹具：每个用例一个全新的临时 SQLite 库，与真实 data/ 目录隔离。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 清掉所有 app.* 模块缓存，确保下面的 config 补丁被重新拾取
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import app.config as config  # noqa: E402

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_dayseal.db")

    import app.main as main  # noqa: E402  导入即建表 + seed

    with TestClient(main.app) as c:
        yield c

"""alembic 마이그레이션 테스트 — 임시 DB 에 적용해 컬럼과 기존 행이 어떻게 되는지 본다."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def alembic_cfg(monkeypatch, tmp_path):
    """임시 DB 를 가리키는 alembic 설정 — 개발 DB(backend/deepeye.db)를 건드리지 않는다."""
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    # env.py 는 app.database.DATABASE_URL 을 읽는다.
    monkeypatch.setattr("app.database.DATABASE_URL", url)
    # ini 파일 없이 만든다 — env.py 가 ini 의 로깅 설정으로 다른 테스트의 로거를 끄지 않게.
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    engine = create_engine(url)
    yield cfg, engine
    engine.dispose()


def test_upgrade_adds_codec_column_with_existing_cameras_unknown(alembic_cfg):
    """기존 카메라는 코덱을 모르는 상태(NULL)로 남는다 — 재등록 때 판별해 채운다."""
    cfg, engine = alembic_cfg
    command.upgrade(cfg, "eb81928ad755")
    with engine.begin() as con:
        con.execute(text("INSERT INTO ip_cams (name, rtsp_url, stream_key) VALUES ('a', 'rtsp://x/1', 'ipcam-a')"))

    command.upgrade(cfg, "head")

    with engine.connect() as con:
        assert con.execute(text("SELECT stream_key, codec FROM ip_cams")).all() == [("ipcam-a", None)]


def test_downgrade_removes_codec_column(alembic_cfg):
    cfg, engine = alembic_cfg
    command.upgrade(cfg, "7927ae6eadfb")

    command.downgrade(cfg, "eb81928ad755")

    assert "codec" not in {c["name"] for c in inspect(engine).get_columns("ip_cams")}


def test_upgrade_starts_existing_cameras_at_version_1(alembic_cfg):
    """기존 카메라는 버전 1로 시작한다 — 화면이 받은 버전과 비교할 기준이 생긴다."""
    cfg, engine = alembic_cfg
    command.upgrade(cfg, "7927ae6eadfb")
    with engine.begin() as con:
        con.execute(text("INSERT INTO ip_cams (name, rtsp_url, stream_key) VALUES ('a', 'rtsp://x/1', 'ipcam-a')"))

    command.upgrade(cfg, "head")

    with engine.connect() as con:
        assert con.execute(text("SELECT stream_key, version FROM ip_cams")).all() == [("ipcam-a", 1)]


def test_downgrade_removes_version_column(alembic_cfg):
    cfg, engine = alembic_cfg
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "7927ae6eadfb")

    assert "version" not in {c["name"] for c in inspect(engine).get_columns("ip_cams")}

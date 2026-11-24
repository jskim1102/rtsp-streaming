import os
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def stream_key_prefix() -> str:
    """이 모듈이 만드는 stream_key 의 앞부분 — 동기화가 남은 path 를 가를 때도 쓴다."""
    prefix = os.getenv("MEDIAMTX_PATH_PREFIX")
    return f"{prefix}__ipcam-" if prefix else "ipcam-"


def generate_stream_key() -> str:
    """등록용 고유 스트림 키 생성.

    공유 mediamtx 사용 시 MEDIAMTX_PATH_PREFIX 로 path 네임스페이싱 → `<prefix>__ipcam-<hex>`
    (프로젝트간 path 충돌·교차열람 차단). 미설정(단독 mediamtx)이면 `ipcam-<hex>` (하위호환).
    mediamtx.py/transcode($MTX_PATH)/WHEP 는 stream_key 를 그대로 쓰므로 자동 네임스페이싱된다.
    """
    return f"{stream_key_prefix()}{uuid.uuid4().hex[:8]}"


class IpCam(Base):
    __tablename__ = "ip_cams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    rtsp_url: Mapped[str] = mapped_column(String(500), nullable=False)
    stream_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, default=generate_stream_key)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # ffprobe 로 판별한 첫 비디오 코덱(h264·hevc 등) — 재등록 때 다시 판별하지 않고 쓴다.
    # None = 모름(판별 실패 또는 마이그레이션 전 카메라) → 재인코딩(libx264)하고, 동기화가 재등록할 때 다시 판별한다.
    # 주소를 바꿀 때만 다시 판별한다 — 카메라 설정에서 코덱만 바꾸면 카메라를 지웠다 다시 등록해야 한다.
    codec: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 낙관적 락용 버전 — 새 카메라는 1, 수정 API 가 저장에 성공할 때마다 1 올린다 (이름만 바꿔도).
    # 화면이 받은 버전과 다르면 다른 사람이 먼저 수정한 것이라 저장을 거부한다(409).
    # 동기화의 재등록·코덱 저장은 사용자가 바꾼 내용이 아니라 올리지 않는다.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

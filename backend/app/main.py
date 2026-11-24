import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ORIGINS, MAX_IPCAMS, logger
from app.database import SessionLocal, init_db
from app.ipcam import router as ipcam_router
from app.mediamtx import run_sync_loop
from app.stream_status import hook_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """서버 시작/종료 시 리소스 관리."""
    # alembic 이 스키마 정본이지만, 테스트/로컬 첫 실행 편의를 위해 init_db() 유지.
    init_db()

    # mediamtx path 동기화는 요청 처리와 분리된 스레드에서 서버 수명 동안 반복한다.
    # 첫 주기는 기동 직후 바로 돈다 — 빠진 path 만 등록하므로 백엔드만 재시작하면 등록 0건이다.
    # 세션 팩토리를 넘겨 주기마다 새 세션을 연다.
    stop_sync = threading.Event()
    threading.Thread(target=run_sync_loop, args=(stop_sync, SessionLocal), daemon=True).start()

    logger.info("RTSP Streaming API 서버 시작")
    yield
    # 대기 중인 루프를 바로 깨워 끝낸다.
    # 등록 도중이면 기다리지 않는다 — 데몬 스레드라 프로세스와 함께 끝난다.
    # 그때 못 한 등록은 다음 기동의 첫 주기가 채운다.
    stop_sync.set()
    logger.info("서버 종료")


app = FastAPI(title="RTSP Streaming API", lifespan=lifespan)

# CORS
cors_origins = ["*"] if CORS_ORIGINS == "*" else [o.strip() for o in CORS_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ipcam_router)
app.include_router(hook_router)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, int]:
    """프론트가 사용할 런타임 설정 — 등록 cap(MAX_IPCAMS). 프론트 하드코딩 제거(P2-1)."""
    return {"max_ipcams": MAX_IPCAMS}

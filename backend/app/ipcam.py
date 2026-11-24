import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, field_serializer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import stream_status
from app.config import MAX_IPCAMS
from app.database import get_db
from app.masking import _MASK, _restore_masked_password, _split_credentials, mask_rtsp_credentials
from app.mediamtx import camera_lock, list_path_status, register_stream, remove_stream, update_stream
from app.mediamtx import _probe_codec, _validate_rtsp_url
from app.models import IpCam, _generate_stream_key

logger = logging.getLogger("rtsp-streaming.ipcam")

router = APIRouter(prefix="/api/ipcams", tags=["ipcam"])


@contextmanager
def _camera_lock_or_409(stream_key: str) -> Iterator[None]:
    """카메라별 작업 잠금을 잡는다 — 같은 카메라에 다른 작업이 진행 중이면 기다리지 않고 409.

    - 다른 작업: 다른 사람의 등록·수정·삭제, 백그라운드 동기화의 재등록.
    - 잠금 안에서 mediamtx 와 DB 를 함께 바꾼다 — 두 쪽이 서로 다른 주소로 남지 않게.
    - 기다리지 않는다 — 앞 작업이 카메라 응답을 기다리면 수십 초 걸리고, 기다렸다 저장하면 앞사람 수정을 덮어쓴다.
    """
    lock = camera_lock(stream_key)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="다른 작업이 진행 중입니다 — 잠시 후 다시 시도하세요")
    try:
        yield
    finally:
        lock.release()


# 입력 길이 상한 — DB 컬럼 길이(models.py 의 String(100)·String(500))를 그대로 쓴다.
# SQLite 는 컬럼 길이를 강제하지 않아 API 가 막는다. 화면 입력란의 maxLength 도 같은 값이다.
_NAME_MAX = IpCam.name.type.length
_URL_MAX = IpCam.rtsp_url.type.length


def _clean_name(name: str) -> str:
    """앞뒤 공백을 지운 이름을 돌려준다 — 비었거나 길면 400 (화면과 같은 규칙)."""
    name = name.strip()
    if not 1 <= len(name) <= _NAME_MAX:
        raise HTTPException(status_code=400, detail=f"카메라 이름은 1~{_NAME_MAX}자로 입력하세요")
    return name


def _check_url_length(rtsp_url: str) -> None:
    """주소 길이 검사 — 길면 400. 빈 주소는 _check_rtsp_url 의 스킴 검사가 막는다."""
    if len(rtsp_url) > _URL_MAX:
        raise HTTPException(status_code=400, detail=f"RTSP 주소는 {_URL_MAX}자 이하로 입력하세요")


def _check_rtsp_url(rtsp_url: str) -> None:
    """rtsp_url 검증 — 위험하면 400 (DB 쓰기 전에 막아 오염·500 방지)."""
    try:
        _validate_rtsp_url(rtsp_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _register_or_fail(stream_key: str, rtsp_url: str, codec: str | None) -> bool:
    """register_stream 호출 — mediamtx 미설정(RuntimeError)·실패(False) 모두 False 로 정규화.

    호출부가 등록 성공을 확인하고, 실패 시 저장한 행을 지우거나 DB 를 그대로 둘 수 있게 한다(P1-3).
    """
    try:
        return register_stream(stream_key, rtsp_url, codec)
    except RuntimeError:
        logger.exception("mediamtx 미설정으로 스트림 등록 불가: %s", stream_key)
        return False


def _update_or_fail(stream_key: str, rtsp_url: str, codec: str | None) -> bool:
    """update_stream(PATCH) 호출 — 미설정(RuntimeError)·실패(False) 모두 False 로 정규화.

    update 의 암전방지 순서용: 호출부가 DB 저장 전에 PATCH 성공을 확인하고, 실패 시
    DB 를 그대로 둔다. PATCH 실패면 mediamtx 가 기존 path 를 보존하므로 카메라 암전이 없다.
    """
    try:
        return update_stream(stream_key, rtsp_url, codec)
    except RuntimeError:
        logger.exception("mediamtx 미설정으로 스트림 갱신 불가: %s", stream_key)
        return False


def _remove_or_fail(stream_key: str) -> bool:
    """remove_stream 호출 — 미설정(RuntimeError)·실패(False) 모두 False 로 정규화.

    호출부(delete_ipcam)가 commit 전에 mediamtx path 제거 성공(또는 이미 없음=멱등)을 확인하고,
    실패 시 DB row 삭제를 중단한다 — 삭제된 카메라의 mediamtx path 가 살아남아 URL 로 계속
    재생되는 orphan 라이브 스트림(노출면 격리 붕괴)을 막는다(codex #4, security).
    """
    try:
        return remove_stream(stream_key)
    except RuntimeError:
        logger.exception("mediamtx 미설정으로 스트림 제거 불가: %s", stream_key)
        return False


def _guard_masked_credential_exfil(new_raw: str, old_url: str) -> None:
    """비번 마스킹복원(***) credential-exfil 가드 (security HIGH).

    update 시 비밀번호가 `***`(마스킹) 채로 들어오면 기존 실제 비번이 복원돼 등록된다.
    이때 비번 외 컴포넌트(scheme/user/host/port/path)가 바뀌어 있으면, 사용자가 모르는
    **다른 호스트로 실제 비번이 재전송**될 수 있다(예: `:***@공격자host`). 이를 막기 위해
    비번이 *** 이면서 나머지가 old 와 다르면 거부 — 평문 비번 재제출을 요구한다.
    비번이 *** 가 아니거나(사용자가 새 비번 입력) old 에 자격증명이 없으면 가드 무관.
    """
    new = _split_credentials(new_raw)
    if new is None or new[2] != _MASK:
        return  # 자격증명 없음 / 비번이 *** 아님 (사용자가 새 비번 전체 입력) — 복원 안 일어남
    old = _split_credentials(old_url)
    # old 에 복원할 실제 비번이 없으면 복원할 게 없음 — 가드 무관(_restore 가 그대로 통과).
    if old is None or old[2] is None:
        return
    # new[2](비번)만 빼고 prefix(scheme)·user·rest(host:port/path) 가 동일해야 복원 허용.
    if (new[0], new[1], new[3]) != (old[0], old[1], old[3]):
        raise HTTPException(
            status_code=400,
            detail="비밀번호를 *** 그대로 두고 주소(호스트/경로 등)를 바꿀 수 없습니다 — "
                   "주소를 변경하려면 실제 비밀번호를 다시 입력하세요.",
        )


# ─── 요청/응답 스키마 ───


class IpCamCreate(BaseModel):
    name: str
    rtsp_url: str


class IpCamUpdate(BaseModel):
    name: str
    rtsp_url: str
    # 화면이 수정 창을 열 때 받은 버전 — 필수다. 없으면 422(낙관적 락 우회 차단).
    version: int


class IpCamResponse(BaseModel):
    id: int
    name: str
    rtsp_url: str
    stream_key: str
    created_at: datetime
    version: int

    model_config = {"from_attributes": True}

    @field_serializer("rtsp_url")
    def _mask_url(self, v: str) -> str:
        # 모든 응답(list/create/update)에서 비밀번호 마스킹 — 평문 자격증명 노출 차단.
        return mask_rtsp_credentials(v)


# ─── 엔드포인트 ───


@router.get("", response_model=list[IpCamResponse])
def list_ipcams(db: Session = Depends(get_db)) -> list[IpCam]:
    """등록된 IP CAM 목록 조회"""
    return db.query(IpCam).order_by(IpCam.id).all()


@router.post("", response_model=IpCamResponse, status_code=201)
def create_ipcam(body: IpCamCreate, db: Session = Depends(get_db)) -> IpCam:
    """IP CAM 등록 + mediamtx path 등록. 등록 대수가 MAX_IPCAMS 이상이면 409.

    - 순서: DB 커밋(짧은 쓰기 락) → mediamtx 등록(락 밖).
    - 등록에는 ffprobe(최대 4초) + 등록 요청(최대 5초)이 걸린다 — 그동안 쓰기 락을 잡으면 다른 등록·수정·삭제가 기다리다 500 이 난다.
    - 등록이 실패하면 저장한 행을 지우고 503 — "성공처럼 보이는 실패" 방지(P1-3).
    - 커밋 전부터 카메라 잠금을 잡는다 — 커밋~등록 사이에 동기화가 '빠진 path'로 보고 먼저 등록하지 않게.
    """
    # 입력 검사는 DB·mediamtx 를 건드리기 전에 한다. 앞뒤 공백을 지운 값으로 검사하고 저장한다.
    name = _clean_name(body.name)
    rtsp_url = body.rtsp_url.strip()
    _check_url_length(rtsp_url)
    _check_rtsp_url(rtsp_url)
    # 잠금을 커밋 전에 잡으려고 stream_key 를 미리 만든다. 새 키라 잠금이 겹치는 일은 없다.
    stream_key = _generate_stream_key()
    with _camera_lock_or_409(stream_key):
        # F7 동시성: count+insert 를 SQLite 쓰기 락으로 직렬화한다. count 조회 *전에* BEGIN IMMEDIATE
        # 로 RESERVED 락을 먼저 획득 — 동시에 들어온 다른 생성 요청은 이 트랜잭션이 commit 될 때까지
        # (busy_timeout 만큼) 대기했다가 갱신된 count 를 다시 관찰한다. 이렇게 하지 않으면 두 요청이
        # 모두 count<MAX 를 보고 둘 다 insert 해 cap 을 초과할 수 있다(SQLite 의 count SELECT 는
        # autocommit 이라 락을 잡지 않으므로 락을 명시적으로 선점해야 한다).
        db.execute(text("BEGIN IMMEDIATE"))
        if db.query(IpCam).count() >= MAX_IPCAMS:
            db.rollback()  # 위에서 선점한 쓰기 락 해제 후 409
            raise HTTPException(
                status_code=409,
                detail=f"최대 {MAX_IPCAMS}대까지 등록할 수 있습니다",
            )
        cam = IpCam(name=name, rtsp_url=rtsp_url, stream_key=stream_key)
        db.add(cam)
        # 여기서 쓰기 락이 풀린다 — 아래 판별·mediamtx 등록은 락 없이 한다.
        db.commit()

        # 코덱은 여기서 한 번만 판별해 등록에 쓰고 저장한다 — 재등록 때는 저장값을 쓴다.
        registered = False
        try:
            codec = _probe_codec(rtsp_url)
            registered = _register_or_fail(stream_key, rtsp_url, codec)
        finally:
            if not registered:
                # 이미 커밋했으므로 롤백 대신 행을 지운다 — 등록 실패(False)든 예외든 행을 남기지 않는다.
                # 이 삭제마저 실패하면 행이 남고, 동기화가 잠금이 풀린 뒤 재등록한다.
                db.delete(cam)
                db.commit()
        if not registered:
            raise HTTPException(
                status_code=503,
                detail="mediamtx 스트림 등록에 실패했습니다 — 카메라가 저장되지 않았습니다",
            )

        # 코덱 저장이 실패해도 카메라는 이미 등록됐다 — 모름(None)으로 두면 재등록 때 다시 판별한다.
        cam.codec = codec
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception("코덱 저장 실패 — 재등록 때 다시 판별한다: %s", stream_key)

    db.refresh(cam)
    logger.info("IP CAM 등록: id=%d name=%s stream_key=%s", cam.id, cam.name, cam.stream_key)
    return cam


def _find_stream_key(db: Session, cam_id: int) -> str:
    """잠금을 잡기 위해 stream_key 만 먼저 읽는다 — 없으면 404."""
    stream_key = db.query(IpCam.stream_key).filter(IpCam.id == cam_id).scalar()
    if stream_key is None:
        raise HTTPException(status_code=404, detail="IP CAM을 찾을 수 없습니다")
    return stream_key


@router.put("/{cam_id}", response_model=IpCamResponse)
def update_ipcam(cam_id: int, body: IpCamUpdate, db: Session = Depends(get_db)) -> IpCam:
    """IP CAM 수정 + (rtsp 변경 시) mediamtx path PATCH 갱신.

    rtsp_url 의 비밀번호가 마스킹(`:***@`)이고 **주소(scheme/user/host/port/path)가 기존과 동일**
    하면, *** 를 기존 실제 비밀번호로 복원해 저장한다(비번 재입력 없이 이름 등만 수정하는 경우).
    비밀번호가 *** 인 채로 **주소를 바꾸면 거부한다(400)** — 실비번이 사용자가 모르는 호스트로
    재전송되는 credential-exfil 을 막기 위해, 주소 변경 시엔 실제 비밀번호를 다시 입력해야 한다
    (_guard_masked_credential_exfil). 비밀번호 자체를 바꿀 때도 *** 를 지우고 새 비밀번호를 입력한다.

    순서 = [카메라 잠금 → 버전 확인 → (주소 변경 시) mediamtx 갱신(락 밖) → DB commit(짧은 쓰기 락)].
    - 버전 확인(낙관적 락): body.version 이 DB 와 다르면 409. 저장에 성공하면 버전이 1 오른다.
    - mediamtx 를 먼저 바꾸는 이유: ffprobe·요청 동안 쓰기 락을 잡지 않기 위해서다.
    - 저장이 실패하면 mediamtx 를 이전 주소로 되돌리고 503.
    """
    # 입력 검사는 DB·mediamtx 를 건드리기 전에 한다. 앞뒤 공백을 지운 값으로 검사하고 저장한다.
    name = _clean_name(body.name)
    raw_url = body.rtsp_url.strip()
    stream_key = _find_stream_key(db, cam_id)
    with _camera_lock_or_409(stream_key):
        # 잠금을 얻기 전에 다른 요청이 지웠거나 바꿨을 수 있어 잠금 안에서 다시 읽는다.
        cam = db.query(IpCam).filter(IpCam.id == cam_id).first()
        if not cam:
            raise HTTPException(status_code=404, detail="IP CAM을 찾을 수 없습니다")

        # 낙관적 락: 화면을 연 뒤 다른 사람이 먼저 저장했으면 거부한다 — 앞사람의 수정을 모르고 덮어쓰지 않게.
        # mediamtx 를 건드리기 전에 확인한다. 카메라 잠금 안이라 비교한 뒤 저장할 때까지 버전이 바뀌지 않는다.
        if body.version != cam.version:
            raise HTTPException(
                status_code=409,
                detail="다른 사람이 먼저 수정했습니다 — 바뀐 값을 확인하고 다시 저장하세요",
            )

        old_url = cam.rtsp_url
        old_codec = cam.codec  # 저장이 실패해 되돌릴 때 다시 판별하지 않고 쓴다.
        # 보안: 비번이 *** 인 채로 주소(host 등)만 바꾸면 실비번이 다른 호스트로 샐 수 있다 —
        # 비번 외 컴포넌트가 바뀌었으면 400(평문 재입력 요구). 동일할 때만 복원 허용.
        _guard_masked_credential_exfil(raw_url, old_url)
        # *** 마스킹된 비밀번호만 기존 실제값으로 복원, 나머지 수정(주소 등)은 보존.
        new_url = _restore_masked_password(raw_url, old_url)
        # 되돌린 뒤의 길이로 검사한다 — *** 가 실제 비밀번호로 바뀌며 길어지고, 검사 도입 전 저장된 긴 주소도 여기서 걸린다.
        _check_url_length(new_url)
        url_changed = new_url != old_url
        new_codec = old_codec

        # RTSP 주소 변경 시에만 mediamtx 갱신. DB 를 건드리기 전, 쓰기 락 없이 먼저 한다.
        # update_stream(PATCH)이 새 url 을 기존 path 에 **원자적 부분 갱신**한다 — teardown-before-register
        # (remove 선행) 없음. 실패 시 mediamtx 가 기존 config 를 그대로 보존하므로 old path 가 살아있다(카메라 암전 0).
        if url_changed:
            _check_rtsp_url(new_url)
            # 새 주소의 코덱을 한 번만 판별해 PATCH 와 등록 폴백에 같이 쓴다. 이름만 바꾸면 판별하지 않는다.
            new_codec = _probe_codec(new_url)
            # PATCH(부분 갱신) 우선 시도. 실패(예: mediamtx 가 독립 재시작돼 path 가 사라진 뒤
            # 수정 → PATCH 404)면 register 로 재생성 폴백(원본 deepeye remove+add 동작 복원) —
            # 그래야 path 부재 상태에서도 수정이 카메라를 복구한다. 둘 다 실패면 DB 는 그대로 → 503.
            if not _update_or_fail(stream_key, new_url, new_codec) and not _register_or_fail(stream_key, new_url, new_codec):
                raise HTTPException(
                    status_code=503,
                    detail="mediamtx 재등록에 실패했습니다 — 변경이 저장되지 않았습니다(기존 스트림 유지)",
                )

        # DB 저장은 짧은 쓰기 락만 잡는다.
        cam.name = name
        cam.rtsp_url = new_url
        cam.codec = new_codec
        cam.version += 1  # 저장이 실패하면 롤백돼 그대로다.
        try:
            db.commit()
        except SQLAlchemyError:
            # 드물다(디스크 오류, 5초 넘는 락 대기). mediamtx 만 새 주소로 남지 않게 이전 주소로 되돌린다.
            # 되돌리기도 실패하면 로그만 남긴다 — 영상은 새 주소, 화면(DB)은 이전 주소인 상태가 된다.
            db.rollback()
            logger.exception("IP CAM 수정 저장 실패: id=%d", cam_id)
            if url_changed and not _update_or_fail(stream_key, old_url, old_codec):
                logger.error("mediamtx 되돌리기 실패 — 새 주소로 재생 중, DB 는 이전 주소: %s", stream_key)
            raise HTTPException(
                status_code=503,
                detail="변경 저장에 실패했습니다 — 카메라가 수정되지 않았습니다",
            )
        db.refresh(cam)

    logger.info("IP CAM 수정: id=%d name=%s", cam.id, cam.name)
    return cam


@router.delete("/{cam_id}", status_code=204)
def delete_ipcam(cam_id: int, db: Session = Depends(get_db)) -> None:
    """IP CAM 삭제 + mediamtx path 제거.

    mediamtx path 제거가 실패하면 DB row 를 지우지 않는다(503) — 지우면 mediamtx path 가
    살아남아 삭제된 카메라가 URL 로 계속 재생되는 orphan 라이브 스트림(노출면 격리 붕괴)이
    된다(codex #4, security). 제거 성공(또는 이미 없음=멱등) 확인 후에만 commit.

    순서 = [카메라 잠금 → mediamtx 제거 성공확인 → DB commit].
    - 잠금을 잡고 있는 동안 백그라운드 동기화는 이 카메라를 재등록하지 않는다.
    - 그래서 제거~commit 사이에 path 가 되살아나지 않는다 (예전에는 commit 뒤 한 번 더 지웠다).
    """
    stream_key = _find_stream_key(db, cam_id)
    with _camera_lock_or_409(stream_key):
        # 잠금을 얻기 전에 다른 요청이 지웠을 수 있어 잠금 안에서 다시 읽는다.
        cam = db.query(IpCam).filter(IpCam.id == cam_id).first()
        if not cam:
            raise HTTPException(status_code=404, detail="IP CAM을 찾을 수 없습니다")

        if not _remove_or_fail(stream_key):
            raise HTTPException(
                status_code=503,
                detail="mediamtx 스트림 제거에 실패했습니다 — 카메라가 삭제되지 않았습니다",
            )

        db.delete(cam)
        db.commit()

    logger.info("IP CAM 삭제: id=%d stream_key=%s", cam_id, stream_key)


@router.get("/events", response_class=EventSourceResponse)
async def stream_ipcam_events() -> AsyncIterator[ServerSentEvent]:
    """카메라 상태 SSE — 화면이 카메라별 stats 를 1초마다 폴링하던 것을 대체한다.

    - 연결하면 `snapshot`(전체 `{stream_key: {active, readers}}`)을 먼저 보낸다.
    - 그 뒤로는 값이 바뀐 카메라만 `update`(`{stream_key, active, readers}`)로 보낸다.
    - 상태는 mediamtx 훅이 바로 바꾸고, 백그라운드 동기화가 5초마다 바로잡는다.
    - keep-alive 주석은 FastAPI 기본값(15초)으로 보낸다.
    """
    async for event in stream_status.hub.subscribe(list_path_status):
        yield event

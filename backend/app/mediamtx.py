"""mediamtx path 동기화 — RTSP 카메라를 mediamtx API 로 등록/삭제/조회.

백엔드는 mediamtx 의 control plane 만 담당(가벼움) — 실제 스트리밍/transcode 는
mediamtx 컨테이너가 한다. v1 에서 떼냈던 경로를 v2 에서 복원한 모듈.
"""

import logging
import subprocess
import threading
from collections.abc import Callable

import httpx
from sqlalchemy.orm import Session

from app.config import MEDIAMTX_API, MEDIAMTX_BACKEND_PASS, MEDIAMTX_BACKEND_USER
from app.masking import mask_rtsp_credentials
from app.models import IpCam, stream_key_prefix

logger = logging.getLogger("rtsp-streaming.mediamtx")

# 동기화 주기 — 영상 플레이어 재접속 시도(약 30초) 안에 path 가 복구되도록 짧게 둔다.
# 한 주기의 기본 부하는 mediamtx 목록 조회 1회다.
SYNC_INTERVAL_SEC = 5.0


def _validate_rtsp_url(rtsp_url: str) -> None:
    """rtsp_url 트러스트 경계 검증 (injection 2중 방어의 1단 — spec v2-transcode-binding §영향).

    - 스킴은 `rtsp://` 또는 `rtsps://`(TLS) 만 허용.
    - 제어문자(\\n \\r \\x00) + 공백/탭 거부 — runOnDemand 가 셸 없이 mediamtx 가
      공백으로 arg 분리하므로, 공백/제어문자가 있으면 ffmpeg 인자가 깨진다.
    RFC1918/loopback IP 는 거부하지 않는다 — IP 카메라는 LAN(192.168.x)에 있는 게 정상.
    """
    if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
        # 에러 메시지(→ 400 응답 본문)에 비번 평문이 새지 않게 마스킹 (P1, CEO #66).
        raise ValueError(f"rtsp:// 또는 rtsps:// 스킴이 아닙니다: {mask_rtsp_credentials(rtsp_url)!r}")
    # 공백/제어문자 + 셸 메타문자 거부 (denylist). runOnDemand 는 셸 없이 직접 exec 되지만,
    # mediamtx 의 $ 변수치환 + 미래 셸-wrapping 대비 defense-in-depth.
    # allowlist 가 아닌 denylist — `!`/`@`/`%` 등 RTSP 비밀번호의 valid 문자는 허용해야 한다.
    # `&` 는 제외: RTSP query string(Dahua/Hikvision `channel=3&subtype=0`)의 정상 문자이고,
    # runOnDemand 는 셸 없이 exec(공백 split)되므로 `&` 는 argv 토큰 내부 리터럴로 무해하다.
    # 실제 위험인 공백(arg 분리)·`$`(mediamtx $VAR 치환 충돌)은 계속 차단한다.
    _FORBIDDEN = ("\n", "\r", "\x00", " ", "\t",
                  "$", "`", ";", "|", "(", ")", "{", "}", "<", ">", "\\", "'", '"')
    if any(c in rtsp_url for c in _FORBIDDEN):
        # 비번에 금지문자가 있어도 평문이 새지 않게 마스킹해서 메시지에 담는다 (P1, CEO #66).
        raise ValueError(
            f"rtsp_url 에 금지문자(공백/제어/셸메타)가 포함돼 있습니다: {mask_rtsp_credentials(rtsp_url)!r}"
        )


def _probe_codec(rtsp_url: str) -> str | None:
    """카메라 첫 비디오 스트림의 코덱명 감지 (ffprobe) — h264=passthrough/else=libx264 결정용.

    `ffprobe -select_streams v:0 -show_entries stream=codec_name` 로 코덱(h264/hevc/mjpeg…)을
    소문자로 읽는다. 백엔드에 ffprobe 필요(Dockerfile 에 ffmpeg 설치). 미설치·미연결·타임아웃 등
    **어떤 실패든 None** → 호출부가 안전기본 libx264 로 폴백한다(현행 동작 유지 = 롤백 안전).
    rtsp_url 은 인자 리스트로만 전달(셸 없음 → injection 무관)·실패 로그엔 비번 마스킹.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-rtsp_transport", "tcp", "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", "-i", rtsp_url],
            capture_output=True, text=True, timeout=4,
        )
        codec = proc.stdout.strip().lower()
        return codec or None
    except (subprocess.SubprocessError, OSError):
        logger.warning("ffprobe 코덱 감지 실패 → libx264 폴백: %s", mask_rtsp_credentials(rtsp_url))
        return None


def _transcode_command(rtsp_url: str) -> str:
    """runOnDemand transcode 커맨드 — h264 소스는 무손실 passthrough, 그 외는 libx264.

    셸 없이 ffmpeg 를 직접 exec 한다(mediamtx 가 공백분리 후 직접 실행). 등록 시점에
    `_probe_codec` 로 코덱을 감지해 **정적 커맨드**를 만든다 — 셸 변수($C/$V) 분기를 쓰지
    않으므로 mediamtx 의 $VAR 치환과 충돌하지 않는다(3개 모듈이 포기했던 함정 회피).
      - h264: `-c:v copy` 무손실 패스스루(재인코딩 0 → 화질 보존·CPU 0). WebRTC 네이티브
        코덱이라 그대로 송출. IDR 간격은 카메라 GOP 를 따른다(copy 라 강제 불가).
      - 그 외(H.265/MJPEG…) 또는 probe 실패: libx264 재인코딩(WebRTC 호환 보장) +
        `-force_key_frames` 2초 IDR 강제(저fps 카메라 WHEP join 지연 방지).
    rtsp_url 은 `_validate_rtsp_url`(공백/제어문자/셸메타 거부)로 검증돼 안전하게 인자로 들어간다.
    `$RTSP_PORT`/`$MTX_PATH` 는 mediamtx 변수 — 정상 치환된다.
    """
    if _probe_codec(rtsp_url) == "h264":
        video = "-c:v copy"
    else:
        video = ("-c:v libx264 -preset veryfast -tune zerolatency "
                 "-force_key_frames expr:gte(t,n_forced*2)")
    # -hide_banner -loglevel error: ffmpeg stdout 의 `Input ... rtsp://admin:비번@` 평문 덤프 차단(#100.4).
    return (
        f"ffmpeg -hide_banner -loglevel error -rtsp_transport tcp -i {rtsp_url} "
        f"{video} -an "
        "-f rtsp rtsp://localhost:$RTSP_PORT/$MTX_PATH"
    )


def _require_api() -> str:
    """MEDIAMTX_API 가 설정돼 있는지 확인 — 없으면 명시적 fail-fast.

    config 가 빈 문자열(미설정)이면 httpx 가 cryptic 'missing protocol' 에러를
    던지고 register/remove 는 그걸 silent 하게 삼킨다. 그 전에 명확히 막는다.
    """
    if not MEDIAMTX_API:
        raise RuntimeError(
            "MEDIAMTX_API 가 설정되지 않았습니다. "
            "Docker 는 compose environment 로, 로컬은 backend/.env 로 주입하세요."
        )
    return MEDIAMTX_API


def _auth() -> tuple[str, str] | None:
    """mediamtx API Basic auth — backend user 자격증명(#100). 비번 미설정이면 None(무인증, 하위호환)."""
    return (MEDIAMTX_BACKEND_USER, MEDIAMTX_BACKEND_PASS) if MEDIAMTX_BACKEND_PASS else None


def register_stream(stream_key: str, rtsp_url: str) -> bool:
    """mediamtx 에 path 등록 — 조건부 transcode runOnDemand (패턴 C).

    `{source: rtsp}` 패스스루 대신 runOnDemand 로 등록해 H.264 외 코덱을 재인코딩한다.
    rtsp_url 은 bash 에 들어가므로 검증 후 사용.
    """
    _require_api()
    _validate_rtsp_url(rtsp_url)
    try:
        resp = httpx.post(
            f"{MEDIAMTX_API}/v3/config/paths/add/{stream_key}",
            json={
                "runOnDemand": _transcode_command(rtsp_url),
                "runOnDemandCloseAfter": "10s",
            },
            auth=_auth(),
            timeout=5,
        )
        if resp.status_code in (200, 201):
            # rtsp_url 은 비번 포함 → 로그엔 마스킹값만 (P1 로그 평문누수 차단, CEO #62).
            logger.info("mediamtx path 등록: %s → %s", stream_key, mask_rtsp_credentials(rtsp_url))
            return True
        logger.warning("mediamtx 등록 실패: %d %s", resp.status_code, resp.text)
        return False
    except httpx.HTTPError:
        logger.exception("mediamtx 연결 실패 (등록: %s)", stream_key)
        return False


def update_stream(stream_key: str, rtsp_url: str) -> bool:
    """기존 mediamtx path 의 runOnDemand 를 새 rtsp_url 로 **원자적 부분 갱신** (PATCH).

    `PATCH /v3/config/paths/patch/{key}` — runOnDemand 만 교체하고 나머지 path 설정은 보존한다
    (검증된 mediamtx 1.19 동작). teardown-before-register(remove→add)가 유발하던 재등록 갭을
    없앤다: 성공 시 path 가 새 url 로 그 자리에서 바뀌고, 실패(400 등) 시 mediamtx 가 기존
    config 를 **그대로 보존**해 카메라 암전이 없다. stream_key 가 update 전후 동일하므로 PATCH 가
    정확히 맞는다. 존재하지 않는 path 면 404(이 함수는 등록된 카메라 update 경로에서만 호출).
    HTTP 메서드는 PATCH (POST 면 404 — mediamtx 가 verb 로 구분).
    """
    _require_api()
    _validate_rtsp_url(rtsp_url)
    try:
        resp = httpx.patch(
            f"{MEDIAMTX_API}/v3/config/paths/patch/{stream_key}",
            json={"runOnDemand": _transcode_command(rtsp_url)},
            auth=_auth(),
            timeout=5,
        )
        if resp.status_code in (200, 201):
            logger.info("mediamtx path 갱신: %s → %s", stream_key, mask_rtsp_credentials(rtsp_url))
            return True
        logger.warning("mediamtx 갱신 실패: %d %s", resp.status_code, resp.text)
        return False
    except httpx.HTTPError:
        logger.exception("mediamtx 연결 실패 (갱신: %s)", stream_key)
        return False


def remove_stream(stream_key: str) -> bool:
    """mediamtx 에서 스트림 path 제거. 끝상태=path 없음이면 True, 아니면 False (security).

    삭제 성공(200/204) 또는 path 가 애초에 없음(404)은 둘 다 '끝상태 = path 없음'이라
    **멱등 성공(True)**. 그 외 상태코드/연결실패는 False — 호출부(delete_ipcam)가 이 False 를
    보고 DB commit 을 중단해, 삭제된 카메라의 mediamtx path 가 살아남아 URL 로 계속 재생되는
    **orphan 라이브 스트림(노출면 격리 붕괴)**을 막는다(codex #4).
    """
    _require_api()
    try:
        resp = httpx.delete(
            f"{MEDIAMTX_API}/v3/config/paths/delete/{stream_key}",
            auth=_auth(),
            timeout=5,
        )
        if resp.status_code in (200, 204):
            logger.info("mediamtx path 제거: %s", stream_key)
            return True
        if resp.status_code == 404:
            # 이미 없는 path — 끝상태(path 없음)는 동일하므로 멱등 성공으로 본다.
            logger.info("mediamtx path 이미 없음(멱등 성공): %s", stream_key)
            return True
        logger.warning("mediamtx 제거 실패: %d %s", resp.status_code, resp.text)
        return False
    except httpx.HTTPError:
        logger.exception("mediamtx 연결 실패 (제거: %s)", stream_key)
        return False


def list_config_paths() -> set[str] | None:
    """mediamtx 에 설정된 path 이름 목록 (`GET /v3/config/paths/list`).

    - 성공: path 이름 집합. 카메라 상한 64대 + all_others 라 기본 페이지(100개) 하나에 다 들어온다.
    - 실패(연결 실패·200 아님): None — 호출부가 '경로 없음'과 구분해 그 주기를 건너뛴다.
    - 로그를 남기지 않는다 — 5초마다 실패할 수 있어 동기화가 연결 상태가 바뀔 때만 남긴다.
    """
    _require_api()
    try:
        resp = httpx.get(f"{MEDIAMTX_API}/v3/config/paths/list", auth=_auth(), timeout=5)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return {item["name"] for item in resp.json()["items"]}


class StreamReconciler:
    """DB 카메라 목록에 mediamtx path 를 맞춘다 — 상태 조회 요청과 분리된 백그라운드 동기화.

    - 빠진 path(DB 에만 있음): 재등록한다. mediamtx 가 재시작되면 동적 등록한 path 가 사라진다.
    - 남은 path(mediamtx 에만 있음): 이 모듈 형식만, 두 주기 연속 보이면 지운다.
    - mediamtx 조회 실패: 그 주기를 건너뛴다. 실패를 '경로 없음'으로 보면 재등록이 폭주한다.
    - 주기 사이에 남은 path 후보와 직전 연결 상태를 기억하므로 인스턴스 하나를 계속 쓴다.
    """

    def __init__(self) -> None:
        self._orphan_candidates: set[str] = set()  # 직전 주기에 처음 본 남은 path
        self._connected: bool | None = None  # None = 첫 조회 전 (첫 성공은 '다시 연결됨'으로 보지 않음)

    def run_once(self, db: Session) -> None:
        """동기화 한 주기 — DB 읽기 → mediamtx 목록 조회 → 빠진 path 재등록 → 남은 path 정리."""
        # DB 를 먼저 읽는다 — 등록 API 는 mediamtx 등록 뒤에 DB 를 커밋한다.
        # 커밋 전 카메라는 여기 안 보이므로, 등록 중인 path 를 빠졌다고 보고 중복 등록하지 않는다.
        cams = dict(db.query(IpCam.stream_key, IpCam.rtsp_url).all())

        # 조회 실패는 '경로 없음'이 아니다 — 등록·삭제 없이 끝낸다.
        # 로그는 연결 상태가 바뀌는 순간에만 남긴다 (끊김 1줄, 다시 연결됨 1줄).
        paths = list_config_paths()
        if paths is None:
            if self._connected is not False:
                logger.warning("mediamtx 연결 끊김 — 동기화를 건너뛴다")
            self._connected = False
            return
        if self._connected is False:
            logger.info("mediamtx 다시 연결됨 — 동기화를 재개한다")
        self._connected = True

        # 빠진 path 를 한 대씩 재등록한다 — 동시 등록은 하지 않는다.
        # 한 대에 ffprobe(최대 4초) + 등록 요청(최대 5초)이 걸려, 대량 재등록이면 주기가 길어진다.
        for stream_key in sorted(cams.keys() - paths):
            self._register_missing(db, stream_key, cams[stream_key])

        # 남은 path 는 이 모듈 형식만 대상이다 — all_others·다른 프로젝트 path 는 건드리지 않는다.
        # 처음 보면 후보로만 둔다 — 등록 API 가 path 를 만든 뒤 커밋하기 직전일 수 있다.
        # 다음 주기에도 보이면 지운다. 제거에 실패하면 후보로 남아 다음 주기에 다시 시도한다.
        orphans = {p for p in paths - cams.keys() if p.startswith(stream_key_prefix())}
        for stream_key in sorted(orphans & self._orphan_candidates):
            if remove_stream(stream_key):
                logger.info("DB 에 없는 mediamtx path 제거: %s", stream_key)
                orphans.discard(stream_key)
        self._orphan_candidates = orphans

    @staticmethod
    def _register_missing(db: Session, stream_key: str, rtsp_url: str) -> None:
        """빠진 path 하나를 재등록한다 — 실패하면 다음 주기에 다시 시도된다."""
        # ffprobe 가 실패해도(카메라 꺼짐 등) 재인코딩 명령으로 등록은 성공한다.
        logger.info("mediamtx path 누락 → 재등록: %s", stream_key)
        if not register_stream(stream_key, rtsp_url):
            return
        # 등록하는 사이 삭제 API 가 카메라를 지웠으면 되살아난 path 를 바로 지운다.
        # 삭제 API 의 sweep 보다 늦게 등록이 끝난 경우를 막는다 — 남겨 두면 지운 카메라가 재생된다.
        if db.query(IpCam.id).filter(IpCam.stream_key == stream_key).first() is None:
            logger.info("재등록 중 삭제된 카메라 → path 제거: %s", stream_key)
            remove_stream(stream_key)


def run_sync_loop(
    stop: threading.Event,
    session_factory: Callable[[], Session],
    interval: float = SYNC_INTERVAL_SEC,
) -> None:
    """백그라운드 스레드 본체 — stop 이 켜질 때까지 동기화를 반복한다.

    - 서버 시작 직후 첫 주기를 돌고, 한 주기가 끝나면 interval 초 쉰다 (고정 간격 아님).
    - 주기마다 세션을 새로 열고 닫는다 — 오래 연 세션이 커넥션을 붙잡지 않게.
    - session_factory·interval 을 받아 테스트가 가짜 세션·0초 대기로 돌릴 수 있다.
    """
    reconciler = StreamReconciler()
    while not stop.is_set():
        db = session_factory()
        try:
            reconciler.run_once(db)
        except Exception:
            # 주기 하나의 예외로 스레드가 멈추면 복구가 조용히 끊긴다 — 로그만 남기고 다음 주기로.
            logger.exception("mediamtx 동기화 주기 실패")
        finally:
            db.close()
        # 종료 신호가 오면 대기 중에도 바로 깨어난다.
        stop.wait(interval)


def get_path(stream_key: str) -> dict | None:
    """mediamtx path 상태 조회 (/v3/paths/get/<path>).

    반환은 mediamtx 응답 dict (`ready`, `readers` 등) 또는 path 없으면 None.
    """
    _require_api()
    try:
        resp = httpx.get(f"{MEDIAMTX_API}/v3/paths/get/{stream_key}", auth=_auth(), timeout=5)
        if resp.status_code == 200:
            return resp.json()
        return None
    except httpx.HTTPError:
        logger.exception("mediamtx path 조회 실패: %s", stream_key)
        return None

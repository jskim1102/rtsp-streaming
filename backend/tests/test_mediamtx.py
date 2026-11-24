"""mediamtx 모듈 테스트 — fail-fast(MEDIAMTX_API 미설정) + runOnDemand transcode
payload + rtsp_url injection 가드.
"""

import logging
import os
import subprocess
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.mediamtx as mediamtx
from app.database import Base
from app.models import IpCam, generate_stream_key, stream_key_prefix
from app.stream_status import PathStatus, StreamStatusHub

# autouse 패치 전 원본 probe_codec — 실제 함수 단위테스트에서 mock subprocess 로 직접 호출.
_REAL_PROBE = mediamtx.probe_codec


@pytest.fixture(autouse=True)
def _stub_probe_codec():
    """코덱을 모르는 카메라를 재등록할 때 부르는 probe_codec 이 실제 ffprobe(네트워크/바이너리)를
    호출하지 않게 — 기본 None(모름 → libx264 폴백). 판별 결과가 필요한 테스트는 return_value 를 바꾼다."""
    with patch("app.mediamtx.probe_codec", return_value=None) as p:
        yield p


def test_register_stream_raises_when_api_unset():
    with patch.object(mediamtx, "MEDIAMTX_API", ""):
        with pytest.raises(RuntimeError, match="MEDIAMTX_API"):
            mediamtx.register_stream("ipcam-x", "rtsp://x/1")


def test_remove_stream_raises_when_api_unset():
    with patch.object(mediamtx, "MEDIAMTX_API", ""):
        with pytest.raises(RuntimeError, match="MEDIAMTX_API"):
            mediamtx.remove_stream("ipcam-x")


# ─── remove_stream 끝상태 반환 (codex #4 — orphan 라이브 스트림 차단) ───


@pytest.mark.parametrize("status", [200, 204])
def test_remove_stream_returns_true_on_success(status):
    """삭제 성공(200/204) → True (끝상태=path 없음)."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.delete") as delete:
        delete.return_value = MagicMock(status_code=status)
        assert mediamtx.remove_stream("ipcam-x") is True


def test_remove_stream_returns_true_on_404_idempotent():
    """path 가 애초에 없음(404) → 끝상태(path 없음)는 동일하므로 멱등 성공(True)."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.delete") as delete:
        delete.return_value = MagicMock(status_code=404)
        assert mediamtx.remove_stream("ipcam-x") is True


def test_remove_stream_returns_false_on_error_status():
    """그 외 상태코드(예: 500) → False (호출부가 DB commit 중단 → orphan 차단)."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.delete") as delete:
        delete.return_value = MagicMock(status_code=500, text="boom")
        assert mediamtx.remove_stream("ipcam-x") is False


def test_remove_stream_returns_false_on_connection_error():
    """mediamtx 연결 실패(HTTPError) → False (끝상태 미확인 → 삭제 보류)."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.delete", side_effect=mediamtx.httpx.ConnectError("down")):
        assert mediamtx.remove_stream("ipcam-x") is False


# ─── runOnDemand 조건부 transcode payload (패턴 C) ───


def test_register_posts_runondemand_payload():
    """register_stream 이 source 대신 runOnDemand transcode 커맨드를 보낸다."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        mediamtx.register_stream("ipcam-abc", "rtsp://cam/stream1")

        _, kwargs = post.call_args
        body = kwargs["json"]
        # source 패스스루 아님 — runOnDemand 사용
        assert "source" not in body
        cmd = body["runOnDemand"]
        # 셸 없이 직접 ffmpeg (mediamtx 가 셸변수 $C/$V 를 먹어서 조건분기 불가)
        assert cmd.startswith("ffmpeg")
        # 카메라 rtsp 는 등록 시점 치환
        assert "rtsp://cam/stream1" in cmd
        # 코덱을 넘기지 않으면(모름) libx264 재인코딩 — h264 패스스루는 조건부 transcode 테스트가 본다
        assert "libx264" in cmd
        # 시간기반 keyframe 강제 — 저fps 카메라 WHEP join 지연 방지 (~2초 내 IDR)
        assert "force_key_frames" in cmd
        # mediamtx 런타임 변수는 literal($ 그대로)
        assert "$RTSP_PORT" in cmd and "$MTX_PATH" in cmd
        # on-demand 종료
        assert body["runOnDemandCloseAfter"] == "10s"


# ─── rtsp_url injection 가드 (셸리스 exec + 스킴/셸메타 denylist) ───


def test_register_rejects_non_rtsp_scheme():
    """rtsp/rtsps 아닌 스킴은 ValueError (POST 도달 전)."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        with pytest.raises(ValueError, match="스킴"):
            mediamtx.register_stream("ipcam-x", "http://cam/stream")
        post.assert_not_called()


def test_register_accepts_rtsps_scheme():
    """rtsps:// (TLS) 스킴도 허용 — 일부 카메라가 암호화 RTSP 를 쓴다."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        assert mediamtx.register_stream("ipcam-x", "rtsps://cam:322/stream") is True


@pytest.mark.parametrize("bad", [
    "rtsp://cam/stream\nmalicious",   # 개행 (헤더/명령 분리 표면)
    "rtsp://cam/stream\rmalicious",   # 캐리지리턴
    "rtsp://cam/stream\x00trunc",     # 널바이트 (C 문자열 절단)
])
def test_register_rejects_control_chars(bad):
    """제어문자(\\n \\r \\x00)는 ValueError (denylist) — POST 도달 전."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        with pytest.raises(ValueError, match="금지문자"):
            mediamtx.register_stream("ipcam-x", bad)
        post.assert_not_called()


@pytest.mark.parametrize("evil", [
    "rtsp://cam/stream'; touch /tmp/pwned; echo '",  # quote breakout + 명령주입
    'rtsp://cam/stream"$(whoami)"',                   # command substitution
    "rtsp://cam/`id`",                                 # backtick
    "rtsp://cam/stream; echo hi",                      # 세미콜론 명령 분리
])
def test_register_rejects_shell_metachars(evil):
    """셸 메타문자(; $ ` | & ( ) ' " 등)는 denylist 로 ValueError — POST 도달 전.

    runOnDemand 는 셸 없이 직접 exec 되지만(mediamtx), mediamtx 의 $ 변수치환 +
    미래 셸-wrapping 대비 defense-in-depth 로 메타문자를 등록 시점에 거부한다.
    """
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        with pytest.raises(ValueError, match="금지문자"):
            mediamtx.register_stream("ipcam-x", evil)
        post.assert_not_called()


def test_register_accepts_normal_rtsp_url():
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        # 인증정보 포함 정상 URL 도 통과
        assert mediamtx.register_stream("ipcam-x", "rtsp://user:pass@192.168.0.10:554/h264") is True


def test_register_accepts_query_string_ampersand():
    """`&` 는 RTSP query string(Dahua/Hikvision channel=X&subtype=Y)의 정상 문자 — 허용.

    셸 없이 exec 되는 runOnDemand 에선 `&` 가 argv 토큰 내부 리터럴이라 무해하므로
    denylist 에서 제외했다. 실제 카메라 URL 형태가 400 으로 막히지 않아야 한다.
    """
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        url = "rtsp://admin:pw@192.168.0.113:554/cam/realmonitor?channel=3&subtype=0"
        assert mediamtx.register_stream("ipcam-x", url) is True
        # POST 가 실제로 도달하고 rtsp_url 이 runOnDemand 커맨드에 그대로 들어갔는지
        cmd = post.call_args.kwargs["json"]["runOnDemand"]
        assert "channel=3&subtype=0" in cmd


# ─── P1 로그 평문 비번 누수 차단 (CEO #62) ───


def test_register_masks_password_in_log(caplog):
    """register_stream 의 등록 로그가 비번을 평문으로 남기지 않는다.

    버그: `→ %s` 로 rtsp_url 전체를 찍어 stdout/docker 로그에 비번 평문 노출.
    / 와 # 를 포함한 비번으로도 로그에 *** 만 남아야 한다.
    """
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        with caplog.at_level(logging.INFO, logger="rtsp-streaming.mediamtx"):
            mediamtx.register_stream("ipcam-x", "rtsp://admin:pa/ss#word@10.0.0.5:554/cam")

    text = caplog.text
    assert "ipcam-x" in text  # 등록 로그가 실제로 찍혔는지
    assert "rtsp://admin:***@10.0.0.5:554/cam" in text  # 마스킹된 형태로
    for leak in ("pa/ss", "#word", "pa/ss#word"):
        assert leak not in text, f"로그 비번 평문 누수: {leak!r}"


# ─── update_stream (PATCH 원자 부분갱신 — remove→add 재등록 갭 제거) ───


def test_update_stream_uses_patch_verb_and_path():
    """update_stream 은 PATCH /v3/config/paths/patch/{key} 로 runOnDemand 만 갱신한다."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.patch") as patch_req:
        patch_req.return_value = MagicMock(status_code=200)
        assert mediamtx.update_stream("ipcam-abc", "rtsp://cam/stream2") is True

        args, kwargs = patch_req.call_args
        assert args[0].endswith("/v3/config/paths/patch/ipcam-abc")  # PATCH verb + patch path
        body = kwargs["json"]
        assert "rtsp://cam/stream2" in body["runOnDemand"]  # 새 url 로 runOnDemand 갱신
        assert "source" not in body  # runOnDemand 만 부분갱신


def test_update_stream_raises_when_api_unset():
    with patch.object(mediamtx, "MEDIAMTX_API", ""):
        with pytest.raises(RuntimeError, match="MEDIAMTX_API"):
            mediamtx.update_stream("ipcam-x", "rtsp://x/1")


# ─── 백그라운드 동기화 (DB → mediamtx path 맞춤, 상태 조회에서 분리) ───


@pytest.fixture()
def sessions(monkeypatch):
    """임시 SQLite 파일 DB 세션 팩토리 — 작업과 API 가 서로 다른 세션을 쓰는 상황을 재현한다."""
    monkeypatch.delenv("MEDIAMTX_PATH_PREFIX", raising=False)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine)
    engine.dispose()
    os.unlink(path)


@pytest.fixture()
def mtx_calls():
    """작업이 부르는 등록·제거를 mock — 실 httpx 호출 차단."""
    with patch("app.mediamtx.register_stream", return_value=True) as register, \
         patch("app.mediamtx.remove_stream", return_value=True) as remove:
        yield {"register": register, "remove": remove}


def _add_cam(sessions, stream_key: str, rtsp_url: str = "rtsp://cam/1", codec: str | None = None) -> None:
    with sessions() as s:
        s.add(IpCam(name=stream_key, rtsp_url=rtsp_url, stream_key=stream_key, codec=codec))
        s.commit()


def _stored_codec(sessions, stream_key: str) -> str | None:
    with sessions() as s:
        return s.query(IpCam.codec).filter(IpCam.stream_key == stream_key).scalar()


def _run_cycles(sessions, listings: list, hub=None, statuses=None) -> None:
    """listings 순서대로 설정 목록을 돌려주며 주기를 돌린다 — None 은 조회 실패.

    hub 를 주면 statuses(주기별 상태 목록, 기본은 모두 조회 실패)로 상태 확인도 돌린다.
    """
    reconciler = mediamtx.StreamReconciler(hub or StreamStatusHub())
    with patch("app.mediamtx.list_config_paths", side_effect=listings), \
         patch("app.mediamtx.list_path_status", side_effect=statuses or [None] * len(listings)):
        for _ in listings:
            with sessions() as db:
                reconciler.run_once(db)


@pytest.mark.parametrize(("prefix", "expected"), [(None, "ipcam-"), ("rtsp_streaming", "rtsp_streaming__ipcam-")])
def test_stream_key_prefix_matches_generated_keys(monkeypatch, prefix, expected):
    """남은 path 판별 형식은 실제로 생성되는 stream_key 와 같아야 한다."""
    if prefix is None:
        monkeypatch.delenv("MEDIAMTX_PATH_PREFIX", raising=False)
    else:
        monkeypatch.setenv("MEDIAMTX_PATH_PREFIX", prefix)

    assert stream_key_prefix() == expected
    assert generate_stream_key().startswith(expected)


def test_list_config_paths_returns_path_names():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"itemCount": 2, "pageCount": 1, "items": [{"name": "all_others"}, {"name": "ipcam-a"}]}
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.get", return_value=resp) as get:
        assert mediamtx.list_config_paths() == {"all_others", "ipcam-a"}
    assert get.call_args.args[0] == "http://mtx:9997/v3/config/paths/list"


@pytest.mark.parametrize("failure", [MagicMock(status_code=401), mediamtx.httpx.ConnectError("down")])
def test_list_config_paths_returns_none_without_logging(failure, caplog):
    """실패는 None — 로그는 작업이 연결 상태가 바뀔 때만 남긴다."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.get", side_effect=[failure]), \
         caplog.at_level(logging.DEBUG, logger="rtsp-streaming"):
        assert mediamtx.list_config_paths() is None
    assert caplog.records == []


def test_reconcile_registers_only_missing_paths(sessions, mtx_calls):
    _add_cam(sessions, "ipcam-a", "rtsp://cam/a")
    _add_cam(sessions, "ipcam-b", "rtsp://cam/b")

    _run_cycles(sessions, [{"all_others", "ipcam-a"}])

    mtx_calls["register"].assert_called_once_with("ipcam-b", "rtsp://cam/b", None)
    mtx_calls["remove"].assert_not_called()


def test_reconcile_retries_failed_registration_next_cycle(sessions, mtx_calls):
    _add_cam(sessions, "ipcam-a")
    mtx_calls["register"].return_value = False

    _run_cycles(sessions, [set(), set()])

    assert mtx_calls["register"].call_count == 2


def test_reconcile_skips_cycle_when_mediamtx_unreachable(sessions, mtx_calls):
    """목록 조회 실패를 '경로 없음'으로 보지 않는다 — 재등록 폭주 차단."""
    _add_cam(sessions, "ipcam-a")

    _run_cycles(sessions, [None, None])

    mtx_calls["register"].assert_not_called()
    mtx_calls["remove"].assert_not_called()


def test_reconcile_logs_only_when_connection_state_changes(sessions, mtx_calls, caplog):
    with caplog.at_level(logging.INFO, logger="rtsp-streaming"):
        _run_cycles(sessions, [None, None, set(), set(), None])

    messages = [r.getMessage() for r in caplog.records]
    assert sum("끊김" in m for m in messages) == 2  # 처음 끊김 + 다시 끊김
    assert sum("다시 연결" in m for m in messages) == 1


def test_reconcile_removes_orphan_after_two_consecutive_cycles(sessions, mtx_calls):
    _run_cycles(sessions, [{"ipcam-z"}])
    mtx_calls["remove"].assert_not_called()

    _run_cycles(sessions, [{"ipcam-z"}, {"ipcam-z"}])
    mtx_calls["remove"].assert_called_once_with("ipcam-z")


def test_reconcile_keeps_orphan_candidate_across_skipped_cycle(sessions, mtx_calls):
    """조회 실패로 건너뛴 주기는 후보를 바꾸지 않는다."""
    _run_cycles(sessions, [{"ipcam-z"}, None, {"ipcam-z"}])

    mtx_calls["remove"].assert_called_once_with("ipcam-z")


def test_reconcile_resets_orphan_candidate_when_path_disappears(sessions, mtx_calls):
    _run_cycles(sessions, [{"ipcam-z"}, set(), {"ipcam-z"}])

    mtx_calls["remove"].assert_not_called()


def test_reconcile_retries_orphan_removal_after_failure(sessions, mtx_calls):
    mtx_calls["remove"].side_effect = [False, True]

    _run_cycles(sessions, [{"ipcam-z"}, {"ipcam-z"}, {"ipcam-z"}])

    assert mtx_calls["remove"].call_count == 2


def test_reconcile_never_removes_paths_outside_module_format(sessions, mtx_calls, monkeypatch):
    monkeypatch.setenv("MEDIAMTX_PATH_PREFIX", "rtsp_streaming")
    foreign = {"all_others", "other__ipcam-x", "ipcam-y", "rtsp_streaming__cam"}

    _run_cycles(sessions, [foreign, foreign])

    mtx_calls["remove"].assert_not_called()


def _run_cycle_with_listing(sessions, listing) -> None:
    """설정 목록 조회 대신 listing() 을 부르며 한 주기를 돌린다 — 조회 사이에 API 가 끼어드는 상황용."""
    reconciler = mediamtx.StreamReconciler(StreamStatusHub())
    with patch("app.mediamtx.list_config_paths", side_effect=listing), \
         patch("app.mediamtx.list_path_status", return_value=None):
        with sessions() as db:
            reconciler.run_once(db)


def test_reconcile_skips_camera_deleted_before_registration(sessions, mtx_calls):
    """DB 를 읽은 뒤 삭제 API 가 카메라를 지웠으면 재등록하지 않는다 — 지운 카메라를 되살리지 않는다."""
    _add_cam(sessions, "ipcam-a")

    def listing_after_api_delete():
        with sessions() as api_db:
            api_db.query(IpCam).filter(IpCam.stream_key == "ipcam-a").delete()
            api_db.commit()
        return set()

    _run_cycle_with_listing(sessions, listing_after_api_delete)

    mtx_calls["register"].assert_not_called()


def test_reconcile_registers_with_latest_db_url(sessions, mtx_calls):
    _add_cam(sessions, "ipcam-a", "rtsp://cam/old")

    def listing_after_api_update():
        with sessions() as api_db:
            api_db.query(IpCam).filter(IpCam.stream_key == "ipcam-a").update({"rtsp_url": "rtsp://cam/new"})
            api_db.commit()
        return set()

    _run_cycle_with_listing(sessions, listing_after_api_update)

    mtx_calls["register"].assert_called_once_with("ipcam-a", "rtsp://cam/new", None)


def test_reconcile_skips_camera_locked_by_api(sessions, mtx_calls):
    """API 가 작업 중인 카메라는 기다리지 않고 그 주기만 건너뛴다."""
    _add_cam(sessions, "ipcam-a")

    with mediamtx.camera_lock("ipcam-a"):
        _run_cycles(sessions, [set()])
    mtx_calls["register"].assert_not_called()

    _run_cycles(sessions, [set()])
    mtx_calls["register"].assert_called_once_with("ipcam-a", "rtsp://cam/1", None)


def test_reconcile_holds_camera_lock_while_registering(sessions, mtx_calls):
    """재등록하는 동안 그 카메라는 잠겨 있다 — 그 사이 수정·삭제 API 는 409 를 받는다."""
    _add_cam(sessions, "ipcam-a")
    locked = []
    mtx_calls["register"].side_effect = lambda stream_key, rtsp_url, codec: locked.append(
        mediamtx.camera_lock(stream_key).locked()) or True

    _run_cycles(sessions, [set()])

    assert locked == [True]
    assert not mediamtx.camera_lock("ipcam-a").locked()


def test_reconcile_reuses_stored_codec_without_probing(sessions, mtx_calls, _stub_probe_codec):
    _add_cam(sessions, "ipcam-a", codec="h264")

    _run_cycles(sessions, [set()])

    mtx_calls["register"].assert_called_once_with("ipcam-a", "rtsp://cam/1", "h264")
    _stub_probe_codec.assert_not_called()


def test_reconcile_probes_unknown_codec_and_saves_it(sessions, mtx_calls, _stub_probe_codec):
    """등록 때 카메라가 꺼져 있었거나 마이그레이션 전 카메라면, 재등록 때 판별해 저장한다."""
    _add_cam(sessions, "ipcam-a", codec=None)
    _stub_probe_codec.return_value = "h264"

    _run_cycles(sessions, [set()])

    _stub_probe_codec.assert_called_once_with("rtsp://cam/1")
    mtx_calls["register"].assert_called_once_with("ipcam-a", "rtsp://cam/1", "h264")
    assert _stored_codec(sessions, "ipcam-a") == "h264"
    # 사용자가 바꾼 내용이 아니라 버전은 올리지 않는다 — 화면을 연 사람이 이유 없이 충돌을 받지 않게.
    with sessions() as s:
        assert s.query(IpCam.version).filter(IpCam.stream_key == "ipcam-a").scalar() == 1


def test_reconcile_keeps_codec_unknown_when_probe_fails(sessions, mtx_calls, _stub_probe_codec):
    _add_cam(sessions, "ipcam-a", codec=None)

    _run_cycles(sessions, [set()])

    mtx_calls["register"].assert_called_once_with("ipcam-a", "rtsp://cam/1", None)
    assert _stored_codec(sessions, "ipcam-a") is None


def test_reconcile_failure_of_one_camera_does_not_block_others(sessions, mtx_calls, _stub_probe_codec, caplog):
    """한 대의 실패(판별·DB 오류 등)가 다른 카메라의 재등록과 남은 path 정리를 막지 않는다."""
    _add_cam(sessions, "ipcam-a", "rtsp://cam/a", codec=None)
    _add_cam(sessions, "ipcam-b", "rtsp://cam/b", codec="h264")

    def probe(rtsp_url):
        raise RuntimeError("판별 중 예상 못 한 오류")

    _stub_probe_codec.side_effect = probe
    with caplog.at_level(logging.ERROR, logger="rtsp-streaming"):
        _run_cycles(sessions, [{"ipcam-zz"}, {"ipcam-zz"}])

    assert mtx_calls["register"].call_args_list[-1].args == ("ipcam-b", "rtsp://cam/b", "h264")
    mtx_calls["remove"].assert_called_once_with("ipcam-zz")
    assert any("ipcam-a" in r.getMessage() for r in caplog.records)


def test_camera_lock_is_one_per_stream_key():
    assert mediamtx.camera_lock("ipcam-a") is mediamtx.camera_lock("ipcam-a")
    assert mediamtx.camera_lock("ipcam-a") is not mediamtx.camera_lock("ipcam-b")


def test_unused_camera_lock_does_not_accumulate():
    """아무도 잡고 있지 않은 잠금은 사라진다 — 실패한 등록 요청(새 stream_key)마다 항목이 쌓이지 않게."""
    import gc

    lock = mediamtx.camera_lock("ipcam-gone")
    assert "ipcam-gone" in mediamtx._camera_locks
    del lock
    gc.collect()

    assert "ipcam-gone" not in mediamtx._camera_locks


def test_sync_loop_survives_cycle_exception(caplog):
    """주기 하나가 예외로 끝나도 스레드는 다음 주기를 돈다."""
    stop = threading.Event()
    calls = []

    def run_once(db):
        calls.append(db)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()

    factory = MagicMock()
    with patch.object(mediamtx.StreamReconciler, "run_once", side_effect=run_once), \
         caplog.at_level(logging.ERROR, logger="rtsp-streaming"):
        mediamtx.run_sync_loop(stop, factory, interval=0)

    assert len(calls) == 2
    assert factory.return_value.close.call_count == 2
    assert any("동기화 주기 실패" in r.getMessage() for r in caplog.records)


# ─── 상태 확인 (훅 보정 — 동기화 주기의 첫 단계) ───


def test_list_path_status_returns_module_paths_with_reader_ids(monkeypatch):
    monkeypatch.delenv("MEDIAMTX_PATH_PREFIX", raising=False)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"itemCount": 3, "pageCount": 1, "items": [
        {"name": "all_others", "ready": False, "readers": []},
        {"name": "other__ipcam-x", "ready": True, "readers": []},
        {"name": "ipcam-a", "ready": True, "readers": [
            {"type": "webRTCSession", "id": "r1"}, {"type": "rtspSession", "id": "r2"}]},
    ]}
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.get", return_value=resp) as get:
        assert mediamtx.list_path_status() == {"ipcam-a": PathStatus(True, {"r1", "r2"})}
    assert get.call_args.args[0] == "http://mtx:9997/v3/paths/list"


@pytest.mark.parametrize("failure", [MagicMock(status_code=401), mediamtx.httpx.ConnectError("down")])
def test_list_path_status_returns_none_without_logging(failure, caplog):
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.get", side_effect=[failure]), \
         caplog.at_level(logging.DEBUG, logger="rtsp-streaming"):
        assert mediamtx.list_path_status() is None
    assert caplog.records == []


def test_reconcile_corrects_hub_from_mediamtx_status(sessions, mtx_calls):
    """훅이 빠져도 주기마다 mediamtx 상태 목록으로 바로잡는다."""
    _add_cam(sessions, "ipcam-a")
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-a", None)  # notready 훅을 놓친 상태

    _run_cycles(sessions, [{"ipcam-a"}], hub, [{"ipcam-a": PathStatus(False, set())}])

    assert hub.snapshot() == {"ipcam-a": {"active": False, "readers": 0}}


def test_reconcile_skips_only_status_check_when_status_list_fails(sessions, mtx_calls):
    _add_cam(sessions, "ipcam-a")
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-b", None)

    _run_cycles(sessions, [set()], hub, [None])

    assert hub.snapshot() == {"ipcam-b": {"active": True, "readers": 0}}
    mtx_calls["register"].assert_called_once_with("ipcam-a", "rtsp://cam/1", None)


def test_reconcile_marks_all_inactive_only_when_connection_drops(sessions, mtx_calls):
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-a", None)
    reconciler = mediamtx.StreamReconciler(hub)

    with patch("app.mediamtx.list_config_paths", return_value=None), \
         patch("app.mediamtx.list_path_status", return_value=None):
        with sessions() as db:
            reconciler.run_once(db)
        assert hub.snapshot()["ipcam-a"]["active"] is False

        # 설정 조회만 실패하고 훅은 계속 오는 경우 — 끊긴 동안 다시 끄지 않는다.
        hub.apply_hook("ready", "ipcam-a", None)
        with sessions() as db:
            reconciler.run_once(db)

    assert hub.snapshot()["ipcam-a"]["active"] is True


def test_sync_loop_feeds_global_status_hub(sessions):
    stop = threading.Event()
    hub = StreamStatusHub()

    def statuses():
        stop.set()
        return {"ipcam-a": PathStatus(True, set())}

    with patch("app.stream_status.hub", hub), \
         patch("app.mediamtx.list_path_status", side_effect=statuses), \
         patch("app.mediamtx.list_config_paths", return_value=set()):
        mediamtx.run_sync_loop(stop, sessions, interval=0)

    assert hub.snapshot() == {"ipcam-a": {"active": True, "readers": 0}}


# ─── 조건부 transcode: h264=passthrough(copy) / 그 외=libx264 (CEO #97 화질 정본) ───


def test_transcode_h264_source_uses_passthrough_copy():
    """h264 소스 → -c:v copy 무손실 패스스루(재인코딩 0, 화질 보존). WebRTC 네이티브."""
    cmd = mediamtx._transcode_command("rtsp://cam/h264", "h264")
    assert "-c:v copy" in cmd
    assert "libx264" not in cmd
    assert "force_key_frames" not in cmd  # copy 라 인코더 옵션 없음
    assert "$RTSP_PORT" in cmd and "$MTX_PATH" in cmd  # mediamtx 변수는 literal 유지


def test_transcode_non_h264_source_uses_libx264():
    """H.265 등 비-h264 → libx264 재인코딩(WebRTC 호환) + 2초 IDR 강제."""
    cmd = mediamtx._transcode_command("rtsp://cam/h265", "hevc")
    assert "-c:v libx264" in cmd
    assert "force_key_frames" in cmd
    assert "-c:v copy" not in cmd


def test_transcode_probe_failure_falls_back_to_libx264():
    """코덱 모름(None: 판별 실패·마이그레이션 전 카메라) → 안전기본 libx264(현행 유지 = 롤백 안전)."""
    cmd = mediamtx._transcode_command("rtsp://cam/unknown", None)
    assert "-c:v libx264" in cmd


def test_register_and_update_use_given_codec_without_probing(_stub_probe_codec):
    """판별은 호출부가 한 번 하고 결과를 넘긴다 — 등록·갱신 함수는 ffprobe 를 돌리지 않는다."""
    with patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post", return_value=MagicMock(status_code=200)) as post, \
         patch("app.mediamtx.httpx.patch", return_value=MagicMock(status_code=200)) as patch_:
        mediamtx.register_stream("ipcam-x", "rtsp://cam/1", "h264")
        mediamtx.update_stream("ipcam-x", "rtsp://cam/2", "h264")

    assert "-c:v copy" in post.call_args.kwargs["json"]["runOnDemand"]
    assert "-c:v copy" in patch_.call_args.kwargs["json"]["runOnDemand"]
    _stub_probe_codec.assert_not_called()


def test_probe_codec_parses_ffprobe_codec_name():
    """ffprobe stdout 의 codec_name 을 소문자로 반환."""
    with patch("app.mediamtx.subprocess.run") as run:
        run.return_value = MagicMock(stdout="H264\n")
        assert _REAL_PROBE("rtsp://cam/1") == "h264"
        assert run.call_args.kwargs.get("timeout") == 4  # CEO #124 — 도달불가 카메라 등록 latency 단축(10→4s)


def test_probe_codec_returns_none_on_any_failure():
    """어떤 실패든 None — 카메라가 UTF-8 이 아닌 오류 문구를 보내 디코드에 실패해도 예외가 새지 않는다."""
    bad = UnicodeDecodeError("utf-8", b"\xb0\xa1", 0, 1, "invalid start byte")
    with patch("app.mediamtx.subprocess.run", side_effect=bad):
        assert _REAL_PROBE("rtsp://cam/1") is None


def test_probe_codec_returns_none_on_failure():
    """ffprobe 타임아웃/미설치 등 실패 → None(→ libx264 폴백)."""
    with patch("app.mediamtx.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 10)):
        assert _REAL_PROBE("rtsp://cam/1") is None


# ─── mediamtx API 인증 (CEO #100 — backend Basic auth) + 로그 마스킹 ───


def test_register_sends_backend_basic_auth_when_pass_set():
    """MEDIAMTX_BACKEND_PASS 설정 시 httpx 호출에 (user,pass) Basic auth 전달."""
    with patch.object(mediamtx, "MEDIAMTX_BACKEND_USER", "backend"), \
         patch.object(mediamtx, "MEDIAMTX_BACKEND_PASS", "s3cret"), \
         patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        mediamtx.register_stream("ipcam-x", "rtsp://cam/1")
        assert post.call_args.kwargs.get("auth") == ("backend", "s3cret")


def test_register_no_auth_when_pass_unset():
    """비번 미설정(빈 문자열)이면 auth=None — 무인증(로컬/테스트 하위호환)."""
    with patch.object(mediamtx, "MEDIAMTX_BACKEND_PASS", ""), \
         patch.object(mediamtx, "MEDIAMTX_API", "http://mtx:9997"), \
         patch("app.mediamtx.httpx.post") as post:
        post.return_value = MagicMock(status_code=200)
        mediamtx.register_stream("ipcam-x", "rtsp://cam/1")
        assert post.call_args.kwargs.get("auth") is None


def test_transcode_suppresses_ffmpeg_plaintext_url_log():
    """ffmpeg -hide_banner -loglevel error: stdout 의 rtsp 평문 URL 덤프 차단(#100.4)."""
    cmd = mediamtx._transcode_command("rtsp://admin:pw@cam/1", None)
    assert "-hide_banner" in cmd and "-loglevel error" in cmd

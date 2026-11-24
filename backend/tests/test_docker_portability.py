"""Docker 배포 계약 — 다른 PC(Linux·macOS·Windows)에서 clone 해 그대로 띄울 때 깨지지 않게.

본보기 = rtsp-detection 의 같은 이름 테스트. GPU overlay · YOLO 가중치 볼륨 검사는 streaming 에 없어 뺐다.
PyYAML 이 이 빌드 의존성에 없어 docker-compose.yml 은 서비스 블록을 들여쓰기로 잘라 글자로 본다.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _service(name: str) -> str:
    """docker-compose.yml 의 services.<name> 블록 본문 — 다음 서비스나 최상위 키 직전까지."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z]|^\S|\Z)", compose, re.M | re.S)
    assert match, f"docker-compose.yml 에 {name} 서비스가 없다"
    # 주석 줄은 뺀다 — '# image: …' 처럼 주석으로 남긴 옛 값을 진짜 설정으로 읽지 않게
    return "\n".join(line for line in match.group(1).splitlines() if not line.lstrip().startswith("#"))


def test_shell_entrypoint_is_normalized_and_invoked_through_posix_shell() -> None:
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY --chmod=0755 docker-entrypoint.sh /app/docker-entrypoint.sh" in dockerfile
    assert "replace(b'\\r\\n', b'\\n')" in dockerfile
    assert "replace(b'\\r', b'\\n')" in dockerfile
    assert "removeprefix(b'\\xef\\xbb\\xbf')" in dockerfile
    assert 'ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]' in dockerfile


def test_compose_uses_public_backend_and_builds_frontend_with_client_settings() -> None:
    backend, frontend = _service("backend"), _service("frontend")
    assert "image: ghcr.io/jskim1102/rtsp-streaming-backend:latest" in backend
    assert "pull_policy" not in backend
    assert "pull_policy: build" in frontend
    assert "http://127.0.0.1:8000/api/health" in backend
    assert "http://127.0.0.1/" in frontend
    assert re.search(r"depends_on:\s+backend:\s+condition: service_healthy", frontend)


def test_mediamtx_advertises_loopback_when_webrtc_host_is_unset() -> None:
    """MEDIAMTX_WEBRTC_HOST 를 비워 두면 같은 PC 브라우저가 게시된 ICE 포트(127.0.0.1)로 붙는다."""
    assert "MTX_WEBRTCADDITIONALHOSTS: ${MEDIAMTX_WEBRTC_HOST:-127.0.0.1}" in _service("mediamtx")


def test_every_service_caps_container_log_size() -> None:
    """로그 상한이 없으면 접근 로그가 디스크를 채워 DB 쓰기와 같은 서버의 다른 서비스까지 멈춘다."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    anchor = re.search(r"^x-logging: &default-logging\n((?:  .*\n)+)", compose, re.M)
    assert anchor, "docker-compose.yml 최상위에 x-logging 공통 로그 설정이 없다"
    settings = anchor.group(1)
    assert "driver: json-file" in settings
    assert 'max-size: "20m"' in settings
    assert 'max-file: "5"' in settings
    for name in ("backend", "frontend", "mediamtx"):
        assert "logging: *default-logging" in _service(name), f"{name} 서비스에 로그 상한이 없다"


@pytest.mark.parametrize(("hook", "event"), [
    ("runOnReady", "ready"), ("runOnNotReady", "notready"), ("runOnRead", "read"), ("runOnUnread", "unread"),
])
def test_mediamtx_hooks_notify_backend_of_path_status(hook: str, event: str) -> None:
    """상태가 바뀌면 mediamtx 가 백엔드 훅 알림 API 를 부른다 — 화면 SSE 푸시의 출처다."""
    config = (ROOT / "mediamtx.yml").read_text(encoding="utf-8")
    defaults = config.split("\npathDefaults:\n", 1)[1].split("\npaths:", 1)[0]

    command = (
        "wget -q -T 3 -O /dev/null --post-data= "
        f"http://backend:8000/internal/mediamtx/hook?event={event}&path=$MTX_PATH&reader=$MTX_READER_ID"
    )
    assert f"  {hook}: {command}\n" in defaults, f"pathDefaults 에 {hook} 훅이 없다"


def test_backend_stops_within_graceful_shutdown_timeout() -> None:
    """SSE 연결이 열려 있으면 uvicorn 이 종료·--reload 에서 멈춘다 — 8초 뒤 연결을 끊고 내려간다."""
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    dev = (ROOT / "dev.sh").read_text(encoding="utf-8")

    assert '"--timeout-graceful-shutdown", "8"' in dockerfile
    assert "--timeout-graceful-shutdown 8" in dev


def _mediamtx_permissions() -> list[list[str]]:
    """mediamtx.yml authInternalUsers 의 계정별 action 목록 — 순서대로 [0] 로컬, [1] backend, [2] viewer."""
    config = (ROOT / "mediamtx.yml").read_text(encoding="utf-8")
    block = config.split("\nauthInternalUsers:\n", 1)[1].split("\n\n", 1)[0]
    return [re.findall(r"- action: (\w+)", user) for user in block.split("\n  - user:")[1:]]


def test_mediamtx_accounts_keep_only_permissions_in_use() -> None:
    """쓰지 않는 권한은 두지 않는다 — 계정이 새도 할 수 있는 일을 줄인다.

    로컬 ffmpeg 는 송출만, backend 는 경로 등록 API 만, viewer 는 시청만 한다. 재생(playback) 서버는 꺼져 있다.
    """
    assert _mediamtx_permissions() == [["publish"], ["api"], ["read"]]


def test_compose_overrides_only_existing_viewer_permission_paths() -> None:
    """없는 권한 칸을 가리키면 mediamtx 가 action 이 빈 칸을 하나 더 만든다 (1.19.1 확인) — 칸 수만큼만 덮어쓴다."""
    indexes = re.findall(r"MTX_AUTHINTERNALUSERS_2_PERMISSIONS_(\d+)_PATH", _service("mediamtx"))

    assert indexes == [str(i) for i in range(len(_mediamtx_permissions()[2]))]


def test_git_forces_lf_for_shell_scripts() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "* text=auto eol=lf" in attributes
    assert "*.sh text eol=lf" in attributes


def test_git_keeps_crlf_for_windows_launcher() -> None:
    """cmd.exe 는 LF 줄끝 배치 파일을 잘못 읽을 수 있어 setup.cmd 만 CRLF 로 받는다."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "setup.cmd text eol=crlf" in attributes

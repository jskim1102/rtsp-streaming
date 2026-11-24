"""Docker 배포 계약 — 다른 PC(Linux·macOS·Windows)에서 clone 해 그대로 띄울 때 깨지지 않게.

본보기 = rtsp-detection 의 같은 이름 테스트. GPU overlay · YOLO 가중치 볼륨 검사는 streaming 에 없어 뺐다.
PyYAML 이 이 빌드 의존성에 없어 docker-compose.yml 은 서비스 블록을 들여쓰기로 잘라 글자로 본다.
"""

import re
from pathlib import Path

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


def test_git_forces_lf_for_shell_scripts() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "* text=auto eol=lf" in attributes
    assert "*.sh text eol=lf" in attributes


def test_git_keeps_crlf_for_windows_launcher() -> None:
    """cmd.exe 는 LF 줄끝 배치 파일을 잘못 읽을 수 있어 setup.cmd 만 CRLF 로 받는다."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "setup.cmd text eol=crlf" in attributes

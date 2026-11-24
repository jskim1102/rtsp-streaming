"""리버스 프록시 계약 — frontend nginx 가 단일 HTTPS 입구가 되는 구성 (체크리스트 1번).

PRD·TRD: harness/docs/{prd,trd}/2026-11-24-rtsp-streaming-reverse-proxy.md 의 R-n 을 따른다.
test_docker_portability.py 처럼 설정 파일을 글자로 읽는다 — 실제 동작은 compose 를 띄워 확인한다.
인증서 스크립트만은 이 PC 에서 경로를 바꿔 실제로 돌린다 — 주소 분류 규칙을 자동으로 확인한다.
컨테이너의 BusyBox 와 이 PC 도구의 차이는 compose 로 확인한다.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _service(name: str) -> str:
    """docker-compose.yml 의 services.<name> 블록 본문 — 주석 줄은 뺀다."""
    compose = _read("docker-compose.yml")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z]|^\S|\Z)", compose, re.M | re.S)
    assert match, f"docker-compose.yml 에 {name} 서비스가 없다"
    return "\n".join(line for line in match.group(1).splitlines() if not line.lstrip().startswith("#"))


def _template() -> str:
    """nginx 설정 템플릿 — 주석 줄은 뺀다 (주석 속 예시를 설정으로 읽지 않게)."""
    text = (FRONTEND / "nginx.conf.template").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _location(pattern: str) -> str:
    """템플릿에서 `location <pattern> {` 블록 본문 — 중첩 블록이 없다는 전제다."""
    match = re.search(rf"location {re.escape(pattern)} \{{(.*?)\n    \}}", _template(), re.S)
    assert match, f"nginx 템플릿에 location {pattern} 이 없다"
    return match.group(1)


# ─── 조각 A: HTTPS 입구 + API (R-1, R-2, R-3, R-5, R-6, R-11, R-12, R-16) ───


def test_backend_port_is_not_published() -> None:
    """백엔드 포트를 공개하면 훅 알림 주소에 외부에서 가짜 훅을 보낼 수 있다 (R-5)."""
    assert "ports:" not in _service("backend")


def test_frontend_is_the_https_entrance_on_frontend_port() -> None:
    frontend = _service("frontend")

    assert '"${FRONTEND_PORT:?set FRONTEND_PORT in .env}:443"' in frontend
    assert re.search(r"networks:\s+- rtsp-streaming", frontend), "frontend 가 backend·mediamtx 내부망에 없다"
    assert "./certs:/etc/nginx/certs" in frontend
    assert '"curl", "-fsk", "-o", "/dev/null", "https://127.0.0.1/"' in frontend


def test_frontend_receives_proxy_settings_at_runtime() -> None:
    """viewer 자격증명·접두사는 실행할 때 넣는다 — 이미지와 번들에 남기지 않는다 (R-6, R-7)."""
    frontend = _service("frontend")

    assert "MEDIAMTX_PATH_PREFIX: ${MEDIAMTX_PATH_PREFIX:-rtsp_streaming}" in frontend
    assert "MEDIAMTX_VIEWER_USER: ${MEDIAMTX_VIEWER_USER:-viewer_rtsp_streaming}" in frontend
    assert "MEDIAMTX_VIEWER_PASS: ${MEDIAMTX_VIEWER_PASS:?set MEDIAMTX_VIEWER_PASS in .env}" in frontend
    assert "MEDIAMTX_WEBRTC_HOST: ${MEDIAMTX_WEBRTC_HOST:-}" in frontend


def test_nginx_serves_https_with_http2_and_redirects_plain_http() -> None:
    """같은 포트로 온 HTTP 는 브라우저가 접속한 호스트·포트 그대로 HTTPS 로 넘긴다 (R-2, R-11)."""
    template = _template()

    assert "listen 443 ssl;" in template
    assert "http2 on;" in template
    assert "error_page 497 =301 https://$http_host$request_uri;" in template
    assert "ssl_certificate     /etc/nginx/active/tls.crt;" in template
    assert "ssl_certificate_key /etc/nginx/active/tls.key;" in template


def test_api_is_proxied_unchanged_through_re_resolved_upstream() -> None:
    """URI 를 붙이지 않아 원래 경로 그대로 넘긴다 — 백엔드가 다시 만들어져도 이름을 다시 찾는다 (R-5)."""
    template = _template()

    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in template
    assert "server backend:8000 resolve;" in template
    assert "proxy_pass http://rtsp_backend;" in _location("/api/")


def test_internal_paths_are_blocked_before_any_other_location() -> None:
    """훅 알림 주소(/internal)는 어떤 표기로도 넘기지 않는다 — 정규식 location 중 맨 앞이다 (R-5)."""
    template = _template()
    regexes = re.findall(r"location (~\*?) (\S+) \{", template)

    assert regexes[0] == ("~*", "^/internal(/|$)"), f"첫 정규식 location 이 /internal 차단이 아니다: {regexes}"
    assert "return 404;" in _location("~* ^/internal(/|$)")


def test_sse_is_not_buffered_and_survives_keepalive_gaps() -> None:
    """SSE 는 버퍼링하지 않고, 15초 keep-alive 사이에 프록시가 끊지 않는다 (R-12)."""
    events = _location("= /api/ipcams/events")

    assert "proxy_pass http://rtsp_backend;" in events
    assert "proxy_buffering off;" in events
    assert "proxy_read_timeout 1h;" in events


def test_frontend_image_pins_nginx_and_prepares_startup_scripts() -> None:
    dockerfile = (FRONTEND / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM nginx:1.30.8-alpine" in dockerfile
    assert "RUN apk add --no-cache openssl" in dockerfile
    assert "RUN rm /etc/nginx/conf.d/default.conf" in dockerfile
    assert "COPY nginx.conf.template /etc/nginx/templates/rtsp-streaming.conf.template" in dockerfile
    assert "COPY --chmod=0755 docker-entrypoint.d/ /docker-entrypoint.d/" in dockerfile
    # 윈도우 checkout 대비 — BusyBox sed 는 \xEF 표기를 몰라 BOM 바이트를 printf 로 넣는다.
    assert "printf '\\357\\273\\277'" in dockerfile
    assert "EXPOSE 443" in dockerfile


def test_env_script_exports_values_for_envsubst() -> None:
    """envsubst 는 자식 프로세스라 export 한 값만 본다. base64 줄바꿈은 헤더를 깨뜨린다."""
    script = (FRONTEND / "docker-entrypoint.d" / "05-rtsp-streaming-env.envsh").read_text(encoding="utf-8")

    assert "export WHEP_PATH_PREFIX VIEWER_AUTHORIZATION" in script
    assert "base64 -w0" in script


def test_cert_script_prefers_operator_files_and_links_active_cert() -> None:
    script = (FRONTEND / "docker-entrypoint.d" / "40-rtsp-streaming-cert.sh").read_text(encoding="utf-8")

    assert "server.crt" in script and "server.key" in script
    assert "ACTIVE=/etc/nginx/active" in script
    assert '"$ACTIVE/tls.crt"' in script and '"$ACTIVE/tls.key"' in script


def test_certs_folder_exists_in_checkout_but_its_files_are_ignored() -> None:
    """폴더가 없으면 Docker 가 root 소유로 만들어 운영자가 인증서를 넣을 수 없다 (R-3)."""
    gitignore = _read(".gitignore")

    assert (ROOT / "certs" / ".gitkeep").exists()
    assert "certs/*" in gitignore
    assert "!certs/.gitkeep" in gitignore


def test_screen_calls_api_with_same_origin_relative_paths() -> None:
    """화면은 포트를 붙인 절대 주소 없이 같은 출처로 부른다 (R-6)."""
    page = (FRONTEND / "src" / "pages" / "CamerasPage.tsx").read_text(encoding="utf-8")

    assert "apiBase" not in page
    assert "fetch(`/api/ipcams`)" in page
    assert "new EventSource(`/api/ipcams/events`)" in page


# ─── 조각 B: WHEP 경유 (R-6, R-7, R-8) ───


def test_webrtc_signaling_port_is_published_to_this_pc_only() -> None:
    """화면은 프록시로 WHEP 를 보낸다. 호스트 공개는 dev.sh 의 개발 서버용이다 (R-8)."""
    mediamtx = _service("mediamtx")

    assert '"127.0.0.1:${MEDIAMTX_WEBRTC_PORT:?set MEDIAMTX_WEBRTC_PORT in .env}:8889/tcp"' in mediamtx
    assert '"${MEDIAMTX_RTSP_PORT:?set MEDIAMTX_RTSP_PORT in .env}:8554"' in mediamtx  # PRD 비목표: 그대로 공개
    # 영상(ICE)은 브라우저가 MediaMTX 로 바로 받는다 — 프록시를 거치지 않아 그대로 연다.
    ice = "${MEDIAMTX_ICE_UDP_PORT:?set MEDIAMTX_ICE_UDP_PORT in .env}"
    assert f'"{ice}:{ice}/udp"' in mediamtx
    assert f'"{ice}:{ice}/tcp"' in mediamtx


def test_frontend_build_takes_no_ports_or_credentials() -> None:
    """빌드에 넣은 값은 화면 번들에 그대로 남는다 (R-6, C-4)."""
    assert "VITE_" not in _service("frontend")
    assert "ARG VITE_" not in (FRONTEND / "Dockerfile").read_text(encoding="utf-8")


def test_whep_is_proxied_with_viewer_credentials_for_module_paths_only() -> None:
    """viewer 자격증명은 프록시가 붙이고, 이 모듈 형식의 경로만 MediaMTX 로 넘긴다 (R-7)."""
    whep = _location("~ ^/${WHEP_PATH_PREFIX}[0-9a-f]+/whep(/|$)")

    assert "server mediamtx:8889 resolve;" in _template()
    assert "proxy_pass http://rtsp_mediamtx;" in whep
    assert 'proxy_set_header Authorization "${VIEWER_AUTHORIZATION}";' in whep
    assert "proxy_hide_header WWW-Authenticate;" in whep


def test_other_whep_paths_get_404_after_the_module_whep_location() -> None:
    """형식이 다른 /…/whep 이 SPA fallback(200 index.html)으로 떨어지지 않게 한다 — 정규식은 적힌 순서로 맞춘다."""
    regexes = [pattern for _, pattern in re.findall(r"location (~\*?) (\S+) \{", _template())]

    assert regexes[:3] == ["^/internal(/|$)", "^/${WHEP_PATH_PREFIX}[0-9a-f]+/whep(/|$)", "^/[^/]+/whep(/|$)"]
    assert "return 404;" in _location("~ ^/[^/]+/whep(/|$)")


def test_screen_never_handles_viewer_credentials() -> None:
    """화면 코드에는 포트·자격증명 빌드 변수와 Basic 인코딩이 없다 (R-6)."""
    sources = {p: p.read_text(encoding="utf-8") for p in (FRONTEND / "src").rglob("*.ts*")}

    assert not (FRONTEND / "src" / "hooks" / "useApi.ts").exists()
    for path, text in sources.items():
        assert "VITE_" not in text, f"{path.name} 에 VITE_ 빌드 변수가 남아 있다"
        assert "btoa(" not in text, f"{path.name} 에 Basic 인코딩이 남아 있다"
        assert "useApi" not in text, f"{path.name} 가 지운 useApi 를 가져온다"
    whep = sources[FRONTEND / "src" / "components" / "WhepPlayer.tsx"]
    assert "fetch(`/${streamKey}/whep`" in whep
    assert "Authorization" not in whep


# ─── 조각 C: 인증서 수명 (R-3, R-4) ───


def _cert_script() -> str:
    return (FRONTEND / "docker-entrypoint.d" / "40-rtsp-streaming-cert.sh").read_text(encoding="utf-8")


def test_self_signed_cert_meets_apple_tls_requirements() -> None:
    """iOS 13·macOS 10.15 이후는 825일 이하 + serverAuth 를 요구한다 (R-4, 2026-11-24 사용자 결정)."""
    script = _cert_script()

    assert "-days 825" in script
    assert "extendedKeyUsage=serverAuth" in script
    assert "rsa:2048" in script


def test_self_signed_cert_is_reused_unless_hosts_change_or_expiry_nears() -> None:
    """자동 생성분은 보존하고, 넣을 주소가 바뀌거나 만료 30일 전일 때만 다시 만든다 (R-4)."""
    script = _cert_script()

    assert 'cat "$CERTS/selfsigned.sans"' in script, "만들 때의 주소 목록과 비교하지 않는다"
    assert "-checkend 2592000" in script, "만료 30일 전 재생성 검사가 없다"


def test_half_present_operator_cert_falls_back_with_warning() -> None:
    """server.crt·server.key 중 하나만 있으면 경고를 남기고 자체 서명 인증서를 쓴다 (R-3)."""
    assert "중 하나만 있음" in _cert_script()


def _run_cert_script(tmp_path: Path, webrtc_host: str, path_prefix: str = "") -> subprocess.CompletedProcess:
    """인증서 스크립트를 /etc/nginx 대신 tmp_path 아래에서 실행한다 — 이 PC 의 openssl 을 쓴다."""
    script = tmp_path / "cert.sh"
    script.write_text(_cert_script().replace("/etc/nginx/", f"{tmp_path}/"), encoding="utf-8")
    (tmp_path / "certs").mkdir()
    env = {**os.environ, "MEDIAMTX_WEBRTC_HOST": webrtc_host, "PATH": path_prefix + os.environ["PATH"]}
    return subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True, timeout=60)


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl 이 없다")


@needs_openssl
def test_cert_leaves_out_invalid_hosts_with_warning(tmp_path: Path) -> None:
    """setup 에서 주소를 잘못 넣어도 화면은 뜬다 — 잘못된 주소만 인증서에서 빼고 로그에 남긴다 (R-3)."""
    bad = ["192.168.0.300", "192.168.0.10:8443", "fd00::1::2", "cafe:8443", "1:2:3"]
    hosts = ", ".join(["192.168.0.10", "cam.example.com", "my_host", "fd00::1", "::1", *bad])

    result = _run_cert_script(tmp_path, hosts)

    assert result.returncode == 0, result.stderr
    san = subprocess.run(
        ["openssl", "x509", "-in", str(tmp_path / "certs" / "selfsigned.crt"), "-noout", "-ext", "subjectAltName"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "IP Address:192.168.0.10" in san
    assert "DNS:cam.example.com" in san
    assert "DNS:my_host" in san  # 예전에 받던 이름은 그대로 받는다 — 업데이트 뒤 '주소 바뀜' 재생성이 없게
    assert "IP Address:FD00:" in san
    assert "IP Address:0:0:0:0:0:0:0:1" in san
    assert "300" not in san and "8443" not in san and "0:2:" not in san
    for host in bad:
        assert f"뺌: {host} " in result.stderr, f"{host} 를 뺐다는 경고가 없다"


def test_cert_generation_failure_reason_is_logged(tmp_path: Path) -> None:
    """openssl 이 실패하면 그 이유를 로그에 남긴다 — 화면이 왜 안 뜨는지 로그로 알 수 있다."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_openssl = fake_bin / "openssl"
    fake_openssl.write_text("#!/bin/sh\necho 'openssl 이 거부한 사유' >&2\nexit 1\n", encoding="utf-8")
    fake_openssl.chmod(0o755)

    result = _run_cert_script(tmp_path, "", path_prefix=f"{fake_bin}{os.pathsep}")

    assert result.returncode != 0
    assert "자체 서명 인증서를 만들지 못함" in result.stderr
    assert "openssl 이 거부한 사유" in result.stderr


# ─── 조각 D: 요청 제한·본문 상한·보안 헤더 (R-9, R-17, R-18) ───


def test_rate_limits_are_per_client_address_with_immediate_429() -> None:
    """IP 는 프록시가 보는 접속 주소다 — 전달 헤더는 믿지 않는다. 순간 한도를 넘으면 기다리지 않고 429 (R-9)."""
    template = _template()

    assert "limit_req_zone $binary_remote_addr zone=api:10m rate=50r/s;" in template
    assert "limit_req_zone $binary_remote_addr zone=whep:10m rate=100r/s;" in template
    assert "limit_conn_zone $binary_remote_addr zone=perip:10m;" in template
    assert "limit_conn perip 250;" in template
    assert "limit_req_status 429;" in template
    assert "limit_conn_status 429;" in template
    assert "X-Forwarded-For" not in template


def test_api_and_whep_locations_apply_their_limits() -> None:
    for location in ("/api/", "= /api/ipcams/events"):
        block = _location(location)
        assert "limit_req zone=api burst=250 nodelay;" in block, f"{location} 에 API 한도가 없다"
        assert "error_page 429 @api_429;" in block, f"{location} 의 429 가 JSON 이 아니다"
    assert "limit_req zone=whep burst=500 nodelay;" in _location("~ ^/${WHEP_PATH_PREFIX}[0-9a-f]+/whep(/|$)")


def test_api_429_body_is_json_detail_like_other_api_errors() -> None:
    """화면의 errorDetail 이 다른 API 오류처럼 문구를 보여 준다."""
    block = _location("@api_429")

    assert 'default_type "application/json; charset=utf-8";' in block
    assert '"detail":' in block and "return 429" in block


def test_request_body_is_capped_and_security_headers_reach_every_location() -> None:
    """add_header 가 있는 하위 location 도 merge 로 보안 헤더를 물려받는다 (R-17, R-18)."""
    template = _template()

    assert "client_max_body_size 64k;" in template
    assert "add_header_inherit merge;" in template
    assert "add_header X-Content-Type-Options nosniff always;" in template
    assert "add_header X-Frame-Options DENY always;" in template


def test_access_log_records_timing_protocol_and_connection() -> None:
    """요청 시간·프로토콜·연결 번호 — 탭 여러 개가 HTTP/2 연결 하나를 나눠 쓰는지 확인할 때 쓴다."""
    template = _template()

    assert re.search(r"log_format rtsp_streaming .*\$request_time \$server_protocol \$connection", template, re.S)
    assert "access_log /var/log/nginx/access.log rtsp_streaming;" in template


# ─── 조각 E: CORS (R-13) ───


def test_backend_env_example_leaves_cors_closed_by_default() -> None:
    """예시 파일을 복사한 개발 환경에서 * 가 다시 켜지지 않게 한다."""
    assert re.search(r"^CORS_ORIGINS=$", _read("backend/.env.example"), re.M)


# ─── 조각 F: 개발 모드 (R-10) ───


def test_vite_dev_server_proxies_api_and_whep_without_exposing_credentials() -> None:
    """개발 서버가 같은 상대 경로를 넘기고 WHEP 에 viewer 자격증명을 붙인다 — 화면 코드에는 드러나지 않는다.

    process.env 는 쓰지 않는다 — @types/node 가 없어 tsc -b(= npm run build 첫 단계)가 실패한다.
    """
    text = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    # 주석 줄은 뺀다 — 주석의 설명 문구("process.env 대신")를 코드로 읽지 않게.
    config = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))

    assert 'loadEnv(mode, ".", "")' in config
    assert "process." not in config and "Buffer" not in config and "btoa(" not in config
    assert '"/api":' in config
    assert "__ipcam-" in config and "/whep" in config
    assert "auth:" in config
    for name in ("BACKEND_PORT", "MEDIAMTX_WEBRTC_PORT", "MEDIAMTX_PATH_PREFIX", "MEDIAMTX_VIEWER_USER", "MEDIAMTX_VIEWER_PASS"):
        assert name in config, f"vite.config.ts 가 {name} 을 읽지 않는다"


def test_dev_sh_passes_proxy_settings_to_vite_without_vite_prefix() -> None:
    """VITE_ 이름은 화면 코드에 드러나므로 쓰지 않는다. 접두사 기본값은 backend 명령과 같다."""
    frontend_cmd = next(line for line in _read("dev.sh").splitlines() if line.strip().startswith('"frontend|'))

    assert "VITE_" not in frontend_cmd
    assert "BACKEND_PORT='${BACKEND_PORT}'" in frontend_cmd
    assert "MEDIAMTX_WEBRTC_PORT='${MEDIAMTX_WEBRTC_PORT}'" in frontend_cmd
    assert "MEDIAMTX_PATH_PREFIX='${MEDIAMTX_PATH_PREFIX:-rtsp_streaming}'" in frontend_cmd
    assert "MEDIAMTX_VIEWER_USER='${MEDIAMTX_VIEWER_USER:-viewer_rtsp_streaming}'" in frontend_cmd
    assert "MEDIAMTX_VIEWER_PASS='${MEDIAMTX_VIEWER_PASS:-}'" in frontend_cmd


# ─── 조각 G: 안내 (R-14) ───


def test_setup_scripts_point_to_https_entrance() -> None:
    """setup.cmd 는 setup.ps1 을 부르기만 하므로 두 파일만 본다."""
    assert "https://localhost:%s" in _read("setup.sh")
    assert "https://localhost:$port" in _read("setup.ps1")
    assert "http://localhost" not in _read("setup.sh") + _read("setup.ps1")

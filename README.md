# rtsp-streaming

![rtsp-streaming 대시보드 — 카메라 CRUD + NxN 실시간 그리드](docs/dashboard.png)

# 설치 · 실행 (setup 스크립트)

```bash
git clone https://github.com/jskim1102/rtsp-streaming rtsp-streaming
cd rtsp-streaming
```

| 운영체제 | 실행 |
|---|---|
| Linux · macOS | `./setup.sh` |
| Windows | `setup.cmd` 더블클릭 |

접속 : `https://localhost:<FRONTEND_PORT>`

* 처음 접속하면 브라우저가 인증서 경고를 띄움 — 아래 "HTTPS 인증서" 참고.

---

* RTSP 카메라를 등록·수정·삭제하고, MediaMTX가 모든 코덱(H.264/H.265/MJPEG 등)을 H.264로 transcode해 브라우저에 WebRTC(WHEP)로 실시간 스트리밍하는 자립형 모듈.
* NxN 자동 그리드(1줄 최대 4칸) 동시뷰 포함.
* 백엔드(FastAPI)는 카메라 CRUD + MediaMTX 제어만 담당, 영상 디코딩은 안 거쳐 가벼운 부하.
* `docker compose` 한 번으로 **backend + frontend + MediaMTX 세 컨테이너** 동시 실행.
* 별도 인프라나 외부 스트리밍 서버 불필요.

# 환경변수 (`.env`)

* setup 이 `.env.example` 을 복사해 `.env` 생성, 포트·비밀번호 자동 입력.
* 값을 바꿀 때만 직접 수정 후 setup 다시 실행 (바뀐 값으로 컨테이너를 다시 띄움).
* 비밀번호가 들어가므로 이 파일은 공유 금지.

| 변수 | 값 | 설명 |
|---|---|---|
| `FRONTEND_PORT` | 자동 | 웹페이지·API·영상 연결의 단일 입구 (HTTPS, 브라우저 접속 주소) |
| `BACKEND_PORT` | 자동 | 백엔드 API 포트 (`./dev.sh` 개발 모드 전용, 외부에 안 열림) |
| `MEDIAMTX_WEBRTC_PORT` | 자동 | MediaMTX 영상 연결 포트 (이 PC 전용, 브라우저는 frontend 를 거침) |
| `MEDIAMTX_ICE_UDP_PORT` | 자동 | MediaMTX 영상 데이터 포트 |
| `MEDIAMTX_RTSP_PORT` | 자동 | MediaMTX RTSP 재송출 포트 |
| `MEDIAMTX_API_PORT` | 자동 | MediaMTX 제어 포트 |
| `MEDIAMTX_BACKEND_PASS` | 자동&nbsp;(무작위) | MediaMTX 제어용 비밀번호 |
| `MEDIAMTX_VIEWER_PASS` | 자동&nbsp;(무작위) | 영상 시청용 비밀번호 |
| `MEDIAMTX_WEBRTC_HOST` | setup&nbsp;이&nbsp;질문 | 브라우저가 이 PC 에 닿는 주소 전부 (쉼표 구분) |
| `MAX_IPCAMS` | 선택&nbsp;(직접&nbsp;추가) | 등록 가능 카메라 수 (기본 16) |
| `CORS_ORIGINS` | 선택&nbsp;(직접&nbsp;추가) | 허용할 다른 출처 (쉼표 구분, 보통 불필요) |

* MediaMTX 포트 4개는 아래 "포트 · 방화벽" 참고.
* 나머지 값(`MEDIAMTX_API`, `MEDIAMTX_PATH_PREFIX`, 사용자명)은 기본값 그대로 유지.
* `MEDIAMTX_WEBRTC_HOST`
  * 예: `192.168.0.20,100.64.0.10,203.0.113.5` (LAN · VPN · 공인 IP).
  * setup 질문에 여러 주소를 쉼표로 넣거나, 나중에 `.env` 에 직접 추가 (아래 "외부망 · VPN 접속" 참고).
  * **비우면 이 PC에서만 영상 표시.**
  * **목록에 없는 주소로 접속하면 화면은 열려도 영상은 검은 화면.**
  * LAN IP 찾는 법: `hostname -I` (Linux) · `ipconfig getifaddr en0` (macOS) · `ipconfig` (Windows).
* `CORS_ORIGINS`
  * 화면과 API 가 같은 주소(frontend)라 보통 비워 둠.
  * 비우면 다른 출처 허용 안 함.
  * 다른 주소의 화면에서 API 를 부를 때만 그 주소 지정 (예: `https://192.168.0.20:<FRONTEND_PORT>`).

> **비밀번호를 직접 바꿀 때 문자 제한**
> * MediaMTX가 비밀번호에 `%`, `?`, `/`, `:`, `,`, 공백, 따옴표 등 사용 불가.
> * 영문·숫자와 `!@#-_.` 정도로만 지정.
> * 허용 안 되는 문자를 쓰면 MediaMTX 컨테이너 재시작 반복.

# 포트 · 방화벽

* 실제 포트 번호는 `.env` 에 기록 (`grep _PORT .env` 로 확인).

| 서비스 | `.env` 변수 | 용도 | 개방 |
|---|---|---|---|
| Frontend | `FRONTEND_PORT` (tcp) | 웹페이지·API·영상&nbsp;연결 (HTTPS) | 필요 |
| Backend | `BACKEND_PORT` (tcp) | API (개발&nbsp;모드&nbsp;전용) | 불필요 (안&nbsp;열림) |
| MediaMTX&nbsp;WebRTC | `MEDIAMTX_WEBRTC_PORT` (tcp) | 영상&nbsp;연결&nbsp;시작 (frontend&nbsp;가&nbsp;넘김) | 불필요 (이&nbsp;PC&nbsp;전용) |
| MediaMTX&nbsp;ICE | `MEDIAMTX_ICE_UDP_PORT` (udp·tcp) | **영상&nbsp;데이터** | 필요 |
| MediaMTX&nbsp;RTSP | `MEDIAMTX_RTSP_PORT` (tcp) | RTSP&nbsp;재송출 | 불필요 |
| MediaMTX&nbsp;API | `MEDIAMTX_API_PORT` (tcp) | backend&nbsp;제어용 | 불필요 |

* 개방 = 다른 기기에서 접속할 때 방화벽 개방 필요 여부.
* 브라우저는 frontend(HTTPS) 하나로 화면·API·영상 연결을 모두 씀 — Backend·MediaMTX WebRTC 포트는 외부에 안 열림.
* ICE 포트는 필수 — 없으면 영상 안 나옴.
* ICE 의 같은 번호 tcp 는 UDP 가 막힌 망용 예비.
* RTSP 재송출은 브라우저 미사용.
* MediaMTX API 와 WebRTC 는 이 PC(127.0.0.1)에만 열림 (WebRTC 는 `./dev.sh` 개발 서버용).
* 다른 기기에서 접속하려면 방화벽/공유기에서 위 "필요" 2개 개방 (ICE 는 udp·tcp 둘 다).
* Linux(ufw) 예 — `< >` 자리에 `.env` 의 실제 번호:

```bash
sudo ufw allow <FRONTEND_PORT>/tcp           # frontend (HTTPS 단일 입구)
sudo ufw allow <MEDIAMTX_ICE_UDP_PORT>/udp   # MediaMTX ICE (영상 데이터 — 필수)
sudo ufw allow <MEDIAMTX_ICE_UDP_PORT>/tcp   # MediaMTX ICE (UDP 막힌 망용 예비)
```

> * ICE udp 포트를 빠뜨리면 카메라 화면이 "연결됨"까지 갔다가 영상만 표시되지 않는, 원인 찾기 어려운 증상 발생.
> * 반드시 개방.

## 외부망 · VPN 접속

* 같은 LAN 밖(회사 밖, 휴대폰 데이터, VPN)에서 접속하는 경우.
* 포트 개방만으로는 부족 — 영상 주소 목록(`MEDIAMTX_WEBRTC_HOST`)에 그 경로의 IP 추가 필요.

1. **공인 IP 로 접속 (공유기 포트포워딩)**
   * 공유기에서 위 "필요" 2개 포워딩: `FRONTEND_PORT` (tcp), `MEDIAMTX_ICE_UDP_PORT` (udp + tcp).
   * **ICE 포트는 바깥 번호와 안쪽 번호를 같게** (예: 공인 `18003` → 이 PC `18003`).
   * MediaMTX 가 안쪽 번호를 그대로 알려 주므로 번호가 다르면 영상 연결 실패.
   * 공인 IP 를 `MEDIAMTX_WEBRTC_HOST` 에 추가.
2. **VPN 으로 접속 (tailscale 등)**
   * 이 PC 의 VPN IP (예: tailscale `tailscale ip -4`) 를 `MEDIAMTX_WEBRTC_HOST` 에 추가.
   * 포트포워딩 불필요.
3. `.env` 를 바꾼 뒤 setup 다시 실행.

* 예: LAN + tailscale + 공인 IP 모두 쓰는 경우 — `MEDIAMTX_WEBRTC_HOST=192.168.0.20,100.64.0.10,203.0.113.5`

# HTTPS 인증서

* 같은 포트로 `http://` 로 들어오면 `https://` 로 넘어감.
* 기본은 자체 서명 인증서 — frontend 가 처음 뜰 때 `certs/` 에 자동 생성.
  * 기기마다 처음 접속할 때 브라우저 경고를 한 번 넘겨야 함.
  * 인증서에 `localhost`, `127.0.0.1`, `MEDIAMTX_WEBRTC_HOST` 의 주소가 들어감.
  * 유효기간 825일. `MEDIAMTX_WEBRTC_HOST` 가 바뀌었거나 만료 30일 전이면 frontend 가 다시 뜰 때 새로 만듦 (그때 경고를 한 번 더 넘김).
* 정식 인증서가 있으면 `certs/server.crt` 와 `certs/server.key` 를 넣고 `docker compose restart frontend`.
  * 둘 중 하나만 있으면 자체 서명 인증서로 돌아가고 `docker compose logs frontend` 에 경고가 남음.

# 주의

* **로그인 없음**
  * 접속 주소(`FRONTEND_PORT`)에 닿는 누구나 카메라 등록·수정·삭제·시청 가능.
  * 사내망·VPN 처럼 믿을 수 있는 망에 둘 것.
  * frontend 가 단일 입구 — 백엔드 포트는 외부에 안 열리고, 요청 횟수·동시 연결 수를 IP 기준으로 제한.
  * rtsp 비밀번호는 응답에서 `***`로 마스킹.
* 카메라 등록은 MediaMTX 등록이 성공해야 저장.
* MediaMTX가 준비되기 전 잠깐은 등록이 실패할 수 있으나 곧 정상화.

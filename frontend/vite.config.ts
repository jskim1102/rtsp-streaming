import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// 개발 서버(dev.sh)가 화면의 상대 경로를 백엔드·MediaMTX 로 넘긴다 — 배포의 frontend 프록시와 같은 경로다.
// - 값은 dev.sh 가 넘기는 환경변수에서 읽는다. VITE_ 이름이 아니라 화면 코드에는 드러나지 않는다.
// - process.env 대신 loadEnv 를 쓴다 — @types/node 가 없어 process 를 쓰면 tsc -b(npm run build 첫 단계)가 실패한다.
// - vite build 는 server 설정을 쓰지 않아 값이 비어 있어도 빌드된다.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  // WHEP 경로 앞부분 — backend/app/models.py 의 stream_key_prefix() 와 같은 규칙. 정규식 특수문자는 이스케이프한다.
  const streamKeyPrefix = env.MEDIAMTX_PATH_PREFIX ? `${env.MEDIAMTX_PATH_PREFIX}__ipcam-` : "ipcam-";
  const whepPath = `^/${streamKeyPrefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[0-9a-f]+/whep`;

  return {
    plugins: [react()],
    server: {
      host: true, // 0.0.0.0 — 전 인터페이스 (§5.3)
      allowedHosts: true, // 외부 도메인/IP/터널 Host 헤더 허용
      strictPort: true, // 포트 점유 시 자동 +1 점프 금지 — 할당 포트 침범 방지 (RULES §4)
      proxy: {
        // SSE(/api/ipcams/events)도 버퍼링 없이 넘긴다.
        "/api": { target: `http://127.0.0.1:${env.BACKEND_PORT}` },
        // viewer 자격증명은 개발 서버가 붙인다 (auth → Basic 헤더). 화면은 자격증명 없이 보낸다.
        [whepPath]: {
          target: `http://127.0.0.1:${env.MEDIAMTX_WEBRTC_PORT}`,
          auth: `${env.MEDIAMTX_VIEWER_USER}:${env.MEDIAMTX_VIEWER_PASS}`,
        },
      },
    },
  };
});

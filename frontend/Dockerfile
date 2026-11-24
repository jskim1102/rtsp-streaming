FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# 화면은 API·WHEP 를 같은 출처 상대 주소로 부른다 — 빌드에 포트·viewer 자격증명을 넣지 않는다.
RUN npm run build

# 프록시 겸 정적 서버 — 화면·API·WHEP 신호의 단일 HTTPS 입구 (체크리스트 1번). 버전을 고정한다.
FROM nginx:1.30.8-alpine
# 자체 서명 인증서를 컨테이너 안에서 만든다 — 호스트에는 openssl 을 요구하지 않는다.
RUN apk add --no-cache openssl
# 공식 기본 설정 대신 템플릿 하나만 쓴다 — 기본 설정을 고치는 10-listen-on-ipv6 스크립트도 이 파일이 없으면 건너뛴다.
RUN rm /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
# 템플릿은 실행할 때 envsubst 가 conf.d 로 채워 쓴다 — viewer 비밀번호를 이미지에 남기지 않는다.
COPY nginx.conf.template /etc/nginx/templates/rtsp-streaming.conf.template
COPY --chmod=0755 docker-entrypoint.d/ /docker-entrypoint.d/
# Git 설정을 무시한 Windows checkout 도 복구한다 — UTF-8 BOM 과 CR 을 지운다.
# BusyBox sed 는 \xEF 표기를 몰라 BOM 바이트를 printf 로 넣는다. tr 은 한글 UTF-8 바이트까지 지워 쓰지 않는다.
RUN for f in /docker-entrypoint.d/*rtsp-streaming*; do sed -i "1s/^$(printf '\357\273\277')//; s/\r\$//" "$f"; done
EXPOSE 443
CMD ["nginx", "-g", "daemon off;"]

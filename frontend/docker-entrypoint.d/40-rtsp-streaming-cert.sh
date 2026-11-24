#!/bin/sh
# HTTPS 인증서를 고른다 — 운영자 인증서가 있으면 그것, 없으면 자체 서명 인증서를 쓴다.
# - 고른 인증서를 /etc/nginx/active/tls.{crt,key} 로 연결한다. nginx 설정은 이 두 경로만 본다.
# - 링크는 컨테이너 안에만 만들고, 호스트와 나누는 /etc/nginx/certs 에는 일반 파일만 쓴다.
set -eu

CERTS=/etc/nginx/certs
ACTIVE=/etc/nginx/active

# 자체 서명 인증서에 넣을 주소 — localhost, 127.0.0.1, 그리고 MEDIAMTX_WEBRTC_HOST 중 형식에 맞는 주소.
sans="DNS:localhost,IP:127.0.0.1"
old_ifs=$IFS
IFS=,
for host in ${MEDIAMTX_WEBRTC_HOST:-}; do
    # 앞뒤 공백을 지우고 빈 항목은 건너뛴다.
    host=$(printf '%s' "$host" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -n "$host" ] || continue
    # IPv4·IPv6 는 IP 항목, 호스트 이름은 DNS 항목이다.
    # 그 밖(192.168.0.300, 포트를 붙인 주소, '::' 를 두 번 쓴 IPv6 등)은 인증서에서 뺀다 — 넣으면 openssl 이 실패해 화면이 뜨지 않는다.
    # IPv4: 네 자리, 자리마다 0~255.
    if printf '%s' "$host" | awk -F. 'NF != 4 { exit 1 } { for (i = 1; i <= 4; i++) if ($i !~ /^[0-9][0-9]?[0-9]?$/ || $i > 255) exit 1 }'; then
        sans="$sans,IP:$host"
    # IPv6: 16진수 묶음(4자리 이하)과 ':'. '::' 는 한 번까지, 없으면 묶음 8개, 있으면 7개 이하.
    elif printf '%s' "$host" | awk '
        !/^[0-9A-Fa-f:]+$/ || /:::/ || /^:[^:]/ || /[^:]:$/ { exit 1 }
        {
            d = gsub(/::/, "&")
            m = split($0, g, ":")
            n = 0
            for (i = 1; i <= m; i++) { if (length(g[i]) > 4) exit 1; if (g[i] != "") n++ }
            if (d > 1 || (d == 0 && n != 8) || (d == 1 && n > 7)) exit 1
        }'; then
        sans="$sans,IP:$host"
    # 호스트 이름: 영문·숫자·'.'·'-'·'_', 영문 1자 이상 (숫자와 '.' 만이면 틀린 IPv4 다).
    elif printf '%s' "$host" | grep -Eq '^[A-Za-z0-9._-]*[A-Za-z][A-Za-z0-9._-]*$'; then
        sans="$sans,DNS:$host"
    else
        echo "$0: 인증서에 넣을 수 없는 주소라 뺌: $host (MEDIAMTX_WEBRTC_HOST 확인)" >&2
    fi
done
IFS=$old_ifs

mkdir -p "$ACTIVE"

if [ -f "$CERTS/server.crt" ] && [ -f "$CERTS/server.key" ]; then
    # 운영자가 넣은 정식 인증서 — 자동 생성분은 건드리지 않는다.
    crt="$CERTS/server.crt"
    key="$CERTS/server.key"
    echo "$0: 운영자 인증서를 씀 ($crt)"
else
    if [ -f "$CERTS/server.crt" ] || [ -f "$CERTS/server.key" ]; then
        # 운영자가 파일을 하나만 넣었거나 이름을 틀렸다 — 왜 경고가 계속 뜨는지 알 수 있게 남긴다.
        echo "$0: server.crt/server.key 중 하나만 있음 — 자체 서명 인증서를 씀"
    fi
    crt="$CERTS/selfsigned.crt"
    key="$CERTS/selfsigned.key"

    # 자동 생성분은 업데이트 뒤에도 그대로 쓴다 — 다시 만들면 기기마다 경고를 또 넘겨야 한다.
    # 다시 만드는 경우는 셋뿐이다: 파일이 없을 때, 넣을 주소가 바뀌었을 때, 30일(2592000초) 안에 만료될 때.
    reason=""
    if [ ! -f "$crt" ] || [ ! -f "$key" ] || [ ! -f "$CERTS/selfsigned.sans" ]; then
        reason="파일 없음"
    elif [ "$(cat "$CERTS/selfsigned.sans")" != "$sans" ]; then
        reason="주소 바뀜"
    elif ! openssl x509 -in "$crt" -noout -checkend 2592000 >/dev/null; then
        reason="만료 30일 전"
    fi

    if [ -n "$reason" ]; then
        # 825일·serverAuth — Apple 기기(iOS 13·macOS 10.15 이후)의 TLS 서버 인증서 요건이다. RSA 2048 은 그 최소 크기다.
        echo "$0: 자체 서명 인증서를 만듦: $reason ($sans)"
        # 진행 표시는 버리고, 실패하면 openssl 의 오류를 로그에 남긴다.
        if ! err=$(openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
            -subj "/CN=rtsp-streaming" \
            -addext "subjectAltName=$sans" \
            -addext "extendedKeyUsage=serverAuth" \
            -keyout "$key" -out "$crt" 2>&1); then
            echo "$0: 자체 서명 인증서를 만들지 못함 ($sans): $err" >&2
            exit 1
        fi
        chmod 600 "$key"
        printf '%s\n' "$sans" > "$CERTS/selfsigned.sans"
    fi
fi

ln -sf "$crt" "$ACTIVE/tls.crt"
ln -sf "$key" "$ACTIVE/tls.key"

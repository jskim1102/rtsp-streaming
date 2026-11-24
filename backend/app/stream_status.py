"""카메라 path 상태 저장소 + mediamtx 훅 알림 API.

전체 흐름:
- mediamtx 는 path 상태가 바뀌면 훅 명령(wget)으로 백엔드 훅 알림 API 를 부른다 (`mediamtx.yml` pathDefaults).
- 훅 알림 API 는 상태 저장소(hub)의 메모리 상태를 바꾼다.
- 상태 저장소는 화면에 보이는 값이 바뀐 경우에만 SSE 로 연결된 브라우저마다 보낸다.
- 훅이 빠지거나 늦은 경우는 백그라운드 동기화(mediamtx.StreamReconciler)가 5초마다 mediamtx 상태 목록으로 바로잡는다.

용어:
- path: mediamtx 의 스트림 이름 — 카메라 하나에 하나이며 stream_key 와 같다.
- ready: path 의 소스(카메라 → ffmpeg)가 연결돼 영상이 들어오는 상태.
- reader: path 를 보고 있는 시청 세션 — 브라우저 탭의 WebRTC 연결 하나 등. 세션마다 ID 가 있다.
- 구독자: SSE 로 연결된 브라우저 탭 하나 (`GET /api/ipcams/events`).
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, Response
from fastapi.sse import ServerSentEvent
from starlette.concurrency import run_in_threadpool

from app.models import stream_key_prefix

logger = logging.getLogger("rtsp-streaming.stream_status")

# mediamtx 훅 4종 — runOnReady·runOnNotReady·runOnRead·runOnUnread 에 대응한다.
# - ready: 소스가 연결됐다 / notready: 소스가 끊겼다
# - read: 시청 세션이 붙었다 / unread: 시청 세션이 떠났다
HookEvent = Literal["ready", "notready", "read", "unread"]

# 구독자 하나에 밀린 이벤트 상한 — 브라우저가 받지 못하고 쌓이면 메모리가 계속 늘기 때문이다.
# 넘으면 그 구독을 끝내고, 브라우저가 다시 연결하면서 받는 snapshot 에 맡긴다.
# 카메라 상한 64대가 한꺼번에 바뀌어도 들어가는 크기다.
_QUEUE_MAX = 100

# 메모리에 없는 path 는 꺼짐으로 본다 — 화면도 상태를 모르는 카메라를 오프라인으로 그린다.
# 그래서 '없음 → 꺼짐'이나 '꺼짐 → 없음'은 바뀐 값으로 치지 않는다.
_INACTIVE = {"active": False, "readers": 0}


@dataclass
class PathStatus:
    """path 하나의 상태 — 소스 연결 여부와 시청 세션 ID 집합.

    - 시청자를 숫자가 아니라 ID 집합으로 둔다.
    - 같은 훅이 두 번 와도 두 번 세지 않는다.
    - unread 가 오면 떠난 그 세션만 뺀다.
    - 5초 보정 때 mediamtx 목록의 `readers[].id` 로 통째로 바꿀 수 있다 (훅의 MTX_READER_ID 와 같은 값).
    """

    ready: bool = False
    readers: set[str] = field(default_factory=set)

    def public(self) -> dict:
        """밖으로 보내는 값 — active 는 ready, readers 는 시청 세션 수다 (사람 수 아님)."""
        return {"active": self.ready, "readers": len(self.readers)}


class StreamStatusHub:
    """카메라별 상태를 메모리에 두고, 밖으로 보이는 값이 바뀔 때만 SSE 구독자에게 보낸다.

    - 상태를 바꾸는 곳은 셋이다.
      - 훅 알림 API: 스레드 풀에서 돈다 (apply_hook).
      - 백그라운드 동기화: 별도 스레드에서 5초마다 돈다 (replace_all, mark_all_inactive).
      - SSE 연결: 이벤트 루프에서 돈다 (subscribe 안의 replace_all).
    - 여러 스레드가 같은 dict 를 건드리므로 상태는 잠금(`_lock`)으로 보호한다.
    - 구독자마다 큐를 하나씩 둔다. SSE 응답은 자기 큐에서 꺼내 브라우저로 보낸다.
    - asyncio 큐는 스레드에 안전하지 않다.
    - 그래서 다른 스레드는 큐를 직접 건드리지 않고, 구독 때 잡아 둔 이벤트 루프에 '넣어 달라'고 넘긴다 (call_soon_threadsafe).
    - 모듈 전역 인스턴스(hub) 하나를 쓴다 — 백엔드가 프로세스 1개라는 가정이다 (여러 개면 메모리 상태가 갈린다).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[str, PathStatus] = {}  # stream_key → 상태
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}  # 구독자 큐 → 그 큐의 이벤트 루프

    def apply_hook(self, event: HookEvent, path: str, reader_id: str | None) -> None:
        """훅 하나를 반영한다.

        - 메모리에 없는 path 면 항목을 새로 만든다 — read 가 ready 보다 먼저 오는 경우가 있다.
        - 바뀌기 전과 뒤의 공개 값을 비교해 달라졌을 때만 보낸다.
        """
        with self._lock:
            status = self._statuses.setdefault(path, PathStatus())
            before = status.public()
            if event == "ready":
                status.ready = True
            elif event == "notready":
                # 소스가 끊기면 시청 세션도 끝난다 — unread 훅이 늦거나 빠져도 0으로 맞춘다.
                status.ready = False
                status.readers.clear()
            # 시청자 ID 가 없는 read·unread 는 누구인지 몰라 상태를 바꾸지 않는다.
            elif event == "read" and reader_id:
                status.readers.add(reader_id)
            elif event == "unread" and reader_id:
                status.readers.discard(reader_id)
            self._publish_if_changed(path, before, status.public())

    def replace_all(self, statuses: dict[str, PathStatus]) -> None:
        """mediamtx 상태 목록(정답)으로 메모리를 통째로 맞춘다 — 값이 바뀐 path 만 보낸다.

        - 부르는 곳: 백그라운드 동기화의 5초 상태 확인, SSE 연결 직후.
        - 목록에서 사라진 path(카메라 삭제, mediamtx 재시작 등)는 꺼짐으로 보내고 메모리에서 지운다.
        - 목록에 새로 나온 path 는 이전 값을 꺼짐으로 보고 비교한다.
        - 훅으로 이미 맞아 있던 path 는 값이 같아 아무것도 보내지 않는다.
        """
        with self._lock:
            for path in self._statuses.keys() - statuses.keys():
                self._publish_if_changed(path, self._statuses.pop(path).public(), _INACTIVE)
            for path, status in statuses.items():
                before = self._statuses[path].public() if path in self._statuses else _INACTIVE
                self._statuses[path] = status
                self._publish_if_changed(path, before, status.public())

    def mark_all_inactive(self) -> None:
        """모든 path 를 꺼짐으로 바꾼다.

        - mediamtx 연결이 끊긴 순간 백그라운드 동기화가 한 번 부른다.
        - 끊긴 동안에는 상태를 알 수 없어 화면을 모두 오프라인으로 둔다.
        - 다시 연결되면 다음 상태 확인(replace_all)이 전체를 맞춘다.
        """
        with self._lock:
            for path, status in self._statuses.items():
                before = status.public()
                status.ready = False
                status.readers.clear()
                self._publish_if_changed(path, before, status.public())

    def snapshot(self) -> dict[str, dict]:
        """메모리의 전체 상태 — `{stream_key: {active, readers}}`. SSE 첫 이벤트로 보낸다."""
        with self._lock:
            return {path: status.public() for path, status in self._statuses.items()}

    async def subscribe(
        self, refresh: Callable[[], dict[str, PathStatus] | None]
    ) -> AsyncIterator[ServerSentEvent]:
        """SSE 구독 하나 — 브라우저 탭 하나가 연결돼 있는 동안 이벤트를 하나씩 내보낸다.

        - 첫 이벤트는 `snapshot`(전체 상태)이다 — 브라우저는 받은 값으로 상태를 통째로 바꾼다.
        - 그 뒤로는 `update`(바뀐 카메라 하나)를 보낸다 — 브라우저는 그 카메라 값만 바꾼다.
        - refresh 는 mediamtx 상태 목록 조회다 (mediamtx.list_path_status). 실패하면 None 을 돌려준다.
        - 이 모듈이 mediamtx.py 를 import 하지 않도록 조회 함수를 인자로 받는다.
        """
        # 1) 구독을 먼저 등록한다 — snapshot 을 만드는 사이에 바뀐 값을 놓치지 않는다.
        #    그 사이의 update 가 snapshot 과 중복으로 갈 수 있지만, 같은 값을 다시 적용할 뿐이라 무해하다.
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            self._subscribers[queue] = asyncio.get_running_loop()
        try:
            # 2) mediamtx 상태 목록으로 메모리를 맞춘다 — 백엔드가 꺼져 있던 동안 놓친 훅을 바로잡는다.
            #    조회는 동기 httpx 라 스레드 풀에서 돌린다 — 이벤트 루프에서 돌리면 다른 요청이 멈춘다.
            #    여기서 바뀐 값은 다른 구독자에게도 update 로 간다.
            try:
                statuses = await run_in_threadpool(refresh)
            except Exception:
                logger.exception("SSE 연결 중 mediamtx 상태 조회 실패 — 메모리 값으로 보낸다")
                statuses = None
            if statuses is not None:
                self.replace_all(statuses)

            # 3) 조회가 실패해도 메모리 값으로 snapshot 을 보낸다.
            #    첫 이벤트 전에 오류 응답이 나가면 브라우저가 다시 연결하지 않는다.
            yield ServerSentEvent(event="snapshot", data=self.snapshot())

            # 4) 큐에 들어오는 update 를 기다렸다가 보낸다 — 연결이 끊길 때까지 반복한다.
            #    None 은 큐가 넘쳤다는 종료 신호다 — 스트림을 끝내면 브라우저가 다시 연결해 snapshot 부터 받는다.
            while (item := await queue.get()) is not None:
                yield ServerSentEvent(event="update", data=item)
        finally:
            # 브라우저가 연결을 끊거나 서버가 작업을 취소해도 여기를 지난다.
            # 구독을 빼야 이미 닫힌 연결(이벤트 루프)로 계속 보내려 하지 않는다.
            with self._lock:
                del self._subscribers[queue]

    def _publish_if_changed(self, path: str, before: dict, after: dict) -> None:
        """밖으로 보이는 값이 바뀌었으면 모든 구독자에게 update 를 보낸다.

        - active·readers 가 그대로면 보내지 않는다 — 예: 이미 켜진 path 에 ready 가 또 온 경우.
        - 잠금 안에서 부른다 — 상태가 바뀐 순서대로 큐에 들어가게 한다.
        - 이 함수는 어느 스레드에서든 불린다. 큐에 넣는 일(_offer)은 구독자의 이벤트 루프에 맡긴다.
        """
        if before == after:
            return
        item = {"stream_key": path, **after}
        for queue, loop in self._subscribers.items():
            loop.call_soon_threadsafe(self._offer, queue, item)

    @staticmethod
    def _offer(queue: asyncio.Queue, item: dict) -> None:
        """구독자 큐에 update 하나를 넣는다 — 구독자의 이벤트 루프에서 돈다.

        - 큐가 차 있으면 브라우저가 이벤트를 못 받아 가는 중이다 (느린 네트워크, 멈춘 탭 등).
        - 이때는 밀린 이벤트를 모두 버리고 종료 신호(None)만 넣는다.
        - 버린 변화는 브라우저가 다시 연결하면서 받는 snapshot 에 다 들어 있다.
        """
        if queue.full():
            while not queue.empty():
                queue.get_nowait()
            item = None
        queue.put_nowait(item)


# 훅 알림 API, 백그라운드 동기화, SSE 엔드포인트가 모두 이 인스턴스 하나를 쓴다.
hub = StreamStatusHub()


# ─── mediamtx 훅 알림 API ───

hook_router = APIRouter(prefix="/internal/mediamtx", tags=["internal"])


@hook_router.post("/hook", status_code=204)
def receive_mediamtx_hook(event: HookEvent, path: str, reader: str = "") -> Response:
    """mediamtx 훅 알림 — `mediamtx.yml` pathDefaults 의 runOn* 명령이 wget 으로 부른다.

    - 요청 예: `POST /internal/mediamtx/hook?event=read&path=rtsp_streaming__ipcam-1a2b3c4d&reader=<세션 ID>`
    - event 는 4종만 받는다 — 그 밖의 값은 FastAPI 검증으로 422.
    - ready·notready 에는 시청자가 없어 reader 가 빈 값으로 온다 — 빈 값은 None 으로 다룬다.
    - 이 모듈 형식이 아닌 path(all_others, 다른 프로젝트 path 등)는 무시한다.
    - 응답 본문이 없다(204) — wget 은 결과를 버리고, 백엔드가 응답하지 않으면 3초 뒤 포기한다.
    - /api 밖에 둔다 — 리버스 프록시(체크리스트 1번)가 /api 만 백엔드로 넘겨 외부에서 닿지 않는다.
    """
    if path.startswith(stream_key_prefix()):
        hub.apply_hook(event, path, reader or None)
    return Response(status_code=204)

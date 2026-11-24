"""stream_status 테스트 — 카메라 상태 저장소(훅 반영·보정·SSE 구독) + mediamtx 훅 알림 API."""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stream_status import PathStatus, StreamStatusHub, hook_router


@pytest.fixture(autouse=True)
def _no_path_prefix(monkeypatch):
    """모듈 형식을 기본값(`ipcam-`)으로 고정한다."""
    monkeypatch.delenv("MEDIAMTX_PATH_PREFIX", raising=False)


def _inactive_refresh():
    """mediamtx 조회 실패(None)를 흉내 낸다 — 메모리 값으로 snapshot 을 보낸다."""
    return None


def _raising_refresh():
    """조회 중 예외(MEDIAMTX_API 미설정 등)를 흉내 낸다."""
    raise RuntimeError("MEDIAMTX_API")


async def _events_after(hub: StreamStatusHub, action, count: int, refresh=_inactive_refresh) -> list:
    """구독해 snapshot 을 받은 뒤 action 을 실행하고, 이어지는 이벤트 count 개를 모은다."""
    stream = hub.subscribe(refresh)
    await anext(stream)
    action()
    events = [await asyncio.wait_for(anext(stream), 1) for _ in range(count)]
    await stream.aclose()
    return events


def _updates(events: list) -> list[tuple]:
    assert all(e.event == "update" for e in events)
    return [(e.data["stream_key"], e.data["active"], e.data["readers"]) for e in events]


# ─── 훅 반영 ───


def test_hooks_update_active_and_reader_count():
    hub = StreamStatusHub()

    def hooks():
        hub.apply_hook("ready", "ipcam-a", None)
        hub.apply_hook("read", "ipcam-a", "r1")
        hub.apply_hook("read", "ipcam-a", "r2")
        hub.apply_hook("unread", "ipcam-a", "r1")
        hub.apply_hook("notready", "ipcam-a", None)

    events = asyncio.run(_events_after(hub, hooks, 5))

    assert _updates(events) == [
        ("ipcam-a", True, 0),
        ("ipcam-a", True, 1),
        ("ipcam-a", True, 2),
        ("ipcam-a", True, 1),
        ("ipcam-a", False, 0),
    ]


def test_hook_without_visible_change_sends_no_update():
    """active·readers 가 그대로면 보내지 않는다 — 다음 이벤트가 바로 바뀐 값이어야 한다."""
    hub = StreamStatusHub()

    def hooks():
        hub.apply_hook("ready", "ipcam-a", None)
        hub.apply_hook("ready", "ipcam-a", None)
        hub.apply_hook("read", "ipcam-a", "r1")
        hub.apply_hook("read", "ipcam-a", "r1")
        hub.apply_hook("read", "ipcam-a", None)
        hub.apply_hook("notready", "ipcam-a", None)

    events = asyncio.run(_events_after(hub, hooks, 3))

    assert _updates(events) == [("ipcam-a", True, 0), ("ipcam-a", True, 1), ("ipcam-a", False, 0)]


def test_hook_for_unknown_path_creates_entry():
    """read 가 ready 보다 먼저 와도 시청자를 센다."""
    hub = StreamStatusHub()

    hub.apply_hook("read", "ipcam-a", "r1")

    assert hub.snapshot() == {"ipcam-a": {"active": False, "readers": 1}}


def test_notready_clears_readers():
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-a", None)
    hub.apply_hook("read", "ipcam-a", "r1")

    hub.apply_hook("notready", "ipcam-a", None)
    hub.apply_hook("ready", "ipcam-a", None)

    assert hub.snapshot() == {"ipcam-a": {"active": True, "readers": 0}}


# ─── 5초 보정 ───


def test_replace_all_sends_only_differences_and_drops_missing_paths():
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-a", None)
    hub.apply_hook("read", "ipcam-a", "r1")
    hub.apply_hook("ready", "ipcam-b", None)

    def correct():
        hub.replace_all({"ipcam-a": PathStatus(True, {"r1"}), "ipcam-c": PathStatus(True, set())})

    events = asyncio.run(_events_after(hub, correct, 2))

    assert sorted(_updates(events)) == [("ipcam-b", False, 0), ("ipcam-c", True, 0)]
    assert hub.snapshot() == {
        "ipcam-a": {"active": True, "readers": 1},
        "ipcam-c": {"active": True, "readers": 0},
    }


def test_mark_all_inactive_turns_every_path_off():
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-a", None)
    hub.apply_hook("read", "ipcam-a", "r1")
    hub.apply_hook("read", "ipcam-b", "r2")

    events = asyncio.run(_events_after(hub, hub.mark_all_inactive, 2))

    assert sorted(_updates(events)) == [("ipcam-a", False, 0), ("ipcam-b", False, 0)]
    assert all(s == {"active": False, "readers": 0} for s in hub.snapshot().values())


# ─── SSE 구독 ───


def test_subscribe_refreshes_from_mediamtx_then_sends_snapshot():
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-old", None)

    async def first_event():
        stream = hub.subscribe(lambda: {"ipcam-a": PathStatus(True, {"r1", "r2"})})
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(first_event())

    assert event.event == "snapshot"
    assert event.data == {"ipcam-a": {"active": True, "readers": 2}}


@pytest.mark.parametrize("refresh", [_inactive_refresh, _raising_refresh])
def test_subscribe_falls_back_to_memory_snapshot_when_refresh_fails(refresh):
    """첫 이벤트 전에 오류 응답이 나가면 브라우저가 다시 연결하지 않는다 — 메모리 값으로 보낸다."""
    hub = StreamStatusHub()
    hub.apply_hook("ready", "ipcam-a", None)

    async def first_event():
        stream = hub.subscribe(refresh)
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(first_event())

    assert event.event == "snapshot"
    assert event.data == {"ipcam-a": {"active": True, "readers": 0}}


def test_subscriber_stream_ends_when_queue_overflows():
    """밀린 이벤트가 큐 상한을 넘으면 그 구독을 끝낸다 — 브라우저가 다시 연결해 snapshot 을 받는다."""
    hub = StreamStatusHub()

    async def overflow():
        stream = hub.subscribe(_inactive_refresh)
        await anext(stream)
        for i in range(101):
            hub.apply_hook("read", "ipcam-a", f"r{i}")
        await asyncio.sleep(0)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 1)

    asyncio.run(overflow())


def test_closed_subscription_no_longer_receives_events():
    """연결이 끊긴 구독은 빠진다 — 닫힌 이벤트 루프로 보내려다 훅 처리가 실패하지 않는다."""
    hub = StreamStatusHub()
    asyncio.run(_events_after(hub, lambda: hub.apply_hook("ready", "ipcam-a", None), 1))

    hub.apply_hook("notready", "ipcam-a", None)

    assert hub.snapshot() == {"ipcam-a": {"active": False, "readers": 0}}


# ─── mediamtx 훅 알림 API ───


@pytest.fixture()
def hook_client():
    """훅 라우터만 단독 마운트하고, 테스트마다 새 저장소를 쓴다."""
    app = FastAPI()
    app.include_router(hook_router)
    hub = StreamStatusHub()
    with patch("app.stream_status.hub", hub), TestClient(app) as c:
        yield c, hub


def test_hook_endpoint_applies_event(hook_client):
    client, hub = hook_client

    resp = client.post("/internal/mediamtx/hook", params={"event": "read", "path": "ipcam-a", "reader": "r1"})

    assert resp.status_code == 204
    assert hub.snapshot() == {"ipcam-a": {"active": False, "readers": 1}}


def test_hook_endpoint_rejects_unknown_event(hook_client):
    client, hub = hook_client

    resp = client.post("/internal/mediamtx/hook", params={"event": "bogus", "path": "ipcam-a"})

    assert resp.status_code == 422
    assert hub.snapshot() == {}


@pytest.mark.parametrize("path", ["all_others", "other__ipcam-x", "rtsp_streaming__cam"])
def test_hook_endpoint_ignores_paths_outside_module_format(hook_client, path):
    client, hub = hook_client

    resp = client.post("/internal/mediamtx/hook", params={"event": "ready", "path": path})

    assert resp.status_code == 204
    assert hub.snapshot() == {}


def test_hook_endpoint_treats_empty_reader_as_none(hook_client):
    """ready·notready 훅은 시청자 ID 가 비어 온다 — 빈 값을 시청자로 세지 않는다."""
    client, hub = hook_client

    resp = client.post("/internal/mediamtx/hook", params={"event": "read", "path": "ipcam-a", "reader": ""})

    assert resp.status_code == 204
    assert hub.snapshot() == {"ipcam-a": {"active": False, "readers": 0}}

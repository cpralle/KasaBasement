import asyncio
import time

import httpx
import pytest
import pytest_asyncio

import kasa_bridge as kb


@pytest.fixture(autouse=True)
def isolate_globals(monkeypatch):
    original_config = kb.config.model_copy(deep=True)
    original_device_state_cache = dict(kb._device_state_cache)
    original_device_conn_cache = dict(kb._device_connection_cache)
    original_room_toggle_state = dict(kb._room_toggle_state)
    original_room_cycle_state = dict(kb._room_cycle_state)
    original_trigger_token = kb.SCENE_TRIGGER_TOKEN

    monkeypatch.setattr(kb, "save_config", lambda _cfg: None)
    monkeypatch.setattr(kb, "SCENE_TRIGGER_TOKEN", None)

    yield

    kb.config = original_config
    kb._device_state_cache.clear()
    kb._device_state_cache.update(original_device_state_cache)
    kb._device_connection_cache.clear()
    kb._device_connection_cache.update(original_device_conn_cache)
    kb._room_toggle_state.clear()
    kb._room_toggle_state.update(original_room_toggle_state)
    kb._room_cycle_state.clear()
    kb._room_cycle_state.update(original_room_cycle_state)
    kb.SCENE_TRIGGER_TOKEN = original_trigger_token


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=kb.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def seed_room_on_config():
    kb.config = kb.Config(
        devices=[
            kb.DeviceConfig(alias="Lamp A", mac="aa:aa:aa:aa:aa:01", host="10.0.0.10", type="plug"),
            kb.DeviceConfig(alias="Lamp B", mac="aa:aa:aa:aa:aa:02", host="10.0.0.11", type="plug"),
        ],
        scenes=[
            kb.Scene(
                name="Movie",
                room_idx=0,
                actions=[
                    kb.SceneAction(device_alias="Lamp A", action="on"),
                    kb.SceneAction(device_alias="Lamp B", action="on"),
                ],
            )
        ],
        rooms=[
            kb.Room(
                name="Living",
                is_on=True,
                active_scene="Movie",
                last_on_scene="Movie",
                active_dim="d4",
                grid_map=[
                    kb.SceneAction(device_alias="Lamp A", action="on"),
                    kb.SceneAction(device_alias="Lamp B", action="on"),
                ],
            )
        ],
        routines=[],
    )


@pytest.mark.asyncio
async def test_regression_double_toggle_should_not_issue_duplicate_off(client, monkeypatch):
    """
    Reproduces real-world race:
    - Toggle request A starts while room is marked ON.
    - Before A completes, toggle request B arrives.
    Current behavior can execute OFF twice (A and B), matching the log anomaly.

    Expected behavior (desired): two rapid toggles should collapse/serialize into OFF then ON.
    """
    seed_room_on_config()
    async with kb.event_log_lock:
        kb.event_log.clear()

    async def slow_unreachable_resolve(_dev_cfg, _mac_to_device):
        # Keep first request in-flight long enough for second request to evaluate stale room.is_on.
        await asyncio.sleep(0.25)
        return None, "mac"

    monkeypatch.setattr(kb, "resolve_device_for_config", slow_unreachable_resolve)

    t1 = asyncio.create_task(client.get("/api/Living/toggle"))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(client.get("/api/Living/toggle"))
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.status_code == 200
    assert r2.status_code == 200

    async with kb.event_log_lock:
        results = [
            e for e in kb.event_log
            if e.get("type") == "scene_run_result" and e.get("path") == "/api/Living/toggle"
        ]

    scene_names = [e.get("details", {}).get("scene_name") for e in results]
    # Desired invariant: no duplicate OFF runs from two rapid toggles.
    # Current code fails this and produces ["Living_toggle:off", "Living_toggle:off"].
    assert scene_names.count("Living_toggle:off") <= 1
    assert "Movie" in scene_names

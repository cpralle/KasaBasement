import asyncio
import time

import httpx
import pytest
import pytest_asyncio

import kasa_bridge as kb


class FakeDevice:
    def __init__(self, host: str, is_on: bool = False):
        self.host = host
        self.is_on = is_on
        self.modules = {}
        self.update_calls = 0
        self.turn_on_calls = 0
        self.turn_off_calls = 0

    async def update(self):
        self.update_calls += 1

    async def turn_on(self):
        self.turn_on_calls += 1
        self.is_on = True

    async def turn_off(self):
        self.turn_off_calls += 1
        self.is_on = False


@pytest.fixture(autouse=True)
def isolate_globals(monkeypatch):
    original_config = kb.config.model_copy(deep=True)
    original_device_state_cache = dict(kb._device_state_cache)
    original_device_conn_cache = dict(kb._device_connection_cache)
    original_room_toggle_state = dict(kb._room_toggle_state)
    original_room_cycle_state = dict(kb._room_cycle_state)
    original_trigger_token = kb.SCENE_TRIGGER_TOKEN

    # Keep tests deterministic and local-only.
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


def seed_single_room_config():
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
                is_on=False,
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
async def test_room_toggle_uses_room_state_fast_path(client, monkeypatch):
    seed_single_room_config()
    devices = {
        "Lamp A": FakeDevice("10.0.0.10", is_on=False),
        "Lamp B": FakeDevice("10.0.0.11", is_on=False),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    # Intentionally poison state cache to opposite state; toggle should still use room.is_on=False and turn ON.
    now = time.time()
    async with kb._device_state_cache_lock:
        kb._device_state_cache[kb.normalize_mac("aa:aa:aa:aa:aa:01")] = {"is_on": True, "ts": now}
        kb._device_state_cache[kb.normalize_mac("aa:aa:aa:aa:aa:02")] = {"is_on": True, "ts": now}

    resp = await client.get("/api/Living/toggle")
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "on"
    assert kb.config.rooms[0].is_on is True
    assert devices["Lamp A"].turn_on_calls == 1
    assert devices["Lamp B"].turn_on_calls == 1


@pytest.mark.asyncio
async def test_notify_state_updates_room_state(client):
    seed_single_room_config()
    kb.config.rooms[0].is_on = False

    resp_on = await client.get("/api/notify/state?state=on")
    assert resp_on.status_code == 200
    assert kb.config.rooms[0].is_on is True

    resp_off = await client.get("/api/notify/state?state=off")
    assert resp_off.status_code == 200
    assert kb.config.rooms[0].is_on is False


@pytest.mark.asyncio
async def test_reconcile_room_states_once_updates_room_and_cache(monkeypatch):
    seed_single_room_config()
    kb.config.rooms[0].is_on = False

    devices = {
        "Lamp A": FakeDevice("10.0.0.10", is_on=True),
        "Lamp B": FakeDevice("10.0.0.11", is_on=False),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    await kb.reconcile_room_states_once()

    assert kb.config.rooms[0].is_on is True
    async with kb._device_state_cache_lock:
        c1 = kb._device_state_cache[kb.normalize_mac("aa:aa:aa:aa:aa:01")]
        c2 = kb._device_state_cache[kb.normalize_mac("aa:aa:aa:aa:aa:02")]
    assert c1["is_on"] is True
    assert c2["is_on"] is False


@pytest.mark.asyncio
async def test_device_keepalive_once_warms_connection_cache(monkeypatch):
    seed_single_room_config()
    kb._device_connection_cache.clear()

    async def fake_discover_single(host):
        return FakeDevice(host, is_on=False)

    monkeypatch.setattr(kb, "kasa_discover_single", fake_discover_single)

    await kb.device_keepalive_once()

    async with kb._device_cache_lock:
        cache_keys = set(kb._device_connection_cache.keys())
        dev_a, _ = kb._device_connection_cache[kb.normalize_mac("aa:aa:aa:aa:aa:01")]
        dev_b, _ = kb._device_connection_cache[kb.normalize_mac("aa:aa:aa:aa:aa:02")]

    assert kb.normalize_mac("aa:aa:aa:aa:aa:01") in cache_keys
    assert kb.normalize_mac("aa:aa:aa:aa:aa:02") in cache_keys
    assert isinstance(dev_a, FakeDevice)
    assert isinstance(dev_b, FakeDevice)
    assert dev_a.update_calls >= 1
    assert dev_b.update_calls >= 1

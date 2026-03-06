import asyncio
import itertools
import random
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

    async def update(self):
        return None

    async def turn_on(self):
        self.is_on = True

    async def turn_off(self):
        self.is_on = False


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


def build_test_house():
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
                    kb.SceneAction(device_alias="Lamp B", action="off"),
                ],
            ),
            kb.Scene(
                name="Relax",
                room_idx=0,
                actions=[
                    kb.SceneAction(device_alias="Lamp A", action="off"),
                    kb.SceneAction(device_alias="Lamp B", action="on"),
                ],
            ),
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
        routines=[
            kb.Routine(
                name="Group On",
                time_hhmm="00:00",
                enabled=False,
                actions=[kb.RoutineAction(kind="group", room_idx=0, group_action="on")],
            ),
            kb.Routine(
                name="Group Off",
                time_hhmm="00:00",
                enabled=False,
                actions=[kb.RoutineAction(kind="group", room_idx=0, group_action="off")],
            ),
            kb.Routine(
                name="Group Toggle",
                time_hhmm="00:00",
                enabled=False,
                actions=[kb.RoutineAction(kind="group", room_idx=0, group_action="toggle")],
            ),
            kb.Routine(
                name="Set Scene Movie",
                time_hhmm="00:00",
                enabled=False,
                actions=[kb.RoutineAction(kind="scene", scene_name="Movie")],
            ),
            kb.Routine(
                name="Set Scene Relax",
                time_hhmm="00:00",
                enabled=False,
                actions=[kb.RoutineAction(kind="scene", scene_name="Relax")],
            ),
        ],
    )


def reset_runtime_state(devices: dict[str, FakeDevice]):
    room = kb.config.rooms[0]
    room.is_on = False
    room.active_scene = "Movie"
    room.last_on_scene = "Movie"
    for d in devices.values():
        d.is_on = False
    kb._device_connection_cache.clear()
    kb._device_state_cache.clear()
    now = time.time()
    for cfg in kb.config.devices:
        kb._device_state_cache[kb.normalize_mac(cfg.mac)] = {"is_on": False, "ts": now}


def assert_state_consistency(devices: dict[str, FakeDevice]):
    room = kb.config.rooms[0]
    physical_any_on = any(d.is_on for d in devices.values())
    assert room.is_on == physical_any_on
    assert room.active_scene in {"Movie", "Relax"}


async def run_command(name: str, client: httpx.AsyncClient):
    if name == "routine_group_on":
        out = await kb.run_routine(0)
        assert out["status"] == "success"
        return
    if name == "routine_group_off":
        out = await kb.run_routine(1)
        assert out["status"] == "success"
        return
    if name == "routine_group_toggle":
        out = await kb.run_routine(2)
        assert out["status"] == "success"
        return
    if name == "routine_set_scene_movie":
        out = await kb.run_routine(3)
        assert out["status"] == "success"
        return
    if name == "routine_set_scene_relax":
        out = await kb.run_routine(4)
        assert out["status"] == "success"
        return
    if name == "group_on":
        out = await kb.run_routine(0)
        assert out["status"] == "success"
        return
    if name == "group_off":
        out = await kb.run_routine(1)
        assert out["status"] == "success"
        return
    if name == "group_toggle":
        out = await kb.run_routine(2)
        assert out["status"] == "success"
        return
    if name == "set_scene_movie":
        out = await kb.run_routine(3)
        assert out["status"] == "success"
        return
    if name == "set_scene_relax":
        out = await kb.run_routine(4)
        assert out["status"] == "success"
        return
    if name == "trigger_scene_movie":
        resp = await client.get("/api/trigger/scene/Movie")
        assert resp.status_code == 200
        return
    if name == "trigger_scene_relax":
        resp = await client.get("/api/trigger/scene/Relax")
        assert resp.status_code == 200
        return
    if name == "room_toggle":
        resp = await client.get("/api/Living/toggle")
        assert resp.status_code == 200
        return
    raise AssertionError(f"Unknown command: {name}")


@pytest.mark.asyncio
async def test_explicit_consecutive_patterns(client, monkeypatch):
    build_test_house()
    devices = {
        "Lamp A": FakeDevice("10.0.0.10"),
        "Lamp B": FakeDevice("10.0.0.11"),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    # Pattern: group on -> group off
    reset_runtime_state(devices)
    await run_command("group_on", client)
    await run_command("group_off", client)
    assert not devices["Lamp A"].is_on
    assert not devices["Lamp B"].is_on
    assert kb.config.rooms[0].is_on is False

    # Pattern: group off -> group on
    reset_runtime_state(devices)
    await run_command("group_off", client)
    await run_command("group_on", client)
    assert kb.config.rooms[0].is_on is True
    # Active scene starts as Movie, so Movie should be applied.
    assert devices["Lamp A"].is_on is True
    assert devices["Lamp B"].is_on is False

    # Pattern: run "set scene routine" while off -> group on
    reset_runtime_state(devices)
    await run_command("set_scene_relax", client)
    assert kb.config.rooms[0].active_scene == "Relax"
    assert not devices["Lamp A"].is_on and not devices["Lamp B"].is_on
    await run_command("group_on", client)
    assert devices["Lamp A"].is_on is False
    assert devices["Lamp B"].is_on is True
    assert kb.config.rooms[0].is_on is True


@pytest.mark.asyncio
async def test_all_two_step_command_transitions(client, monkeypatch):
    build_test_house()
    devices = {
        "Lamp A": FakeDevice("10.0.0.10"),
        "Lamp B": FakeDevice("10.0.0.11"),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    commands = [
        "group_on",
        "group_off",
        "group_toggle",
        "set_scene_movie",
        "set_scene_relax",
        "trigger_scene_movie",
        "trigger_scene_relax",
        "room_toggle",
    ]

    for c1, c2 in itertools.product(commands, repeat=2):
        reset_runtime_state(devices)
        await run_command(c1, client)
        assert_state_consistency(devices)
        await run_command(c2, client)
        assert_state_consistency(devices)


@pytest.mark.asyncio
async def test_all_three_step_command_transitions(client, monkeypatch):
    build_test_house()
    devices = {
        "Lamp A": FakeDevice("10.0.0.10"),
        "Lamp B": FakeDevice("10.0.0.11"),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    commands = [
        "group_on",
        "group_off",
        "group_toggle",
        "set_scene_movie",
        "set_scene_relax",
        "trigger_scene_movie",
        "trigger_scene_relax",
        "room_toggle",
    ]

    for c1, c2, c3 in itertools.product(commands, repeat=3):
        reset_runtime_state(devices)
        await run_command(c1, client)
        assert_state_consistency(devices)
        await run_command(c2, client)
        assert_state_consistency(devices)
        await run_command(c3, client)
        assert_state_consistency(devices)


@pytest.mark.asyncio
async def test_random_long_sequences_for_large_combination_space(client, monkeypatch):
    """
    Sequence-space coverage approach:
    We cannot execute all high-9-digit combinations, so we run many seeded
    random long sequences and assert safety/state invariants at every step.
    """
    build_test_house()
    devices = {
        "Lamp A": FakeDevice("10.0.0.10"),
        "Lamp B": FakeDevice("10.0.0.11"),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    commands = [
        "group_on",
        "group_off",
        "group_toggle",
        "set_scene_movie",
        "set_scene_relax",
        "trigger_scene_movie",
        "trigger_scene_relax",
        "room_toggle",
    ]

    rng = random.Random(20260304)
    sequence_count = 300
    sequence_len = 20

    for _ in range(sequence_count):
        reset_runtime_state(devices)
        for _step in range(sequence_len):
            cmd = rng.choice(commands)
            await run_command(cmd, client)
            assert_state_consistency(devices)


@pytest.mark.asyncio
async def test_partial_group_apply_one_device_fails(client, monkeypatch):
    build_test_house()

    class FailingDevice(FakeDevice):
        async def turn_off(self):
            raise TimeoutError("simulated device failure")

    devices = {
        "Lamp A": FakeDevice("10.0.0.10"),
        "Lamp B": FailingDevice("10.0.0.11"),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    # Turn room on first so group off does real work.
    await run_command("group_on", client)
    assert kb.config.rooms[0].is_on is True

    out = await kb.run_routine(1)  # Group Off routine
    assert out["status"] == "success"
    result = out["results"][0]["result"]["results"]
    ok = [r for r in result if r.get("status") == "success"]
    err = [r for r in result if r.get("status") == "error"]
    assert ok
    assert err


@pytest.mark.asyncio
async def test_two_command_combos_with_idle_gap_including_routines(client, monkeypatch):
    build_test_house()
    devices = {
        "Lamp A": FakeDevice("10.0.0.10"),
        "Lamp B": FakeDevice("10.0.0.11"),
    }

    async def fake_resolve(dev_cfg, _mac_to_device):
        return devices[dev_cfg.alias], "fake"

    monkeypatch.setattr(kb, "resolve_device_for_config", fake_resolve)

    # Simulate "long idle" quickly by shrinking TTL and aging caches between steps.
    old_ttl = kb.DEVICE_STATE_CACHE_TTL
    monkeypatch.setattr(kb, "DEVICE_STATE_CACHE_TTL", 0.01)

    async def simulate_idle_gap():
        await asyncio.sleep(0.03)
        kb._device_connection_cache.clear()
        now = time.time()
        async with kb._device_state_cache_lock:
            for cfg in kb.config.devices:
                mac = kb.normalize_mac(cfg.mac)
                kb._device_state_cache[mac] = {"is_on": False, "ts": now - 3600.0}

    commands = [
        # Explicit routine executions
        "routine_group_on",
        "routine_group_off",
        "routine_group_toggle",
        "routine_set_scene_movie",
        "routine_set_scene_relax",
        # Non-routine command paths
        "trigger_scene_movie",
        "trigger_scene_relax",
        "room_toggle",
    ]

    for c1, c2 in itertools.product(commands, repeat=2):
        reset_runtime_state(devices)
        await run_command(c1, client)
        assert_state_consistency(devices)
        await simulate_idle_gap()
        await run_command(c2, client)
        assert_state_consistency(devices)

    monkeypatch.setattr(kb, "DEVICE_STATE_CACHE_TTL", old_ttl)

import asyncio
import time

import pytest

from tests.light_model import AckModel
from tests.light_model import Distribution
from tests.light_model import FlapModel
from tests.light_model import LightModelConfig
from tests.light_model import LossModel
from tests.light_model import RateLimitModel
from tests.light_model import RebootModel
from tests.light_model import ReorderModel
from tests.light_model import ScriptedKasaLight
from tests.light_model import SimulatedKasaLight
from tests.retry_harness import RetryPolicy
from tests.retry_harness import with_retry


@pytest.mark.asyncio
async def test_online_offline_flapping_mtbf_mttr():
    cfg = LightModelConfig(
        flap=FlapModel(enabled=True, mtbf_s=0.02, mttr_s=0.02, start_online=True),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=7, config=cfg)

    states = []
    start = time.monotonic()
    while (time.monotonic() - start) < 0.2:
        states.append(dev.is_online)
        await asyncio.sleep(0.005)

    assert any(states)
    assert any(not s for s in states)


def test_latency_distribution_has_jitter_and_spikes():
    cfg = LightModelConfig(
        latency=Distribution(
            base_ms=2,
            jitter_ms=2,
            spike_probability=0.5,
            spike_min_ms=10,
            spike_max_ms=20,
        ),
        apply_delay=Distribution(base_ms=0),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=11, config=cfg)

    samples_ms = []
    for _ in range(200):
        s = dev._sample_distribution_seconds(dev._cfg.latency, rate_limited=False)
        samples_ms.append(s * 1000.0)

    assert max(samples_ms) > 10.0
    assert min(samples_ms) < 5.0


@pytest.mark.asyncio
async def test_loss_probability_with_burst_mode():
    cfg = LightModelConfig(
        loss=LossModel(
            loss_probability=0.0,
            burst_probability=1.0,
            burst_min=3,
            burst_max=3,
        ),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=3, config=cfg)

    failures = 0
    for _ in range(3):
        with pytest.raises(TimeoutError):
            await dev.update()
        failures += 1

    assert failures == 3


def test_apply_delay_distribution_base_jitter_and_spikes():
    cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        apply_delay=Distribution(
            base_ms=1,
            jitter_ms=1,
            spike_probability=0.5,
            spike_min_ms=10,
            spike_max_ms=20,
        ),
        ack=AckModel(style="apply"),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=19, config=cfg)

    samples_ms = []
    for _ in range(200):
        s = dev._sample_distribution_seconds(dev._cfg.apply_delay, rate_limited=False)
        samples_ms.append(s * 1000.0)

    assert max(samples_ms) > 10.0
    assert min(samples_ms) < 3.0


@pytest.mark.asyncio
async def test_ack_style_receipt_vs_apply_and_late_responses():
    receipt_cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        apply_delay=Distribution(base_ms=30),
        ack=AckModel(style="receipt", late_response_probability=1.0, late_min_ms=10, late_max_ms=10),
    )
    apply_cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        apply_delay=Distribution(base_ms=30),
        ack=AckModel(style="apply", late_response_probability=1.0, late_min_ms=10, late_max_ms=10),
    )

    d_receipt = SimulatedKasaLight("10.0.0.10", seed=1, config=receipt_cfg)
    d_apply = SimulatedKasaLight("10.0.0.11", seed=1, config=apply_cfg)

    t0 = time.monotonic()
    await d_receipt.turn_on()
    receipt_elapsed_ms = (time.monotonic() - t0) * 1000.0
    # Receipt ACK returns before apply completes; state should still be pending immediately.
    assert receipt_elapsed_ms < 30.0
    assert d_receipt.actual_is_on is False
    await d_receipt.wait_for_idle()
    assert d_receipt.actual_is_on is True

    t1 = time.monotonic()
    await d_apply.turn_on()
    apply_elapsed_ms = (time.monotonic() - t1) * 1000.0
    assert apply_elapsed_ms >= 35.0
    assert d_apply.actual_is_on is True


@pytest.mark.asyncio
async def test_rate_limit_threshold_increases_delay_and_loss():
    cfg = LightModelConfig(
        latency=Distribution(base_ms=2),
        apply_delay=Distribution(base_ms=0),
        loss=LossModel(loss_probability=0.0, burst_probability=0.0),
        rate_limit=RateLimitModel(
            threshold=1,
            window_s=0.2,
            delay_multiplier=10.0,
            extra_loss_probability=1.0,
        ),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=5, config=cfg)

    t0 = time.monotonic()
    await dev.update()  # first command not rate-limited
    first_ms = (time.monotonic() - t0) * 1000.0

    t1 = time.monotonic()
    with pytest.raises(TimeoutError):
        await dev.update()  # second command inside window gets extra loss
    second_ms = (time.monotonic() - t1) * 1000.0

    assert second_ms > first_ms


@pytest.mark.asyncio
async def test_stale_read_lag_delays_reported_state():
    cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        apply_delay=Distribution(base_ms=0),
        ack=AckModel(style="apply"),
        stale_read_lag_s=0.05,
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=29, config=cfg, initial_is_on=False)

    await dev.turn_on()
    assert dev.actual_is_on is True
    # Reported value should still lag briefly.
    assert dev.is_on is False
    await asyncio.sleep(0.06)
    assert dev.is_on is True


@pytest.mark.asyncio
async def test_retry_backoff_recovers_from_transient_timeouts():
    attempts = {"n": 0}

    async def op():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise TimeoutError("transient")
        return "ok"

    out = await with_retry(
        op,
        policy=RetryPolicy(max_attempts=5, base_backoff_ms=1, max_backoff_ms=10, jitter_ms=0, multiplier=2),
        seed=1,
    )
    assert out == "ok"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_apply_are_modeled():
    cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        apply_delay=Distribution(base_ms=0),
        ack=AckModel(style="receipt"),
        reorder=ReorderModel(
            enabled=True,
            out_of_order_probability=1.0,
            extra_delay_min_ms=0.0,
            extra_delay_max_ms=40.0,
            duplicate_apply_probability=1.0,
        ),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=123, config=cfg)

    await asyncio.gather(dev.turn_on(), dev.turn_off())
    await dev.wait_for_idle()
    trace = dev.export_trace()
    applies = [e for e in trace if e.get("event") == "apply"]

    assert len(applies) >= 4  # each write duplicated
    ids = [e["command_id"] for e in applies]
    assert ids != sorted(ids)  # out-of-order apply


@pytest.mark.asyncio
async def test_reboot_semantics_force_temporary_offline_and_state_reset():
    cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        reboot=RebootModel(probability=1.0, downtime_min_s=0.03, downtime_max_s=0.03, reset_state="off"),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=9, config=cfg, initial_is_on=True)

    with pytest.raises(TimeoutError):
        await dev.turn_on()
    assert dev.actual_is_on is False  # reset_state=off took effect
    assert dev.is_online is False

    await asyncio.sleep(0.04)
    # Next call reboots again (probability=1), so set probability to 0 after first event.
    dev._cfg.reboot.probability = 0.0
    await dev.update()
    assert dev.is_online is True


@pytest.mark.asyncio
async def test_scenario_trace_playback_reproduces_outcomes():
    cfg = LightModelConfig(
        latency=Distribution(base_ms=0),
        apply_delay=Distribution(base_ms=0),
        loss=LossModel(loss_probability=0.0, burst_probability=1.0, burst_min=1, burst_max=1),
        ack=AckModel(style="apply"),
    )
    dev = SimulatedKasaLight("10.0.0.10", seed=99, config=cfg)

    outcomes = []
    for op in ("update", "turn_on", "turn_off"):
        try:
            await getattr(dev, op)()
            outcomes.append("ok")
        except TimeoutError:
            outcomes.append("drop")

    script = []
    for e in dev.export_trace():
        if e.get("event") not in ("apply", "drop"):
            continue
        step = {"event": e["event"], "op": e.get("op"), "target_state": e.get("target_state"), "sleep_s": 0.0}
        script.append(step)

    replay = ScriptedKasaLight("10.0.0.10", script, initial_is_on=False)
    replay_outcomes = []
    for op in ("update", "turn_on", "turn_off"):
        try:
            await getattr(replay, op)()
            replay_outcomes.append("ok")
        except TimeoutError:
            replay_outcomes.append("drop")
    assert outcomes == replay_outcomes

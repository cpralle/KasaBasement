import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque
from typing import Optional


@dataclass
class Distribution:
    base_ms: float = 0.0
    jitter_ms: float = 0.0
    spike_probability: float = 0.0
    spike_min_ms: float = 0.0
    spike_max_ms: float = 0.0


@dataclass
class FlapModel:
    enabled: bool = False
    mtbf_s: float = 60.0
    mttr_s: float = 2.0
    start_online: bool = True


@dataclass
class LossModel:
    loss_probability: float = 0.0
    burst_probability: float = 0.0
    burst_min: int = 1
    burst_max: int = 3


@dataclass
class RateLimitModel:
    threshold: int = 10
    window_s: float = 1.0
    delay_multiplier: float = 1.0
    extra_loss_probability: float = 0.0


@dataclass
class AckModel:
    # "receipt" = command acknowledged before state is applied
    # "apply"   = response after state apply completes
    style: str = "apply"
    late_response_probability: float = 0.0
    late_min_ms: float = 0.0
    late_max_ms: float = 0.0


@dataclass
class ReorderModel:
    enabled: bool = False
    out_of_order_probability: float = 0.0
    extra_delay_min_ms: float = 0.0
    extra_delay_max_ms: float = 0.0
    duplicate_apply_probability: float = 0.0


@dataclass
class RebootModel:
    probability: float = 0.0
    downtime_min_s: float = 0.0
    downtime_max_s: float = 0.0
    reset_state: str = "preserve"  # "preserve" | "off" | "on"


@dataclass
class LightModelConfig:
    flap: FlapModel = field(default_factory=FlapModel)
    latency: Distribution = field(default_factory=Distribution)
    apply_delay: Distribution = field(default_factory=Distribution)
    loss: LossModel = field(default_factory=LossModel)
    ack: AckModel = field(default_factory=AckModel)
    reorder: ReorderModel = field(default_factory=ReorderModel)
    reboot: RebootModel = field(default_factory=RebootModel)
    rate_limit: RateLimitModel = field(default_factory=RateLimitModel)
    stale_read_lag_s: float = 0.0


class SimulatedKasaLight:
    """
    A test-only Kasa-like device model with network and device behavior controls.
    Methods mirror the app's expected async device interface: update/turn_on/turn_off.
    """

    def __init__(
        self,
        host: str,
        *,
        seed: int = 0,
        initial_is_on: bool = False,
        config: LightModelConfig | None = None,
    ):
        self.host = host
        self.modules = {}
        self._rng = random.Random(seed)
        self._cfg = config or LightModelConfig()
        self._actual_is_on = bool(initial_is_on)
        self._history: Deque[tuple[float, bool]] = deque(maxlen=2048)
        self._history.append((time.monotonic(), self._actual_is_on))
        self._burst_remaining = 0
        self._cmd_times: Deque[float] = deque()
        self._online = bool(self._cfg.flap.start_online)
        self._next_flap_ts = self._schedule_next_flap(time.monotonic())
        self._pending_apply_tasks: set[asyncio.Task] = set()
        self._forced_offline_until = 0.0
        self._trace: list[dict] = []
        self._command_id = 0

    @property
    def actual_is_on(self) -> bool:
        return self._actual_is_on

    @property
    def is_online(self) -> bool:
        self._advance_flap_state(time.monotonic())
        return self._effective_online(time.monotonic())

    @property
    def is_on(self) -> bool:
        """
        Reported state can lag behind actual state (stale reads).
        """
        now = time.monotonic()
        lagged_time = now - max(0.0, float(self._cfg.stale_read_lag_s))
        for ts, state in reversed(self._history):
            if ts <= lagged_time:
                return state
        return self._history[0][1]

    async def update(self):
        await self._simulate_round_trip(is_write=False)

    async def turn_on(self):
        await self._simulate_write(target_state=True)

    async def turn_off(self):
        await self._simulate_write(target_state=False)

    async def wait_for_idle(self):
        while self._pending_apply_tasks:
            tasks = list(self._pending_apply_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    def clear_trace(self):
        self._trace.clear()

    def export_trace(self) -> list[dict]:
        return [dict(e) for e in self._trace]

    def _record_trace(self, event: dict):
        out = dict(event)
        out.setdefault("t", time.monotonic())
        self._trace.append(out)

    def _sample_exp(self, mean_s: float) -> float:
        m = max(0.0, float(mean_s))
        if m == 0.0:
            return 0.0
        return self._rng.expovariate(1.0 / m)

    def _schedule_next_flap(self, now: float) -> float:
        if not self._cfg.flap.enabled:
            return float("inf")
        if self._online:
            return now + self._sample_exp(self._cfg.flap.mtbf_s)
        return now + self._sample_exp(self._cfg.flap.mttr_s)

    def _advance_flap_state(self, now: float):
        if not self._cfg.flap.enabled:
            return
        while now >= self._next_flap_ts:
            self._online = not self._online
            self._next_flap_ts = self._schedule_next_flap(self._next_flap_ts)

    def _effective_online(self, now: float) -> bool:
        return self._online and now >= self._forced_offline_until

    def _sample_distribution_seconds(self, dist: Distribution, *, rate_limited: bool = False) -> float:
        base = float(dist.base_ms)
        jitter = float(dist.jitter_ms)
        val_ms = base + self._rng.uniform(-jitter, jitter)
        val_ms = max(0.0, val_ms)
        if self._rng.random() < float(dist.spike_probability):
            lo = float(dist.spike_min_ms)
            hi = float(dist.spike_max_ms)
            if hi < lo:
                lo, hi = hi, lo
            val_ms += self._rng.uniform(lo, hi)
        if rate_limited:
            mult = max(1.0, float(self._cfg.rate_limit.delay_multiplier))
            val_ms *= mult
        return val_ms / 1000.0

    def _is_rate_limited(self, now: float) -> bool:
        window = max(0.001, float(self._cfg.rate_limit.window_s))
        while self._cmd_times and (now - self._cmd_times[0]) > window:
            self._cmd_times.popleft()
        self._cmd_times.append(now)
        threshold = max(1, int(self._cfg.rate_limit.threshold))
        return len(self._cmd_times) > threshold

    def _should_drop(self, *, rate_limited: bool) -> bool:
        if self._burst_remaining > 0:
            self._burst_remaining -= 1
            return True

        p = float(self._cfg.loss.loss_probability)
        if rate_limited:
            p += float(self._cfg.rate_limit.extra_loss_probability)

        if self._rng.random() < max(0.0, min(1.0, p)):
            return True

        burst_p = max(0.0, min(1.0, float(self._cfg.loss.burst_probability)))
        if self._rng.random() < burst_p:
            bmin = max(1, int(self._cfg.loss.burst_min))
            bmax = max(bmin, int(self._cfg.loss.burst_max))
            self._burst_remaining = self._rng.randint(bmin, bmax) - 1
            return True
        return False

    def _sample_late_response_s(self) -> float:
        ack = self._cfg.ack
        if self._rng.random() >= max(0.0, min(1.0, float(ack.late_response_probability))):
            return 0.0
        lo = float(ack.late_min_ms)
        hi = float(ack.late_max_ms)
        if hi < lo:
            lo, hi = hi, lo
        return self._rng.uniform(lo, hi) / 1000.0

    def _sample_reorder_extra_s(self) -> float:
        r = self._cfg.reorder
        if not r.enabled:
            return 0.0
        if self._rng.random() >= max(0.0, min(1.0, float(r.out_of_order_probability))):
            return 0.0
        lo = float(r.extra_delay_min_ms)
        hi = float(r.extra_delay_max_ms)
        if hi < lo:
            lo, hi = hi, lo
        return self._rng.uniform(lo, hi) / 1000.0

    def _maybe_start_reboot(self, now: float) -> bool:
        rb = self._cfg.reboot
        p = max(0.0, min(1.0, float(rb.probability)))
        if p <= 0.0:
            return False
        if self._rng.random() >= p:
            return False
        lo = max(0.0, float(rb.downtime_min_s))
        hi = max(lo, float(rb.downtime_max_s))
        self._forced_offline_until = now + self._rng.uniform(lo, hi)
        mode = str(rb.reset_state or "preserve").strip().lower()
        if mode == "off":
            self._set_actual_state(False)
        elif mode == "on":
            self._set_actual_state(True)
        self._record_trace({"event": "reboot", "forced_offline_until": self._forced_offline_until})
        return True

    def _set_actual_state(self, state: bool):
        self._actual_is_on = bool(state)
        self._history.append((time.monotonic(), self._actual_is_on))

    async def _apply_after(self, target_state: bool, apply_delay_s: float, command_id: int, duplicate: bool = False):
        try:
            if apply_delay_s > 0:
                await asyncio.sleep(apply_delay_s)
            self._set_actual_state(target_state)
            self._record_trace(
                {
                    "event": "apply",
                    "command_id": command_id,
                    "target_state": bool(target_state),
                    "duplicate": bool(duplicate),
                }
            )
        finally:
            self._pending_apply_tasks.discard(asyncio.current_task())

    async def _simulate_round_trip(self, *, is_write: bool):
        self._command_id += 1
        cid = self._command_id
        now = time.monotonic()
        self._advance_flap_state(now)
        rate_limited = self._is_rate_limited(now)
        self._maybe_start_reboot(now)

        net_s = self._sample_distribution_seconds(self._cfg.latency, rate_limited=rate_limited)
        if net_s > 0:
            await asyncio.sleep(net_s)

        self._advance_flap_state(time.monotonic())
        if (not self._effective_online(time.monotonic())) or self._should_drop(rate_limited=rate_limited):
            self._record_trace({"event": "drop", "command_id": cid, "op": "update"})
            if is_write:
                raise TimeoutError("simulated write timeout/drop")
            raise TimeoutError("simulated read timeout/drop")

        late_s = self._sample_late_response_s()
        if late_s > 0:
            await asyncio.sleep(late_s)
        self._record_trace({"event": "ack", "command_id": cid, "op": "update"})

    async def _simulate_write(self, *, target_state: bool):
        self._command_id += 1
        cid = self._command_id
        now = time.monotonic()
        self._advance_flap_state(now)
        rate_limited = self._is_rate_limited(now)
        self._maybe_start_reboot(now)

        net_s = self._sample_distribution_seconds(self._cfg.latency, rate_limited=rate_limited)
        if net_s > 0:
            await asyncio.sleep(net_s)

        self._advance_flap_state(time.monotonic())
        if (not self._effective_online(time.monotonic())) or self._should_drop(rate_limited=rate_limited):
            self._record_trace({"event": "drop", "command_id": cid, "op": "write", "target_state": bool(target_state)})
            raise TimeoutError("simulated write timeout/drop")

        apply_s = self._sample_distribution_seconds(self._cfg.apply_delay, rate_limited=rate_limited) + self._sample_reorder_extra_s()
        late_s = self._sample_late_response_s()
        duplicate = self._rng.random() < max(0.0, min(1.0, float(self._cfg.reorder.duplicate_apply_probability)))

        style = (self._cfg.ack.style or "apply").strip().lower()
        if style == "receipt":
            task = asyncio.create_task(self._apply_after(target_state, apply_s, cid, duplicate=False))
            self._pending_apply_tasks.add(task)
            if duplicate:
                dtask = asyncio.create_task(self._apply_after(target_state, apply_s, cid, duplicate=True))
                self._pending_apply_tasks.add(dtask)
            if late_s > 0:
                await asyncio.sleep(late_s)
            self._record_trace({"event": "ack", "command_id": cid, "op": "write", "ack_style": "receipt"})
            return

        if apply_s > 0:
            await asyncio.sleep(apply_s)
        self._set_actual_state(target_state)
        self._record_trace(
            {"event": "apply", "command_id": cid, "target_state": bool(target_state), "duplicate": False}
        )
        if duplicate:
            self._set_actual_state(target_state)
            self._record_trace(
                {"event": "apply", "command_id": cid, "target_state": bool(target_state), "duplicate": True}
            )
        if late_s > 0:
            await asyncio.sleep(late_s)
        self._record_trace({"event": "ack", "command_id": cid, "op": "write", "ack_style": "apply"})


class ScriptedKasaLight:
    """
    Deterministic player for scenario traces.
    """

    def __init__(self, host: str, script: list[dict], *, initial_is_on: bool = False):
        self.host = host
        self.modules = {}
        self._script = deque(script)
        self._actual_is_on = bool(initial_is_on)

    @property
    def is_on(self) -> bool:
        return self._actual_is_on

    @property
    def actual_is_on(self) -> bool:
        return self._actual_is_on

    async def update(self):
        await self._run("update", None)

    async def turn_on(self):
        await self._run("write", True)

    async def turn_off(self):
        await self._run("write", False)

    async def _run(self, expected_op: str, target_state: Optional[bool]):
        if not self._script:
            raise RuntimeError("script exhausted")
        step = self._script.popleft()
        if step.get("event") == "drop":
            if step.get("op") != expected_op:
                raise RuntimeError("script op mismatch")
            await asyncio.sleep(float(step.get("sleep_s", 0.0)))
            raise TimeoutError("scripted drop")
        if step.get("event") != "apply":
            raise RuntimeError("script step must be apply/drop")
        if expected_op != "write":
            raise RuntimeError("apply step invalid for update")
        if target_state is not None and bool(step.get("target_state")) != bool(target_state):
            raise RuntimeError("script target mismatch")
        await asyncio.sleep(float(step.get("sleep_s", 0.0)))
        self._actual_is_on = bool(step.get("target_state"))

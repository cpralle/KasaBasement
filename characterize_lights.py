#!/usr/bin/env python3
import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kasa import Discover


@dataclass
class DeviceTarget:
    alias: str
    host: Optional[str]
    mac: Optional[str]
    dev_type: Optional[str]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_alias(x: str) -> str:
    return (x or "").strip().lower()


def unique_in_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for i in items:
        k = normalize_alias(i)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out


def room_aliases(cfg: dict, room_name: str) -> list[str]:
    rooms = cfg.get("rooms", []) or []
    for room in rooms:
        if normalize_alias(room.get("name")) != normalize_alias(room_name):
            continue
        aliases = []
        for cell in (room.get("grid_map") or []):
            if isinstance(cell, dict):
                a = cell.get("device_alias")
                if a:
                    aliases.append(a)
        return unique_in_order(aliases)
    raise ValueError(f"Room not found: {room_name}")


def scene_aliases(cfg: dict, scene_name: str) -> list[str]:
    scenes = cfg.get("scenes", []) or []
    for scene in scenes:
        if normalize_alias(scene.get("name")) != normalize_alias(scene_name):
            continue
        aliases = []
        for action in (scene.get("actions") or []):
            if isinstance(action, dict):
                a = action.get("device_alias")
                if a:
                    aliases.append(a)
        return unique_in_order(aliases)
    raise ValueError(f"Scene not found: {scene_name}")


def build_targets(cfg: dict, aliases: list[str]) -> list[DeviceTarget]:
    devices = cfg.get("devices", []) or []
    by_alias = {normalize_alias(d.get("alias")): d for d in devices if d.get("alias")}
    out = []
    missing = []
    for alias in aliases:
        d = by_alias.get(normalize_alias(alias))
        if not d:
            missing.append(alias)
            continue
        out.append(
            DeviceTarget(
                alias=str(d.get("alias")),
                host=d.get("host"),
                mac=d.get("mac"),
                dev_type=d.get("type"),
            )
        )
    if missing:
        raise ValueError(f"Aliases missing from config devices: {missing}")
    return out


def percentile(vals: list[float], p: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    if len(v) == 1:
        return float(v[0])
    idx = (len(v) - 1) * max(0.0, min(1.0, p))
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return float(v[lo])
    frac = idx - lo
    return float(v[lo] * (1.0 - frac) + v[hi] * frac)


def stats(vals: list[float]) -> dict:
    if not vals:
        return {
            "count": 0,
            "mean_ms": None,
            "stdev_ms": None,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "min_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(vals),
        "mean_ms": float(statistics.mean(vals)),
        "stdev_ms": float(statistics.pstdev(vals)) if len(vals) > 1 else 0.0,
        "p50_ms": percentile(vals, 0.50),
        "p90_ms": percentile(vals, 0.90),
        "p95_ms": percentile(vals, 0.95),
        "p99_ms": percentile(vals, 0.99),
        "min_ms": float(min(vals)),
        "max_ms": float(max(vals)),
    }


async def discover_single(host: str, *, username: Optional[str], password: Optional[str], interface: Optional[str], timeout_s: float):
    kwargs = {}
    if interface:
        kwargs["interface"] = interface
    if username and password:
        kwargs["username"] = username
        kwargs["password"] = password
    try:
        return await asyncio.wait_for(Discover.discover_single(host, **kwargs), timeout=timeout_s)
    except TypeError:
        return await asyncio.wait_for(Discover.discover_single(host), timeout=timeout_s)


def read_is_on(device) -> Optional[bool]:
    try:
        v = getattr(device, "is_on", None)
        if isinstance(v, bool):
            return v
    except Exception:
        pass
    return None


class Runner:
    def __init__(self, args, targets: list[DeviceTarget]):
        self.args = args
        self.targets = targets
        self.device_cache: dict[str, object] = {}
        self.records: list[dict] = []
        self.initial_state: dict[str, Optional[bool]] = {}
        self.seq = 0
        self.start_mono = time.monotonic()
        self.start_iso = now_iso()
        self.username = os.getenv("KASA_USERNAME")
        self.password = os.getenv("KASA_PASSWORD")
        self.interface = os.getenv("KASA_INTERFACE")

    async def get_device(self, t: DeviceTarget):
        if t.alias in self.device_cache:
            return self.device_cache[t.alias]
        if not t.host:
            raise RuntimeError(f"{t.alias}: missing host in config")
        d = await discover_single(
            t.host,
            username=self.username,
            password=self.password,
            interface=self.interface,
            timeout_s=self.args.connect_timeout_s,
        )
        await asyncio.wait_for(d.update(), timeout=self.args.update_timeout_s)
        self.device_cache[t.alias] = d
        return d

    async def capture_initial_state(self):
        for t in self.targets:
            try:
                d = await self.get_device(t)
                await asyncio.wait_for(d.update(), timeout=self.args.update_timeout_s)
                self.initial_state[t.alias] = read_is_on(d)
            except Exception:
                self.initial_state[t.alias] = None

    async def restore_initial_state(self):
        for t in self.targets:
            target = self.initial_state.get(t.alias)
            if target is None:
                continue
            try:
                d = await self.get_device(t)
                t0 = time.monotonic()
                if target:
                    await asyncio.wait_for(d.turn_on(), timeout=self.args.command_timeout_s)
                else:
                    await asyncio.wait_for(d.turn_off(), timeout=self.args.command_timeout_s)
                _ = (time.monotonic() - t0) * 1000.0
            except Exception:
                pass

    async def issue(self, t: DeviceTarget, *, mode: str, target_on: bool) -> dict:
        self.seq += 1
        rec = {
            "seq": self.seq,
            "ts": now_iso(),
            "alias": t.alias,
            "host": t.host,
            "mode": mode,
            "target_on": bool(target_on),
            "ack_ok": False,
            "converged": False,
            "ack_latency_ms": None,
            "convergence_latency_ms": None,
            "error": None,
        }
        t0 = time.monotonic()
        try:
            d = await self.get_device(t)
            if target_on:
                await asyncio.wait_for(d.turn_on(), timeout=self.args.command_timeout_s)
            else:
                await asyncio.wait_for(d.turn_off(), timeout=self.args.command_timeout_s)
            ack_ms = (time.monotonic() - t0) * 1000.0
            rec["ack_ok"] = True
            rec["ack_latency_ms"] = ack_ms

            deadline = time.monotonic() + self.args.converge_timeout_s
            while time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(d.update(), timeout=self.args.update_timeout_s)
                except Exception:
                    await asyncio.sleep(self.args.poll_interval_s)
                    continue
                is_on = read_is_on(d)
                if is_on is not None and bool(is_on) == bool(target_on):
                    rec["converged"] = True
                    rec["convergence_latency_ms"] = (time.monotonic() - t0) * 1000.0
                    break
                await asyncio.sleep(self.args.poll_interval_s)

            if not rec["converged"]:
                rec["error"] = "convergence_timeout"
        except Exception as e:
            rec["error"] = str(e)
            self.device_cache.pop(t.alias, None)
        self.records.append(rec)
        return rec

    async def run_mode_once(self, mode: str, target_on: bool):
        if mode == "single":
            for t in self.targets:
                await self.issue(t, mode=mode, target_on=target_on)
                await asyncio.sleep(self.args.between_commands_s)
            return
        if mode == "group-sequential":
            for t in self.targets:
                await self.issue(t, mode=mode, target_on=target_on)
            return
        if mode == "group-parallel":
            await asyncio.gather(*(self.issue(t, mode=mode, target_on=target_on) for t in self.targets))
            return
        raise ValueError(f"Unknown mode: {mode}")

    def should_stop(self, rounds_done: int) -> bool:
        if self.args.rounds and rounds_done >= self.args.rounds:
            return True
        if self.args.duration_s and (time.monotonic() - self.start_mono) >= self.args.duration_s:
            return True
        return False


def summarize(records: list[dict]) -> dict:
    by_alias = {}
    for r in records:
        by_alias.setdefault(r["alias"], []).append(r)

    def summarize_records(rs: list[dict]) -> dict:
        ack_ok = [x for x in rs if x["ack_ok"]]
        conv_ok = [x for x in rs if x["converged"]]
        ack_vals = [float(x["ack_latency_ms"]) for x in ack_ok if x["ack_latency_ms"] is not None]
        conv_vals = [float(x["convergence_latency_ms"]) for x in conv_ok if x["convergence_latency_ms"] is not None]
        lag_vals = []
        for x in conv_ok:
            if x["ack_latency_ms"] is None or x["convergence_latency_ms"] is None:
                continue
            lag_vals.append(max(0.0, float(x["convergence_latency_ms"]) - float(x["ack_latency_ms"])))

        fails = [0 if x["ack_ok"] else 1 for x in rs]
        bursts = []
        cur = 0
        for f in fails:
            if f:
                cur += 1
            else:
                if cur:
                    bursts.append(cur)
                cur = 0
        if cur:
            bursts.append(cur)

        attempts = len(rs)
        ack_success = len(ack_ok)
        return {
            "attempts": attempts,
            "ack_success": ack_success,
            "ack_failure": attempts - ack_success,
            "ack_success_rate": (ack_success / attempts) if attempts else None,
            "loss_probability": ((attempts - ack_success) / attempts) if attempts else None,
            "ack_latency": stats(ack_vals),
            "convergence_latency": stats(conv_vals),
            "stale_read_lag_ms": stats(lag_vals),
            "failure_burst_count": len(bursts),
            "failure_burst_max": max(bursts) if bursts else 0,
            "failure_burst_mean": float(statistics.mean(bursts)) if bursts else 0.0,
        }

    overall = summarize_records(records)
    per_alias = {a: summarize_records(rs) for a, rs in by_alias.items()}

    p50 = overall["ack_latency"]["p50_ms"]
    p90 = overall["ack_latency"]["p90_ms"]
    p95 = overall["ack_latency"]["p95_ms"]
    p99 = overall["ack_latency"]["p99_ms"]
    c50 = overall["convergence_latency"]["p50_ms"]
    c95 = overall["convergence_latency"]["p95_ms"]
    lag95 = overall["stale_read_lag_ms"]["p95_ms"]

    suggested = {
        "latency": {
            "base_ms": p50,
            "jitter_ms": (p90 - p50) if (p90 is not None and p50 is not None) else None,
            "spike_probability": 0.01 if p99 and p95 and p99 > p95 else 0.0,
            "spike_min_ms": (p95 - p50) if (p95 is not None and p50 is not None) else None,
            "spike_max_ms": (p99 - p50) if (p99 is not None and p50 is not None) else None,
        },
        "apply_delay": {
            "base_ms": max(0.0, (c50 - p50)) if (c50 is not None and p50 is not None) else None,
            "jitter_ms": max(0.0, ((c95 - p95) if (c95 is not None and p95 is not None) else 0.0)),
        },
        "loss": {
            "loss_probability": overall["loss_probability"],
            "burst_max": overall["failure_burst_max"],
            "burst_mean": overall["failure_burst_mean"],
        },
        "stale_read_lag_s": (lag95 / 1000.0) if lag95 is not None else None,
    }

    return {
        "overall": overall,
        "per_alias": per_alias,
        "suggested_simulator_profile": suggested,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Characterize physical Kasa light behavior for simulator tuning.")
    p.add_argument("--config", default="config.json", help="Path to config.json")
    p.add_argument("--room", default="", help="Target only devices in this room name")
    p.add_argument("--scene", default="", help="Target only devices in this scene name")
    p.add_argument("--aliases", default="", help="Comma-separated alias list")
    p.add_argument("--mode", default="single,group-sequential,group-parallel", help="Comma-separated modes")
    p.add_argument("--rounds", type=int, default=0, help="Number of full mode rounds (0 means unlimited by rounds)")
    p.add_argument("--duration-s", type=int, default=0, help="Run duration seconds (0 disables duration cap)")
    p.add_argument("--idle-seconds", type=float, default=0.0, help="Sleep inserted after each round")
    p.add_argument("--between-commands-s", type=float, default=0.1, help="Gap between single-mode device commands")
    p.add_argument("--command-timeout-s", type=float, default=4.0, help="Timeout for turn_on/turn_off")
    p.add_argument("--update-timeout-s", type=float, default=2.0, help="Timeout for device.update()")
    p.add_argument("--connect-timeout-s", type=float, default=2.0, help="Timeout for initial connect")
    p.add_argument("--converge-timeout-s", type=float, default=8.0, help="Timeout to observe target state")
    p.add_argument("--poll-interval-s", type=float, default=0.15, help="Polling interval for convergence")
    p.add_argument("--restore-initial-state", action="store_true", help="Restore original on/off state when done")
    p.add_argument("--output-dir", default="characterization_output", help="Directory for output files")
    p.add_argument("--confirm-live", action="store_true", help="Required safety flag to actually send commands")
    return p.parse_args()


def pick_aliases(cfg: dict, args) -> list[str]:
    if args.aliases:
        return unique_in_order([x.strip() for x in args.aliases.split(",") if x.strip()])
    if args.room:
        return room_aliases(cfg, args.room)
    if args.scene:
        return scene_aliases(cfg, args.scene)
    return unique_in_order([d.get("alias", "") for d in (cfg.get("devices") or []) if d.get("alias")])


async def async_main():
    args = parse_args()
    if not args.confirm_live:
        print("Safety stop: this script controls real lights.")
        print("Re-run with --confirm-live to proceed.")
        return 2

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    cfg = read_json(cfg_path)
    aliases = pick_aliases(cfg, args)
    targets = build_targets(cfg, aliases)

    modes = [m.strip() for m in args.mode.split(",") if m.strip()]
    valid_modes = {"single", "group-sequential", "group-parallel"}
    bad = [m for m in modes if m not in valid_modes]
    if bad:
        raise ValueError(f"Invalid modes: {bad}; valid={sorted(valid_modes)}")
    if not targets:
        raise ValueError("No target devices selected")

    print(f"Targets ({len(targets)}): {[t.alias for t in targets]}")
    print(f"Modes: {modes}")
    print(f"Rounds: {args.rounds}  Duration cap (s): {args.duration_s}")
    if args.rounds and args.duration_s:
        print("Note: run stops when either rounds or duration cap is reached.")

    runner = Runner(args, targets)
    captured_initial = False
    interrupted = False
    round_idx = 0

    try:
        if args.restore_initial_state:
            print("Capturing initial device state...")
            await runner.capture_initial_state()
            captured_initial = True

        while True:
            if runner.should_stop(round_idx):
                break
            target_on = (round_idx % 2 == 0)
            print(f"[{now_iso()}] Round {round_idx + 1} target_on={target_on}")
            for mode in modes:
                if runner.should_stop(round_idx):
                    break
                print(f"  mode={mode}")
                await runner.run_mode_once(mode, target_on=target_on)
            round_idx += 1
            if args.idle_seconds > 0:
                await asyncio.sleep(args.idle_seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        print("Interrupted: writing partial results and cleaning up...")
    finally:
        if args.restore_initial_state and captured_initial:
            print("Restoring initial state...")
            try:
                await runner.restore_initial_state()
            except Exception as e:
                print(f"Warning: failed to restore initial state: {e}")

        summary = summarize(runner.records)
        summary_meta = {
            "started_at": runner.start_iso,
            "finished_at": now_iso(),
            "duration_s": time.monotonic() - runner.start_mono,
            "targets": [t.alias for t in targets],
            "modes": modes,
            "args": vars(args),
            "interrupted": interrupted,
            "rounds_completed": round_idx,
        }
        out = {"meta": summary_meta, "summary": summary, "records": runner.records}

        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_path = out_dir / f"characterization_{stamp}.json"
        summary_path = out_dir / f"characterization_summary_{stamp}.json"

        with full_path.open("w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump({"meta": summary_meta, "summary": summary}, f, indent=2)

        print(f"Wrote full output: {full_path}")
        print(f"Wrote summary: {summary_path}")
        print("Overall loss probability:", summary["overall"]["loss_probability"])

    return 130 if interrupted else 0


def main():
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

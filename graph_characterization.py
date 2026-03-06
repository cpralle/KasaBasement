#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Optional


def parse_args():
    p = argparse.ArgumentParser(
        description="Graph Kasa light characterization output (full or summary JSON)."
    )
    p.add_argument(
        "--input",
        default="",
        help="Path to characterization JSON. If omitted, latest in --input-dir is used.",
    )
    p.add_argument(
        "--input-dir",
        default="characterization_output",
        help="Directory to search for latest characterization file when --input is omitted.",
    )
    p.add_argument(
        "--output-dir",
        default="characterization_output/graphs",
        help="Directory to write PNG charts.",
    )
    p.add_argument(
        "--prefix",
        default="",
        help="Optional output filename prefix (defaults to input filename stem).",
    )
    return p.parse_args()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def pick_latest(input_dir: Path) -> Path:
    candidates = sorted(input_dir.glob("characterization*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"No characterization JSON files found in {input_dir}")
    return candidates[-1]


def safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def split_payload(payload: dict):
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    records = payload.get("records", []) if isinstance(payload, dict) else []
    if summary:
        return meta, summary, records
    # If caller provides a non-standard flat shape, try best effort.
    if "overall" in payload and "per_alias" in payload:
        return meta, payload, records
    raise ValueError("Input JSON does not look like characterization output")


def require_matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception as e:
        raise RuntimeError(
            "matplotlib is required for graphing. Install with: "
            "python -m pip install matplotlib"
        ) from e


def build_overall_latency_plot(plt, overall: dict, out: Path):
    labels = ["ack p50", "ack p90", "ack p95", "ack p99", "conv p50", "conv p95", "conv p99"]
    vals = [
        safe_float(overall.get("ack_latency", {}).get("p50_ms")),
        safe_float(overall.get("ack_latency", {}).get("p90_ms")),
        safe_float(overall.get("ack_latency", {}).get("p95_ms")),
        safe_float(overall.get("ack_latency", {}).get("p99_ms")),
        safe_float(overall.get("convergence_latency", {}).get("p50_ms")),
        safe_float(overall.get("convergence_latency", {}).get("p95_ms")),
        safe_float(overall.get("convergence_latency", {}).get("p99_ms")),
    ]
    x = list(range(len(labels)))
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(x, [v if v is not None else 0.0 for v in vals], color="#3B82F6")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("Milliseconds")
    ax.set_title("Overall Latency Percentiles")
    for i, v in enumerate(vals):
        if v is not None:
            ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def build_per_alias_latency_plot(plt, per_alias: dict, out: Path):
    aliases = sorted(per_alias.keys())
    p95 = []
    for a in aliases:
        p95.append(safe_float((per_alias.get(a) or {}).get("ack_latency", {}).get("p95_ms")) or 0.0)

    fig, ax = plt.subplots(figsize=(max(10, len(aliases) * 0.45), 5))
    ax.bar(range(len(aliases)), p95, color="#10B981")
    ax.set_xticks(range(len(aliases)))
    ax.set_xticklabels(aliases, rotation=55, ha="right")
    ax.set_ylabel("Milliseconds")
    ax.set_title("Per-Device Ack p95 Latency")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def build_per_alias_success_plot(plt, per_alias: dict, out: Path):
    aliases = sorted(per_alias.keys())
    rates = []
    for a in aliases:
        v = safe_float((per_alias.get(a) or {}).get("ack_success_rate"))
        rates.append((100.0 * v) if v is not None else 0.0)

    fig, ax = plt.subplots(figsize=(max(10, len(aliases) * 0.45), 5))
    ax.bar(range(len(aliases)), rates, color="#F59E0B")
    ax.set_ylim(0, 100)
    ax.set_xticks(range(len(aliases)))
    ax.set_xticklabels(aliases, rotation=55, ha="right")
    ax.set_ylabel("Ack Success Rate (%)")
    ax.set_title("Per-Device Ack Success")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def build_ack_histogram(plt, records: list[dict], out: Path):
    vals = [safe_float(r.get("ack_latency_ms")) for r in records if r.get("ack_ok")]
    vals = [v for v in vals if v is not None]
    if not vals:
        return False
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.hist(vals, bins=60, color="#6366F1", alpha=0.85)
    ax.set_xlabel("Ack Latency (ms)")
    ax.set_ylabel("Count")
    ax.set_title("Ack Latency Distribution")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def build_time_series(plt, records: list[dict], out: Path):
    points = []
    t0 = None
    for r in records:
        if not r.get("ack_ok"):
            continue
        ts = r.get("ts")
        v = safe_float(r.get("ack_latency_ms"))
        if not ts or v is None:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if t0 is None:
            t0 = dt
        sec = (dt - t0).total_seconds()
        points.append((sec, v))
    if not points:
        return False

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.scatter([p[0] for p in points], [p[1] for p in points], s=6, alpha=0.6, color="#EF4444")
    ax.set_xlabel("Seconds Since Start")
    ax.set_ylabel("Ack Latency (ms)")
    ax.set_title("Ack Latency Over Time")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def main():
    args = parse_args()
    in_path = Path(args.input) if args.input else pick_latest(Path(args.input_dir))
    payload = load_json(in_path)
    meta, summary, records = split_payload(payload)

    overall = summary.get("overall", {})
    per_alias = summary.get("per_alias", {})
    if not overall or not per_alias:
        raise ValueError("Summary missing overall/per_alias fields")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix.strip() or in_path.stem

    plt = require_matplotlib()
    outputs = []

    p = out_dir / f"{prefix}_overall_latency.png"
    build_overall_latency_plot(plt, overall, p)
    outputs.append(p)

    p = out_dir / f"{prefix}_per_alias_ack_p95.png"
    build_per_alias_latency_plot(plt, per_alias, p)
    outputs.append(p)

    p = out_dir / f"{prefix}_per_alias_ack_success.png"
    build_per_alias_success_plot(plt, per_alias, p)
    outputs.append(p)

    if records:
        p = out_dir / f"{prefix}_ack_hist.png"
        if build_ack_histogram(plt, records, p):
            outputs.append(p)
        p = out_dir / f"{prefix}_ack_timeseries.png"
        if build_time_series(plt, records, p):
            outputs.append(p)

    print(f"Input: {in_path}")
    if meta:
        print("Run window:", meta.get("started_at"), "->", meta.get("finished_at"))
    print("Wrote graphs:")
    for p in outputs:
        print(f"  {p}")


if __name__ == "__main__":
    main()


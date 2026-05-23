"""Launch a batch of dry-run bots with different configs, log each to its own
file. Used during the 11-hour autonomous sweep.

Each "trial" runs in background; the launcher only kicks them off.

Usage:
    python scripts/sweep_runner.py wave1
    python scripts/sweep_runner.py status
    python scripts/sweep_runner.py kill_all
"""
import os, sys, subprocess, time, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
LOGS = ROOT / "logs"

COMMON = [
    "python3", "-u", "dryrun_bot_v3.py",
    "--max-time-out-sec", "50",
    "--min-mult", "1.2", "--max-mult", "50",
    "--stake", "1", "--max-loss", "9999", "--max-taps", "9999",
    "--cadence-sec", "8", "--max-quote-age-ms", "700",
]

WAVES: dict[str, list[dict]] = {
    # TOP-tier bots: run for the full 11h, never killed by waves.
    "top": [
        {"tag": "TOP_v7_R1F4",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--max-sigma", "0.15", "--bandit-window", "6",
                  "--bandit-floor", "0.15", "--bandit-pause-sec", "300",
                  "--signal-source", "coinbase"]},
        {"tag": "TOP_c30r_pure",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "TOP_c30s_orig",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.40",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "TOP_meanrev_R1",
         "args": ["--strategy", "c30-strong-meanrev", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--max-sigma", "0.15", "--signal-source", "coinbase"]},
        {"tag": "TOP_combo_all",
         "args": ["--strategy", "combo", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--c-thr", "0.10", "--c-tau-sec", "90",
                  "--signal-source", "coinbase", "--max-sigma", "0.15"]},
    ],
    "wave1": [
        # Baseline current bot (no filters, no calibrator)
        {"tag": "W1_baseline_c30r",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        # Counter-trend variant (bets AGAINST drift)
        {"tag": "W1_meanrev",
         "args": ["--strategy", "c30-strong-meanrev", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        # Same as baseline but EU signal source
        {"tag": "W1_eu_signal",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "euphoria"]},
        # R1 σ-ceiling only
        {"tag": "W1_R1_only",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--max-sigma", "0.15"]},
        # R1 + F4 + Calibrator2 (current "best stack" hypothesis)
        {"tag": "W1_R1_F4_cal2",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--max-sigma", "0.15",
                  "--bandit-window", "6", "--bandit-floor", "0.15",
                  "--bandit-pause-sec", "300",
                  "--calibrator2", "--cal2-blend", "0.4"]},
        # Wider quote age (test MM-staleness hypothesis)
        {"tag": "W1_quote_drop",
         "args": ["--strategy", "c30-strong-quotedrop", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--min-quote-age-ms", "800"]},
    ],
    "wave2": [
        # Parameter sweep on c-thr and tau
        {"tag": "W2_thr015_tau30",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.15",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "W2_thr020_tau30",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.20",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "W2_thr030_tau30",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.30",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "W2_thr025_tau15",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "15", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "W2_thr025_tau60",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "60", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "W2_thr025_vol60",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "60",
                  "--signal-source", "coinbase"]},
    ],
    "wave3": [
        # Filter combinations
        {"tag": "W3_F1k5", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                     "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                     "--signal-source", "coinbase", "--trend-gate-k", "5"]},
        {"tag": "W3_F4k4", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                     "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                     "--signal-source", "coinbase", "--bandit-window", "4",
                                     "--bandit-floor", "0.15", "--bandit-pause-sec", "180"]},
        {"tag": "W3_F4k8", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                     "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                     "--signal-source", "coinbase", "--bandit-window", "8",
                                     "--bandit-floor", "0.15", "--bandit-pause-sec", "300"]},
        {"tag": "W3_R1_strict", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                          "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                          "--signal-source", "coinbase", "--max-sigma", "0.12"]},
        {"tag": "W3_R3_drift",  "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                          "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                          "--signal-source", "coinbase", "--max-abs-drift", "0.50"]},
        {"tag": "W3_cal2_only", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                          "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                          "--signal-source", "coinbase", "--calibrator2"]},
    ],
    # Different strategy TYPES (not c30-strong)
    "wave4": [
        {"tag": "W4_strat_prob", "args": ["--strategy", "prob",
                                           "--vol-window-sec", "30",
                                           "--signal-source", "coinbase",
                                           "--min-p", "0.30", "--max-p", "0.60"]},
        {"tag": "W4_strat_c90", "args": ["--strategy", "c90", "--c-thr", "0.10",
                                          "--c-tau-sec", "90", "--vol-window-sec", "30",
                                          "--signal-source", "coinbase"]},
        {"tag": "W4_strat_edge_b", "args": ["--strategy", "b",
                                             "--vol-window-sec", "30",
                                             "--b-vol-threshold", "0.10",
                                             "--b-mult-min", "2.0", "--b-mult-max", "5.0",
                                             "--b-target-mult", "3.0",
                                             "--signal-source", "coinbase"]},
        {"tag": "W4_c30s_meanrev", "args": ["--strategy", "c30-strong-meanrev",
                                             "--c-thr-strong", "0.40",
                                             "--c-tau-strong-sec", "30",
                                             "--vol-window-sec", "30",
                                             "--signal-source", "coinbase",
                                             "--max-sigma", "0.15"]},
        {"tag": "W4_heston", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                       "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                       "--signal-source", "coinbase",
                                       "--vol-model", "heston",
                                       "--heston-paths", "200"]},
        {"tag": "W4_studentt", "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                                         "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                                         "--signal-source", "coinbase",
                                         "--vol-model", "t", "--vol-df", "4.5"]},
    ],

    # ----- RS wave: RedStone-resolved bake-off of v9, v9 variants, and
    # previous top configs. All resolve against RS oracle (in addition to
    # EU and CB) for clean LIVE-equivalent ranking.
    "rs": [
        # ---- v9 family (drift-aware) ----
        {"tag": "RS_v9_c30_drift",
         "args": ["--strategy", "c30-strong", "--signal-source", "coinbase",
                  "--calibrator2", "--drift-aware-touch", "--drift-conv-sec", "2.0"]},
        {"tag": "RS_v9_c30_drift_slow",
         "args": ["--strategy", "c30-strong", "--signal-source", "coinbase",
                  "--calibrator2", "--drift-aware-touch", "--drift-conv-sec", "4.0"]},
        {"tag": "RS_v9_c30_drift_fast",
         "args": ["--strategy", "c30-strong", "--signal-source", "coinbase",
                  "--calibrator2", "--drift-aware-touch", "--drift-conv-sec", "1.0"]},
        # ---- v8 family (regime switcher) ----
        {"tag": "RS_v8_baseline",
         "args": ["--strategy", "c30-strong", "--signal-source", "coinbase",
                  "--calibrator2"]},
        {"tag": "RS_v8_switcher",
         "args": ["--strategy", "c30-strong", "--signal-source", "coinbase",
                  "--calibrator2", "--regime-switch", "--rs-start-mode", "HOT"]},
        # ---- LIVE-validated configs from prior runs ----
        {"tag": "RS_LIVE_v7_R1F4k6",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--max-sigma", "0.15", "--bandit-window", "6",
                  "--bandit-floor", "0.15", "--bandit-pause-sec", "300",
                  "--calibrator2"]},
        {"tag": "RS_LIVE_c30r_vw30",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--roll-correction"]},
        # ---- HOUR_FINAL top 15 (deduped) ----
        {"tag": "RS_baseline_c30r",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "RS_R1_strict",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--calibrator2", "--max-sigma", "0.12"]},
        {"tag": "RS_cal2_only",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--calibrator2"]},
        {"tag": "RS_R1_only",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--max-sigma", "0.15"]},
        {"tag": "RS_F4k4",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--calibrator2",
                  "--bandit-window", "4", "--bandit-floor", "0.15"]},
        {"tag": "RS_R3_drift",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--calibrator2", "--max-abs-drift", "0.50"]},
        {"tag": "RS_studentt",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--vol-model", "t", "--vol-df", "4.5"]},
        {"tag": "RS_F4k8",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--calibrator2",
                  "--bandit-window", "8", "--bandit-floor", "0.15"]},
        {"tag": "RS_heston",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--vol-model", "heston", "--heston-paths", "200"]},
        {"tag": "RS_R1_F4_cal2",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--calibrator2", "--max-sigma", "0.15", "--bandit-window", "6",
                  "--bandit-floor", "0.15", "--bandit-pause-sec", "300"]},
        {"tag": "RS_strat_c90",
         "args": ["--strategy", "c90", "--c-thr", "0.10", "--c-tau-sec", "90",
                  "--signal-source", "coinbase", "--calibrator2"]},
    ],

    # ----- Sustainable RS wave: top 6 backtest configs + v9. Bot RedStone
    # feed default is now 2000ms; 7 bots × 0.5 req/s = 3.5 req/s at RPC ceiling.
    "rs_sus": [
        {"tag": "RSUS_baseline_c30r",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase"]},
        {"tag": "RSUS_R1_only",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--max-sigma", "0.15"]},
        {"tag": "RSUS_F4k4",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase", "--calibrator2",
                  "--bandit-window", "4", "--bandit-floor", "0.15"]},
        {"tag": "RSUS_R3_drift",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--calibrator2", "--max-abs-drift", "0.50"]},
        {"tag": "RSUS_studentt",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--vol-model", "t", "--vol-df", "4.5"]},
        {"tag": "RSUS_R1_strict",
         "args": ["--strategy", "c30-strong", "--c-thr-strong", "0.25",
                  "--c-tau-strong-sec", "30", "--vol-window-sec", "30",
                  "--signal-source", "coinbase",
                  "--calibrator2", "--max-sigma", "0.12"]},
        {"tag": "RSUS_v9_drift",
         "args": ["--strategy", "c30-strong", "--signal-source", "coinbase",
                  "--calibrator2", "--drift-aware-touch", "--drift-conv-sec", "2.0"]},
    ],
}


def launch_wave(wave: str) -> list[dict]:
    if wave not in WAVES:
        print(f"unknown wave: {wave}", file=sys.stderr); sys.exit(1)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = []
    for trial in WAVES[wave]:
        tag = trial["tag"]
        log_path = LOGS / f"{tag}_{ts}.log"
        cmd = COMMON + ["--tag", tag] + trial["args"]
        with open(log_path, "w") as f:
            p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        out.append({"tag": tag, "pid": p.pid, "log": str(log_path), "cmd": cmd})
        print(f"launched {tag} pid={p.pid} log={log_path.name}")
        time.sleep(0.5)  # stagger to avoid proxy thundering-herd
    # Save manifest
    manifest = LOGS / f"sweep_manifest_{wave}_{ts}.json"
    with open(manifest, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nmanifest: {manifest}")
    return out


def status() -> None:
    """List running dryrun_bot_v3 processes via ps."""
    out = subprocess.run(["ps", "-Ao", "pid,command"], capture_output=True, text=True)
    procs = []
    for line in out.stdout.splitlines():
        if "dryrun_bot_v3.py" not in line:
            continue
        if "grep" in line or "/bin/sh" in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, cmd = parts
        tag = "?"
        cmd_parts = cmd.split()
        if "--tag" in cmd_parts:
            idx = cmd_parts.index("--tag")
            if idx + 1 < len(cmd_parts):
                tag = cmd_parts[idx + 1]
        procs.append((pid, tag))
    print(f"Running dryrun_bot_v3 processes: {len(procs)}")
    for pid, tag in procs:
        print(f"  pid={pid:>6} tag={tag}")


def kill_all() -> None:
    """Kill all dry-run bots (does NOT touch --live)."""
    out = subprocess.run(["ps", "-Ao", "pid,command"], capture_output=True, text=True)
    n = 0
    for line in out.stdout.splitlines():
        if "dryrun_bot_v3.py" not in line or "--live" in line:
            continue
        if "grep" in line or "/bin/sh" in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid = parts[0]
        try:
            subprocess.run(["kill", pid], check=True)
            print(f"  killed pid={pid}")
            n += 1
        except Exception as e:
            print(f"  fail pid={pid}: {e}")
    print(f"\nkilled {n} dry-run bots")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    op = sys.argv[1]
    if op == "status":
        status()
    elif op == "kill_all":
        kill_all()
    elif op in WAVES:
        launch_wave(op)
    else:
        print(f"unknown op: {op}", file=sys.stderr); sys.exit(1)

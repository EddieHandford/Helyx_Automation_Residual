# Helyx Residual Monitor

A simple GUI tool that monitors a running Helyx/OpenFOAM solver and automatically stops it when velocity residuals drop below a user-defined threshold.

## Quick Start

1. Run `helyx_monitor.py` (or the built `.exe`)
2. Browse to your Helyx case directory — the log file is auto-detected
3. Set your residual threshold (e.g. `1e-4`)
4. Tick which variables to monitor (Ux, Uy, Uz)
5. Click **Start Monitoring**

The tool watches the solver log in real time. When all selected residuals drop below the threshold, it modifies `system/controlDict` to `stopAt writeNow`, cleanly stopping the solver. A backup of the original `controlDict` is saved automatically.

## Requirements

- Python 3.9+ (tkinter is included with standard Python on Windows)
- No third-party dependencies

## Building a Standalone .exe (Windows)

```
pip install pyinstaller
build_windows.bat
```

The executable will be at `dist/HelyxResidualMonitor.exe`.

## How It Works

- Tails the solver log file, parsing OpenFOAM-format residual lines
- Uses **Initial residual** (the standard CFD convergence metric)
- When the threshold is met, patches `controlDict` with `stopAt writeNow` — the solver stops cleanly at the end of the current timestep
- A timestamped backup (`controlDict.bak_YYYYMMDD_HHMMSS`) is always created before any modification

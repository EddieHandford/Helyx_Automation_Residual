"""
Helyx Residual Monitor
A standalone GUI tool that monitors a running Helyx/OpenFOAM solver
and automatically stops it when velocity residuals drop below a threshold.
"""

import re
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime

try:
    import winsound
except ImportError:
    winsound = None  # Non-Windows fallback — audio alert silently disabled

# ---------------------------------------------------------------------------
# Constants & Regex
# ---------------------------------------------------------------------------

RESIDUAL_PATTERN = re.compile(
    r'Solving for (?P<var>\w+),\s*Initial residual\s*=\s*(?P<initial>[0-9eE+\-\.]+),\s*'
    r'Final residual\s*=\s*(?P<final>[0-9eE+\-\.]+)'
)

STOP_AT_PATTERN = re.compile(r'(stopAt\s+)\w+(\s*;)', re.MULTILINE)
TIME_PATTERN    = re.compile(r'^Time\s*=\s*(?P<time>[0-9eE+\-\.]+)', re.MULTILINE)

DEFAULT_VARIABLES       = ['Ux', 'Uy', 'Uz']
POLL_INTERVAL_MS        = 500
LOG_TAIL_CHUNK          = 8192
NO_DATA_WARNING_SECONDS = 30

THEME_LIGHT = {
    "bg":       "#f0f0f0",
    "fg":       "#000000",
    "entry_bg": "#ffffff",
    "green":    "green",
    "red":      "red",
    "gray":     "gray",
}
THEME_DARK = {
    "bg":       "#1e1e1e",
    "fg":       "#e0e0e0",
    "entry_bg": "#2d2d2d",
    "green":    "#4ec94e",
    "red":      "#e05c5c",
    "gray":     "#888888",
}

# ---------------------------------------------------------------------------
# Backend: ResidualMonitor
# ---------------------------------------------------------------------------

class ResidualMonitor:
    """Monitors a solver log file in a background thread and patches
    controlDict when residuals drop below threshold."""

    def __init__(self, case_dir, log_path, threshold, variables, on_update, on_trigger, on_status):
        self.case_dir     = Path(case_dir)
        self.log_path     = Path(log_path)
        self.threshold    = threshold
        self.variables    = list(variables)
        self.on_update    = on_update
        self.on_trigger   = on_trigger
        self.on_status    = on_status
        self._stop_event  = threading.Event()
        self._latest      = {}
        self._current_time = None
        self._thread      = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        # Wait for log file to appear
        while not self.log_path.exists():
            if self._stop_event.is_set():
                return
            self.on_status("Waiting for log file...")
            time.sleep(1.0)

        self.on_status("Monitoring...")

        first_data_time = None
        has_seen_data   = False
        line_buffer     = ""

        with open(self.log_path, 'rb') as f:
            # Seek to end — we only care about new output
            f.seek(0, 2)

            while not self._stop_event.is_set():
                chunk = f.read(LOG_TAIL_CHUNK)
                if not chunk:
                    # No new data — track how long we've been waiting
                    if not has_seen_data:
                        if first_data_time is None:
                            first_data_time = time.time()
                        elif time.time() - first_data_time > NO_DATA_WARNING_SECONDS:
                            self.on_status("Warning: no residuals detected — is the solver running?")
                            first_data_time = time.time()  # reset so warning repeats
                    time.sleep(POLL_INTERVAL_MS / 1000.0)
                    continue

                # Handle potential file truncation / rotation
                try:
                    current_pos = f.tell()
                    file_size   = os.path.getsize(self.log_path)
                    if current_pos > file_size:
                        f.seek(0)
                        continue
                except OSError:
                    pass

                text        = chunk.decode('utf-8', errors='replace')
                line_buffer += text
                lines        = line_buffer.split('\n')
                line_buffer  = lines[-1]  # keep incomplete last line

                for line in lines[:-1]:
                    # Parse Time / iteration counter
                    time_match = TIME_PATTERN.search(line)
                    if time_match:
                        self._current_time = time_match.group('time')

                    # Parse residuals
                    match = RESIDUAL_PATTERN.search(line)
                    if match and match.group('var') in self.variables:
                        has_seen_data = True
                        var = match.group('var')
                        try:
                            val = float(match.group('initial'))
                            self._latest[var] = val
                        except ValueError:
                            pass

                if self._latest:
                    self.on_update(dict(self._latest), self._current_time)

                # Check convergence: all monitored variables must be present and below threshold
                if (len(self._latest) >= len(self.variables)
                        and all(self._latest.get(v, float('inf')) < self.threshold
                                for v in self.variables)):
                    self._patch_control_dict()
                    self.on_trigger()
                    return

    def _patch_control_dict(self):
        control_dict = self.case_dir / 'system' / 'controlDict'

        try:
            content = control_dict.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as e:
            self.on_status(f"Error reading controlDict: {e}")
            return

        # Write backup
        timestamp   = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = control_dict.with_name(f'controlDict.bak_{timestamp}')
        try:
            backup_path.write_text(content, encoding='utf-8')
        except OSError as e:
            self.on_status(f"Error writing backup: {e}")
            return

        # Patch stopAt
        if STOP_AT_PATTERN.search(content):
            new_content = STOP_AT_PATTERN.sub(r'\1writeNow\2', content)
        else:
            # Insert stopAt before the final closing brace
            last_brace = content.rfind('}')
            if last_brace != -1:
                new_content = content[:last_brace] + '    stopAt          writeNow;\n' + content[last_brace:]
            else:
                self.on_status("Error: could not find closing brace in controlDict")
                return

        # Atomic write: write to temp, then replace
        tmp_path = control_dict.with_name('controlDict.tmp')
        try:
            tmp_path.write_text(new_content, encoding='utf-8')
            os.replace(str(tmp_path), str(control_dict))
        except PermissionError:
            self.on_status("Error: could not write to controlDict — check file permissions")
            return
        except OSError as e:
            self.on_status(f"Error patching controlDict: {e}")
            return


# ---------------------------------------------------------------------------
# GUI: App
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("HELYX Residual Monitor")
        self.root.resizable(False, False)
        self.monitor     = None
        self._theme      = THEME_LIGHT

        # --- Variables ---
        self.case_dir_var    = tk.StringVar()
        self.log_file_var    = tk.StringVar()
        self.threshold_var   = tk.StringVar(value="1e-4")
        self.status_var      = tk.StringVar(value="Idle")
        self.audio_alert_var = tk.BooleanVar(value=True)
        self.dark_mode_var   = tk.BooleanVar(value=False)
        self.monitor_vars    = {}
        for v in DEFAULT_VARIABLES:
            self.monitor_vars[v] = tk.BooleanVar(value=True)
        self.res_labels  = {}
        self.time_label  = None

        self._build_ui()

    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        # --- Input frame ---
        input_frame = ttk.LabelFrame(self.root, text="Configuration", padding=10)
        input_frame.grid(row=0, column=0, sticky='ew', **pad)

        # Case directory
        ttk.Label(input_frame, text="Case Directory:").grid(row=0, column=0, sticky='w')
        ttk.Entry(input_frame, textvariable=self.case_dir_var, width=45).grid(row=0, column=1, sticky='ew', padx=(4, 0))
        ttk.Button(input_frame, text="Browse", command=self._browse_case_dir).grid(row=0, column=2, padx=(4, 0))

        # Log file
        ttk.Label(input_frame, text="Log File:").grid(row=1, column=0, sticky='w', pady=(4, 0))
        ttk.Entry(input_frame, textvariable=self.log_file_var, width=45).grid(row=1, column=1, sticky='ew', padx=(4, 0), pady=(4, 0))
        ttk.Button(input_frame, text="Browse", command=self._browse_log).grid(row=1, column=2, padx=(4, 0), pady=(4, 0))

        # Threshold
        ttk.Label(input_frame, text="Threshold:").grid(row=2, column=0, sticky='w', pady=(4, 0))
        ttk.Entry(input_frame, textvariable=self.threshold_var, width=12).grid(row=2, column=1, sticky='w', padx=(4, 0), pady=(4, 0))

        # Variable checkboxes
        ttk.Label(input_frame, text="Monitor:").grid(row=3, column=0, sticky='w', pady=(4, 0))
        cb_frame = ttk.Frame(input_frame)
        cb_frame.grid(row=3, column=1, sticky='w', padx=(4, 0), pady=(4, 0))
        for i, v in enumerate(DEFAULT_VARIABLES):
            ttk.Checkbutton(cb_frame, text=v, variable=self.monitor_vars[v]).grid(row=0, column=i, padx=(0, 12))

        # Audio alert checkbox
        ttk.Label(input_frame, text="Audio alert:").grid(row=4, column=0, sticky='w', pady=(4, 0))
        alert_frame = ttk.Frame(input_frame)
        alert_frame.grid(row=4, column=1, columnspan=2, sticky='w', padx=(4, 0), pady=(4, 0))
        ttk.Checkbutton(alert_frame, text="Beep on convergence", variable=self.audio_alert_var).grid(row=0, column=0, padx=(0, 20))
        ttk.Checkbutton(alert_frame, text="Dark mode", variable=self.dark_mode_var, command=self._apply_theme).grid(row=0, column=1)

        input_frame.columnconfigure(1, weight=1)

        # --- Start/Stop button ---
        self.start_btn = ttk.Button(self.root, text="Start Monitoring", command=self._start_monitoring)
        self.start_btn.grid(row=1, column=0, pady=8)

        # --- Residuals frame ---
        res_frame = ttk.LabelFrame(self.root, text="Current Residuals", padding=10)
        res_frame.grid(row=2, column=0, sticky='ew', **pad)

        for i, v in enumerate(DEFAULT_VARIABLES):
            ttk.Label(res_frame, text=f"{v}:").grid(row=0, column=i * 2, padx=(0 if i == 0 else 16, 4))
            lbl = ttk.Label(res_frame, text="---", width=12, anchor='w')
            lbl.grid(row=0, column=i * 2 + 1)
            self.res_labels[v] = lbl

        # Iteration / time counter
        ttk.Label(res_frame, text="Iteration / Time:").grid(row=1, column=0, columnspan=2, sticky='w', pady=(6, 0))
        self.time_label = ttk.Label(res_frame, text="---", width=20, anchor='w')
        self.time_label.grid(row=1, column=2, columnspan=4, sticky='w', pady=(6, 0))

        # --- Status bar ---
        status_frame = ttk.Frame(self.root, padding=(8, 4))
        status_frame.grid(row=3, column=0, sticky='ew')
        ttk.Label(status_frame, text="Status:").pack(side='left')
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var)
        self.status_label.pack(side='left', padx=(4, 0))

        # --- Branding footer ---
        self.footer_label = ttk.Label(
            self.root,
            text="Handford Engineering 2026",
            font=("Segoe UI", 7),
            foreground="gray"
        )
        self.footer_label.grid(row=4, column=0, pady=(0, 6))

    # --- Theme ---

    def _apply_theme(self):
        t = THEME_DARK if self.dark_mode_var.get() else THEME_LIGHT
        self._theme = t

        style = ttk.Style(self.root)
        style.configure("TFrame",        background=t["bg"])
        style.configure("TLabel",        background=t["bg"], foreground=t["fg"])
        style.configure("TLabelframe",   background=t["bg"], foreground=t["fg"])
        style.configure("TLabelframe.Label", background=t["bg"], foreground=t["fg"])
        style.configure("TButton",       background=t["bg"], foreground=t["fg"])
        style.configure("TCheckbutton",  background=t["bg"], foreground=t["fg"])
        style.configure("TEntry",        fieldbackground=t["entry_bg"], foreground=t["fg"])

        self.root.configure(bg=t["bg"])
        self.footer_label.configure(foreground=t["gray"])

        # Re-apply residual colours with new theme palette
        if hasattr(self, '_last_residuals'):
            self._update_labels(self._last_residuals, getattr(self, '_last_time', None))

    # --- Browse handlers ---

    def _browse_case_dir(self):
        d = filedialog.askdirectory(title="Select Helyx Case Directory")
        if d:
            self.case_dir_var.set(d)
            self._auto_detect_log(d)

    def _auto_detect_log(self, case_dir):
        p = Path(case_dir)
        candidates = []
        candidates.extend(p.glob('log.*'))
        candidates.extend(p.glob('*.log'))
        candidates = [c for c in candidates if c.is_file()]
        if candidates:
            best = max(candidates, key=lambda f: f.stat().st_mtime)
            self.log_file_var.set(str(best))

    def _browse_log(self):
        f = filedialog.askopenfilename(
            title="Select Solver Log File",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")]
        )
        if f:
            self.log_file_var.set(f)

    # --- Audio alert ---

    def _play_alert(self):
        if not self.audio_alert_var.get() or winsound is None:
            return
        def _beep():
            for _ in range(3):
                winsound.Beep(1000, 300)  # 1 kHz tone, 300 ms
                time.sleep(0.2)            # 200 ms gap within pair
                winsound.Beep(1000, 300)
                time.sleep(0.8)            # 800 ms pause between groups
        threading.Thread(target=_beep, daemon=True).start()

    # --- Monitoring control ---

    def _start_monitoring(self):
        case_dir = self.case_dir_var.get().strip()
        if not case_dir or not Path(case_dir).is_dir():
            messagebox.showerror("Error", "Please select a valid case directory.")
            return

        control_dict = Path(case_dir) / 'system' / 'controlDict'
        if not control_dict.exists():
            messagebox.showerror("Error", "system/controlDict not found in the selected case directory.")
            return

        log_file = self.log_file_var.get().strip()
        if not log_file:
            messagebox.showerror("Error", "Please select or specify a log file.")
            return

        try:
            threshold = float(self.threshold_var.get().strip())
            if threshold <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Threshold must be a positive number (e.g. 1e-4).")
            return

        selected = [v for v, bv in self.monitor_vars.items() if bv.get()]
        if not selected:
            messagebox.showerror("Error", "Select at least one variable to monitor.")
            return

        self.monitor = ResidualMonitor(
            case_dir=case_dir,
            log_path=log_file,
            threshold=threshold,
            variables=selected,
            on_update=lambda r, t: self.root.after(0, self._update_labels, r, t),
            on_trigger=lambda: self.root.after(0, self._on_trigger),
            on_status=lambda s: self.root.after(0, self._set_status, s),
        )
        self.monitor.start()

        self.start_btn.config(text="Stop Monitoring", command=self._stop_monitoring)
        self._set_status("Monitoring...")

    def _stop_monitoring(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        self.start_btn.config(text="Start Monitoring", command=self._start_monitoring)
        self._set_status("Stopped")

    # --- Callbacks ---

    def _update_labels(self, residuals, current_time):
        self._last_residuals = residuals
        self._last_time      = current_time
        t = self._theme

        try:
            threshold = float(self.threshold_var.get().strip())
        except ValueError:
            threshold = 1e-4

        for v, lbl in self.res_labels.items():
            if v in residuals:
                val = residuals[v]
                lbl.config(text=f"{val:.4e}",
                           foreground=t["green"] if val < threshold else t["red"])
            else:
                lbl.config(text="---", foreground=t["fg"])

        if self.time_label is not None:
            self.time_label.config(
                text=str(current_time) if current_time is not None else "---"
            )

    def _on_trigger(self):
        self._play_alert()
        self._stop_monitoring()
        self._set_status("Threshold reached — solver stopped")
        messagebox.showinfo(
            "Threshold Reached",
            f"All monitored velocity residuals have dropped below {self.threshold_var.get()}.\n\n"
            "controlDict has been modified to stop the solver (stopAt writeNow).\n"
            "A backup of the original controlDict has been saved."
        )

    def _set_status(self, text):
        self.status_var.set(text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()

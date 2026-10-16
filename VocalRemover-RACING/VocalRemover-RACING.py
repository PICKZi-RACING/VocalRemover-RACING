import importlib.util
import json
import multiprocessing
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

FROZEN = getattr(sys, "frozen", False)

# รองรับการรันเมื่อแพ็กเป็นไฟล์ .exe
if len(sys.argv) > 1 and sys.argv[1] == "--run-demucs":
    from demucs.separate import main as demucs_main
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    demucs_main()
    sys.exit(0)

import customtkinter as ctk

try:
    import psutil
except ImportError:
    psutil = None

try:
    import torch
except ImportError:
    torch = None

try:
    import pynvml
    pynvml.nvmlInit()
    NVML_HANDLE = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    pynvml = None
    NVML_HANDLE = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_OK = True
except ImportError:
    DND_OK = False

HAS_CUDA = bool(torch and torch.cuda.is_available())
DEVICE = "cuda" if HAS_CUDA else "cpu"

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
DEFAULT_OUTPUT = os.path.join(BASE_DIR, "separated_output")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".demucs_studio.json")
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac")
PROGRESS_RE = re.compile(r"(\d{1,3})%\|")

STATUS_COLORS = {
    "รอคิว": "#94A3B8",
    "กำลังทำ": "#F59E0B",
    "เสร็จแล้ว": "#22C55E",
    "ผิดพลาด": "#EF4444",
    "ยกเลิก": "#FB923C",
}

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

WINDOW_BASES = (ctk.CTk, TkinterDnD.DnDWrapper) if DND_OK else (ctk.CTk,)


def pick_font():
    available = set(tkfont.families())
    for name in ("Cordia New", "Tahoma", "Segoe UI", "Noto Sans Thai", "Arial"):
        if name in available:
            return name
    return "TkDefaultFont"


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def demucs_base_command():
    if FROZEN:
        return [sys.executable, "--run-demucs"]
    return [sys.executable, "-u", "-m", "demucs.separate"]


class AudioSeparatorApp(*WINDOW_BASES):
    def __init__(self):
        super().__init__()
        self.title("โปรแกรมแยกเสียง RACING")
        
        # ตั้งค่าขนาดเริ่มต้นและขนาดขั้นต่ำกันปุ่มหาย
        self.geometry("800x920")
        self.minsize(760, 780)
        self.resizable(True, True)

        self.dnd_ready = False
        if DND_OK:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ready = True
            except Exception:
                pass

        self.font_name = pick_font()
        self.cfg = load_config()
        self.output_dir = self.cfg.get("output_dir") or DEFAULT_OUTPUT
        self.last_dir = self.cfg.get("last_dir") or os.path.expanduser("~")

        self.jobs = []
        self.preview_files = {}
        self.proc = None
        self.running = False
        self.cancelled = False
        self.ui_queue = queue.Queue()
        self.monitor_job = None
        self.cur_n = self.cur_total = 0
        self.cur_name = ""

        self.build_ui()
        self.refresh_job_list()
        self.check_dependencies()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.update_monitor()
        self.poll_queue()

    # ---------- UI ----------

    def font(self, size, bold=False):
        return ctk.CTkFont(family=self.font_name, size=size,
                           weight="bold" if bold else "normal")

    def build_ui(self):
        ctk.CTkLabel(self, text="โปรแกรมแยกเสียง RACING",
                     font=self.font(26, True)).pack(pady=(12, 4))

        # 1. ยึดชุดปุ่มสั่งการไว้ขอบล่างสุดก่อน เพื่อไม่ให้โดนบัง
        self.build_button_section()

        # 2. วางส่วนประกอบส่วนบน
        self.build_queue_section()
        self.build_monitor_section()
        self.build_settings_section()

        # 3. วางส่วน Log ไว้ตรงกลาง ยืด-หดตามขนาดพื้นที่ที่เหลือ
        self.build_log_section()

        if self.dnd_ready:
            for widget in (self, self.queue_frame):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)

    def build_queue_section(self):
        self.queue_frame = ctk.CTkFrame(self)
        self.queue_frame.pack(fill="x", padx=20, pady=4)
        self.queue_frame.columnconfigure(0, weight=1)

        ctk.CTkLabel(self.queue_frame, text="รายการไฟล์ (ประมวลผลทีละไฟล์ตามลำดับ)",
                     font=self.font(17, True), anchor="w"
                     ).grid(row=0, column=0, padx=12, pady=(6, 2), sticky="w")

        self.add_btn = ctk.CTkButton(self.queue_frame, text="เพิ่มไฟล์", width=100,
                                     font=self.font(15, True), command=self.browse_files)
        self.add_btn.grid(row=0, column=1, padx=4, pady=(6, 2))

        self.clear_btn = ctk.CTkButton(self.queue_frame, text="ล้างรายการ", width=100,
                                       font=self.font(15), fg_color="#475569",
                                       hover_color="#334155", command=self.clear_jobs)
        self.clear_btn.grid(row=0, column=2, padx=(4, 12), pady=(6, 2))

        self.file_list = ctk.CTkScrollableFrame(self.queue_frame, height=100)
        self.file_list.grid(row=1, column=0, columnspan=3, padx=10, pady=4, sticky="ew")

        self.out_label = ctk.CTkLabel(self.queue_frame, text=f"Output: {self.output_dir}",
                                      font=self.font(14), anchor="w", text_color="#94A3B8")
        self.out_label.grid(row=2, column=0, columnspan=2, padx=12, pady=(0, 6), sticky="ew")

        ctk.CTkButton(self.queue_frame, text="เปลี่ยนโฟลเดอร์", width=100,
                      font=self.font(14), fg_color="#475569", hover_color="#334155",
                      command=self.choose_output_dir
                      ).grid(row=2, column=2, padx=(4, 12), pady=(0, 6))

    def build_monitor_section(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=4)
        frame.columnconfigure((0, 1, 2, 3), weight=1, uniform="cards")

        self.cpu_label, self.cpu_bar = self.make_card(frame, 0, "CPU Usage", "0 %", "#38BDF8")
        self.ram_label, self.ram_bar = self.make_card(frame, 1, "System RAM", "0.0 GB", "#A855F7")
        self.gpu_label, self.gpu_bar = self.make_card(
            frame, 2, "GPU VRAM", "CUDA Idle" if HAS_CUDA else "CPU Mode", "#22C55E")

        status_card = self.card_frame(frame, 3)
        self.card_title(status_card, "AI Status")
        self.engine_label = ctk.CTkLabel(status_card, text="STANDBY",
                                         font=self.font(18, True), text_color="#F59E0B")
        self.engine_label.pack(pady=(0, 2))
        self.engine_sub = ctk.CTkLabel(status_card, text=f"{DEVICE.upper()} Ready",
                                       font=self.font(13), text_color="#64748B")
        self.engine_sub.pack(pady=(0, 6))

    def build_settings_section(self):
        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=20, pady=4)

        ctk.CTkLabel(settings, text="เลือกโหมดการแยกเสียง:", font=self.font(17, True)
                     ).grid(row=0, column=0, sticky="w", padx=15, pady=(6, 2))

        self.mode_var = ctk.StringVar(value=self.cfg.get("mode", "2stems"))
        modes = [
            ("2 Stems (เสียงร้อง + เสียงดนตรีรวม)", "2stems"),
            ("4 Stems (Vocals, Drums, Bass, Other)", "4stems"),
            ("6 Stems (Vocals, Drums, Bass, Guitar, Piano, Other)", "6stems"),
        ]
        for i, (text, value) in enumerate(modes, start=1):
            ctk.CTkRadioButton(settings, text=text, font=self.font(16),
                               variable=self.mode_var, value=value
                               ).grid(row=i, column=0, sticky="w", padx=25, pady=2)

        ctk.CTkLabel(settings, text="ฟอร์แมตไฟล์ Output:", font=self.font(17, True)
                     ).grid(row=0, column=1, sticky="w", padx=15, pady=(6, 2))
        self.format_var = ctk.StringVar(value=self.cfg.get("fmt", "wav"))
        ctk.CTkOptionMenu(settings, values=["wav", "mp3", "flac"], width=120,
                          font=self.font(15), variable=self.format_var
                          ).grid(row=1, column=1, sticky="w", padx=15, pady=2)

        self.auto_open_var = ctk.BooleanVar(value=self.cfg.get("auto_open", True))
        ctk.CTkCheckBox(settings, text="เปิดโฟลเดอร์เมื่อเสร็จ", font=self.font(15),
                        variable=self.auto_open_var
                        ).grid(row=2, column=1, sticky="w", padx=15, pady=2)

        adv = ctk.CTkFrame(settings, fg_color="transparent")
        adv.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 2))

        ctk.CTkLabel(adv, text="โมเดล:", font=self.font(16)).grid(row=0, column=0, padx=(5, 4))
        self.model_var = ctk.StringVar(value="htdemucs")
        self.model_menu = ctk.CTkOptionMenu(adv, values=["htdemucs"], width=130,
                                            font=self.font(15), variable=self.model_var)
        self.model_menu.grid(row=0, column=1, padx=(0, 14))

        ctk.CTkLabel(adv, text="Shifts:", font=self.font(16)).grid(row=0, column=2, padx=(0, 4))
        self.shifts_var = ctk.StringVar(value=str(self.cfg.get("shifts", 1)))
        ctk.CTkOptionMenu(adv, values=["0", "1", "2", "3", "4"], width=70,
                          font=self.font(15), variable=self.shifts_var
                          ).grid(row=0, column=3, padx=(0, 14))

        ctk.CTkLabel(adv, text="Overlap:", font=self.font(16)).grid(row=0, column=4, padx=(0, 4))
        self.overlap_slider = ctk.CTkSlider(adv, from_=0.1, to=0.5, number_of_steps=8,
                                            width=120, command=self.on_overlap_change)
        self.overlap_slider.set(self.cfg.get("overlap", 0.25))
        self.overlap_slider.grid(row=0, column=5)
        self.overlap_label = ctk.CTkLabel(adv, text="", font=self.font(15), width=40)
        self.overlap_label.grid(row=0, column=6, padx=(4, 0))
        self.on_overlap_change(self.overlap_slider.get())

        ctk.CTkLabel(settings,
                     text="ใช้โมเดล htdemucs ความเร็วสูงสปีดมาตรฐาน (โหมด 6 Stems ใช้โมเดล htdemucs_6s เสมอ)",
                     font=self.font(13), text_color="#64748B", anchor="w", wraplength=700
                     ).grid(row=5, column=0, columnspan=2, sticky="w", padx=15, pady=(0, 6))

        self.mode_var.trace_add("write", self.on_mode_change)
        self.on_mode_change()

    def build_log_section(self):
        log_frame = ctk.CTkFrame(self)
        log_frame.pack(fill="both", expand=True, padx=20, pady=4)

        self.status_label = ctk.CTkLabel(log_frame, text="สถานะ: พร้อมใช้งาน",
                                         font=self.font(16), anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=(4, 2))

        self.progress_bar = ctk.CTkProgressBar(log_frame)
        self.progress_bar.pack(fill="x", padx=10, pady=2)
        self.progress_bar.set(0)

        self.textbox = ctk.CTkTextbox(log_frame, height=80, font=self.font(15))
        self.textbox.pack(fill="both", expand=True, padx=10, pady=(2, 6))

    def build_button_section(self):
        bottom_container = ctk.CTkFrame(self, fg_color="transparent")
        bottom_container.pack(side="bottom", fill="x", padx=20, pady=(0, 10))

        preview = ctk.CTkFrame(bottom_container, fg_color="transparent")
        preview.pack(fill="x", pady=(0, 4))

        ctk.CTkLabel(preview, text="ฟังตัวอย่าง:", font=self.font(16)).pack(side="left", padx=(0, 6))
        self.preview_var = ctk.StringVar(value="-")
        self.preview_menu = ctk.CTkOptionMenu(preview, values=["-"], variable=self.preview_var,
                                              font=self.font(14), state="disabled", width=420)
        self.preview_menu.pack(side="left", padx=(0, 6))
        self.preview_btn = ctk.CTkButton(preview, text="เล่น", width=70, font=self.font(15, True),
                                         state="disabled", command=self.play_preview)
        self.preview_btn.pack(side="left")

        btns = ctk.CTkFrame(bottom_container, fg_color="transparent")
        btns.pack(fill="x", pady=(2, 0))

        self.run_btn = ctk.CTkButton(btns, text="เริ่มแยกเสียง", height=42, font=self.font(18, True),
                                     fg_color="#2FA572", hover_color="#1E714C",
                                     command=self.start_processing)
        self.run_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.cancel_btn = ctk.CTkButton(btns, text="ยกเลิก", height=42, width=100,
                                        font=self.font(16, True), state="disabled",
                                        fg_color="#DC2626", hover_color="#991B1B",
                                        command=self.cancel_processing)
        self.cancel_btn.pack(side="left", padx=5)

        ctk.CTkButton(btns, text="เปิดโฟลเดอร์ Output", height=42, font=self.font(16, True),
                      fg_color="#3B82F6", hover_color="#1D4ED8",
                      command=self.open_output_folder).pack(side="right", padx=(5, 0))

    def card_frame(self, parent, col):
        card = ctk.CTkFrame(parent, corner_radius=10, fg_color="#1E293B",
                            border_width=1, border_color="#334155")
        card.grid(row=0, column=col, padx=5, pady=5, sticky="nsew")
        return card

    def card_title(self, card, text):
        ctk.CTkLabel(card, text=text, font=self.font(15, True),
                     text_color="#94A3B8").pack(pady=(6, 0))

    def make_card(self, parent, col, title, initial, color):
        card = self.card_frame(parent, col)
        self.card_title(card, title)
        value = ctk.CTkLabel(card, text=initial, font=self.font(20, True), text_color=color)
        value.pack()
        bar = ctk.CTkProgressBar(card, height=6, progress_color=color)
        bar.pack(fill="x", padx=10, pady=(2, 8))
        bar.set(0)
        return value, bar

    def on_overlap_change(self, value):
        self.overlap_label.configure(text=f"{float(value):.2f}")

    def on_mode_change(self, *_):
        state = "disabled" if self.mode_var.get() == "6stems" else "normal"
        self.model_menu.configure(state=state)

    # ---------- Resource monitor ----------

    def read_vram(self):
        if pynvml:
            info = pynvml.nvmlDeviceGetMemoryInfo(NVML_HANDLE)
            return info.used / 2**20, info.total / 2**20
        free_b, total_b = torch.cuda.mem_get_info()
        return (total_b - free_b) / 2**20, total_b / 2**20

    def update_monitor(self):
        if psutil:
            cpu = psutil.cpu_percent()
            self.cpu_label.configure(text=f"{cpu:.1f} %")
            self.cpu_bar.set(cpu / 100)

            mem = psutil.virtual_memory()
            self.ram_label.configure(text=f"{mem.used / 2**30:.1f}/{mem.total / 2**30:.0f} GB")
            self.ram_bar.set(mem.percent / 100)

        if HAS_CUDA and self.running:
            try:
                used, total = self.read_vram()
                self.gpu_label.configure(text=f"{used:.0f} MB")
                self.gpu_bar.set(min(used / total, 1.0))
            except Exception:
                self.gpu_label.configure(text="CUDA")
        elif HAS_CUDA:
            self.gpu_label.configure(text="CUDA Idle")
            self.gpu_bar.set(0.05)
        else:
            self.gpu_label.configure(text="CPU Mode")
            self.gpu_bar.set(0)

        self.monitor_job = self.after(500, self.update_monitor)

    # ---------- Job list ----------

    def refresh_job_list(self):
        for child in self.file_list.winfo_children():
            child.destroy()

        if not self.jobs:
            hint = ("ลากไฟล์เพลงมาวางที่นี่ หรือกด 'เพิ่มไฟล์'" if self.dnd_ready
                    else "กด 'เพิ่มไฟล์' เพื่อเลือกเพลง (เลือกได้หลายไฟล์)")
            ctk.CTkLabel(self.file_list, text=hint, font=self.font(15),
                         text_color="#64748B").pack(pady=18)
            return

        for job in self.jobs:
            row = ctk.CTkFrame(self.file_list, fg_color="transparent")
            row.pack(fill="x", pady=1)
            row.columnconfigure(0, weight=1)

            ctk.CTkLabel(row, text=os.path.basename(job["path"]), anchor="w",
                         font=self.font(15)).grid(row=0, column=0, sticky="ew", padx=(4, 6))
            job["status_label"] = ctk.CTkLabel(
                row, text=job["status"], width=80, font=self.font(14),
                text_color=STATUS_COLORS.get(job["status"], "#94A3B8"))
            job["status_label"].grid(row=0, column=1, padx=4)
            ctk.CTkButton(row, text="x", width=28, height=24, fg_color="#475569",
                          hover_color="#DC2626",
                          command=lambda j=job: self.remove_job(j)).grid(row=0, column=2, padx=(0, 4))

    def set_job_status(self, index, status):
        job = self.jobs[index]
        job["status"] = status
        label = job.get("status_label")
        if label is not None and label.winfo_exists():
            label.configure(text=status, text_color=STATUS_COLORS.get(status, "#94A3B8"))

    def add_files(self, paths):
        if self.running:
            self.log("กำลังประมวลผลอยู่ ยังเพิ่มไฟล์ไม่ได้")
            return

        candidates = []
        for p in paths:
            if os.path.isdir(p):
                candidates += [os.path.join(p, n) for n in sorted(os.listdir(p))]
            else:
                candidates.append(p)

        existing = {job["path"] for job in self.jobs}
        added = 0
        for p in candidates:
            p = os.path.abspath(p)
            if (os.path.isfile(p) and p.lower().endswith(AUDIO_EXTS) and p not in existing):
                self.jobs.append({"path": p, "status": "รอคิว"})
                existing.add(p)
                added += 1
                self.last_dir = os.path.dirname(p)

        if added:
            self.log(f"เพิ่ม {added} ไฟล์เข้าคิว")
            self.refresh_job_list()

    def on_drop(self, event):
        self.add_files(self.tk.splitlist(event.data))

    def browse_files(self):
        paths = filedialog.askopenfilenames(
            title="เลือกไฟล์เสียง", initialdir=self.last_dir,
            filetypes=[("Audio Files", "*.mp3 *.wav *.flac *.m4a *.ogg *.aac")])
        if paths:
            self.add_files(paths)

    def remove_job(self, job):
        if self.running:
            return
        self.jobs.remove(job)
        self.refresh_job_list()

    def clear_jobs(self):
        if self.running:
            return
        self.jobs.clear()
        self.refresh_job_list()

    # ---------- Helpers ----------

    def check_dependencies(self):
        if not FROZEN and importlib.util.find_spec("demucs") is None:
            self.log("ไม่พบ demucs ติดตั้งก่อนด้วย: pip install demucs")
        if shutil.which("ffmpeg") is None:
            self.log("ไม่พบ ffmpeg ไฟล์บางชนิด (เช่น m4a) และการบันทึกเป็น mp3 อาจใช้ไม่ได้")
        if not DND_OK:
            self.log("ถ้าอยากลากไฟล์มาวางได้ ติดตั้ง: pip install tkinterdnd2")
        elif not self.dnd_ready:
            self.log("โหลด tkinterdnd2 ไม่สำเร็จ ฟีเจอร์ลากวางจะใช้ไม่ได้")

    def log(self, message):
        self.textbox.insert("end", f"{message}\n")
        self.textbox.see("end")

    def choose_output_dir(self):
        path = filedialog.askdirectory(title="เลือกโฟลเดอร์ Output", initialdir=self.output_dir)
        if path:
            self.output_dir = os.path.abspath(path)
            self.out_label.configure(text=f"Output: {self.output_dir}")

    def open_output_folder(self):
        os.makedirs(self.output_dir, exist_ok=True)
        open_path(self.output_dir)

    def set_running(self, running):
        self.running = running
        idle_state = "disabled" if running else "normal"
        self.add_btn.configure(state=idle_state)
        self.clear_btn.configure(state=idle_state)

        if running:
            self.run_btn.configure(state="disabled", text="กำลังประมวลผล...")
            self.cancel_btn.configure(state="normal")
            self.engine_label.configure(text="RUNNING", text_color="#22C55E")
            self.engine_sub.configure(text=f"{DEVICE.upper()} Active")
        else:
            self.run_btn.configure(state="normal", text="เริ่มแยกเสียง")
            self.cancel_btn.configure(state="disabled")
            self.engine_label.configure(text="STANDBY", text_color="#F59E0B")
            self.engine_sub.configure(text=f"{DEVICE.upper()} Ready")

    def current_settings(self):
        return {
            "mode": self.mode_var.get(),
            "fmt": self.format_var.get(),
            "model": "htdemucs",
            "shifts": int(self.shifts_var.get()),
            "overlap": round(self.overlap_slider.get(), 2),
            "output_dir": self.output_dir,
            "auto_open": self.auto_open_var.get(),
        }

    def save_settings(self):
        data = self.current_settings()
        data["last_dir"] = self.last_dir
        save_config(data)

    def update_preview_menu(self, files):
        self.preview_files = {os.path.basename(p): p for p in files}
        names = list(self.preview_files) or ["-"]
        state = "normal" if self.preview_files else "disabled"
        self.preview_menu.configure(values=names, state=state)
        self.preview_btn.configure(state=state)
        self.preview_var.set(names[0])

    def play_preview(self):
        path = self.preview_files.get(self.preview_var.get())
        if path and os.path.isfile(path):
            open_path(path)

    # ---------- Processing ----------

    def start_processing(self):
        todo = [(i, job["path"]) for i, job in enumerate(self.jobs) if job["status"] != "เสร็จแล้ว"]
        if not todo:
            messagebox.showwarning("คำเตือน", "กรุณาเพิ่มไฟล์เสียงก่อน (หรือไฟล์ในรายการเสร็จหมดแล้ว)")
            return
        if not FROZEN and importlib.util.find_spec("demucs") is None:
            messagebox.showerror("ไม่พบ demucs", "ติดตั้งก่อนด้วยคำสั่ง: pip install demucs")
            return

        settings = self.current_settings()
        self.save_settings()

        for index, _ in todo:
            self.set_job_status(index, "รอคิว")

        self.cancelled = False
        self.update_preview_menu([])
        self.set_running(True)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.status_label.configure(text=f"สถานะ: กำลังเริ่มบน {DEVICE.upper()}...")

        threading.Thread(target=self.run_batch, args=(todo, settings), daemon=True).start()

    def cancel_processing(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.log("กำลังยกเลิก...")

    def build_command(self, input_file, settings):
        mode = settings["mode"]
        model = "htdemucs_6s" if mode == "6stems" else "htdemucs"

        cmd = demucs_base_command() + [
            "-o", settings["output_dir"],
            "-n", model,
            "-d", DEVICE,
            "-j", "2",
            "--segment", "7",
            "--shifts", str(settings["shifts"]),
            "--overlap", f"{settings['overlap']:.2f}",
        ]

        if mode == "2stems":
            cmd += ["--two-stems", "vocals"]
        if settings["fmt"] == "mp3":
            cmd += ["--mp3", "--mp3-bitrate", "320"]
        elif settings["fmt"] == "flac":
            cmd.append("--flac")

        cmd.append(input_file)
        return cmd, model

    def run_batch(self, todo, settings):
        ok_count = fail_count = 0
        outputs = []
        total = len(todo)

        try:
            for n, (index, path) in enumerate(todo, start=1):
                if self.cancelled:
                    break

                name = os.path.basename(path)
                self.ui_queue.put(("job", index, "กำลังทำ"))
                self.ui_queue.put(("batch", n, total, name))
                self.ui_queue.put(("log", "=" * 45))
                self.ui_queue.put(("log", f"[{n}/{total}] {name}"))

                try:
                    ok, files = self.separate_one(path, settings)
                except Exception as exc:
                    self.ui_queue.put(("log", f"เกิดข้อผิดพลาด: {exc}"))
                    ok, files = False, []

                if self.cancelled:
                    self.ui_queue.put(("job", index, "ยกเลิก"))
                    break
                if ok:
                    ok_count += 1
                    outputs += files
                    self.ui_queue.put(("job", index, "เสร็จแล้ว"))
                else:
                    fail_count += 1
                    self.ui_queue.put(("job", index, "ผิดพลาด"))
        finally:
            self.ui_queue.put(("done", ok_count, fail_count, self.cancelled, outputs))

    def separate_one(self, input_file, settings):
        if not os.path.isfile(input_file):
            self.ui_queue.put(("log", f"ไม่พบไฟล์: {input_file}"))
            return False, []

        cmd, model = self.build_command(input_file, settings)
        self.ui_queue.put(("log", "คำสั่ง: " + " ".join(cmd)))

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env[var] = "2"

        flags = 0
        if sys.platform == "win32":
            flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env, bufsize=1, creationflags=flags)

        buffer = ""
        while True:
            char = self.proc.stdout.read(1)
            if not char and self.proc.poll() is not None:
                break
            if char in ("\r", "\n"):
                line = buffer.strip()
                buffer = ""
                if line:
                    match = PROGRESS_RE.search(line)
                    if match:
                        self.ui_queue.put(("progress", min(int(match.group(1)), 100) / 100))
                    else:
                        self.ui_queue.put(("log", line))
            else:
                buffer += char

        self.proc.wait()

        if self.cancelled or self.proc.returncode != 0:
            return False, []
        return True, self.collect_outputs(input_file, model, settings["output_dir"])

    def collect_outputs(self, input_file, model, output_dir):
        """ย้ายไฟล์ stem ออกมาไว้ที่ output_dir แล้วลบโฟลเดอร์ชั่วคราว"""
        track = os.path.splitext(os.path.basename(input_file))[0]
        model_dir = os.path.join(output_dir, model)
        stem_dir = os.path.join(model_dir, track)
        saved = []

        if not os.path.isdir(stem_dir):
            return saved

        for name in os.listdir(stem_dir):
            src = os.path.join(stem_dir, name)
            if not os.path.isfile(src):
                continue
            stem, ext = os.path.splitext(name)
            dest = os.path.join(output_dir, f"{track}_{stem}{ext}")
            os.replace(src, dest)
            saved.append(dest)
            self.ui_queue.put(("log", f"  บันทึก: {os.path.basename(dest)}"))

        try:
            os.rmdir(stem_dir)
            os.rmdir(model_dir)
        except OSError:
            pass
        return saved

    # ---------- Queue (worker -> UI) ----------

    def poll_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    self.log(item[1])

                elif kind == "job":
                    self.set_job_status(item[1], item[2])

                elif kind == "batch":
                    _, self.cur_n, self.cur_total, self.cur_name = item
                    if self.progress_bar.cget("mode") == "indeterminate":
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                    self.progress_bar.set((self.cur_n - 1) / self.cur_total)
                    self.status_label.configure(
                        text=f"สถานะ: ไฟล์ {self.cur_n}/{self.cur_total} - กำลังเตรียม {self.cur_name}")

                elif kind == "progress":
                    frac = item[1]
                    if self.cur_total:
                        self.progress_bar.set((self.cur_n - 1 + frac) / self.cur_total)
                    self.status_label.configure(
                        text=f"สถานะ: ไฟล์ {self.cur_n}/{self.cur_total} - {self.cur_name} ({frac * 100:.0f}%)")

                elif kind == "done":
                    self.finish_batch(*item[1:])
        except queue.Empty:
            pass

        self.after(100, self.poll_queue)

    def finish_batch(self, ok_count, fail_count, cancelled, outputs):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.set_running(False)
        self.update_preview_menu(outputs)

        if cancelled:
            summary = "ยกเลิกการประมวลผลแล้ว"
            self.progress_bar.set(0)
        else:
            summary = f"เสร็จ {ok_count} ไฟล์"
            if fail_count:
                summary += f", ผิดพลาด {fail_count} ไฟล์ (ดู log)"
            self.progress_bar.set(1.0 if ok_count else 0)

        self.status_label.configure(text=f"สถานะ: {summary}")
        self.log(summary)

        if ok_count and not cancelled:
            if self.auto_open_var.get():
                self.open_output_folder()
            messagebox.showinfo("เสร็จแล้ว", summary)

    # ---------- Shutdown ----------

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("ยืนยัน", "ยังประมวลผลอยู่ ต้องการยกเลิกและปิดโปรแกรมหรือไม่?"):
                return
            self.cancelled = True
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

        self.save_settings()
        if self.monitor_job:
            self.after_cancel(self.monitor_job)
        self.destroy()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    AudioSeparatorApp().mainloop()
"""
G_macro 설정/실행 GUI.

규칙(트리거/타깃 이미지)과 설정을 편집해 config.json에 저장하고, 매크로를 시작/정지한다.
검색 영역은 화면 위 오버레이(최대 3개)로 지정하며, 각 오버레이 가운데가 내 캐릭터 위치다.
설정 변경은 정지 후 다시 시작하면 적용된다.
"""
import copy
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import mss
from pynput import keyboard

import macro  # DPI 설정이 Tk 창보다 먼저 적용되도록 가장 먼저 import
import capture
import tune
from overlay import COLORS, MAX_OVERLAYS, Overlay

KEY_CHOICES = [f"f{i}" for i in range(1, 13)]
IMAGES_DIR = macro.BASE_DIR / "images"
PREVIEW_MAX = 160
RULE_ALARM_SLOTS = 2  # 규칙 편집 창의 알람 이미지 칸 수
LOG_MAX_LINES = 2000


def rel_path(path):
    """프로그램 폴더 안의 파일이면 상대경로로 (폴더째 옮겨도 동작하도록)."""
    p = Path(path).resolve()
    try:
        return p.relative_to(macro.BASE_DIR).as_posix()
    except ValueError:
        return str(p)


def parse_number(text, kind, label, minimum=None):
    try:
        value = kind(text)
    except (TypeError, ValueError):
        raise ValueError(f"'{label}' 값이 올바르지 않습니다: {text}")
    if minimum is not None and value < minimum:
        raise ValueError(f"'{label}'은(는) {minimum} 이상이어야 합니다.")
    return value


def default_region():
    """주 모니터 가운데 1280x720 (모니터가 더 작으면 맞춰서)."""
    with mss.mss() as sct:
        mon = sct.monitors[1]
    w, h = min(1280, mon["width"] - 100), min(720, mon["height"] - 100)
    return [mon["left"] + (mon["width"] - w) // 2, mon["top"] + (mon["height"] - h) // 2, w, h]


class ImageFieldsMixin:
    """이미지 경로 입력 + 찾기 + 화면 캡처 + 미리보기 줄. self.app, self.v_name 필요."""

    def _add_image_row(self, frm, row, label, var, kind):
        if not hasattr(self, "previews"):
            self.previews = {}
            self.image_vars = {}  # 점수 테스트에서 고를 템플릿
        self.image_vars[label] = var
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=(10, 3))
        ttk.Entry(frm, textvariable=var, width=40).grid(row=row, column=1, sticky="ew", pady=(10, 3))
        ttk.Button(frm, text="찾기", width=6, command=lambda: self.browse(var)).grid(
            row=row, column=2, padx=(4, 0), pady=(10, 3))
        ttk.Button(frm, text="화면 캡처", command=lambda: self.capture(var, kind)).grid(
            row=row, column=3, padx=(4, 0), pady=(10, 3))
        preview = ttk.Label(frm, text="(미리보기 없음)", anchor="center", relief="groove")
        preview.grid(row=row + 1, column=1, columnspan=3, sticky="ew", ipady=4)
        self.previews[kind] = preview
        var.trace_add("write", lambda *_: self.update_preview(var, kind))
        self.update_preview(var, kind)

    def _capture_index(self):
        """캡처할 오버레이 번호(0부터)."""
        return 0

    def browse(self, var):
        path = filedialog.askopenfilename(
            parent=self, initialdir=IMAGES_DIR, filetypes=[("이미지", "*.png *.jpg *.bmp"), ("모든 파일", "*.*")])
        if path:
            var.set(rel_path(path))

    def capture(self, var, kind):
        name = self.v_name.get().strip() or kind
        path = self.app.capture_image(self, f"{name}_{kind}", self._capture_index())
        if path:
            var.set(path)

    def update_preview(self, var, kind):
        label = self.previews[kind]
        path = (macro.BASE_DIR / var.get()).resolve() if var.get() else None
        if not path or not path.is_file():
            label.config(image="", text="(미리보기 없음)")
            label.image = None
            return
        try:
            img = tk.PhotoImage(file=str(path))
            factor = math.ceil(max(img.width(), img.height()) / PREVIEW_MAX)
            if factor > 1:
                img = img.subsample(factor)
            label.config(image=img, text="")
            label.image = img  # 참조 유지 (없으면 이미지가 사라짐)
        except tk.TclError:
            label.config(image="", text="(미리보기를 표시할 수 없는 형식)")
            label.image = None

    def _add_threshold_row(self, frm, row):
        ttk.Label(frm, text="일치 기준").grid(row=row, column=0, sticky="w", pady=(10, 3))
        th_frm = ttk.Frame(frm)
        th_frm.grid(row=row, column=1, columnspan=3, sticky="ew", pady=(10, 3))
        th_label = ttk.Label(th_frm, width=5)
        ttk.Scale(th_frm, from_=0.5, to=1.0, variable=self.v_threshold,
                  command=lambda v: th_label.config(text=f"{float(v):.2f}")).pack(side="left", fill="x", expand=True)
        th_label.pack(side="left", padx=(6, 0))
        th_label.config(text=f"{self.v_threshold.get():.2f}")
        self.th_label = th_label
        ttk.Button(th_frm, text="점수 테스트", command=self.open_tuner).pack(side="left", padx=(8, 0))

    def open_tuner(self):
        """테스트 이미지로 매칭 점수를 보고 일치 기준을 정하는 창."""
        app = self.app
        tool = tune.ThresholdTool(
            self, templates=[(k, v.get().strip()) for k, v in self.image_vars.items()],
            threshold=self.v_threshold.get(), regions=[o.region for o in app.overlays],
            hide_windows=lambda: [self, app.root] + app.overlays, on_apply=self._set_threshold)
        tool.transient(self)
        tool.grab_set()
        self.wait_window(tool)
        if self.winfo_exists():
            self.grab_set()

    def _set_threshold(self, value):
        self.v_threshold.set(value)
        self.th_label.config(text=f"{value:.2f}")

    def _add_ok_cancel(self, frm, row):
        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=row, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(btn_frm, text="확인", command=self.ok).pack(side="left", padx=4)
        ttk.Button(btn_frm, text="취소", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())


class RuleDialog(ImageFieldsMixin, tk.Toplevel):
    """규칙 추가/편집 창. 확인하면 self.result에 규칙 dict가 들어간다."""

    def __init__(self, app, rule=None):
        super().__init__(app.root)
        self.app = app
        self.result = None
        rule = rule or {}
        self.title("규칙 편집" if rule else "규칙 추가")
        self.transient(app.root)
        self.resizable(False, False)

        offset = macro.offset_ranges(rule)
        self.v_name = tk.StringVar(value=rule.get("name", ""))
        self.v_trigger = tk.StringVar(value=rule.get("trigger", ""))
        self.v_target = tk.StringVar(value=rule.get("target", ""))
        alarms = macro.rule_alarm_paths(rule)
        self.v_alarms = [tk.StringVar(value=alarms[i] if i < len(alarms) else "") for i in range(RULE_ALARM_SLOTS)]
        self.v_threshold = tk.DoubleVar(value=rule.get("threshold", 0.85))
        self.v_off = [(tk.StringVar(value=str(lo)), tk.StringVar(value=str(hi))) for lo, hi in offset]  # X, Y
        self.v_cooldown = tk.StringVar(value=str(rule["cooldown"]) if "cooldown" in rule else "")
        hold = rule.get("hold", macro.DEFAULT_HOLD)
        gap = rule.get("gap", macro.DEFAULT_GAP)
        delay = rule.get("delay", macro.DEFAULT_DELAY)
        self.v_clicks = tk.StringVar(value=str(rule.get("clicks", 1)))
        self.v_hold = (tk.StringVar(value=str(hold[0])), tk.StringVar(value=str(hold[1])))
        self.v_gap = (tk.StringVar(value=str(gap[0])), tk.StringVar(value=str(gap[1])))
        self.v_delay = (tk.StringVar(value=str(delay[0])), tk.StringVar(value=str(delay[1])))

        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="이름").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.v_name, width=40).grid(row=0, column=1, columnspan=3, sticky="ew")

        self._add_image_row(frm, 1, "트리거 이미지", self.v_trigger, "trigger")
        self._add_image_row(frm, 3, "타깃 이미지 (우클릭)", self.v_target, "target")
        for i, var in enumerate(self.v_alarms):
            self._add_image_row(frm, 5 + i * 2, f"알람 이미지 {i + 1} (선택)", var, f"alarm{i + 1}")

        n = len(app.overlays)
        selected = set(rule.get("overlays") or range(1, n + 1))
        self.v_ovs = [tk.BooleanVar(value=i in selected) for i in range(1, n + 1)]
        ttk.Label(frm, text="적용 오버레이").grid(row=10, column=0, sticky="w", pady=(10, 0))
        ov_frm = ttk.Frame(frm)
        ov_frm.grid(row=10, column=1, columnspan=3, sticky="w", pady=(10, 0))
        for i, var in enumerate(self.v_ovs, 1):
            tk.Label(ov_frm, text="■", fg=COLORS[i - 1]).pack(side="left")
            ttk.Checkbutton(ov_frm, text=str(i), variable=var).pack(side="left", padx=(0, 10))
        ttk.Label(frm, text="타깃이 여러 개면 오버레이 가운데(내 캐릭터)와 가장 가까운 타깃을 클릭합니다.\n"
                       "알람 이미지 중 하나라도 적용 오버레이에 보이면 경고음을 울리고 매크로를 정지합니다.",
                  foreground="gray").grid(row=11, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self._add_threshold_row(frm, 12)

        ttk.Label(frm, text="클릭 위치 보정").grid(row=13, column=0, sticky="w", pady=3)
        off_frm = ttk.Frame(frm)
        off_frm.grid(row=13, column=1, columnspan=3, sticky="w")
        for axis, (v_min, v_max) in zip("XY", self.v_off):
            ttk.Label(off_frm, text=axis).pack(side="left", padx=(0 if axis == "X" else 12, 2))
            ttk.Entry(off_frm, textvariable=v_min, width=5).pack(side="left")
            ttk.Label(off_frm, text="~").pack(side="left", padx=2)
            ttk.Entry(off_frm, textvariable=v_max, width=5).pack(side="left")
        ttk.Label(off_frm, text="타깃 가운데 기준 픽셀, 범위에서 랜덤 (X+ 오른쪽, Y+ 아래)",
                  foreground="gray").pack(side="left", padx=6)

        ttk.Label(frm, text="클릭 횟수").grid(row=15, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=100, textvariable=self.v_clicks, width=6).grid(row=15, column=1, sticky="w")

        for r, label, (v_min, v_max), hint in (
            (14, "반응 대기(초)", self.v_delay, "발견 후 랜덤하게 기다렸다가 다시 확인하고 클릭 (0 = 바로)"),
            (16, "누르는 시간(초)", self.v_hold, "누른 뒤 이 범위에서 랜덤하게 기다렸다가 뗌"),
            (17, "클릭 간격(초)", self.v_gap, "여러 번 클릭할 때 뗀 뒤 다음 누르기까지"),
        ):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3)
            rng = ttk.Frame(frm)
            rng.grid(row=r, column=1, columnspan=3, sticky="w")
            ttk.Entry(rng, textvariable=v_min, width=6).pack(side="left")
            ttk.Label(rng, text="~").pack(side="left", padx=4)
            ttk.Entry(rng, textvariable=v_max, width=6).pack(side="left")
            ttk.Label(rng, text=hint, foreground="gray").pack(side="left", padx=6)

        ttk.Label(frm, text="쿨다운(초)").grid(row=18, column=0, sticky="w", pady=3)
        cd_frm = ttk.Frame(frm)
        cd_frm.grid(row=18, column=1, columnspan=3, sticky="w")
        ttk.Entry(cd_frm, textvariable=self.v_cooldown, width=8).pack(side="left")
        ttk.Label(cd_frm, text="비우면 설정 탭의 기본값 사용", foreground="gray").pack(side="left", padx=6)

        self._add_ok_cancel(frm, 19)
        self.grab_set()

    def _capture_index(self):
        # 체크된 첫 번째 오버레이 영역에서 캡처
        return next((i for i, v in enumerate(self.v_ovs) if v.get()), 0)

    def ok(self):
        try:
            trigger, target = self.v_trigger.get().strip(), self.v_target.get().strip()
            for label, p in (("트리거", trigger), ("타깃", target)):
                if not p:
                    raise ValueError(f"{label} 이미지를 지정하세요.")
                if not (macro.BASE_DIR / p).is_file():
                    raise ValueError(f"{label} 이미지 파일이 없습니다: {p}")
            rule = {
                "name": self.v_name.get().strip() or Path(target).stem,
                "trigger": trigger,
                "target": target,
                "threshold": round(self.v_threshold.get(), 2),
            }
            offset = []
            for axis, (v_min, v_max) in zip("XY", self.v_off):
                lo = parse_number(v_min.get(), int, f"보정 {axis} 최소")
                hi = parse_number(v_max.get(), int, f"보정 {axis} 최대")
                if lo > hi:
                    raise ValueError(f"클릭 위치 보정 {axis}: 최소가 최대보다 큽니다.")
                offset.append([lo, hi])
            # 범위가 없으면 예전처럼 [x, y]로 저장
            rule["offset"] = [lo for lo, _ in offset] if all(lo == hi for lo, hi in offset) else offset
            alarms = [v.get().strip() for v in self.v_alarms if v.get().strip()]
            for alarm in alarms:
                if not (macro.BASE_DIR / alarm).is_file():
                    raise ValueError(f"알람 이미지 파일이 없습니다: {alarm}")
            if alarms:
                rule["alarm"] = alarms
            selected = [i for i, v in enumerate(self.v_ovs, 1) if v.get()]
            if not selected:
                raise ValueError("적용할 오버레이를 하나 이상 선택하세요.")
            if len(selected) < len(self.v_ovs):
                rule["overlays"] = selected  # 전체 선택이면 생략 (오버레이를 추가해도 자동 적용)
            rule["clicks"] = parse_number(self.v_clicks.get(), int, "클릭 횟수", 1)
            for key, label, (v_min, v_max) in (("delay", "반응 대기", self.v_delay), ("hold", "누르는 시간", self.v_hold),
                                               ("gap", "클릭 간격", self.v_gap)):
                lo = parse_number(v_min.get(), float, f"{label} 최소", 0)
                hi = parse_number(v_max.get(), float, f"{label} 최대", 0)
                if lo > hi:
                    raise ValueError(f"{label}: 최소가 최대보다 큽니다.")
                rule[key] = [lo, hi]
            if self.v_cooldown.get().strip():
                rule["cooldown"] = parse_number(self.v_cooldown.get(), float, "쿨다운", 0)
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e), parent=self)
            return
        self.result = rule
        self.destroy()


class AlarmDialog(ImageFieldsMixin, tk.Toplevel):
    """알람 추가/편집 창. 확인하면 self.result에 알람 dict가 들어간다."""

    def __init__(self, app, alarm=None):
        super().__init__(app.root)
        self.app = app
        self.result = None
        alarm = alarm or {}
        self.title("알람 편집" if alarm else "알람 추가")
        self.transient(app.root)
        self.resizable(False, False)
        self.v_name = tk.StringVar(value=alarm.get("name", ""))
        self.v_image = tk.StringVar(value=alarm.get("image", ""))
        self.v_threshold = tk.DoubleVar(value=alarm.get("threshold", 0.85))

        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="이름").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.v_name, width=40).grid(row=0, column=1, columnspan=3, sticky="ew")
        self._add_image_row(frm, 1, "알람 이미지", self.v_image, "alarm")
        self._add_threshold_row(frm, 3)
        ttk.Label(frm, text="어느 오버레이에서든 이 이미지가 보이면 경고음을 울리고 매크로를 정지합니다.",
                  foreground="gray").grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._add_ok_cancel(frm, 5)
        self.grab_set()

    def ok(self):
        image = self.v_image.get().strip()
        if not image or not (macro.BASE_DIR / image).is_file():
            messagebox.showerror("입력 오류", "알람 이미지 파일을 지정하세요.", parent=self)
            return
        self.result = {"name": self.v_name.get().strip() or Path(image).stem, "image": image,
                       "threshold": round(self.v_threshold.get(), 2)}
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = macro.load_config()
        self.saved = copy.deepcopy(self.cfg)
        # 이전 버전의 me 항목은 더 이상 쓰지 않음
        self.rules = [{k: v for k, v in r.items() if k != "me"} for r in self.cfg.get("rules", [])]
        self.alarms = copy.deepcopy(self.cfg.get("alarms", []))
        self.alarm_stop = threading.Event()  # 경고음 끄기
        self.events = queue.Queue()  # 다른 스레드 → GUI 스레드 (Tk는 스레드 안전하지 않음)
        self.macro = None
        self.thread = None
        self.hotkeys = (None, None)

        root.title("G_macro" + (" (관리자)" if macro.is_admin() else ""))
        root.minsize(620, 520)
        self._build()
        self.overlays = []
        self.v_regions = []  # 오버레이별 [X, Y, W, H] StringVar
        regions = self.cfg.get("regions") or ([self.cfg["region"]] if self.cfg.get("region") else [default_region()])
        for region in regions[:MAX_OVERLAYS]:
            self._create_overlay(region)
        self._load_into_ui(self.cfg)
        self._apply_hotkeys(self.cfg)

        keyboard.Listener(on_press=self._on_key, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)

    # ---------- 화면 구성 ----------

    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.Frame(root, padding=(10, 10, 10, 0))
        top.grid(row=0, column=0, sticky="ew")
        self.btn_start = ttk.Button(top, text="▶ 시작", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(top, text="■ 정지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.status = ttk.Label(top, text="정지됨", foreground="gray")
        self.status.pack(side="left", padx=10)
        ttk.Button(top, text="저장", command=self.save).pack(side="right")

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)

        nb = ttk.Notebook(paned)
        nb.add(self._build_rules_tab(nb), text="규칙")
        nb.add(self._build_alarms_tab(nb), text="알람")
        nb.add(self._build_settings_tab(nb), text="설정")
        paned.add(nb, weight=3)
        paned.add(self._build_log(paned), weight=2)

    def _build_rules_tab(self, parent):
        frm = ttk.Frame(parent, padding=8)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(0, weight=1)

        cols = ("name", "trigger", "target", "alarm", "overlays", "clicks", "threshold")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="browse", height=6)
        for col, text, width in zip(cols, ("이름", "트리거", "타깃", "알람", "오버레이", "횟수", "기준"), (100, 140, 140, 120, 60, 40, 50)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=col not in ("overlays", "clicks", "threshold"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda e: self.edit_rule())
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.config(yscrollcommand=sb.set)

        btns = ttk.Frame(frm)
        btns.grid(row=0, column=2, sticky="n", padx=(8, 0))
        for text, cmd in (("추가", self.add_rule), ("편집", self.edit_rule), ("삭제", self.delete_rule),
                          ("▲ 위로", lambda: self.move_rule(-1)), ("▼ 아래로", lambda: self.move_rule(1))):
            ttk.Button(btns, text=text, command=cmd, width=9).pack(pady=2)
        return frm

    def _build_alarms_tab(self, parent):
        frm = ttk.Frame(parent, padding=8)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)
        ttk.Label(frm, text="어느 오버레이에서든 알람 이미지가 보이면 경고음을 울리고 매크로를 정지합니다. (규칙보다 먼저 검사)",
                  foreground="gray").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        cols = ("name", "image", "threshold")
        self.alarm_tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="browse", height=6)
        for col, text, width in zip(cols, ("이름", "이미지", "기준"), (150, 300, 50)):
            self.alarm_tree.heading(col, text=text)
            self.alarm_tree.column(col, width=width, stretch=col != "threshold")
        self.alarm_tree.grid(row=1, column=0, sticky="nsew")
        self.alarm_tree.bind("<Double-1>", lambda e: self.edit_alarm())
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.alarm_tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.alarm_tree.config(yscrollcommand=sb.set)
        btns = ttk.Frame(frm)
        btns.grid(row=1, column=2, sticky="n", padx=(8, 0))
        for text, cmd in (("추가", self.add_alarm), ("편집", self.edit_alarm), ("삭제", self.delete_alarm),
                          ("소리 테스트", self.test_alarm_sound)):
            ttk.Button(btns, text=text, command=cmd, width=10).pack(pady=2)
        return frm

    def _build_settings_tab(self, parent):
        frm = ttk.Frame(parent, padding=8)
        self.v_overlay = tk.BooleanVar(value=True)
        self.v_interval = tk.StringVar()
        self.v_cooldown = tk.StringVar()
        self.v_return = tk.BooleanVar()
        self.v_debug = tk.BooleanVar()
        self.v_input = tk.StringVar()
        self.v_dd = tk.StringVar()
        self.v_start_key = tk.StringVar()
        self.v_stop_key = tk.StringVar()

        def row_label(r, text):
            ttk.Label(frm, text=text).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))

        row_label(0, "검색 영역")
        reg = ttk.Frame(frm)
        reg.grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(reg, text="오버레이 표시", variable=self.v_overlay,
                        command=self._toggle_overlay).pack(side="left")
        self.btn_add_ov = ttk.Button(reg, text=f"+ 오버레이 추가 (최대 {MAX_OVERLAYS}개)", command=self.add_overlay)
        self.btn_add_ov.pack(side="left", padx=(12, 0))
        self.ov_rows = ttk.Frame(frm)
        self.ov_rows.grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="오버레이 테두리를 마우스로 끌어 게임 창에 맞추세요. 가운데 + 표시가 내 캐릭터 위치입니다.",
                  foreground="gray").grid(row=2, column=1, sticky="w")

        row_label(3, "검사 주기(초)")
        ttk.Entry(frm, textvariable=self.v_interval, width=8).grid(row=3, column=1, sticky="w")
        row_label(4, "기본 쿨다운(초)")
        ttk.Entry(frm, textvariable=self.v_cooldown, width=8).grid(row=4, column=1, sticky="w")

        row_label(5, "입력 방식")
        inp = ttk.Frame(frm)
        inp.grid(row=5, column=1, sticky="w")
        ttk.Combobox(inp, textvariable=self.v_input, values=("sendinput", "dd"), state="readonly", width=10).pack(
            side="left")
        ttk.Label(inp, text="dd는 관리자 권한 필요", foreground="gray").pack(side="left", padx=6)

        row_label(6, "DD DLL 경로")
        dd = ttk.Frame(frm)
        dd.grid(row=6, column=1, sticky="ew")
        ttk.Entry(dd, textvariable=self.v_dd, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(dd, text="찾기", width=6, command=self.browse_dd).pack(side="left", padx=(4, 0))

        row_label(7, "단축키")
        keys = ttk.Frame(frm)
        keys.grid(row=7, column=1, sticky="w")
        ttk.Label(keys, text="시작").pack(side="left")
        ttk.Combobox(keys, textvariable=self.v_start_key, values=KEY_CHOICES, width=5).pack(side="left", padx=(2, 10))
        ttk.Label(keys, text="정지").pack(side="left")
        ttk.Combobox(keys, textvariable=self.v_stop_key, values=KEY_CHOICES, width=5).pack(side="left", padx=2)

        ttk.Checkbutton(frm, text="클릭 후 마우스를 원래 위치로", variable=self.v_return).grid(
            row=8, column=1, sticky="w", pady=(8, 2))
        ttk.Checkbutton(frm, text="디버그 로그 (매 검사마다 일치 점수 표시)", variable=self.v_debug).grid(
            row=9, column=1, sticky="w")

        ttk.Label(frm, text="※ 실행 중 변경한 설정은 정지 후 다시 시작하면 적용됩니다.", foreground="gray").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(12, 0))
        frm.columnconfigure(1, weight=1)
        return frm

    def _build_log(self, parent):
        frm = ttk.Frame(parent)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)
        head = ttk.Frame(frm)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(head, text="로그").pack(side="left")
        ttk.Button(head, text="지우기", command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")
        self.log_text = tk.Text(frm, height=8, wrap="none", font=("Consolas", 9))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.log_text.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.log_text.config(yscrollcommand=sb.set)
        return frm

    # ---------- 설정 <-> 화면 ----------

    def _load_into_ui(self, cfg):
        self.v_overlay.set(cfg.get("overlay", True))
        self._toggle_overlay()
        self._rebuild_overlay_rows()
        self.v_interval.set(str(cfg.get("interval", 0.2)))
        self.v_cooldown.set(str(cfg.get("cooldown", 1.0)))
        self.v_return.set(bool(cfg.get("return_cursor")))
        self.v_debug.set(bool(cfg.get("debug")))
        self.v_input.set(cfg.get("input", "sendinput"))
        self.v_dd.set(cfg.get("dd_dll") or "")
        self.v_start_key.set(cfg.get("start_key") or "f8")
        self.v_stop_key.set(cfg.get("stop_key") or "f12")
        self._refresh_tree()
        self._refresh_alarm_tree()

    def collect_config(self):
        """화면 값을 검증해 config dict로. 잘못된 값이면 ValueError."""
        cfg = copy.deepcopy(self.saved)  # exit_key 등 화면에 없는 키는 유지
        cfg.pop("region", None)  # 이전 버전의 단일 영역 키
        cfg["regions"] = [list(o.region) for o in self.overlays]
        cfg["overlay"] = self.v_overlay.get()
        cfg["interval"] = parse_number(self.v_interval.get(), float, "검사 주기", 0.01)
        cfg["cooldown"] = parse_number(self.v_cooldown.get(), float, "기본 쿨다운", 0)
        cfg["return_cursor"] = self.v_return.get()
        cfg["debug"] = self.v_debug.get()
        cfg["input"] = self.v_input.get()
        cfg["dd_dll"] = self.v_dd.get().strip()
        cfg["start_key"] = self.v_start_key.get().strip().lower()
        cfg["stop_key"] = self.v_stop_key.get().strip().lower()
        if not cfg["start_key"] or not cfg["stop_key"]:
            raise ValueError("시작/정지 단축키를 지정하세요.")
        if cfg["start_key"] == cfg["stop_key"]:
            raise ValueError("시작과 정지 단축키가 같습니다.")
        cfg["rules"] = copy.deepcopy(self.rules)
        cfg["alarms"] = copy.deepcopy(self.alarms)
        return cfg

    # ---------- 오버레이 ----------

    def _create_overlay(self, region):
        ov = Overlay(self.root, region, self._on_overlay_change, number=len(self.overlays) + 1)
        self.overlays.append(ov)
        self.v_regions.append([tk.StringVar() for _ in range(4)])
        self._on_overlay_change(ov)
        if not self.v_overlay.get():
            ov.withdraw()

    def _rebuild_overlay_rows(self):
        for child in self.ov_rows.winfo_children():
            child.destroy()
        running = self.is_running()
        for i, (ov, vars_) in enumerate(zip(self.overlays, self.v_regions)):
            row = ttk.Frame(self.ov_rows)
            row.pack(anchor="w", pady=1)
            tk.Label(row, text=f"■ {i + 1}", fg=ov.color, font=("맑은 고딕", 10, "bold")).pack(side="left")
            for text, var in zip(("X", "Y", "W", "H"), vars_):
                ttk.Label(row, text=text).pack(side="left", padx=(8, 2))
                entry = ttk.Entry(row, textvariable=var, width=6)
                entry.pack(side="left")
                entry.bind("<Return>", lambda e, i=i: self._apply_region_entries(i))
                entry.bind("<FocusOut>", lambda e, i=i: self._apply_region_entries(i))
            ttk.Button(row, text="삭제", width=5, command=lambda ov=ov: self.remove_overlay(ov),
                       state="disabled" if running or len(self.overlays) <= 1 else "normal").pack(side="left", padx=(8, 0))
        full = len(self.overlays) >= MAX_OVERLAYS
        self.btn_add_ov.config(state="disabled" if running or full else "normal")

    def add_overlay(self):
        if len(self.overlays) >= MAX_OVERLAYS or self.is_running():
            return
        x, y, w, h = self.overlays[-1].region
        self._create_overlay([x + 40, y + 40, w, h])
        self._rebuild_overlay_rows()

    def remove_overlay(self, ov):
        if len(self.overlays) <= 1 or self.is_running():
            return
        n = self.overlays.index(ov) + 1
        if not messagebox.askyesno("오버레이 삭제", f"오버레이 {n}을(를) 삭제할까요?"):
            return
        ov.destroy()
        del self.overlays[n - 1], self.v_regions[n - 1]
        for k, o in enumerate(self.overlays, 1):
            o.set_number(k)
        # 규칙의 오버레이 번호를 당겨서 맞춤. 남는 게 없으면 전체 적용으로
        for r in self.rules:
            if r.get("overlays"):
                nums = [k - (k > n) for k in r["overlays"] if k != n]
                if nums:
                    r["overlays"] = nums
                else:
                    del r["overlays"]
        self._refresh_tree()
        self._rebuild_overlay_rows()

    def _on_overlay_change(self, ov):
        for var, val in zip(self.v_regions[self.overlays.index(ov)], ov.region):
            var.set(str(val))

    def _apply_region_entries(self, i):
        """X/Y/W/H 입력값으로 오버레이 이동. 잘못된 값이면 원래 값으로 되돌림."""
        if i >= len(self.overlays):
            return
        ov = self.overlays[i]
        try:
            region = [int(v.get()) for v in self.v_regions[i]]
        except ValueError:
            region = ov.region
        if self.is_running():
            region = ov.region  # 실행 중에는 고정
        ov.set_region(region)
        self._on_overlay_change(ov)

    def _toggle_overlay(self):
        for ov in self.overlays:
            if self.v_overlay.get():
                ov.deiconify()
            else:
                ov.withdraw()

    def _apply_hotkeys(self, cfg):
        self.hotkeys = (macro.parse_key(cfg.get("start_key") or "f8"),
                        macro.parse_key(cfg.get("stop_key") or "f12"))
        self._update_status()

    # ---------- 규칙 ----------

    def _refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rules):
            self.tree.insert("", "end", iid=str(i),
                             values=(r.get("name", ""), r["trigger"], r["target"],
                                     ", ".join(Path(a).name for a in macro.rule_alarm_paths(r)),
                                     ",".join(map(str, r["overlays"])) if r.get("overlays") else "전체",
                                     r.get("clicks", 1), r.get("threshold", 0.85)))
        if select is not None and 0 <= select < len(self.rules):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def add_rule(self):
        dlg = RuleDialog(self)
        self.root.wait_window(dlg)
        if dlg.result:
            self.rules.append(dlg.result)
            self._refresh_tree(len(self.rules) - 1)

    def edit_rule(self):
        i = self._selected_index()
        if i is None:
            return
        dlg = RuleDialog(self, self.rules[i])
        self.root.wait_window(dlg)
        if dlg.result:
            self.rules[i] = dlg.result
            self._refresh_tree(i)

    def delete_rule(self):
        i = self._selected_index()
        if i is None:
            return
        if messagebox.askyesno("규칙 삭제", f"'{self.rules[i].get('name', '')}' 규칙을 삭제할까요?\n(이미지 파일은 지우지 않습니다)"):
            del self.rules[i]
            self._refresh_tree(min(i, len(self.rules) - 1))

    def move_rule(self, delta):
        i = self._selected_index()
        if i is None or not 0 <= i + delta < len(self.rules):
            return
        self.rules[i], self.rules[i + delta] = self.rules[i + delta], self.rules[i]
        self._refresh_tree(i + delta)

    # ---------- 알람 ----------

    def _refresh_alarm_tree(self, select=None):
        self.alarm_tree.delete(*self.alarm_tree.get_children())
        for i, a in enumerate(self.alarms):
            self.alarm_tree.insert("", "end", iid=str(i), values=(a.get("name", ""), a["image"], a.get("threshold", 0.85)))
        if select is not None and 0 <= select < len(self.alarms):
            self.alarm_tree.selection_set(str(select))

    def _selected_alarm(self):
        sel = self.alarm_tree.selection()
        return int(sel[0]) if sel else None

    def add_alarm(self):
        dlg = AlarmDialog(self)
        self.root.wait_window(dlg)
        if dlg.result:
            self.alarms.append(dlg.result)
            self._refresh_alarm_tree(len(self.alarms) - 1)

    def edit_alarm(self):
        i = self._selected_alarm()
        if i is None:
            return
        dlg = AlarmDialog(self, self.alarms[i])
        self.root.wait_window(dlg)
        if dlg.result:
            self.alarms[i] = dlg.result
            self._refresh_alarm_tree(i)

    def delete_alarm(self):
        i = self._selected_alarm()
        if i is not None and messagebox.askyesno("알람 삭제", f"'{self.alarms[i].get('name', '')}' 알람을 삭제할까요?"):
            del self.alarms[i]
            self._refresh_alarm_tree(min(i, len(self.alarms) - 1))

    def test_alarm_sound(self):
        threading.Thread(target=macro.play_alarm, args=(None, 2), daemon=True).start()

    def _on_alarm(self, name, overlay_no):
        """알람 감지: 매크로 정지 + 경고음 + 알림 창 (확인을 누르면 경고음 끔)."""
        self.stop()
        self.alarm_stop.clear()
        threading.Thread(target=macro.play_alarm, args=(self.alarm_stop,), daemon=True).start()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        messagebox.showwarning("알람", f"오버레이 {overlay_no}에서 '{name}' 알람이 감지되어 매크로를 정지했습니다.\n\n"
                                     "확인을 누르면 경고음이 꺼집니다.", parent=self.root)
        self.root.attributes("-topmost", False)
        self.alarm_stop.set()

    # ---------- 캡처 ----------

    def _grab_roi(self, dialog=None, index=0):
        """창을 숨기고 잠시 후 index번 오버레이 영역을 캡처해 드래그 영역 선택. (이미지, roi) 또는 None."""
        x, y, w, h = self.overlays[min(index, len(self.overlays) - 1)].region
        windows = [self.root] + self.overlays + ([dialog] if dialog else [])
        for win in windows:
            win.withdraw()
        self.root.update()
        try:
            time.sleep(capture.DELAY)  # 이 사이에 게임 화면을 띄운다
            image = capture.grab_region({"left": x, "top": y, "width": w, "height": h})
            roi = capture.select_roi(image)
        except Exception as e:
            messagebox.showerror("캡처 실패", str(e))
            return None
        finally:
            self.root.deiconify()
            self._toggle_overlay()
            if dialog:
                dialog.deiconify()
                dialog.lift()
                dialog.focus_force()
        return (image, roi) if roi else None

    def capture_image(self, dialog, default_name, index=0):
        if not messagebox.askokcancel(
                "화면 캡처", f"확인을 누르면 창이 숨겨지고 {capture.DELAY}초 후 오버레이 {index + 1} 영역을 캡처합니다.\n"
                "그 사이에 게임 화면을 띄우세요.\n\n캡처 창에서 영역을 드래그하고 Enter (취소: c)", parent=dialog):
            return None
        grabbed = self._grab_roi(dialog, index)
        if not grabbed:
            return None
        image, (x, y, w, h) = grabbed
        IMAGES_DIR.mkdir(exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=dialog, initialdir=IMAGES_DIR, initialfile=f"{default_name}.png",
            defaultextension=".png", filetypes=[("PNG", "*.png")])
        if not path:
            return None
        capture.save_png(image[y:y + h, x:x + w], path)
        self.log(f"이미지 저장: {path} ({w}x{h})")
        return rel_path(path)

    def browse_dd(self):
        path = filedialog.askopenfilename(filetypes=[("DLL", "*.dll"), ("모든 파일", "*.*")])
        if path:
            self.v_dd.set(rel_path(path))

    # ---------- 실행 ----------

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.is_running():
            return
        try:
            cfg = self.collect_config()
        except ValueError as e:
            messagebox.showerror("설정 오류", str(e))
            return
        if cfg["input"] == "dd" and not macro.is_admin():
            messagebox.showwarning("관리자 권한 필요",
                                   "DD 입력은 관리자 권한이 필요합니다.\n프로그램을 '관리자 권한으로 실행'으로 다시 여세요.")
            return
        self._apply_hotkeys(cfg)

        if cfg["input"] == "dd":
            self.log("DD 로드 중...")
            self.root.update_idletasks()
        try:
            m = macro.Macro(cfg, log_fn=lambda msg: self.events.put(("log", macro.timestamp(msg))),
                            on_alarm=lambda name, no: self.events.put(("alarm", (name, no))))
        except macro.MacroError as e:
            self.log(f"시작 실패: {e}")
            messagebox.showerror("시작 실패", str(e))
            return
        self.macro = m
        m.start()
        self.thread = threading.Thread(target=self._run, args=(m,), daemon=True)
        self.thread.start()
        self._update_status()

    def _run(self, m):
        try:
            m.run()
        except Exception as e:
            self.events.put(("log", macro.timestamp(f"오류로 정지: {e}")))
        finally:
            self.events.put(("ended", m))

    def stop(self):
        if self.macro:
            self.macro.shutdown()
            self.log("정지")

    def _update_status(self):
        start = (self.v_start_key.get() or "f8").upper()
        stop = (self.v_stop_key.get() or "f12").upper()
        running = self.is_running()
        for ov in self.overlays:
            ov.set_running(running)
        self._rebuild_overlay_rows()
        self.status.config(text=f"실행 중  ({stop}: 정지)" if running else f"정지됨  ({start}: 시작)",
                           foreground="green" if running else "gray")
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")

    def _on_key(self, key):
        # pynput 스레드에서 호출되므로 GUI는 직접 건드리지 않고 큐로 전달
        start_key, stop_key = self.hotkeys
        if key == start_key:
            self.events.put(("start", None))
        elif key == stop_key:
            self.events.put(("stop", None))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "start":
                    self.start()
                elif kind == "stop":
                    self.stop()
                elif kind == "alarm":
                    self._on_alarm(*payload)
                elif kind == "ended":
                    if payload is self.macro:
                        self.macro = None
                    self._update_status()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ---------- 로그 / 저장 / 종료 ----------

    def log(self, msg):
        self._append_log(macro.timestamp(msg))

    def _append_log(self, line):
        at_bottom = self.log_text.yview()[1] >= 0.999
        self.log_text.insert("end", line + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{lines - LOG_MAX_LINES}.0")
        if at_bottom:
            self.log_text.see("end")

    def save(self):
        try:
            cfg = self.collect_config()
        except ValueError as e:
            messagebox.showerror("설정 오류", str(e))
            return False
        macro.save_config(cfg)
        self.saved = copy.deepcopy(cfg)
        self._apply_hotkeys(cfg)
        self.log(f"설정 저장: {macro.CONFIG_PATH}")
        return True

    def on_close(self):
        try:
            changed = self.collect_config() != self.saved
        except ValueError:
            changed = True
        if changed:
            answer = messagebox.askyesnocancel("저장", "변경된 설정을 저장할까요?")
            if answer is None or (answer and not self.save()):
                return
        if self.macro:
            self.macro.shutdown()
        self.alarm_stop.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

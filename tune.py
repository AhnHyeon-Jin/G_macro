"""
일치 기준(threshold) 정하는 도구.

템플릿(트리거/타깃/알람 이미지)과 테스트 이미지들의 매칭 점수를 매크로와 같은 방식으로 계산한다.
테스트 이미지마다 "있음/없음"을 표시하면 두 그룹을 가르는 추천 기준을 알려준다.
단독 실행: python tune.py  /  GUI 규칙·알람 편집 창의 "점수 테스트" 버튼
"""
import base64
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np

import macro  # DPI 설정이 Tk 창보다 먼저 적용되도록 가장 먼저 import
import capture

TESTS_DIR = macro.BASE_DIR / "tests"
LABELS = {None: "?", True: "있음", False: "없음"}
NEXT_LABEL = {None: True, True: False, False: None}
MAX_MARKS = 50


def read_color(path):
    """한글 경로도 읽도록 imdecode."""
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"이미지를 읽을 수 없습니다: {path}")
    return img


def to_photo(bgr, max_w, max_h):
    """BGR 이미지를 (max_w, max_h) 안에 맞춰 줄인 PhotoImage와 배율."""
    h, w = bgr.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1.0:
        bgr = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    data = base64.b64encode(cv2.imencode(".png", bgr)[1].tobytes())
    return tk.PhotoImage(data=data), scale


def recommend(samples):
    """samples: [(정답, 최고점수)]. (추천 기준 또는 None, 설명) 반환."""
    pos = [s for label, s in samples if label is True]
    neg = [s for label, s in samples if label is False]
    if not pos and not neg:
        return None, "테스트 이미지마다 있음/없음을 표시하면 추천 기준을 계산합니다."
    if pos and neg:
        lo, hi = min(pos), max(neg)
        if lo > hi:
            # 가운데값이 너무 낮으면 테스트에 없던 화면에서 오탐할 수 있으므로 있음 최저 - 0.1 보다 낮추지 않음
            th = round(max((lo + hi) / 2, lo - 0.1), 2)
            return th, (f"있음 최저 {lo:.2f} / 없음 최고 {hi:.2f} → 추천 {th:.2f} "
                        f"(여유 {lo - hi:.2f}{', 여유가 작으니 템플릿을 더 특징적으로 자르세요' if lo - hi < 0.1 else ''})")
        th = min(round(hi + 0.01, 2), 1.0)
        missed = sum(1 for s in pos if s < th)
        return th, (f"⚠ 구분 불가: 있음 최저 {lo:.2f} ≤ 없음 최고 {hi:.2f}. 오탐을 막으면 있음 {missed}개를 놓칩니다. "
                    "템플릿을 다시 자르세요.")
    if pos:
        th = max(round(min(pos) - 0.05, 2), 0.5)
        return th, f"있음 최저 {min(pos):.2f} → 추천 {th:.2f} (없음 이미지도 추가하면 더 정확합니다)"
    th = min(round(max(neg) + 0.05, 2), 1.0)
    return th, f"없음 최고 {max(neg):.2f} → 추천 {th:.2f} 이상 (있음 이미지도 추가하면 더 정확합니다)"


class ThresholdTool(tk.Toplevel):
    """
    templates: [(이름, 경로)] 고를 수 있는 템플릿 목록
    regions: 오버레이 영역 [[x, y, w, h], ...] (화면 캡처용)
    hide_windows: 캡처할 때 숨길 창 목록을 돌려주는 함수 (GUI 본창, 오버레이 등)
    on_apply: on_apply(기준) - "기준 적용" 버튼을 누르면 호출
    """

    def __init__(self, master, templates=(), threshold=0.85, regions=None, hide_windows=None, on_apply=None):
        super().__init__(master)
        self.title("점수 테스트 - 일치 기준 정하기")
        self.minsize(900, 560)
        self.regions = regions or []
        self.hide_windows = hide_windows
        self.on_apply = on_apply
        self.template = None  # 흑백 템플릿
        self.samples = []  # {"path", "color", "gray", "res", "best", "label"}
        self.photo = None

        self.templates = [(n, p) for n, p in templates if p]
        self.v_template = tk.StringVar(value=self.templates[0][1] if self.templates else "")
        self.v_threshold = tk.DoubleVar(value=threshold)
        self._build()
        self.v_template.trace_add("write", lambda *_: self.load_template())
        self.load_template()
        self.bind("<Escape>", lambda e: self.destroy())

    # ---------- 화면 구성 ----------

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(top, text="템플릿").pack(side="left")
        names = [p for _, p in self.templates]
        ttk.Combobox(top, textvariable=self.v_template, values=names, width=50).pack(side="left", padx=6)
        ttk.Button(top, text="찾기", width=6, command=self.browse_template).pack(side="left")
        self.tpl_preview = ttk.Label(top, text="", relief="groove")
        self.tpl_preview.pack(side="left", padx=10)
        self.tpl_info = ttk.Label(top, text="", foreground="gray")
        self.tpl_info.pack(side="left")

        left = ttk.Frame(self, padding=(10, 4, 4, 4))
        left.grid(row=1, column=0, sticky="nsw")
        left.rowconfigure(0, weight=1)
        cols = ("file", "label", "best", "count")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=14)
        for col, text, width in zip(cols, ("테스트 이미지", "정답", "최고점수", "잡힌 수"), (190, 45, 65, 55)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=col == "file", anchor="w" if col == "file" else "center")
        self.tree.grid(row=0, column=0, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.redraw())
        self.tree.bind("<Double-1>", lambda e: self.toggle_label())
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.config(yscrollcommand=sb.set)

        btns = ttk.Frame(left)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="이미지 추가", command=self.add_files).pack(side="left")
        self.cap_btn = ttk.Menubutton(btns, text="화면 캡처 ▾")
        menu = tk.Menu(self.cap_btn, tearoff=False)
        for i in range(len(self.regions)):
            menu.add_command(label=f"오버레이 {i + 1}", command=lambda i=i: self.capture(i))
        menu.add_command(label="주 모니터 전체", command=lambda: self.capture(None))
        self.cap_btn["menu"] = menu
        self.cap_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="정답 바꾸기", command=self.toggle_label).pack(side="left")
        ttk.Button(btns, text="삭제", command=self.remove_sample).pack(side="left", padx=4)
        ttk.Label(left, text="정답: 더블클릭하면 ? → 있음 → 없음 순으로 바뀜\n"
                             "있음 = 템플릿이 화면에 있는 이미지, 없음 = 없는 이미지",
                  foreground="gray", justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.canvas = tk.Canvas(self, bg="#202020", highlightthickness=0)
        self.canvas.grid(row=1, column=1, sticky="nsew", padx=(4, 10), pady=4)
        self.canvas.bind("<Configure>", lambda e: self.redraw())

        bottom = ttk.Frame(self, padding=(10, 4, 10, 10))
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew")
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="일치 기준").grid(row=0, column=0, sticky="w")
        ttk.Scale(bottom, from_=0.5, to=1.0, variable=self.v_threshold,
                  command=lambda v: self.on_threshold()).grid(row=0, column=1, sticky="ew", padx=6)
        self.th_label = ttk.Label(bottom, width=5)
        self.th_label.grid(row=0, column=2)
        self.rec_label = ttk.Label(bottom, text="", wraplength=760, justify="left")
        self.rec_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btn_frm = ttk.Frame(bottom)
        btn_frm.grid(row=1, column=2, sticky="e", pady=(6, 0))
        self.rec_btn = ttk.Button(btn_frm, text="추천값으로", command=self.use_recommended, state="disabled")
        self.rec_btn.pack(side="left")
        if self.on_apply:
            ttk.Button(btn_frm, text="기준 적용", command=self.apply).pack(side="left", padx=(6, 0))
        self.recommended = None

    # ---------- 템플릿 ----------

    def browse_template(self):
        path = filedialog.askopenfilename(parent=self, initialdir=macro.BASE_DIR / "images",
                                          filetypes=[("이미지", "*.png *.jpg *.bmp"), ("모든 파일", "*.*")])
        if path:
            self.v_template.set(path)

    def load_template(self):
        self.template = None
        self.tpl_preview.config(image="", text="")
        path = self.v_template.get().strip()
        if path:
            full = (macro.BASE_DIR / path).resolve()
            try:
                self.template = macro.load_image(full)
                self._tpl_photo, _ = to_photo(read_color(full), 120, 60)
                self.tpl_preview.config(image=self._tpl_photo)
                h, w = self.template.shape
                self.tpl_info.config(text=f"{w}x{h}")
            except (macro.MacroError, ValueError) as e:
                self.tpl_info.config(text=str(e))
        else:
            self.tpl_info.config(text="템플릿 이미지를 고르세요")
        for s in self.samples:
            self._score(s)
        self.refresh()

    # ---------- 테스트 이미지 ----------

    def _score(self, s):
        """템플릿과의 매칭 결과(res)와 최고점수 계산."""
        s["res"], s["best"] = None, None
        t = self.template
        if t is None or t.shape[0] > s["gray"].shape[0] or t.shape[1] > s["gray"].shape[1]:
            return
        s["res"] = cv2.matchTemplate(s["gray"], t, cv2.TM_CCOEFF_NORMED)
        s["best"] = float(s["res"].max())

    def _add_sample(self, path, color):
        s = {"path": Path(path), "color": color, "gray": cv2.cvtColor(color, cv2.COLOR_BGR2GRAY), "label": None}
        self._score(s)
        self.samples.append(s)
        self.refresh(select=len(self.samples) - 1)

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self, initialdir=TESTS_DIR if TESTS_DIR.exists() else macro.BASE_DIR,
                                            filetypes=[("이미지", "*.png *.jpg *.bmp"), ("모든 파일", "*.*")])
        for p in paths:
            try:
                self._add_sample(p, read_color(p))
            except ValueError as e:
                messagebox.showerror("이미지 추가", str(e), parent=self)

    def capture(self, index):
        """창들을 숨기고 잠시 후 오버레이(또는 주 모니터) 영역을 캡처해 tests/ 에 저장하고 추가."""
        if not messagebox.askokcancel("화면 캡처", f"확인을 누르면 창이 숨겨지고 {capture.DELAY}초 후 캡처합니다.\n"
                                                  "그 사이에 게임 화면을 띄우세요.", parent=self):
            return
        windows = [self] + (self.hide_windows() if self.hide_windows else [])
        visible = [w for w in windows if w.winfo_exists() and w.state() == "normal"]
        for w in visible:
            w.withdraw()
        self.update()
        try:
            time.sleep(capture.DELAY)
            if index is None:
                image, _ = capture.grab_monitor(1)
            else:
                x, y, w, h = self.regions[index]
                image = capture.grab_region({"left": x, "top": y, "width": w, "height": h})
        except Exception as e:
            image = None
            messagebox.showerror("캡처 실패", str(e), parent=self)
        finally:
            for w in reversed(visible):
                w.deiconify()
            self.lift()
            self.focus_force()
        if image is None:
            return
        suffix = "full" if index is None else f"o{index + 1}"
        path = TESTS_DIR / f"test_{datetime.now():%Y%m%d_%H%M%S}_{suffix}.png"
        capture.save_png(image, path)
        self._add_sample(path, image)

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def toggle_label(self):
        i = self._selected()
        if i is not None:
            self.samples[i]["label"] = NEXT_LABEL[self.samples[i]["label"]]
            self.refresh(select=i)

    def remove_sample(self):
        i = self._selected()
        if i is not None:
            del self.samples[i]  # 파일은 지우지 않음
            self.refresh(select=min(i, len(self.samples) - 1))

    # ---------- 표시 ----------

    def _matches(self, s):
        if s["res"] is None:
            return []
        th, tw = self.template.shape
        return macro.find_peaks(s["res"].copy(), tw, th, self.v_threshold.get(), MAX_MARKS)

    def on_threshold(self):
        self.th_label.config(text=f"{self.v_threshold.get():.2f}")
        self.refresh(select=self._selected())

    def refresh(self, select=None):
        self.th_label.config(text=f"{self.v_threshold.get():.2f}")
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.samples):
            best = "-" if s["best"] is None else f"{s['best']:.3f}"
            count = len(self._matches(s)) if s["best"] is not None else "-"
            self.tree.insert("", "end", iid=str(i), values=(s["path"].name, LABELS[s["label"]], best, count))
        if select is not None and 0 <= select < len(self.samples):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

        scored = [(s["label"], s["best"]) for s in self.samples if s["best"] is not None]
        self.recommended, text = recommend(scored)
        self.rec_label.config(text=text, foreground="#c62828" if text.startswith("⚠") else "")
        self.rec_btn.config(state="normal" if self.recommended is not None else "disabled")
        self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        i = self._selected()
        if i is None:
            c.create_text(c.winfo_width() // 2, c.winfo_height() // 2, fill="#aaaaaa",
                          text="테스트 이미지를 추가하고 선택하세요\n(이미지 추가 또는 화면 캡처)", justify="center")
            return
        s = self.samples[i]
        img = s["color"].copy()
        matches = self._matches(s)
        if self.template is not None:
            th, tw = self.template.shape
            for k, ((cx, cy), score) in enumerate(matches):
                x0, y0 = cx - tw // 2, cy - th // 2
                cv2.rectangle(img, (x0, y0), (x0 + tw, y0 + th), (0, 200, 0), 3 if k == 0 else 2)
                cv2.putText(img, f"{score:.2f}", (x0, max(12, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if not matches and s["res"] is not None:
                # 기준 미달이면 가장 비슷한 위치를 빨간 박스로 표시
                _, score, _, (x0, y0) = cv2.minMaxLoc(s["res"])
                cv2.rectangle(img, (x0, y0), (x0 + tw, y0 + th), (0, 0, 220), 1)
                cv2.putText(img, f"best {score:.2f}", (x0, max(12, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        self.photo, _ = to_photo(img, max(c.winfo_width(), 50), max(c.winfo_height(), 50))
        c.create_image(c.winfo_width() // 2, c.winfo_height() // 2, image=self.photo)
        status = f"기준 {self.v_threshold.get():.2f}에서 {len(matches)}개 잡힘 (초록)" if matches else \
            "기준 미달 - 빨간 박스는 가장 비슷한 위치"
        c.create_text(8, 8, anchor="nw", fill="white", text=status)

    # ---------- 결과 ----------

    def use_recommended(self):
        if self.recommended is not None:
            self.v_threshold.set(self.recommended)
            self.on_threshold()

    def apply(self):
        self.on_apply(round(self.v_threshold.get(), 2))
        self.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    cfg = macro.load_config()
    regions = cfg.get("regions") or ([cfg["region"]] if cfg.get("region") else [])
    templates = []
    for r in cfg.get("rules", []):
        for key in ("trigger", "target"):
            if r.get(key):
                templates.append((f"{r.get('name', '')} {key}", r[key]))
        templates += [(f"{r.get('name', '')} alarm", a) for a in macro.rule_alarm_paths(r)]
    templates += [(a.get("name", ""), a["image"]) for a in cfg.get("alarms", [])]
    seen = set()
    templates = [t for t in templates if not (t[1] in seen or seen.add(t[1]))]
    tool = ThresholdTool(root, templates=templates, regions=regions)
    tool.protocol("WM_DELETE_WINDOW", root.destroy)
    tool.bind("<Destroy>", lambda e: root.destroy() if e.widget is tool else None)
    root.mainloop()


if __name__ == "__main__":
    main()

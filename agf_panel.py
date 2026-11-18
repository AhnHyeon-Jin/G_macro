"""
GUI의 AGF 탭: 몬스터 스프라이트(.AGF)를 열어 방향별 대표 이미지를 고르고 규칙 타깃으로 적용.

AGF는 프로그램 폴더의 agf/ 로 복사해 두고, 대표 이미지는 images/agf/<AGF이름>/dir1.png ... 로 저장한다.
같은 이름의 AGF로 교체하고 다시 저장하면 규칙은 그대로 새 이미지를 쓴다.
"""
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import macro
import agf_extract
import tune

AGF_DIR = macro.BASE_DIR / "agf"
OUT_DIR = macro.BASE_DIR / "images" / "agf"
SHEET_SCALE = 0.33
THUMB = 96


class AgfPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self.path = None  # 프로그램 폴더 기준 AGF 경로
        self.frames = []
        self.picks = []  # [{"dir", "frame", "mean", "min"}]
        self.v_use = []
        self.v_frame = []
        self.job = 0  # 계산 중 다른 파일을 열면 이전 결과는 버림
        self.v_start, self.v_end = tk.StringVar(), tk.StringVar()
        self.v_per_dir, self.v_dirs = tk.StringVar(), tk.StringVar()
        self.v_flip = tk.BooleanVar(value=True)
        self.v_rule = tk.StringVar()
        self._build()

    # ---------- 화면 구성 ----------

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="AGF 파일 열기...", command=self.open_file).pack(side="left")
        self.info = ttk.Label(top, text="몬스터 스프라이트(.AGF)를 열면 방향별 대표 이미지를 골라 타깃으로 쓸 수 있습니다.",
                              foreground="gray")
        self.info.pack(side="left", padx=8)

        lay = ttk.Frame(self)
        lay.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        for text, var, w in (("프레임 범위", self.v_start, 5), ("~", self.v_end, 5),
                             ("방향당 프레임", self.v_per_dir, 4), ("방향 수", self.v_dirs, 4)):
            ttk.Label(lay, text=text).pack(side="left", padx=(8 if text != "프레임 범위" and text != "~" else 2, 2))
            ttk.Entry(lay, textvariable=var, width=w).pack(side="left")
        ttk.Button(lay, text="자동 감지", command=self.auto_layout).pack(side="left", padx=(10, 2))
        self.calc_btn = ttk.Button(lay, text="대표 이미지 계산", command=self.compute)
        self.calc_btn.pack(side="left", padx=2)
        self.status = ttk.Label(lay, text="", foreground="gray")
        self.status.pack(side="left", padx=6)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.grid(row=2, column=0, sticky="nsew")
        sheet_frm = ttk.Frame(body)
        sheet_frm.rowconfigure(1, weight=1)
        sheet_frm.columnconfigure(0, weight=1)
        ttk.Label(sheet_frm, text="전체 프레임 (번호 확인용)").grid(row=0, column=0, sticky="w")
        self.sheet = tk.Canvas(sheet_frm, bg="#3c3c3c", highlightthickness=0, width=300, height=200)
        self.sheet.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(sheet_frm, orient="vertical", command=self.sheet.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.sheet.config(yscrollcommand=sb.set)
        self.sheet.bind("<MouseWheel>", lambda e: self.sheet.yview_scroll(-e.delta // 120, "units"))
        body.add(sheet_frm, weight=1)

        pick_frm = ttk.Frame(body)
        pick_frm.columnconfigure(0, weight=1)
        ttk.Label(pick_frm, text="방향별 대표 이미지 (프레임 번호를 바꿀 수 있음, 체크한 것만 저장)").grid(row=0, column=0, sticky="w")
        self.cards = ttk.Frame(pick_frm)
        self.cards.grid(row=1, column=0, sticky="nw", pady=4)
        ttk.Label(pick_frm, text="점수 = 같은 방향 다른 프레임들과의 매칭 점수 (평균 / 최저)",
                  foreground="gray").grid(row=2, column=0, sticky="w")
        body.add(pick_frm, weight=2)

        bottom = ttk.Frame(self)
        bottom.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.save_btn = ttk.Button(bottom, text="대표 이미지 저장", command=self.save, state="disabled")
        self.save_btn.pack(side="left")
        self.out_label = ttk.Label(bottom, text="", foreground="gray")
        self.out_label.pack(side="left", padx=6)
        self.apply_btn = ttk.Button(bottom, text="규칙 타깃으로 적용", command=self.apply, state="disabled")
        self.apply_btn.pack(side="right")
        ttk.Checkbutton(bottom, text="좌우 반전도 사용", variable=self.v_flip).pack(side="right", padx=6)
        self.rule_combo = ttk.Combobox(bottom, textvariable=self.v_rule, state="readonly", width=18)
        self.rule_combo.pack(side="right")
        ttk.Label(bottom, text="적용할 규칙").pack(side="right", padx=(0, 4))

    # ---------- 파일 ----------

    def open_file(self):
        src = filedialog.askopenfilename(filetypes=[("AGF", "*.agf *.AGF"), ("모든 파일", "*.*")])
        if not src:
            return
        src = Path(src).resolve()
        dst = AGF_DIR / src.name
        if src != dst.resolve():
            if dst.exists() and not messagebox.askyesno("AGF 교체", f"agf/{src.name}이(가) 이미 있습니다. 새 파일로 교체할까요?"):
                return
            AGF_DIR.mkdir(exist_ok=True)
            shutil.copy2(src, dst)
        if not self.load(dst):
            return
        self.auto_layout()
        self.compute()

    def load(self, path):
        """AGF를 읽어 전체 프레임을 표시. 성공하면 True."""
        try:
            frames = agf_extract.read_agf(path)
        except Exception as e:
            messagebox.showerror("AGF 읽기 실패", f"{path}\n\n{e}")
            return False
        self.path = Path(path)
        self.frames = frames
        self.picks = []
        self.job += 1
        self.info.config(text=f"{self._rel()}  ·  {len(frames)}프레임", foreground="")
        self.out_label.config(text=f"→ images/agf/{self.path.stem}/")
        self._draw_sheet()
        self._draw_cards()
        return True

    def _rel(self):
        return macro_rel(self.path) if self.path else ""

    def _draw_sheet(self):
        sheet = agf_extract.make_sheet(self.frames, columns=8)
        h, w = sheet.shape[:2]
        self._sheet_photo, _ = tune.to_photo(sheet, int(w * SHEET_SCALE), int(h * SHEET_SCALE))
        self.sheet.delete("all")
        self.sheet.create_image(0, 0, anchor="nw", image=self._sheet_photo)
        self.sheet.config(scrollregion=(0, 0, self._sheet_photo.width(), self._sheet_photo.height()),
                          width=self._sheet_photo.width())

    # ---------- 방향 묶음 ----------

    def _layout(self):
        start, end = int(self.v_start.get()), int(self.v_end.get())
        per_dir, dirs = int(self.v_per_dir.get()), int(self.v_dirs.get())
        if not (0 <= start <= end < len(self.frames)) or per_dir < 1 or dirs < 1:
            raise ValueError(f"프레임 범위는 0~{len(self.frames) - 1}, 방향당 프레임과 방향 수는 1 이상이어야 합니다.")
        return start, end, per_dir, dirs

    def auto_layout(self):
        if not self.frames:
            return
        start, end, per_dir, dirs = agf_extract.detect_layout(self.frames)
        for var, val in zip((self.v_start, self.v_end, self.v_per_dir, self.v_dirs), (start, end, per_dir, dirs)):
            var.set(str(val))
        self.status.config(text=f"자동 감지: {start}~{end}, 방향당 {per_dir}프레임, {dirs}방향")

    def compute(self):
        """방향별 대표 이미지를 백그라운드에서 계산 (결과는 app 이벤트 큐로 돌아옴)."""
        if not self.frames:
            return
        try:
            layout = self._layout()
        except ValueError as e:
            messagebox.showerror("방향 묶음", str(e))
            return
        self.job += 1
        job, frames = self.job, self.frames
        self.calc_btn.config(state="disabled")
        self.status.config(text="대표 이미지 계산 중...")

        def work():
            try:
                picks = agf_extract.pick_representatives(
                    frames, *layout, progress=lambda d, n: self.app.events.put(("agf", (job, "progress", (d, n)))))
                self.app.events.put(("agf", (job, "done", picks)))
            except Exception as e:
                self.app.events.put(("agf", (job, "error", str(e))))

        threading.Thread(target=work, daemon=True).start()

    def on_event(self, payload):
        """App._poll 에서 호출 (GUI 스레드)."""
        job, kind, data = payload
        if job != self.job:
            return
        if kind == "progress":
            self.status.config(text=f"대표 이미지 계산 중... {data[0]}/{data[1]} 방향")
            return
        self.calc_btn.config(state="normal")
        if kind == "error":
            self.status.config(text=f"계산 실패: {data}")
            return
        self.picks = [{"dir": d, "frame": i, "mean": m, "min": n} for d, i, m, n in data]
        self.status.config(text=f"대표 이미지 {len(self.picks)}장 계산 완료")
        self._draw_cards()

    # ---------- 대표 이미지 카드 ----------

    def _draw_cards(self, use=None):
        for child in self.cards.winfo_children():
            child.destroy()
        self._card_photos = []
        self.v_use, self.v_frame = [], []
        try:
            groups = agf_extract.direction_frames(*self._layout())
        except ValueError:
            groups = []
        for k, p in enumerate(self.picks):
            card = ttk.Frame(self.cards, relief="groove", padding=4)
            card.grid(row=k // 4, column=k % 4, padx=3, pady=3, sticky="n")
            img_label = ttk.Label(card)
            img_label.pack()
            v_use = tk.BooleanVar(value=True if use is None else use[k])
            v_frame = tk.StringVar(value=str(p["frame"]))
            row = ttk.Frame(card)
            row.pack(pady=(2, 0))
            ttk.Checkbutton(row, text=f"방향 {p['dir'] + 1}", variable=v_use).pack(side="left")
            values = groups[p["dir"]] if p["dir"] < len(groups) else [p["frame"]]
            spin = ttk.Spinbox(row, values=values, textvariable=v_frame, width=4, wrap=True,
                               command=lambda k=k: self._change_frame(k))
            spin.pack(side="left", padx=(4, 0))
            spin.bind("<Return>", lambda e, k=k: self._change_frame(k))
            score = ttk.Label(card, text=self._score_text(p), foreground="gray")
            score.pack()
            p["_img"], p["_score"] = img_label, score
            self.v_use.append(v_use)
            self.v_frame.append(v_frame)
            self._show_frame(k)
        state = "normal" if self.picks else "disabled"
        self.save_btn.config(state=state)
        self.apply_btn.config(state=state)
        self.refresh_rules()

    @staticmethod
    def _score_text(p):
        return "점수 -" if p.get("mean") is None else f"점수 {p['mean']:.2f} / {p['min']:.2f}"

    def _show_frame(self, k):
        p = self.picks[k]
        photo = tune.bgra_to_photo(self.frames[p["frame"]][2], THUMB, THUMB)
        self._card_photos.append(photo)
        p["_img"].config(image=photo)

    def _change_frame(self, k):
        try:
            frame = int(self.v_frame[k].get())
            groups = agf_extract.direction_frames(*self._layout())
        except ValueError:
            return
        p = self.picks[k]
        if frame not in groups[p["dir"]] or frame == p["frame"]:
            self.v_frame[k].set(str(p["frame"]))
            return
        p["frame"] = frame
        others = [j for j in groups[p["dir"]] if j != frame and self.frames[j][2][:, :, 3].any()] or [frame]
        scores = agf_extract.frame_scores(self.frames, frame, others)
        p["mean"], p["min"] = sum(scores) / len(scores), min(scores)
        p["_score"].config(text=self._score_text(p))
        self._show_frame(k)

    # ---------- 저장 / 적용 ----------

    def save(self):
        """체크한 대표 이미지를 images/agf/<이름>/dirN.png 로 저장. 저장된 상대경로 목록 반환."""
        chosen = [p for p, v in zip(self.picks, self.v_use) if v.get()]
        if not chosen:
            messagebox.showerror("대표 이미지 저장", "저장할 대표 이미지를 하나 이상 체크하세요.")
            return None
        out = OUT_DIR / self.path.stem
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("dir*.png"):
            old.unlink()
        paths = []
        for p in chosen:
            path = out / f"dir{p['dir'] + 1}.png"
            agf_extract.save_png(self.frames[p["frame"]][2], path)
            paths.append(macro_rel(path))
        self.app.log(f"AGF 대표 이미지 {len(paths)}장 저장: {out}")
        return paths

    def refresh_rules(self):
        names = [f"{i + 1}. {r.get('name', '')}" for i, r in enumerate(self.app.rules)]
        self.rule_combo.config(values=names)
        if self.v_rule.get() not in names:
            self.v_rule.set(names[0] if names else "")

    def apply(self):
        if not self.app.rules:
            messagebox.showinfo("규칙 타깃으로 적용", "규칙 탭에서 먼저 규칙(트리거 이미지)을 추가하세요.")
            return
        index = int(self.v_rule.get().split(".")[0]) - 1
        paths = self.save()
        if not paths:
            return
        rule = self.app.rules[index]
        rule["target"] = paths
        rule.setdefault("target_threshold", 0.5)  # 움직이는 몬스터라 트리거보다 낮게 시작
        if self.v_flip.get():
            rule["flip"] = True
        else:
            rule.pop("flip", None)
        self.app._refresh_tree(index)
        self.app.log(f"규칙 '{rule.get('name', '')}' 타깃을 AGF 대표 이미지 {len(paths)}장"
                     f"{' + 좌우 반전' if self.v_flip.get() else ''}으로 바꿨습니다. 저장을 눌러야 config에 남습니다.")
        messagebox.showinfo("규칙 타깃으로 적용", f"'{rule.get('name', '')}' 규칙의 타깃을 {len(paths)}장으로 바꿨습니다.\n"
                                           f"타깃 기준: {rule['target_threshold']} - 규칙 편집 창의 '타깃 기준' 점수 테스트로\n"
                                           "실제 게임 화면을 캡처해서 정하세요 (0.4~0.6 근처 예상).")

    # ---------- 설정 저장/복원 ----------

    def state(self):
        """config에 저장할 상태 (AGF를 연 적 없으면 None)."""
        if not self.path:
            return None
        state = {"file": macro_rel(self.path), "flip": self.v_flip.get()}
        try:
            start, end, per_dir, dirs = self._layout()
            state.update(range=[start, end], per_dir=per_dir, dirs=dirs)
        except ValueError:
            pass
        if self.picks:
            state["picks"] = [p["frame"] for p in self.picks]
            state["use"] = [v.get() for v in self.v_use]
        return state

    def load_state(self, state):
        """config의 agf 항목으로 복원 (다시 계산하지 않고 저장된 대표 프레임을 그대로 보여줌)."""
        if not state or not state.get("file"):
            return
        path = macro.BASE_DIR / state["file"]
        if not path.exists():
            self.info.config(text=f"{state['file']} 파일이 없습니다. AGF 파일을 다시 여세요.", foreground="#c62828")
            return
        self.v_flip.set(state.get("flip", True))
        if not self.load(path):
            return
        if "range" in state:
            self.v_start.set(str(state["range"][0]))
            self.v_end.set(str(state["range"][1]))
            self.v_per_dir.set(str(state["per_dir"]))
            self.v_dirs.set(str(state["dirs"]))
        else:
            self.auto_layout()
        frames = [f for f in state.get("picks", []) if 0 <= f < len(self.frames)]
        if frames:
            self.picks = [{"dir": d, "frame": f, "mean": None, "min": None} for d, f in enumerate(frames)]
            use = state.get("use") if len(state.get("use", [])) == len(frames) else None
            self._draw_cards(use)


def macro_rel(path):
    """프로그램 폴더 안이면 상대경로."""
    p = Path(path).resolve()
    try:
        return p.relative_to(macro.BASE_DIR).as_posix()
    except ValueError:
        return str(p)

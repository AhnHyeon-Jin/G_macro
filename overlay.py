"""
검색 영역을 표시하는 오버레이 창.

안쪽은 투명 + 클릭 통과(Windows transparentcolor), 테두리만 보인다.
위쪽 탭 드래그 = 이동, 테두리 드래그 = 크기 조절. 실행 중에는 잠금.
region은 테두리 안쪽(실제 검색 영역)의 화면 좌표 [x, y, w, h].
오버레이는 최대 3개, 번호마다 색이 다르다.
"""
import tkinter as tk

KEY = "#ff00fe"  # 이 색은 투명 + 클릭 통과로 처리됨
BORDER = 8  # 테두리 두께 (검색 영역 바깥)
TAB = 22  # 위쪽 이동용 탭 높이
GRIP = 18  # 모서리 판정 범위
MIN_SIZE = 100
COLORS = ["#00c853", "#2979ff", "#ff9100"]  # 오버레이 1, 2, 3
MAX_OVERLAYS = len(COLORS)

CURSORS = {
    "move": "fleur",
    "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
    "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
    "nw": "size_nw_se", "se": "size_nw_se",
    "ne": "size_ne_sw", "sw": "size_ne_sw",
}


class Overlay(tk.Toplevel):
    def __init__(self, master, region, on_change, number=1):
        super().__init__(master)
        self.on_change = on_change  # on_change(overlay) - 드래그로 영역이 바뀔 때
        self.number = number
        self.running = False
        self.drag = None
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass  # Windows 외에서는 지원 안 됨 (안쪽이 불투명하게 보임)
        self.config(bg=KEY)
        self.canvas = tk.Canvas(self, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.region = [0, 0, 0, 0]
        self.set_region(region)

    def set_region(self, region):
        x, y, w, h = (int(v) for v in region)
        self.region = [x, y, max(w, MIN_SIZE), max(h, MIN_SIZE)]
        x, y, w, h = self.region
        self.geometry(f"{w + 2 * BORDER}x{h + 2 * BORDER + TAB}+{x - BORDER}+{y - BORDER - TAB}")
        self._redraw()

    @property
    def color(self):
        return COLORS[(self.number - 1) % len(COLORS)]

    def set_number(self, number):
        self.number = number
        self._redraw()

    def set_running(self, running):
        self.running = running
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        x, y, w, h = self.region
        ww, wh = w + 2 * BORDER, h + 2 * BORDER + TAB
        color = self.color

        c.create_rectangle(0, 0, ww, TAB, fill=color, width=0)
        text = (f"{self.number} · 실행 중 · {w}x{h} (고정됨)" if self.running
                else f"{self.number} · 검색 영역 {w}x{h} · 탭: 이동 / 테두리: 크기")
        c.create_text(6, TAB // 2, text=text, anchor="w", fill="white", font=("맑은 고딕", 9, "bold"))

        top = TAB
        for x0, y0, x1, y1 in ((0, top, ww, top + BORDER), (0, wh - BORDER, ww, wh),
                               (0, top, BORDER, wh), (ww - BORDER, top, ww, wh)):
            c.create_rectangle(x0, y0, x1, y1, fill=color, width=0)

        if not self.running:
            # 가운데(내 캐릭터 위치) 표시. 실행 중에는 검색 화면에 찍히지 않도록 숨김
            cx, cy = BORDER + w // 2, top + BORDER + h // 2
            c.create_line(cx - 12, cy, cx + 13, cy, fill=color, width=2)
            c.create_line(cx, cy - 12, cx, cy + 13, fill=color, width=2)

    def _hit(self, x, y):
        if y < TAB:
            return "move"
        ww, wh = self.winfo_width(), self.winfo_height()
        edge = ("n" if y < TAB + GRIP else "s" if y > wh - GRIP else "")
        edge += ("w" if x < GRIP else "e" if x > ww - GRIP else "")
        return edge or None

    def _on_motion(self, e):
        mode = None if self.running else self._hit(e.x, e.y)
        self.canvas.config(cursor=CURSORS.get(mode, ""))

    def _on_press(self, e):
        mode = None if self.running else self._hit(e.x, e.y)
        self.drag = (mode, e.x_root, e.y_root, list(self.region)) if mode else None

    def _on_drag(self, e):
        if not self.drag:
            return
        mode, sx, sy, (x, y, w, h) = self.drag
        dx, dy = e.x_root - sx, e.y_root - sy
        if mode == "move":
            x, y = x + dx, y + dy
        else:
            if "n" in mode:
                dy = min(dy, h - MIN_SIZE)
                y, h = y + dy, h - dy
            if "s" in mode:
                h += dy
            if "w" in mode:
                dx = min(dx, w - MIN_SIZE)
                x, w = x + dx, w - dx
            if "e" in mode:
                w += dx
        self.set_region([x, y, w, h])
        self.on_change(self)

    def _on_release(self, e):
        self.drag = None

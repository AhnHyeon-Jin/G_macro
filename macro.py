"""
트리거 이미지가 화면에 보이면 타깃 이미지를 찾아 우클릭하는 매크로 (Windows 전용).

설정은 config.json, 키: 시작 = start_key, 정지 = stop_key, 프로그램 종료 = exit_key
타깃이 여러 개면 검색 영역 가운데(내 캐릭터 위치)와 가장 가까운 것을 클릭.
GUI는 gui.py, 이 파일을 직접 실행하면 콘솔 모드로 동작
"""
import copy
import ctypes
import ctypes.wintypes
import json
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np
from pynput import keyboard

if sys.platform == "win32":
    # 고DPI(125%, 150% 배율) 화면에서 캡처 좌표와 마우스 좌표가 어긋나지 않도록 설정
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class INPUT(ctypes.Structure):
    # union 중 MOUSEINPUT이 가장 커서 mi만 둬도 sizeof(INPUT)이 Windows와 일치함
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


# exe로 빌드했을 때도 config.json / images 를 실행 파일 옆에서 찾도록
BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "monitor": 1,
    "regions": [],  # 오버레이 검색 영역 [[x, y, w, h], ...] 최대 3개. 비우면 모니터 전체
    "interval": 0.2,
    "cooldown": 1.0,
    "return_cursor": False,
    "input": "sendinput",
    "dd_dll": "dd/dd63330.dll",
    "start_key": "f8",
    "stop_key": "f12",
    "exit_key": None,
    "debug": False,
    "overlay": True,
    "rules": [],
    "alarms": [],  # [{"name", "image", "threshold"}] 어느 오버레이든 보이면 소리 + 매크로 정지 (규칙별 알람은 rules[].alarm)
}


# 규칙에 hold / gap 이 없을 때 쓰는 기본 범위 (초)
DEFAULT_HOLD = [0.05, 0.1]
DEFAULT_GAP = [0.1, 0.2]
DEFAULT_DELAY = [0.0, 0.0]  # 트리거·타깃 발견 후 클릭까지 대기. 0이면 바로 클릭


class MacroError(Exception):
    """설정/이미지/DLL 문제로 매크로를 시작할 수 없을 때."""


def timestamp(msg):
    return f"[{datetime.now():%H:%M:%S}] {msg}"


def log(msg):
    print(timestamp(msg), flush=True)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rule_alarm_paths(rule):
    """규칙의 알람 이미지 경로 목록. 이전 버전은 문자열 하나로 저장했으므로 둘 다 받음."""
    alarm = rule.get("alarm") or []
    return [alarm] if isinstance(alarm, str) else [a for a in alarm if a]


def offset_ranges(rule):
    """클릭 위치 보정 [[x최소, x최대], [y최소, y최대]]. 고정값 [x, y]로 저장된 것도 받음."""
    return [sorted(v) if isinstance(v, (list, tuple)) else [v, v] for v in rule.get("offset", [0, 0])]


def load_image(path):
    """한글 경로도 읽을 수 있도록 imdecode 사용 (cv2.imread는 한글 경로 실패)."""
    full = (BASE_DIR / path).resolve()
    if not full.exists():
        raise MacroError(f"이미지 파일이 없습니다: {full}")
    img = cv2.imdecode(np.fromfile(str(full), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise MacroError(f"이미지를 읽을 수 없습니다: {full}")
    return img


def find(screen, template, threshold):
    """화면에서 template을 찾아 (중심좌표, 점수) 반환. 못 찾으면 (None, 점수)."""
    th, tw = template.shape[:2]
    if th > screen.shape[0] or tw > screen.shape[1]:
        return None, 0.0
    res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        return None, score
    return (loc[0] + tw // 2, loc[1] + th // 2), score


def find_all(screen, template, threshold, max_count=50):
    """화면에서 template과 일치하는 곳을 모두 찾아 [(중심좌표, 점수), ...] 반환 (점수 높은 순)."""
    th, tw = template.shape[:2]
    if th > screen.shape[0] or tw > screen.shape[1]:
        return []
    res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    return find_peaks(res, tw, th, threshold, max_count)


def find_peaks(res, tw, th, threshold, max_count=50):
    """matchTemplate 결과 res에서 threshold 이상인 곳을 겹치지 않게 골라 [(중심좌표, 점수), ...] 반환.
    res는 수정되므로 다시 쓸 거면 복사본을 넘길 것."""
    found = []
    while len(found) < max_count:
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score < threshold:
            break
        found.append(((x + tw // 2, y + th // 2), score))
        # 같은 대상이 1픽셀 옆에서 또 잡히지 않도록 주변을 지움
        res[max(0, y - th // 2):y + th // 2 + 1, max(0, x - tw // 2):x + tw // 2 + 1] = -1
    return found


def send_mouse(flags, dx=0, dy=0):
    """성공하면 True. 실패는 보통 UIPI(게임이 관리자 권한) 때문."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, 0, flags, 0, 0))
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def move_to(x, y):
    """SendInput 절대좌표(0~65535, 멀티모니터 전체 기준)로 커서 이동."""
    gsm = ctypes.windll.user32.GetSystemMetrics
    vx, vy = gsm(SM_XVIRTUALSCREEN), gsm(SM_YVIRTUALSCREEN)
    vw, vh = gsm(SM_CXVIRTUALSCREEN), gsm(SM_CYVIRTUALSCREEN)
    nx = round((x - vx) * 65535 / max(vw - 1, 1))
    ny = round((y - vy) * 65535 / max(vh - 1, 1))
    return send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)


class SendInputMouse:
    def move(self, x, y):
        return move_to(x, y)

    def right_down(self):
        return send_mouse(MOUSEEVENTF_RIGHTDOWN)

    def right_up(self):
        return send_mouse(MOUSEEVENTF_RIGHTUP)


class DDMouse:
    """DD 드라이버 DLL(dd*.dll)로 마우스 입력. 관리자 권한 필요."""
    BTN_INIT, BTN_R_DOWN, BTN_R_UP = 0, 4, 8

    def __init__(self, path):
        if not path.exists():
            raise MacroError(f"DD DLL 파일이 없습니다: {path}")
        self.dll = ctypes.WinDLL(str(path))
        for name, argc in (("DD_btn", 1), ("DD_mov", 2)):
            fn = getattr(self.dll, name)
            fn.argtypes = [ctypes.c_int] * argc
            fn.restype = ctypes.c_int
        time.sleep(2)  # 드라이버 로드 대기
        if self.dll.DD_btn(self.BTN_INIT) != 1:
            raise MacroError("DD 초기화 실패 - 관리자 권한으로 실행했는지 확인하세요")

    def move(self, x, y):
        self.dll.DD_mov(x, y)
        return True

    def right_down(self):
        self.dll.DD_btn(self.BTN_R_DOWN)
        return True

    def right_up(self):
        self.dll.DD_btn(self.BTN_R_UP)
        return True


_dd_cache = {}


def get_dd_mouse(dll_path):
    """GUI에서 시작/정지를 반복해도 DD는 한 번만 로드."""
    path = (BASE_DIR / dll_path).resolve()
    if path not in _dd_cache:
        _dd_cache[path] = DDMouse(path)
    return _dd_cache[path]


def play_alarm(stop_event=None, max_seconds=60):
    """경고음 반복. stop_event가 set되거나 max_seconds가 지나면 멈춤."""
    try:
        import winsound
    except ImportError:
        winsound = None
    end = time.time() + max_seconds
    while time.time() < end and not (stop_event and stop_event.is_set()):
        if winsound:
            winsound.Beep(1200, 250)
            winsound.Beep(800, 250)
        else:
            print("\a", end="", flush=True)
            time.sleep(0.5)
        time.sleep(0.3)


def right_click(mouse, x, y, return_cursor, clicks=1, hold=DEFAULT_HOLD, gap=DEFAULT_GAP):
    """(x, y)로 이동해 clicks번 우클릭. 누르는 시간은 hold, 클릭 사이 간격은 gap 범위(초)에서 랜덤."""
    if return_cursor:
        prev = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(prev))
    ok = mouse.move(x, y)
    time.sleep(0.03)
    for i in range(clicks):
        if i:
            time.sleep(random.uniform(*gap))
        ok = mouse.right_down() and ok
        time.sleep(random.uniform(*hold))
        ok = mouse.right_up() and ok
    if return_cursor:
        time.sleep(0.03)
        mouse.move(prev.x, prev.y)
    return ok


def parse_key(name):
    name = name.lower()
    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)
    return keyboard.KeyCode.from_char(name)


class Macro:
    """설정 하나로 만든 매크로 실행기. run()은 shutdown()까지 블로킹."""

    def __init__(self, cfg, log_fn=log, on_alarm=None):
        self.log = log_fn
        self.on_alarm = on_alarm  # on_alarm(알람 이름, 오버레이 번호). 없으면 경고음만
        self.interval = float(cfg.get("interval", 0.2))
        self.debug = bool(cfg.get("debug", False))
        self.return_cursor = bool(cfg.get("return_cursor", False))

        with mss.mss() as sct:
            regions = cfg.get("regions") or ([cfg["region"]] if cfg.get("region") else [])
            if regions:
                self.boxes = [{"left": x, "top": y, "width": w, "height": h} for x, y, w, h in regions]
            else:
                monitor = int(cfg.get("monitor", 1))
                if not 1 <= monitor < len(sct.monitors):
                    raise MacroError(f"모니터 {monitor}번이 없습니다 (1~{len(sct.monitors) - 1})")
                self.boxes = [dict(sct.monitors[monitor])]

        self.rules = []
        for r in cfg.get("rules", []):
            name = r.get("name") or r["target"]
            # overlays: 적용할 오버레이 번호(1부터). 없으면 전체
            boxes = [i - 1 for i in r.get("overlays") or range(1, len(self.boxes) + 1) if 1 <= i <= len(self.boxes)]
            if not boxes:
                raise MacroError(f"규칙 '{name}'을(를) 적용할 오버레이가 없습니다 (지정: {r.get('overlays')})")
            self.rules.append({
                "name": name,
                "trigger": load_image(r["trigger"]),
                "target": load_image(r["target"]),
                "threshold": r.get("threshold", 0.85),
                "offset": offset_ranges(r),
                "clicks": max(1, int(r.get("clicks", 1))),
                "hold": sorted(r.get("hold", DEFAULT_HOLD)),
                "gap": sorted(r.get("gap", DEFAULT_GAP)),
                "delay": sorted(r.get("delay", DEFAULT_DELAY)),
                "cooldown": r.get("cooldown", cfg.get("cooldown", 1.0)),
                "boxes": boxes,
                "last": [0.0] * len(self.boxes),  # 오버레이별 쿨다운
            })
        # 알람 탭의 알람은 모든 오버레이, 규칙의 알람은 그 규칙이 적용되는 오버레이에서만 검사
        all_boxes = list(range(len(self.boxes)))
        self.alarms = [{
            "name": a.get("name") or a["image"],
            "image": load_image(a["image"]),
            "threshold": a.get("threshold", 0.85),
            "boxes": all_boxes,
        } for a in cfg.get("alarms", [])]
        for r, rule in zip(cfg.get("rules", []), self.rules):
            paths = rule_alarm_paths(r)
            for k, path in enumerate(paths, 1):
                self.alarms.append({
                    "name": f"{rule['name']} 알람{k if len(paths) > 1 else ''}",
                    "image": load_image(path),
                    "threshold": rule["threshold"],
                    "boxes": rule["boxes"],
                })

        if not self.rules:
            raise MacroError("규칙이 없습니다. 규칙을 하나 이상 추가하세요.")

        input_mode = cfg.get("input", "sendinput").lower()
        if input_mode == "dd":
            if not cfg.get("dd_dll"):
                raise MacroError("DD DLL 경로가 비어 있습니다.")
            self.mouse = get_dd_mouse(cfg["dd_dll"])
        elif input_mode == "sendinput":
            self.mouse = SendInputMouse()
        else:
            raise MacroError(f"알 수 없는 input 값: {input_mode} (sendinput 또는 dd)")

        self.running = threading.Event()
        self.stop_event = threading.Event()

    def start(self):
        if not self.running.is_set():
            self.running.set()
            self.log("시작")

    def pause(self):
        if self.running.is_set():
            self.running.clear()
            self.log("정지")

    def shutdown(self):
        self.running.clear()
        self.stop_event.set()

    def run(self):
        for i, b in enumerate(self.boxes, 1):
            self.log(f"검색 영역 {i}: {b['width']}x{b['height']} @ ({b['left']}, {b['top']})")
        self.log(f"규칙 {len(self.rules)}개")
        # mss 인스턴스는 사용하는 스레드에서 만들어야 함
        with mss.mss() as sct:
            while not self.stop_event.is_set():
                if not self.running.is_set():
                    time.sleep(0.1)
                    continue
                self._check_boxes(sct)
                time.sleep(self.interval)

    def _grab(self, sct, bi):
        return cv2.cvtColor(np.array(sct.grab(self.boxes[bi])), cv2.COLOR_BGRA2GRAY)

    def _check_boxes(self, sct):
        """오버레이마다 독립적으로: 그 영역만 캡처해서 그 안에서 트리거/타깃을 찾고 그 안을 클릭."""
        # 알람은 어느 오버레이든 보이면 클릭 전에 정지해야 하므로 전체를 먼저 검사
        screens = [self._grab(sct, bi) for bi in range(len(self.boxes))]
        if any(self._check_alarms(bi, sc) for bi, sc in enumerate(screens)):
            return
        for bi in range(len(self.boxes)):
            if bi > 0:
                # 앞 오버레이에서 클릭하는 동안 화면이 바뀌었을 수 있으므로 직전에 다시 캡처
                screens[bi] = self._grab(sct, bi)
                if self._check_alarms(bi, screens[bi]):
                    return
            screen = screens[bi]
            for rule in self.rules:
                if self.stop_event.is_set() or not self.running.is_set():
                    return
                if bi in rule["boxes"] and self._check_rule(rule, bi, screen, sct):
                    screen = self._grab(sct, bi)  # 클릭으로 화면이 바뀌었으니 다음 규칙은 새 화면으로

    def _check_alarms(self, bi, screen):
        """알람 이미지가 보이면 매크로를 정지하고 True."""
        for alarm in self.alarms:
            if bi not in alarm["boxes"]:
                continue
            pos, score = find(screen, alarm["image"], alarm["threshold"])
            if pos is None:
                continue
            self.pause()
            self.log(f"⚠ 알람 [{bi + 1}] {alarm['name']} 감지 (점수 {score:.2f}) - 매크로 정지")
            if self.on_alarm:
                self.on_alarm(alarm["name"], bi + 1)
            else:
                threading.Thread(target=play_alarm, kwargs={"max_seconds": 5}, daemon=True).start()
            return True
        return False

    def _wait(self, seconds):
        """seconds 동안 대기. 도중에 정지되면 False."""
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            if self.stop_event.is_set() or not self.running.is_set():
                return False
            time.sleep(min(0.05, left))
        return True

    def _find_target(self, rule, bi, screen):
        """트리거가 보이면 가운데와 가장 가까운 타깃 (위치, 설명). 없으면 None."""
        tag = self._tag(rule, bi)
        trig_pos, trig_score = find(screen, rule["trigger"], rule["threshold"])
        if trig_pos is None:
            if self.debug:
                self.log(f"{tag}: 트리거 없음 (점수 {trig_score:.2f})")
            return None
        targets = find_all(screen, rule["target"], rule["threshold"])
        if not targets:
            if self.debug:
                self.log(f"{tag}: 트리거 감지, 타깃 없음")
            return None
        # 검색 영역 가운데(내 캐릭터 위치)와 가장 가까운 타깃 선택
        box = self.boxes[bi]
        cx, cy = box["width"] / 2, box["height"] / 2
        tgt_pos, tgt_score = min(targets, key=lambda t: (t[0][0] - cx) ** 2 + (t[0][1] - cy) ** 2)
        dist = ((tgt_pos[0] - cx) ** 2 + (tgt_pos[1] - cy) ** 2) ** 0.5
        return tgt_pos, (f"트리거 {trig_score:.2f} / 타깃 {len(targets)}개 중 가운데와 최단 {dist:.0f}px "
                         f"({tgt_score:.2f})")

    def _tag(self, rule, bi):
        return f"[{bi + 1}] {rule['name']}" if len(self.boxes) > 1 else rule["name"]

    def _check_rule(self, rule, bi, screen, sct):
        """클릭했으면 True."""
        if time.time() - rule["last"][bi] < rule["cooldown"]:
            return False
        found = self._find_target(rule, bi, screen)
        if found is None:
            return False

        if rule["delay"][1] > 0:
            # 반응 대기 후 다시 캡처해서 트리거가 아직 있는지, 타깃이 어디 있는지 새로 확인
            wait = random.uniform(*rule["delay"])
            if not self._wait(wait):
                return False
            screen = self._grab(sct, bi)
            if self._check_alarms(bi, screen):
                return False
            found = self._find_target(rule, bi, screen)
            if found is None:
                self.log(f"{self._tag(rule, bi)}: {wait:.2f}초 대기 후 트리거/타깃이 사라져 클릭 안 함")
                return False
            found = (found[0], f"{found[1]} / {wait:.2f}초 대기")

        tgt_pos, detail = found
        box = self.boxes[bi]
        (x_lo, x_hi), (y_lo, y_hi) = rule["offset"]
        x = box["left"] + tgt_pos[0] + random.randint(x_lo, x_hi)
        y = box["top"] + tgt_pos[1] + random.randint(y_lo, y_hi)
        if not right_click(self.mouse, x, y, self.return_cursor, rule["clicks"], rule["hold"], rule["gap"]):
            self.log("입력 실패 - 게임이 관리자 권한이면 매크로도 관리자로 실행하세요")
        rule["last"][bi] = time.time()
        self.log(f"{self._tag(rule, bi)}: 우클릭 {rule['clicks']}회 ({x}, {y})  {detail}")
        return True


def main():
    cfg = load_config()
    try:
        macro = Macro(cfg)
    except MacroError as e:
        sys.exit(str(e))

    start_name = cfg.get("start_key") or "f8"
    stop_name = cfg.get("stop_key") or "f12"
    exit_name = cfg.get("exit_key")  # null이면 콘솔 창을 닫거나 Ctrl+C로 종료
    start_key = parse_key(start_name)
    stop_key = parse_key(stop_name)
    exit_key = parse_key(exit_name) if exit_name else None

    def on_press(key):
        if key == start_key:
            macro.start()
        elif key == stop_key:
            macro.pause()
        elif exit_key is not None and key == exit_key:
            macro.shutdown()
            return False

    keyboard.Listener(on_press=on_press, daemon=True).start()

    exit_hint = f"[{exit_name.upper()}]" if exit_name else "창 닫기 또는 Ctrl+C"
    log(f"[{start_name.upper()}] 시작  [{stop_name.upper()}] 정지  {exit_hint} 프로그램 종료")
    macro.run()
    log("종료")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

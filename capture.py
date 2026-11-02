"""
매크로에 쓸 이미지를 화면에서 잘라 images/ 폴더에 저장하는 도구.

사용법: python capture.py [저장할이름]
  1. 실행 후 3초 안에 게임 화면을 띄워둔다
  2. 캡처된 화면에서 마우스로 영역을 드래그하고 Enter (취소는 c)
"""
import ctypes
import sys
import time
from pathlib import Path

import cv2
import mss
import numpy as np

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
DELAY = 3


def grab_monitor(monitor=1):
    """모니터 화면을 BGR 이미지로 캡처. (이미지, 모니터 정보 dict) 반환."""
    with mss.mss() as sct:
        mon = dict(sct.monitors[monitor])
        image = cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
    return image, mon


def grab_region(box):
    """화면의 box({"left", "top", "width", "height"}) 영역을 BGR 이미지로 캡처."""
    with mss.mss() as sct:
        return cv2.cvtColor(np.array(sct.grab(box)), cv2.COLOR_BGRA2BGR)


def select_roi(image):
    """드래그로 영역 선택. (x, y, w, h) 반환, 취소하면 None."""
    # OpenCV 창 제목은 한글이 깨지므로 영어로
    win = "Drag to select, Enter = save, c = cancel"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)
    x, y, w, h = cv2.selectROI(win, image, showCrosshair=True)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        return None
    return x, y, w, h


def save_png(image, path):
    """한글 경로도 저장되도록 imencode 사용."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", image)[1].tofile(str(path))


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else input("저장할 이름 (예: trigger): ").strip()
    if not name:
        sys.exit("이름이 필요합니다.")
    if not name.lower().endswith(".png"):
        name += ".png"

    for i in range(DELAY, 0, -1):
        print(f"{i}초 후 캡처...", flush=True)
        time.sleep(1)

    screen, _ = grab_monitor(1)
    print("캡처 창에서 영역을 드래그한 뒤 Enter (다시 그리려면 다시 드래그, 취소는 c)", flush=True)
    roi = select_roi(screen)
    if roi is None:
        sys.exit("취소했습니다.")

    x, y, w, h = roi
    out = BASE_DIR / "images" / name
    save_png(screen[y:y + h, x:x + w], out)
    print(f"저장 완료: {out}  ({w}x{h})")


if __name__ == "__main__":
    main()

"""
.AGF 스프라이트 파일에서 프레임 이미지를 PNG로 추출.

사용법: python agf_extract.py 파일.AGF [출력폴더]
  출력폴더 기본값: agf_frames/<파일이름>/
  frame_000.png ... (투명 배경) + sheet.png (전체 프레임 모음)

AGF 구조 (Ice_Python.AGF 분석 결과):
  0x000  uint32 x4   헤더 (마지막 값 = 프레임 수)
  0x4c0  uint32 xN   프레임별 시작 위치 (압축 해제 데이터 기준, 첫 값 0)
  0x970  int16 x4 xN 프레임별 (x, y, 폭, 높이)
  0x1030 uint32      압축 해제 크기, 0x105c uint32 압축 크기, 0x1060~ zlib 스트림
  픽셀: 4바이트 단위. 00 NN NN 00 = 투명 픽셀 NNNN개, FF R G B = 불투명 픽셀 1개
"""
import struct
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np

FRAME_OFFSETS = 0x4C0
FRAME_RECTS = 0x970
RAW_SIZE = 0x1030
ZLIB_SIZE = 0x105C
ZLIB_DATA = 0x1060


def read_agf(path):
    """[(x, y, BGRA 이미지), ...] 반환."""
    d = Path(path).read_bytes()
    count = struct.unpack_from("<I", d, 12)[0]
    offsets = list(struct.unpack_from(f"<{count}I", d, FRAME_OFFSETS))
    rects = [struct.unpack_from("<4h", d, FRAME_RECTS + 8 * i) for i in range(count)]
    raw_size = struct.unpack_from("<I", d, RAW_SIZE)[0]
    zlib_size = struct.unpack_from("<I", d, ZLIB_SIZE)[0]
    raw = zlib.decompress(d[ZLIB_DATA:ZLIB_DATA + zlib_size])
    if len(raw) != raw_size:
        raise ValueError(f"압축 해제 크기 불일치: {len(raw)} != {raw_size}")

    frames = []
    for i, (x, y, w, h) in enumerate(rects):
        end = offsets[i + 1] if i + 1 < count else len(raw)
        words = np.frombuffer(raw, dtype=np.uint8, count=end - offsets[i], offset=offsets[i]).reshape(-1, 4)
        # 투명 구간은 픽셀 수만큼 늘려서 펼침
        runs = np.where(words[:, 0] == 0, words[:, 1].astype(np.int32) | (words[:, 2].astype(np.int32) << 8), 1)
        if runs.sum() != w * h:
            raise ValueError(f"프레임 {i}: 픽셀 수 {runs.sum()} != {w}x{h}")
        pixels = np.repeat(words, runs, axis=0)
        bgra = np.zeros((w * h, 4), np.uint8)
        opaque = pixels[:, 0] == 0xFF
        bgra[opaque, 0] = pixels[opaque, 3]  # B
        bgra[opaque, 1] = pixels[opaque, 2]  # G
        bgra[opaque, 2] = pixels[opaque, 1]  # R
        bgra[opaque, 3] = 255
        frames.append((x, y, bgra.reshape(h, w, 4)))
    return frames


def _canvas_masks(frames):
    """프레임마다 (x, y) 위치에 놓은 불투명 영역 마스크 (같은 캔버스 기준이라 서로 비교 가능)."""
    W = max(x + img.shape[1] for x, y, img in frames)
    H = max(y + img.shape[0] for x, y, img in frames)
    masks = []
    for x, y, img in frames:
        m = np.zeros((H, W), bool)
        m[y:y + img.shape[0], x:x + img.shape[1]] = img[:, :, 3] > 0
        masks.append(m)
    return masks


def _iou(a, b):
    return (a & b).sum() / max((a | b).sum(), 1)


def detect_layout(frames, cut=0.65):
    """
    방향별 동작 묶음을 추정. (시작 프레임, 끝 프레임, 방향당 프레임 수, 방향 수) 반환.
    이웃 프레임 모양이 크게 바뀌는 곳을 묶음 경계로 보고, 같은 길이 묶음이 가장 길게 이어진 구간을 고른다.
    묶음이 일정 간격으로 반복되면(예: 대기 5방향 + 이동 5방향) 그 간격을 방향 수로 본다.
    """
    masks = _canvas_masks(frames)
    valid = [i for i, m in enumerate(masks) if m.any()]  # 빈 프레임 제외
    if not valid:
        return 0, len(frames) - 1, len(frames), 1
    segments, start = [], valid[0]
    for a, b in zip(valid, valid[1:]):
        if b != a + 1 or _iou(masks[a], masks[b]) < cut:
            segments.append((start, a))
            start = b
    segments.append((start, valid[-1]))

    best = None  # (묶음 개수, 시작 인덱스, 길이)
    i = 0
    while i < len(segments):
        length = segments[i][1] - segments[i][0] + 1
        j = i
        while j + 1 < len(segments) and segments[j + 1][1] - segments[j + 1][0] + 1 == length \
                and segments[j + 1][0] == segments[j][1] + 1:
            j += 1
        if length >= 4 and (best is None or j - i + 1 > best[0]):
            best = (j - i + 1, i, length)
        i = j + 1
    if best is None or best[0] < 2:
        return valid[0], valid[-1], valid[-1] - valid[0] + 1, 1

    count, i, length = best
    run = segments[i:i + count]
    dirs = count
    for p in range(1, count):
        if count % p == 0 and all(_iou(masks[run[g][0]], masks[run[g + p][0]]) > 0.8 for g in range(count - p)):
            dirs = p
            break
    return run[0][0], run[-1][1], length, dirs


def direction_frames(start, end, per_dir, dirs):
    """방향별 프레임 번호 목록. 묶음 g는 방향 g % dirs."""
    out = [[] for _ in range(dirs)]
    for g, s in enumerate(range(start, end + 1, per_dir)):
        out[g % dirs] += list(range(s, min(s + per_dir, end + 1)))
    return out


def _gray_mask(img):
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY), np.where(img[:, :, 3] > 0, 255, 0).astype(np.uint8)


def _on_background(img, rng, pad=20):
    """프레임을 무늬 배경에 합성한 흑백 이미지 (게임 화면 대용)."""
    h, w = img.shape[:2]
    bg = cv2.GaussianBlur(rng.integers(60, 140, (h + 2 * pad, w + 2 * pad, 3), dtype=np.uint8), (9, 9), 0)
    a = img[:, :, 3:4] / 255.0
    bg[pad:pad + h, pad:pad + w] = (img[:, :, :3] * a + bg[pad:pad + h, pad:pad + w] * (1 - a)).astype(np.uint8)
    return cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)


def frame_scores(frames, template_index, indices, rng=None):
    """template_index 프레임을 마스크 템플릿으로 썼을 때 indices 프레임들에서의 점수 목록."""
    rng = rng or np.random.default_rng(0)
    g, m = _gray_mask(frames[template_index][2])
    scores = []
    for j in indices:
        scene = _on_background(frames[j][2], rng)
        if g.shape[0] > scene.shape[0] or g.shape[1] > scene.shape[1]:
            scores.append(0.0)
            continue
        res = np.nan_to_num(cv2.matchTemplate(scene, g, cv2.TM_CCOEFF_NORMED, mask=m), nan=0, posinf=0, neginf=0)
        scores.append(float(res.max()))
    return scores


def pick_representatives(frames, start, end, per_dir, dirs, progress=None):
    """방향마다 같은 방향의 다른 프레임들과 평균 점수가 가장 높은 프레임을 고름.
    [(방향, 프레임 번호, 평균 점수, 최저 점수)] 반환. progress(완료 방향 수, 전체)로 진행 상황 알림."""
    picks = []
    groups = direction_frames(start, end, per_dir, dirs)
    for d, idx in enumerate(groups):
        idx = [i for i in idx if frames[i][2][:, :, 3].any()]
        best = None
        for i in idx:
            others = [j for j in idx if j != i] or [i]
            sc = frame_scores(frames, i, others, np.random.default_rng(0))
            if best is None or np.mean(sc) > best[2]:
                best = (d, i, float(np.mean(sc)), float(min(sc)))
        if best:
            picks.append(best)
        if progress:
            progress(d + 1, len(groups))
    return picks


def make_sheet(frames, columns=12, cell_bg=(60, 60, 60)):
    """프레임 번호를 붙인 모음 이미지 (BGR)."""
    cw = max(img.shape[1] for _, _, img in frames) + 8
    ch = max(img.shape[0] for _, _, img in frames) + 22
    rows = (len(frames) + columns - 1) // columns
    sheet = np.full((rows * ch, columns * cw, 3), cell_bg, np.uint8)
    for i, (_, _, img) in enumerate(frames):
        r, c = divmod(i, columns)
        h, w = img.shape[:2]
        y0, x0 = r * ch + 18, c * cw + (cw - w) // 2
        alpha = img[:, :, 3:4] / 255.0
        cell = sheet[y0:y0 + h, x0:x0 + w]
        cell[:] = (img[:, :, :3] * alpha + cell * (1 - alpha)).astype(np.uint8)
        cv2.putText(sheet, str(i), (c * cw + 4, r * ch + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return sheet


def save_png(img, path):
    cv2.imencode(".png", img)[1].tofile(str(path))  # 한글 경로도 저장되도록


def main():
    if len(sys.argv) < 2:
        sys.exit("사용법: python agf_extract.py 파일.AGF [출력폴더]")
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent / "agf_frames" / src.stem
    out.mkdir(parents=True, exist_ok=True)
    frames = read_agf(src)
    for i, (_, _, img) in enumerate(frames):
        save_png(img, out / f"frame_{i:03d}.png")
    save_png(make_sheet(frames), out / "sheet.png")
    print(f"{len(frames)}개 프레임 저장: {out}")


if __name__ == "__main__":
    main()

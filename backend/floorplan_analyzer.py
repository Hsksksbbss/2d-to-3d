"""
Floor plan analysis pipeline.

Pipeline stages:
  1. Preprocess (grayscale, denoise, adaptive threshold)
  2. OCR text extraction + text mask (Tesseract)
  3. Wall mask: thick dark strokes only (via morphological opening with wall-sized kernels)
  4. Wall vectorization: contours -> centerline polylines, merge parallel/collinear segments
  5. Room detection: flood-fill of enclosed regions in the inverse wall mask
  6. Match OCR labels to rooms (label point-in-polygon)
  7. Parse dimensions like  14'-0" X 12'-0"  and calibrate scale where possible
  8. Detect doors via arc/quarter-circle patterns + gaps
  9. Detect windows via double-line thin-rectangle patterns on walls
 10. Emit structured JSON + debug PNGs at every stage

Output units for lengths are METERS, unless otherwise stated.
Wall/door/window heights are user-configurable defaults declared as such.
"""

from __future__ import annotations

import base64
import io
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any

import cv2
import numpy as np
import pytesseract
from PIL import Image


# ------------------------- constants (labels & defaults) ------------------

DEFAULT_WALL_HEIGHT_M = 2.7            # user-configurable default
DEFAULT_DOOR_HEIGHT_M = 2.1            # user-configurable default
DEFAULT_WINDOW_HEIGHT_M = 1.2          # user-configurable default
DEFAULT_WINDOW_SILL_M = 0.9            # user-configurable default
DEFAULT_WALL_THICKNESS_M = 0.1524      # 6 inches

ROOM_KEYWORDS = [
    "kitchen", "bed room", "bedroom", "m. bed room", "master bed",
    "master bedroom", "dining", "living", "drawing", "toilet",
    "bath", "bathroom", "wc", "puja", "pooja", "foyer", "verandah",
    "veranda", "balcony", "porch", "passage", "hall", "utility",
    "study", "office", "garage", "store", "storage", "closet",
    "wardrobe", "o.t.s", "ots", "lobby", "lift", "stair",
]

# --------------------------- data models --------------------------------

@dataclass
class Point:
    x: float
    y: float


@dataclass
class WallSeg:
    id: str
    x1: float
    y1: float
    x2: float
    y2: float
    thickness_m: float
    length_m: float
    confidence: float


@dataclass
class Room:
    id: str
    name: str
    polygon: list[list[float]]      # list of [x,y] in meters
    label_position: list[float] | None
    length_m: float
    width_m: float
    area_m2: float
    confidence: float
    detected_dim_text: str | None = None
    dim_certain: bool = False


@dataclass
class Opening:
    id: str
    kind: str                    # "door" | "window"
    x: float
    y: float
    width_m: float
    orientation_deg: float
    wall_id: str | None
    confidence: float
    wall_t: float | None = None  # 0..1 position along the wall (set post-snap)


@dataclass
class DimensionText:
    text: str
    x: float
    y: float
    width_ft: float | None = None
    height_ft: float | None = None


@dataclass
class AnalysisResult:
    image_width_px: int
    image_height_px: int
    px_per_m: float
    px_per_m_confidence: float           # 0..1, low means it's a fallback guess
    walls: list[WallSeg] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)
    doors: list[Opening] = field(default_factory=list)
    windows: list[Opening] = field(default_factory=list)
    dimensions: list[DimensionText] = field(default_factory=list)
    stairs: list[dict] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    debug: dict = field(default_factory=dict)     # base64 PNGs per stage


# --------------------------- helpers ---------------------------------------

def _png_b64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return ""
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()


def _parse_dimension(txt: str) -> tuple[float | None, float | None]:
    """Parse texts like  14'-0" X 12'-0"  or 32' X 47'  ->  (14.0, 12.0) feet."""
    t = txt.upper().replace("×", "X").replace("’", "'").replace("”", '"').replace('“', '"')
    # find two feet-inch tokens separated by X
    pat = r"(\d+)\s*'\s*(?:-\s*(\d+)\s*\"?)?"
    tokens = re.findall(pat, t)
    if len(tokens) < 2 or "X" not in t:
        return None, None
    feet_vals: list[float] = []
    for ft, inch in tokens[:2]:
        f = float(ft)
        i = float(inch) if inch else 0.0
        feet_vals.append(f + i / 12.0)
    return feet_vals[0], feet_vals[1]


def _segment_length(seg: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = seg
    return math.hypot(x2 - x1, y2 - y1)


# --------------------------- pipeline -------------------------------------

class FloorPlanAnalyzer:
    def __init__(self, image_bgr: np.ndarray):
        self.img = image_bgr
        self.h, self.w = image_bgr.shape[:2]
        self.debug: dict[str, np.ndarray] = {"original": image_bgr.copy()}
        # scale gets filled by dimension calibration; fallback used if not found
        self.px_per_m: float = max(self.w, self.h) / 15.0
        self.px_per_m_confidence: float = 0.2

    # -------- stage 1: preprocess --------
    def preprocess(self) -> np.ndarray:
        gray = cv2.cvtColor(self.img, cv2.COLOR_BGR2GRAY)
        gray = cv2.medianBlur(gray, 3)
        # inverse threshold -> ink is white
        bw = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 25, 10,
        )
        self.debug["cleaned"] = bw.copy()
        self.bw = bw
        self.gray = gray
        return bw

    # -------- stage 2: OCR --------
    def run_ocr(self) -> tuple[list[DimensionText], list[dict], np.ndarray]:
        # try multiple page-segmentation modes and merge results, tesseract
        # is finicky about small labels on architectural drawings
        candidates: list[dict] = []
        for cfg in ("--psm 6", "--psm 11", "--psm 12"):
            try:
                d = pytesseract.image_to_data(
                    self.gray, output_type=pytesseract.Output.DICT, config=cfg
                )
                candidates.append(d)
            except Exception:
                continue
        if not candidates:
            candidates = [{"text": [], "left": [], "top": [], "width": [],
                           "height": [], "conf": []}]

        text_mask = np.zeros((self.h, self.w), dtype=np.uint8)
        dims: list[DimensionText] = []
        labels: list[dict] = []
        seen_texts: set[tuple[int, int, str]] = set()

        for data in candidates:
            n = len(data["text"])
            # iterate per WORD/token — grouping into lines merges labels
            # from different rooms and their bounding-boxes then erase walls
            # in between. So we paint the mask token-by-token.
            for i in range(n):
                t = (data["text"][i] or "").strip()
                if not t:
                    continue
                if len(t) < 2 and not t.replace("'", "").replace('"', "").isdigit():
                    continue
                x0 = data["left"][i]
                y0 = data["top"][i]
                w_ = data["width"][i]
                h_ = data["height"][i]
                x1_ = x0 + w_
                y1_ = y0 + h_
                cx_ = x0 + w_ / 2.0
                cy_ = y0 + h_ / 2.0
                try:
                    conf = float(data["conf"][i])
                except (ValueError, TypeError):
                    conf = 0.0
                key = (int(cx_ // 8), int(cy_ // 8), t.lower())
                if key in seen_texts:
                    continue
                seen_texts.add(key)
                pad = 3
                cv2.rectangle(text_mask,
                              (max(x0 - pad, 0), max(y0 - pad, 0)),
                              (min(x1_ + pad, self.w - 1),
                               min(y1_ + pad, self.h - 1)),
                              255, -1)
                low = t.lower()
                for kw in ROOM_KEYWORDS:
                    if kw in low:
                        labels.append({"text": t, "x": cx_, "y": cy_,
                                       "keyword": kw, "conf": conf})
                        break

            # also collect dimension strings by scanning grouped lines
            by_line: dict[tuple, list[int]] = {}
            for i in range(n):
                key = (data.get("block_num", [0]*n)[i],
                       data.get("par_num", [0]*n)[i],
                       data.get("line_num", [0]*n)[i])
                by_line.setdefault(key, []).append(i)
            for idxs in by_line.values():
                toks = [(data["text"][i] or "").strip() for i in idxs]
                toks = [t for t in toks if t]
                if not toks:
                    continue
                line_text = " ".join(toks)
                ft_w, ft_h = _parse_dimension(line_text)
                if ft_w and ft_h:
                    xs = [data["left"][i] for i in idxs]
                    ys = [data["top"][i] for i in idxs]
                    dims.append(DimensionText(
                        text=line_text,
                        x=float(np.mean(xs)),
                        y=float(np.mean(ys)),
                        width_ft=ft_w, height_ft=ft_h,
                    ))

        self.debug["text_mask"] = text_mask
        return dims, labels, text_mask

    # -------- stage 3: wall mask --------
    def wall_mask(self, text_mask: np.ndarray) -> np.ndarray:
        bw = self.bw.copy()
        # remove text
        bw[text_mask > 0] = 0

        # walls tend to be thicker than dimension / furniture lines.
        # Estimate stroke thickness distribution from the image and keep only
        # strokes at least K pixels wide (in either horizontal or vertical).
        min_wall_px = max(3, int(round(min(self.w, self.h) / 250.0)))

        # opening removes thin ink; walls survive
        k = min_wall_px
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        opened = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)

        # keep large connected components (drop remaining specks)
        num, labels_cc, stats, _ = cv2.connectedComponentsWithStats(opened, 8)
        keep = np.zeros_like(opened)
        min_area = max(80, (self.w * self.h) // 4000)
        for i in range(1, num):
            if stats[i, cv2.CC_STAT_AREA] >= min_area:
                keep[labels_cc == i] = 255

        # gentle close to bridge tiny gaps in walls (openings will still show up)
        keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        self.wall_bw = keep
        self.debug["walls_mask"] = keep
        return keep

    # -------- stage 4: wall vectorization --------
    def vectorize_walls(self) -> list[WallSeg]:
        # skeletonize + probabilistic Hough for centerlines
        try:
            from skimage.morphology import skeletonize
            skel = (skeletonize(self.wall_bw > 0).astype(np.uint8)) * 255
        except Exception:
            skel = self.wall_bw

        min_len_px = max(20, int(min(self.w, self.h) * 0.03))
        lines = cv2.HoughLinesP(
            skel, 1, np.pi / 180, threshold=40,
            minLineLength=min_len_px, maxLineGap=int(min_len_px * 0.6),
        )
        raw: list[tuple[float, float, float, float]] = []
        if lines is not None:
            for l in lines:
                arr = l[0] if hasattr(l, "__len__") and len(l) == 1 else l
                x1, y1, x2, y2 = arr[0], arr[1], arr[2], arr[3]
                raw.append((float(x1), float(y1), float(x2), float(y2)))

        merged = _merge_collinear(raw, angle_tol_deg=5.0,
                                  perp_tol_px=max(4, min_len_px // 6),
                                  gap_tol_px=min_len_px)

        # estimate thickness per wall from the wall mask
        walls: list[WallSeg] = []
        for i, seg in enumerate(merged):
            t_px = _estimate_thickness(self.wall_bw, seg)
            t_m = max(0.08, t_px / self.px_per_m) if self.px_per_m else DEFAULT_WALL_THICKNESS_M
            length_m = _segment_length(seg) / self.px_per_m if self.px_per_m else 0.0
            walls.append(WallSeg(
                id=f"w{i}",
                x1=seg[0], y1=seg[1], x2=seg[2], y2=seg[3],
                thickness_m=round(t_m, 3),
                length_m=round(length_m, 3),
                confidence=0.7,
            ))

        # debug rendering
        vis = cv2.cvtColor(self.wall_bw, cv2.COLOR_GRAY2BGR)
        for wseg in walls:
            cv2.line(vis, (int(wseg.x1), int(wseg.y1)),
                     (int(wseg.x2), int(wseg.y2)), (0, 0, 255), 2)
        self.debug["walls_vector"] = vis
        return walls

    # -------- stage 5: rooms --------
    def detect_rooms(self, labels: list[dict], dims: list[DimensionText]) -> list[Room]:
        # inverse of walls -> connected components = candidate rooms
        inv = cv2.bitwise_not(self.wall_bw)
        # trim near border so exterior is one component
        border = 2
        cv2.rectangle(inv, (0, 0), (self.w - 1, self.h - 1), 0, border)
        num, cc, stats, cents = cv2.connectedComponentsWithStats(inv, 8)
        total_area = self.w * self.h
        rooms: list[Room] = []
        # sort by area desc, skip biggest (usually exterior)
        order = sorted(range(1, num),
                       key=lambda i: -stats[i, cv2.CC_STAT_AREA])
        skipped_exterior = False
        min_room_area = max(400, total_area // 400)   # tiny gaps ignored
        max_room_area = total_area * 0.35             # exterior threshold
        idx = 0
        vis = cv2.cvtColor(self.gray, cv2.COLOR_GRAY2BGR)
        rng = np.random.default_rng(42)
        for cci in order:
            area = stats[cci, cv2.CC_STAT_AREA]
            if area < min_room_area:
                continue
            if not skipped_exterior and area > max_room_area:
                skipped_exterior = True
                continue
            mask = (cc == cci).astype(np.uint8) * 255
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            cnt = max(contours, key=cv2.contourArea)
            eps = 0.005 * cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2)
            poly_m = [[float(p[0] / self.px_per_m), float(p[1] / self.px_per_m)]
                      for p in approx]
            x, y, w, h = cv2.boundingRect(cnt)
            cx, cy = cents[cci]
            # match label if any inside
            name = f"Room {idx+1}"
            label_pos = None
            for lb in labels:
                if cv2.pointPolygonTest(cnt, (float(lb["x"]), float(lb["y"])), False) >= 0:
                    name = lb["text"].title().replace("’", "'")
                    label_pos = [float(lb["x"] / self.px_per_m),
                                 float(lb["y"] / self.px_per_m)]
                    break
            # dimension match: dim text physically inside the polygon
            dim_txt = None
            dim_certain = False
            length_m = round(h / self.px_per_m, 3)
            width_m = round(w / self.px_per_m, 3)
            for d in dims:
                if cv2.pointPolygonTest(cnt, (float(d.x), float(d.y)), False) >= 0:
                    dim_txt = d.text
                    if d.width_ft and d.height_ft:
                        length_m = round(max(d.width_ft, d.height_ft) * 0.3048, 3)
                        width_m = round(min(d.width_ft, d.height_ft) * 0.3048, 3)
                        dim_certain = True
                    break
            area_m2 = round(area / (self.px_per_m ** 2), 3)
            rooms.append(Room(
                id=f"r{idx}",
                name=name,
                polygon=poly_m,
                label_position=label_pos,
                length_m=length_m,
                width_m=width_m,
                area_m2=area_m2,
                confidence=0.6 if label_pos else 0.4,
                detected_dim_text=dim_txt,
                dim_certain=dim_certain,
            ))
            color = tuple(int(c) for c in rng.integers(60, 220, 3))
            cv2.drawContours(vis, [cnt], -1, color, 2)
            cv2.putText(vis, name, (int(cx), int(cy)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
            idx += 1
        self.debug["rooms"] = vis
        return rooms

    # -------- stage 6: doors & windows --------
    def detect_openings(self, walls: list[WallSeg]) -> tuple[list[Opening], list[Opening]]:
        # Doors: detect quarter-circle arcs via HoughCircles on cleaned image.
        # (Furniture etc might also produce circles; we filter by radius range
        # and by proximity to a wall.)
        gray = self.gray.copy()
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.5, minDist=30,
            param1=60, param2=22,
            minRadius=int(self.px_per_m * 0.25),
            maxRadius=int(self.px_per_m * 1.6),
        )
        doors: list[Opening] = []
        vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        if circles is not None:
            best_per_wall: dict[str, tuple[float, Opening]] = {}
            for i, c in enumerate(circles[0]):
                cx, cy, r = float(c[0]), float(c[1]), float(c[2])
                wall, dist = _nearest_wall((cx, cy), walls)
                if wall is None or dist > r * 0.6:
                    continue
                width_m = round(2 * r / self.px_per_m, 3)
                if width_m < 0.55 or width_m > 1.8:
                    continue
                # score by inverse distance to wall
                score = 1.0 / (1.0 + dist)
                op = Opening(
                    id=f"d{i}", kind="door",
                    x=cx / self.px_per_m, y=cy / self.px_per_m,
                    width_m=width_m,
                    orientation_deg=_wall_angle(wall),
                    wall_id=wall.id, confidence=0.6,
                )
                key = wall.id
                if key not in best_per_wall or score > best_per_wall[key][0]:
                    best_per_wall[key] = (score, op)
                cv2.circle(vis, (int(cx), int(cy)), int(r), (0, 165, 255), 1)
            for _, op in best_per_wall.values():
                doors.append(op)
        self.debug["doors"] = vis

        # Windows: gaps along walls filled with parallel thin lines - detect
        # short parallel line pairs. Simplified: look for zones on walls that
        # are non-solid in the wall mask but have parallel edges nearby.
        wvis = cv2.cvtColor(self.gray, cv2.COLOR_GRAY2BGR)
        windows: list[Opening] = []
        # We are conservative: only mark clear low-fill segments as windows
        # and require enough continuous evidence, otherwise skip. This avoids
        # peppering the wall with false-positive windows.
        edges = cv2.Canny(self.gray, 60, 160)
        for wi, w in enumerate(walls):
            length_px = _segment_length((w.x1, w.y1, w.x2, w.y2))
            if length_px < self.px_per_m * 1.2:
                continue
            samples = max(6, int(length_px // max(10, int(self.px_per_m * 0.4))))
            hits: list[tuple[int, float, float]] = []
            for s in range(1, samples - 1):
                t = s / samples
                px = w.x1 + t * (w.x2 - w.x1)
                py = w.y1 + t * (w.y2 - w.y1)
                r = 6
                x0, y0 = int(max(px-r, 0)), int(max(py-r, 0))
                x1_ = int(min(px+r, self.w-1)); y1_ = int(min(py+r, self.h-1))
                epatch = edges[y0:y1_, x0:x1_]
                wpatch = self.wall_bw[y0:y1_, x0:x1_]
                if epatch.size == 0 or wpatch.size == 0:
                    continue
                density = float(epatch.mean())
                wfill = float(wpatch.mean()) / 255.0
                if 25 < density < 120 and 0.15 < wfill < 0.5:
                    hits.append((s, px, py))
            # coalesce consecutive hits into a single window
            if hits:
                groups: list[list[tuple[int, float, float]]] = []
                cur = [hits[0]]
                for h in hits[1:]:
                    if h[0] - cur[-1][0] <= 2:
                        cur.append(h)
                    else:
                        groups.append(cur); cur = [h]
                groups.append(cur)
                # keep only groups with at least 3 hits (real windows)
                for gi, g in enumerate(groups):
                    if len(g) < 3:
                        continue
                    ax = sum(p[1] for p in g) / len(g)
                    ay = sum(p[2] for p in g) / len(g)
                    span = _segment_length((g[0][1], g[0][2], g[-1][1], g[-1][2]))
                    win_w_m = round(max(0.6, span / self.px_per_m), 3)
                    windows.append(Opening(
                        id=f"win{wi}_{gi}", kind="window",
                        x=ax / self.px_per_m, y=ay / self.px_per_m,
                        width_m=win_w_m,
                        orientation_deg=_wall_angle(w),
                        wall_id=w.id, confidence=0.55,
                    ))
                    cv2.circle(wvis, (int(ax), int(ay)), 8, (255, 200, 0), 2)
        windows = _dedupe_openings(windows, min_dist_m=1.5)
        self.debug["windows"] = wvis
        return doors, windows

    # -------- stage 7: calibrate scale from dimensions --------
    def calibrate_scale(self, dims: list[DimensionText]) -> None:
        # Rough calibration: overall building bounding box in px vs largest
        # dimension text like  32' X 47'  (usually the outermost dim).
        if not dims:
            return
        if self.wall_bw is None:
            return
        ys, xs = np.where(self.wall_bw > 0)
        if len(xs) < 100:
            return
        bbox_w_px = xs.max() - xs.min()
        bbox_h_px = ys.max() - ys.min()
        # pick the dim with the largest total feet-value as outer dimension
        best = None
        for d in dims:
            if d.width_ft and d.height_ft:
                s = d.width_ft + d.height_ft
                if best is None or s > best[0]:
                    best = (s, d)
        if best is None:
            return
        _, d = best
        d_max_ft = max(d.width_ft, d.height_ft)
        d_min_ft = min(d.width_ft, d.height_ft)
        px_max = max(bbox_w_px, bbox_h_px)
        px_min = min(bbox_w_px, bbox_h_px)
        px_per_ft_a = px_max / d_max_ft
        px_per_ft_b = px_min / d_min_ft
        # accept if both agree within 15 %
        if abs(px_per_ft_a - px_per_ft_b) / max(px_per_ft_a, px_per_ft_b) < 0.2:
            px_per_ft = (px_per_ft_a + px_per_ft_b) / 2.0
            self.px_per_m = px_per_ft / 0.3048
            self.px_per_m_confidence = 0.9

    # -------- stage 7.5: stairs --------
    def detect_stairs(self) -> list[dict]:
        """Detect stair regions from tightly-packed groups of parallel short
        line segments (tread pattern). Very conservative to avoid false
        positives on floor tiles or window blinds.
        """
        try:
            edges = cv2.Canny(self.gray, 40, 120)
            edges = cv2.bitwise_and(edges, cv2.bitwise_not(self.wall_bw))
        except Exception:
            return []
        min_len_px = max(20, int(self.px_per_m * 0.7))
        max_len_px = int(self.px_per_m * 1.8)     # a tread is < ~1.8m wide
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180, threshold=30,
            minLineLength=min_len_px,
            maxLineGap=int(min_len_px * 0.2),
        )
        if lines is None:
            return []
        segs: list[tuple[float, float, float, float, float]] = []
        for l in lines:
            arr = l[0] if len(l) == 1 else l
            x1, y1, x2, y2 = arr[0], arr[1], arr[2], arr[3]
            L = math.hypot(x2 - x1, y2 - y1)
            if L < min_len_px or L > max_len_px:
                continue
            a = _seg_angle((x1, y1, x2, y2))
            segs.append((float(x1), float(y1), float(x2), float(y2), a))
        stairs: list[dict] = []
        used = [False] * len(segs)
        min_tread_gap = self.px_per_m * 0.18   # 18 cm typical tread depth
        max_tread_gap = self.px_per_m * 0.35   # 35 cm
        for i, s in enumerate(segs):
            if used[i]:
                continue
            group = [i]
            a0 = s[4]
            ar = math.radians(a0)
            nx, ny = -math.sin(ar), math.cos(ar)
            base = nx * (s[0] + s[2]) / 2 + ny * (s[1] + s[3]) / 2
            for j in range(i + 1, len(segs)):
                if used[j]:
                    continue
                if abs(((segs[j][4] - a0 + 90) % 180) - 90) > 3:
                    continue
                pj = nx * (segs[j][0] + segs[j][2]) / 2 + ny * (segs[j][1] + segs[j][3]) / 2
                # only accept if perpendicular spacing matches tread size
                offs = [nx * (segs[k][0] + segs[k][2]) / 2 + ny * (segs[k][1] + segs[k][3]) / 2
                        for k in group]
                nearest = min(abs(pj - o) for o in offs)
                if min_tread_gap <= nearest <= max_tread_gap:
                    group.append(j)
            # need enough treads AND consistent spacing
            if len(group) < 5:
                continue
            offs = sorted(nx * (segs[k][0] + segs[k][2]) / 2 + ny * (segs[k][1] + segs[k][3]) / 2
                          for k in group)
            gaps = [offs[k+1] - offs[k] for k in range(len(offs)-1)]
            mean_gap = sum(gaps) / len(gaps)
            if not (min_tread_gap <= mean_gap <= max_tread_gap):
                continue
            std_gap = (sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) ** 0.5
            if std_gap / mean_gap > 0.35:     # tread spacing should be regular
                continue
            for k in group:
                used[k] = True
            xs = [segs[k][0] for k in group] + [segs[k][2] for k in group]
            ys = [segs[k][1] for k in group] + [segs[k][3] for k in group]
            minx, maxx = min(xs), max(xs)
            miny, maxy = min(ys), max(ys)
            # bounding box must be compact enough to be a stair
            bb_area_px = (maxx - minx) * (maxy - miny)
            plan_area_px = self.w * self.h
            if bb_area_px > plan_area_px * 0.12:
                continue
            stairs.append({
                "id": f"stair{len(stairs)}",
                "polygon_m": [
                    [minx / self.px_per_m, miny / self.px_per_m],
                    [maxx / self.px_per_m, miny / self.px_per_m],
                    [maxx / self.px_per_m, maxy / self.px_per_m],
                    [minx / self.px_per_m, maxy / self.px_per_m],
                ],
                "treads": len(group),
                "angle_deg": a0,
                "confidence": 0.6,
            })
        return stairs

    # -------- orchestrate --------
    def run(self) -> AnalysisResult:
        self.preprocess()
        dims, labels, text_mask = self.run_ocr()
        self.wall_mask(text_mask)
        self.calibrate_scale(dims)
        walls = self.vectorize_walls()
        rooms = self.detect_rooms(labels, dims)
        doors, windows = self.detect_openings(walls)
        stairs = self.detect_stairs()

        # snap doors/windows to their wall centerlines and remember parametric
        # position along the wall (t in [0,1]) so the frontend can cut walls.
        doors = _snap_openings_to_walls(doors, walls, self.px_per_m)
        windows = _snap_openings_to_walls(windows, walls, self.px_per_m)

        debug_b64 = {name: _png_b64(img) for name, img in self.debug.items()}

        result = AnalysisResult(
            image_width_px=self.w,
            image_height_px=self.h,
            px_per_m=self.px_per_m,
            px_per_m_confidence=self.px_per_m_confidence,
            walls=walls,
            rooms=rooms,
            doors=doors,
            windows=windows,
            dimensions=dims,
            stairs=stairs,
            defaults={
                "wall_height_m": DEFAULT_WALL_HEIGHT_M,
                "door_height_m": DEFAULT_DOOR_HEIGHT_M,
                "window_height_m": DEFAULT_WINDOW_HEIGHT_M,
                "window_sill_m": DEFAULT_WINDOW_SILL_M,
                "wall_thickness_m": DEFAULT_WALL_THICKNESS_M,
                "note": (
                    "Wall/door/window heights and wall thickness are defaults, "
                    "not measured from the drawing. Adjust in the viewer."
                ),
            },
            debug=debug_b64,
        )
        return result


# --------------------------- geometry helpers -----------------------------

def _seg_angle(seg: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = seg
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0


def _merge_collinear(segs: list[tuple[float, float, float, float]],
                     angle_tol_deg: float, perp_tol_px: float,
                     gap_tol_px: float) -> list[tuple[float, float, float, float]]:
    if not segs:
        return []
    groups: list[dict[str, Any]] = []
    for s in segs:
        a = _seg_angle(s)
        # perpendicular distance from origin
        x1, y1, x2, y2 = s
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        offset = nx * x1 + ny * y1
        matched = None
        for g in groups:
            if abs(((a - g["angle"] + 90) % 180) - 90) <= angle_tol_deg:
                if abs(offset - g["offset"]) <= perp_tol_px:
                    matched = g
                    break
        if matched is None:
            groups.append({"angle": a, "offset": offset, "segs": [s]})
        else:
            matched["segs"].append(s)
    merged = []
    for g in groups:
        # project all endpoints onto the line direction and take min/max
        a = math.radians(g["angle"])
        dx, dy = math.cos(a), math.sin(a)
        pts = []
        for s in g["segs"]:
            pts.append((s[0], s[1]))
            pts.append((s[2], s[3]))
        ts = [p[0] * dx + p[1] * dy for p in pts]
        i_min = int(np.argmin(ts))
        i_max = int(np.argmax(ts))
        merged.append((pts[i_min][0], pts[i_min][1], pts[i_max][0], pts[i_max][1]))
    return merged


def _estimate_thickness(mask: np.ndarray, seg: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = seg
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / L, dx / L
    samples = []
    steps = 15
    for i in range(1, steps):
        t = i / steps
        px = x1 + t * dx
        py = y1 + t * dy
        # walk perpendicular both ways until leaving the mask
        w = 0
        for k in range(1, 40):
            x = int(round(px + nx * k))
            y = int(round(py + ny * k))
            if 0 <= x < mask.shape[1] and 0 <= y < mask.shape[0] and mask[y, x] > 0:
                w += 1
            else:
                break
        for k in range(1, 40):
            x = int(round(px - nx * k))
            y = int(round(py - ny * k))
            if 0 <= x < mask.shape[1] and 0 <= y < mask.shape[0] and mask[y, x] > 0:
                w += 1
            else:
                break
        samples.append(w)
    if not samples:
        return 4.0
    samples.sort()
    return float(samples[len(samples)//2] + 1)


def _wall_angle(w: WallSeg) -> float:
    return math.degrees(math.atan2(w.y2 - w.y1, w.x2 - w.x1))


def _nearest_wall(pt: tuple[float, float], walls: list[WallSeg]):
    best = None
    best_d = float("inf")
    for w in walls:
        d = _point_seg_dist(pt, (w.x1, w.y1, w.x2, w.y2))
        if d < best_d:
            best_d = d
            best = w
    return best, best_d


def _point_seg_dist(p, s):
    x1, y1, x2, y2 = s
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0]-x1, p[1]-y1)
    t = max(0.0, min(1.0, ((p[0]-x1)*dx + (p[1]-y1)*dy) / L2))
    return math.hypot(p[0]-(x1+t*dx), p[1]-(y1+t*dy))


def _dedupe_openings(items: list[Opening], min_dist_m: float) -> list[Opening]:
    kept: list[Opening] = []
    for it in items:
        collision = False
        for k in kept:
            if math.hypot(it.x - k.x, it.y - k.y) < min_dist_m:
                collision = True
                break
        if not collision:
            kept.append(it)
    return kept


def _snap_openings_to_walls(items: list[Opening], walls: list[WallSeg],
                            px_per_m: float) -> list[Opening]:
    """Project each opening onto its owning wall centerline and record the
    parametric position along the wall (0..1). Coordinates in meters.
    Returns opening list with .x/.y updated to the wall centerline.
    """
    wall_by_id = {w.id: w for w in walls}
    out: list[Opening] = []
    for op in items:
        w = wall_by_id.get(op.wall_id or "")
        if not w:
            out.append(op)
            continue
        # convert wall endpoints to meters
        wx1, wy1 = w.x1 / px_per_m, w.y1 / px_per_m
        wx2, wy2 = w.x2 / px_per_m, w.y2 / px_per_m
        dx, dy = wx2 - wx1, wy2 - wy1
        L2 = dx * dx + dy * dy
        if L2 <= 1e-9:
            out.append(op)
            continue
        t = max(0.0, min(1.0, ((op.x - wx1) * dx + (op.y - wy1) * dy) / L2))
        nx = wx1 + t * dx
        ny = wy1 + t * dy
        # store t in wall_id-tagged position; overwrite x/y with snapped coords.
        new = Opening(
            id=op.id, kind=op.kind, x=nx, y=ny,
            width_m=op.width_m, orientation_deg=op.orientation_deg,
            wall_id=op.wall_id, confidence=op.confidence,
        )
        # attach parametric position along the wall
        new.wall_t = float(t)
        out.append(new)
    return out


# --------------------------- public API -----------------------------------

def analyze_image(image_bytes: bytes) -> dict:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        img = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    # cap dimensions for performance
    max_side = 1600
    if max(img.shape[:2]) > max_side:
        scale = max_side / max(img.shape[:2])
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    result = FloorPlanAnalyzer(img).run()
    # dataclass -> dict
    def _dc(x):
        if hasattr(x, "__dataclass_fields__"):
            return {k: _dc(v) for k, v in asdict(x).items()}
        return x
    return _dc(result)

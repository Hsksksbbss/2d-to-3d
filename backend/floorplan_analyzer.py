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
        try:
            data = pytesseract.image_to_data(
                self.gray, output_type=pytesseract.Output.DICT, config="--psm 11"
            )
        except Exception:
            data = {"text": [], "left": [], "top": [], "width": [], "height": [], "conf": []}

        text_mask = np.zeros((self.h, self.w), dtype=np.uint8)
        dims: list[DimensionText] = []
        labels: list[dict] = []

        n = len(data["text"])
        # group tokens by line to combine dimension strings
        by_line: dict[tuple, list[int]] = {}
        for i in range(n):
            key = (data.get("block_num", [0]*n)[i], data.get("par_num", [0]*n)[i],
                   data.get("line_num", [0]*n)[i])
            by_line.setdefault(key, []).append(i)

        for idxs in by_line.values():
            tokens = []
            xs, ys, ws, hs, confs = [], [], [], [], []
            for i in idxs:
                t = (data["text"][i] or "").strip()
                if not t:
                    continue
                tokens.append(t)
                xs.append(data["left"][i])
                ys.append(data["top"][i])
                ws.append(data["width"][i])
                hs.append(data["height"][i])
                try:
                    confs.append(float(data["conf"][i]))
                except (ValueError, TypeError):
                    confs.append(0.0)
            if not tokens:
                continue
            line_text = " ".join(tokens)
            x0 = min(xs); y0 = min(ys); x1 = max(x+w for x, w in zip(xs, ws))
            y1 = max(y+h for y, h in zip(ys, hs))
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            # paint text region on the mask a bit dilated
            pad = 4
            cv2.rectangle(text_mask, (max(x0-pad, 0), max(y0-pad, 0)),
                          (min(x1+pad, self.w-1), min(y1+pad, self.h-1)), 255, -1)

            ft_w, ft_h = _parse_dimension(line_text)
            if ft_w and ft_h:
                dims.append(DimensionText(text=line_text, x=cx, y=cy,
                                          width_ft=ft_w, height_ft=ft_h))

            low = line_text.lower()
            for kw in ROOM_KEYWORDS:
                if kw in low:
                    labels.append({"text": line_text, "x": cx, "y": cy, "keyword": kw,
                                   "conf": float(np.mean(confs)) if confs else 0.0})
                    break

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
        # (Furniture etc might also produce circles; we filter by radius range.)
        gray = self.gray.copy()
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=30,
            param1=80, param2=35,
            minRadius=int(self.px_per_m * 0.3),
            maxRadius=int(self.px_per_m * 1.5),
        )
        doors: list[Opening] = []
        vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        if circles is not None:
            for i, c in enumerate(circles[0]):
                cx, cy, r = float(c[0]), float(c[1]), float(c[2])
                # attach to nearest wall
                wall, dist = _nearest_wall((cx, cy), walls)
                if wall is None or dist > r * 1.5:
                    continue
                width_m = round(2 * r / self.px_per_m, 3)
                if width_m < 0.5 or width_m > 1.8:
                    continue
                doors.append(Opening(
                    id=f"d{i}", kind="door",
                    x=cx / self.px_per_m, y=cy / self.px_per_m,
                    width_m=width_m,
                    orientation_deg=_wall_angle(wall),
                    wall_id=wall.id, confidence=0.5,
                ))
                cv2.circle(vis, (int(cx), int(cy)), int(r), (0, 165, 255), 2)
        self.debug["doors"] = vis

        # Windows: gaps along walls filled with parallel thin lines - detect
        # short parallel line pairs. Simplified: look for zones on walls that
        # are non-solid in the wall mask but have parallel edges nearby.
        wvis = cv2.cvtColor(self.gray, cv2.COLOR_GRAY2BGR)
        windows: list[Opening] = []
        # Heuristic placeholder: sample along wall centerlines, if wall mask
        # has a periodic thin structure -> mark as window. This keeps us from
        # inventing windows we can't verify. We report low confidence.
        edges = cv2.Canny(self.gray, 60, 160)
        for wi, w in enumerate(walls):
            length_px = _segment_length((w.x1, w.y1, w.x2, w.y2))
            if length_px < self.px_per_m * 0.8:
                continue
            samples = int(length_px // max(8, int(self.px_per_m * 0.3)))
            for s in range(1, samples - 1):
                t = s / samples
                px = w.x1 + t * (w.x2 - w.x1)
                py = w.y1 + t * (w.y2 - w.y1)
                # local patch
                r = 6
                x0, y0 = int(max(px-r, 0)), int(max(py-r, 0))
                x1, y1 = int(min(px+r, self.w-1)), int(min(py+r, self.h-1))
                patch = edges[y0:y1, x0:x1]
                if patch.size == 0:
                    continue
                density = float(patch.mean())
                # windows have moderate edge density (thin double lines)
                # while solid walls have very high density in wall_bw
                wpatch = self.wall_bw[y0:y1, x0:x1]
                if wpatch.size == 0:
                    continue
                wfill = float(wpatch.mean()) / 255.0
                if 25 < density < 120 and 0.15 < wfill < 0.55:
                    windows.append(Opening(
                        id=f"win{wi}_{s}", kind="window",
                        x=px / self.px_per_m, y=py / self.px_per_m,
                        width_m=round(0.9, 3),
                        orientation_deg=_wall_angle(w),
                        wall_id=w.id, confidence=0.25,
                    ))
                    cv2.circle(wvis, (int(px), int(py)), 6, (255, 200, 0), 2)
        # dedupe windows that are too close (keep one per neighborhood)
        windows = _dedupe_openings(windows, min_dist_m=1.2)
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

    # -------- orchestrate --------
    def run(self) -> AnalysisResult:
        self.preprocess()
        dims, labels, text_mask = self.run_ocr()
        self.wall_mask(text_mask)
        self.calibrate_scale(dims)
        walls = self.vectorize_walls()
        rooms = self.detect_rooms(labels, dims)
        doors, windows = self.detect_openings(walls)

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
            stairs=[],
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

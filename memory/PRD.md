# Plan → Space (Floor Plan → 3D)

## Problem statement (verbatim summary)
The previous implementation converted every drawing line into a thick wall and
reported 160 walls / 0 rooms / 0 doors / 0 windows. This session rebuilds
the analysis pipeline so that walls, rooms, doors, windows are properly
detected and the 3D result looks like a real house.

## Architecture
- **Backend**: FastAPI + OpenCV (headless) + Tesseract OCR + scikit-image.
  Route: `POST /api/floorplan/analyze` returns JSON: `walls`, `rooms`,
  `doors`, `windows`, `dimensions`, `defaults`, plus base64 debug images
  for each pipeline stage.
- **Frontend**: React + Tailwind + shadcn/ui + Three.js (three, @react-three/fiber, @react-three/drei).
- **Storage**: MongoDB `floorplans` collection (image base64 + analysis JSON).

## What's implemented (Feb 2026)
- CV pipeline: grayscale → adaptive threshold → OCR text-mask → morphological
  wall isolation → connected components → Hough centerline → line merging.
- Room detection via inverse-wall CC flood fill, exterior filtered by size.
- Room name matching via OCR labels (KITCHEN, BED ROOM, LIVING, …).
- Dimension parsing (`14'-0" X 12'-0"`) + px/m scale calibration.
- Door detection via Hough circles (quarter-circle door swings).
- Window detection along wall centerlines (low confidence, deduped).
- 3D viewer with orbit / top / front / back / left / right presets,
  clickable rooms, isolate mode, material controls (wall/floor/door/window
  colors, wall height & thickness defaults with disclosure).
- Debug pipeline tab showing every stage as an image.

## Non-goals for this iteration
- Stairs detection (placeholder empty list).
- Cutting exact door/window openings out of wall geometry (visual overlay).
- Furniture detection (materials toggle exists but detection unreliable).

## Backlog (P0/P1)
- P1: Cut openings from wall geometry (BSP / union).
- P1: Actual stairs detection from parallel tread lines.
- P2: Save plans list + reload prior analyses (`GET /api/floorplans` already exists).
- P2: Auto sample floor-plan (baked into image).

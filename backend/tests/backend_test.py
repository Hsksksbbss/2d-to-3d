"""Backend tests for Floor Plan -> 3D API (/api/floorplan/*)."""
import io
import os

import cv2
import numpy as np
import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

SAMPLE_PATH = "/tmp/sample.png"


def _build_sample():
    img = np.full((800, 1000, 3), 255, dtype=np.uint8)
    cv2.rectangle(img, (100, 100), (900, 700), (0, 0, 0), 8)
    cv2.line(img, (500, 100), (500, 400), (0, 0, 0), 6)
    cv2.line(img, (100, 400), (900, 400), (0, 0, 0), 6)
    cv2.line(img, (700, 400), (700, 700), (0, 0, 0), 6)
    cv2.ellipse(img, (500, 400), (60, 60), 0, 180, 270, (0, 0, 0), 2)
    for t, x, y, s in [("KITCHEN", 200, 250, 1.0), ("BED ROOM", 620, 250, 1.0),
                       ("LIVING", 300, 550, 1.0), ("DINING", 750, 550, 0.8)]:
        cv2.putText(img, t, (x, y), cv2.FONT_HERSHEY_SIMPLEX, s, (0, 0, 0), 2)
    cv2.imwrite(SAMPLE_PATH, img)


@pytest.fixture(scope="session")
def sample_bytes():
    if not os.path.exists(SAMPLE_PATH):
        _build_sample()
    with open(SAMPLE_PATH, "rb") as f:
        return f.read()


@pytest.fixture(scope="session")
def api_client():
    return requests.Session()


@pytest.fixture(scope="session")
def analysis(api_client, sample_bytes):
    r = api_client.post(
        f"{BASE_URL}/api/floorplan/analyze",
        files={"file": ("sample.png", io.BytesIO(sample_bytes), "image/png")},
        timeout=180,
    )
    if r.status_code != 200:
        pytest.fail(f"analyze failed {r.status_code}: {r.text[:600]}")
    return r.json()


# --- health ---
class TestHealth:
    def test_root(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/", timeout=30)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# --- POST /api/floorplan/analyze ---
class TestAnalyze:
    def test_response_shape(self, analysis):
        assert isinstance(analysis.get("id"), str) and analysis["id"]
        assert analysis["image"].startswith("data:image/png;base64,")
        a = analysis["analysis"]
        for k in ["walls", "rooms", "doors", "windows", "dimensions", "defaults", "debug"]:
            assert k in a, f"missing key {k}"
        assert isinstance(a["walls"], list)
        assert isinstance(a["defaults"], dict)
        assert isinstance(a["debug"], dict)

    def test_no_mongo_id_leak(self, analysis):
        assert "_id" not in analysis

    def test_sane_counts(self, analysis):
        a = analysis["analysis"]
        nw, nr = len(a["walls"]), len(a["rooms"])
        assert 4 <= nw <= 30, f"walls={nw} out of sane range"
        assert nr >= 3, f"rooms={nr} < 3"
        assert len(a["doors"]) >= 0
        assert len(a["windows"]) >= 0

    def test_wall_geometry(self, analysis):
        for w in analysis["analysis"]["walls"]:
            for key in ["x1", "y1", "x2", "y2", "thickness_m"]:
                assert key in w, f"wall missing {key}: {w}"
            assert w["thickness_m"] > 0
            assert (w["x1"], w["y1"]) != (w["x2"], w["y2"])

    def test_room_fields(self, analysis):
        for r in analysis["analysis"]["rooms"]:
            for key in ["id", "name", "length_m", "width_m", "area_m2", "confidence"]:
                assert key in r, f"room missing {key}: {r}"
            assert r["area_m2"] > 0
            assert 0 <= r["confidence"] <= 1
        ids = [r["id"] for r in analysis["analysis"]["rooms"]]
        assert len(ids) == len(set(ids)), "duplicate room ids"

    def test_room_labels_from_ocr(self, analysis):
        names = " ".join(r["name"].upper() for r in analysis["analysis"]["rooms"])
        found = [n for n in ["KITCHEN", "BED", "LIVING", "DINING"] if n in names]
        assert found, f"no OCR room labels matched, names={names}"

    def test_debug_stages(self, analysis):
        dbg = analysis["analysis"]["debug"]
        for stage in ["original", "cleaned", "text_mask", "walls_mask",
                      "walls_vector", "rooms", "doors", "windows"]:
            assert stage in dbg, f"debug stage missing: {stage}"
            assert str(dbg[stage]).startswith("data:image"), f"stage {stage} not a data url"

    def test_defaults_and_scale(self, analysis):
        a = analysis["analysis"]
        d = a["defaults"]
        assert abs(d.get("wall_thickness_m", 0) - 0.1524) < 1e-6, d
        assert d.get("wall_height_m", 0) > 0
        assert a.get("px_per_m", 0) > 0
        assert 0 <= a.get("px_per_m_confidence", 0) <= 1


# --- validation / error handling ---
class TestValidation:
    def test_non_image_rejected(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/floorplan/analyze",
            files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
            timeout=60,
        )
        assert r.status_code == 400, r.text[:300]

    def test_missing_file(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/floorplan/analyze", timeout=60)
        assert r.status_code == 422

    def test_empty_image(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/floorplan/analyze",
            files={"file": ("a.png", io.BytesIO(b""), "image/png")},
            timeout=60,
        )
        assert r.status_code == 400, r.text[:300]

    def test_corrupt_image(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/floorplan/analyze",
            files={"file": ("a.png", io.BytesIO(b"notanimage" * 20), "image/png")},
            timeout=60,
        )
        assert r.status_code in (400, 422, 500), r.status_code


# --- GET /api/floorplan/{id} + persistence, GET /api/floorplans ---
class TestPersistence:
    def test_get_by_id(self, api_client, analysis):
        pid = analysis["id"]
        r = api_client.get(f"{BASE_URL}/api/floorplan/{pid}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "_id" not in data
        assert data["id"] == pid
        assert len(data["analysis"]["rooms"]) == len(analysis["analysis"]["rooms"])
        assert len(data["analysis"]["walls"]) == len(analysis["analysis"]["walls"])
        assert "debug" in data["analysis"]

    def test_get_unknown_id_404(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/floorplan/does-not-exist-123", timeout=30)
        assert r.status_code == 404

    def test_list(self, api_client, analysis):
        r = api_client.get(f"{BASE_URL}/api/floorplans", timeout=60)
        assert r.status_code == 200
        items = r.json()["items"]
        assert isinstance(items, list)
        assert any(i["id"] == analysis["id"] for i in items)
        for i in items:
            assert "_id" not in i

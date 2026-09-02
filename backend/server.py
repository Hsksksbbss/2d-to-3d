from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import base64
import logging
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from floorplan_analyzer import analyze_image

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI(title="Floor Plan → 3D API")
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "floorplan-3d", "status": "ok"}


@api_router.post("/floorplan/analyze")
async def analyze_floorplan(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Please upload an image file.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    try:
        result = analyze_image(data)
    except Exception as e:
        logging.exception("Analysis failed")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}") from e

    plan_id = str(uuid.uuid4())
    doc = {
        "id": plan_id,
        "filename": file.filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image_b64": "data:{ct};base64,{b}".format(
            ct=file.content_type, b=base64.b64encode(data).decode()
        ),
        # store analysis without heavy debug images to keep the doc small
        "analysis": {k: v for k, v in result.items() if k != "debug"},
        "debug": result["debug"],
    }
    await db.floorplans.insert_one(doc)
    # response includes image + full analysis + debug
    return {
        "id": plan_id,
        "image": doc["image_b64"],
        "analysis": result,
    }


@api_router.get("/floorplan/{plan_id}")
async def get_floorplan(plan_id: str) -> dict[str, Any]:
    doc = await db.floorplans.find_one({"id": plan_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "id": plan_id,
        "image": doc["image_b64"],
        "analysis": {**doc["analysis"], "debug": doc.get("debug", {})},
    }


@api_router.get("/floorplans")
async def list_floorplans() -> dict[str, Any]:
    items = await db.floorplans.find(
        {}, {"_id": 0, "id": 1, "filename": 1, "created_at": 1}
    ).sort("created_at", -1).to_list(50)
    return {"items": items}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

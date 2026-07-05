"""
Bulk processing service for the Hospital Directory API.

Accepts CSV uploads containing hospital records, forwards them
to the remote Hospital Directory API one-by-one, then activates
the entire batch once all rows succeed.
"""

import csv
import io
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

HOSPITAL_API_BASE = "https://hospital-directory.onrender.com"
MAX_ROWS = 20
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Hospital Bulk Processor",
    description="CSV bulk upload service that integrates with the Hospital Directory API",
    version="1.0.0",
)

# in-memory store for batch progress / results
batch_store: dict = {}


def _validate_csv(content: str) -> list[dict]:
    """
    Parse and validate the CSV content.
    Returns a list of dicts with keys: name, address, phone (optional).
    Raises ValueError on any validation issue.
    """
    reader = csv.DictReader(io.StringIO(content))

    if reader.fieldnames is None:
        raise ValueError("CSV file appears to be empty")

    headers = [h.strip().lower() for h in reader.fieldnames]
    if "name" not in headers or "address" not in headers:
        raise ValueError("CSV must contain at least 'name' and 'address' columns")

    rows = []
    for i, row in enumerate(reader, start=1):
        normalized = {k.strip().lower(): v.strip() if v else "" for k, v in row.items()}
        name = normalized.get("name", "")
        address = normalized.get("address", "")
        phone = normalized.get("phone", "")

        if not name:
            raise ValueError(f"Row {i}: 'name' cannot be empty")
        if not address:
            raise ValueError(f"Row {i}: 'address' cannot be empty")

        rows.append({"name": name, "address": address, "phone": phone})

    if len(rows) == 0:
        raise ValueError("CSV contains no data rows")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"CSV exceeds maximum of {MAX_ROWS} hospitals")

    return rows


@app.post("/hospitals/bulk")
async def bulk_create_hospitals(file: UploadFile = File(...)):
    """
    Upload a CSV file (columns: name, address, phone) to create hospitals
    in bulk via the Hospital Directory API.
    """
    if file.content_type and file.content_type not in (
        "text/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
    ):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File encoding must be UTF-8")

    try:
        rows = _validate_csv(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    batch_id = str(uuid.uuid4())
    total = len(rows)

    batch_store[batch_id] = {
        "batch_id": batch_id,
        "total_hospitals": total,
        "processed_hospitals": 0,
        "failed_hospitals": 0,
        "status": "processing",
        "hospitals": [],
    }

    start = time.time()
    hospitals_result = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for idx, row in enumerate(rows, start=1):
            payload = {
                "name": row["name"],
                "address": row["address"],
                "creation_batch_id": batch_id,
            }
            # only include phone if it's not empty
            if row["phone"]:
                payload["phone"] = row["phone"]

            try:
                resp = await client.post(
                    f"{HOSPITAL_API_BASE}/hospitals/",
                    json=payload,
                )
                if resp.status_code >= 400:
                    error_detail = resp.text
                    hospitals_result.append({
                        "row": idx,
                        "hospital_id": None,
                        "name": row["name"],
                        "address": row["address"],
                        "phone": row["phone"],
                        "status": "failed",
                        "error": f"API returned {resp.status_code}: {error_detail}",
                    })
                    batch_store[batch_id]["failed_hospitals"] += 1
                else:
                    data = resp.json()
                    hospitals_result.append({
                        "row": idx,
                        "hospital_id": data.get("id"),
                        "name": row["name"],
                        "status": "created",
                    })
                    batch_store[batch_id]["processed_hospitals"] += 1

            except httpx.RequestError as exc:
                hospitals_result.append({
                    "row": idx,
                    "hospital_id": None,
                    "name": row["name"],
                    "address": row["address"],
                    "phone": row["phone"],
                    "status": "failed",
                    "error": f"Connection error: {type(exc).__name__} - {exc}",
                })
                batch_store[batch_id]["failed_hospitals"] += 1

    # activate the batch only when every hospital was created successfully
    batch_activated = False
    if batch_store[batch_id]["failed_hospitals"] == 0:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                activate_resp = await client.patch(
                    f"{HOSPITAL_API_BASE}/hospitals/batch/{batch_id}/activate"
                )
                if activate_resp.status_code < 400:
                    batch_activated = True
                    for h in hospitals_result:
                        h["status"] = "created_and_activated"
        except httpx.RequestError:
            batch_activated = False

    elapsed = round(time.time() - start, 2)

    result = {
        "batch_id": batch_id,
        "total_hospitals": total,
        "processed_hospitals": batch_store[batch_id]["processed_hospitals"],
        "failed_hospitals": batch_store[batch_id]["failed_hospitals"],
        "processing_time_seconds": elapsed,
        "batch_activated": batch_activated,
        "hospitals": hospitals_result,
    }

    batch_store[batch_id].update(result)
    batch_store[batch_id]["status"] = "completed"

    return JSONResponse(content=result)


@app.get("/hospitals/bulk/{batch_id}/status")
async def get_batch_status(batch_id: str):
    """Poll this endpoint to check progress of a batch that is being processed."""
    entry = batch_store.get(batch_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Batch not found")
    return entry


@app.post("/hospitals/bulk/{batch_id}/resume")
async def resume_batch(batch_id: str):
    """
    Re-attempt failed rows from a previous batch.
    Retries any row marked 'failed', and re-activates if everything passes.
    """
    entry = batch_store.get(batch_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Batch not found")

    failed_rows = [h for h in entry.get("hospitals", []) if h["status"] == "failed"]
    if not failed_rows:
        return {"message": "No failed rows to retry", "batch_id": batch_id}

    start = time.time()

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for item in failed_rows:
            payload = {
                "name": item["name"],
                "address": item["address"],
                "creation_batch_id": batch_id,
            }
            if item.get("phone"):
                payload["phone"] = item["phone"]

            try:
                resp = await client.post(f"{HOSPITAL_API_BASE}/hospitals/", json=payload)
                if resp.status_code < 400:
                    data = resp.json()
                    item["hospital_id"] = data.get("id")
                    item["status"] = "created"
                    item.pop("error", None)
                    entry["processed_hospitals"] += 1
                    entry["failed_hospitals"] -= 1
            except httpx.RequestError:
                pass

    # try activation again if no failures remain
    batch_activated = entry.get("batch_activated", False)
    if entry["failed_hospitals"] == 0 and not batch_activated:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                activate_resp = await client.patch(
                    f"{HOSPITAL_API_BASE}/hospitals/batch/{batch_id}/activate"
                )
                if activate_resp.status_code < 400:
                    entry["batch_activated"] = True
                    for h in entry["hospitals"]:
                        if h["status"] == "created":
                            h["status"] = "created_and_activated"
        except httpx.RequestError:
            pass

    elapsed = round(time.time() - start, 2)
    return {
        "batch_id": batch_id,
        "retried_rows": len(failed_rows),
        "still_failed": entry["failed_hospitals"],
        "processing_time_seconds": elapsed,
        "batch_activated": entry.get("batch_activated", False),
    }


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def home():
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text())

# Hospital Bulk Processing Service

A lightweight FastAPI service that accepts CSV uploads of hospital records and forwards them to the [Hospital Directory API](https://hospital-directory.onrender.com/docs) in batch.

## How it works

1. Client uploads a CSV with columns `name`, `address`, `phone` (phone optional).
2. The service validates the file (max 20 rows, required fields present).
3. A UUID batch ID is generated, and each row is sent to `POST /hospitals/` on the remote API with that batch ID.
4. Once every row is created, the batch is activated via `PATCH /hospitals/batch/{batch_id}/activate`.
5. A summary response is returned containing per-row status and timing info.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/hospitals/bulk` | Upload CSV, create hospitals, activate batch |
| GET | `/hospitals/bulk/{batch_id}/status` | Poll processing progress |
| POST | `/hospitals/bulk/{batch_id}/resume` | Retry failed rows from a batch |
| POST | `/hospitals/bulk/validate` | Validate CSV without creating anything |
| WS | `/ws/bulk/{batch_id}` | WebSocket for real-time progress |
| GET | `/health` | Health check |

## Running locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

The API docs will be at http://localhost:8000/docs

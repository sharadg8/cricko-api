import os
import logging
import time
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Configure logging for Render dashboard
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("simple-api")

app = FastAPI()

# Enable CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    logger.info(f"Path: {request.url.path} Duration: {duration:.2f}s")
    return response

@app.get("/")
def health_check():
    return {
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "unix_time": time.time(),
        "message": "Simple Test API is running"
    }

@app.get("/test-time")
def get_time():
    """Simple endpoint to verify the API is responsive."""
    return {
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "UTC",
        "service": "FastAPI on Render"
    }

if __name__ == "__main__":
    import uvicorn
    # Render assigns a dynamic port via environment variable
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
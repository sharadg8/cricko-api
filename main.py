import os
import re
import logging
import time
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from curl_cffi import requests

# Configure logging for Render dashboard
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cric-scraper")

app = FastAPI()

# Enable CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome"

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
        "message": "Cricinfo Scraper is live"
    }

@app.get("/test-time")
def get_time():
    return {
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "service": "FastAPI on Render"
    }

@app.post("/scrape-match")
async def scrape_match(payload: ScrapeRequest):
    logger.info(f"Scraping request: {payload.url}")
    
    if "espncricinfo.com" not in payload.url:
        raise HTTPException(status_code=400, detail="Invalid URL. Must be an ESPNCricinfo link.")

    try:
        # Using curl_cffi to bypass Imperva/Cloudflare
        resp = requests.get(
            payload.url, 
            impersonate=payload.impersonate,
            timeout=20
        )
        
        if resp.status_code != 200:
            logger.error(f"Cricinfo returned status {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail="Cricinfo blocked the request")

        html = resp.text
        
        # Metadata Extraction
        match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if not match:
            match = re.search(r'<title>(.*?)</title>', html)
            
        full_title = match.group(1) if match else "Unknown Match"
        clean_title = full_title.split('|')[0].strip()

        return {
            "success": True,
            "match_title": clean_title,
            "full_title": full_title
        }

    except Exception as e:
        logger.error(f"Scrape failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
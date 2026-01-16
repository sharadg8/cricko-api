import os
import re
import logging
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from curl_cffi import requests

# Setup logging for Render logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("cric-scraper")

app = FastAPI()

# Enable CORS so your React frontend can talk to this API
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
    logger.info(f"Method: {request.method} Path: {request.url.path} Duration: {duration:.2f}s")
    return response

@app.get("/")
def health_check():
    return {"status": "healthy", "service": "cricinfo-scraper"}

@app.post("/scrape-match")
async def scrape_match(payload: ScrapeRequest):
    logger.info(f"Scraping attempt: {payload.url}")
    
    if "espncricinfo.com" not in payload.url:
        raise HTTPException(status_code=400, detail="Invalid URL. Must be an ESPNCricinfo link.")

    try:
        # Using curl_cffi to bypass bot detection
        resp = requests.get(
            payload.url, 
            impersonate=payload.impersonate,
            timeout=15
        )
        
        if resp.status_code != 200:
            logger.error(f"Target returned {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail="Could not access Cricinfo")

        html = resp.text
        
        # Metadata Extraction logic
        # 1. Try OpenGraph
        match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if not match:
            # 2. Fallback to standard Title
            match = re.search(r'<title>(.*?)</title>', html)
            
        full_title = match.group(1) if match else "Unknown Match"
        # Clean title (Cricinfo usually adds " | Match Report | ESPNcricinfo")
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
    # Render sets the PORT environment variable automatically
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
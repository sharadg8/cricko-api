import os
import re
import json
import logging
import time
import traceback
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from curl_cffi import requests
from bs4 import BeautifulSoup

# Configure logging with more detail
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("cric-scraper")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE = {}
CACHE_TTL = 60 

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome120"

@app.get("/")
def health_check():
    return {"status": "online", "version": "Cricko v8.3"}

@app.post("/scrape-schedule")
async def scrape_schedule(payload: ScrapeRequest):
    target_url = payload.url.split('?')[0]
    start_time = time.time()
    
    logger.info(f"--- New Schedule Request: {target_url} ---")
    
    if target_url in CACHE:
        cached_item = CACHE[target_url]
        if start_time < cached_item['expiry']:
            logger.info("Progress: Serving from cache.")
            return cached_item['data']
        logger.info("Progress: Cache expired. Re-fetching.")

    try:
        logger.info(f"Progress: Fetching URL via {payload.impersonate}...")
        resp = requests.get(target_url, impersonate=payload.impersonate, timeout=30, verify=False)
        
        if resp.status_code != 200:
            logger.error(f"Failed: HTTP {resp.status_code} for {target_url}")
            raise HTTPException(status_code=resp.status_code, detail="Cricinfo unreachable")

        logger.info("Progress: HTML received. Parsing BeautifulSoup...")
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            logger.error("Failed: __NEXT_DATA__ script tag not found in HTML.")
            raise HTTPException(status_code=500, detail="Data tag missing")

        logger.info("Progress: Extracting JSON payload from script tag...")
        data = json.loads(script_tag.string)
        
        # Navigate the JSON structure
        app_props = data.get('props', {}).get('appPageProps') or data.get('props', {}).get('pageProps', {})
        data_content = app_props.get('data', {}).get('content', {})
        
        series_info = app_props.get('data', {}).get('series', {})
        series_slug = series_info.get('slug', 'series')
        series_prefix = "-".join(series_slug.split('-')[:3])
        logger.info(f"Progress: Identified series prefix: {series_prefix}")

        # Robust match list extraction
        matches_list = data_content.get('matches', []) 
        if not matches_list:
            logger.info("Note: 'matches' not in primary path, checking initialState...")
            matches_list = app_props.get('initialState', {}).get('content', {}).get('matches', [])
        if not matches_list:
            logger.info("Note: Checking schedule containers...")
            containers = data_content.get('schedule', {}).get('containers', [])
            if containers:
                matches_list = containers[0].get('matches', [])

        match_count = len(matches_list)
        logger.info(f"Progress: Found {match_count} matches. Beginning formatting...")

        formatted_schedule = {}
        for idx, match in enumerate(matches_list, 1):
            mid = f"{series_prefix}-{str(idx).zfill(3)}"
            teams = match.get('teams') or []
            
            t1 = teams[0] if len(teams) > 0 else {}
            t2 = teams[1] if len(teams) > 1 else {}
            home = t1 if t1.get('isHome') else (t2 if t2.get('isHome') else t1)
            away = t2 if home == t1 else t1
            
            status = (match.get('state') or '').lower()
            ground = match.get('ground') or {}
            
            entry = {
                "ci": f"{match.get('slug', '')}-{match.get('objectId', '')}",
                "date": match.get('startTime'),
                "info": match.get('title'),
                "num": idx,
                "state": status,
                "teams": {
                    "away": {"abbr": (away.get('team') or {}).get('abbreviation', ''), "name": (away.get('team') or {}).get('longName', 'TBC')},
                    "home": {"abbr": (home.get('team') or {}).get('abbreviation', ''), "name": (home.get('team') or {}).get('longName', 'TBC')}
                },
                "venue": {"cc": ground.get('country', {}).get('name', ''), "city": ground.get('town', {}).get('name', ''), "name": ground.get('name', 'TBA')}
            }

            if status == "post":
                parse_scoreinfo = lambda s: "20" if not s else (str(s).split()[0].split("/")[0])
                entry["result"] = {
                    "away": {"overs": parse_scoreinfo(away.get('scoreInfo')), "total": away.get('score', '0/0')},
                    "home": {"overs": parse_scoreinfo(home.get('scoreInfo')), "total": home.get('score', '0/0')},
                    "result": match.get('statusText', ''),
                    "win": next((t["team"]["abbreviation"] for t in match.get("teams", []) if str(t.get("team", {}).get("id")) == str(match.get('winnerTeamId'))), None)
                }
            
            formatted_schedule[mid] = entry
            
            if idx % 10 == 0:
                logger.debug(f"Progress: Formatted {idx}/{match_count} matches...")

        logger.info(f"Success: Scraped {len(formatted_schedule)} matches in {round(time.time() - start_time, 2)}s")
        
        CACHE[target_url] = {"expiry": time.time() + (CACHE_TTL * 5), "data": formatted_schedule}
        return formatted_schedule

    except Exception as e:
        logger.error(f"Error occurred during scraping: {str(e)}")
        logger.error(traceback.format_exc())
        return {}

@app.post("/scrape-match")
async def scrape_match(payload: ScrapeRequest):
    logger.info(f"--- New Match Request: {payload.url} ---")
    # Additional trace points can be added here once logic is implemented
    return {"message": "Endpoint active, logic pending update"}

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Cricko API Server...")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
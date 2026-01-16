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

# Configure logging for Render dashboard
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cric-scraper")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache: { "url": {"expiry": timestamp, "data": result} }
CACHE = {}
CACHE_TTL = 60  # Cache duration in seconds (1 minute)

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome120"

@app.get("/")
def health_check():
    return {
        "status": "online", 
        "version": "Cricko v6"
    }

@app.post("/scrape-match")
async def scrape_match(payload: ScrapeRequest):
    # Clean URL to ensure we hit the scorecard
    target_url = payload.url.split('?')[0]
    if "full-scorecard" not in target_url:
        target_url = target_url.rstrip("/") + "/full-scorecard"

    # Check Cache
    now = time.time()
    if target_url in CACHE:
        cached_item = CACHE[target_url]
        if now < cached_item['expiry']:
            logger.info(f"--- Cache Hit for: {target_url} ---")
            return cached_item['data']
        else:
            logger.info(f"--- Cache Expired for: {target_url} ---")
            del CACHE[target_url]

    logger.info(f"--- Starting Scrape Request [v5] for: {payload.url} ---")
    
    # Validation
    if "espncricinfo.com" not in payload.url:
        raise HTTPException(status_code=400, detail="Invalid URL. Must be an ESPNCricinfo link.")

    try:
        # 1. Fetch Page
        logger.info(f"Step 1: Fetching HTML from {target_url}...")
        resp = requests.get(
            target_url, 
            impersonate=payload.impersonate,
            timeout=30,
            verify=False 
        )
        
        if resp.status_code != 200:
            logger.error(f"Step 1 Failed: Cricinfo returned status {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail=f"Cricinfo returned {resp.status_code}")
        
        logger.info("Step 1 Success: HTML content retrieved.")

        # 2. Parse HTML
        logger.info("Step 2: Searching for __NEXT_DATA__ script tag...")
        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        
        if not script_tag:
            logger.error("Step 2 Failed: __NEXT_DATA__ tag not found.")
            if "pardon our interruption" in resp.text.lower():
                logger.error("Detection: Blocked by Imperva Bot Protection.")
                raise HTTPException(status_code=403, detail="Blocked by Cricinfo Bot Protection")
            raise HTTPException(status_code=500, detail="JSON Data Tag not found on page")
        
        logger.info("Step 2 Success: Script tag found.")

        # 3. Process JSON
        logger.info("Step 3: Loading JSON string...")
        data = json.loads(script_tag.string)
        logger.info("Step 3 Success: JSON parsed successfully.")
        
        # 4. Access Properties
        logger.info("Step 4: Navigating JSON tree (props -> pageProps/appPageProps)...")
        props = data.get('props', {})
        app_props = props.get('appPageProps') or props.get('pageProps')
        
        if app_props is None:
            logger.error(f"Step 4 Failed: 'props' found but both 'appPageProps' and 'pageProps' are None. Available keys in props: {list(props.keys())}")
            raise HTTPException(status_code=500, detail="Required page properties are missing from JSON.")
        
        logger.info(f"Step 4 Success: Found properties block (Keys: {list(app_props.keys())})")
        
        # 5. Access Data Wrapper
        logger.info("Step 5: Accessing 'data' wrapper...")
        data_wrapper = app_props.get('data')
        if data_wrapper is None:
            logger.error(f"Step 5 Failed: 'app_props' found but 'data' is None. Keys in app_props: {list(app_props.keys())}")
            raise HTTPException(status_code=500, detail="Data wrapper is NoneType.")
        
        logger.info("Step 5 Success: Data wrapper retrieved.")

        # 6. Content and Match extraction
        content = data_wrapper.get('content') or {}
        match_obj = data_wrapper.get('match') or {}
        
        if not match_obj:
            logger.warning("Step 6: Match object is empty.")
            raise HTTPException(status_code=404, detail="Match details missing.")
        
        logger.info(f"Step 6 Success: Match extracted: {match_obj.get('title')}")

        # --- Extraction Logic ---
        m_state = (match_obj.get('state') or 'pre').lower()
        venue_obj = match_obj.get('ground') or {}
        teams_list = match_obj.get('teams') or []
        
        home_team = next((t for t in teams_list if t.get('isHome')), teams_list[0] if teams_list else {})
        away_team = next((t for t in teams_list if not t.get('isHome')), teams_list[1] if len(teams_list) > 1 else {})

        # Build response payload
        response_data = {
            "success": True,
            "version": "Cricko v5",
            "state": m_state,
            "meta": {
                "date": match_obj.get('startTime'),
                "info": match_obj.get('title'),
                "teams": {
                    "home": {"abbr": home_team.get('team', {}).get('abbreviation'), "name": home_team.get('team', {}).get('longName')},
                    "away": {"abbr": away_team.get('team', {}).get('abbreviation'), "name": away_team.get('team', {}).get('longName')}
                },
                "venue": {
                    "cc": venue_obj.get('country', {}).get('name'),
                    "city": venue_obj.get('town', {}).get('name'),
                    "name": venue_obj.get('name')
                }
            },
            "pre": {
                "officials": {
                    "umpires": [u.get('player', {}).get('longName') for u in match_obj.get('umpires') or []]
                },
                "toss": {
                    "choice": "bat" if match_obj.get('tossWinnerChoice') == 1 else "bowl",
                    "win": next((t.get("team", {}).get("abbreviation") for t in (match_obj.get("teams") or []) if t.get("team", {}).get("id") == match_obj.get('tossWinnerTeamId')), "N/A")
                }
            },
            "post": {
                "result": {"result": match_obj.get('statusText')},
                "innings_1": format_innings(content.get('innings') or [], 0),
                "innings_2": format_innings(content.get('innings') or [], 1)
            }
        }
        
        # Save to Cache
        CACHE[target_url] = {
            "expiry": time.time() + CACHE_TTL,
            "data": response_data
        }
        
        logger.info("--- Final Response Constructed Successfully [v5] ---")
        return response_data

    except json.JSONDecodeError:
        logger.error("JSON Decode Error: Script tag content was not valid JSON.")
        raise HTTPException(status_code=500, detail="Failed to parse Cricinfo JSON payload")
    except Exception as e:
        logger.error(f"CRITICAL ERROR [v5]: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Scraper Error: {str(e)}")

def format_innings(innings_list, index):
    if not innings_list or len(innings_list) <= index: return None
    inn = innings_list[index] or {}
    
    batting = []
    for b in inn.get('inningBatsmen') or []:
        if b and b.get('player'):
            # Fix: Defensive check for dismissalText which can be None
            dismissal_obj = b.get('dismissalText') or {}
            
            batting.append({
                "id": b.get('player', {}).get('slug', 'unknown'),
                "r": b.get('runs', 0),
                "b": b.get('balls', 0),
                "r4": b.get('fours', 0),
                "r6": b.get('sixes', 0),
                "sr": b.get('strikerate', '0.00'),
                "sts": dismissal_obj.get('long', 'not out')
            })

    bowling = [
        {
            "id": bo.get('player', {}).get('slug', 'unknown'),
            "o": bo.get('overs', 0),
            "m": bo.get('maidens', 0),
            "r": bo.get('conceded', 0),
            "w": bo.get('wickets', 0),
            "econ": bo.get('economy', '0.00'),
            "r0": bo.get('dots', 0)
        } for bo in inn.get('inningBowlers') or [] if bo and bo.get('player')
    ]

    return {
        "team": (inn.get('team') or {}).get('abbreviation', 'UNK'),
        "total": f"{inn.get('runs', 0)}/{inn.get('wickets', 0)}",
        "overs": inn.get('overs', 0),
        "batting": batting,
        "bowling": bowling,
        "partnerships": [
            {
                "r": p.get('runs', 0),
                "b": p.get('balls', 0),
                "p1": (p.get('player1') or {}).get('slug', 'p1'),
                "p2": (p.get('player2') or {}).get('slug', 'p2')
            } for p in inn.get('inningPartnerships') or [] if p
        ],
        "fow": [
            {
                "id": (f.get('player') or {}).get('slug', 'p'),
                "over": f.get('fowOvers', 0),
                "score": f"{f.get('fowRuns', 0)}/{f.get('fowWicketNum', 0)}"
            } for f in inn.get('inningWickets') or [] if f
        ]
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
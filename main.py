import os
import re
import json
import logging
import time
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

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome120"

@app.get("/")
def health_check():
    return {"status": "online", "message": "Cricinfo Advanced Scraper is live"}

@app.post("/scrape-match")
async def scrape_match(payload: ScrapeRequest):
    logger.info(f"Target URL: {payload.url}")
    
    # Validation
    if "espncricinfo.com" not in payload.url:
        raise HTTPException(status_code=400, detail="Invalid URL. Must be an ESPNCricinfo link.")

    # Clean URL to ensure we hit the scorecard
    target_url = payload.url.split('?')[0] # Remove query params
    if "full-scorecard" not in target_url:
        target_url = target_url.rstrip("/") + "/full-scorecard"

    try:
        # 1. Fetch Page
        resp = requests.get(
            target_url, 
            impersonate=payload.impersonate,
            timeout=30,
            verify=False # Sometimes helps with local/proxy issues
        )
        
        if resp.status_code != 200:
            logger.error(f"Cricinfo Error: {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail=f"Cricinfo returned {resp.status_code}")

        # 2. Parse HTML
        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        
        if not script_tag:
            logger.error("Failed to find __NEXT_DATA__")
            # Fallback: check if we got a "Please enable JS" or "Blocked" page
            if "pardon our interruption" in resp.text.lower():
                raise HTTPException(status_code=403, detail="Blocked by Cricinfo Bot Protection (Imperva)")
            raise HTTPException(status_code=500, detail="JSON Data Tag not found on page")

        # 3. Process JSON
        data = json.loads(script_tag.string)
        
        # Access nested properties safely using .get()
        app_props = data.get('props', {}).get('appPageProps', {})
        if not app_props:
            app_props = data.get('props', {}).get('pageProps', {}) # Alternative path
            
        content = app_props.get('data', {}).get('content', {})
        match_obj = app_props.get('data', {}).get('match', {})
        
        if not match_obj:
            logger.warning("Match object missing in JSON")
            raise HTTPException(status_code=404, detail="Match details not found in data payload")

        # --- Extraction Logic ---
        m_state = (match_obj.get('state') or 'pre').lower()
        venue_obj = match_obj.get('ground', {})
        teams_list = match_obj.get('teams', [])
        
        # Safely identify teams
        home_team = next((t for t in teams_list if t.get('isHome')), teams_list[0] if teams_list else {})
        away_team = next((t for t in teams_list if not t.get('isHome')), teams_list[1] if len(teams_list) > 1 else {})

        # Build response payload
        return {
            "success": True,
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
                    "umpires": [u.get('player', {}).get('longName') for u in match_obj.get('umpires', [])]
                },
                "toss": {
                    "choice": "bat" if match_obj.get('tossWinnerChoice') == 1 else "bowl",
                    "win": next((t["team"]["abbreviation"] for t in match_obj.get("teams", []) if t["team"].get("id") == match_obj.get('tossWinnerTeamId')), "N/A")
                }
            },
            "post": {
                "result": {"result": match_obj.get('statusText')},
                "innings_1": format_innings(content.get('innings', []), 0),
                "innings_2": format_innings(content.get('innings', []), 1)
            }
        }

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse Cricinfo JSON payload")
    except Exception as e:
        logger.error(f"Detailed Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scraper Error: {str(e)}")

def format_innings(innings_list, index):
    if len(innings_list) <= index: return None
    inn = innings_list[index]
    
    # Batting
    batting = []
    for b in inn.get('inningBatsmen', []):
        if b.get('player'):
            batting.append({
                "id": b.get('player', {}).get('slug', 'unknown'),
                "r": b.get('runs', 0),
                "b": b.get('balls', 0),
                "r4": b.get('fours', 0),
                "r6": b.get('sixes', 0),
                "sr": b.get('strikerate', '0.00'),
                "sts": b.get('dismissalText', {}).get('long', 'not out')
            })

    # Bowling
    bowling = [
        {
            "id": bo.get('player', {}).get('slug', 'unknown'),
            "o": bo.get('overs', 0),
            "m": bo.get('maidens', 0),
            "r": bo.get('conceded', 0),
            "w": bo.get('wickets', 0),
            "econ": bo.get('economy', '0.00'),
            "r0": bo.get('dots', 0)
        } for bo in inn.get('inningBowlers', []) if bo.get('player')
    ]

    return {
        "team": inn.get('team', {}).get('abbreviation', 'UNK'),
        "total": f"{inn.get('runs', 0)}/{inn.get('wickets', 0)}",
        "overs": inn.get('overs', 0),
        "batting": batting,
        "bowling": bowling,
        "partnerships": [
            {
                "r": p.get('runs', 0),
                "b": p.get('balls', 0),
                "p1": p.get('player1', {}).get('slug', 'p1'),
                "p2": p.get('player2', {}).get('slug', 'p2')
            } for p in inn.get('inningPartnerships', [])
        ],
        "fow": [
            {
                "id": f.get('player', {}).get('slug', 'p'),
                "over": f.get('fowOvers', 0),
                "score": f"{f.get('fowRuns', 0)}/{f.get('fowWicketNum', 0)}"
            } for f in inn.get('inningWickets', [])
        ]
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
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
CACHE_TTL = 60  # Cache duration in seconds

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome120"

@app.get("/")
def health_check():
    return {
        "status": "online", 
        "version": "Cricko v8"
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

    logger.info(f"--- Starting Scrape Request [v5] for: {payload.url} ---")
    
    if "espncricinfo.com" not in payload.url:
        raise HTTPException(status_code=400, detail="Invalid URL. Must be an ESPNCricinfo link.")

    try:
        resp = requests.get(
            target_url, 
            impersonate=payload.impersonate,
            timeout=30,
            verify=False 
        )
        
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Cricinfo returned {resp.status_code}")

        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        
        if not script_tag:
            raise HTTPException(status_code=500, detail="JSON Data Tag not found on page")

        data = json.loads(script_tag.string)
        props = data.get('props', {})
        app_props = props.get('appPageProps') or props.get('pageProps')
        
        if not app_props:
            raise HTTPException(status_code=500, detail="Required page properties are missing from JSON.")
        
        data_wrapper = app_props.get('data', {})
        content = data_wrapper.get('content', {})
        match_obj = data_wrapper.get('match', {})
        
        if not match_obj:
            raise HTTPException(status_code=404, detail="Match details missing.")

        m_state = (match_obj.get('state') or 'pre').lower()
        venue_obj = match_obj.get('ground') or {}
        teams_list = match_obj.get('teams') or []
        
        home_team = next((t for t in teams_list if t.get('isHome')), teams_list[0] if teams_list else {})
        away_team = next((t for t in teams_list if not t.get('isHome')), teams_list[1] if len(teams_list) > 1 else {})

        # --- EXTRACT SQUADS (PRE-DATA) ---
        squads = {}
        teams_players = content.get('matchPlayers', {}).get('teamPlayers', [])
        for tp in teams_players:
            t_data = tp.get('team')
            t_abbr = t_data.get('abbreviation') if t_data else "UNK"
            squads[t_abbr] = {}
            for p in tp.get('players', []):
                p_obj = p.get('player', {})
                p_slug = p_obj.get('slug')
                if p_slug:
                    roles_raw = p_obj.get('playingRoles', [])
                    role_str = ", ".join(roles_raw) if isinstance(roles_raw, list) else roles_raw
                    squads[t_abbr][p_slug] = {
                        "name": p_obj.get('longName'),
                        "slug": p_slug,
                        "role": f"[{p.get('playerRoleType', {})}] {role_str}"
                    }

        # --- EXTRACT POM (POST-DATA) ---
        awards = content.get('matchPlayerAwards', [])
        pom_slug = next((a.get('player', {}).get('slug', "") for a in awards if a.get('type') == "PLAYER_OF_MATCH"), "")

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
                    "match_referee": [u.get('player', {}).get('longName') for u in match_obj.get('matchReferees') or []],
                    "tv_umpire": [u.get('player', {}).get('longName') for u in match_obj.get('tvUmpires') or []],
                    "umpires": [u.get('player', {}).get('longName') for u in match_obj.get('umpires') or []]
                },
                "squads": squads,
                "toss": {
                    "choice": "bat" if match_obj.get('tossWinnerChoice') == 1 else "bowl",
                    "win": next((t.get("team", {}).get("abbreviation") for t in teams_list if t.get("team", {}).get("id") == match_obj.get('tossWinnerTeamId')), "N/A")
                }
            },
            "post": {
                "result": {
                    "result": match_obj.get('statusText'),
                    "pom": pom_slug,
                    "win": next((t.get("team", {}).get("abbreviation") for t in teams_list if t.get("team", {}).get("id") == match_obj.get('winnerTeamId')), None)
                },
                "innings_1": format_innings(content.get('innings') or [], 0),
                "innings_2": format_innings(content.get('innings') or [], 1)
            }
        }
        
        CACHE[target_url] = {"expiry": time.time() + CACHE_TTL, "data": response_data}
        return response_data

    except Exception as e:
        logger.error(f"CRITICAL ERROR [v5]: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scraper Error: {str(e)}")

def format_innings(innings_list, index):
    if not innings_list or len(innings_list) <= index: return None
    inn = innings_list[index] or {}
    
    batting = []
    for b in inn.get('inningBatsmen') or []:
        if b and b.get('player') and (b.get('runs') is not None or b.get('balls') is not None or b.get('isOut') or b.get('isBatting')):
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

    return {
        "team": (inn.get('team') or {}).get('abbreviation', 'UNK'),
        "total": f"{inn.get('runs', 0)}/{inn.get('wickets', 0)}",
        "overs": inn.get('overs', 0),
        "batting": batting,
        "bowling": [
            {
                "id": bo.get('player', {}).get('slug', 'unknown'),
                "o": bo.get('overs', 0),
                "m": bo.get('maidens', 0),
                "r": bo.get('conceded', 0),
                "w": bo.get('wickets', 0),
                "r0": bo.get('dots', 0),
                "r4": bo.get('fours', 0),
                "r6": bo.get('sixes', 0),
                "wd": bo.get('wides', 0),
                "nb": bo.get('noballs', 0),
                "econ": bo.get('economy', '0.00')
            } for bo in inn.get('inningBowlers') or [] if bo and bo.get('player')
        ],
        "partnerships": [
            {
                "r": p.get('runs', 0), 
                "b": p.get('balls', 0), 
                "p1": (p.get('player1') or {}).get('slug', 'p1'), 
                "p2": (p.get('player2') or {}).get('slug', 'p2'),
                "p1r": p.get('player1Runs'),
                "p1b": p.get('player1Balls'),
                "p2r": p.get('player2Runs'),
                "p2b": p.get('player2Balls')
            }
            for p in inn.get('inningPartnerships') or [] if p
        ],
        "fow": [
            {"id": (f.get('player') or {}).get('slug', 'p'), "over": f.get('fowOvers', 0), "score": f"{f.get('fowRuns', 0)}/{f.get('fowWicketNum', 0)}"}
            for f in inn.get('inningWickets') or [] if f
        ],
        "extras": {
            "b": inn.get('byes', 0),
            "lb": inn.get('legbyes', 0),
            "wd": inn.get('wides', 0),
            "nb": inn.get('noballs', 0),
            "tot": inn.get('extras', 0)
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
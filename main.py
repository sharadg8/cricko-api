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

# Enable CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome120"

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
        "message": "Cricinfo Advanced Scraper is live"
    }

@app.post("/scrape-match")
async def scrape_match(payload: ScrapeRequest):
    logger.info(f"Scraping detailed scorecard: {payload.url}")
    
    if "espncricinfo.com" not in payload.url:
        raise HTTPException(status_code=400, detail="Invalid URL. Must be an ESPNCricinfo link.")

    # Ensure we are looking at the full-scorecard page for maximum data
    target_url = payload.url
    if "full-scorecard" not in target_url and not target_url.endswith("/"):
        target_url = target_url.rstrip("/") + "/full-scorecard"
    elif "full-scorecard" not in target_url:
        target_url = target_url + "/full-scorecard"

    try:
        # Using curl_cffi to bypass Imperva/Cloudflare
        resp = requests.get(
            target_url, 
            impersonate=payload.impersonate,
            timeout=30
        )
        
        if resp.status_code != 200:
            logger.error(f"Cricinfo returned status {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail=f"Cricinfo blocked the request (Status {resp.status_code})")

        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        
        if not script_tag:
            logger.error("Could not find __NEXT_DATA__ script tag")
            raise HTTPException(status_code=500, detail="Could not find match data on page")

        data = json.loads(script_tag.string)
        app_props = data.get('props', {}).get('appPageProps', {})
        content = app_props.get('data', {}).get('content', {})
        match_obj = app_props.get('data', {}).get('match', {})
        
        if not match_obj:
            raise HTTPException(status_code=404, detail="Match data not found in Cricinfo response")

        m_state = match_obj.get('state', '').lower() or 'pre'
        
        # --- META DATA EXTRACTION ---
        venue_obj = match_obj.get('ground', {})
        teams_list = match_obj.get('teams', [])
        home_team = next((t for t in teams_list if t.get('isHome')), teams_list[0] if teams_list else {})
        away_team = next((t for t in teams_list if not t.get('isHome')), teams_list[1] if len(teams_list) > 1 else {})

        meta_data = {
            "date": match_obj.get('startTime'),
            "info": match_obj.get('title'),
            "teams": {
                "home": {
                    "abbr": home_team.get('team', {}).get('abbreviation'),
                    "name": home_team.get('team', {}).get('longName')
                },
                "away": {
                    "abbr": away_team.get('team', {}).get('abbreviation'),
                    "name": away_team.get('team', {}).get('longName')
                }
            },
            "venue": {
                "cc": venue_obj.get('country', {}).get('name'),
                "city": venue_obj.get('town', {}).get('name'),
                "name": venue_obj.get('name')
            }
        }

        # --- PRE DATA ---
        pre_data = {
            "officials": {
                "match_referee": [u.get('player', {}).get('longName') for u in match_obj.get('matchReferees') or []],
                "tv_umpire": [u.get('player', {}).get('longName') for u in match_obj.get('tvUmpires') or []],
                "umpires": [u.get('player', {}).get('longName') for u in match_obj.get('umpires') or []]
            },
            "squads": {},
            "toss": {
                "choice": "bat" if match_obj.get('tossWinnerChoice') == 1 else "bowl",
                "win": next((t["team"]["abbreviation"] for t in match_obj.get("teams", []) if t["team"].get("id") == match_obj.get('tossWinnerTeamId')), None)
            }
        }
        
        teams_players = content.get('matchPlayers', {}).get('teamPlayers', [])
        for tp in teams_players:
            t_data = tp.get('team')
            t_abbr = t_data.get('abbreviation') if t_data else "UNK"
            pre_data["squads"][t_abbr] = {}
            for p in tp.get('players', []):
                p_slug = p.get('player', {}).get('slug')
                if p_slug:
                    roles_raw = p.get('player', {}).get('playingRoles', [])
                    role_str = ", ".join(roles_raw) if isinstance(roles_raw, list) else str(roles_raw)
                    pre_data["squads"][t_abbr][p_slug] = {
                        "name": p.get('player', {}).get('longName'),
                        "slug": p.get('player', {}).get('slug'),
                        "role": f"[{p.get('playerRoleType', {})}] {role_str}"
                    }

        # --- POST DATA ---
        post_data = {"result": {}}
        if m_state == "post" or m_state == "live":
            awards = content.get('matchPlayerAwards', [])
            pom_slug = next((a.get('player', {}).get('slug', "") for a in awards if a.get('type') == "PLAYER_OF_MATCH"), "")

            post_data["result"] = {
                "result": match_obj.get('statusText'),
                "pom": pom_slug,
                "win": next((t["team"]["abbreviation"] for t in match_obj.get("teams", []) if t["team"].get("id") == match_obj.get('winnerTeamId')), None)
            }

            for idx, inn in enumerate(content.get('innings', []), 1):
                inn_key = f"innings_{idx}"
                
                # Batting
                batting_list = []
                for b in inn.get('inningBatsmen', []):
                    if b.get('player') and (b.get('runs') is not None or b.get('balls') is not None or b.get('isOut') or b.get('isBatting')):
                        batting_list.append({
                            "id": b.get('player', {}).get('slug'),
                            "r": b.get('runs'),
                            "b": b.get('balls'),
                            "r4": b.get('fours'),
                            "r6": b.get('sixes'),
                            "sr": b.get('strikerate'),
                            "sts": b.get('dismissalText', {}).get('long', 'not out')
                        })

                # Fielding
                fielding_stats = {}
                for b in inn.get('inningBatsmen', []):
                    if b.get('isOut') and b.get('dismissalText'):
                        for f in b.get('dismissalFielders', []):
                            f_slug = f.get('player', {}).get('slug')
                            if not f_slug: continue
                            if f_slug not in fielding_stats:
                                fielding_stats[f_slug] = {"c": 0, "st": 0, "ro": 0}
                            
                            d_type = b.get('dismissalType')
                            if d_type == 1: fielding_stats[f_slug]["c"] += 1
                            elif d_type == 5: fielding_stats[f_slug]["st"] += 1
                            elif d_type == 4: fielding_stats[f_slug]["ro"] += 1

                post_data[inn_key] = {
                    "team": inn.get('team', {}).get('abbreviation'),
                    "total": f"{inn.get('runs')}/{inn.get('wickets')}",
                    "overs": inn.get('overs'),
                    "batting": batting_list,
                    "bowling": [
                        {
                            "id": bo.get('player', {}).get('slug'),
                            "o": bo.get('overs'),
                            "m": bo.get('maidens'),
                            "r": bo.get('conceded'),
                            "w": bo.get('wickets'),
                            "econ": bo.get('economy'),
                            "nb": bo.get('noballs'),
                            "wd": bo.get('wides'),
                            "r0": bo.get('dots'),
                            "r4": bo.get('fours'),
                            "r6": bo.get('sixes')
                        } for bo in inn.get('inningBowlers', []) if bo.get('player')
                    ],
                    "fielding": [{"id": s, **v} for s, v in fielding_stats.items()],
                    "partnerships": [
                        {
                            "r": p.get('runs'),
                            "b": p.get('balls'),
                            "p1": p.get('player1', {}).get('slug'),
                            "p2": p.get('player2', {}).get('slug'),
                            "p1r": p.get('player1Runs'),
                            "p1b": p.get('player1Balls'),
                            "p2r": p.get('player2Runs'),
                            "p2b": p.get('player2Balls')
                        } for p in inn.get('inningPartnerships', [])
                    ],
                    "fow": [
                        {
                            "id": f.get('player', {}).get('slug'),
                            "over": f.get('fowOvers'),
                            "score": f"{f.get('fowRuns')}/{f.get('fowWicketNum')}"
                        } for f in inn.get('inningWickets', [])
                    ]
                }

        return {
            "success": True,
            "state": m_state,
            "meta": meta_data,
            "pre": pre_data,
            "post": post_data
        }

    except Exception as e:
        logger.error(f"Scrape failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
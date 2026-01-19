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

# Standard logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
    series_prefix: str = "" # Optional input for custom ID prefix

def format_innings(innings_list, index):
    """Helper to format individual innings data for scorecards."""
    if not innings_list or len(innings_list) <= index: 
        return None
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

@app.get("/")
def health_check():
    return {"status": "online", "version": "Cricko v8.6"}

@app.post("/schedule")
async def scrape_schedule(payload: ScrapeRequest):
    target_url = payload.url.split('?')[0]
    
    if target_url in CACHE:
        cached_item = CACHE[target_url]
        if time.time() < cached_item['expiry']:
            return cached_item['data']

    try:
        resp = requests.get(target_url, impersonate=payload.impersonate, timeout=30, verify=False)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Cricinfo unreachable")

        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            raise HTTPException(status_code=500, detail="Data tag missing")

        data = json.loads(script_tag.string)
        app_props = data.get('props', {}).get('appPageProps') or data.get('props', {}).get('pageProps', {})
        data_content = app_props.get('data', {}).get('content', {})
        
        series_prefix = payload.series_prefix
        if not series_prefix:
            series_info = app_props.get('data', {}).get('series', {})
            series_slug = series_info.get('slug', '')
            if series_slug:
                slug_parts = [p for p in series_slug.split('-') if p not in ['men', 's', 'women']]
                series_prefix = "-".join(slug_parts[:3])
            else:
                series_prefix = ""

        matches_list = data_content.get('matches', []) or \
                       data_content.get('seriesMatches', {}).get('matches', []) or \
                       app_props.get('initialState', {}).get('content', {}).get('matches', [])
        
        if not matches_list:
            containers = data_content.get('schedule', {}).get('containers', [])
            if containers:
                matches_list = containers[0].get('matches', [])

        formatted_schedule = {}
        for idx, match in enumerate(matches_list, 1):
            mid = f"{series_prefix}-{str(idx).zfill(3)}" if series_prefix else str(idx).zfill(3)
            
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

        CACHE[target_url] = {"expiry": time.time() + (CACHE_TTL * 5), "data": formatted_schedule}
        return formatted_schedule
    except Exception:
        logger.error(traceback.format_exc())
        return {}

@app.post("/match")
async def scrape_match(payload: ScrapeRequest):
    # Ensure URL is pointing to scorecard
    target_url = payload.url.split('?')[0]
    if "full-scorecard" not in target_url:
        target_url = target_url.rstrip("/") + "/full-scorecard"

    if target_url in CACHE:
        cached_item = CACHE[target_url]
        if time.time() < cached_item['expiry']:
            return cached_item['data']

    try:
        resp = requests.get(target_url, impersonate=payload.impersonate, timeout=30, verify=False)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Cricinfo unreachable")

        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            raise HTTPException(status_code=500, detail="Data tag missing")

        data = json.loads(script_tag.string)
        app_props = data.get('props', {}).get('appPageProps') or data.get('props', {}).get('pageProps', {})
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

        # Squads
        squads = {}
        teams_players = content.get('matchPlayers', {}).get('teamPlayers', [])
        for tp in teams_players:
            t_abbr = tp.get('team', {}).get('abbreviation', 'UNK')
            squads[t_abbr] = {}
            for p in tp.get('players', []):
                p_obj = p.get('player', {})
                p_slug = p_obj.get('slug')
                if p_slug:
                    roles = p_obj.get('playingRoles', [])
                    role_str = ", ".join(roles) if isinstance(roles, list) else str(roles)
                    squads[t_abbr][p_slug] = {
                        "name": p_obj.get('longName'),
                        "slug": p_slug,
                        "role": f"[{p.get('playerRoleType', {})}] {role_str}"
                    }

        # POM
        awards = content.get('matchPlayerAwards', [])
        pom_slug = next((a.get('player', {}).get('slug', "") for a in awards if a.get('type') == "PLAYER_OF_MATCH"), "")

        # Live Data
        live_data = {}
        lp = match_obj.get('livePerformance', {})
        if lp:
            live_data = {
                "batting": [{"id": b.get('player', {}).get('slug'), "r": b.get('runs'), "b": b.get('balls'), "r4": b.get('fours'), "r6": b.get('sixes'), "sr": b.get('strikerate'), "is_striker": b.get('isStriker', False)} for b in lp.get('batsmen', []) if b.get('player')],
                "bowling": [{"id": bo.get('player', {}).get('slug'), "o": bo.get('overs'), "r": bo.get('conceded'), "w": bo.get('wickets'), "econ": bo.get('economy'), "r0": bo.get('dots')} for bo in lp.get('bowlers', []) if bo.get('player')]
            }

        response_data = {
            "version": "Cricko v8.6",
            "state": m_state,
            "live": live_data,
            "meta": {
                "date": match_obj.get('startTime'),
                "info": match_obj.get('title'),
                "teams": {
                    "home": {"abbr": home_team.get('team', {}).get('abbreviation'), "name": home_team.get('team', {}).get('longName')},
                    "away": {"abbr": away_team.get('team', {}).get('abbreviation'), "name": away_team.get('team', {}).get('longName')}
                },
                "venue": {"cc": venue_obj.get('country', {}).get('name'), "city": venue_obj.get('town', {}).get('name'), "name": venue_obj.get('name')}
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
    except Exception:
        logger.error(traceback.format_exc())
        return {}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
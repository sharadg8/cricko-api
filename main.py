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

# --- SECURITY: ALLOWED ORIGINS ---
ALLOWED_ORIGINS = [
    "http://localhost:5173",          # For local React development
    "http://127.0.0.1:5173",
    "https://cricko.web.app/" # Your actual frontend domain
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Temporarily open for testing, change back to ALLOWED_ORIGINS later
    allow_credentials=True,
    allow_methods=["POST", "GET"],    # Limit to required methods
    allow_headers=["*"],
)

CACHE = {}
CACHE_TTL = 60 

# Local lookup for abbreviations and colors (HTML hex)
TEAM_META = {
    # International Teams
    "India": {"abbr": "IND", "color": "#004ba0"},
    "Australia": {"abbr": "AUS", "color": "#ffcd00"},
    "England": {"abbr": "ENG", "color": "#00285e"},
    "South Africa": {"abbr": "SA", "color": "#007a33"},
    "Pakistan": {"abbr": "PAK", "color": "#006629"},
    "New Zealand": {"abbr": "NZ", "color": "#000000"},
    "West Indies": {"abbr": "WI", "color": "#7b0031"},
    "Sri Lanka": {"abbr": "SL", "color": "#002080"},
    "Afghanistan": {"abbr": "AFG", "color": "#0000ff"},
    "Bangladesh": {"abbr": "BAN", "color": "#006a4e"},
    "Ireland": {"abbr": "IRE", "color": "#16a04a"},
    "Scotland": {"abbr": "SCO", "color": "#004b8d"},
    "Netherlands": {"abbr": "NED", "color": "#f36c21"},
    "Namibia": {"abbr": "NAM", "color": "#0035ad"},
    "United States of America": {"abbr": "USA", "color": "#002868"},
    "Canada": {"abbr": "CAN", "color": "#ff0000"},
    "Nepal": {"abbr": "NEP", "color": "#dc143c"},
    "Oman": {"abbr": "OMA", "color": "#ff0000"},
    "Papua New Guinea": {"abbr": "PNG", "color": "#000000"},
    "Uganda": {"abbr": "UGA", "color": "#fcdc04"},
    "United Arab Emirates": {"abbr": "UAE", "color": "#00732f"},
    "Zimbabwe": {"abbr": "ZIM", "color": "#ef3340"},
    "P.N.G.": {"abbr": "PNG", "color": "#000000"},
    "U.S.A.": {"abbr": "USA", "color": "#002868"},
    "U.A.E.": {"abbr": "UAE", "color": "#00732f"},
    
    # IPL Teams
    "Mumbai Indians": {"abbr": "MI", "color": "#004ba0"},
    "Chennai Super Kings": {"abbr": "CSK", "color": "#ffff00"},
    "Royal Challengers Bengaluru": {"abbr": "RCB", "color": "#2b2a29"},
    "Royal Challengers Bangalore": {"abbr": "RCB", "color": "#2b2a29"},
    "Kolkata Knight Riders": {"abbr": "KKR", "color": "#3a225d"},
    "Delhi Capitals": {"abbr": "DC", "color": "#00008b"},
    "Rajasthan Royals": {"abbr": "RR", "color": "#ea1a85"},
    "Punjab Kings": {"abbr": "PBKS", "color": "#dd1f2d"},
    "Kings XI": {"abbr": "KXIP", "color": "#dd1f2d"},
    "Sunrisers Hyderabad": {"abbr": "SRH", "color": "#ff822a"},
    "Gujarat Titans": {"abbr": "GT", "color": "#1b2133"},
    "Guj Lions": {"abbr": "GL", "color": "#1b2133"},
    "Lucknow Super Giants": {"abbr": "LSG", "color": "#0057e2"},
    "Daredevils": {"abbr": "DD", "color": "#00008b"},
    "Delhi": {"abbr": "DD", "color": "#00008b"},
    "Supergiant": {"abbr": "RPS", "color": "#ff00ff"},
    "Supergiants": {"abbr": "RPS", "color": "#ff00ff"}
}

class ScrapeRequest(BaseModel):
    url: str
    impersonate: str = "chrome120"
    series_prefix: str = ""

def format_innings(innings_list, index):
    """Helper to format individual innings data for scorecards, including fielding stats."""
    if not innings_list or len(innings_list) <= index: 
        return None
    inn = innings_list[index] or {}
    
    batting = []
    fielding_stats = {} # Map player_slug -> {c, st, ro}

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

            # Fielding Logic
            if b.get('isOut'):
                d_type = b.get('dismissalType')
                # dismissalFielders contains the players involved in the dismissal
                for f in b.get('dismissalFielders', []):
                    if f.get('player'):
                        f_slug = f.get('player', {}).get('slug')
                        if not f_slug: continue
                        if f_slug not in fielding_stats:
                            fielding_stats[f_slug] = {"c": 0, "st": 0, "ro": 0}
                        
                        if d_type == 1: # Caught
                            fielding_stats[f_slug]["c"] += 1
                        elif d_type == 5: # Stumped
                            fielding_stats[f_slug]["st"] += 1
                        elif d_type == 4: # Run out
                            fielding_stats[f_slug]["ro"] += 1

    fielding_list = [
        {"id": slug, "c": s["c"], "st": s["st"], "ro": s["ro"]}
        for slug, s in fielding_stats.items()
    ]

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
        "fielding": fielding_list,
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

async def fetch_json(url, impersonate="chrome120"):
    """Generic fetch for __NEXT_DATA__ JSON from Cricinfo."""
    try:
        resp = requests.get(url, impersonate=impersonate, timeout=30, verify=False)
        if resp.status_code != 200: 
            logger.warning(f"Non-200 status code: {resp.status_code} for URL: {url}")
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            logger.warning(f"__NEXT_DATA__ script tag not found in {url}")
        return json.loads(script_tag.string) if script_tag else None
    except Exception as e:
        logger.error(f"Error fetching JSON from {url}: {str(e)}")
        return None

@app.get("/")
def health_check():
    return {"status": "online", "version": "Cricko v0.8"}

@app.post("/schedule")
async def scrape_schedule(payload: ScrapeRequest):
    target_url = payload.url.split('?')[0]
    if target_url in CACHE:
        if time.time() < CACHE[target_url]['expiry']: return CACHE[target_url]['data']

    raw_json = await fetch_json(target_url, payload.impersonate)
    if not raw_json: raise HTTPException(status_code=500, detail="Failed to fetch data")

    try:
        app_props = raw_json.get('props', {}).get('appPageProps') or raw_json.get('props', {}).get('pageProps', {})
        data_content = app_props.get('data', {}).get('content', {})
        
        series_prefix = payload.series_prefix
        if not series_prefix:
            series_info = app_props.get('data', {}).get('series', {})
            series_slug = series_info.get('slug', '')
            if series_slug:
                slug_parts = [p for p in series_slug.split('-') if p not in ['men', 's', 'women']]
                series_prefix = "-".join(slug_parts[:3])

        matches_list = data_content.get('matches', []) or \
                       data_content.get('seriesMatches', {}).get('matches', []) or \
                       app_props.get('initialState', {}).get('content', {}).get('matches', [])
        
        if not matches_list:
            containers = data_content.get('schedule', {}).get('containers', [])
            if containers: matches_list = containers[0].get('matches', [])

        formatted_schedule = {"version": "Cricko v0.8", "data": {}}
        
        for idx, match in enumerate(matches_list, 1):
            mid = f"{series_prefix}-{str(idx).zfill(3)}" if series_prefix else str(idx).zfill(3)
            teams = match.get('teams') or []
            t1, t2 = (teams[0] if len(teams) > 0 else {}), (teams[1] if len(teams) > 1 else {})
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
                parse_overs = lambda s: "20" if not s else (str(s).split()[0].split("/")[0])
                entry["result"] = {
                    "away": {"overs": parse_overs(away.get('scoreInfo')), "total": away.get('score', '0/0')},
                    "home": {"overs": parse_overs(home.get('scoreInfo')), "total": home.get('score', '0/0')},
                    "result": match.get('statusText', ''),
                    "win": next((t["team"]["abbreviation"] for t in match.get("teams", []) if str(t.get("team", {}).get("id")) == str(match.get('winnerTeamId'))), None)
                }
            formatted_schedule["data"][mid] = entry

        CACHE[target_url] = {"expiry": time.time() + (CACHE_TTL * 5), "data": formatted_schedule}
        return formatted_schedule
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"version": "Cricko v0.8", "data": {}, "error": str(e)}

@app.post("/match")
async def scrape_match(payload: ScrapeRequest):
    target_url = payload.url.split('?')[0]
    if "live-cricket-score" not in target_url: target_url = target_url.rstrip("/") + "/live-cricket-score"
    if target_url in CACHE:
        if time.time() < CACHE[target_url]['expiry']: return CACHE[target_url]['data']

    raw_json = await fetch_json(target_url, payload.impersonate)
    if not raw_json: raise HTTPException(status_code=500, detail="Failed to fetch scorecard")

    try:
        app_props = raw_json.get('props', {}).get('appPageProps') or raw_json.get('props', {}).get('pageProps', {})
        data_wrapper = app_props.get('data', {}).get('data', {})
        content, match_obj = data_wrapper.get('content', {}), data_wrapper.get('match', {})
        live_obj, innings_list = content.get('livePerformance', {}), content.get('innings', [])
        
        m_state = (match_obj.get('state') or 'pre').lower()
        venue_obj = match_obj.get('ground') or {}
        teams_list = match_obj.get('teams') or []
        home_team = next((t for t in teams_list if t.get('isHome')), teams_list[0] if teams_list else {})
        away_team = next((t for t in teams_list if not t.get('isHome')), teams_list[1] if len(teams_list) > 1 else {})
        
        squads = {}
        for tp in content.get('matchPlayers', {}).get('teamPlayers', []):
            t_abbr = tp.get('team', {}).get('abbreviation', 'UNK')
            squads[t_abbr] = {p.get('player', {}).get('slug'): {"name": p.get('player', {}).get('longName'), "slug": p.get('player', {}).get('slug'), "role": f"[{p.get('playerRoleType', {})}] {', '.join(p.get('player', {}).get('playingRoles', []))}"} for p in tp.get('players', []) if p.get('player', {}).get('slug')}

        result_data = {
            "state": m_state,
            "meta": {"date": match_obj.get('startTime'), "info": match_obj.get('title'), "teams": {"home": {"abbr": home_team.get('team', {}).get('abbreviation'), "name": home_team.get('team', {}).get('longName')}, "away": {"abbr": away_team.get('team', {}).get('abbreviation'), "name": away_team.get('team', {}).get('longName')}}, "venue": {"cc": venue_obj.get('country', {}).get('name'), "city": venue_obj.get('town', {}).get('name'), "name": venue_obj.get('name')}},
            "pre": {"officials": {"match_referee": [u.get('player', {}).get('longName') for u in match_obj.get('matchReferees') or []], "tv_umpire": [u.get('player', {}).get('longName') for u in match_obj.get('tvUmpires') or []], "umpires": [u.get('player', {}).get('longName') for u in match_obj.get('umpires') or []]}, "squads": squads, "toss": {"choice": "bat" if match_obj.get('tossWinnerChoice') == 1 else "bowl", "win": next((t.get("team", {}).get("abbreviation") for t in teams_list if t.get("team", {}).get("id") == match_obj.get('tossWinnerTeamId')), "N/A")}},
            "post": {"result": {"result": match_obj.get('statusText'), "pom": next((a.get('player', {}).get('slug', "") for a in content.get('matchPlayerAwards', []) if a.get('type') == "PLAYER_OF_MATCH"), ""), "win": next((t.get("team", {}).get("abbreviation") for t in teams_list if t.get("team", {}).get("id") == match_obj.get('winnerTeamId')), None)}, "innings_1": format_innings(content.get('innings') or [], 0), "innings_2": format_innings(content.get('innings') or [], 1)}
        }

        live_inn = next((inn for inn in innings_list if inn.get('isCurrent')), {})
        if live_obj and live_inn:            
            # Bowler lookup to enrich livePerformance with r4, r6, nb, wd
            bowl_map = {b.get('player', {}).get('slug'): b for inn in innings_list for b in (inn.get('inningBowlers') or []) if b.get('player')}
            # Partnership logic: Loop for isLive: true in current innings partnerships
            inn_pships = live_inn.get('inningPartnerships', [])
            pship = next((p for p in inn_pships if p.get('isLive') is True), None)
 
            # Fallback to content or livePerformance if still None
            if not pship:
                pship = live_obj.get('partnership') or content.get('partnership', {})
            result_data["live"] = {
                "team": live_inn.get('team', {}).get('abbreviation'),
                "score": f"{live_inn.get('runs', 0)}/{live_inn.get('wickets', 0)}",
                "overs": live_inn.get('overs', 0),
                "crr": match_obj.get('statusData', {}).get('statusTextLangData', {}).get('crr') or content.get('supportInfo', {}).get('liveInfo', {}).get('currentRunRate'),
                "rrr": match_obj.get('statusData', {}).get('statusTextLangData', {}).get('rrr') or content.get('supportInfo', {}).get('liveInfo', {}).get('requiredRunrate'),
                "target": live_inn.get('target'),
                "pship": {
                    "r": pship.get('runs', 0), 
                    "b": pship.get('balls', 0), 
                    "p1": f"{pship.get('batsman1', {}).get('longName', '')} {pship.get('batsman1Runs', 0)}({pship.get('batsman1Balls', 0)})",
                    "p2": f"{pship.get('batsman2', {}).get('longName', '')} {pship.get('batsman2Runs', 0)}({pship.get('batsman2Balls', 0)})"
                } if pship else None,
                "recent": [{"o": b.get('oversUnique'), "v": b.get('totalRuns')} for b in (content.get('recentBallCommentary', {}).get('ballComments') or [])[:18]],
                "batting": [{"id": b.get('player', {}).get('slug'), "name": b.get('player', {}).get('longName'), "r": b.get('runs'), "b": b.get('balls'), "r4": b.get('fours'), "r6": b.get('sixes'), "sr": b.get('strikerate'), "is_striker": b.get('isStriker', False)} for b in live_obj.get('batsmen', []) if b.get('player')] if live_obj else [],
                "bowling": [{"id": bo.get('player', {}).get('slug'), "name": bo.get('player', {}).get('longName'), "o": bo.get('overs'), "r": bo.get('conceded'), "m": bo.get('maidens'), "w": bo.get('wickets'), "econ": bo.get('economy'), "r4": bowl_map.get(bo.get('player', {}).get('slug'), {}).get('fours', 0), "r6": bowl_map.get(bo.get('player', {}).get('slug'), {}).get('sixes', 0), "nb": bowl_map.get(bo.get('player', {}).get('slug'), {}).get('noballs', 0), "wd": bowl_map.get(bo.get('player', {}).get('slug'), {}).get('wides', 0), "r0": bo.get('dots')} for bo in live_obj.get('bowlers', []) if bo.get('player')] if live_obj else []
            }
        
        response = {"version": "Cricko v0.8", "data": result_data}
        CACHE[target_url] = {"expiry": time.time() + CACHE_TTL, "data": response}
        return response
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"version": "Cricko v0.8", "data": {}, "error": str(e)}

@app.post("/teams")
async def scrape_teams(payload: ScrapeRequest):
    """Parses series squad list and deep-scrapes each team for full squad details."""
    target_url = payload.url.split('?')[0]
    
    if target_url in CACHE and time.time() < CACHE[target_url]['expiry']:
        return CACHE[target_url]['data']

    raw_json = await fetch_json(target_url, payload.impersonate)
    if not raw_json: 
        logger.error("TRACING: Failed to fetch initial squads list JSON")
        raise HTTPException(status_code=500, detail="Failed to fetch squads list")

    try:
        app_props = raw_json.get('props', {}).get('appPageProps') or raw_json.get('props', {}).get('pageProps', {})
        data_content = app_props.get('data', {}).get('content', {})
        squads_list = data_content.get('squads') or app_props.get('initialState', {}).get('content', {}).get('squads', [])
        
        try:
            series_id = target_url.split('/series/')[1].split('/')[0]
        except Exception as e:
            logger.error(f"TRACING: Failed to extract series_id from {target_url}: {str(e)}")
            return {"version": "Cricko v0.8", "teams": [], "error": "Invalid series URL structure"}

        formatted_teams = []

        for item in squads_list:
            team_info = item.get('squad', {})
            t_slug = team_info.get('slug', '')
            t_id = team_info.get('objectId', '')
            t_name_placeholder = team_info.get('name') or item.get('title') or "Unknown Team"
            
            if not t_slug or not t_id:
                logger.warning(f"TRACING: Skipping squad {t_name_placeholder} due to missing slug/ID")
                continue

            team_url = f"https://www.espncricinfo.com/series/{series_id}/{t_slug}-{t_id}/series-squads"
            team_json = await fetch_json(team_url, payload.impersonate)
            
            if team_json:
                t_props = team_json.get('props', {}).get('appPageProps', team_json.get('props', {}).get('pageProps', {}))
                t_content = t_props.get('data', {}).get('content', {})
                squad_details = t_content.get('squadDetails', {})
                
                official_name = squad_details.get('team', {}).get('name') or squad_details.get('squad', {}).get('teamName') or squad_details.get('team', {}).get('longName') or t_name_placeholder
                members = squad_details.get('players') or t_content.get('squadMembers', [])
                
                players = []
                captain_slug = ""
                for m in members:
                    p_info = m.get('player') if 'player' in m else m
                    slug = p_info.get('slug')
                    if not slug: continue
                    if "C" in str(m.get('playerRoleType', '')) or m.get('isCaptain'): captain_slug = slug
                    roles_raw = p_info.get('playingRoles') or p_info.get('playingRole', [])
                    role_str = ", ".join([r.get('name') if isinstance(r, dict) else str(r) for r in (roles_raw if isinstance(roles_raw, list) else [roles_raw])])
                    players.append({"name": p_info.get('longName') or p_info.get('name'), "slug": slug, "role": role_str})

                meta = TEAM_META.get(official_name)
                if not meta:
                    for name, data in TEAM_META.items():
                        if name.lower() in official_name.lower() or official_name.lower() in name.lower():
                            meta = data
                            break
                
                if not meta:
                    logger.warning(f"TRACING: No meta found for {official_name}. Using defaults.")
                    meta = {"abbr": official_name[:3].upper(), "color": "#888888"}

                formatted_teams.append({
                    "ci": f"{t_slug}-{t_id}",
                    "name": official_name,
                    "abbr": meta["abbr"],
                    "color": meta["color"],
                    "cpt": captain_slug,
                    "squad": players
                })
            else:
                logger.error(f"TRACING: Failed to fetch deep-scrape JSON for {team_url}")

            time.sleep(0.5)

        response = {"version": "Cricko v0.8", "data": formatted_teams}
        CACHE[target_url] = {"expiry": time.time() + (CACHE_TTL * 60), "data": response}
        return response
    except Exception as e:
        logger.error(f"TRACING CRITICAL ERROR: {traceback.format_exc()}")
        return {"version": "Cricko v0.8", "data": [], "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
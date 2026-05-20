import os
import json
import anthropic
import requests
import time
import random
import string
import sqlite3
import hashlib
import secrets
from flask import Flask, render_template, request, jsonify, session
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")

DB_FILE = "out_app.db"
SQUADS_FILE = "squads.json"

# ── SEARCH CACHE (30 min in-memory) ──
_search_cache = {}
CACHE_TTL = 60 * 30  # 30 minutes in seconds

def make_cache_key(city, interests, budget, time_filter):
    interests_str = ",".join(sorted(interests))
    return f"{city.lower()}|{interests_str}|{budget}|{time_filter}".strip()

def get_cached(key):
    if key in _search_cache:
        result, timestamp = _search_cache[key]
        if time.time() - timestamp < CACHE_TTL:
            print(f"✓ Cache hit: {key}")
            return result
        else:
            del _search_cache[key]
    return None

def set_cache(key, result):
    _search_cache[key] = (result, time.time())
    print(f"✓ Cached: {key} ({len(result)} events)")

# ── DATABASE SETUP ──

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        city TEXT DEFAULT 'London',
        bio TEXT DEFAULT '',
        avatar_color TEXT DEFAULT '#FF3CAC',
        interests TEXT DEFAULT '[]',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saved_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        event_data TEXT NOT NULL,
        saved_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS going (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        event_title TEXT NOT NULL,
        event_date TEXT,
        event_location TEXT,
        event_url TEXT,
        status TEXT DEFAULT 'going',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, event_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        event_title TEXT,
        event_id TEXT,
        event_data TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_id INTEGER NOT NULL,
        following_id INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(follower_id, following_id)
    )""")

    conn.commit()
    conn.close()

init_db()

# ── AUTH HELPERS ──

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(user) if user else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Login required", "login_required": True}), 401
        return f(*args, **kwargs)
    return decorated

def log_activity(user_id, action, event_title=None, event_id=None, event_data=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO activity (user_id, action, event_title, event_id, event_data) VALUES (?,?,?,?,?)",
        (user_id, action, event_title, event_id, json.dumps(event_data) if event_data else None)
    )
    conn.commit()
    conn.close()

# ── SQUAD HELPERS ──

def load_squads():
    if not os.path.exists(SQUADS_FILE):
        return {}
    try:
        with open(SQUADS_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_squads(squads):
    with open(SQUADS_FILE, "w") as f:
        json.dump(squads, f, indent=2)

def generate_squad_code():
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=3)) + '-' + ''.join(random.choices(chars, k=3))

# ── WEATHER ──

def get_weather(city):
    if not OPENWEATHER_API_KEY:
        return {"description": "mild weather", "temp": 15, "is_sunny": False, "icon": "🌤️"}
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={OPENWEATHER_API_KEY}&units=metric"
        r = requests.get(url, timeout=5)
        data = r.json()
        condition = data["weather"][0]["main"]
        icon_map = {"Clear":"☀️","Clouds":"⛅","Rain":"🌧️","Drizzle":"🌦️","Snow":"❄️","Thunderstorm":"⛈️","Mist":"🌫️"}
        return {
            "description": data["weather"][0]["description"],
            "temp": round(data["main"]["temp"]),
            "is_sunny": condition in ["Clear","Clouds"],
            "condition": condition,
            "icon": icon_map.get(condition, "🌤️")
        }
    except:
        return {"description": "mild weather", "temp": 15, "is_sunny": False, "icon": "🌤️", "condition": "Unknown"}

# ── EVENT SEARCH ──

# ── MUSEUM SCRAPERS ──

def fetch_va_exhibitions():
    """Scrape V&A What's On page for current exhibitions."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; out-app/1.0)"}
        r = requests.get("https://www.vam.ac.uk/whatson", headers=headers, timeout=8)
        if r.status_code != 200:
            return []

        from html.parser import HTMLParser
        import re

        # Use regex to extract exhibition data from the page
        text = r.text

        # Find exhibition titles and details using pattern matching
        events = []

        # Look for structured data in JSON-LD or meta tags
        json_ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL)
        for match in json_ld_matches:
            try:
                data = json.loads(match.strip())
                if isinstance(data, list):
                    for item in data:
                        if item.get("@type") in ["Event", "ExhibitionEvent"]:
                            events.append(_parse_schema_event(item, "V&A Museum", "Cromwell Rd, London SW7"))
                elif data.get("@type") in ["Event", "ExhibitionEvent"]:
                    events.append(_parse_schema_event(data, "V&A Museum", "Cromwell Rd, London SW7"))
            except:
                continue

        # Fallback: use Claude to parse the page if no structured data found
        if not events:
            events = _parse_museum_page_with_claude(
                text[:8000],
                "V&A Museum",
                "Cromwell Rd, South Kensington, London SW7 2RL",
                "https://www.vam.ac.uk/whatson"
            )

        return events[:6]
    except Exception as e:
        print(f"V&A scrape error: {e}")
        return []


def fetch_tate_exhibitions():
    """Scrape Tate What's On for current exhibitions."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; out-app/1.0)"}
        # Try Tate Modern first
        events = []
        urls = [
            ("https://www.tate.org.uk/visit/tate-modern/whats-on", "Tate Modern", "Bankside, London SE1 9TG"),
            ("https://www.tate.org.uk/visit/tate-britain/whats-on", "Tate Britain", "Millbank, London SW1P 4RG"),
        ]
        for url, venue, location in urls:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            import re
            text = r.text
            # Try JSON-LD first
            json_ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL)
            for match in json_ld_matches:
                try:
                    data = json.loads(match.strip())
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if item.get("@type") in ["Event", "ExhibitionEvent", "VisualArtsEvent"]:
                            events.append(_parse_schema_event(item, venue, location))
                except:
                    continue
            # Fallback to Claude parsing
            if not any(e.get("source") == venue for e in events):
                parsed = _parse_museum_page_with_claude(text[:8000], venue, location, url)
                events.extend(parsed[:3])

        return events[:6]
    except Exception as e:
        print(f"Tate scrape error: {e}")
        return []


def _parse_schema_event(item, default_venue, default_location):
    """Convert a JSON-LD Event object to our event format."""
    name = item.get("name", "Exhibition")
    start = item.get("startDate", "")
    end = item.get("endDate", "")
    location = item.get("location", {})
    loc_name = location.get("name", default_venue) if isinstance(location, dict) else default_venue
    loc_addr = location.get("address", {})
    loc_str = loc_addr.get("streetAddress", default_location) if isinstance(loc_addr, dict) else default_location
    price_spec = item.get("offers", {})
    price = "Free"
    price_value = 0
    if isinstance(price_spec, dict):
        p = price_spec.get("price", 0)
        if p and float(p) > 0:
            price = f"£{p}"
            price_value = float(p)
    date_str = start[:10] if start else "Ongoing"
    return {
        "title": name,
        "organiser": default_venue,
        "date": date_str,
        "time": "10:00",
        "location": f"{loc_name}, {loc_str}",
        "price": price,
        "price_value": price_value,
        "category": "culture",
        "is_outdoor": False,
        "description": item.get("description", f"Exhibition at {default_venue}")[:200],
        "url": item.get("url", ""),
        "source": default_venue,
        "weather_match": True
    }


def _parse_museum_page_with_claude(html_text, venue, location, url):
    """Use Claude to extract exhibition listings from raw HTML."""
    try:
        # Strip HTML tags roughly
        import re
        text = re.sub(r'<[^>]+>', ' ', html_text)
        text = re.sub(r'\s+', ' ', text).strip()[:4000]

        prompt = f"""Extract exhibition listings from this {venue} webpage text.

Text: {text}

Return ONLY a JSON array of exhibitions currently showing or upcoming:
[{{"title":"Exhibition name","date":"date range e.g. Until 15 Jun 2025","time":"10:00","location":"{location}","price":"Free or £X","price_value":0,"category":"culture","is_outdoor":false,"description":"one sentence","url":"{url}","source":"{venue}","weather_match":true}}]

Output only the JSON array. If no exhibitions found, return []."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        text_out = response.content[0].text.strip()
        if text_out.startswith("```"):
            text_out = text_out.split("```")[1]
            if text_out.startswith("json"):
                text_out = text_out[4:]
        start = text_out.find("[")
        end = text_out.rfind("]") + 1
        if start != -1 and end > start:
            return json.loads(text_out[start:end])
    except Exception as e:
        print(f"Museum Claude parse error: {e}")
    return []


# ── MUSEUM CACHE ──
_museum_cache = {}
MUSEUM_CACHE_TTL = 60 * 60 * 3  # 3 hours

def get_museum_events():
    """Get V&A + Tate events, cached for 3 hours."""
    now = time.time()
    if "museums" in _museum_cache:
        events, ts = _museum_cache["museums"]
        if now - ts < MUSEUM_CACHE_TTL:
            print("✓ Museum cache hit")
            return events

    print("Fetching museum events...")
    va_events = fetch_va_exhibitions()
    tate_events = fetch_tate_exhibitions()
    all_events = va_events + tate_events
    _museum_cache["museums"] = (all_events, now)
    print(f"✓ Fetched {len(all_events)} museum events")
    return all_events


def search_events(city, interests, budget, weather, time_filter):
    # Check cache first
    cache_key = make_cache_key(city, interests, budget, time_filter)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    today = datetime.now().strftime("%d %B %Y")
    interests_str = ", ".join(interests) if interests else "art, music, culture, food, tech"
    budget_map = {"free":"only free events","low":"events under £20","medium":"events under £50","any":"any budget"}
    budget_desc = budget_map.get(budget, "any budget")

    # More specific search per category for better results
    category_sources = {
        "tech": "Eventbrite, Luma, Meetup tech groups",
        "art": "Eventbrite, museum websites, gallery listings, Time Out London",
        "food": "Eventbrite, food festival listings, Time Out London food events",
        "music": "Dice.fm, RA (Resident Advisor), Eventbrite",
        "hackathon": "Eventbrite, Luma, Devpost, Meetup",
        "culture": "museum websites, Eventbrite, Time Out London",
        "social": "Eventbrite, Luma, Meetup",
        "outdoor": "Eventbrite, local council listings, Time Out London",
        "film": "BFI, Eventbrite, local cinema listings",
        "clubbing": "Dice.fm, RA (Resident Advisor), Eventbrite",
    }
    sources = set()
    for interest in (interests or []):
        if interest in category_sources:
            sources.update(category_sources[interest].split(", "))
    sources_str = ", ".join(sources) if sources else "Eventbrite, Luma, Dice.fm, Meetup, Time Out London"

    search_prompt = f"""Today is {today}. Search for real upcoming events in {city} happening {time_filter}.

I want events specifically about: {interests_str}
Budget: {budget_desc}

Search these sources: {sources_str}

Find at least 5 real events. For each one include: exact event name, date, time, venue name, area of city, ticket price, and booking URL.
Be specific — search for "{interests_str} events {city} {time_filter}" directly."""

    for attempt in range(3):
        try:
            search_response = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1500,
                tools=[{"type":"web_search_20250305","name":"web_search"}],
                messages=[{"role":"user","content":search_prompt}]
            )
            raw_text = "".join(b.text for b in search_response.content if b.type == "text")
            if not raw_text.strip():
                return []

            format_prompt = f"""Convert these event listings into a JSON array.
Events found: {raw_text}
Return ONLY a valid JSON array:
[{{"title":"","organiser":"","date":"","time":"","location":"","price":"","price_value":0,"category":"","is_outdoor":false,"description":"","url":"","source":"","weather_match":false}}]
Output only the JSON array, nothing else."""

            format_response = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=2000,
                messages=[{"role":"user","content":format_prompt}]
            )
            json_text = format_response.content[0].text.strip()
            if json_text.startswith("```"):
                json_text = json_text.split("```")[1]
                if json_text.startswith("json"):
                    json_text = json_text[4:]
            start = json_text.find("[")
            end = json_text.rfind("]") + 1
            if start != -1 and end > start:
                events = json.loads(json_text[start:end])

                # Inject museum events if searching for art/culture in London
                cultural_interests = {"art", "culture", "museum", "exhibition", "gallery"}
                is_london = "london" in city.lower()
                wants_culture = bool(set(interests) & cultural_interests) or not interests

                if is_london and wants_culture:
                    museum_events = get_museum_events()
                    # Filter by budget if needed
                    if budget == "free":
                        museum_events = [e for e in museum_events if e.get("price_value", 0) == 0]
                    elif budget == "low":
                        museum_events = [e for e in museum_events if e.get("price_value", 0) <= 20]
                    elif budget == "medium":
                        museum_events = [e for e in museum_events if e.get("price_value", 0) <= 50]
                    # Add up to 3 museum events, avoid duplicates
                    existing_titles = {e.get("title", "").lower() for e in events}
                    added = 0
                    for mev in museum_events:
                        if mev.get("title", "").lower() not in existing_titles and added < 3:
                            events.insert(added * 2, mev)  # Interleave with other results
                            added += 1

                set_cache(cache_key, events)
                return events
            return []

        except anthropic.RateLimitError:
            if attempt < 2:
                time.sleep(30)
            else:
                return []
        except Exception as e:
            print(f"Search error: {e}")
            return []
    return []


# ════════════════════════════
# AUTH ROUTES
# ════════════════════════════

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.json
    username = data.get("username", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    city = data.get("city", "London").strip()

    if not username or not email or not password:
        return jsonify({"error": "All fields required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    colors = ["#FF3CAC","#3B82F6","#00D4AA","#FF6B35","#FFE000","#8B5CF6"]
    avatar_color = random.choice(colors)

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO users (username, email, password_hash, city, avatar_color) VALUES (?,?,?,?,?)",
            (username, email, hash_password(password), city, avatar_color)
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        session["user_id"] = user["id"]
        log_activity(user["id"], "joined out.")

        return jsonify({"user": {
            "id": user["id"], "username": user["username"],
            "email": user["email"], "city": user["city"],
            "avatar_color": user["avatar_color"], "interests": []
        }})
    except sqlite3.IntegrityError as e:
        return jsonify({"error": "Username or email already taken"}), 400


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND password_hash = ?",
        (email, hash_password(password))
    ).fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "Wrong email or password"}), 401

    session["user_id"] = user["id"]
    interests = json.loads(user["interests"] or "[]")
    return jsonify({"user": {
        "id": user["id"], "username": user["username"],
        "email": user["email"], "city": user["city"],
        "avatar_color": user["avatar_color"],
        "bio": user["bio"], "interests": interests
    }})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
def me():
    user = get_current_user()
    if not user:
        return jsonify({"user": None})
    interests = json.loads(user.get("interests") or "[]")
    return jsonify({"user": {
        "id": user["id"], "username": user["username"],
        "email": user["email"], "city": user["city"],
        "avatar_color": user["avatar_color"],
        "bio": user["bio"], "interests": interests
    }})


@app.route("/api/auth/update", methods=["POST"])
@login_required
def update_profile():
    user = get_current_user()
    data = request.json
    city = data.get("city", user["city"])
    bio = data.get("bio", user["bio"])
    interests = data.get("interests", [])

    conn = get_db()
    conn.execute(
        "UPDATE users SET city=?, bio=?, interests=? WHERE id=?",
        (city, bio, json.dumps(interests), user["id"])
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ════════════════════════════
# SOCIAL ROUTES
# ════════════════════════════

@app.route("/api/social/save-event", methods=["POST"])
@login_required
def save_event():
    user = get_current_user()
    data = request.json
    event = data.get("event", {})
    event_id = data.get("event_id") or f"ev_{hash(event.get('title',''))}"

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM saved_events WHERE user_id=? AND event_id=?",
        (user["id"], event_id)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM saved_events WHERE user_id=? AND event_id=?", (user["id"], event_id))
        conn.commit()
        conn.close()
        return jsonify({"saved": False})
    else:
        conn.execute(
            "INSERT INTO saved_events (user_id, event_id, event_data) VALUES (?,?,?)",
            (user["id"], event_id, json.dumps(event))
        )
        conn.commit()
        conn.close()
        log_activity(user["id"], "saved", event.get("title"), event_id, event)
        return jsonify({"saved": True})


@app.route("/api/social/saved-events", methods=["GET"])
@login_required
def get_saved_events():
    user = get_current_user()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM saved_events WHERE user_id=? ORDER BY saved_at DESC",
        (user["id"],)
    ).fetchall()
    conn.close()
    events = []
    for row in rows:
        ev = json.loads(row["event_data"])
        ev["event_id"] = row["event_id"]
        ev["saved_at"] = row["saved_at"]
        events.append(ev)
    return jsonify({"events": events})


@app.route("/api/social/going", methods=["POST"])
@login_required
def mark_going():
    user = get_current_user()
    data = request.json
    event_id = data.get("event_id")
    event = data.get("event", {})

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM going WHERE user_id=? AND event_id=?",
        (user["id"], event_id)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM going WHERE user_id=? AND event_id=?", (user["id"], event_id))
        conn.commit()
        count = conn.execute("SELECT COUNT(*) as c FROM going WHERE event_id=?", (event_id,)).fetchone()["c"]
        conn.close()
        return jsonify({"going": False, "count": count})
    else:
        conn.execute(
            "INSERT OR REPLACE INTO going (user_id, event_id, event_title, event_date, event_location, event_url) VALUES (?,?,?,?,?,?)",
            (user["id"], event_id, event.get("title",""), event.get("date",""), event.get("location",""), event.get("url",""))
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) as c FROM going WHERE event_id=?", (event_id,)).fetchone()["c"]
        conn.close()
        log_activity(user["id"], "going", event.get("title"), event_id, event)
        return jsonify({"going": True, "count": count})


@app.route("/api/social/going-count/<event_id>", methods=["GET"])
def going_count(event_id):
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM going WHERE event_id=?", (event_id,)).fetchone()["c"]
    user_going = False
    if session.get("user_id"):
        user_going = bool(conn.execute(
            "SELECT id FROM going WHERE user_id=? AND event_id=?",
            (session["user_id"], event_id)
        ).fetchone())
    conn.close()
    return jsonify({"count": count, "user_going": user_going})


@app.route("/api/social/feed", methods=["GET"])
@login_required
def activity_feed():
    user = get_current_user()
    conn = get_db()

    # Get followed user IDs + own activity
    following = conn.execute(
        "SELECT following_id FROM follows WHERE follower_id=?", (user["id"],)
    ).fetchall()
    ids = [user["id"]] + [f["following_id"] for f in following]
    placeholders = ",".join("?" * len(ids))

    rows = conn.execute(f"""
        SELECT a.*, u.username, u.avatar_color
        FROM activity a
        JOIN users u ON a.user_id = u.id
        WHERE a.user_id IN ({placeholders})
        ORDER BY a.created_at DESC LIMIT 30
    """, ids).fetchall()
    conn.close()

    feed = []
    for row in rows:
        item = dict(row)
        if item.get("event_data"):
            item["event_data"] = json.loads(item["event_data"])
        feed.append(item)

    return jsonify({"feed": feed})


@app.route("/api/social/follow", methods=["POST"])
@login_required
def follow_user():
    user = get_current_user()
    data = request.json
    target_id = data.get("user_id")

    if target_id == user["id"]:
        return jsonify({"error": "Can't follow yourself"}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM follows WHERE follower_id=? AND following_id=?",
        (user["id"], target_id)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM follows WHERE follower_id=? AND following_id=?", (user["id"], target_id))
        following = False
    else:
        conn.execute("INSERT INTO follows (follower_id, following_id) VALUES (?,?)", (user["id"], target_id))
        following = True

    conn.commit()
    conn.close()
    return jsonify({"following": following})


@app.route("/api/social/profile/<username>", methods=["GET"])
def get_profile(username):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    user = dict(user)
    activity = conn.execute(
        "SELECT * FROM activity WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (user["id"],)
    ).fetchall()

    going_events = conn.execute(
        "SELECT * FROM going WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
        (user["id"],)
    ).fetchall()

    follower_count = conn.execute(
        "SELECT COUNT(*) as c FROM follows WHERE following_id=?", (user["id"],)
    ).fetchone()["c"]

    following_count = conn.execute(
        "SELECT COUNT(*) as c FROM follows WHERE follower_id=?", (user["id"],)
    ).fetchone()["c"]

    is_following = False
    if session.get("user_id"):
        is_following = bool(conn.execute(
            "SELECT id FROM follows WHERE follower_id=? AND following_id=?",
            (session["user_id"], user["id"])
        ).fetchone())

    conn.close()

    return jsonify({
        "user": {
            "id": user["id"], "username": user["username"],
            "city": user["city"], "bio": user["bio"],
            "avatar_color": user["avatar_color"],
            "interests": json.loads(user.get("interests") or "[]"),
            "follower_count": follower_count,
            "following_count": following_count,
            "is_following": is_following
        },
        "activity": [dict(a) for a in activity],
        "going_events": [dict(g) for g in going_events]
    })


@app.route("/api/social/search-users", methods=["GET"])
def search_users():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"users": []})
    conn = get_db()
    users = conn.execute(
        "SELECT id, username, city, avatar_color, bio FROM users WHERE username LIKE ? LIMIT 10",
        (f"%{q}%",)
    ).fetchall()
    conn.close()
    return jsonify({"users": [dict(u) for u in users]})


# ════════════════════════════
# SQUAD ROUTES
# ════════════════════════════

@app.route("/squad")
def squad_page():
    return render_template("squad.html")

@app.route("/squad/<code>")
def squad_join(code):
    return render_template("squad.html", squad_code=code)

@app.route("/api/squad/create", methods=["POST"])
def create_squad():
    data = request.json
    code = generate_squad_code()
    squads = load_squads()
    squads[code] = {
        "name": data.get("name","My Squad"),
        "city": data.get("city","London"),
        "created_at": datetime.now().isoformat(),
        "members": [{"name": data.get("creator","Someone"), "joined_at": datetime.now().isoformat()}],
        "events": [], "votes": {}
    }
    save_squads(squads)
    return jsonify({"code": code, "squad": squads[code]})

@app.route("/api/squad/<code>", methods=["GET"])
def get_squad(code):
    squads = load_squads()
    if code not in squads:
        return jsonify({"error": "Squad not found"}), 404
    squad = squads[code]
    events_with_votes = []
    for ev in squad.get("events", []):
        ev_id = ev.get("id","")
        ev_votes = squad.get("votes",{}).get(ev_id,{})
        yes = sum(1 for v in ev_votes.values() if v=="yes")
        no = sum(1 for v in ev_votes.values() if v=="no")
        maybe = sum(1 for v in ev_votes.values() if v=="maybe")
        total = len(squad.get("members",[]))
        events_with_votes.append({
            **ev,
            "votes": {"yes":yes,"no":no,"maybe":maybe},
            "consensus": "everyone's in" if yes==total and total>0
                        else "popular" if yes>=total*0.6 and total>1
                        else "mixed" if yes>0 else "undecided",
            "total_members": total
        })
    events_with_votes.sort(key=lambda x: x["votes"]["yes"], reverse=True)
    return jsonify({**squad, "events": events_with_votes})

@app.route("/api/squad/<code>/set-deadline", methods=["POST"])
def set_deadline(code):
    squads = load_squads()
    if code not in squads:
        return jsonify({"error": "Squad not found"}), 404
    data = request.json
    minutes = int(data.get("minutes", 30))
    minutes = max(1, min(minutes, 1440))  # clamp 1min - 24hrs
    deadline = (datetime.now() + timedelta(minutes=minutes)).isoformat()
    squads[code]["deadline"] = deadline
    squads[code]["deadline_minutes"] = minutes
    save_squads(squads)
    return jsonify({"deadline": deadline, "minutes": minutes})


@app.route("/api/squad/<code>/clear-deadline", methods=["POST"])
def clear_deadline(code):
    squads = load_squads()
    if code not in squads:
        return jsonify({"error": "Squad not found"}), 404
    squads[code].pop("deadline", None)
    squads[code].pop("deadline_minutes", None)
    save_squads(squads)
    return jsonify({"ok": True})


@app.route("/api/squad/<code>/join", methods=["POST"])
def join_squad(code):
    squads = load_squads()
    if code not in squads:
        return jsonify({"error": "Squad not found"}), 404
    data = request.json
    name = data.get("name","Anonymous")
    squad = squads[code]
    if name not in [m["name"] for m in squad.get("members",[])]:
        squad["members"].append({"name": name, "joined_at": datetime.now().isoformat()})
        save_squads(squads)
    return jsonify({"squad": squad, "code": code})

@app.route("/api/squad/<code>/add-events", methods=["POST"])
def add_events_to_squad(code):
    squads = load_squads()
    if code not in squads:
        return jsonify({"error": "Squad not found"}), 404
    data = request.json
    squad = squads[code]
    weather = get_weather(squad.get("city","London"))
    events = search_events(squad.get("city","London"), data.get("interests",["social"]),
                          data.get("budget","any"), weather, data.get("time_filter","this weekend"))
    for i, ev in enumerate(events):
        ev["id"] = f"ev_{int(time.time())}_{i}"
    squad["events"] = events
    squad["votes"] = {}
    save_squads(squads)
    return jsonify({"events": events, "count": len(events)})

@app.route("/api/squad/<code>/vote", methods=["POST"])
def vote_on_event(code):
    squads = load_squads()
    if code not in squads:
        return jsonify({"error": "Squad not found"}), 404
    data = request.json
    event_id = data.get("event_id")
    member = data.get("member_name")
    vote = data.get("vote")
    if vote not in ["yes","no","maybe"]:
        return jsonify({"error": "Invalid vote"}), 400
    squad = squads[code]
    if event_id not in squad["votes"]:
        squad["votes"][event_id] = {}
    squad["votes"][event_id][member] = vote
    save_squads(squads)
    ev_votes = squad["votes"][event_id]
    return jsonify({
        "yes": sum(1 for v in ev_votes.values() if v=="yes"),
        "no": sum(1 for v in ev_votes.values() if v=="no"),
        "maybe": sum(1 for v in ev_votes.values() if v=="maybe"),
        "my_vote": vote
    })


# ════════════════════════════
# MAIN ROUTES
# ════════════════════════════

@app.route("/api/museums", methods=["GET"])
def museum_events():
    """Return current V&A and Tate exhibitions directly."""
    events = get_museum_events()
    return jsonify({"events": events, "count": len(events)})


@app.route("/api/cache/status", methods=["GET"])
def cache_status():
    now = time.time()
    entries = []
    for key, (result, ts) in _search_cache.items():
        age_secs = int(now - ts)
        expires_in = max(0, CACHE_TTL - age_secs)
        entries.append({
            "key": key,
            "events": len(result),
            "age": f"{age_secs//60}m {age_secs%60}s ago",
            "expires_in": f"{expires_in//60}m {expires_in%60}s",
            "fresh": expires_in > 0
        })
    return jsonify({
        "cached_searches": len(entries),
        "entries": entries,
        "cache_ttl_minutes": CACHE_TTL // 60
    })


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    _search_cache.clear()
    print("✓ Cache cleared")
    return jsonify({"ok": True, "message": "Cache cleared"})


@app.route("/api/popular", methods=["GET"])
def popular_events():
    """Return most-saved/going events, falling back to AI-generated popular picks."""
    city = request.args.get("city", "London")
    conn = get_db()

    # Get top events by going count
    rows = conn.execute("""
        SELECT event_id, event_title, event_date, event_location, event_url,
               COUNT(*) as going_count
        FROM going
        WHERE event_location LIKE ?
        GROUP BY event_id
        ORDER BY going_count DESC
        LIMIT 5
    """, (f"%{city}%",)).fetchall()
    conn.close()

    events = []
    for row in rows:
        events.append({
            "title": row["event_title"],
            "date": row["event_date"],
            "location": row["event_location"],
            "url": row["event_url"],
            "going_count": row["going_count"],
            "price": "",
            "price_value": 0
        })

    # If not enough real data, use Claude to suggest popular events for the city
    if len(events) < 3:
        try:
            today = datetime.now().strftime("%d %B %Y")
            prompt = f"""Today is {today}. Search for the most popular and well-known upcoming events in {city} this weekend and next week. These should be events with high attendance or buzz — festivals, major gigs, popular markets, big exhibitions.

Return ONLY a JSON array of 5 events:
[{{"title":"","date":"","location":"","price":"","price_value":0,"url":"","going_count":0,"description":""}}]
Output only the JSON array."""

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            full_text = "".join(b.text for b in response.content if b.type == "text")
            full_text = full_text.strip()
            if full_text.startswith("```"):
                full_text = full_text.split("```")[1]
                if full_text.startswith("json"):
                    full_text = full_text[4:]
            start = full_text.find("[")
            end = full_text.rfind("]") + 1
            if start != -1 and end > start:
                ai_events = json.loads(full_text[start:end])
                # Merge: real going data first, then AI suggestions
                existing_titles = {e["title"] for e in events}
                for ev in ai_events:
                    if ev.get("title") and ev["title"] not in existing_titles:
                        events.append(ev)
                        if len(events) >= 5:
                            break
        except Exception as e:
            print(f"Popular events AI error: {e}")

    return jsonify({"events": events[:5], "city": city})


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/profile")
def profile_page():
    return render_template("profile.html")

@app.route("/profile/<username>")
def user_profile_page(username):
    return render_template("profile.html", profile_username=username)

@app.route("/api/events", methods=["POST"])
def get_events():
    data = request.json
    city = data.get("city","London")
    interests = data.get("interests",["art","music"])
    budget = data.get("budget","any")
    time_filter = data.get("time_filter","this weekend")
    weather = get_weather(city)
    events = search_events(city, interests, budget, weather, time_filter)
    return jsonify({"events": events, "weather": weather, "city": city, "count": len(events)})

@app.route("/api/ask-ai", methods=["POST"])
def ask_ai():
    data = request.json
    event = data.get("event",{})
    question = data.get("question","")
    if not question or not event:
        return jsonify({"error": "Missing question or event"}), 400
    prompt = f"""You are a helpful local guide. Event: {event.get('title')} on {event.get('date')} at {event.get('location')}, {event.get('price')}.
User asks: {question}
Answer in 2-3 sentences. Be friendly and direct."""
    try:
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role":"user","content":prompt}])
        return jsonify({"answer": response.content[0].text})
    except Exception as e:
        return jsonify({"error": "Could not get answer"}), 500

@app.route("/api/surprise", methods=["POST"])
def surprise():
    data = request.json
    city = data.get("city","London")
    mood = data.get("mood","adventurous")
    weather = get_weather(city)
    prompt = f"""User in {city}, feeling {mood}. Weather: {weather['icon']} {weather['temp']}°C.
Search for one perfect upcoming event. Return ONLY JSON:
{{"title":"","organiser":"","date":"","time":"","location":"","price":"","category":"","is_outdoor":false,"description":"","url":"","source":"","weather_match":true,"surprise_reason":""}}"""
    try:
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1000,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            messages=[{"role":"user","content":prompt}])
        full_text = "".join(b.text for b in response.content if b.type=="text")
        start = full_text.find("{")
        end = full_text.rfind("}") + 1
        if start != -1 and end > start:
            return jsonify({"event": json.loads(full_text[start:end]), "weather": weather})
    except Exception as e:
        print(f"Surprise error: {e}")
    return jsonify({"error": "Could not find event"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)

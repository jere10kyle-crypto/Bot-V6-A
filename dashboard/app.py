from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import json, os, secrets
from datetime import datetime
from collections import Counter

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

DEFAULT_SETTINGS = {
    "tier1_strikes": 1,  "tier1_minutes": 5,
    "tier2_strikes": 2,  "tier2_minutes": 15,
    "tier3_strikes": 3,  "tier3_minutes": 60,
    "tier4_strikes": 4,  "tier4_minutes": 1440,
    "tier5_strikes": 5,  "tier5_minutes": 40320,
    "spam_word_limit":   5,
    "spam_word_window":  10,
    "spam_word_tier":    2,
    "raid_join_limit":       5,
    "raid_join_window":      10,
    "raid_lockdown_minutes": 10,
    "raid_timeout_minutes":  10,
}

DEFAULT_WELCOME = {
    "enabled":    False,
    "dm":         True,
    "channel_id": "",
    "message":    "👋 Welcome to **{server}**, {user}!\nPlease read the rules and enjoy your stay.",
}

# ── Guild helpers ──────────────────────────────────────────────────────────────
def list_guilds():
    guilds = []
    if not os.path.isdir(DATA_DIR):
        return guilds
    for name in os.listdir(DATA_DIR):
        if name.isdigit():
            info_path = os.path.join(DATA_DIR, name, "guild_info.json")
            try:
                with open(info_path) as f:
                    info = json.load(f)
                guilds.append(info)
            except (FileNotFoundError, json.JSONDecodeError):
                guilds.append({"id": name, "name": f"Server {name}", "member_count": 0})
    return guilds

def get_active_guild_id():
    guilds = list_guilds()
    if not guilds:
        return None
    saved = session.get("guild_id")
    ids   = [g["id"] for g in guilds]
    if saved and saved in ids:
        return saved
    gid = guilds[0]["id"]
    session["guild_id"] = gid
    return gid

def guild_data_dir(guild_id):
    d = os.path.join(DATA_DIR, str(guild_id))
    os.makedirs(d, exist_ok=True)
    return d

def load(guild_id, f, default):
    path = os.path.join(guild_data_dir(guild_id), f)
    try:
        with open(path) as fp:
            return json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save(guild_id, f, data):
    path = os.path.join(guild_data_dir(guild_id), f)
    with open(path, "w") as fp:
        json.dump(data, fp, indent=2)

# ── Auth helpers ──────────────────────────────────────────────────────────────
def get_users():
    path = os.path.join(DATA_DIR, "users.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(path) as fp:
            return json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_users(users):
    path = os.path.join(DATA_DIR, "users.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as fp:
        json.dump(users, fp, indent=2)

def current_user():
    uname = session.get("username")
    if not uname:
        return None
    users = get_users()
    u = users.get(uname)
    if not u:
        return None
    return {"username": uname, **u}

def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped

def edit_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not (u.get("is_admin") or u.get("can_edit")):
            flash("You don't have edit permission. Ask the admin to grant it.", "error")
            return redirect(url_for("index"))
        return view(*a, **kw)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u.get("is_admin"):
            flash("Admin only.", "error")
            return redirect(url_for("index"))
        return view(*a, **kw)
    return wrapped

def owner_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u.get("is_owner"):
            flash("Only the server owner can do that.", "error")
            return redirect(url_for("index") + "?tab=manage-users")
        return view(*a, **kw)
    return wrapped

@app.context_processor
def inject_context():
    u      = current_user()
    guilds = list_guilds()
    gid    = get_active_guild_id()
    active_guild = next((g for g in guilds if g["id"] == gid), None)
    return {
        "current_user":   u,
        "can_edit":       bool(u and (u.get("is_admin") or u.get("can_edit"))),
        "is_admin":       bool(u and u.get("is_admin")),
        "is_owner":       bool(u and u.get("is_owner")),
        "guild_list":     guilds,
        "active_guild":   active_guild,
        "active_guild_id": gid,
    }

# ── Guild switch ───────────────────────────────────────────────────────────────
@app.route("/switch_guild/<guild_id>")
@login_required
def switch_guild(guild_id):
    guilds = list_guilds()
    if any(g["id"] == guild_id for g in guilds):
        session["guild_id"] = guild_id
    return redirect(url_for("index"))

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        uname = request.form.get("username", "").strip().lower()
        pw    = request.form.get("password", "")
        users = get_users()
        u = users.get(uname)
        if u and check_password_hash(u["password_hash"], pw):
            session["username"] = uname
            return redirect(url_for("index"))
        flash("Wrong username or password.", "error")
    if not get_users():
        return redirect(url_for("signup"))
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    users      = get_users()
    first_user = len(users) == 0
    if request.method == "POST":
        uname = request.form.get("username", "").strip().lower()
        pw    = request.form.get("password", "")
        if not uname or not pw:
            flash("Username and password required.", "error")
        elif len(pw) < 4:
            flash("Password must be at least 4 characters.", "error")
        elif uname in users:
            flash("That username is taken.", "error")
        else:
            users[uname] = {
                "password_hash": generate_password_hash(pw),
                "is_owner":  first_user,
                "is_admin":  first_user,
                "can_edit":  first_user,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            save_users(users)
            session["username"] = uname
            if first_user:
                flash("Welcome! You're the admin.", "ok")
            else:
                flash("Account created. Waiting for admin to grant edit access.", "ok")
            return redirect(url_for("index"))
    return render_template("signup.html", first_user=first_user)

@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

@app.route("/users")
@admin_required
def users_page():
    users = get_users()
    return render_template("users.html", users=users)

@app.route("/users/toggle_edit/<username>")
@admin_required
def toggle_edit(username):
    users = get_users()
    if username in users and not users[username].get("is_admin"):
        users[username]["can_edit"] = not users[username].get("can_edit", False)
        save_users(users)
    return redirect(url_for("index") + "?tab=manage-users")

@app.route("/users/make_admin/<username>")
@owner_required
def make_admin(username):
    users = get_users()
    if username in users and not users[username].get("is_owner"):
        users[username]["is_admin"] = True
        users[username]["can_edit"] = True
        save_users(users)
    return redirect(url_for("index") + "?tab=manage-users")

@app.route("/users/revoke_admin/<username>")
@owner_required
def revoke_admin(username):
    users = get_users()
    if username in users and not users[username].get("is_owner"):
        users[username]["is_admin"] = False
        save_users(users)
    return redirect(url_for("index") + "?tab=manage-users")

@app.route("/users/delete/<username>")
@admin_required
def delete_user(username):
    me = current_user()
    if me and username == me["username"]:
        flash("You can't delete yourself.", "error")
        return redirect(url_for("index") + "?tab=manage-users")
    users = get_users()
    users.pop(username, None)
    save_users(users)
    return redirect(url_for("index") + "?tab=manage-users")

# ── Main routes ───────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    gid = get_active_guild_id()
    if gid is None:
        return render_template("no_guilds.html")

    strikes      = load(gid, "strikes.json", {})
    logs         = load(gid, "logs.json", [])
    _bw          = load(gid, "banned_words.json", {})
    banned_words = {w: t for w, t in _bw.items()} if isinstance(_bw, dict) else {w: 1 for w in _bw}
    user_names   = load(gid, "user_names.json", {})
    users        = get_users()

    top_users     = sorted(strikes.items(), key=lambda x: x[1], reverse=True)[:10]
    action_counts = Counter(e["action"] for e in logs)
    recent_logs   = list(reversed(logs[-20:]))
    distribution  = Counter(strikes.values())
    settings      = {**DEFAULT_SETTINGS, **load(gid, "settings.json", {})}
    welcome       = {**DEFAULT_WELCOME,  **load(gid, "welcome.json",   {})}

    return render_template(
        "index.html",
        strikes=strikes,
        top_users=top_users,
        logs=recent_logs,
        banned_words=banned_words,
        action_counts=dict(action_counts),
        distribution={str(k): v for k, v in sorted(distribution.items())},
        total_strikes=sum(strikes.values()),
        total_logs=len(logs),
        total_words=len(banned_words),
        settings=settings,
        user_names=user_names,
        users=users,
        welcome=welcome,
    )

@app.route("/add_word", methods=["POST"])
@edit_required
def add_word():
    gid  = get_active_guild_id()
    word = request.form.get("word", "").lower().strip()
    tier = max(1, min(5, int(request.form.get("tier", 1))))
    if word and gid:
        words = load(gid, "banned_words.json", {})
        if isinstance(words, list):
            words = {w: 1 for w in words}
        words[word] = tier
        save(gid, "banned_words.json", words)
    return redirect(url_for("index"))

@app.route("/remove_word/<word>")
@edit_required
def remove_word(word):
    gid   = get_active_guild_id()
    words = load(gid, "banned_words.json", {})
    if isinstance(words, list):
        words = {w: 1 for w in words}
    words.pop(word, None)
    save(gid, "banned_words.json", words)
    return redirect(url_for("index"))

@app.route("/add_strike", methods=["POST"])
@edit_required
def add_strike_route():
    gid      = get_active_guild_id()
    username = request.form.get("username", "").strip()
    reason   = request.form.get("reason", "Manual strike (dashboard)").strip()
    if not username:
        flash("A username is required.", "error")
        return redirect(url_for("index") + "?tab=overview")

    user_names = load(gid, "user_names.json", {})
    uid = next(
        (uid for uid, dname in user_names.items()
         if dname.lower() == username.lower()),
        None
    )
    if not uid:
        flash(f"No user named \"{username}\" found. They need to have sent at least one message so the bot knows them.", "error")
        return redirect(url_for("index") + "?tab=overview")

    display = user_names.get(uid, username)
    pending = load(gid, "pending_strikes.json", [])
    pending.append({
        "user_id": uid,
        "reason":  f"{reason} (dashboard — {session.get('username', 'moderator')})",
    })
    save(gid, "pending_strikes.json", pending)

    strikes = load(gid, "strikes.json", {})
    new_count = strikes.get(uid, 0) + 1
    strikes[uid] = new_count
    save(gid, "strikes.json", strikes)

    logs = load(gid, "logs.json", [])
    logs.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "action":    "STRIKE",
        "user":      f"{display} ({uid})",
        "reason":    reason,
        "moderator": session.get("username", "dashboard"),
    })
    if len(logs) > 500:
        logs = logs[-500:]
    save(gid, "logs.json", logs)
    flash(f"Strike added — {display} now has {new_count} strike(s). The bot will apply any mute within ~10 seconds.", "ok")
    return redirect(url_for("index") + "?tab=overview")

@app.route("/unmute_me", methods=["POST"])
@owner_required
def unmute_me():
    gid = request.form.get("guild_id") or get_active_guild_id()
    if not gid:
        flash("No server selected.", "error")
        return redirect(url_for("index"))
    owner_id = os.environ.get("OWNER_ID", "").strip()
    if not owner_id:
        flash("OWNER_ID is not configured.", "error")
        return redirect(url_for("index"))
    pending = load(gid, "pending_unmutes.json", [])
    pending.append({"user_id": owner_id, "reason": "Self-unmute via dashboard"})
    save(gid, "pending_unmutes.json", pending)
    flash("Unmute request sent — takes effect within ~10 seconds.", "ok")
    return redirect(url_for("index"))

@app.route("/unmute_user/<uid>")
@edit_required
def unmute_user(uid):
    gid = get_active_guild_id()
    if not gid:
        flash("No server selected.", "error")
        return redirect(url_for("index"))
    pending = load(gid, "pending_unmutes.json", [])
    pending.append({"user_id": uid, "reason": "Manual unmute via dashboard"})
    save(gid, "pending_unmutes.json", pending)
    names  = load(gid, "user_names.json", {})
    display = names.get(uid, uid)
    flash(f"Unmute request sent for {display} — takes effect within ~10 seconds.", "ok")
    return redirect(url_for("index") + "?tab=overview")

@app.route("/reset_strikes/<uid>")
@edit_required
def reset_strikes(uid):
    gid     = get_active_guild_id()
    strikes = load(gid, "strikes.json", {})
    strikes.pop(uid, None)
    save(gid, "strikes.json", strikes)
    return redirect(url_for("index"))

@app.route("/api/stats")
@login_required
def api_stats():
    gid     = get_active_guild_id()
    strikes = load(gid, "strikes.json", {})
    logs    = load(gid, "logs.json",    [])
    words   = load(gid, "banned_words.json", {})
    return jsonify({
        "total_strikes":       sum(strikes.values()),
        "total_users_striked": len(strikes),
        "total_logs":          len(logs),
        "total_banned_words":  len(words),
    })

@app.route("/save_welcome", methods=["POST"])
@edit_required
def save_welcome():
    gid = get_active_guild_id()
    w   = {**DEFAULT_WELCOME, **load(gid, "welcome.json", {})}
    w["enabled"]    = request.form.get("enabled") == "1"
    w["dm"]         = request.form.get("dm") == "1"
    w["channel_id"] = request.form.get("channel_id", "").strip()
    w["message"]    = request.form.get("message", DEFAULT_WELCOME["message"])
    save(gid, "welcome.json", w)
    flash("Welcome message saved.", "ok")
    return redirect(url_for("index") + "?tab=welcome")

@app.route("/save_settings", methods=["POST"])
@edit_required
def save_settings():
    gid = get_active_guild_id()
    s   = {**DEFAULT_SETTINGS, **load(gid, "settings.json", {})}
    try:
        for t in range(1, 6):
            s[f"tier{t}_strikes"] = max(1, int(request.form.get(f"tier{t}_strikes", DEFAULT_SETTINGS[f"tier{t}_strikes"])))
            s[f"tier{t}_minutes"] = max(1, int(request.form.get(f"tier{t}_minutes", DEFAULT_SETTINGS[f"tier{t}_minutes"])))
        s["spam_word_limit"]       = max(2, int(request.form.get("spam_word_limit",       DEFAULT_SETTINGS["spam_word_limit"])))
        s["spam_word_window"]      = max(1, int(request.form.get("spam_word_window",      DEFAULT_SETTINGS["spam_word_window"])))
        s["spam_word_tier"]        = max(1, min(5, int(request.form.get("spam_word_tier", DEFAULT_SETTINGS["spam_word_tier"]))))
        s["raid_join_limit"]       = max(2, int(request.form.get("raid_join_limit",       DEFAULT_SETTINGS["raid_join_limit"])))
        s["raid_join_window"]      = max(1, int(request.form.get("raid_join_window",      DEFAULT_SETTINGS["raid_join_window"])))
        s["raid_lockdown_minutes"] = max(1, int(request.form.get("raid_lockdown_minutes", DEFAULT_SETTINGS["raid_lockdown_minutes"])))
        s["raid_timeout_minutes"]  = max(1, int(request.form.get("raid_timeout_minutes",  DEFAULT_SETTINGS["raid_timeout_minutes"])))
    except ValueError:
        pass
    save(gid, "settings.json", s)
    return redirect(url_for("index") + "#settings")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

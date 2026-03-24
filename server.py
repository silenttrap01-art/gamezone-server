"""
GameZone Server — никнеймы, запросы в друзья, уведомления внутри апки и через бота
"""
import os, json, random, sqlite3, threading
import uuid as _uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "7934530406:AAGJyT8LUYcmSx47GBV4v7zDCzAUIFgi4cA")
MINI_APP_URL = os.environ.get("MINI_APP_URL", "https://gamezone-app.vercel.app")

bot = telebot.TeleBot(BOT_TOKEN)
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── БД ───────────────────────────────────────────────────────
DB = "gamezone.db"

def get_db():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    c = get_db()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT 'Игрок',
        nickname TEXT DEFAULT '',
        registered INTEGER DEFAULT 0,
        games_played INTEGER DEFAULT 0,
        games_won INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS friends (
        user_id INTEGER, friend_id INTEGER,
        PRIMARY KEY(user_id, friend_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS friend_requests (
        id TEXT PRIMARY KEY,
        from_id INTEGER,
        from_nick TEXT,
        to_id INTEGER,
        status TEXT DEFAULT 'pending',
        created_at INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        type TEXT,
        data TEXT,
        read INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT 0)""")
    # Добавляем колонки если их нет (для существующих БД)
    try: c.execute("ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT ''")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN registered INTEGER DEFAULT 0")
    except: pass
    c.commit(); c.close()

init_db()

import time

def upsert_user(user):
    c = get_db()
    c.execute("""INSERT INTO users(user_id,username,first_name)
        VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name""",
        (user.id, user.username or "", user.first_name or "Игрок"))
    c.commit(); c.close()

def get_user(uid):
    c = get_db()
    r = c.execute("SELECT user_id,first_name,username,nickname,registered,games_won,games_played FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    if not r: return None
    return {"id":r[0],"name":r[1],"username":r[2],"nickname":r[3],"registered":r[4],"wins":r[5],"games":r[6]}

def get_registered_users():
    """Только пользователи с никнеймом (зарегистрированные в апке)"""
    c = get_db()
    rows = c.execute("SELECT user_id,first_name,username,nickname,games_won,games_played FROM users WHERE registered=1 AND nickname!=''").fetchall()
    c.close()
    return [{"id":r[0],"name":r[1],"username":r[2],"nickname":r[3],"wins":r[4],"games":r[5]} for r in rows]

def set_nickname(uid, nickname):
    c = get_db()
    c.execute("UPDATE users SET nickname=?, registered=1 WHERE user_id=?", (nickname, uid))
    c.commit(); c.close()

def nickname_taken(nickname, uid):
    c = get_db()
    r = c.execute("SELECT user_id FROM users WHERE nickname=? AND user_id!=?", (nickname, uid)).fetchone()
    c.close()
    return r is not None

def get_friends(uid):
    c = get_db()
    rows = c.execute("""SELECT u.user_id,u.first_name,u.nickname,u.games_won,u.games_played
        FROM friends f JOIN users u ON f.friend_id=u.user_id
        WHERE f.user_id=? AND u.registered=1""", (uid,)).fetchall()
    c.close()
    return [{"id":r[0],"name":r[1],"nickname":r[2],"wins":r[3],"games":r[4]} for r in rows]

def add_friend_db(uid, fid):
    c = get_db()
    c.execute("INSERT OR IGNORE INTO friends(user_id,friend_id) VALUES(?,?)", (uid,fid))
    c.execute("INSERT OR IGNORE INTO friends(user_id,friend_id) VALUES(?,?)", (fid,uid))
    c.commit(); c.close()

def are_friends(uid, fid):
    c = get_db()
    r = c.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (uid,fid)).fetchone()
    c.close()
    return r is not None

def create_friend_request(from_id, from_nick, to_id):
    # Проверяем нет ли уже запроса
    c = get_db()
    existing = c.execute("SELECT id FROM friend_requests WHERE from_id=? AND to_id=? AND status='pending'", (from_id,to_id)).fetchone()
    if existing: c.close(); return existing[0]
    rid = _uuid.uuid4().hex[:8]
    c.execute("INSERT INTO friend_requests(id,from_id,from_nick,to_id,status,created_at) VALUES(?,?,?,?,'pending',?)",
              (rid, from_id, from_nick, to_id, int(time.time())))
    c.commit(); c.close()
    return rid

def get_pending_requests(uid):
    c = get_db()
    rows = c.execute("""SELECT fr.id,fr.from_id,fr.from_nick,u.games_won,u.games_played
        FROM friend_requests fr LEFT JOIN users u ON fr.from_id=u.user_id
        WHERE fr.to_id=? AND fr.status='pending'""", (uid,)).fetchall()
    c.close()
    return [{"id":r[0],"from_id":r[1],"from_nick":r[2],"wins":r[3]or 0,"games":r[4]or 0} for r in rows]

def accept_request(rid, uid):
    c = get_db()
    r = c.execute("SELECT from_id FROM friend_requests WHERE id=? AND to_id=? AND status='pending'", (rid,uid)).fetchone()
    if r:
        c.execute("UPDATE friend_requests SET status='accepted' WHERE id=?", (rid,))
        c.commit(); c.close()
        add_friend_db(uid, r[0])
        return r[0]
    c.close(); return None

def decline_request(rid, uid):
    c = get_db()
    c.execute("UPDATE friend_requests SET status='declined' WHERE id=? AND to_id=?", (rid,uid))
    c.commit(); c.close()

def add_notification(uid, ntype, data):
    c = get_db()
    nid = _uuid.uuid4().hex[:8]
    c.execute("INSERT INTO notifications(id,user_id,type,data,read,created_at) VALUES(?,?,?,?,0,?)",
              (nid, uid, ntype, json.dumps(data), int(time.time())))
    c.commit(); c.close()
    return nid

def get_notifications(uid):
    c = get_db()
    rows = c.execute("SELECT id,type,data,read,created_at FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (uid,)).fetchall()
    c.close()
    return [{"id":r[0],"type":r[1],"data":json.loads(r[2]),"read":r[3],"created_at":r[4]} for r in rows]

def mark_read(uid):
    c = get_db()
    c.execute("UPDATE notifications SET read=1 WHERE user_id=?", (uid,))
    c.commit(); c.close()

def record_win(uid):
    c = get_db()
    c.execute("UPDATE users SET games_played=games_played+1,games_won=games_won+1 WHERE user_id=?", (uid,))
    c.commit(); c.close()

def record_game(uid):
    c = get_db()
    c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id=?", (uid,))
    c.commit(); c.close()

# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────
games = {}
connections = {}       # game_id → {user_id: ws}
user_connections = {}  # user_id → ws  (для уведомлений)
rematch_requests = {}

async def broadcast(game_id, msg):
    if game_id not in connections: return
    dead = []
    for uid, ws in list(connections[game_id].items()):
        try: await ws.send_text(json.dumps(msg))
        except: dead.append(uid)
    for uid in dead: connections[game_id].pop(uid, None)

async def send_to(game_id, uid, msg):
    ws = connections.get(game_id, {}).get(uid)
    if ws:
        try: await ws.send_text(json.dumps(msg))
        except: pass

async def send_notification(uid, msg):
    """Отправить уведомление конкретному пользователю если он онлайн"""
    ws = user_connections.get(str(uid))
    if ws:
        try: await ws.send_text(json.dumps(msg))
        except: pass

# ── WEBSOCKET УВЕДОМЛЕНИЙ ─────────────────────────────────────
@app.websocket("/ws/notify/{user_id}")
async def notify_endpoint(ws: WebSocket, user_id: str):
    """Отдельный WS канал для уведомлений (запросы в друзья, вызовы)"""
    await ws.accept()
    user_connections[user_id] = ws
    # Отправляем накопленные уведомления
    notifs = get_notifications(int(user_id))
    unread = [n for n in notifs if not n["read"]]
    if unread:
        await ws.send_text(json.dumps({"type":"notifications","items":unread}))
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "mark_read":
                mark_read(int(user_id))
            elif msg.get("type") == "get_notifications":
                notifs = get_notifications(int(user_id))
                await ws.send_text(json.dumps({"type":"notifications","items":notifs}))
    except WebSocketDisconnect:
        user_connections.pop(user_id, None)

# ── ИГРОВОЙ WEBSOCKET ─────────────────────────────────────────
@app.websocket("/ws/{game_id}/{user_id}/{username}")
async def ws_endpoint(ws: WebSocket, game_id: str, user_id: str, username: str):
    await ws.accept()
    connections.setdefault(game_id, {})[user_id] = ws

    if game_id in games:
        game = games[game_id]
        if game["player2"] is None and game["player1"]["id"] != user_id:
            game["player2"] = {"id": user_id, "name": username}
            game["status"] = "playing"
            await broadcast(game_id, {"type": "game_state", "game": game})
        else:
            await ws.send_text(json.dumps({"type": "game_state", "game": game}))
    else:
        gtype = game_id.split("_")[0]
        game = make_game(gtype, game_id, user_id, username)
        games[game_id] = game
        await ws.send_text(json.dumps({"type": "game_state", "game": game}))

    try:
        while True:
            data = await ws.receive_text()
            await handle_msg(game_id, user_id, username, json.loads(data))
    except WebSocketDisconnect:
        connections.get(game_id, {}).pop(user_id, None)
        game = games.get(game_id)
        if game and game["status"] == "playing":
            await broadcast(game_id, {"type": "opponent_left", "message": f"{username} покинул игру"})

def make_game(gtype, gid, uid, uname):
    base = {"id":gid,"type":gtype,"player1":{"id":uid,"name":uname},"player2":None,"status":"waiting","chat":[]}
    if gtype=="ttt": base.update({"board":[" "]*9,"current":uid,"winner":None})
    elif gtype=="rps": base.update({"choices":{},"ready":{},"result":None})
    elif gtype=="checkers":
        board=[None]*64
        for row in range(3):
            for col in range(8):
                if (row+col)%2==1: board[row*8+col]={"color":"white","king":False}
        for row in range(5,8):
            for col in range(8):
                if (row+col)%2==1: board[row*8+col]={"color":"red","king":False}
        base.update({"board":board,"current_color":"red","selected":None,"chain_piece":None,"winner":None})
    elif gtype=="dice":
        base.update({"round":1,"scores":{"p1":0,"p2":0},"guesser_role":"p1",
                     "secret":None,"attempts_left":3,"round_log":[],"phase":"guessing","winner":None})
    return base

async def handle_msg(game_id, user_id, username, msg):
    game = games.get(game_id)
    if not game: return
    t = msg.get("type")

    if t == "chat":
        entry = {"from":username,"text":msg["text"],"user_id":user_id}
        game["chat"].append(entry)
        await broadcast(game_id, {"type":"chat","entry":entry})

    elif t == "ttt_move":
        if game["status"]!="playing" or game["current"]!=user_id: return
        cell=msg["cell"]
        if game["board"][cell]!=" ": return
        sym="X" if user_id==game["player1"]["id"] else "O"
        game["board"][cell]=sym
        w=check_ttt(game["board"])
        if w:
            game["status"]="finished"; game["winner"]="draw" if w=="draw" else user_id
            if w!="draw":
                try: record_win(int(user_id))
                except: pass
                opp=game["player2"]["id"] if user_id==game["player1"]["id"] else game["player1"]["id"]
                try: record_game(int(opp))
                except: pass
        else:
            p1,p2=game["player1"]["id"],game["player2"]["id"] if game["player2"] else None
            game["current"]=p2 if user_id==p1 else p1
        await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "rps_choose":
        if game["status"]!="playing": return
        game["choices"][user_id]=msg["choice"]
        await broadcast(game_id, {"type":"rps_chose","user_id":user_id})

    elif t == "rps_ready":
        if game["status"]!="playing": return
        if not game["choices"].get(user_id):
            await send_to(game_id,user_id,{"type":"error","message":"Выбери оружие!"}); return
        game["ready"][user_id]=True
        p1=game["player1"]["id"]; p2=game["player2"]["id"] if game["player2"] else None
        if game["ready"].get(p1) and game["ready"].get(p2):
            c1,c2=game["choices"].get(p1),game["choices"].get(p2)
            beats={"rock":"scissors","scissors":"paper","paper":"rock"}
            if c1==c2: wid="draw"
            elif beats.get(c1)==c2: wid=p1
            else: wid=p2
            game["result"]={"c1":c1,"c2":c2,"winner":wid}
            game["status"]="finished"; game["winner"]=wid
            if wid!="draw":
                try: record_win(int(wid))
                except: pass
                try: record_game(int(p2 if wid==p1 else p1))
                except: pass
            await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "checkers_move":
        if game["status"]!="playing": return
        cur=game["current_color"]
        myc="red" if user_id==game["player1"]["id"] else "white"
        if myc!=cur: return
        action,cell=msg.get("action"),msg.get("cell")
        if action=="select":
            p=game["board"][cell]
            if p and p["color"]==cur: game["selected"]=cell; await broadcast(game_id,{"type":"game_state","game":game})
        elif action=="move":
            if apply_checkers(game,cell): await broadcast(game_id,{"type":"game_state","game":game})

    elif t == "dice_guess":
        if game["status"]!="playing": return
        p1=game["player1"]["id"]
        guesser=p1 if game["guesser_role"]=="p1" else (game["player2"]["id"] if game["player2"] else None)
        if user_id!=guesser: return
        game["secret"]=msg["number"]; game["phase"]="throwing"
        await broadcast(game_id,{"type":"game_state","game":game})

    elif t == "dice_throw":
        if game["status"]!="playing": return
        p1=game["player1"]["id"]; p2=game["player2"]["id"] if game["player2"] else None
        thrower=p2 if game["guesser_role"]=="p1" else p1
        if user_id!=thrower: return
        d1,d2=random.randint(1,6),random.randint(1,6); total=d1+d2
        game["round_log"].append({"d1":d1,"d2":d2,"total":total})
        game["attempts_left"]-=1
        gr=game["guesser_role"]; tr="p2" if gr=="p1" else "p1"
        if total==game["secret"]: game["scores"][gr]+=1; await finish_dice(game_id,game)
        elif game["attempts_left"]==0: game["scores"][tr]+=1; await finish_dice(game_id,game)
        else: await broadcast(game_id,{"type":"game_state","game":game})

    elif t == "surrender":
        p1=game["player1"]["id"]; p2=game["player2"]["id"] if game["player2"] else None
        wid=p2 if user_id==p1 else p1
        game["status"]="finished"; game["winner"]=wid; game["surrender"]=username
        if wid:
            try: record_win(int(wid))
            except: pass
            try: record_game(int(user_id))
            except: pass
        await broadcast(game_id,{"type":"game_state","game":game})

    elif t == "rematch_request":
        if not game or game["status"]!="finished": return
        rematch_requests.setdefault(game_id,set()).add(user_id)
        await broadcast(game_id,{"type":"rematch_requested","from_id":user_id,"from_name":username})
        reqs=rematch_requests.get(game_id,set())
        p1id=game["player1"]["id"]; p2id=game["player2"]["id"] if game["player2"] else None
        if p1id in reqs and p2id in reqs: await start_rematch(game_id,game)

    elif t == "rematch_accept":
        if not game or game["status"]!="finished": return
        rematch_requests.setdefault(game_id,set()).add(user_id)
        reqs=rematch_requests.get(game_id,set())
        p1id=game["player1"]["id"]; p2id=game["player2"]["id"] if game["player2"] else None
        if p1id in reqs and p2id in reqs: await start_rematch(game_id,game)
        else: await broadcast(game_id,{"type":"rematch_requested","from_id":user_id,"from_name":username})

async def start_rematch(old_id, old_game):
    gtype=old_game["type"]; p1=old_game["player1"]; p2=old_game["player2"]
    new_state=make_game(gtype,old_id,p1["id"],p1["name"])
    new_state["player2"]=p2; new_state["status"]="playing"
    games[old_id]=new_state; rematch_requests.pop(old_id,None)
    await broadcast(old_id,{"type":"rematch_start","game":new_state})

async def finish_dice(game_id,game):
    if game["round"]==1:
        game["round"]=2; game["guesser_role"]="p2"; game["secret"]=None
        game["attempts_left"]=3; game["round_log"]=[]; game["phase"]="guessing"
        await broadcast(game_id,{"type":"game_state","game":game})
    else:
        s1,s2=game["scores"]["p1"],game["scores"]["p2"]
        p1=game["player1"]["id"]; p2=game["player2"]["id"] if game["player2"] else None
        if s1>s2: game["winner"]=p1
        elif s2>s1: game["winner"]=p2
        else: game["winner"]="draw"
        game["status"]="finished"
        if game["winner"]!="draw" and game["winner"]:
            try: record_win(int(game["winner"]))
            except: pass
            opp=p2 if game["winner"]==p1 else p1
            if opp:
                try: record_game(int(opp))
                except: pass
        await broadcast(game_id,{"type":"game_state","game":game})

# ── ШАШКИ ────────────────────────────────────────────────────
def check_ttt(b):
    for a,bb,c in[(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]:
        if b[a]!=" " and b[a]==b[bb]==b[c]: return b[a]
    return "draw" if " " not in b else None

def get_all_caps(board,color):
    r=[]
    for i in range(64):
        p=board[i]
        if p and p["color"]==color: r.extend(piece_caps(board,i,p))
    return r

def piece_caps(board,pos,piece):
    r=[]; row,col=pos//8,pos%8
    if piece["king"]:
        for dr,dc in[(-1,-1),(-1,1),(1,-1),(1,1)]:
            rr,cc=row+dr,col+dc
            while 0<=rr<8 and 0<=cc<8:
                mid=rr*8+cc
                if board[mid] and board[mid]["color"]!=piece["color"]:
                    r2,c2=rr+dr,cc+dc
                    while 0<=r2<8 and 0<=c2<8:
                        land=r2*8+c2
                        if not board[land]: r.append((pos,land,mid))
                        else: break
                        r2+=dr; c2+=dc
                    break
                elif board[mid]: break
                rr+=dr; cc+=dc
    else:
        for dr,dc in[(-1,-1),(-1,1),(1,-1),(1,1)]:
            mr,mc=row+dr,col+dc; tr,tc=row+2*dr,col+2*dc
            if 0<=mr<8 and 0<=mc<8 and 0<=tr<8 and 0<=tc<8:
                mid=mr*8+mc; land=tr*8+tc
                if board[mid] and board[mid]["color"]!=piece["color"] and not board[land]:
                    r.append((pos,land,mid))
    return r

def get_moves(board,pos,piece):
    r=[]; row,col=pos//8,pos%8
    if piece["king"]:
        for dr,dc in[(-1,-1),(-1,1),(1,-1),(1,1)]:
            rr,cc=row+dr,col+dc
            while 0<=rr<8 and 0<=cc<8:
                if not board[rr*8+cc]: r.append(rr*8+cc)
                else: break
                rr+=dr; cc+=dc
    else:
        dirs=[(-1,-1),(-1,1)] if piece["color"]=="red" else[(1,-1),(1,1)]
        for dr,dc in dirs:
            nr,nc=row+dr,col+dc
            if 0<=nr<8 and 0<=nc<8 and not board[nr*8+nc]: r.append(nr*8+nc)
    return r

def apply_checkers(game,cell):
    board=game["board"]; cur=game["current_color"]; sel=game["selected"]
    if sel is None: return False
    sp=board[sel]
    if not sp: return False
    all_caps=get_all_caps(board,cur); must=len(all_caps)>0
    pc=[(f,t,c) for f,t,c in all_caps if f==sel]
    ct={t:c for _,t,c in pc}
    if cell in ct:
        mid=ct[cell]; board[cell]=board[sel]; board[sel]=None; board[mid]=None
        if(board[cell]["color"]=="red" and cell//8==0) or(board[cell]["color"]=="white" and cell//8==7):
            board[cell]["king"]=True
        game["selected"]=None
        nc=piece_caps(board,cell,board[cell])
        if nc: game["chain_piece"]=cell; game["selected"]=cell
        else: game["chain_piece"]=None; game["current_color"]="white" if cur=="red" else "red"
        chk_win(game); return True
    elif not must:
        moves=get_moves(board,sel,sp)
        if cell in moves:
            board[cell]=board[sel]; board[sel]=None
            if(board[cell]["color"]=="red" and cell//8==0) or(board[cell]["color"]=="white" and cell//8==7):
                board[cell]["king"]=True
            game["selected"]=None; game["chain_piece"]=None
            game["current_color"]="white" if cur=="red" else "red"
            chk_win(game); return True
    return False

def chk_win(game):
    board=game["board"]
    r=sum(1 for p in board if p and p["color"]=="red")
    w=sum(1 for p in board if p and p["color"]=="white")
    if r==0:
        game["status"]="finished"
        game["winner"]=game["player2"]["id"] if game["player2"] else "white"
        if game["player2"]:
            try: record_win(int(game["player2"]["id"]))
            except: pass
            try: record_game(int(game["player1"]["id"]))
            except: pass
    elif w==0:
        game["status"]="finished"; game["winner"]=game["player1"]["id"]
        try: record_win(int(game["player1"]["id"]))
        except: pass
        if game["player2"]:
            try: record_game(int(game["player2"]["id"]))
            except: pass

# ── HTTP API ──────────────────────────────────────────────────
@app.get("/")
def root(): return {"status":"GameZone running"}

@app.get("/health")
def health(): return {"ok":True,"games":len(games),"online":len(user_connections)}

@app.get("/users")
def api_users(): return get_registered_users()

@app.get("/user/{user_id}")
def api_user(user_id:int): return get_user(user_id) or {}

@app.get("/friends/{user_id}")
def api_friends(user_id:int): return get_friends(user_id)

@app.post("/set_nickname")
async def api_set_nickname(data:dict):
    uid=data.get("user_id"); nick=data.get("nickname","").strip()
    if not uid or not nick: return {"ok":False,"error":"Нет данных"}
    if len(nick)<2: return {"ok":False,"error":"Никнейм слишком короткий (мин. 2 символа)"}
    if len(nick)>20: return {"ok":False,"error":"Никнейм слишком длинный (макс. 20 символов)"}
    if nickname_taken(nick, uid): return {"ok":False,"error":"Этот никнейм уже занят"}
    set_nickname(uid, nick)
    return {"ok":True,"nickname":nick}

@app.post("/friend_request")
async def api_friend_request(data:dict):
    from_id=data.get("from_id"); to_id=data.get("to_id"); from_nick=data.get("from_nick","Игрок")
    if not from_id or not to_id: return {"ok":False,"error":"missing fields"}
    if are_friends(from_id, to_id): return {"ok":False,"error":"already_friends"}
    rid=create_friend_request(from_id, from_nick, to_id)
    notif_data={"request_id":rid,"from_id":from_id,"from_nick":from_nick}
    add_notification(to_id,"friend_request",notif_data)
    # Отправляем в апку если онлайн
    await send_notification(to_id,{"type":"friend_request","request_id":rid,"from_id":from_id,"from_nick":from_nick})
    # Отправляем через бота если офлайн
    if str(to_id) not in user_connections:
        try:
            markup=InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("👀 Открыть GameZone",web_app=telebot.types.WebAppInfo(url=MINI_APP_URL)))
            bot.send_message(to_id,f"👋 *{from_nick}* хочет добавить тебя в друзья в GameZone!\n\nОткрой приложение чтобы принять:",parse_mode="Markdown",reply_markup=markup)
        except: pass
    return {"ok":True,"request_id":rid}

@app.post("/friend_request/accept")
async def api_accept_request(data:dict):
    rid=data.get("request_id"); uid=data.get("user_id")
    if not rid or not uid: return {"ok":False}
    from_id=accept_request(rid,uid)
    if from_id:
        # Уведомляем отправителя
        user=get_user(uid)
        nick=user["nickname"] if user else "Игрок"
        await send_notification(from_id,{"type":"friend_accepted","from_id":uid,"from_nick":nick})
        return {"ok":True,"friend_id":from_id}
    return {"ok":False,"error":"Request not found"}

@app.post("/friend_request/decline")
async def api_decline_request(data:dict):
    rid=data.get("request_id"); uid=data.get("user_id")
    if rid and uid: decline_request(rid,uid)
    return {"ok":True}

@app.get("/notifications/{user_id}")
def api_notifications(user_id:int): return get_notifications(user_id)

@app.post("/challenge")
async def api_challenge(data:dict):
    from_id=data.get("from_id"); to_id=data.get("to_id")
    gtype=data.get("game_type","ttt"); from_nick=data.get("from_nick","Игрок")
    game_id=data.get("game_id") or gtype+"_"+_uuid.uuid4().hex[:8]
    if not from_id or not to_id: return {"ok":False,"error":"missing fields"}
    gnames={"ttt":"Крестики-нолики","rps":"Камень-ножницы-бумага","checkers":"Шашки","dice":"Кости"}
    gname=gnames.get(gtype,gtype)
    notif_data={"game_id":game_id,"game_type":gtype,"game_name":gname,"from_id":from_id,"from_nick":from_nick}
    add_notification(to_id,"challenge",notif_data)
    # Отправляем в апку если онлайн
    await send_notification(to_id,{"type":"challenge","game_id":game_id,"game_type":gtype,"game_name":gname,"from_id":from_id,"from_nick":from_nick})
    # Бот — всегда
    try:
        markup=InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⚔️ Принять вызов!",web_app=telebot.types.WebAppInfo(url=f"{MINI_APP_URL}?join={game_id}")))
        bot.send_message(to_id,f"⚔️ *{from_nick}* вызывает тебя на *{gname}*!\n\nНажми чтобы принять:",parse_mode="Markdown",reply_markup=markup)
    except: pass
    return {"ok":True,"game_id":game_id}

@app.post("/add_friend")
async def api_add_friend(data:dict):
    uid=data.get("user_id"); fid=data.get("friend_id")
    if uid and fid: add_friend_db(int(uid),int(fid)); return {"ok":True}
    return {"ok":False}

# ── БОТ ─────────────────────────────────────────────────────
def main_markup():
    m=InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("🎮 Открыть GameZone",web_app=telebot.types.WebAppInfo(url=MINI_APP_URL)))
    return m

@bot.message_handler(commands=["start"])
def cmd_start(message):
    upsert_user(message.from_user)
    args=message.text.split()
    if len(args)>1:
        m=InlineKeyboardMarkup()
        m.add(InlineKeyboardButton("🎮 Принять вызов!",web_app=telebot.types.WebAppInfo(url=f"{MINI_APP_URL}?join={args[1]}")))
        bot.send_message(message.chat.id,"⚔️ Тебя вызывают на игру!",reply_markup=m)
        return
    bot.send_message(message.chat.id,
        f"👾 Привет, *{message.from_user.first_name}*!\n\nДобро пожаловать в *GameZone*!\n\nВыбери никнейм и играй с друзьями:",
        parse_mode="Markdown",reply_markup=main_markup())

@bot.message_handler(func=lambda m:True)
def handle_any(message):
    upsert_user(message.from_user)
    bot.send_message(message.chat.id,"Открой GameZone 👇",reply_markup=main_markup())

def run_bot():
    print("Bot started")
    bot.infinity_polling(timeout=10,long_polling_timeout=5)

@app.on_event("startup")
async def startup():
    threading.Thread(target=run_bot,daemon=True).start()
    print("Server started")

if __name__=="__main__":
    import uvicorn
    port=int(os.environ.get("PORT",8080))
    uvicorn.run(app,host="0.0.0.0",port=port)

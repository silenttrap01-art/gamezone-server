"""
GameZone — FastAPI + Telegram Bot
Реванш работает внутри Mini App через WebSocket без бота.
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

# ── БД ────────────────────────────────────────────────────────
DB = "gamezone.db"

def get_db():
    c = sqlite3.connect(DB, check_same_thread=False)
    return c

def init_db():
    c = get_db()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
        first_name TEXT DEFAULT 'Игрок',
        games_played INTEGER DEFAULT 0, games_won INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS friends (
        user_id INTEGER, friend_id INTEGER, PRIMARY KEY(user_id,friend_id))""")
    c.commit(); c.close()

init_db()

def upsert_user(user):
    c = get_db()
    c.execute("INSERT OR REPLACE INTO users(user_id,username,first_name) VALUES(?,?,?)",
              (user.id, user.username or "", user.first_name or "Игрок"))
    c.commit(); c.close()

def get_users():
    c = get_db()
    rows = c.execute("SELECT user_id,first_name,username,games_won,games_played FROM users").fetchall()
    c.close()
    return [{"id":r[0],"name":r[1],"username":r[2],"wins":r[3],"games":r[4]} for r in rows]

def get_friends(uid):
    c = get_db()
    rows = c.execute("""SELECT u.user_id,u.first_name,u.username,u.games_won,u.games_played
        FROM friends f JOIN users u ON f.friend_id=u.user_id WHERE f.user_id=?""", (uid,)).fetchall()
    c.close()
    return [{"id":r[0],"name":r[1],"username":r[2],"wins":r[3],"games":r[4]} for r in rows]

def add_friend(uid, fid):
    c = get_db()
    c.execute("INSERT OR IGNORE INTO friends(user_id,friend_id) VALUES(?,?)", (uid,fid))
    c.execute("INSERT OR IGNORE INTO friends(user_id,friend_id) VALUES(?,?)", (fid,uid))
    c.commit(); c.close()

def record_win(uid):
    c = get_db()
    c.execute("UPDATE users SET games_played=games_played+1,games_won=games_won+1 WHERE user_id=?", (uid,))
    c.commit(); c.close()

def record_game(uid):
    c = get_db()
    c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id=?", (uid,))
    c.commit(); c.close()

# ── ХРАНИЛИЩЕ ─────────────────────────────────────────────────
games = {}        # game_id → game state
connections = {}  # game_id → {user_id: websocket}
# Для реванша: отслеживаем кто хочет реванш
rematch_requests = {}  # game_id → set of user_ids

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

# ── WEBSOCKET ─────────────────────────────────────────────────
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
    if gtype=="ttt":
        base.update({"board":[" "]*9,"current":uid,"winner":None})
    elif gtype=="rps":
        base.update({"choices":{},"ready":{},"result":None})
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
        cell = msg["cell"]
        if game["board"][cell]!=" ": return
        sym = "X" if user_id==game["player1"]["id"] else "O"
        game["board"][cell] = sym
        w = check_ttt(game["board"])
        if w:
            game["status"]="finished"; game["winner"]="draw" if w=="draw" else user_id
            if w!="draw":
                try: record_win(int(user_id))
                except: pass
                opp = game["player2"]["id"] if user_id==game["player1"]["id"] else game["player1"]["id"]
                try: record_game(int(opp))
                except: pass
        else:
            p1,p2 = game["player1"]["id"],game["player2"]["id"] if game["player2"] else None
            game["current"] = p2 if user_id==p1 else p1
        await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "rps_choose":
        if game["status"]!="playing": return
        game["choices"][user_id] = msg["choice"]
        await broadcast(game_id, {"type":"rps_chose","user_id":user_id})

    elif t == "rps_ready":
        if game["status"]!="playing": return
        if not game["choices"].get(user_id):
            await send_to(game_id,user_id,{"type":"error","message":"Выбери оружие!"}); return
        game["ready"][user_id] = True
        p1 = game["player1"]["id"]
        p2 = game["player2"]["id"] if game["player2"] else None
        if game["ready"].get(p1) and game["ready"].get(p2):
            c1,c2 = game["choices"].get(p1),game["choices"].get(p2)
            beats = {"rock":"scissors","scissors":"paper","paper":"rock"}
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
        cur = game["current_color"]
        myc = "red" if user_id==game["player1"]["id"] else "white"
        if myc!=cur: return
        action,cell = msg.get("action"),msg.get("cell")
        if action=="select":
            p=game["board"][cell]
            if p and p["color"]==cur:
                game["selected"]=cell
                await broadcast(game_id,{"type":"game_state","game":game})
        elif action=="move":
            if apply_checkers(game,cell):
                await broadcast(game_id,{"type":"game_state","game":game})

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
        if total==game["secret"]:
            game["scores"][gr]+=1; await finish_dice(game_id,game)
        elif game["attempts_left"]==0:
            game["scores"][tr]+=1; await finish_dice(game_id,game)
        else:
            await broadcast(game_id,{"type":"game_state","game":game})

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

    # ── РЕВАНШ ВНУТРИ АПКИ ────────────────────────────────────
    elif t == "rematch_request":
        # Игрок хочет реванш — сообщаем сопернику
        game = games.get(game_id)
        if not game or game["status"] != "finished": return
        rematch_requests.setdefault(game_id, set()).add(user_id)
        # Сообщаем сопернику что этот игрок хочет реванш
        await broadcast(game_id, {
            "type": "rematch_requested",
            "from_id": user_id,
            "from_name": username
        })
        # Если оба хотят реванш — создаём новую игру
        reqs = rematch_requests.get(game_id, set())
        p1id = game["player1"]["id"]
        p2id = game["player2"]["id"] if game["player2"] else None
        if p1id in reqs and p2id in reqs:
            await start_rematch(game_id, game)

    elif t == "rematch_accept":
        # Принять реванш = тоже хочу реванш
        game = games.get(game_id)
        if not game or game["status"] != "finished": return
        rematch_requests.setdefault(game_id, set()).add(user_id)
        reqs = rematch_requests.get(game_id, set())
        p1id = game["player1"]["id"]
        p2id = game["player2"]["id"] if game["player2"] else None
        if p1id in reqs and p2id in reqs:
            await start_rematch(game_id, game)
        else:
            await broadcast(game_id, {
                "type": "rematch_requested",
                "from_id": user_id,
                "from_name": username
            })

async def start_rematch(old_game_id, old_game):
    """Сбрасываем игру до начального состояния — оба игрока остаются подключены."""
    gtype = old_game["type"]
    p1 = old_game["player1"]
    p2 = old_game["player2"]

    # Сбрасываем состояние той же игры
    new_state = make_game(gtype, old_game_id, p1["id"], p1["name"])
    new_state["player2"] = p2
    new_state["status"] = "playing"

    # Перезаписываем игру
    games[old_game_id] = new_state
    rematch_requests.pop(old_game_id, None)

    # Сообщаем обоим — игра началась заново
    await broadcast(old_game_id, {"type": "rematch_start", "game": new_state})

async def finish_dice(game_id, game):
    if game["round"]==1:
        game["round"]=2; game["guesser_role"]="p2"
        game["secret"]=None; game["attempts_left"]=3
        game["round_log"]=[]; game["phase"]="guessing"
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

# ── ШАШКИ ЛОГИКА ─────────────────────────────────────────────
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

# ── HTTP ─────────────────────────────────────────────────────
@app.get("/")
def root(): return {"status":"GameZone running"}

@app.get("/health")
def health(): return {"ok":True,"games":len(games)}

@app.get("/users")
def api_users(): return get_users()

@app.get("/friends/{user_id}")
def api_friends(user_id:int): return get_friends(user_id)

@app.post("/add_friend")
async def api_add_friend(data:dict):
    uid=data.get("user_id"); fid=data.get("friend_id")
    if uid and fid: add_friend(int(uid),int(fid)); return {"ok":True}
    return {"ok":False}

@app.post("/challenge")
async def api_challenge(data:dict):
    from_id=data.get("from_id"); to_id=data.get("to_id")
    gtype=data.get("game_type","ttt"); from_name=data.get("from_name","Игрок")
    game_id=data.get("game_id") or gtype+"_"+_uuid.uuid4().hex[:8]
    if not from_id or not to_id: return {"ok":False,"error":"missing fields"}
    gnames={"ttt":"Крестики-нолики","rps":"Камень-ножницы-бумага","checkers":"Шашки","dice":"Кости"}
    try:
        markup=InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎮 Принять вызов!",
            web_app=telebot.types.WebAppInfo(url=f"{MINI_APP_URL}?join={game_id}")))
        bot.send_message(to_id,
            f"⚔️ *{from_name}* вызывает тебя на *{gnames.get(gtype,gtype)}*!\n\nНажми чтобы принять:",
            parse_mode="Markdown",reply_markup=markup)
        return {"ok":True,"game_id":game_id}
    except Exception as e:
        return {"ok":False,"error":str(e)}

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
        f"👾 Привет, *{message.from_user.first_name}*! Добро пожаловать в *GameZone*!",
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

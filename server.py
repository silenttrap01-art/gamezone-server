import os
import json
import random
import uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

games = {}
connections = {}

async def broadcast(game_id, message):
    if game_id not in connections: return
    dead = []
    for uid, ws in connections[game_id].items():
        try: await ws.send_text(json.dumps(message))
        except: dead.append(uid)
    for uid in dead: del connections[game_id][uid]

async def send_to(game_id, user_id, message):
    ws = connections.get(game_id, {}).get(user_id)
    if ws:
        try: await ws.send_text(json.dumps(message))
        except: pass

@app.websocket("/ws/{game_id}/{user_id}/{username}")
async def websocket_endpoint(ws: WebSocket, game_id: str, user_id: str, username: str):
    await ws.accept()
    if game_id not in connections: connections[game_id] = {}
    connections[game_id][user_id] = ws

    if game_id in games:
        game = games[game_id]
        if game["player2"] is None and game["player1"]["id"] != user_id:
            game["player2"] = {"id": user_id, "name": username}
            game["status"] = "playing"
            await broadcast(game_id, {"type": "game_state", "game": game})
        else:
            await ws.send_text(json.dumps({"type": "game_state", "game": game}))
    else:
        game_type = game_id.split("_")[0]
        game = create_game(game_type, game_id, user_id, username)
        games[game_id] = game
        await ws.send_text(json.dumps({"type": "game_state", "game": game}))

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            await handle_message(game_id, user_id, username, msg)
    except WebSocketDisconnect:
        if game_id in connections and user_id in connections[game_id]:
            del connections[game_id][user_id]
        if game_id in games and games[game_id]["status"] == "playing":
            await broadcast(game_id, {"type": "opponent_left", "message": f"{username} покинул игру"})

def create_game(game_type, game_id, user_id, username):
    base = {
        "id": game_id, "type": game_type,
        "player1": {"id": user_id, "name": username},
        "player2": None, "status": "waiting", "chat": [],
    }
    if game_type == "ttt":
        base["board"] = [" "] * 9
        base["current"] = user_id
        base["winner"] = None
    elif game_type == "rps":
        base["choices"] = {}; base["ready"] = {}; base["result"] = None
    elif game_type == "checkers":
        board = [None] * 64
        for row in range(3):
            for col in range(8):
                if (row+col)%2==1: board[row*8+col] = {"color":"white","king":False}
        for row in range(5,8):
            for col in range(8):
                if (row+col)%2==1: board[row*8+col] = {"color":"red","king":False}
        base["board"]=board; base["current_color"]="red"
        base["selected"]=None; base["chain_piece"]=None; base["winner"]=None
    elif game_type == "dice":
        base["round"]=1; base["scores"]={"p1":0,"p2":0}
        base["guesser_role"]="p1"; base["secret"]=None
        base["attempts_left"]=3; base["round_log"]=[]
        base["phase"]="guessing"; base["winner"]=None
    return base

async def handle_message(game_id, user_id, username, msg):
    game = games.get(game_id)
    if not game: return
    t = msg.get("type")

    if t == "chat":
        # Включаем user_id чтобы клиент не дублировал своё сообщение
        entry = {"from": username, "text": msg["text"], "user_id": user_id}
        game["chat"].append(entry)
        await broadcast(game_id, {"type": "chat", "entry": entry})

    elif t == "ttt_move":
        if game["status"] != "playing": return
        if game["current"] != user_id: return
        cell = msg["cell"]
        if game["board"][cell] != " ": return
        sym = "X" if user_id == game["player1"]["id"] else "O"
        game["board"][cell] = sym
        winner = check_ttt(game["board"])
        if winner:
            game["status"] = "finished"
            game["winner"] = "draw" if winner == "draw" else user_id
        else:
            p1id = game["player1"]["id"]
            p2id = game["player2"]["id"] if game["player2"] else None
            game["current"] = p2id if user_id == p1id else p1id
        await broadcast(game_id, {"type": "game_state", "game": game})

    elif t == "rps_choose":
        if game["status"] != "playing": return
        game["choices"][user_id] = msg["choice"]
        await broadcast(game_id, {"type": "rps_chose", "user_id": user_id})

    elif t == "rps_ready":
        if game["status"] != "playing": return
        if not game["choices"].get(user_id):
            await send_to(game_id, user_id, {"type":"error","message":"Сначала выбери оружие!"}); return
        game["ready"][user_id] = True
        p1id = game["player1"]["id"]
        p2id = game["player2"]["id"] if game["player2"] else None
        if game["ready"].get(p1id) and game["ready"].get(p2id):
            c1 = game["choices"].get(p1id)
            c2 = game["choices"].get(p2id)
            beats = {"rock":"scissors","scissors":"paper","paper":"rock"}
            if c1==c2: winner_id="draw"
            elif beats[c1]==c2: winner_id=p1id
            else: winner_id=p2id
            game["result"]={"c1":c1,"c2":c2,"winner":winner_id}
            game["status"]="finished"; game["winner"]=winner_id
            await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "checkers_move":
        if game["status"] != "playing": return
        cur_color = game["current_color"]
        p1id = game["player1"]["id"]
        my_color = "red" if user_id==p1id else "white"
        if my_color != cur_color: return
        action = msg.get("action"); cell = msg.get("cell")
        if action == "select":
            piece = game["board"][cell]
            if piece and piece["color"]==cur_color:
                game["selected"] = cell
                await broadcast(game_id, {"type":"game_state","game":game})
        elif action == "move":
            result = apply_checkers_move(game, cell)
            if result:
                await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "dice_guess":
        if game["status"] != "playing": return
        p1id = game["player1"]["id"]
        guesser_id = p1id if game["guesser_role"]=="p1" else game["player2"]["id"]
        if user_id != guesser_id: return
        game["secret"] = msg["number"]
        game["phase"] = "throwing"
        await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "dice_throw":
        if game["status"] != "playing": return
        p1id = game["player1"]["id"]
        p2id = game["player2"]["id"] if game["player2"] else None
        thrower_id = p2id if game["guesser_role"]=="p1" else p1id
        if user_id != thrower_id: return
        d1=random.randint(1,6); d2=random.randint(1,6); total=d1+d2
        game["round_log"].append({"d1":d1,"d2":d2,"total":total})
        game["attempts_left"] -= 1
        gr = game["guesser_role"]
        tr = "p2" if gr=="p1" else "p1"
        if total == game["secret"]:
            game["scores"][gr] += 1
            await finish_dice_round(game_id, game, "guesser_wins")
        elif game["attempts_left"] == 0:
            game["scores"][tr] += 1
            await finish_dice_round(game_id, game, "thrower_wins")
        else:
            await broadcast(game_id, {"type":"game_state","game":game})

    elif t == "surrender":
        p1id = game["player1"]["id"]
        p2id = game["player2"]["id"] if game["player2"] else None
        winner_id = p2id if user_id==p1id else p1id
        game["status"]="finished"; game["winner"]=winner_id; game["surrender"]=username
        await broadcast(game_id, {"type":"game_state","game":game})

async def finish_dice_round(game_id, game, result):
    if game["round"] == 1:
        game["round"]=2; game["guesser_role"]="p2"
        game["secret"]=None; game["attempts_left"]=3
        game["round_log"]=[]; game["phase"]="guessing"
        game["last_round_result"]=result
        await broadcast(game_id, {"type":"game_state","game":game})
    else:
        s1=game["scores"]["p1"]; s2=game["scores"]["p2"]
        p1id=game["player1"]["id"]
        p2id=game["player2"]["id"] if game["player2"] else None
        if s1>s2: game["winner"]=p1id
        elif s2>s1: game["winner"]=p2id
        else: game["winner"]="draw"
        game["status"]="finished"
        await broadcast(game_id, {"type":"game_state","game":game})

# ── ШАШКИ ─────────────────────────────────────────
def get_caps(board, color):
    r=[]
    for i in range(64):
        p=board[i]
        if p and p["color"]==color: r.extend(piece_caps(board,i,p))
    return r

def piece_caps(board, pos, piece):
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
                        r2+=dr;c2+=dc
                    break
                elif board[mid]: break
                rr+=dr;cc+=dc
    else:
        for dr,dc in[(-1,-1),(-1,1),(1,-1),(1,1)]:
            mr,mc=row+dr,col+dc;tr2,tc=row+2*dr,col+2*dc
            if 0<=mr<8 and 0<=mc<8 and 0<=tr2<8 and 0<=tc<8:
                mid=mr*8+mc;land=tr2*8+tc
                if board[mid] and board[mid]["color"]!=piece["color"] and not board[land]:
                    r.append((pos,land,mid))
    return r

def get_moves_s(board, pos, piece):
    r=[]; row,col=pos//8,pos%8
    if piece["king"]:
        for dr,dc in[(-1,-1),(-1,1),(1,-1),(1,1)]:
            rr,cc=row+dr,col+dc
            while 0<=rr<8 and 0<=cc<8:
                if not board[rr*8+cc]: r.append(rr*8+cc)
                else: break
                rr+=dr;cc+=dc
    else:
        dirs=[(-1,-1),(-1,1)] if piece["color"]=="red" else[(1,-1),(1,1)]
        for dr,dc in dirs:
            nr,nc=row+dr,col+dc
            if 0<=nr<8 and 0<=nc<8 and not board[nr*8+nc]: r.append(nr*8+nc)
    return r

def check_ttt(board):
    for a,b,c in[(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]:
        if board[a]!=" " and board[a]==board[b]==board[c]: return board[a]
    return "draw" if " " not in board else None

def apply_checkers_move(game, cell):
    board=game["board"]; cur=game["current_color"]; sel=game["selected"]
    if sel is None: return False
    sp=board[sel]
    if not sp: return False
    all_caps=get_caps(board,cur); must_cap=len(all_caps)>0
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
        chk_winner(game); return True
    elif not must_cap:
        moves=get_moves_s(board,sel,sp)
        if cell in moves:
            board[cell]=board[sel]; board[sel]=None
            if(board[cell]["color"]=="red" and cell//8==0) or(board[cell]["color"]=="white" and cell//8==7):
                board[cell]["king"]=True
            game["selected"]=None; game["chain_piece"]=None
            game["current_color"]="white" if cur=="red" else "red"
            chk_winner(game); return True
    return False

def chk_winner(game):
    board=game["board"]
    r=sum(1 for p in board if p and p["color"]=="red")
    w=sum(1 for p in board if p and p["color"]=="white")
    if r==0: game["status"]="finished"; game["winner"]=game["player2"]["id"] if game["player2"] else "white"
    elif w==0: game["status"]="finished"; game["winner"]=game["player1"]["id"]

@app.get("/")
def root(): return {"status":"GameZone Server running"}

@app.get("/health")
def health(): return {"ok":True,"games":len(games)}

if __name__=="__main__":
    import uvicorn
    port=int(os.environ.get("PORT",8080))
    uvicorn.run(app,host="0.0.0.0",port=port)

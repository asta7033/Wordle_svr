import os
import random
import threading
import time
import uuid
from collections import Counter
from typing import Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Wordle API Server")

# 目前 client 是桌面程式，不受瀏覽器 CORS 限制。若日後改成網頁，
# 再把 allow_origins 限定為實際前端網址。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

WORD_LENGTH = 5
MAX_ATTEMPTS = 6
# 閒置超過此秒數的遊戲會被清除，避免記憶體無限成長。
GAME_TTL_SECONDS = int(os.environ.get("GAME_TTL_SECONDS", "3600"))
# 同時保留的遊戲數上限，防止大量建立新局把記憶體塞爆。
MAX_GAMES = int(os.environ.get("MAX_GAMES", "10000"))

# 簡易單字庫
WORD_BANK = ["APPLE", "TRAIN", "HOUSE", "PLANT", "SMART", "WORLD", "LIGHT", "BRAIN", "CLOUD", "WATER"]

# 記憶體儲存遊戲狀態：
# {game_id: {"target": "APPLE", "status": "PLAYING", "attempts": 0, "last_active": 0.0}}
games_db: Dict[str, dict] = {}
games_lock = threading.Lock()  # sync endpoint 會在 threadpool 中並行執行


class GuessRequest(BaseModel):
    game_id: str
    guess: str


def score_guess(guess: str, target: str) -> List[str]:
    """ 比對猜測與答案，回傳每個位置的 correct / present / absent（正確處理重複字母） """
    feedback = ["absent"] * len(target)
    remaining = Counter(target)

    # 1. 先標記綠色 correct，並扣掉已配對的字母
    for i, (g, t) in enumerate(zip(guess, target)):
        if g == t:
            feedback[i] = "correct"
            remaining[g] -= 1

    # 2. 再標記黃色 present
    for i, g in enumerate(guess):
        if feedback[i] != "correct" and remaining[g] > 0:
            feedback[i] = "present"
            remaining[g] -= 1

    return feedback


def _purge_expired(now: float) -> None:
    """ 清除閒置過久的遊戲（呼叫端需持有 games_lock） """
    expired = [gid for gid, g in games_db.items() if now - g["last_active"] > GAME_TTL_SECONDS]
    for gid in expired:
        del games_db[gid]


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/api/new-game")
def start_new_game():
    """ 建立新一局遊戲，回傳 game_id """
    now = time.time()
    game_id = str(uuid.uuid4())
    with games_lock:
        _purge_expired(now)
        if len(games_db) >= MAX_GAMES:
            # 仍超過上限時，移除最久沒動作的遊戲
            oldest = min(games_db, key=lambda gid: games_db[gid]["last_active"])
            del games_db[oldest]
        games_db[game_id] = {
            "target": random.choice(WORD_BANK),
            "status": "PLAYING",
            "attempts": 0,
            "last_active": now,
        }
    return {
        "game_id": game_id,
        "message": "New game started!",
        "word_length": WORD_LENGTH,
        "max_attempts": MAX_ATTEMPTS,
    }


@app.post("/api/guess")
def process_guess(req: GuessRequest):
    """ 接收玩家猜測，比對答案並回傳顏色結果 """
    guess = req.guess.strip().upper()

    if len(guess) != WORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Word must be {WORD_LENGTH} letters.")
    if not (guess.isascii() and guess.isalpha()):
        raise HTTPException(status_code=400, detail="Word must contain only letters A-Z.")

    with games_lock:
        game = games_db.get(req.game_id)
        if game is None:
            raise HTTPException(status_code=404, detail="Game session not found.")
        if game["status"] != "PLAYING":
            raise HTTPException(status_code=409, detail="Game is already over. Start a new game.")

        target = game["target"]
        game["attempts"] += 1
        game["last_active"] = time.time()

        is_correct = guess == target
        if is_correct:
            game["status"] = "WON"
        elif game["attempts"] >= MAX_ATTEMPTS:
            game["status"] = "LOST"

        status = game["status"]
        attempts_left = MAX_ATTEMPTS - game["attempts"]

    return {
        "guess": guess,
        "feedback": score_guess(guess, target),  # 例如: ["correct", "present", "absent", "absent", "correct"]
        "is_correct": is_correct,
        "status": status,
        "attempts_left": attempts_left,
        # 遊戲結束（猜對或用完次數）才揭曉答案
        "target_word": target if status != "PLAYING" else None,
    }


if __name__ == "__main__":
    import uvicorn
    # Render 會透過 PORT 環境變數指定服務埠；本機則預設使用 8000。
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)

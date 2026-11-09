import os
import queue
import sys
import threading

import pygame
import requests

# 桌面 client 在本機執行，透過這個網址呼叫雲端 FastAPI server。
# 可用環境變數 WORDLE_API_URL 覆寫，例如本機測試：WORDLE_API_URL=http://127.0.0.1:8000
API_BASE_URL = os.environ.get("WORDLE_API_URL", "https://wordle-svr-lkbo.onrender.com")
# Render 免費服務休眠後，第一次請求可能需要較久才會喚醒。
REQUEST_TIMEOUT = 60

ROWS, COLS = 6, 5

# 顏色定義
COLOR_BG = (18, 18, 19)
COLOR_TEXT = (255, 255, 255)
COLOR_EMPTY = (58, 58, 60)
COLOR_BORDER = (80, 80, 80)
COLOR_CORRECT = (83, 141, 78)   # 綠色
COLOR_PRESENT = (181, 159, 59)  # 黃色
COLOR_ABSENT = (58, 58, 60)     # 灰色
FEEDBACK_COLORS = {"correct": COLOR_CORRECT, "present": COLOR_PRESENT, "absent": COLOR_ABSENT}

pygame.init()
WIDTH, HEIGHT = 500, 650
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Pygame Wordle + FastAPI Client")
font = pygame.font.SysFont("Arial", 36, bold=True)
small_font = pygame.font.SysFont("Arial", 18)

# 預先渲染 A-Z 字母，避免每一幀重複呼叫 font.render
LETTER_SURFACES = {ch: font.render(ch, True, COLOR_TEXT) for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}

# 重用 TCP/TLS 連線，減少每次請求的握手延遲
session = requests.Session()


def error_detail(res, default):
    """ 安全地取出伺服器錯誤訊息（回應可能不是 JSON，detail 也可能是 list） """
    try:
        detail = res.json().get("detail", default)
    except ValueError:
        return f"{default} (HTTP {res.status_code})"
    return detail if isinstance(detail, str) else default


class WordleGame:
    def __init__(self):
        self.results = queue.Queue()  # 背景執行緒 -> 主迴圈 的網路結果
        self.busy = False
        self.reset_board()
        self.start_new_game()

    def reset_board(self):
        self.game_id = None
        self.grid = [["" for _ in range(COLS)] for _ in range(ROWS)]
        self.colors = [[COLOR_EMPTY for _ in range(COLS)] for _ in range(ROWS)]
        self.row = 0
        self.col = 0
        self.game_over = False

    # ---------- 網路請求（在背景執行緒執行，避免畫面凍結） ----------

    def _run_async(self, kind, func):
        self.busy = True

        def worker():
            try:
                result = func()
            except requests.exceptions.RequestException as exc:
                result = exc
            self.results.put((kind, result))

        threading.Thread(target=worker, daemon=True).start()

    def start_new_game(self):
        """ 向 FastAPI 伺服器請求建立新的一局 """
        if self.busy:
            return
        self.message = "Connecting to server..."
        self._run_async("new_game", lambda: session.post(
            f"{API_BASE_URL}/api/new-game", timeout=REQUEST_TIMEOUT))

    def submit_guess(self):
        """ 將當前行單字發送至 FastAPI 驗證 """
        if self.col < COLS:
            self.message = "Not enough letters!"
            return
        if self.game_id is None:
            self.message = "No active game. Press ESC to retry."
            return

        payload = {"game_id": self.game_id, "guess": "".join(self.grid[self.row])}
        self.message = "Checking..."
        self._run_async("guess", lambda: session.post(
            f"{API_BASE_URL}/api/guess", json=payload, timeout=REQUEST_TIMEOUT))

    def process_results(self):
        """ 在主迴圈中套用背景請求的結果（pygame 狀態只在主執行緒修改） """
        while not self.results.empty():
            kind, res = self.results.get_nowait()
            self.busy = False
            if kind == "new_game":
                self._on_new_game(res)
            else:
                self._on_guess(res)

    def _on_new_game(self, res):
        if isinstance(res, Exception):
            self.message = "Cannot connect to server! Press ESC to retry."
        elif res.status_code == 200:
            self.reset_board()
            self.game_id = res.json()["game_id"]
            self.message = "New game started!"
        else:
            self.message = error_detail(res, "Server error starting new game.")

    def _on_guess(self, res):
        if isinstance(res, Exception):
            self.message = "Network error during guess!"
            return
        if res.status_code != 200:
            self.message = error_detail(res, "Error processing guess")
            return

        data = res.json()
        for i, fb in enumerate(data["feedback"]):
            self.colors[self.row][i] = FEEDBACK_COLORS.get(fb, COLOR_ABSENT)

        if data["is_correct"]:
            self.message = "Congratulations! You won! Press ENTER to play again."
            self.game_over = True
            return

        self.row += 1
        self.col = 0
        if self.row == ROWS or data.get("status") == "LOST":
            answer = data.get("target_word")
            suffix = f" The word was {answer}." if answer else ""
            self.message = f"Game Over!{suffix} Press ENTER to play again."
            self.game_over = True
        else:
            self.message = "Keep guessing!"

    # ---------- 輸入處理 ----------

    def handle_keydown(self, event):
        if self.busy:
            return  # 等待伺服器回應期間忽略輸入

        # ESC 隨時可重新開局；不再使用 R，否則無法輸入字母 R
        if event.key == pygame.K_ESCAPE:
            self.start_new_game()
            return

        if self.game_over:
            if event.key == pygame.K_RETURN:
                self.start_new_game()
            return

        if event.key == pygame.K_BACKSPACE:
            if self.col > 0:
                self.col -= 1
                self.grid[self.row][self.col] = ""
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.submit_guess()
        else:
            ch = event.unicode.upper()
            if ch in LETTER_SURFACES and self.col < COLS:  # 只接受 A-Z
                self.grid[self.row][self.col] = ch
                self.col += 1

    # ---------- 繪圖 ----------

    def draw(self, surface):
        surface.fill(COLOR_BG)
        # 繪製 6x5 網格
        margin_x, margin_y = 75, 50
        box_size = 60
        gap = 10

        for r in range(ROWS):
            for c in range(COLS):
                x = margin_x + c * (box_size + gap)
                y = margin_y + r * (box_size + gap)
                rect = pygame.Rect(x, y, box_size, box_size)

                # 畫背景框
                pygame.draw.rect(surface, self.colors[r][c], rect, border_radius=4)
                if self.colors[r][c] == COLOR_EMPTY:
                    pygame.draw.rect(surface, COLOR_BORDER, rect, width=2, border_radius=4)

                # 畫字母
                char = self.grid[r][c]
                if char:
                    text_surface = LETTER_SURFACES[char]
                    surface.blit(text_surface, text_surface.get_rect(center=rect.center))

        # 畫底部提示訊息
        msg_surface = small_font.render(self.message, True, COLOR_TEXT)
        msg_rect = msg_surface.get_rect(center=(WIDTH // 2, HEIGHT - 40))
        surface.blit(msg_surface, msg_rect)


def main():
    clock = pygame.time.Clock()
    game = WordleGame()

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            elif event.type == pygame.KEYDOWN:
                game.handle_keydown(event)

        game.process_results()
        game.draw(screen)
        pygame.display.flip()
        clock.tick(30)


if __name__ == "__main__":
    main()

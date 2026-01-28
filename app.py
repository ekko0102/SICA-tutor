from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import os
import openai
import time
import traceback
import requests
import redis

app = Flask(__name__)

# --- 1. 初始化設定 (從環境變數讀取) ---
# Redis 設定
redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL 未在 Render 後台設置")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True)

# LINE 設定
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# OpenAI 設定
openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY 未在 Render 後台設置")

client = openai.OpenAI(api_key=openai_api_key)
# 這裡對應你之前的 ASSISTANT_ID 或 OPENAI_MODEL_ID
ASSISTANT_ID = os.getenv('ASSISTANT_ID') 

# --- 2. 核心功能函數 ---

def GPT_response(user_id, text):
    try:
        # 嘗試從 Redis 中取得 thread_id
        thread_id = redis_db.get(f"thread_id:{user_id}")
        max_wait_time = 15  
        start_time = time.time()

        if not thread_id:
            # 創建新 Thread
            thread = client.beta.threads.create(
                messages=[{"role": "user", "content": text}]
            )
            thread_id = thread.id
            redis_db.set(f"thread_id:{user_id}", thread_id)
        else:
            # 檢查是否有未完成的 Run
            active_runs = client.beta.threads.runs.list(thread_id=thread_id).data
            while active_runs and any(run.status not in ["completed", "failed", "cancelled", "expired"] for run in active_runs):
                if time.time() - start_time > max_wait_time:
                    # 等待過久則重置 Thread
                    thread = client.beta.threads.create(
                        messages=[{"role": "user", "content": text}]
                    )
                    thread_id = thread.id
                    redis_db.set(f"thread_id:{user_id}", thread_id)
                    break
                time.sleep(2)
                active_runs = client.beta.threads.runs.list(thread_id=thread_id).data

            # 正常加入訊息
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text
            )

        # 執行助理
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)

        # 等待完成
        while run.status != "completed":
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status in ["failed", "cancelled", "expired"]:
                raise Exception(f"OpenAI Run 失敗，狀態為: {run.status}")
            time.sleep(1)
        
        # 抓取回覆
        message_response = client.beta.threads.messages.list(thread_id=thread_id)
        ai_reply = message_response.data[0].content[0].text.value
        return ai_reply

    except Exception as e:
        print(f"GPT_response Error: {e}")
        return "系統忙碌中，請稍後再問我一次！"

def send_loading_animation(chat_id):
    """傳送 LINE 的正在輸入動畫"""
    url = 'https://api.line.me/v2/bot/chat/loading/start'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
    }
    data = {"chatId": chat_id, "loadingSeconds": 10}
    requests.post(url, headers=headers, json=data)

# --- 3. LINE Webhook 路由 ---

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200
def stop_loading_animation(chat_id):
    """主動停止 LINE 的載入動畫"""
    url = 'https://api.line.me/v2/bot/chat/loading/stop'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
    }
    data = {"chatId": chat_id}
    requests.post(url, headers=headers, json=data)
@handler.add(MessageEvent, message=TextMessage)
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # 1. 取得訊息識別資訊
    msg_id = event.message.id  # 每個 LINE 訊息都有唯一 ID
    user_msg = event.message.text
    user_id = event.source.user_id

    # 2. 檢查 Redis 防止 LINE 重試（Retry）導致重複執行 (新增)
    # 如果這個 ID 已經在處理中或處理過，就直接跳出，不跑下面的動畫和 GPT
    if redis_db.get(f"proc:{msg_id}"):
        return 
    # 標記此 ID 正在處理，設定 60 秒過期
    redis_db.set(f"proc:{msg_id}", "true", ex=60)

    # 3. 你原本的「群組過濾邏輯」 (保留)
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            return 

    try:
        # 4. 顯示打字中動畫 (保留)
        send_loading_animation(user_id)
        
        # 5. 取得 AI 回覆 (保留)
        answer = GPT_response(user_id, user_msg)
        
        # 6. 停止動畫 (新增：確保在發送訊息前主動關閉)
        stop_loading_animation(user_id)
        
        # 7. 回傳訊息 (保留)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=answer))
        
    except Exception:
        # 8. 錯誤處理 (保留)
        print(traceback.format_exc())
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我的大腦斷線了，請檢查 API Key 或額度。"))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

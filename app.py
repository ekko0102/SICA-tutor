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
                return "抱歉，助理在處理訊息時遇到問題，請稍後再試。"
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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    user_id = event.source.user_id

    # 群組過濾邏輯
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            return 

    try:
        # 顯示打字中動畫
        send_loading_animation(user_id)
        
        # 取得 AI 回覆
        answer = GPT_response(user_id, user_msg)
        
        # 回傳
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=answer))
        
    except Exception:
        print(traceback.format_exc())
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我的大腦斷線了，請檢查 API Key 或額度。"))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

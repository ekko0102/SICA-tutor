from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import os
import openai
import time
import traceback
import requests
import redis
import json
from datetime import datetime
import hashlib
import threading

app = Flask(__name__)

# --- 1. 初始化設定 ---
redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL is not set in Render environment variables")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True)

line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set in Render environment variables")

client = openai.OpenAI(api_key=openai_api_key, timeout=30.0)
ASSISTANT_ID = os.getenv('ASSISTANT_ID') 

# --- 2. 優化設定（針對 gpt-4o-mini） ---
MAX_WAIT_TIME = 20  # 保持合理的等待時間
MAX_THREAD_MESSAGES = 10  # 中等歷史長度

# --- 3. 監控器（只監控不限流） ---
class RequestMonitor:
    def __init__(self):
        self.active = 0
        self.total_processed = 0
        self.lock = threading.Lock()
        self.start_time = time.time()
    
    def request_start(self):
        with self.lock:
            self.active += 1
            self.total_processed += 1
    
    def request_end(self):
        with self.lock:
            self.active -= 1
    
    def get_stats(self):
        with self.lock:
            uptime = time.time() - self.start_time
            return {
                "active_requests": self.active,
                "total_processed": self.total_processed,
                "requests_per_minute": self.total_processed / (uptime / 60) if uptime > 0 else 0,
                "uptime_seconds": uptime
            }

monitor = RequestMonitor()

# --- 4. 資料儲存（保持不變） ---

def generate_anonymous_id(user_id):
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]

def save_message(user_id, role, content):
    try:
        student_id = generate_anonymous_id(user_id)
        timestamp = datetime.now().isoformat()
        
        message_data = {
            "student_id": student_id,
            "role": role,
            "content": content[:3000],
            "timestamp": timestamp,
            "user_id_hash": student_id
        }
        
        redis_db.rpush(f"student_history:{student_id}", json.dumps(message_data))
        
        # 限制歷史長度
        if redis_db.llen(f"student_history:{student_id}") > 100:
            redis_db.ltrim(f"student_history:{student_id}", -100, -1)
        
        redis_db.hset(f"student:{student_id}", "last_active", timestamp)
        
        return True
    except Exception as e:
        print(f"Error saving message: {e}")
        return False

# --- 5. GPT_response（移除限流） ---

def GPT_response(user_id, text):
    # 開始監控
    monitor.request_start()
    
    try:
        # 0. 檢查是否為重複請求（保持防重複）
        request_hash = hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()[:8]
        if redis_db.get(f"req:{request_hash}"):
            return "[System] Processing your previous question..."
        redis_db.setex(f"req:{request_hash}", 30, "processing")
        
        # 1. 儲存使用者訊息
        save_message(user_id, "user", text)
        
        # 2. 檢查並清理過長的對話
        thread_id = redis_db.get(f"thread_id:{user_id}")
        
        if thread_id:
            try:
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    limit=MAX_THREAD_MESSAGES + 3,
                    timeout=3.0
                )
                
                # 如果超過限制，清理到只剩最近5條
                if len(messages.data) > MAX_THREAD_MESSAGES:
                    print(f"🔄 Cleaning long thread ({len(messages.data)} messages)")
                    
                    # 只保留最近的5條
                    recent_count = min(5, len(messages.data))
                    recent_messages = []
                    
                    for msg in messages.data[:recent_count]:
                        if hasattr(msg, 'content') and msg.content:
                            content = msg.content[0].text.value
                            # 限制每條訊息長度
                            if len(content) > 500:
                                content = content[:500] + "...[trimmed]"
                            recent_messages.append({
                                "role": msg.role,
                                "content": content
                            })
                    
                    # 創建新thread
                    new_thread = client.beta.threads.create(
                        messages=recent_messages[::-1]
                    )
                    thread_id = new_thread.id
                    redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)
                    
            except Exception as e:
                print(f"Error checking thread: {e}")
                thread_id = None
        
        # 3. 如果沒有thread，創建新的
        if not thread_id:
            thread = client.beta.threads.create(
                messages=[{"role": "user", "content": text}]
            )
            thread_id = thread.id
            redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)
        
        # 4. 加入新訊息到thread
        else:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text,
                timeout=5.0
            )
        
        # 5. 執行助理
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=10.0
        )
        
        # 6. 等待完成
        start = time.time()
        
        while run.status != "completed":
            if time.time() - start > MAX_WAIT_TIME:
                return "[System] Taking longer than expected. Please try again."
            
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message if run.last_error else "Unknown error"
                print(f"Run failed: {run.status}, Error: {error_msg}")
                
                # 如果是模型特定錯誤，清理thread
                if "context_length" in error_msg.lower() or "too many tokens" in error_msg.lower():
                    redis_db.delete(f"thread_id:{user_id}")
                    return "[System] Conversation too long. Starting fresh..."
                
                break
            
            time.sleep(0.8)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id,
                timeout=5.0
            )
        
        # 7. 取得回覆
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1,
            timeout=5.0
        )
        
        if not messages.data or not messages.data[0].content:
            return "[System] No response received."
            
        ai_reply = messages.data[0].content[0].text.value
        
        # 8. 儲存回覆
        save_message(user_id, "assistant", ai_reply)
        
        # 9. 定期重置計數（每5次對話）
        conv_key = f"conv:{user_id}"
        conv_count = redis_db.incr(conv_key)
        redis_db.expire(conv_key, 3600)
        
        if conv_count >= 5:
            redis_db.delete(conv_key)
            redis_db.delete(f"thread_id:{user_id}")
            print(f"🔄 Periodic reset for {user_id[:8]}")
        
        return ai_reply
        
    except openai.RateLimitError:
        # 即使改用4o-mini，仍可能有極端情況
        print(f"⚠️ Rate limit hit (unexpected)")
        return "[System] High traffic. Please wait 30 seconds."
        
    except openai.APITimeoutError:
        return "[System] Service timeout. Please try again."
        
    except Exception as e:
        print(f"GPT_response Error: {e}")
        traceback.print_exc()
        return "[System] Error processing request."
    
    finally:
        # 結束監控
        monitor.request_end()

# --- 6. LINE 處理（保持非同步） ---

import concurrent.futures
executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)  # 增加worker數

def process_async(user_id, text, reply_token):
    """非同步處理"""
    try:
        answer = GPT_response(user_id, text)
        
        if len(answer) > 3500:
            answer = answer[:3500] + "\n\n[Message trimmed]"
        
        line_bot_api.reply_message(
            reply_token, 
            TextSendMessage(text=answer)
        )
    except Exception as e:
        print(f"Async error: {e}")

def send_loading(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 10}
        requests.post(url, headers=headers, json=data, timeout=2)
    except:
        pass

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
    msg_id = event.message.id
    user_msg = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token

    # 防重複處理
    if redis_db.get(f"proc:{msg_id}"):
        return 
    redis_db.setex(f"proc:{msg_id}", 30, "true")

    # 群組過濾
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(f"proc:{msg_id}")
            return
    
    # 立即回覆
    try:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="Processing...")
        )
    except:
        return
    
    # 顯示動畫
    send_loading(user_id)
    
    # 非同步處理
    executor.submit(process_async, user_id, user_msg, reply_token)

# --- 7. 監控端點（強化） ---

@app.route("/monitor", methods=['GET'])
def system_monitor():
    """系統監控"""
    stats = monitor.get_stats()
    
    # Redis 資訊
    redis_info = {
        "connected": True,
        "keys": redis_db.dbsize(),
        "students_count": len(redis_db.keys("student_history:*")),
        "memory_used": redis_db.info().get('used_memory_human', 'N/A')
    }
    
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "model": "gpt-4o-mini",
        "rate_limit": "200,000 TPM",
        "monitor": stats,
        "redis": redis_info,
        "config": {
            "max_thread_messages": MAX_THREAD_MESSAGES,
            "max_wait_time": MAX_WAIT_TIME
        }
    })

# 保持其他端點不變（/export/conversations, /health, /reset/threads）

if __name__ == "__main__":
    print("🚀 Starting English Tutor Bot with gpt-4o-mini")
    print(f"📊 Token limit: 200,000 TPM (no rate limiting needed)")
    print(f"📝 Max thread messages: {MAX_THREAD_MESSAGES}")
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

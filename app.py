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
import concurrent.futures

app = Flask(__name__)

# --- 1. 初始化設定 ---
redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL is not set")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True,
                                     max_connections=10)  # 限制連接數

line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set")

client = openai.OpenAI(api_key=openai_api_key, timeout=25.0)  # 減少timeout
ASSISTANT_ID = os.getenv('ASSISTANT_ID') 

# --- 2. 根據硬體優化設定 ---
MAX_THREAD_MESSAGES = 15          # 適當增加對話記憶
MAX_MESSAGE_LENGTH = 2000         # 限制單條訊息長度
MAX_CONCURRENT_REQUESTS = 4       # 減少併發數（0.5 CPU）
MAX_WORKERS = 3                   # 背景執行緒數
REQUEST_TIMEOUT = 12              # 請求超時時間
REDIS_MAX_PER_STUDENT = 80        # 每生最大訊息數

# --- 3. 資源監控 ---
class ResourceMonitor:
    def __init__(self):
        self.request_count = 0
        self.start_time = time.time()
        self.lock = threading.Lock()
    
    def increment(self):
        with self.lock:
            self.request_count += 1
    
    def get_stats(self):
        with self.lock:
            uptime = time.time() - self.start_time
            return {
                "total_requests": self.request_count,
                "requests_per_minute": self.request_count / (uptime / 60) if uptime > 0 else 0,
                "uptime_hours": round(uptime / 3600, 2)
            }

monitor = ResourceMonitor()

# --- 4. 優化資料儲存 ---
def generate_anonymous_id(user_id):
    return hashlib.md5(user_id.encode()).hexdigest()[:10]  # 更短ID

def save_message_optimized(user_id, role, content):
    """節省記憶體的儲存方式"""
    try:
        student_id = generate_anonymous_id(user_id)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")  # 更緊湊時間格式
        
        # 壓縮內容
        if len(content) > MAX_MESSAGE_LENGTH:
            keep = MAX_MESSAGE_LENGTH // 2
            content = content[:keep] + "..." + content[-keep//2:]
        
        # 最小化資料結構
        message_data = {
            "s": student_id,
            "r": role[0],  # 'u' 或 'a'
            "c": content,
            "t": timestamp
        }
        
        # 使用更短的鍵名
        key = f"h:{student_id}"
        redis_db.rpush(key, json.dumps(message_data, separators=(',', ':')))
        
        # 嚴格控制歷史長度
        if redis_db.llen(key) > REDIS_MAX_PER_STUDENT:
            redis_db.ltrim(key, -REDIS_MAX_PER_STUDENT, -1)
        
        return True
    except Exception as e:
        print(f"Save optimized error: {e}")
        return False

# --- 5. GPT_response 函數（資源感知）---
def GPT_response(user_id, text):
    """資源感知的 AI 回應函數"""
    monitor.increment()
    
    try:
        # 檢查資源使用（簡化版）
        if monitor.get_stats()["requests_per_minute"] > 30:
            return "System is busy. Please wait a moment and try again."
        
        # 儲存使用者訊息
        save_message_optimized(user_id, "user", text[:1500])  # 進一步限制輸入長度
        
        # 取得或創建 thread
        thread_id = redis_db.get(f"t:{user_id}")  # 更短的鍵名
        
        # 智能清理 thread
        if thread_id:
            try:
                # 快速檢查訊息數量
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    limit=MAX_THREAD_MESSAGES + 2,
                    timeout=2.0
                )
                
                # 如果超過限制，清理到保留8條
                if len(messages.data) > MAX_THREAD_MESSAGES:
                    print(f"Cleaning thread ({len(messages.data)} -> 8)")
                    
                    # 只保留最近8條
                    keep_messages = []
                    for msg in messages.data[-8:]:
                        if hasattr(msg, 'content') and msg.content:
                            content = msg.content[0].text.value
                            if len(content) > 800:
                                content = content[:800] + "..."
                            keep_messages.append({
                                "role": msg.role,
                                "content": content
                            })
                    
                    if keep_messages:
                        new_thread = client.beta.threads.create(
                            messages=keep_messages
                        )
                        thread_id = new_thread.id
                        redis_db.setex(f"t:{user_id}", 2400, thread_id)  # 40分鐘
                    else:
                        thread_id = None
                        
            except Exception as e:
                print(f"Thread cleanup error: {e}")
                thread_id = None
        
        # 創建新 thread
        if not thread_id:
            thread = client.beta.threads.create(
                messages=[{"role": "user", "content": text[:1500]}]
            )
            thread_id = thread.id
            redis_db.setex(f"t:{user_id}", 2400, thread_id)
        
        # 加入新訊息
        else:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text[:1500],
                timeout=2.0
            )
        
        # 執行助理（較短timeout）
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=6.0
        )
        
        # 等待完成（最多10秒）
        start = time.time()
        while run.status != "completed":
            if time.time() - start > REQUEST_TIMEOUT:
                return "Processing taking longer than usual. Please try a shorter question."
            
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message[:100] if run.last_error else "Unknown"
                print(f"Run failed: {error_msg}")
                break
            
            time.sleep(0.6)  # 減少檢查頻率
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id,
                timeout=2.0
            )
        
        # 取得回覆
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1,
            timeout=2.0
        )
        
        if not messages.data or not messages.data[0].content:
            return "No response generated."
            
        ai_reply = messages.data[0].content[0].text.value
        
        # 儲存回覆（限制長度）
        save_message_optimized(user_id, "assistant", ai_reply[:2000])
        
        # 定期清理計數器
        conv_key = f"c:{user_id}"
        conv_count = redis_db.incr(conv_key)
        redis_db.expire(conv_key, 1800)
        
        if conv_count >= 6:  # 每6次對話清理
            redis_db.delete(conv_key)
            redis_db.delete(f"t:{user_id}")
            print(f"Periodic cleanup for {user_id[:8]}")
        
        return ai_reply
        
    except openai.APITimeoutError:
        return "AI service timeout. Please try again."
        
    except Exception as e:
        print(f"GPT_response error: {e}")
        return "System error. Please try again."

# --- 6. LINE 處理（保持不變但優化）---
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)

def send_loading(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 9}
        requests.post(url, headers=headers, json=data, timeout=2)
    except:
        pass

def stop_loading(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/stop'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id}
        requests.post(url, headers=headers, json=data, timeout=2)
    except:
        pass

def process_background(user_id, text, reply_token):
    try:
        send_loading(user_id)
        answer = GPT_response(user_id, text)
        stop_loading(user_id)
        
        if len(answer) > 2500:
            answer = answer[:2500] + "\n\n[Trimmed]"
        
        line_bot_api.reply_message(
            reply_token, 
            TextSendMessage(text=answer)
        )
        
    except Exception as e:
        print(f"Background error: {e}")
        try:
            stop_loading(user_id)
        except:
            pass

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
        return 'OK', 200
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"Callback error: {e}")
        return 'OK', 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg_id = event.message.id
    user_msg = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token

    # 防重複處理
    if redis_db.get(f"p:{msg_id}"):
        return 
    redis_db.setex(f"p:{msg_id}", 20, "1")

    # 群組過濾
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(f"p:{msg_id}")
            return
    
    # 提交背景處理
    executor.submit(process_background, user_id, user_msg, reply_token)

# --- 7. 管理端點 ---
@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        stats = monitor.get_stats()
        return jsonify({
            "status": "healthy",
            "resources": stats,
            "config": {
                "max_concurrent": MAX_CONCURRENT_REQUESTS,
                "max_thread_messages": MAX_THREAD_MESSAGES,
                "max_workers": MAX_WORKERS
            }
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/export/conversations", methods=['GET'])
def export_conversations():
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        all_data = []
        cursor = '0'
        
        while True:
            cursor, keys = redis_db.scan(cursor, match="h:*", count=30)
            
            for key in keys:
                student_id = key.split(":")[1]
                messages = redis_db.lrange(key, 0, -1)
                
                student_msgs = []
                for msg_json in messages:
                    try:
                        msg = json.loads(msg_json)
                        # 還原格式
                        student_msgs.append({
                            "student_id": msg["s"],
                            "role": "user" if msg["r"] == "u" else "assistant",
                            "content": msg["c"],
                            "timestamp": msg["t"]
                        })
                    except:
                        continue
                
                if student_msgs:
                    all_data.append({
                        "student_id": student_id,
                        "total_messages": len(student_msgs),
                        "messages": student_msgs[:50]
                    })
            
            if cursor == '0':
                break
        
        return jsonify({
            "export_time": datetime.now().isoformat(),
            "total_students": len(all_data),
            "data": all_data
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route("/test-openai", methods=['POST'])
def test_openai():
    """測試用的端點，確保真的呼叫 OpenAI"""
    try:
        data = request.json
        user_id = data.get('user_id', 'test_user')
        message = data.get('message', 'Hello, please give me a real response.')
        
        print(f"🔍 Test endpoint called by {user_id}: {message[:50]}")
        
        # 確保這是需要真實回應的測試
        wait_for_real = data.get('wait_for_real_response', False)
        
        if wait_for_real:
            print(f"⏳ Making real OpenAI call for {user_id}")
            # 實際呼叫 GPT_response
            response = GPT_response(user_id, message)
            print(f"✅ OpenAI responded to {user_id}")
        else:
            # 快速測試模式
            response = "Test response (quick mode)"
        
        return jsonify({
            "success": True,
            "user_id": user_id,
            "response": response[:500] if response else "",
            "response_length": len(response) if response else 0,
            "timestamp": datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        print(f"❌ Test endpoint error: {e}")
        traceback.print_exc()
        return jsonify({
            "success": False, 
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500
# --- 8. 啟動 ---
if __name__ == "__main__":
    print(f"🚀 Starting with {MAX_WORKERS} workers, {MAX_CONCURRENT_REQUESTS} concurrent limit")
    print(f"💾 Memory optimized: {MAX_THREAD_MESSAGES} messages per thread")
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

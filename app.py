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
import concurrent.futures  # 新增這行！

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

# --- 2. 優化設定 ---
MAX_WAIT_TIME = 8  # 低於 LINE 的 10 秒限制
MAX_THREAD_MESSAGES = 10

# --- 3. 資料儲存 ---
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
            "timestamp": timestamp
        }
        
        redis_db.rpush(f"student_history:{student_id}", json.dumps(message_data))
        
        if redis_db.llen(f"student_history:{student_id}") > 100:
            redis_db.ltrim(f"student_history:{student_id}", -100, -1)
        
        return True
    except Exception as e:
        print(f"Save error: {e}")
        return False

# --- 4. GPT_response 函數（優化速度）---
def GPT_response(user_id, text):
    try:
        # 1. 快速儲存使用者訊息
        save_message(user_id, "user", text)
        
        # 2. 取得 thread_id（快速）
        thread_id = redis_db.get(f"thread_id:{user_id}")
        
        # 3. 如果 thread 太長，直接創建新的（不檢查，加快速度）
        if not thread_id:
            thread = client.beta.threads.create(
                messages=[{"role": "user", "content": text}]
            )
            thread_id = thread.id
            redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)
        else:
            # 快速加入訊息（不檢查歷史長度）
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text,
                timeout=3.0
            )
        
        # 4. 快速執行助理
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=5.0
        )
        
        # 5. 快速等待（最多 8 秒）
        start = time.time()
        while run.status != "completed":
            if time.time() - start > MAX_WAIT_TIME:
                # 超時時返回提示
                return "I need more time to think about this. Please try asking a shorter question or wait a moment."
            
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message if run.last_error else "Unknown"
                print(f"Run failed: {error_msg}")
                break
            
            time.sleep(0.5)  # 更頻繁檢查
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id,
                timeout=3.0
            )
        
        # 6. 快速取得回覆
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1,
            timeout=3.0
        )
        
        if not messages.data or not messages.data[0].content:
            return "I couldn't generate a response. Please try again."
            
        ai_reply = messages.data[0].content[0].text.value
        
        # 7. 儲存回覆
        save_message(user_id, "assistant", ai_reply)
        
        # 8. 定期清理（每 5 次對話）
        conv_key = f"conv:{user_id}"
        conv_count = redis_db.incr(conv_key)
        redis_db.expire(conv_key, 3600)
        
        if conv_count >= 5:
            redis_db.delete(conv_key)
            redis_db.delete(f"thread_id:{user_id}")
            print(f"Cleaned thread for {user_id[:8]}")
        
        return ai_reply
        
    except openai.APITimeoutError:
        return "The AI service is responding slowly. Please try again."
        
    except Exception as e:
        print(f"GPT_response error: {e}")
        return "System error. Please try again."

# --- 5. LINE 動畫函數（有重試保護）---
def send_loading_animation(chat_id, request_id):
    """傳送載入動畫，帶有請求ID防止重複"""
    try:
        # 檢查是否已經有動畫在運行
        loading_key = f"loading:{request_id}"
        if redis_db.get(loading_key):
            print(f"Loading already active for request {request_id[:8]}")
            return False
        
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 8}  # 減少秒數
        
        response = requests.post(url, headers=headers, json=data, timeout=2)
        
        if response.status_code == 200:
            # 記錄動畫開始時間
            redis_db.setex(loading_key, 10, "active")
            return True
        else:
            print(f"Loading failed: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"Send loading error: {e}")
        return False

def stop_loading_animation(chat_id, request_id):
    """停止載入動畫，帶有請求ID"""
    try:
        # 檢查是否有動畫在運行
        loading_key = f"loading:{request_id}"
        if not redis_db.get(loading_key):
            print(f"No active loading for request {request_id[:8]}")
            return False
        
        url = 'https://api.line.me/v2/bot/chat/loading/stop'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id}
        
        response = requests.post(url, headers=headers, json=data, timeout=2)
        
        # 無論成功與否，都清除標記
        redis_db.delete(loading_key)
        
        if response.status_code != 200:
            print(f"Stop loading failed: {response.status_code}")
        
        return True
        
    except Exception as e:
        print(f"Stop loading error: {e}")
        return False

# --- 6. 背景處理執行緒池 ---
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

def process_message_background(request_id, user_id, text, reply_token):
    """背景處理訊息，使用 push_message 避免 reply_token 過期"""
    try:
        # 1. 取得 AI 回應
        answer = GPT_response(user_id, text)
        
        # 2. 停止動畫
        stop_loading_animation(user_id, request_id)
        
        # 3. 檢查長度
        if len(answer) > 3000:
            answer = answer[:3000] + "\n\n[Message trimmed]"
        
        # 4. 嘗試使用 reply_token（可能已失效）
        try:
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text=answer)
            )
            print(f"✅ Replied with token for {user_id[:8]}")
            
        except Exception as reply_error:
            print(f"Reply token expired, using push message: {reply_error}")
            
            # 改用 push_message（不需要 reply_token）
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=answer)
            )
            print(f"✅ Pushed message for {user_id[:8]}")
        
    except Exception as e:
        print(f"Background processing error: {e}")
        traceback.print_exc()
        
        # 確保停止動畫
        try:
            stop_loading_animation(user_id, request_id)
        except:
            pass
        
        # 嘗試發送錯誤訊息
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="Sorry, there was an error processing your message.")
            )
        except:
            pass

# --- 7. LINE Webhook 處理（關鍵修正）---
@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook 端點"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    # 記錄請求時間
    request_time = time.time()
    
    try:
        # 立即處理 LINE 簽名驗證
        handler.handle(body, signature)
        
        # 立即返回 200 OK（防止 LINE 重試）
        return 'OK', 200
        
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"Callback error: {e}")
        # 即使出錯也要返回 200，防止 LINE 重試
        return 'OK', 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理文字訊息"""
    # 1. 取得基本資訊
    msg_id = event.message.id
    user_msg = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token
    
    # 2. 生成請求 ID（用於追蹤）
    request_id = hashlib.md5(f"{msg_id}:{user_id}".encode()).hexdigest()[:12]
    
    # 3. 防重複處理（關鍵！）
    processing_key = f"processing:{msg_id}"
    
    # 如果正在處理或已處理，直接返回
    if redis_db.get(processing_key):
        print(f"⚠️ Duplicate request for message {msg_id}, skipping")
        return  # 不返回任何內容，但 handler 會處理
    
    # 標記為處理中（15秒過期，防止卡住）
    redis_db.setex(processing_key, 15, "true")
    
    # 4. 群組過濾
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(processing_key)
            return
    
    # 5. 顯示載入動畫
    send_loading_animation(user_id, request_id)
    
    # 6. 提交到背景執行緒處理
    executor.submit(
        process_message_background,
        request_id,
        user_id,
        user_msg,
        reply_token
    )
    
    # 7. 立即返回（不等待處理完成）
    # handler 會自動處理，這裡不需要 return 任何東西
    
    # 8. 設定清理任務（5秒後清理標記）
    def cleanup():
        time.sleep(5)
        redis_db.delete(processing_key)
    
    threading.Thread(target=cleanup, daemon=True).start()

# --- 8. 管理端點 ---
@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

@app.route("/monitor", methods=['GET'])
def monitor():
    """系統監控"""
    try:
        # 檢查正在處理的請求
        processing_count = len(redis_db.keys("processing:*"))
        loading_count = len(redis_db.keys("loading:*"))
        
        return jsonify({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "redis_connected": True,
            "processing_requests": processing_count,
            "active_loading": loading_count,
            "thread_pool": {
                "max_workers": 5,
                "active_threads": threading.active_count()
            }
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/cleanup", methods=['POST'])
def cleanup_all():
    """清理所有暫存狀態"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # 清理所有 processing 標記
        cursor = '0'
        deleted_count = 0
        
        while True:
            cursor, keys = redis_db.scan(cursor, match="processing:*", count=100)
            if keys:
                redis_db.delete(*keys)
                deleted_count += len(keys)
            if cursor == '0':
                break
        
        # 清理所有 loading 標記
        cursor = '0'
        loading_deleted = 0
        
        while True:
            cursor, keys = redis_db.scan(cursor, match="loading:*", count=100)
            if keys:
                redis_db.delete(*keys)
                loading_deleted += len(keys)
            if cursor == '0':
                break
        
        return jsonify({
            "status": "cleaned",
            "processing_cleared": deleted_count,
            "loading_cleared": loading_deleted,
            "message": "All temporary states cleared"
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/export/conversations", methods=['GET'])
def export_conversations():
    """匯出對話資料"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        all_data = []
        cursor = '0'
        
        while True:
            cursor, keys = redis_db.scan(cursor, match="student_history:*", count=50)
            
            for key in keys:
                student_id = key.split(":")[1]
                messages = redis_db.lrange(key, 0, -1)
                
                student_msgs = []
                for msg_json in messages:
                    try:
                        student_msgs.append(json.loads(msg_json))
                    except:
                        continue
                
                if student_msgs:
                    all_data.append({
                        "student_id": student_id,
                        "total_messages": len(student_msgs),
                        "messages": student_msgs[:100]  # 最多100條
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

# --- 9. 啟動應用程式 ---
if __name__ == "__main__":
    print("🚀 Starting English Tutor Bot")
    print(f"⚡ Max wait time: {MAX_WAIT_TIME}s (under LINE's 10s limit)")
    print(f"💾 Max thread messages: {MAX_THREAD_MESSAGES}")
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

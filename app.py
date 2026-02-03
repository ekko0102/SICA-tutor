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
from functools import wraps

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

# --- 2. 實驗優化設定 ---
MAX_WAIT_TIME = 20  # 低於 Render 的 30秒
MAX_THREAD_MESSAGES = 12  # 每個 thread 最多保留 12 條訊息
MAX_CONCURRENT_REQUESTS = 8  # 同時處理上限

# 請求限流器
request_semaphore = threading.Semaphore(MAX_CONCURRENT_REQUESTS)

def rate_limit(f):
    """限流裝飾器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not request_semaphore.acquire(blocking=False):
            return "Too many requests, please try again later.", 429
        try:
            return f(*args, **kwargs)
        finally:
            request_semaphore.release()
    return decorated_function

# --- 3. 強化資料儲存 ---

def generate_anonymous_id(user_id):
    """生成匿名學生ID"""
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]

def save_message_with_backup(user_id, role, content):
    """強化儲存，有錯誤備援"""
    try:
        student_id = generate_anonymous_id(user_id)
        timestamp = datetime.now().isoformat()
        
        # 限制內容長度，減少 token 使用
        content_limited = content[:3000] if len(content) > 3000 else content
        
        message_data = {
            "student_id": student_id,
            "role": role,
            "content": content_limited,
            "timestamp": timestamp,
            "user_id_hash": student_id
        }
        
        # 主要儲存
        redis_db.rpush(
            f"student_history:{student_id}", 
            json.dumps(message_data)
        )
        
        # 限制每個學生的歷史紀錄長度（最多100條）
        if redis_db.llen(f"student_history:{student_id}") > 100:
            redis_db.ltrim(f"student_history:{student_id}", -100, -1)
        
        # 更新學生狀態
        redis_db.hset(f"student:{student_id}", "last_active", timestamp)
        
        # 記錄活動計數
        today = datetime.now().strftime("%Y%m%d")
        redis_db.incr(f"stats:messages:{today}")
        
        return True
    except Exception as e:
        print(f"Error saving message: {e}")
        return False

# --- 4. 強化 GPT_response（解決 token 限制問題）---

@rate_limit
def GPT_response(user_id, text):
    try:
        # 0. 檢查是否為重複請求
        request_hash = hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()[:8]
        if redis_db.get(f"req:{request_hash}"):
            return "[System] I'm still thinking about your previous question..."
        redis_db.setex(f"req:{request_hash}", 30, "processing")
        
        # 1. 儲存使用者訊息
        save_message_with_backup(user_id, "user", text)
        
        # 2. 檢查並清理過長的對話
        thread_id = redis_db.get(f"thread_id:{user_id}")
        
        if thread_id:
            try:
                # 快速檢查訊息數量
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    limit=MAX_THREAD_MESSAGES + 5,  # 多檢查幾條
                    timeout=3.0
                )
                
                # 如果超過限制，創建新thread
                if len(messages.data) > MAX_THREAD_MESSAGES:
                    print(f"🔄 Resetting long thread ({len(messages.data)} messages) for {user_id[:8]}")
                    
                    # 只保留最近的3條訊息
                    recent_count = min(3, len(messages.data))
                    recent_messages = []
                    
                    for msg in messages.data[:recent_count]:
                        if hasattr(msg, 'content') and msg.content:
                            recent_messages.append({
                                "role": msg.role,
                                "content": msg.content[0].text.value[:500]  # 限制長度
                            })
                    
                    # 創建新thread
                    if recent_messages:
                        new_thread = client.beta.threads.create(
                            messages=[
                                {
                                    "role": "system", 
                                    "content": f"[Continued from previous conversation, {len(messages.data) - recent_count} earlier messages truncated]"
                                }
                            ] + recent_messages[::-1]  # 反轉順序
                        )
                    else:
                        new_thread = client.beta.threads.create(
                            messages=[{"role": "user", "content": text}]
                        )
                    
                    thread_id = new_thread.id
                    redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)  # 30分鐘
                    
                    print(f"✅ Created new thread with {len(recent_messages)} recent messages")
                    
            except Exception as e:
                print(f"Error checking thread size: {e}")
                # 如果檢查失敗，刪除舊thread重新開始
                redis_db.delete(f"thread_id:{user_id}")
                thread_id = None
        
        # 3. 如果沒有thread，創建新的
        if not thread_id:
            thread = client.beta.threads.create(
                messages=[{"role": "user", "content": text}]
            )
            thread_id = thread.id
            redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)
            print(f"🆕 Created new thread for {user_id[:8]}")
        
        # 4. 如果是現有thread，加入新訊息
        else:
            try:
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=text,
                    timeout=5.0
                )
            except Exception as e:
                print(f"Error adding message to thread: {e}")
                # 如果加入失敗，創建新thread
                thread = client.beta.threads.create(
                    messages=[{"role": "user", "content": text}]
                )
                thread_id = thread.id
                redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)
        
        # 5. 執行助理
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=10.0
        )
        
        # 6. 等待完成
        start = time.time()
        
        while run.status != "completed":
            # 檢查是否超時
            if time.time() - start > MAX_WAIT_TIME:
                print(f"⏰ Timeout waiting for run completion for {user_id[:8]}")
                
                # 嘗試取得現有回應
                try:
                    messages = client.beta.threads.messages.list(
                        thread_id=thread_id,
                        order="desc",
                        limit=1,
                        timeout=3.0
                    )
                    if messages.data and messages.data[0].role == "assistant":
                        ai_reply = messages.data[0].content[0].text.value
                        save_message_with_backup(user_id, "assistant", ai_reply)
                        return ai_reply
                except:
                    pass
                
                return "[System] Taking longer than expected. Please try a shorter question."
            
            # 檢查是否失敗
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message if run.last_error else "Unknown error"
                print(f"❌ Run failed: {run.status}, Error: {error_msg}")
                
                # 特別處理token限制錯誤
                if "tokens per min" in error_msg or "TPM" in error_msg or "Request too large" in error_msg:
                    # 清除thread，強制重新開始
                    redis_db.delete(f"thread_id:{user_id}")
                    # 增加等待時間
                    redis_db.setex(f"ratelimit:{user_id}", 60, "wait")
                    return "[System] Rate limit reached. Please wait a minute before continuing."
                
                raise Exception(f"Run failed: {run.status}, Error: {error_msg}")
            
            # 短暫等待後再次檢查
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
            return "[System] No response received. Please try again."
            
        ai_reply = messages.data[0].content[0].text.value
        
        # 8. 儲存回覆
        save_message_with_backup(user_id, "assistant", ai_reply)
        
        # 9. 定期清理計數器
        conv_count_key = f"count:{user_id}"
        conv_count = redis_db.incr(conv_count_key)
        redis_db.expire(conv_count_key, 3600)  # 1小時過期
        
        # 每4次對話清理一次thread
        if conv_count >= 4:
            redis_db.delete(conv_count_key)
            redis_db.delete(f"thread_id:{user_id}")
            print(f"🧹 Cleaned thread for {user_id[:8]} after 4 conversations")
        
        return ai_reply
        
    except openai.RateLimitError as e:
        print(f"⚠️ OpenAI RateLimitError: {e}")
        # 設置冷卻時間
        redis_db.setex(f"ratelimit:{user_id}", 90, "wait")
        return "[System] The AI service is experiencing high traffic. Please wait 1-2 minutes and try again."
        
    except openai.APITimeoutError as e:
        print(f"⚠️ OpenAI APITimeoutError: {e}")
        return "[System] The AI service is responding slowly. Please try a shorter question."
        
    except Exception as e:
        print(f"GPT_response Error for {user_id[:8]}: {e}")
        traceback.print_exc()
        
        # 檢查是否為速率限制相關錯誤
        error_str = str(e)
        if "rate limit" in error_str.lower() or "tokens per min" in error_str or "TPM" in error_str:
            redis_db.setex(f"ratelimit:{user_id}", 120, "wait")
            return "[System] Rate limit reached. Please wait 2 minutes before asking more questions."
        
        return "[System] Error processing request. Please try again."

# --- 5. 非同步處理 LINE 回應 ---

import concurrent.futures
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

def process_message_async(user_id, text, reply_token):
    """非同步處理訊息，避免 LINE 超時"""
    try:
        # 檢查速率限制
        if redis_db.get(f"ratelimit:{user_id}"):
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text="[System] Please wait a moment before sending more messages. The system is currently busy.")
            )
            return
        
        answer = GPT_response(user_id, text)
        
        if len(answer) > 3500:
            answer = answer[:3500] + "\n\n[Message truncated due to length]"
        
        line_bot_api.reply_message(
            reply_token, 
            TextSendMessage(text=answer)
        )
    except Exception as e:
        print(f"Async processing error: {e}")
        try:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="[System] Error processing your message. Please try again.")
            )
        except:
            pass

# --- 6. LINE Webhook 處理 ---

def send_loading_animation(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 12}
        response = requests.post(url, headers=headers, json=data, timeout=2)
        if response.status_code != 200:
            print(f"Loading animation failed: {response.status_code}")
    except:
        pass  # 動畫失敗不影響主要功能

def stop_loading_animation(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/stop'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id}
        response = requests.post(url, headers=headers, json=data, timeout=2)
        if response.status_code != 200:
            print(f"Stop animation failed: {response.status_code}")
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
    
    # 立即回覆接收確認（避免 LINE 超時）
    try:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="Got your message! Thinking...")
        )
    except Exception as e:
        print(f"Error sending initial reply: {e}")
        return
    
    # 顯示動畫
    send_loading_animation(user_id)
    
    # 非同步處理主要邏輯
    executor.submit(
        process_message_async,
        user_id, 
        user_msg, 
        reply_token
    )
    
    # 立即返回，避免超時
    return

# --- 7. 監控與管理端點 ---

@app.route("/monitor", methods=['GET'])
def monitor():
    """監控系統狀態"""
    try:
        stats = {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "redis_connected": True,
            "active_threads": threading.active_count(),
            "queue_size": MAX_CONCURRENT_REQUESTS - request_semaphore._value,
            "today_messages": redis_db.get("stats:messages:" + datetime.now().strftime("%Y%m%d")) or 0,
            "config": {
                "max_thread_messages": MAX_THREAD_MESSAGES,
                "max_wait_time": MAX_WAIT_TIME,
                "max_concurrent": MAX_CONCURRENT_REQUESTS
            }
        }
        return jsonify(stats)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/export/conversations", methods=['GET'])
def export_conversations():
    """匯出所有對話資料"""
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
                
                student_messages = []
                for msg_json in messages:
                    try:
                        student_messages.append(json.loads(msg_json))
                    except:
                        continue
                
                if student_messages:
                    # 取得學生資訊
                    student_info = redis_db.hgetall(f"student:{student_id}")
                    
                    all_data.append({
                        "student_id": student_id,
                        "total_messages": len(student_messages),
                        "last_active": student_info.get("last_active", ""),
                        "messages": student_messages[:50]  # 每生最多50則
                    })
            
            if cursor == '0':
                break
        
        return jsonify({
            "export_time": datetime.now().isoformat(),
            "total_students": len(all_data),
            "sampled": True,
            "data": all_data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reset/threads", methods=['POST'])
def reset_threads():
    """重設所有 thread（實驗前使用）"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        deleted_count = 0
        cursor = '0'
        
        while True:
            cursor, keys = redis_db.scan(cursor, match="thread_id:*", count=100)
            if keys:
                redis_db.delete(*keys)
                deleted_count += len(keys)
            if cursor == '0':
                break
        
        return jsonify({
            "status": "success",
            "deleted_threads": deleted_count,
            "message": "All threads have been reset"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        return "OK", 200
    except:
        return "Redis connection failed", 500

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server with config:")
    print(f"- MAX_THREAD_MESSAGES: {MAX_THREAD_MESSAGES}")
    print(f"- MAX_WAIT_TIME: {MAX_WAIT_TIME}")
    print(f"- MAX_CONCURRENT_REQUESTS: {MAX_CONCURRENT_REQUESTS}")
    app.run(host='0.0.0.0', port=port, threaded=True)

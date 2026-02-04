from flask import Flask, request, abort, jsonify, make_response, send_file
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

# 硬碟儲存設定
try:
    from disk_config import disk_storage
    DISK_ENABLED = True
    print(f"✅ Disk storage enabled at: /data")
except ImportError as e:
    DISK_ENABLED = False
    print(f"⚠️  Disk storage disabled: {e}")
except Exception as e:
    DISK_ENABLED = False
    print(f"⚠️  Disk storage disabled (other error): {e}")

app = Flask(__name__)

# =============================================
# 初始化設定
# =============================================

redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL is not set")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True,
                                     max_connections=20)

line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set")

# 初始化 OpenAI 客戶端
try:
    client = openai.OpenAI(api_key=openai_api_key)
except Exception as e:
    print(f"❌ OpenAI client initialization failed: {e}")
    class SimpleOpenAIClient:
        def __init__(self, api_key):
            self.api_key = api_key
    client = SimpleOpenAIClient(api_key=openai_api_key)

ASSISTANT_ID = os.getenv('ASSISTANT_ID') 

# =============================================
# 優化設定
# =============================================

MAX_THREAD_MESSAGES = 15
MAX_MESSAGE_LENGTH = 2000
REDIS_MAX_PER_STUDENT = 80

# =============================================
# 簡單隊列系統（使用 ThreadPoolExecutor）
# =============================================

# 全域執行緒池（控制最大並發數）
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)

def process_in_background(user_id, text, reply_token=None):
    """背景處理訊息的函數"""
    try:
        print(f"🤖 Background processing for {user_id[:8]}")
        
        # 1. 啟動載入動畫
        # try:
        #     send_loading(user_id, loading_seconds=30)
        #     print(f"▶️ Loading animation started for {user_id[:8]}")
        # except Exception as e:
        #     print(f"⚠️ Loading failed: {e}")
        
        # # 2. 立即回覆確認（如果 reply_token 還有效）
        # if reply_token:
        #     try:
        #         line_bot_api.reply_message(
        #             reply_token,
        #             TextSendMessage(text="正在為您思考中...")
        #         )
        #         print(f"💭 Confirmation sent for {user_id[:8]}")
        #     except Exception as e:
        #         print(f"⚠️ Confirmation failed: {e}")
        
        # 3. 呼叫 GPT
        start_time = time.time()
        response = GPT_response_direct(user_id, text)
        elapsed = time.time() - start_time
        
        print(f"✅ GPT response for {user_id[:8]} in {elapsed:.1f}s")
        print(f"📄 Response preview: {response[:100]}...")
        
        # 4. 發送回應
        if len(response) > 3000:
            response = response[:3000] + "\n\n[訊息已截斷]"
        
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=response)
            )
            print(f"📤 Response sent to {user_id[:8]} ({len(response)} chars)")
        except Exception as e:
            print(f"❌ Push message failed: {e}")
            
            # 嘗試使用 reply_token 作為備用
            if reply_token:
                try:
                    line_bot_api.reply_message(
                        reply_token,
                        TextSendMessage(text=response)
                    )
                    print(f"📤 Response sent via reply_token")
                except Exception as e2:
                    print(f"❌ Reply token also failed: {e2}")
        
    except Exception as e:
        print(f"❌❌❌ Background processing error: {e}")
        traceback.print_exc()
        
        # 發送錯誤安慰訊息
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="Sorry, there were some issues during processing. Please try again later.。")
            )
        except:
            pass

# =============================================
# 資源監控
# =============================================

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

# =============================================
# 優化資料儲存
# =============================================

def generate_anonymous_id(user_id):
    return hashlib.md5(user_id.encode()).hexdigest()[:10]

def save_message_optimized(user_id, role, content):
    """節省記憶體的儲存方式"""
    try:
        student_id = generate_anonymous_id(user_id)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        
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

# =============================================
# GPT_response 函數
# =============================================

def GPT_response_direct(user_id, text):
    """直接呼叫 OpenAI 的版本 - 永不返回錯誤訊息"""
    monitor.increment()
    
    # 儲存使用者訊息
    save_message_optimized(user_id, "user", text[:1500])
    
    # 移除所有超時檢查和錯誤訊息返回
    try:
        # 取得或創建 thread
        thread_id = redis_db.get(f"t:{user_id}")
        
        # 智能清理 thread
        if thread_id:
            try:
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    limit=MAX_THREAD_MESSAGES + 2,
                    timeout=10.0  # 增加超時
                )
                
                if len(messages.data) > MAX_THREAD_MESSAGES:
                    print(f"Cleaning thread ({len(messages.data)} -> 8)")
                    
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
                        redis_db.setex(f"t:{user_id}", 3600, thread_id)  # 增加到1小時
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
            redis_db.setex(f"t:{user_id}", 3600, thread_id)
        
        # 加入新訊息
        else:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text[:1500],
                timeout=10.0
            )
        
        # 執行助理 - 增加超時
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=30.0
        )
        
        # 耐心等待完成 - 無超時限制
        while run.status != "completed":
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message[:100] if run.last_error else "Unknown"
                print(f"Run failed: {error_msg}")
                # 重新開始
                run = client.beta.threads.runs.create(
                    thread_id=thread_id, 
                    assistant_id=ASSISTANT_ID,
                    timeout=30.0
                )
            
            time.sleep(1)  # 每秒檢查一次
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id,
                timeout=10.0
            )
        
        # 取得回覆
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1,
            timeout=10.0
        )
        
        if not messages.data or not messages.data[0].content:
            # 如果沒有回應，返回預設回應而不是錯誤
            ai_reply = "I've received your question and I'm thinking about it. Please wait a moment."
        else:
            ai_reply = messages.data[0].content[0].text.value
        
        # 儲存回覆
        save_message_optimized(user_id, "assistant", ai_reply[:2000])
        
        # 定期清理
        conv_key = f"c:{user_id}"
        conv_count = redis_db.incr(conv_key)
        redis_db.expire(conv_key, 3600)
        
        if conv_count >= 10:  # 增加到10次對話才清理
            redis_db.delete(conv_key)
            redis_db.delete(f"t:{user_id}")
            print(f"Periodic cleanup for {user_id[:8]}")
        
        # 硬碟儲存（如果啟用）
        if DISK_ENABLED:
            threading.Thread(
                target=save_to_disk_in_background,
                args=(user_id,),
                daemon=True
            ).start()        
        return ai_reply
        
    except Exception as e:
        print(f"GPT_response error: {e}")
        # 返回中性回應，而不是錯誤訊息
        return "I'm currently processing your request. Please give me a moment to think."

def save_to_disk_in_background(user_id):
    """背景執行：儲存對話到硬碟"""
    try:
        # 等待一下，讓 Redis 有時間儲存
        time.sleep(2)
        
        # 取得學生匿名 ID
        student_id = generate_anonymous_id(user_id)
        
        # 從 Redis 取得完整的對話歷史
        key = f"h:{student_id}"
        messages_json = redis_db.lrange(key, 0, -1)
        
        # 轉換為標準格式
        messages_list = []
        for msg_json in messages_json:
            try:
                msg = json.loads(msg_json)
                messages_list.append({
                    "role": "user" if msg["r"] == "u" else "assistant",
                    "content": msg["c"],
                    "timestamp": msg["t"]
                })
            except:
                continue
        
        # 儲存到硬碟
        if messages_list and DISK_ENABLED:
            success = disk_storage.save_student_conversation(student_id, messages_list)
            if success:
                print(f"💾 Disk save successful for {student_id[:8]} ({len(messages_list)} messages)")
            else:
                print(f"❌ Disk save failed for {student_id[:8]}")
        
    except Exception as e:
        print(f"⚠️  Background disk save failed: {e}")

# =============================================
# LINE 載入動畫函數
# =============================================

def send_loading(chat_id, loading_seconds=60):
    """發送載入動畫"""
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": loading_seconds}
        response = requests.post(url, headers=headers, json=data, timeout=3)
        if response.status_code == 200:
            print(f"▶️ Started loading animation for {chat_id[:8]} ({loading_seconds}s)")
        return True
    except Exception as e:
        print(f"Failed to start loading: {e}")
        return False

def stop_loading(chat_id):
    """停止載入動畫（可選，載入動畫會自動停止）"""
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/stop'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id}
        response = requests.post(url, headers=headers, json=data, timeout=3)
        if response.status_code == 200:
            print(f"⏹️ Stopped loading animation for {chat_id[:8]}")
        return True
    except Exception as e:
        print(f"Failed to stop loading: {e}")
        return False

# =============================================
# LINE Webhook 處理
# =============================================

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

    print(f"📩 LINE Message received: {user_id[:8]} said: {user_msg[:50]}")

    # 防重複處理
    if redis_db.get(f"p:{msg_id}"):
        print(f"⚠️  Duplicate message {msg_id}, skipping")
        return 
    
    redis_db.setex(f"p:{msg_id}", 20, "1")

    # 群組過濾
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(f"p:{msg_id}")
            return
    
    # 立即撥放動畫
    try:
        send_loading(user_id)
        print(f"▶️ Started loading animation for {user_id}")
    except Exception as e:
        print(f"⚠️  Failed to start loading: {e}")
    
    # 提交到背景處理隊列
    thread_pool.submit(process_in_background, user_id, user_msg, reply_token)
    
    print(f"✅ Message submitted to thread pool for {user_id[:8]}")

# =============================================
# 管理端點
# =============================================

@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        stats = monitor.get_stats()
        
        return jsonify({
            "status": "healthy",
            "resources": stats,
            "thread_pool": {
                "max_workers": thread_pool._max_workers,
                "active_threads": len([t for t in threading.enumerate() if t.name.startswith("ThreadPool")])
            },
            "config": {
                "max_thread_messages": MAX_THREAD_MESSAGES,
                "disk_storage": "enabled" if DISK_ENABLED else "disabled"
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

# =============================================
# 啟動
# =============================================

if __name__ == "__main__":
    print(f"""
    ========================================
    🚀 SIMPLE LINE BOT STARTING
    ========================================
    Features:
    ✅ Simple thread pool system
    ✅ Loading animations
    ✅ No error messages to users
    ✅ Background processing
    
    Thread Pool: {thread_pool._max_workers} workers
    Disk Storage: {'✅ Enabled' if DISK_ENABLED else '❌ Disabled'}
    ========================================
    """)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

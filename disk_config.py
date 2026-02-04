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
import uuid

app = Flask(__name__)

# =============================================
# 1. 導入硬碟儲存
# =============================================
try:
    from disk_config import disk_storage
    DISK_ENABLED = True
    print(f"✅ Disk storage enabled at: {disk_storage.mount_path}")
except ImportError as e:
    DISK_ENABLED = False
    print(f"⚠️  Disk storage disabled: {e}")

# =============================================
# 2. 現有的隊列系統（保持不變）
# =============================================
class OpenAIBatchProcessor:
    """批量處理 OpenAI 請求，避免超載"""
    def __init__(self, max_concurrent=5):
        self.max_concurrent = max_concurrent
        self.semaphore = threading.Semaphore(max_concurrent)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent)
        self.request_count = 0
        
    def process(self, user_id, text):
        """處理單一請求"""
        self.request_count += 1
        req_num = self.request_count
        
        print(f"[{req_num}] Request from {user_id[:8]} waiting for semaphore...")
        
        acquired = self.semaphore.acquire(blocking=False)
        if not acquired:
            print(f"[{req_num}] Queue full, waiting...")
            self.semaphore.acquire(blocking=True)
        
        try:
            print(f"[{req_num}] Processing for {user_id[:8]}...")
            result = self._call_gpt_response(user_id, text)
            return result
            
        finally:
            self.semaphore.release()
            print(f"[{req_num}] Completed for {user_id[:8]}")
    
    def _call_gpt_response(self, user_id, text):
        """呼叫現有的 GPT_response 函數"""
        return GPT_response_direct(user_id, text)

# 建立全域處理器
openai_processor = OpenAIBatchProcessor(max_concurrent=5)

# =============================================
# 3. 初始化設定（保持不變）
# =============================================
redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL is not set")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True, max_connections=10)

line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set")

client = openai.OpenAI(api_key=openai_api_key, timeout=25.0)
ASSISTANT_ID = os.getenv('ASSISTANT_ID')

# =============================================
# 4. 優化設定（保持不變）
# =============================================
MAX_THREAD_MESSAGES = 15
MAX_MESSAGE_LENGTH = 2000
MAX_CONCURRENT_REQUESTS = 5
MAX_WORKERS = 3
REQUEST_TIMEOUT = 12
REDIS_MAX_PER_STUDENT = 80

# =============================================
# 5. 資源監控（保持不變）
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
# 6. 優化資料儲存（保持不變）
# =============================================
def generate_anonymous_id(user_id):
    return hashlib.md5(user_id.encode()).hexdigest()[:10]

def save_message_optimized(user_id, role, content):
    """節省記憶體的儲存方式"""
    try:
        student_id = generate_anonymous_id(user_id)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        
        if len(content) > MAX_MESSAGE_LENGTH:
            keep = MAX_MESSAGE_LENGTH // 2
            content = content[:keep] + "..." + content[-keep//2:]
        
        message_data = {
            "s": student_id,
            "r": role[0],
            "c": content,
            "t": timestamp
        }
        
        key = f"h:{student_id}"
        redis_db.rpush(key, json.dumps(message_data, separators=(',', ':')))
        
        if redis_db.llen(key) > REDIS_MAX_PER_STUDENT:
            redis_db.ltrim(key, -REDIS_MAX_PER_STUDENT, -1)
        
        return True
    except Exception as e:
        print(f"Save optimized error: {e}")
        return False

# =============================================
# 7. GPT_response 函數（新增硬碟儲存）
# =============================================
def GPT_response_direct(user_id, text):
    """直接呼叫 OpenAI 的版本 - 新增硬碟儲存"""
    monitor.increment()
    
    try:
        # 儲存使用者訊息到 Redis
        save_message_optimized(user_id, "user", text[:1500])
        
        # 取得或創建 thread（保持原邏輯）
        thread_id = redis_db.get(f"t:{user_id}")
        
        if thread_id:
            try:
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    limit=MAX_THREAD_MESSAGES + 2,
                    timeout=2.0
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
                        new_thread = client.beta.threads.create(messages=keep_messages)
                        thread_id = new_thread.id
                        redis_db.setex(f"t:{user_id}", 2400, thread_id)
                    else:
                        thread_id = None
                        
            except Exception as e:
                print(f"Thread cleanup error: {e}")
                thread_id = None
        
        if not thread_id:
            thread = client.beta.threads.create(messages=[{"role": "user", "content": text[:1500]}])
            thread_id = thread.id
            redis_db.setex(f"t:{user_id}", 2400, thread_id)
        else:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text[:1500],
                timeout=2.0
            )
        
        # 執行助理
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=6.0
        )
        
        # 等待完成
        start = time.time()
        while run.status != "completed":
            if time.time() - start > REQUEST_TIMEOUT:
                return "Processing taking longer than usual. Please try a shorter question."
            
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message[:100] if run.last_error else "Unknown"
                print(f"Run failed: {error_msg}")
                break
            
            time.sleep(0.6)
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
        
        # 儲存回覆到 Redis
        save_message_optimized(user_id, "assistant", ai_reply[:2000])
        
        # =============================================
        # 新增：儲存到硬碟（實驗數據）
        # =============================================
        if DISK_ENABLED:
            # 在背景執行，不影響回應速度
            threading.Thread(
                target=save_to_disk_background,
                args=(user_id,),
                daemon=True
            ).start()
        
        return ai_reply
        
    except openai.APITimeoutError:
        return "AI service timeout. Please try again."
        
    except Exception as e:
        print(f"GPT_response error: {e}")
        return "System error. Please try again."

def save_to_disk_background(user_id):
    """背景執行：儲存對話到硬碟"""
    try:
        # 等待一下，避免影響主要流程
        time.sleep(1)
        
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
        if messages_list:
            success = disk_storage.save_student_conversation(student_id, messages_list)
            if success:
                print(f"💾 Disk save successful for {student_id[:8]} ({len(messages_list)} messages)")
        
    except Exception as e:
        print(f"⚠️  Background disk save failed: {e}")

def GPT_response(user_id, text):
    """新的 GPT_response，使用隊列處理"""
    try:
        print(f"📨 Received request from {user_id[:8]}: {text[:30]}...")
        
        # 使用批處理器
        result = openai_processor.process(user_id, text)
        
        print(f"✅ Response ready for {user_id[:8]}")
        return result
        
    except Exception as e:
        print(f"❌ Error in queued GPT_response: {e}")
        return f"Processing error: {str(e)[:100]}"

# =============================================
# 8. LINE 處理（保持不變）
# =============================================
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
    
    # 顯示載入動畫
    send_loading(user_id)
    
    # 使用執行緒處理
    def process_in_thread():
        try:
            # 使用 GPT_response（會自動排隊）
            answer = GPT_response(user_id, user_msg)
            
            # 停止動畫
            stop_loading(user_id)
            
            # 檢查長度
            if len(answer) > 3000:
                answer = answer[:3000] + "\n\n[Message trimmed]"
            
            # 使用 push_message
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=answer)
            )
            
            print(f"📤 Sent reply to {user_id[:8]}")
            
        except Exception as e:
            print(f"Error in process_in_thread: {e}")
            try:
                stop_loading(user_id)
            except:
                pass
    
    # 啟動背景執行緒
    thread = threading.Thread(target=process_in_thread)
    thread.daemon = True
    thread.start()

# =============================================
# 9. 新增：硬碟管理端點（實驗數據）
# =============================================
@app.route("/disk/status", methods=['GET'])
def disk_status():
    """檢查硬碟狀態"""
    if not DISK_ENABLED:
        return jsonify({
            "status": "disabled",
            "message": "Disk storage not enabled"
        }), 200
    
    try:
        info = disk_storage.get_disk_info()
        
        if "error" in info:
            return jsonify(info), 500
        
        return jsonify({
            "status": "enabled",
            "disk_info": info,
            "timestamp": datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/disk/export", methods=['GET'])
def disk_export():
    """匯出實驗數據"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    if not DISK_ENABLED:
        return jsonify({"error": "Disk storage not enabled"}), 400
    
    try:
        format_type = request.args.get('format', 'json')
        
        export_path = disk_storage.export_all_data(format_type)
        
        if not export_path:
            return jsonify({"error": "Export failed"}), 500
        
        if format_type == 'csv':
            # 回傳 CSV 檔案下載
            with open(export_path, 'r', encoding='utf-8') as f:
                csv_content = f.read()
            
            response = app.response_class(
                response=csv_content,
                status=200,
                mimetype='text/csv',
                headers={
                    'Content-Disposition': 'attachment; filename=experiment_data.csv'
                }
            )
            return response
        
        else:
            # 回傳 JSON 數據
            with open(export_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
            
            return jsonify(json_data), 200
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/disk/students", methods=['GET'])
def list_students():
    """列出所有學生的對話檔案"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    if not DISK_ENABLED:
        return jsonify({"error": "Disk storage not enabled"}), 400
    
    try:
        files = disk_storage.get_all_student_files()
        
        return jsonify({
            "total_students": len(files),
            "students": files,
            "timestamp": datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/disk/student/<student_id>", methods=['GET'])
def get_student_data(student_id):
    """取得特定學生的對話數據"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    if not DISK_ENABLED:
        return jsonify({"error": "Disk storage not enabled"}), 400
    
    try:
        data = disk_storage.get_student_data(student_id)
        
        if not data:
            return jsonify({"error": "Student not found"}), 404
        
        return jsonify(data), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================
# 10. 原有的管理端點（保持不變）
# =============================================
@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        stats = monitor.get_stats()
        
        # 加入硬碟狀態
        disk_info = {}
        if DISK_ENABLED:
            disk_info = disk_storage.get_disk_info()
        
        return jsonify({
            "status": "healthy",
            "resources": stats,
            "disk_enabled": DISK_ENABLED,
            "disk_info": disk_info if DISK_ENABLED else None,
            "config": {
                "max_concurrent": MAX_CONCURRENT_REQUESTS,
                "max_thread_messages": MAX_THREAD_MESSAGES,
                "max_workers": MAX_WORKERS
            }
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/processor-stats", methods=['GET'])
def processor_stats():
    """查看處理器狀態"""
    stats = {
        "max_concurrent": openai_processor.max_concurrent,
        "total_requests": openai_processor.request_count,
        "current_semaphore_value": openai_processor.semaphore._value,
        "active_requests": openai_processor.max_concurrent - openai_processor.semaphore._value,
        "timestamp": datetime.now().isoformat()
    }
    return jsonify(stats)

@app.route("/export/conversations", methods=['GET'])
def export_conversations():
    """匯出 Redis 中的對話紀錄"""
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
            "data": all_data,
            "note": "From Redis (for OpenAI context)"
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test-simple", methods=['POST', 'GET'])
def test_simple():
    """測試端點"""
    try:
        if request.method == 'GET':
            return jsonify({
                "status": "ready",
                "disk_enabled": DISK_ENABLED,
                "message": "Use POST to test"
            }), 200
        
        data = request.json or {}
        user_id = data.get('user_id', f"test_{int(time.time())}")
        message = data.get('message', 'Hello, this is a test message')
        
        print(f"🧪 Test request from {user_id}: {message[:50]}...")
        
        start_time = time.time()
        response_text = GPT_response(user_id, message)
        duration = time.time() - start_time
        
        return jsonify({
            "success": True,
            "user_id": user_id,
            "response": response_text[:500],
            "response_length": len(response_text),
            "duration_seconds": round(duration, 2),
            "disk_saved": DISK_ENABLED,
            "timestamp": datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        print(f"❌ Test error: {e}")
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

# =============================================
# 11. 啟動程式
# =============================================
if __name__ == "__main__":
    print(f"""
    ========================================
    🚀 SICA TUTOR STARTING
    ========================================
    OpenAI Queue: {openai_processor.max_concurrent} concurrent
    Max Workers: {MAX_WORKERS}
    
    Storage:
    - Redis: For OpenAI context (fast)
    - Disk: {DISK_ENABLED} {f'at {disk_storage.mount_path}' if DISK_ENABLED else ''}
    
    Endpoints:
    - /health                : Health check
    - /disk/status          : Disk status
    - /disk/export          : Export experiment data
    - /export/conversations : Export Redis data
    - /test-simple          : Test endpoint
    ========================================
    """)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

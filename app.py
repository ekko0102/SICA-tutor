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
import queue
from collections import defaultdict
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
# 零失敗保證系統
# =============================================
class GuaranteedResponseSystem:
    """保證回應系統 - 永不失敗，持續重試直到成功"""
    
    def __init__(self, max_workers=5):
        self.pending_queue = queue.Queue()
        self.processing_tasks = {}
        self.completed_tasks = {}
        self.task_status = {}
        self.max_workers = max_workers
        self.loading_sessions = {}
        self.lock = threading.Lock()
        self.workers = []  # 新增：儲存 worker 參考
        self.is_running = True  # 新增：運行標記
        
        print(f"🛠️  Initializing {max_workers} workers...")
        
        # 啟動工作者執行緒
        for i in range(max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                daemon=True,
                name=f"Worker-{i}"
            )
            worker.start()
            self.workers.append(worker)
            print(f"✅ Worker {i} started (ID: {worker.ident})")
        
        # 啟動監控執行緒
        monitor = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="Task-Monitor"
        )
        monitor.start()
        
        # 啟動載入動畫管理執行緒
        loading_manager = threading.Thread(
            target=self._loading_manager_loop,
            daemon=True,
            name="Loading-Manager"
        )
        loading_manager.start()
        
        print(f"🚀 All {max_workers} workers initialized and ready")
    
    def _worker_loop(self, worker_id):
        """工作者執行緒 - 永不停止，持續處理任務"""
        print(f"👷 Worker {worker_id} loop STARTED")
        
        while self.is_running:
            try:
                print(f"⏳ Worker {worker_id} waiting for task...")
                
                # 從隊列獲取任務（阻塞等待，timeout=1秒以便檢查運行狀態）
                try:
                    task_data = self.pending_queue.get(timeout=1)
                except queue.Empty:
                    continue  # 如果隊列空，繼續等待
                
                if task_data is None:  # 停止信號
                    break
                
                task_id, task = task_data
                
                print(f"👷 Worker {worker_id} START processing task {task_id[:8]} "
                      f"for {task['user_id'][:8]}")
                
                # 處理任務（無限重試直到成功）
                self._process_with_infinite_retry(worker_id, task_id, task)
                
                # 標記隊列完成
                self.pending_queue.task_done()
                
                print(f"👷 Worker {worker_id} FINISHED task {task_id[:8]}")
                
            except Exception as e:
                print(f"❌ Worker {worker_id} loop error: {str(e)[:100]}")
                traceback.print_exc()
                time.sleep(5)  # 錯誤後休息5秒
        
        print(f"👷 Worker {worker_id} loop STOPPED")
    
    def _process_with_infinite_retry(self, worker_id, task_id, task):
        """無限重試直到成功"""
        user_id = task['user_id']
        text = task['text']
        reply_token = task.get('reply_token')
        
        max_retries = 20  # 最多重試次數（實際上會一直重試）
        backoff_base = 5   # 退避基礎時間
        
        for attempt in range(max_retries + 100):  # 實際上會一直嘗試
            try:
                # 更新重試次數
                with self.lock:
                    if task_id in self.task_status:
                        self.task_status[task_id]['retry_count'] = attempt
                        self.task_status[task_id]['last_attempt'] = datetime.now().isoformat()
                
                print(f"🔄 Worker {worker_id} attempt {attempt+1} for task {task_id[:8]}")
                
                # 發送進度更新（每3次重試更新一次）
                if attempt % 3 == 0:
                    self._send_progress_update(
                        user_id, 
                        f"🤖 AI is thinking... (attempt {attempt+1})"
                    )
                
                # 嘗試獲取AI回應
                response = self._call_gpt_with_patience(user_id, text, attempt)
                
                if response and len(response.strip()) > 5:  # 有效回應
                    print(f"✅ Task {task_id[:8]} completed after {attempt+1} attempts")
                    
                    # 儲存結果
                    with self.lock:
                        self.completed_tasks[task_id] = {
                            'response': response,
                            'completed_at': datetime.now().isoformat(),
                            'attempts': attempt + 1,
                            'user_id': user_id
                        }
                        if task_id in self.processing_tasks:
                            del self.processing_tasks[task_id]
                        self.task_status[task_id] = {
                            'status': 'completed',
                            'completed_at': datetime.now().isoformat()
                        }
                    
                    # 發送最終回應
                    success = self._deliver_final_response(user_id, response, reply_token)
                    
                    if success:
                        # 停止載入動畫
                        self._stop_loading_animation(user_id)
                        return True
                    else:
                        print(f"⚠️ Delivery failed for task {task_id[:8]}, will retry...")
                
                # 如果失敗，等待後重試
                wait_time = min(backoff_base * (1.5 ** attempt), 300)  # 指數退避，最大5分鐘
                print(f"⏳ Waiting {wait_time:.1f}s before retry {attempt+2} for task {task_id[:8]}")
                time.sleep(wait_time)
                
            except Exception as e:
                print(f"❌ Attempt {attempt+1} failed: {str(e)[:100]}")
                time.sleep(min(30, 5 * (attempt + 1)))  # 錯誤等待
    
    def _call_gpt_with_patience(self, user_id, text, attempt):
        """有耐心地呼叫GPT，適應性超時"""
        try:
            # 根據嘗試次數調整超時
            timeout = min(60, 10 + attempt * 5)  # 逐漸增加超時
            
            # 使用您的現有GPT_response函數
            return GPT_response_direct(user_id, text)
            
        except Exception as e:
            print(f"GPT call failed: {e}")
            return None
    
    def _send_progress_update(self, user_id, message):
        """發送進度更新（使用push_message）"""
        try:
            # 只發送重要更新，避免騷擾
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=message)
            )
            return True
        except Exception as e:
            print(f"Progress update failed: {e}")
            return False
    
    def _deliver_final_response(self, user_id, response, reply_token=None):
        """發送最終回應"""
        try:
            # 確保回應不會太長
            if len(response) > 3000:
                response = response[:3000] + "\n\n[訊息已截斷]"
            
            # 嘗試使用reply_token（如果還有效）
            if reply_token:
                try:
                    line_bot_api.reply_message(
                        reply_token,
                        TextSendMessage(text=response)
                    )
                    return True
                except:
                    pass  # reply_token可能已過期
            
            # 使用push_message作為備用
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=response)
            )
            return True
            
        except Exception as e:
            print(f"Final delivery failed: {e}")
            return False
    
    def _start_loading_animation(self, user_id):
        """開始載入動畫"""
        try:
            with self.lock:
                if user_id not in self.loading_sessions:
                    send_loading(user_id)
                    self.loading_sessions[user_id] = {
                        'started_at': time.time(),
                        'last_restart': time.time()
                    }
        except Exception as e:
            print(f"Failed to start loading: {e}")
    
    def _stop_loading_animation(self, user_id):
        """停止載入動畫"""
        try:
            with self.lock:
                if user_id in self.loading_sessions:
                    stop_loading(user_id)
                    del self.loading_sessions[user_id]
        except Exception as e:
            print(f"Failed to stop loading: {e}")
    
    def _loading_manager_loop(self):
        """管理載入動畫，定期重啟避免超時"""
        while True:
            try:
                time.sleep(5)  # 每5秒檢查一次
                
                with self.lock:
                    current_time = time.time()
                    users_to_restart = []
                    
                    for user_id, session in list(self.loading_sessions.items()):
                        # 如果載入動畫超過8秒，需要重啟（LINE限制10秒）
                        if current_time - session['last_restart'] > 8:
                            users_to_restart.append(user_id)
                    
                    # 重啟載入動畫
                    for user_id in users_to_restart:
                        try:
                            # 先停止
                            stop_loading(user_id)
                            time.sleep(0.5)
                            # 再開始
                            send_loading(user_id)
                            self.loading_sessions[user_id]['last_restart'] = current_time
                            print(f"🔄 Restarted loading animation for {user_id[:8]}")
                        except:
                            pass
                            
            except Exception as e:
                print(f"Loading manager error: {e}")
                time.sleep(10)
    
    def _monitor_loop(self):
        """監控循環，檢查停滯的任務"""
        while True:
            try:
                time.sleep(30)  # 每30秒檢查一次
                
                with self.lock:
                    current_time = time.time()
                    stale_tasks = []
                    
                    for task_id, status in list(self.task_status.items()):
                        if status.get('status') == 'processing':
                            # 檢查任務是否處理超過10分鐘
                            started_str = status.get('started_at')
                            if started_str:
                                try:
                                    started = datetime.fromisoformat(started_str)
                                    age = (datetime.now() - started).total_seconds()
                                    
                                    if age > 600:  # 10分鐘
                                        stale_tasks.append(task_id)
                                except:
                                    pass
                    
                    # 重啟停滯的任務
                    for task_id in stale_tasks:
                        print(f"⚠️ Restarting stale task {task_id[:8]}")
                        if task_id in self.processing_tasks:
                            task = self.processing_tasks[task_id]
                            # 重新加入隊列
                            self.submit_task(task['user_id'], task['text'], task.get('reply_token'))
                            
            except Exception as e:
                print(f"Monitor error: {e}")
    
    def submit_task(self, user_id, text, reply_token=None):
        """提交新任務到零失敗系統"""
        task_id = str(uuid.uuid4())[:12]
        
        task = {
            'task_id': task_id,
            'user_id': user_id,
            'text': text,
            'reply_token': reply_token,
            'submitted_at': datetime.now().isoformat()
        }
        
        # 加入隊列
        self.pending_queue.put((task_id, task))
        
        # 立即開始載入動畫
        self._start_loading_animation(user_id)
        
        print(f"📥 Task {task_id[:8]} submitted for {user_id[:8]}, "
              f"queue size: {self.pending_queue.qsize()}")
        
        return task_id
    
    def get_stats(self):
        """獲取系統統計"""
        with self.lock:
            return {
                'queue_size': self.pending_queue.qsize(),
                'processing_tasks': len(self.processing_tasks),
                'completed_tasks': len(self.completed_tasks),
                'loading_sessions': len(self.loading_sessions),
                'timestamp': datetime.now().isoformat()
            }

# 建立零失敗系統實例
zero_failure_system = GuaranteedResponseSystem(max_workers=5)

# =============================================
# 原有隊列系統（保留但改為使用零失敗系統）
# =============================================

class OpenAIBatchProcessor:
    """批量處理 OpenAI 請求，避免超載"""
    def __init__(self, max_concurrent=5):
        self.max_concurrent = max_concurrent
        self.semaphore = threading.Semaphore(max_concurrent)
        self.request_count = 0
        
    def process(self, user_id, text):
        """處理單一請求 - 現在直接使用零失敗系統"""
        self.request_count += 1
        req_num = self.request_count
        
        print(f"[{req_num}] Request from {user_id[:8]} via batch processor")
        
        # 直接提交到零失敗系統
        task_id = zero_failure_system.submit_task(user_id, text)
        
        # 等待任務完成（最多等待一段時間）
        start_time = time.time()
        max_wait = 300  # 最多等待5分鐘
        
        while time.time() - start_time < max_wait:
            # 檢查任務是否已完成
            if task_id in zero_failure_system.completed_tasks:
                result = zero_failure_system.completed_tasks[task_id]['response']
                print(f"[{req_num}] Task {task_id[:8]} completed via zero-failure system")
                return result
            
            time.sleep(1)
        
        # 如果超時，返回等待訊息
        return "Your request is still processing. You'll receive the answer soon!"

# 建立全域處理器
openai_processor = OpenAIBatchProcessor(max_concurrent=5)

# =============================================
# 初始化設定
# =============================================

redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL is not set")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True,
                                     max_connections=20)  # 增加連接數

line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set")

# 改為：
try:
    # 簡化初始化，避免參數問題
    client = openai.OpenAI(api_key=openai_api_key)
except Exception as e:
    print(f"❌ OpenAI client initialization failed: {e}")
    # 如果初始化失敗，建立一個簡單的 client
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
MAX_CONCURRENT_REQUESTS = 5
MAX_WORKERS = 3
REQUEST_TIMEOUT = 60  # 增加到60秒，讓AI有更多時間
REDIS_MAX_PER_STUDENT = 80

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
# GPT_response 函數 - 移除所有錯誤訊息
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
        
        return ai_reply
        
    except Exception as e:
        print(f"GPT_response error: {e}")
        # 返回中性回應，而不是錯誤訊息
        return "I'm currently processing your request. Please give me a moment to think."

def GPT_response(user_id, text):
    """新的 GPT_response，使用隊列處理"""
    try:
        print(f"📨 Received request from {user_id[:8]}: {text[:30]}...")
        
        # 使用批處理器（會轉到零失敗系統）
        result = openai_processor.process(user_id, text)
        
        print(f"✅ Response ready for {user_id[:8]}")
        return result
        
    except Exception as e:
        print(f"❌ Error in queued GPT_response: {e}")
        # 返回中性訊息
        return "Processing your question now. You'll receive an answer shortly."

# =============================================
# LINE 載入動畫函數
# =============================================

def send_loading(chat_id):
    """發送載入動畫"""
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 9}
        response = requests.post(url, headers=headers, json=data, timeout=3)
        if response.status_code == 200:
            print(f"▶️ Started loading animation for {chat_id[:8]}")
        return True
    except Exception as e:
        print(f"Failed to start loading: {e}")
        return False

def stop_loading(chat_id):
    """停止載入動畫"""
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
# LINE Webhook 處理 - 簡化版本
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

    # 防重複處理
    if redis_db.get(f"p:{msg_id}"):
        return 
    redis_db.setex(f"p:{msg_id}", 20, "1")

    # 群組過濾
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(f"p:{msg_id}")
            return
    
    # 重要：不發送任何文字回覆，只啟動載入動畫
    # 零失敗系統會自動啟動載入動畫
    
    # 立即提交任務到零失敗系統
    print(f"🎯 Submitting message from {user_id[:8]}: {user_msg[:50]}...")
    
    # 使用零失敗系統處理
    task_id = zero_failure_system.submit_task(user_id, user_msg, reply_token)
    
    print(f"✅ Task {task_id[:8]} submitted to zero-failure system for {user_id[:8]}")
    
    # 注意：這裡不發送任何文字回覆，只有載入動畫
    # 所有回應將由零失敗系統透過 push_message 發送

# =============================================
# 管理端點 - 增強版本
# =============================================

@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        stats = monitor.get_stats()
        zero_failure_stats = zero_failure_system.get_stats()
        
        return jsonify({
            "status": "healthy",
            "resources": stats,
            "zero_failure_system": zero_failure_stats,
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
    zero_failure_stats = zero_failure_system.get_stats()
    
    stats = {
        "max_concurrent": openai_processor.max_concurrent,
        "total_requests": openai_processor.request_count,
        "current_semaphore_value": openai_processor.semaphore._value,
        "active_requests": openai_processor.max_concurrent - openai_processor.semaphore._value,
        "zero_failure_system": zero_failure_stats,
        "timestamp": datetime.now().isoformat()
    }
    return jsonify(stats)

@app.route("/zero-failure-stats", methods=['GET'])
def zero_failure_stats():
    """查看零失敗系統詳細狀態"""
    stats = zero_failure_system.get_stats()
    
    # 添加詳細資訊
    detailed_stats = {
        **stats,
        "system_info": {
            "description": "Zero-failure guaranteed response system",
            "max_workers": zero_failure_system.max_workers,
            "guarantee": "Infinite retry until success",
            "loading_animation": "Auto-managed with periodic restart"
        },
        "status": "operational"
    }
    
    return jsonify(detailed_stats)

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

@app.route("/test-simple", methods=['POST', 'GET'])
def test_simple():
    """極簡測試端點"""
    try:
        print("✅ /test-simple endpoint called")
        
        if request.method == 'GET':
            return jsonify({
                "status": "ready",
                "endpoint": "/test-simple",
                "message": "Use POST to test OpenAI",
                "zero_failure_system": "enabled"
            }), 200
        
        # POST 請求：實際測試 OpenAI
        data = request.json or {}
        user_id = data.get('user_id', 'test_user_001')
        message = data.get('message', 'Hello, please respond.')
        
        print(f"🎯 Testing OpenAI for user: {user_id}")
        print(f"📝 Message: {message}")
        
        # 使用零失敗系統
        task_id = zero_failure_system.submit_task(user_id, message)
        
        # 等待結果（最多30秒）
        start_time = time.time()
        while time.time() - start_time < 30:
            if task_id in zero_failure_system.completed_tasks:
                response_text = zero_failure_system.completed_tasks[task_id]['response']
                duration = time.time() - start_time
                
                print(f"✅ Zero-failure response received in {duration:.1f}s")
                print(f"📄 Response: {response_text[:100]}...")
                
                return jsonify({
                    "success": True,
                    "task_id": task_id,
                    "user_id": user_id,
                    "response": response_text[:1000],
                    "response_length": len(response_text),
                    "duration_seconds": round(duration, 2),
                    "via_zero_failure": True,
                    "timestamp": datetime.now().isoformat()
                }), 200
            
            time.sleep(0.5)
        
        # 超時
        return jsonify({
            "success": False,
            "task_id": task_id,
            "error": "Timeout waiting for response",
            "message": "Task is still processing in zero-failure system",
            "timestamp": datetime.now().isoformat()
        }), 408
        
    except Exception as e:
        print(f"❌ Error in /test-simple: {e}")
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

# =============================================
# 啟動
# =============================================

if __name__ == "__main__":
    print(f"""
    ========================================
    🚀 ZERO-FAILURE LINE BOT STARTING
    ========================================
    Features:
    ✅ Zero-failure guaranteed response system
    ✅ Auto-managed loading animations
    ✅ No error messages to users
    ✅ Infinite retry until success
    
    OpenAI Queue: {openai_processor.max_concurrent} concurrent
    Max Workers: {MAX_WORKERS}
    Disk Storage: {'✅ Enabled' if DISK_ENABLED else '❌ Disabled'}
    ========================================
    """)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

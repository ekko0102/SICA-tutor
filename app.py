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

app = Flask(__name__)

# --- 1. 初始化設定 ---
redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL is not set")
redis_db = redis.StrictRedis.from_url(redis_url, decode_responses=True)

line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set")

client = openai.OpenAI(api_key=openai_api_key, timeout=30.0)
ASSISTANT_ID = os.getenv('ASSISTANT_ID') 

# --- 2. 簡化設定 ---
MAX_WAIT_TIME = 20
MAX_THREAD_MESSAGES = 10

# --- 3. 資料儲存 ---
def save_message(user_id, role, content):
    try:
        student_id = hashlib.sha256(user_id.encode()).hexdigest()[:12]
        timestamp = datetime.now().isoformat()
        
        message_data = {
            "student_id": student_id,
            "role": role,
            "content": content[:3000],
            "timestamp": timestamp
        }
        
        redis_db.rpush(f"student_history:{student_id}", json.dumps(message_data))
        
        # 限制長度
        if redis_db.llen(f"student_history:{student_id}") > 100:
            redis_db.ltrim(f"student_history:{student_id}", -100, -1)
        
        return True
    except Exception as e:
        print(f"Save error: {e}")
        return False

# --- 4. GPT_response 函數 ---
def GPT_response(user_id, text):
    try:
        # 儲存使用者訊息
        save_message(user_id, "user", text)
        
        # 取得或創建 thread
        thread_id = redis_db.get(f"thread_id:{user_id}")
        
        # 如果對話太長，清理
        if thread_id:
            try:
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    limit=MAX_THREAD_MESSAGES + 3,
                    timeout=3.0
                )
                
                if len(messages.data) > MAX_THREAD_MESSAGES:
                    print(f"Cleaning long thread for {user_id[:8]}")
                    # 只保留最近3條
                    recent = []
                    for msg in messages.data[:3]:
                        if hasattr(msg, 'content') and msg.content:
                            recent.append({
                                "role": msg.role,
                                "content": msg.content[0].text.value[:300]
                            })
                    
                    if recent:
                        new_thread = client.beta.threads.create(
                            messages=recent[::-1]
                        )
                        thread_id = new_thread.id
                    else:
                        thread_id = None
                        
            except:
                thread_id = None
        
        # 創建新 thread
        if not thread_id:
            thread = client.beta.threads.create(
                messages=[{"role": "user", "content": text}]
            )
            thread_id = thread.id
            redis_db.setex(f"thread_id:{user_id}", 1800, thread_id)
        
        # 加入訊息到 thread
        else:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text,
                timeout=5.0
            )
        
        # 執行助理
        run = client.beta.threads.runs.create(
            thread_id=thread_id, 
            assistant_id=ASSISTANT_ID,
            timeout=10.0
        )
        
        # 等待完成
        start = time.time()
        while run.status != "completed":
            if time.time() - start > MAX_WAIT_TIME:
                return "Taking longer than expected. Please try again."
            
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message if run.last_error else "Unknown"
                print(f"Run failed: {error_msg}")
                break
            
            time.sleep(0.8)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id,
                timeout=5.0
            )
        
        # 取得回覆
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1,
            timeout=5.0
        )
        
        if not messages.data or not messages.data[0].content:
            return "No response received."
            
        ai_reply = messages.data[0].content[0].text.value
        
        # 儲存回覆
        save_message(user_id, "assistant", ai_reply)
        
        return ai_reply
        
    except Exception as e:
        print(f"GPT_response error: {e}")
        return "System error. Please try again."

# --- 5. LINE 動畫函數 ---
def send_loading(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 15}
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

# --- 6. LINE Webhook ---
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

    # 防重複
    if redis_db.get(f"proc:{msg_id}"):
        return 
    redis_db.setex(f"proc:{msg_id}", 30, "true")

    # 群組過濾
    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(f"proc:{msg_id}")
            return
    
    try:
        # 顯示動畫
        send_loading(user_id)
        
        # 取得 AI 回應（同步處理！）
        answer = GPT_response(user_id, user_msg)
        
        # 停止動畫
        stop_loading(user_id)
        
        # 檢查長度
        if len(answer) > 3500:
            answer = answer[:3500] + "\n\n[Trimmed]"
        
        # 回覆訊息
        line_bot_api.reply_message(
            reply_token, 
            TextSendMessage(text=answer)
        )
        
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        
        try:
            stop_loading(user_id)
        except:
            pass
        
        try:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="Error. Please try again.")
            )
        except:
            pass
    finally:
        try:
            redis_db.delete(f"proc:{msg_id}")
        except:
            pass

# --- 7. 其他端點 ---
@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        return "OK", 200
    except:
        return "Redis failed", 500

@app.route("/export/conversations", methods=['GET'])
def export_conversations():
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return jsonify({"error": "Unauthorized"}), 401
    
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
                    "messages": student_msgs[:50]
                })
        
        if cursor == '0':
            break
    
    return jsonify({
        "export_time": datetime.now().isoformat(),
        "total_students": len(all_data),
        "data": all_data
    })

if __name__ == "__main__":
    print("Starting English Tutor Bot...")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

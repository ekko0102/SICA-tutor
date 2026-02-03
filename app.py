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
import json
from datetime import datetime
import hashlib

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

client = openai.OpenAI(api_key=openai_api_key)
ASSISTANT_ID = os.getenv('ASSISTANT_ID') 

# --- 2. 簡化資料儲存功能 ---

def generate_anonymous_id(user_id):
    """生成匿名學生ID"""
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]

def save_message(user_id, role, content, metadata=None):
    """儲存單一訊息"""
    try:
        student_id = generate_anonymous_id(user_id)
        timestamp = datetime.now().isoformat()
        
        message_data = {
            "student_id": student_id,
            "role": role,  # 'user' 或 'assistant'
            "content": content,
            "timestamp": timestamp,
            "user_id_hash": student_id  # 只用匿名ID
        }
        
        # 添加到學生的對話列表
        redis_db.rpush(
            f"student_history:{student_id}", 
            json.dumps(message_data)
        )
        
        # 添加到全域時間軸（可選）
        redis_db.rpush(
            "all_conversations",
            json.dumps({**message_data, "original_user": user_id[:8]})
        )
        
        # 更新最後活動時間
        redis_db.hset(
            f"student:{student_id}", 
            "last_active", 
            timestamp
        )
        
        return True
    except Exception as e:
        print(f"Error saving message: {e}")
        return False

# --- 3. GPT_response 函數（只添加儲存功能）---

def GPT_response(user_id, text):
    try:
        # 儲存使用者訊息
        save_message(user_id, "user", text)
        
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
            
            run = client.beta.threads.runs.create(
                thread_id=thread_id, 
                assistant_id=ASSISTANT_ID
            )
        else:
            # 檢查是否有未完成的 Run
            active_runs = client.beta.threads.runs.list(thread_id=thread_id).data
            
            while active_runs and any(
                run.status not in ["completed", "failed", "cancelled", "expired"] 
                for run in active_runs
            ):
                if time.time() - start_time > max_wait_time:
                    for run in active_runs:
                        if run.status not in ["completed", "failed", "cancelled", "expired"]:
                            try:
                                client.beta.threads.runs.cancel(
                                    thread_id=thread_id,
                                    run_id=run.id
                                )
                            except:
                                pass
                    break
                time.sleep(2)
                active_runs = client.beta.threads.runs.list(thread_id=thread_id).data

            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=text
            )
            
            run = client.beta.threads.runs.create(
                thread_id=thread_id, 
                assistant_id=ASSISTANT_ID
            )

        # 等待完成
        while run.status != "completed":
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id
            )
            if run.status in ["failed", "cancelled", "expired"]:
                error_msg = run.last_error.message if run.last_error else "Unknown error"
                raise Exception(f"OpenAI Run failed: {run.status}, Error: {error_msg}")
        
        # 抓取回覆
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1
        )
        
        if not messages.data or not messages.data[0].content:
            raise Exception("OpenAI returned empty message")
            
        ai_reply = messages.data[0].content[0].text.value
        
        # 儲存AI回覆
        save_message(user_id, "assistant", ai_reply)
        
        return ai_reply

    except Exception as e:
        print(f"GPT_response Error: {e}")
        traceback.print_exc()
        return "System is busy, please try again later."

# --- 4. 資料匯出功能（最簡單版本）---

@app.route("/export/conversations", methods=['GET'])
def export_conversations():
    """匯出所有對話資料（需簡單認證）"""
    # 簡單認證：檢查 query 參數
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return json.dumps({"error": "Unauthorized"}), 401
    
    # 取得所有學生的ID
    student_keys = redis_db.keys("student_history:*")
    all_data = []
    
    for key in student_keys:
        student_id = key.split(":")[1]
        messages = redis_db.lrange(key, 0, -1)  # 取得所有訊息
        
        student_messages = []
        for msg_json in messages:
            msg = json.loads(msg_json)
            student_messages.append(msg)
        
        if student_messages:
            all_data.append({
                "student_id": student_id,
                "total_messages": len(student_messages),
                "messages": student_messages
            })
    
    return json.dumps({
        "export_time": datetime.now().isoformat(),
        "total_students": len(all_data),
        "data": all_data
    }, ensure_ascii=False, indent=2)

@app.route("/export/raw", methods=['GET'])
def export_raw():
    """匯出原始 Redis 資料"""
    secret = request.args.get('secret')
    if secret != os.getenv('EXPORT_SECRET', 'default123'):
        return json.dumps({"error": "Unauthorized"}), 401
    
    # 取得所有對話
    all_conversations = redis_db.lrange("all_conversations", 0, -1)
    
    conversations = []
    for conv_json in all_conversations:
        conversations.append(json.loads(conv_json))
    
    return json.dumps({
        "total_conversations": len(conversations),
        "conversations": conversations
    }, ensure_ascii=False, indent=2)

# --- 5. 原有 LINE 功能保持不變 ---

def send_loading_animation(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/start'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id, "loadingSeconds": 10}
        response = requests.post(url, headers=headers, json=data, timeout=3)
        if response.status_code != 200:
            print(f"Loading animation failed: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"Error sending loading animation: {e}")

def stop_loading_animation(chat_id):
    try:
        url = 'https://api.line.me/v2/bot/chat/loading/stop'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
        }
        data = {"chatId": chat_id}
        response = requests.post(url, headers=headers, json=data, timeout=3)
        if response.status_code != 200:
            print(f"Stop animation failed: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"Error stopping loading animation: {e}")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

@app.route("/health", methods=['GET'])
def health_check():
    try:
        redis_db.ping()
        return "OK", 200
    except:
        return "Redis connection failed", 500

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg_id = event.message.id
    user_msg = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token

    if redis_db.get(f"proc:{msg_id}"):
        return 
    redis_db.set(f"proc:{msg_id}", "true", ex=60)

    if event.source.type == 'group':
        if 'bot' not in user_msg.lower() and '@AI' not in user_msg:
            redis_db.delete(f"proc:{msg_id}")
            return

    try:
        send_loading_animation(user_id)
        answer = GPT_response(user_id, user_msg)
        stop_loading_animation(user_id)
        
        if len(answer) > 4500:
            answer = answer[:4500] + "\n\n(Message too long, truncated)"
        
        line_bot_api.reply_message(
            reply_token, 
            TextSendMessage(text=answer)
        )
        
    except Exception as e:
        print(f"handle_message error: {e}")
        traceback.print_exc()
        
        try:
            stop_loading_animation(user_id)
        except:
            pass
        
        try:
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text="Error processing your message. Please check API key or quota.")
            )
        except Exception as reply_error:
            print(f"Failed to send error message: {reply_error}")
    finally:
        try:
            redis_db.delete(f"proc:{msg_id}")
        except:
            pass

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

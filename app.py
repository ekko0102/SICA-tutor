import os
import time
from flask import Flask, request, abort
from openai import OpenAI
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 1. 從環境變數讀取金鑰 (請在 Render 後台設定)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
ASSISTANT_ID = os.getenv('ASSISTANT_ID')

@app.route("/callback", methods=['POST'])
def callback():
    # 驗證 LINE 傳來的簽章
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    
    try:
        # 2. 建立 Thread (此版本每次對話開新 Thread，若要記憶對話需另存 Thread ID)
        thread = client.beta.threads.create()

        # 3. 將使用者訊息加入 Thread
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=user_msg
        )

        # 4. 執行 Assistant
        run = client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        # 5. 等待回覆 (Polling)
        while True:
            run_status = client.beta.threads.runs.retrieve(
                thread_id=thread.id, 
                run_id=run.id
            )
            if run_status.status == 'completed':
                break
            elif run_status.status in ['failed', 'cancelled', 'expired']:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="抱歉，AI 處理時發生錯誤。")
                )
                return
            time.sleep(1) # 等待 1 秒後再次檢查

        # 6. 抓取助理的回覆內容
        messages = client.beta.threads.messages.list(thread_id=thread.id)
        # messages.data[0] 是最新的一則訊息
        ai_reply = messages.data[0].content[0].text.value

        # 7. 回傳訊息給使用者
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=ai_reply)
        )

    except Exception as e:
        print(f"Error: {e}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="系統繁忙中，請稍後再試。")
        )

if __name__ == "__main__":
    # Render 會自動分配 PORT，若本機測試則預設 5000
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

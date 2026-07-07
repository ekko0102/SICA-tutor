# disk_config.py
import os
import json
from datetime import datetime

class DiskStorage:
    def __init__(self, base_path="/data"):
        self.base_path = base_path
        os.makedirs(base_path, exist_ok=True)
        print(f"📁 Disk storage initialized at: {base_path}")
    
    def save_message(self, user_id, role, content, timestamp=None):
        """儲存單一訊息到硬碟"""
        try:
            if timestamp is None:
                timestamp = datetime.now()
            
            # 建立使用者目錄
            user_dir = os.path.join(self.base_path, "users", user_id)
            os.makedirs(user_dir, exist_ok=True)
            
            # 建立每日日誌檔案
            date_str = timestamp.strftime("%Y-%m-%d")
            log_file = os.path.join(user_dir, f"{date_str}.json")
            
            # 訊息資料
            message_data = {
                "timestamp": timestamp.isoformat(),
                "role": role,
                "content": content[:1000]
            }
            
            # 讀取或建立檔案
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    try:
                        data = json.load(f)
                    except:
                        data = []
            else:
                data = []
            
            # 添加新訊息
            data.append(message_data)
            
            # 限制每個檔案的最大訊息數
            if len(data) > 100:
                data = data[-100:]
            
            # 寫回檔案
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            return True
            
        except Exception as e:
            print(f"❌ Disk save error: {e}")
            return False
    
    def save_student_conversation(self, student_id, messages_list):
        """儲存學生完整對話到硬碟"""
        try:
            if not messages_list:
                return False
            
            # 建立使用者目錄
            user_dir = os.path.join(self.base_path, "users", student_id)
            os.makedirs(user_dir, exist_ok=True)
            
            # 按日期分組訊息
            messages_by_date = {}
            for msg in messages_list:
                try:
                    # 從時間戳解析日期
                    if "t" in msg:  # Redis 格式
                        timestamp_str = msg["t"]
                        date_str = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S").strftime("%Y-%m-%d")
                    elif "timestamp" in msg:  # 標準格式
                        timestamp_str = msg["timestamp"]
                        date_str = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).strftime("%Y-%m-%d")
                    else:
                        date_str = datetime.now().strftime("%Y-%m-%d")
                except:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                
                if date_str not in messages_by_date:
                    messages_by_date[date_str] = []
                
                # 標準化訊息格式
                formatted_msg = {
                    "timestamp": msg.get("timestamp") or msg.get("t") or datetime.now().isoformat(),
                    "role": msg.get("role") or ("user" if msg.get("r") == "u" else "assistant"),
                    "content": msg.get("content") or msg.get("c") or ""
                }
                messages_by_date[date_str].append(formatted_msg)
            
            # 儲存每個日期的檔案
            for date_str, msgs in messages_by_date.items():
                log_file = os.path.join(user_dir, f"{date_str}.json")
                
                # 讀取現有資料或建立新檔案
                if os.path.exists(log_file):
                    with open(log_file, "r", encoding="utf-8") as f:
                        try:
                            existing_data = json.load(f)
                        except:
                            existing_data = []
                else:
                    existing_data = []
                
                # 合併並去重（基於時間戳）
                existing_timestamps = {msg["timestamp"] for msg in existing_data}
                new_msgs = [msg for msg in msgs if msg["timestamp"] not in existing_timestamps]
                
                # 合併資料
                combined_data = existing_data + new_msgs
                
                # 按時間排序
                combined_data.sort(key=lambda x: x["timestamp"])
                
                # 限制每個檔案的最大訊息數
                if len(combined_data) > 100:
                    combined_data = combined_data[-100:]
                
                # 寫回檔案
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(combined_data, f, ensure_ascii=False, indent=2)
            
            return True
            
        except Exception as e:
            print(f"❌ Save student conversation error: {e}")
            return False
    
    def get_user_conversations(self, user_id, date_str=None):
        """取得使用者的對話紀錄"""
        try:
            user_dir = os.path.join(self.base_path, "users", user_id)
            
            if not os.path.exists(user_dir):
                return []
            
            conversations = []
            
            if date_str:
                # 取得特定日期的紀錄
                log_file = os.path.join(user_dir, f"{date_str}.json")
                if os.path.exists(log_file):
                    with open(log_file, "r", encoding="utf-8") as f:
                        conversations = json.load(f)
            else:
                # 取得所有紀錄
                for filename in sorted(os.listdir(user_dir)):
                    if filename.endswith('.json'):
                        log_file = os.path.join(user_dir, filename)
                        try:
                            with open(log_file, "r", encoding="utf-8") as f:
                                day_conversations = json.load(f)
                                conversations.extend(day_conversations)
                        except:
                            continue
            
            # 按時間排序
            conversations.sort(key=lambda x: x.get("timestamp", ""))
            
            return conversations
            
        except Exception as e:
            print(f"❌ Disk read error: {e}")
            return []
    
    def get_all_users(self):
        """取得所有使用者清單"""
        try:
            users_dir = os.path.join(self.base_path, "users")
            if not os.path.exists(users_dir):
                return []
            
            users = []
            for user_id in os.listdir(users_dir):
                user_dir = os.path.join(users_dir, user_id)
                if os.path.isdir(user_dir):
                    users.append(user_id)
            
            return users
            
        except Exception as e:
            print(f"❌ Get users error: {e}")
            return []
    
    def export_all_data(self):
        """匯出所有資料"""
        try:
            users_dir = os.path.join(self.base_path, "users")
            all_data = {}
            
            for user_id in os.listdir(users_dir):
                user_dir = os.path.join(users_dir, user_id)
                if os.path.isdir(user_dir):
                    conversations = self.get_user_conversations(user_id)
                    if conversations:
                        all_data[user_id] = {
                            "total_messages": len(conversations),
                            "conversations": conversations  # 最後200條
                        }
            
            return all_data
            
        except Exception as e:
            print(f"❌ Export error: {e}")
            return {}

# 建立全域實例
disk_storage = DiskStorage()

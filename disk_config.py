# disk_config.py
import os
import json
from pathlib import Path
from datetime import datetime

class SimpleDiskStorage:
    """簡單的硬碟儲存 - 只儲存實驗數據"""
    
    def __init__(self, mount_path="/data"):
        self.mount_path = Path(mount_path)
        
        # 建立實驗數據目錄
        self.experiment_dir = self.mount_path / "experiment_data"
        self.experiment_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"💾 Disk storage ready at: {self.experiment_dir}")
    
    def save_student_conversation(self, student_id, messages):
        """
        儲存單一學生的對話歷史
        student_id: 學生匿名ID
        messages: 完整的對話列表
        """
        try:
            # 建立學生檔案
            filename = f"{student_id}.json"
            filepath = self.experiment_dir / filename
            
            # 準備要儲存的數據
            save_data = {
                "student_id": student_id,
                "total_messages": len(messages),
                "messages": messages,
                "saved_at": datetime.now().isoformat(),
                "experiment_date": datetime.now().strftime("%Y-%m-%d")
            }
            
            # 寫入檔案
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
            
            print(f"💾 Saved {len(messages)} messages for student: {student_id[:8]}")
            return True
            
        except Exception as e:
            print(f"❌ Failed to save to disk: {e}")
            return False
    
    def get_all_student_files(self):
        """取得所有學生的檔案列表"""
        try:
            files = []
            for filepath in self.experiment_dir.glob("*.json"):
                stat = filepath.stat()
                files.append({
                    "filename": filepath.name,
                    "student_id": filepath.stem,
                    "size_kb": round(stat.st_size / 1024, 2),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
            
            return files
            
        except Exception as e:
            print(f"❌ Failed to list files: {e}")
            return []
    
    def get_student_data(self, student_id):
        """取得特定學生的數據"""
        try:
            filepath = self.experiment_dir / f"{student_id}.json"
            
            if not filepath.exists():
                return None
            
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return data
            
        except Exception as e:
            print(f"❌ Failed to load student data: {e}")
            return None
    
    def export_all_data(self, output_format="json"):
        """匯出所有數據"""
        try:
            all_data = []
            total_messages = 0
            
            # 讀取所有檔案
            for filepath in self.experiment_dir.glob("*.json"):
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    all_data.append(data)
                    total_messages += data.get("total_messages", 0)
            
            # 建立匯出數據
            export_data = {
                "export_time": datetime.now().isoformat(),
                "total_students": len(all_data),
                "total_messages": total_messages,
                "experiment_date": datetime.now().strftime("%Y-%m-%d"),
                "data": all_data
            }
            
            # 建立匯出目錄
            export_dir = self.mount_path / "exports"
            export_dir.mkdir(exist_ok=True)
            
            # 儲存匯出檔案
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            if output_format == "json":
                export_path = export_dir / f"experiment_export_{timestamp}.json"
                with open(export_path, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
            
            elif output_format == "csv":
                import csv
                export_path = export_dir / f"experiment_export_{timestamp}.csv"
                
                with open(export_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["student_id", "total_messages", "saved_at"])
                    
                    for student_data in all_data:
                        writer.writerow([
                            student_data.get("student_id", ""),
                            student_data.get("total_messages", 0),
                            student_data.get("saved_at", "")
                        ])
            
            print(f"✅ Exported {len(all_data)} students to {export_path}")
            return str(export_path)
            
        except Exception as e:
            print(f"❌ Export failed: {e}")
            return None
    
    def get_disk_info(self):
        """取得硬碟資訊"""
        try:
            import shutil
            total, used, free = shutil.disk_usage(self.mount_path)
            
            # 計算實驗數據大小
            experiment_size = sum(f.stat().st_size for f in self.experiment_dir.rglob("*") if f.is_file())
            
            return {
                "mount_path": str(self.mount_path),
                "experiment_dir": str(self.experiment_dir),
                "total_gb": round(total / (1024**3), 2),
                "used_gb": round(used / (1024**3), 2),
                "free_gb": round(free / (1024**3), 2),
                "usage_percent": round(used / total * 100, 1),
                "experiment_data_mb": round(experiment_size / (1024**2), 2),
                "student_files": len(list(self.experiment_dir.glob("*.json")))
            }
            
        except Exception as e:
            return {"error": str(e)}

# 建立全域實例
disk_storage = SimpleDiskStorage(mount_path="/data")

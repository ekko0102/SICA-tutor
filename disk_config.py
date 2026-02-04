# disk_config.py
import os
import json
from pathlib import Path
from datetime import datetime, timedelta

class ExperimentDataArchiver:
    """實驗數據歸檔系統 - 只儲存不參與即時處理"""
    
    def __init__(self, mount_path="/data"):
        self.mount_path = Path(mount_path)
        
        # 實驗數據專用目錄
        self.dirs = {
            'experiments': self.mount_path / 'experiments',
            'backups': self.mount_path / 'backups',
            'exports': self.mount_path / 'exports',
            'analytics': self.mount_path / 'analytics'
        }
        
        self.init_directories()
        print(f"📁 Experiment archiver initialized at: {self.mount_path}")
    
    def init_directories(self):
        """建立目錄結構"""
        for name, dir_path in self.dirs.items():
            dir_path.mkdir(parents=True, exist_ok=True)
            print(f"  ✅ Created: {dir_path}")
    
    def archive_conversation(self, experiment_id, student_id, conversation_data):
        """
        歸檔單一對話
        experiment_id: 實驗識別碼
        student_id: 學生匿名ID
        conversation_data: 完整的對話數據
        """
        try:
            # 建立實驗目錄
            exp_dir = self.dirs['experiments'] / experiment_id
            exp_dir.mkdir(parents=True, exist_ok=True)
            
            # 建立學生目錄
            student_dir = exp_dir / student_id
            student_dir.mkdir(parents=True, exist_ok=True)
            
            # 儲存檔案
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"conversation_{timestamp}.json"
            filepath = student_dir / filename
            
            # 完整的對話數據
            archive_data = {
                'experiment_id': experiment_id,
                'student_id': student_id,
                'archived_at': datetime.now().isoformat(),
                'conversation': conversation_data,
                'metadata': {
                    'total_messages': len(conversation_data),
                    'message_types': {
                        'user': sum(1 for msg in conversation_data if msg.get('role') == 'user'),
                        'assistant': sum(1 for msg in conversation_data if msg.get('role') == 'assistant')
                    }
                }
            }
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(archive_data, f, ensure_ascii=False, indent=2)
            
            print(f"💾 Archived conversation for {student_id[:8]} in experiment {experiment_id}")
            return str(filepath)
            
        except Exception as e:
            print(f"❌ Archive failed: {e}")
            return None
    
    def archive_batch(self, experiment_id, conversations_data):
        """批量歸檔多個對話"""
        archived = []
        failed = []
        
        for student_id, conv_data in conversations_data.items():
            result = self.archive_conversation(experiment_id, student_id, conv_data)
            if result:
                archived.append(student_id)
            else:
                failed.append(student_id)
        
        return {
            'archived': len(archived),
            'failed': len(failed),
            'details': {
                'archived_students': archived[:10],  # 只顯示前10個
                'failed_students': failed[:10]
            }
        }
    
    def get_experiment_data(self, experiment_id, student_id=None):
        """取得實驗數據"""
        try:
            exp_dir = self.dirs['experiments'] / experiment_id
            
            if not exp_dir.exists():
                return {'error': 'Experiment not found'}
            
            if student_id:
                # 取得特定學生的所有對話
                student_dir = exp_dir / student_id
                if not student_dir.exists():
                    return {'error': 'Student not found'}
                
                conversations = []
                for filepath in student_dir.glob("*.json"):
                    with open(filepath, 'r', encoding='utf-8') as f:
                        conversations.append(json.load(f))
                
                return {
                    'experiment_id': experiment_id,
                    'student_id': student_id,
                    'total_conversations': len(conversations),
                    'conversations': conversations
                }
            
            else:
                # 取得整個實驗的統計
                student_dirs = list(exp_dir.iterdir())
                total_conversations = 0
                
                for student_dir in student_dirs:
                    if student_dir.is_dir():
                        total_conversations += len(list(student_dir.glob("*.json")))
                
                return {
                    'experiment_id': experiment_id,
                    'total_students': len(student_dirs),
                    'total_conversations': total_conversations,
                    'students': [d.name for d in student_dirs[:20]]  # 只顯示前20個
                }
                
        except Exception as e:
            return {'error': str(e)}
    
    def export_experiment(self, experiment_id, format='json'):
        """匯出整個實驗數據"""
        try:
            exp_dir = self.dirs['experiments'] / experiment_id
            
            if not exp_dir.exists():
                return {'error': 'Experiment not found'}
            
            # 收集所有數據
            all_data = []
            total_messages = 0
            
            for student_dir in exp_dir.iterdir():
                if student_dir.is_dir():
                    student_id = student_dir.name
                    student_conversations = []
                    
                    for filepath in student_dir.glob("*.json"):
                        with open(filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            student_conversations.append(data)
                            total_messages += len(data.get('conversation', []))
                    
                    all_data.append({
                        'student_id': student_id,
                        'conversations': student_conversations,
                        'conversation_count': len(student_conversations)
                    })
            
            export_data = {
                'experiment_id': experiment_id,
                'export_time': datetime.now().isoformat(),
                'total_students': len(all_data),
                'total_conversations': sum(item['conversation_count'] for item in all_data),
                'total_messages': total_messages,
                'data': all_data
            }
            
            # 儲存到 exports 目錄
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            export_filename = f"experiment_{experiment_id}_{timestamp}.{format}"
            export_path = self.dirs['exports'] / export_filename
            
            if format == 'json':
                with open(export_path, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
            elif format == 'csv':
                # 簡化的 CSV 匯出
                import csv
                with open(export_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(['student_id', 'conversation_count', 'total_messages'])
                    for item in all_data:
                        student_msgs = sum(len(conv['conversation']) for conv in item['conversations'])
                        writer.writerow([item['student_id'], item['conversation_count'], student_msgs])
            
            return {
                'success': True,
                'export_path': str(export_path),
                'export_data': export_data
            }
            
        except Exception as e:
            return {'error': str(e)}
    
    def get_disk_usage(self):
        """取得硬碟使用狀況"""
        try:
            import shutil
            total, used, free = shutil.disk_usage(self.mount_path)
            
            dir_sizes = {}
            for name, dir_path in self.dirs.items():
                if dir_path.exists():
                    size = sum(f.stat().st_size for f in dir_path.rglob('*') if f.is_file())
                    dir_sizes[name] = size
            
            return {
                'mount_path': str(self.mount_path),
                'total_bytes': total,
                'used_bytes': used,
                'free_bytes': free,
                'usage_percent': (used / total) * 100 if total > 0 else 0,
                'directory_sizes': dir_sizes
            }
        except Exception as e:
            return {'error': str(e)}

# 全域實例
archiver = ExperimentDataArchiver(mount_path="/data")

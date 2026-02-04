# disk_config.py
import os
import json
from pathlib import Path
from datetime import datetime

class DiskManager:
    """Disk 管理類別"""
    
    def __init__(self, mount_path="/data"):
        """
        初始化 Disk 管理
        mount_path: Render Disk 的掛載路徑
        """
        self.mount_path = Path(mount_path)
        
        # 建立目錄結構
        self.dirs = {
            'conversations': self.mount_path / 'conversations',
            'backups': self.mount_path / 'backups',
            'exports': self.mount_path / 'exports',
            'analytics': self.mount_path / 'analytics',
            'logs': self.mount_path / 'logs',
            'temp': self.mount_path / 'temp',
            'config': self.mount_path / 'config'
        }
        
        # 初始化
        self.init_directories()
        self.print_disk_info()
    
    def init_directories(self):
        """建立所有必要的目錄"""
        print(f"📁 Initializing disk storage at: {self.mount_path}")
        
        for name, dir_path in self.dirs.items():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
                print(f"  ✅ Created: {dir_path}")
            except Exception as e:
                print(f"  ❌ Failed to create {dir_path}: {e}")
        
        # 建立說明檔案
        self.create_readme()
    
    def create_readme(self):
        """建立說明檔案"""
        readme_content = f"""# SICA Tutor Data Storage

Mount Path: {self.mount_path}
Created: {datetime.now().isoformat()}

## Directory Structure

{self.mount_path}/
├── conversations/     # 聊天對話紀錄
├── backups/          # 自動備份
├── exports/          # 匯出檔案 (CSV/JSON)
├── analytics/        # 分析資料
├── logs/             # 應用程式日誌
├── temp/             # 暫存檔案 (24小時自動清理)
└── config/           # 設定檔案

## Usage in Code

```python
from disk_config import disk

# 儲存對話
file_path = disk.dirs['conversations'] / 'user_123.json'

# 取得匯出路徑
export_path = disk.dirs['exports'] / 'report.csv'

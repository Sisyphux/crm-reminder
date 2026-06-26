"""
客户跟进提醒系统 - 数据库模型与初始化
SQLite 本地存储，支持 iCloud 同步
"""
import sqlite3
import sys
import os
import platform
import logging

logger = logging.getLogger(__name__)


def is_packaged():
    """检测是否运行在 PyInstaller 打包环境中"""
    return getattr(sys, 'frozen', False)


def get_app_root():
    """获取应用根目录（打包模式 vs 源码模式自动适配）"""
    if is_packaged():
        # macOS: ~/Library/Application Support/客户跟进提醒系统/
        system = platform.system()
        if system == 'Darwin':
            return os.path.expanduser('~/Library/Application Support/客户跟进提醒系统')
        elif system == 'Windows':
            return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), '客户跟进提醒系统')
        else:
            return os.path.join(os.path.expanduser('~'), '.crm_reminders')
    else:
        return os.path.dirname(os.path.abspath(__file__))


def get_db_dir():
    """获取数据库目录（始终使用本地可写目录，支持从 iCloud 启动同步）"""
    # 优先使用环境变量指定的路径
    env_db_path = os.environ.get('CRM_DB_PATH')
    if env_db_path:
        return env_db_path
    
    return os.path.join(get_app_root(), 'data')


def _get_icloud_dirs():
    """获取 iCloud 候选目录列表"""
    system = platform.system()
    if system == 'Darwin':
        return [
            os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/工作/客户资料'),
            os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/客户跟进'),
        ]
    elif system == 'Windows':
        return [
            r'D:\iCloudDrive\工作\客户资料',
            r'D:\iCloud Drive\工作\客户资料',
            r'D:\iCloudDrive\工作\客户跟进',
            r'D:\iCloud Drive\工作\客户跟进',
        ]
    return []


def get_icloud_db_path():
    """获取 iCloud 中已有的数据库路径（用于拉取同步），不存在则返回 None"""
    for path in _get_icloud_dirs():
        db_file = os.path.join(path, 'crm_reminders.db')
        if os.path.exists(db_file):
            return db_file
    return None


def get_icloud_backup_path():
    """获取 iCloud 备份目标路径（用于推送到 iCloud），优先选用已存在的目录"""
    for path in _get_icloud_dirs():
        if os.path.exists(path):
            return os.path.join(path, 'crm_reminders.db')
    return None


def sync_db_from_icloud():
    """启动时：如果 iCloud 有更新的数据库则复制到本地"""
    import shutil
    icloud_db = get_icloud_db_path()
    if not icloud_db:
        return False
    
    local_db = DB_PATH
    local_exists = os.path.exists(local_db)
    
    # 如果本地没有数据库，直接复制
    if not local_exists:
        try:
            ensure_db_dir()
            shutil.copy2(icloud_db, local_db)
            logger.info(f'已从 iCloud 同步数据库到本地: {local_db}')
            return True
        except Exception as e:
            logger.warning(f'从 iCloud 复制数据库失败: {e}')
            return False
    
    # 比较修改时间，用较新的覆盖
    try:
        icloud_mtime = os.path.getmtime(icloud_db)
        local_mtime = os.path.getmtime(local_db)
        
        if icloud_mtime > local_mtime + 10:  # iCloud 更新（留 10 秒容差）
            shutil.copy2(icloud_db, local_db)
            logger.info(f'iCloud 数据库更新，已同步到本地 (iCloud 较新 {icloud_mtime:.0f} > 本地 {local_mtime:.0f})')
            return True
        else:
            logger.debug(f'本地数据库已是最新，跳过 iCloud 同步')
            return False
    except Exception as e:
        logger.warning(f'比较数据库时间戳失败: {e}')
        return False


def backup_db_to_icloud():
    """将本地数据库备份到 iCloud（供定时任务调用）"""
    import shutil
    icloud_path = get_icloud_backup_path()
    if not icloud_path:
        return False
    
    local_db = DB_PATH
    if not os.path.exists(local_db):
        return False
    
    try:
        icloud_dir = os.path.dirname(icloud_path)
        os.makedirs(icloud_dir, exist_ok=True)
        shutil.copy2(local_db, icloud_path)
        logger.info(f'已备份数据库到 iCloud: {icloud_path}')
        return True
    except Exception as e:
        logger.warning(f'备份数据库到 iCloud 失败: {e}')
        return False


# 数据库目录和文件路径
DB_DIR = get_db_dir()
DB_PATH = os.path.join(DB_DIR, 'crm_reminders.db')


def ensure_db_dir():
    """确保数据库目录存在"""
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)


def get_db():
    """获取数据库连接（已启用外键约束，确保数据持久化）"""
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")  # 确保数据写入磁盘
    conn.execute("PRAGMA cache_size = -2000")   # 2MB 缓存
    return conn


def init_db():
    """初始化数据库表结构"""
    ensure_db_dir()
    conn = get_db()
    c = conn.cursor()

    # 客户表（完整版：含客户分类、行业、规模、年营收等）
    c.execute('''
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company TEXT DEFAULT '',
            country TEXT DEFAULT '',
            level TEXT DEFAULT 'C' CHECK(level IN ('A', 'B', 'C', 'C+', 'D')),
            type TEXT DEFAULT '' CHECK(type IN ('中间商', '终端', '')),
            website TEXT DEFAULT '',
            profile TEXT DEFAULT '',
            field TEXT DEFAULT '',
            status TEXT DEFAULT '未建联' CHECK(status IN ('未建联', '已建联', '跟进中', '成交', '流失')),
            notes TEXT DEFAULT '',
            system_notes TEXT DEFAULT '',
            last_contact TEXT DEFAULT '',
            next_follow_up TEXT DEFAULT '',
            manual_next_follow INTEGER DEFAULT 0,
            customer_type TEXT DEFAULT 'existing' CHECK(customer_type IN ('new', 'existing')),
            industry TEXT DEFAULT '',
            company_size TEXT DEFAULT '',
            annual_revenue TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 提醒表
    c.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            content TEXT DEFAULT '',
            remind_date TEXT NOT NULL,
            is_done INTEGER DEFAULT 0,
            reminder_type TEXT DEFAULT 'follow_up',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    ''')

    # 跟进历史记录表
    c.execute('''
        CREATE TABLE IF NOT EXISTS follow_up_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            content TEXT DEFAULT '',
            follow_date TEXT NOT NULL,
            result TEXT DEFAULT '',
            next_plan TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    ''')

    # 联系人表
    c.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            name TEXT DEFAULT '',
            title TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            linkedin TEXT DEFAULT '',
            is_primary INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    ''')

    # 开发信记录表
    c.execute('''
        CREATE TABLE IF NOT EXISTS outreach_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            subject TEXT DEFAULT '',
            content TEXT DEFAULT '',
            sent_date TEXT DEFAULT '',
            reply_status TEXT DEFAULT 'pending' CHECK(reply_status IN ('pending', 'replied', 'bounced', 'no_reply')),
            reply_content TEXT DEFAULT '',
            reply_date TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    ''')

    # 背调报告表
    c.execute('''
        CREATE TABLE IF NOT EXISTS research_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL UNIQUE,
            summary TEXT DEFAULT '',
            company_info TEXT DEFAULT '',
            key_findings TEXT DEFAULT '',
            needs_analysis TEXT DEFAULT '',
            cooperation_value TEXT DEFAULT '',
            raw_input TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    ''')

    # 操作日志表
    c.execute('''
        CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id INTEGER,
            details TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 邮件发送记录表
    c.execute('''
        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            message TEXT DEFAULT '',
            reminder_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    conn.commit()
    conn.close()

    # --- 数据库迁移：为旧数据库添加缺失的列 ---
    migrate_database()

    # --- 跨设备同步日志回放（已停用） ---
    pass


def migrate_database():
    """迁移旧数据库，添加新字段"""
    conn = get_db()
    c = conn.cursor()

    # customers 表迁移
    c.execute("PRAGMA table_info(customers)")
    existing_cols = [row[1] for row in c.fetchall()]

    customer_migrations = {
        'system_notes': "TEXT DEFAULT ''",
        'manual_next_follow': "INTEGER DEFAULT 0",
        'last_contact': "TEXT DEFAULT ''",
        'customer_type': "TEXT DEFAULT 'existing'",
        'industry': "TEXT DEFAULT ''",
        'company_size': "TEXT DEFAULT ''",
        'annual_revenue': "TEXT DEFAULT ''",
        'import_source': "TEXT DEFAULT 'manual'",
        'is_deleted': "INTEGER DEFAULT 0",
        'deleted_at': "TEXT DEFAULT ''",
    }

    for col_name, col_def in customer_migrations.items():
        if col_name not in existing_cols:
            try:
                c.execute(f"ALTER TABLE customers ADD COLUMN {col_name} {col_def}")
                logger.info(f"[OK] 数据库迁移：已为 customers 添加字段 {col_name}")
            except Exception as e:
                logger.error(f"[ERROR] 迁移 customers.{col_name} 失败: {e}")

    # reminders 表迁移
    c.execute("PRAGMA table_info(reminders)")
    reminder_cols = [row[1] for row in c.fetchall()]

    reminder_migrations = {
        'reminder_type': "TEXT DEFAULT 'follow_up'",
    }

    for col_name, col_def in reminder_migrations.items():
        if col_name not in reminder_cols:
            try:
                c.execute(f"ALTER TABLE reminders ADD COLUMN {col_name} {col_def}")
                logger.info(f"[OK] 数据库迁移：已为 reminders 添加字段 {col_name}")
            except Exception as e:
                logger.error(f"[ERROR] 迁移 reminders.{col_name} 失败: {e}")

    # follow_up_logs 表迁移
    c.execute("PRAGMA table_info(follow_up_logs)")
    log_cols = [row[1] for row in c.fetchall()]

    log_migrations = {
        'source': "TEXT DEFAULT 'manual'",
    }

    for col_name, col_def in log_migrations.items():
        if col_name not in log_cols:
            try:
                c.execute(f"ALTER TABLE follow_up_logs ADD COLUMN {col_name} {col_def}")
                logger.info(f"[OK] 数据库迁移：已为 follow_up_logs 添加字段 {col_name}")
            except Exception as e:
                logger.error(f"[ERROR] 迁移 follow_up_logs.{col_name} 失败: {e}")

    conn.commit()
    conn.close()


def seed_demo_data():
    """插入演示数据（仅在数据库为空时）"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM customers")
    if c.fetchone()[0] > 0:
        conn.close()
        return

    from datetime import datetime, timedelta
    today = datetime.now().strftime('%Y-%m-%d')
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    next_week = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')

    demo_customers = [
        ('ProSEP S.R.L.', 'ProSEP S.R.L.', '罗马尼亚', 'C+', '中间商', 'https://prosep.ro/',
         '罗马尼亚主要的塑料半成品进口与分销商', '亚克力分销', '跟进中', '5/31领英添加好友→客户回复询问介绍资料', tomorrow),
        ('Mar Industrial', 'Mar Industrial Distribuidora', '墨西哥', 'C', '中间商', '',
         '墨西哥工程塑料分销，主营PC、亚克力、尼龙等', '工程塑料分销', '未建联', '寻求亚洲非中国产地的聚碳酸酯板材供应商', today),
        ('Regal Plastics', 'Regal Plastics', '美国', 'C+', '中间商', 'https://www.regal-plastics.com/',
         '美国大型塑料板材分销，主要供应商为泰国titan', '亚克力分销', '跟进中', '询价40尺整柜透明浇铸板，等待6月正式报价', next_week),
        ('Enseignes Valois', 'Enseignes Valois', '加拿大', 'C+', '终端', 'https://www.enseignesvalois.com/',
         '魁北克拉瓦勒的标识制造商，成立于2016年', '标牌制造', '跟进中', '已发送报价未回复', today),
        ('Bentleigh Group', 'Bentleigh Group', '澳大利亚', 'C+', '终端', '',
         '澳洲老牌标识企业，自营墨尔本、布里斯班两大工厂', '标牌制造', '跟进中', '样品已寄出，等待客户反馈', tomorrow),
    ]

    for cust in demo_customers:
        c.execute('''INSERT INTO customers (name, company, country, level, type, website, profile, field, status, notes, next_follow_up, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))''', cust)

    # 为每个客户创建提醒
    c.execute('SELECT id, name, next_follow_up, notes FROM customers')
    for row in c.fetchall():
        c.execute('''
            INSERT INTO reminders (customer_id, content, remind_date, is_done, reminder_type, created_at)
            VALUES (?, ?, ?, 0, 'follow_up', datetime('now'))
        ''', (row['id'], f'跟进 {row["name"]}: {row["notes"]}', row['next_follow_up']))

    conn.commit()
    conn.close()

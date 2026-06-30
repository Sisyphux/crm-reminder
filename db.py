"""
客户跟进提醒系统 - 数据库模型与初始化（多用户版）
SQLite 本地存储，每人独立数据库，支持自动备份与恢复
"""
import sqlite3
import sys
import os
import platform
import logging
import shutil
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ========== 用户配置 ==========
USERS = {
    'hamid': {'name': 'Hamid', 'label': 'Hamid', 'color': '#8B9DAF'},
    'amy':   {'name': 'Amy',   'label': 'Amy',   'color': '#C4877A'},
    'kelly': {'name': 'Kelly', 'label': 'Kelly', 'color': '#8BA88A'},
}
USERS_LIST = list(USERS.keys())

# ========== 当前用户上下文（线程级别） ==========
_current_user = None

def set_db_user(user):
    """设置当前数据库用户（用于 get_db() 路由）"""
    global _current_user
    _current_user = user

def get_current_user():
    """获取当前用户"""
    return _current_user


# ========== 路径工具 ==========

def is_packaged():
    """检测是否运行在 PyInstaller 打包环境中"""
    return getattr(sys, 'frozen', False)


def get_app_root():
    """获取应用根目录"""
    if is_packaged():
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
    """获取数据库目录"""
    env_db_path = os.environ.get('CRM_DB_PATH')
    if env_db_path:
        return env_db_path
    return os.path.join(get_app_root(), 'data')


# 数据库目录
DB_DIR = get_db_dir()


def ensure_db_dir():
    """确保数据库目录存在"""
    os.makedirs(DB_DIR, exist_ok=True)
    # 确保备份目录存在
    os.makedirs(os.path.join(DB_DIR, 'backups'), exist_ok=True)


# ========== 数据库连接 ==========

def get_system_db():
    """获取系统数据库连接（存放用户元数据、周报等）"""
    ensure_db_dir()
    path = os.path.join(DB_DIR, 'system.db')
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA cache_size = -2000")
    return conn


def get_user_db_path(user):
    """获取用户数据库文件路径"""
    if user in USERS:
        return os.path.join(DB_DIR, f'{user}.db')
    return os.path.join(DB_DIR, 'system.db')


def get_db():
    """获取当前用户的数据库连接（根据 _current_user 自动路由）"""
    ensure_db_dir()
    user = _current_user
    if user and user in USERS:
        path = get_user_db_path(user)
    else:
        path = os.path.join(DB_DIR, 'system.db')
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA cache_size = -2000")
    return conn


# ========== 数据库完整性检查 ==========

def check_integrity():
    """检查所有数据库的完整性，返回检查结果"""
    results = {}
    ensure_db_dir()
    
    all_dbs = {'system.db': os.path.join(DB_DIR, 'system.db')}
    for user in USERS:
        all_dbs[f'{user}.db'] = get_user_db_path(user)
    
    for name, path in all_dbs.items():
        try:
            if os.path.exists(path):
                conn = sqlite3.connect(path, timeout=10.0)
                c = conn.cursor()
                c.execute("PRAGMA integrity_check")
                result = c.fetchone()[0]
                conn.close()
                results[name] = 'ok' if result == 'ok' else result
            else:
                results[name] = 'not_found'
        except Exception as e:
            results[name] = f'error: {e}'
    
    return results


# ========== 自动备份 ==========

def backup_database():
    """备份当前所有数据库到 data/backups/YYYY-MM-DD/"""
    ensure_db_dir()
    date_str = datetime.now().strftime('%Y-%m-%d')
    backup_dir = os.path.join(DB_DIR, 'backups', date_str)
    os.makedirs(backup_dir, exist_ok=True)
    
    backed_up = []
    failed = []
    
    # 备份 system.db
    sys_db = os.path.join(DB_DIR, 'system.db')
    if os.path.exists(sys_db):
        try:
            shutil.copy2(sys_db, os.path.join(backup_dir, 'system.db'))
            backed_up.append('system.db')
        except Exception as e:
            failed.append(f'system.db: {e}')
    
    # 备份每个用户的数据库
    for user in USERS:
        user_db = get_user_db_path(user)
        if os.path.exists(user_db):
            try:
                shutil.copy2(user_db, os.path.join(backup_dir, f'{user}.db'))
                backed_up.append(f'{user}.db')
            except Exception as e:
                failed.append(f'{user}.db: {e}')
    
    # 如果所有文件都备份成功，删除旧的备份目录（保留最近7天）
    if not failed:
        _cleanup_old_backups()
    
    return {'backed_up': backed_up, 'failed': failed, 'path': backup_dir}


def _cleanup_old_backups(retain_days=7):
    """清理超过 retain_days 天的备份目录"""
    backup_root = os.path.join(DB_DIR, 'backups')
    if not os.path.exists(backup_root):
        return
    cutoff = datetime.now() - timedelta(days=retain_days)
    for d in os.listdir(backup_root):
        d_path = os.path.join(backup_root, d)
        if os.path.isdir(d_path):
            try:
                d_date = datetime.strptime(d, '%Y-%m-%d')
                if d_date < cutoff:
                    shutil.rmtree(d_path)
                    logger.info(f'已清理旧备份: {d}')
            except ValueError:
                continue


def list_backups():
    """列出所有可用备份"""
    backup_root = os.path.join(DB_DIR, 'backups')
    if not os.path.exists(backup_root):
        return []
    backups = []
    for d in sorted(os.listdir(backup_root), reverse=True):
        d_path = os.path.join(backup_root, d)
        if os.path.isdir(d_path):
            files = [f for f in os.listdir(d_path) if f.endswith('.db')]
            if files:
                backups.append({'date': d, 'files': files})
    return backups


# ========== 恢复备份 ==========

def restore_from_backup(backup_date):
    """从指定日期恢复数据库（先备份当前数据，再覆盖）"""
    backup_dir = os.path.join(DB_DIR, 'backups', backup_date)
    if not os.path.exists(backup_dir):
        return {'success': False, 'error': f'备份目录不存在: {backup_date}'}
    
    # 先备份当前数据
    backup_database()
    
    restored = []
    for file in os.listdir(backup_dir):
        if file.endswith('.db'):
            src = os.path.join(backup_dir, file)
            dst = os.path.join(DB_DIR, file)
            try:
                shutil.copy2(src, dst)
                restored.append(file)
            except Exception as e:
                return {'success': False, 'error': f'恢复 {file} 失败: {e}'}
    
    return {'success': True, 'restored': restored}


# ========== 迁移旧数据库 ==========

def migrate_old_database():
    """将旧的 crm_reminders.db 迁移为用户 hamid.db"""
    old_path = os.path.join(DB_DIR, 'crm_reminders.db')
    hamid_path = get_user_db_path('hamid')
    if os.path.exists(old_path) and not os.path.exists(hamid_path):
        try:
            shutil.copy2(old_path, hamid_path)
            logger.info(f'已将旧数据库 {old_path} 迁移到 {hamid_path}')
            return True
        except Exception as e:
            logger.error(f'迁移旧数据库失败: {e}')
    return False


# ========== 用户表结构 ==========

USER_TABLE_SQL = [
    # 客户表
    '''
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
        import_source TEXT DEFAULT 'manual',
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
    # 提醒表
    '''
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
    ''',
    # 跟进历史
    '''
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
    ''',
    # 联系人
    '''
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
    ''',
    # 开发信
    '''
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
    ''',
    # 背调报告
    '''
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
    ''',
    # 操作日志
    '''
    CREATE TABLE IF NOT EXISTS operation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id INTEGER,
        details TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
    # 邮件发送记录（保留但不再前台展示）
    '''
    CREATE TABLE IF NOT EXISTS email_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL,
        message TEXT DEFAULT '',
        reminder_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
]

# 用户数据库迁移：为旧库加新列
USER_MIGRATIONS = {
    'customers': {
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
    },
    'reminders': {
        'reminder_type': "TEXT DEFAULT 'follow_up'",
    },
    'follow_up_logs': {
        'source': "TEXT DEFAULT 'manual'",
    },
}


def init_user_tables(user):
    """初始化/迁移单个用户的数据库"""
    old_user = _current_user
    set_db_user(user)
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 创建所有表
        for sql in USER_TABLE_SQL:
            c.execute(sql)
        
        # 数据库迁移
        for table_name, migrations in USER_MIGRATIONS.items():
            try:
                c.execute(f"PRAGMA table_info({table_name})")
                existing_cols = [row[1] for row in c.fetchall()]
                for col_name, col_def in migrations.items():
                    if col_name not in existing_cols:
                        c.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
            except Exception as e:
                logger.debug(f"迁移 {table_name} 跳过: {e}")
        
        conn.commit()
        conn.close()
    finally:
        set_db_user(old_user)
    
    # 插入演示数据（仅当数据库为空时）
    seed_demo_data_for_user(user)


def seed_demo_data_for_user(user):
    """为指定用户插入演示数据（仅 Hamid 有演示数据，Amy/Kelly 为空）"""
    if user not in ('hamid',):
        return
    old_user = _current_user
    set_db_user(user)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM customers")
        if c.fetchone()[0] > 0:
            conn.close()
            return
        
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
        
        c.execute('SELECT id, name, next_follow_up, notes FROM customers')
        for row in c.fetchall():
            c.execute('''
                INSERT INTO reminders (customer_id, content, remind_date, is_done, reminder_type, created_at)
                VALUES (?, ?, ?, 0, 'follow_up', datetime('now'))
            ''', (row['id'], f'跟进 {row["name"]}: {row["notes"]}', row['next_follow_up']))
        
        conn.commit()
        conn.close()
    finally:
        set_db_user(old_user)


# ========== 系统数据库初始化 ==========

def init_system_db():
    """初始化系统数据库（用户信息、周报等）"""
    conn = get_system_db()
    c = conn.cursor()
    
    # 用户表
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            color TEXT DEFAULT '#666',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    
    # 周报表
    c.execute('''
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            week_start TEXT NOT NULL,
            content TEXT DEFAULT '',
            highlights TEXT DEFAULT '',
            challenges TEXT DEFAULT '',
            next_plan TEXT DEFAULT '',
            status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'submitted')),
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, week_start)
        )
    ''')
    
    # 应用设置
    c.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    
    # 插入默认用户
    for uid, info in USERS.items():
        c.execute('''
            INSERT OR IGNORE INTO users (id, name, label, color)
            VALUES (?, ?, ?, ?)
        ''', (uid, info['name'], info['label'], info['color']))
    
    conn.commit()
    conn.close()


# ========== 全部初始化 ==========

def init_all_dbs():
    """初始化所有数据库"""
    ensure_db_dir()
    
    # 1. 迁移旧数据库
    migrate_old_database()
    
    # 2. 初始化系统数据库
    init_system_db()
    
    # 3. 初始化每个用户的数据库
    for user in USERS:
        init_user_tables(user)
    
    # 4. 执行一次完整性检查
    integrity = check_integrity()
    for name, status in integrity.items():
        if status != 'ok':
            logger.warning(f'数据库完整性检查 [{name}]: {status}')
    
    logger.info('所有数据库初始化完成')


# ========== 向下兼容 ==========

def init_db():
    """兼容旧版本：初始化所有数据库"""
    init_all_dbs()


def seed_demo_data():
    """兼容旧版本：为所有用户插入演示数据"""
    for user in USERS:
        seed_demo_data_for_user(user)


# ========== iCloud 同步（保留原接口） ==========

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


def sync_db_from_icloud():
    """启动时：从 iCloud 拉取所有用户的数据库"""
    for user in USERS:
        local_path = get_user_db_path(user)
        for icloud_dir in _get_icloud_dirs():
            icloud_path = os.path.join(icloud_dir, f'{user}.db')
            if os.path.exists(icloud_path):
                try:
                    if os.path.exists(local_path):
                        icloud_mtime = os.path.getmtime(icloud_path)
                        local_mtime = os.path.getmtime(local_path)
                        if icloud_mtime > local_mtime + 10:
                            shutil.copy2(icloud_path, local_path)
                            logger.info(f'{user}: iCloud → 本地同步完成')
                    else:
                        shutil.copy2(icloud_path, local_path)
                        logger.info(f'{user}: 从 iCloud 复制到本地')
                except Exception as e:
                    logger.warning(f'{user}: iCloud 同步失败: {e}')
    return True


def backup_db_to_icloud():
    """备份数据库到 iCloud"""
    for icloud_dir in _get_icloud_dirs():
        if os.path.exists(icloud_dir):
            for user in USERS:
                local_path = get_user_db_path(user)
                if os.path.exists(local_path):
                    try:
                        icloud_path = os.path.join(icloud_dir, f'{user}.db')
                        shutil.copy2(local_path, icloud_path)
                        logger.info(f'{user}: 已备份到 iCloud')
                    except Exception as e:
                        logger.warning(f'{user}: iCloud 备份失败: {e}')
            return True
    return False

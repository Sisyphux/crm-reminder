"""
客户跟进提醒系统 - Flask 后端应用
核心提醒功能 + 跟进历史 + 操作日志 + 邮件提醒 + 双向 Excel 同步 + AI 深度调研
"""
import os
import sys
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from db import get_db, init_db, seed_demo_data, DB_PATH, DB_DIR
from config import SMTP_CONFIG
from ical_gen import build_icalendar
from scheduler import start_scheduler, stop_scheduler, get_scheduler_status
from app.engine import analyze_customer, analyze_single_intelligence, quick_chat


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# PyInstaller 打包模式下，静态资源在 sys._MEIPASS 中
if getattr(sys, 'frozen', False):
    app = Flask(__name__, static_folder=os.path.join(sys._MEIPASS, 'static'), static_url_path='')
else:
    app = Flask(__name__, static_folder='app/static', static_url_path='')
CORS(app)


# ========== 操作日志工具 ==========
def log_operation(action, target_type, target_id=None, details=''):
    """记录操作日志"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO operation_logs (action, target_type, target_id, details, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (action, target_type, target_id, details, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"记录操作日志失败: {str(e)}")


# ========== 国家名称标准化 ==========
_COUNTRY_MAP = {
    'usa': '美国', 'united states': '美国', 'us': '美国', 'u.s.a.': '美国',
    'uae': '阿联酋', 'united arab emirates': '阿联酋',
    'saudi arabia': '沙特阿拉伯', 'ksa': '沙特阿拉伯',
    'qatar': '卡塔尔',
    'germany': '德国', 'deutschland': '德国',
    'france': '法国',
    'united kingdom': '英国', 'uk': '英国',
    'australia': '澳大利亚', 'au': '澳大利亚',
    'canada': '加拿大', 'ca': '加拿大',
    'mexico': '墨西哥', 'mx': '墨西哥',
    'italy': '意大利',
    'romania': '罗马尼亚',
    'egypt': '埃及',
    'india': '印度',
    'turkey': '土耳其',
    'denmark': '丹麦',
    'new zealand': '新西兰',
    'colombia': '哥伦比亚',
    'iran': '伊朗',
    'oman': '阿曼',
    'kuwait': '科威特',
}

def normalize_country(name):
    """将国家名称统一为中文"""
    if not name:
        return ''
    key = name.strip().lower()
    return _COUNTRY_MAP.get(key, name.strip())


# ========== 初始化 ==========
@app.before_request
def ensure_db():
    init_db()


# ========== 静态文件 ==========
@app.route('/')
def index():
    # 多路径 fallback，确保打包后也能找到 index.html
    candidates = []
    sf = app.static_folder
    if sf:
        candidates.append(os.path.join(sf, 'index.html'))
    candidates.append(os.path.join(os.getcwd(), 'static', 'index.html'))
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, 'static', 'index.html'))

    # 兜底：指向 app/static/
    candidates.append(os.path.join(os.getcwd(), 'app', 'static', 'index.html'))

    for path in candidates:
        if os.path.isfile(path):
            directory = os.path.dirname(path)
            response = send_from_directory(directory, 'index.html')
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

    # 最后兜底：返回一个简单页面说明问题
    return jsonify({'error': 'index.html not found', 'tried': candidates}), 404


# ========== 客户 API ==========

@app.route('/api/customers', methods=['GET'])
def get_customers():
    """获取客户列表，支持搜索和筛选"""
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    level = request.args.get('level', '').strip()
    customer_type = request.args.get('customer_type', '').strip()
    sort = request.args.get('sort', 'next_follow_up')
    order = request.args.get('order', 'asc')
    include_deleted = request.args.get('deleted', '0').strip()

    conn = get_db()
    c = conn.cursor()

    # 默认过滤已删除的客户；传 deleted=1 只看回收站
    if include_deleted == '1':
        query = 'SELECT * FROM customers WHERE is_deleted = 1'
    elif include_deleted == 'all':
        query = 'SELECT * FROM customers WHERE 1=1'
    else:
        query = 'SELECT * FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)'
    params = []

    if search:
        query += ' AND (name LIKE ? OR company LIKE ? OR country LIKE ? OR field LIKE ?)'
        like = f'%{search}%'
        params.extend([like, like, like, like])

    if status:
        query += ' AND status = ?'
        params.append(status)

    if level:
        query += ' AND level = ?'
        params.append(level)

    if customer_type:
        query += ' AND customer_type = ?'
        params.append(customer_type)

    allowed_sorts = ['name', 'company', 'country', 'level', 'status', 'next_follow_up', 'created_at', 'last_contact']
    if sort not in allowed_sorts:
        sort = 'next_follow_up'
    if order not in ('asc', 'desc'):
        order = 'asc'
    query += f' ORDER BY {sort} {order}'

    c.execute(query, params)
    customers = [dict(row) for row in c.fetchall()]

    # 批量获取所有客户的最后一次联系时间（避免 N+1 查询）
    today = datetime.now().strftime('%Y-%m-%d')
    if customers:
        customer_ids = [cust['id'] for cust in customers]
        placeholders = ','.join('?' * len(customer_ids))
        c.execute(f'''
            SELECT customer_id, MAX(follow_date) as follow_date
            FROM follow_up_logs
            WHERE customer_id IN ({placeholders}) AND follow_date <= ?
            GROUP BY customer_id
        ''', customer_ids + [today])
        last_contacts = {}
        for row in c.fetchall():
            last_contacts[row['customer_id']] = row['follow_date']
        
        for cust in customers:
            cust['last_contact'] = last_contacts.get(cust['id'], '')

    conn.close()
    return jsonify({'customers': customers, 'total': len(customers)})


@app.route('/api/customers/<int:customer_id>', methods=['GET'])
def get_customer(customer_id):
    """获取单个客户详情（含提醒和历史）"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM customers WHERE id = ?', (customer_id,))
    customer = c.fetchone()

    if not customer:
        conn.close()
        return jsonify({'error': '客户不存在'}), 404

    # 获取未完成的提醒
    c.execute('SELECT * FROM reminders WHERE customer_id = ? AND is_done = 0 ORDER BY remind_date ASC', (customer_id,))
    reminders = [dict(row) for row in c.fetchall()]

    # 获取跟进历史
    c.execute('SELECT * FROM follow_up_logs WHERE customer_id = ? ORDER BY follow_date DESC, created_at DESC', (customer_id,))
    follow_history = [dict(row) for row in c.fetchall()]

    # 获取联系人
    c.execute('SELECT * FROM contacts WHERE customer_id = ? ORDER BY is_primary DESC, created_at DESC', (customer_id,))
    contacts = [dict(row) for row in c.fetchall()]

    # 获取开发信记录
    c.execute('SELECT * FROM outreach_emails WHERE customer_id = ? ORDER BY sent_date DESC, created_at DESC', (customer_id,))
    outreach_emails = [dict(row) for row in c.fetchall()]

    # 获取背调报告
    c.execute('SELECT * FROM research_reports WHERE customer_id = ?', (customer_id,))
    research = c.fetchone()

    conn.close()

    result = dict(customer)
    result['reminders'] = reminders
    result['follow_history'] = follow_history
    result['contacts'] = contacts
    result['outreach_emails'] = outreach_emails
    result['research'] = dict(research) if research else None
    return jsonify(result)


@app.route('/api/customers', methods=['POST'])
def create_customer():
    """创建新客户"""
    data = request.json
    conn = get_db()
    c = conn.cursor()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 统一国家名称为中文
    country_raw = data.get('country', '').strip()
    country = normalize_country(country_raw)

    c.execute('''
        INSERT INTO customers (name, company, country, level, type, website, profile, field, status, notes, system_notes, last_contact, next_follow_up, customer_type, industry, company_size, annual_revenue, import_source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('name', ''),
        data.get('company', ''),
        country,
        data.get('level', 'C'),
        data.get('type', ''),
        data.get('website', ''),
        data.get('profile', ''),
        data.get('field', ''),
        data.get('status', '未建联'),
        data.get('notes', ''),
        data.get('system_notes', ''),
        data.get('last_contact', ''),
        data.get('next_follow_up', ''),
        data.get('customer_type', 'existing'),
        data.get('industry', ''),
        data.get('company_size', ''),
        data.get('annual_revenue', ''),
        'manual',
        now, now
    ))

    customer_id = c.lastrowid

    # 如果用户手动设置了下次跟进日期，创建对应提醒
    manual_next_follow = data.get('next_follow_up', '')
    if manual_next_follow:
        c.execute('''
            INSERT INTO reminders (customer_id, content, remind_date, is_done, created_at)
            VALUES (?, ?, ?, 0, ?)
        ''', (customer_id, f'跟进 {data.get("name", "")}: {data.get("notes", "")}', manual_next_follow, now))

    # 如果是新客户，自动创建 15/30/60 天跟进提醒（作为补充提醒）
    if data.get('customer_type') == 'new':
        customer_name = data.get('name', '')
        created_date = datetime.now()

        # 15天/30天/60天 自动开发提醒
        for days, label in [(15, '15天'), (30, '30天'), (60, '60天')]:
            target_date = (created_date + timedelta(days=days)).strftime('%Y-%m-%d')
            c.execute('''
                INSERT INTO reminders (customer_id, content, remind_date, is_done, reminder_type, created_at)
                VALUES (?, ?, ?, 0, ?, ?)
            ''', (customer_id, f'新客户开发跟进（{label}）: {customer_name}', target_date, f'outreach_{label}', now))

        # next_follow_up：手动设置的优先，否则默认 15 天
        final_next = manual_next_follow if manual_next_follow else (created_date + timedelta(days=15)).strftime('%Y-%m-%d')
        c.execute('UPDATE customers SET next_follow_up = ? WHERE id = ?', (final_next, customer_id))

    conn.commit()
    conn.close()

    # 记录操作日志
    log_operation('CREATE', 'customer', customer_id, f'创建客户: {data.get("name", "")}')

    return jsonify({'id': customer_id, 'message': '客户创建成功'}), 201


@app.route('/api/customers/<int:customer_id>', methods=['PUT'])
def update_customer(customer_id):
    """更新客户信息"""
    data = request.json
    conn = get_db()
    c = conn.cursor()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 获取原客户的下次跟进日期和 manual_next_follow 标记
    c.execute('SELECT next_follow_up, name, manual_next_follow FROM customers WHERE id = ?', (customer_id,))
    old = c.fetchone()
    old_date = old['next_follow_up'] if old else ''
    customer_name = old['name'] if old else data.get('name', '')
    old_manual = old['manual_next_follow'] if old else 0

    # 检查是否手动修改了下次跟进日期
    new_next_follow = data.get('next_follow_up', '')
    if new_next_follow and new_next_follow != old_date:
        is_manual_date = 1
    else:
        is_manual_date = old_manual

    # 根据状态自动判断客户类型：未建联 → New Client Pool，其他 → Existing
    new_status = data.get('status', '')
    auto_customer_type = 'new' if new_status == '未建联' else 'existing'

    c.execute('''
        UPDATE customers SET
            name = ?, company = ?, country = ?, level = ?, type = ?,
            website = ?, profile = ?, field = ?, status = ?, notes = ?, system_notes = ?,
            last_contact = ?, next_follow_up = ?, manual_next_follow = ?, customer_type = ?,
            industry = ?, company_size = ?, annual_revenue = ?, updated_at = ?
        WHERE id = ?
    ''', (
        data.get('name', ''),
        data.get('company', ''),
        normalize_country(data.get('country', '')),
        data.get('level', 'C'),
        data.get('type', ''),
        data.get('website', ''),
        data.get('profile', ''),
        data.get('field', ''),
        new_status,
        data.get('notes', ''),
        data.get('system_notes', ''),
        data.get('last_contact', ''),
        new_next_follow,
        is_manual_date,
        auto_customer_type,
        data.get('industry', ''),
        data.get('company_size', ''),
        data.get('annual_revenue', ''),
        now,
        customer_id
    ))

    # 如果下次跟进日期变了，更新提醒
    new_date = data.get('next_follow_up', '')
    if new_date and new_date != old_date:
        # 关闭旧的未完成提醒
        c.execute('UPDATE reminders SET is_done = 1 WHERE customer_id = ? AND is_done = 0', (customer_id,))
        # 创建新提醒
        c.execute('''
            INSERT INTO reminders (customer_id, content, remind_date, is_done, created_at)
            VALUES (?, ?, ?, 0, ?)
        ''', (customer_id, f'跟进 {customer_name}: {data.get("notes", "")}', new_date, now))

    conn.commit()
    conn.close()

    # 记录操作日志
    log_operation('UPDATE', 'customer', customer_id, f'更新客户: {data.get("name", "")}')

    return jsonify({'message': '客户更新成功'})


@app.route('/api/customers/<int:customer_id>', methods=['DELETE'])
def delete_customer(customer_id):
    """软删除客户（标记为已删除，可恢复）"""
    conn = get_db()
    c = conn.cursor()

    # 获取客户名称用于日志
    c.execute('SELECT name FROM customers WHERE id = ?', (customer_id,))
    row = c.fetchone()
    customer_name = row['name'] if row else '未知'

    # 软删除：标记 is_deleted = 1
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('UPDATE customers SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE id = ?',
              (now, now, customer_id))

    conn.commit()
    conn.close()

    # 记录操作日志
    log_operation('SOFT_DELETE', 'customer', customer_id, f'移至回收站: {customer_name}')

    return jsonify({'message': f'已将 {customer_name} 移至回收站'})


# ========== 批量操作 API ==========

@app.route('/api/customers/batch/status', methods=['POST'])
def batch_update_status():
    """批量修改客户状态"""
    data = request.json
    ids = data.get('ids', [])
    value = data.get('value', '')
    if not ids or not value:
        return jsonify({'error': '缺少参数'}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute(f'SELECT name FROM customers WHERE id IN ({",".join("?" * len(ids))})', ids)
    names = [row[0] for row in c.fetchall()]
    
    c.execute(f'UPDATE customers SET status = ?, updated_at = ? WHERE id IN ({",".join("?" * len(ids))})',
              [value, datetime.now().strftime('%Y-%m-%d %H:%M:%S')] + ids)
    conn.commit()
    conn.close()
    
    log_operation('BATCH_UPDATE', 'customer', None, f'批量修改状态为"{value}": {", ".join(names[:5])}{"..." if len(names) > 5 else ""}')



    return jsonify({'message': f'已修改 {len(ids)} 个客户状态为 {value}'})


@app.route('/api/customers/batch/level', methods=['POST'])
def batch_update_level():
    """批量修改客户等级"""
    data = request.json
    ids = data.get('ids', [])
    value = data.get('value', '')
    if not ids or not value:
        return jsonify({'error': '缺少参数'}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute(f'SELECT name FROM customers WHERE id IN ({",".join("?" * len(ids))})', ids)
    names = [row[0] for row in c.fetchall()]
    
    c.execute(f'UPDATE customers SET level = ?, updated_at = ? WHERE id IN ({",".join("?" * len(ids))})',
              [value, datetime.now().strftime('%Y-%m-%d %H:%M:%S')] + ids)
    conn.commit()
    conn.close()
    
    log_operation('BATCH_UPDATE', 'customer', None, f'批量修改等级为"{value}": {", ".join(names[:5])}{"..." if len(names) > 5 else ""}')



    return jsonify({'message': f'已修改 {len(ids)} 个客户等级为 {value}'})


@app.route('/api/customers/batch/delete', methods=['POST'])
def batch_delete_customers():
    """批量软删除客户（移至回收站）"""
    data = request.json
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'error': '缺少参数'}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute(f'SELECT name FROM customers WHERE id IN ({",".join("?" * len(ids))})', ids)
    names = [row[0] for row in c.fetchall()]
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(f'UPDATE customers SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE id IN ({",".join("?" * len(ids))})',
              [now, now] + ids)
    conn.commit()
    conn.close()
    
    log_operation('BATCH_SOFT_DELETE', 'customer', None, f'批量移至回收站: {", ".join(names[:5])}{"..." if len(names) > 5 else ""}')



    return jsonify({'message': f'已将 {len(ids)} 个客户移至回收站'})


@app.route('/api/customers/<int:customer_id>/restore', methods=['POST'])
def restore_customer(customer_id):
    """从回收站恢复客户"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('SELECT name FROM customers WHERE id = ? AND is_deleted = 1', (customer_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': '客户不存在或未在回收站中'}), 404
    
    customer_name = row['name']
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('UPDATE customers SET is_deleted = 0, deleted_at = "", updated_at = ? WHERE id = ?', (now, customer_id))
    conn.commit()
    conn.close()
    
    log_operation('RESTORE', 'customer', customer_id, f'从回收站恢复: {customer_name}')
    return jsonify({'message': f'已恢复 {customer_name}'})


@app.route('/api/customers/<int:customer_id>/permanent', methods=['DELETE'])
def permanent_delete_customer(customer_id):
    """永久删除客户（物理删除，不可恢复）"""
    conn = get_db()
    c = conn.cursor()

    c.execute('SELECT name FROM customers WHERE id = ?', (customer_id,))
    row = c.fetchone()
    customer_name = row['name'] if row else '未知'

    # 级联物理删除
    c.execute('DELETE FROM follow_up_logs WHERE customer_id = ?', (customer_id,))
    c.execute('DELETE FROM reminders WHERE customer_id = ?', (customer_id,))
    c.execute('DELETE FROM contacts WHERE customer_id = ?', (customer_id,))
    c.execute('DELETE FROM outreach_emails WHERE customer_id = ?', (customer_id,))
    c.execute('DELETE FROM research_reports WHERE customer_id = ?', (customer_id,))
    c.execute('DELETE FROM customers WHERE id = ?', (customer_id,))

    conn.commit()
    conn.close()

    log_operation('PERMANENT_DELETE', 'customer', customer_id, f'永久删除: {customer_name}')
    return jsonify({'message': f'已永久删除 {customer_name}'})


@app.route('/api/customers/recycle-bin/empty', methods=['POST'])
def empty_recycle_bin():
    """清空回收站（永久删除所有已删除客户）"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) as cnt FROM customers WHERE is_deleted = 1')
    count = c.fetchone()['cnt']
    
    if count == 0:
        conn.close()
        return jsonify({'message': '回收站已为空'})
    
    c.execute('SELECT id, name FROM customers WHERE is_deleted = 1')
    deleted = c.fetchall()
    
    for row in deleted:
        c.execute('DELETE FROM follow_up_logs WHERE customer_id = ?', (row['id'],))
        c.execute('DELETE FROM reminders WHERE customer_id = ?', (row['id'],))
        c.execute('DELETE FROM contacts WHERE customer_id = ?', (row['id'],))
        c.execute('DELETE FROM outreach_emails WHERE customer_id = ?', (row['id'],))
        c.execute('DELETE FROM research_reports WHERE customer_id = ?', (row['id'],))
        c.execute('DELETE FROM customers WHERE id = ?', (row['id'],))
    
    conn.commit()
    conn.close()
    
    log_operation('EMPTY_RECYCLE_BIN', 'customer', None, f'清空回收站，永久删除 {count} 个客户')
    return jsonify({'message': f'已清空回收站，永久删除 {count} 个客户'})


@app.route('/api/customers/recycle-bin/count', methods=['GET'])
def get_recycle_bin_count():
    """获取回收站中的客户数量"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) as cnt FROM customers WHERE is_deleted = 1')
    count = c.fetchone()['cnt']
    conn.close()
    return jsonify({'count': count})


@app.route('/api/reminders/batch/complete', methods=['POST'])
def batch_complete_reminders():
    """批量完成提醒"""
    data = request.json
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'error': '缺少参数'}), 400
    
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 收集批量完成数据（用于后续同步日志，必须在 conn.close() 前完成）
    batch_items = []
    for rid in ids:
        c.execute('SELECT r.*, c.name as customer_name FROM reminders r JOIN customers c ON r.customer_id = c.id WHERE r.id = ?', (rid,))
        reminder = c.fetchone()
        if reminder:
            c.execute('UPDATE reminders SET is_done = 1 WHERE id = ?', (rid,))
            c.execute('''
                INSERT INTO follow_up_logs (customer_id, content, follow_date, result, next_plan, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (reminder['customer_id'], reminder['content'], reminder['remind_date'], 
                  '批量完成', '', 'manual', now))
            batch_items.append({'reminder_id': reminder['id'], 'customer_id': reminder['customer_id'], 'content': reminder['content']})
    
    conn.commit()
    conn.close()
    
    log_operation('BATCH_COMPLETE', 'reminder', None, f'批量完成 {len(ids)} 条提醒')



    return jsonify({'message': f'已完成 {len(ids)} 条提醒'})


# ========== 提醒 API ==========

@app.route('/api/reminders/today', methods=['GET'])
def get_today_reminders():
    """获取今日及逾期提醒（含客户基本信息）"""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        SELECT r.*,
               c.name as customer_name, c.company as customer_company,
               c.country, c.level, c.status, c.field, c.website,
               c.profile, c.last_contact, c.notes as customer_notes,
               c.type as customer_type
        FROM reminders r
        JOIN customers c ON r.customer_id = c.id
        WHERE r.is_done = 0 AND r.remind_date <= ?
          AND r.reminder_type NOT LIKE 'outreach_%'
        ORDER BY r.remind_date ASC, c.level DESC
    ''', (today,))

    reminders = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(reminders)


@app.route('/api/reminders/upcoming', methods=['GET'])
def get_upcoming_reminders():
    """获取未来提醒（含客户基本信息）"""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        SELECT r.*,
               c.name as customer_name, c.company as customer_company,
               c.country, c.level, c.status, c.field, c.website,
               c.profile, c.last_contact, c.notes as customer_notes,
               c.type as customer_type
        FROM reminders r
        JOIN customers c ON r.customer_id = c.id
        WHERE r.is_done = 0 AND r.remind_date > ?
          AND r.reminder_type NOT LIKE 'outreach_%'
        ORDER BY r.remind_date ASC
    ''', (today,))

    reminders = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(reminders)


@app.route('/api/reminders/<int:reminder_id>', methods=['PUT'])
def complete_reminder(reminder_id):
    """记录本次跟进，设置下次跟进日期 + 备注"""
    data = request.json or {}
    result_text = data.get('result', '')
    next_follow_date = data.get('next_follow_up', '')

    conn = get_db()
    c = conn.cursor()

    # 获取提醒信息
    c.execute('''
        SELECT r.*, c.name as customer_name, c.customer_type as customer_type
        FROM reminders r
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ?
    ''', (reminder_id,))
    reminder = c.fetchone()

    if not reminder:
        conn.close()
        return jsonify({'error': '提醒不存在'}), 404

    # 1) 标记当前提醒为已完成
    c.execute('UPDATE reminders SET is_done = 1 WHERE id = ?', (reminder_id,))

    # 2) 记录本次跟进历史
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''
        INSERT INTO follow_up_logs (customer_id, content, follow_date, result, next_plan, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        reminder['customer_id'],
        reminder['content'],
        reminder['remind_date'],
        result_text,
        next_follow_date,
        now
    ))

    # 3) 如果设置了下次跟进日期，为客户创建新的提醒
    customer_id = reminder['customer_id']
    next_follow_message = ''
    if next_follow_date:
        # 更新客户表的 next_follow_up 与 last_contact
        c.execute('''
            UPDATE customers SET
                next_follow_up = ?,
                manual_next_follow = 1,
                last_contact = ?,
                status = CASE WHEN status = '未建联' THEN '跟进中' ELSE status END,
                notes = ?
            WHERE id = ?
        ''', (
            next_follow_date,
            datetime.now().strftime('%Y-%m-%d'),
            result_text or '',
            customer_id
        ))
        # 创建新的提醒
        c.execute('''
            INSERT INTO reminders (customer_id, content, remind_date, is_done, reminder_type, created_at)
            VALUES (?, ?, ?, 0, 'follow_up', ?)
        ''', (customer_id, f'跟进 {reminder["customer_name"]}: {result_text or "继续跟进"}', next_follow_date, now))
        next_follow_message = f'，下次跟进日期：{next_follow_date}'

    conn.commit()
    conn.close()

    log_operation('FOLLOW_UP', 'reminder', reminder_id,
                  f'记录跟进: {reminder["customer_name"]} - {result_text}{next_follow_message}')

    return jsonify({'message': f'跟进记录已保存{next_follow_message}'})


@app.route('/api/reminders/<int:reminder_id>', methods=['DELETE'])
def delete_reminder(reminder_id):
    """删除提醒"""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM reminders WHERE id = ?', (reminder_id,))
    conn.commit()
    conn.close()

    log_operation('DELETE', 'reminder', reminder_id, '删除提醒')
    return jsonify({'message': '提醒已删除'})


# ========== 跟进历史 API ==========

@app.route('/api/customers/<int:customer_id>/follow_history', methods=['GET'])
def get_follow_history(customer_id):
    """获取客户的跟进历史"""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT f.*, c.name as customer_name
        FROM follow_up_logs f
        JOIN customers c ON f.customer_id = c.id
        WHERE f.customer_id = ?
        ORDER BY f.follow_date DESC, f.created_at DESC
    ''', (customer_id,))
    history = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(history)


# ========== 全局跟进历史 API ==========

@app.route('/api/follow-history', methods=['GET'])
def get_all_follow_history():
    """获取所有跟进历史（最新50条）"""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT f.*, c.name as customer_name
        FROM follow_up_logs f
        JOIN customers c ON f.customer_id = c.id
        ORDER BY f.follow_date DESC, f.created_at DESC
        LIMIT 50
    ''')
    history = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(history)


@app.route('/api/follow-history/<int:log_id>', methods=['PUT'])
def update_follow_history(log_id):
    """编辑跟进历史记录"""
    data = request.json or {}
    conn = get_db()
    c = conn.cursor()
    
    c.execute('SELECT id FROM follow_up_logs WHERE id = ?', (log_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': '记录不存在'}), 404
    
    c.execute('''
        UPDATE follow_up_logs
        SET follow_date = ?, content = ?, result = ?, next_plan = ?
        WHERE id = ?
    ''', (
        data.get('follow_date', ''),
        data.get('content', ''),
        data.get('result', ''),
        data.get('next_plan', ''),
        log_id
    ))
    
    conn.commit()
    
    # 记录操作日志
    log_operation('update', 'follow_up_log', log_id, f'Edited follow-up log #{log_id}')
    
    c.execute('''
        SELECT f.*, c.name as customer_name
        FROM follow_up_logs f
        JOIN customers c ON f.customer_id = c.id
        WHERE f.id = ?
    ''', (log_id,))
    updated = dict(c.fetchone())
    conn.close()
    return jsonify(updated)


@app.route('/api/follow-history/<int:log_id>', methods=['DELETE'])
def delete_follow_history(log_id):
    """删除跟进历史记录"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('SELECT id FROM follow_up_logs WHERE id = ?', (log_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': '记录不存在'}), 404
    
    c.execute('DELETE FROM follow_up_logs WHERE id = ?', (log_id,))
    conn.commit()
    
    log_operation('delete', 'follow_up_log', log_id, f'Deleted follow-up log #{log_id}')
    conn.close()
    return jsonify({'success': True})


# ========== 联系人 API ==========

@app.route('/api/customers/<int:customer_id>/contacts', methods=['GET'])
def get_contacts(customer_id):
    """获取客户联系人"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM contacts WHERE customer_id = ? ORDER BY is_primary DESC, created_at DESC', (customer_id,))
    contacts = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(contacts)


@app.route('/api/customers/<int:customer_id>/contacts', methods=['POST'])
def add_contact(customer_id):
    """添加联系人"""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    c.execute('''
        INSERT INTO contacts (customer_id, name, title, email, phone, linkedin, is_primary, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (customer_id, data.get('name', ''), data.get('title', ''), data.get('email', ''),
          data.get('phone', ''), data.get('linkedin', ''), data.get('is_primary', 0),
          data.get('notes', ''), now))
    
    conn.commit()
    conn.close()
    log_operation('CREATE', 'contact', customer_id, f'添加联系人: {data.get("name", "")}')



    return jsonify({'message': '联系人添加成功'}), 201


@app.route('/api/contacts/<int:contact_id>', methods=['PUT'])
def update_contact(contact_id):
    """更新联系人"""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''
        UPDATE contacts SET
            name = ?, title = ?, email = ?, phone = ?, linkedin = ?, is_primary = ?, notes = ?
        WHERE id = ?
    ''', (data.get('name', ''), data.get('title', ''), data.get('email', ''),
          data.get('phone', ''), data.get('linkedin', ''), data.get('is_primary', 0),
          data.get('notes', ''), contact_id))
    
    conn.commit()
    conn.close()
    log_operation('UPDATE', 'contact', contact_id, f'更新联系人: {data.get("name", "")}')
    return jsonify({'message': '联系人更新成功'})


@app.route('/api/contacts/<int:contact_id>', methods=['DELETE'])
def delete_contact(contact_id):
    """删除联系人"""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM contacts WHERE id = ?', (contact_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'contact', contact_id, '删除联系人')
    return jsonify({'message': '联系人删除成功'})


# ========== 开发信 API ==========

@app.route('/api/customers/<int:customer_id>/outreach', methods=['GET'])
def get_outreach_emails(customer_id):
    """获取客户开发信记录"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM outreach_emails WHERE customer_id = ? ORDER BY sent_date DESC, created_at DESC', (customer_id,))
    emails = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(emails)


@app.route('/api/customers/<int:customer_id>/outreach', methods=['POST'])
def add_outreach_email(customer_id):
    """添加开发信记录"""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    c.execute('''
        INSERT INTO outreach_emails (customer_id, subject, content, sent_date, reply_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (customer_id, data.get('subject', ''), data.get('content', ''),
          data.get('sent_date', ''), data.get('reply_status', 'pending'), now))
    
    conn.commit()
    conn.close()
    log_operation('CREATE', 'outreach', customer_id, f'添加开发信: {data.get("subject", "")}')



    return jsonify({'message': '开发信记录添加成功'}), 201


@app.route('/api/outreach/<int:outreach_id>', methods=['PUT'])
def update_outreach_email(outreach_id):
    """更新开发信记录（如收到回复）"""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''
        UPDATE outreach_emails SET
            reply_status = ?, reply_content = ?, reply_date = ?
        WHERE id = ?
    ''', (data.get('reply_status', 'pending'), data.get('reply_content', ''),
          data.get('reply_date', ''), outreach_id))
    
    conn.commit()
    conn.close()
    log_operation('UPDATE', 'outreach', outreach_id, f'更新开发信回复状态: {data.get("reply_status", "")}')
    return jsonify({'message': '开发信记录更新成功'})


@app.route('/api/outreach/<int:outreach_id>', methods=['DELETE'])
def delete_outreach_email(outreach_id):
    """删除开发信记录"""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM outreach_emails WHERE id = ?', (outreach_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'outreach', outreach_id, '删除开发信记录')
    return jsonify({'message': '开发信记录删除成功'})


# ========== 背调报告 API ==========

@app.route('/api/customers/<int:customer_id>/research', methods=['GET'])
def get_research_report(customer_id):
    """获取客户背调报告"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM research_reports WHERE customer_id = ?', (customer_id,))
    report = c.fetchone()
    conn.close()
    return jsonify(dict(report) if report else None)


@app.route('/api/customers/<int:customer_id>/research', methods=['POST'])
def create_research_report(customer_id):
    """创建/更新背调报告"""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 检查是否已存在
    c.execute('SELECT id FROM research_reports WHERE customer_id = ?', (customer_id,))
    existing = c.fetchone()
    
    if existing:
        c.execute('''
            UPDATE research_reports SET
                summary = ?, company_info = ?, key_findings = ?, needs_analysis = ?,
                cooperation_value = ?, raw_input = ?, updated_at = ?
            WHERE customer_id = ?
        ''', (data.get('summary', ''), data.get('company_info', ''), data.get('key_findings', ''),
              data.get('needs_analysis', ''), data.get('cooperation_value', ''),
              data.get('raw_input', ''), now, customer_id))
    else:
        c.execute('''
            INSERT INTO research_reports (customer_id, summary, company_info, key_findings, needs_analysis, cooperation_value, raw_input, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (customer_id, data.get('summary', ''), data.get('company_info', ''),
              data.get('key_findings', ''), data.get('needs_analysis', ''),
              data.get('cooperation_value', ''), data.get('raw_input', ''), now, now))
    
    conn.commit()
    conn.close()
    log_operation('CREATE' if not existing else 'UPDATE', 'research', customer_id, '创建/更新背调报告')



    return jsonify({'message': '背调报告保存成功'})


@app.route('/api/customers/<int:customer_id>/research', methods=['DELETE'])
def delete_research_report(customer_id):
    """删除背调报告"""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM research_reports WHERE customer_id = ?', (customer_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'research', customer_id, '删除背调报告')
    return jsonify({'message': '背调报告删除成功'})


# ========== 智能导入 API ==========

# 域名后缀到国家的映射
DOMAIN_COUNTRY_MAP = {
    '.com': ['美国', '全球'],
    '.com.au': ['澳大利亚'],
    '.com.cn': ['中国'],
    '.cn': ['中国'],
    '.com.hk': ['中国香港'],
    '.hk': ['中国香港'],
    '.com.tw': ['中国台湾'],
    '.tw': ['中国台湾'],
    '.jp': ['日本'],
    '.co.jp': ['日本'],
    '.kr': ['韩国'],
    '.co.kr': ['韩国'],
    '.de': ['德国'],
    '.at': ['奥地利'],
    '.ch': ['瑞士'],
    '.fr': ['法国'],
    '.uk': ['英国'],
    '.co.uk': ['英国'],
    '.ie': ['爱尔兰'],
    '.nl': ['荷兰'],
    '.be': ['比利时'],
    '.lu': ['卢森堡'],
    '.es': ['西班牙'],
    '.pt': ['葡萄牙'],
    '.it': ['意大利'],
    '.gr': ['希腊'],
    '.cy': ['塞浦路斯'],
    '.mt': ['马耳他'],
    '.si': ['斯洛文尼亚'],
    '.hr': ['克罗地亚'],
    '.bg': ['保加利亚'],
    '.ro': ['罗马尼亚'],
    '.hu': ['匈牙利'],
    '.pl': ['波兰'],
    '.cz': ['捷克'],
    '.sk': ['斯洛伐克'],
    '.ee': ['爱沙尼亚'],
    '.lv': ['拉脱维亚'],
    '.lt': ['立陶宛'],
    '.fi': ['芬兰'],
    '.se': ['瑞典'],
    '.no': ['挪威'],
    '.dk': ['丹麦'],
    '.is': ['冰岛'],
    '.ru': ['俄罗斯'],
    '.by': ['白俄罗斯'],
    '.ua': ['乌克兰'],
    '.kz': ['哈萨克斯坦'],
    '.ae': ['阿联酋'],
    '.sa': ['沙特阿拉伯'],
    '.eg': ['埃及'],
    '.za': ['南非'],
    '.co.za': ['南非'],
    '.com.sg': ['新加坡'],
    '.sg': ['新加坡'],
    '.my': ['马来西亚'],
    '.com.my': ['马来西亚'],
    '.id': ['印度尼西亚'],
    '.co.id': ['印度尼西亚'],
    '.th': ['泰国'],
    '.co.th': ['泰国'],
    '.vn': ['越南'],
    '.com.vn': ['越南'],
    '.ph': ['菲律宾'],
    '.com.ph': ['菲律宾'],
    '.in': ['印度'],
    '.co.in': ['印度'],
    '.pk': ['巴基斯坦'],
    '.bd': ['孟加拉国'],
    '.nz': ['新西兰'],
    '.co.nz': ['新西兰'],
    '.ca': ['加拿大'],
    '.mx': ['墨西哥'],
    '.com.mx': ['墨西哥'],
    '.br': ['巴西'],
    '.com.br': ['巴西'],
    '.ar': ['阿根廷'],
    '.cl': ['智利'],
    '.pe': ['秘鲁'],
    '.co': ['哥伦比亚'],
}

# 行业列表
INDUSTRY_LIST = [
    '亚克力分销',
    '工程塑料分销',
    '塑料板材分销',
    '标牌制造',
    '建筑建材',
    '家具制造',
    '照明灯具',
    '电子电器',
    '汽车相关',
    '医疗器械',
    '日用品消费品',
    '包装印刷',
    '工业制造',
    '贸易商/进口商',
    '其他',
]

@app.route('/api/customers/smart-import', methods=['POST'])
def smart_import_customer():
    """智能导入客户：根据公司名或网站自动填充信息"""
    data = request.json
    company_input = data.get('company', '').strip()
    website_input = data.get('website', '').strip()
    
    result = {
        'name': company_input,
        'company': company_input,
        'country': '',
        'field': '',
        'website': website_input,
        'profile': '',
        'notes': '',
        'auto_filled': [],
    }
    
    # 解析网站 URL，提取域名和国家
    if website_input:
        parsed = parse_website(website_input)
        if parsed['domain']:
            result['website'] = parsed['domain']
            if parsed['country']:
                result['country'] = ', '.join(parsed['country'])
                result['auto_filled'].append('country')
        
        # 尝试从域名推断行业
        inferred_field = infer_field_from_domain(parsed['domain'])
        if inferred_field:
            result['field'] = inferred_field
            result['auto_filled'].append('field')
    
    # 如果没有网站但有公司名，尝试从公司名推断
    if company_input and not website_input:
        inferred_field = infer_field_from_name(company_input)
        if inferred_field:
            result['field'] = inferred_field
            result['auto_filled'].append('field')
    
    # 如果没有国家，默认使用常见商业国家
    if not result['country']:
        result['country'] = '美国'
    
    return jsonify(result)


def parse_website(url):
    """解析网站 URL，提取域名和推断国家"""
    try:
        from urllib.parse import urlparse
        
        # 处理没有协议的URL
        if not url.startswith('http'):
            url = 'http://' + url
        
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        
        # 移除 www.
        if domain.startswith('www.'):
            domain = domain[4:]
        
        # 移除端口
        if ':' in domain:
            domain = domain.split(':')[0]
        
        # 推断国家
        country = []
        for suffix, countries in DOMAIN_COUNTRY_MAP.items():
            if domain.endswith(suffix):
                country.extend(countries)
                break
        
        # 如果没有匹配到，尝试二级域名
        if not country and '.' in domain:
            parts = domain.split('.')
            if len(parts) >= 2:
                tld = '.' + parts[-1]
                if tld in DOMAIN_COUNTRY_MAP:
                    country.extend(DOMAIN_COUNTRY_MAP[tld])
        
        return {
            'domain': 'https://' + domain,
            'country': list(set(country)),
        }
    except Exception:
        return {'domain': url, 'country': []}


def infer_field_from_domain(domain):
    """从域名推断行业"""
    domain_lower = domain.lower()
    keywords = {
        'acrylic': '亚克力分销',
        'plastic': '工程塑料分销',
        'plastics': '工程塑料分销',
        'sheet': '塑料板材分销',
        'sign': '标牌制造',
        'signage': '标牌制造',
        'advertising': '标牌制造',
        'building': '建筑建材',
        'construction': '建筑建材',
        'furniture': '家具制造',
        'lighting': '照明灯具',
        'light': '照明灯具',
        'electronics': '电子电器',
        'electronic': '电子电器',
        'automotive': '汽车相关',
        'auto': '汽车相关',
        'medical': '医疗器械',
        'healthcare': '医疗器械',
        'consumer': '日用品消费品',
        'packaging': '包装印刷',
        'printing': '包装印刷',
        'industrial': '工业制造',
        'trade': '贸易商/进口商',
        'import': '贸易商/进口商',
        'distributor': '贸易商/进口商',
        'distribution': '贸易商/进口商',
    }
    
    for keyword, field in keywords.items():
        if keyword in domain_lower:
            return field
    return None


def infer_field_from_name(name):
    """从公司名称推断行业"""
    name_lower = name.lower()
    keywords = {
        'acrylic': '亚克力分销',
        'plastic': '工程塑料分销',
        'plastics': '工程塑料分销',
        'sheet': '塑料板材分销',
        'sign': '标牌制造',
        'signage': '标牌制造',
        'advertising': '标牌制造',
        'build': '建筑建材',
        'construction': '建筑建材',
        'furniture': '家具制造',
        'lighting': '照明灯具',
        'light': '照明灯具',
        'electronics': '电子电器',
        'electronic': '电子电器',
        'auto': '汽车相关',
        'automotive': '汽车相关',
        'medical': '医疗器械',
        'healthcare': '医疗器械',
        'consumer': '日用品消费品',
        'packaging': '包装印刷',
        'printing': '包装印刷',
        'industrial': '工业制造',
        'trade': '贸易商/进口商',
        'import': '贸易商/进口商',
        'distributor': '贸易商/进口商',
        'distribution': '贸易商/进口商',
    }
    
    for keyword, field in keywords.items():
        if keyword in name_lower:
            return field
    return None


# ========== AI 背调生成 API ==========

@app.route('/api/research/generate', methods=['POST'])
def generate_research_report():
    """根据输入文本生成结构化背调报告"""
    data = request.json
    raw_input = data.get('raw_input', '')
    company_name = data.get('company_name', '')
    
    if not raw_input:
        return jsonify({'error': '请输入背调内容'}), 400
    
    # 使用简单的模板提取，实际可集成 LLM API
    report = parse_research_input(raw_input, company_name)
    return jsonify(report)


def parse_research_input(raw_input, company_name=''):
    """解析背调文本，提取结构化信息"""
    lines = raw_input.split('\n')
    
    # 提取公司名称
    if not company_name:
        for line in lines:
            if '公司名称' in line or 'Company' in line:
                parts = line.split(':')
                if len(parts) > 1:
                    company_name = parts[1].strip()
                    break
    
    # 提取摘要（第一行或包含"战略价值"的句子）
    summary = ''
    for line in lines:
        if '战略价值' in line or '最具' in line:
            summary = line.strip()
            break
    if not summary and lines:
        summary = lines[0][:200]
    
    # 提取核心信息（表格部分）
    company_info = []
    in_table = False
    for line in lines:
        if '背调查要' in line or '公司介绍' in line:
            in_table = True
        if in_table and '|' in line:
            company_info.append(line.strip())
        if in_table and ('3条' in line or '需求' in line):
            break
    
    # 提取需求分析
    needs_analysis = []
    in_needs = False
    for line in lines:
        if '3条最可能' in line or '需求' in line:
            in_needs = True
        if in_needs and (line.strip().startswith('1.') or line.strip().startswith('2.') or line.strip().startswith('3.')):
            needs_analysis.append(line.strip())
    
    # 提取合作价值
    cooperation_value = ''
    for line in lines:
        if '合作价值' in line or '优先级' in line:
            cooperation_value = line.strip()
            break
    
    return {
        'company_name': company_name,
        'summary': summary,
        'company_info': '\n'.join(company_info),
        'key_findings': '',
        'needs_analysis': '\n'.join(needs_analysis),
        'cooperation_value': cooperation_value,
        'raw_input': raw_input
    }


# ========== 统计 API ==========

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """获取仪表盘统计数据"""
    conn = get_db()
    c = conn.cursor()

    today = datetime.now().strftime('%Y-%m-%d')

    c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
    total = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM customers WHERE customer_type = ? AND (is_deleted = 0 OR is_deleted IS NULL)', ('new',))
    new_customers = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM customers WHERE customer_type = ? AND (is_deleted = 0 OR is_deleted IS NULL)', ('existing',))
    existing_customers = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date <= ?', (today,))
    pending = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date < ?', (today,))
    overdue = c.fetchone()[0]

    # 状态分布 - 基于 next_follow_up 智能归类
    c.execute('SELECT status, next_follow_up FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
    rows = c.fetchall()
    
    status_counts = {'未建联': 0, '已建联': 0, '跟进中': 0, '成交': 0, '流失': 0, '': 0}
    for status, nfu in rows:
        s = status or ''
        # 智能归类：根据 next_follow_up 距离今天的天数
        if s == '跟进中':
            if nfu and nfu.strip():
                try:
                    nfu_date = datetime.strptime(nfu.strip()[:10], '%Y-%m-%d')
                    days_diff = (nfu_date - datetime.now()).days
                    if days_diff <= 30:
                        status_counts['跟进中'] += 1
                    else:
                        status_counts['已建联'] += 1
                except ValueError:
                    # 日期格式不对或空，归为已建联（偶尔跟进）
                    status_counts['已建联'] += 1
            else:
                # 无下次跟进日期，归为已建联（偶尔跟进）
                status_counts['已建联'] += 1
        elif s == '已建联':
            if nfu and nfu.strip():
                try:
                    nfu_date = datetime.strptime(nfu.strip()[:10], '%Y-%m-%d')
                    days_diff = (nfu_date - datetime.now()).days
                    if days_diff <= 30:
                        status_counts['跟进中'] += 1
                    else:
                        status_counts['已建联'] += 1
                except ValueError:
                    status_counts['已建联'] += 1
            else:
                status_counts['已建联'] += 1
        else:
            status_counts[s] += 1
    
    # 移除空字符串计数
    status_counts.pop('', None)

    c.execute('SELECT level, COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL) GROUP BY level')
    level_counts = {row[0]: row[1] for row in c.fetchall()}

    # 回收站数量
    c.execute('SELECT COUNT(*) FROM customers WHERE is_deleted = 1')
    deleted_count = c.fetchone()[0]

    conn.close()

    following = status_counts.get('跟进中', 0)

    return jsonify({
        'total': total,
        'new_customers': new_customers,
        'existing_customers': existing_customers,
        'deleted_count': deleted_count,
        'pending': pending,
        'overdue': overdue,
        'following': following,
        'status_counts': status_counts,
        'level_counts': level_counts,
    })


# ========== Excel 同步 API ==========

# ========== Excel 上传 ==========
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'uploads')
UPLOAD_SOURCE_FILE = os.path.join(UPLOAD_DIR, '_source.json')


def get_uploaded_excel_path():
    """获取用户上传的 Excel 路径"""
    try:
        if os.path.exists(UPLOAD_SOURCE_FILE):
            import json
            with open(UPLOAD_SOURCE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            path = data.get('path', '')
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    return None


@app.route('/api/excel/upload', methods=['POST'])
def upload_excel():
    """上传 Excel 文件并自动导入"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '请选择文件'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': '文件名为空'}), 400

    # 检查扩展名
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.xlsx', '.xls'):
        return jsonify({'success': False, 'error': '仅支持 .xlsx / .xls 格式'}), 400

    # 保存文件
    import json, time
    safe_name = f"uploaded_{int(time.time())}{ext}"
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    file.save(save_path)

    # 记录为当前 Excel 来源
    with open(UPLOAD_SOURCE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'path': save_path, 'original_name': file.filename, 'uploaded_at': datetime.now().isoformat()}, f, ensure_ascii=False)

    # 自动同步
    result = sync_from_excel(save_path)
    if result.get('success'):
        log_operation('UPLOAD_EXCEL', 'system', None,
                      f'上传 {file.filename} | 新增 {result.get("new_customers", 0)} 个, 更新 {result.get("updated_customers", 0)} 个')

    result['file_name'] = file.filename
    result['file_path'] = save_path
    return jsonify(result)


@app.route('/api/excel/info', methods=['GET'])
def excel_info():
    """获取当前 Excel 文件信息"""
    path = get_uploaded_excel_path()
    if path:
        return jsonify({
            'source': 'upload',
            'path': path,
            'name': os.path.basename(path),
            'modified': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')
        })
    found = find_excel_file()
    if found:
        return jsonify({
            'source': 'auto',
            'path': found,
            'name': os.path.basename(found),
            'modified': datetime.fromtimestamp(os.path.getmtime(found)).strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify({'source': None})


@app.route('/api/sync', methods=['POST'])
def sync_excel():
    """手动触发 Excel 同步（Excel -> CRM）"""
    data = request.json or {}
    excel_path = data.get('excel_path')
    result = sync_from_excel(excel_path)

    if result.get('success'):
        removed = result.get('removed_customers', [])
        log_msg = f'Excel同步: 新增{result.get("new_customers", 0)}个, 更新{result.get("updated_customers", 0)}个'
        if removed:
            log_msg += f', 清理孤立客户{len(removed)}个: {", ".join(removed[:5])}{"…" if len(removed) > 5 else ""}'
        log_operation('SYNC', 'system', None, log_msg)

    return jsonify(result)


@app.route('/api/sync/to_excel', methods=['POST'])
def sync_to_excel_api():
    """手动触发双向同步（CRM -> Excel）"""
    data = request.json or {}
    customer_id = data.get('customer_id')
    result = sync_to_excel(customer_id)

    if result.get('success'):
        log_operation('SYNC_TO_EXCEL', 'system', None, result.get('message', ''))

    return jsonify(result)





# ========== 日历订阅 iCal API ==========

@app.route('/api/calendar/ical')
def calendar_ical():
    """生成 iCalendar (.ics) 订阅文件，供 iPhone/Google Calendar 订阅"""
    conn = get_db()
    c = conn.cursor()
    
    # 查询未完成的提醒：仅今日及以后，且每个客户只保留最近的一条跟进
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('''
        SELECT r.id, r.remind_date, r.content, 
               COALESCE(c.company, c.name, 'Unknown') as customer_name,
               r.customer_id
        FROM reminders r
        JOIN customers c ON r.customer_id = c.id
        WHERE r.remind_date >= ?
          AND r.is_done = 0
          AND (c.is_deleted = 0 OR c.is_deleted IS NULL)
        ORDER BY r.customer_id, r.remind_date ASC
    ''', (today,))
    rows = [dict(row) for row in c.fetchall()]
    
    # 去重：每个客户只保留最近的一条跟进
    seen = set()
    reminders = []
    for r in rows:
        if r['customer_id'] not in seen:
            seen.add(r['customer_id'])
            reminders.append(r)
    
    conn.close()
    
    ics_content = build_icalendar(reminders)
    
    from flask import Response
    return Response(
        ics_content,
        mimetype='text/calendar',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        }
    )


# ========== 局域网 IP 检测 API ==========

@app.route('/api/network/ip')
def get_local_ip():
    """检测本机局域网 IP 地址，用于日历订阅链接"""
    import socket
    ips = []
    # 遍历所有网络接口，找出非回环的 IPv4 地址
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if addr and not addr.startswith('127.') and '.' in addr and ':' not in addr:
                # 跳过常见的虚拟网卡地址段
                if not any(addr.startswith(p) for p in ['169.254.', '198.18.', '0.']):
                    ips.append(addr)
    except Exception:
        pass
    
    # 如果上面的方法没拿到，用传统方法
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith('127.'):
                ips.append(ip)
        except Exception:
            pass
    
    local_ip = ips[0] if ips else 'localhost'
    port = request.host.split(':')[1] if ':' in request.host else '8080'
    
    return jsonify({
        'local_ip': local_ip,
        'all_ips': ips,
        'port': port,
        'subscribe_url': f'http://{local_ip}:{port}/api/calendar/ical',
        'test_url': f'http://{local_ip}:{port}/api/network/ping',
    })


@app.route('/api/network/ping')
def network_ping():
    """简单的网络连通性测试端点"""
    return jsonify({'status': 'ok', 'message': 'iPhone 可以访问本服务'})


# ========== 系统信息 API ==========

@app.route('/api/system', methods=['GET'])
def get_system_info():
    """获取系统信息"""
    import os
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
    customer_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reminders')
    reminder_count = c.fetchone()[0]
    conn.close()

    scheduler_info = get_scheduler_status()
    
    return jsonify({
        'db_path': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'crm_reminders.db'),
        'db_exists': True,
        'scheduler_running': scheduler_info.get('running', False),
        'scheduler_jobs': scheduler_info.get('jobs', []),
        'customer_count': customer_count,
        'reminder_count': reminder_count,
    })


# ========== 操作日志 API ==========

@app.route('/api/logs', methods=['GET'])
def get_operation_logs():
    """获取操作日志"""
    limit = request.args.get('limit', 100, type=int)
    action = request.args.get('action', '').strip()

    conn = get_db()
    c = conn.cursor()

    query = 'SELECT * FROM operation_logs WHERE 1=1'
    params = []

    if action:
        query += ' AND action = ?'
        params.append(action)

    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)

    c.execute(query, params)
    logs = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(logs)


# ========== 邮件 API ==========

@app.route('/api/email/config', methods=['GET'])
def get_email_config():
    """获取邮件配置"""
    return jsonify({
        'enabled': False,
        'host': '',
        'port': 0,
        'user': '',
        'to_email': '',
        'use_tls': False,
    })


@app.route('/api/email/logs', methods=['GET'])
def get_email_logs():
    """获取邮件发送记录"""
    limit = request.args.get('limit', 50, type=int)
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('SELECT * FROM email_logs ORDER BY created_at DESC LIMIT ?', (limit,))
        logs = [dict(row) for row in c.fetchall()]
    except Exception:
        logs = []
    conn.close()
    return jsonify(logs)


# ========== 系统健康检测 API ==========

@app.route('/api/health', methods=['GET'])
def system_health_check():
    """系统健康检测：诊断数据库连接、调度器状态、最近错误等"""
    import time
    import platform
    import sys
    
    health = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checks': [],
        'overall': 'healthy'
    }
    
    # 1. 数据库连接检测
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT 1')
        c.execute('SELECT COUNT(*) FROM customers')
        customer_count = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM reminders')
        reminder_count = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date < ?', (datetime.now().strftime('%Y-%m-%d'),))
        overdue_count = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM follow_up_logs')
        follow_count = c.fetchone()[0]
        conn.close()
        health['checks'].append({
            'name': '数据库连接',
            'status': 'ok',
            'detail': f'连接正常 · {customer_count}客户 · {reminder_count}提醒 · {overdue_count}逾期 · {follow_count}跟进记录'
        })
    except Exception as e:
        health['checks'].append({'name': '数据库连接', 'status': 'error', 'detail': str(e)})
        health['overall'] = 'degraded'
    
    # 2. 调度器状态
    try:
        scheduler_info = get_scheduler_status()
        if scheduler_info.get('running'):
            jobs = scheduler_info.get('jobs', [])
            health['checks'].append({
                'name': '调度器',
                'status': 'ok',
                'detail': f'运行中 · {len(jobs)}个任务'
            })
        else:
            health['checks'].append({
                'name': '调度器',
                'status': 'warning',
                'detail': '调度器未运行（首次启动需要手动开启）'
            })
    except Exception as e:
        health['checks'].append({'name': '调度器', 'status': 'error', 'detail': str(e)})
        health['overall'] = 'degraded'
    
    # 3. 日志文件检测
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    try:
        if os.path.exists(log_dir):
            log_files = [f for f in os.listdir(log_dir) if f.endswith('.log')]
            if log_files:
                newest = max(log_files, key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
                log_path = os.path.join(log_dir, newest)
                log_size = os.path.getsize(log_path)
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as lf:
                    lines = lf.readlines()
                    errors_24h = [l for l in lines[-500:] if 'ERROR' in l or 'error' in l or 'Exception' in l]
                health['checks'].append({
                    'name': '日志文件',
                    'status': 'ok',
                    'detail': f'{len(log_files)}个日志 · 最近24h错误: {len(errors_24h)}条'
                })
                if len(errors_24h) > 10:
                    health['checks'][-1]['status'] = 'warning'
                    health['checks'][-1]['detail'] += ' (偏多，建议检查)'
            else:
                health['checks'].append({'name': '日志文件', 'status': 'warning', 'detail': '无日志文件'})
        else:
            health['checks'].append({'name': '日志文件', 'status': 'warning', 'detail': '日志目录不存在'})
    except Exception as e:
        health['checks'].append({'name': '日志文件', 'status': 'error', 'detail': str(e)})
    
    # 4. 系统资源
    try:
        health['checks'].append({
            'name': '系统环境',
            'status': 'ok',
            'detail': f'Python {platform.python_version()} · {platform.system()} {platform.release()}'
        })
    except:
        pass
    
    # 5. 数据库文件大小
    try:
        if os.path.exists(DB_PATH):
            db_size = os.path.getsize(DB_PATH)
            size_str = f'{db_size / 1024:.1f} KB' if db_size < 1024 * 1024 else f'{db_size / (1024*1024):.1f} MB'
            health['checks'].append({'name': '数据库文件', 'status': 'ok', 'detail': size_str})
    except:
        pass
    
    # 6. Excel 同步状态
    try:
        excel_info = get_excel_status()
        if excel_info.get('found'):
            health['checks'].append({'name': 'Excel同步源', 'status': 'ok', 'detail': f'已配置 · 最后修改: {excel_info.get("modified", "未知")}'})
        else:
            health['checks'].append({'name': 'Excel同步源', 'status': 'info', 'detail': '未配置（可选功能）'})
    except:
        pass
    
    return jsonify(health)


# ========== AI 深度调研 API ==========

@app.route('/api/intelligence/submit', methods=['POST'])
def submit_intelligence():
    """录入情报（可选是否进行AI分析）"""
    data = request.json or {}
    customer_id = data.get('customer_id')
    raw_input = data.get('raw_input', '').strip()
    with_ai = data.get('with_ai', True)

    if not customer_id or not raw_input:
        return jsonify({'error': '缺少客户ID或情报内容'}), 400

    conn = get_db()
    c = conn.cursor()

    # 获取客户信息
    c.execute('SELECT * FROM customers WHERE id = ?', (customer_id,))
    customer = c.fetchone()
    if not customer:
        conn.close()
        return jsonify({'error': '客户不存在'}), 404

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if with_ai:
        # 使用 AI 分析单条情报
        try:
            analysis = analyze_single_intelligence(raw_input, dict(customer).get('name', ''))
            summary = analysis.get('summary', '')
            key_findings = analysis.get('key_findings', '')
            needs_analysis = analysis.get('pain_points', '')
            cooperation_value = analysis.get('opportunity', '')
            raw_analysis = analysis.get('suggested_action', '')
            if 'raw_analysis' in analysis:
                raw_analysis = analysis['raw_analysis']
        except Exception as e:
            logger.error(f'AI分析失败: {str(e)}')
            summary = ''
            key_findings = ''
            needs_analysis = ''
            cooperation_value = ''
            raw_input_with_note = f'{raw_input}\n\n[AI分析失败: {str(e)}]'
    else:
        summary = ''
        key_findings = ''
        needs_analysis = ''
        cooperation_value = ''

    # 检查是否已有报告（customer_id 是 UNIQUE 约束）
    c.execute('SELECT id FROM research_reports WHERE customer_id = ?', (customer_id,))
    existing = c.fetchone()
    if existing:
        c.execute('''
            UPDATE research_reports SET
                summary = ?, company_info = ?, key_findings = ?,
                needs_analysis = ?, cooperation_value = ?,
                raw_input = ?, updated_at = ?
            WHERE customer_id = ?
        ''', (summary, '', key_findings,
              needs_analysis, cooperation_value, raw_input, now, customer_id))
        report_id = existing['id']
    else:
        c.execute('''
            INSERT INTO research_reports
                (customer_id, summary, company_info, key_findings,
                 needs_analysis, cooperation_value, raw_input, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (customer_id, summary, '', key_findings,
              needs_analysis, cooperation_value, raw_input, now, now))
        report_id = c.lastrowid

    conn.commit()

    log_operation('INTELLIGENCE', 'customer', customer_id, f'录入情报: {raw_input[:50]}...')



    # 获取该客户所有情报
    c.execute('SELECT * FROM research_reports WHERE customer_id = ? ORDER BY created_at DESC', (customer_id,))
    intelligences = [dict(row) for row in c.fetchall()]
    conn.close()

    return jsonify({
        'report_id': report_id,
        'intelligences': intelligences,
        'message': '情报录入成功' + ('(已AI分析)' if with_ai else '')
    })


@app.route('/api/intelligence/list/<int:customer_id>', methods=['GET'])
def list_intelligences(customer_id):
    """获取客户的所有情报记录"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM research_reports WHERE customer_id = ? ORDER BY created_at DESC', (customer_id,))
    intelligences = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(intelligences)


@app.route('/api/intelligence/analyze/<int:customer_id>', methods=['POST'])
def deep_analyze_customer(customer_id):
    """对客户进行综合深度AI分析"""
    conn = get_db()
    c = conn.cursor()

    # 获取客户基础信息
    c.execute('SELECT * FROM customers WHERE id = ?', (customer_id,))
    customer = c.fetchone()
    if not customer:
        conn.close()
        return jsonify({'error': '客户不存在'}), 404

    # 获取跟进记录
    c.execute('SELECT * FROM follow_up_logs WHERE customer_id = ? ORDER BY follow_date DESC LIMIT 20', (customer_id,))
    follow_ups = [dict(row) for row in c.fetchall()]

    # 获取已有情报
    c.execute('SELECT * FROM research_reports WHERE customer_id = ? ORDER BY created_at DESC LIMIT 10', (customer_id,))
    intelligences = [dict(row) for row in c.fetchall()]
    conn.close()

    try:
        analysis_result = analyze_customer(dict(customer), follow_ups, intelligences)
        log_operation('ANALYZE', 'customer', customer_id, '执行深度AI分析')
        return jsonify({'analysis': analysis_result})
    except Exception as e:
        logger.error(f'深度分析失败: {str(e)}')
        return jsonify({'error': f'AI分析失败: {str(e)}'}), 500


@app.route('/api/intelligence/chat', methods=['POST'])
def intelligence_chat():
    """针对客户的智能问答"""
    data = request.json or {}
    question = data.get('question', '').strip()
    customer_id = data.get('customer_id')

    if not question:
        return jsonify({'error': '请输入问题'}), 400

    customer_context = ''
    if customer_id:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM customers WHERE id = ?', (customer_id,))
        customer = c.fetchone()
        if customer:
            cust = dict(customer)
            context_parts = [
                f"公司: {cust.get('name', '')}",
                f"国家: {cust.get('country', '')}",
                f"领域: {cust.get('field', '')}",
                f"等级: {cust.get('level', '')}",
                f"状态: {cust.get('status', '')}",
            ]
            if cust.get('profile'):
                context_parts.append(f"简介: {cust['profile']}")
            if cust.get('notes'):
                context_parts.append(f"备注: {cust['notes']}")

            # 获取情报摘要
            c.execute('SELECT summary, key_findings, needs_analysis FROM research_reports WHERE customer_id = ? ORDER BY created_at DESC LIMIT 5', (customer_id,))
            reports = c.fetchall()
            if reports:
                context_parts.append('\n已有调研发现:')
                for r in reports[:3]:
                    if r['summary']:
                        context_parts.append(f"- {r['summary']}")
                    if r['key_findings']:
                        context_parts.append(f"  关键发现: {r['key_findings']}")
            conn.close()
            customer_context = '\n'.join(context_parts)

    try:
        answer = quick_chat(question, customer_context)
        return jsonify({'answer': answer})
    except Exception as e:
        logger.error(f'智能问答失败: {str(e)}')
        return jsonify({'error': f'问答失败: {str(e)}'}), 500


# ========== 启动 ==========
if __name__ == '__main__':
    import threading
    import signal
    import time

    # ============================================================
    # 核心架构修正（2024-06-24）：
    # Flask 必须在主线程运行，否则后台线程崩溃 → 守护 Flask 线程会被杀死。
    # 初始化放在后台线程，出错不影响 Flask。
    # init_db() 提前执行，确保请求到达时数据库已就绪。
    # ============================================================

    # 提前初始化数据库（轻量操作，确保表结构已存在）
    try:
        init_db()
    except Exception as e:
        print(f'数据库初始化失败: {e}')

    def _run_init():
        """后台初始化：定时器，出错不阻塞 Flask"""
        try:
            print('正在启动定时任务...')
            start_scheduler()
            print('定时任务已启动')
        except Exception as e:
            print(f'定时任务启动失败（不影响服务运行）: {e}')
            import traceback
            traceback.print_exc()

    # 以非守护线程启动初始化，确保即使耗时较长也会执行完毕
    init_thread = threading.Thread(target=_run_init, daemon=False)
    init_thread.start()

    # ========== 优雅关闭：确保数据不丢失 ==========
    def shutdown(signum=None, frame=None):
        """收到终止信号时，强制将 WAL 写入主数据库文件，确保数据持久化"""
        print('\n正在安全关闭...')
        try:
            from db import get_db
            conn = get_db()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            print('数据已保存')
        except Exception as e:
            print(f'关闭时保存数据失败: {e}')
        stop_scheduler()
        import os as _os
        _os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Flask 在主线程运行，永不因初始化异常而退出
    print('客户跟进提醒系统启动中...')
    print(f'数据库位置: {DB_PATH}')
    try:
        app.run(debug=False, port=8080, host='0.0.0.0')
    except Exception as e:
        print(f'Flask 服务异常退出: {e}')
        import traceback
        traceback.print_exc()
        # 等待用户看到错误信息
        time.sleep(10)

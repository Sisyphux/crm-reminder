"""
客户跟进提醒系统 - Flask 后端应用（多用户版）
支持 Hamid / Amy / Kelly 三人独立数据 + 周报总览 + 自动备份
"""
import os
import sys
import json
import logging
import time
import threading
import signal
import socket
import platform
import shutil
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, session, g, Response
from flask_cors import CORS
from db import (
    get_db, get_system_db, get_user_db_path, set_db_user, get_current_user,
    init_all_dbs, USERS, USERS_LIST,
    backup_database, list_backups, restore_from_backup, check_integrity,
    DB_DIR,
)
from ical_gen import build_icalendar, bump_calendar_seq, get_calendar_seq
from scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# ========== 配置 ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask 应用
if getattr(sys, 'frozen', False):
    app = Flask(__name__, static_folder=os.path.join(sys._MEIPASS, 'static'), static_url_path='')
else:
    app = Flask(__name__, static_folder='app/static', static_url_path='')
app.secret_key = 'crm-reminder-secret-key-2026-hamid-amy-kelly'
CORS(app, supports_credentials=True)


# 禁止浏览器缓存 HTML/JS/CSS
@app.after_request
def add_no_cache(response):
    if response.content_type and any(t in response.content_type for t in ['text/html', 'application/javascript', 'text/css', 'text/javascript']):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# ========== 用户上下文 ==========
@app.before_request
def before_request():
    """在每个请求前设置当前用户"""
    user = session.get('user', '')
    if user in USERS:
        set_db_user(user)
        g.current_user = user
    else:
        set_db_user(None)
        g.current_user = ''


def login_required(f):
    """装饰器：需要登录才能访问"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.current_user:
            return jsonify({'error': '未登录', 'login_required': True}), 401
        return f(*args, **kwargs)
    return decorated


# ========== 操作日志 ==========
def log_operation(action, target_type, target_id=None, details=''):
    """记录当前用户的操作日志"""
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
    'qatar': '卡塔尔', 'germany': '德国', 'deutschland': '德国',
    'france': '法国', 'united kingdom': '英国', 'uk': '英国',
    'australia': '澳大利亚', 'au': '澳大利亚',
    'canada': '加拿大', 'ca': '加拿大',
    'mexico': '墨西哥', 'mx': '墨西哥',
    'italy': '意大利', 'romania': '罗马尼亚',
    'egypt': '埃及', 'india': '印度',
    'turkey': '土耳其', 'denmark': '丹麦',
    'new zealand': '新西兰', 'colombia': '哥伦比亚',
    'iran': '伊朗', 'oman': '阿曼', 'kuwait': '科威特',
}

def normalize_country(name):
    if not name: return ''
    return _COUNTRY_MAP.get(name.strip().lower(), name.strip())


# ========== 登录 / 认证 API ==========

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """用户登录"""
    data = request.json or {}
    user_id = data.get('user', '').strip().lower()
    if user_id not in USERS:
        return jsonify({'error': '无效的用户'}), 400
    session['user'] = user_id
    session.permanent = True
    set_db_user(user_id)
    g.current_user = user_id
    user_info = dict(USERS[user_id])
    user_info['id'] = user_id
    return jsonify({'success': True, 'user': user_info})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """退出登录"""
    session.pop('user', None)
    set_db_user(None)
    g.current_user = ''
    return jsonify({'success': True})


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """获取当前登录用户信息"""
    user_id = session.get('user', '')
    if user_id in USERS:
        user_info = dict(USERS[user_id])
        user_info['id'] = user_id
        return jsonify({'user': user_info, 'logged_in': True})
    return jsonify({'user': None, 'logged_in': False})


@app.route('/api/auth/users', methods=['GET'])
def auth_users():
    """获取所有用户列表"""
    users_list = []
    for uid, info in USERS.items():
        users_list.append({'id': uid, 'name': info['name'], 'label': info['label'], 'color': info['color']})
    return jsonify({'users': users_list})


# ========== 首页 ==========

@app.route('/')
def index():
    candidates = []
    sf = app.static_folder
    if sf:
        candidates.append(os.path.join(sf, 'index.html'))
    candidates.append(os.path.join(os.getcwd(), 'static', 'index.html'))
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, 'static', 'index.html'))
    candidates.append(os.path.join(os.getcwd(), 'app', 'static', 'index.html'))
    for path in candidates:
        if os.path.isfile(path):
            directory = os.path.dirname(path)
            response = send_from_directory(directory, 'index.html')
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
    return jsonify({'error': 'index.html not found', 'tried': candidates}), 404


# ========== 客户 API ==========
# 以下所有路由通过 get_db() 自动路由到当前用户数据库

@app.route('/api/customers', methods=['GET'])
@login_required
def get_customers():
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    level = request.args.get('level', '').strip()
    customer_type = request.args.get('customer_type', '').strip()
    sort = request.args.get('sort', 'next_follow_up')
    order = request.args.get('order', 'asc')
    include_deleted = request.args.get('deleted', '0').strip()

    conn = get_db()
    c = conn.cursor()

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
    if sort not in allowed_sorts: sort = 'next_follow_up'
    if order not in ('asc', 'desc'): order = 'asc'
    query += f' ORDER BY {sort} {order}'

    c.execute(query, params)
    customers = [dict(row) for row in c.fetchall()]

    today = datetime.now().strftime('%Y-%m-%d')
    if customers:
        customer_ids = [cust['id'] for cust in customers]
        placeholders = ','.join('?' * len(customer_ids))
        c.execute(f'SELECT customer_id, MAX(follow_date) as follow_date FROM follow_up_logs WHERE customer_id IN ({placeholders}) AND follow_date <= ? GROUP BY customer_id',
                  customer_ids + [today])
        last_contacts = {}
        for row in c.fetchall():
            last_contacts[row['customer_id']] = row['follow_date']
        for cust in customers:
            cust['last_contact'] = last_contacts.get(cust['id'], '')

    conn.close()
    return jsonify({'customers': customers, 'total': len(customers)})


@app.route('/api/customers/<int:customer_id>', methods=['GET'])
@login_required
def get_customer(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM customers WHERE id = ?', (customer_id,))
    customer = c.fetchone()
    if not customer:
        conn.close()
        return jsonify({'error': '客户不存在'}), 404
    c.execute('SELECT * FROM reminders WHERE customer_id = ? AND is_done = 0 ORDER BY remind_date ASC', (customer_id,))
    reminders = [dict(row) for row in c.fetchall()]
    c.execute('SELECT * FROM follow_up_logs WHERE customer_id = ? ORDER BY follow_date DESC, created_at DESC', (customer_id,))
    follow_history = [dict(row) for row in c.fetchall()]
    c.execute('SELECT * FROM contacts WHERE customer_id = ? ORDER BY is_primary DESC, created_at DESC', (customer_id,))
    contacts = [dict(row) for row in c.fetchall()]
    c.execute('SELECT * FROM outreach_emails WHERE customer_id = ? ORDER BY sent_date DESC, created_at DESC', (customer_id,))
    outreach_emails = [dict(row) for row in c.fetchall()]
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
@login_required
def create_customer():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    country = normalize_country(data.get('country', ''))
    c.execute('''
        INSERT INTO customers (name, company, country, level, type, website, profile, field, status, notes, system_notes, last_contact, next_follow_up, customer_type, industry, company_size, annual_revenue, import_source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (data.get('name', ''), data.get('company', ''), country, data.get('level', 'C'),
          data.get('type', ''), data.get('website', ''), data.get('profile', ''),
          data.get('field', ''), data.get('status', '未建联'), data.get('notes', ''),
          data.get('system_notes', ''), data.get('last_contact', ''),
          data.get('next_follow_up', ''), data.get('customer_type', 'existing'),
          data.get('industry', ''), data.get('company_size', ''),
          data.get('annual_revenue', ''), 'manual', now, now))
    customer_id = c.lastrowid
    manual_next_follow = data.get('next_follow_up', '')
    if manual_next_follow:
        c.execute('INSERT INTO reminders (customer_id, content, remind_date, is_done, created_at) VALUES (?, ?, ?, 0, ?)',
                  (customer_id, f'跟进 {data.get("name", "")}: {data.get("notes", "")}', manual_next_follow, now))
    if data.get('customer_type') == 'new':
        customer_name = data.get('name', '')
        created_date = datetime.now()
        for days, label in [(15, '15天'), (30, '30天'), (60, '60天')]:
            target_date = (created_date + timedelta(days=days)).strftime('%Y-%m-%d')
            c.execute('INSERT INTO reminders (customer_id, content, remind_date, is_done, reminder_type, created_at) VALUES (?, ?, ?, 0, ?, ?)',
                      (customer_id, f'新客户开发跟进（{label}）: {customer_name}', target_date, f'outreach_{label}', now))
        final_next = manual_next_follow if manual_next_follow else (created_date + timedelta(days=15)).strftime('%Y-%m-%d')
        c.execute('UPDATE customers SET next_follow_up = ? WHERE id = ?', (final_next, customer_id))
    conn.commit()
    conn.close()
    log_operation('CREATE', 'customer', customer_id, f'创建客户: {data.get("name", "")}')
    return jsonify({'id': customer_id, 'message': '客户创建成功'}), 201


@app.route('/api/customers/<int:customer_id>', methods=['PUT'])
@login_required
def update_customer(customer_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('SELECT next_follow_up, name, manual_next_follow FROM customers WHERE id = ?', (customer_id,))
    old = c.fetchone()
    old_date = old['next_follow_up'] if old else ''
    customer_name = old['name'] if old else data.get('name', '')
    old_manual = old['manual_next_follow'] if old else 0
    new_next_follow = data.get('next_follow_up', '')
    is_manual_date = 1 if (new_next_follow and new_next_follow != old_date) else old_manual
    new_status = data.get('status', '')
    auto_customer_type = 'new' if new_status == '未建联' else 'existing'
    c.execute('''
        UPDATE customers SET name=?, company=?, country=?, level=?, type=?, website=?, profile=?, field=?, status=?, notes=?, system_notes=?,
        last_contact=?, next_follow_up=?, manual_next_follow=?, customer_type=?, industry=?, company_size=?, annual_revenue=?, updated_at=? WHERE id=?
    ''', (data.get('name', ''), data.get('company', ''), normalize_country(data.get('country', '')),
          data.get('level', 'C'), data.get('type', ''), data.get('website', ''), data.get('profile', ''),
          data.get('field', ''), new_status, data.get('notes', ''), data.get('system_notes', ''),
          data.get('last_contact', ''), new_next_follow, is_manual_date, auto_customer_type,
          data.get('industry', ''), data.get('company_size', ''), data.get('annual_revenue', ''), now, customer_id))
    new_date = data.get('next_follow_up', '')
    if new_date and new_date != old_date:
        c.execute('UPDATE reminders SET is_done = 1 WHERE customer_id = ? AND is_done = 0', (customer_id,))
        c.execute('INSERT INTO reminders (customer_id, content, remind_date, is_done, created_at) VALUES (?, ?, ?, 0, ?)',
                  (customer_id, f'跟进 {customer_name}: {data.get("notes", "")}', new_date, now))
    conn.commit()
    conn.close()
    log_operation('UPDATE', 'customer', customer_id, f'更新客户: {data.get("name", "")}')
    return jsonify({'message': '客户更新成功'})


@app.route('/api/customers/<int:customer_id>', methods=['DELETE'])
@login_required
def delete_customer(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT name FROM customers WHERE id = ?', (customer_id,))
    row = c.fetchone()
    customer_name = row['name'] if row else '未知'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('UPDATE customers SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE id = ?', (now, now, customer_id))
    conn.commit()
    conn.close()
    log_operation('SOFT_DELETE', 'customer', customer_id, f'移至回收站: {customer_name}')
    return jsonify({'message': f'已将 {customer_name} 移至回收站'})


# ========== 批量操作 API ==========

@app.route('/api/customers/batch/status', methods=['POST'])
@login_required
def batch_update_status():
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
@login_required
def batch_update_level():
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
@login_required
def batch_delete_customers():
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
@login_required
def restore_customer(customer_id):
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
@login_required
def permanent_delete_customer(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT name FROM customers WHERE id = ?', (customer_id,))
    row = c.fetchone()
    customer_name = row['name'] if row else '未知'
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
@login_required
def empty_recycle_bin():
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
@login_required
def get_recycle_bin_count():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) as cnt FROM customers WHERE is_deleted = 1')
    count = c.fetchone()['cnt']
    conn.close()
    return jsonify({'count': count})


# ========== 提醒 API ==========

@app.route('/api/reminders/today', methods=['GET'])
@login_required
def get_today_reminders():
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT r.*, c.name as customer_name, c.company as customer_company,
               c.country, c.level, c.status, c.field, c.website,
               c.profile, c.last_contact, c.notes as customer_notes,
               c.type as customer_type
        FROM reminders r JOIN customers c ON r.customer_id = c.id
        WHERE r.is_done = 0 AND r.remind_date <= ?
          AND r.reminder_type NOT LIKE 'outreach_%'
        ORDER BY r.remind_date ASC, c.level DESC
    ''', (today,))
    reminders = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(reminders)


@app.route('/api/reminders/upcoming', methods=['GET'])
@login_required
def get_upcoming_reminders():
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT r.*, c.name as customer_name, c.company as customer_company,
               c.country, c.level, c.status, c.field, c.website,
               c.profile, c.last_contact, c.notes as customer_notes,
               c.type as customer_type
        FROM reminders r JOIN customers c ON r.customer_id = c.id
        WHERE r.is_done = 0 AND r.remind_date > ?
          AND r.reminder_type NOT LIKE 'outreach_%'
        ORDER BY r.remind_date ASC
    ''', (today,))
    reminders = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(reminders)


@app.route('/api/reminders/batch/complete', methods=['POST'])
@login_required
def batch_complete_reminders():
    data = request.json
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'error': '缺少参数'}), 400
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for rid in ids:
        c.execute('SELECT r.*, c.name as customer_name FROM reminders r JOIN customers c ON r.customer_id = c.id WHERE r.id = ?', (rid,))
        reminder = c.fetchone()
        if reminder:
            c.execute('UPDATE reminders SET is_done = 1 WHERE id = ?', (rid,))
            c.execute('INSERT INTO follow_up_logs (customer_id, content, follow_date, result, next_plan, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                      (reminder['customer_id'], reminder['content'], reminder['remind_date'], '批量完成', '', 'manual', now))
    conn.commit()
    conn.close()
    log_operation('BATCH_COMPLETE', 'reminder', None, f'批量完成 {len(ids)} 条提醒')
    return jsonify({'message': f'已完成 {len(ids)} 条提醒'})


@app.route('/api/reminders/<int:reminder_id>', methods=['PUT'])
@login_required
def complete_reminder(reminder_id):
    data = request.json or {}
    result_text = data.get('result', '')
    next_follow_date = data.get('next_follow_up', '')
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT r.*, c.name as customer_name, c.customer_type FROM reminders r JOIN customers c ON r.customer_id = c.id WHERE r.id = ?', (reminder_id,))
    reminder = c.fetchone()
    if not reminder:
        conn.close()
        return jsonify({'error': '提醒不存在'}), 404
    c.execute('UPDATE reminders SET is_done = 1 WHERE id = ?', (reminder_id,))
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('INSERT INTO follow_up_logs (customer_id, content, follow_date, result, next_plan, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (reminder['customer_id'], reminder['content'], reminder['remind_date'], result_text, next_follow_date, now))
    customer_id = reminder['customer_id']
    next_follow_message = ''
    if next_follow_date:
        c.execute('UPDATE customers SET next_follow_up=?, manual_next_follow=1, last_contact=?, status=CASE WHEN status=\'未建联\' THEN \'跟进中\' ELSE status END, notes=? WHERE id=?',
                  (next_follow_date, datetime.now().strftime('%Y-%m-%d'), result_text or '', customer_id))
        c.execute('INSERT INTO reminders (customer_id, content, remind_date, is_done, reminder_type, created_at) VALUES (?, ?, ?, 0, ?, ?)',
                  (customer_id, f'跟进 {reminder["customer_name"]}: {result_text or "继续跟进"}', next_follow_date, 'follow_up', now))
        next_follow_message = f'，下次跟进日期：{next_follow_date}'
    conn.commit()
    conn.close()
    log_operation('FOLLOW_UP', 'reminder', reminder_id, f'记录跟进: {reminder["customer_name"]} - {result_text}{next_follow_message}')
    return jsonify({'message': f'跟进记录已保存{next_follow_message}'})


@app.route('/api/reminders/<int:reminder_id>', methods=['DELETE'])
@login_required
def delete_reminder(reminder_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM reminders WHERE id = ?', (reminder_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'reminder', reminder_id, '删除提醒')
    return jsonify({'message': '提醒已删除'})


# ========== 跟进历史 API ==========

@app.route('/api/customers/<int:customer_id>/follow_history', methods=['GET'])
@login_required
def get_follow_history(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT f.*, c.name as customer_name FROM follow_up_logs f JOIN customers c ON f.customer_id = c.id WHERE f.customer_id = ? ORDER BY f.follow_date DESC, f.created_at DESC', (customer_id,))
    history = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(history)


@app.route('/api/follow-history', methods=['GET'])
@login_required
def get_all_follow_history():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT f.*, c.name as customer_name FROM follow_up_logs f JOIN customers c ON f.customer_id = c.id ORDER BY f.follow_date DESC, f.created_at DESC LIMIT 50')
    history = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(history)


@app.route('/api/follow-history/<int:log_id>', methods=['PUT'])
@login_required
def update_follow_history(log_id):
    data = request.json or {}
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM follow_up_logs WHERE id = ?', (log_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': '记录不存在'}), 404
    c.execute('UPDATE follow_up_logs SET follow_date=?, content=?, result=?, next_plan=? WHERE id=?',
              (data.get('follow_date', ''), data.get('content', ''), data.get('result', ''), data.get('next_plan', ''), log_id))
    conn.commit()
    log_operation('update', 'follow_up_log', log_id, f'编辑跟进记录 #{log_id}')
    c.execute('SELECT f.*, c.name as customer_name FROM follow_up_logs f JOIN customers c ON f.customer_id = c.id WHERE f.id = ?', (log_id,))
    updated = dict(c.fetchone())
    conn.close()
    return jsonify(updated)


@app.route('/api/follow-history/<int:log_id>', methods=['DELETE'])
@login_required
def delete_follow_history(log_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM follow_up_logs WHERE id = ?', (log_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': '记录不存在'}), 404
    c.execute('DELETE FROM follow_up_logs WHERE id = ?', (log_id,))
    conn.commit()
    log_operation('delete', 'follow_up_log', log_id, f'删除跟进记录 #{log_id}')
    conn.close()
    return jsonify({'success': True})


# ========== 联系人 API ==========

@app.route('/api/customers/<int:customer_id>/contacts', methods=['GET'])
@login_required
def get_contacts(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM contacts WHERE customer_id = ? ORDER BY is_primary DESC, created_at DESC', (customer_id,))
    contacts = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(contacts)


@app.route('/api/customers/<int:customer_id>/contacts', methods=['POST'])
@login_required
def add_contact(customer_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('INSERT INTO contacts (customer_id, name, title, email, phone, linkedin, is_primary, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
              (customer_id, data.get('name', ''), data.get('title', ''), data.get('email', ''),
               data.get('phone', ''), data.get('linkedin', ''), data.get('is_primary', 0), data.get('notes', ''), now))
    conn.commit()
    conn.close()
    log_operation('CREATE', 'contact', customer_id, f'添加联系人: {data.get("name", "")}')
    return jsonify({'message': '联系人添加成功'}), 201


@app.route('/api/contacts/<int:contact_id>', methods=['PUT'])
@login_required
def update_contact(contact_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE contacts SET name=?, title=?, email=?, phone=?, linkedin=?, is_primary=?, notes=? WHERE id=?',
              (data.get('name', ''), data.get('title', ''), data.get('email', ''),
               data.get('phone', ''), data.get('linkedin', ''), data.get('is_primary', 0), data.get('notes', ''), contact_id))
    conn.commit()
    conn.close()
    log_operation('UPDATE', 'contact', contact_id, f'更新联系人: {data.get("name", "")}')
    return jsonify({'message': '联系人更新成功'})


@app.route('/api/contacts/<int:contact_id>', methods=['DELETE'])
@login_required
def delete_contact(contact_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM contacts WHERE id = ?', (contact_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'contact', contact_id, '删除联系人')
    return jsonify({'message': '联系人删除成功'})


# ========== 开发信 API ==========

@app.route('/api/customers/<int:customer_id>/outreach', methods=['GET'])
@login_required
def get_outreach_emails(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM outreach_emails WHERE customer_id = ? ORDER BY sent_date DESC, created_at DESC', (customer_id,))
    emails = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(emails)


@app.route('/api/customers/<int:customer_id>/outreach', methods=['POST'])
@login_required
def add_outreach_email(customer_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('INSERT INTO outreach_emails (customer_id, subject, content, sent_date, reply_status, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (customer_id, data.get('subject', ''), data.get('content', ''),
               data.get('sent_date', ''), data.get('reply_status', 'pending'), now))
    conn.commit()
    conn.close()
    log_operation('CREATE', 'outreach', customer_id, f'添加开发信: {data.get("subject", "")}')
    return jsonify({'message': '开发信记录添加成功'}), 201


@app.route('/api/outreach/<int:outreach_id>', methods=['PUT'])
@login_required
def update_outreach_email(outreach_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE outreach_emails SET reply_status=?, reply_content=?, reply_date=? WHERE id=?',
              (data.get('reply_status', 'pending'), data.get('reply_content', ''), data.get('reply_date', ''), outreach_id))
    conn.commit()
    conn.close()
    log_operation('UPDATE', 'outreach', outreach_id, f'更新开发信回复状态: {data.get("reply_status", "")}')
    return jsonify({'message': '开发信记录更新成功'})


@app.route('/api/outreach/<int:outreach_id>', methods=['DELETE'])
@login_required
def delete_outreach_email(outreach_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM outreach_emails WHERE id = ?', (outreach_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'outreach', outreach_id, '删除开发信记录')
    return jsonify({'message': '开发信记录删除成功'})


# ========== 背调报告 API ==========

@app.route('/api/customers/<int:customer_id>/research', methods=['GET'])
@login_required
def get_research_report(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM research_reports WHERE customer_id = ?', (customer_id,))
    report = c.fetchone()
    conn.close()
    return jsonify(dict(report) if report else None)


@app.route('/api/customers/<int:customer_id>/research', methods=['POST'])
@login_required
def create_research_report(customer_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('SELECT id FROM research_reports WHERE customer_id = ?', (customer_id,))
    existing = c.fetchone()
    if existing:
        c.execute('UPDATE research_reports SET summary=?, company_info=?, key_findings=?, needs_analysis=?, cooperation_value=?, raw_input=?, updated_at=? WHERE customer_id=?',
                  (data.get('summary', ''), data.get('company_info', ''), data.get('key_findings', ''),
                   data.get('needs_analysis', ''), data.get('cooperation_value', ''),
                   data.get('raw_input', ''), now, customer_id))
    else:
        c.execute('INSERT INTO research_reports (customer_id, summary, company_info, key_findings, needs_analysis, cooperation_value, raw_input, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                  (customer_id, data.get('summary', ''), data.get('company_info', ''),
                   data.get('key_findings', ''), data.get('needs_analysis', ''),
                   data.get('cooperation_value', ''), data.get('raw_input', ''), now, now))
    conn.commit()
    conn.close()
    log_operation('CREATE' if not existing else 'UPDATE', 'research', customer_id, '创建/更新背调报告')
    return jsonify({'message': '背调报告保存成功'})


@app.route('/api/customers/<int:customer_id>/research', methods=['DELETE'])
@login_required
def delete_research_report(customer_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM research_reports WHERE customer_id = ?', (customer_id,))
    conn.commit()
    conn.close()
    log_operation('DELETE', 'research', customer_id, '删除背调报告')
    return jsonify({'message': '背调报告删除成功'})


# ========== 智能导入 API ==========

DOMAIN_COUNTRY_MAP = {
    '.com': ['美国', '全球'], '.com.au': ['澳大利亚'], '.com.cn': ['中国'], '.cn': ['中国'],
    '.com.hk': ['中国香港'], '.hk': ['中国香港'], '.com.tw': ['中国台湾'], '.tw': ['中国台湾'],
    '.jp': ['日本'], '.co.jp': ['日本'], '.kr': ['韩国'], '.co.kr': ['韩国'],
    '.de': ['德国'], '.at': ['奥地利'], '.ch': ['瑞士'], '.fr': ['法国'],
    '.uk': ['英国'], '.co.uk': ['英国'], '.ie': ['爱尔兰'], '.nl': ['荷兰'],
    '.be': ['比利时'], '.lu': ['卢森堡'], '.es': ['西班牙'], '.pt': ['葡萄牙'],
    '.it': ['意大利'], '.gr': ['希腊'], '.cy': ['塞浦路斯'], '.mt': ['马耳他'],
    '.si': ['斯洛文尼亚'], '.hr': ['克罗地亚'], '.bg': ['保加利亚'], '.ro': ['罗马尼亚'],
    '.hu': ['匈牙利'], '.pl': ['波兰'], '.cz': ['捷克'], '.sk': ['斯洛伐克'],
    '.ee': ['爱沙尼亚'], '.lv': ['拉脱维亚'], '.lt': ['立陶宛'],
    '.fi': ['芬兰'], '.se': ['瑞典'], '.no': ['挪威'], '.dk': ['丹麦'],
    '.is': ['冰岛'], '.ru': ['俄罗斯'], '.by': ['白俄罗斯'], '.ua': ['乌克兰'],
    '.kz': ['哈萨克斯坦'], '.ae': ['阿联酋'], '.sa': ['沙特阿拉伯'],
    '.eg': ['埃及'], '.za': ['南非'], '.co.za': ['南非'],
    '.com.sg': ['新加坡'], '.sg': ['新加坡'], '.my': ['马来西亚'], '.com.my': ['马来西亚'],
    '.id': ['印度尼西亚'], '.co.id': ['印度尼西亚'],
    '.th': ['泰国'], '.co.th': ['泰国'], '.vn': ['越南'], '.com.vn': ['越南'],
    '.ph': ['菲律宾'], '.com.ph': ['菲律宾'],
    '.in': ['印度'], '.co.in': ['印度'], '.pk': ['巴基斯坦'], '.bd': ['孟加拉国'],
    '.nz': ['新西兰'], '.co.nz': ['新西兰'],
    '.ca': ['加拿大'], '.mx': ['墨西哥'], '.com.mx': ['墨西哥'],
    '.br': ['巴西'], '.com.br': ['巴西'], '.ar': ['阿根廷'], '.cl': ['智利'],
    '.pe': ['秘鲁'], '.co': ['哥伦比亚'],
}

INDUSTRY_LIST = ['亚克力分销', '工程塑料分销', '塑料板材分销', '标牌制造', '建筑建材',
                 '家具制造', '照明灯具', '电子电器', '汽车相关', '医疗器械',
                 '日用品消费品', '包装印刷', '工业制造', '贸易商/进口商', '其他']

@app.route('/api/customers/smart-import', methods=['POST'])
@login_required
def smart_import_customer():
    data = request.json
    company_input = data.get('company', '').strip()
    website_input = data.get('website', '').strip()
    result = {'name': company_input, 'company': company_input, 'country': '', 'field': '', 'website': website_input, 'profile': '', 'notes': '', 'auto_filled': []}
    if website_input:
        from urllib.parse import urlparse
        url = website_input if website_input.startswith('http') else 'http://' + website_input
        parsed = urlparse(url)
        domain = (parsed.netloc or parsed.path).lower()
        if domain.startswith('www.'): domain = domain[4:]
        if ':' in domain: domain = domain.split(':')[0]
        result['website'] = 'https://' + domain
        for suffix, countries in DOMAIN_COUNTRY_MAP.items():
            if domain.endswith(suffix):
                result['country'] = ', '.join(countries)
                result['auto_filled'].append('country')
                break
        if not result['country'] and '.' in domain:
            tld = '.' + domain.split('.')[-1]
            if tld in DOMAIN_COUNTRY_MAP:
                result['country'] = ', '.join(DOMAIN_COUNTRY_MAP[tld])
                result['auto_filled'].append('country')
    if company_input and not result['field']:
        name_lower = company_input.lower()
        kw = {'acrylic': '亚克力分销', 'plastic': '工程塑料分销', 'sign': '标牌制造', 'signage': '标牌制造',
              'lighting': '照明灯具', 'light': '照明灯具', 'furniture': '家具制造',
              'medical': '医疗器械', 'packaging': '包装印刷', 'printing': '包装印刷',
              'industrial': '工业制造', 'trading': '贸易商/进口商', 'distributor': '贸易商/进口商'}
        for keyword, field in kw.items():
            if keyword in name_lower:
                result['field'] = field
                result['auto_filled'].append('field')
                break
    if website_input:
        domain = urlparse(website_input if website_input.startswith('http') else 'http://' + website_input).netloc.lower()
        domain = domain[4:] if domain.startswith('www.') else domain
        kw = {'acrylic': '亚克力分销', 'plastic': '工程塑料分销', 'sign': '标牌制造',
              'lighting': '照明灯具', 'furniture': '家具制造', 'medical': '医疗器械',
              'packaging': '包装印刷', 'industrial': '工业制造', 'distributor': '贸易商/进口商'}
        for keyword, field in kw.items():
            if keyword in domain and not result.get('field'):
                result['field'] = field
                result['auto_filled'].append('field')
                break
    if not result['country']:
        result['country'] = '美国'
    return jsonify(result)


# ========== 统计 API ==========

@app.route('/api/stats', methods=['GET'])
@login_required
def get_stats():
    conn = get_db()
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
    total = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM customers WHERE customer_type=? AND (is_deleted = 0 OR is_deleted IS NULL)', ('new',))
    new_customers = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM customers WHERE customer_type=? AND (is_deleted = 0 OR is_deleted IS NULL)', ('existing',))
    existing_customers = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date <= ?', (today,))
    pending = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date < ?', (today,))
    overdue = c.fetchone()[0]
    c.execute('SELECT status, next_follow_up FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
    rows = c.fetchall()
    status_counts = {'未建联': 0, '已建联': 0, '跟进中': 0, '成交': 0, '流失': 0}
    for status, nfu in rows:
        s = status or ''
        if s == '跟进中':
            if nfu and nfu.strip():
                try:
                    nfu_date = datetime.strptime(nfu.strip()[:10], '%Y-%m-%d')
                    if (nfu_date - datetime.now()).days <= 30:
                        status_counts['跟进中'] += 1
                    else:
                        status_counts['已建联'] += 1
                except ValueError:
                    status_counts['已建联'] += 1
            else:
                status_counts['已建联'] += 1
        elif s == '已建联':
            if nfu and nfu.strip():
                try:
                    nfu_date = datetime.strptime(nfu.strip()[:10], '%Y-%m-%d')
                    if (nfu_date - datetime.now()).days <= 30:
                        status_counts['跟进中'] += 1
                    else:
                        status_counts['已建联'] += 1
                except ValueError:
                    status_counts['已建联'] += 1
            else:
                status_counts['已建联'] += 1
        elif s in status_counts:
            status_counts[s] += 1
    c.execute('SELECT level, COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL) GROUP BY level')
    level_counts = {row[0]: row[1] for row in c.fetchall()}
    c.execute('SELECT COUNT(*) FROM customers WHERE is_deleted = 1')
    deleted_count = c.fetchone()[0]
    conn.close()
    return jsonify({
        'total': total, 'new_customers': new_customers, 'existing_customers': existing_customers,
        'deleted_count': deleted_count, 'pending': pending, 'overdue': overdue,
        'following': status_counts.get('跟进中', 0),
        'status_counts': status_counts, 'level_counts': level_counts,
    })


# ========== Excel 上传 ==========
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'uploads')
UPLOAD_SOURCE_FILE = os.path.join(UPLOAD_DIR, '_source.json')


def get_uploaded_excel_path():
    try:
        if os.path.exists(UPLOAD_SOURCE_FILE):
            with open(UPLOAD_SOURCE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            path = data.get('path', '')
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    return None


def find_excel_file():
    """在项目目录查找 Excel 文件"""
    for f in os.listdir(os.path.dirname(os.path.abspath(__file__))):
        if f.endswith(('.xlsx', '.xls')) and not f.startswith('~'):
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), f)
    return None


def sync_from_excel(excel_path=None):
    """从 Excel 导入数据到当前用户的数据库"""
    import openpyxl
    excel_path = excel_path or get_uploaded_excel_path()
    if not excel_path or not os.path.exists(excel_path):
        return {'success': False, 'error': 'Excel 文件未找到，请先上传'}

    try:
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows or len(rows) < 2:
            return {'success': False, 'error': 'Excel 文件为空或只有表头'}
    except Exception as e:
        return {'success': False, 'error': f'读取 Excel 失败: {str(e)}'}

    headers = [str(h or '').strip().lower() for h in rows[0]]
    
    # 列名映射
    NAME_KEYS = ['客户名称', 'name', 'customer name', '公司名称', 'company name']
    COUNTRY_KEYS = ['国家', 'country', 'nation']
    LEVEL_KEYS = ['客户等级', 'level', 'grade']
    TYPE_KEYS = ['客户类型', 'type', 'customer type']
    STATUS_KEYS = ['状态', 'status']
    WEBSITE_KEYS = ['网站', 'website', 'web', '网址']
    FIELD_KEYS = ['行业', 'field', 'industry']
    NOTES_KEYS = ['备注', 'notes', 'note', 'remark', '备注']
    COMPANY_KEYS = ['公司', 'company', '公司名']
    CONTACT_KEYS = ['联系人', '联系人名称', 'contact', 'contact name']
    EMAIL_KEYS = ['邮箱', 'email', 'e-mail']
    PHONE_KEYS = ['电话', 'phone', 'tel', 'telephone', 'mobile']
    PROFILE_KEYS = ['简介', 'profile', '介绍', 'description']
    CUSTOMER_TYPE_KEYS = ['客户分类', 'customer type', '分类']

    def find_col(keywords):
        for k in keywords:
            for i, h in enumerate(headers):
                if k in h:
                    return i
        return -1

    col_name = find_col(NAME_KEYS)
    col_country = find_col(COUNTRY_KEYS)
    col_level = find_col(LEVEL_KEYS)
    col_type = find_col(TYPE_KEYS)
    col_status = find_col(STATUS_KEYS)
    col_website = find_col(WEBSITE_KEYS)
    col_field = find_col(FIELD_KEYS)
    col_notes = find_col(NOTES_KEYS)
    col_company = find_col(COMPANY_KEYS)
    col_contact = find_col(CONTACT_KEYS)
    col_email = find_col(EMAIL_KEYS)
    col_phone = find_col(PHONE_KEYS)
    col_profile = find_col(PROFILE_KEYS)
    col_customer_type = find_col(CUSTOMER_TYPE_KEYS)

    if col_name == -1:
        return {'success': False, 'error': '未找到"客户名称"列，请确保表格包含客户名称'}

    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_count = 0
    updated_count = 0
    total_rows = 0

    for row in rows[1:]:
        if not row or not row[col_name]:
            continue
        total_rows += 1
        name = str(row[col_name]).strip()[:200]
        company = str(row[col_company]).strip()[:200] if col_company >= 0 and row[col_company] else name
        country = normalize_country(str(row[col_country]).strip() if col_country >= 0 and row[col_country] else '')
        level = str(row[col_level]).strip().upper() if col_level >= 0 and row[col_level] else 'C'
        if level not in ('A', 'B', 'C', 'C+', 'D'): level = 'C'
        cust_type = str(row[col_type]).strip() if col_type >= 0 and row[col_type] else ''
        status = str(row[col_status]).strip() if col_status >= 0 and row[col_status] else '未建联'
        if status not in ('未建联', '已建联', '跟进中', '成交', '流失'): status = '未建联'
        website = str(row[col_website]).strip() if col_website >= 0 and row[col_website] else ''
        field = str(row[col_field]).strip() if col_field >= 0 and row[col_field] else ''
        notes = str(row[col_notes]).strip() if col_notes >= 0 and row[col_notes] else ''
        profile = str(row[col_profile]).strip() if col_profile >= 0 and row[col_profile] else ''
        customer_type = str(row[col_customer_type]).strip() if col_customer_type >= 0 and row[col_customer_type] else 'existing'

        # 检查是否已存在（按名称匹配）
        c.execute('SELECT id FROM customers WHERE name = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (name,))
        existing = c.fetchone()
        if existing:
            c.execute('UPDATE customers SET company=?, country=?, level=?, type=?, website=?, field=?, status=?, notes=?, profile=?, customer_type=?, updated_at=? WHERE id=?',
                      (company, country, level, cust_type, website, field, status, notes, profile, customer_type, now, existing['id']))
            cust_id = existing['id']
            updated_count += 1
        else:
            c.execute('''INSERT INTO customers (name, company, country, level, type, website, profile, field, status, notes, customer_type, import_source, created_at, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (name, company, country, level, cust_type, website, profile, field, status, notes, customer_type, 'excel', now, now))
            cust_id = c.lastrowid
            new_count += 1

        # 如果有联系人信息，添加联系人
        contact_name = str(row[col_contact]).strip() if col_contact >= 0 and row[col_contact] else ''
        email = str(row[col_email]).strip() if col_email >= 0 and row[col_email] else ''
        phone = str(row[col_phone]).strip() if col_phone >= 0 and row[col_phone] else ''
        if contact_name or email or phone:
            c.execute('INSERT INTO contacts (customer_id, name, email, phone, created_at) VALUES (?, ?, ?, ?, ?)',
                      (cust_id, contact_name or name, email, phone, now))

    conn.commit()
    conn.close()

    return {
        'success': True,
        'new_customers': new_count,
        'updated_customers': updated_count,
        'total_rows': total_rows,
        'message': f'导入完成: 新增 {new_count} 个, 更新 {updated_count} 个 (共 {total_rows} 行)'
    }


def sync_to_excel(customer_id=None):
    """同步到 Excel（已禁用）"""
    return {'success': False, 'error': '此功能已禁用'}


def get_excel_status():
    """获取 Excel 同步状态"""
    path = get_uploaded_excel_path() or find_excel_file()
    if path:
        return {'found': True, 'path': path, 'modified': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')}
    return {'found': False}


@app.route('/api/excel/upload', methods=['POST'])
@login_required
def upload_excel():
    """上传 Excel 文件并自动导入到当前用户的数据库"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '请选择文件'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': '文件名为空'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.xlsx', '.xls'):
        return jsonify({'success': False, 'error': '仅支持 .xlsx / .xls 格式'}), 400
    safe_name = f"uploaded_{int(time.time())}{ext}"
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    file.save(save_path)
    with open(UPLOAD_SOURCE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'path': save_path, 'original_name': file.filename, 'uploaded_at': datetime.now().isoformat()}, f, ensure_ascii=False)
    result = sync_from_excel(save_path)
    if result.get('success'):
        log_operation('UPLOAD_EXCEL', 'system', None, f'上传 {file.filename} | 新增 {result.get("new_customers", 0)} 个, 更新 {result.get("updated_customers", 0)} 个')
    result['file_name'] = file.filename
    result['file_path'] = save_path
    return jsonify(result)


@app.route('/api/excel/info', methods=['GET'])
@login_required
def excel_info():
    path = get_uploaded_excel_path()
    if path:
        return jsonify({'source': 'upload', 'path': path, 'name': os.path.basename(path), 'modified': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')})
    found = find_excel_file()
    if found:
        return jsonify({'source': 'auto', 'path': found, 'name': os.path.basename(found), 'modified': datetime.fromtimestamp(os.path.getmtime(found)).strftime('%Y-%m-%d %H:%M:%S')})
    return jsonify({'source': None})


@app.route('/api/sync', methods=['POST'])
@login_required
def sync_excel():
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
@login_required
def sync_to_excel_api():
    data = request.json or {}
    customer_id = data.get('customer_id')
    result = sync_to_excel(customer_id)
    if result.get('success'):
        log_operation('SYNC_TO_EXCEL', 'system', None, result.get('message', ''))
    return jsonify(result)


# ========== 日历 API ==========

@app.route('/api/calendar/ical')
def calendar_ical():
    """生成所有用户的日历 .ics 文件（无需登录，供 iPhone 订阅）"""
    from db import set_db_user, USERS
    today = datetime.now().strftime('%Y-%m-%d')
    all_reminders = []
    
    for user in USERS:
        try:
            set_db_user(user)
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                SELECT r.id, r.remind_date, r.content, 
                       COALESCE(c.company, c.name, 'Unknown') as customer_name,
                       r.customer_id
                FROM reminders r JOIN customers c ON r.customer_id = c.id
                WHERE r.remind_date >= ? AND r.is_done = 0
                  AND (c.is_deleted = 0 OR c.is_deleted IS NULL)
                ORDER BY r.remind_date ASC
            ''', (today,))
            for row in c.fetchall():
                r = dict(row)
                # 在事件名称中标明负责人
                r['customer_name'] = f'[{USERS[user]["label"]}] {r["customer_name"]}'
                all_reminders.append(r)
            conn.close()
        except Exception:
            pass
    
    set_db_user(session.get('user', ''))
    
    # 去重：每个客户只保留最近一条提醒
    seen = set()
    reminders = []
    for r in all_reminders:
        cid = (r['customer_id'], r['customer_name'].split(']')[0])  # 按客户id+负责人去重
        if cid not in seen:
            seen.add(cid)
            reminders.append(r)
    
    ics_content = build_icalendar(reminders)
    return Response(ics_content, mimetype='text/calendar', headers={
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache', 'Expires': '0',
    })


@app.route('/api/network/ip')
def get_local_ip():
    import socket as sk
    ips = []
    try:
        hostname = sk.gethostname()
        for info in sk.getaddrinfo(hostname, None):
            addr = info[4][0]
            if addr and not addr.startswith('127.') and '.' in addr and ':' not in addr:
                if not any(addr.startswith(p) for p in ['169.254.', '198.18.', '0.']):
                    ips.append(addr)
    except Exception:
        pass
    if not ips:
        try:
            s = sk.socket(sk.AF_INET, sk.SOCK_DGRAM)
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
        'local_ip': local_ip, 'all_ips': ips, 'port': port,
        'subscribe_url': f'http://{local_ip}:{port}/api/calendar/ical',
        'calendar_seq': get_calendar_seq(),
        'test_url': f'http://{local_ip}:{port}/api/network/ping',
    })


@app.route('/api/calendar/refresh', methods=['POST'])
def calendar_refresh():
    """手动触发日历推送更新"""
    seq = bump_calendar_seq()
    logger.info(f'日历推送更新: 序列号={seq}')
    return jsonify({'success': True, 'sequence': seq, 'message': f'日历已刷新 v{seq}'})


@app.route('/api/network/ping')
def network_ping():
    return jsonify({'status': 'ok', 'message': '服务运行正常'})


# ========== 系统信息 API ==========

@app.route('/api/system', methods=['GET'])
@login_required
def get_system_info():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
    customer_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reminders')
    reminder_count = c.fetchone()[0]
    conn.close()
    scheduler_info = get_scheduler_status()
    user = get_current_user()
    return jsonify({
        'current_user': user,
        'db_path': get_user_db_path(user) if user in USERS else '',
        'scheduler_running': scheduler_info.get('running', False),
        'scheduler_jobs': scheduler_info.get('jobs', []),
        'customer_count': customer_count,
        'reminder_count': reminder_count,
    })


# ========== 操作日志 API ==========

@app.route('/api/logs', methods=['GET'])
@login_required
def get_operation_logs():
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


# ========== 系统健康检测 API ==========

@app.route('/api/health', methods=['GET'])
def system_health_check():
    from db import check_integrity
    health = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checks': [],
        'overall': 'healthy'
    }
    # 1. 数据库完整性
    try:
        integrity = check_integrity()
        all_ok = all(v == 'ok' for v in integrity.values())
        health['checks'].append({
            'name': '数据库完整性',
            'status': 'ok' if all_ok else 'error',
            'detail': ', '.join([f'{k}: {v}' for k, v in integrity.items()])
        })
        if not all_ok:
            health['overall'] = 'degraded'
    except Exception as e:
        health['checks'].append({'name': '数据库完整性', 'status': 'error', 'detail': str(e)})
        health['overall'] = 'degraded'
    # 2. 调度器
    try:
        scheduler_info = get_scheduler_status()
        health['checks'].append({
            'name': '调度器',
            'status': 'ok' if scheduler_info.get('running') else 'warning',
            'detail': f'运行中 · {len(scheduler_info.get("jobs", []))}个任务' if scheduler_info.get('running') else '未运行'
        })
    except Exception as e:
        health['checks'].append({'name': '调度器', 'status': 'error', 'detail': str(e)})
        health['overall'] = 'degraded'
    # 3. 备份状态
    try:
        backups = list_backups()
        health['checks'].append({
            'name': '备份',
            'status': 'ok' if backups else 'info',
            'detail': f'共 {len(backups)} 个备份 · 最新: {backups[0]["date"] if backups else "尚未备份"}'
        })
    except Exception as e:
        health['checks'].append({'name': '备份', 'status': 'warning', 'detail': str(e)})
    # 4. 系统环境
    try:
        health['checks'].append({
            'name': '系统环境',
            'status': 'ok',
            'detail': f'Python {platform.python_version()} · {platform.system()} {platform.release()}'
        })
    except:
        pass
    # 5. 用户数据
    for user in USERS:
        try:
            db_path = get_user_db_path(user)
            if os.path.exists(db_path):
                size = os.path.getsize(db_path)
                size_str = f'{size/1024:.1f}KB' if size < 1024*1024 else f'{size/(1024*1024):.1f}MB'
                health['checks'].append({'name': f'{user}.db', 'status': 'ok', 'detail': size_str})
            else:
                health['checks'].append({'name': f'{user}.db', 'status': 'info', 'detail': '未创建'})
        except:
            pass
    return jsonify(health)


# ========== AI 深度调研 API ==========

# ========== 周报 API（自动从跟进历史采集） ==========

def get_week_start(date=None):
    """获取指定日期所在周的周一"""
    d = date or datetime.now()
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')


def get_week_range(week_start_str):
    """获取周一的日期和周末的日期"""
    d = datetime.strptime(week_start_str, '%Y-%m-%d')
    end = d + timedelta(days=6)
    return d.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


@app.route('/api/weekly-summary', methods=['GET'])
def weekly_summary():
    """自动从跟进历史生成某周的周报（不需要登录，供总览使用）"""
    week_start = request.args.get('week_start', '')
    if not week_start:
        week_start = get_week_start()
    
    week_start_str, week_end_str = get_week_range(week_start)
    week_label = f'{week_start_str} ~ {week_end_str}'
    
    result = {}
    for user in USERS:
        try:
            set_db_user(user)
            conn = get_db()
            c = conn.cursor()
            
            # 该周的跟进记录
            c.execute('''
                SELECT f.*, c.name as customer_name, c.company as customer_company
                FROM follow_up_logs f
                JOIN customers c ON f.customer_id = c.id
                WHERE f.follow_date >= ? AND f.follow_date <= ?
                ORDER BY f.follow_date DESC, f.created_at DESC
            ''', (week_start_str, week_end_str))
            follow_ups = [dict(row) for row in c.fetchall()]
            
            # 本周新增客户
            c.execute('SELECT COUNT(*) FROM customers WHERE created_at >= ? AND created_at <= ? AND (is_deleted = 0 OR is_deleted IS NULL)',
                      (week_start_str, week_end_str + ' 23:59:59'))
            new_customers = c.fetchone()[0]
            
            # 本周完成跟进数
            c.execute('SELECT COUNT(*) FROM follow_up_logs WHERE follow_date >= ? AND follow_date <= ?',
                      (week_start_str, week_end_str))
            follow_count = c.fetchone()[0]
            
            # 本周新增待办
            c.execute('SELECT COUNT(*) FROM reminders WHERE remind_date >= ? AND remind_date <= ?',
                      (week_start_str, week_end_str))
            new_reminders = c.fetchone()[0]
            
            # 总客户数
            c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
            total_customers = c.fetchone()[0]
            
            conn.close()
            
            # 结构化周报数据
            summary = {
                'user_id': user,
                'user_label': USERS[user]['label'],
                'user_color': USERS[user]['color'],
                'week_start': week_start_str,
                'week_end': week_end_str,
                'week_label': week_label,
                'stats': {
                    'new_customers': new_customers,
                    'follow_ups': follow_count,
                    'new_reminders': new_reminders,
                    'total_customers': total_customers,
                },
                'follow_ups': [{
                    'customer_name': f.get('customer_name', ''),
                    'customer_company': f.get('customer_company', ''),
                    'content': f.get('content', ''),
                    'result': f.get('result', ''),
                    'follow_date': f.get('follow_date', ''),
                    'next_plan': f.get('next_plan', ''),
                } for f in follow_ups],
            }
            result[user] = summary
        except Exception as e:
            logger.error(f'生成 {user} 周报失败: {e}')
            result[user] = {
                'user_id': user,
                'user_label': USERS[user]['label'],
                'user_color': USERS[user]['color'],
                'error': str(e),
            }
    
    set_db_user(session.get('user', ''))
    return jsonify(result)


@app.route('/api/overview/reports', methods=['GET'])
def overview_reports():
    """获取所有用户的周报汇总（自动生成）"""
    week_start = request.args.get('week_start', '')
    if not week_start:
        week_start = get_week_start()
    # 直接调用自动生成的周报
    from flask import Response as Rsp
    resp = weekly_summary()
    if isinstance(resp, tuple):
        data = resp[0]
    else:
        data = resp
    return data


# ========== 总览 API（周会展示用，只读） ==========

@app.route('/api/overview/stats', methods=['GET'])
def overview_stats():
    """获取所有用户的汇总统计数据（用于周会展示，不需要登录）"""
    result = {}
    for user in USERS:
        try:
            set_db_user(user)
            conn = get_db()
            c = conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')
            c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
            total = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date <= ?', (today,))
            pending = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM customers WHERE customer_type=? AND (is_deleted = 0 OR is_deleted IS NULL)', ('new',))
            new_count = c.fetchone()[0]
            c.execute('SELECT status, COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL) GROUP BY status')
            status_rows = c.fetchall()
            status_counts = {row[0]: row[1] for row in status_rows}
            conn.close()
            result[user] = {
                'total_customers': total,
                'pending_reminders': pending,
                'new_customers': new_count,
                'status_counts': status_counts,
                'label': USERS[user]['label'],
                'color': USERS[user]['color'],
            }
        except Exception as e:
            result[user] = {'error': str(e), 'label': USERS[user]['label'], 'color': USERS[user]['color']}
    set_db_user(session.get('user', ''))
    return jsonify(result)


@app.route('/api/overview/all-customers', methods=['GET'])
def overview_all_customers():
    """获取所有用户的所有客户（带归属人信息），用于总览"""
    all_customers = []
    search = request.args.get('search', '').strip().lower()
    
    for user in USERS:
        try:
            set_db_user(user)
            conn = get_db()
            c = conn.cursor()
            query = 'SELECT * FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)'
            params = []
            if search:
                query += ' AND (name LIKE ? OR company LIKE ? OR country LIKE ? OR field LIKE ?)'
                like = f'%{search}%'
                params.extend([like, like, like, like])
            query += ' ORDER BY updated_at DESC'
            c.execute(query, params)
            for row in c.fetchall():
                cust = dict(row)
                cust['owner'] = user
                cust['owner_label'] = USERS[user]['label']
                cust['owner_color'] = USERS[user]['color']
                all_customers.append(cust)
            conn.close()
        except Exception as e:
            logger.error(f'获取 {user} 客户失败: {e}')
    
    set_db_user(session.get('user', ''))
    return jsonify({'customers': all_customers, 'total': len(all_customers)})


# ========== 备份与恢复 API ==========

@app.route('/api/backup', methods=['POST'])
def api_backup():
    """手动触发数据库备份"""
    result = backup_database()
    if result.get('failed'):
        return jsonify({'success': False, 'error': f'部分备份失败: {result["failed"]}'}), 500
    return jsonify({'success': True, 'path': result.get('path', ''), 'files': result.get('backed_up', [])})


@app.route('/api/backup/list', methods=['GET'])
def api_backup_list():
    """列出所有可用备份"""
    backups = list_backups()
    return jsonify({'backups': backups})


@app.route('/api/backup/restore', methods=['POST'])
def api_backup_restore():
    """从指定日期恢复数据库"""
    data = request.json or {}
    backup_date = data.get('date', '')
    if not backup_date:
        return jsonify({'error': '请指定备份日期'}), 400
    result = restore_from_backup(backup_date)
    if result.get('success'):
        return jsonify({'success': True, 'restored': result.get('restored', [])})
    return jsonify({'success': False, 'error': result.get('error', '恢复失败')}), 500


@app.route('/api/backup/integrity', methods=['GET'])
def api_integrity():
    """检查所有数据库完整性"""
    integrity = check_integrity()
    return jsonify({'integrity': integrity})


# ========== 启动 ==========
if __name__ == '__main__':
    # 初始化所有数据库
    try:
        init_all_dbs()
    except Exception as e:
        print(f'数据库初始化失败: {e}')

    def _run_init():
        try:
            print('正在启动定时任务...')
            start_scheduler()
            print('定时任务已启动')
        except Exception as e:
            print(f'定时任务启动失败（不影响服务运行）: {e}')
            import traceback
            traceback.print_exc()

    init_thread = threading.Thread(target=_run_init, daemon=False)
    init_thread.start()

    # 优雅关闭
    def shutdown(signum=None, frame=None):
        print('\n正在安全关闭...')
        try:
            from db import backup_database
            backup_database()
            print('数据已备份')
        except Exception as e:
            print(f'关闭时备份失败: {e}')
        stop_scheduler()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print('客户跟进提醒系统启动中...')
    print(f'数据库目录: {DB_DIR}')
    try:
        app.run(debug=False, port=8080, host='0.0.0.0')
    except Exception as e:
        print(f'Flask 服务异常退出: {e}')
        import traceback
        traceback.print_exc()
        time.sleep(10)

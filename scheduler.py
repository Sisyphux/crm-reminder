"""
定时任务模块 - 每天 Windows 系统通知提醒（多用户版）
使用 APScheduler 实现定时调度
"""
import logging
import subprocess
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from db import get_db, set_db_user, USERS, USERS_LIST

logger = logging.getLogger(__name__)
scheduler = None


def get_today_reminders():
    """获取所有用户的今日及逾期提醒"""
    today = datetime.now().strftime('%Y-%m-%d')
    all_reminders = []
    user_stats = {}
    
    for user in USERS:
        try:
            set_db_user(user)
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                SELECT r.*, c.name as customer_name, c.company as customer_company, 
                       c.country, c.level, c.status, c.field
                FROM reminders r
                JOIN customers c ON r.customer_id = c.id
                WHERE r.is_done = 0 AND r.remind_date <= ?
                ORDER BY r.remind_date ASC, c.level DESC
            ''', (today,))
            user_reminders = [dict(row) for row in c.fetchall()]
            
            # Add user label to each reminder
            for r in user_reminders:
                r['user_label'] = USERS[user]['label']
            
            c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
            total = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date <= ?', (today,))
            pending = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date < ?', (today,))
            overdue = c.fetchone()[0]
            conn.close()
            
            user_stats[user] = {
                'total_customers': total,
                'pending_reminders': pending,
                'overdue_reminders': overdue,
                'label': USERS[user]['label'],
            }
            all_reminders.extend(user_reminders)
        except Exception as e:
            logger.error(f'获取 {user} 的提醒失败: {e}')
    
    # Reset user context
    set_db_user(None)
    
    # Aggregate stats
    total_all = sum(s.get('total_customers', 0) for s in user_stats.values())
    pending_all = sum(s.get('pending_reminders', 0) for s in user_stats.values())
    overdue_all = sum(s.get('overdue_reminders', 0) for s in user_stats.values())
    
    stats = {
        'total_customers': total_all,
        'pending_reminders': pending_all,
        'overdue_reminders': overdue_all,
        'per_user': user_stats,
    }
    
    return all_reminders, stats


def send_windows_notification(title, body):
    """发送 Windows 系统通知（使用 PowerShell）"""
    escaped_title = title.replace("'", "''")
    escaped_body = body.replace("'", "''")
    
    ps_script = f'''
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $textNodes = $template.GetElementsByTagName("text")
    $textNodes.Item(0).AppendChild($template.CreateTextNode('{escaped_title}')) | Out-Null
    $textNodes.Item(1).AppendChild($template.CreateTextNode('{escaped_body}')) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("客户跟进提醒系统").Show($toast)
    '''
    
    try:
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return True
    except Exception:
        # 备用方案：用 msg 命令（Windows 经典弹窗）
        try:
            subprocess.run(['msg', '*', f'{title}\n{body}'], capture_output=True, timeout=5)
            return True
        except Exception:
            return False


def send_daily_notification():
    """每天定时发送 Windows 系统通知"""
    logger.info("执行每日待办提醒通知...")

    try:
        reminders, stats = get_today_reminders()

        if not reminders:
            logger.info("今日没有待办提醒，跳过通知")
            return

        overdue = stats.get('overdue_reminders', 0)
        pending = stats.get('pending_reminders', 0)
        
        # 统计每个用户
        per_user = stats.get('per_user', {})
        user_lines = []
        for uid, us in per_user.items():
            if us.get('pending_reminders', 0) > 0:
                user_lines.append(f"  {us['label']}: {us['pending_reminders']}待办({us['overdue_reminders']}逾期)")

        # 生成通知正文：最多列 5 个客户
        lines = [f'待办 {pending} 项（逾期 {overdue} 项）']
        if user_lines:
            lines.append('───')
            lines.extend(user_lines)
            lines.append('───')
        for r in reminders[:5]:
            name = r.get('customer_company') or r.get('customer_name', '未知')
            label = r.get('user_label', '')
            lines.append(f'  [{label}] {name}' if label else f'  - {name}')
        if len(reminders) > 5:
            lines.append(f'  ... 还有 {len(reminders) - 5} 项')

        body = '\n'.join(lines)
        send_windows_notification('📋 客户跟进提醒', body)
        logger.info(f"✅ 系统通知已发送: {pending} 项待办, {overdue} 项逾期")

    except Exception as e:
        logger.error(f"通知发送异常: {str(e)}")


def start_scheduler():
    """启动定时调度器"""
    global scheduler

    if scheduler is not None and scheduler.running:
        logger.info("定时调度器已在运行")
        return

    scheduler = BackgroundScheduler()

    # 每天早上 9:00 发送 Windows 系统通知
    trigger = CronTrigger(hour=9, minute=0)
    scheduler.add_job(
        send_daily_notification,
        trigger=trigger,
        id='daily_notification',
        name='每日跟进提醒通知',
        replace_existing=True
    )

    scheduler.start()
    logger.info("✅ 定时调度器已启动，每天 9:00 系统通知提醒")


def stop_scheduler():
    """停止定时调度器"""
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown()
        logger.info("定时调度器已停止")


def get_scheduler_status():
    """获取调度器状态"""
    if scheduler is None:
        return {'running': False, 'jobs': []}

    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'name': job.name,
            'next_run_time': str(job.next_run_time) if job.next_run_time else None,
        })

    return {
        'running': scheduler.running,
        'jobs': jobs,
    }

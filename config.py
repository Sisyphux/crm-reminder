"""
客户跟进提醒系统 - 配置文件
所有可配置项集中管理
"""
import os

# ========== SMTP 邮件配置（已禁用）==========
# 由于 SSL 连接不稳定 (EOF occurred in violation of protocol)，邮件功能已禁用。
# 如需重新启用：1) 设置 enabled=True  2) 填写正确的邮箱账号密码
SMTP_CONFIG = {
    'enabled': False,
    'host': 'smtp.qiye.163.com',
    'port': 465,
    'user': 'hamid.luo@hzrj-intl.com',
    'password': '',
    'use_tls': True,
    'to_email': 'hamid.luo@hzrj-intl.com',
}

# 从环境变量读取配置（优先级高于上面的默认值）
if os.environ.get('SMTP_HOST'):
    SMTP_CONFIG['host'] = os.environ.get('SMTP_HOST')
    SMTP_CONFIG['port'] = int(os.environ.get('SMTP_PORT', 587))
    SMTP_CONFIG['user'] = os.environ.get('SMTP_USER', '')
    SMTP_CONFIG['password'] = os.environ.get('SMTP_PASSWORD', '')
    SMTP_CONFIG['use_tls'] = os.environ.get('SMTP_TLS', 'true').lower() == 'true'
    SMTP_CONFIG['to_email'] = os.environ.get('SMTP_TO_EMAIL', SMTP_CONFIG['user'])
    SMTP_CONFIG['enabled'] = os.environ.get('SMTP_ENABLED', 'false').lower() == 'true'


# ========== Excel 同步配置 ==========
EXCEL_CONFIG = {
    'filename': 'Hamid客户跟进表格.xlsx',
    'sheet_name': '中东客户跟进',  # Excel 中的 sheet 名称
    'enable_bidirectional_sync': False,  # 双向同步已禁用（会破坏Excel格式）
    'auto_remove_orphans': False,  # 自动删除数据库中已从Excel删除的客户（True=自动清理，False=保留不动）
    # 注意：已改为 False，防止系统自动删除用户手动添加的客户。
    # Excel 同步时不再自动清理，改为在界面的回收站中手动管理。
}

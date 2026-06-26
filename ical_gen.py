# -*- coding: utf-8 -*-
"""
iCalendar (.ics) 生成工具
为 CRM 系统提供日历订阅功能
符合 RFC 5545 标准
"""

from datetime import datetime, timezone
import uuid


def _fold_line(line, max_len=75):
    """RFC 5545 要求每行不超过 75 个字符，超过需折叠"""
    if len(line) <= max_len:
        return line
    result = [line[:max_len]]
    remaining = line[max_len:]
    while remaining:
        result.append(' ' + remaining[:73])  # 续行以空格开头，最多 74 字符
        remaining = remaining[73:]
    return '\r\n'.join(result)


def _escape_text(text):
    """转义 iCal 文本中的特殊字符"""
    text = text.replace('\\', '\\\\')
    text = text.replace(';', '\\;')
    text = text.replace(',', '\\,')
    text = text.replace('\n', '\\n')
    return text


def build_icalendar(reminders):
    """
    将提醒列表转换为 iCalendar (.ics) 格式字符串。
    
    reminders: list of dict, 每个 dict 应包含：
        - customer_name (str)
        - remind_date (str, 'YYYY-MM-DD')
        - content (str, optional)
        - id (int)
    
    返回: str (UTF-8 编码的 .ics 内容)
    """
    # DTSTAMP 必须是当前 UTC 时间，iOS 日历强制要求此字段
    now_utc = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//PROMAX CRM//Follow-up Calendar//ZH',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:客户跟进提醒',
        'X-WR-TIMEZONE:America/Chicago',  # 用户 CDT 时区
        'REFRESH-INTERVAL;VALUE=DURATION:PT6H',  # 建议刷新间隔 6 小时
        'X-PUBLISHED-TTL:PT6H',
    ]

    for r in reminders:
        uid = str(uuid.uuid4())
        date_str = r.get('remind_date', '')
        if date_str:
            date_ical = date_str.replace('-', '')
        else:
            date_ical = datetime.now(timezone.utc).strftime('%Y%m%d')

        customer = r.get('customer_name', 'Unknown')
        content = r.get('content', '')
        desc = _escape_text(content) if content else '\u8ddf\u8fdb\u63d0\u9192'

        # 每行不超过 75 字符（RFC 5545）
        lines.append('BEGIN:VEVENT')
        lines.append(f'UID:{uid}')
        lines.append(f'DTSTAMP:{now_utc}')
        lines.append(f'DTSTART;VALUE=DATE:{date_ical}')
        lines.append(f'DTEND;VALUE=DATE:{date_ical}')
        lines.append(_fold_line(f'SUMMARY:{customer}'))
        lines.append(_fold_line(f'DESCRIPTION:{desc}'))
        lines.append('STATUS:CONFIRMED')
        lines.append('TRANSP:TRANSPARENT')
        lines.append('END:VEVENT')

    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'

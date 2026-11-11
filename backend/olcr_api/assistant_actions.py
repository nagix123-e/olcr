"""Typed everyday actions, shared by text/voice chat and inline controls."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .assistant_integrations import ActionError, GoogleCalendar, PlayerTickets, search_youtube, YOUTUBE_HANDLE
from .permissions import capability_enabled


ERRORS = {
    'EXTERNAL_ACCESS_DISABLED': ('外部アクセスがオフです。設定で有効にしてください。', 'External access is off. Enable it in Settings.'),
    'CALENDAR_ACTION_CHANGED': ('予定の確認内容が更新されたか、有効期限が切れました。最新の予定を確認してください。','The event draft changed or expired. Review the latest event.'),
    'ALARM_TIME_REQUIRED': ('アラームを鳴らす時刻を1つ指定してください。例：明日の午前7時、23:30。', 'Specify one alarm time, such as tomorrow at 7 AM or 23:30.'),
    'ALARM_TIME_PAST': ('指定日時は過ぎています。未来の時刻を指定してください。', 'That alarm time has passed. Choose a future time.'),
    'ALARM_TIME_TOO_FAR': ('アラームは7日以内で指定してください。', 'Choose an alarm time within seven days.'),
    'ALARM_CONTROL_UNSUPPORTED': ('時刻指定アラームは一時停止できません。取り消して設定し直してください。', 'A fixed-time alarm cannot be paused. Cancel it and set a new time.'),
    'TIMER_DURATION_REQUIRED': ('タイマーの長さを指定してください。例：10分のタイマー。', 'Specify a duration, for example: set a timer for 10 minutes.'),
    'TIMER_DURATION_INVALID': ('タイマーは1秒から7日までの長さで指定してください。', 'Timer duration must be between 1 second and 7 days.'),
    'TIMER_NOT_FOUND': ('対象のタイマーが見つかりません。', 'The timer was not found.'),
    'TIMER_STATE_CHANGED': ('タイマーの状態が変わりました。最新の状態を確認してください。', 'The timer state changed. Check its current state.'),
    'TIMER_LIMIT_REACHED': ('実行中のタイマーが多すぎます。不要なタイマーを停止してください。', 'Too many active timers. Cancel an unused timer first.'),
    'CALENDAR_NOT_CONNECTED': ('設定の「カレンダーに接続」を押し、macOSのアクセス許可を承認してください。', 'Choose Connect Calendar in Settings and allow macOS calendar access.'),
    'CALENDAR_PERMISSION_DISABLED': ('設定の「サービス接続」で、カレンダーのカテゴリ許可をオンにしてください。', 'Enable Calendar category access in Service connections.'),
    'MACOS_CALENDAR_UNAVAILABLE': ('macOSカレンダー機能を起動できません。OLCRを更新して開き直してください。', 'macOS Calendar is unavailable. Update and reopen OLCR.'),
    'CALENDAR_NATIVE_SETUP': ('APIの初期設定は不要です。「カレンダーに接続」からmacOSの許可を承認してください。', 'No API setup is needed. Choose Connect Calendar and allow macOS access.'),
    'CALENDAR_PROVIDER_CHANGED': ('カレンダーの接続先が変わりました。予定の確認をやり直してください。', 'The calendar connection changed. Review the event again.'),
    'CALENDAR_NO_CALENDARS': ('使用できる予定表がありません。Macのカレンダーアプリで予定表を追加してください。', 'No calendar is available. Add one in the Mac Calendar app.'),
    'CALENDAR_AUTH_TIMEOUT': ('カレンダーの許可を待っています。macOSの確認画面で許可してから再試行してください。', 'Allow calendar access in the macOS prompt, then try again.'),
    'CALENDAR_CLIENT_REQUIRED': ('Googleのデスクトップ用OAuthクライアントIDを設定すると、ブラウザで接続できます。', 'Set a Google desktop OAuth client ID to connect through your browser.'),
    'CALENDAR_RECONNECT_REQUIRED': ('Googleカレンダーへの再接続が必要です。設定から接続し直してください。', 'Reconnect Google Calendar in Settings.'),
    'CALENDAR_NETWORK_FAILED': ('カレンダーに接続できませんでした。通信状態を確認してください。', 'Could not reach your calendar. Check your connection.'),
    'CALENDAR_EVENT_CHANGED': ('この予定は別の場所で変更されました。最新の予定を取得して確認し直してください。', 'This event changed elsewhere. Fetch and review it again.'),
    'CALENDAR_ACCESS_DENIED': ('カレンダーへのアクセスが許可されていません。macOSの「プライバシーとセキュリティ」→「カレンダー」でOLCRを許可してください。', 'Allow OLCR in macOS Privacy & Security → Calendars.'),
    'MACOS_CALENDAR_AUTH_FAILED': ('macOSのカレンダー許可を要求できませんでした。更新したOLCRを開き直して再試行してください。', 'Could not request macOS Calendar access. Reopen the updated OLCR and try again.'),
    'CALENDAR_PERMISSION_DENIED': ('このカレンダーを操作する権限がありません。接続先と共有権限を確認してください。', 'Calendar access was denied. Check your account and calendar permissions.'),
    'CALENDAR_EVENT_NOT_FOUND': ('この予定は見つかりません。削除や移動が行われた可能性があります。', 'The event was not found. It may have been deleted or moved.'),
    'CALENDAR_REQUEST_FAILED': ('カレンダーへの操作に失敗しました。接続状態を確認してください。', 'The calendar request failed. Check the connection.'),
    'CALENDAR_TIME_REQUIRED': ('予定の日時とタイトルを確認してください。', 'Check the event title, start and end times.'),
    'CALENDAR_ACTION_EXPIRED': ('確認画面の有効期限が切れました。予定を取得し直してください。', 'This preview expired. Fetch the event again.'),
    'CALENDAR_ACTION_NOT_FOUND': ('この会話の予定操作が見つかりません。', 'This calendar action was not found in this chat.'),
    'CALENDAR_ACTION_BUSY': ('この予定操作は処理中です。重複して実行しません。', 'This calendar action is processing. It will not be repeated.'),
    'CALENDAR_REVISION_INVALID': ('予定の確認内容が一致しません。確認画面を開き直してください。', 'The preview changed. Open it again before confirming.'),
    'CALENDAR_TIMEZONE_INVALID': ('有効なタイムゾーンを指定してください。', 'Specify a valid time zone.'),
    'CALENDAR_RANGE_INVALID': ('予定の取得期間を確認してください。最大366日間まで指定できます。', 'Check the date range. Up to 366 days is supported.'),
    'CALENDAR_CLIENT_ID_INVALID': ('Googleのデスクトップ用OAuthクライアントIDを確認してください。', 'Check your Google desktop OAuth client ID.'),
    'CALENDAR_CLIENT_SECRET_INVALID': ('OAuthクライアントシークレットを確認してください。', 'Check the OAuth client secret.'),
    'CALENDAR_AUTH_FAILED': ('Googleとの接続を完了できませんでした。再試行してください。', 'Google sign-in did not complete. Try again.'),
    'CALENDAR_AUTH_CANCELLED': ('Googleとの接続がキャンセルされました。', 'Google sign-in was cancelled.'),
    'YOUTUBE_QUERY_REQUIRED': ('曲名、アーティスト名、またはYouTubeの動画URLを指定してください。', 'Specify a song, artist, or YouTube video URL.'),
    'YOUTUBE_SEARCH_NOT_CONFIGURED': ('YouTubeは接続設定なしで使えます。曲名や動画URLを指定してください。', 'YouTube needs no connection setup. Specify a song or video URL.'),
    'YOUTUBE_SEARCH_FAILED': ('YouTubeの候補を取得できませんでした。YouTubeで検索して再生できます。', 'Could not load YouTube results. Open YouTube search to play.'),
    'YOUTUBE_NO_RESULTS': ('再生できるYouTube動画が見つかりませんでした。曲名や動画URLを変えてください。', 'No playable YouTube videos were found. Try another name or video URL.'),
    'YOUTUBE_VIDEO_INVALID': ('YouTubeの動画URLを確認してください。', 'Check the YouTube video URL.'),
    'YOUTUBE_PLAYER_EXPIRED': ('プレイヤーの有効期限が切れました。もう一度再生してください。', 'The player session expired. Start playback again.'),
}


def message_for(code, language='ja'):
    return ERRORS.get(code, ('操作を完了できませんでした。設定と入力内容を確認してください。',
                             'Could not complete the action. Check your settings and request.'))[language != 'ja']


def number(value):
    try:
        return float(value)
    except ValueError:
        words = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'fifteen': 15,
                 'twenty': 20, 'thirty': 30, 'forty': 40, 'sixty': 60}
        if value.lower() in words:
            return words[value.lower()]
        digits = dict(zip('零〇一二三四五六七八九', (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
        total = current = 0
        for char in value:
            if char in digits:
                current = current * 10 + digits[char]
            elif char in '十百千':
                total += (current or 1) * {'十': 10, '百': 100, '千': 1000}[char]
                current = 0
            else:
                raise ValueError('number')
        return total + current


def duration_seconds(text):
    source = text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    if re.search(r'[-−]\s*\d|マイナス|negative', source, re.I):
        raise ActionError('TIMER_DURATION_INVALID')
    pattern = r'(\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千]+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|sixty)\s*(時間|分間?|秒間?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|[hms])(?![A-Za-z])'
    seconds, matched = 0, False
    for match in re.finditer(pattern, source, re.I):
        unit = match[2].lower()
        multiplier = 86400 if unit.startswith('day') else 3600 if unit.startswith(('時間', 'h')) else 60 if unit.startswith(('分', 'm')) else 1
        seconds += number(match[1]) * multiplier
        if source[match.end():].startswith('半'):
            seconds += multiplier / 2
        matched = True
    if not matched:
        raise ActionError('TIMER_DURATION_REQUIRED')
    if not math.isfinite(seconds) or not 1 <= seconds <= 604800:
        raise ActionError('TIMER_DURATION_INVALID')
    return round(seconds)


def alarm_deadline(text, zone, now=None):
    """One-shot wall-clock alarm. An unspecified day means the next occurrence."""
    import unicodedata
    text=unicodedata.normalize('NFKC',text)
    tz=timezone(zone);now=now.astimezone(tz) if now else datetime.fromtimestamp(time.time(),tz)
    if re.search(r'[-−]\s*\d+\s*(?:時|:)|マイナス|negative',text,re.I): raise ActionError('ALARM_TIME_REQUIRED')
    pattern=r'(?<![\d])(?:(午前|午後|am|pm)\s*)?(\d{1,2}|[零〇一二三四五六七八九十]+)(?::(\d{2})|時(?:(\d{1,2}|[零〇一二三四五六七八九十]+)分|(半))?)(?:\s*(am|pm))?'
    matches=list(re.finditer(pattern,text,re.I))
    if not matches:
        matches=list(re.finditer(r'(?:(am|pm)\s*)?(\d{1,2})()()()(?:\s*(am|pm))\b',text,re.I))
    if not matches:
        matches=list(re.finditer(r'\bat\s+()(\d{1,2})()()()()\b',text,re.I))
    if len(matches)!=1: raise ActionError('ALARM_TIME_REQUIRED')
    m=matches[0];hour=int(number(m[2]));minute=int(number(m[3] or m[4])) if m[3] or m[4] else 30 if m[5] else 0
    period=(m[1] or m[6] or '').lower()
    if minute>59 or hour>23 or (period and not 1<=hour<=12): raise ActionError('ALARM_TIME_REQUIRED')
    if period in {'午後','pm'} and hour<12: hour+=12
    if period in {'午前','am'} and hour==12: hour=0
    explicit=bool(re.search(r'\d{1,4}[年/月-]\d{1,2}|今日|明日|明後日|曜|today|tomorrow|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',text,re.I))
    try:
        day=calendar_window(text,zone,now)[0]
        target=day.replace(hour=hour,minute=minute,second=0,microsecond=0)
        if target.timestamp()<=now.timestamp():
            if explicit: raise ActionError('ALARM_TIME_PAST')
            target+=timedelta(days=1)
        roundtrip=datetime.fromtimestamp(target.timestamp(),tz)
        if roundtrip.replace(tzinfo=None)!=target.replace(tzinfo=None): raise ActionError('ALARM_TIME_REQUIRED')
        if target.timestamp()-now.timestamp()>604800: raise ActionError('ALARM_TIME_TOO_FAR')
    except ValueError as exc: raise ActionError('ALARM_TIME_REQUIRED') from exc
    return target.timestamp()


def implicit_music_request(text):
    """Bare artist/title playback commands, without treating every 'かけて' as music."""
    # Everyday operations and local playback have their own execution paths.
    if re.search(r'電話|通話|声を|声かけ|声掛け|呼びかけ|呼び掛け|話しかけ|話し掛け|鍵|カギ|ロック|保険|お金|費用|コスト|リスク|負荷|時間を|タイマー|アラーム|目覚まし|予定|カレンダー|ローカル|ファイル|このMac',text,re.I):
        return False
    if re.search(r'(?:を|の後に|したら)?(?:かけて|流して|聴かせて|聞かせて)(?:ください|下さい|くれる|もらえる)?[。.!！?？]*$',text):
        target=re.sub(r'(?:を)?(?:かけて|流して|聴かせて|聞かせて)(?:ください|下さい|くれる|もらえる)?[。.!！?？]*$','',text).strip()
        # An artist + title, a quoted title, or a bare title/artist with を.
        return bool(target and (re.search(r'.+の.+',target)
            or re.search(r'[「『"].+[」』"]',target)
            or re.search(r'を(?:かけて|流して|聴かせて|聞かせて)',text)))
    return bool(re.match(r'^play\s+.+\s+by\s+.+',text,re.I))


def youtube_request(text):
    """Extract search terms and ordering without depending on creator names."""
    import unicodedata
    text=unicodedata.normalize('NFKC',text)
    if re.search(r'https?://',text):return text,'relevance'
    text=re.sub(r'ようつべ|ユーチューブ|ユーチュブ','YouTube',text,flags=re.I)
    latest=bool(re.search(r'最新|新着|一番新しい|もっとも新しい|\b(?:latest|newest|most recent)\b',text,re.I))
    text=re.sub(r'(?:\bon\s+)?youtube(?:\s*music)?(?:で|から)?','',text,flags=re.I)
    text=re.sub(r'最新(?:の)?|新着(?:の)?|一番新しい|もっとも新しい|おすすめ(?:の)?|お勧め(?:の)?|オススメ(?:の)?|\b(?:the\s+)?(?:latest|newest|most recent|recommended)\b','',text,flags=re.I)
    text=re.sub(r'^(?:play|search|find|show|watch|recommend)(?:\s+me)?\s*(?:for\s+)?','',text.strip(),flags=re.I)
    text=re.sub(r'\bvideos?\s+(?:by|from|of)\s+','',text,flags=re.I)
    text=re.sub(r'(?:の)?動画(?:を)?(?:出して|見せて|教えて|再生して|探して|検索して|表示して|見たい|観たい)(?:ください)?[。.!！]*$','',text)
    text=re.sub(r'(?:を)?(?:検索して|検索|再生して|再生|かけて|流して|聴かせて|聞かせて|探して|見つけて)(?:ください|下さい|くれる|もらえる)?[。.!！?？]*$','',text)
    text=re.sub(r'(ミュージックビデオ|(?<![A-Za-z])MV)(?:を|が)?(?:見たい|観たい|見せて)(?:ください)?[。.!！?？]*$',r'\1',text,flags=re.I)
    text=re.sub(r'\s+please[.!]*$','',text,flags=re.I)
    return re.sub(r'\s+',' ',text).strip(' 、,「」"。.!！'), 'date' if latest else 'relevance'


def action_intent(text):
    """Recognize explicit operations; ordinary discussion/code requests stay in chat."""
    from .mail_chat import matches as mail_operation
    if mail_operation(text):
        return None
    text = re.sub(r'ようつべ|ユーチューブ|ユーチュブ','YouTube',text,flags=re.I).strip().replace('たいまー','タイマー').replace('あらーむ','アラーム')
    if re.match(r'^/(timer|calendar|youtube)(?:\s|$)', text, re.I):
        service, _, argument = text[1:].partition(' ')
        return {'service': service.lower(), 'text': argument.strip()}
    video_search = (bool(re.search(r'youtube|動画|ミュージックビデオ|(?<![A-Za-z])MV(?![A-Za-z])|\bvideos?\b', text, re.I))
                    and bool(re.search(r'検索|探して|探し|見つけ|見せて|見たい|観たい|出して|教えて|再生|流して|かけて|聴かせて|聞かせて|おすすめ|お勧め|オススメ|\b(?:search|find|show|watch|recommend)\b|look\s+for', text, re.I)))
    if video_search and not re.search(r'```|実装(?:して|する|したい)|設計(?:して|する)|(?:設計|使い方|仕組み|実現方法)(?:を)?(?:教えて|説明して|知りたい)|機能.*(?:実装|追加|作って)|\b(?:implement|implementation|build|refactor)\b', text, re.I) and not (not re.search(r'youtube', text, re.I) and re.search(r'ローカル|このMac|ファイル', text)) and len(text) <= 1600:
        return {'service': 'youtube', 'text': text}
    if '```' in text or len(text) > 1600 or re.search(
            r'実装|実現方法|仕組み|設計|コード|機能を追加|\bevent\s+(?:handler|listener|bus|loop)\b|'
            r'使い方(?:を)?(?:教えて|説明して|知りたい)|とは|って(?:何|なに)|'
            r'\b(?:implement|implementation|code|build|explain|what is|how (?:do|does|to))\b',
            text, re.I):
        return None
    timer = bool(re.search(r'タイマー|\btimers?\b', text, re.I))
    alarm = bool(re.search(r'アラーム|目覚まし|\balarms?\b',text,re.I))
    calendar = bool(re.search(r'カレンダー|予定|スケジュール|会議|ミーティング|\b(?:calendar|appointments?|events?|meetings?|schedule)\b', text, re.I))
    media = bool(re.search(r'youtube|youtu\.be|音楽|曲|\b(?:music|song|track)\b', text, re.I))
    media = (media or implicit_music_request(text)) and bool(re.search(r'再生|かけ|流し|聴かせ|聞かせ|止め|停止|再開|検索|探し|今の曲|\b(?:play|pause|resume|stop|search|find|playing)\b|https?://', text, re.I))
    verb = bool(re.search(r'かけ|再生|流し|聴かせ|聞かせ|止め|停止|一時停止|再開|残り|一覧|教え|見せ|表示|確認|セット|追加|設定|開始|キャンセル|削除|消し|変更|ずら|接続|ある|入れ|登録|書き込|書込|鳴ら|検索|探し|お願い|\b(?:play|pause|resume|stop|cancel|set|start|list|show|what|add|create|schedule|delete|remove|change|move|connect|search|find)\b', text, re.I))
    if calendar and re.search(r'今日|明日|明後日|今週|来週|\b(?:today|tomorrow|week)\b', text, re.I):
        verb = True
    if media and re.search(r'https?://(?:www\.)?(?:youtube\.com|youtu\.be)/', text):
        verb = True
    if not verb and not (timer and re.search(r'(?:\d+|[一二三四五六七八九十百]+)\s*(?:分|秒|時間|min|sec|hour)', text, re.I)):
        return None
    services = [s for s, yes in [('timer', timer), ('alarm', alarm), ('calendar', calendar), ('youtube', media)] if yes]
    return {'service': services[0], 'text': text} if len(services) == 1 else ({'service': 'clarify', 'text': text} if len(services) > 1 else None)


def timezone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ActionError('CALENDAR_TIMEZONE_INVALID')


def calendar_window(text, zone, now=None):
    now = now.astimezone(timezone(zone)) if now else datetime.now(timezone(zone))
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r'明後日|day after tomorrow', text, re.I):
        day += timedelta(days=2)
    elif re.search(r'明日|tomorrow', text, re.I):
        day += timedelta(days=1)
    elif re.search(r'昨日|yesterday', text, re.I):
        day -= timedelta(days=1)
    elif re.search(r'来週|next week', text, re.I):
        day += timedelta(days=7 - day.weekday())
    elif re.search(r'今週|this week', text, re.I):
        day -= timedelta(days=day.weekday())
    explicit = re.search(r'(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})(?:日)?', text)
    if explicit:
        try:
            day = day.replace(year=int(explicit[1] or day.year), month=int(explicit[2]), day=int(explicit[3]))
        except ValueError:
            raise ActionError('CALENDAR_TIME_REQUIRED')
    weekday = re.search(r'(月|火|水|木|金|土|日)曜|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', text, re.I)
    if weekday and not explicit:
        target = '月火水木金土日'.index(weekday[1]) if weekday[1] else ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'].index(weekday[2].lower())
        delta = (target - now.weekday()) % 7
        if re.search(r'来週|next\b', text, re.I):
            delta = target + 7 - now.weekday()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=delta)
    days = 7 if re.search(r'今週|来週|this week|next week|次の予定|upcoming|next events?', text, re.I) and not weekday else 1
    return day, day + timedelta(days=days)


def event_proposal(text, zone, now=None):
    # Explicit field labels delimit metadata so times in URLs/notes do not
    # accidentally become the event's end time.
    labels=r'場所|location|メモ|備考|notes?|URL|通話URL|ビデオ通話|カレンダー|calendar|招待者|参加者|添付|attendees|attachments'
    pattern=re.compile(r'(?:^|[、,;；\n]\s*)('+labels+r')\s*(?:[:：]|は)\s*',re.I)
    matches=list(pattern.finditer(text));core=text[:matches[0].start()] if matches else text
    result=_timed_event_proposal(core,zone,now)
    fields={}
    for i,m in enumerate(matches):
        value=text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)].strip()
        fields[m[1].lower()]=value
    mapping={'場所':'location','location':'location','メモ':'description','備考':'description','note':'description','notes':'description',
             'url':'url','通話url':'url','ビデオ通話':'url','カレンダー':'calendar_name','calendar':'calendar_name'}
    for label,key in mapping.items():
        if label in fields:result[key]=fields[label]
    unsupported=[label for label in fields if label in {'招待者','参加者','attendees','添付','attachments'}]
    if unsupported:result['unsupported_fields']=unsupported
    return result


def _timed_event_proposal(text, zone, now=None):
    import unicodedata
    # Quoted event names are content, not date/time instructions.
    timing=re.sub(r'[「『"“].*?[」』"”]','',unicodedata.normalize('NFKC',text))
    timing=re.sub(r'[零〇一二三四五六七八九十百千]+(?=年|月|日|時|分|秒)',lambda m:str(int(number(m[0]))),timing)
    timing=re.sub(r'夕方|夜|晩','午後',timing)
    timing=timing.replace('朝','午前').replace('正午','12時').replace('真夜中','0時')
    start, _ = calendar_window(timing, zone, now)
    if re.search(r'終日|一日中|\ball[ -]day\b',timing,re.I):
        return {'summary':extract_title(text),'start':start.date().isoformat(),'end':(start+timedelta(days=1)).date().isoformat(),'timeZone':zone,'all_day':True}
    pattern=r'(?:(午前|午後|am|pm)\s*)?(\d{1,2})(?:時(?!間)(?:(\d{1,2})分|(?P<half>半))?|:(\d{2}))\s*(am|pm)?'
    matches=list(re.finditer(pattern,timing,re.I))
    if not matches:
        matches=list(re.finditer(r'\bat\s+()(\d{1,2})()(?P<half>)()\s*(am|pm)?\b',timing,re.I))
    if not matches:
        return {'summary':extract_title(text),'start':'','end':'','timeZone':zone}
    def clock(match,inherited=''):
        hour,minute=int(match[2]),int(match[3] or match[5] or (30 if match['half'] else 0))
        period=(match[1] or match[6] or inherited).lower()
        if hour>23 or minute>59 or (period and hour>12):raise ActionError('CALENDAR_TIME_REQUIRED')
        if period in {'午後','pm'} and hour<12:hour+=12
        if period in {'午前','am'} and hour==12:hour=0
        return hour,minute
    hour,minute=clock(matches[0]);start=start.replace(hour=hour,minute=minute)
    end=start+timedelta(hours=1)
    if len(matches)>1:
        hour,minute=clock(matches[1],matches[0][1] or matches[0][6] or '')
        end=start.replace(hour=hour,minute=minute)
        if end<=start:end+=timedelta(days=1)
    else:
        duration=re.search(r'(?:for\s+|長さ|所要時間|から)(.+)',timing,re.I)
        if duration:
            try:end=start+timedelta(seconds=duration_seconds(duration[1]))
            except ActionError:pass
    return {'summary':extract_title(text),'start':start.isoformat(),'end':end.isoformat(),'timeZone':zone}


def extract_title(text):
    quoted = re.search(r'[「『"“](.+?)[」』"”]', text)
    if quoted:
        return quoted[1][:200]
    japanese = re.search(r'(?:時(?:[0-9零〇一二三四五六七八九十]+分|半)?|:\d{2})\s*(?:に|から)\s*(.+?)(?:という予定|の予定|予定|を追加|を登録|を入れ|を作成|を書き込|を書込)', text)
    if japanese:
        return japanese[1].strip(' にを')[:200]
    english = re.search(r'\b(?:add|create|schedule)\s+(?:an?\s+)?(?:event\s+)?(.+?)(?=\s+(?:today|tomorrow|on|at|for)\b|$)', text, re.I)
    return english[1].strip()[:200] if english else ''


def event_target(text):
    quoted = re.search(r'[「『"“](.+?)[」』"”]', text)
    if quoted:
        return quoted[1].strip()
    japanese = re.search(r'^(?:(?:今日|明日|明後日|今週|来週)の?)?(.+?)(?:の予定)?を(?:.+?に)?(?:変更|ずら|削除|キャンセル)', text)
    if japanese:
        return japanese[1].strip().removeprefix('カレンダーの')
    english = re.search(r'\b(?:move|change|delete|remove|cancel)\s+(?:the\s+)?(.+?)(?=\s+(?:to|at|on|today|tomorrow)\b|$)', text, re.I)
    return english[1].strip() if english else ''


class AssistantActions:
    def __init__(self, db, settings, calendar=None):
        self.db, self.settings = db, settings
        from .apple_calendar import AppleCalendar
        self.calendar = calendar or AppleCalendar(db)
        self.calendar.network_guard = self.network
        self.players = PlayerTickets()
        self.youtube_key = os.environ.get(YOUTUBE_HANDLE, '')
        self.lock = threading.RLock()
        self.calendar_lock = threading.RLock()
        self._turn_locks = {}
        self._turn_locks_guard = threading.Lock()
        with db.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS assistant_timers(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  name TEXT NOT NULL, duration REAL NOT NULL, deadline REAL, remaining REAL NOT NULL,
                  status TEXT NOT NULL, notification_status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_calendar_actions(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  action TEXT NOT NULL, calendar_id TEXT NOT NULL, event_id TEXT, etag TEXT, payload_json TEXT NOT NULL,
                  status TEXT NOT NULL, revision TEXT NOT NULL, expires_at REAL NOT NULL, result_json TEXT);
                CREATE TABLE IF NOT EXISTS assistant_requests(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  request_hash TEXT NOT NULL, result_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_preferences(id INTEGER PRIMARY KEY CHECK(id=1), value_json TEXT NOT NULL);
            ''')
            con.execute("UPDATE assistant_calendar_actions SET status='uncertain' WHERE status='executing'")
            columns = {row[1] for row in con.execute('PRAGMA table_info(assistant_calendar_actions)')}
            if 'provider' not in columns:
                con.execute("ALTER TABLE assistant_calendar_actions ADD COLUMN provider TEXT NOT NULL DEFAULT 'google'")

        with db.connect() as con:
            if 'kind' not in {row[1] for row in con.execute('PRAGMA table_info(assistant_timers)')}:
                con.execute("ALTER TABLE assistant_timers ADD COLUMN kind TEXT NOT NULL DEFAULT 'timer'")

    def network(self):
        if not capability_enabled(self.settings(), 'external_access_enabled'):
            raise ActionError('EXTERNAL_ACCESS_DISABLED')

    def calendar_network(self):
        if not getattr(self.calendar, 'native', False):
            self.network()
            return
        with self.db.connect() as con:
            table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_connector_categories'").fetchone()
            row = con.execute('SELECT enabled FROM service_connector_categories WHERE id=?', ('calendar',)).fetchone() if table else None
        if not row or not row[0]:
            raise ActionError('CALENDAR_PERMISSION_DISABLED')

    def turn_lock(self, conversation_id):
        with self._turn_locks_guard:
            return self._turn_locks.setdefault(conversation_id, threading.RLock())

    def preferences(self):
        with self.db.connect() as con:
            row = con.execute('SELECT value_json FROM assistant_preferences WHERE id=1').fetchone()
        value = json.loads(row[0]) if row else {'calendar_id': 'primary', 'time_zone': 'UTC'}
        if getattr(self.calendar, 'native', False) and value.get('calendar_provider') != 'apple':
            value['calendar_id'] = 'primary'
        return value

    def save_preferences(self, calendar_id, time_zone):
        timezone(time_zone)
        if not calendar_id or len(calendar_id) > 512:
            raise ActionError('CALENDAR_EVENT_INVALID')
        value = {'calendar_id': calendar_id, 'time_zone': time_zone,
                 'calendar_provider': getattr(self.calendar, 'provider', 'google')}
        with self.db.connect() as con:
            con.execute('INSERT INTO assistant_preferences VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json', (json.dumps(value),))
        return value

    def timers(self, active=False):
        now = time.time()
        with self.db.connect() as con:
            con.execute("UPDATE assistant_timers SET status='finished',remaining=0,updated_at=? WHERE status='running' AND deadline<=?", (now, now))
            rows = con.execute("SELECT * FROM assistant_timers ORDER BY CASE WHEN status IN ('running','paused') THEN 0 ELSE 1 END,created_at DESC LIMIT 300").fetchall()
        items = [dict(row) for row in rows]
        return [t for t in items if not active or t['status'] in {'running', 'paused'}]

    def timer(self, ident):
        now = time.time()
        with self.db.connect() as con:
            con.execute("UPDATE assistant_timers SET status='finished',remaining=0,updated_at=? WHERE id=? AND status='running' AND deadline<=?", (now, ident, now))
            row = con.execute('SELECT * FROM assistant_timers WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ActionError('TIMER_NOT_FOUND')
        return dict(row)

    def start_timer(self, seconds, name, conversation_id, ident=None, *, deadline=None, kind='timer'):
        if not math.isfinite(seconds) or not 1 <= seconds <= 604800:
            raise ActionError('TIMER_DURATION_INVALID')
        ident = ident or uuid.uuid4().hex
        now = time.time()
        if kind not in {'timer','alarm'}: raise ActionError('TIMER_DURATION_INVALID')
        target=now+seconds if deadline is None else deadline
        if not math.isfinite(target) or target<=now or target-now>604800: raise ActionError('TIMER_DURATION_INVALID')
        with self.lock, self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("UPDATE assistant_timers SET status='finished',remaining=0,updated_at=? WHERE status='running' AND deadline<=?", (now, now))
            count = con.execute("SELECT count(*) FROM assistant_timers WHERE status IN ('running','paused')").fetchone()[0]
            if count >= 100:
                raise ActionError('TIMER_LIMIT_REACHED')
            con.execute("INSERT OR IGNORE INTO assistant_timers(id,conversation_id,name,duration,deadline,remaining,status,notification_status,created_at,updated_at,kind) VALUES(?,?,?,?,?,?,'running','pending',?,?,?)",
                        (ident, conversation_id, name[:80], seconds, target, seconds, now, now, kind))
        return self.timer(ident)

    def update_timer(self, ident, action):
        with self.lock:
            item = self.timer(ident)
            now = time.time()
            if item.get('kind')=='alarm' and action in {'pause','resume'}: raise ActionError('ALARM_CONTROL_UNSUPPORTED')
            if action == 'cancel' and item['status'] in {'running', 'paused'}:
                item.update(status='cancelled', deadline=None, remaining=0)
            elif action == 'pause' and item['status'] == 'running':
                item.update(status='paused', remaining=max(0, item['deadline'] - now), deadline=None)
            elif action == 'resume' and item['status'] == 'paused':
                item.update(status='running', deadline=now + item['remaining'])
            else:
                raise ActionError('TIMER_STATE_CHANGED')
            with self.db.connect() as con:
                con.execute('UPDATE assistant_timers SET status=?,deadline=?,remaining=?,notification_status=?,updated_at=? WHERE id=?',
                            (item['status'], item['deadline'], item['remaining'], 'pending', now, ident))
            return self.timer(ident)

    def notification_result(self, ids, status):
        if status not in {'scheduled', 'denied', 'unavailable', 'failed'}:
            raise ActionError('TIMER_STATE_CHANGED')
        with self.db.connect() as con:
            for ident in ids[:100]:
                con.execute('UPDATE assistant_timers SET notification_status=? WHERE id=? AND status IN (\'running\',\'finished\')', (status, ident))

    def _native_create_calendar(self, requested):
        calendars=self.calendar.calendars()
        writable=[item for item in calendars if item.get('writable')]
        chosen=next((item for item in writable if item.get('id')==requested),None)
        if not chosen and requested not in {'primary',''}:
            named=[item for item in writable if item.get('title')==requested]
            if len(named)!=1:raise ActionError('CALENDAR_NOT_FOUND')
            chosen=named[0]
        chosen=chosen or next((item for item in writable if item.get('primary')),None) or next(iter(writable),None)
        if not chosen:raise ActionError('CALENDAR_NO_CALENDARS')
        return chosen['id']

    def prepare_calendar(self, conversation_id, action='create', event_id='', payload=None, ident=None, calendar_id=None):
        self.calendar_network()
        if not self.calendar.status()['connected']:
            raise ActionError('CALENDAR_NOT_CONNECTED')
        preferences = self.preferences()
        calendar_id, zone = calendar_id or preferences['calendar_id'], preferences['time_zone']
        if getattr(self.calendar,'native',False) and action=='create':
            calendar_id=self._native_create_calendar(calendar_id)
        if action not in {'create', 'update', 'delete'}:
            raise ActionError('CALENDAR_EVENT_INVALID')
        etag = None
        if action != 'create':
            event = self.calendar.request('GET', self.calendar.event_path(calendar_id, event_id))
            etag = event.get('etag')
            if not etag:
                raise ActionError('CALENDAR_EVENT_CHANGED')
            if 'dateTime' not in event.get('start', {}):
                # All-day event deletion is supported; editing keeps explicit dates.
                base = {'summary': event.get('summary', ''), 'start': event.get('start', {}).get('date', ''),
                        'end': event.get('end', {}).get('date', ''), 'timeZone': zone, 'all_day': True}
            else:
                base = {'summary': event.get('summary', ''), 'start': event['start']['dateTime'],
                        'end': event.get('end', {}).get('dateTime', ''), 'timeZone': zone}
            base.update({key:event[key] for key in ('location','description','url') if key in event})
            changes = payload or {}
            if action == 'update' and changes.get('start') and 'end' not in changes and not base.get('all_day'):
                try:
                    start = datetime.fromisoformat(changes['start'].replace('Z', '+00:00'))
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone(zone))
                    duration = datetime.fromisoformat(base['end'].replace('Z', '+00:00')) - datetime.fromisoformat(base['start'].replace('Z', '+00:00'))
                    changes = {**changes, 'end': (start + duration).isoformat()}
                except (ValueError, KeyError, AttributeError):
                    raise ActionError('CALENDAR_TIME_REQUIRED')
            payload = {**base, **changes}
        payload = payload or {'summary': '', 'start': '', 'end': '', 'timeZone': zone}
        ident = ident or uuid.uuid4().hex
        with self.db.connect() as con:
            con.execute('INSERT OR IGNORE INTO assistant_calendar_actions '
                        '(id,conversation_id,action,calendar_id,event_id,etag,payload_json,status,revision,expires_at,result_json,provider) '
                        'VALUES(?,?,?,?,?,?,?,\'pending\',?,?,NULL,?)',
                        (ident, conversation_id, action, calendar_id, event_id, etag, json.dumps(payload),
                         uuid.uuid4().hex, time.time() + 1800, getattr(self.calendar, 'provider', 'google')))
        return self.calendar_action(ident, conversation_id)

    def calendar_action(self, ident, conversation_id):
        with self.db.connect() as con:
            row = con.execute('SELECT * FROM assistant_calendar_actions WHERE id=? AND conversation_id=?', (ident, conversation_id)).fetchone()
        if not row:
            raise ActionError('CALENDAR_ACTION_NOT_FOUND')
        item = dict(row)
        item['payload'] = json.loads(item.pop('payload_json'))
        item['result'] = json.loads(item.pop('result_json') or '{}')
        return item

    @staticmethod
    def validated_event(payload):
        title = str(payload.get('summary', '')).strip()
        zone = payload.get('timeZone', 'UTC')
        tz = timezone(zone)
        if not title or len(title) > 200:
            raise ActionError('CALENDAR_TIME_REQUIRED')
        try:
            if payload.get('all_day'):
                start, end = datetime.fromisoformat(payload['start']), datetime.fromisoformat(payload['end'])
                if len(payload['start']) != 10 or len(payload['end']) != 10:
                    raise ValueError()
                date_key = 'date'
            else:
                start, end = (datetime.fromisoformat(payload[k].replace('Z', '+00:00')) for k in ('start', 'end'))
                original = (start, end)
                start, end = (dt.replace(tzinfo=tz) if dt.tzinfo is None else dt for dt in original)
                # Reject nonexistent wall times at DST changes instead of shifting them silently.
                for raw, dt in zip(original, (start, end)):
                    if raw.tzinfo is None and dt.astimezone(ZoneInfo('UTC')).astimezone(tz).replace(tzinfo=None) != raw:
                        raise ValueError()
                date_key = 'dateTime'
            if end <= start or (end - start).total_seconds() > 366 * 86400:
                raise ValueError()
        except (ValueError, KeyError, AttributeError, TypeError):
            raise ActionError('CALENDAR_TIME_REQUIRED')
        extras={}
        from urllib.parse import urlsplit
        for key,limit in [('location',2000),('description',10000),('url',4096)]:
            if key not in payload:continue
            value=payload[key]
            if not isinstance(value,str) or len(value)>limit:raise ActionError('CALENDAR_EVENT_INVALID')
            if key=='url' and value and (urlsplit(value).scheme not in {'https','http'} or not urlsplit(value).hostname):raise ActionError('CALENDAR_EVENT_INVALID')
            extras[key]=value
        return {**extras,'summary': title, 'start': {date_key: start.date().isoformat() if date_key == 'date' else start.isoformat(), **({'timeZone': zone} if date_key == 'dateTime' else {})},
                'end': {date_key: end.date().isoformat() if date_key == 'date' else end.isoformat(), **({'timeZone': zone} if date_key == 'dateTime' else {})}}

    def confirm_calendar(self, ident, conversation_id, revision, payload=None):
        self.calendar_network()
        with self.calendar_lock:
            item = self.calendar_action(ident, conversation_id)
            if item.get('provider', 'google') != getattr(self.calendar, 'provider', 'google'):
                raise ActionError('CALENDAR_PROVIDER_CHANGED')
            if item['revision'] != revision:
                raise ActionError('CALENDAR_REVISION_INVALID')
            if item['status'] == 'completed':
                return item
            if item['status'] != 'pending':
                raise ActionError('CALENDAR_ACTION_BUSY')
            if item['expires_at'] < time.time():
                raise ActionError('CALENDAR_ACTION_EXPIRED')
            data = self.validated_event(payload or item['payload']) if item['action'] != 'delete' else None
            if getattr(self.calendar,'native',False):
                if not self.calendar.status()['connected']:
                    raise ActionError('CALENDAR_NOT_CONNECTED')
                if item['action']=='create':
                    item['calendar_id']=self._native_create_calendar(item['calendar_id'])
                    with self.db.connect() as con:con.execute('UPDATE assistant_calendar_actions SET calendar_id=? WHERE id=?',(item['calendar_id'],ident))
            with self.db.connect() as con:
                con.execute("UPDATE assistant_calendar_actions SET status='executing',payload_json=? WHERE id=?", (json.dumps(payload or item['payload']), ident))
            event_id = item['event_id'] or 'olcr' + ident
            path = self.calendar.event_path(item['calendar_id'], event_id if item['action'] != 'create' else '')
            try:
                if item['action'] == 'create':
                    result = self.calendar.request('POST', path, payload={**data, 'id': event_id})
                elif item['action'] == 'update':
                    result = self.calendar.request('PATCH', path, payload=data, etag=item['etag'])
                else:
                    result = self.calendar.request('DELETE', path, etag=item['etag'])
            except ActionError as exc:
                status = 'uncertain' if str(exc) == 'CALENDAR_NETWORK_FAILED' else 'failed'
                with self.db.connect() as con:
                    con.execute('UPDATE assistant_calendar_actions SET status=?,result_json=? WHERE id=?', (status, json.dumps({'error': str(exc)}), ident))
                raise
            with self.db.connect() as con:
                con.execute("UPDATE assistant_calendar_actions SET status='completed',result_json=? WHERE id=?", (json.dumps(self.calendar.public_event(result)), ident))
            return self.calendar_action(ident, conversation_id)

    def cancel_calendar(self, ident, conversation_id, revision):
        with self.calendar_lock:
            item = self.calendar_action(ident, conversation_id)
            if item['revision'] != revision:
                raise ActionError('CALENDAR_REVISION_INVALID')
            if item['status'] not in {'pending', 'cancelled'}:
                raise ActionError('CALENDAR_ACTION_BUSY')
            with self.db.connect() as con:
                con.execute("UPDATE assistant_calendar_actions SET status='cancelled' WHERE id=?", (ident,))
            return self.calendar_action(ident, conversation_id)

    def handle(self, intent, conversation_id, source_id, language='ja'):
        ident = uuid.uuid5(uuid.NAMESPACE_URL, conversation_id + ':' + source_id).hex
        request_hash = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        with self.turn_lock(conversation_id):
            with self.db.connect() as con:
                prior = con.execute('SELECT request_hash,result_json FROM assistant_requests WHERE id=?', (ident,)).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise ActionError('REQUEST_ID_REUSED')
                return json.loads(prior[1])
            try:
                result = self._handle(intent, conversation_id, ident, language)
            except ActionError as exc:
                code = str(exc)
                setup = code in {'EXTERNAL_ACCESS_DISABLED', 'CALENDAR_NOT_CONNECTED', 'CALENDAR_CLIENT_REQUIRED', 'CALENDAR_RECONNECT_REQUIRED', 'YOUTUBE_SEARCH_NOT_CONFIGURED'}
                result = {'response': message_for(code, language), 'blocks': [{
                    'type': 'assistant_action', 'kind': 'setup', 'id': ident, 'service': intent['service'], 'error': code}] if setup else []}
            if intent.get('followup') and not result['blocks'] and intent['service'] in {'timer','alarm'}:
                context=intent['followup']
                if result['response']!=('変更を取り消しました。' if language=='ja' else 'Cancelled the change.'):
                    result['blocks']=[{'type':'assistant_action','kind':'clarification','id':ident,'followup':{'service':intent['service'],'operation':'timer_edit','target_ids':context.get('target_ids',[]),'field':'unknown','question':result['response']}}]
            with self.db.connect() as con:
                con.execute('INSERT INTO assistant_requests VALUES(?,?,?,?)', (ident, conversation_id, request_hash, json.dumps(result)))
            return result

    def _followup_question(self, base, context, question):
        return {'response':question,'blocks':[{**base,'kind':'clarification','followup':{**context,'question':question}}]}

    @staticmethod
    def _followup_index(text, count):
        if re.search(r'\bfirst\b',text,re.I):return 0
        if re.search(r'\bsecond\b',text,re.I):return 1 if count>1 else None
        match=re.search(r'[0-9一二三四五六七八九十]+',text)
        if match:
            index=int(number(match[0]))-1
            if 0<=index<count:return index
        return None

    def _clock_choices(self,text,zone,current,calendar=False):
        from .action_followups import CLOCK,DAY
        text=re.sub(r'夕方|夜','午後',text);text=re.sub(r'朝','午前',text)
        text=re.sub(r'\bat\s+(\d{1,2})(?![:\d])\b',r'\1:00',text,flags=re.I)
        if not re.search(DAY,text,re.I):text=current.strftime('%Y-%m-%d')+' '+text
        match=re.search(CLOCK,text,re.I)
        if not match:
            text+=' '+current.strftime('%H:%M')
        # A bare 1..12 o'clock correction is not silently interpreted as AM.
        match=re.search(r'(?<!\d)([0-9零〇一二三四五六七八九十]+)(?:時(?!間)|:\d{2})',text)
        ambiguous=bool(match and 1<=number(match[1])<=12 and not re.search(r'午前|午後|\bam\b|\bpm\b',text,re.I))
        texts=[(text[:match.start()]+'午前'+text[match.start():],'am'),(text[:match.start()]+'午後'+text[match.start():],'pm')] if ambiguous else [(text,None)]
        choices=[]
        for candidate,period in texts:
            try:
                if calendar:
                    start=event_proposal(candidate,zone)['start'];deadline=datetime.fromisoformat(start).timestamp()
                else:
                    deadline=alarm_deadline(candidate,zone);start=datetime.fromtimestamp(deadline,timezone(zone)).isoformat()
                choices.append({'deadline':deadline,'start':start,'period':period,'label':datetime.fromtimestamp(deadline,timezone(zone)).strftime('%Y-%m-%d %H:%M %Z')})
            except (ActionError,ValueError):pass
        return choices,ambiguous

    def _handle_followup(self,intent,conversation_id,base,ja):
        f=dict(intent['followup']);text=intent['text'];service=f['service'];operation=f['operation']
        if f.get('cancel'):
            return {'response':'変更を取り消しました。' if ja else 'Cancelled the change.','blocks':[]}
        if f.get('reply') and f.get('choices'):
            choices=f['choices'];reply=f['reply'];index=self._followup_index(reply,len(choices))
            period='pm' if re.search(r'午後|\bpm\b',reply,re.I) else 'am' if re.search(r'午前|\bam\b',reply,re.I) else None
            if period:index=next((i for i,c in enumerate(choices) if c.get('period')==period),None)
            elif index is None and len(choices)==1:index=0
            if index is None:return self._followup_question(base,f,f['question'])
            f['chosen']=choices[index]
        if operation=='media_select':
            self.network()
            choices=f.get('candidates',[]);index=self._followup_index(text,len(choices))
            if index is None:return self._followup_question(base,f,'何番目の動画にしますか？' if ja else 'Which video number would you like?')
            video=choices[index]
            return {'response':'選んだ動画を再生できます。' if ja else 'Here is the selected video.','blocks':[{**base,'kind':'media','candidates':[video],'autoplay':True}]}
        if operation=='calendar_edit':
            action=self.calendar_action(f['target_id'],conversation_id)
            if action['status']!='pending' or action['revision']!=f['revision'] or action['expires_at']<=time.time():raise ActionError('CALENDAR_ACTION_CHANGED')
            payload=dict(action['payload']);zone=payload.get('timeZone',self.preferences()['time_zone'])
            if f['field']=='unknown':return self._followup_question(base,f,'タイトル・日時・長さのどれを変更しますか？' if ja else 'Would you like to change the title, date, time, or duration?')
            if f['field']=='name':payload['summary']=f['name']
            elif f['field']=='duration':
                try:
                    start=datetime.fromisoformat(payload['start'].replace('Z','+00:00'));end=datetime.fromisoformat(payload['end'].replace('Z','+00:00'))
                except (ValueError,KeyError):raise ActionError('CALENDAR_TIME_REQUIRED')
                raw=f.get('text',text);duration=timedelta(seconds=duration_seconds(raw))
                if re.search(r'早め|早く|前倒し|earlier',raw,re.I):start-=duration;end-=duration
                elif re.search(r'遅ら|遅く|後ろ|later',raw,re.I):start+=duration;end+=duration
                elif re.search(r'追加|延長|more',raw,re.I):end+=duration
                elif re.search(r'短く|短縮|less',raw,re.I):end-=duration
                else:end=start+duration
                if end<=start or payload.get('all_day'):raise ActionError('CALENDAR_TIME_REQUIRED')
                payload.update(start=start.isoformat(),end=end.isoformat())
            else:
                try:
                    current=datetime.fromisoformat(payload['start'].replace('Z','+00:00'))
                    current=current.replace(tzinfo=timezone(zone)) if current.tzinfo is None else current.astimezone(timezone(zone))
                except (ValueError,KeyError):current=datetime.fromtimestamp(time.time(),timezone(zone))
                if f.get('chosen'):choice=f['chosen']
                else:
                    choices,ambiguous=self._clock_choices(f.get('text',text),zone,current,True)
                    if not choices:return self._followup_question(base,{**f,'choices':[]},'変更する日時を指定してください。' if ja else 'Specify the new date and time.')
                    if ambiguous:return self._followup_question(base,{**f,'choices':choices},('どちらの日時に変更しますか？ ' if ja else 'Which date and time? ')+ ' / '.join(c['label'] for c in choices))
                    choice=choices[0]
                try:delta=datetime.fromisoformat(payload['end'].replace('Z','+00:00'))-datetime.fromisoformat(payload['start'].replace('Z','+00:00'))
                except (ValueError,KeyError):delta=timedelta(hours=1)
                start=datetime.fromisoformat(choice['start'])
                if payload.get('all_day') and f['field']=='day':payload.update(start=start.date().isoformat(),end=(start+delta).date().isoformat())
                else:
                    payload.pop('all_day',None);payload.update(start=start.isoformat(),end=(start+delta).isoformat())
            revision=uuid.uuid4().hex
            with self.db.connect() as con:
                changed=con.execute("UPDATE assistant_calendar_actions SET payload_json=?,revision=?,expires_at=? WHERE id=? AND conversation_id=? AND revision=? AND status='pending'",(json.dumps(payload),revision,time.time()+1800,action['id'],conversation_id,action['revision'])).rowcount
            if not changed:raise ActionError('CALENDAR_ACTION_CHANGED')
            return {'response':'予定の確認内容を修正しました。内容を確認して登録してください。' if ja else 'Updated the event draft. Review it before applying.','blocks':[{**base,'kind':'calendar_action','action':self.calendar_action(action['id'],conversation_id)}]}
        if operation not in {'timer_edit','timer_control'}:raise ActionError('TIMER_STATE_CHANGED')
        ids=f['target_ids']
        if operation=='timer_control' and f['control']=='status':
            items=[self.timer(ident) for ident in ids]
            if any(item['conversation_id']!=conversation_id for item in items):raise ActionError('TIMER_NOT_FOUND')
            zone=intent.get('time_zone') or self.preferences()['time_zone']
            labels=[]
            for item in items:
                status=({'running':'実行中','paused':'一時停止中','cancelled':'停止済み','finished':'終了'}.get(item['status'],item['status']) if ja else item['status'])
                if item.get('kind')=='alarm' and item.get('deadline'):detail=datetime.fromtimestamp(item['deadline'],timezone(zone)).strftime('%Y-%m-%d %H:%M %Z')
                else:
                    seconds=max(0,math.ceil(item['deadline']-time.time() if item['status']=='running' and item['deadline'] else item['remaining']))
                    detail=f'残り{seconds//60}分{seconds%60}秒' if ja else f'{seconds//60}m {seconds%60}s remaining'
                labels.append(f"{item['name']}: {detail} ({status})")
            return {'response':' / '.join(labels),'blocks':[{**base,'kind':'timers','timers':items}]}
        if len(ids)>1:
            index=self._followup_index(f.get('reply') or text,len(ids)) if f.get('reply') or f.get('field')=='selection' else None
            if index is None:
                labels=[f"{i+1}. {self.timer(ident)['name']}" for i,ident in enumerate(ids)]
                return self._followup_question(base,{**f,'await_target':True},('変更する対象を番号で指定してください。 ' if ja else 'Choose the item number to change. ')+ ' / '.join(labels))
            ids=[ids[index]];f.update(target_ids=ids,reply=None)
        item=self.timer(ids[0])
        if item['conversation_id']!=conversation_id or item.get('kind','timer')!=service:raise ActionError('TIMER_NOT_FOUND')
        if f.get('expected') and f['expected']!={k:item[k] for k in ('status','deadline')} :raise ActionError('TIMER_STATE_CHANGED')
        if operation=='timer_control':
            updated=self.update_timer(item['id'],f['control'])
            return {'response':'操作を反映しました。' if ja else 'Updated the selected item.','blocks':[{**base,'kind':'timers','timers':[updated]}]}
        if f['field']=='name':
            with self.db.connect() as con:con.execute('UPDATE assistant_timers SET name=?,updated_at=? WHERE id=?',(f['name'][:80],time.time(),item['id']))
            return {'response':'名前を変更しました。' if ja else 'Renamed it.','blocks':[{**base,'kind':'timers','timers':[self.timer(item['id'])]}]}
        zone=intent.get('time_zone') or self.preferences()['time_zone'];raw=f.get('text',text);now=time.time()
        active=item['status'] in {'running','paused'}
        if f.get('chosen'):choice=f['chosen']
        elif service=='alarm' and f['field'] in {'clock','day'}:
            current=datetime.fromtimestamp(item['deadline'] or now,timezone(zone))
            choices,ambiguous=self._clock_choices(raw,zone,current)
            if not choices:return self._followup_question(base,{**f,'choices':[]},'その日時は過ぎているか、時刻を確定できません。未来の日時を指定してください。' if ja else 'That time is past or unclear. Specify a future date and time.')
            choice=choices[0]
            if ambiguous:
                prefix=('停止・終了済みのアラームを設定し直します。' if not active else '') if ja else ('This alarm is stopped or finished; this will restart it. ' if not active else '')
                question=(prefix+choices[0]['label']+(' に変更しますか？' if ja else ': confirm this time?')) if len(choices)==1 else prefix+('午前・午後のどちらにしますか？ ' if ja else 'AM or PM? ')+ ' / '.join(c['label'] for c in choices)
                return self._followup_question(base,{**f,'choices':choices,'expected':{k:item[k] for k in ('status','deadline')},'allow_restart':not active},question)
        elif f['field']=='duration':
            seconds=duration_seconds(raw)
            delta=re.search(r'追加|延長|後ろ|遅ら|遅く|more|later',raw,re.I);subtract=re.search(r'短く|短縮|前倒し|早め|早く|less|earlier',raw,re.I)
            if delta or subtract:
                remaining=max(0,item['deadline']-now) if item['deadline'] else item['remaining']
                seconds=remaining+(-seconds if subtract else seconds)
            choice={'deadline':now+seconds}
        else:
            return self._followup_question(base,{**f,'choices':[]},'変更する時刻・時間・名前を指定してください。' if ja else 'Specify the new time, duration, or name.')
        deadline=choice['deadline'];seconds=deadline-now
        if not math.isfinite(seconds) or not 1<=seconds<=604800:raise ActionError('TIMER_DURATION_INVALID')
        if not active and not f.get('allow_restart'):
            label=datetime.fromtimestamp(deadline,timezone(zone)).strftime('%Y-%m-%d %H:%M %Z')
            return self._followup_question(base,{**f,'choices':[{**choice,'label':label}],'expected':{k:item[k] for k in ('status','deadline')},'allow_restart':True},(f'停止・終了済みです。{label} に設定し直しますか？' if ja else f'It is stopped or finished. Restart it for {label}?'))
        status='paused' if service=='timer' and item['status']=='paused' else 'running'
        with self.lock,self.db.connect() as con:
            changed=con.execute("UPDATE assistant_timers SET duration=?,deadline=?,remaining=?,status=?,notification_status='pending',updated_at=? WHERE id=? AND conversation_id=? AND status=? AND deadline IS ?",(seconds,None if status=='paused' else deadline,seconds,status,now,item['id'],conversation_id,item['status'],item['deadline'])).rowcount
        if not changed:raise ActionError('TIMER_STATE_CHANGED')
        updated=self.timer(item['id']);clock=datetime.fromtimestamp(deadline,timezone(zone)).strftime('%Y-%m-%d %H:%M %Z')
        return {'response':(f'アラームを {clock} に変更しました。' if service=='alarm' else f'タイマーを残り {round(seconds)} 秒に変更しました。') if ja else (f'Alarm changed to {clock}.' if service=='alarm' else f'Timer changed to {round(seconds)} seconds remaining.'),'blocks':[{**base,'kind':'timers','timers':[updated]}]}

    def _handle(self, intent, conversation_id, ident, language):
        service, text = intent['service'], intent['text']
        ja = language == 'ja'
        base = {'type': 'assistant_action', 'id': ident, 'conversation_id': conversation_id, 'created_at': time.time()}
        if intent.get('followup'):return self._handle_followup(intent,conversation_id,base,ja)
        if service == 'clarify':
            return {'response': 'タイマー・アラーム・予定・音楽は、一つずつ指示してください。' if ja else 'Ask for one timer, alarm, calendar, or music action at a time.', 'blocks': []}
        if service in {'timer','alarm'}:
            if re.search(r'変更|訂正|にして|ずら|延長|短縮|\b(?:change|modify)\b',text,re.I):
                from .action_followups import fields
                change=fields(text) or {'field':'unknown','text':text}
                items=[item for item in self.timers(active=True) if item.get('kind','timer')==service and item['conversation_id']==conversation_id]
                if not items:return {'response':'変更する対象がありません。新しく設定する場合は、時刻または時間を指定して設定を依頼してください。' if ja else 'There is no active item to change. Ask to set a new time or duration.','blocks':[]}
                return self._handle_followup({'service':service,'text':text,'followup':{'service':service,'operation':'timer_edit','target_ids':[item['id'] for item in items],**change}},conversation_id,base,ja)
            verb = 'cancel' if re.search(r'止め|停止|キャンセル|削除|消し|\b(?:cancel|stop|delete)\b', text, re.I) else 'pause' if re.search(r'一時停止|\bpause\b', text, re.I) else 'resume' if re.search(r'再開|\bresume\b', text, re.I) else 'list' if re.search(r'残り|一覧|見せ|教え|表示|確認|\b(?:list|show|status|remaining)\b', text, re.I) else 'start'
            # Pause is more specific than the generic Japanese stop token.
            if re.search(r'一時停止|\bpause\b', text, re.I):
                verb = 'pause'
            if verb == 'start':
                deadline=alarm_deadline(text,intent.get('time_zone') or self.preferences()['time_zone']) if service=='alarm' else None
                seconds=deadline-time.time() if deadline else duration_seconds(text)
                label = re.search(r'[「"『](.+?)[」"』]', text)
                timer = self.start_timer(seconds, label[1] if label else (('アラーム' if ja else 'Alarm') if service=='alarm' else ('タイマー' if ja else 'Timer')), conversation_id, ident,deadline=deadline,kind=service)
                response = f'{seconds}秒のタイマーを開始しました。' if ja else f'Started a timer for {seconds} seconds.'
                if service=='alarm':
                    clock=datetime.fromtimestamp(deadline,timezone(intent.get('time_zone') or self.preferences()['time_zone'])).strftime('%Y-%m-%d %H:%M %Z')
                    response=f'{clock} にアラームを設定しました。' if ja else f'Alarm set for {clock}.'
                items = [timer]
            else:
                items = [item for item in self.timers(active=True) if item.get('kind','timer')==service]
                if verb != 'list':
                    named = [t for t in items if t['id'] in text or (t['name'] not in {'タイマー', 'Timer', 'アラーム', 'Alarm'} and t['name'] in text)]
                    if not named:
                        try:
                            seconds = duration_seconds(text)
                            named = [t for t in items if t['duration'] == seconds]
                        except ActionError:
                            pass
                    eligible = [t for t in items if verb == 'cancel' or (verb == 'pause' and t['status'] == 'running') or (verb == 'resume' and t['status'] == 'paused')]
                    targets = named or eligible
                    if len(targets) != 1 and not re.search(r'全部|すべて|全て|\ball\b', text, re.I):
                        label=('アラーム' if service=='alarm' else 'タイマー') if ja else ('alarm' if service=='alarm' else 'timer')
                        return {'response': (f'操作する{label}を選んでください。' if items else f'実行中の{label}はありません。') if ja else (f'Choose an {label} to control.' if service=='alarm' and items else f'Choose a {label} to control.' if items else f'No {label}s are active.'),
                                'blocks': [{**base, 'kind': 'timers', 'timers': items}]}
                    items = [self.update_timer(t['id'], verb) for t in targets]
                label=('アラーム' if service=='alarm' else 'タイマー') if ja else ('alarms' if service=='alarm' else 'timers')
                response=(f'{label}の状態です。' if items else f'実行中の{label}はありません。') if ja else (f'Here are your {label}.' if items else f'No {label} are active.')
            return {'response': response, 'blocks': [{**base, 'kind': 'timers', 'timers': items}]}
        if service == 'calendar':
            if intent.get('cancel'):
                action = self.cancel_calendar(intent['cancel'], conversation_id, intent['revision'])
                return {'response': '予定の操作を取り消しました。' if ja else 'Cancelled the calendar change.',
                        'blocks': [{**base, 'kind': 'calendar_action', 'action': action}]}
            if re.search(r'接続|設定|\b(?:connect|settings)\b', text, re.I):
                return {'response': '設定の「カレンダーに接続」から、このMacのカレンダーを使えます。' if ja else 'Choose Connect Calendar in Settings to use this Mac’s calendars.',
                        'blocks': [{**base, 'kind': 'setup', 'service': 'calendar'}]}
            self.calendar_network()
            if intent.get('confirm'):
                action = self.confirm_calendar(intent['confirm'], conversation_id, intent['revision'])
                response = ('予定を削除しました。' if action['action'] == 'delete' else '予定を変更しました。' if action['action'] == 'update' else '予定を追加しました。') if ja else ('Event deleted.' if action['action'] == 'delete' else 'Event updated.' if action['action'] == 'update' else 'Event added.')
                return {'response': response, 'blocks': [{**base, 'kind': 'calendar_action', 'action': action}]}
            creating=bool(re.search(r'追加|登録|入れ|作成|書き込|書込|\b(?:add|create|schedule)\b',text,re.I))
            if not self.calendar.status()['connected'] and not (creating and getattr(self.calendar,'native',False)):
                raise ActionError('CALENDAR_NOT_CONNECTED')
            prefs = self.preferences()
            if intent.get('time_zone'): prefs={**prefs,'time_zone':intent['time_zone']}
            if re.search(r'追加|登録|入れ|作成|書き込|書込|\b(?:add|create|schedule)\b', text, re.I):
                proposal = event_proposal(text, prefs['time_zone'])
                if proposal.get('unsupported_fields'):
                    return {'response':('招待者の追加・ファイルのネイティブ添付は、このカレンダー連携では未対応です。カレンダーアプリで設定してください。' if ja else 'Invitations and native file attachments are not supported by this calendar connection. Set them in Calendar.'),'blocks':[]}
                calendar_name=proposal.pop('calendar_name',None)
                item = self.prepare_calendar(conversation_id, payload=proposal, ident=ident,calendar_id=calendar_name)
                if not proposal.get('start'):
                    return self._followup_question(base,{'service':'calendar','operation':'calendar_edit','target_id':item['id'],'revision':item['revision'],'field':'unknown'},'予定の開始日時を教えてください。例：明日朝8時。' if ja else 'When should the event start? For example, tomorrow at 8 AM.')
                return {'response': '予定の内容を確認して登録してください。' if ja else 'Review the event before adding it.',
                        'blocks': [{**base, 'kind': 'calendar_action', 'action': item}]}
            start, end = calendar_window(text, prefs['time_zone'])
            data = self.calendar.events(prefs['calendar_id'], start, end)
            operation = 'delete' if re.search(r'削除|消し|キャンセル|\b(?:delete|remove|cancel)\b', text, re.I) else 'update' if re.search(r'変更|ずら|\b(?:change|move|update)\b', text, re.I) else None
            proposal = event_proposal(text, prefs['time_zone']) if operation == 'update' else {}
            if operation:
                target = event_target(text)
                if target:
                    data['events'] = [e for e in data['events'] if target.casefold() in (e.get('summary') or '').casefold()]
                if len(data['events']) == 1:
                    changes = {'start': proposal['start']} if proposal.get('start') else None
                    item = self.prepare_calendar(conversation_id, operation, data['events'][0]['id'], changes, ident)
                    return {'response': '予定の内容を確認してください。' if ja else 'Review the calendar change.',
                            'blocks': [{**base, 'kind': 'calendar_action', 'action': item}]}
            return {'response': ('操作する予定を選んでください。' if operation else 'カレンダーの予定です。') if ja else ('Choose an event to change.' if operation else 'Here are your calendar events.'),
                    'blocks': [{**base, 'kind': 'calendar_events', **data, 'operation': operation, 'proposed_start': proposal.get('start'), 'calendar_id': prefs['calendar_id'], 'time_zone': prefs['time_zone']}]}
        self.network()
        searching = bool(re.search(r'検索|探して|探し|見つけ|おすすめ|お勧め|オススメ|出して|\b(?:search|find|recommend)\b|look\s+for', text, re.I))
        control = None if searching else 'pause' if re.search(r'一時停止|\bpause\b', text, re.I) else 'stop' if re.search(r'停止|止め|\bstop\b', text, re.I) else 'play' if re.search(r'再開|\bresume\b', text, re.I) else 'status' if re.search(r'今の曲|what.*playing|再生状態', text, re.I) else None
        if control:
            return {'response': 'プレイヤーに操作を送ります。' if ja else 'Sending the request to the player.',
                    'blocks': [{**base, 'kind': 'media_control', 'command': control}]}
        query,order = youtube_request(text)
        autoplay = (not searching or bool(re.search(r'再生して|かけて|流して|\bplay\b', text, re.I))) and not bool(re.search(r'再生しない|再生せず|(?:do not|don.t)\s+play', text, re.I))
        try:
            choices = search_youtube(query, self.youtube_key, **({'order':'date'} if order=='date' else {}))
        except ActionError as exc:
            if str(exc) not in {'YOUTUBE_SEARCH_FAILED', 'YOUTUBE_NO_RESULTS'}:
                raise
            return {'response': message_for(str(exc), language),
                    'blocks': [{**base, 'kind': 'setup', 'service': 'youtube',
                                'search_url': 'https://www.youtube.com/results?' + urlencode({'search_query': query})}]}
        return {'response': ('YouTubeで動画を検索しました。候補から選んで再生できます。' if searching else 'YouTubeの再生候補です。プレイヤーで再生できます。') if ja else 'Here are YouTube search results. Choose a video to play.',
                'blocks': [{**base, 'kind': 'media', 'query': query, 'candidates': choices, 'autoplay': autoplay}]}

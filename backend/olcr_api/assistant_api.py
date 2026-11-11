"""Authenticated HTTP controls used by existing Settings and chat blocks."""
from typing import Literal
import time
import uuid

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .assistant_actions import ActionError, message_for
from .response_language import response_language
from .permissions import capability_enabled


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Preferences(Input):
    calendar_id: str = Field(default='primary', min_length=1, max_length=512)
    time_zone: str = Field(min_length=1, max_length=100)


class ClientConfiguration(Input):
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str = Field(default='', max_length=1000)


class CredentialAcknowledgement(Input):
    generation: int = Field(ge=0)


class YouTubeKey(Input):
    key: str = Field(min_length=1, max_length=1000)


class TimerOperation(Input):
    action: Literal['pause', 'resume', 'cancel']


class NotificationResult(Input):
    ids: list[str] = Field(max_length=100)
    status: Literal['scheduled', 'denied', 'unavailable', 'failed']


class CalendarPreparation(Input):
    conversation_id: str = Field(min_length=1, max_length=128)
    action: Literal['create', 'update', 'delete'] = 'create'
    event_id: str = Field(default='', max_length=1024)
    calendar_id: str | None = Field(default=None, max_length=512)
    payload: dict | None = None


class CalendarConfirmation(Input):
    conversation_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=128)
    payload: dict | None = None


class PlayerInput(Input):
    video_id: str = Field(pattern=r'^[A-Za-z0-9_-]{11}$')


def assistant_router(get_service):
    router = APIRouter()

    def record_action(item, completed=False):
        service = get_service()
        ja = response_language() == 'ja'
        text = ('予定を削除しました。' if item['action'] == 'delete' else '予定を変更しました。' if item['action'] == 'update' else '予定を追加しました。') if completed and ja else ('Event deleted.' if item['action'] == 'delete' else 'Event updated.' if item['action'] == 'update' else 'Event added.') if completed else ('予定の内容を確認してください。' if ja else 'Review the calendar change.')
        ident = uuid.uuid5(uuid.NAMESPACE_URL, item['id'] + (':completed' if completed else ':preview')).hex
        block = {'type': 'assistant_action', 'kind': 'calendar_action', 'id': item['id'],
                 'conversation_id': item['conversation_id'], 'created_at': time.time(), 'action': item}
        with service.lock, service.db.connect() as con:
            exists = con.execute('SELECT 1 FROM messages WHERE id=?', (ident,)).fetchone()
            if not exists:
                service.db.add_message(item['conversation_id'], 'assistant', text, time.time(), ident, blocks=[block])
        return {'action': item, 'message': {'id': ident, 'role': 'assistant', 'content': text, 'blocks': [block]}}

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ActionError as exc:
            raise HTTPException(422, {'code': str(exc), 'message': message_for(str(exc), response_language())}) from None

    @router.get('/api/assistant/settings')
    def settings():
        service = get_service()
        with service.db.connect() as con:
            configured = bool(con.execute('SELECT 1 FROM assistant_preferences WHERE id=1').fetchone())
        return {'calendar': service.calendar.status(), 'preferences': service.preferences(),
                'external_access_enabled': capability_enabled(service.settings(), 'external_access_enabled'),
                'time_zone_configured': configured,
                'youtube_search_configured': True,
                'youtube_key_configured': bool(service.youtube_key)}

    @router.get('/api/assistant/calendar/icon')
    def calendar_icon(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return invoke(get_service().calendar.icon)

    @router.put('/api/assistant/preferences')
    def preferences(value: Preferences):
        return invoke(get_service().save_preferences, value.calendar_id, value.time_zone)

    @router.put('/api/assistant/calendar/client')
    def client(value: ClientConfiguration):
        return invoke(get_service().calendar.configure, value.client_id, value.client_secret)

    @router.post('/api/assistant/calendar/connect')
    def connect():
        service = get_service()
        invoke(service.calendar_network)
        return invoke(service.calendar.start, response_language())

    @router.post('/api/assistant/calendar/disconnect')
    def disconnect():
        return get_service().calendar.disconnect()

    @router.post('/api/assistant/calendar/credential/export')
    def export():
        # Protected by the same GUI-session guard as every other /api route.
        # The desktop immediately commits this secret to its allowlisted Keychain bundle.
        return get_service().calendar.export()

    @router.post('/api/assistant/calendar/credential/ack')
    def acknowledge(value: CredentialAcknowledgement):
        return get_service().calendar.acknowledge(value.generation)

    @router.get('/api/assistant/calendar/calendars')
    def calendars():
        service = get_service()
        invoke(service.calendar_network)
        return {'calendars': invoke(service.calendar.calendars)}

    @router.put('/api/assistant/youtube/key')
    def youtube_key(value: YouTubeKey):
        get_service().youtube_key = value.key.strip()
        return {'configured': True}

    @router.get('/api/assistant/timers')
    def timers(active: bool = False):
        return {'timers': get_service().timers(active)}

    @router.get('/api/assistant/timers/{ident}')
    def timer(ident: str):
        return invoke(get_service().timer, ident)

    @router.post('/api/assistant/timers/{ident}/control')
    def timer_control(ident: str, value: TimerOperation):
        return invoke(get_service().update_timer, ident, value.action)

    @router.post('/api/assistant/notifications')
    def notifications(value: NotificationResult):
        get_service().notification_result(value.ids, value.status)
        return {'ok': True}

    @router.post('/api/assistant/calendar/actions')
    def prepare(value: CalendarPreparation):
        service = get_service()
        if not service.db.conversation(value.conversation_id):
            raise HTTPException(404, 'Conversation not found')
        return record_action(invoke(service.prepare_calendar, **value.model_dump()))

    @router.get('/api/assistant/calendar/actions/{ident}')
    def calendar_action(ident: str, conversation_id: str):
        return invoke(get_service().calendar_action, ident, conversation_id)

    @router.post('/api/assistant/calendar/actions/{ident}/confirm')
    def confirm(ident: str, value: CalendarConfirmation):
        return record_action(invoke(get_service().confirm_calendar, ident, **value.model_dump()), completed=True)

    @router.post('/api/assistant/calendar/actions/{ident}/cancel')
    def cancel(ident: str, value: CalendarConfirmation):
        return invoke(get_service().cancel_calendar, ident, value.conversation_id, value.revision)

    @router.post('/api/assistant/media/player')
    def player(value: PlayerInput):
        service = get_service()
        invoke(service.network)
        ticket = invoke(service.players.issue, value.video_id)
        return {'ticket': ticket, 'path': '/assistant-player/' + ticket}

    @router.get('/assistant-player/{ticket}', response_class=HTMLResponse)
    def player_page(ticket: str):
        if len(ticket) > 64:
            raise HTTPException(404)
        service = get_service()
        try:
            service.network()
            content, policy = service.players.render(ticket)
        except ActionError:
            raise HTTPException(404) from None
        return HTMLResponse(content, headers={'Content-Security-Policy': policy,
            'Cache-Control': 'no-store', 'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Permissions-Policy': 'autoplay=(self "https://www.youtube.com" "https://www.youtube-nocookie.com"), fullscreen=(self "https://www.youtube.com" "https://www.youtube-nocookie.com")'})

    return router

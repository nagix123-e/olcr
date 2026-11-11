"""Authenticated personal-task routes; guarded by the app's /api session middleware."""
from fastapi import HTTPException
from pydantic import Field
from typing import Literal
from .personal_tasks import PersonalTasks, ProfilePreferences, TaskDefinition, GoalDefinition, Input


class TaskControl(Input):
    action: Literal['pause', 'resume', 'stop']


class CalendarPreview(Input):
    proposal_index: int = Field(ge=0, le=29)
    conversation_id: str = Field(min_length=1, max_length=128)


def register_personal_task_routes(app, host_callable):
    context = host_callable()
    owner = context.get('personal_owner', 'local-user')
    if callable(owner): owner = owner()
    service = PersonalTasks(context['db'],host_callable,owner=owner)

    def invoke(method, *args):
        try: return method(*args)
        except ValueError as exc:
            code = str(exc)
            raise HTTPException(404 if code.endswith('_NOT_FOUND') else 422, code) from None

    @app.get('/api/personal/profile')
    def profile(): return service.profile()

    @app.put('/api/personal/profile')
    def update_profile(value: ProfilePreferences): return service.save_profile(value)

    @app.get('/api/personal/goals')
    def goals(): return {'goals':service.goals()}

    @app.post('/api/personal/goals')
    def create_goal(value: GoalDefinition): return invoke(service.save_goal,value)

    @app.put('/api/personal/goals/{ident}')
    def update_goal(ident: str,value: GoalDefinition): return invoke(service.save_goal,value,ident)

    @app.get('/api/personal/tasks')
    def tasks(): return {'tasks':service.tasks()}

    @app.post('/api/personal/tasks')
    def create_task(value: TaskDefinition): return invoke(service.save_task,value)

    @app.put('/api/personal/tasks/{ident}')
    def update_task(ident: str,value: TaskDefinition): return invoke(service.save_task,value,ident)

    @app.post('/api/personal/tasks/{ident}/control')
    def control(ident: str,value: TaskControl): return invoke(service.control,ident,value.action)

    @app.get('/api/personal/runs')
    def runs(task_id: str | None=None): return {'runs':invoke(service.runs,task_id)}

    @app.get('/api/personal/inbox')
    def inbox(): return {'notifications':service.inbox()}

    @app.post('/api/personal/inbox/{ident}/read')
    def read(ident: str): return invoke(service.mark_read,ident)

    @app.post('/api/personal/runs/{ident}/calendar-preview')
    def preview(ident: str,value: CalendarPreview): return {'action':invoke(service.preview_calendar,ident,value.proposal_index,value.conversation_id)}

    return service

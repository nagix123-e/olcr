"""Session-authenticated connector controls, distinct from internal APIs."""
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from .service_connectors import ConnectorError
from .permissions import capability_enabled

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Policy(Input):
    enabled:StrictBool|None=None
    read:StrictBool|None=None
    write:StrictBool|None=None
    purchase:StrictBool|None=None

class SignIn(Input):
    client_id:str=Field(default='',max_length=300)
    client_secret:str=Field(default='',max_length=1000)

class CategoryPolicy(Input):
    enabled:StrictBool

class Credential(Input):
    bundle:str=Field(min_length=1,max_length=16384)

class Action(Input):
    action:str=Field(max_length=50)
    arguments:dict=Field(default_factory=dict)

class Confirmation(Input):
    approve:StrictBool
    order:dict

def connector_router(service):
    router=APIRouter(prefix='/api/connectors')
    def invoke(call):
        try:return call()
        except ValueError as exc:
            code=str(exc)
            # Never forward provider response bodies or submitted credential values.
            safe=code if code.replace('_','').isalnum() and code.upper()==code else 'CONNECTOR_REQUEST_FAILED'
            raise HTTPException(403 if 'PERMISSION' in safe or safe.endswith('_DENIED') or safe=='EXTERNAL_ACCESS_REQUIRED' else 422,safe) from None
    @router.get('')
    def inventory():
        current=service()
        return {'connectors':current.inventory(),'categories':current.categories(),
                'google_oauth_client_id':current.google.oauth_client_id(),
                'external_access_enabled':capability_enabled(current.settings(),'external_access_enabled')}
    @router.get('/service.apple.mail/icon')
    def apple_mail_icon(response:Response):
        response.headers['Cache-Control']='no-store'
        return invoke(service().apple_mail.icon)
    @router.patch('/categories/{category}')
    def category_policy(category:str,value:CategoryPolicy):return invoke(lambda:service().set_category(category,value.enabled))
    @router.patch('/{cid}/policy')
    def policy(cid:str,value:Policy):return invoke(lambda:service().policy(cid,value.model_dump(exclude_none=True)))
    @router.post('/{cid}/connect')
    def connect(cid:str):return invoke(lambda:service().connect(cid))
    @router.post('/{cid}/oauth/start')
    def start(cid:str,value:SignIn):
        if not capability_enabled(service().settings(),'external_access_enabled'):raise HTTPException(403,'EXTERNAL_ACCESS_REQUIRED')
        return invoke(lambda:service().google.start(cid,value.client_id,value.client_secret))
    @router.get('/oauth/{attempt}')
    def poll(attempt:str,response:Response):
        response.headers['Cache-Control']='no-store'
        return invoke(lambda:service().google.poll(attempt))
    @router.delete('/oauth/{attempt}')
    def cancel(attempt:str):
        service().google.finish(attempt)
        return {'cancelled':True}
    @router.post('/{cid}/credential')
    def credential(cid:str,value:Credential):return invoke(lambda:service().credential(cid,value.bundle))
    @router.get('/{cid}/credential-sync')
    def pending_credential(cid:str,response:Response):
        response.headers['Cache-Control']='no-store'
        return invoke(lambda:{'bundle':service().google.pending_credential(cid)})
    @router.post('/{cid}/credential-sync')
    def acknowledge_credential(cid:str,value:Credential):
        invoke(lambda:service().google.acknowledge_credential(cid,value.bundle))
        return {'saved':True}
    @router.delete('/{cid}/connection')
    def disconnect(cid:str):return invoke(lambda:service().disconnect(cid))
    @router.post('/{cid}/execute')
    def execute(cid:str,value:Action):return invoke(lambda:service().execute(cid,value.action,value.arguments))
    @router.post('/purchases/review')
    def review(value:dict):return invoke(lambda:service().prepare_purchase(value))
    @router.post('/purchases/{rid}/confirm')
    def confirm(rid:str,value:Confirmation):return invoke(lambda:service().confirm_purchase(rid,value.approve,value.order))
    return router

"""User-driven marketplace sign-in in OLCR-owned persistent browser profiles."""
from fastapi import HTTPException


def register_consumer_session_routes(app, host):
    def authorize(provider):
        from .connector_marketplaces import PROVIDERS
        if 'service.commerce.'+provider not in PROVIDERS: raise HTTPException(404,'MARKETPLACE_NOT_FOUND')
        context=host();service=context['service_connectors']()
        try: service.authorize('service.commerce.'+provider,'read')
        except ValueError: raise HTTPException(403,'MARKETPLACE_CONNECTION_DISABLED') from None
        return context, PROVIDERS['service.commerce.'+provider]

    @app.get('/api/commerce-sessions')
    def inventory():
        context=host();profiles=context['browser_sessions'].profiles()
        from .connector_marketplaces import PROVIDERS
        available={p['profile_id']:p for p in profiles}
        return {'sessions':[{'provider':key.removeprefix('service.commerce.'),'profile_saved':'market-'+key.removeprefix('service.commerce.') in available,
                 'in_use':available.get('market-'+key.removeprefix('service.commerce.'),{}).get('in_use',False),
                 'account_status':'sign_in_unverified'} for key in PROVIDERS]}

    @app.post('/api/commerce-sessions/{provider}/open')
    def open_session(provider:str):
        context,definition=authorize(provider)
        browsers=context['browser_sessions'];profile='market-'+provider
        existing=next((s for s in browsers.list() if s.get('profile_id')==profile),None)
        if existing:
            return {'status':'session_open','session_id':existing['session_id'],'profile_id':profile,
                    'account_status':'sign_in_unverified','persisted_locally':True}
        opened=browsers.open(profile_id=profile,interactive_login=True)
        if opened.get('status')!='READY': raise HTTPException(503,'MARKETPLACE_BROWSER_UNAVAILABLE')
        sid=opened['session_id']
        try:
            authorize(provider)  # revoke may race the runtime startup
            page=browsers.navigate(sid,definition['home'])
            if page.get('status')!='AVAILABLE': raise HTTPException(502,'MARKETPLACE_BROWSER_NAVIGATION_FAILED')
        except Exception:
            browsers.close(sid)
            raise
        # Never copy page snapshots, login fields, cookies or a claimed login result into chat.
        return {'status':'session_open','session_id':sid,'profile_id':profile,
                'account_status':'sign_in_unverified','persisted_locally':True}

    @app.delete('/api/commerce-sessions/{provider}/session')
    def close_session(provider:str):
        from .connector_marketplaces import PROVIDERS
        if 'service.commerce.'+provider not in PROVIDERS: raise HTTPException(404,'MARKETPLACE_NOT_FOUND')
        browsers=host()['browser_sessions'];profile='market-'+provider
        for entry in browsers.list():
            if entry.get('profile_id')==profile:browsers.close(entry['session_id'])
        return {'closed':True,'profile_retained':True}

    @app.delete('/api/commerce-sessions/{provider}/session/forget')
    def forget_session(provider:str):
        from .connector_marketplaces import PROVIDERS
        if 'service.commerce.'+provider not in PROVIDERS: raise HTTPException(404,'MARKETPLACE_NOT_FOUND')
        browsers=host()['browser_sessions'];profile='market-'+provider
        for entry in browsers.list():
            if entry.get('profile_id')==profile:browsers.close(entry['session_id'])
        try:browsers.forget_profile(profile)
        except ValueError as exc:
            if str(exc)!='BROWSER_PROFILE_NOT_FOUND':raise HTTPException(409,'MARKETPLACE_PROFILE_BUSY') from None
        return {'forgotten':True}

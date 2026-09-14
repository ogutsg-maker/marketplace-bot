from aiohttp import web
import json, os
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
from client_ai import ClientAI

def uid(request):
    raw = request.headers.get('X-Telegram-Init-Data','').strip()
    if not raw: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':'telegram_init_data_required'}), content_type='application/json')
    try: return int(validate_telegram_webapp_init_data(raw, request.app.get('stage3_bot_token') or os.getenv('BOT_TOKEN',''))['id'])
    except (TelegramWebAppAuthError,ValueError,TypeError,KeyError) as e: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':str(e)}), content_type='application/json')

async def chat(request):
    user_id = uid(request)
    data = await request.json()
    text = str(data.get('text') or '').strip()
    if not text: return web.json_response({'ok':False,'error':'text_required'}, status=400)
    from database import DatabaseManager
    lang = (DatabaseManager().get_user(user_id) or {}).get('lang','hy')
    reply = await request.app['client_ai'].process(user_id, text, lang)
    return web.json_response({'ok':True,'reply':reply})

def register_client_routes(app, ai):
    app['client_ai'] = ClientAI(ai)
    app.router.add_post('/api/client/ai/chat', chat)

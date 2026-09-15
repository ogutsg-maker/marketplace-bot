"""End-to-end Armenia AI Guide marketplace flow."""
from __future__ import annotations
import json, os, secrets
from decimal import Decimal
from datetime import datetime, date
import psycopg
from aiohttp import web
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
from booking_schema import ensure_booking_schema

def _db_url():
    value=os.getenv('DATABASE_URL','').strip()
    if not value: raise RuntimeError('DATABASE_URL is not configured')
    return value

def _safe(v):
    if isinstance(v,(datetime,date)): return v.isoformat()
    if isinstance(v,Decimal): return float(v)
    if isinstance(v,dict): return {k:_safe(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [_safe(x) for x in v]
    return v

def _rows(sql,params=()):
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,params); cols=[d.name for d in cur.description] if cur.description else []
            rows=[_safe(dict(zip(cols,row))) for row in cur.fetchall()]
    return rows

def _one(sql,params=()):
    rows=_rows(sql,params); return rows[0] if rows else None

def _exec(sql,params=(),returning=False):
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,params); result=None
            if returning:
                row=cur.fetchone(); cols=[d.name for d in cur.description] if cur.description else []
                result=_safe(dict(zip(cols,row))) if row else None
        conn.commit()
    return result

def _uid(request):
    raw=request.headers.get('X-Telegram-Init-Data','').strip()
    if not raw: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':'telegram_init_data_required'}),content_type='application/json')
    try:
        data=validate_telegram_webapp_init_data(raw,request.app.get('stage3_bot_token') or os.getenv('BOT_TOKEN','')); return int(data['id'])
    except (TelegramWebAppAuthError,ValueError,TypeError,KeyError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':str(exc)}),content_type='application/json')

def _json(value): return json.dumps(value or {},ensure_ascii=False)

def _commission(service):
    data=service.get('data_json') or {}
    if isinstance(data,str):
        try:data=json.loads(data)
        except Exception:data={}
    try:rate=float(data.get('commission_percent',10))
    except Exception:rate=10.0
    return max(0.0,min(100.0,rate))

async def client_search(request):
    uid=_uid(request); data=await request.json(); q=str(data.get('query') or data.get('text') or '').strip(); city=str(data.get('city') or '').strip(); category_id=int(data.get('category_id') or 0)
    params=[]; where=["s.status='approved'","p.status='approved'","pd.status='approved'"]
    if q:
        like=f'%{q}%'; params += [like]*5
        where.append("(LOWER(s.name) LIKE LOWER(%s) OR LOWER(s.description) LIKE LOWER(%s) OR LOWER(p.business_name) LIKE LOWER(%s) OR LOWER(c.name_am) LIKE LOWER(%s) OR LOWER(c.name_ru) LIKE LOWER(%s))")
    if city: params.append(city); where.append("EXISTS(SELECT 1 FROM partner_locations pl WHERE pl.partner_id=p.id AND LOWER(COALESCE(pl.city,''))=LOWER(%s))")
    if category_id: params.append(category_id); where.append('s.category_id=%s')
    sql=f"""SELECT s.id service_id,s.partner_id,s.category_id,s.name service_name,s.description,s.price,s.currency,s.duration_minutes,s.data_json,p.business_name,p.business_description,p.contact_share_policy,c.name_am category_name_am,c.name_ru category_name_ru,COALESCE((SELECT pl.city FROM partner_locations pl WHERE pl.partner_id=p.id ORDER BY pl.id LIMIT 1),'') city,COALESCE((SELECT pl.marz FROM partner_locations pl WHERE pl.partner_id=p.id ORDER BY pl.id LIMIT 1),'') marz FROM services s JOIN partners p ON p.id=s.partner_id JOIN partner_direction_categories pdc ON pdc.category_id=s.category_id JOIN partner_directions pd ON pd.id=pdc.partner_direction_id AND pd.partner_id=p.id LEFT JOIN categories c ON c.id=s.category_id WHERE {' AND '.join(where)} GROUP BY s.id,p.id,c.id,p.business_name,p.business_description,p.contact_share_policy ORDER BY s.created_at DESC LIMIT 20"""
    items=_rows(sql,tuple(params))
    for item in items: item.pop('data_json',None); item.pop('business_description',None)
    return web.json_response({'ok':True,'items':items,'query':q,'client_id':uid})

async def create_request(request):
    uid=_uid(request); data=await request.json(); summary=str(data.get('summary') or data.get('query') or '').strip(); city=str(data.get('city') or '').strip() or None; lang=str(data.get('language') or 'hy')[:5]; profile=data.get('preferences') or {}
    item=_exec("INSERT INTO service_requests(client_id,status,language,city,summary,preferences_json) VALUES(%s,'searching',%s,%s,%s,%s::jsonb) RETURNING *",(uid,lang,city,summary,_json(profile)),True)
    return web.json_response({'ok':True,'request':item})

async def select_candidate(request):
    uid=_uid(request); rid=int(request.match_info['request_id']); data=await request.json(); service_id=int(data.get('service_id') or 0)
    item=_one("SELECT sr.id request_id,sr.status,s.id service_id,s.partner_id,s.name service_name,s.price,s.currency,s.duration_minutes,p.business_name,p.contact_share_policy FROM service_requests sr JOIN services s ON s.id=%s JOIN partners p ON p.id=s.partner_id WHERE sr.id=%s AND sr.client_id=%s AND s.status='approved' AND p.status='approved'",(service_id,rid,uid))
    if not item:return web.json_response({'ok':False,'error':'candidate_not_found'},status=404)
    _exec("INSERT INTO request_candidates(request_id,partner_id,service_id,rank_score,status) VALUES(%s,%s,%s,100,'selected') ON CONFLICT(request_id,partner_id,service_id) DO UPDATE SET status='selected'",(rid,item['partner_id'],service_id))
    negotiation=_exec("INSERT INTO negotiations(request_id,client_id,partner_id,state_json) VALUES(%s,%s,%s,%s::jsonb) RETURNING *",(rid,uid,item['partner_id'],_json({'service_id':service_id,'service_name':item['service_name'],'price':float(item['price'] or 0),'currency':item['currency'],'client_agreed':False,'partner_agreed':False})),True)
    _exec("UPDATE service_requests SET status='negotiating',updated_at=NOW() WHERE id=%s",(rid,))
    _exec("INSERT INTO negotiation_messages(negotiation_id,sender_role,message,data_json) VALUES(%s,'ai',%s,%s::jsonb)",(negotiation['id'],f"Ընտրված ծառայությունն է՝ {item['service_name']}։ Գինը՝ {item['price']} {item['currency']}։ Կարող եք գրել ձեր ցանկությունները։",_json({'type':'selection'})))
    return web.json_response({'ok':True,'negotiation':negotiation,'candidate':item})

async def negotiation_get(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); n=_one('SELECT * FROM negotiations WHERE id=%s AND client_id=%s',(nid,uid))
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_found'},status=404)
    return web.json_response({'ok':True,'negotiation':n,'messages':_rows('SELECT id,sender_role,sender_id,message,data_json,created_at FROM negotiation_messages WHERE negotiation_id=%s ORDER BY id',(nid,))})

def _state(n):
    s=n.get('state_json') or {}
    if isinstance(s,str):
        try:s=json.loads(s)
        except Exception:s={}
    return s

async def negotiation_client_message(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); data=await request.json(); text=str(data.get('message') or '').strip()
    if not text:return web.json_response({'ok':False,'error':'message_required'},status=400)
    n=_one("SELECT * FROM negotiations WHERE id=%s AND client_id=%s AND status='active'",(nid,uid))
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_active'},status=400)
    _exec("INSERT INTO negotiation_messages(negotiation_id,sender_role,sender_id,message) VALUES(%s,'client',%s,%s)",(nid,uid,text))
    st=_state(n); low=text.lower()
    if any(x in low for x in ('согласен','согласна','беру','agree','համաձայն եմ','համաձայն եմ։','այո')):
        st['client_agreed']=True; _exec("UPDATE negotiations SET state_json=%s::jsonb,updated_at=NOW() WHERE id=%s",(_json(st),nid))
        reply='Ձեր համաձայնությունը պահպանվեց։ Սպասում ենք գործընկերոջ վերջնական համաձայնությանը։' if not st.get('partner_agreed') else 'Երկու կողմն էլ համաձայն են։ Հաջորդ քայլը՝ Test Idram վճարումը։'
        if st.get('partner_agreed'):
            _exec("UPDATE negotiations SET status='agreed',updated_at=NOW() WHERE id=%s",(nid,)); _exec("UPDATE service_requests SET status='confirmed',updated_at=NOW() WHERE id=%s",(n['request_id'],))
    else: reply='Ձեր հաղորդագրությունը փոխանցված է գործընկերոջը։ Գործընկերը կարող է պատասխանել իր կաբինետից։'
    _exec("INSERT INTO negotiation_messages(negotiation_id,sender_role,message) VALUES(%s,'ai',%s)",(nid,reply))
    return await negotiation_get(request)

async def partner_negotiations(request):
    uid=_uid(request); partner=_one('SELECT * FROM partners WHERE user_id=%s',(uid,))
    if not partner:return web.json_response({'ok':False,'error':'partner_not_found'},status=404)
    items=_rows("SELECT n.id,n.request_id,n.status,n.state_json,n.updated_at,sr.summary,sr.city,s.name service_name,p.business_name FROM negotiations n JOIN service_requests sr ON sr.id=n.request_id LEFT JOIN services s ON s.id=(n.state_json->>'service_id')::bigint JOIN partners p ON p.id=n.partner_id WHERE n.partner_id=%s AND n.status='active' ORDER BY n.updated_at DESC",(partner['id'],))
    return web.json_response({'ok':True,'items':items})

async def partner_negotiation_messages(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); n=_one("SELECT n.* FROM negotiations n JOIN partners p ON p.id=n.partner_id WHERE n.id=%s AND p.user_id=%s",(nid,uid))
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_found'},status=404)
    return web.json_response({'ok':True,'negotiation':n,'messages':_rows('SELECT * FROM negotiation_messages WHERE negotiation_id=%s ORDER BY id',(nid,))})

async def partner_reply(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); data=await request.json(); text=str(data.get('message') or '').strip()
    if not text:return web.json_response({'ok':False,'error':'message_required'},status=400)
    n=_one("SELECT n.* FROM negotiations n JOIN partners p ON p.id=n.partner_id WHERE n.id=%s AND p.user_id=%s AND n.status='active'",(nid,uid))
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_active'},status=400)
    _exec("INSERT INTO negotiation_messages(negotiation_id,sender_role,sender_id,message) VALUES(%s,'partner',%s,%s)",(nid,uid,text))
    return web.json_response({'ok':True,'messages':_rows('SELECT * FROM negotiation_messages WHERE negotiation_id=%s ORDER BY id',(nid,))})

async def partner_agree(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); n=_one("SELECT n.* FROM negotiations n JOIN partners p ON p.id=n.partner_id WHERE n.id=%s AND p.user_id=%s AND n.status='active'",(nid,uid))
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_active'},status=400)
    st=_state(n); st['partner_agreed']=True
    if st.get('client_agreed'):
        _exec("UPDATE negotiations SET state_json=%s::jsonb,status='agreed',updated_at=NOW() WHERE id=%s",(_json(st),nid)); _exec("UPDATE service_requests SET status='confirmed',updated_at=NOW() WHERE id=%s",(n['request_id'],))
        message='Համաձայն եմ։ Երկու կողմն էլ համաձայն են։ Կարող ենք ամրագրել։'
    else:
        _exec("UPDATE negotiations SET state_json=%s::jsonb,updated_at=NOW() WHERE id=%s",(_json(st),nid)); message='Համաձայն եմ պայմաններին։ Սպասում ենք հաճախորդի վերջնական համաձայնությանը։'
    _exec("INSERT INTO negotiation_messages(negotiation_id,sender_role,sender_id,message) VALUES(%s,'partner',%s,%s)",(nid,uid,message))
    return web.json_response({'ok':True,'status':'agreed' if st.get('client_agreed') else 'waiting_client'})

async def test_payment(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); data=await request.json(); n=_one("SELECT * FROM negotiations WHERE id=%s AND client_id=%s AND status='agreed'",(nid,uid))
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_agreed'},status=400)
    st=_state(n); service_id=int(st.get('service_id') or 0); service=_one("SELECT * FROM services WHERE id=%s AND partner_id=%s AND status='approved'",(service_id,n['partner_id']))
    if not service:return web.json_response({'ok':False,'error':'service_not_available'},status=404)
    price=float(data.get('amount') or service.get('price') or 0); rate=_commission(service); commission=round(price*rate/100,2); partner_amount=round(price-commission,2); txn='TEST-IDRAM-'+secrets.token_hex(6)
    booking=_exec("INSERT INTO bookings(request_id,negotiation_id,client_id,partner_id,service_id,status,service_name,agreed_price,currency,commission_amount,partner_amount,data_json) VALUES(%s,%s,%s,%s,%s,'paid',%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",(n['request_id'],nid,uid,n['partner_id'],service_id,service['name'],price,service['currency'],commission,partner_amount,_json({'payment_mode':'TEST_IDRAM','test_transaction':txn,'service_price':price,'commission_percent':rate})),True)
    payment=_exec("INSERT INTO payments(booking_id,client_id,partner_id,payment_type,status,amount,currency,provider,provider_payment_id,data_json) VALUES(%s,%s,%s,'commission','paid',%s,%s,'IDRAM_TEST',%s,%s::jsonb) RETURNING *",(booking['id'],uid,n['partner_id'],commission,service['currency'],txn,_json({'test':True})),True)
    _exec("UPDATE service_requests SET status='booked',updated_at=NOW() WHERE id=%s",(n['request_id'],))
    _exec("INSERT INTO partner_financial_ledger(partner_id,booking_id,entry_type,amount,currency,description) VALUES(%s,%s,'commission',%s,%s,%s)",(n['partner_id'],booking['id'],commission,service['currency'],'Test Idram platform commission'))
    _exec("INSERT INTO partner_financial_ledger(partner_id,booking_id,entry_type,amount,currency,description) VALUES(%s,%s,'partner_due',%s,%s,%s)",(n['partner_id'],booking['id'],partner_amount,service['currency'],'Partner amount after platform commission'))
    check=_exec("INSERT INTO booking_checkins(booking_id,token) VALUES(%s,%s) RETURNING *",(booking['id'],secrets.token_urlsafe(24)),True)
    partner=_one("SELECT id,business_name,business_description,contact_share_policy,contact_sharing_enabled,profile_json FROM partners WHERE id=%s",(n['partner_id'],)); locations=_rows("SELECT marz,city,village,address,location_type FROM partner_locations WHERE partner_id=%s ORDER BY id LIMIT 5",(n['partner_id'],))
    profile=partner.get('profile_json') or {}
    if isinstance(profile,str):
        try:profile=json.loads(profile)
        except Exception:profile={}
    contact={}
    if partner.get('contact_sharing_enabled'): contact={k:profile.get(k) for k in ('phone','website','telegram') if profile.get(k)}
    details={'business_name':partner['business_name'],'locations':locations,'contact':contact,'service':service['name'],'price':price,'currency':service['currency'],'booking_id':booking['id']}
    return web.json_response({'ok':True,'payment':payment,'booking':booking,'checkin':check,'partner':details})

def register_marketplace_flow_routes(app):
    ensure_booking_schema()
    app.router.add_post('/api/market/client/search',client_search)
    app.router.add_post('/api/market/client/request',create_request)
    app.router.add_post('/api/market/client/request/{request_id}/select',select_candidate)
    app.router.add_get('/api/market/client/negotiation/{negotiation_id}',negotiation_get)
    app.router.add_post('/api/market/client/negotiation/{negotiation_id}/message',negotiation_client_message)
    app.router.add_post('/api/market/client/negotiation/{negotiation_id}/pay-test',test_payment)
    app.router.add_get('/api/market/partner/negotiations',partner_negotiations)
    app.router.add_get('/api/market/partner/negotiation/{negotiation_id}',partner_negotiation_messages)
    app.router.add_post('/api/market/partner/negotiation/{negotiation_id}/reply',partner_reply)
    app.router.add_post('/api/market/partner/negotiation/{negotiation_id}/agree',partner_agree)

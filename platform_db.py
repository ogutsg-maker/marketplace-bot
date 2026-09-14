"""Small repository for the new AI-first marketplace layer."""
from __future__ import annotations
import json
import os
from decimal import Decimal
from datetime import date, datetime
import psycopg
from psycopg.rows import dict_row


def _url():
    v=os.getenv('DATABASE_URL','').strip()
    if not v: raise RuntimeError('DATABASE_URL is not configured')
    return v

def _conn():
    return psycopg.connect(_url(), row_factory=dict_row)

def _safe(v):
    if isinstance(v,(datetime,date)): return v.isoformat()
    if isinstance(v,Decimal): return float(v)
    if isinstance(v,dict): return {k:_safe(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [_safe(x) for x in v]
    return v

def one(sql, params=()):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(sql,params); r=cur.fetchone(); c.commit(); return _safe(dict(r)) if r else None

def rows(sql, params=()):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(sql,params); r=cur.fetchall(); c.commit(); return [_safe(dict(x)) for x in r]

def execute(sql, params=(), returning=False):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(sql,params)
            result=None
            if returning:
                r=cur.fetchone(); result=_safe(dict(r)) if r else None
        c.commit(); return result

def json_dump(v): return json.dumps(v or {}, ensure_ascii=False)

# Partner

def get_partner_by_user(user_id): return one('SELECT * FROM partners WHERE user_id=%s',(user_id,))
def get_partner(partner_id): return one('SELECT * FROM partners WHERE id=%s',(partner_id,))
def ensure_partner(user_id, name='', description=''):
    p=get_partner_by_user(user_id)
    if p: return p
    return execute('INSERT INTO partners(user_id,business_name,business_description) VALUES(%s,%s,%s) RETURNING *',(user_id,name,description),True)
def update_partner(partner_id, **fields):
    allowed={'business_name','business_description','status','verification_status','rejection_reason','contact_share_policy','profile_json'}
    fields={k:v for k,v in fields.items() if k in allowed}
    if not fields:return get_partner(partner_id)
    sets=[]; vals=[]
    for k,v in fields.items():
        if k.endswith('_json') and not isinstance(v,str): v=json_dump(v)
        sets.append(f'{k}=%s'); vals.append(v)
    sets.append('updated_at=NOW()'); vals.append(partner_id)
    return execute(f'UPDATE partners SET {", ".join(sets)} WHERE id=%s RETURNING *',vals,True)

# AI sessions

def active_session(user_id, role, session_type):
    return one("SELECT * FROM ai_sessions WHERE user_id=%s AND role=%s AND session_type=%s AND status='active' LIMIT 1",(user_id,role,session_type))
def create_session(user_id, role, session_type, context=None):
    old=active_session(user_id,role,session_type)
    if old:return old
    return execute('INSERT INTO ai_sessions(user_id,role,session_type,context_json) VALUES(%s,%s,%s,%s::jsonb) RETURNING *',(user_id,role,session_type,json_dump(context or {})),True)
def update_session(session_id, context):
    return execute('UPDATE ai_sessions SET context_json=%s::jsonb,updated_at=NOW() WHERE id=%s RETURNING *',(json_dump(context),session_id),True)
def add_ai_message(session_id, sender_role, text, data=None):
    return execute('INSERT INTO ai_messages(session_id,sender_role,message_text,data_json) VALUES(%s,%s,%s,%s::jsonb) RETURNING *',(session_id,sender_role,text,json_dump(data or {})),True)
def recent_ai_messages(session_id, limit=16):
    return rows('SELECT sender_role,message_text,created_at FROM ai_messages WHERE session_id=%s ORDER BY id DESC LIMIT %s',(session_id,limit))[::-1]

# Catalog / directions

def catalog_tree():
    return rows('''SELECT m.id master_category_id,m.name_am master_name_am,m.name_ru master_name_ru,m.slug master_slug,
                         c.id category_id,c.name_am category_name_am,c.name_ru category_name_ru,c.slug category_slug
                  FROM master_categories m LEFT JOIN categories c ON c.master_category_id=m.id AND c.is_active=TRUE
                  WHERE m.is_active=TRUE ORDER BY m.id,c.id''')

def approved_partner_categories(partner_id):
    return rows('''SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.slug
                   FROM partner_direction_categories pdc
                   JOIN partner_directions pd ON pd.id=pdc.partner_direction_id AND pd.status='approved'
                   JOIN categories c ON c.id=pdc.category_id AND c.is_active=TRUE
                   WHERE pd.partner_id=%s ORDER BY c.id''',(partner_id,))

def upsert_direction(partner_id, master_category_id, category_ids=None, status='pending'):
    pd=execute('''INSERT INTO partner_directions(partner_id,master_category_id,status)
                   VALUES(%s,%s,%s)
                   ON CONFLICT(partner_id,master_category_id) DO UPDATE SET updated_at=NOW()
                   RETURNING *''',(partner_id,master_category_id,status),True)
    for cid in category_ids or []:
        execute('INSERT INTO partner_direction_categories(partner_direction_id,category_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(pd['id'],cid))
    return one('SELECT * FROM partner_directions WHERE id=%s',(pd['id'],))

# AI catalog proposals

def create_or_update_proposal(partner_id, data, proposal_id=None):
    fields=(data.get('master_category'),data.get('category'),data.get('subcategory'),data.get('service'),data.get('description',''),data.get('reason',''),json_dump(data))
    if proposal_id:
        return execute('''UPDATE ai_catalog_proposals SET proposed_master_category=%s,proposed_category=%s,proposed_subcategory=%s,proposed_service=%s,description=%s,reason=%s,payload_json=%s::jsonb,status='pending',updated_at=NOW() WHERE id=%s RETURNING *''',(*fields,proposal_id),True)
    return execute('''INSERT INTO ai_catalog_proposals(partner_id,proposed_master_category,proposed_category,proposed_subcategory,proposed_service,description,reason,payload_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *''',(partner_id,*fields),True)
def proposal(proposal_id): return one('SELECT * FROM ai_catalog_proposals WHERE id=%s',(proposal_id,))
def proposals(status=None):
    if status:return rows('SELECT p.*,pr.business_name FROM ai_catalog_proposals p LEFT JOIN partners pr ON pr.id=p.partner_id WHERE p.status=%s ORDER BY p.created_at DESC',(status,))
    return rows('SELECT p.*,pr.business_name FROM ai_catalog_proposals p LEFT JOIN partners pr ON pr.id=p.partner_id ORDER BY p.created_at DESC')
def review_proposal(proposal_id,status,admin_id,comment=''):
    return execute('UPDATE ai_catalog_proposals SET status=%s,admin_comment=%s,reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s RETURNING *',(status,comment,admin_id,proposal_id),True)

def edit_proposal(proposal_id, admin_id, fields):
    allowed={k:fields.get(k) for k in ('proposed_master_category','proposed_category','proposed_subcategory','proposed_service','description','reason') if k in fields}
    if not allowed: return proposal(proposal_id)
    sets=[];vals=[]
    for k,v in allowed.items(): sets.append(f'{k}=%s'); vals.append(v)
    sets += ["status='edited'","reviewed_by=%s","reviewed_at=NOW()","updated_at=NOW()"]
    vals += [admin_id,proposal_id]
    return execute(f'UPDATE ai_catalog_proposals SET {", ".join(sets)} WHERE id=%s RETURNING *',vals,True)


def add_clarification(proposal_id,partner_id,admin_id,message):
    return execute('INSERT INTO admin_clarifications(proposal_id,partner_id,admin_id,message) VALUES(%s,%s,%s,%s) RETURNING *',(proposal_id,partner_id,admin_id,message),True)
def latest_clarification(partner_id): return one("SELECT * FROM admin_clarifications WHERE partner_id=%s AND status='sent' ORDER BY id DESC LIMIT 1",(partner_id,))

# Potential partners

def create_potential(data):
    return execute('''INSERT INTO potential_partners(source,business_name,description,direction,category,subcategory,country,marz,city,village,phone,website,email,social_json,services_json,prices_json,source_urls_json,ai_reason,ai_confidence,status)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s) RETURNING *''',(
        data.get('source','ai_research'),data.get('business_name',''),data.get('description',''),data.get('direction'),data.get('category'),data.get('subcategory'),data.get('country','Armenia'),data.get('marz'),data.get('city'),data.get('village'),data.get('phone'),data.get('website'),data.get('email'),json_dump(data.get('social',{})),json_dump(data.get('services',[])),json_dump(data.get('prices',[])),json_dump(data.get('source_urls',[])),data.get('ai_reason',''),data.get('ai_confidence'),data.get('status','new')),True)
def potential_partners(status=None, q=None):
    clauses=[]; vals=[]
    if status: clauses.append('p.status=%s'); vals.append(status)
    if q: clauses.append('(LOWER(p.business_name) LIKE LOWER(%s) OR LOWER(COALESCE(p.city,\'\')) LIKE LOWER(%s) OR LOWER(COALESCE(p.category,\'\')) LIKE LOWER(%s))'); vals += [f'%{q}%',f'%{q}%',f'%{q}%']
    where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
    return rows('SELECT p.*,pr.business_name AS linked_partner_name FROM potential_partners p LEFT JOIN partners pr ON pr.id=p.partner_id'+where+' ORDER BY p.created_at DESC',vals)
def update_potential(pid, **fields):
    allowed={'status','admin_comment','partner_id','business_name','description','direction','category','subcategory','marz','city','village','phone','website','email','social_json','services_json','prices_json','source_urls_json','ai_reason','ai_confidence'}
    fields={k:v for k,v in fields.items() if k in allowed}
    if not fields:return one('SELECT * FROM potential_partners WHERE id=%s',(pid,))
    sets=[];vals=[]
    for k,v in fields.items():
        if k.endswith('_json') and not isinstance(v,str):v=json_dump(v)
        sets.append(f'{k}=%s');vals.append(v)
    sets.append('updated_at=NOW()');vals.append(pid)
    return execute(f'UPDATE potential_partners SET {", ".join(sets)} WHERE id=%s RETURNING *',vals,True)

def save_partner_document(partner_id, direction_id, filename, mime_type, file_data, document_type='business_document'):
    row=execute(
        "INSERT INTO partner_verification_documents(partner_id,partner_direction_id,document_type,original_filename,file_data,mime_type,file_size,status) VALUES(%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING *",
        (partner_id,direction_id,document_type,filename,file_data,mime_type,len(file_data)),True
    )
    execute("UPDATE partners SET verification_status=CASE WHEN status='approved' THEN verification_status ELSE 'pending' END,status=CASE WHEN status IN ('draft','rejected') THEN 'pending' ELSE status END,rejection_reason=NULL,updated_at=NOW() WHERE id=%s",(partner_id,))
    if direction_id:
        execute("UPDATE partner_directions SET status='pending',rejection_reason=NULL,updated_at=NOW() WHERE id=%s AND partner_id=%s",(direction_id,partner_id))
    return row

def mark_clarification_answered(clarification_id):
    return execute("UPDATE admin_clarifications SET status='answered',answered_at=NOW() WHERE id=%s RETURNING *",(clarification_id,),True)

from __future__ import annotations
import json
from platform_db import create_session, active_session, add_ai_message, update_session, execute, rows

class ClientAI:
    def __init__(self, ai): self.ai = ai

    async def process(self, user_id, text, lang='hy'):
        session = active_session(user_id, 'client', 'sales') or create_session(user_id, 'client', 'sales', {'request_id': None, 'stage': 'discovery'})
        ctx = session.get('context_json') or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        add_ai_message(session['id'], 'user', text)
        cats = rows('SELECT id,name_am name_hy,name_ru,name_am name_en FROM categories WHERE is_active=TRUE ORDER BY id')
        analysis = await self.ai.analyze_request(text, cats)
        if not ctx.get('request_id'):
            req = execute('INSERT INTO service_requests(client_id,category_id,status,language,city,summary,preferences_json) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *', (user_id, analysis.category_id, 'searching', analysis.language, analysis.city, analysis.summary, json.dumps({'raw': text, 'checklist': analysis.checklist}, ensure_ascii=False)), True)
            ctx['request_id'] = req['id']
        else:
            execute("UPDATE service_requests SET status='searching',summary=%s,city=%s,updated_at=NOW() WHERE id=%s", (analysis.summary, analysis.city, ctx['request_id']))
        candidates = rows("""SELECT s.id service_id,s.name service_name,s.price,s.duration_minutes,c.id category_id
                            FROM services s JOIN categories c ON c.id=s.category_id
                            JOIN partner_directions pd ON pd.partner_id=s.partner_id AND pd.status='approved'
                            WHERE s.status='approved' AND s.category_id=%s
                            ORDER BY s.created_at DESC LIMIT 3""", (analysis.category_id,)) if analysis.category_id else []
        for rank, c in enumerate(candidates, 1):
            execute('INSERT INTO request_candidates(request_id,partner_id,service_id,rank_score,match_reason) SELECT %s,s.partner_id,%s,%s,%s FROM services s WHERE s.id=%s ON CONFLICT DO NOTHING', (ctx['request_id'], c['service_id'], 100-rank*5, 'Համապատասխանում է կատեգորիային', c['service_id']))
        ctx['stage'] = 'options_found' if candidates else 'discovery'
        ctx['last_analysis'] = analysis.model_dump()
        update_session(session['id'], ctx)
        if candidates:
            options = []
            for i, c in enumerate(candidates, 1):
                price = f" · {float(c['price']):,.0f} ֏" if c.get('price') is not None else ''
                options.append(f'{i}. {c["service_name"]}{price}')
            texts = {
                'hy': 'Գտա մի քանի համապատասխան տարբերակ։ Անձնական տվյալները դեռ չեմ բացահայտում։ Ընտրեք տարբերակը կամ գրեք՝ ինչն եք ուզում փոխել։',
                'ru': 'Нашёл несколько подходящих вариантов. Данные партнёра пока скрыты. Выберите вариант или напишите, что изменить.',
                'en': 'I found several suitable options. Partner identifying details stay hidden for now. Choose an option or tell me what to change.'
            }
            reply = texts.get(lang, texts['hy']) + '\n\n' + '\n'.join(options)
        else:
            texts = {
                'hy': 'Հասկացա։ Մի քանի բան էլ ճշտեմ, որպեսզի ճիշտ տարբերակներ գտնեմ։',
                'ru': 'Понял. Уточню ещё несколько деталей, чтобы подобрать подходящие варианты.',
                'en': 'Understood. I need a few more details to find suitable options.'
            }
            reply = texts.get(lang, texts['hy']) + '\n\n' + '\n'.join('• ' + q for q in analysis.checklist[:4])
        add_ai_message(session['id'], 'ai', reply, {'analysis': analysis.model_dump(), 'candidates': candidates})
        return reply

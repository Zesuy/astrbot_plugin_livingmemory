"""Bounded, source-backed queries. Invoked only from the archive LLM tool."""
from __future__ import annotations

import json
import re
import time
import uuid
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from ..utils.event_memory import parse_overview_window
from ..utils.recall_policy import name_in_query
from ..memory_scope import parse_identity_aliases, scope_sql
from .archive_sampling import overview, style_sample, author_parts

TZ = ZoneInfo("Asia/Shanghai")
STOP = {"的","是","谁","叫","什么","名字","我","你","他","她","一下","查","查询","昨天","昨晚","晚上","时候","吗","有","了","呢","和","在"}


def tool_output_metadata(row):
    for component in json.loads(row['components']):
        if component.get('type') == 'tool_output':
            return {key: value for key, value in component.items() if key != 'type'}
    return None


def window_for(expression: str, start: str, end: str, now: float):
    expression = expression.strip()
    if expression and (start or end):
        raise ValueError("choose time_range or explicit start/end, not both")
    if start or end:
        if not start or not end:
            raise ValueError("start and end must both be supplied")
        values = [datetime.fromisoformat(v) for v in (start,end)]
        if any(v.tzinfo is None for v in values):
            raise ValueError("explicit dates require UTC offset, e.g. +08:00")
        a,b = (v.timestamp() for v in values)
        if a >= b:
            raise ValueError("start must precede end")
        return a, min(b,now)
    if expression:
        recent = re.fullmatch(r'(?:最近|近)\s*(\d{1,3})\s*天',expression)
        if expression in {'刚才','刚刚'}:
            return now-30*60,now
        if expression in {'最近一周','近一周','最近一星期'}:
            recent_days = 7
        elif recent:
            recent_days = int(recent.group(1))
        else:
            recent_days = None
        if recent_days is not None:
            if not 1<=recent_days<=31:
                raise ValueError('recent day count must be 1..31')
            midnight = datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0)
            return (midnight-timedelta(days=recent_days-1)).timestamp(),now
        if len(re.findall(r"20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}",expression)) > 1:
            raise ValueError("multiple dates require explicit start/end")
        window = parse_overview_window(expression + "发生了什么事情",now)
        if window is None:
            raise ValueError("unrecognized time_range; use explicit start/end")
        return window.start,window.end
    return None,now


class ArchiveQueryService:
    def __init__(self, store, *, max_candidates=400, max_results=8000,
                 result_chars=24000, max_sessions=32, identity_aliases=""):
        self.store = store
        self.path = Path(store.db_path).resolve().as_uri()+"?mode=ro"
        self.max_candidates = max_candidates
        self.max_results = max_results
        self.result_chars = result_chars
        self.max_sessions = max_sessions
        self.identity_aliases = identity_aliases
        self.snapshots = OrderedDict()
        self.alias_resolver = None
        self.calls = 0

    async def _person(self, conn, conversation, person, question):
        from ..user_profile import resolve_member, read_scope_profiles, retired_aliases, normalize_value
        profiles = await read_scope_profiles(conversation, conn)
        retired = retired_aliases(profiles)
        if person and not person.isdigit() and normalize_value(person) in retired:
            return [], False, "explicitly_retired_alias"
        predicate, scope_args = scope_sql("conversation", conversation)
        learned = await resolve_member(conversation, person, conn, include_archive=False) if person else None
        if learned:
            ids = [learned] if isinstance(learned, str) else learned
            observed = await conn.execute_fetchall("SELECT DISTINCT sender_id,nickname FROM archive_names WHERE " + predicate, scope_args)
            return ([{"account_id": uid, "observed_names": sorted({person, *[name for account, name in observed if account == uid and normalize_value(name) not in retired_aliases(profiles, uid)]}), "configured_names": []} for uid in ids],
                    len(ids) > 1, "learned_or_observed_identity")
        rows = await conn.execute_fetchall("SELECT DISTINCT n.sender_id,n.nickname,m.platform FROM archive_names n JOIN archive_messages m ON m.uid=n.uid WHERE " + predicate.replace("conversation", "n.conversation"),scope_args)
        names = {}
        configured = {}
        manual = parse_identity_aliases(self.identity_aliases)
        for account,nickname,platform in rows:
            if normalize_value(nickname) not in retired_aliases(profiles, account):
                names.setdefault(account,set()).add(nickname)
            for source in (platform+":"+account,account,nickname):
                alias = manual.get(source.casefold())
                if alias and normalize_value(alias) not in retired_aliases(profiles, account):
                    configured.setdefault(account,set()).add(alias)
        found = {account for account,aliases in names.items()
                 if (person and (person==account or person in aliases))
                 or (not person and any(name_in_query(alias,question) for alias in [account,*aliases]))}
        found.update(a for a,aliases in configured.items() if person in aliases or (not person and any(name_in_query(n,question) for n in aliases)))
        hint_status = "not_used"
        if self.alias_resolver:
            # Existing graph aliases are scoped hints; not newly invented identities.
            try:
                hints = await asyncio.wait_for(self.alias_resolver(person or question,conversation,None),timeout=2)
                hint_status = "legacy_scoped_hint"
            except Exception:
                hints = []
                hint_status = "alias_hint_unavailable"
            for hint in hints:
                if hint.get("sender_id"):
                    account = str(hint["sender_id"])
                    personal_retired = retired_aliases(profiles, account)
                    aliases = [name for name in hint.get("aliases", []) if normalize_value(name) not in personal_retired]
                    if (hint.get("aliases") and not aliases) or (person and normalize_value(person) in personal_retired):
                        continue
                    names.setdefault(account,set()).update(aliases)
                    found.add(account)
        if person and person.isdigit():
            found.add(person)
        if person and not found and any(normalize_value(person) in retired_aliases(profiles, uid) for uid in profiles):
            hint_status = "explicitly_retired_alias"
        accounts = [{"account_id":a,"observed_names":sorted(names.get(a,[])),"configured_names":sorted(configured.get(a,[]))} for a in sorted(found)]
        ambiguous = bool(person and len(found)>1)
        return accounts,ambiguous,hint_status

    @staticmethod
    def _terms(question, keywords, accounts):
        text = " ".join(keywords) if keywords else question
        identities = {n.casefold() for a in accounts for n in
                      [a["account_id"],*a["observed_names"],*a.get("configured_names",[])]}
        for account in accounts:
            for name in [account["account_id"],*account["observed_names"],*account.get("configured_names",[])]:
                text = text.replace(name," ")
        import jieba
        terms = list(dict.fromkeys(w.strip() for w in jieba.cut(text) if w.strip() and w not in STOP and len(w.strip())>=2))
        # Preserve explicit phrases and names that the tokenizer splits.
        if keywords:
            terms = list(dict.fromkeys([*keywords,*terms]))
        terms = [t for t in terms if t.casefold() not in identities and t not in STOP]
        # Specific phrases get one vote; their tokenizer fragments are not extra evidence.
        terms = [t for t in terms if not any(t!=other and t.casefold() in other.casefold() for other in terms)]
        # Keep a single-character query usable, but do not let a broad character
        # dominate a query that also supplies meaningful phrases.
        if any(len(t)>1 for t in terms):
            terms = [t for t in terms if len(t)>1]
        return terms[:24]

    async def query(self, *, conversation, requester, question="", mode="lookup",
                    person="", person_role="about", keywords=None, time_range="",
                    start="", end="", cursor="", evidence_handle="", page_size=40,
                    now=None, exclude_uid="", read_scopes=None):
        self.calls += 1
        scopes = tuple(sorted(set(read_scopes or (conversation,))))
        if len(question)>4000 or len(person)>200 or len(keywords or [])>24 or any(not isinstance(k,str) or len(k)>160 for k in keywords or []):
            return {"status":"error","error":"query input exceeds bounded tool limits"}
        now = time.time() if now is None else now
        page_size = max(1,min(100,int(page_size)))
        if mode not in {"lookup","time_read","read","overview","style_sample"} or person_role not in {"speaker","about","any"}:
            return {"status":"error","error":"unsupported mode or person_role"}
        token = cursor or evidence_handle
        read_offset,text_offset,page_offset = 0,0,0
        if ":" in token:
            parts = token.split(":")
            token = parts[0]
            try:
                if mode=="read":
                    read_offset = int(parts[1])
                    text_offset = int(parts[2]) if len(parts)>2 else 0
                else:
                    page_offset = int(parts[1])
                if min(read_offset,text_offset,page_offset)<0:
                    raise ValueError()
            except ValueError:
                return {"status":"error","error":"invalid evidence_handle"}
        if token:
            snapshot = self.snapshots.get(token)
            if not snapshot or snapshot["expires"] < time.monotonic():
                return {"status":"invalidated","reason":"unknown_or_expired_cursor"}
            if snapshot["conversation"]!=conversation or snapshot["requester"]!=requester or snapshot["scopes"] != scopes:
                return {"status":"unavailable","reason":"cursor_scope_mismatch"}
            if mode=="read" and evidence_handle:
                if not 0<=read_offset<len(snapshot["rows"]):
                    return {"status":"unavailable","reason":"invalid_evidence_position"}
                return await self._page(snapshot,read_offset,1 if ":" in evidence_handle else page_size,full=True,text_offset=text_offset)
            return await self._page(snapshot,page_offset,page_size)
        if mode=="read":
            return {"status":"error","error":"read requires a returned evidence_handle"}
        if not question.strip():
            return {"status":"error","error":"query is empty"}
        try:
            if mode=='overview' and not (time_range or start or end):
                inferred = parse_overview_window(question,now)
                if inferred:
                    start,end = (datetime.fromtimestamp(t,TZ).isoformat() for t in (inferred.start,inferred.end))
            if mode=='style_sample' and not (time_range or start or end):
                time_range='最近30天'
            low,high = window_for(time_range,start,end,now)
        except ValueError as error:
            return {"status":"error","error":str(error)}
        if mode in {"time_read","overview"} and low is None:
            return {"status":"error","error":"time_read requires a time window"}
        if mode in {'overview','style_sample'} and (low is None or high-low>31*86400):
            return {'status':'error','error':'sampling requires a window of at most 31 days'}
        if low is not None and low >= high:
            return {'status':'error','error':'time window contains no past interval'}
        if mode=='overview' and person_role=='speaker':
            return {'status':'error','error':'overview includes all authors; use person to inspect activity, style_sample for author examples, or time_read for exact author filtering'}
        async with aiosqlite.connect(self.path,uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN")
            states = {}
            for scope in scopes:
                state = await conn.execute_fetchall("SELECT epoch,generation FROM archive_scopes WHERE conversation=?",(scope,))
                epoch, generation = tuple(state[0]) if state else (0, 0)
                watermark = (await conn.execute_fetchall("SELECT COALESCE(MAX(id),0) FROM archive_messages WHERE conversation=?",(scope,)))[0][0]
                states[scope] = (epoch, generation, watermark)
            watermark = max(state[2] for state in states.values())
            accounts,ambiguous,hint_status = ([],False,"not_used") if mode in {"time_read","overview"} and not person else await self._person(conn,scopes,person,question)
            resolved = {"original_question":question,"mode":mode,"conversation":conversation,
                        "person":person,"person_role":person_role,"accounts":accounts,
                        "time_start":datetime.fromtimestamp(low,TZ).isoformat() if low is not None else None,
                        "time_end_exclusive":datetime.fromtimestamp(high,TZ).isoformat(),
                        "timezone":"Asia/Shanghai","watermark":watermark,"scope_watermarks":{scope: state[2] for scope, state in states.items()},"conversations":scopes,"identity_hint":hint_status}
            if ambiguous:
                return {"status":"ambiguous","resolved_query":resolved}
            unresolved_person = bool(person and not accounts)
            resolved["identity_resolution"] = "literal_mentions_only_unresolved" if unresolved_person else "resolved_accounts" if accounts else "not_requested"
            if unresolved_person and person_role=="speaker":
                return {"status":"ambiguous","resolved_query":resolved,"note":"Cannot filter author by an unresolved nickname. Supply an account ID."}
            if unresolved_person and mode=='overview':
                return {'status':'ambiguous','resolved_query':resolved,
                        'note':'Cannot inspect activity for an unresolved person. Supply an account ID or omit person for a group overview.'}
            terms = self._terms(question,keywords or [],accounts) if mode=="lookup" else []
            ids = [a["account_id"] for a in accounts]
            if mode=='style_sample':
                if not person or len(ids)!=1:
                    return {'status':'ambiguous','resolved_query':resolved,'note':'style_sample requires one explicit, resolved person/account; no nickname guessing'}
                person_role = resolved['person_role'] = 'speaker'
            scope_conditions = []
            params = []
            for scope, (scope_epoch, _, scope_watermark) in states.items():
                scope_conditions.append("(conversation=? AND id<=? AND epoch=?)")
                params.extend((scope, scope_watermark, scope_epoch))
            clauses = ["(" + " OR ".join(scope_conditions) + ")", "(COALESCE(sent_at,saved_at)<? OR COALESCE(sent_at,saved_at) IS NULL)"]
            params.append(high)
            if low is not None:
                clauses.append("COALESCE(sent_at,saved_at)>=?")
                params.append(low)
            if mode in {'overview','style_sample'} and exclude_uid:
                clauses.append('uid<>?')
                params.append(exclude_uid)
            if person_role=="speaker" and ids:
                clauses.append("sender_id IN("+",".join("?" for _ in ids)+")")
                params.extend(ids)
            where = " AND ".join(clauses)
            available = (await conn.execute_fetchall("SELECT COUNT(*) FROM archive_messages WHERE "+where,params))[0][0]
            hit_reasons = {}
            bounded = False
            sampling = None
            if mode=='overview':
                rows,hit_reasons,sampling = await overview(conn,where,params,low,high,
                    max(40,page_size),ids[0] if len(ids)==1 else None)
                resolved['keywords_used'] = False
            elif mode=='style_sample':
                rows,hit_reasons,sampling = await style_sample(conn,where,params,low,high,
                    page_size,self.max_candidates)
                bounded = sampling['candidate_scan_bounded']
                resolved['keywords_used'] = False
            elif mode=="time_read":
                rows = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+where+" ORDER BY COALESCE(sent_at,saved_at),id LIMIT ?",[*params,self.max_results+1])
                bounded = len(rows)>self.max_results
                rows = rows[:self.max_results]
                for row in rows:
                    hit_reasons[row["uid"]] = ["time_window","speaker_filter" if person_role=="speaker" and ids else "time_window"]
            else:
                candidates = {}
                if terms:
                    # Exact substring route is independent of token cleaning/FTS.
                    literals = ["instr(lower(text),lower(?))>0" for _ in terms]
                    literal_person = " AND instr(text,?)>0" if unresolved_person else ""
                    literal_args = [person] if unresolved_person else []
                    anchors = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+where+" AND ("+" OR ".join(literals)+")"+literal_person+" ORDER BY COALESCE(sent_at,saved_at) DESC,id DESC LIMIT ?",[*params,*terms,*literal_args,self.max_candidates+1])
                    bounded = len(anchors)>self.max_candidates
                    for row in anchors[:self.max_candidates]:
                        candidates[row["uid"]] = row
                if ids:
                    related = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+where+" AND (sender_id IN("+",".join("?" for _ in ids)+") OR EXISTS(SELECT 1 FROM json_each(archive_messages.mentions) WHERE value IN("+",".join("?" for _ in ids)+")) OR EXISTS(SELECT 1 FROM json_each(archive_messages.components) WHERE json_extract(value,'$.type')='tool_output' AND json_extract(value,'$.trigger_sender_id') IN("+",".join("?" for _ in ids)+"))) ORDER BY COALESCE(sent_at,saved_at) DESC,id DESC LIMIT ?",[*params,*ids,*ids,*ids,self.max_candidates+1])
                    bounded = bounded or len(related)>self.max_candidates
                    for row in related[:self.max_candidates]:
                        candidates[row["uid"]] = row
                ranked = []
                for row in candidates.values():
                    body = row["text"]
                    matches = [t for t in terms if t.casefold() in body.casefold()]
                    mentions = json.loads(row["mentions"])
                    names = [n for a in accounts for n in a["observed_names"]]
                    output = tool_output_metadata(row)
                    trigger_hit = output is not None and output.get('trigger_sender_id') in ids
                    person_hit = row["sender_id"] in ids or any(a in mentions for a in ids) or any(n in body for n in names) or trigger_hit
                    if terms and not matches and not person_hit:
                        continue
                    reasons = ["topic:"+m for m in matches]
                    if terms and not matches:
                        reasons.append("person_context_candidate")
                    if row["sender_id"] in ids:
                        reasons.append("author")
                    if trigger_hit:
                        reasons.append("output_triggered_by")
                    if any(a in mentions for a in ids):
                        reasons.append("explicit_at")
                    if any(n in body for n in names):
                        reasons.append("literal_name_mention")
                    hit_reasons[row["uid"]] = reasons
                    score = 8*len(matches)+(12 if person_hit else 0)-(2 if row["role"]=="assistant" else 0)
                    ranked.append((score,row["id"],row))
                ranked.sort(key=lambda x:(-x[0],-x[1]))
                bounded = bounded or len(ranked)>24
                rows_by_uid = OrderedDict()
                context_groups = []
                for _,_,anchor in ranked[:24]:
                    rows_by_uid[anchor["uid"]] = anchor
                    clock = anchor["sent_at"] or anchor["saved_at"]
                    # Explicit reply relationships can retrieve short, keyword-free answers.
                    context_where = "conversation=? AND id<=? AND epoch=? AND (COALESCE(sent_at,saved_at)<? OR COALESCE(sent_at,saved_at) IS NULL)"
                    anchor_epoch, _, anchor_watermark = states[anchor["conversation"]]
                    context_params = [anchor["conversation"],anchor_watermark,anchor_epoch,high]
                    neighbors = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+context_where+" AND ((reply_key IS NOT NULL AND reply_key=?) OR (native_key IS NOT NULL AND native_key=?)) ORDER BY id LIMIT 24",[*context_params,anchor["native_key"],anchor["reply_key"]])
                    before,after = [],[]
                    if clock is not None:
                        before = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+context_where+" AND COALESCE(sent_at,saved_at)>=? AND (COALESCE(sent_at,saved_at)<? OR (COALESCE(sent_at,saved_at)=? AND id<=?)) ORDER BY COALESCE(sent_at,saved_at) DESC,id DESC LIMIT 9",[*context_params,clock-600,clock,clock,anchor["id"]])
                        after = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+context_where+" AND COALESCE(sent_at,saved_at)<=? AND (COALESCE(sent_at,saved_at)>? OR (COALESCE(sent_at,saved_at)=? AND id>?)) ORDER BY COALESCE(sent_at,saved_at),id LIMIT 8",[*context_params,clock+600,clock,clock,anchor["id"]])
                    # Immediate responses before incidental preceding chatter;
                    # explicit reply links retain their priority.
                    neighbors = [*neighbors,*after,*before]
                    direct = neighbors[:24]
                    for linked in direct[:4]:
                        if linked["native_key"] or linked["reply_key"]:
                            second = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE "+context_where+" AND ((reply_key IS NOT NULL AND reply_key=?) OR (native_key IS NOT NULL AND native_key=?)) ORDER BY id LIMIT 12",[*context_params,linked["native_key"],linked["reply_key"]])
                            neighbors.extend(second)
                    context_groups.append(neighbors)
                # Show the anchor diversity first, then give each discussion a turn.
                # A dense early thread must not bury every other thread's answer.
                for offset in range(max((len(g) for g in context_groups),default=0)):
                    for group in context_groups:
                        if offset<len(group):
                            row = group[offset]
                            rows_by_uid.setdefault(row["uid"],row)
                            hit_reasons.setdefault(row["uid"],["reply_or_neighbor_context"])
                rows = list(rows_by_uid.values())
            token = uuid.uuid4().hex
            snapshot = {"token":token,"conversation":conversation,"requester":requester,
                        "scopes":scopes,"states":states,"expires":time.monotonic()+900,
                        "position":0,"delivered":set(),"rows":[(r["uid"],r["revision"]) for r in rows],
                        "reasons":hit_reasons,"resolved":resolved,"available":available,
                        "bounded":bounded,"low":low,"high":high,"sampling":sampling}
            while len(self.snapshots)>=self.max_sessions:
                self.snapshots.popitem(last=False)
            self.snapshots[token] = snapshot
        return await self._page(snapshot,0,page_size)

    async def _page(self, snapshot, offset, size, full=False, text_offset=0):
        async with self.store.publication_lock:
            return await self._page_locked(snapshot,offset,size,full,text_offset)

    async def _page_locked(self, snapshot, offset, size, full=False, text_offset=0):
        conversation = snapshot["conversation"]
        async with aiosqlite.connect(self.path,uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN")
            if not await self._snapshot_valid(conn, snapshot):
                return {"status":"invalidated","reason":"source_changed_or_cleared"}
            selected = snapshot["rows"][offset:offset+size]
            messages,used = [],0
            for index,(uid,revision) in enumerate(selected,offset):
                predicate, scope_args = scope_sql("conversation", snapshot["scopes"])
                rows = await conn.execute_fetchall("SELECT * FROM archive_messages WHERE uid=? AND " + predicate,(uid,*scope_args))
                if not rows or rows[0]["revision"]!=revision:
                    return {"status":"invalidated","reason":"source_revision_changed"}
                row = rows[0]
                text = row["text"]
                own,quotes = author_parts(row) if snapshot.get('sampling') else (None,[])
                projection = 'original'
                if snapshot['resolved']['mode']=='style_sample' and not full:
                    text = own or ''
                    projection = 'author_plain_only'
                allocation = max(1,(self.result_chars-used)//max(1,len(selected)-(index-offset))) if snapshot.get('sampling') and not full else self.result_chars-used
                own_limit = min(600,allocation//3) if own is not None else 0
                quote_limit = min(150,allocation//max(6,6*len(quotes[:2])))
                quotes_truncated = len(quotes)>2 or any(len(q)>quote_limit for q in quotes[:2])
                quotes = [q[:quote_limit] for q in quotes[:2]]
                auxiliary_chars = (len(own[:own_limit]) if own else 0) + sum(map(len,quotes))
                limit = min(max(0,len(text)-text_offset),max(0,self.result_chars-used-auxiliary_chars),
                            10000 if full else min(600,max(1,allocation-auxiliary_chars)) if snapshot.get('sampling') else 2000)
                if limit <= 0 and len(text)>text_offset:
                    break
                clock = row["sent_at"] or row["saved_at"]
                next_offset = text_offset+limit
                messages.append({"source_uid":uid,"conversation":row["conversation"],"revision":revision,"evidence_handle":snapshot["token"]+":"+str(index)+":"+str(text_offset),
                    "next_text_handle":snapshot["token"]+":"+str(index)+":"+str(next_offset) if next_offset<len(text) else None,"text_offset":text_offset,"sender_id":row["sender_id"],
                    "empty_text":not text,
                    "sender_name":row["nickname"],"role":row["role"],"time":datetime.fromtimestamp(clock,TZ).isoformat() if clock else None,
                    "time_quality":row["time_quality"],"source_state":row["state"],"text":text[text_offset:next_offset],"truncated":len(text)>next_offset,
                    "hit_reasons":snapshot["reasons"].get(uid,[]),"mentions":json.loads(row["mentions"]),
                    "context_only":bool("person_context_candidate" in snapshot["reasons"].get(uid,[]) or (snapshot["low"] is not None and (clock is None or not snapshot["low"]<=clock<snapshot["high"])) or
                        (snapshot["resolved"]["person_role"]=="speaker" and snapshot["resolved"]["accounts"] and row["sender_id"] not in [a["account_id"] for a in snapshot["resolved"]["accounts"]])),
                    "reply_native_key":row["reply_key"], "tool_output":tool_output_metadata(row)})
                if snapshot.get('sampling'):
                    messages[-1].update({'text_projection':projection,
                        'author_plain':own[:own_limit] if own is not None else None,
                        'author_plain_truncated':own is not None and len(own)>own_limit,
                        'quoted_context':quotes,
                        'quoted_context_truncated':quotes_truncated,
                        'original_evidence_handle':snapshot['token']+':'+str(index)+':0'})
                    # Expansion offsets refer to originals, never to the shorter
                    # Plain projection. Original read has its own continuation.
                    if projection!='original':
                        messages[-1]['next_text_handle']=None
                used += limit + auxiliary_chars
        if not await self._snapshot_valid(self.store.read_connection, snapshot):
            return {"status":"invalidated","reason":"changed_before_publish"}
        position = offset+len(messages)
        snapshot["delivered"].update(range(offset,position))
        more = not full and position<len(snapshot["rows"])
        sampling = snapshot.get('sampling')
        if sampling:
            sampling = {**sampling,'delivered_strata':sorted({int(reason.split('-')[1])
                for m in messages for reason in m['hit_reasons'] if reason.startswith('stratum-')}),
                'delivered_source_uids':[m['source_uid'] for m in messages]}
        return {"status":"partial" if snapshot["bounded"] or more else "ok" if messages else "no_support_found",
                "resolved_query":snapshot["resolved"],"messages":messages,
                "evidence_handle":snapshot["token"],"cursor":snapshot["token"]+":"+str(position) if more else None,
                "has_more":more,"processing_coverage":{"available_in_snapshot":snapshot["available"],
                "selected_evidence":len(snapshot["rows"]),"returned_this_call":len(messages),"cumulative_distinct_messages_returned":len(snapshot["delivered"]),
                "truncated_texts_this_call":sum(m["truncated"] for m in messages),"bounded_candidates":snapshot["bounded"]},
                "sampling":sampling,
                "answer_scope_note":("跨时段原文样本，不保证发现全部话题；已有证据足以回答可停止，缺少具体讨论时缩窄时段或关键词查证。模仿只采用author_plain，引用与旁人评价不是本人语气。无发言记录不等于离线。") if sampling else ("此时段保留"+str(snapshot["available"])+"条消息，本次查询累计展示"+str(len(snapshot["delivered"]))+"条。证据足以回答可停止；不把已读片段说成完整统计。")
                    if snapshot["resolved"]["mode"]=="time_read" and len(snapshot["delivered"])<snapshot["available"] else "原文仅证明当时说过什么；部分长消息需继续展开，不保证现实事实或原群历史完整。",
                "archive_coverage":{"complete_capture":False,"note":"Retained records only; legacy time is save time; duplicates may exist. No support found does not prove an event never occurred."},
                "interpretation":"Authors, At and Reply are relationships, not proof of fact ownership. tool_output identifies the triggering user and target conversation, not an individual recipient or reader. send_call_returned records submitted content after the send API returned normally; it is not a platform message receipt. A person_context_candidate only shares identity, not proven topic relevance. Bot statements are not independent confirmation. Partial time_read covers only returned pages; continue with cursor or explicitly state the limited reading range."}


    @staticmethod
    async def _snapshot_valid(conn, snapshot):
        for scope, (epoch, generation, _) in snapshot["states"].items():
            state = await conn.execute_fetchall("SELECT epoch,generation FROM archive_scopes WHERE conversation=?", (scope,))
            if (tuple(state[0]) if state else (0, 0)) != (epoch, generation):
                return False
        return True

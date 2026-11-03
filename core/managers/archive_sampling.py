"""Bounded original-message sampling, without model calls or derived memories."""
from __future__ import annotations

import json
import math
import re
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
CLOCK = 'COALESCE(sent_at,saved_at)'


def iso(stamp):
    return datetime.fromtimestamp(stamp, TZ).isoformat()


def author_parts(row):
    """Native Plain is submitted by this author; it may still be copied prose."""
    parts = json.loads(row['components'])
    if not parts:
        return None, []
    plain = '\n'.join(p.get('text', '') for p in parts if p.get('type') == 'plain').strip()
    quotes = [p.get('text', '') for p in parts if p.get('type') == 'reply' and p.get('text')]
    return plain, quotes


async def overview(conn, where, params, low, high, budget, activity_account):
    """Time strata contain contiguous windows, not keyword-confirming hits.

    At default page size the first page covers every nonempty stratum. Extra
    windows are allocated by message count, then placed at ordinal quantiles.
    Only small candidate windows are materialized, even for large archives.
    """
    raw_count=(await conn.execute_fetchall('SELECT COUNT(*) FROM archive_messages WHERE '+where,params))[0][0]
    # Working-store and retained-memory imports can preserve the exact same
    # assistant output twice. Collapse only indistinguishable legacy copies in
    # this scouting view; native messages and canonical archive rows stay intact.
    prefix=('WITH scoped AS (SELECT * FROM archive_messages WHERE '+where+'), ranked AS ('
        'SELECT *,ROW_NUMBER() OVER (PARTITION BY sender_id,role,'+CLOCK+',text,'
        "CASE WHEN native_key IS NULL AND time_quality='legacy_saved' AND "+CLOCK+
        " IS NOT NULL AND sender_id NOT IN ('','unknown','user','assistant') THEN '' ELSE uid END ORDER BY id) AS copy_rank FROM scoped), "
        'sampling_pool AS (SELECT * FROM ranked WHERE copy_rank=1) ')
    scoped_params=list(params)
    async def fetch(sql,args=()):
        return await conn.execute_fetchall(prefix+sql,[*scoped_params,*args])
    where,params='1=1',[]
    pool_count=(await fetch('SELECT COUNT(*) FROM sampling_pool'))[0][0]
    width = max((high-low)/8, 1)
    strata = []
    for index in range(8):
        a, b = low+index*width, min(high, low+(index+1)*width)
        if a >= high:
            break
        bucket_where = where + f' AND {CLOCK}>=? AND {CLOCK}<?'
        bucket_params = [*params, a, b]
        count, speakers = (await fetch(
            'SELECT COUNT(*),COUNT(DISTINCT sender_id) FROM sampling_pool WHERE '+bucket_where,
            bucket_params))[0]
        strata.append({'index': index, 'start': iso(a), 'end_exclusive': iso(b),
                       'available_messages': count, 'speakers': speakers,
                       '_where': bucket_where, '_params': bucket_params})
    active = [s for s in strata if s['available_messages']]
    seed_size = min(5, max(1, budget//max(1,len(active))))
    for s in active:
        s['_sizes'] = [min(seed_size,s['available_messages'])]
    used = sum(sum(s['_sizes']) for s in active)
    while used < budget:
        expandable = [s for s in active if sum(s['_sizes']) < s['available_messages']
            and min(3,s['available_messages']-sum(s['_sizes']))<=budget-used]
        if not expandable:
            break
        winner = max(expandable, key=lambda s: s['available_messages']/(len(s['_sizes'])+1))
        extra = min(3,winner['available_messages']-sum(winner['_sizes']))
        used += extra
        winner['_sizes'].append(extra)
    selected, windows, seen = [], [], set()
    for s in active:
        count, sizes = s['available_messages'], s['_sizes']
        ordinal_weight = 0
        for window_size in sizes:
            # Partition by message ordinal so dense bursts cannot hide all
            # quieter discussions; choose a meaningful center within each part.
            left = count*ordinal_weight//sum(sizes)
            ordinal_weight += window_size
            right = count*ordinal_weight//sum(sizes)
            probe_count = min(15, right-left)
            offset = left + max(0, (right-left-probe_count)//2)
            probe = await fetch('SELECT * FROM sampling_pool WHERE '+s['_where']+
                f' ORDER BY {CLOCK},id LIMIT ? OFFSET ?', [*s['_params'],probe_count,offset])
            if not probe:
                continue
            def usefulness(row):
                own, _ = author_parts(row)
                body = own if own is not None else row['text']
                return min(len(body.strip()),160) - (80 if len(body)>2000 else 0) - (30 if row['role']=='assistant' else 0)
            center = max(range(len(probe)), key=lambda i: (usefulness(probe[i]), -abs(i-len(probe)//2)))
            begin = max(left,min(max(left,right-window_size), offset+center-window_size//2))
            rows = await fetch('SELECT * FROM sampling_pool WHERE '+s['_where']+
                f' ORDER BY {CLOCK},id LIMIT ? OFFSET ?', [*s['_params'], min(window_size,right-left),begin])
            rows = [r for r in rows if r['uid'] not in seen]
            if not rows:
                continue
            key = 'window-'+str(len(windows))
            for row in rows:
                seen.add(row['uid'])
                selected.append((row,key))
            windows.append({'window_id':key,'stratum':s['index'],
                'start':iso(rows[0]['sent_at'] or rows[0]['saved_at']),
                'end_exclusive':iso(min(high,(rows[-1]['sent_at'] or rows[-1]['saved_at'])+0.001)),
                'suggested_context_start':iso(max(low,(rows[0]['sent_at'] or rows[0]['saved_at'])-300)),
                'suggested_context_end_exclusive':iso(min(high,(rows[-1]['sent_at'] or rows[-1]['saved_at'])+300)),
                'source_uids':[r['uid'] for r in rows]})
    activity = None
    if activity_account:
        posts = await fetch('SELECT '+CLOCK+' AS stamp FROM sampling_pool WHERE '+where+
            ' AND sender_id=? ORDER BY stamp,id',[*params,activity_account])
        stamps = [r[0] for r in posts]
        boundaries = [low,*sorted(set(stamps)),high]
        candidates = sorted(((a,b) for a,b in zip(boundaries,boundaries[1:]) if b>a),
                            key=lambda pair:pair[0]-pair[1])
        gaps = []
        for a,b in candidates[:12]:
            if b<=a:
                continue
            # A query boundary is inclusive; an observed author's post is not.
            inclusive = a==low and a not in stamps
            n = (await fetch('SELECT COUNT(*) FROM sampling_pool WHERE '+where+
                f' AND {CLOCK}{">=" if inclusive else ">"}? AND {CLOCK}<?',[*params,a,b]))[0][0]
            gaps.append({'start':iso(a),'start_inclusive':inclusive,'end_exclusive':iso(b), 'other_messages':n,
                         'seconds':round(b-a,3)})
        gaps.sort(key=lambda g:-g['seconds'])
        activity = {'account_id':activity_account,'observed_posts':len(stamps),
            'count_basis':'sampling view with exact legacy content copies collapsed',
            'recent_post_times':[iso(t) for t in stamps[-32:]],'post_times_truncated':len(stamps)>32,
            'no_post_intervals':gaps,'intervals_truncated':len(candidates)>12,
            'note':'Intervals show no retained posts, not offline status or unread messages. No interval is automatically chosen as the requested absence.'}
    by_window = {w['window_id']:w['stratum'] for w in windows}
    return [r for r,_ in selected], {r['uid']:['stratified_window',key,'stratum-'+str(by_window[key])] for r,key in selected}, {
        'method':'time_stratified_contiguous_windows',
        'raw_retained_records':raw_count,'sampling_rows':pool_count,
        'collapsed_legacy_copies':raw_count-pool_count,
        'strata':[{k:v for k,v in s.items() if not k.startswith('_')} for s in strata],
        'windows':windows,'person_activity':activity,
        'note':'These are scouting samples, not a complete topic list. Suggested context bounds pad the sample by five minutes, not inferred event boundaries. Use time_read on a selected context window if needed.'}


async def style_sample(conn, where, params, low, high, budget, max_candidates):
    """Sample only one resolved author's own Plain across time and bursts."""
    buckets, inspected, eligible, excluded, bounded = [], 0, 0, {}, False
    width = max((high-low)/8,1)
    quota = max(8,math.ceil(max_candidates/8))
    for index in range(8):
        a,b=low+index*width,min(high,low+(index+1)*width)
        if a>=high:
            break
        predicate=where+f' AND {CLOCK}>=? AND {CLOCK}<?'
        args=[*params,a,b]
        # Filter the eligible pool BEFORE ordinal sampling. Otherwise periodic
        # image/Plain alternation can make a fixed stride select only images.
        classified = ("WITH authored AS (SELECT *,CASE WHEN json_array_length(components)>0 "
            "THEN trim(COALESCE((SELECT group_concat(json_extract(value,'$.text'),char(10)) FROM json_each(components) "
            "WHERE json_extract(value,'$.type')='plain'),''),char(9)||char(10)||char(13)||' ') END AS author_text "
            'FROM archive_messages WHERE '+predicate+"), classified AS (SELECT *,CASE "
            "WHEN author_text IS NULL THEN 'no_native_author_boundary' WHEN author_text='' THEN 'no_plain_text' "
            "WHEN author_text GLOB '[./!#。！／][A-Za-z]*' THEN 'command_like' END AS exclusion FROM authored) ")
        counts=await conn.execute_fetchall(classified+'SELECT exclusion,COUNT(*) FROM classified GROUP BY exclusion',args)
        count=0
        for reason,n in counts:
            if reason is None:
                count=n
            else:
                excluded[reason]=excluded.get(reason,0)+n
        eligible+=count
        # ROW_NUMBER keeps candidate inspection distributed within each bucket,
        # rather than silently taking only its earliest or latest messages.
        rows=await conn.execute_fetchall(classified+', ordered AS (SELECT *,ROW_NUMBER() OVER (ORDER BY '+CLOCK+
            ',id)-1 AS ordinal FROM classified WHERE exclusion IS NULL'+
            ') SELECT * FROM ordered WHERE ordinal % ? = 0 ORDER BY ordinal LIMIT ?',
            [*args,max(1,math.ceil(count/quota)),quota])
        bounded=bounded or count>len(rows)
        candidates=[]
        for row in rows:
            inspected+=1
            own,_=author_parts(row)
            reason=None
            if own is None: reason='no_native_author_boundary'
            elif not own: reason='no_plain_text'
            elif re.match(r'^[./!#。！／][A-Za-z]',own): reason='command_like'
            if reason:
                excluded[reason]=excluded.get(reason,0)+1
                continue
            candidates.append((row,own))
        # Round-robin alone would take only each bucket's oldest prefix. Start
        # at both temporal ends, then bisect interiors, so recent speech is not
        # hidden inside a busy multi-day bucket.
        ordered=[]
        if candidates:
            ordered.append(candidates[0])
        if len(candidates)>1:
            ordered.append(candidates[-1])
        intervals=deque([(1,len(candidates)-2)])
        while intervals:
            left,right=intervals.popleft()
            if left>right:
                continue
            middle=(left+right)//2
            ordered.append(candidates[middle])
            intervals.extend(((left,middle-1),(middle+1,right)))
        buckets.append(ordered)
    chosen,seen,bursts=[],set(),{}
    for depth in range(max((len(b) for b in buckets),default=0)):
        for bucket in buckets:
            if depth>=len(bucket):
                continue
            row,own=bucket[depth]
            normalized=re.sub(r'\s+',' ',own).strip()
            burst=int((row['sent_at'] or row['saved_at'])//600)
            if normalized in seen or bursts.get(burst,0)>=2:
                continue
            seen.add(normalized)
            bursts[burst]=bursts.get(burst,0)+1
            chosen.append(row)
            if len(chosen)>=min(budget,32):
                break
        if len(chosen)>=min(budget,32):
            break
    return chosen,{r['uid']:['author_plain_style_example'] for r in chosen},{
        'method':'author_plain_temporal_sample','inspected_candidates':inspected,
        'eligible_candidates':eligible,'excluded_candidates':excluded,'candidate_scan_bounded':bounded,
        'filter_counts_scope':'entire requested author/time window; inspected_candidates counts materialized eligible rows',
        'note':'Plain proves submitted text, not original composition or stable personality. Short replies are retained; exact repeats and a single burst cannot fill the sample. Quotes are context only.'}

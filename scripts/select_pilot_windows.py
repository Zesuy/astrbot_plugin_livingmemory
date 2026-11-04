import sqlite3,json,re,hashlib,os
from datetime import datetime,timezone,timedelta
import argparse
parser=argparse.ArgumentParser(description="Select private local pilot slices from an existing read-only snapshot")
parser.add_argument("directory",help="private directory containing production-conversations-snapshot.db")
args=parser.parse_args()
p=os.path.abspath(args.directory)+os.sep
c=sqlite3.connect("file:"+p+"production-conversations-snapshot.db?mode=ro",uri=True);c.row_factory=sqlite3.Row
bots={r[0] for r in c.execute("select distinct sender_id from archive_messages where role='assistant'")}
bots.update(["3658276025","3778698314","3829158339","3768485245","3162054915"])
rows=c.execute("select * from archive_messages where role='user' and sent_at is not null order by conversation,sent_at,id").fetchall()
windows=[]; current=[]
for r in rows:
 r=dict(r)
 if r["sender_id"] in bots or not r["sender_id"].isdigit():continue
 parts=json.loads(r["components"])
 text=" ".join(str(x.get("text","")) for x in parts if str(x.get("type","")).lower()=="plain").strip()
 if not text or re.fullmatch(r"(?:\[[^\]]+\]\s*)+",text):continue
 r["text"]=text
 if current and (r["conversation"]!=current[-1]["conversation"] or r["sent_at"]-current[-1]["sent_at"]>=1800 or datetime.fromtimestamp(r["sent_at"],timezone(timedelta(hours=8))).date()!=datetime.fromtimestamp(current[-1]["sent_at"],timezone(timedelta(hours=8))).date() or len(current)>=30):
  if len(current)>=20:windows.append(current)
  current=[]
 current.append(r)
if len(current)>=20:windows.append(current)
patterns={"identity":r"真名|原名|叫我|我叫|不是.{1,8}是|外号|简称","technical":r"模型|显卡|内存|Linux|OpenWrt|API|代码|部署|gemini|GPT|token|cpu","game":r"游戏|抽卡|明日方舟|原神|王者|主玩"}
def tags(w):
 text=" ".join(r["text"] for r in w)
 return [k for k,v in patterns.items() if re.search(v,text,re.I)] or ["daily"]
chosen=[]
for category,n in [("identity",10),("technical",15),("game",10),("daily",15)]:
 pool=[w for w in windows if category in tags(w) and w not in chosen]
 for i in range(min(n,len(pool))):chosen.append(pool[int(i*len(pool)/min(n,len(pool)))])
for w in windows:
 if len(chosen)>=50:break
 if w not in chosen:chosen.append(w)
chosen.sort(key=lambda w:(w[0]["sent_at"],w[0]["id"]))
out={"source":"production-conversations-snapshot.db","rules":{"day_timezone":"Asia/Shanghai","silence_seconds":1800,"max_messages":30,"min_messages":20,"selection":"keyword-stratified deterministic spread; not a random quality benchmark","bot_ids":sorted(bots)},"candidate_windows":len(windows),"windows":[{"id":i+1,"tags":tags(w),"messages":w} for i,w in enumerate(chosen)]}
dest=p+"pilot_slices.json"
fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w",encoding="utf-8") as f:json.dump(out,f,ensure_ascii=False)
print(json.dumps({"windows":len(chosen),"messages":sum(map(len,chosen)),"candidate_windows":len(windows),"path":dest,"sha256":hashlib.sha256(open(dest,"rb").read()).hexdigest()}))


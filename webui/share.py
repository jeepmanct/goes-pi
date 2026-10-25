"""Public read-only page: latest GOES views behind an unguessable link.

Started from app.py with start(frames_fn, gif_fn). Listens on 127.0.0.1 only; expose it with
`tailscale funnel`. The token lives in ~/goes/share-token.txt (delete the file and restart to rotate).
Each link has its own token (managed from the panel); visits are counted per link.
Only the whitelisted views below are served, only as JPEG/GIF, and nothing here can write or control anything.
"""
import datetime as dt
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_FILE = Path.home() / "goes" / "share-token.txt"          # legacy single token (migrated)
LINKS_FILE = Path.home() / "goes" / "share-tokens.json"        # {"links": [{id, name, email, token, created}]}
VISITS_FILE = Path.home() / "goes" / "share-visits.json"       # {id: {total, last, days: {YYYY-MM-DD: n}}}
PORT = 8090
VIEWS = {  # name -> label
    "stratford": "Stratford and the Sound", "nyc": "New York City", "northeast": "Northeast US", "fog_northeast": "Northeast fog (night)",
    "eastus": "East Coast", "namer": "North America", "gulf": "Gulf of Mexico",
    "samer": "South America", "globe": "Full disk globe",
    "wv_namer": "Water vapor, North America", "wv_globe": "Water vapor, full disk",
    "ct_stratford": "Cloud tops, Stratford", "ct_northeast": "Cloud tops, Northeast US",
    "fog_stratford": "Stratford fog (night)", "meso1": "Mesoscale 1 (1-minute)",
}
MESO_MAX = 60       # mesoscale frames are about a minute apart, so the player only gets the newest few
HOURS = 24          # Views player + GIF window
GALLERY_HOURS = 72  # how far back the Gallery goes
THUMBS = Path.home() / "goes" / "webui" / "thumbs" / "share"
_frames_fn = _gif_fn = _forecast_fn = _icon = None
_wx = None            # (local module, products module) for the Weather tab; None turns the tab's data off
_wx_cache = {}
_cache = {}
_lock = threading.Lock()


_links_cache = {"mtime": None, "links": []}
_visits = {}
_visits_dirty = [0.0]


def _read_links():
    try:
        return json.loads(LINKS_FILE.read_text()).get("links", [])
    except (OSError, ValueError):
        return None


def _write_links(links):
    tmp = LINKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"links": links}, indent=1) + "\n")
    tmp.chmod(0o600)
    tmp.replace(LINKS_FILE)


def links():
    """All links, re-read when the file changes. Creates the first one (migrating the old single token)."""
    with _lock:
        links = _read_links()
        if links is None:
            tok = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
            if len(tok) < 20:
                tok = secrets.token_urlsafe(24)
            links = [{"id": secrets.token_hex(4), "name": "Everyone", "email": "", "token": tok,
                      "created": int(time.time())}]
            _write_links(links)
        return links


def link_for(tok):
    found = None
    for l in links():                       # compare against every token so timing doesn't reveal a match
        if hmac.compare_digest(l["token"], tok):
            found = l
    return found


def _new_id(ls):
    while True:
        i = secrets.token_hex(4)
        if all(l["id"] != i for l in ls):
            return i


def add_link(name, email=""):
    name, email = str(name).strip()[:60], str(email).strip()[:120]
    if not name:
        raise ValueError("give the link a name")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("that email address doesn't look valid")
    with _lock:
        ls = links_unlocked()
        if len(ls) >= 25:
            raise ValueError("at most 25 links")
        if email and any(l["email"].lower() == email.lower() for l in ls):
            raise ValueError("that email already has a link")
        ls.append({"id": _new_id(ls), "name": name, "email": email, "token": secrets.token_urlsafe(24),
                   "created": int(time.time())})
        _write_links(ls)
    return ls[-1]


def links_unlocked():
    return _read_links() or []


def rotate_link(lid):
    with _lock:
        ls = links_unlocked()
        for l in ls:
            if l["id"] == lid:
                l["token"] = secrets.token_urlsafe(24)
                _write_links(ls)
                return l
    raise ValueError("no such link")


def revoke_link(lid):
    with _lock:
        ls = links_unlocked()
        if len(ls) <= 1 and ls and ls[0]["id"] == lid:
            raise ValueError("keep at least one link (rotate it instead)")
        n = [l for l in ls if l["id"] != lid]
        if len(n) == len(ls):
            raise ValueError("no such link")
        _write_links(n)
        _visits.pop(lid, None)
        _flush_visits(force=True)


def _load_visits():
    try:
        _visits.update(json.loads(VISITS_FILE.read_text()))
    except (OSError, ValueError):
        pass


def _flush_visits(force=False):
    if not force and time.time() - _visits_dirty[0] < 30:
        return
    try:
        tmp = VISITS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_visits))
        tmp.replace(VISITS_FILE)
        _visits_dirty[0] = time.time()
    except OSError:
        pass


def count_visit(lid):
    day = dt.date.today().isoformat()
    with _lock:
        v = _visits.setdefault(lid, {"total": 0, "last": 0, "days": {}})
        v["total"] += 1
        v["last"] = int(time.time())
        v["days"][day] = v["days"].get(day, 0) + 1
        for d in sorted(v["days"])[:-60]:
            del v["days"][d]
        _flush_visits()


def summary(base_url=""):
    """For the panel: every link with its URL and visit numbers."""
    wk = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    out = []
    for l in links():
        v = _visits.get(l["id"], {})
        out.append({"id": l["id"], "name": l["name"], "email": l["email"], "created": l["created"],
                    "path": f"/s/{l['token']}/", "total": v.get("total", 0), "last": v.get("last", 0),
                    "week": sum(n for d, n in v.get("days", {}).items() if d >= wk)})
    return out


def frames(view):
    """[(epoch, path)] for the last GALLERY_HOURS hours, oldest first (cached 60 s)."""
    with _lock:
        c = _cache.get(view)
        if c and time.time() - c[0] < 60:
            return c[1]
    now = dt.datetime.now(dt.timezone.utc)
    fr = [(t.timestamp(), p) for t, p in _frames_fn(view) if now - t <= dt.timedelta(hours=GALLERY_HOURS)]
    with _lock:
        _cache[view] = (time.time(), fr)
    return fr


def thumb(p, width=360):
    from PIL import Image
    THUMBS.mkdir(parents=True, exist_ok=True)
    out = THUMBS / f"{p.stem}_{width}.jpg"
    if not out.exists():
        im = Image.open(p).convert("RGB")
        im.thumbnail((width, width * 2))
        tmp = out.with_suffix(".tmp")
        im.save(tmp, "JPEG", quality=78)
        tmp.replace(out)
    return out


HOW = """
<h2>The short version</h2>
<p>A weather satellite 36,000 km above the equator photographs the Earth every few minutes, sends the pictures
down to a NOAA ground station, and NOAA sends selected pictures back up to the satellite, which rebroadcasts them
to anyone with a dish. This page shows what a small dish in the Northeast US picked up from that rebroadcast.</p>
<div class="flow"><span>GOES-19 satellite</span><i>&rarr;</i><span>NOAA ground station</span><i>&rarr;</i><span>back up to GOES-19</span><i>&rarr;</i>
<span>home dish</span><i>&rarr;</i><span>Raspberry Pi</span><i>&rarr;</i><span>this page</span></div>

<h2>1. The satellite takes the pictures</h2>
<p>GOES-19 (GOES East) sits in geostationary orbit at 75&deg;W, about 35,786 km up, over the equator. At that height one
orbit takes exactly a day, so it stays over the same spot and watches the Americas and the Atlantic constantly.
Its main camera, the Advanced Baseline Imager (ABI), sees 16 colours of light (visible and infrared) and scans the
whole visible disk of the Earth roughly every 10 minutes. Infrared channels also work at night, because they
measure heat rather than reflected sunlight: cold cloud tops look bright, warm ground looks dark.</p>

<h2>2. Down to the ground station</h2>
<p>The raw instrument data (far more than a home dish could receive) is sent down by radio in the X-band to NOAA&rsquo;s
Command and Data Acquisition Station at Wallops Island, Virginia, with a backup site in West Virginia. From there it
goes by fibre to NOAA&rsquo;s satellite operations centre in Suitland, Maryland.</p>

<h2>3. Processing on the ground</h2>
<p>At NOAA the raw data is calibrated, corrected for the satellite&rsquo;s position and turned into finished image products.
Some of it goes out to scientists and forecasters as the high-rate &ldquo;GOES Rebroadcast&rdquo;. A smaller, friendlier
selection is cut into small chunks, compressed and packaged for the low-rate broadcast that hobbyists can receive.
Weather bulletins from the National Weather Service (forecast discussions, warnings and more) are packaged into the
same stream.</p>

<h2>4. Back up to the satellite</h2>
<p>NOAA sends those packages up to GOES-19, and the satellite re-transmits them to the whole hemisphere on
1694.1 MHz (L-band) as <b>HRIT</b> (High Rate Information Transmission, about 400 kbit/s) and <b>EMWIN</b> (the Emergency
Managers Weather Information Network). This is a one-way broadcast: anyone with the right antenna can listen, and listening
costs nothing and leaves no trace. Only some channels are included, which is why you see infrared and one visible
channel here but not a full-colour picture.</p>

<h2>5. The home station</h2>
<ol>
<li><b>Dish.</b> A parabolic dish collects the very faint signal (about a billionth of a billionth of a watt) and focuses it on a feed.</li>
<li><b>Filter and amplifier.</b> A SAWbird+ GOES module right behind the feed filters out cell and Wi-Fi noise and boosts the
signal before it gets lost in cable noise.</li>
<li><b>Software-defined radio.</b> A USB RTL-SDR stick digitises the radio signal into numbers.</li>
<li><b>Raspberry Pi.</b> A program called <i>goesrecv</i> demodulates the digital signal and fixes transmission errors with
two error-correcting codes (Viterbi and Reed-Solomon). A second program, <i>goesproc</i>, reassembles the chunks into complete
images and weather text files.</li>
<li><b>Map making.</b> A script projects the satellite&rsquo;s view onto a map, draws coastlines and borders, and crops regional
views such as New York and the Northeast. The &ldquo;fog&rdquo; view compares two infrared channels at night, where fog and low cloud show up in aqua.
The &ldquo;cloud tops&rdquo; views colour the infrared channel by cloud-top temperature, so the coldest, highest storm tops stand out in red and magenta.
The &ldquo;mesoscale&rdquo; view is a small box the satellite aims at active weather and refreshes about once a minute; its location changes as the weather moves.
The colour pictures are synthesised from the visible and infrared channels, so greens and blues are an approximation, not a photo.</li>
</ol>

<h2>6. Getting it to you</h2>
<p>A small web server on the Pi serves these read-only pages through an encrypted Tailscale tunnel, so the Pi never has to open
a port on the home router. The link you used is private to the people it was sent to. Nothing you do here can control the
station, and only the pictures shown are exposed.</p>

<h2>Good to know</h2>
<ul>
<li>Times are shown in your device&rsquo;s local time. Pictures lag the actual scene by roughly 15&ndash;30 minutes because of
the trip up, down and around.</li>
<li>Gaps happen: rain or snow on the dish, a power or internet blip, or the satellite&rsquo;s own schedule can drop images.</li>
<li>Imagery &copy; NOAA/NESDIS (public domain), received and processed at home. Forecast text is from the National Weather Service.</li>
</ul>
"""


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<link rel="manifest" href="%BASE%manifest.webmanifest"><link rel="apple-touch-icon" href="%BASE%icon.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="GOES">
<meta name="theme-color" content="#0e1828"><title>GOES-19 Satellite</title><style>
:root{color-scheme:light dark;--bg:#fff;--fg:#14202e;--mut:#667;--card:#f1f4f8;--acc:#1f6feb}
@media(prefers-color-scheme:dark){:root{--bg:#0e1828;--fg:#e6edf5;--mut:#98a6b8;--card:#17253a;--acc:#58a6ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 4px}p{color:var(--mut);margin:0 0 12px;font-size:14px}
.card{background:var(--card);border-radius:10px;padding:10px}select,button,a.b{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid #8884;background:var(--bg);color:var(--fg);text-decoration:none}
img{width:100%;display:block;border-radius:8px;background:#000;margin-top:10px}
#bar{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
input[type=range]{flex:1 1 120px;min-width:0}#t{font-variant-numeric:tabular-nums;margin-left:auto;white-space:nowrap;font-size:14px}
#tabs{display:flex;gap:8px;margin-bottom:10px}#tabs .on{background:var(--acc);color:#fff;border-color:var(--acc)}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:10px}
#grid div{cursor:pointer;background:var(--bg);border-radius:8px;overflow:hidden;font-size:12px;line-height:1.35}
#grid img{margin:0;border-radius:0;aspect-ratio:1;object-fit:cover}#grid span{display:block;padding:6px 8px}
#lb{position:fixed;inset:0;background:#000d;z-index:9;overflow:auto;text-align:center;padding:12px;box-sizing:border-box}
#lb img{max-width:100%;max-height:80vh;width:auto;display:inline-block}#lb div{margin-top:10px}
@media(min-width:900px){#im{max-height:78vh;object-fit:contain}#grid{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}}
button,a.b,select{cursor:pointer;min-height:40px}
#how h2{font-size:18px;margin:18px 0 4px}#how h2:first-child{margin-top:4px}#how p,#how li{color:var(--fg);font-size:15px;margin:6px 0}
#how ol{padding-left:22px}#how .flow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px;margin:8px 0}
#how .flow span{background:var(--bg);border:1px solid #8884;border-radius:8px;padding:5px 9px}#how .flow i{color:var(--mut);font-style:normal}
#stale{padding:10px 12px;border-radius:8px;margin-bottom:10px;font-size:14px;background:#d9770622;border:1px solid #d97706}
#fc h2{font-size:18px;margin:14px 0 2px}#fc h3{font-size:14px;margin:12px 0 2px;text-transform:uppercase;letter-spacing:.03em;color:var(--mut)}
#fc p{color:var(--fg);font-size:15px;margin:5px 0}#fc .iss{color:var(--mut);font-size:13px}
#wx h2{font-size:18px;margin:16px 0 2px}#wx .iss{color:var(--mut);font-size:13px}#wx .big{font-size:28px;font-weight:700;margin:4px 0}
#wx p{color:var(--fg);font-size:15px;margin:5px 0}#wx pre{background:var(--bg);border-radius:8px;padding:8px;overflow:auto;font-size:13px}
#wx .al{padding:10px 12px;border-radius:8px;font-size:15px;font-weight:600}#wx .al.ok{background:#2ea04322;border:1px solid #2ea043}
#wx .al.bad{background:#da363322;border:1px solid #da3633}#wx #cbar{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
#hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 4px}#hd h1{margin:0}
#th{font-size:12px;padding:3px 11px;min-height:0;border-radius:99px;color:var(--mut)}
#v,#gv{width:100%;max-width:100%;min-width:0;box-sizing:border-box;text-overflow:ellipsis}
#gif{font-size:12px;padding:3px 11px;min-height:0;line-height:1.5;border-radius:99px;color:var(--mut)}#th.on{background:var(--acc);color:#fff;border-color:var(--acc)}
#tabs{overflow-x:auto}#tabs button{white-space:nowrap}
</style></head><body><main><div id="hd"><h1>GOES-19 East satellite</h1><button id="th">How it works</button></div>
<p>Live images from a home-built receiver. Updated every few minutes; showing the last 24 hours.<span id="lu"></span></p>
<div id="stale" style="display:none"></div>
<div id="tabs"><button id="tv" class="on">Views</button><button id="tw">Weather</button><button id="tg">Gallery</button><button id="tf">Forecast</button></div>
<div class="card" id="views"><select id="v">%OPTS%</select>
<img id="im" alt="GOES-19 view"><div id="bar"><button id="play">Play</button>
<a class="b" id="gif" download>GIF</a><input type="range" id="s" min="0" max="0" value="0"><span id="t"></span></div>
<p id="m" style="margin-top:8px"></p></div>
<div class="card" id="gal" style="display:none"><select id="gv"><option value="">All views</option>%OPTS%</select>
<div id="grid"></div><p id="gm"></p><button id="more" style="display:none">Load more</button></div>
<div class="card" id="fc" style="display:none"><p id="fm">Loading…</p><div id="ft"></div></div>
<div class="card" id="wx" style="display:none"><p id="wxm">Loading…</p><div id="wxt"></div>
<h2>Radar and weather maps</h2><div id="cbar"><select id="cs"></select><button id="cp">Pause</button></div>
<img id="ci" alt="NWS weather map" style="display:none"><input type="range" id="csl" min="0" max="0" value="0" style="width:100%;margin-top:8px;display:none">
<p id="cm"></p></div>
<div class="card" id="how" style="display:none">%HOW%</div>
<div id="lb" style="display:none"><img id="lbi"><div><a class="b" id="lbs">Save</a> <button id="lbc">Close</button></div></div></main><script>
const V=%VJSON%;
const $=q=>document.querySelector(q),B='%BASE%';let F=[],T=null;
function show(i){const f=F[i];if(!f)return;$('#s').value=i;$('#im').src=B+'img/'+$('#v').value+'/'+f[1];
const d=new Date(f[0]*1000);$('#t').textContent=d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}
function stop(){clearInterval(T);T=null;$('#play').textContent='Play'}
async function load(){stop();const v=$('#v').value;$('#gif').href=B+'gif/'+v+'.gif';
const r=await fetch(B+'api/'+v);F=(await r.json()).frames;$('#s').max=Math.max(F.length-1,0);
$('#m').textContent=F.length?F.length+' frames. Tap the picture to open it full size.':'No images for this view yet.';
$('#im').style.display=F.length?'':'none';if(F.length)show(F.length-1)}
$('#v').onchange=load;$('#s').oninput=e=>{stop();show(+e.target.value)};
$('#im').onclick=()=>window.open($('#im').src);
$('#play').onclick=async()=>{if(T)return stop();if(F.length<2)return;$('#play').disabled=true;
await Promise.all(F.map(f=>new Promise(r=>{const i=new Image();i.onload=i.onerror=r;i.src=B+'img/'+$('#v').value+'/'+f[1]})));
$('#play').disabled=false;$('#play').textContent='Pause';let i=+$('#s').value>=F.length-1?0:+$('#s').value;show(i);
T=setInterval(()=>{i=(i+1)%F.length;show(i)},300)};
const fmt=t=>{const d=new Date(t*1000);return d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})};
function tab(n){const ids=['views','gal','how','fc','wx'],btn=['tv','tg','th','tf','tw'];
ids.forEach((id,i)=>{$('#'+id).style.display=i==n?'':'none';$('#'+btn[i]).className=i==n?'on':''});
if(n)stop();if(n!=4)stopC();if(n==3)forecast();if(n==1&&!$('#grid').children.length)gal(true);if(n==4)weather()}
$('#tv').onclick=()=>tab(0);$('#tg').onclick=()=>tab(1);$('#th').onclick=()=>tab(2);$('#tf').onclick=()=>tab(3);$('#tw').onclick=()=>tab(4);
let fdone=0;
async function forecast(){if(fdone&&Date.now()-fdone<120000)return;fdone=Date.now();
const r=await(await fetch(B+'api/forecast')).json();const h=r.offices.map(o=>'<h2>'+esc(o.office)+'</h2><div class="iss">'+esc(o.issued)+(o.age_h>12?' &middot; '+Math.round(o.age_h)+' h old':'')+'</div>'+
o.sections.map(s=>'<h3>'+esc(s.head)+'</h3>'+s.paras.map(p=>'<p>'+esc(p)+'</p>').join('')).join('')).join('');
$('#ft').innerHTML=h;$('#fm').textContent=r.offices.length?'The National Weather Service area forecast discussions, as received by the satellite broadcast. Written by forecasters for a technical audience.':'No forecast discussions have arrived yet.'}
let wdone=0,CH=[],CT=null,cinit=false;
function wblock(title,note,rows){return '<h2>'+esc(title)+'</h2>'+(note?'<div class="iss">'+esc(note)+'</div>':'')+rows.map(r=>'<p>'+(r[1]===undefined?esc(r[0]):'<b>'+esc(r[0])+'</b> '+esc(r[1]))+'</p>').join('')}
async function weather(){if(wdone&&Date.now()-wdone<120000){return}wdone=Date.now();
let r;try{r=await(await fetch(B+'api/weather')).json()}catch(e){$('#wxm').textContent='Weather data is not available right now.';return}
if(r.error){$('#wxm').textContent='Weather data is not available right now.';return}
const al=r.alerts||[];let h='<div class="al '+(al.length?'bad':'ok')+'">'+(al.length?al.length+' alert'+(al.length>1?'s':'')+' for Stratford: '+al.map(a=>esc(a.name)+(a.ends?' until '+fmt(a.ends):'')).join(' &middot; '):'No alerts in force for Fairfield County or the western Sound')+'</div>';
const n=r.now;if(n)h+='<h2>'+esc(r.place)+'</h2><div class="big">'+(n.temp_f!=null?n.temp_f+'&deg;F ':'')+esc(n.wx.length?n.wx[0]:(n.sky.length?n.sky[n.sky.length-1].cover:''))+'</div><div class="iss">Bridgeport airport, '+fmt(n.t)+' &middot; '+esc(n.summary)+(n.pressure_inhg?' &middot; '+n.pressure_inhg.toFixed(2)+' inHg':'')+'</div>';
if(r.forecast)h+=wblock(r.forecast.name+' forecast',r.forecast.issued,r.forecast.periods.slice(0,6));
if(r.marine)h+=wblock('Long Island Sound (west of New Haven)',r.marine.issued,(r.marine.headlines||[]).map(x=>[x]).concat(r.marine.periods.slice(0,5)));
if(r.taf)h+='<h2>Airport forecast (TAF) for Bridgeport</h2><div class="iss">'+fmt(r.taf.t)+'</div><pre>'+esc(r.taf.text)+'</pre>';
const sp=r.space;if(sp&&(sp.alert||sp.sunspot!=null)){const rows=[];if(sp.alert)rows.push([sp.alert.text+',',fmt(sp.alert.t)+(sp.alert.aurora?'. '+sp.alert.aurora:'')]);
if(sp.sunspot!=null)rows.push(['Sunspot number',sp.sunspot+' · 10.7 cm flux '+sp.flux]);h+=wblock('Space weather','Aurora is only likely in Connecticut at a K-index of 7 or more.',rows)}
$('#wxt').innerHTML=h;$('#wxm').textContent='Current conditions and forecasts from the National Weather Service, as received over the satellite.';
if(!cinit){cinit=true;try{const c=await(await fetch(B+'api/charts')).json();
$('#cs').innerHTML=c.groups.map(g=>'<optgroup label="'+esc(g.group)+'">'+g.items.map(i=>'<option value="'+esc(i.code)+'">'+esc(i.label)+'</option>').join('')+'</optgroup>').join('');
const rad=[...$('#cs').options].find(o=>o.value=='RADNTHES');if(rad)$('#cs').value='RADNTHES';chart()}catch(e){}}}
function stopC(){clearInterval(CT);CT=null;$('#cp').textContent='Play'}
function cshow(i){const f=CH[i];if(!f)return;$('#ci').src=B+'chart/'+encodeURIComponent(f.name);$('#csl').value=i;
const m=Math.round((Date.now()/1000-f.t)/60);$('#cm').textContent=fmt(f.t)+' ('+(m<90?m+' min':Math.round(m/60)+' h')+' old)'+(CH.length>1?' \u00b7 frame '+(i+1)+' of '+CH.length:'')+'. Issued time; the feed repeats older maps.'}
async function chart(){stopC();const r=await(await fetch(B+'api/chart?code='+encodeURIComponent($('#cs').value)+'&n=12')).json();CH=r.frames||[];
$('#ci').style.display=CH.length?'':'none';$('#csl').style.display=CH.length>1?'':'none';$('#cp').style.display=CH.length>1?'':'none';
if(!CH.length){$('#cm').textContent='Nothing received yet.';return}$('#csl').max=CH.length-1;CH.forEach(f=>{const i=new Image();i.src=B+'chart/'+encodeURIComponent(f.name)});
cshow(CH.length-1);if(CH.length>1){$('#cp').textContent='Pause';let i=CH.length-1;CT=setInterval(()=>{i=(i+1)%CH.length;cshow(i)},700)}}
$('#cs').onchange=chart;$('#csl').oninput=e=>{stopC();cshow(+e.target.value)};
$('#cp').onclick=()=>{if(CT)return stopC();let i=+$('#csl').value;CT=setInterval(()=>{i=(i+1)%CH.length;cshow(i)},700);$('#cp').textContent='Pause'};
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function stale(){try{const r=await(await fetch(B+'api/status')).json();const m=Math.round((r.now-r.last)/60);const b=$('#stale');
if(!r.last){b.style.display='';b.textContent='No images have arrived yet.';return}
if(m>=120){b.style.display='';b.textContent='The newest picture is '+(m>=1440?Math.round(m/1440)+' day(s)':Math.round(m/60)+' hours')+' old. The receiver may be offline, so these may not be current.'}
else b.style.display='none';$('#lu').textContent=' Newest picture: '+(m<2?'just now':m<90?m+' min ago':Math.round(m/60)+' hours ago')+'.'}catch(e){}}
stale();setInterval(stale,300000);
let go=0;
async function gal(reset){if(reset){go=0;$('#grid').innerHTML=''}
const r=await(await fetch(B+'api/gallery?view='+$('#gv').value+'&offset='+go)).json();
for(const [t,v,n] of r.items){const d=document.createElement('div');
d.innerHTML='<img loading="lazy" src="'+B+'thumb/'+v+'/'+n+'"><span><b>'+fmt(t)+'</b><br>'+V[v]+'</span>';
d.onclick=()=>{$('#lbi').src=B+'img/'+v+'/'+n;$('#lbs').href=B+'img/'+v+'/'+n+'?dl=1';$('#lb').style.display=''};$('#grid').appendChild(d)}
go+=r.items.length;$('#more').style.display=go<r.total?'':'none';
$('#gm').textContent=r.total?'':'No images yet.'}
$('#gv').onchange=()=>gal(true);$('#more').onclick=()=>gal(false);$('#lbc').onclick=()=>{$('#lb').style.display='none';$('#lbi').src=''};
document.onkeydown=e=>{if(e.key==='Escape')$('#lbc').click();
if($('#views').style.display!=='none'&&$('#lb').style.display==='none'&&(e.key==='ArrowLeft'||e.key==='ArrowRight')&&F.length){stop();show(Math.min(F.length-1,Math.max(0,+$('#s').value+(e.key==='ArrowRight'?1:-1))))}
if(e.key===' '&&$('#views').style.display!=='none'&&e.target===document.body){e.preventDefault();$('#play').click()}};
load();</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "goes-share"

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def wx_data(self):
        """Weather tab data, cached for two minutes so many viewers cost one scan."""
        hit = _wx_cache.get("w")
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        d = _wx[0].summary()
        d = {k: d[k] for k in ("place", "now", "taf", "forecast", "marine", "space")} | {"alerts": d["alerts"]}
        _wx_cache["w"] = (time.time(), d)
        return d

    def do_GET(self):
        try:
            path, _, query = self.path.partition("?")
            q = parse_qs(query)
            m = re.fullmatch(r"/s/([A-Za-z0-9_-]{20,64})/(.*)", path)
            lk = link_for(m.group(1)) if m else None
            if not lk:
                return self.reply(404, b"not found")
            rest, base = m.group(2), f"/s/{m.group(1)}/"
            if rest == "manifest.webmanifest":
                man = {"name": "GOES-19 Satellite", "short_name": "GOES", "start_url": base, "scope": base,
                       "display": "standalone", "background_color": "#0e1828", "theme_color": "#0e1828",
                       "icons": [{"src": base + "icon.png", "sizes": "512x512", "type": "image/png"}]}
                return self.reply(200, json.dumps(man).encode(), "application/manifest+json")
            if rest == "icon.png" and _icon and _icon.exists():
                return self.reply(200, _icon.read_bytes(), "image/png", {"Cache-Control": "max-age=86400"})
            if rest == "api/status":
                last = max([fr[-1][0] for v in VIEWS for fr in [frames(v)] if fr] or [0])
                return self.reply(200, json.dumps({"last": round(last), "now": round(time.time())}).encode(),
                                  "application/json", {"Cache-Control": "no-cache"})
            if rest == "api/forecast":
                data = _forecast_fn() if _forecast_fn else []
                return self.reply(200, json.dumps({"offices": data}).encode(), "application/json",
                                  {"Cache-Control": "max-age=120"})
            if rest == "api/weather" and _wx:
                return self.reply(200, json.dumps(self.wx_data()).encode(), "application/json", {"Cache-Control": "max-age=60"})
            if rest == "api/charts" and _wx:
                return self.reply(200, json.dumps({"groups": _wx[1].charts()}).encode(), "application/json",
                                  {"Cache-Control": "max-age=60"})
            if rest == "api/chart" and _wx:
                try:
                    fr = _wx[1].chart_frames(q.get("code", [""])[0], int(q.get("n", ["12"])[0]))
                except ValueError:
                    return self.reply(404, b"not found")
                return self.reply(200, json.dumps({"frames": fr}).encode(), "application/json", {"Cache-Control": "max-age=60"})
            m = re.fullmatch(r"chart/([A-Za-z0-9_.-]+\.(GIF|PNG|JPG))", rest)
            if m and _wx:
                p = _wx[1].chart_file(m.group(1))
                if p:
                    ctype = {"GIF": "image/gif", "PNG": "image/png", "JPG": "image/jpeg"}[m.group(2)]
                    return self.reply(200, p.read_bytes(), ctype, {"Cache-Control": "max-age=3600"})
                return self.reply(404, b"not found")
            if rest == "":
                count_visit(lk["id"])
                opts = "".join(f'<option value="{k}">{v}</option>' for k, v in VIEWS.items())
                page = PAGE.replace("%OPTS%", opts).replace("%BASE%", base).replace("%VJSON%", json.dumps(VIEWS)).replace("%HOW%", HOW).encode()
                return self.reply(200, page, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            m = re.fullmatch(r"api/([a-z0-9_]+)", rest)
            if m and m.group(1) in VIEWS:
                cut = time.time() - HOURS * 3600
                fr = [[round(t), p.name] for t, p in frames(m.group(1)) if t >= cut]
                if m.group(1).startswith("meso"):
                    fr = fr[-MESO_MAX:]
                return self.reply(200, json.dumps({"frames": fr}).encode(), "application/json",
                                  {"Cache-Control": "max-age=30"})
            if rest == "api/gallery":
                view = q.get("view", [""])[0]
                off = max(int(q.get("offset", ["0"])[0]), 0)
                allf = [(t, v, p.name) for v in ([view] if view in VIEWS else VIEWS) for t, p in frames(v)]
                allf.sort(reverse=True)
                return self.reply(200, json.dumps({"total": len(allf), "items": [[round(t), v, n] for t, v, n in allf[off:off + 48]]}).encode(),
                                  "application/json", {"Cache-Control": "max-age=30"})
            m = re.fullmatch(r"(img|thumb)/([a-z0-9_]+)/([A-Za-z0-9_.-]+\.jpg)", rest)
            if m and m.group(2) in VIEWS:
                for _t, p in frames(m.group(2)):
                    if p.name == m.group(3):
                        extra = {"Cache-Control": "max-age=86400, immutable"}
                        if "dl=1" in query:
                            extra["Content-Disposition"] = f'attachment; filename="{p.name}"'
                        src = thumb(p) if m.group(1) == "thumb" else p
                        return self.reply(200, src.read_bytes(), "image/jpeg", extra)
            m = re.fullmatch(r"gif/([a-z0-9_]+)\.gif", rest)
            if m and m.group(1) in VIEWS:
                try:
                    p = _gif_fn(m.group(1), HOURS)
                except ValueError as e:
                    return self.reply(404, str(e).encode())
                return self.reply(200, Path(p).read_bytes(), "image/gif",
                                  {"Content-Disposition": f'attachment; filename="goes19-{m.group(1)}.gif"'})
            self.reply(404, b"not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("share error:", e, flush=True)
            try:
                self.reply(500, b"error")
            except OSError:
                pass


def start(frames_fn, gif_fn, forecast_fn=None, icon=None, wx=None):
    global _frames_fn, _gif_fn, _forecast_fn, _icon, _wx
    _frames_fn, _gif_fn, _forecast_fn, _icon, _wx = frames_fn, gif_fn, forecast_fn, icon, wx
    links()
    _load_visits()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"share page on 127.0.0.1:{PORT}", flush=True)

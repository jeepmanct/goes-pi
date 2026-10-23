"""Public read-only page: latest GOES views behind an unguessable link.

Started from app.py with start(frames_fn, gif_fn). Listens on 127.0.0.1 only; expose it with
`tailscale funnel`. The token lives in ~/goes/share-token.txt (delete the file and restart to rotate).
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

TOKEN_FILE = Path.home() / "goes" / "share-token.txt"
PORT = 8090
VIEWS = {  # name -> label
    "nyc": "New York City", "northeast": "Northeast US", "fog_northeast": "Northeast fog (night)",
    "eastus": "East Coast", "namer": "North America", "gulf": "Gulf of Mexico",
    "samer": "South America", "globe": "Full disk globe",
}
HOURS = 24          # Views player + GIF window
GALLERY_HOURS = 72  # how far back the Gallery goes
THUMBS = Path.home() / "goes" / "webui" / "thumbs" / "share"
_frames_fn = _gif_fn = None
_cache = {}
_lock = threading.Lock()


def token():
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if len(t) >= 20:
            return t
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t + "\n")
    TOKEN_FILE.chmod(0o600)
    return t


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
<title>GOES-19 Satellite</title><style>
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
</style></head><body><main><h1>GOES-19 East satellite</h1>
<p>Live images from a home-built receiver. Updated every few minutes; showing the last 24 hours.</p>
<div id="tabs"><button id="tv" class="on">Views</button><button id="tg">Gallery</button><button id="th">How it works</button></div>
<div class="card" id="views"><select id="v">%OPTS%</select>
<img id="im" alt="GOES-19 view"><div id="bar"><button id="play">Play</button>
<a class="b" id="gif" download>GIF</a><input type="range" id="s" min="0" max="0" value="0"><span id="t"></span></div>
<p id="m" style="margin-top:8px"></p></div>
<div class="card" id="gal" style="display:none"><select id="gv"><option value="">All views</option>%OPTS%</select>
<div id="grid"></div><p id="gm"></p><button id="more" style="display:none">Load more</button></div>
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
function tab(n){$('#views').style.display=n==0?'':'none';$('#gal').style.display=n==1?'':'none';$('#how').style.display=n==2?'':'none';$('#tv').className=n==0?'on':'';$('#tg').className=n==1?'on':'';$('#th').className=n==2?'on':'';if(n)stop();if(n==1&&!$('#grid').children.length)gal(true)}
$('#tv').onclick=()=>tab(0);$('#tg').onclick=()=>tab(1);$('#th').onclick=()=>tab(2);
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

    def do_GET(self):
        try:
            path, _, query = self.path.partition("?")
            q = parse_qs(query)
            m = re.fullmatch(r"/s/([A-Za-z0-9_-]{20,64})/(.*)", path)
            if not m or not hmac.compare_digest(m.group(1), self.server.tok):
                return self.reply(404, b"not found")
            rest, base = m.group(2), f"/s/{m.group(1)}/"
            if rest == "":
                opts = "".join(f'<option value="{k}">{v}</option>' for k, v in VIEWS.items())
                page = PAGE.replace("%OPTS%", opts).replace("%BASE%", base).replace("%VJSON%", json.dumps(VIEWS)).replace("%HOW%", HOW).encode()
                return self.reply(200, page, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            m = re.fullmatch(r"api/([a-z_]+)", rest)
            if m and m.group(1) in VIEWS:
                cut = time.time() - HOURS * 3600
                fr = [[round(t), p.name] for t, p in frames(m.group(1)) if t >= cut]
                return self.reply(200, json.dumps({"frames": fr}).encode(), "application/json",
                                  {"Cache-Control": "max-age=30"})
            if rest == "api/gallery":
                view = q.get("view", [""])[0]
                off = max(int(q.get("offset", ["0"])[0]), 0)
                allf = [(t, v, p.name) for v in ([view] if view in VIEWS else VIEWS) for t, p in frames(v)]
                allf.sort(reverse=True)
                return self.reply(200, json.dumps({"total": len(allf), "items": [[round(t), v, n] for t, v, n in allf[off:off + 48]]}).encode(),
                                  "application/json", {"Cache-Control": "max-age=30"})
            m = re.fullmatch(r"(img|thumb)/([a-z_]+)/([A-Za-z0-9_.-]+\.jpg)", rest)
            if m and m.group(2) in VIEWS:
                for _t, p in frames(m.group(2)):
                    if p.name == m.group(3):
                        extra = {"Cache-Control": "max-age=86400, immutable"}
                        if "dl=1" in query:
                            extra["Content-Disposition"] = f'attachment; filename="{p.name}"'
                        src = thumb(p) if m.group(1) == "thumb" else p
                        return self.reply(200, src.read_bytes(), "image/jpeg", extra)
            m = re.fullmatch(r"gif/([a-z_]+)\.gif", rest)
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


def start(frames_fn, gif_fn):
    global _frames_fn, _gif_fn
    _frames_fn, _gif_fn = frames_fn, gif_fn
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.tok = token()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"share page on 127.0.0.1:{PORT}", flush=True)

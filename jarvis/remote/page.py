"""The phone dashboard's single page. Text from JARVIS is set with textContent only."""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS</title>
<style>
:root { --bg:#0b0f14; --panel:#121a23; --line:#1f2c3a; --text:#d8e6f2; --dim:#7f93a6;
        --cyan:#38d6ff; --red:#ff5c6c; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
header { padding:16px; display:flex; justify-content:space-between; align-items:center;
         border-bottom:1px solid var(--line); }
h1 { margin:0; font-size:18px; letter-spacing:.2em; color:var(--cyan); }
#state { font-size:12px; letter-spacing:.15em; text-transform:uppercase; color:var(--dim); }
main { padding:12px 16px 120px; max-width:720px; margin:0 auto; }
.entry { background:var(--panel); border:1px solid var(--line); border-radius:12px;
         padding:10px 12px; margin:8px 0; white-space:pre-wrap; word-wrap:break-word; }
.user { border-color:#24425a; } .error { border-color:var(--red); }
.meta { font-size:11px; color:var(--dim); letter-spacing:.1em; text-transform:uppercase; }
form { position:fixed; left:0; right:0; bottom:0; display:flex; gap:8px; padding:12px 16px;
       background:var(--bg); border-top:1px solid var(--line); }
input { flex:1; min-width:0; font:inherit; color:var(--text); background:var(--panel);
        border:1px solid var(--line); border-radius:10px; padding:12px; }
button { font:inherit; color:var(--bg); background:var(--cyan); border:0; border-radius:10px;
         padding:0 16px; }
#pair { display:none; padding:24px 16px; max-width:420px; margin:0 auto; }
#pair p { color:var(--dim); } #pair form { position:static; padding:0; border:0; }
#note { color:var(--red); min-height:1.5em; }
</style></head>
<body>
<header><h1>JARVIS</h1><span id="state">…</span></header>
<section id="pair">
  <p>Ask JARVIS on your PC to "pair my phone", then enter the 6-digit code it shows.</p>
  <form id="pairform"><input id="code" inputmode="numeric" maxlength="6"
    autocomplete="one-time-code" placeholder="Code"><button>Pair</button></form>
  <p id="note"></p>
</section>
<main id="feed"></main>
<form id="ask"><input id="text" placeholder="Ask JARVIS…" autocomplete="off">
  <button>Send</button></form>
<script>
const $ = (id) => document.getElementById(id);
const post = (path, body) => fetch(path, {method: "POST", credentials: "same-origin",
  headers: {"Content-Type": "application/json", "X-Jarvis": "1"}, body: JSON.stringify(body)});
function showPairing(on) {
  $("pair").style.display = on ? "block" : "none";
  $("feed").style.display = $("ask").style.display = on ? "none" : "";
}
async function refresh() {
  try {
    const r = await fetch("/state", {credentials: "same-origin"});
    if (r.status === 401) { showPairing(true); return; }
    const data = await r.json();
    showPairing(false);
    $("state").textContent = data.state;
    const feed = $("feed");
    feed.replaceChildren();
    for (const e of data.entries) {
      const div = document.createElement("div");
      div.className = "entry " + e.kind;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = e.kind + (e.mode ? " · " + e.mode : "");
      const text = document.createElement("div");
      text.textContent = e.text;
      div.append(meta, text);
      feed.append(div);
    }
    window.scrollTo(0, document.body.scrollHeight);
  } catch (err) { $("state").textContent = "offline"; }
}
$("pairform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const r = await post("/pair", {code: $("code").value});
  const data = await r.json();
  $("note").textContent = data.ok ? "" : data.error;
  if (data.ok) refresh();
});
$("ask").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("text").value.trim();
  if (!text) return;
  const r = await post("/ask", {text});
  const data = await r.json();
  if (data.ok) $("text").value = "";
  else $("state").textContent = "busy, try again";
  refresh();
});
refresh();
setInterval(refresh, 2500);
</script></body></html>
"""

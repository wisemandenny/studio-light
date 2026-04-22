"""Synchronous HTTP configuration server.

Designed to be polled from the main loop via ``tick()`` while the device is
in AP mode. ``tick()`` accepts at most one pending connection per call and
returns immediately if none is waiting (the listen socket is set to a
short timeout on start). HTTP Basic Auth gates all requests unless the
configured password is empty.

Routes:
  GET  /         -> HTML config page
  GET  /config   -> current networks.json contents as JSON
  POST /config   -> validate + persist new networks.json, then invoke the
                    ``on_saved`` callback (typically WifiManager.reload_and_reconnect)
  POST /validate -> live-test a single {ssid, password} pair against the
                    real radio, without saving anything. Delegates to the
                    ``validator`` callable supplied by the caller
                    (typically WifiManager.validate_credentials). Returns
                    ``{"ok": bool, "ssid": ..., "message": ...}`` as JSON.
"""
import json
import socket
import time
import ubinascii


_INDEX_HTML = """<!DOCTYPE html>
<html><head><title>StudioLight Wi-Fi Setup</title>
<style>
body{font-family:Arial,sans-serif;margin:20px;max-width:720px;}
textarea{width:100%;font-family:monospace;}
button{margin-right:8px;padding:8px 14px;}
input{padding:6px;margin-right:6px;}
fieldset{margin-top:18px;padding:12px;border:1px solid #ccc;}
legend{padding:0 6px;font-weight:bold;}
#status{margin-top:10px;min-height:1.5em;}
</style></head><body>
<h2>StudioLight Wi-Fi Setup</h2>
<textarea id="config" rows="22" placeholder="Loading..."></textarea><br><br>
<button onclick="loadConfig()">Reload</button>
<button onclick="validateJson()">Validate JSON</button>
<button onclick="saveConfig()">Save &amp; Apply</button>

<fieldset>
<legend>Test Wi-Fi credentials</legend>
<p style="margin-top:0;">Try a single SSID/password pair against the radio before
you save, or batch-test everything under <code>known_networks</code> in
the JSON above. Either way, the light stays in setup mode -- these are
non-destructive checks.</p>
<input id="test_ssid" placeholder="SSID" size="24">
<input id="test_pwd" type="password" placeholder="Password" size="24">
<button onclick="testCreds()">Test connection</button>
<button id="add_btn" onclick="addValidatedToJson()" disabled title="Enabled after a successful Test connection">Add to known networks</button>
<button onclick="testAll()">Test all configured</button>
<pre id="test_log" style="background:#f4f4f4;padding:8px;margin-top:10px;max-height:200px;overflow:auto;"></pre>
</fieldset>

<div id="status"></div>
<script>
function setStatus(msg, err){
  const s=document.getElementById('status');
  s.textContent=msg; s.style.color=err?'#b00':'#060';
}
function loadConfig(){
  fetch('/config').then(r=>r.text()).then(d=>{
    try{document.getElementById('config').value=JSON.stringify(JSON.parse(d),null,2);
        setStatus('Loaded.');}
    catch(e){document.getElementById('config').value=d; setStatus('Loaded raw (not JSON).',true);}
  }).catch(e=>setStatus('Load failed: '+e,true));
}
function validateJson(){
  try{const v=JSON.parse(document.getElementById('config').value);
      if(!v.known_networks||!v.access_point) throw new Error('missing sections');
      setStatus('JSON looks valid.');
  }catch(e){setStatus('Invalid JSON: '+e.message,true);}
}
function saveConfig(){
  const body=document.getElementById('config').value;
  try{JSON.parse(body);}catch(e){return setStatus('Cannot save: '+e.message,true);}
  fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body})
    .then(r=>r.text()).then(()=>setStatus('Saved. Device will reconnect.'))
    .catch(e=>setStatus('Save failed: '+e,true));
}
function logLine(line, err){
  const log=document.getElementById('test_log');
  const div=document.createElement('div');
  div.textContent=line;
  if(err){div.style.color='#b00';}
  log.appendChild(div);
  log.scrollTop=log.scrollHeight;
}
function clearLog(){document.getElementById('test_log').textContent='';}
let lastValidated=null;
function invalidateAdd(){
  lastValidated=null;
  const b=document.getElementById('add_btn');
  if(b){b.disabled=true;}
}
function doValidate(ssid, pwd){
  return fetch('/validate',{method:'POST',headers:{'Content-Type':'application/json'},
                            body:JSON.stringify({ssid:ssid,password:pwd})})
           .then(r=>r.json());
}
function testCreds(){
  const ssid=document.getElementById('test_ssid').value;
  const pwd=document.getElementById('test_pwd').value;
  if(!ssid){return setStatus('Enter an SSID to test.',true);}
  clearLog();
  invalidateAdd();
  setStatus('Testing "'+ssid+'"... (up to ~8s, the AP may briefly hiccup)');
  logLine('> '+ssid+' ...');
  doValidate(ssid, pwd).then(d=>{
    logLine((d.ok?'  ok: ':'  FAIL: ')+(d.message||''), !d.ok);
    if(d.ok){
      setStatus('OK: '+(d.message||'connected.')+' Click "Add to known networks" to save it.');
      lastValidated={ssid:ssid, password:pwd};
      const b=document.getElementById('add_btn');
      if(b){b.disabled=false;}
    }else{
      setStatus('Failed: '+(d.message||'unknown error'),true);
    }
  }).catch(e=>{logLine('  request error: '+e,true); setStatus('Test request failed: '+e,true);});
}
function addValidatedToJson(){
  if(!lastValidated){return setStatus('Test a network successfully first.',true);}
  let cfg;
  try{cfg=JSON.parse(document.getElementById('config').value);}
  catch(e){return setStatus('Cannot parse JSON above: '+e.message,true);}
  if(!Array.isArray(cfg.known_networks)){cfg.known_networks=[];}
  let found=false;
  for(let i=0;i<cfg.known_networks.length;i++){
    const n=cfg.known_networks[i]||{};
    if(n.ssid===lastValidated.ssid){
      cfg.known_networks[i]=Object.assign({},n,{ssid:lastValidated.ssid,password:lastValidated.password});
      found=true;
      break;
    }
  }
  if(!found){
    cfg.known_networks.push({ssid:lastValidated.ssid, password:lastValidated.password});
  }
  document.getElementById('config').value=JSON.stringify(cfg,null,2);
  const action=found?'Updated':'Added';
  logLine('  '+action.toLowerCase()+' "'+lastValidated.ssid+'" in known_networks (not yet saved).');
  setStatus(action+' "'+lastValidated.ssid+'" in known_networks. Click "Save & Apply" to persist.');
  invalidateAdd();
}
async function testAll(){
  let cfg;
  try{cfg=JSON.parse(document.getElementById('config').value);}
  catch(e){return setStatus('Cannot parse JSON above: '+e.message,true);}
  const list=Array.isArray(cfg.known_networks)?cfg.known_networks:[];
  if(list.length===0){return setStatus('No known_networks to test.',true);}
  clearLog();
  setStatus('Testing '+list.length+' network(s)... (~'+(list.length*8)+'s max)');
  let okCount=0, failCount=0;
  for(let i=0;i<list.length;i++){
    const n=list[i]||{};
    const ssid=n.ssid||'';
    const pwd=n.password||'';
    logLine('['+(i+1)+'/'+list.length+'] > '+(ssid||'<no ssid>')+' ...');
    if(!ssid){logLine('  FAIL: entry has no ssid',true); failCount++; continue;}
    try{
      const d=await doValidate(ssid, pwd);
      if(d.ok){logLine('  ok: '+(d.message||'')); okCount++;}
      else{logLine('  FAIL: '+(d.message||''),true); failCount++;}
    }catch(e){logLine('  request error: '+e,true); failCount++;}
  }
  const summary=okCount+' ok, '+failCount+' failed';
  setStatus('Done. '+summary, failCount>0);
}
loadConfig();
['test_ssid','test_pwd'].forEach(function(id){
  const el=document.getElementById(id);
  if(el){el.addEventListener('input', invalidateAdd);}
});
</script></body></html>"""


class ConfigServer:
    def __init__(self, port=8080, password="micropython",
                 config_path="/networks.json", on_saved=None,
                 validator=None):
        self.port = port
        self.password = password
        self.config_path = config_path
        self.on_saved = on_saved
        # validator(ssid, password) -> (ok: bool, message: str)
        # Optional. If None, /validate returns 501.
        self.validator = validator
        self._sock = None
        self.activity_at_ms = 0

    def start(self):
        if self._sock is not None:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.listen(1)
            s.settimeout(0.01)
            self._sock = s
            print("config_server: listening on :{}".format(self.port))
            return True
        except OSError as e:
            print("config_server: bind failed:", e)
            self._sock = None
            return False

    def stop(self):
        if self._sock is None:
            return
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        print("config_server: stopped")

    def tick(self):
        if self._sock is None and not self.start():
            return

        try:
            conn, _ = self._sock.accept()
        except OSError:
            return  # no client waiting

        try:
            conn.settimeout(5.0)
            raw = conn.recv(4096)
            if not raw:
                return
            request = raw.decode("utf-8", "replace")
            self.activity_at_ms = time.ticks_ms()
            response = self._handle(request)
            conn.send(response.encode())
        except Exception as e:
            print("config_server: request error:", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # --- internals ------------------------------------------------------

    def _require_auth(self, request):
        if not self.password:
            return True
        for line in request.split("\r\n"):
            if line.lower().startswith("authorization: basic "):
                try:
                    raw = line.split(" ", 2)[2]
                    decoded = ubinascii.a2b_base64(raw).decode()
                    if ":" in decoded:
                        user, pwd = decoded.split(":", 1)
                        if user == "admin" and pwd == self.password:
                            return True
                except Exception:
                    pass
                break
        return False

    def _unauthorized(self):
        return ("HTTP/1.1 401 Unauthorized\r\n"
                "WWW-Authenticate: Basic realm=\"StudioLight\"\r\n"
                "Content-Type: text/plain\r\n\r\n"
                "Authentication required")

    def _handle(self, request):
        if not self._require_auth(request):
            return self._unauthorized()

        if request.startswith("POST /config"):
            return self._post_config(request)
        if request.startswith("POST /validate"):
            return self._post_validate(request)
        if request.startswith("GET /config"):
            return self._get_config()
        if request.startswith("GET / ") or request.startswith("GET /index"):
            return ("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
                    "{}".format(_INDEX_HTML))
        return "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNot found"

    def _get_config(self):
        try:
            with open(self.config_path, "r") as f:
                data = f.read()
            return ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
                    "{}".format(data))
        except Exception as e:
            return ("HTTP/1.1 500 Internal Server Error\r\n"
                    "Content-Type: text/plain\r\n\r\n"
                    "Could not read config: {}".format(e))

    def _post_config(self, request):
        idx = request.find("\r\n\r\n")
        if idx < 0:
            return ("HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: text/plain\r\n\r\nNo request body")
        body = request[idx + 4:]
        try:
            cfg = json.loads(body)
            if "known_networks" not in cfg or "access_point" not in cfg:
                raise ValueError("missing required sections")
        except Exception as e:
            return ("HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: text/plain\r\n\r\n"
                    "Invalid JSON: {}".format(e))
        try:
            with open(self.config_path, "w") as f:
                f.write(body)
        except Exception as e:
            return ("HTTP/1.1 500 Internal Server Error\r\n"
                    "Content-Type: text/plain\r\n\r\n"
                    "Failed to save: {}".format(e))
        print("config_server: config updated via web interface")
        if self.on_saved is not None:
            try:
                self.on_saved()
            except Exception as e:
                print("config_server: on_saved callback error:", e)
        return ("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                "Saved. Device will reconnect.")

    def _post_validate(self, request):
        if self.validator is None:
            return ("HTTP/1.1 501 Not Implemented\r\n"
                    "Content-Type: application/json\r\n\r\n"
                    "{\"ok\":false,\"message\":\"validator not configured\"}")
        idx = request.find("\r\n\r\n")
        body = request[idx + 4:] if idx >= 0 else ""
        try:
            data = json.loads(body) if body else {}
            ssid = data.get("ssid", "")
            pwd = data.get("password", "")
        except Exception as e:
            payload = json.dumps({"ok": False,
                                  "message": "bad request: {}".format(e)})
            return ("HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: application/json\r\n\r\n{}".format(payload))
        try:
            ok, message = self.validator(ssid, pwd)
        except Exception as e:
            ok, message = False, "validator raised: {}".format(e)
        payload = json.dumps({"ok": bool(ok),
                              "ssid": ssid,
                              "message": message})
        print("config_server: /validate ssid='{}' -> ok={} ({})".format(
            ssid, ok, message))
        return ("HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n\r\n{}".format(payload))

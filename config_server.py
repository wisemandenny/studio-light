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
#status{margin-top:10px;min-height:1.5em;}
</style></head><body>
<h2>StudioLight Wi-Fi Setup</h2>
<textarea id="config" rows="22" placeholder="Loading..."></textarea><br><br>
<button onclick="loadConfig()">Reload</button>
<button onclick="validate()">Validate JSON</button>
<button onclick="saveConfig()">Save &amp; Apply</button>
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
function validate(){
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
loadConfig();
</script></body></html>"""


class ConfigServer:
    def __init__(self, port=8080, password="micropython",
                 config_path="/networks.json", on_saved=None):
        self.port = port
        self.password = password
        self.config_path = config_path
        self.on_saved = on_saved
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

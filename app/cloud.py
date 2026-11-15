"""
Cloud-Spiegel: legt eine verschlüsselte Kopie der Daten plus die Web-App in ein
GitHub-Repository mit GitHub Pages. Das Handy öffnet die App dort, auch wenn der PC aus ist.
Änderungen vom Handy landen verschlüsselt im Ordner edits/ und werden vom PC übernommen.

Verschlüsselung (nur Standardbibliothek, im Browser per WebCrypto nachgebaut):
  Schlüssel  = PBKDF2-HMAC-SHA256(Passwort, Salt, 310.000 Runden) -> 64 Byte (Verschlüsselung + Prüfsumme)
  Strom      = HMAC-SHA256(Schlüssel, Nonce || Zähler) im Zählermodus
  Integrität = HMAC-SHA256(Prüfschlüssel, Salt || Nonce || Geheimtext)
"""
import base64
import gzip
import hashlib
import hmac
import json
import time
import os
import urllib.error

from common import http_json

ITERATIONS = 310_000
API = "https://api.github.com"


# ---------------------------------------------------------------- Verschlüsselung

def _keys(password, salt, iterations):
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 64)
    return k[:32], k[32:]


def _stream(key, nonce, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def _xor(a, b):
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big") if a else b""


def encrypt(obj, password, gz=True, iterations=ITERATIONS):
    pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if gz:
        pt = gzip.compress(pt, 9)
    salt, nonce = os.urandom(16), os.urandom(12)
    ek, mk = _keys(password, salt, iterations)
    ct = _xor(pt, _stream(ek, nonce, len(pt)))
    tag = hmac.new(mk, salt + nonce + ct, hashlib.sha256).digest()
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "it": iterations, "gz": gz, "salt": b(salt), "nonce": b(nonce),
                       "ct": b(ct), "tag": b(tag)}).encode()


def decrypt(blob, password):
    d = json.loads(blob)
    salt, nonce, ct, tag = (base64.b64decode(d[k]) for k in ("salt", "nonce", "ct", "tag"))
    ek, mk = _keys(password, salt, int(d.get("it", ITERATIONS)))
    if not hmac.compare_digest(hmac.new(mk, salt + nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("Falsches Passwort oder beschädigte Daten")
    pt = _xor(ct, _stream(ek, nonce, len(ct)))
    if d.get("gz"):
        pt = gzip.decompress(pt)
    return json.loads(pt.decode("utf-8"))


# ---------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self, repo, token):
        self.repo = repo.strip().strip("/").replace("https://github.com/", "")
        self.token = token.strip()
        self._branch = None

    def call(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(4):  # kurze Verbindungsabbrüche bei GitHub abfangen
            try:
                return http_json(API + path, data=data, headers=headers, method=method, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                self._http_error(e, path)
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError("Verbindung zu GitHub abgebrochen – wird beim nächsten Abgleich automatisch erneut versucht.") from e

    def _http_error(self, e, path):
        if e.code == 401:
            raise RuntimeError("GitHub lehnt den Token ab (abgelaufen oder falsch).")
        if e.code == 403:
            raise RuntimeError("GitHub verweigert den Zugriff. Hat der Token 'Contents: Read and write' für das Repository?")
        if e.code == 404:
            raise FileNotFoundError(path)
        if e.code == 409:
            raise RuntimeError("Das Repository ist leer. Lege es mit 'Add a README file' an.")
        raise RuntimeError(f"GitHub-Fehler {e.code}")

    def token_expiry(self):
        """Ablaufdatum des Tokens laut GitHub (Header bei jeder Antwort), z. B. '2027-02-15 10:00:00 UTC' – oder None."""
        import urllib.request
        req = urllib.request.Request(API + f"/repos/{self.repo}", headers={"Authorization": "Bearer " + self.token,
                                     "Accept": "application/vnd.github+json", "User-Agent": "GamingRecap"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers.get("github-authentication-token-expiration")

    def info(self):
        try:
            r = self.call("GET", f"/repos/{self.repo}")
        except FileNotFoundError:
            raise RuntimeError(f"Repository '{self.repo}' nicht gefunden. Format: benutzername/gaming-recap")
        self._branch = r.get("default_branch", "main")
        return r

    @property
    def branch(self):
        if not self._branch:
            self.info()
        return self._branch

    def pages_url(self):
        owner, name = self.repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io/"
        return f"https://{owner.lower()}.github.io/{name}/"

    def list_edits(self):
        try:
            items = self.call("GET", f"/repos/{self.repo}/contents/edits?ref={self.branch}")
        except FileNotFoundError:
            return []
        return [i for i in items if i.get("type") == "file" and i.get("name", "").endswith(".enc")]

    def read_file(self, item):
        r = self.call("GET", f"/repos/{self.repo}/contents/{item['path']}?ref={self.branch}")
        return base64.b64decode(r.get("content", ""))

    def commit(self, files, delete=(), message="Gaming Recap Abgleich"):
        """Schreibt mehrere Dateien und Löschungen in einem einzigen Commit."""
        if not files and not delete:
            return None
        br = self.branch
        blobs = []
        for path, content in files.items():
            blob = self.call("POST", f"/repos/{self.repo}/git/blobs",
                             {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        # Das Handy kann gleichzeitig Änderungen hochladen -> bei Konflikt mit frischem Stand neu versuchen
        last = None
        for attempt in range(5):
            try:
                head = self.call("GET", f"/repos/{self.repo}/git/ref/heads/{br}")["object"]["sha"]
                base_tree = self.call("GET", f"/repos/{self.repo}/git/commits/{head}")["tree"]["sha"]
                existing = set()
                if delete:
                    tr = self.call("GET", f"/repos/{self.repo}/git/trees/{base_tree}?recursive=1")
                    existing = {x["path"] for x in tr.get("tree", [])}
                tree = blobs + [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in delete if p in existing]
                if not tree:
                    return None
                t = self.call("POST", f"/repos/{self.repo}/git/trees", {"base_tree": base_tree, "tree": tree})
                c = self.call("POST", f"/repos/{self.repo}/git/commits", {"message": message, "tree": t["sha"], "parents": [head]})
                self.call("PATCH", f"/repos/{self.repo}/git/refs/heads/{br}", {"sha": c["sha"]})
                return c["sha"]
            except RuntimeError as e:
                last = e
                if "422" not in str(e):
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise last

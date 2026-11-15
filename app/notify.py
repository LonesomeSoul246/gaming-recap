"""Windows-Benachrichtigungen (Toasts) ohne Zusatzpakete, über PowerShell."""
import os
import subprocess
from xml.sax.saxutils import escape

_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($env:GR_TOAST)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


PUSH = {"topic": None}  # vom Server gesetzt (Einstellungen)


def push(title, body, url=None, topic=None, delay=None, sid=None):
    """Push aufs Handy über ntfy.sh (App „ntfy“, Thema abonnieren). Kein Konto nötig.
    delay: Unix-Zeit für zeitversetzte Zustellung (max. 3 Tage), sid: Sequenz-ID zum späteren Löschen."""
    import urllib.request
    t = topic or PUSH.get("topic")
    if not t:
        return False
    try:
        headers = {"Title": _hdr(title), "Tags": "video_game"}
        if url:
            headers["Click"] = url
        if delay:
            headers["Delay"] = str(int(delay))
        if sid:
            headers["X-Sequence-ID"] = sid
        req = urllib.request.Request(f"https://ntfy.sh/{t}", data=body.encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print("  Push fehlgeschlagen:", e)
        return False


def cancel(sid, topic=None):
    """Geplante (oder zugestellte) Nachricht mit dieser Sequenz-ID löschen."""
    import urllib.request
    t = topic or PUSH.get("topic")
    if not t or not sid:
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{t}/{sid}", method="DELETE"), timeout=15).read()
        return True
    except Exception as e:
        print("  Push löschen fehlgeschlagen:", e)
        return False


def _hdr(text):
    # HTTP-Header müssen latin-1 sein: Umlaute gehen, Emojis nach RFC 2047
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        import base64
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def toast(title, body, url=None, phone=True, win=True):
    """Zeigt eine Windows-Benachrichtigung und schickt (falls eingerichtet) einen Push aufs Handy."""
    print(f"  [Hinweis] {title}: {body}")
    if phone:
        push(title, body, url)
    if os.name != "nt" or not win:
        return False
    launch = f' launch="{escape(url)}" activationType="protocol"' if url else ""
    xml = (f"<toast{launch}><visual><binding template='ToastGeneric'>"
           f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _PS],
                         env=dict(os.environ, GR_TOAST=xml), creationflags=0x08000000,  # CREATE_NO_WINDOW
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

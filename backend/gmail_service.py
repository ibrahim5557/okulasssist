"""Local Gmail connector: read-only, small bounded result set, no message storage."""
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = ROOT / 'data' / 'gmail_token.json'
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
lock = threading.Lock()

def demo():
    # .env dosyasında GMAIL_DEMO=1 varsa gerçek Gmail yerine örnek mailler gösterilir
    return os.getenv('GMAIL_DEMO', '').strip() == '1'

def connected():
    return demo() or TOKEN.is_file()

DEMO_MAILLER = [
    (0, 'Bilgisayar ve Bilişim Bilimleri Fakültesi <bbf@sakarya.edu.tr>', 'Ara sınav takvimi yayımlandı',
     'Değerli öğrencilerimiz, 2026-2027 Güz yarıyılı ara sınav takvimi fakülte web sayfasında yayımlanmıştır. Sınav yerlerinizi kontrol ediniz.', True),
    (1, 'Sistem Programlama <sistem.programlama@sakarya.edu.tr>', 'Ödev 1 teslim hatırlatması',
     'Ödev 1 için son teslim tarihi 27.11.2026. Kaynak kodlarınızı SABİS üzerinden zip olarak yükleyiniz.', True),
    (2, 'Kütüphane ve Dokümantasyon <kutuphane@sakarya.edu.tr>', 'Sınav dönemi uzatılmış çalışma saatleri',
     'Ara sınav dönemi boyunca merkez kütüphane hafta içi 08:00-24:00 saatleri arasında hizmet verecektir.', False),
    (4, 'Öğrenci İşleri <ogrenciisleri@sakarya.edu.tr>', 'Ders ekle-bırak dönemi sonuçları',
     'Ders ekle-bırak işlemleri sonuçlanmıştır. Danışman onayınızı SABİS üzerinden kontrol edebilirsiniz.', False),
]

def demo_messages(query):
    simdi = datetime.now()
    q = (query or '').lower()
    mesajlar = []
    for i, (gun_once, gonderen, baslik, ozet, okunmamis) in enumerate(DEMO_MAILLER):
        if 'sakarya.edu.tr' in q or not q or any(k in (gonderen + baslik + ozet).lower() for k in re.findall(r'[\wçğıöşü]+', q)):
            tarih = (simdi - timedelta(days=gun_once, hours=i * 3)).strftime('%a, %d %b %Y %H:%M')
            mesajlar.append({'id': 'demo' + str(i), 'gonderen': gonderen, 'baslik': baslik, 'tarih': tarih,
                             'ozet': ozet, 'okunmamis': okunmamis})
    return mesajlar

def service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if not connected():
        raise RuntimeError('Gmail hesabı bağlı değil. Önce bağlantı adımlarını tamamlayın.')
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding='utf-8')
    if not creds.valid:
        raise RuntimeError('Gmail bağlantısının süresi doldu. Bağlantıyı yeniden kurun.')
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)

def fetch_messages(query):
    if demo():
        return demo_messages(query)
    with lock:
        api = service()
        response = api.users().messages().list(userId='me', q=query, maxResults=20).execute()
        messages=[]
        for entry in response.get('messages', []):
            mid=entry.get('id','')
            if not re.fullmatch(r'[a-fA-F0-9]+',mid):
                continue
            m=api.users().messages().get(userId='me',id=mid,format='metadata',metadataHeaders=['From','Subject','Date']).execute()
            headers={x['name'].lower():x.get('value','') for x in m.get('payload',{}).get('headers',[])}
            messages.append({'id':mid,'gonderen':headers.get('from',''), 'baslik':headers.get('subject','(Konu yok)'), 'tarih':headers.get('date',''), 'ozet':m.get('snippet',''), 'okunmamis': 'UNREAD' in m.get('labelIds',[])})
        return messages

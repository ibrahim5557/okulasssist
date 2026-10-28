"""Run on the same computer as the local demo; opens Google OAuth in a browser."""
from pathlib import Path
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = ROOT / 'credentials.json'
TOKEN = ROOT / 'data' / 'gmail_token.json'
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def main():
    if not CREDENTIALS.is_file():
        print('credentials.json bulunamadı. Google Cloud üzerinden indirip proje klasörüne koyun.')
        return 1
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
        creds = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True)
        TOKEN.parent.mkdir(exist_ok=True)
        TOKEN.write_text(creds.to_json(), encoding='utf-8')
        print('Gmail bağlantısı kaydedildi. OkulAsist ekranına dönüp Gmail yenile butonuna basın.')
        return 0
    except Exception as exc:
        print('Bağlantı tamamlanamadı. Google izinleri veya ağ bağlantısını kontrol edin.')
        print('Hata türü:', type(exc).__name__)
        return 1

if __name__ == '__main__':
    sys.exit(main())

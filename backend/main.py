import json, os, re, time, uuid, sqlite3, threading, hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import pymupdf as fitz
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import JSONResponse
from backend import gmail_service
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY")) if os.getenv("GEMINI_API_KEY") else None
MODEL = os.getenv("MODEL", "gemini-3.8-flash")
FALLBACK = os.getenv("FALLBACK_MODEL", "").strip()  # 503 durumunda yedek model (opsiyonel)
MAX_CHARS = 120_000
MAX_PDF_MB = 20

SCHEMA = (
    ' SADECE şu JSON nesnesini döndür: {"baslik":"kısa başlık","sure_dk":10,"kartlar":['
    '{"baslik":"kısa konu başlığı","icerik":"2-4 cümlelik açıklama","sayfa":"3 veya 3-4",'
    '"onem":"yuksek|orta|dusuk","ornek":"kısa örnek (yoksa boş string)"}]}'
)
PROMPTS = {
    "10": ("Aşağıdaki ders notunu üniversite öğrencisi için ~10 dakikada okunacak şekilde özetle. "
           "5-7 kart üret; her kart tek bir ana kavram, tanım veya formül olsun. Kısa tut, ornek alanını boş bırak."),
    "30": ("Aşağıdaki ders notunu ~30 dakikalık detaylı çalışma özeti olarak hazırla. "
           "8-12 kart üret, notun akışını izle; her kartta kavramı açıkla ve ornek alanına kısa bir örnek yaz."),
    "hazirlik": ("Öğrenciye akşam bildirimi olarak gidecek 'yarın için hazırlık' notu hazırla. Derse girmeden "
                 "bakılması gereken EN ÖNEMLİ 3 konuyu seç (3 kart). icerik alanına tek cümleyle neden önemli olduğunu yaz, "
                 "sayfa alanına nerede olduğunu yaz, onem alanını önem sırasına göre doldur."),
}
RULES = (" Sadece verilen içeriğe dayan, içerikte olmayan bilgi ekleme. Türkçe yaz. "
         "Metin [Sayfa N] etiketleriyle gelir, sayfa alanını bunlara göre doldur." + SCHEMA)
QUIZ_PROMPT = ("Ders notundan 5 çoktan seçmeli soru üret. SADECE şu JSON dizisini döndür: "
               '[{"soru":"...","secenekler":["A","B","C","D"],"dogru":0,"aciklama":"..."}] '
               "dogru = doğru seçeneğin indeksi (0-3). Türkçe yaz, sadece nota dayan.")

DATA = json.loads((Path(__file__).parent / "mock_data.json").read_text(encoding="utf-8"))
DB = ROOT / "data" / "okulasist.db"
DB.parent.mkdir(exist_ok=True)

def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

with db() as conn:
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, ders TEXT NOT NULL, baslik TEXT NOT NULL, text TEXT NOT NULL, pdf BLOB NOT NULL, created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS results (note_id TEXT NOT NULL, mode TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(note_id, mode), FOREIGN KEY(note_id) REFERENCES notes(id));
      CREATE TABLE IF NOT EXISTS analysis_status (note_id TEXT PRIMARY KEY, status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hidden_announcements (id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS read_notes (id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS done_tasks (id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS card_progress (note_id TEXT NOT NULL, mode TEXT NOT NULL, card_index INTEGER NOT NULL, PRIMARY KEY(note_id, mode, card_index));
      CREATE TABLE IF NOT EXISTS sabis_grades (anahtar TEXT PRIMARY KEY, ders_kodu TEXT NOT NULL, ders_adi TEXT NOT NULL, sira INTEGER NOT NULL, tip TEXT NOT NULL, oran TEXT NOT NULL, notu TEXT NOT NULL, guncellendi TEXT NOT NULL, okundu INTEGER NOT NULL DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS sabis_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sabis_duyurular (id TEXT PRIMARY KEY, tur TEXT NOT NULL, ders TEXT NOT NULL, baslik TEXT NOT NULL, link TEXT NOT NULL, zaman_metin TEXT NOT NULL, zaman TEXT NOT NULL, son_teslim TEXT NOT NULL, eklendi TEXT NOT NULL, okundu INTEGER NOT NULL DEFAULT 0);
    """)
    conn.execute("UPDATE analysis_status SET status='error' WHERE status='pending'")

SABIS_YOLU = "/api/sabis/notlar"
locks_guard = threading.Lock()
locks = {}
app = FastAPI()

@app.middleware("http")
async def protect_local_api(request: Request, call_next):
    # Browser-originated requests must come from this same local app.
    origin = request.headers.get("origin")
    if request.url.path == SABIS_YOLU:
        return await call_next(request)  # kendi kontrolünü yapar: sadece Chrome eklentisinden kabul edilir
    if request.url.path.startswith("/api/") and origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail":"Bu istek farklı bir kaynaktan geldi."}, status_code=403)
    if request.url.path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail":"Bu istek farklı bir kaynaktan geldi."}, status_code=403)
    return await call_next(request)


def extract(data: bytes) -> str:
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(f"[Sayfa {i}]\n{p.get_text()}" for i, p in enumerate(doc, 1))[:MAX_CHARS]


def ask(note: dict, system: str, json_out: bool = False) -> str:
    if client is None:
        raise HTTPException(503, "Gemini API anahtarı eksik. .env dosyasını kontrol et ve sunucuyu yeniden başlat.")
    if len(note["text"].strip()) >= 50:
        contents = note["text"]
    else:  # taranmış PDF: dosyayı doğrudan Gemini'ye ver
        contents = [types.Part.from_bytes(data=note["bytes"], mime_type="application/pdf"),
                    "Bu PDF'in içeriğini kullan."]
    cfg = types.GenerateContentConfig(
        system_instruction=system, max_output_tokens=8000, temperature=0.3,
        response_mime_type="application/json" if json_out else "text/plain")
    last = None
    for model in [m for m in (MODEL, FALLBACK) if m]:
        for wait in (0, 2, 4):  # 503/429 için artan bekleme
            time.sleep(wait)
            try:
                r = client.models.generate_content(model=model, contents=contents, config=cfg)
                if r.text:
                    return r.text
                last = "Model boş yanıt döndürdü."
            except Exception as e:
                last = str(e)
                if not any(code in last for code in ("429", "503", "UNAVAILABLE", "500", "502", "504")):
                    break
    if last and ("503" in last or "UNAVAILABLE" in last or "429" in last):
        raise HTTPException(503, "Gemini şu an yoğun. Birkaç saniye sonra tekrar dene.")
    raise HTTPException(502, "Gemini isteği tamamlanamadı. API anahtarını ve erişebildiğin model adını kontrol et.")


def parse_json(raw: str):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)


def get_note(nid: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
    if not row:
        raise HTTPException(404, "Not bulunamadı")
    return dict(row)


def generate(nid: str, mode: str) -> str:
    with locks_guard:
        lock = locks.setdefault((nid, mode), threading.Lock())
    with lock:
        return generate_locked(nid, mode)

def generate_locked(nid: str, mode: str) -> str:
    note = get_note(nid)
    with db() as conn:
        cached = conn.execute("SELECT result FROM results WHERE note_id = ? AND mode = ?", (nid, mode)).fetchone()
    if cached:
        return cached["result"]
    note["bytes"] = note["pdf"]
    result = ask(note, QUIZ_PROMPT if mode == "quiz" else PROMPTS[mode] + RULES, json_out=True)
    try:
        parsed = parse_json(result)
        if (mode == "quiz" and not isinstance(parsed, list)) or (mode != "quiz" and (not isinstance(parsed, dict) or not isinstance(parsed.get("kartlar"), list))):
            raise ValueError("Yanıt biçimi geçersiz")
    except (ValueError, TypeError):
        raise HTTPException(502, "Yanıt okunamadı, tekrar dene")
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO results (note_id, mode, result) VALUES (?, ?, ?)", (nid, mode, result))
    return result

def analyze(nid: str):
    try:
        generate(nid, "hazirlik")
        status = "ready"
    except Exception:
        status = "error"
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO analysis_status VALUES (?, ?)", (nid, status))

@app.get("/api/feed")
def feed():
    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    with db() as conn:
        hidden = {row[0] for row in conn.execute("SELECT id FROM hidden_announcements")}
        read = {row[0] for row in conn.execute("SELECT id FROM read_notes")}
        done_tasks = {row[0] for row in conn.execute("SELECT id FROM done_tasks")}
        rows = conn.execute("SELECT n.id, n.ders, n.baslik, n.created, a.status, r.result FROM notes n LEFT JOIN analysis_status a ON a.note_id=n.id LEFT JOIN results r ON r.note_id=n.id AND r.mode='hazirlik' ORDER BY n.created DESC").fetchall()
    live = []
    for row in rows:
        topics = []
        if row["result"]:
            try:
                topics = [str(x.get("baslik", "")) for x in parse_json(row["result"]).get("kartlar", []) if isinstance(x, dict)][:3]
            except (ValueError, AttributeError, TypeError):
                pass
        live.append({"id": row["id"], "kaynak": "Ders notu", "ders": row["ders"], "baslik": "Yeni ders notu: " + row["baslik"], "zaman": row["created"][:16].replace("T", " "), "pdf": True, "analiz": "ready" if topics else (row["status"] or "none"), "konular": topics, "okundu": row["id"] in read})
    sabis_duyuru, sabis_odev = sabis_duyuru_listesi()
    archived = []
    for item in ([] if sabis_duyuru else DATA["duyurular"]):  # gerçek SABİS duyurusu gelince örnek kayıtlar gizlenir
        fingerprint = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        announcement_id = "arsiv_" + fingerprint
        if announcement_id not in hidden:
            archived.append({**item, "arsiv_id": announcement_id})
    tasks = []
    kaynak_odevler = sabis_odev if sabis_odev else [{**item, "task_id": "odev_" + str(i)} for i, item in enumerate(DATA["odevler"])]
    for item in kaynak_odevler:
        task_id = item["task_id"]
        try:
            due = datetime.strptime(item["son"], "%d.%m.%Y").date()
            remaining = (due - now.date()).days
        except ValueError:
            remaining = None
        tasks.append({**item, "kalan_gun": remaining, "tamamlandi": task_id in done_tasks})
    tasks.sort(key=lambda x: (x["tamamlandi"], x["kalan_gun"] is None or x["kalan_gun"] < 0, x["kalan_gun"] if x["kalan_gun"] is not None else 999999))
    return {"sabis": sabis_ozet(),
            "tarih": now.strftime("%d.%m.%Y"), "program": DATA["program"], "dersler_bugun": [x for x in DATA["program"] if x["gun"] == now.weekday()],
            "dersler_yarin": [x for x in DATA["program"] if x["gun"] == (now.weekday()+1)%7],
            "dersler": [x["ders"] for x in DATA["program"]],
            "duyurular": live + sabis_duyuru + archived, "odevler": tasks}


class Toggle(BaseModel):
    value: bool

@app.post("/api/notes/read-all")
def mark_all_read():
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO read_notes (id) SELECT id FROM notes")
    return {"ok": True}

@app.post("/api/notes/{nid}/read")
def mark_read(nid: str):
    get_note(nid)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO read_notes VALUES (?)", (nid,))
    return {"ok": True}

@app.post("/api/tasks/{task_id}")
def update_task(task_id: str, body: Toggle):
    with db() as conn:
        sabis_ids = {"sabis_" + r[0] for r in conn.execute("SELECT id FROM sabis_duyurular WHERE tur='odev'")}
    if task_id not in {"odev_" + str(i) for i in range(len(DATA["odevler"]))} | sabis_ids:
        raise HTTPException(404, "Ödev bulunamadı")
    with db() as conn:
        if body.value: conn.execute("INSERT OR IGNORE INTO done_tasks VALUES (?)", (task_id,))
        else: conn.execute("DELETE FROM done_tasks WHERE id = ?", (task_id,))
    return {"ok": True}

@app.get("/api/progress/{nid}/{mode}")
def get_progress(nid: str, mode: str):
    get_note(nid)
    if mode not in PROMPTS: raise HTTPException(400, "Geçersiz mod")
    with db() as conn:
        return {"completed": [x[0] for x in conn.execute("SELECT card_index FROM card_progress WHERE note_id=? AND mode=? ORDER BY card_index", (nid,mode))]}

@app.post("/api/progress/{nid}/{mode}/{index}")
def update_progress(nid: str, mode: str, index: int, body: Toggle):
    get_note(nid)
    if mode not in PROMPTS or index < 0 or index > 100: raise HTTPException(400, "Geçersiz kart")
    with db() as conn:
        if body.value: conn.execute("INSERT OR IGNORE INTO card_progress VALUES (?, ?, ?)", (nid, mode, index))
        else: conn.execute("DELETE FROM card_progress WHERE note_id=? AND mode=? AND card_index=?", (nid,mode,index))
    return {"ok": True}


class MailQuery(BaseModel):
    query: str = ""

@app.get("/api/gmail/status")
def gmail_status():
    return {"connected": gmail_service.connected()}

@app.post("/api/gmail/sync")
def gmail_sync(body: MailQuery):
    if not gmail_service.connected():
        raise HTTPException(400, "Gmail bağlı değil. Önce kurulum adımlarını tamamla.")
    if len(body.query) > 300:
        raise HTTPException(400, "Arama filtresi çok uzun.")
    try:
        return {"messages": gmail_service.fetch_messages(body.query.strip())}
    except Exception:
        raise HTTPException(502, "Gmail okunamadı. Hesap izinlerini ve internet bağlantını kontrol et; gerekirse bağlantıyı yeniden kur.")


# ---------- SABİS notları (OkulAsist SABİS Chrome eklentisinden gelir) ----------

def tr_kucuk(metin: str) -> str:
    return " ".join(str(metin).replace("İ", "i").replace("I", "ı").lower().split())

def program_index(ders_adi: str):
    hedef = tr_kucuk(ders_adi)
    for i, x in enumerate(DATA["program"]):
        if tr_kucuk(x["ders"]) == hedef:
            return i
    return None

def kisa(deger, uzunluk=200) -> str:
    return str(deger if deger is not None else "").strip()[:uzunluk]

def sabis_ozet():
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sabis_grades ORDER BY ders_kodu, sira")]
        meta = conn.execute("SELECT v FROM sabis_meta WHERE k='son_senkron'").fetchone()
    dersler = {}
    for r in rows:
        if r["demo"]:
            continue
        d = dersler.setdefault(r["ders_kodu"], {"kod": r["ders_kodu"], "ad": r["ders_adi"], "program_index": program_index(r["ders_adi"]), "satirlar": []})
        d["satirlar"].append({"anahtar": r["anahtar"], "tip": r["tip"], "oran": r["oran"], "not": r["notu"], "yeni": bool(r["notu"]) and not r["okundu"]})
    aciklanan = sorted([r for r in rows if r["notu"]], key=lambda r: r["guncellendi"], reverse=True)[:20]
    return {
        "son_senkron": meta["v"] if meta else None,
        "dersler": list(dersler.values()),
        "notlar": [{"anahtar": r["anahtar"], "kod": r["ders_kodu"], "ad": r["ders_adi"], "tip": r["tip"], "oran": r["oran"],
                    "not": r["notu"], "zaman": r["guncellendi"][:16].replace("T", " "), "okundu": bool(r["okundu"]),
                    "demo": bool(r["demo"]), "program_index": program_index(r["ders_adi"])} for r in aciklanan],
    }

AYLAR = {"ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6, "temmuz": 7,
         "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12}

def sabis_tarih(metin: str) -> str:
    """'26 Ekim, 12:54' -> ISO tarih. SABİS yıl yazmıyor; gelecekte kalırsa geçen yıl sayılır."""
    m = re.match(r"\s*(\d{1,2})\s+(\S+?),?\s+(\d{1,2}):(\d{2})", metin or "")
    if not m or tr_kucuk(m.group(2)) not in AYLAR:
        return ""
    simdi = datetime.now(ZoneInfo("Europe/Istanbul"))
    try:
        t = datetime(simdi.year, AYLAR[tr_kucuk(m.group(2))], int(m.group(1)), int(m.group(3)), int(m.group(4)), tzinfo=simdi.tzinfo)
    except ValueError:
        return ""
    if t > simdi + timedelta(days=2):
        t = t.replace(year=t.year - 1)
    return t.isoformat()

def ders_gorunen(ad: str) -> str:
    i = program_index(ad)
    return DATA["program"][i]["ders"] if i is not None else ad.title()

def sabis_duyuru_listesi():
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sabis_duyurular ORDER BY zaman DESC, eklendi DESC")]
    duyurular, odevler = [], []
    for r in rows:
        link = "https://obs.sabis.sakarya.edu.tr" + r["link"] if r["link"].startswith("/") else ""
        ders = ders_gorunen(r["ders"])
        duyurular.append({"sabis_id": r["id"], "kaynak": "SABİS · Ödev" if r["tur"] == "odev" else "SABİS · Duyuru",
                          "ders": ders, "baslik": r["baslik"], "zaman": r["zaman_metin"], "link": link, "yeni": not r["okundu"]})
        if r["tur"] == "odev":
            odevler.append({"ders": ders, "baslik": "SABİS ödevi · verildi " + r["zaman_metin"], "son": r["son_teslim"] or "-",
                            "task_id": "sabis_" + r["id"], "link": link})
    return duyurular, odevler

def sabis_duyurulari_kaydet(conn, duyurular, simdi: str) -> int:
    ilk = conn.execute("SELECT COUNT(*) FROM sabis_duyurular").fetchone()[0] == 0
    yeni = 0
    for d in duyurular[:100]:
        if not isinstance(d, dict):
            continue
        zaman_metin, ders, baslik = kisa(d.get("zaman"), 40), kisa(d.get("ders")), kisa(d.get("baslik"), 500)
        link = kisa(d.get("link"), 200)
        if not link.startswith("/"):
            link = ""
        tur = "odev" if d.get("tur") == "odev" else "duyuru"
        if not (ders or baslik):
            continue
        did = hashlib.sha256(f"{link}|{zaman_metin}|{baslik}".encode("utf-8")).hexdigest()[:24]
        son = re.search(r"(\d{2}\.\d{2}\.\d{4})", baslik)
        cur = conn.execute("INSERT OR IGNORE INTO sabis_duyurular VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (did, tur, ders, baslik, link, zaman_metin, sabis_tarih(zaman_metin) or simdi,
                            son.group(1) if son else "", simdi, 1 if ilk else 0))
        yeni += 0 if ilk else cur.rowcount
    return yeni

@app.post(SABIS_YOLU)
async def sabis_notlar(request: Request):
    # Tarayıcıdaki siteler Origin başlığını değiştiremez; bu yüzden sadece Chrome eklentisinden gelen istek kabul edilir.
    if not (request.headers.get("origin") or "").startswith("chrome-extension://"):
        raise HTTPException(403, "Bu adres sadece OkulAsist SABİS eklentisi içindir.")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Geçersiz veri")
    if not isinstance(data, dict):
        raise HTTPException(400, "Geçersiz veri")
    simdi = datetime.now(ZoneInfo("Europe/Istanbul")).isoformat()

    if data.get("demo"):
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO sabis_grades VALUES (?,?,?,?,?,?,?,?,0,1)",
                         ("DEMO#SWE301#0", "SWE301", "SİSTEM PROGRAMLAMA", 0, "Ara Sınav", "36", "85", simdi))
        return {"ok": True, "yeni": 1, "demo": True}

    dersler = data.get("dersler")
    if not isinstance(dersler, list) or len(dersler) > 60:
        raise HTTPException(400, "Ders listesi bekleniyordu")
    yeni = 0
    with db() as conn:
        mevcut = {r["anahtar"]: r["notu"] for r in conn.execute("SELECT anahtar, notu FROM sabis_grades WHERE demo=0")}
        ilk_senkron = not mevcut
        for ders in dersler:
            if not isinstance(ders, dict):
                continue
            kod = kisa(ders.get("kod"), 30)
            ad = kisa(ders.get("ad"))
            satirlar = ders.get("satirlar") if isinstance(ders.get("satirlar"), list) else []
            if not kod:
                continue
            for i, satir in enumerate(satirlar[:30]):
                if not isinstance(satir, dict):
                    continue
                anahtar = f"{kod}#{i}"
                notu = kisa(satir.get("not"), 20)
                tip, oran = kisa(satir.get("tip"), 100), kisa(satir.get("oran"), 20)
                if anahtar not in mevcut:
                    conn.execute("INSERT INTO sabis_grades VALUES (?,?,?,?,?,?,?,?,?,0)",
                                 (anahtar, kod, ad, i, tip, oran, notu, simdi, 1 if (ilk_senkron or not notu) else 0))
                    yeni += 0 if (ilk_senkron or not notu) else 1
                elif mevcut[anahtar] != notu:
                    conn.execute("UPDATE sabis_grades SET ders_adi=?, tip=?, oran=?, notu=?, guncellendi=?, okundu=? WHERE anahtar=?",
                                 (ad, tip, oran, notu, simdi, 0 if notu else 1, anahtar))
                    yeni += 1 if notu else 0
                else:
                    conn.execute("UPDATE sabis_grades SET ders_adi=?, tip=?, oran=? WHERE anahtar=?", (ad, tip, oran, anahtar))
        yeni_duyuru = sabis_duyurulari_kaydet(conn, data["duyurular"], simdi) if isinstance(data.get("duyurular"), list) else 0
        conn.execute("INSERT OR REPLACE INTO sabis_meta VALUES ('son_senkron', ?)", (simdi,))
    return {"ok": True, "yeni": yeni, "yeni_duyuru": yeni_duyuru}

@app.post("/api/sabis/okundu")
def sabis_okundu():
    with db() as conn:
        conn.execute("UPDATE sabis_grades SET okundu=1")
        conn.execute("UPDATE sabis_duyurular SET okundu=1")
    return {"ok": True}


@app.delete("/api/announcements/{announcement_id}")
def hide_announcement(announcement_id: str):
    valid_ids = {"arsiv_" + hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24] for item in DATA["duyurular"]}
    if announcement_id not in valid_ids:
        raise HTTPException(404, "Geçmiş duyuru bulunamadı")
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO hidden_announcements (id) VALUES (?)", (announcement_id,))
    return {"ok": True}


@app.post("/api/teacher/upload")
async def teacher_upload(bg: BackgroundTasks, ders: str = Form(...), baslik: str = Form(""),
                         file: UploadFile = File(...)):
    raw = await file.read()
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Sadece PDF yüklenebilir")
    if len(raw) > MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF en fazla {MAX_PDF_MB} MB olabilir")
    nid = uuid.uuid4().hex[:8]
    baslik = baslik or file.filename
    try:
        text = extract(raw)
    except Exception:
        raise HTTPException(400, "PDF açılamadı. Dosyanın geçerli ve şifresiz olduğundan emin ol.")
    if ders not in [x["ders"] for x in DATA["program"]]:
        raise HTTPException(400, "Lütfen listeden bir ders seç.")
    with db() as conn:
        conn.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?)", (nid, ders, baslik, text, raw, datetime.now(ZoneInfo("Europe/Istanbul")).isoformat()))
        conn.execute("INSERT INTO analysis_status VALUES (?, ?)", (nid, "pending"))
    bg.add_task(analyze, nid)
    return {"id": nid}


@app.get("/api/notes/{nid}/{mode}")
def note_result(nid: str, mode: str):
    if mode not in (*PROMPTS, "quiz"):
        raise HTTPException(400, "Geçersiz mod")
    out = generate(nid, mode)
    try:
        data = parse_json(out)
    except json.JSONDecodeError:
        with db() as conn:
            conn.execute("DELETE FROM results WHERE note_id = ? AND mode = ?", (nid, mode))
        raise HTTPException(502, "Yanıt okunamadı, tekrar dene")
    note = get_note(nid)
    if mode == "quiz":
        return {"quiz": data}
    return {"mode": mode, "ders": note["ders"], "baslik": note["baslik"], "ozet": data}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True))

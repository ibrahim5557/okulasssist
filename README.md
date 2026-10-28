# OkulAsist — hackathon demosu

## Windows kurulumu
1. ZIP dosyasını çıkarıp klasörde Komut İstemi aç.
2. `python -m venv venv`
3. `venv\Scripts\activate`
4. `pip install -r backend/requirements.txt`
5. `copy .env.example .env` komutunu çalıştır ve `notepad .env` ile kendi Gemini API anahtarını yaz. Anahtarını paylaşma.
6. `python -m uvicorn backend.main:app --reload --host 127.0.0.1`
7. Bilgisayarda http://127.0.0.1:8000 adresini aç.

Telefonla aynı ağda denemek için host değerini `0.0.0.0` yap ve bilgisayarın yerel IP adresini kullan. Bu demo giriş/kimlik doğrulama sağlamaz; yalnızca güvendiğin bir ağda kısa süreli çalıştır.

## Demo
Hoca modu alanından ders adını ve PDF seçip yükle; duyurudan notu aç ve hazırlık, 10/30 dakika veya quiz seç. İçerik seçilince Gemini'ye gönderilir. Süreler yaklaşık hedeflerdir. Sunucu kapanınca yüklenen PDF ve sonuçlar silinir. Dersler, ödevler, SABİS ve mail akışı örnek veridir; gerçek entegrasyon değildir. Ekran bildirimi sayfa açıkken çalışır; gerçek arka plan push ve planlı akşam bildirimi yoktur. Bildirim izni güvenli bağlam (HTTPS veya localhost) gerektirir.

## Gemini hataları
503 servis yoğunluğudur; uygulama sınırlı sayıda tekrar dener. Sonuç alınamazsa daha sonra yeniden dene. İstersen `.env` dosyasındaki `FALLBACK_MODEL` alanına hesabında generateContent erişimi olan farklı bir model yaz ve sunucuyu yeniden başlat. Farklı modelin çalışması garanti değildir. 429 kota veya hız sınırına işaret edebilir; Google AI Studio kotanı kontrol et. Model adlarını hesabına göre Google AI Studio'da doğrula. Taranmış PDF Gemini'ye dosya olarak, metin içeren PDF çıkarılan metin olarak gönderilir. İlk 120.000 karakter işlenir. Gizli veya paylaşım izni olmayan notları yükleme.

## Kalıcı kayıt ve kişisel demo verisi
PDF'ler, metin ve üretilen özet/quiz sonuçları ilk çalıştırmada oluşan `data/okulasist.db` SQLite dosyasına kaydedilir. Bu dosyayı silmeyin; yedeklemek için sunucu kapalıyken kopyalayın. Hoca modu bir ders seçerek PDF yükler. İki açık sekmede öğrenci ekranı yaklaşık 5 saniyede güncellenir. Tarayıcı açıkken görünen bildirim demo niteliğindedir; uygulama kapalıyken push ve okunma takibi yoktur. Ders programı 21–25 Eylül 2026 ekran görüntüsünden haftalık olarak varsayılmıştır. SABİS duyuruları elle eklenmiştir ve güncellenmez. Ödev tarihleri de elle eklenmiştir. Özet üretimi için PDF içeriği Gemini'ye gönderilir; önceden kaydedilmemiş içerikler servis yoğunluğunda üretilemeyebilir. Kaynak sayfaları kontrol edin.

"""
Perpanjangan Sewa Kos via Transfer Rekening + Bukti Pembayaran
---------------------------------------------------------------
File ini berdiri sendiri (tidak butuh Midtrans / API key).

Alur:
  Penyewa buka halaman -> pilih kamar, lama perpanjangan, isi nomor HP -> transfer ke
  rekening yang tertera -> unggah bukti transfer -> admin memeriksa di /admin lalu
  menekan Setujui -> masa kos bertambah otomatis dan tercatat sebagai pemasukan
  "Sewa Kamar" di data_keuangan_kos.csv.

Persiapan:
  1. python -m pip install flask
  2. Isi REKENING dan PASSWORD_ADMIN di bagian PENGATURAN di bawah
     (password juga bisa lewat variabel lingkungan ADMIN_PASSWORD).
  3. Jalankan:  python bayar_transfer.py

Halaman penyewa : http://localhost:5002        Halaman admin : http://localhost:5002/admin
"""

import csv
import functools
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import calendar
from datetime import date, datetime

from flask import (Flask, abort, redirect, render_template_string, request, send_from_directory,
                   session, url_for)

BASE = os.path.dirname(os.path.abspath(__file__))
F_TRF = os.path.join(BASE, "pembayaran_transfer.csv")
F_MASA = os.path.join(BASE, "masa_kos.json")                # sama dengan bayar_kos.py
F_KEUANGAN = os.path.join(BASE, "data_keuangan_kos.csv")    # sama dengan server_2.py
DIR_BUKTI = os.path.join(BASE, "bukti_transfer")
KOLOM_T = ["kode", "waktu", "kamar", "hp", "bulan", "jumlah", "bukti", "status", "catatan", "berlaku_sampai"]
KOLOM_K = ["id", "tanggal", "jenis", "kategori", "kamar", "keterangan", "jumlah"]
LOCK = threading.Lock()

# ====== PENGATURAN - UBAH SESUAI KOS ANDA ======
REKENING = [  # boleh lebih dari satu
    {"bank": "BCA", "nomor": "0000000000", "atas_nama": "ISI NAMA PEMILIK REKENING"},
    # {"bank": "BRI", "nomor": "1111222233334444", "atas_nama": "NAMA PEMILIK"},
]
PASSWORD_ADMIN = "kostdago"  # isi password admin di sini (atau pakai variabel lingkungan ADMIN_PASSWORD)

DAFTAR_KAMAR = {
    "2 B": {"penyewa": "Daniel", "tarif": 650_000},
    "3 B": {"penyewa": "Ahmad", "tarif": 500_000},
    "4 B": {"penyewa": "Izul", "tarif": 500_000},
    "5 B": {"penyewa": "Yoga", "tarif": 600_000},
    "6 B": {"penyewa": "Yoan", "tarif": 600_000},
    "7 B": {"penyewa": "Arifin", "tarif": 650_000},
    "4 A": {"penyewa": "Raka", "tarif": 750_000},
    "9 A": {"penyewa": "Akbar + Aji", "tarif": 1_000_000},
    "10 A": {"penyewa": "Rafi", "tarif": 750_000},
    "11 A": {"penyewa": "Gerald", "tarif": 600_000},
    "12 A": {"penyewa": "Radefa", "tarif": 550_000},
    "13 A": {"penyewa": "Hilal", "tarif": 300_000},
}
PILIHAN_BULAN = [1, 2, 3, 6, 12]
CATAT_KE_KEUANGAN = True   # True = yang disetujui otomatis masuk ke data_keuangan_kos.csv
MAKS_BUKTI = 5 * 1024 * 1024  # 5 MB
# ================================================

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or PASSWORD_ADMIN
PORT = int(os.environ.get("PORT", "5002"))
BULAN_SINGKAT = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  MAX_CONTENT_LENGTH=MAKS_BUKTI + 512 * 1024)


def rupiah(n):
    return "Rp {:,.0f}".format(n).replace(",", ".")


def tgl_id(s):
    try:
        d = date.fromisoformat(s)
        return f"{d.day} {BULAN_SINGKAT[d.month]} {d.year}"
    except (TypeError, ValueError):
        return "-"


def nomor_wa(hp):
    hp = re.sub(r"\D", "", hp or "")
    return "62" + hp[1:] if hp.startswith("0") else hp


app.jinja_env.filters.update(rp=rupiah, tgl=tgl_id, wa=nomor_wa)


# ---------- penyimpanan ----------
def muat_t():
    if not os.path.exists(F_TRF):
        return []
    with open(F_TRF, newline="", encoding="utf-8") as f:
        data = list(csv.DictReader(f))
    for d in data:
        d["bulan"], d["jumlah"] = int(d["bulan"]), int(d["jumlah"])
    return data


def simpan_t(data):
    with open(F_TRF, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KOLOM_T)
        w.writeheader()
        w.writerows(data)


def muat_masa():
    try:
        with open(F_MASA, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def simpan_masa(masa):
    with open(F_MASA, "w", encoding="utf-8") as f:
        json.dump(masa, f, indent=2)


def catat_keuangan(rec):
    if not CATAT_KE_KEUANGAN:
        return
    ada, max_id = os.path.exists(F_KEUANGAN), 0
    if ada:
        with open(F_KEUANGAN, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    max_id = max(max_id, int(r["id"]))
                except (ValueError, KeyError):
                    pass
    with open(F_KEUANGAN, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KOLOM_K)
        if not ada:
            w.writeheader()
        w.writerow({"id": max_id + 1, "tanggal": date.today().isoformat(), "jenis": "Pemasukan",
                    "kategori": "Sewa Kamar", "kamar": rec["kamar"],
                    "keterangan": f"Transfer {rec['bulan']} bln (kode {rec['kode']})",
                    "jumlah": rec["jumlah"]})


# ---------- logika ----------
def tambah_bulan(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def status_masa(s):
    try:
        sisa = (date.fromisoformat(s) - date.today()).days
    except (TypeError, ValueError):
        return "Belum diatur", "mut"
    if sisa < 0:
        return "Habis", "out"
    return ("Segera habis", "warn") if sisa <= 7 else ("Aktif", "in")


def jenis_file(head):
    """Kenali tipe file dari isinya (bukan dari nama), hanya JPG/PNG/WEBP/PDF."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"%PDF"):
        return ".pdf"
    return None


# ---------- halaman penyewa ----------
def halaman(inner, judul="Perpanjang Kos", sub="Bayar sewa lewat transfer rekening", **ctx):
    return render_template_string(BASE_HTML, judul=judul, sub=sub,
                                  isi=render_template_string(inner, **ctx))


def tampil_index(galat="", **isian):
    masa, opsi = muat_masa(), []
    for k in DAFTAR_KAMAR:
        s = masa.get(k)
        teks = f"{k} - " + (f"{'berlaku sampai' if status_masa(s)[1] != 'out' else 'habis sejak'} {tgl_id(s)}"
                            if s else "belum diatur")
        opsi.append({"kamar": k, "teks": teks})
    return halaman(INDEX, galat=galat, opsi=opsi, pilihan=PILIHAN_BULAN, rekening=REKENING,
                   tarif={k: v["tarif"] for k, v in DAFTAR_KAMAR.items()},
                   pilih=isian.get("kamar", ""), hp=isian.get("hp", ""))


@app.route("/")
def index():
    return tampil_index()


@app.errorhandler(413)
def terlalu_besar(_):
    return tampil_index("Ukuran file terlalu besar (maksimal 5 MB)."), 413


@app.post("/kirim")
def kirim():
    f = request.form
    kamar, hp = f.get("kamar", ""), re.sub(r"\D", "", f.get("hp", ""))
    isian = {"kamar": kamar, "hp": hp}
    try:
        bulan = int(f.get("bulan", ""))
    except ValueError:
        bulan = 0
    if kamar not in DAFTAR_KAMAR or bulan not in PILIHAN_BULAN:
        return tampil_index("Pilih kamar dan lama perpanjangan.", **isian)
    if not 9 <= len(hp) <= 15:
        return tampil_index("Nomor HP tidak valid.", **isian)
    berkas = request.files.get("bukti")
    if not berkas or not berkas.filename:
        return tampil_index("Unggah bukti transfer terlebih dahulu.", **isian)
    data = berkas.read(MAKS_BUKTI + 1)
    if len(data) > MAKS_BUKTI:
        return tampil_index("Ukuran file terlalu besar (maksimal 5 MB).", **isian)
    ext = jenis_file(data[:12])
    if not ext:
        return tampil_index("Bukti harus berupa foto (JPG/PNG/WEBP) atau PDF.", **isian)

    with LOCK:
        semua = muat_t()
        if any(r["kamar"] == kamar and r["status"] == "menunggu" for r in semua):
            return tampil_index("Pembayaran untuk kamar ini sedang menunggu konfirmasi admin.", **isian)
        kode = secrets.token_hex(4)
        os.makedirs(DIR_BUKTI, exist_ok=True)
        with open(os.path.join(DIR_BUKTI, kode + ext), "wb") as out:
            out.write(data)
        semua.append({"kode": kode, "waktu": datetime.now().strftime("%Y-%m-%d %H:%M"), "kamar": kamar,
                      "hp": hp, "bulan": bulan, "jumlah": DAFTAR_KAMAR[kamar]["tarif"] * bulan,
                      "bukti": kode + ext, "status": "menunggu", "catatan": "", "berlaku_sampai": ""})
        simpan_t(semua)
    return redirect(url_for("status", kode=kode))


@app.route("/status/<kode>")
def status(kode):
    rec = next((r for r in muat_t() if r["kode"] == kode), None)
    if not rec:
        abort(404)
    return halaman(STATUS, rec=rec, judul="Status Pembayaran", sub="Pantau konfirmasi dari admin")


# ---------- admin ----------
def wajib_admin(fn):
    @functools.wraps(fn)
    def bungkus(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*a, **k)
    return bungkus


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    galat = ""
    if not ADMIN_PASSWORD:
        galat = "Password admin belum diatur (isi PASSWORD_ADMIN di file atau variabel ADMIN_PASSWORD)."
    elif request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = True
            return redirect(url_for("admin_home"))
        time.sleep(1)
        galat = "Password salah."
    return halaman(LOGIN, judul="Admin Kos", sub="Masuk untuk memeriksa pembayaran", galat=galat)


@app.post("/admin/keluar")
def keluar():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin")
@wajib_admin
def admin_home():
    semua = muat_t()
    for r in semua:
        r["penyewa"] = DAFTAR_KAMAR.get(r["kamar"], {}).get("penyewa", "")
    menunggu = [r for r in semua if r["status"] == "menunggu"]
    riwayat = [r for r in reversed(semua) if r["status"] != "menunggu"][:30]
    masa, baris = muat_masa(), []
    for k, info in DAFTAR_KAMAR.items():
        s = masa.get(k, "")
        label, kelas = status_masa(s)
        baris.append({"kamar": k, **info, "sampai": s, "label": label, "kelas": kelas})
    return halaman(ADMIN, judul="Admin Kos", sub="Periksa bukti transfer & atur masa kos",
                   menunggu=menunggu, riwayat=riwayat, baris=baris)


@app.post("/admin/proses")
@wajib_admin
def admin_proses():
    kode, aksi = request.form.get("kode", ""), request.form.get("aksi", "")
    catatan = request.form.get("catatan", "").strip()[:120]
    with LOCK:
        semua = muat_t()
        rec = next((r for r in semua if r["kode"] == kode), None)
        if rec and rec["status"] == "menunggu":
            if aksi == "setuju":
                masa = muat_masa()
                try:
                    awal = max(date.today(), date.fromisoformat(masa.get(rec["kamar"], "")))
                except ValueError:
                    awal = date.today()
                baru = tambah_bulan(awal, rec["bulan"])
                masa[rec["kamar"]] = baru.isoformat()
                simpan_masa(masa)
                rec.update(status="disetujui", berlaku_sampai=baru.isoformat(), catatan=catatan)
                catat_keuangan(rec)
                simpan_t(semua)
            elif aksi == "tolak":
                rec.update(status="ditolak", catatan=catatan)
                simpan_t(semua)
    return redirect(url_for("admin_home"))


@app.post("/admin/atur")
@wajib_admin
def admin_atur():
    kamar, tgl = request.form.get("kamar", ""), request.form.get("tanggal", "")
    try:
        date.fromisoformat(tgl)
    except ValueError:
        return redirect(url_for("admin_home"))
    if kamar in DAFTAR_KAMAR:
        with LOCK:
            masa = muat_masa()
            masa[kamar] = tgl
            simpan_masa(masa)
    return redirect(url_for("admin_home"))


@app.route("/admin/bukti/<nama>")
@wajib_admin
def bukti(nama):
    if not re.fullmatch(r"[0-9a-f]{8}\.(jpg|png|webp|pdf)", nama):
        abort(404)
    resp = send_from_directory(DIR_BUKTI, nama)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


BASE_HTML = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{judul}} - Kost Dago DT 20</title>
<link rel="icon" href="/static/logo.jpg">
<style>
:root{--bg:#f4f6fb;--card:#fff;--tx:#0f172a;--mut:#64748b;--line:#e8ecf3;--pri:#4f46e5;
--in:#059669;--inbg:#d1fae5;--out:#dc2626;--outbg:#fee2e2;--warn:#b45309;--warnbg:#fef3c7}
@media(prefers-color-scheme:dark){:root{--bg:#0b1020;--card:#151b2e;--tx:#e5e7eb;--mut:#94a3b8;
--line:#263049;--inbg:#064e3b;--outbg:#7f1d1d;--warn:#fbbf24;--warnbg:#78350f}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--tx)}
.hero{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:22px 16px 80px}
.w,.main{max-width:1000px;margin:auto}.hero h1{margin:0;font-size:1.4rem}.hero p{margin:2px 0 0;opacity:.8;font-size:.85rem}
.brand{display:flex;align-items:center;gap:14px}
.logo{height:88px;width:auto;background:#fff;border-radius:16px;padding:4px}
.main{margin-top:-56px;padding:0 16px 30px}
.card{background:var(--card);border-radius:14px;padding:18px;margin-bottom:16px;box-shadow:0 4px 14px #0f172a12}
h2{font-size:1rem;margin:0 0 14px;display:flex;justify-content:space-between;align-items:center;gap:8px}
label{font-size:.75rem;color:var(--mut);display:block;margin:12px 0 4px;font-weight:600}
input,select{font:inherit;padding:10px;border:1px solid var(--line);border-radius:10px;width:100%;background:var(--bg);color:var(--tx)}
button{font:inherit;font-weight:600;background:var(--pri);color:#fff;border:0;border-radius:10px;padding:11px 18px;cursor:pointer}
button.k{padding:6px 12px;font-size:.8rem}button.o{background:var(--line);color:var(--tx)}
button.d{background:var(--outbg);color:var(--out)}
.big{width:100%;margin-top:14px;font-size:1rem}
.total{margin-top:14px;padding:12px;background:var(--bg);border-radius:10px;display:flex;justify-content:space-between}
.rek{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px;background:var(--bg);
border-radius:10px;margin-bottom:8px}.rek b{font-size:1.15rem;letter-spacing:.03em}
.alert{background:var(--outbg);color:var(--out);padding:10px 12px;border-radius:10px;font-size:.9rem;margin-bottom:6px}
.mut{color:var(--mut);font-size:.82rem}.in{color:var(--in)}.out{color:var(--out)}.warn{color:var(--warn)}
.t{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:.9rem}
th{font-size:.72rem;text-transform:uppercase;color:var(--mut);text-align:left;padding:8px;border-bottom:2px solid var(--line)}
td{padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
.badge.in{background:var(--inbg)}.badge.out{background:var(--outbg)}.badge.warn{background:var(--warnbg)}.badge.mut{background:var(--line)}
form.i{display:flex;gap:6px;flex-wrap:wrap}form.i input{padding:6px;width:auto}
.item{display:grid;grid-template-columns:1fr auto;gap:12px;padding:14px 0;border-bottom:1px solid var(--line)}
.item form{grid-column:1/-1}.thumb{height:90px;border-radius:8px;border:1px solid var(--line)}
a{color:var(--pri)}
</style></head><body>
<div class="hero"><div class="w brand">
<img class="logo" src="/static/logo.jpg" alt="Kost Dago DT 20" onerror="this.style.display='none'">
<div><h1>{{judul}}</h1><p>{{sub}}</p></div></div></div>
<div class="main">{{isi|safe}}</div></body></html>"""

INDEX = """<div style="max-width:560px;margin:auto">
<div class="card"><h2>1. Transfer ke rekening</h2>
{% for r in rekening %}<div class="rek"><div><span class="mut">{{r.bank}} - a.n. {{r.atas_nama}}</span><br>
<b id="rek{{loop.index}}">{{r.nomor}}</b></div>
<button type="button" class="k o" onclick="salin('rek{{loop.index}}',this)">Salin</button></div>{% endfor %}
<p class="mut" style="margin-bottom:0">Transfer sesuai <b>total bayar</b> di bawah, lalu simpan bukti transfernya.</p></div>

<div class="card"><h2>2. Isi data &amp; unggah bukti</h2>
{% if galat %}<div class="alert">{{galat}}</div>{% endif %}
<form method="post" action="/kirim" enctype="multipart/form-data">
<label>Kamar</label><select name="kamar" id="kamar" onchange="hit()" required>
<option value="">- pilih kamar -</option>
{% for o in opsi %}<option value="{{o.kamar}}" {{'selected' if o.kamar==pilih}}>{{o.teks}}</option>{% endfor %}</select>
<label>Perpanjang selama</label><select name="bulan" id="bulan" onchange="hit()">
{% for b in pilihan %}<option value="{{b}}">{{b}} bulan</option>{% endfor %}</select>
<label>Nomor HP / WhatsApp</label><input name="hp" inputmode="tel" maxlength="20" required value="{{hp}}" placeholder="08xxxxxxxxxx">
<div class="total"><span>Total bayar</span><b id="total">Rp 0</b></div>
<label>Bukti transfer (foto/screenshot atau PDF, maks. 5 MB)</label>
<input type="file" name="bukti" accept="image/*,application/pdf" required>
<button class="big">Kirim Bukti Pembayaran</button></form>
<p class="mut">Masa kos bertambah setelah admin memeriksa dan menyetujui pembayaran Anda.</p></div></div>
<script>
const T={{tarif|tojson}};
function hit(){const k=document.getElementById('kamar').value,b=+document.getElementById('bulan').value;
document.getElementById('total').textContent='Rp '+((T[k]||0)*b).toLocaleString('id-ID');}
function salin(id,btn){const t=document.getElementById(id).textContent;
(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>{btn.textContent='Tersalin!'},
()=>{btn.textContent='Salin manual'});setTimeout(()=>btn.textContent='Salin',1800);}
hit();
</script>"""

STATUS = """<div class="card" style="max-width:560px;margin:auto"><h2>Status Pembayaran</h2>
{% if rec.status=='disetujui' %}
<p><span class="badge in">Disetujui</span></p>
<p>Terima kasih! Kamar <b>{{rec.kamar}}</b> diperpanjang {{rec.bulan}} bulan.<br>
Masa kos berlaku sampai <b>{{rec.berlaku_sampai|tgl}}</b>.</p>
{% elif rec.status=='ditolak' %}
<p><span class="badge out">Ditolak</span></p>
<p>Pembayaran belum dapat diterima.{% if rec.catatan %}<br>Alasan: <b>{{rec.catatan}}</b>{% endif %}</p>
<a href="/"><button>Kirim ulang bukti</button></a>
{% else %}
<p><span class="badge warn">Menunggu konfirmasi admin</span></p>
<p>Bukti Anda sudah diterima. Admin akan memeriksa dan menyetujuinya. Muat ulang halaman ini untuk melihat status terbaru.</p>
<a href=""><button class="o">Muat ulang</button></a>
{% endif %}
<p class="mut">Kamar {{rec.kamar}} &middot; {{rec.bulan}} bulan &middot; {{rec.jumlah|rp}}<br>
Simpan kode ini untuk memeriksa status: <b>{{rec.kode}}</b><br>
Alamat halaman ini bisa Anda simpan/bookmark.</p></div>"""

LOGIN = """<div class="card" style="max-width:380px;margin:auto"><h2>Masuk Admin</h2>
{% if galat %}<div class="alert">{{galat}}</div>{% endif %}
<form method="post"><label>Password</label><input type="password" name="password" autofocus>
<button class="big">Masuk</button></form></div>"""

ADMIN = """<div class="card"><h2><span>Menunggu Konfirmasi
<span class="badge {{'warn' if menunggu else 'mut'}}">{{menunggu|length}}</span></span>
<form method="post" action="/admin/keluar"><button class="k o">Keluar</button></form></h2>
{% for r in menunggu %}<div class="item">
<div><b>Kamar {{r.kamar}}</b> &middot; {{r.penyewa}}<br>
<span class="mut">{{r.waktu}} &middot; {{r.bulan}} bulan &middot; <b>{{r.jumlah|rp}}</b><br>
HP/WA: <a href="https://wa.me/{{r.hp|wa}}" target="_blank" rel="noopener">{{r.hp}}</a></span></div>
<div>{% if r.bukti.endswith('.pdf') %}<a href="/admin/bukti/{{r.bukti}}" target="_blank">📄 Buka bukti (PDF)</a>
{% else %}<a href="/admin/bukti/{{r.bukti}}" target="_blank"><img class="thumb" src="/admin/bukti/{{r.bukti}}" alt="bukti"></a>{% endif %}</div>
<form class="i" method="post" action="/admin/proses"><input type="hidden" name="kode" value="{{r.kode}}">
<input name="catatan" maxlength="120" placeholder="Catatan (isi alasan jika menolak)" style="flex:1;min-width:180px">
<button class="k" name="aksi" value="setuju" onclick="return confirm('Setujui dan perpanjang masa kos?')">✔ Setujui</button>
<button class="k d" name="aksi" value="tolak" onclick="return confirm('Tolak pembayaran ini?')">✖ Tolak</button></form>
</div>{% else %}<p class="mut">Tidak ada pembayaran yang menunggu.</p>{% endfor %}</div>

<div class="card"><h2>Masa Kos Per Kamar</h2><div class="t"><table>
<tr><th>Kamar</th><th>Penyewa</th><th>Tarif</th><th>Berlaku sampai</th><th>Status</th><th>Atur tanggal</th></tr>
{% for b in baris %}<tr><td><b>{{b.kamar}}</b></td><td>{{b.penyewa}}</td><td>{{b.tarif|rp}}</td>
<td>{{b.sampai|tgl}}</td><td><span class="badge {{b.kelas}}">{{b.label}}</span></td>
<td><form class="i" method="post" action="/admin/atur"><input type="hidden" name="kamar" value="{{b.kamar}}">
<input type="date" name="tanggal" value="{{b.sampai}}" required><button class="k">Simpan</button></form></td></tr>{% endfor %}
</table></div><p class="mut">Isi tanggal ini sekali untuk tiap kamar. Setelah itu bertambah otomatis saat Anda menyetujui pembayaran.</p></div>

<div class="card"><h2>Riwayat (30 terakhir)</h2><div class="t"><table>
<tr><th>Waktu</th><th>Kamar</th><th>Lama</th><th>Jumlah</th><th>Status</th><th>Berlaku sampai</th><th>Catatan</th></tr>
{% for r in riwayat %}<tr><td>{{r.waktu}}</td><td>{{r.kamar}}</td><td>{{r.bulan}} bln</td><td>{{r.jumlah|rp}}</td>
<td><span class="badge {{'in' if r.status=='disetujui' else 'out'}}">{{r.status}}</span></td>
<td>{{r.berlaku_sampai|tgl}}</td><td>{{r.catatan}}</td></tr>
{% else %}<tr><td colspan="7" class="mut">Belum ada riwayat.</td></tr>{% endfor %}
</table></div></div>"""


def ip_lokal():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    print("\nServer perpanjangan kos (transfer rekening) berjalan.")
    print(f"  Halaman penyewa    : http://localhost:{PORT}")
    print(f"  Dari HP (WiFi sama): http://{ip_lokal()}:{PORT}")
    print(f"  Halaman admin      : http://localhost:{PORT}/admin")
    if any(r["nomor"].startswith("0000") for r in REKENING):
        print("  ! Nomor rekening masih contoh -> ubah REKENING di bagian PENGATURAN.")
    if not ADMIN_PASSWORD:
        print("  ! Password admin belum diisi -> halaman admin tidak bisa dipakai.")
    print("  Tekan Ctrl+C untuk berhenti.\n")
    app.run(host="0.0.0.0", port=PORT)
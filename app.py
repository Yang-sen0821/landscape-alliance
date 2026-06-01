# -*- coding: utf-8 -*-
import os, io, json, uuid, time, hashlib
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
import google.auth.transport.requests as _g_req
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'landscape-2026-secret')
csrf = CSRFProtect(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

ADMIN_EMAIL     = os.environ.get('ADMIN_EMAIL', 'g2349311@gmail.com')
SHEET_ID        = os.environ.get('SHEET_ID', '1E76TuBWUEUw93KjOz_xgNGcTqUuDhc0AdfFpShVIWbs')
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID', '1qCzsnVGQl6RAQprtWuh4aCMt98J59BkD')
CONTACT_PHONE   = os.environ.get('CONTACT_PHONE', '0910-006-229')

MASTER_TAGS = {
    '設計風格': ['現代簡約', '日式禪風', '南洋熱帶', '地中海風', '自然鄉村', '工業風'],
    '適用空間': ['前院', '後院', '中庭', '屋頂花園', '陽台', '商業空間'],
    '植栽':     ['草坪', '喬木', '灌木', '竹子', '花卉', '水生植物', '多肉植物'],
    '鋪面材料': ['天然石材', '木材/塑木', '磚砌', '碎石子', '混凝土'],
    '家具設施': ['涼亭棚架', '水景噴泉', '戶外家具', '戶外照明', '圍籬圍牆', '景觀步道'],
}

SUPPLIER_HEADERS = ['供應商ID', '公司名稱', 'Email', '密碼hash', '聯絡人', '電話', '狀態', '註冊時間']
MATERIAL_HEADERS = ['素材ID', '素材名稱', '品牌', '規格尺寸', '單位', '零售價', '照片IDs', '標籤', '供應商Email', '狀態', '上傳時間', '備註']

SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
DRIVE_SCOPES  = ['https://www.googleapis.com/auth/drive.file']

_gc = _gc_ts = _drive_svc = None

def _sa_creds():
    raw = os.environ.get('GOOGLE_CREDENTIALS')
    info = json.loads(raw) if raw else json.load(open(r'E:\keys\google_service_account.json', encoding='utf-8'))
    return SACredentials.from_service_account_info(info, scopes=SHEET_SCOPES)

def _drive_oauth_creds():
    """OAuth2 credentials for Drive — uses g2349311@musengarden.com, avoids SA quota issue."""
    raw = os.environ.get('DRIVE_OAUTH_TOKEN')
    if raw:
        info = json.loads(raw)
    else:
        local_file = r'E:\keys\landscape_drive_token.json'
        with open(local_file, encoding='utf-8') as f:
            info = json.load(f)
    creds = OAuthCredentials(
        token=info.get('token'),
        refresh_token=info['refresh_token'],
        token_uri=info.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=info['client_id'],
        client_secret=info['client_secret'],
        scopes=DRIVE_SCOPES,
    )
    if not creds.valid:
        creds.refresh(_g_req.Request())
    return creds

def _client():
    global _gc, _gc_ts
    if _gc and time.time() - (_gc_ts or 0) < 3600:
        return _gc
    _gc = gspread.authorize(_sa_creds())
    _gc_ts = time.time()
    return _gc

def _drive():
    global _drive_svc
    _drive_svc = build('drive', 'v3', credentials=_drive_oauth_creds())
    return _drive_svc

def _ws(tab):
    return _client().open_by_key(SHEET_ID).worksheet(tab)

# ── Auth helpers ──────────────────────────────────────────────
def _hash(pw):
    """Generate strong password hash (pbkdf2:sha256)."""
    return generate_password_hash(pw, method='pbkdf2:sha256')

def _is_legacy_sha256(stored_hash):
    """Detect old bare SHA-256 hex strings (64 hex chars, no method prefix)."""
    return len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash)

def _verify_password(pw, stored_hash):
    """Verify password against stored hash; returns (ok, needs_upgrade)."""
    if _is_legacy_sha256(stored_hash):
        legacy_ok = hashlib.sha256(pw.encode('utf-8')).hexdigest() == stored_hash
        return legacy_ok, legacy_ok  # (ok, needs_upgrade)
    return check_password_hash(stored_hash, pw), False

def current_user():
    return session.get('email')

def is_admin():
    return current_user() == ADMIN_EMAIL

def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for('login', next=request.path))
        return f(*a, **kw)
    return wrapped

def admin_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not is_admin():
            abort(403)
        return f(*a, **kw)
    return wrapped

# ── Supplier helpers ───────────────────────────────────────────
def _ensure_ws(tab, headers):
    try:
        return _client().open_by_key(SHEET_ID).worksheet(tab)
    except Exception:
        sh = _client().open_by_key(SHEET_ID)
        ws = sh.add_worksheet(title=tab, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

def _ws_supplier():
    return _ensure_ws('供應商', ['供應商ID','公司名稱','Email','密碼hash','聯絡人','電話','狀態','註冊時間'])

def _ws_material():
    return _ensure_ws('素材', ['素材ID','素材名稱','品牌','規格尺寸','單位','零售價','照片IDs','標籤','供應商Email','狀態','上傳時間','備註'])

def _ws_partner():
    return _ensure_ws('夥伴資料', ['會員Email','所在縣市','服務縣市','最小接案金額','公司簡介','LINE ID','官方網站','更新時間'])

def find_supplier(email):
    for row in _ws_supplier().get_all_records():
        if row.get('Email','').lower() == email.lower():
            return row
    return None

def get_materials(supplier_email=None, status=None):
    rows = _ws_material().get_all_records()
    if supplier_email:
        rows = [r for r in rows if r.get('供應商Email','').lower() == supplier_email.lower()]
    if status:
        rows = [r for r in rows if r.get('狀態') == status]
    return rows

def get_partner_profile(email):
    for row in _ws_partner().get_all_records():
        if row.get('會員Email','').lower() == email.lower():
            return row
    return {}

def current_supplier():
    return session.get('supplier_email')

def supplier_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_supplier():
            return redirect(url_for('supplier_login', next=request.path))
        return f(*a, **kw)
    return wrapped

TW_COUNTIES = [
    '台北市','新北市','基隆市','桃園市','新竹市','新竹縣','宜蘭縣',
    '苗栗縣','台中市','彰化縣','南投縣','雲林縣',
    '嘉義市','嘉義縣','台南市','高雄市','屏東縣',
    '花蓮縣','台東縣',
    '澎湖縣','金門縣','連江縣（馬祖）',
]

# ── Sheet helpers ─────────────────────────────────────────────
def get_works(status=None):
    rows = _ws('作品').get_all_records()
    if status:
        rows = [r for r in rows if r.get('狀態') == status]
    return rows

def get_all_tags():
    tags = set()
    for w in get_works('published'):
        for t in w.get('標籤', '').split(','):
            t = t.strip()
            if t:
                tags.add(t)
    return sorted(tags)

def find_member(email):
    for m in _ws('會員').get_all_records():
        if m.get('Email', '').lower() == email.lower():
            return m
    return None

def photo_urls(ids_str, size='w600'):
    return [f'https://drive.google.com/thumbnail?id={fid.strip()}&sz={size}'
            for fid in ids_str.split(',') if fid.strip()]

def fmt_price(val):
    try:
        return f'NT$ {int(str(val).replace(",","").replace("$","").strip()):,}'
    except Exception:
        return str(val)

def get_ratings(work_id=None):
    rows = _ws('評分').get_all_records()
    if work_id:
        rows = [r for r in rows if r.get('作品ID') == work_id]
    return rows

def avg_rating(work_id):
    ratings = get_ratings(work_id)
    scores = [int(r['分數']) for r in ratings if str(r.get('分數','')).isdigit()]
    if not scores:
        return None, 0
    return round(sum(scores) / len(scores), 1), len(scores)

def ratings_map(works):
    all_r = _ws('評分').get_all_records()
    result = {}
    for w in works:
        wid = w.get('作品ID')
        scores = [int(r['分數']) for r in all_r if r.get('作品ID') == wid and str(r.get('分數','')).isdigit()]
        result[wid] = (round(sum(scores)/len(scores),1), len(scores)) if scores else (None, 0)
    return result

app.jinja_env.globals.update(photo_urls=photo_urls, fmt_price=fmt_price,
                             is_admin=is_admin, current_user=current_user,
                             avg_rating=avg_rating,
                             current_supplier=current_supplier,
                             master_tags=MASTER_TAGS,
                             tw_counties=TW_COUNTIES)

# ── Drive upload ──────────────────────────────────────────────
_ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

def upload_photos(files, work_id):
    svc = _drive()
    folder_meta = {
        'name': work_id,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [DRIVE_FOLDER_ID]
    }
    wf = svc.files().create(body=folder_meta, fields='id').execute()
    wfid = wf['id']
    ids = []
    try:
        for i, f in enumerate(files):
            if not f or not f.filename:
                continue
            # 副檔名白名單驗證
            ext = os.path.splitext(f.filename)[1].lstrip('.').lower()
            if ext not in _ALLOWED_EXTENSIONS:
                raise ValueError(f'不支援的檔案格式「.{ext}」，僅接受：{", ".join(sorted(_ALLOWED_EXTENSIONS))}')
            # 單檔大小上限 10 MB
            data = f.stream.read()
            if len(data) > _MAX_FILE_BYTES:
                raise ValueError(f'檔案「{f.filename}」超過 10 MB 上限（{len(data) // 1024 // 1024} MB）')
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=f.content_type or 'image/jpeg', resumable=False)
            cf = svc.files().create(
                body={'name': f'{work_id}_{i}.{ext}', 'parents': [wfid]},
                media_body=media, fields='id'
            ).execute()
            fid = cf['id']
            svc.permissions().create(fileId=fid, body={'type': 'anyone', 'role': 'reader'}).execute()
            ids.append(fid)
    except Exception:
        # 清除孤兒檔案與資料夾，避免殘留在 Drive
        for fid in ids:
            try:
                svc.files().delete(fileId=fid).execute()
            except Exception:
                pass
        try:
            svc.files().delete(fileId=wfid).execute()
        except Exception:
            pass
        raise
    return ids

# ── Public routes ─────────────────────────────────────────────
@app.route('/')
def index():
    works = get_works('published')
    tags  = get_all_tags()
    rmap  = ratings_map(works[:9])
    return render_template('public/index.html', works=works[:9], tags=tags,
                           total=len(works), rmap=rmap)

@app.route('/works')
def works():
    all_w = get_works('published')
    tag   = request.args.get('tag', '').strip()
    mn    = request.args.get('min', '').strip()
    mx    = request.args.get('max', '').strip()
    filtered = all_w
    if tag:
        filtered = [w for w in filtered if tag in w.get('標籤', '')]
    if mn.isdigit():
        filtered = [w for w in filtered
                    if str(w.get('完工金額','')).replace(',','').isdigit()
                    and int(str(w.get('完工金額','')).replace(',','')) >= int(mn)]
    if mx.isdigit():
        filtered = [w for w in filtered
                    if str(w.get('完工金額','')).replace(',','').isdigit()
                    and int(str(w.get('完工金額','')).replace(',','')) <= int(mx)]
    rmap = ratings_map(filtered)
    return render_template('public/works.html', works=filtered,
                           tags=get_all_tags(), active_tag=tag,
                           min_val=mn, max_val=mx, rmap=rmap)

@app.route('/work/<work_id>')
def work_detail(work_id):
    work = next((w for w in get_works('published') if w.get('作品ID') == work_id), None)
    if not work:
        abort(404)
    photos  = photo_urls(work.get('照片IDs', ''), size='w1000')
    tags    = [t.strip() for t in work.get('標籤', '').split(',') if t.strip()]
    ratings = get_ratings(work_id)
    avg, cnt = avg_rating(work_id)
    return render_template('public/work.html', work=work, photos=photos, tags=tags,
                           contact_phone=CONTACT_PHONE,
                           ratings=ratings, avg=avg, cnt=cnt)

@app.route('/work/<work_id>/rate', methods=['POST'])
def rate_work(work_id):
    work = next((w for w in get_works('published') if w.get('作品ID') == work_id), None)
    if not work:
        abort(404)
    name  = request.form.get('name','').strip()
    phone = request.form.get('phone','').strip()
    score = request.form.get('score','').strip()
    note  = request.form.get('note','').strip()
    if not name or not score or not score.isdigit() or not (1 <= int(score) <= 5):
        flash('請填寫姓名並選擇評分', 'error')
        return redirect(url_for('work_detail', work_id=work_id) + '#rate')
    _ws('評分').append_row([
        str(uuid.uuid4())[:8], work_id, name, phone, int(score), note,
        datetime.now().strftime('%Y-%m-%d %H:%M')
    ])
    flash('感謝您的評價！', 'success')
    return redirect(url_for('work_detail', work_id=work_id) + '#ratings')

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M'),
            request.form.get('name','').strip(),
            request.form.get('phone','').strip(),
            request.form.get('message','').strip(),
            request.form.get('work_id','').strip(),
        ]
        _ws('聯絡').append_row(row)
        flash('已收到您的訊息，我們會盡快與您聯絡！', 'success')
        return redirect(url_for('contact'))
    work_id = request.args.get('work_id', '')
    return render_template('public/contact.html', work_id=work_id,
                           contact_phone=CONTACT_PHONE)

# ── Auth ──────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email   = request.form.get('email','').strip().lower()
        pw      = request.form.get('password','').strip()
        name    = request.form.get('name','').strip()
        company = request.form.get('company','').strip()
        phone   = request.form.get('phone','').strip()
        if not all([email, pw, name, company, phone]):
            flash('請填寫所有欄位', 'error')
            return render_template('auth/register.html')
        if len(pw) < 8:
            flash('密碼至少 8 個字元', 'error')
            return render_template('auth/register.html')
        if find_member(email):
            flash('此 Email 已註冊', 'error')
            return render_template('auth/register.html')
        _ws('會員').append_row([
            str(uuid.uuid4())[:8], email, _hash(pw),
            name, company, phone, 'active',
            datetime.now().strftime('%Y-%m-%d %H:%M')
        ])
        session['email'] = email
        session['name']  = name
        flash(f'歡迎，{name}！帳號建立完成', 'success')
        return redirect(url_for('upload'))
    return render_template('auth/register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        pw    = request.form.get('password','').strip()
        m     = find_member(email)
        if m and m.get('狀態') == 'active':
            ok, needs_upgrade = _verify_password(pw, m.get('密碼hash', ''))
            if ok:
                if needs_upgrade:
                    # 自動升級舊 SHA-256 hash 為 pbkdf2:sha256
                    ws = _ws('會員')
                    rows = ws.get_all_records()
                    idx = next((i for i, r in enumerate(rows)
                                if r.get('Email', '').lower() == email), None)
                    if idx is not None:
                        ws.update_cell(idx + 2, 3, _hash(pw))
                session['email'] = email
                session['name']  = m.get('姓名','')
                nxt = request.form.get('next') or (url_for('admin_dashboard') if email == ADMIN_EMAIL else url_for('upload'))
                return redirect(nxt)
        flash('帳號或密碼錯誤', 'error')
    return render_template('auth/login.html', next=request.args.get('next',''))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ── Member ────────────────────────────────────────────────────
@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        name   = request.form.get('name','').strip()
        scale  = request.form.get('scale','').strip()
        tags   = request.form.get('tags','').strip()
        price  = request.form.get('price','').strip()
        photos = request.files.getlist('photos')
        valid  = [f for f in photos if f and f.filename]
        if not name:
            flash('請填寫作品名稱', 'error')
            return render_template('member/upload.html')
        if not valid:
            flash('請至少上傳一張照片', 'error')
            return render_template('member/upload.html')
        work_id = str(uuid.uuid4()).replace('-','')[:12]
        try:
            fids = upload_photos(valid, work_id)
        except Exception as e:
            flash(f'圖片上傳失敗：{e}', 'error')
            return render_template('member/upload.html')
        _ws('作品').append_row([
            work_id, name, tags, price, ','.join(fids),
            current_user(), 'pending',
            datetime.now().strftime('%Y-%m-%d %H:%M'), '', scale
        ])
        flash('上傳成功！審核通過後即公開展示', 'success')
        return redirect(url_for('my_works'))
    return render_template('member/upload.html')

@app.route('/my-profile', methods=['GET', 'POST'])
@login_required
def my_profile():
    if request.method == 'POST':
        county   = request.form.get('county','').strip()
        service  = ','.join(request.form.getlist('service_areas'))
        min_amt  = request.form.get('min_amount','').strip()
        intro    = request.form.get('intro','').strip()
        line_id  = request.form.get('line_id','').strip()
        website  = request.form.get('website','').strip()
        ws = _ws_partner()
        rows = ws.get_all_records()
        idx = next((i for i,r in enumerate(rows) if r.get('會員Email','').lower() == current_user().lower()), None)
        row_data = [current_user(), county, service, min_amt, intro, line_id, website,
                    datetime.now().strftime('%Y-%m-%d %H:%M')]
        if idx is None:
            ws.append_row(row_data)
        else:
            ws.update(f'A{idx+2}:H{idx+2}', [row_data])
        flash('資料已更新', 'success')
        return redirect(url_for('my_profile'))
    profile = get_partner_profile(current_user())
    member  = find_member(current_user()) or {}
    return render_template('member/profile.html', profile=profile, member=member)

@app.route('/my-works')
@login_required
def my_works():
    rows = _ws('作品').get_all_records()
    mine = [r for r in rows if r.get('上傳者Email') == current_user()]
    return render_template('member/my_works.html', works=mine)

# ── Admin ─────────────────────────────────────────────────────
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    rows     = _ws('作品').get_all_records()
    pending  = [r for r in rows if r.get('狀態') == 'pending']
    published= [r for r in rows if r.get('狀態') == 'published']
    rejected = [r for r in rows if r.get('狀態') == 'rejected']
    contacts = _ws('聯絡').get_all_records()[-20:]
    members  = _ws('會員').get_all_records()
    mats_pending = get_materials(status='pending')
    mats_active  = get_materials(status='active')
    return render_template('admin/dashboard.html',
                           pending=pending, published=published,
                           rejected=rejected, contacts=contacts,
                           members=members,
                           mats_pending=mats_pending,
                           mats_active=mats_active)

@app.route('/admin/work/<work_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_work(work_id):
    ws   = _ws('作品')
    rows = ws.get_all_records()
    idx  = next((i for i,r in enumerate(rows) if r.get('作品ID') == work_id), None)
    if idx is None:
        abort(404)
    work    = rows[idx]
    row_num = idx + 2

    if request.method == 'POST':
        action = request.form.get('action')
        if action in ('approve', 'save'):
            ws.update(f'B{row_num}:D{row_num}', [[
                request.form.get('name', work['作品名稱']),
                request.form.get('tags', work['標籤']),
                request.form.get('price', work['完工金額']),
            ]])
            ws.update_cell(row_num, 9, request.form.get('note',''))
            if action == 'approve':
                ws.update_cell(row_num, 7, 'published')
                flash('已審核通過並上架', 'success')
            else:
                flash('已儲存', 'success')
        elif action == 'reject':
            ws.update_cell(row_num, 7, 'rejected')
            flash('已退回', 'success')
        elif action == 'unpublish':
            ws.update_cell(row_num, 7, 'pending')
            flash('已下架', 'success')
        elif action == 'delete':
            ws.delete_rows(row_num)
            flash('已刪除', 'success')
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('admin_dashboard'))

    photos = photo_urls(work.get('照片IDs',''), size='w800')
    tags   = work.get('標籤','')
    return render_template('admin/work_edit.html', work=work,
                           photos=photos, tags=tags)

@app.route('/platform')
def platform():
    return render_template('public/platform.html')

@app.route('/platform/investor')
def platform_investor():
    return render_template('public/platform_investor.html')

@app.route('/contact/investor', methods=['GET', 'POST'])
def contact_investor():
    if request.method == 'POST':
        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M'),
            request.form.get('name','').strip(),
            request.form.get('company','').strip(),
            request.form.get('phone','').strip(),
            request.form.get('email','').strip(),
            request.form.get('amount','').strip(),
            request.form.get('message','').strip(),
        ]
        _ws('投資人詢問').append_row(row)
        flash('已收到您的訊息，楊森會在 48 小時內親自回覆。', 'success')
        return redirect(url_for('contact_investor'))
    return render_template('public/contact_investor.html')

# ── Supplier Portal ────────────────────────────────────────────
@app.route('/supplier/register', methods=['GET','POST'])
def supplier_register():
    if request.method == 'POST':
        email   = request.form.get('email','').strip().lower()
        pw      = request.form.get('password','').strip()
        company = request.form.get('company','').strip()
        contact = request.form.get('contact','').strip()
        phone   = request.form.get('phone','').strip()
        if not all([email, pw, company, contact, phone]):
            flash('請填寫所有欄位', 'error')
            return render_template('supplier/register.html')
        if len(pw) < 8:
            flash('密碼至少 8 個字元', 'error')
            return render_template('supplier/register.html')
        if find_supplier(email):
            flash('此 Email 已註冊', 'error')
            return render_template('supplier/register.html')
        _ws_supplier().append_row([
            str(uuid.uuid4())[:8], company, email, _hash(pw),
            contact, phone, 'active',
            datetime.now().strftime('%Y-%m-%d %H:%M')
        ])
        session['supplier_email']   = email
        session['supplier_company'] = company
        flash(f'歡迎，{company}！', 'success')
        return redirect(url_for('supplier_upload'))
    return render_template('supplier/register.html')

@app.route('/supplier/login', methods=['GET','POST'])
def supplier_login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        pw    = request.form.get('password','').strip()
        s     = find_supplier(email)
        if s and s.get('狀態') == 'active':
            ok, needs_upgrade = _verify_password(pw, s.get('密碼hash', ''))
            if ok:
                if needs_upgrade:
                    # 自動升級舊 SHA-256 hash 為 pbkdf2:sha256
                    ws = _ws_supplier()
                    rows = ws.get_all_records()
                    idx = next((i for i, r in enumerate(rows)
                                if r.get('Email', '').lower() == email), None)
                    if idx is not None:
                        ws.update_cell(idx + 2, 4, _hash(pw))
                session['supplier_email']   = email
                session['supplier_company'] = s.get('公司名稱','')
                return redirect(request.form.get('next') or url_for('supplier_upload'))
        flash('帳號或密碼錯誤', 'error')
    return render_template('supplier/login.html', next=request.args.get('next',''))

@app.route('/supplier/logout')
def supplier_logout():
    session.pop('supplier_email', None)
    session.pop('supplier_company', None)
    return redirect(url_for('index'))

@app.route('/supplier/upload', methods=['GET','POST'])
@supplier_required
def supplier_upload():
    if request.method == 'POST':
        name   = request.form.get('name','').strip()
        brand  = request.form.get('brand','').strip()
        spec   = request.form.get('spec','').strip()
        unit   = request.form.get('unit','').strip()
        price  = request.form.get('price','').strip()
        tags   = request.form.get('tags','').strip()
        photos = request.files.getlist('photos')
        valid  = [f for f in photos if f and f.filename]
        if not all([name, spec, unit, price]):
            flash('請填寫必要欄位（素材名稱、規格尺寸、單位、零售價）', 'error')
            return render_template('supplier/upload.html')
        mat_id = str(uuid.uuid4()).replace('-','')[:12]
        fids   = []
        if valid:
            try:
                fids = upload_photos(valid, f'mat_{mat_id}')
            except Exception as e:
                flash(f'圖片上傳失敗：{e}', 'error')
                return render_template('supplier/upload.html')
        _ws_material().append_row([
            mat_id, name, brand, spec, unit, price,
            ','.join(fids), tags, current_supplier(),
            'pending', datetime.now().strftime('%Y-%m-%d %H:%M'), ''
        ])
        flash('上傳成功！審核通過後即可在平台顯示', 'success')
        return redirect(url_for('supplier_materials'))
    return render_template('supplier/upload.html')

@app.route('/supplier/materials')
@supplier_required
def supplier_materials():
    mats = get_materials(supplier_email=current_supplier())
    return render_template('supplier/my_materials.html', materials=mats)

# ── Admin material review ──────────────────────────────────────
@app.route('/admin/material/<mat_id>', methods=['POST'])
@login_required
@admin_required
def admin_material(mat_id):
    ws   = _ws_material()
    rows = ws.get_all_records()
    idx  = next((i for i,r in enumerate(rows) if r.get('素材ID') == mat_id), None)
    if idx is None:
        abort(404)
    row_num = idx + 2
    action  = request.form.get('action')
    note    = request.form.get('note','').strip()
    extra   = request.form.get('extra_tags','').strip()
    if action == 'approve':
        ws.update_cell(row_num, 10, 'active')
        if note: ws.update_cell(row_num, 12, note)
        flash('素材已審核上架', 'success')
    elif action == 'reject':
        ws.update_cell(row_num, 10, 'rejected')
        if note: ws.update_cell(row_num, 12, note)
        flash('素材已退回', 'success')
    elif action == 'add_tags':
        existing = rows[idx].get('標籤','')
        combined = ','.join(filter(None, [existing, extra]))
        ws.update_cell(row_num, 8, combined)
        flash('標籤已補充', 'success')
    return redirect(url_for('admin_dashboard') + '#materials')

@app.errorhandler(403)
def e403(e):
    return render_template('error.html', code=403, msg='沒有權限'), 403

@app.errorhandler(404)
def e404(e):
    return render_template('error.html', code=404, msg='頁面不存在'), 404

if __name__ == '__main__':
    app.run(debug=True, port=5001)

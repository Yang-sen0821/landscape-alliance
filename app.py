# -*- coding: utf-8 -*-
import os, json, uuid, time, hashlib
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort)
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'landscape-2026-secret')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

ADMIN_EMAIL     = os.environ.get('ADMIN_EMAIL', 'g2349311@gmail.com')
SHEET_ID        = os.environ.get('SHEET_ID', '1E76TuBWUEUw93KjOz_xgNGcTqUuDhc0AdfFpShVIWbs')
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID', '1qCzsnVGQl6RAQprtWuh4aCMt98J59BkD')
CONTACT_PHONE   = os.environ.get('CONTACT_PHONE', '0910-006-229')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

_gc = _gc_ts = _drive_svc = None

def _creds():
    raw = os.environ.get('GOOGLE_CREDENTIALS')
    info = json.loads(raw) if raw else json.load(open(r'E:\keys\google_service_account.json', encoding='utf-8'))
    return Credentials.from_service_account_info(info, scopes=SCOPES)

def _client():
    global _gc, _gc_ts
    if _gc and time.time() - (_gc_ts or 0) < 3600:
        return _gc
    _gc = gspread.authorize(_creds())
    _gc_ts = time.time()
    return _gc

def _drive():
    global _drive_svc
    if not _drive_svc:
        _drive_svc = build('drive', 'v3', credentials=_creds())
    return _drive_svc

def _ws(tab):
    return _client().open_by_key(SHEET_ID).worksheet(tab)

# ── Auth helpers ──────────────────────────────────────────────
def _hash(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

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

app.jinja_env.globals.update(photo_urls=photo_urls, fmt_price=fmt_price, is_admin=is_admin)

# ── Drive upload ──────────────────────────────────────────────
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
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue
        media = MediaIoBaseUpload(f.stream, mimetype=f.content_type or 'image/jpeg', resumable=False)
        cf = svc.files().create(
            body={'name': f'{work_id}_{i}{os.path.splitext(f.filename)[1]}', 'parents': [wfid]},
            media_body=media, fields='id'
        ).execute()
        fid = cf['id']
        svc.permissions().create(fileId=fid, body={'type': 'anyone', 'role': 'reader'}).execute()
        ids.append(fid)
    return ids

# ── Public routes ─────────────────────────────────────────────
@app.route('/')
def index():
    works = get_works('published')
    tags  = get_all_tags()
    return render_template('public/index.html', works=works[:9], tags=tags,
                           total=len(works))

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
    return render_template('public/works.html', works=filtered,
                           tags=get_all_tags(), active_tag=tag,
                           min_val=mn, max_val=mx)

@app.route('/work/<work_id>')
def work_detail(work_id):
    work = next((w for w in get_works('published') if w.get('作品ID') == work_id), None)
    if not work:
        abort(404)
    photos = photo_urls(work.get('照片IDs', ''), size='w1000')
    tags   = [t.strip() for t in work.get('標籤', '').split(',') if t.strip()]
    return render_template('public/work.html', work=work, photos=photos, tags=tags,
                           contact_phone=CONTACT_PHONE)

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
        if m and m.get('密碼hash') == _hash(pw) and m.get('狀態') == 'active':
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
            datetime.now().strftime('%Y-%m-%d %H:%M'), ''
        ])
        flash('上傳成功！審核通過後即公開展示', 'success')
        return redirect(url_for('my_works'))
    return render_template('member/upload.html')

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
    return render_template('admin/dashboard.html',
                           pending=pending, published=published,
                           rejected=rejected, contacts=contacts,
                           members=members)

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

@app.errorhandler(403)
def e403(e):
    return render_template('error.html', code=403, msg='沒有權限'), 403

@app.errorhandler(404)
def e404(e):
    return render_template('error.html', code=404, msg='頁面不存在'), 404

if __name__ == '__main__':
    app.run(debug=True, port=5001)

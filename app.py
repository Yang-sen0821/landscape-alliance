# -*- coding: utf-8 -*-
import os, io, json, uuid, time, hashlib
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
import google.auth.transport.requests as _g_req
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'landscape-2026-secret')

# ── Database config ────────────────────────────────────────────
db_url = os.environ.get('DATABASE_URL', 'sqlite:///landscape.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

csrf = CSRFProtect(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

ADMIN_EMAIL     = os.environ.get('ADMIN_EMAIL', 'g2349311@gmail.com')
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID', '1qCzsnVGQl6RAQprtWuh4aCMt98J59BkD')
CONTACT_PHONE   = os.environ.get('CONTACT_PHONE', '0910-006-229')

MASTER_TAGS = {
    '設計風格': ['現代簡約', '日式禪風', '南洋熱帶', '地中海風', '自然鄉村', '工業風'],
    '適用空間': ['前院', '後院', '中庭', '屋頂花園', '陽台', '商業空間'],
    '植栽':     ['草坪', '喬木', '灌木', '竹子', '花卉', '水生植物', '多肉植物'],
    '鋪面材料': ['天然石材', '木材/塑木', '磚砌', '碎石子', '混凝土'],
    '家具設施': ['涼亭棚架', '水景噴泉', '戶外家具', '戶外照明', '圍籬圍牆', '景觀步道'],
}

TW_COUNTIES = [
    '台北市','新北市','基隆市','桃園市','新竹市','新竹縣','宜蘭縣',
    '苗栗縣','台中市','彰化縣','南投縣','雲林縣',
    '嘉義市','嘉義縣','台南市','高雄市','屏東縣',
    '花蓮縣','台東縣',
    '澎湖縣','金門縣','連江縣（馬祖）',
]

DRIVE_SCOPES  = ['https://www.googleapis.com/auth/drive.file']
_drive_svc = None

# ── Database Models ────────────────────────────────────────────

class Member(db.Model):
    __tablename__ = 'members'
    id = db.Column(db.String(12), primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    legacy_pw_hash = db.Column(db.String(64))  # 舊 SHA-256，遷移用
    name = db.Column(db.String(120))
    company = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    status = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    works = db.relationship('Work', backref='uploader', lazy=True, cascade='all, delete-orphan')

class Work(db.Model):
    __tablename__ = 'works'
    id = db.Column(db.String(12), primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    tags = db.Column(db.String(500))
    price = db.Column(db.String(50))
    photo_ids = db.Column(db.Text)  # CSV
    uploader_email = db.Column(db.String(120), db.ForeignKey('members.email'), nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.Text)
    scale = db.Column(db.String(50))
    avg_rating = db.Column(db.Float)
    rating_count = db.Column(db.Integer, default=0)
    ratings = db.relationship('Rating', backref='work', lazy=True, cascade='all, delete-orphan')

class Rating(db.Model):
    __tablename__ = 'ratings'
    id = db.Column(db.String(12), primary_key=True)
    work_id = db.Column(db.String(12), db.ForeignKey('works.id'), nullable=False)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    score = db.Column(db.Integer)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Contact(db.Model):
    __tablename__ = 'contacts'
    id = db.Column(db.String(12), primary_key=True)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    message = db.Column(db.Text)
    work_id = db.Column(db.String(12))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id = db.Column(db.String(12), primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    legacy_pw_hash = db.Column(db.String(64))
    company = db.Column(db.String(120))
    contact_name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    status = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    materials = db.relationship('Material', backref='supplier', lazy=True, cascade='all, delete-orphan')

class Material(db.Model):
    __tablename__ = 'materials'
    id = db.Column(db.String(12), primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    brand = db.Column(db.String(120))
    spec = db.Column(db.String(255))
    unit = db.Column(db.String(50))
    price = db.Column(db.String(50))
    photo_ids = db.Column(db.Text)  # CSV
    tags = db.Column(db.String(500))
    supplier_email = db.Column(db.String(120), db.ForeignKey('suppliers.email'), nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.Text)

class PartnerProfile(db.Model):
    __tablename__ = 'partner_profiles'
    id = db.Column(db.String(12), primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    county = db.Column(db.String(50))
    service_areas = db.Column(db.String(500))  # CSV
    min_amount = db.Column(db.String(50))
    intro = db.Column(db.Text)
    line_id = db.Column(db.String(120))
    website = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class InvestorInquiry(db.Model):
    __tablename__ = 'investor_inquiries'
    id = db.Column(db.String(12), primary_key=True)
    name = db.Column(db.String(120))
    company = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    amount = db.Column(db.String(50))
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

# ── Member helpers ─────────────────────────────────────────────
def find_member(email):
    return Member.query.filter_by(email=email.lower()).first()

def get_partner_profile(email):
    return PartnerProfile.query.filter_by(email=email.lower()).first() or {}

def current_supplier():
    return session.get('supplier_email')

def supplier_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_supplier():
            return redirect(url_for('supplier_login', next=request.path))
        return f(*a, **kw)
    return wrapped

# ── Supplier helpers ───────────────────────────────────────────
def find_supplier(email):
    return Supplier.query.filter_by(email=email.lower()).first()

def get_materials(supplier_email=None, status=None):
    query = Material.query
    if supplier_email:
        query = query.filter_by(supplier_email=supplier_email.lower())
    if status:
        query = query.filter_by(status=status)
    return query.all()

# ── Sheet helpers ──────────────────────────────────────────────
def get_works(status=None):
    query = Work.query
    if status:
        query = query.filter_by(status=status)
    return query.all()

def get_all_tags():
    tags = set()
    for w in get_works('published'):
        for t in (w.tags or '').split(','):
            t = t.strip()
            if t:
                tags.add(t)
    return sorted(tags)

def photo_urls(ids_str, size='w600'):
    return [f'https://drive.google.com/thumbnail?id={fid.strip()}&sz={size}'
            for fid in (ids_str or '').split(',') if fid.strip()]

def fmt_price(val):
    try:
        return f'NT$ {int(str(val).replace(",","").replace("$","").strip()):,}'
    except Exception:
        return str(val)

def get_ratings(work_id=None):
    query = Rating.query
    if work_id:
        query = query.filter_by(work_id=work_id)
    return query.all()

def avg_rating(work_id):
    """讀取快取的評分（重算在 _recalc_avg 進行）"""
    work = Work.query.filter_by(id=work_id).first()
    if work:
        return work.avg_rating, work.rating_count or 0
    return None, 0

def _recalc_avg(work_id):
    """重新計算並快取評分"""
    work = Work.query.filter_by(id=work_id).first()
    if not work:
        return
    scores = db.session.query(func.avg(Rating.score), func.count(Rating.id)).filter_by(work_id=work_id).first()
    if scores[0]:
        work.avg_rating = round(float(scores[0]), 1)
        work.rating_count = scores[1]
    else:
        work.avg_rating = None
        work.rating_count = 0
    db.session.commit()

def ratings_map(works):
    """為多個作品快速取得評分"""
    result = {}
    for w in works:
        result[w.id] = (w.avg_rating, w.rating_count or 0)
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

def _drive():
    global _drive_svc
    _drive_svc = build('drive', 'v3', credentials=_drive_oauth_creds())
    return _drive_svc

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
    works = get_works('published')[:9]
    tags  = get_all_tags()
    rmap  = ratings_map(works)
    return render_template('public/index.html', works=works, tags=tags,
                           total=len(get_works('published')), rmap=rmap)

@app.route('/works')
def works():
    all_w = get_works('published')
    tag   = request.args.get('tag', '').strip()
    mn    = request.args.get('min', '').strip()
    mx    = request.args.get('max', '').strip()
    filtered = all_w
    if tag:
        filtered = [w for w in filtered if tag in (w.tags or '')]
    if mn.isdigit():
        filtered = [w for w in filtered
                    if (w.price or '').replace(',','').replace('$','').isdigit()
                    and int((w.price or '').replace(',','').replace('$','')) >= int(mn)]
    if mx.isdigit():
        filtered = [w for w in filtered
                    if (w.price or '').replace(',','').replace('$','').isdigit()
                    and int((w.price or '').replace(',','').replace('$','')) <= int(mx)]
    rmap = ratings_map(filtered)
    return render_template('public/works.html', works=filtered,
                           tags=get_all_tags(), active_tag=tag,
                           min_val=mn, max_val=mx, rmap=rmap)

@app.route('/work/<work_id>')
def work_detail(work_id):
    work = Work.query.filter_by(id=work_id, status='published').first()
    if not work:
        abort(404)
    photos  = photo_urls(work.photo_ids, size='w1000')
    tags    = [t.strip() for t in (work.tags or '').split(',') if t.strip()]
    ratings = get_ratings(work_id)
    avg, cnt = avg_rating(work_id)
    return render_template('public/work.html', work=work, photos=photos, tags=tags,
                           contact_phone=CONTACT_PHONE,
                           ratings=ratings, avg=avg, cnt=cnt)

@app.route('/work/<work_id>/rate', methods=['POST'])
def rate_work(work_id):
    work = Work.query.filter_by(id=work_id, status='published').first()
    if not work:
        abort(404)
    name  = request.form.get('name','').strip()
    phone = request.form.get('phone','').strip()
    score = request.form.get('score','').strip()
    note  = request.form.get('note','').strip()
    if not name or not score or not score.isdigit() or not (1 <= int(score) <= 5):
        flash('請填寫姓名並選擇評分', 'error')
        return redirect(url_for('work_detail', work_id=work_id) + '#rate')

    rating = Rating(
        id=str(uuid.uuid4())[:8],
        work_id=work_id,
        name=name,
        phone=phone,
        score=int(score),
        note=note,
        created_at=datetime.utcnow()
    )
    db.session.add(rating)
    db.session.commit()
    _recalc_avg(work_id)

    flash('感謝您的評價！', 'success')
    return redirect(url_for('work_detail', work_id=work_id) + '#ratings')

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        contact_obj = Contact(
            id=str(uuid.uuid4())[:8],
            name=request.form.get('name','').strip(),
            phone=request.form.get('phone','').strip(),
            message=request.form.get('message','').strip(),
            work_id=request.form.get('work_id','').strip(),
            created_at=datetime.utcnow()
        )
        db.session.add(contact_obj)
        db.session.commit()
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

        member = Member(
            id=str(uuid.uuid4())[:8],
            email=email,
            password_hash=_hash(pw),
            name=name,
            company=company,
            phone=phone,
            status='active',
            created_at=datetime.utcnow()
        )
        db.session.add(member)
        db.session.commit()

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
        if m and m.status == 'active':
            ok, needs_upgrade = _verify_password(pw, m.password_hash)
            if ok:
                if needs_upgrade:
                    # 自動升級舊 SHA-256 hash 為 pbkdf2:sha256
                    m.password_hash = _hash(pw)
                    m.legacy_pw_hash = None
                    db.session.commit()
                session['email'] = email
                session['name']  = m.name or ''
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

        work = Work(
            id=work_id,
            name=name,
            tags=tags,
            price=price,
            photo_ids=','.join(fids),
            uploader_email=current_user(),
            status='pending',
            created_at=datetime.utcnow(),
            scale=scale
        )
        db.session.add(work)
        db.session.commit()

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

        profile = PartnerProfile.query.filter_by(email=current_user()).first()
        if profile:
            profile.county = county
            profile.service_areas = service
            profile.min_amount = min_amt
            profile.intro = intro
            profile.line_id = line_id
            profile.website = website
            profile.updated_at = datetime.utcnow()
        else:
            profile = PartnerProfile(
                id=str(uuid.uuid4())[:8],
                email=current_user(),
                county=county,
                service_areas=service,
                min_amount=min_amt,
                intro=intro,
                line_id=line_id,
                website=website,
                updated_at=datetime.utcnow()
            )
            db.session.add(profile)
        db.session.commit()

        flash('資料已更新', 'success')
        return redirect(url_for('my_profile'))
    profile = get_partner_profile(current_user())
    member  = find_member(current_user()) or {}
    return render_template('member/profile.html', profile=profile, member=member)

@app.route('/my-works')
@login_required
def my_works():
    works = Work.query.filter_by(uploader_email=current_user()).all()
    return render_template('member/my_works.html', works=works)

# ── Admin ─────────────────────────────────────────────────────
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    pending  = Work.query.filter_by(status='pending').all()
    published = Work.query.filter_by(status='published').all()
    rejected = Work.query.filter_by(status='rejected').all()
    contacts = Contact.query.order_by(Contact.created_at.desc()).limit(20).all()
    members  = Member.query.all()
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
    work = Work.query.filter_by(id=work_id).first()
    if not work:
        abort(404)

    if request.method == 'POST':
        action = request.form.get('action')
        if action in ('approve', 'save'):
            work.name = request.form.get('name', work.name)
            work.tags = request.form.get('tags', work.tags)
            work.price = request.form.get('price', work.price)
            work.note = request.form.get('note','')
            if action == 'approve':
                work.status = 'published'
                flash('已審核通過並上架', 'success')
            else:
                flash('已儲存', 'success')
        elif action == 'reject':
            work.status = 'rejected'
            flash('已退回', 'success')
        elif action == 'unpublish':
            work.status = 'pending'
            flash('已下架', 'success')
        elif action == 'delete':
            db.session.delete(work)
            db.session.commit()
            flash('已刪除', 'success')
            return redirect(url_for('admin_dashboard'))

        db.session.commit()
        return redirect(url_for('admin_dashboard'))

    photos = photo_urls(work.photo_ids, size='w800')
    return render_template('admin/work_edit.html', work=work, photos=photos)

@app.route('/platform')
def platform():
    return render_template('public/platform.html')

@app.route('/platform/investor')
def platform_investor():
    return render_template('public/platform_investor.html')

@app.route('/contact/investor', methods=['GET', 'POST'])
def contact_investor():
    if request.method == 'POST':
        inquiry = InvestorInquiry(
            id=str(uuid.uuid4())[:8],
            name=request.form.get('name','').strip(),
            company=request.form.get('company','').strip(),
            phone=request.form.get('phone','').strip(),
            email=request.form.get('email','').strip(),
            amount=request.form.get('amount','').strip(),
            message=request.form.get('message','').strip(),
            created_at=datetime.utcnow()
        )
        db.session.add(inquiry)
        db.session.commit()
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

        supplier = Supplier(
            id=str(uuid.uuid4())[:8],
            email=email,
            password_hash=_hash(pw),
            company=company,
            contact_name=contact,
            phone=phone,
            status='active',
            created_at=datetime.utcnow()
        )
        db.session.add(supplier)
        db.session.commit()

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
        if s and s.status == 'active':
            ok, needs_upgrade = _verify_password(pw, s.password_hash)
            if ok:
                if needs_upgrade:
                    # 自動升級舊 SHA-256 hash 為 pbkdf2:sha256
                    s.password_hash = _hash(pw)
                    s.legacy_pw_hash = None
                    db.session.commit()
                session['supplier_email']   = email
                session['supplier_company'] = s.company or ''
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

        material = Material(
            id=mat_id,
            name=name,
            brand=brand,
            spec=spec,
            unit=unit,
            price=price,
            photo_ids=','.join(fids),
            tags=tags,
            supplier_email=current_supplier(),
            status='pending',
            created_at=datetime.utcnow()
        )
        db.session.add(material)
        db.session.commit()

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
    material = Material.query.filter_by(id=mat_id).first()
    if not material:
        abort(404)

    action  = request.form.get('action')
    note    = request.form.get('note','').strip()
    extra   = request.form.get('extra_tags','').strip()

    if action == 'approve':
        material.status = 'active'
        if note:
            material.note = note
        flash('素材已審核上架', 'success')
    elif action == 'reject':
        material.status = 'rejected'
        if note:
            material.note = note
        flash('素材已退回', 'success')
    elif action == 'add_tags':
        existing = material.tags or ''
        combined = ','.join(filter(None, [existing, extra]))
        material.tags = combined
        flash('標籤已補充', 'success')

    db.session.commit()
    return redirect(url_for('admin_dashboard') + '#materials')

@app.errorhandler(403)
def e403(e):
    return render_template('error.html', code=403, msg='沒有權限'), 403

@app.errorhandler(404)
def e404(e):
    return render_template('error.html', code=404, msg='頁面不存在'), 404

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False)

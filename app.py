# -*- coding: utf-8 -*-
import os, io, json, uuid, time, hashlib, base64
import urllib.request as _u_req
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

# ── Secret key — 生產環境未設定時拒絕啟動 ──────────────────────
_is_render = bool(os.environ.get('RENDER'))
_secret_key = os.environ.get('SECRET_KEY', '')
if _is_render and not _secret_key:
    raise RuntimeError(
        '[FATAL] SECRET_KEY environment variable is not set. '
        'Set it in Render dashboard before deploying.'
    )
app.config['SECRET_KEY'] = _secret_key or os.urandom(24)

# ── Session cookie security ────────────────────────────────────
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if _is_render:
    app.config['SESSION_COOKIE_SECURE'] = True

# ── Database config ────────────────────────────────────────────
_db_url_raw = os.environ.get('DATABASE_URL', '')
if _is_render and not _db_url_raw:
    raise RuntimeError(
        '[FATAL] DATABASE_URL environment variable is not set. '
        'Set it in Render dashboard before deploying.'
    )
if not _db_url_raw:
    import warnings
    warnings.warn(
        '[WARNING] DATABASE_URL is not set — using local SQLite (landscape.db). '
        'Data written here will NOT be visible in production.',
        stacklevel=1
    )
    print('[WARNING] DATABASE_URL not set, falling back to sqlite:///landscape.db — '
          'DO NOT use this in production!')
    _db_url_raw = 'sqlite:///landscape.db'
db_url = _db_url_raw
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

# ── AI 模擬設計標籤（業主端，固定集合） ────────────────────────
AI_STYLE_TAGS = {
    '日式禪風':  'Japanese zen garden style with raked gravel, moss, carefully pruned pines and stone lanterns',
    '南洋度假':  'tropical resort style with palm trees, lush layered foliage and a warm vacation atmosphere',
    '現代簡約':  'modern minimalist style with clean lines, geometric layout and restrained elegant planting',
    '地中海風':  'Mediterranean style with terracotta tones, gravel, olive trees and warm sunlight',
    '自然鄉村':  'natural cottage garden style with informal flower borders and soft curved paths',
    '工業風':    'industrial chic style with concrete surfaces, corten steel planters and architectural grasses',
}
AI_ELEMENT_TAGS = {
    '草坪':     'a healthy manicured lawn area',
    '水景':     'a water feature such as a small pond or fountain',
    '木平台':   'a wooden deck platform for outdoor seating',
    '石材步道': 'a natural stone walkway',
    '夜間照明': 'warm landscape accent lighting',
    '涼亭棚架': 'a pergola or pavilion structure',
    '花卉植栽': 'colorful flowering plants and shrubs',
    '竹子':     'bamboo planting used as a green screen',
    '戶外家具': 'tasteful outdoor furniture for relaxing',
    '圍籬圍牆': 'an elegant fence or low garden wall',
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

class AiDesign(db.Model):
    __tablename__ = 'ai_designs'
    id = db.Column(db.String(12), primary_key=True)
    owner_name = db.Column(db.String(120))
    owner_phone = db.Column(db.String(20))
    owner_email = db.Column(db.String(120))
    style = db.Column(db.String(50))
    tags = db.Column(db.String(500))           # 元素標籤 CSV
    source_photo_id = db.Column(db.String(120))  # Google Drive file id（原始現場照）
    result_photo_id = db.Column(db.String(120))  # Google Drive file id（AI 模擬圖）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

class RateLimit(db.Model):
    """Simple per-IP rate-limit log for AI design and form endpoints."""
    __tablename__ = 'rate_limits'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ip = db.Column(db.String(45), nullable=False, index=True)
    endpoint = db.Column(db.String(60), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

# ── Auth helpers ──────────────────────────────────────────────
def _safe_redirect(nxt, default):
    """Only allow relative on-site paths; reject open-redirect attempts."""
    if nxt and isinstance(nxt, str):
        # Must start with exactly one '/', no '//', no scheme, no backslash
        if (nxt.startswith('/') and not nxt.startswith('//') and
                '://' not in nxt and not nxt.startswith('\\')):
            return redirect(nxt)
    return redirect(default)

def _check_field_lengths(**fields):
    """
    Validate field lengths against DB column limits.
    fields: {field_label: (value, max_len)}
    Returns (ok: bool, error_msg: str|None).
    """
    for label, (value, max_len) in fields.items():
        if value and len(value) > max_len:
            return False, f'「{label}」超過 {max_len} 字元上限（目前 {len(value)} 字元）'
    return True, None

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

def _verify_password_for_user(pw, user):
    """
    Verify password for a Member or Supplier model instance.
    Handles the 'MIGRATED_NEEDS_RESET' placeholder left by migration:
    falls back to legacy_pw_hash (bare SHA-256) and on success upgrades
    to pbkdf2:sha256 in-place, returning (ok, upgraded).
    """
    ph = user.password_hash or ''
    # 待重辦帳號：一律不可登入，須用原 Email 重新註冊接回
    if ph == 'NEEDS_REGISTER':
        return False, False
    # Placeholder from migration — use legacy_pw_hash for actual check
    if ph == 'MIGRATED_NEEDS_RESET':
        legacy = user.legacy_pw_hash or ''
        if not legacy:
            return False, False
        ok = _verify_password(pw, legacy)[0] if _is_legacy_sha256(legacy) else False
        if ok:
            user.password_hash = _hash(pw)
            user.legacy_pw_hash = None
        return ok, ok  # ok=True means login allowed; ok also signals upgrade was done
    # Normal path (covers plain pbkdf2 AND bare sha256 written directly)
    ok, needs_upgrade = _verify_password(pw, ph)
    if ok and needs_upgrade:
        user.password_hash = _hash(pw)
        user.legacy_pw_hash = None
    return ok, needs_upgrade

def _is_reclaimable(user):
    """帳號是否為「待重辦」狀態（無任何可登入的憑證）：
    NEEDS_REGISTER 佔位，或舊遷移佔位且無 legacy hash。
    這類帳號允許用原 Email 重新註冊接回（保留名下作品/素材）。"""
    ph = user.password_hash or ''
    return ph == 'NEEDS_REGISTER' or (
        ph == 'MIGRATED_NEEDS_RESET' and not (user.legacy_pw_hash or ''))

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

# ── Rate-limit helpers ─────────────────────────────────────────
def _get_client_ip():
    """Return client IP, honouring X-Forwarded-For (Render sits behind a proxy)."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'

def _rl_check(endpoint, max_calls, window_seconds):
    """
    Returns True if the request is allowed (i.e. under the rate limit).
    Silently allows on any DB error to avoid blocking legitimate users.
    window_seconds: rolling window length in seconds.
    """
    from datetime import timedelta
    try:
        ip = _get_client_ip()
        window_start = datetime.utcnow() - timedelta(seconds=window_seconds)
        count = RateLimit.query.filter(
            RateLimit.ip == ip,
            RateLimit.endpoint == endpoint,
            RateLimit.created_at >= window_start,
        ).count()
        if count >= max_calls:
            return False
        db.session.add(RateLimit(ip=ip, endpoint=endpoint, created_at=datetime.utcnow()))
        db.session.commit()
        return True
    except Exception:
        return True  # fail open — don't break the site

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

# ── AI 模擬設計（Gemini 影像生成） ──────────────────────────────
GEMINI_IMAGE_MODEL = 'gemini-2.5-flash-image'

def _gemini_api_key():
    key = os.environ.get('GEMINI_API_KEY', '').strip()
    if key:
        return key
    try:
        with open(r'E:\keys\gemini.key', encoding='utf-8') as f:  # 本機開發 fallback
            return f.read().strip()
    except OSError:
        return ''

# 業務鐵律：景觀情境圖絕對不加任何文字
_NO_TEXT_RULE = ('STRICT RULE: the generated image must contain absolutely NO text of any kind — '
                 'no words, no letters, no numbers, no watermarks, no logos, no labels, '
                 'no captions, no annotations, no signatures. Pure photographic imagery only.')

def _build_design_prompt(style, elements):
    style_en = AI_STYLE_TAGS.get(style, 'modern minimalist style')
    parts = [
        'Photorealistic landscape design simulation.',
        'Transform this site photo into a beautifully landscaped space in ' + style_en + '.',
        'Keep the original camera angle, perspective, buildings and property boundaries unchanged; '
        'redesign only the outdoor landscape areas.',
    ]
    elems_en = [AI_ELEMENT_TAGS[e] for e in elements if e in AI_ELEMENT_TAGS]
    if elems_en:
        parts.append('Incorporate the following elements naturally: ' + '; '.join(elems_en) + '.')
    parts.append('High-end landscape magazine quality, natural daylight, realistic materials and plants.')
    parts.append(_NO_TEXT_RULE)
    return ' '.join(parts)

def generate_design_image(photo_bytes, mime_type, style, elements):
    """以 Gemini 影像模型將現場照片轉成景觀模擬設計圖，回傳 (bytes, mime)。"""
    key = _gemini_api_key()
    if not key:
        raise RuntimeError('AI 影像服務尚未啟用（未設定 GEMINI_API_KEY）')
    body = json.dumps({
        'contents': [{'parts': [
            {'text': _build_design_prompt(style, elements)},
            {'inline_data': {'mime_type': mime_type,
                             'data': base64.b64encode(photo_bytes).decode('ascii')}},
        ]}],
        'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']},
    }).encode('utf-8')
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{GEMINI_IMAGE_MODEL}:generateContent?key={key}')
    req = _u_req.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with _u_req.urlopen(req, timeout=150) as resp:
        data = json.loads(resp.read())
    for cand in data.get('candidates', []):
        for part in cand.get('content', {}).get('parts', []):
            blob = part.get('inlineData') or part.get('inline_data') or {}
            if blob.get('data'):
                return base64.b64decode(blob['data']), (blob.get('mimeType')
                                                        or blob.get('mime_type') or 'image/png')
    raise RuntimeError('AI 未回傳影像，請稍後再試')

def upload_design_to_drive(design_id, src_bytes, src_mime, src_ext, gen_bytes, gen_mime):
    """原始照 + 模擬圖一起上傳 Drive，回傳 (src_id, gen_id)；失敗時清除孤兒檔案。"""
    svc = _drive()
    folder = svc.files().create(body={
        'name': f'aidsgn_{design_id}',
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [DRIVE_FOLDER_ID],
    }, fields='id').execute()
    wfid = folder['id']
    gen_ext = 'png' if 'png' in (gen_mime or '') else 'jpg'
    ids = []
    try:
        for fname, blob, mime in ((f'{design_id}_site.{src_ext}', src_bytes, src_mime),
                                  (f'{design_id}_design.{gen_ext}', gen_bytes, gen_mime)):
            media = MediaIoBaseUpload(io.BytesIO(blob), mimetype=mime or 'image/jpeg', resumable=False)
            cf = svc.files().create(body={'name': fname, 'parents': [wfid]},
                                    media_body=media, fields='id').execute()
            fid = cf['id']
            svc.permissions().create(fileId=fid, body={'type': 'anyone', 'role': 'reader'}).execute()
            ids.append(fid)
    except Exception:
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
    return ids[0], ids[1]

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
    # Anti-duplicate: same IP may not rate again within 60 seconds
    if not _rl_check('rate_work', max_calls=1, window_seconds=60):
        flash('您剛剛已送出評分，請稍候再試', 'error')
        return redirect(url_for('work_detail', work_id=work_id) + '#ratings')
    name  = request.form.get('name','').strip()
    phone = request.form.get('phone','').strip()
    score = request.form.get('score','').strip()
    note  = request.form.get('note','').strip()
    if not name or not score or not score.isdigit() or not (1 <= int(score) <= 5):
        flash('請填寫姓名並選擇評分', 'error')
        return redirect(url_for('work_detail', work_id=work_id) + '#rate')
    ok, err = _check_field_lengths(姓名=(name, 120), 電話=(phone, 20), 備註=(note, 2000))
    if not ok:
        flash(err, 'error')
        return redirect(url_for('work_detail', work_id=work_id) + '#rate')

    try:
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
        # Recalc avg within the same transaction
        scores = (db.session.query(func.avg(Rating.score), func.count(Rating.id))
                  .filter(Rating.work_id == work_id).first())
        # include the new rating not yet committed — use python calc instead
        all_scores = [r.score for r in work.ratings] + [int(score)]
        work.avg_rating   = round(sum(all_scores) / len(all_scores), 1)
        work.rating_count = len(all_scores)
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('評分儲存失敗，請稍後再試', 'error')
        return redirect(url_for('work_detail', work_id=work_id) + '#rate')

    flash('感謝您的評價！', 'success')
    return redirect(url_for('work_detail', work_id=work_id) + '#ratings')

# ── AI 模擬設計（業主端，免登入） ──────────────────────────────
@app.route('/ai-design', methods=['GET', 'POST'])
def ai_design():
    ctx = dict(styles=list(AI_STYLE_TAGS.keys()),
               elements=list(AI_ELEMENT_TAGS.keys()),
               ai_ready=bool(_gemini_api_key()))
    if request.method == 'POST':
        # Rate limit: 3 AI generations per IP per day (86400 seconds)
        if not _rl_check('ai_design', max_calls=3, window_seconds=86400):
            flash('今日 AI 設計生成次數已達上限（每日 3 次），歡迎來電洽詢 ' + CONTACT_PHONE, 'error')
            return render_template('public/ai_design.html', **ctx)

        style    = request.form.get('style', '').strip()
        chosen   = [e for e in request.form.getlist('elements') if e in AI_ELEMENT_TAGS][:6]
        name     = request.form.get('name', '').strip()
        phone    = request.form.get('phone', '').strip()
        photo    = request.files.get('photo')

        if style not in AI_STYLE_TAGS:
            flash('請選擇一種設計風格', 'error')
            return render_template('public/ai_design.html', **ctx)
        if not photo or not photo.filename:
            flash('請上傳一張現場照片', 'error')
            return render_template('public/ai_design.html', **ctx)
        ext = os.path.splitext(photo.filename)[1].lstrip('.').lower()
        if ext not in _ALLOWED_EXTENSIONS:
            flash(f'不支援的檔案格式「.{ext}」，僅接受：{", ".join(sorted(_ALLOWED_EXTENSIONS))}', 'error')
            return render_template('public/ai_design.html', **ctx)
        src_bytes = photo.stream.read()
        if len(src_bytes) > _MAX_FILE_BYTES:
            flash('照片超過 10 MB 上限，請壓縮後再試', 'error')
            return render_template('public/ai_design.html', **ctx)
        src_mime = photo.content_type or f'image/{"jpeg" if ext == "jpg" else ext}'

        try:
            gen_bytes, gen_mime = generate_design_image(src_bytes, src_mime, style, chosen)
        except Exception as e:
            flash(f'AI 生成失敗：{e}', 'error')
            return render_template('public/ai_design.html', **ctx)

        design_id = str(uuid.uuid4()).replace('-', '')[:12]
        try:
            src_id, gen_id = upload_design_to_drive(design_id, src_bytes, src_mime, ext,
                                                    gen_bytes, gen_mime)
        except Exception as e:
            flash(f'圖片儲存失敗：{e}', 'error')
            return render_template('public/ai_design.html', **ctx)

        design = AiDesign(
            id=design_id,
            owner_name=name,
            owner_phone=phone,
            owner_email=current_user() or '',
            style=style,
            tags=','.join(chosen),
            source_photo_id=src_id,
            result_photo_id=gen_id,
            created_at=datetime.utcnow(),
        )
        db.session.add(design)
        db.session.commit()
        return redirect(url_for('ai_design_result', design_id=design_id))
    return render_template('public/ai_design.html', **ctx)

@app.route('/ai-design/<design_id>')
def ai_design_result(design_id):
    design = AiDesign.query.filter_by(id=design_id).first()
    if not design:
        abort(404)
    src_url = photo_urls(design.source_photo_id, size='w1000')[0]
    gen_url = photo_urls(design.result_photo_id, size='w1000')[0]
    tags = [t for t in (design.tags or '').split(',') if t]
    return render_template('public/ai_design_result.html', design=design,
                           src_url=src_url, gen_url=gen_url, tags=tags,
                           contact_phone=CONTACT_PHONE)

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        # Anti-duplicate: same IP may not submit again within 60 seconds
        if not _rl_check('contact', max_calls=1, window_seconds=60):
            flash('您剛剛已送出訊息，請稍候再試', 'error')
            work_id = request.form.get('work_id','').strip()
            return render_template('public/contact.html', work_id=work_id,
                                   contact_phone=CONTACT_PHONE)
        name    = request.form.get('name','').strip()
        phone   = request.form.get('phone','').strip()
        message = request.form.get('message','').strip()
        work_id = request.form.get('work_id','').strip()
        ok, err = _check_field_lengths(
            姓名=(name, 120), 電話=(phone, 20), 訊息=(message, 2000),
        )
        if not ok:
            flash(err, 'error')
            return render_template('public/contact.html', work_id=work_id,
                                   contact_phone=CONTACT_PHONE)
        contact_obj = Contact(
            id=str(uuid.uuid4())[:8],
            name=name,
            phone=phone,
            message=message,
            work_id=work_id,
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
        ok, err = _check_field_lengths(
            Email=(email, 120), 姓名=(name, 120),
            公司=(company, 120), 電話=(phone, 20),
        )
        if not ok:
            flash(err, 'error')
            return render_template('auth/register.html')
        existing = find_member(email)
        if existing:
            if _is_reclaimable(existing):
                # 待重辦帳號：用原 Email 重新註冊即接回原帳號（作品保留）
                existing.password_hash = _hash(pw)
                existing.legacy_pw_hash = None
                existing.name = name
                existing.company = company
                existing.phone = phone
                existing.status = 'active'
                db.session.commit()
                session['email'] = email
                session['name'] = name
                flash(f'歡迎回來，{name}！帳號已重新啟用', 'success')
                return redirect(url_for('upload'))
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
            ok, _upgraded = _verify_password_for_user(pw, m)
            if ok:
                db.session.commit()  # persist any hash upgrade
                session['email'] = email
                session['name']  = m.name or ''
                nxt = request.form.get('next', '')
                default = url_for('admin_dashboard') if email == ADMIN_EMAIL else url_for('upload')
                return _safe_redirect(nxt, default)
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
        ok, err = _check_field_lengths(
            作品名稱=(name, 255), 標籤=(tags, 500), 金額=(price, 50), 規模=(scale, 50),
        )
        if not ok:
            flash(err, 'error')
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

        ok, err = _check_field_lengths(
            縣市=(county, 50), 服務縣市=(service, 500), 最小接案金額=(min_amt, 50),
            LINE_ID=(line_id, 120), 官網=(website, 255),
        )
        if not ok:
            flash(err, 'error')
            return redirect(url_for('my_profile'))

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
        name    = request.form.get('name','').strip()
        company = request.form.get('company','').strip()
        phone   = request.form.get('phone','').strip()
        email   = request.form.get('email','').strip()
        amount  = request.form.get('amount','').strip()
        message = request.form.get('message','').strip()
        ok, err = _check_field_lengths(
            姓名=(name, 120), 公司=(company, 120), 電話=(phone, 20),
            Email=(email, 120), 金額=(amount, 50), 訊息=(message, 2000),
        )
        if not ok:
            flash(err, 'error')
            return render_template('public/contact_investor.html')
        inquiry = InvestorInquiry(
            id=str(uuid.uuid4())[:8],
            name=name, company=company, phone=phone,
            email=email, amount=amount, message=message,
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
        ok, err = _check_field_lengths(
            Email=(email, 120), 公司名稱=(company, 120),
            聯絡人=(contact, 120), 電話=(phone, 20),
        )
        if not ok:
            flash(err, 'error')
            return render_template('supplier/register.html')
        existing = find_supplier(email)
        if existing:
            if _is_reclaimable(existing):
                # 待重辦帳號：用原 Email 重新註冊即接回原帳號（素材保留）
                existing.password_hash = _hash(pw)
                existing.legacy_pw_hash = None
                existing.company = company
                existing.contact_name = contact
                existing.phone = phone
                existing.status = 'active'
                db.session.commit()
                session['supplier_email']   = email
                session['supplier_company'] = company
                flash(f'歡迎回來，{company}！帳號已重新啟用', 'success')
                return redirect(url_for('supplier_upload'))
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
            ok, _upgraded = _verify_password_for_user(pw, s)
            if ok:
                db.session.commit()  # persist any hash upgrade
                session['supplier_email']   = email
                session['supplier_company'] = s.company or ''
                nxt = request.form.get('next', '')
                return _safe_redirect(nxt, url_for('supplier_upload'))
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
        ok, err = _check_field_lengths(
            素材名稱=(name, 255), 品牌=(brand, 120), 規格尺寸=(spec, 255),
            單位=(unit, 50), 零售價=(price, 50), 標籤=(tags, 500),
        )
        if not ok:
            flash(err, 'error')
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

# 確保新表（如 ai_designs）在部署後自動建立；create_all 具冪等性，不動既有表
with app.app_context():
    try:
        db.create_all()
    except Exception as _e:
        print(f'[WARN] db.create_all skipped: {_e}')

if __name__ == '__main__':
    app.run(debug=False)

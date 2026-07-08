# -*- coding: utf-8 -*-
import os, io, json, uuid, time, hashlib, base64, math
import re
import urllib.request as _u_req
import urllib.parse as _u_parse
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort, make_response)
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
# 畫筆遮罩（mask_overlay）以 base64 hidden 欄位隨表單上傳：
# Werkzeug >=3.1 對多部分表單「非檔案欄位」預設只允許 500KB，會在進到路由前就回 413。
# 放寬到 12MB（遮罩規格上限 8MB + 餘裕）；同時設 config（Flask >=3.1）與 class 屬性（Flask 3.0 相容）。
app.config['MAX_FORM_MEMORY_SIZE'] = 12 * 1024 * 1024
app.request_class.max_form_memory_size = 12 * 1024 * 1024

ADMIN_EMAIL     = os.environ.get('ADMIN_EMAIL', 'g2349311@gmail.com')
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID', '1qCzsnVGQl6RAQprtWuh4aCMt98J59BkD')
CONTACT_PHONE   = os.environ.get('CONTACT_PHONE', '0910-006-229')

# ── 客戶帳號體系（與聯盟夥伴完全分離）外部服務金鑰 ──────────────
# 未設定時優雅降級：Google 按鈕不渲染、reCAPTCHA 跳過驗證且不渲染 widget
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '').strip()
RECAPTCHA_SITE_KEY     = os.environ.get('RECAPTCHA_SITE_KEY', '').strip()
RECAPTCHA_SECRET_KEY   = os.environ.get('RECAPTCHA_SECRET_KEY', '').strip()

MASTER_TAGS = {
    '設計風格': ['現代簡約', '日式禪風', '南洋熱帶', '地中海風', '自然鄉村', '工業風'],
    '適用空間': ['前院', '後院', '中庭', '屋頂花園', '陽台', '商業空間'],
    '植栽':     ['草坪', '喬木', '灌木', '竹子', '花卉', '水生植物', '多肉植物'],
    '鋪面材料': ['天然石材', '木材/塑木', '磚砌', '碎石子', '混凝土'],
    '家具設施': ['涼亭棚架', '水景噴泉', '戶外家具', '戶外照明', '圍籬圍牆', '景觀步道'],
}

# ── AI 模擬設計標籤（業主端，固定集合） ────────────────────────
# 來源：E:\AI-Wiki\Projects\景觀標籤提示詞庫.md（2026-06-13 森哥審核定案）
AI_STYLE_TAGS = {
    '日式禪風':    'Japanese zen garden style with raked gravel, moss, carefully pruned pines and stone lanterns',
    '南洋度假':    'tropical resort style with palm trees, lush layered foliage and a warm vacation atmosphere',
    '現代簡約':    'modern minimalist style with clean lines, geometric layout and restrained elegant planting',
    '地中海風':    'Mediterranean style with terracotta tones, gravel, olive trees and warm sunlight',
    '自然鄉村':    'natural cottage garden style with informal flower borders and soft curved paths',
    '工業風':      'industrial chic style with concrete surfaces, corten steel planters and architectural grasses',
    '雜木林風':    'naturalistic Japanese zoki woodland style with multi-stemmed deciduous trees, dappled shade, woodland underplanting of ferns and shade perennials',
    '新中式庭園':  'contemporary Chinese garden style with framed views, dark-tile accents, sculptural black pines, white gravel and minimalist water basin',
    '森林苔庭':    'shaded forest moss garden style with carpets of moss, ferns, stepping stones and a cool tranquil atmosphere',
    '北歐極簡':    'Scandinavian minimal style with pale timber decking, soft ornamental grasses, muted greens and airy uncluttered openness',
    '荒野草原風':  'naturalistic meadow style with flowing drifts of ornamental grasses and perennials, soft seed heads catching the light',
    '侘寂風':      'wabi-sabi garden style with weathered natural stone, aged timber, raked gravel, muted earthy tones and the quiet beauty of imperfection',
    '法式對稱庭園': 'formal French garden style with symmetrical clipped hedges, geometric parterres and an elegant central axis',
    '旱生節水景觀': 'xeriscape style with drought-tolerant agaves, gravel mulch, sculptural cacti and water-wise design',
}
AI_ELEMENT_TAGS = {
    # 植栽
    '花卉植栽':     'colorful flowering plants and shrubs',
    '竹子':         'bamboo planting used as a green screen',
    '草坪':         'a healthy manicured lawn area',
    '景觀喬木':     'a sculptural specimen tree as the focal point, beautifully pruned with layered branching',
    '植生綠牆':     'a lush living green wall densely planted with varied foliage textures',
    '觀賞草':       'soft ornamental grasses swaying gently, adding movement and texture',
    '蕨類陰生植栽': 'shade-loving ferns and broadleaf foliage layered in a cool green understory',
    '多肉旱生植栽': 'drought-tolerant succulents and agaves arranged in gravel mulch',
    '香草可食花園': 'raised edible garden beds with herbs and leafy vegetables in neat rows',
    '花箱盆栽組':   'curated container garden with large planter boxes in varied heights',
    '攀藤棚架植栽': 'flowering climbing vines trained over a pergola creating a living canopy',
    '原生蜜源花園': 'native pollinator-friendly planting alive with butterflies, layered wildflowers and grasses',
    # 硬體構造
    '木平台':       'a wooden deck platform for outdoor seating',
    '石材步道':     'a natural stone walkway',
    '涼亭棚架':     'a pergola or pavilion structure',
    '圍籬圍牆':     'an elegant fence or low garden wall',
    '戶外家具':     'tasteful outdoor furniture for relaxing',
    '南方松木作':   'warm-toned treated timber screens and planters with visible wood grain',
    '汀步石':       'irregular stepping stones set into lawn or gravel forming a casual path',
    '抿石子鋪面':   'smooth exposed-aggregate paving in warm grey tones',
    '鐵件構造':     'minimal black steel garden structures with clean thin profiles',
    '遮陽帆':       'a taut shade sail stretched overhead casting soft shadows',
    '卵石旱溪':     'a dry creek bed of smooth river pebbles winding through the planting',
    # 水景與氛圍
    '水景':         'a water feature such as a small pond or fountain',
    '錦鯉魚池':     'a clear koi pond with colorful koi fish and natural rock edging',
    '壁面瀑布水幕': 'a sleek wall-mounted waterfall feature with a thin sheet of falling water',
    '生態池':       'a naturalistic ecological pond with aquatic plants and gravel margins',
    '倒影靜水池':   'a still dark reflecting pool mirroring the sky and surrounding greenery',
    '雨水花園':     'a rain garden of moisture-loving plants in a shallow planted basin with river stones',
    '枯山水砂紋':   'raked white gravel patterns flowing around feature stones',
    '霧森系統':     'fine cooling mist drifting through the foliage creating a mystical atmosphere',
    '夜間照明':     'warm landscape accent lighting',
    # 機能空間
    '屋頂花園':     'a rooftop garden with windproof planting, deck flooring and skyline views',
    '陽台綠化':     'a compact balcony garden with vertical planting and a small seating nook',
    '中庭天井':     'a serene lightwell courtyard garden visible from indoors',
    '玄關門面':     'an impressive entrance planting composition framing the doorway',
    '戶外用餐區':   'an inviting outdoor dining area surrounded by layered greenery',
    '兒童遊戲草地': 'a safe open play lawn with soft ground and low planting edges',
    '人工草皮':     'premium artificial turf with realistic texture, perfectly even',
    '火爐聚會區':   'an architectural fire pit with built-in bench seating, warm flames glowing',
    '戶外廚房吧台': 'a sleek outdoor kitchen counter with built-in grill under a sheltering pergola',
    '靜謐角落':     'a cozy intimate garden nook with a comfortable chair tucked among lush planting',
}

# 元素分組（僅供模板顯示，內容必須與 AI_ELEMENT_TAGS 一一對應）
AI_ELEMENT_GROUPS = [
    ('植栽', ['花卉植栽', '竹子', '草坪', '景觀喬木', '植生綠牆', '觀賞草',
              '蕨類陰生植栽', '多肉旱生植栽', '香草可食花園', '花箱盆栽組',
              '攀藤棚架植栽', '原生蜜源花園']),
    ('硬體構造', ['木平台', '石材步道', '涼亭棚架', '圍籬圍牆', '戶外家具',
                  '南方松木作', '汀步石', '抿石子鋪面', '鐵件構造', '遮陽帆',
                  '卵石旱溪']),
    ('水景與氛圍', ['水景', '錦鯉魚池', '壁面瀑布水幕', '生態池', '倒影靜水池',
                    '雨水花園', '枯山水砂紋', '霧森系統', '夜間照明']),
    ('機能空間', ['屋頂花園', '陽台綠化', '中庭天井', '玄關門面', '戶外用餐區',
                  '兒童遊戲草地', '人工草皮', '火爐聚會區', '戶外廚房吧台',
                  '靜謐角落']),
]

# 啟動時驗證：分組內容與 AI_ELEMENT_TAGS 完全一致（不多、不少、不重複）
_group_keys = [k for _, _keys in AI_ELEMENT_GROUPS for k in _keys]
assert len(_group_keys) == len(set(_group_keys)), 'AI_ELEMENT_GROUPS 有重複元素'
assert set(_group_keys) == set(AI_ELEMENT_TAGS.keys()), \
    f'AI_ELEMENT_GROUPS 與 AI_ELEMENT_TAGS 不一致：{set(_group_keys) ^ set(AI_ELEMENT_TAGS.keys())}'

# 矛盾標籤設定（前端互斥隱藏 + 後端防呆共用）
AI_ELEMENT_EXCLUSIVE_GROUPS = [
    ['草坪', '人工草皮'],                              # 草地擇一
    ['水景', '錦鯉魚池', '生態池', '倒影靜水池'],      # 水體擇一
    ['屋頂花園', '陽台綠化', '中庭天井', '玄關門面'],  # 空間屬性擇一
]
AI_ELEMENT_CONFLICT_PAIRS = [
    ['多肉旱生植栽', '蕨類陰生植栽'],
    ['多肉旱生植栽', '雨水花園'],
    ['多肉旱生植栽', '霧森系統'],
]

def _elements_conflict(a, b):
    """兩個元素標籤是否互斥（同擇一群組或矛盾配對）。"""
    for g in AI_ELEMENT_EXCLUSIVE_GROUPS:
        if a in g and b in g:
            return True
    for p in AI_ELEMENT_CONFLICT_PAIRS:
        if {a, b} == set(p):
            return True
    return False

def _filter_conflicting_elements(chosen):
    """依表單順序去除互斥元素：保留先選的、丟棄後選的（含重複）。"""
    kept = []
    for e in chosen:
        if e in kept:
            continue
        if any(_elements_conflict(e, k) for k in kept):
            continue
        kept.append(e)
    return kept

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
    last_login = db.Column(db.DateTime)  # 最後登入時間（登入成功時更新）
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
    status = db.Column(db.String(20), default='active')  # pending=待審核 active=已核准 rejected=已拒絕
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)  # 最後登入時間（登入成功時更新）
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
    # 同行審核狀態：none=未申請 pending=審核中 approved=已核准 rejected=已拒絕/已撤銷
    status = db.Column(db.String(20), default='none')
    applied_at = db.Column(db.DateTime)   # 送出申請時間
    reviewed_at = db.Column(db.DateTime)  # 管理員審核時間

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

class AppUser(db.Model):
    """終端客戶帳號（業主），與聯盟夥伴 Member / Supplier 完全分離。"""
    __tablename__ = 'app_users'
    id = db.Column(db.String(12), primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(120))
    password_hash = db.Column(db.String(255), nullable=True)  # Google-only 帳號可為空
    google_sub = db.Column(db.String(64), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)  # 最後登入時間（登入成功時更新）

class UserEvent(db.Model):
    """行為事件記錄（瀏覽作品 / AI 生成等），供推薦與後台統計使用。"""
    __tablename__ = 'user_events'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    etype = db.Column(db.String(40), nullable=False, index=True)
    user_email = db.Column(db.String(120), nullable=True)
    session_key = db.Column(db.String(64))   # 匿名訪客：簽名 session cookie 內的隨機 id
    work_id = db.Column(db.String(12), nullable=True, index=True)
    tags = db.Column(db.Text)                # 事件當下作品標籤快照（CSV）
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

def partner_status(email):
    """同行審核狀態：none / pending / approved / rejected（none=尚未申請）。
    每次呼叫直接查 DB，不快取在 session，核准或撤銷後立即生效。"""
    p = PartnerProfile.query.filter_by(email=(email or '').lower()).first()
    return (p.status or 'none') if p else 'none'

def supplier_status(email):
    """供應商帳號狀態：none / pending / active / rejected（none=無供應商帳號）。"""
    s = Supplier.query.filter_by(email=(email or '').lower()).first()
    return (s.status or 'none') if s else 'none'

def is_partner():
    """目前登入會員是否為已核准同行（管理員視同同行）。每請求查 DB，即時生效。"""
    email = current_user()
    if not email:
        return False
    if is_admin():
        return True
    return partner_status(email) == 'approved'

def partner_required(f):
    """同行專屬功能（上傳/編修作品）：須登入且同行申請已核准；管理員不受限。"""
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for('login', next=request.path))
        if not is_partner():
            flash('此功能僅開放「已核准的同行夥伴」使用，請先提出申請', 'error')
            return redirect(url_for('apply_partner'))
        return f(*a, **kw)
    return wrapped

def current_supplier():
    return session.get('supplier_email')

def supplier_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_supplier():
            return redirect(url_for('supplier_login', next=request.path))
        # 每請求查 DB 確認帳號仍為已核准（active）狀態，核准/撤銷即時生效
        s = find_supplier(current_supplier())
        if not s or s.status != 'active':
            session.pop('supplier_email', None)
            session.pop('supplier_company', None)
            flash('您的供應商帳號尚未通過審核或已被停權，如有疑問請聯絡管理員', 'error')
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

# ── Customer (AppUser) helpers ─────────────────────────────────
def _new_id():
    return str(uuid.uuid4()).replace('-', '')[:12]

def current_customer():
    """客戶登入狀態（session['customer_email']），與夥伴 session['email'] 完全分離。"""
    return session.get('customer_email')

def find_app_user(email):
    return AppUser.query.filter_by(email=(email or '').lower()).first()

def customer_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_customer():
            return redirect(url_for('account_login', next=request.path))
        return f(*a, **kw)
    return wrapped

def _recaptcha_enabled():
    return bool(RECAPTCHA_SITE_KEY and RECAPTCHA_SECRET_KEY)

def _verify_recaptcha(response_token):
    """reCAPTCHA v2 後端驗證。金鑰未設定時一律通過（優雅降級）。"""
    if not _recaptcha_enabled():
        return True
    if not response_token:
        return False
    try:
        body = _u_parse.urlencode({
            'secret': RECAPTCHA_SECRET_KEY,
            'response': response_token,
            'remoteip': _get_client_ip(),
        }).encode('utf-8')
        req = _u_req.Request('https://www.google.com/recaptcha/api/siteverify', data=body)
        with _u_req.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return bool(data.get('success'))
    except Exception:
        return False  # 驗證服務異常時保守拒絕，請使用者重試

def _google_verify_id_token(credential):
    """以 Google tokeninfo 端點驗證 GIS ID token（含簽章與效期），回傳 claims dict。"""
    url = ('https://oauth2.googleapis.com/tokeninfo?id_token='
           + _u_parse.quote(credential, safe=''))
    req = _u_req.Request(url)
    with _u_req.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def _google_login_uri(nxt=''):
    """GIS 按鈕的 data-login_uri（絕對網址；Render 走 proxy 須強制 https）。"""
    if not GOOGLE_OAUTH_CLIENT_ID:
        return ''
    kwargs = {'_external': True}
    if nxt:
        kwargs['next'] = nxt
    uri = url_for('account_google', **kwargs)
    if _is_render and uri.startswith('http://'):
        uri = 'https://' + uri[len('http://'):]
    return uri

@app.context_processor
def _customer_template_vars():
    return dict(
        google_client_id=GOOGLE_OAUTH_CLIENT_ID,
        recaptcha_site_key=(RECAPTCHA_SITE_KEY if _recaptcha_enabled() else ''),
    )

# ── 行為事件記錄 ────────────────────────────────────────────────
def _event_session_key():
    """匿名訪客識別：首訪發放隨機 id，存於簽名 session cookie。"""
    sk = session.get('event_sk')
    if not sk:
        sk = uuid.uuid4().hex
        session['event_sk'] = sk
    return sk

def _log_event(etype, work_id=None, tags=None, user_email=None):
    """寫入一筆行為事件；任何失敗 rollback 後吞掉，絕不影響頁面。"""
    try:
        if user_email is None:
            user_email = current_customer() or current_user() or None
        db.session.add(UserEvent(
            etype=etype,
            user_email=user_email,
            session_key=_event_session_key(),
            work_id=work_id,
            tags=tags,
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

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

# ── 單一登入接橋（identity bridge） ─────────────────────────────
@app.before_request
def _identity_bridge():
    """單一登入接橋：從任一入口登入後，自動補上同一 Email 本來就擁有的其他身分。

    原則：只「授予該 Email 原本就有的資格」，不做任何資格提升——
    - 夥伴（member）：members 表有該 Email 且同行審核 approved 才接橋；
      管理員特例：Email 等於 ADMIN_EMAIL 即接橋（is_admin 只比對 email，
      即使 members 表無此列也放行）。
    - 客戶（app_users）：表中有該 Email 即接橋。
    - 供應商（suppliers）：該 Email 存在且 status == 'active' 才接橋。
    每請求開頭執行、查詢皆為索引輕量查詢；任何例外一律吞掉，不影響原請求。
    """
    if request.endpoint == 'static':
        return  # 靜態檔不需身分，省 DB 查詢
    try:
        # 來源 Email：以已登入的身分為準（夥伴 > 客戶 > 供應商）
        src = (session.get('email') or session.get('customer_email')
               or session.get('supplier_email'))
        if not src:
            return
        # 1) 補夥伴身分（session['email']）
        if not session.get('email'):
            m = find_member(src)
            if src == ADMIN_EMAIL:
                session['email'] = src
                session['name'] = ((m.name if m else '') or
                                   session.get('customer_name', ''))
            elif m and partner_status(src) == 'approved':
                session['email'] = src
                session['name'] = m.name or session.get('customer_name', '')
        # 2) 補客戶身分（session['customer_email']）
        if not session.get('customer_email'):
            u = find_app_user(src)
            if u:
                session['customer_email'] = u.email
                session['customer_name'] = u.name or session.get('name', '')
        # 3) 補供應商身分（session['supplier_email']）
        if not session.get('supplier_email'):
            s = find_supplier(src)
            if s and s.status == 'active':
                session['supplier_email'] = s.email
                session['supplier_company'] = s.company or ''
    except Exception:
        # 接橋失敗絕不影響原請求；rollback 避免壞掉的交易狀態外洩
        try:
            db.session.rollback()
        except Exception:
            pass

def _clear_identity_session():
    """登出時統一清除所有身分 session 鍵。

    因為接橋會自動補齊其他身分，若只清單一身分，下個請求會立刻被接橋
    補回來（等於登不出去），故三個登出路由一律全清，避免殘留半身分。
    """
    for k in ('email', 'name', 'customer_email', 'customer_name',
              'supplier_email', 'supplier_company'):
        session.pop(k, None)

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

def _drive_thumb_url(file_id, size='w600'):
    """Drive 縮圖直連網址（僅供伺服器端內部抓圖；瀏覽器顯示一律走 /img 代理）。"""
    return f'https://drive.google.com/thumbnail?id={file_id}&sz={size}'


def photo_urls(ids_str, size='w600'):
    """回傳第一方代理網址：手機 webview（LINE 內建瀏覽器等）會擋 Google
    Drive 圖片外連，改由本站 /img/<id> 轉發後全平台可顯示。"""
    return [f'/img/{fid.strip()}?sz={size}'
            for fid in (ids_str or '').split(',') if fid.strip()]


_IMG_ID_RE = re.compile(r'^[A-Za-z0-9_-]{10,90}$')
_IMG_SZ_OK = {'w200', 'w400', 'w600', 'w1000', 'w1600'}


@app.route('/img/<file_id>')
def drive_image(file_id):
    """Google Drive 圖片第一方代理（含瀏覽器快取一天）。"""
    if not _IMG_ID_RE.match(file_id or ''):
        abort(404)
    sz = request.args.get('sz', 'w600')
    if sz not in _IMG_SZ_OK:
        sz = 'w600'
    try:
        req = _u_req.Request(_drive_thumb_url(file_id, sz),
                             headers={'User-Agent': 'Mozilla/5.0'})
        with _u_req.urlopen(req, timeout=30) as resp:
            data = resp.read(_MAX_FILE_BYTES + 1)
            ctype = resp.headers.get('Content-Type', 'image/jpeg')
    except Exception:
        abort(404)
    if not data or len(data) > _MAX_FILE_BYTES:
        abort(404)
    r = make_response(data)
    r.headers['Content-Type'] = ctype
    r.headers['Cache-Control'] = 'public, max-age=86400'
    return r

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
                             current_customer=current_customer,
                             is_partner=is_partner,
                             partner_status=partner_status,
                             supplier_status=supplier_status,
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
GEMINI_DETECT_MODEL = 'gemini-2.5-flash'  # 作品標籤偵測（文字模型，非 image 模型）

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

def _build_design_prompt(style, elements, has_ref=False, has_mask=False):
    style_en = AI_STYLE_TAGS.get(style, 'modern minimalist style')
    parts = [
        'Photorealistic landscape design simulation.',
        'Transform this site photo into a beautifully landscaped space in ' + style_en + '.',
        'Keep the original camera angle, perspective, buildings and property boundaries unchanged; '
        'redesign only the outdoor landscape areas.',
    ]
    if has_mask:
        parts.append(
            'A copy of the site photo overlaid with semi-transparent red markings is also '
            'provided: redesign ONLY the red-marked region; every unmarked area must remain '
            'exactly identical to the original site photo.')
    if has_ref:
        if has_mask:
            # 有遮罩時圖片不只兩張，避免 FIRST/SECOND 序數誤導
            parts.append(
                'A completed project by our own design team is also provided purely as a '
                'style reference: follow its material vocabulary, planting composition style '
                'and craftsmanship quality, but do NOT copy its site layout or replace the '
                'client site with it.')
        else:
            parts.append(
                'Two images are provided. The FIRST image is the client site photo: strictly '
                'preserve its camera angle, perspective, buildings and property boundaries, and '
                'transform only the landscape areas. The SECOND image is a completed project by '
                'our own design team, provided purely as a style reference: follow its material '
                'vocabulary, planting composition style and craftsmanship quality, but do NOT '
                'copy its site layout or replace the client site with it.')
    elems_en = [AI_ELEMENT_TAGS[e] for e in elements if e in AI_ELEMENT_TAGS]
    if elems_en:
        parts.append('Incorporate the following elements naturally: ' + '; '.join(elems_en) + '.')
    parts.append('High-end landscape magazine quality, natural daylight, realistic materials and plants.')
    parts.append(_NO_TEXT_RULE)
    return ' '.join(parts)

def generate_design_image(photo_bytes, mime_type, style, elements,
                          ref_bytes=None, ref_mime=None,
                          mask_bytes=None, mask_mime=None):
    """以 Gemini 影像模型將現場照片轉成景觀模擬設計圖，回傳 (bytes, mime)。

    可選 ref_bytes / ref_mime：本團隊完成作品照片，作為風格參照；
    可選 mask_bytes / mask_mime：現場照疊上半透明紅色標記的合成圖，
    紅色區域 = 唯一允許改造的範圍；皆未提供時行為與舊版完全相同（向後相容）。
    """
    key = _gemini_api_key()
    if not key:
        raise RuntimeError('AI 影像服務尚未啟用（未設定 GEMINI_API_KEY）')
    parts = [
        {'text': _build_design_prompt(style, elements, has_ref=bool(ref_bytes),
                                      has_mask=bool(mask_bytes))},
        {'inline_data': {'mime_type': mime_type,
                         'data': base64.b64encode(photo_bytes).decode('ascii')}},
    ]
    if mask_bytes:
        parts.append({'text': (
            'The next image is the SAME site photo overlaid with semi-transparent red '
            'markings drawn by the client: the red-marked region is the ONLY area allowed '
            'to be redesigned; every unmarked area must remain exactly identical to the '
            'original site photo. The red tint is purely an annotation mask — the output '
            'image must NOT contain any red tint, red markings or red color cast.')})
        parts.append({'inline_data': {'mime_type': mask_mime or 'image/jpeg',
                                      'data': base64.b64encode(mask_bytes).decode('ascii')}})
    if ref_bytes:
        parts.append({'text': (
            'Style reference image below — a completed landscape project by our design '
            'team. Use it only for material vocabulary, planting style and craftsmanship '
            'quality; do not copy its site layout.')})
        parts.append({'inline_data': {'mime_type': ref_mime or 'image/jpeg',
                                      'data': base64.b64encode(ref_bytes).decode('ascii')}})
    body = json.dumps({
        'contents': [{'parts': parts}],
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

def refine_design_image(prev_bytes, prev_mime, instruction):
    """以 Gemini 影像模型依客戶文字指示微調既有模擬圖，回傳 (bytes, mime)。

    僅依客戶指示修改，其餘構圖／視角／既有元素保持不變；_NO_TEXT_RULE 照常附加。
    """
    key = _gemini_api_key()
    if not key:
        raise RuntimeError('AI 影像服務尚未啟用（未設定 GEMINI_API_KEY）')
    prompt = (
        'The image provided is an existing photorealistic landscape design simulation. '
        'Modify it ONLY according to the client instruction below. Keep the camera angle, '
        'perspective, composition, lighting, buildings and every element NOT mentioned in '
        'the instruction completely unchanged. '
        'Client instruction (written in Traditional Chinese, quoted verbatim): '
        f'"{instruction}" '
        + _NO_TEXT_RULE)
    body = json.dumps({
        'contents': [{'parts': [
            {'text': prompt},
            {'inline_data': {'mime_type': prev_mime or 'image/jpeg',
                             'data': base64.b64encode(prev_bytes).decode('ascii')}},
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

def detect_work_tags(photo_bytes, mime_type):
    """以 Gemini 文字模型辨識作品照片，回傳詞彙表內的標籤清單（styles+elements 合併去重）。"""
    key = _gemini_api_key()
    if not key:
        raise RuntimeError('AI 服務尚未啟用（未設定 GEMINI_API_KEY）')
    styles_vocab   = list(AI_STYLE_TAGS.keys())
    elements_vocab = list(AI_ELEMENT_TAGS.keys())
    prompt = (
        '你是景觀設計作品的標籤助手。請觀察這張景觀照片，從下方「詞彙表」中挑選適用的標籤。\n'
        f'風格詞彙表（最多選 2 個）：{json.dumps(styles_vocab, ensure_ascii=False)}\n'
        f'元素詞彙表（最多選 6 個，必須是圖中實際可見的元素）：{json.dumps(elements_vocab, ensure_ascii=False)}\n'
        '規則：只能從詞彙表中選，標籤文字必須與詞彙表完全一致，不得自創；不確定的不要選。\n'
        '回傳 JSON 格式：{"styles": ["..."], "elements": ["..."]}'
    )
    body = json.dumps({
        'contents': [{'parts': [
            {'text': prompt},
            {'inline_data': {'mime_type': mime_type,
                             'data': base64.b64encode(photo_bytes).decode('ascii')}},
        ]}],
        'generationConfig': {'responseMimeType': 'application/json'},
    }).encode('utf-8')
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{GEMINI_DETECT_MODEL}:generateContent?key={key}')
    req = _u_req.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with _u_req.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    text = ''
    for cand in data.get('candidates', []):
        for part in cand.get('content', {}).get('parts', []):
            if part.get('text'):
                text += part['text']
    result = json.loads(text)
    if not isinstance(result, dict):
        raise RuntimeError('AI 回傳格式錯誤')
    # 過濾：只留詞彙表內的值，套用數量上限
    styles   = [s for s in result.get('styles', []) if s in AI_STYLE_TAGS][:2]
    elements = [e for e in result.get('elements', []) if e in AI_ELEMENT_TAGS][:6]
    tags = []
    for t in styles + elements:
        if t not in tags:
            tags.append(t)
    return tags

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

def upload_refined_to_drive(design_id, gen_bytes, gen_mime):
    """修圖專用：只上傳一張新生成圖到 Drive（原始照沿用 parent，不重傳），回傳 gen_id。"""
    svc = _drive()
    folder = svc.files().create(body={
        'name': f'aidsgn_{design_id}',
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [DRIVE_FOLDER_ID],
    }, fields='id').execute()
    wfid = folder['id']
    gen_ext = 'png' if 'png' in (gen_mime or '') else 'jpg'
    try:
        media = MediaIoBaseUpload(io.BytesIO(gen_bytes), mimetype=gen_mime or 'image/jpeg',
                                  resumable=False)
        cf = svc.files().create(body={'name': f'{design_id}_design.{gen_ext}', 'parents': [wfid]},
                                media_body=media, fields='id').execute()
        fid = cf['id']
        svc.permissions().create(fileId=fid, body={'type': 'anyone', 'role': 'reader'}).execute()
    except Exception:
        try:
            svc.files().delete(fileId=wfid).execute()  # 刪資料夾連同其中檔案
        except Exception:
            pass
        raise
    return fid

# ── Public routes ─────────────────────────────────────────────
@app.route('/')
def index():
    works = get_works('published')[:9]
    tags  = get_all_tags()
    rmap  = ratings_map(works)
    return render_template('public/index.html', works=works, tags=tags,
                           total=len(get_works('published')), rmap=rmap)

PING_TO_M2 = 3.305785  # 與 upload.html convertScale() 一致

def _parse_area(s):
    """面積輸入防呆：非數字/負數視為未填。"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None

@app.route('/works')
def works():
    all_w = get_works('published')
    tag   = request.args.get('tag', '').strip()
    mn    = request.args.get('min', '').strip()
    mx    = request.args.get('max', '').strip()
    a_mn  = request.args.get('area_min', '').strip()
    a_mx  = request.args.get('area_max', '').strip()
    a_unit = request.args.get('area_unit', 'ping').strip()
    if a_unit not in ('ping', 'm2'):
        a_unit = 'ping'
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
    # 面積篩選：scale 以坪儲存；m² 輸入先換算成坪再比較
    amin = _parse_area(a_mn)
    amax = _parse_area(a_mx)
    if a_unit == 'm2':
        if amin is not None: amin /= PING_TO_M2
        if amax is not None: amax /= PING_TO_M2
    if amin is not None or amax is not None:
        def _area_ok(w):
            s = _parse_area((w.scale or '').strip())
            if s is None:
                return False  # 無面積資料的作品在面積篩選時排除
            if amin is not None and s < amin:
                return False
            if amax is not None and s > amax:
                return False
            return True
        filtered = [w for w in filtered if _area_ok(w)]
    rmap = ratings_map(filtered)
    return render_template('public/works.html', works=filtered,
                           tags=get_all_tags(), active_tag=tag,
                           min_val=mn, max_val=mx,
                           area_min=a_mn, area_max=a_mx, area_unit=a_unit,
                           rmap=rmap)

@app.route('/work/<work_id>')
def work_detail(work_id):
    work = Work.query.filter_by(id=work_id, status='published').first()
    if not work:
        abort(404)
    _log_event('work_view', work_id=work_id, tags=work.tags or '')
    photos  = photo_urls(work.photo_ids, size='w1000')
    tags    = [t.strip() for t in (work.tags or '').split(',') if t.strip()]
    ratings = get_ratings(work_id)
    avg, cnt = avg_rating(work_id)
    similar = _similar_works(work)
    return render_template('public/work.html', work=work, photos=photos, tags=tags,
                           contact_phone=CONTACT_PHONE,
                           ratings=ratings, avg=avg, cnt=cnt,
                           similar=similar, srmap=ratings_map(similar))

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

def _work_tag_list(work):
    return [t.strip() for t in (work.tags or '').split(',') if t.strip()]

def _similar_works(work, limit=4):
    """相似作品推薦：標籤交集數×2 +（scale 相同 +1），分數 0 不列，取前 limit。"""
    base = set(_work_tag_list(work))
    scored = []
    for w in Work.query.filter(Work.status == 'published', Work.id != work.id).all():
        score = len(base & set(_work_tag_list(w))) * 2
        if (work.scale or '').strip() and (w.scale or '').strip() == (work.scale or '').strip():
            score += 1
        if score > 0:
            scored.append((score, w))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [w for _, w in scored[:limit]]

# ── AI 模擬設計（業主端，登入制） ──────────────────────────────

def _ai_design_rl_key(email):
    """生成與修圖共用的限流 key（超長 email 改用雜湊避免超出欄位）。"""
    rl_key = f'ai_design:{email}'
    if len(rl_key) > 60:
        rl_key = 'ai_design:' + hashlib.sha256(email.encode('utf-8')).hexdigest()[:40]
    return rl_key


# 生圖無上限白名單（逗號分隔 email，環境變數可覆寫；2026-06-13 森哥指定）
AI_UNLIMITED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get('AI_UNLIMITED_EMAILS', 'g2349311@gmail.com').split(',')
    if e.strip()}


def _ai_design_quota_ok(email):
    """生成/修圖配額：白名單無上限，其餘每位客戶每日 3 次（共用池）。"""
    if (email or '').strip().lower() in AI_UNLIMITED_EMAILS:
        return True
    return _rl_check(_ai_design_rl_key(email), max_calls=3, window_seconds=86400)

def _fetch_design_result_photo(design):
    """抓 design 既有模擬圖 bytes（修圖用）；任何失敗或 >10MB 回 None。"""
    fids = [f for f in (design.result_photo_id or '').split(',') if f.strip()]
    if not fids:
        return None
    try:
        req = _u_req.Request(_drive_thumb_url(fids[0].strip(), 'w1000'),
                             headers={'User-Agent': 'Mozilla/5.0'})
        with _u_req.urlopen(req, timeout=30) as resp:
            data = resp.read(_MAX_FILE_BYTES + 1)
        if not data or len(data) > _MAX_FILE_BYTES:
            return None
        return data
    except Exception:
        return None

def _fetch_work_ref_photo(work):
    """抓參照作品第一張照片 bytes；任何失敗或 >10MB 回 None（絕不讓生成失敗）。"""
    fids = [f for f in (work.photo_ids or '').split(',') if f.strip()]
    if not fids:
        return None
    try:
        req = _u_req.Request(_drive_thumb_url(fids[0].strip(), 'w1000'),
                             headers={'User-Agent': 'Mozilla/5.0'})
        with _u_req.urlopen(req, timeout=30) as resp:
            data = resp.read(_MAX_FILE_BYTES + 1)
        if not data or len(data) > _MAX_FILE_BYTES:
            return None
        return data
    except Exception:
        return None

_MASK_DATAURL_PREFIX = 'data:image/jpeg;base64,'
_MASK_MAX_DATAURL_LEN = 8 * 1024 * 1024  # base64 字串上限 8MB

def _decode_mask_overlay(raw):
    """解碼畫筆遮罩 dataURL（data:image/jpeg;base64,...）→ (bytes, mime)。

    前綴不符、超過 8MB、解碼失敗一律回 (None, None)：遮罩是選用功能，絕不擋生成。
    """
    raw = (raw or '').strip()
    if not raw.startswith(_MASK_DATAURL_PREFIX) or len(raw) > _MASK_MAX_DATAURL_LEN:
        return None, None
    try:
        data = base64.b64decode(raw[len(_MASK_DATAURL_PREFIX):], validate=True)
    except Exception:
        return None, None
    if not data:
        return None, None
    return data, 'image/jpeg'

def _ai_design_context(ref_work_id=None):
    """組 /ai-design 模板 context：標籤資料 + 側欄作品清單 + 參照作品（可選）。"""
    sidebar_works = []
    for w in (Work.query.filter_by(status='published')
              .order_by(Work.created_at.desc()).limit(24).all()):
        thumbs = photo_urls(w.photo_ids, size='w600')
        sidebar_works.append({'id': w.id, 'name': w.name,
                              'tags': _work_tag_list(w),
                              'thumb': thumbs[0] if thumbs else ''})
    ctx = dict(styles=list(AI_STYLE_TAGS.keys()),
               elements=list(AI_ELEMENT_TAGS.keys()),
               element_groups=AI_ELEMENT_GROUPS,
               element_conflicts={'groups': AI_ELEMENT_EXCLUSIVE_GROUPS,
                                  'pairs': AI_ELEMENT_CONFLICT_PAIRS},
               ai_ready=bool(_gemini_api_key()),
               sidebar_works=sidebar_works,
               ref_work=None, ref_style=None, ref_elements=[])
    # 客戶登入狀態：姓名自動帶入 + 未登入提示卡的 next 連結（含 ref 參數）
    cust = find_app_user(current_customer()) if current_customer() else None
    ctx['customer_name'] = (cust.name or '') if cust else ''
    nxt = request.full_path if request.method == 'GET' else url_for('ai_design')
    ctx['login_next'] = (nxt or url_for('ai_design')).rstrip('?')
    if ref_work_id:
        w = Work.query.filter_by(id=ref_work_id, status='published').first()
        if w:  # 非 published 一律忽略
            tags = _work_tag_list(w)
            thumbs = photo_urls(w.photo_ids, size='w600')
            ctx['ref_work'] = {'id': w.id, 'name': w.name, 'tags': tags,
                               'thumb': thumbs[0] if thumbs else ''}
            # 預選風格：work.tags 中第一個命中 AI_STYLE_TAGS 的
            ctx['ref_style'] = next((t for t in tags if t in AI_STYLE_TAGS), None)
            # 預勾元素：命中 AI_ELEMENT_TAGS，依表單順序過互斥防呆，上限 6
            hits = {t for t in tags if t in AI_ELEMENT_TAGS}
            form_order = [k for _, ks in AI_ELEMENT_GROUPS for k in ks]
            ctx['ref_elements'] = _filter_conflicting_elements(
                [k for k in form_order if k in hits])[:6]
    return ctx

@app.route('/ai-design', methods=['GET', 'POST'])
def ai_design():
    if request.method == 'POST':
        # 生成需要客戶登入（行銷展示頁 GET 仍開放）
        cust_email = current_customer()
        if not cust_email:
            nxt = url_for('ai_design')
            ref_id = request.form.get('ref_work_id', '').strip()
            if ref_id:
                nxt += '?ref=' + _u_parse.quote(ref_id)
            flash('生成專屬模擬圖需要先登入（免費註冊）', 'error')
            return redirect(url_for('account_login', next=nxt))
        cust = find_app_user(cust_email)

        ctx = _ai_design_context()
        # Rate limit: 每位登入客戶每日 3 次（生成與修圖共用同一池；白名單無上限）
        if not _ai_design_quota_ok(cust_email):
            flash('今日 AI 設計生成次數已達上限（每日 3 次），歡迎來電洽詢 ' + CONTACT_PHONE, 'error')
            return render_template('public/ai_design.html', **ctx)

        style    = request.form.get('style', '').strip()
        # 防呆：互斥組合保留先選的、丟棄後選的（依表單順序），再套用 6 個上限
        chosen   = _filter_conflicting_elements(
            [e for e in request.form.getlist('elements') if e in AI_ELEMENT_TAGS])[:6]
        name     = request.form.get('name', '').strip()[:120]
        if not name and cust:
            name = (cust.name or '').strip()[:120]  # 表單未填時帶 AppUser 姓名
        phone    = request.form.get('phone', '').strip()[:20]
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

        # 參照作品（可選）：抓不到圖就放棄參照繼續生成，絕不讓整個生成失敗
        ref_bytes = ref_mime = None
        ref_used_id = None
        ref_form_id = request.form.get('ref_work_id', '').strip()
        if ref_form_id:
            ref_w = Work.query.filter_by(id=ref_form_id, status='published').first()
            if ref_w:
                ref_bytes = _fetch_work_ref_photo(ref_w)
                if ref_bytes:
                    ref_mime = 'image/jpeg'
                    ref_used_id = ref_w.id

        # 畫筆遮罩（可選）：解碼失敗就當沒有，絕不擋生成
        mask_bytes, mask_mime = _decode_mask_overlay(request.form.get('mask_overlay', ''))

        try:
            gen_bytes, gen_mime = generate_design_image(src_bytes, src_mime, style, chosen,
                                                        ref_bytes=ref_bytes, ref_mime=ref_mime,
                                                        mask_bytes=mask_bytes, mask_mime=mask_mime)
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
            owner_email=cust_email,
            style=style,
            # ref:<work_id> 以標籤形式記錄（不加新欄位）；結果頁顯示時過濾
            tags=','.join(chosen + ([f'ref:{ref_used_id}'] if ref_used_id else [])),
            source_photo_id=src_id,
            result_photo_id=gen_id,
            created_at=datetime.utcnow(),
        )
        db.session.add(design)
        db.session.commit()
        # 行為事件：AI 生成成功（tags 含風格 + 元素）
        _log_event('ai_generate', work_id=ref_used_id,
                   tags=','.join([style] + chosen), user_email=cust_email)
        return redirect(url_for('ai_design_result', design_id=design_id))
    return render_template('public/ai_design.html',
                           **_ai_design_context(request.args.get('ref', '').strip()))

@app.route('/ai-design/<design_id>')
def ai_design_result(design_id):
    design = AiDesign.query.filter_by(id=design_id).first()
    if not design:
        abort(404)
    src_url = photo_urls(design.source_photo_id, size='w1000')[0]
    gen_url = photo_urls(design.result_photo_id, size='w1000')[0]
    # ref:<work_id>／refine:<design_id> 是內部記錄用標籤，不對外顯示
    tags = [t for t in (design.tags or '').split(',')
            if t and not t.startswith('ref:') and not t.startswith('refine:')]
    # 參照作品而生成的，把參照作品照一併呈現（作品已刪除或無照片則略過）
    ref_work = None
    ref_id = next((t[4:] for t in (design.tags or '').split(',')
                   if t.startswith('ref:')), None)
    if ref_id:
        w = Work.query.filter_by(id=ref_id).first()
        if w and (w.photo_ids or '').strip():
            ref_work = {'id': w.id, 'name': w.name,
                        'photo': photo_urls(w.photo_ids, size='w1000')[0]}
    # 只有 design 擁有者（登入客戶）能用文字迭代修圖；舊記錄 owner_email 為空者不開放
    is_owner = bool(design.owner_email) and current_customer() == design.owner_email
    return render_template('public/ai_design_result.html', design=design,
                           src_url=src_url, gen_url=gen_url, tags=tags,
                           ref_work=ref_work, is_owner=is_owner,
                           contact_phone=CONTACT_PHONE)

@app.route('/ai-design/<design_id>/refine', methods=['POST'])
@customer_required
def ai_design_refine(design_id):
    """文字迭代修圖：以既有模擬圖為底，依客戶文字指示生成新的一張（owner 限定）。"""
    design = AiDesign.query.filter_by(id=design_id).first()
    if not design:
        abort(404)
    cust_email = current_customer()
    if not design.owner_email or design.owner_email != cust_email:
        abort(403)
    result_url = url_for('ai_design_result', design_id=design_id)

    instruction = (request.form.get('instruction') or '').strip()
    if not instruction or len(instruction) > 300:
        flash('請用 1–300 字描述想修改的地方', 'error')
        return redirect(result_url)

    # 與生成共用每日 3 次配額（一次修改 = 一次配額；白名單無上限）
    if not _ai_design_quota_ok(cust_email):
        flash('今日 AI 設計生成次數已達上限（每日 3 次），歡迎來電洽詢 ' + CONTACT_PHONE, 'error')
        return redirect(result_url)

    prev_bytes = _fetch_design_result_photo(design)
    if not prev_bytes:
        flash('暫時無法讀取這張模擬圖，請稍後再試', 'error')
        return redirect(result_url)

    try:
        gen_bytes, gen_mime = refine_design_image(prev_bytes, 'image/jpeg', instruction)
    except Exception as e:
        flash(f'AI 修改失敗：{e}', 'error')
        return redirect(result_url)

    new_id = str(uuid.uuid4()).replace('-', '')[:12]
    try:
        gen_id = upload_refined_to_drive(new_id, gen_bytes, gen_mime)
    except Exception as e:
        flash(f'圖片儲存失敗：{e}', 'error')
        return redirect(result_url)

    # tags 承襲 parent（過濾掉舊 refine:），再記錄 refine:<parent_id>（顯示時過濾）
    base_tags = [t for t in (design.tags or '').split(',')
                 if t and not t.startswith('refine:')]
    new_design = AiDesign(
        id=new_id,
        owner_name=design.owner_name,
        owner_phone=design.owner_phone,
        owner_email=design.owner_email,
        style=design.style,
        tags=','.join(base_tags + [f'refine:{design.id}']),
        source_photo_id=design.source_photo_id,  # 原始照沿用 parent，不重傳
        result_photo_id=gen_id,
        created_at=datetime.utcnow(),
    )
    db.session.add(new_design)
    db.session.commit()
    _log_event('ai_refine', tags=design.style or '', user_email=cust_email)
    flash('已依您的描述產生新的模擬圖', 'success')
    return redirect(url_for('ai_design_result', design_id=new_id))

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
        _log_event('contact_submit', work_id=work_id or None)
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
                existing.last_login = datetime.utcnow()
                db.session.commit()
                session['email'] = email
                session['name'] = name
                flash(f'歡迎回來，{name}！帳號已重新啟用', 'success')
                return redirect(url_for('my_profile'))
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
            created_at=datetime.utcnow(),
            last_login=datetime.utcnow()
        )
        db.session.add(member)
        db.session.commit()

        session['email'] = email
        session['name']  = name
        flash(f'歡迎，{name}！帳號建立完成。若要上傳作品，請先申請成為同行夥伴', 'success')
        return redirect(url_for('my_profile'))
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
                m.last_login = datetime.utcnow()
                db.session.commit()  # persist last_login + any hash upgrade
                session['email'] = email
                session['name']  = m.name or ''
                nxt = request.form.get('next', '')
                # 管理員 → 後台；已核准同行 → 上傳作品；其餘 → 公司資料頁（可申請身分）
                if email == ADMIN_EMAIL:
                    default = url_for('admin_dashboard')
                elif partner_status(email) == 'approved':
                    default = url_for('upload')
                else:
                    default = url_for('my_profile')
                return _safe_redirect(nxt, default)
        flash('帳號或密碼錯誤', 'error')
    return render_template('auth/login.html', next=request.args.get('next',''))

@app.route('/logout')
def logout():
    # 單一登入接橋後，登出一律清除全部身分，避免被接橋自動補回
    _clear_identity_session()
    return redirect(url_for('index'))

# ── 客戶帳號（/account，與夥伴帳號完全分離） ────────────────────
@app.route('/account/register', methods=['GET', 'POST'])
def account_register():
    nxt = request.values.get('next', '')
    ctx = dict(next=nxt, google_login_uri=_google_login_uri(nxt))
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '').strip()
        if not all([name, email, pw]):
            flash('請填寫所有欄位', 'error')
            return render_template('account/register.html', **ctx)
        if len(pw) < 8:
            flash('密碼至少 8 個字元', 'error')
            return render_template('account/register.html', **ctx)
        ok, err = _check_field_lengths(姓名=(name, 120), Email=(email, 120))
        if not ok:
            flash(err, 'error')
            return render_template('account/register.html', **ctx)
        if not _verify_recaptcha(request.form.get('g-recaptcha-response', '')):
            flash('請完成「我不是機器人」驗證後再送出', 'error')
            return render_template('account/register.html', **ctx)
        if find_app_user(email):
            flash('此 Email 已註冊，請直接登入', 'error')
            return render_template('account/register.html', **ctx)
        user = AppUser(
            id=_new_id(),
            email=email,
            name=name,
            password_hash=_hash(pw),
            created_at=datetime.utcnow(),
            last_login=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.commit()
        session['customer_email'] = email
        session['customer_name']  = name
        flash(f'歡迎，{name}！帳號建立完成', 'success')
        return _safe_redirect(nxt, url_for('ai_design'))
    return render_template('account/register.html', **ctx)

@app.route('/account/login', methods=['GET', 'POST'])
def account_login():
    nxt = request.values.get('next', '')
    ctx = dict(next=nxt, google_login_uri=_google_login_uri(nxt))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '').strip()
        u = find_app_user(email)
        if u and u.password_hash:
            ok, _needs_upgrade = _verify_password(pw, u.password_hash)
            if ok:
                u.last_login = datetime.utcnow()
                db.session.commit()
                session['customer_email'] = u.email
                session['customer_name']  = u.name or ''
                return _safe_redirect(nxt, url_for('ai_design'))
        if u and not u.password_hash and u.google_sub:
            flash('此帳號是以 Google 註冊的，請點下方「使用 Google 帳號繼續」', 'error')
        else:
            flash('Email 或密碼錯誤', 'error')
    return render_template('account/login.html', **ctx)

@app.route('/account/logout')
def account_logout():
    # 單一登入接橋後，登出一律清除全部身分，避免被接橋自動補回
    _clear_identity_session()
    return redirect(url_for('index'))

@app.route('/account/google', methods=['POST'])
@csrf.exempt
def account_google():
    """GIS 官方按鈕 callback：驗證 ID token 後建號 / 綁定 / 登入。

    CSRF：GIS POST 不帶我們的 token，改驗證 GIS 內建的 g_csrf_token
    double-submit cookie（cookie 與表單值必須一致）。
    """
    if not GOOGLE_OAUTH_CLIENT_ID:
        abort(404)
    nxt = request.args.get('next', '')
    cookie_token = request.cookies.get('g_csrf_token', '')
    body_token   = request.form.get('g_csrf_token', '')
    if not cookie_token or cookie_token != body_token:
        flash('Google 登入驗證失敗，請再試一次', 'error')
        return redirect(url_for('account_login', next=nxt))
    credential = request.form.get('credential', '').strip()
    if not credential:
        flash('Google 登入失敗：未收到憑證', 'error')
        return redirect(url_for('account_login', next=nxt))
    try:
        info = _google_verify_id_token(credential)
    except Exception:
        flash('Google 登入驗證失敗，請稍後再試', 'error')
        return redirect(url_for('account_login', next=nxt))
    aud   = info.get('aud', '')
    sub   = (info.get('sub') or '').strip()
    email = (info.get('email') or '').strip().lower()
    email_verified = str(info.get('email_verified', '')).lower() == 'true'
    if aud != GOOGLE_OAUTH_CLIENT_ID or not sub or not email or not email_verified:
        flash('Google 登入驗證失敗（憑證無效）', 'error')
        return redirect(url_for('account_login', next=nxt))
    gname = (info.get('name') or '').strip()

    user = AppUser.query.filter_by(google_sub=sub).first()
    if not user:
        user = find_app_user(email)
        if user:
            # 既有密碼帳號：綁定 google_sub，之後兩種方式皆可登入
            user.google_sub = sub
            if not (user.name or '').strip() and gname:
                user.name = gname
        else:
            user = AppUser(
                id=_new_id(),
                email=email,
                name=gname,
                password_hash=None,   # Google-only 帳號
                google_sub=sub,
                created_at=datetime.utcnow(),
            )
            db.session.add(user)
        db.session.commit()
    user.last_login = datetime.utcnow()
    db.session.commit()
    session['customer_email'] = user.email
    session['customer_name']  = user.name or ''
    flash(f'歡迎，{user.name or user.email}！', 'success')
    return _safe_redirect(nxt, url_for('ai_design'))

@app.route('/my-designs')
@customer_required
def my_designs():
    designs = (AiDesign.query.filter_by(owner_email=current_customer())
               .order_by(AiDesign.created_at.desc()).all())
    return render_template('account/my_designs.html', designs=designs)

# ── Member ────────────────────────────────────────────────────
@app.route('/upload', methods=['GET', 'POST'])
@partner_required
def upload():
    if request.method == 'POST':
        name   = request.form.get('name','').strip()
        scale  = request.form.get('scale','').strip()
        scale_unit = request.form.get('scale_unit','ping').strip()
        tags   = request.form.get('tags','').strip()
        price  = request.form.get('price','').strip()
        photos = request.files.getlist('photos')
        valid  = [f for f in photos if f and f.filename]
        if not name:
            flash('請填寫作品名稱', 'error')
            return render_template('member/upload.html')
        # 面積可用坪或 m² 輸入，一律換算成坪儲存（/works 篩選與相似推薦皆以坪解讀）
        if scale_unit not in ('ping', 'm2'):
            scale_unit = 'ping'
        try:
            _sv = float(scale)
        except ValueError:
            _sv = None
        if _sv is None or not math.isfinite(_sv) or _sv <= 0:
            flash('請填寫有效的案場規模（大於 0 的數字）', 'error')
            return render_template('member/upload.html')
        if scale_unit == 'm2':
            _sv /= PING_TO_M2
        scale = f'{_sv:.2f}'.rstrip('0').rstrip('.')
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

@app.route('/api/detect-tags', methods=['POST'])
@partner_required
def api_detect_tags():
    """作品照片 AI 偵測標籤（同行限定，屬上傳作品流程）。任何 Gemini 錯誤回 200 + 空標籤，不回 500。"""
    # 限流：同 IP 每日 30 次
    if not _rl_check('detect_tags', max_calls=30, window_seconds=86400):
        return {'tags': [], 'error': '今日 AI 偵測次數已達上限（每日 30 次）'}
    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return {'tags': [], 'error': '請先選擇一張照片'}, 400
    ext = os.path.splitext(photo.filename)[1].lstrip('.').lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return {'tags': [], 'error': f'不支援的檔案格式「.{ext}」，僅接受：{", ".join(sorted(_ALLOWED_EXTENSIONS))}'}, 400
    data = photo.stream.read()
    if len(data) > _MAX_FILE_BYTES:
        return {'tags': [], 'error': '照片超過 10 MB 上限'}, 400
    mime = photo.content_type or f'image/{"jpeg" if ext == "jpg" else ext}'
    try:
        tags = detect_work_tags(data, mime)
    except Exception:
        return {'tags': [], 'error': '偵測服務暫時無法使用'}
    return {'tags': tags}

@app.route('/api/track', methods=['POST'])
def api_track():
    """前端輕量頁面瀏覽追蹤；任何狀況都回 204，永不影響使用者頁面。"""
    try:
        # 每 IP 每小時上限，防灌量（page_view 已在前端去重，正常用量極低）
        if not _rl_check('track', max_calls=600, window_seconds=3600):
            return ('', 204)
        data = request.get_json(silent=True) or {}
        page = (data.get('page') or '').strip()[:120]
        if page:
            _log_event('page_view', tags=page)
    except Exception:
        pass
    return ('', 204)

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

# ── 同行資格申請 ────────────────────────────────────────────────
@app.route('/apply/partner', methods=['GET', 'POST'])
@login_required
def apply_partner():
    """申請成為同行夥伴：送出後 status='pending' 待管理員審核；
    rejected 可修改資料後重新申請；approved / pending 不重複收件。"""
    profile = PartnerProfile.query.filter_by(email=current_user()).first()
    status  = (profile.status or 'none') if profile else 'none'

    if request.method == 'POST':
        if status == 'approved':
            flash('您已是同行夥伴，無需再次申請', 'success')
            return redirect(url_for('my_profile'))
        if status == 'pending':
            flash('您的申請正在審核中，請耐心等候', 'error')
            return redirect(url_for('apply_partner'))

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
            return redirect(url_for('apply_partner'))

        now = datetime.utcnow()
        if profile:
            profile.county = county
            profile.service_areas = service
            profile.min_amount = min_amt
            profile.intro = intro
            profile.line_id = line_id
            profile.website = website
            profile.updated_at = now
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
                updated_at=now,
            )
            db.session.add(profile)
        profile.status = 'pending'
        profile.applied_at = now
        profile.reviewed_at = None
        db.session.commit()

        flash('申請已送出！管理員審核通過後，即可上傳與管理作品', 'success')
        return redirect(url_for('apply_partner'))

    member = find_member(current_user()) or {}
    return render_template('member/apply_partner.html',
                           profile=profile or {}, member=member, status=status)

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

    # ── 身分申請審核（同行 + 供應商） ──────────────────────────
    partner_apps  = (PartnerProfile.query.filter_by(status='pending')
                     .order_by(PartnerProfile.applied_at.asc()).all())
    supplier_apps = (Supplier.query.filter_by(status='pending')
                     .order_by(Supplier.created_at.asc()).all())
    _app_emails = [p.email for p in partner_apps]
    app_members = ({m.email: m for m in Member.query.filter(Member.email.in_(_app_emails)).all()}
                   if _app_emails else {})

    # ── AI 生成記錄（最近 50 筆）──────────────────────────────
    ai_designs = AiDesign.query.order_by(AiDesign.created_at.desc()).limit(50).all()
    emails = {d.owner_email for d in ai_designs if d.owner_email}
    users_by_email = ({u.email: u for u in AppUser.query.filter(AppUser.email.in_(emails)).all()}
                      if emails else {})
    ref_ids = set()
    for d in ai_designs:
        for t in (d.tags or '').split(','):
            if t.startswith('ref:') and t[4:]:
                ref_ids.add(t[4:])
    ref_names = ({w.id: w.name for w in Work.query.filter(Work.id.in_(ref_ids)).all()}
                 if ref_ids else {})
    ai_rows = []
    for d in ai_designs:
        d_tags = (d.tags or '').split(',')
        rid = next((t[4:] for t in d_tags if t.startswith('ref:')), None)
        # refine:<design_id>：此圖由哪張模擬圖修改而來（顯示為「修改自 #id」）
        refine_id = next((t[7:] for t in d_tags if t.startswith('refine:') and t[7:]), None)
        u = users_by_email.get(d.owner_email)
        ai_rows.append({
            'd': d,
            'account_name': (u.name or '') if u else '',
            'tags': [t for t in d_tags
                     if t and not t.startswith('ref:') and not t.startswith('refine:')],
            'ref_id': rid,
            'ref_name': ref_names.get(rid) if rid else None,
            'refine_id': refine_id,
        })

    # ── 近 30 天 work_view Top 10 ─────────────────────────────
    top_views = []
    try:
        since = datetime.utcnow() - timedelta(days=30)
        rows = (db.session.query(UserEvent.work_id, func.count(UserEvent.id))
                .filter(UserEvent.etype == 'work_view',
                        UserEvent.created_at >= since,
                        UserEvent.work_id.isnot(None))
                .group_by(UserEvent.work_id)
                .order_by(func.count(UserEvent.id).desc())
                .limit(10).all())
        wnames = ({w.id: w.name for w in Work.query.filter(
                       Work.id.in_([r[0] for r in rows])).all()}
                  if rows else {})
        top_views = [{'work_id': wid, 'count': cnt, 'name': wnames.get(wid, wid)}
                     for wid, cnt in rows]
    except Exception:
        db.session.rollback()  # 統計失敗不影響後台其他區塊

    # ── 註冊客戶與使用數據 ────────────────────────────────────
    customers = []
    try:
        gen_counts = dict(db.session.query(AiDesign.owner_email, func.count(AiDesign.id))
                          .filter(AiDesign.owner_email != '')
                          .group_by(AiDesign.owner_email).all())
        view_counts = dict(db.session.query(UserEvent.user_email, func.count(UserEvent.id))
                           .filter(UserEvent.etype == 'work_view',
                                   UserEvent.user_email.isnot(None))
                           .group_by(UserEvent.user_email).all())
        last_seen = dict(db.session.query(UserEvent.user_email,
                                          func.max(UserEvent.created_at))
                         .filter(UserEvent.user_email.isnot(None))
                         .group_by(UserEvent.user_email).all())
        for u in AppUser.query.order_by(AppUser.created_at.desc()).all():
            customers.append({
                'u': u,
                'via_google': bool(u.google_sub),
                'gen_count': gen_counts.get(u.email, 0),
                'view_count': view_counts.get(u.email, 0),
                'last_seen': last_seen.get(u.email),
            })
    except Exception:
        db.session.rollback()

    return render_template('admin/dashboard.html',
                           pending=pending, published=published,
                           rejected=rejected, contacts=contacts,
                           members=members,
                           mats_pending=mats_pending,
                           mats_active=mats_active,
                           partner_apps=partner_apps,
                           supplier_apps=supplier_apps,
                           app_members=app_members,
                           ai_rows=ai_rows, top_views=top_views,
                           customers=customers)

@app.route('/admin/analytics')
@login_required
@admin_required
def admin_analytics():
    """後台數據分析（近 30 天）。所有查詢唯讀且各自 try/except，失敗只回空、不影響頁面。"""
    since = datetime.utcnow() - timedelta(days=30)

    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            db.session.rollback()
            return default

    # 每日流量：page_view 次數 + 不重複訪客（distinct session_key）
    def _daily():
        rows = (db.session.query(func.date(UserEvent.created_at),
                                 func.count(UserEvent.id),
                                 func.count(func.distinct(UserEvent.session_key)))
                .filter(UserEvent.etype == 'page_view', UserEvent.created_at >= since)
                .group_by(func.date(UserEvent.created_at))
                .order_by(func.date(UserEvent.created_at)).all())
        return [{'date': str(d), 'views': v, 'visitors': u} for d, v, u in rows]
    daily = _safe(_daily, [])
    traffic_max = max([d['views'] for d in daily], default=0) or 1

    # 各頁面瀏覽次數 Top
    def _pages():
        rows = (db.session.query(UserEvent.tags, func.count(UserEvent.id))
                .filter(UserEvent.etype == 'page_view', UserEvent.created_at >= since)
                .group_by(UserEvent.tags)
                .order_by(func.count(UserEvent.id).desc()).limit(15).all())
        return [{'page': p or '(未知)', 'count': c} for p, c in rows]
    pages = _safe(_pages, [])

    # 近 30 天總覽
    def _totals():
        def cnt(etype):
            return (db.session.query(func.count(UserEvent.id))
                    .filter(UserEvent.etype == etype, UserEvent.created_at >= since)
                    .scalar()) or 0
        uniq = (db.session.query(func.count(func.distinct(UserEvent.session_key)))
                .filter(UserEvent.created_at >= since).scalar()) or 0
        return {'visitors': uniq, 'page_view': cnt('page_view'),
                'work_view': cnt('work_view'), 'ai_generate': cnt('ai_generate'),
                'ai_refine': cnt('ai_refine'), 'contact': cnt('contact_submit'),
                'investor': cnt('investor_inquiry_submit')}
    totals = _safe(_totals, {})

    # 作品瀏覽 Top10（近 30 天）
    def _top_works():
        rows = (db.session.query(UserEvent.work_id, func.count(UserEvent.id))
                .filter(UserEvent.etype == 'work_view', UserEvent.created_at >= since,
                        UserEvent.work_id.isnot(None))
                .group_by(UserEvent.work_id)
                .order_by(func.count(UserEvent.id).desc()).limit(10).all())
        names = ({w.id: w.name for w in Work.query.filter(
                     Work.id.in_([r[0] for r in rows])).all()} if rows else {})
        return [{'name': names.get(wid, wid), 'count': c} for wid, c in rows]
    top_works = _safe(_top_works, [])

    # 夥伴成效：每位夥伴旗下作品的 work_view 加總 Top
    def _partner():
        rows = (db.session.query(Work.uploader_email, func.count(UserEvent.id))
                .join(UserEvent, UserEvent.work_id == Work.id)
                .filter(UserEvent.etype == 'work_view', UserEvent.created_at >= since)
                .group_by(Work.uploader_email)
                .order_by(func.count(UserEvent.id).desc()).limit(10).all())
        return [{'email': e, 'count': c} for e, c in rows]
    partner = _safe(_partner, [])

    # 累計成長（總量）
    def _growth():
        out = {}
        for label, model in (('夥伴', Member), ('作品', Work),
                             ('客戶', AppUser), ('供應商', Supplier)):
            out[label] = (db.session.query(func.count(model.id)).scalar()) or 0
        return out
    growth = _safe(_growth, {})

    return render_template('admin/analytics.html', since=since, daily=daily,
                           traffic_max=traffic_max, pages=pages, totals=totals,
                           top_works=top_works, partner=partner, growth=growth)

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
            # 換封面：把選中的照片排到第一張（封面 = photo_ids 第一個）
            cover_id = request.form.get('cover_id', '').strip()
            if cover_id:
                _ids = [x for x in (work.photo_ids or '').split(',') if x.strip()]
                if cover_id in _ids:
                    work.photo_ids = ','.join([cover_id] + [x for x in _ids if x != cover_id])
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

    _ids   = [x for x in (work.photo_ids or '').split(',') if x.strip()]
    photos = list(zip(_ids, photo_urls(work.photo_ids, size='w800')))  # (id, url)
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
        _log_event('investor_inquiry_submit')
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
                # 注意：不改動 status——若管理員已核准（active）可直接登入，
                # 否則維持原審核狀態，避免重辦變成繞過審核的後門
                existing.password_hash = _hash(pw)
                existing.legacy_pw_hash = None
                existing.company = company
                existing.contact_name = contact
                existing.phone = phone
                if existing.status == 'active':
                    existing.last_login = datetime.utcnow()
                    db.session.commit()
                    session['supplier_email']   = email
                    session['supplier_company'] = company
                    flash(f'歡迎回來，{company}！帳號已重新啟用', 'success')
                    return redirect(url_for('supplier_upload'))
                db.session.commit()
                flash('帳號已重新啟用，待管理員審核通過後即可登入上傳素材', 'success')
                return redirect(url_for('supplier_login'))
            flash('此 Email 已註冊', 'error')
            return render_template('supplier/register.html')

        supplier = Supplier(
            id=str(uuid.uuid4())[:8],
            email=email,
            password_hash=_hash(pw),
            company=company,
            contact_name=contact,
            phone=phone,
            status='pending',  # 新註冊一律待審核，管理員核准後才可登入
            created_at=datetime.utcnow()
        )
        db.session.add(supplier)
        db.session.commit()

        flash('申請已送出！管理員審核通過後，即可登入上傳產品素材', 'success')
        return redirect(url_for('supplier_login'))
    return render_template('supplier/register.html')

@app.route('/supplier/login', methods=['GET','POST'])
def supplier_login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        pw    = request.form.get('password','').strip()
        s     = find_supplier(email)
        if s:
            ok, _upgraded = _verify_password_for_user(pw, s)
            if ok and s.status == 'active':
                s.last_login = datetime.utcnow()
                db.session.commit()  # persist last_login + any hash upgrade
                session['supplier_email']   = email
                session['supplier_company'] = s.company or ''
                nxt = request.form.get('next', '')
                return _safe_redirect(nxt, url_for('supplier_upload'))
            if ok:
                db.session.commit()  # 保留密碼升級
                if s.status == 'pending':
                    flash('您的供應商申請仍在審核中，通過後即可登入', 'error')
                else:
                    flash('此帳號未通過審核或已停用，如有疑問請聯絡管理員', 'error')
                return render_template('supplier/login.html', next=request.args.get('next',''))
        flash('帳號或密碼錯誤', 'error')
    return render_template('supplier/login.html', next=request.args.get('next',''))

@app.route('/supplier/logout')
def supplier_logout():
    # 單一登入接橋後，登出一律清除全部身分，避免被接橋自動補回
    _clear_identity_session()
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

# ── Admin 身分申請審核 ─────────────────────────────────────────
@app.route('/admin/application/partner/<pid>', methods=['POST'])
@login_required
@admin_required
def admin_partner_application(pid):
    """同行申請審核：核准 → approved；拒絕 → rejected（可重新申請）。"""
    p = PartnerProfile.query.filter_by(id=pid).first()
    if not p:
        abort(404)
    action = request.form.get('action')
    if action == 'approve':
        p.status = 'approved'
        flash(f'已核准 {p.email} 成為同行夥伴', 'success')
    elif action == 'reject':
        p.status = 'rejected'
        flash(f'已拒絕 {p.email} 的同行申請', 'success')
    else:
        abort(400)
    p.reviewed_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/application/supplier/<sid>', methods=['POST'])
@login_required
@admin_required
def admin_supplier_application(sid):
    """供應商申請審核：核准 → active（可登入）；拒絕 → rejected。"""
    s = Supplier.query.filter_by(id=sid).first()
    if not s:
        abort(404)
    action = request.form.get('action')
    if action == 'approve':
        s.status = 'active'
        flash(f'已核准 {s.company or s.email} 的供應商申請', 'success')
    elif action == 'reject':
        s.status = 'rejected'
        flash(f'已拒絕 {s.company or s.email} 的供應商申請', 'success')
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

# ── Admin 帳號總覽與直接授權 ───────────────────────────────────
@app.route('/admin/accounts')
@login_required
@admin_required
def admin_accounts():
    """三套帳號總覽（同行會員 / 客戶 / 供應商），附身分狀態與直接授權操作。"""
    members   = Member.query.order_by(Member.created_at.desc()).all()
    app_users = AppUser.query.order_by(AppUser.created_at.desc()).all()
    suppliers = Supplier.query.order_by(Supplier.created_at.desc()).all()
    # 一次撈齊狀態對照表，避免逐列查詢
    pstatus = {p.email: (p.status or 'none') for p in PartnerProfile.query.all()}
    sstatus = {s.email: (s.status or 'none') for s in suppliers}
    return render_template('admin/accounts.html',
                           members=members, app_users=app_users,
                           suppliers=suppliers, pstatus=pstatus, sstatus=sstatus)

@app.route('/admin/account/partner', methods=['POST'])
@login_required
@admin_required
def admin_account_partner():
    """直接授權/撤銷同行身分（不經申請流程）。"""
    email  = (request.form.get('email') or '').strip().lower()
    action = request.form.get('action')
    if not email:
        abort(400)
    p = PartnerProfile.query.filter_by(email=email).first()
    now = datetime.utcnow()
    if action == 'grant':
        if not p:
            p = PartnerProfile(id=str(uuid.uuid4())[:8], email=email,
                               applied_at=now, updated_at=now)
            db.session.add(p)
        p.status = 'approved'
        p.reviewed_at = now
        flash(f'已將 {email} 設為同行夥伴', 'success')
    elif action == 'revoke':
        if not p:
            flash(f'{email} 沒有同行資料，無需撤銷', 'error')
            return redirect(url_for('admin_accounts'))
        p.status = 'rejected'
        p.reviewed_at = now
        flash(f'已撤銷 {email} 的同行資格', 'success')
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for('admin_accounts'))

@app.route('/admin/account/supplier', methods=['POST'])
@login_required
@admin_required
def admin_account_supplier():
    """直接授權/撤銷供應商身分。
    該 email 尚無供應商帳號時，以會員/客戶資料建立：
    - 有密碼（members / app_users 皆為 werkzeug 相容格式）→ 直接沿用，同一組密碼即可登入
    - 無密碼（Google 註冊客戶）→ 標記 NEEDS_REGISTER，
      該用戶到「供應商註冊」用原 Email 重新設定密碼即可接回（沿用既有待重辦機制）"""
    email  = (request.form.get('email') or '').strip().lower()
    action = request.form.get('action')
    if not email:
        abort(400)
    s = find_supplier(email)
    now = datetime.utcnow()
    if action == 'grant':
        if s:
            s.status = 'active'
            flash(f'已核准 {email} 的供應商資格', 'success')
        else:
            m = find_member(email)
            u = find_app_user(email)
            if not m and not u:
                flash(f'{email} 不在任何帳號表中，無法建立供應商帳號', 'error')
                return redirect(url_for('admin_accounts'))
            src_hash = (m.password_hash if m else None) or (u.password_hash if u else None)
            s = Supplier(
                id=str(uuid.uuid4())[:8],
                email=email,
                password_hash=src_hash or 'NEEDS_REGISTER',
                company=((m.company if m else '') or (u.name if u else '') or ''),
                contact_name=((m.name if m else '') or (u.name if u else '') or ''),
                phone=((m.phone if m else '') or ''),
                status='active',
                created_at=now,
            )
            db.session.add(s)
            if src_hash:
                flash(f'已建立並核准 {email} 的供應商帳號（沿用原帳號密碼登入）', 'success')
            else:
                flash(f'已建立並核准 {email} 的供應商帳號；該帳號原以 Google 登入無密碼，'
                      f'請通知對方到「供應商註冊」用原 Email 重新設定密碼即可啟用', 'success')
    elif action == 'revoke':
        if not s:
            flash(f'{email} 沒有供應商帳號，無需撤銷', 'error')
            return redirect(url_for('admin_accounts'))
        s.status = 'pending'
        flash(f'已撤銷 {email} 的供應商資格（轉為待審核）', 'success')
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for('admin_accounts'))

@app.errorhandler(403)
def e403(e):
    return render_template('error.html', code=403, msg='沒有權限'), 403

@app.errorhandler(404)
def e404(e):
    return render_template('error.html', code=404, msg='頁面不存在'), 404

# ── 開機自跑遷移器 ──────────────────────────────────────────────
# 對應 migrations/2026-07-04_account_roles.sql，語句逐字取自該檔（去除 BEGIN/COMMIT，
# 交易改由程式控制）。修改時請與 .sql 檔保持同步。
_MIGRATION_2026_07_04_NAME = '2026-07-04_account_roles'
_MIGRATION_2026_07_04_STMTS = [
    # ── A-1. partner_profiles：審核狀態欄位 ─────────────────────
    """ALTER TABLE partner_profiles ADD COLUMN IF NOT EXISTS status      VARCHAR(20) DEFAULT 'none';""",
    """ALTER TABLE partner_profiles ADD COLUMN IF NOT EXISTS applied_at  TIMESTAMP;""",
    """ALTER TABLE partner_profiles ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;""",
    # ── A-2. 三套帳號表：last_login ─────────────────────────────
    """ALTER TABLE members   ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;""",
    """ALTER TABLE app_users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;""",
    """ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;""",
    # ── B-1. 既有同行資料全部撤權（保留資料），管理員本人核准 ────
    """UPDATE partner_profiles
   SET status = 'rejected', reviewed_at = NOW()
 WHERE email IS DISTINCT FROM 'g2349311@gmail.com';""",
    """UPDATE partner_profiles
   SET status = 'approved', reviewed_at = NOW()
 WHERE email = 'g2349311@gmail.com';""",
    # ── B-2. 管理員在 members 卻沒有 partner_profiles 列 → 補建一列 approved ──
    """INSERT INTO partner_profiles (id, email, status, applied_at, reviewed_at, updated_at)
SELECT substr(md5(random()::text), 1, 8),
       'g2349311@gmail.com', 'approved', NOW(), NOW(), NOW()
 WHERE EXISTS (SELECT 1 FROM members WHERE email = 'g2349311@gmail.com')
   AND NOT EXISTS (SELECT 1 FROM partner_profiles WHERE email = 'g2349311@gmail.com');""",
    # ── B-3. 既有供應商全部轉待審核，管理員本人（若有供應商帳號）維持 active ──
    """UPDATE suppliers
   SET status = 'pending'
 WHERE email IS DISTINCT FROM 'g2349311@gmail.com';""",
    """UPDATE suppliers
   SET status = 'active'
 WHERE email = 'g2349311@gmail.com';""",
]

def _run_startup_migrations():
    """開機自跑遷移器：部署後應用程式啟動時，自動執行一次性 schema／資料遷移。

    - 只針對 PostgreSQL（生產庫）；本地 SQLite fallback 直接跳過
      （pg_advisory_xact_lock 與 IS DISTINCT FROM 皆為 PG 語法）
    - schema_migrations 表記錄已執行的遷移名稱，重複啟動不會重跑
    - pg_advisory_xact_lock 防 Render 多 worker 同時啟動的競態：
      同一時間只有一個 worker 能進入遷移交易，其餘等它 COMMIT 後
      查到記錄即跳過
    - fail-open：遷移失敗只 rollback 並印醒目錯誤，不讓 app 起不來，
      網站至少能跑舊功能
    """
    from sqlalchemy import text as _sql_text
    if db.engine.dialect.name != 'postgresql':
        print('[MIGRATE] 非 PostgreSQL（本地 SQLite fallback 模式），跳過開機遷移器')
        return
    try:
        # (a) 遷移記錄表（獨立交易，冪等）
        with db.engine.begin() as conn:
            conn.execute(_sql_text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT NOW())"))
        # (b)~(d) 單一交易：advisory lock → 查記錄 → 逐句執行 → 寫記錄 → COMMIT
        # with begin() 區塊結束自動 COMMIT；任何一句失敗自動 ROLLBACK 全部
        with db.engine.begin() as conn:
            conn.execute(_sql_text(
                "SELECT pg_advisory_xact_lock(hashtext('musen_schema_migrations'))"))
            done = conn.execute(
                _sql_text("SELECT 1 FROM schema_migrations WHERE name = :n"),
                {'n': _MIGRATION_2026_07_04_NAME}).first()
            if done:
                print(f'[MIGRATE] {_MIGRATION_2026_07_04_NAME} 已執行過，跳過')
                return
            for stmt in _MIGRATION_2026_07_04_STMTS:
                conn.execute(_sql_text(stmt))
            conn.execute(
                _sql_text("INSERT INTO schema_migrations (name) VALUES (:n)"),
                {'n': _MIGRATION_2026_07_04_NAME})
        print(f'[MIGRATE] {_MIGRATION_2026_07_04_NAME} 遷移執行成功')
    except Exception as _e:
        # fail-open：印醒目錯誤但不阻止啟動（交易已由 with begin() 自動 rollback）
        print('=' * 70)
        print(f'[MIGRATE][ERROR] 開機遷移 {_MIGRATION_2026_07_04_NAME} 失敗，已回滾：{_e}')
        print('[MIGRATE][ERROR] 網站將以舊 schema 繼續運行，請盡快人工處理！')
        print('=' * 70)

# 確保新表（如 ai_designs）在部署後自動建立；create_all 具冪等性，不動既有表
with app.app_context():
    try:
        db.create_all()
    except Exception as _e:
        print(f'[WARN] db.create_all skipped: {_e}')
    _run_startup_migrations()

if __name__ == '__main__':
    app.run(debug=False)

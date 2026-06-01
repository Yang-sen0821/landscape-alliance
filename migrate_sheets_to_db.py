#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
遷移腳本：從 Google Sheets 遷移到 PostgreSQL
冪等性設計：重跑不會產生重複資料
"""
import os
import json
import hashlib
from datetime import datetime
from uuid import uuid4
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

# ── 載入 app 模型 ────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(__file__))

from app import (
    Member, Work, Rating, Contact, Supplier, Material,
    PartnerProfile, InvestorInquiry, db
)

# ── Sheets 認證 ────────────────────────────────────────────────
SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SHEET_ID = os.environ.get('SHEET_ID', '1E76TuBWUEUw93KjOz_xgNGcTqUuDhc0AdfFpShVIWbs')

def get_gc():
    raw = os.environ.get('GOOGLE_CREDENTIALS')
    if raw:
        info = json.loads(raw)
    else:
        with open(r'E:\keys\google_service_account.json', encoding='utf-8') as f:
            info = json.load(f)
    creds = SACredentials.from_service_account_info(info, scopes=SHEET_SCOPES)
    return gspread.authorize(creds)

gc = get_gc()
sh = gc.open_by_key(SHEET_ID)

# ── 密碼轉換 ────────────────────────────────────────────────
def _hash(pw):
    return generate_password_hash(pw, method='pbkdf2:sha256')

def _is_legacy_sha256(stored_hash):
    return len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash)

def _migrate_password(stored_hash):
    """
    若是舊 SHA-256，回傳 ('legacy', sha256_hash)
    若是新 pbkdf2，回傳 ('new', pbkdf2_hash)
    """
    if _is_legacy_sha256(stored_hash):
        return 'legacy', stored_hash
    return 'new', stored_hash

# ── 工作表取得 ────────────────────────────────────────────
def _get_ws(name):
    try:
        return sh.worksheet(name)
    except Exception:
        return None

def _safe_get_rows(ws):
    """安全取得 worksheet 資料"""
    if not ws:
        return []
    try:
        return ws.get_all_records()
    except Exception as e:
        print(f"  [WARN] Unable to read worksheet: {e}")
        return []

# ── 遷移函數 ────────────────────────────────────────────────
def migrate_members():
    print("\n[*] Migrating members...")
    # 檢查是否已有資料
    count = db.session.execute(text('SELECT COUNT(*) FROM members')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('會員')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            email = (row.get('Email') or row.get('email') or '').strip().lower()
            if not email:
                continue

            member_id = (row.get('會員ID') or row.get('id') or str(uuid4())[:8])[:12]
            pw_hash = row.get('密碼hash') or row.get('password_hash') or ''

            pw_type, final_hash = _migrate_password(pw_hash)
            member = Member(
                id=member_id,
                email=email,
                password_hash=final_hash if pw_type == 'new' else 'MIGRATED_NEEDS_RESET',
                legacy_pw_hash=final_hash if pw_type == 'legacy' else None,
                name=row.get('姓名') or row.get('name') or '',
                company=row.get('公司') or row.get('company') or '',
                phone=row.get('電話') or row.get('phone') or '',
                status=row.get('狀態') or row.get('status') or 'active',
            )
            db.session.add(member)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

def migrate_suppliers():
    print("\n[*] Migrating suppliers...")
    count = db.session.execute(text('SELECT COUNT(*) FROM suppliers')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('供應商') or _get_ws('suppliers')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            email = (row.get('Email') or row.get('email') or '').strip().lower()
            if not email:
                continue

            supplier_id = (row.get('供應商ID') or row.get('id') or str(uuid4())[:8])[:12]
            pw_hash = row.get('密碼hash') or row.get('password_hash') or ''

            pw_type, final_hash = _migrate_password(pw_hash)
            supplier = Supplier(
                id=supplier_id,
                email=email,
                password_hash=final_hash if pw_type == 'new' else 'MIGRATED_NEEDS_RESET',
                legacy_pw_hash=final_hash if pw_type == 'legacy' else None,
                company=row.get('公司名稱') or row.get('company') or '',
                contact_name=row.get('聯絡人') or row.get('contact_name') or '',
                phone=row.get('電話') or row.get('phone') or '',
                status=row.get('狀態') or row.get('status') or 'active',
            )
            db.session.add(supplier)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

def migrate_works():
    print("\n[*] Migrating works...")
    count = db.session.execute(text('SELECT COUNT(*) FROM works')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('作品') or _get_ws('works')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            work_id = (row.get('作品ID') or row.get('id') or str(uuid4())[:8])[:12]
            uploader_email = (row.get('上傳者Email') or row.get('uploader_email') or '').strip().lower()

            # 檢查 uploader 是否存在
            uploader = db.session.execute(
                text('SELECT id FROM members WHERE email = :email'),
                {'email': uploader_email}
            ).scalar()
            if not uploader and uploader_email:
                # 自動建立會員
                new_member = Member(
                    id=str(uuid4())[:8],
                    email=uploader_email,
                    password_hash='MIGRATED_NEEDS_RESET',
                    status='active',
                )
                db.session.add(new_member)

            work = Work(
                id=work_id,
                name=row.get('作品名稱') or row.get('name') or '',
                tags=row.get('標籤') or row.get('tags') or '',
                price=row.get('完工金額') or row.get('price') or '',
                photo_ids=row.get('照片IDs') or row.get('photo_ids') or '',
                uploader_email=uploader_email or 'unknown@example.com',
                status=row.get('狀態') or row.get('status') or 'pending',
                note=row.get('備註') or row.get('note') or '',
                scale=row.get('規模') or row.get('scale') or '',
            )
            db.session.add(work)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

def migrate_ratings():
    print("\n[*] Migrating ratings...")
    count = db.session.execute(text('SELECT COUNT(*) FROM ratings')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('評分') or _get_ws('ratings')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            rating_id = (row.get('評分ID') or row.get('id') or str(uuid4())[:8])[:12]
            work_id = (row.get('作品ID') or row.get('work_id') or '').strip()
            score_str = row.get('分數') or row.get('score') or ''

            if not work_id or not str(score_str).isdigit():
                continue

            rating = Rating(
                id=rating_id,
                work_id=work_id,
                name=row.get('姓名') or row.get('name') or '',
                phone=row.get('電話') or row.get('phone') or '',
                score=int(score_str),
                note=row.get('評論') or row.get('note') or '',
            )
            db.session.add(rating)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()

    # 重新計算所有 works 的評分快取
    print("  Recalculating rating cache...")
    from sqlalchemy import func
    works = db.session.execute(text('SELECT id FROM works')).scalars().all()
    for work_id in works:
        result = db.session.execute(
            text('SELECT AVG(score), COUNT(id) FROM ratings WHERE work_id = :work_id'),
            {'work_id': work_id}
        ).first()
        avg_score, cnt = result if result else (None, 0)
        db.session.execute(
            text('UPDATE works SET avg_rating = :avg, rating_count = :cnt WHERE id = :id'),
            {'avg': round(float(avg_score), 1) if avg_score else None, 'cnt': cnt or 0, 'id': work_id}
        )
    db.session.commit()
    print(f"  [OK] Migrated {migrated} records, rating cache updated")

def migrate_materials():
    print("\n[*] Migrating materials...")
    count = db.session.execute(text('SELECT COUNT(*) FROM materials')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('素材') or _get_ws('materials')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            mat_id = (row.get('素材ID') or row.get('id') or str(uuid4())[:8])[:12]
            supplier_email = (row.get('供應商Email') or row.get('supplier_email') or '').strip().lower()

            # 檢查 supplier 是否存在
            supplier = db.session.execute(
                text('SELECT id FROM suppliers WHERE email = :email'),
                {'email': supplier_email}
            ).scalar()
            if not supplier and supplier_email:
                # 自動建立供應商
                new_supplier = Supplier(
                    id=str(uuid4())[:8],
                    email=supplier_email,
                    password_hash='MIGRATED_NEEDS_RESET',
                    status='active',
                )
                db.session.add(new_supplier)

            material = Material(
                id=mat_id,
                name=row.get('素材名稱') or row.get('name') or '',
                brand=row.get('品牌') or row.get('brand') or '',
                spec=row.get('規格尺寸') or row.get('spec') or '',
                unit=row.get('單位') or row.get('unit') or '',
                price=row.get('零售價') or row.get('price') or '',
                photo_ids=row.get('照片IDs') or row.get('photo_ids') or '',
                tags=row.get('標籤') or row.get('tags') or '',
                supplier_email=supplier_email or 'unknown@example.com',
                status=row.get('狀態') or row.get('status') or 'pending',
                note=row.get('備註') or row.get('note') or '',
            )
            db.session.add(material)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

def migrate_partner_profiles():
    print("\n[*] Migrating partner profiles...")
    count = db.session.execute(text('SELECT COUNT(*) FROM partner_profiles')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('夥伴資料') or _get_ws('partner_profiles')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            email = (row.get('會員Email') or row.get('email') or '').strip().lower()
            if not email:
                continue

            profile = PartnerProfile(
                id=str(uuid4())[:8],
                email=email,
                county=row.get('所在縣市') or row.get('county') or '',
                service_areas=row.get('服務縣市') or row.get('service_areas') or '',
                min_amount=row.get('最小接案金額') or row.get('min_amount') or '',
                intro=row.get('公司簡介') or row.get('intro') or '',
                line_id=row.get('LINE ID') or row.get('line_id') or '',
                website=row.get('官方網站') or row.get('website') or '',
            )
            db.session.add(profile)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

def migrate_contacts():
    print("\n[*] Migrating contacts...")
    count = db.session.execute(text('SELECT COUNT(*) FROM contacts')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('聯絡') or _get_ws('contacts')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            contact = Contact(
                id=str(uuid4())[:8],
                name=row.get('姓名') or row.get('name') or '',
                phone=row.get('電話') or row.get('phone') or '',
                message=row.get('訊息') or row.get('message') or '',
                work_id=row.get('作品ID') or row.get('work_id') or '',
            )
            db.session.add(contact)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

def migrate_investor_inquiries():
    print("\n[*] Migrating investor inquiries...")
    count = db.session.execute(text('SELECT COUNT(*) FROM investor_inquiries')).scalar()
    if count > 0:
        print(f"  Found {count} records, skipping")
        return

    ws = _get_ws('投資人詢問') or _get_ws('investor_inquiries')
    rows = _safe_get_rows(ws)
    if not rows:
        print("  Worksheet empty or not found")
        return

    migrated = 0
    for row in rows:
        try:
            inquiry = InvestorInquiry(
                id=str(uuid4())[:8],
                name=row.get('姓名') or row.get('name') or '',
                company=row.get('公司') or row.get('company') or '',
                phone=row.get('電話') or row.get('phone') or '',
                email=row.get('Email') or row.get('email') or '',
                amount=row.get('金額') or row.get('amount') or '',
                message=row.get('訊息') or row.get('message') or '',
            )
            db.session.add(inquiry)
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row failed: {e}")

    db.session.commit()
    print(f"  [OK] Migrated {migrated} records")

# ── 主遷移流程 ────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Google Sheets -> PostgreSQL Migration Started")
    print("=" * 60)

    try:
        # 依 FK 依賴鏈順序遷移
        migrate_members()
        migrate_suppliers()
        migrate_works()
        migrate_ratings()
        migrate_materials()
        migrate_partner_profiles()
        migrate_contacts()
        migrate_investor_inquiries()

        print("\n" + "=" * 60)
        print("[SUCCESS] Migration completed!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Verify all data migrated correctly")
        print("2. Old SHA-256 passwords will require user reset on next login")
        print("3. Manually verify Google Drive photo_ids are complete")

    except Exception as e:
        print(f"\n[ERROR] Migration failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    # Import app for context
    from app import app
    with app.app_context():
        main()

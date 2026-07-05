-- ============================================================================
-- 帳號權限整改：身分審核制（同行 / 供應商）＋ last_login 追蹤
-- 日期：2026-07-04
-- 目標庫：Supabase PostgreSQL（生產庫）
--
-- ⚠️⚠️⚠️ 本檔須經森哥核准後才可對生產庫執行，執行前請先確認已備份 ⚠️⚠️⚠️
--
-- 內容：
--   A. Schema 變更（冪等，可重複執行）
--      1. partner_profiles 加 status / applied_at / reviewed_at
--      2. members / app_users / suppliers 各加 last_login
--   B. 資料遷移（一次性）
--      1. 既有 partner_profiles 全部 → 'rejected'（保留資料但撤權），
--         唯 g2349311@gmail.com → 'approved'
--      2. g2349311@gmail.com 在 members 卻無 partner_profiles 列時補建一列 approved
--      3. 既有 suppliers 全部 → 'pending'，唯 g2349311@gmail.com（若存在）維持 'active'
--
-- 執行方式：整檔在單一交易內執行（BEGIN/COMMIT），任何一句失敗全部回滾。
-- ============================================================================

BEGIN;

-- ── A-1. partner_profiles：審核狀態欄位 ─────────────────────────────────────
-- status 語意：none=未申請 / pending=審核中 / approved=已核准 / rejected=已拒絕或已撤銷
ALTER TABLE partner_profiles ADD COLUMN IF NOT EXISTS status      VARCHAR(20) DEFAULT 'none';
ALTER TABLE partner_profiles ADD COLUMN IF NOT EXISTS applied_at  TIMESTAMP;
ALTER TABLE partner_profiles ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;

-- ── A-2. 三套帳號表：last_login（登入成功時由應用程式更新）───────────────────
ALTER TABLE members   ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;
ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;

-- ── B-1. 既有同行資料全部撤權（保留資料），管理員本人核准 ────────────────────
-- 先全部設 rejected，再把管理員設回 approved（順序執行，結果確定）
UPDATE partner_profiles
   SET status = 'rejected', reviewed_at = NOW()
 WHERE email IS DISTINCT FROM 'g2349311@gmail.com';  -- IS DISTINCT FROM：email 為 NULL 的列也一併撤權

UPDATE partner_profiles
   SET status = 'approved', reviewed_at = NOW()
 WHERE email = 'g2349311@gmail.com';

-- ── B-2. 管理員在 members 卻沒有 partner_profiles 列 → 補建一列 approved ────
INSERT INTO partner_profiles (id, email, status, applied_at, reviewed_at, updated_at)
SELECT substr(md5(random()::text), 1, 8),  -- 隨機 8 碼 id，與應用程式格式一致
       'g2349311@gmail.com', 'approved', NOW(), NOW(), NOW()
 WHERE EXISTS (SELECT 1 FROM members WHERE email = 'g2349311@gmail.com')
   AND NOT EXISTS (SELECT 1 FROM partner_profiles WHERE email = 'g2349311@gmail.com');

-- ── B-3. 既有供應商全部轉待審核，管理員本人（若有供應商帳號）維持 active ────
UPDATE suppliers
   SET status = 'pending'
 WHERE email IS DISTINCT FROM 'g2349311@gmail.com';  -- IS DISTINCT FROM：email 為 NULL 的列也一併轉待審

UPDATE suppliers
   SET status = 'active'
 WHERE email = 'g2349311@gmail.com';

COMMIT;

-- ── 驗證查詢（唯讀，可於執行後手動確認）────────────────────────────────────
-- SELECT status, count(*) FROM partner_profiles GROUP BY status;
-- SELECT status, count(*) FROM suppliers GROUP BY status;
-- SELECT email, status FROM partner_profiles WHERE email = 'g2349311@gmail.com';

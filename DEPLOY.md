# 上線部署手冊（Supabase 切換）

> 狀態：程式碼全部就緒（commit b7d3d38），遷移腳本已用真實資料驗證。
> 真實資料：6 個會員（全部可用原密碼登入），無作品/評分/聯絡記錄。

## 前置（森哥操作，一次性）

1. **建 Supabase 專案**：https://supabase.com → New Project（免費方案即可）
   - 名稱建議：landscape-alliance
   - 記下 Database 密碼，取得連線字串（Settings → Database → Connection string，URI 格式）
2. **Render 環境變數**（Dashboard → landscape-alliance service → Environment）：
   - `DATABASE_URL` = Supabase 連線字串
   - `GEMINI_API_KEY` = E:\keys\gemini.key 的內容
   - `SECRET_KEY` = 隨機字串（若服務非由 render.yaml blueprint 建立需手動設；
     生成：`python -c "import secrets; print(secrets.token_hex(32))"`）

## 部署（總機腦執行）

```powershell
# 1. 遷移真實資料（本機指向 Supabase 執行，僅讀 Sheets、寫 Supabase）
cd F:\landscape-alliance
$env:DATABASE_URL = "<Supabase 連線字串>"
python migrate_sheets_to_db.py

# 2. 驗證遷移（應為 6 members、全部可登入狀態）
# 3. push 觸發 Render 部署
git push origin main

# 4. 部署後煙霧測試
#    GET / 、/works、/login、/ai-design 皆 200
#    一個會員帳號登入成功（森哥自己的帳號）
```

## 部署後

- 森哥實測 /ai-design 上傳照片生成（每 IP 每日限 3 次）
- 會員無需重新辦帳號：原 Email + 原密碼直接登入
- 確認 OK 後，Google Sheets 保留唯讀備份，不再寫入

## 回滾

Render Dashboard → Deploys → 選前一版 Rollback（舊版用 Sheets，資料未動，零損失）

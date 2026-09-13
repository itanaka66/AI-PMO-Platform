# 🎉 GitHub Pull + WebUI 選択式インストール - 完全ガイド

**更新日**: 2026-09-13  
**ステータス**: ✅ Production Ready  

---

## 📥 GitHub から最新版をプル

### **ステップ 1: リポジトリをクローン（初回）**

```bash
# リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform
```

### **ステップ 2: 最新版をプル（更新時）**

```bash
# リポジトリディレクトリに移動
cd AI-PMO-Platform

# 最新版をプル
git pull origin main

# 状態を確認
git status
```

**出力例:**
```
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean
```

### **ステップ 3: 変更内容を確認**

```bash
# 最新の 5 コミットを表示
git log --oneline -5

# 差分を確認
git diff HEAD~1 HEAD
```

---

## ⚡ インストール（WebUI 選択式）

### **Linux / macOS**

```bash
# インストールスクリプトを実行
bash scripts/install.sh
```

**実行結果:**

```
╔════════════════════════════════════════════════════════════════╗
║          AI-PMO-Platform Installation Script                  ║
║                      Linux / macOS                            ║
╚════════════════════════════════════════════════════════════════╝

Detected OS: Linux

[1/5] Checking Python...
✓ Python 3.10.12

[2/5] Setting up virtual environment...
✓ Virtual environment activated

[3/5] Upgrading pip...
✓ pip upgraded

[4/5] Installing dependencies...
✓ Dependencies installed

[5/5] WebUI Installation
Do you want to install WebUI (FastAPI + React)?
  1) Yes - Full installation with WebUI
  2) No  - CLI only

Select (1 or 2) [default: 1]: 1
[OK] FastAPI dependencies installed
[OK] React dependencies installed
✓ WebUI installation complete
```

### **Windows（PowerShell）**

```powershell
# PowerShell 実行ポリシーを一時的に変更
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

### **Windows（バッチ）**

```batch
# バッチスクリプトを実行
scripts\install.bat
```

---

## 🎯 インストール時の選択肢

### **選択肢 1: WebUI をインストール（推奨）**

```
[5/5] WebUI Installation
Do you want to install WebUI (FastAPI + React)?
  1) Yes - Full installation with WebUI
  2) No  - CLI only

Select (1 or 2) [default: 1]: 1
```

**メリット:**
✅ Web ダッシュボードで可視化  
✅ REST API + WebSocket で外部連携  
✅ リアルタイム更新  
✅ モバイル対応  

**インストール内容:**
- FastAPI バックエンド
- React フロントエンド
- npm 依存関係

### **選択肢 2: CLI のみ（軽量）**

```
Select (1 or 2) [default: 1]: 2
```

**メリット:**
✅ インストール軽量  
✅ サーバーなしで実行  
✅ スケジューラーと組み合わせ容易  

**インストール内容:**
- Python 依存関係のみ

---

## 🌐 WebUI との接続方法

### **アーキテクチャ図**

```
┌──────────────────────────────────────────┐
│        Web ブラウザ                       │
│   http://localhost:5173 (開発)           │
│   http://localhost:8000 (本番)           │
└──────────────────┬───────────────────────┘
                   │
                   ↓ HTTP + WebSocket
┌──────────────────────────────────────────┐
│    FastAPI バックエンド                   │
│    aipmo.web.api:app                     │
│    http://localhost:8000                 │
│                                          │
│  REST API エンドポイント:                │
│    - /api/templates                     │
│    - /api/sessions/create               │
│    - /api/evaluate                      │
│    - /api/analysis/{template}           │
│    - /api/scores/{template}             │
│    - /api/readiness/{template}          │
│    - /api/report/{template}             │
│                                          │
│  WebSocket:                              │
│    - /ws/{template_name}                │
└──────────────────┬───────────────────────┘
                   │
                   ↓ (分析実行)
┌──────────────────────────────────────────┐
│    AI Maturation Engine                  │
│    ハイブリッド評価・スコア分析           │
└──────────────────────────────────────────┘
```

### **3 つの接続モード**

#### **モード A: 開発環境（推奨）**

**ターミナル 1: FastAPI バックエンド**

```bash
# 仮想環境を有効化
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate  # Windows

# FastAPI を起動
uvicorn aipmo.web.api:app --reload --port 8000
```

**ターミナル 2: React フロントエンド**

```bash
cd aipmo/web/frontend
npm install  # 初回のみ
npm run dev  # Vite 開発サーバー起動
```

**ブラウザでアクセス:**
```
http://localhost:5173 (Vite 開発サーバー)
```

**通信フロー:**
```
ブラウザ (localhost:5173)
    ↓
Vite Dev Server (localhost:5173)
    ↓ (プロキシ)
FastAPI (localhost:8000)
```

#### **モード B: 本番環境（Docker）**

**ステップ 1: ビルド**

```bash
docker-compose -f docker-compose.web.yml build
```

**ステップ 2: 起動**

```bash
docker-compose -f docker-compose.web.yml up -d
```

**ステップ 3: アクセス**

```
http://localhost:8000
```

**Docker コンテナ構成:**
```
┌──────────────────────────────────┐
│  nginx / Caddy (リバースプロキシ) │
│  localhost:8000                  │
└──────────────┬───────────────────┘
               │
       ┌───────┴────────┐
       ↓                ↓
   FastAPI          React (Static)
   :8001            :3000
```

#### **モード C: カスタム設定**

FastAPI のホスト・ポート変更：

```bash
# すべてのネットワークインターフェースでリッスン
uvicorn aipmo.web.api:app --host 0.0.0.0 --port 8000

# リモートマシンからアクセス可能
# http://192.168.1.100:8000
```

React のデバイス IP 設定：

```bash
# vite.config.js を編集
export default {
  server: {
    host: '0.0.0.0',
    port: 5173
  }
}

npm run dev
# http://192.168.1.100:5173 からアクセス可能
```

---

## 📡 API リクエスト例

### **1. セッション作成**

```bash
curl -X POST http://localhost:8000/api/sessions/create \
  -H "Content-Type: application/json" \
  -d '{
    "template_name": "meeting_to_tasks",
    "user_id": "user123"
  }'
```

**レスポンス:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "template_name": "meeting_to_tasks",
  "user_id": "user123",
  "created_at": "2026-09-13T01:15:30.123456"
}
```

### **2. 評価を追加**

```bash
curl -X POST http://localhost:8000/api/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "template_name": "meeting_to_tasks",
    "iteration": 1,
    "ai_score": 78.5,
    "user_score": 85.0,
    "correctness": 80,
    "usability": 85,
    "maintainability": 90,
    "clarity": 88,
    "comments": "Good output, minor improvements needed",
    "recommendation": "good"
  }'
```

**レスポンス:**
```json
{
  "status": "success",
  "iteration": 1,
  "hybrid_score": 82.4
}
```

### **3. 分析結果を取得**

```bash
curl -X GET http://localhost:8000/api/analysis/meeting_to_tasks
```

**レスポンス:**
```json
{
  "template_name": "meeting_to_tasks",
  "status": "success",
  "current_iteration": 1,
  "scores": [...],
  "statistics": {
    "ai_mean": 78.5,
    "user_mean": 85.0,
    "hybrid_mean": 82.4,
    "correlation": 0.95
  },
  "trend": "上昇傾向",
  "readiness": {
    "status": "🟡 本番環境準備ほぼ完了",
    "score": 82
  }
}
```

### **4. WebSocket でリアルタイム更新を受け取る**

```javascript
// JavaScript クライアント
const ws = new WebSocket('ws://localhost:8000/ws/meeting_to_tasks');

ws.onopen = () => {
  console.log('Connected to WebSocket');
  // サーバーにpingを送信
  ws.send(JSON.stringify({ type: 'ping' }));
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  
  if (message.type === 'evaluation_update') {
    console.log(`Iteration ${message.iteration} updated`);
    console.log('Analysis:', message.analysis);
    
    // UI を更新
    updateDashboard(message.analysis);
  }
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Disconnected from WebSocket');
};
```

---

## 🚀 本番環境へのデプロイ

### **Oracle Cloud Always Free**

```bash
# 1. リモートサーバーに接続
ssh -i your-key.key ubuntu@your-instance-ip

# 2. リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# 3. インストール
bash scripts/install.sh

# 4. 環境変数を設定
cp .env.example .env
nano .env  # API キーなどを設定

# 5. Docker で起動
docker-compose -f docker-compose.web.yml up -d

# 6. Caddy でリバースプロキシを設定
# Caddyfile を作成して HTTPS 対応

# 7. アクセス
https://your-domain.com
```

### **AWS / Azure / GCP**

```bash
# 同様の手順でセットアップ
# クラウドプロバイダーの管理コンソールで:
# - セキュリティグループ設定（ポート 80, 443 開放）
# - SSL/TLS 証明書設定（ACM / Azure Keyvault など）
# - オートスケーリング設定（必要に応じて）
```

---

## 📋 インストール後のチェックリスト

セットアップ完了後、以下を確認：

```bash
□ Python 依存関係をインストール
  pip list | grep fastapi  # fastapi がリストに表示される

□ テストが全パス
  pytest tests/ -q
  # 540+ passed と表示される

□ FastAPI が起動
  uvicorn aipmo.web.api:app --reload
  # INFO: Uvicorn running on http://127.0.0.1:8000 と表示

□ React が起動
  cd aipmo/web/frontend && npm run dev
  # ➜  Local: http://localhost:5173 と表示

□ ブラウザでアクセス
  http://localhost:5173
  # Web ダッシュボード UI が表示される

□ API が応答
  curl http://localhost:8000/health
  # {"status": "healthy", "timestamp": "..."} と返される

□ WebSocket が接続
  # ブラウザコンソール → Network → WS
  # ws://localhost:8000/ws/meeting_to_tasks が表示される
```

---

## 💡 トラブルシューティング

### **A. FastAPI が起動しない**

```bash
# 1. ポートが使用中か確認
lsof -i :8000              # Linux/macOS
netstat -ano | findstr :8000  # Windows

# 2. 別ポートで起動
uvicorn aipmo.web.api:app --port 9000

# 3. 依存関係を再インストール
pip install --upgrade fastapi uvicorn websockets
```

### **B. React が起動しない**

```bash
# 1. npm キャッシュをクリア
npm cache clean --force

# 2. node_modules を削除して再インストール
rm -rf node_modules package-lock.json
npm install

# 3. 別ポートで起動
npm run dev -- --port 3000
```

### **C. WebSocket 接続エラー**

```bash
# ブラウザコンソール（F12）→ Network タブで確認
# 1. WS フレームが赤色の場合 → FastAPI が起動していない
# 2. CORS エラーの場合 → api.py の CORS 設定を確認

# 開発環境では CORS をすべて許可
# allow_origins=["*"]

# 本番環境では限定
# allow_origins=["https://your-domain.com"]
```

---

## 📖 次のドキュメント

インストール完了後、以下を参照：

| ドキュメント | 説明 |
|-----------|------|
| **INSTALL.md** | 詳細インストールガイド |
| **README.md** | プロジェクト概要 |
| **docs/guide/en.md** | 使用ガイド（英語） |
| **docs/guide/ja.md** | 使用ガイド（日本語） |
| **docs/ARCHITECTURE.md** | システムアーキテクチャ |
| **PHASE_B_WEB_DASHBOARD.md** | Web ダッシュボード詳細 |
| **PHASE_C_HYBRID_EVALUATION_ENGINE.md** | 分析エンジン詳細 |

---

## 🎉 インストール完了！

これであなたの環境で以下が利用可能です：

```
✅ CLI インターフェース
   python -m aipmo.engine.maturation.cli

✅ Web ダッシュボード
   http://localhost:5173 (開発) or :8000 (本番)

✅ REST API
   http://localhost:8000/api/...

✅ WebSocket リアルタイム更新
   ws://localhost:8000/ws/...

✅ Docker コンテナ化
   docker-compose up -d
```

---

## 📞 サポート

**GitHub**: https://github.com/itanaka66/AI-PMO-Platform  
**Issues**: https://github.com/itanaka66/AI-PMO-Platform/issues  
**License**: MIT  

---

**Happy coding! 🚀**

**最終更新**: 2026-09-13


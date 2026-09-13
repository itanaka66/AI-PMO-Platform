# 📖 AI-PMO-Platform - インストールガイド

**プロジェクト**: AI-PMO-Platform / AI Maturation Engine  
**バージョン**: 1.0.0  
**最終更新**: 2026-09-11  

---

## 🚀 クイックスタート

### **Linux / macOS（推奨）**

```bash
# 1. リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# 2. インストールスクリプトを実行
bash scripts/install.sh

# 3. CLI または WebUI を選択して起動
```

### **Windows（PowerShell）**

```powershell
# 1. リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# 2. インストールスクリプトを実行
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 3. CLI または WebUI を選択して起動
```

### **Windows（バッチ）**

```batch
# 1. リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# 2. インストールスクリプトを実行
scripts\install.bat

# 3. CLI または WebUI を選択して起動
```

---

## 📋 詳細インストール手順

### **前提条件**

| 要件 | 最小バージョン | 推奨バージョン |
|-----|--------|--------|
| Python | 3.8 | 3.10+ |
| Node.js | 16 | 18+ |
| pip | - | 最新 |
| npm | - | 最新 |

### **OS 別インストール**

#### **1. Ubuntu / Debian**

```bash
# システム依存関係をインストール
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv git

# Node.js をインストール（WebUI 使用時）
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs

# リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# インストール
bash scripts/install.sh
```

#### **2. Fedora / CentOS / RHEL**

```bash
# システム依存関係をインストール
sudo dnf install -y python3 python3-pip python3-devel git

# Node.js をインストール（WebUI 使用時）
curl -fsSL https://rpm.nodesource.com/setup_18.x | sudo bash -
sudo dnf install -y nodejs

# リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# インストール
bash scripts/install.sh
```

#### **3. macOS（Homebrew）**

```bash
# Homebrew をインストール（未インストール時）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 依存関係をインストール
brew install python@3.10 node git

# リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# インストール
bash scripts/install.sh
```

#### **4. Windows 11（PowerShell）**

```powershell
# 1. Python をインストール（https://www.python.org）
# 「Add Python to PATH」をチェック

# 2. Node.js をインストール（https://nodejs.org）
# LTS バージョンを推奨

# 3. Git をインストール（https://git-scm.com）

# 4. リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# 5. インストールスクリプトを実行
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

#### **5. Docker（推奨）**

```bash
# Docker をインストール
# https://docs.docker.com/install/

# リポジトリをクローン
git clone https://github.com/itanaka66/AI-PMO-Platform.git
cd AI-PMO-Platform

# Docker イメージをビルド
docker-compose build

# コンテナを起動
docker-compose up -d

# Web UI にアクセス
open http://localhost:8000
```

---

## 📝 インストール時の選択肢

インストール時に以下の選択肢が表示されます：

### **WebUI インストール（推奨）**

```
[5/5] WebUI Installation

Do you want to install WebUI (FastAPI + React)?
  1) Yes - Full installation with WebUI
  2) No  - CLI only

Select (1 or 2) [default: 1]: 1
```

**推奨**: `1` を選択して WebUI をインストール

### **WebUI なし（CLI のみ）**

```
Select (1 or 2) [default: 1]: 2
```

CLI モードのみで実行する場合は `2` を選択

---

## 🌐 WebUI との接続方法

### **概要**

WebUI は以下の 2 つのコンポーネントで構成されています：

```
┌─────────────────────────────────────────┐
│            Web ブラウザ                   │
│   http://localhost:3000 または :8000     │
└─────────────────┬───────────────────────┘
                  │
                  ↓ HTTP / WebSocket
┌─────────────────────────────────────────┐
│        FastAPI バックエンド               │
│      http://localhost:8000                │
│   REST API + WebSocket エンドポイント     │
└─────────────────┬───────────────────────┘
                  │
                  ↓ (分析エンジン実行)
┌─────────────────────────────────────────┐
│    AI Maturation Engine                  │
│    ハイブリッド評価・スコア分析           │
└─────────────────────────────────────────┘
```

### **モード 1: 開発環境（推奨）**

#### **ステップ 1: FastAPI バックエンドを起動**

```bash
# 仮想環境を有効化
source venv/bin/activate  # Linux/macOS
# または
venv\Scripts\activate  # Windows

# FastAPI サーバーを起動
uvicorn aipmo.web.api:app --reload --port 8000
```

出力例：
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete
```

#### **ステップ 2: React フロントエンドを起動（別ターミナル）**

```bash
# React ディレクトリに移動
cd aipmo/web/frontend

# 依存関係をインストール（初回のみ）
npm install

# 開発サーバーを起動
npm run dev
```

出力例：
```
  VITE v4.3.9  ready in 234 ms

  ➜  Local:   http://localhost:5173/
  ➜  press h to show help
```

#### **ステップ 3: ブラウザでアクセス**

**Vite 開発サーバー（推奨）:**
```
http://localhost:5173
```

**FastAPI サーバー（本番環境）:**
```
http://localhost:8000
```

### **モード 2: 本番環境（Docker）**

#### **ステップ 1: Docker イメージをビルド**

```bash
# Web ダッシュボード用 Docker Compose
docker-compose -f docker-compose.web.yml build
```

#### **ステップ 2: コンテナを起動**

```bash
docker-compose -f docker-compose.web.yml up -d
```

#### **ステップ 3: ブラウザでアクセス**

```
http://localhost:8000
```

#### **ステップ 4: ログを確認**

```bash
# ログをリアルタイム表示
docker-compose -f docker-compose.web.yml logs -f web

# コンテナを停止
docker-compose -f docker-compose.web.yml down
```

### **モード 3: カスタム設定**

FastAPI サーバーのポート・ホストを変更：

```bash
# ポート 9000 で起動
uvicorn aipmo.web.api:app --host 0.0.0.0 --port 9000

# リモートホストから接続可能に（0.0.0.0 を指定）
uvicorn aipmo.web.api:app --host 0.0.0.0 --port 8000
```

React フロントエンドのデバイス IP を設定：

```bash
# vite.config.js を編集
export default defineConfig({
  server: {
    host: '0.0.0.0',  # すべてのネットワークインターフェース
    port: 5173
  }
})

# 起動
npm run dev
```

---

## 🔗 API エンドポイント

### **REST API**

| メソッド | エンドポイント | 説明 |
|---------|----------|------|
| GET | `/health` | ヘルスチェック |
| GET | `/api/templates` | テンプレート一覧 |
| POST | `/api/sessions/create` | セッション作成 |
| POST | `/api/evaluate` | 評価追加 |
| GET | `/api/analysis/{template}` | 分析結果取得 |
| GET | `/api/scores/{template}` | スコア取得 |
| GET | `/api/insights/{template}` | 洞察取得 |
| GET | `/api/readiness/{template}` | 準備度取得 |
| GET | `/api/report/{template}` | レポート取得 |

### **WebSocket**

| エンドポイント | 説明 |
|-----------|------|
| `/ws/{template_name}` | リアルタイム更新 |

**例:**
```javascript
// JavaScript クライアント
const ws = new WebSocket('ws://localhost:8000/ws/meeting_to_tasks');

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log('Update:', message);
};
```

---

## 🧪 テストの実行

### **全テスト実行**

```bash
pytest tests/ -v
```

### **特定のテストスイート**

```bash
# 熟成エンジンのテスト
pytest tests/test_maturation/ -v

# 並列実行のテスト
pytest tests/test_parallel_runner.py -v

# カバレッジ付き
pytest tests/ --cov=aipmo
```

### **期待される結果**

```
=============== 540+ passed in 12.34s ===============
```

---

## ⚙️ 環境変数設定

### **FastAPI のための環境変数**

```bash
# .env ファイルを作成
cp .env.example .env

# エディタで編集
nano .env
```

**設定例:**

```bash
# OpenAI
OPENAI_API_KEY=sk-...

# Ollama
OLLAMA_HOST=http://localhost:11434

# Web UI
AIPMO_WEB_HOST=0.0.0.0
AIPMO_WEB_PORT=8000

# CORS（VITE 開発サーバーからのリクエスト許可）
CORS_ORIGINS=["http://localhost:5173", "http://localhost:3000"]
```

### **React のための環境変数**

```bash
# aipmo/web/frontend/.env を作成
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000
```

---

## 🐛 トラブルシューティング

### **Python が見つからない**

```bash
# Python パスを確認
which python3      # Linux/macOS
where python       # Windows

# パスを追加（Windows）
# 環境変数 → PATH に C:\Users\YourName\AppData\Local\Programs\Python\Python310 を追加
```

### **仮想環境を有効化できない**

```bash
# スクリプト実行ポリシーを変更（Windows）
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# venv を再作成
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
venv\Scripts\activate     # Windows
```

### **npm install が失敗する**

```bash
# キャッシュをクリア
npm cache clean --force

# 再実行
npm install

# ディスク容量を確認
npm cache verify
```

### **FastAPI が起動しない**

```bash
# ポートが使用中かどうか確認
lsof -i :8000              # Linux/macOS
netstat -ano | findstr :8000  # Windows

# 別のポートで起動
uvicorn aipmo.web.api:app --port 9000
```

### **WebSocket 接続エラー**

```bash
# ブラウザコンソールでエラーを確認
# F12 キーで Developer Tools を開く → Console タブ

# CORS エラーの場合、api.py の CORS 設定を確認
# allow_origins=["*"] に変更（開発環境のみ）
```

### **React 開発サーバーが起動しない**

```bash
# ポート 5173 が使用中か確認
lsof -i :5173

# package.json の scripts を確認
cat package.json | grep -A 5 '"scripts"'

# 別のポートで起動
npm run dev -- --port 3000
```

---

## 📡 API リクエスト例

### **cURL での評価追加**

```bash
curl -X POST http://localhost:8000/api/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "uuid-here",
    "template_name": "meeting_to_tasks",
    "iteration": 1,
    "ai_score": 78.5,
    "user_score": 85.0,
    "correctness": 80,
    "usability": 85,
    "maintainability": 90,
    "clarity": 88,
    "comments": "Good output",
    "recommendation": "good"
  }'
```

### **Python での API リクエスト**

```python
import requests
import json

# セッション作成
response = requests.post(
    "http://localhost:8000/api/sessions/create",
    params={
        "template_name": "meeting_to_tasks",
        "user_id": "user123"
    }
)
session_id = response.json()["session_id"]

# 評価追加
response = requests.post(
    "http://localhost:8000/api/evaluate",
    json={
        "session_id": session_id,
        "template_name": "meeting_to_tasks",
        "iteration": 1,
        "ai_score": 78.5,
        "user_score": 85.0
    }
)

# 分析結果取得
response = requests.get(
    "http://localhost:8000/api/analysis/meeting_to_tasks"
)
analysis = response.json()
print(json.dumps(analysis, indent=2))
```

### **JavaScript での WebSocket**

```javascript
// WebSocket 接続
const ws = new WebSocket('ws://localhost:8000/ws/meeting_to_tasks');

// 接続成功
ws.onopen = () => {
  console.log('Connected');
  ws.send(JSON.stringify({ type: 'ping' }));
};

// メッセージ受信
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log('Received:', message);
};

// エラー
ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

// 接続を閉じる
ws.close();
```

---

## 🔄 CLI との併用

CLI と WebUI は並行して実行可能：

```bash
# ターミナル 1: FastAPI
uvicorn aipmo.web.api:app --reload

# ターミナル 2: React
cd aipmo/web/frontend && npm run dev

# ターミナル 3: CLI
python -m aipmo.engine.maturation.cli
```

---

## 📚 次のステップ

1. **インストール完了後:**
   - `docs/guide/en.md` を読む
   - サンプルテンプレートを試す
   - WebUI でダッシュボードを確認

2. **カスタマイズ:**
   - `templates/examples/` のテンプレートを参照
   - 独自のテンプレートを作成
   - LLM プロバイダーを設定

3. **本番環境:**
   - Docker で運用
   - クラウド（Oracle Cloud Free など）にデプロイ
   - ロードバランシング・監視を設定

---

## 🆘 サポート

### **ドキュメント**

- 📖 `README.md` - プロジェクト概要
- 🚀 `docs/ARCHITECTURE.md` - システムアーキテクチャ
- 💬 `docs/guide/en.md` - 使用ガイド（英語）
- 🎯 `docs/guide/ja.md` - 使用ガイド（日本語）

### **GitHub Issues**

https://github.com/itanaka66/AI-PMO-Platform/issues

### **ライセンス**

MIT License - 自由に使用、修正、配布可能

---

**Happy installation! 🎉**

**最終更新**: 2026-09-11  
**バージョン**: 1.0.0

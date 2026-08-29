# インストール方法 / Installation

PC の操作に不慣れでも入れられるようにしています。
自分に合うものを 1 つ選んでください。

Written for people who are not comfortable with a terminal. Pick one.

| | 向いている人 / Who it suits | AI |
|---|---|---|
| **A. Windows インストーラ** | Windows。一番かんたん / easiest on Windows | クラウド / cloud |
| **B. Mac・Linux スクリプト** | Mac または Linux | クラウド / cloud |
| **C. Docker** | 社内データを外に出したくない / keeps data in-house | ローカル / local |

---

## A. Windows インストーラ / Windows installer

1. `AI-PMO-Setup-0.1.0.exe` をダウンロードする / download it
2. ダブルクリックする / double-click it
3. 画面の指示に従う / follow the prompts

管理者権限は不要です。インストール後にセットアップ画面が開くので、
AI の提供元を選んで API キーを貼り付けてください。

No administrator rights required. A setup screen opens afterwards; choose an AI
provider and paste your API key into it.

**API キーの取得 / Getting an API key**
選んだ提供元のサイトで作成します。迷ったら OpenAI で構いません。
Create one with the provider you chose; OpenAI is a fine default.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com
- Groq — https://console.groq.com/keys
- OpenRouter — https://openrouter.ai/keys

提供元ごとの違いは [docs/PROVIDERS.md](docs/PROVIDERS.md) にあります。
Groq と OpenRouter には埋め込み API が無いため、ベクトル検索を使う場合は
鍵が2つ要ります。ウィザードがその場で知らせます。

Groq and OpenRouter have no embeddings API, so vector search needs a second
key; the wizard says so at the time.

### インストーラを自分でビルドする / Building the installer yourself

Windows と [Inno Setup 6](https://jrsoftware.org/isdl.php) が必要です。
PyInstaller はクロスコンパイルできないので、Windows 上でしかビルドできません。

Requires Windows and Inno Setup 6. PyInstaller cannot cross-compile, so this
must run on Windows.

```powershell
.\installer\build.ps1
# → dist\AI-PMO-Setup-0.1.0.exe
```

タグを push すると GitHub Actions が同じものを作ります。
Pushing a tag builds the same artifact in GitHub Actions.

---

## B. Mac・Linux / macOS and Linux

ターミナルを開いて、次の 1 行を貼り付けてください。

Open Terminal and paste this single line.

```bash
curl -fsSL https://raw.githubusercontent.com/aipmo/aipmo/main/scripts/install.sh | bash
```

リポジトリを既に持っている場合 / If you already have the repository:

```bash
./scripts/install.sh
```

`sudo` は使いません。`~/.local` の下にだけ書き込みます。
システムの Python には手を触れず、専用の仮想環境を作ります。

No `sudo`. Everything lands under `~/.local`. Your system Python is left alone;
the installer builds an isolated virtual environment.

> **`aipmo: command not found` と出たら / If you see this**
> `~/.local/bin` が PATH に入っていません。次を実行してください。
> Add `~/.local/bin` to your PATH:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
> ```
> bash を使っている場合は `~/.bashrc` に置き換えてください / use `~/.bashrc` for bash.

---

## C. Docker（ローカル AI）/ Docker (local AI)

会議の記録を外部の AI サービスに送りたくない場合はこちらです。
PostgreSQL・Qdrant・ローカル LLM がまとめて起動します。

Use this when meeting transcripts must not go to an external AI service.
It brings up PostgreSQL, Qdrant and a local LLM together.

**必要なもの / Requirements**
- [Docker Desktop](https://docs.docker.com/get-docker/)
- 空きディスク 20GB 程度 / about 20GB free
- メモリ 16GB 以上を推奨 / 16GB RAM or more recommended
- GPU があると実用的な速度になります（無くても動きます）/ a GPU makes it usable in
  practice, though it runs without one

```bash
./scripts/install-docker.sh
```

初回はモデルのダウンロードで数 GB あります。時間がかかります。
The first run downloads several GB of model weights. It takes a while.

GPU を使う場合は `docker-compose.yml` の `deploy:` のコメントを外してください。
To use a GPU, uncomment the `deploy:` block in `docker-compose.yml`.

---

## セットアップウィザード / Setup wizard

インストール後に自動で開きます。後からやり直すこともできます。
It opens automatically after installation. You can re-run it any time:

```bash
aipmo setup
```

聞かれること / What it asks:

1. **AI をどこで動かすか / where the AI runs** — クラウドかローカルか
2. **提供元 / provider** — OpenAI / Gemini / Groq / OpenRouter（クラウドの場合）
3. **API キー / API key** — クラウドを選んだ場合のみ / cloud only
4. **組織名 / organization name** — データの保管先を分ける識別子。
   英小文字・数字・アンダースコアのみ /
   an identifier that separates where your data is stored; lowercase, digits and
   underscore only
5. **データベース連携 / data layer** — 分からなければ N で構いません /
   answer N if you are unsure

ウィザードは画面の言語に合わせて日本語・英語・中国語・韓国語・スペイン語・
フランス語・ドイツ語・ポルトガル語で表示されます。

The wizard follows your system language across eight languages.

API キーは `config.yaml` ではなく `.env` に、提供元ごとの正しい変数名
（`OPENAI_API_KEY`、`GEMINI_API_KEY` など）で保存され、本人しか読めない権限に
設定されます。`config.yaml` はチームで共有したりコミットしたりする前提なので、
キーが混ざらないように分けてあります。

The key goes to `.env`, not `config.yaml`, and is locked to your user account.
Config files get shared and committed; keys should not ride along.

---

## 動作確認 / Verifying it works

```bash
aipmo validate templates/examples/meeting_to_tasks.yaml
aipmo adapters
aipmo doctor          # 接続確認 / connection check
```

続けて使うもの / What you will use next:

```bash
aipmo serve --host 0.0.0.0   # スマホ向け画面 / mobile interface
aipmo schedule --list        # 定時実行の予定 / scheduled runs
```

スマホからの利用と権限分離は [docs/MOBILE.md](docs/MOBILE.md)、
定時実行は [docs/SCHEDULER.md](docs/SCHEDULER.md) にあります。

`OK  templates/examples/meeting_to_tasks.yaml  [software] ステップ 6 件`
と表示されれば成功です。

Seeing that line means it worked.

---

## アンインストール / Uninstalling

**Windows** — 設定 → アプリ → AI-PMO Platform → アンインストール
Settings → Apps → AI-PMO Platform → Uninstall

**Mac・Linux**
```bash
rm -rf ~/.local/share/ai-pmo ~/.local/bin/aipmo
```

**Docker**
```bash
docker compose down -v    # -v はデータも消します / -v also deletes the data
```

---

## うまくいかないとき / When it does not work

**日本語が文字化けする / Japanese text comes out garbled**
古い版を使っている可能性があります。`.bat` は CP932、`.ps1` と `.iss` は
UTF-8 (BOM 付き) で保存されている必要があります。自分で編集した場合は、
保存時の文字コードを確認してください。

If you edited these files yourself, check what your editor saved them as:
`.bat` must be CP932, while `.ps1` and `.iss` need UTF-8 **with** a BOM —
Windows PowerShell 5.1 reads a BOM-less script as ANSI.

**Windows で `.ps1` をダブルクリックしても何も起きない**
既定の実行ポリシーで PowerShell スクリプトがブロックされています。
`install.bat` の方をダブルクリックしてください。こちらが回避策込みで起動します。

Windows blocks `.ps1` files by default. Double-click `install.bat` instead — it
launches the script with the policy bypass already applied.

**`python3-venv` が無いと言われる / venv creation fails on Debian or Ubuntu**
```bash
sudo apt install python3-venv
```

**Docker が起動していないと言われる / Docker is not running**
Docker Desktop を起動してから、もう一度実行してください。
Start Docker Desktop, then run the script again.

**API キーを入れ忘れた / Forgot to enter the API key**
```bash
aipmo setup
```
をもう一度実行してください / run it again.

**ウイルス対策ソフトがインストーラを止める / Antivirus blocks the installer**
署名のない実行ファイルは警告されることがあります。心配な場合は
B（Mac・Linux）か C（Docker）の方法を使うか、`installer\build.ps1` で
自分でビルドしてください。

Unsigned executables can trigger warnings. If that concerns you, use option B or
C instead, or build it yourself with `installer\build.ps1`.

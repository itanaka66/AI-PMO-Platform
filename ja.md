# はじめてのAI-PMO

> 翻訳元 / Source: この日本語版と英語版が原本です。他の言語は翻訳です。

---

## これは何ですか

プロジェクト管理（PMO）の作業を、AI に自動でやらせるための道具です。

たとえば、こんなことができます。

- Teams の会議記録から、**議事録を自動で作る**
- 議事録から「誰が・何を・いつまでに」を**取り出してタスクにする**
- 期限を過ぎたタスクの担当者に、**自動で催促を送る**

「テンプレート」という設計図を選ぶだけで動きます。
プログラミングの知識は要りません。

---

## 誰のための道具ですか

- **学生** — プロジェクト管理の型を学びながら使えます
- **中小企業** — 専任の PMO を置けなくても、作業の型が手に入ります
- **大企業** — 部署ごとにばらついた進め方を、テンプレートで揃えられます

無料です。使用料はかかりません。

---

## 使うのに必要なもの

| | 必要なもの | 費用 |
|---|---|---|
| **かんたん構成** | パソコン、AI サービスの API キー | AI の利用料（従量制・少額） |
| **社内構成** | Docker、メモリ 16GB 以上、できれば GPU | 無料（電気代のみ） |

> **どちらを選べばいい？**
> まず試すなら **かんたん構成**。
> 会議の内容を社外のサービスに送りたくない場合は **社内構成** です。

---

## 3ステップで始める

### 1. 入れる

[INSTALL.md](../../INSTALL.md) の手順に従ってください。

- **Windows** — `AI-PMO-Setup.exe` をダブルクリック
- **Mac / Linux** — ターミナルで `./scripts/install.sh`
- **Docker** — `./scripts/install-docker.sh`

### 2. 設定する

インストールが終わると、設定画面が自動で開きます。
質問に答えてください。分からなければ Enter を押せば既定値になります。

```
1) AI をどこで動かしますか？      → 1（クラウド）
2) AI の提供元を選んでください    → 1（OpenAI）
3) API キーを入力してください     → 貼り付ける
4) 組織を識別する名前             → 会社名など（英小文字）
5) データベース連携を使いますか？  → N
```

**AI の提供元は4つから選べます。** 迷ったら OpenAI にしてください。
埋め込み機能も揃っていて、設定が1つで済みます。

| 提供元 | 特徴 |
|---|---|
| OpenAI | 迷ったらこれ |
| Gemini | 長い会議記録を安く処理できる |
| Groq | 速い。ただし鍵が2つ要る |
| OpenRouter | 1つの鍵で多くのモデルを試せる |

**API キーの取り方**
選んだ提供元のサイトでアカウントを作り、キーを発行します。
長い文字列です。他人に見せないでください。

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

詳しくは [PROVIDERS.md](../PROVIDERS.md) を見てください。

### 3. 動かしてみる

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

こう表示されれば成功です。

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

---

## テンプレートとは

「どういう順番で、何をするか」を書いた設計図です。
これ1つが、1つの PMO 作業に対応します。

```yaml
name: meeting_minutes          # 名前
trigger: "event:teams:meeting_ended"   # いつ動くか（会議が終わったとき）

steps:                         # 何をするか
  - id: fetch_transcript       # ① 会議記録を取ってくる
    adapter: teams

  - id: minutes                # ② AI に議事録を書かせる
    llm: { profile: default }

  - id: register_jira          # ③ タスクを登録する
    adapter: jira
```

やりたいことが変われば、テンプレートを差し替えるだけです。
**AI の使い方そのものが、テンプレートによって変わります。**

---

## よく使う操作

```bash
aipmo setup       # 設定をやり直す
aipmo validate <ファイル>   # テンプレートに間違いがないか調べる
aipmo run <ファイル>        # 実行する
aipmo adapters    # つながっている外部ツールの一覧
aipmo doctor      # 接続できているか確認する
```

---

## 安全のために知っておくこと

**API キーは `.env` に保存されます。** `config.yaml` には入りません。
設定ファイルは同僚と共有したり、Git に登録したりするものなので、
キーが混ざらないように分けてあります。

**社内データは外に出ません。** 会社ごとにデータの保管場所が分かれていて、
別の会社のデータには技術的に到達できないようになっています。

**公開は自動では行われません。** ノウハウを一般公開する仕組みがありますが、
必ず人の承認が要ります。プログラムが勝手に公開することはできません。

---

## 困ったときは

**`aipmo` と打っても「見つかりません」と出る**
Mac / Linux の場合、次を実行してから、ターミナルを開き直してください。
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**Windows で `.ps1` をダブルクリックしても何も起きない**
`install.bat` の方をダブルクリックしてください。

**API キーを入れ忘れた**
`aipmo setup` をもう一度実行してください。

**ウイルス対策ソフトがインストーラを止める**
署名のないファイルは警告が出ることがあります。
気になる場合は Mac / Linux 版か Docker 版を使ってください。

さらに詳しくは [INSTALL.md](../../INSTALL.md) を見てください。

---

## 次に読むもの

- [INSTALL.md](../../INSTALL.md) — インストールの詳細
- [MOBILE.md](../MOBILE.md) — スマホから使う
- [PROVIDERS.md](../PROVIDERS.md) — AI の提供元の選び方
- [AGENTS.md](../AGENTS.md) — AI に自分で判断させる
- [TEAMS.md](../TEAMS.md) — Teams の会議記録とつなぐ
- [JIRA-SLACK.md](../JIRA-SLACK.md) — Jira への起票と Slack への通知
- [SCHEDULER.md](../SCHEDULER.md) — 決まった時刻に自動で動かす
- [README.md](../../README.md) — 仕組みと設計（開発者向け）
- `templates/examples/` — テンプレートの実例

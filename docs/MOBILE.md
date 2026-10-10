# スマホから使う / Using it from a phone

Web サーバーも AI サーバーも、**どこで動かすかは利用者が決めます**。
このソフトが用意するのは待ち受け側だけで、公開範囲・URL・ポートは設定で指定します。

Both the web server and the AI server are **yours to place**. This software
provides only the listener; exposure, URL and port are configuration.

---

## 起動する / Starting it

```bash
aipmo serve --host 0.0.0.0
```

起動すると URL が表示されます。スマホのブラウザで開いてください。

```
  スマホからこの URL を開いてください:
    http://192.168.1.24:8765/?token=xxxxxxxxxxxx
```

同じ Wi-Fi につながっていれば、そのまま開けます。
ホーム画面に追加すると、アプリのように使えます。

Connect the phone to the same Wi-Fi and open the URL. Add it to the home screen
to use it like an app.

---

## 表示言語を選ぶ / Choosing the display language

トークンが無い（または期限切れの）状態で開くと出るログイン画面で、8言語
（日本語・英語・中文・한국어・Español・Français・Deutsch・Português）から選べます。
選んだ言語は Cookie に残り、次にそのブラウザで開いたときもそのまま使われます。

**アプリの中に言語を切り替える場所はありません。** 変えたいときは、ログイン
画面まで戻って選び直してください（ログアウトする・トークンを変える・Cookie
を消す、のいずれかで戻れます）。ログイン画面自体は、選ぶ言語に関わらず
常に英語です——その選択がまだ決まっていない、唯一の画面だからです。

The login screen (shown when there is no valid token yet) offers all 8
languages. The choice is kept in a cookie and reused the next time that
browser opens the app. **Nothing inside the app itself switches it** — to
change it, go back to the login screen (log out, change the token, or clear
the cookie) and pick again. The login screen itself always stays in English,
regardless of which language gets picked, since it is the one screen where
that choice has not been made yet.

---

## 操作が無いときの自動ログアウト / Auto-logout when idle

マウス・キーボード・タッチの操作が **10分** 無いと、自動でログアウトして
ログイン画面に戻ります。画面を開いたまま離席したときに、他人がそのまま
触れる時間を限るためのものです。アクセスキーの Cookie だけを消し、選んだ
表示言語は残ります。10分のうちに何か1つでも操作すれば、そこからまた
10分数え直します。

スマホを机に置いたまま離席したときにも同じように効きます。再び使うときは
ログイン画面でトークンを入力し直してください（共有した URL に `?token=`
が残っていれば、そこから開き直すだけで入れます——これは今までと同じ
挙動です）。

After **10 minutes** with no mouse, keyboard, or touch activity at all, the
screen automatically logs out and returns to the login screen — limiting how
long someone else could use an unattended, already-open screen. Only the
access-key cookie is cleared; the chosen display language stays. Any single
interaction within the 10 minutes restarts the countdown.

The same applies to a phone left open on a desk. To use it again, enter the
token on the login screen (or reopen a shared URL that still has `?token=`
in it — that still works exactly as before).

---

## 権限 / Roles

URL は2種類表示されます。**渡す相手によって使い分けてください。**

Two URLs are printed. **Which one you hand out matters.**

```
  実行できる人へ / can run:
    http://192.168.1.24:8765/?token=xxxx
  見るだけの人へ / view only:
    http://192.168.1.24:8765/?token=yyyy
```

| | できること |
|---|---|
| **実行できる人 / operator** | テンプレートの実行、履歴と状況の閲覧 |
| **見るだけの人 / viewer** | 履歴と状況の閲覧のみ |

PMO では「メンバーは進捗を見るだけ、担当者だけが実行」という分け方が自然です。
**トークンが1本しかないと、進捗を見せたいだけの相手に、課題の起票と通知の送信まで
できる権限を渡すことになります。**

The natural split is that members watch progress while one person runs things.
With a single token, showing someone the progress means handing them the
ability to file issues and send messages.

**閲覧用を渡した相手は実行できません。** 画面上でボタンが押せないだけでなく、
サーバー側が拒否します。ボタンを隠すのは案内であって、権限管理ではないためです。

Someone given the viewer URL cannot run anything. The button is untappable, but
more importantly the server refuses: hiding a button is a courtesy, not access
control.

固定したい場合は環境変数で渡します。指定しなければ起動のたびに変わります。

```bash
export AIPMO_WEB_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
export AIPMO_VIEWER_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
```

> 2つは必ず別の値にしてください。同じだと分離になりません。
> 同じ値を設定した場合、起動時に拒否されます。
>
> The two must differ, or there is no separation at all. Setting them the same
> is refused at startup.

**誰が実行したかは履歴に残ります。** PMO では「いつ動いたか」より
「誰が動かしたか」が問われることがあるためです。

Runs record who started them: the question asked is often who ran this, not
merely when.

**実行・WBS再計画提案の承認/却下・認証の失敗は、アプリのログにも
記録されます**（ロガー名 `aipmo.web`）。履歴やDBはテナント単位のクエリを
打たないと見えませんが、ログは通常の監視・集約基盤（syslog・CloudWatch
など）にそのまま流れるので、承認待ちの提案が承認された瞬間や、権限の無い
トークンでの操作の試みを、外部から監視できます。**トークンそのものは
ログに残しません** — 誤って有効な鍵に近い値を書き残さないためです。

Runs, WBS-replan proposal approve/reject decisions, and auth failures are
also written to the application log (logger name `aipmo.web`). The run
history and database are only visible via a tenant-scoped query; the log
reaches whatever monitoring/aggregation pipeline (syslog, CloudWatch, etc.)
already watches this process, so a proposal being approved, or an attempt
with a token lacking permission, can be observed from outside. **The token
itself is never logged** — so a value close to a real credential never ends
up sitting in the logs.

---

## アクセスキー / The access key

URL の `?token=` がアクセスキーです。**これを知っている人は誰でも操作できます。**

初回に開くと、キーはブラウザの Cookie に移り、アドレス欄から消えます。
スクリーンショットや履歴からの漏洩を減らすためです。

The `?token=` value is the access key. **Anyone who has it can operate the
system.** On first open it moves into a cookie and disappears from the address
bar, so it stops leaking through screenshots and browser history.

キーを固定したい場合は環境変数で渡します。指定しなければ起動のたびに変わります。

To pin the key, pass it in the environment. Without it, a new one is generated
on every start.

```bash
export AIPMO_WEB_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
```

> `config.yaml` にキーを書かないでください。設定ファイルは同僚と共有したり
> Git に登録したりするものです。
> Do not put the key in `config.yaml` — config files get shared and committed.

---

## 公開範囲 / Exposure

| 設定 | 届く範囲 / Reachable from |
|---|---|
| `127.0.0.1`（既定 / default） | その PC のみ / that machine only |
| `0.0.0.0` | 同じネットワーク上の全端末 / every device on the network |

既定が `127.0.0.1` なのは、**社内 LAN 全体に誤って開くことが事故では起きないように**
するためです。スマホから使うには明示的な変更が必要です。

The default is `127.0.0.1` so that exposing the system to an entire office
network cannot happen by accident. Reaching it from a phone takes a deliberate
change.

**社外から使う場合 / Reaching it from outside**

インターネットに直接開かないでください。次のいずれかを使います。

Do not expose it directly to the internet. Use one of:

- VPN（Tailscale、WireGuard など）
- リバースプロキシで TLS を終端する（Caddy、nginx）/ terminate TLS at a proxy

このソフト自体は TLS を提供しません。前段で用意してください。
This software does not provide TLS. Provide it in front.

```
# Caddy の例 / Caddy example
pmo.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

---

## 設定 / Configuration

```yaml
web:
  host: 0.0.0.0        # 待ち受けアドレス / bind address
  port: 8765
  templates_dir: templates
```

### 別オリジンから呼ぶ場合（CORS）/ Calling from another origin (CORS)

**既定では CORS ヘッダを一切付けません。** この画面はトークンをクエリ文字列
やクッキーで運ぶため、任意のオリジンからの読み取りを許すと認証だけでは
防げない経路が生まれます。別ドメインの画面や自作アプリからこの API を
直接呼ぶ場合だけ、許可するオリジンを明示してください。

**No CORS headers are sent by default.** This screen carries its token in a
query string or cookie, so allowing any origin to read responses would open
a path authentication alone does not close. Only set this up when a
separate-domain screen or your own app calls this API directly.

```yaml
web:
  cors_origins:
    - https://app.example.com
```

Docker やクラウド展開では、`config.yaml` を書き換える代わりに環境変数
`AIPMO_CORS_ORIGINS`（カンマ区切り）で渡せます。設定してあれば
`config.yaml` の `web.cors_origins` より優先されます:

In a Docker or cloud deployment, pass this via the `AIPMO_CORS_ORIGINS`
environment variable (comma-separated) instead of editing `config.yaml`.
When set, it takes priority over `config.yaml`'s `web.cors_origins`:

```bash
AIPMO_CORS_ORIGINS=https://app.example.com,https://admin.example.com
```

`*` を指定すると全オリジンを許可しますが、その場合ブラウザの仕様上
Cookie による認証は使えなくなり、クエリ文字列のトークンだけが機能します。

Specifying `*` allows every origin, but browsers then refuse to combine a
wildcard with cookie-based credentials — only the query-string token still
works in that case.

### AI サーバーを自分で用意する / Pointing at your own AI server

OpenAI 互換のエンドポイントなら何でも指せます。vLLM、LM Studio、llama.cpp、
社内のゲートウェイなど。

Any OpenAI-compatible endpoint works — vLLM, LM Studio, llama.cpp, or a
corporate gateway.

```yaml
llm:
  default:
    provider: openai
    model: your-model-name
    base_url: http://192.168.1.50:8000/v1
```

Ollama を使う場合 / For Ollama:

```yaml
llm:
  default:
    provider: ollama
    model: qwen2.5:14b
    host: http://192.168.1.50:11434
```

AI サーバーを別の機械に置けば、手元の PC は非力なままで構いません。
Putting the AI server on another machine leaves the client machine free to be
modest.

---

## 画面の見かた / Reading the screen

**テンプレート** — 押すと実行されます。工程数と業種が出ます。
読み込めないテンプレートは、隠さずファイル名と原因を表示します。
直すべきファイルを探せるのはファイル名の方だからです。

**Templates** — tap to run. A template that fails to load shows its filename
and the reason rather than disappearing: the filename is what lets you find and
fix it.

**実行** — 新しい順に並びます。各行の帯が工程で、幅は所要時間に比例します。

| 表示 / Mark | 意味 / Meaning |
|---|---|
| 緑 / green | 成功 / succeeded |
| 赤 / red | 失敗 / failed |
| 斜線 / hatched | 条件を満たさず実行されなかった / skipped |

押すと工程ごとの内訳が開きます。失敗した実行は最初から開いた状態で出ます。
確認したいのはそこだからです。

**Runs**, newest first. The band is the run's steps, sized in proportion to how
long each took. Tap for the breakdown; failed runs open already expanded,
because that is what you came to look at.

**PMO Core** — 画面の先頭に出ます（`aipmo schedule` が書く台帳がある構成のみ。無ければ欄ごと出ません）。
全体レベル、警告、担当の提案、優先順位（押すと点数の内訳）、メンバーの負荷、学習した補正、
直近の判断が並びます。担当の提案には「この担当で確定」ボタンがあり、**実行用トークンのときだけ**
出ます（閲覧用では出ず、サーバーも 403 で断ります）。そのタスクのトラッカー（Jira・GitHub Projects・Plane・OpenProject・Azure DevOps）の
アダプタがあり、メンバーのアカウントが設定されていれば、トラッカー側の担当者も更新します。画面は台帳を読むだけで、周を回したり、通知や自動起動を起こしたりはしません。
常駐側の更新が 15 分以上止まっていると、その旨を警告します。
プロジェクトが 2 つ以上あるときは先頭に絞り込みが出ます。

閲覧用トークンを特定のプロジェクトに限定するには、`config.yaml` に書きます
（未設定・空なら制限なし）。

```yaml
web:
  viewer_projects: [ALPHA, BETA]   # 閲覧用トークンが見てよいプロジェクト
```

限定された閲覧者には、そのプロジェクトのタスク・警告・担当の提案・判断だけが返ります。
メンバーの負荷・学習した補正・高リスク時の応答・ほかのプロジェクトの一覧は返りません。
範囲外のプロジェクトを指定すると 403 です。実行用トークンは制限されません。

With `web.viewer_projects` set, the viewer token sees only those projects'
tasks, alerts, proposals and decisions — no member load, learned adjustments,
responses or other projects — and naming a project outside the scope is a 403.
The operator token is never confined.

**PMO Core** — at the top, only where a ledger written by `aipmo schedule`
exists. Overall level, alerts, assignment proposals, priorities (tap for the
score breakdown), member load, learned adjustments and recent decisions. Each
proposal has a confirm button that appears **only with the operator token**
(the server also answers 403 to a viewer); when the task's own tracker (Jira,
GitHub Projects, Plane, OpenProject, Azure DevOps) has an adapter and the
member's account there is configured (`pmo_core.members[].accounts`), it updates
the tracker's assignee too — and never by guessing a name. A tracker that does
not take the assignee is an error, not a success. The screen only reads: it never runs a cycle, notifies or
launches templates. If the resident side has not updated for 15 minutes or
more, it says so.

左上の丸は接続状態です。緑なら外部ツールが応答しています。
The dot in the header is connection state; green means the adapters answered.

画面に戻ったときだけ更新します。定期的な通信は電池を消費するので行いません。
The screen refreshes when you return to it. It does not poll on a timer, which
would drain the battery.

---

## 困ったときは / When it does not work

**実行しようとすると拒否される / It says the token cannot run**
閲覧用の URL を開いています。実行用の URL を使ってください。
You opened the viewer URL; use the operator one.

**スマホから開けない / Cannot reach it from the phone**
`--host 0.0.0.0` で起動しているか、両方の端末が同じ Wi-Fi につながっているかを
確認してください。PC のファイアウォールがポートを塞いでいることもあります。

Check that you started with `--host 0.0.0.0`, that both devices are on the same
Wi-Fi, and that the machine's firewall is not blocking the port.

**「アクセスキーが必要です」と出る / It asks for an access key**
サーバー側で `aipmo serve` を実行し直すと、URL が再表示されます。
Run `aipmo serve` again on the server to print the URL.

**`aipmo[web]` が必要と言われる / It says extra packages are needed**
```bash
pip install "aipmo[web]"
```

---

### 他言語の要約 / Summary in other languages

**中文**：Web 服务器和 AI 服务器**部署在哪里由使用者决定**；这款软件只提供监听端，公开范围、URL、端口都由配置指定。

**한국어**：웹 서버와 AI 서버 **어디서 돌릴지는 사용자가 정합니다.** 이 소프트웨어는 수신 측만 제공하며 공개 범위·URL·포트는 설정으로 지정합니다.

**Español**：Tanto el servidor web como el servidor de IA **se ejecutan donde el usuario decida**; este software solo provee el extremo que escucha, y la exposición, la URL y el puerto se definen por configuración.

**Français**：Le serveur web comme le serveur IA **s'exécutent où l'utilisateur le décide** ; ce logiciel ne fournit que l'écoute, l'exposition, l'URL et le port étant définis par configuration.

**Deutsch**：Sowohl der Webserver als auch der KI-Server **laufen dort, wo der Nutzer es entscheidet**; diese Software stellt nur den Listener bereit, Sichtbarkeit, URL und Port werden per Konfiguration festgelegt.

**Português**：Tanto o servidor web quanto o servidor de IA **rodam onde o usuário decidir**; este software fornece apenas o lado que escuta, e a exposição, URL e porta são definidas por configuração.

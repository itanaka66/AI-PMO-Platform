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

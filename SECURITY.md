# セキュリティ / Security

## 脆弱性の報告 / Reporting a vulnerability

**公開の Issue や PR には書かないでください。** 修正が出る前に悪用の手がかりを
公開してしまうことになります。

**Do not open a public issue or PR.** Doing so publishes exploitation details
before a fix exists.

代わりに GitHub の Private vulnerability reporting を使ってください:
[Security タブ → "Report a vulnerability"](https://github.com/itanaka66/AI-PMO-Platform/security/advisories/new)。
報告はメンテナだけに届き、修正が済むまで非公開のまま扱われます。

Instead, use GitHub's private vulnerability reporting:
[Security tab → "Report a vulnerability"](https://github.com/itanaka66/AI-PMO-Platform/security/advisories/new).
Reports go only to the maintainer and stay private until a fix is ready.

再現手順・影響範囲・分かっていれば該当ファイルを含めていただけると、
確認が早くなります。

Include reproduction steps, impact, and — if known — the affected file(s);
this speeds up triage.

---

## 対象 / Scope

このリポジトリのコード（`aipmo/` 以下のエンジン・アダプタ・DSL・CLI・Web 画面）
と、配布しているインストーラ・Docker 構成が対象です。

In scope: the code in this repository (the engine, adapters, DSL, CLI, and
web UI under `aipmo/`) and the distributed installers and Docker setup.

テンプレートは第三者が書いて配布される前提で設計されています — 生 SQL を
書けない、コレクション名を指定できない、道具の一覧を必ず明示するなど、
配布テンプレートが越境できないことを前提とした設計判断が
[README の「設計上の判断」](README.md#設計上の判断--design-decisions) に
まとまっています。この境界を破れる入力が見つかった場合は、まさに
報告してほしい種類の問題です。

Templates are designed to be authored by third parties and distributed —
raw SQL cannot be written, a collection name cannot be named directly, the
tool list must always be explicit, and so on. These boundary decisions are
written up in
[README's "Design decisions"](README.md#設計上の判断--design-decisions).
An input that breaks through one of them is exactly the kind of issue worth
reporting.

---

## この基盤の防御 / What this platform already checks for

自動化されている確認 — 新しい変更のたびに動きます。

Automated checks that run on every change:

| | |
|---|---|
| [.github/workflows/tests.yml](.github/workflows/tests.yml) | push・PR のたびにテストスイートを実行 |
| [.github/workflows/codeql.yml](.github/workflows/codeql.yml) | push・PR・週次で CodeQL による静的解析 |
| [.github/dependabot.yml](.github/dependabot.yml) | 依存ライブラリの更新を週次で提案 |
| Secret scanning + push protection | シークレットを含む push をブロック |

| | |
|---|---|
| [.github/workflows/tests.yml](.github/workflows/tests.yml) | runs the test suite on every push and PR |
| [.github/workflows/codeql.yml](.github/workflows/codeql.yml) | static analysis via CodeQL on push, PR, and weekly |
| [.github/dependabot.yml](.github/dependabot.yml) | proposes dependency updates weekly |
| Secret scanning + push protection | blocks a push that contains a secret |

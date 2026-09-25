# ライセンスについて / Licensing

本体は **GNU General Public License v3.0 以降（GPL-3.0-or-later）** です。
全文は [LICENSE](LICENSE) にあります。

This software is released under the **GNU General Public License v3.0 or
later (GPL-3.0-or-later)**; the full text is in [LICENSE](LICENSE).

商用利用・改変・再配布は自由です。ただしコピーレフト——改変・再配布した
バージョンも同じ GPL-3.0 で公開し、ソースコードを添えて提供する必要が
あります。**クローズドソース製品に組み込むことはできません。**

Commercial use, modification and redistribution are all permitted. It is
copyleft, though: a modified or redistributed version must also be released
under GPL-3.0, with its source made available. **It cannot be incorporated
into a closed-source product.**

---

## 何が含まれるか / What it covers

GPL-3.0 License は、このリポジトリに含まれるものすべてに適用されます。
コードだけでなく、**テンプレート（`templates/`）とプロンプト（`prompts/`）も
同じ条件で自由に使われます。**

The licence covers everything in this repository — not only the code, but the
templates and prompts as well.

**このリポジトリにあるものは、すべて無料です。** テンプレートもプロンプトも、
制限付きの版や機能を落とした版ではありません。使うために支払うものはありません。

**Everything in this repository is free.** The templates and prompts are not
reduced or time-limited versions; there is nothing here that costs money to use.

有償の教材として販売するテンプレートは、**このリポジトリには置きません。**
GPL-3.0 License はここに入れたものすべてに及ぶため、置いた時点で誰でも
無料で使え、ソース付きでの再配布もできるようになります。無料と有償の境界は、
リポジトリの境界です。

Templates sold as teaching material are **kept out of this repository**. The
GPL-3.0 licence reaches everything placed here, so anything added becomes free
for anyone to use and redistribute (with source). The boundary between free
and paid is the repository boundary itself.

---

## 依存ライブラリ / Dependencies

依存ライブラリはそれぞれのライセンスに従います。本体の GPL-3.0 License は
それらには及びません——いずれも寛容型ライセンス（MIT・Apache-2.0・
BSD-3-Clause）か LGPL-3.0 で、GPL-3.0 と組み合わせて配布できます。

Dependencies carry their own licences; the GPL-3.0 licence here does not
extend to them. All of them are either permissive (MIT, Apache-2.0,
BSD-3-Clause) or LGPL-3.0, so all are compatible with distributing this
project under GPL-3.0.

| ライブラリ | ライセンス | 用途 |
|---|---|---|
| PyYAML | MIT | テンプレートの読み込み |
| openai | Apache-2.0 | クラウド AI（`[cloud]`） |
| anthropic | MIT | クラウド AI・Claude（`[cloud]`） |
| psycopg | LGPL-3.0 | PostgreSQL（`[data]`） |
| qdrant-client | Apache-2.0 | ベクトル検索・Qdrant（`[data]`） |
| pgvector | MIT | ベクトル検索・pgvector（`[vector-pgvector]`） |
| chromadb | Apache-2.0 | ベクトル検索・Chroma（`[vector-chroma]`） |
| pymilvus | Apache-2.0 | ベクトル検索・Milvus（`[vector-milvus]`） |
| weaviate-client | BSD-3-Clause | ベクトル検索・Weaviate（`[vector-weaviate]`） |
| FastAPI | MIT | Web 画面（`[web]`） |
| uvicorn | BSD-3-Clause | Web 画面（`[web]`） |

いずれも既定の導入には含まれません。基盤のみ (`pip install aipmo`) の依存は
PyYAML だけです。

None are part of the base install; `pip install aipmo` depends only on PyYAML.

> **psycopg は LGPL** です。動的リンクでの利用は制約になりませんが、
> 改変して再配布する場合は条件があります。PostgreSQL 連携を使わない構成
> （`[data]` を入れない）なら関係しません。
>
> psycopg is LGPL. Using it as a dependency is unproblematic; modifying and
> redistributing it is where conditions apply. Deployments without the data
> extras never pull it in.

外部サービス（OpenAI、Microsoft Graph、Atlassian、Slack など）の利用は、
それぞれの利用規約に従います。

Use of the external services follows their own terms.

---

## 著作権者 / Copyright holder

Copyright (C) 2026 株式会社エージーネディア / agNedia Inc.

有償サービスを提供する法人が権利を保有しています。individual から法人へ
移す手続きは要りません。GPL-3.0 の全文（[LICENSE](LICENSE)）はライセンス
文書そのものなので改変せず、著作権表示はこの NOTICE.md 側に置いています。

The corporation that provides the paid offerings holds the rights, so no
transfer from an individual is needed later. LICENSE holds the GPL-3.0 text
verbatim (the license document itself is not to be altered); this project's
own copyright notice lives here in NOTICE.md instead.

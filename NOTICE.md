# ライセンスについて / Licensing

本体は **MIT License** です。全文は [LICENSE](LICENSE) にあります。

This software is released under the **MIT License**; the full text is in
[LICENSE](LICENSE).

商用利用・改変・再配布・クローズドソース製品への組み込みが可能です。
著作権表示とライセンス文の同梱だけが条件です。

Commercial use, modification, redistribution and inclusion in closed-source
products are all permitted. The only condition is that the copyright notice and
licence text travel with it.

---

## 何が含まれるか / What it covers

MIT License は、このリポジトリに含まれるものすべてに適用されます。
コードだけでなく、**テンプレート（`templates/`）とプロンプト（`prompts/`）も
同じ条件で自由に使われます。**

The licence covers everything in this repository — not only the code, but the
templates and prompts as well.

**このリポジトリにあるものは、すべて無料です。** テンプレートもプロンプトも、
制限付きの版や機能を落とした版ではありません。使うために支払うものはありません。

**Everything in this repository is free.** The templates and prompts are not
reduced or time-limited versions; there is nothing here that costs money to use.

有償の教材として販売するテンプレートは、**このリポジトリには置きません。**
MIT License はここに入れたものすべてに及ぶため、置いた時点で誰でも無料で
使え、再販もできるようになります。無料と有償の境界は、リポジトリの境界です。

Templates sold as teaching material are **kept out of this repository**. The MIT
licence reaches everything placed here, so anything added becomes free for
anyone to use and to resell. The boundary between free and paid is the
repository boundary itself.

---

## 依存ライブラリ / Dependencies

依存ライブラリはそれぞれのライセンスに従います。本体の MIT License は
それらには及びません。

Dependencies carry their own licences; the MIT licence here does not extend to
them.

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

株式会社エージーネディア / agNedia Inc.

有償サービスを提供する法人が権利を保有しています。individual から法人へ
移す手続きは要りません。

The corporation that provides the paid offerings holds the rights, so no
transfer from an individual is needed later.

# はじめてのガイド / Getting started

| | | |
|---|---|---|
| [日本語](ja.md) | [English](en.md) | [简体中文](zh.md) |
| [한국어](ko.md) | [Español](es.md) | [Français](fr.md) |
| [Deutsch](de.md) | [Português (BR)](pt.md) | |

---

## 翻訳について / About the translations

**原本は `ja.md` と `en.md` です。** 他の言語はその訳です。
内容を変えるときは、まず原本を直してください。

`ja.md` and `en.md` are the source of truth; the rest are translations of them.
Change the source first.

見出しの構成は全言語で揃えてあります。`tests/test_guides.py` が
これを検査するので、片方だけ節を足すとテストが落ちます。
**8言語ぶんの食い違いは、放っておくと誰も気づきません。**

The section structure is kept identical across all languages, and
`tests/test_guides.py` enforces it: adding a section to one file alone fails
the suite. Divergence across eight files is otherwise something nobody notices.

画面に出る文言は `aipmo/i18n.py` にあります。ガイドだけ訳して
ウィザードが英語のままだと、案内の途中で言語が切り替わります。
両方を揃えてください。

The strings shown on screen live in `aipmo/i18n.py`. Translating the guide but
leaving the wizard in English switches language midway through the very
walkthrough the reader is following. Keep both in step.

## 言語を足すには / Adding a language

1. `en.md` を訳して `<コード>.md` を置く
2. `aipmo/i18n.py` の `CATALOG` と `LANGUAGES` に追加する
3. この表に行を足す
4. `pytest` を走らせる。抜けはテストが指摘します

"""多言語メッセージ / Message catalogue.

セットアップウィザードの文言をここに集約する。
ガイドだけ翻訳してウィザードが英語のままだと、案内の途中で言語が切り替わり、
初心者がそこで止まる。表示される文字列は全部ここを通す。

All wizard strings live here. Translating the guide but leaving the wizard in
English breaks the hand-off exactly where a beginner is least able to recover,
so every string the user sees goes through this module.

新しい言語を足すとき / To add a language:
  1. CATALOG に言語コードのエントリを追加する
  2. pytest を走らせる。キーの過不足はテストが検出する
  1. Add an entry keyed by language code.
  2. Run pytest — missing or extra keys are caught by the test suite.
"""
from __future__ import annotations

import os

DEFAULT_LANG = "en"

# 言語コード → 表示名 / language code to display name
LANGUAGES = {
    "ja": "日本語",
    "en": "English",
    "zh": "简体中文",
    "ko": "한국어",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português (Brasil)",
}

CATALOG: dict[str, dict[str, str]] = {
    "ja": {
        "title": "AI-PMO セットアップ",
        "intro": "いくつか質問します。分からない項目は Enter で既定値になります。",
        "overwrite": "config.yaml が既にあります。上書きしますか？ [y/N]: ",
        "cancelled": "中止しました。",
        "q_mode": "1) AI をどこで動かしますか？",
        "mode_cloud": "[1] クラウド (OpenAI) — ノート PC 向け",
        "mode_local": "[2] ローカル (Ollama) — GPU 搭載機・Docker 向け",
        "choose": "   選択 [1]: ",
        "q_key": "2) OpenAI の API キーを入力してください。",
        "key_hidden": "   入力内容は画面に表示されません。貼り付けて Enter を押してください。",
        "key_where": "   取得先: https://platform.openai.com/api-keys",
        "key_prompt": "   API キー: ",
        "q_tenant": "3) 組織を識別する名前を入力してください。",
        "tenant_rule": "   英小文字・数字・アンダースコアのみ。",
        "tenant_prompt": "   名前 [my_company]: ",
        "q_data": "4) データベース連携を使いますか？",
        "data_note": "   PostgreSQL と Qdrant が必要です。分からなければ N で構いません。",
        "data_prompt": "   使う？ [y/N]: ",
        "done": "完了しました。",
        "next_try": "次のコマンドで試せます:",
        "pull_models": "Ollama のモデル取得が必要です:",
        "key_stored": "API キーは .env に保存しました。本人だけが読める設定です。",
        "err_mode": "mode は cloud か local を指定してください",
        "err_tenant": "名前は英小文字・数字・アンダースコアで 2〜31 文字にしてください",
        "err_key": "クラウド構成には API キーが必要です",
        "err_perms": ".env のアクセス権を制限できませんでした。手動で確認してください",
        "role_operator": "実行できる人へ / can run:",
        "role_viewer": "見るだけの人へ / view only:",
        "web_view_only": "このトークンでは実行できません",
        "q_provider": "AI の提供元を選んでください。",
        "no_embeddings": "（埋め込み非対応・別途 OpenAI の鍵が要ります）",
        "web_templates": "テンプレート",
        "web_runs": "実行",
        "web_running": "実行中…",
        "web_run_done": "完了しました",
        "web_run_failed": "実行に失敗しました",
        "web_no_runs": "まだ何も実行していません。上のテンプレートを押してください。",
        "web_no_templates": "テンプレートが見つかりません。",
        "serve_ready": "スマホからこの URL を開いてください:",
        "serve_local_only": "この URL はこの端末からしか開けません。スマホから使うには host を変更してください。",
        "serve_exposed": "外部に公開しています。TLS はリバースプロキシで用意してください。",
    },
    "en": {
        "title": "AI-PMO setup",
        "intro": "A few questions. Press Enter to accept the default.",
        "overwrite": "config.yaml already exists. Overwrite? [y/N]: ",
        "cancelled": "Cancelled.",
        "q_mode": "1) Where should the AI run?",
        "mode_cloud": "[1] Cloud (OpenAI) — best for laptops",
        "mode_local": "[2] Local (Ollama) — needs a GPU or Docker",
        "choose": "   Choose [1]: ",
        "q_key": "2) Enter your OpenAI API key.",
        "key_hidden": "   Your input stays hidden. Paste it and press Enter.",
        "key_where": "   Get one at: https://platform.openai.com/api-keys",
        "key_prompt": "   API key: ",
        "q_tenant": "3) Enter a short name identifying your organization.",
        "tenant_rule": "   Lowercase letters, digits and underscore only.",
        "tenant_prompt": "   Name [my_company]: ",
        "q_data": "4) Enable the database layer?",
        "data_note": "   Requires PostgreSQL and Qdrant. Answer N if you are unsure.",
        "data_prompt": "   Enable? [y/N]: ",
        "done": "Done.",
        "next_try": "Try it with:",
        "pull_models": "You still need to pull the Ollama models:",
        "key_stored": "Your API key was saved to .env, readable only by your account.",
        "err_mode": "mode must be either cloud or local",
        "err_tenant": "the name must be 2-31 characters of lowercase letters, digits or underscore",
        "err_key": "the cloud setup requires an API key",
        "err_perms": "could not restrict permissions on .env; please check it manually",
        "role_operator": "For people who may run things:",
        "role_viewer": "For people who may only look:",
        "web_view_only": "This token can view but not run",
        "q_provider": "Choose an AI provider.",
        "no_embeddings": " (no embeddings — needs a separate OpenAI key)",
        "web_templates": "Templates",
        "web_runs": "Runs",
        "web_running": "Running…",
        "web_run_done": "Finished",
        "web_run_failed": "Run failed",
        "web_no_runs": "Nothing has run yet. Tap a template above.",
        "web_no_templates": "No templates found.",
        "serve_ready": "Open this URL on your phone:",
        "serve_local_only": "This URL only works on this machine. Change host to reach it from a phone.",
        "serve_exposed": "Exposed beyond this machine. Provide TLS with a reverse proxy.",
    },
    "zh": {
        "title": "AI-PMO 安装向导",
        "intro": "先问几个问题。不清楚的项目直接按回车即可使用默认值。",
        "overwrite": "config.yaml 已存在。要覆盖吗？[y/N]: ",
        "cancelled": "已取消。",
        "q_mode": "1) AI 在哪里运行？",
        "mode_cloud": "[1] 云端 (OpenAI) — 适合笔记本电脑",
        "mode_local": "[2] 本地 (Ollama) — 需要 GPU 或 Docker",
        "choose": "   请选择 [1]: ",
        "q_key": "2) 请输入你的 OpenAI API 密钥。",
        "key_hidden": "   输入内容不会显示在屏幕上。粘贴后按回车。",
        "key_where": "   获取地址: https://platform.openai.com/api-keys",
        "key_prompt": "   API 密钥: ",
        "q_tenant": "3) 请输入用于标识你所在组织的名称。",
        "tenant_rule": "   仅限小写字母、数字和下划线。",
        "tenant_prompt": "   名称 [my_company]: ",
        "q_data": "4) 是否启用数据库功能？",
        "data_note": "   需要 PostgreSQL 和 Qdrant。不确定就选 N。",
        "data_prompt": "   启用？[y/N]: ",
        "done": "完成。",
        "next_try": "可以这样试用:",
        "pull_models": "还需要下载 Ollama 模型:",
        "key_stored": "API 密钥已保存到 .env，仅你本人可读。",
        "err_mode": "mode 只能是 cloud 或 local",
        "err_tenant": "名称需为 2-31 个字符，仅限小写字母、数字和下划线",
        "err_key": "云端配置需要 API 密钥",
        "err_perms": "无法限制 .env 的访问权限，请手动确认",
        "role_operator": "给可以执行的人:",
        "role_viewer": "给只能查看的人:",
        "web_view_only": "此密钥只能查看，不能执行",
        "q_provider": "请选择 AI 提供方。",
        "no_embeddings": "（不支持嵌入，需另配 OpenAI 密钥）",
        "web_templates": "模板",
        "web_runs": "运行记录",
        "web_running": "运行中…",
        "web_run_done": "已完成",
        "web_run_failed": "运行失败",
        "web_no_runs": "还没有运行过。点击上方的模板。",
        "web_no_templates": "未找到模板。",
        "serve_ready": "请在手机上打开此网址:",
        "serve_local_only": "该网址仅限本机访问。要从手机访问，请修改 host。",
        "serve_exposed": "已对外开放。请通过反向代理提供 TLS。",
    },
    "ko": {
        "title": "AI-PMO 설정",
        "intro": "몇 가지만 물어봅니다. 모르는 항목은 Enter 를 누르면 기본값이 됩니다.",
        "overwrite": "config.yaml 이 이미 있습니다. 덮어쓸까요? [y/N]: ",
        "cancelled": "취소했습니다.",
        "q_mode": "1) AI 를 어디에서 실행할까요?",
        "mode_cloud": "[1] 클라우드 (OpenAI) — 노트북에 적합",
        "mode_local": "[2] 로컬 (Ollama) — GPU 또는 Docker 필요",
        "choose": "   선택 [1]: ",
        "q_key": "2) OpenAI API 키를 입력하세요.",
        "key_hidden": "   입력한 내용은 화면에 표시되지 않습니다. 붙여넣고 Enter 를 누르세요.",
        "key_where": "   발급처: https://platform.openai.com/api-keys",
        "key_prompt": "   API 키: ",
        "q_tenant": "3) 조직을 구분할 이름을 입력하세요.",
        "tenant_rule": "   영소문자, 숫자, 밑줄만 사용할 수 있습니다.",
        "tenant_prompt": "   이름 [my_company]: ",
        "q_data": "4) 데이터베이스 연동을 사용할까요?",
        "data_note": "   PostgreSQL 과 Qdrant 가 필요합니다. 모르겠으면 N 을 선택하세요.",
        "data_prompt": "   사용? [y/N]: ",
        "done": "완료했습니다.",
        "next_try": "다음 명령으로 시험해 볼 수 있습니다:",
        "pull_models": "Ollama 모델을 내려받아야 합니다:",
        "key_stored": "API 키는 .env 에 저장했습니다. 본인만 읽을 수 있습니다.",
        "err_mode": "mode 는 cloud 또는 local 이어야 합니다",
        "err_tenant": "이름은 영소문자, 숫자, 밑줄로 2~31자여야 합니다",
        "err_key": "클라우드 구성에는 API 키가 필요합니다",
        "err_perms": ".env 의 접근 권한을 제한하지 못했습니다. 직접 확인해 주세요",
        "role_operator": "실행할 수 있는 사람용:",
        "role_viewer": "보기만 하는 사람용:",
        "web_view_only": "이 토큰으로는 실행할 수 없습니다",
        "q_provider": "AI 제공자를 선택하세요.",
        "no_embeddings": " (임베딩 미지원 — OpenAI 키가 별도로 필요)",
        "web_templates": "템플릿",
        "web_runs": "실행 기록",
        "web_running": "실행 중…",
        "web_run_done": "완료했습니다",
        "web_run_failed": "실행에 실패했습니다",
        "web_no_runs": "아직 실행한 적이 없습니다. 위 템플릿을 누르세요.",
        "web_no_templates": "템플릿을 찾을 수 없습니다.",
        "serve_ready": "휴대폰에서 이 주소를 여세요:",
        "serve_local_only": "이 주소는 이 기기에서만 열립니다. 휴대폰에서 쓰려면 host 를 바꾸세요.",
        "serve_exposed": "외부에 공개되어 있습니다. TLS 는 리버스 프록시로 준비하세요.",
    },
    "es": {
        "title": "Configuración de AI-PMO",
        "intro": "Unas pocas preguntas. Pulsa Enter para aceptar el valor predeterminado.",
        "overwrite": "config.yaml ya existe. ¿Sobrescribir? [y/N]: ",
        "cancelled": "Cancelado.",
        "q_mode": "1) ¿Dónde debe ejecutarse la IA?",
        "mode_cloud": "[1] Nube (OpenAI) — recomendado para portátiles",
        "mode_local": "[2] Local (Ollama) — requiere GPU o Docker",
        "choose": "   Elige [1]: ",
        "q_key": "2) Introduce tu clave de API de OpenAI.",
        "key_hidden": "   Lo que escribas no aparecerá en pantalla. Pégala y pulsa Enter.",
        "key_where": "   Consíguela en: https://platform.openai.com/api-keys",
        "key_prompt": "   Clave de API: ",
        "q_tenant": "3) Introduce un nombre corto que identifique tu organización.",
        "tenant_rule": "   Solo minúsculas, dígitos y guion bajo.",
        "tenant_prompt": "   Nombre [my_company]: ",
        "q_data": "4) ¿Activar la capa de base de datos?",
        "data_note": "   Requiere PostgreSQL y Qdrant. Si no lo tienes claro, responde N.",
        "data_prompt": "   ¿Activar? [y/N]: ",
        "done": "Listo.",
        "next_try": "Pruébalo con:",
        "pull_models": "Todavía tienes que descargar los modelos de Ollama:",
        "key_stored": "Tu clave se guardó en .env, legible solo por tu cuenta.",
        "err_mode": "mode debe ser cloud o local",
        "err_tenant": "el nombre debe tener entre 2 y 31 caracteres de minúsculas, dígitos o guion bajo",
        "err_key": "la configuración en la nube requiere una clave de API",
        "err_perms": "no se pudieron restringir los permisos de .env; revísalo manualmente",
        "role_operator": "Para quien puede ejecutar:",
        "role_viewer": "Para quien solo mira:",
        "web_view_only": "Esta clave permite ver, no ejecutar",
        "q_provider": "Elige un proveedor de IA.",
        "no_embeddings": " (sin embeddings — necesita una clave de OpenAI aparte)",
        "web_templates": "Plantillas",
        "web_runs": "Ejecuciones",
        "web_running": "Ejecutando…",
        "web_run_done": "Terminado",
        "web_run_failed": "La ejecución falló",
        "web_no_runs": "Aún no se ha ejecutado nada. Toca una plantilla arriba.",
        "web_no_templates": "No se encontraron plantillas.",
        "serve_ready": "Abre esta dirección en tu teléfono:",
        "serve_local_only": "Esta dirección solo funciona en este equipo. Cambia host para llegar desde un teléfono.",
        "serve_exposed": "Expuesto fuera de este equipo. Provee TLS con un proxy inverso.",
    },
    "fr": {
        "title": "Configuration d'AI-PMO",
        "intro": "Quelques questions. Appuyez sur Entrée pour accepter la valeur par défaut.",
        "overwrite": "config.yaml existe déjà. L'écraser ? [y/N] : ",
        "cancelled": "Annulé.",
        "q_mode": "1) Où l'IA doit-elle s'exécuter ?",
        "mode_cloud": "[1] Cloud (OpenAI) — recommandé pour un ordinateur portable",
        "mode_local": "[2] Local (Ollama) — nécessite un GPU ou Docker",
        "choose": "   Choix [1] : ",
        "q_key": "2) Saisissez votre clé d'API OpenAI.",
        "key_hidden": "   La saisie reste masquée. Collez-la puis appuyez sur Entrée.",
        "key_where": "   Obtenez-en une sur : https://platform.openai.com/api-keys",
        "key_prompt": "   Clé d'API : ",
        "q_tenant": "3) Saisissez un nom court identifiant votre organisation.",
        "tenant_rule": "   Minuscules, chiffres et tiret bas uniquement.",
        "tenant_prompt": "   Nom [my_company] : ",
        "q_data": "4) Activer la couche base de données ?",
        "data_note": "   Nécessite PostgreSQL et Qdrant. En cas de doute, répondez N.",
        "data_prompt": "   Activer ? [y/N] : ",
        "done": "Terminé.",
        "next_try": "Essayez avec :",
        "pull_models": "Il reste à télécharger les modèles Ollama :",
        "key_stored": "Votre clé a été enregistrée dans .env, lisible par vous seul.",
        "err_mode": "mode doit valoir cloud ou local",
        "err_tenant": "le nom doit comporter 2 à 31 caractères en minuscules, chiffres ou tiret bas",
        "err_key": "la configuration cloud nécessite une clé d'API",
        "err_perms": "impossible de restreindre les droits sur .env ; vérifiez-le manuellement",
        "role_operator": "Pour ceux qui peuvent exécuter :",
        "role_viewer": "Pour ceux qui consultent seulement :",
        "web_view_only": "Cette clé permet de consulter, pas d'exécuter",
        "q_provider": "Choisissez un fournisseur d'IA.",
        "no_embeddings": " (pas d'embeddings — nécessite une clé OpenAI distincte)",
        "web_templates": "Modèles",
        "web_runs": "Exécutions",
        "web_running": "Exécution…",
        "web_run_done": "Terminé",
        "web_run_failed": "L'exécution a échoué",
        "web_no_runs": "Rien n'a encore été exécuté. Touchez un modèle ci-dessus.",
        "web_no_templates": "Aucun modèle trouvé.",
        "serve_ready": "Ouvrez cette adresse sur votre téléphone :",
        "serve_local_only": "Cette adresse ne fonctionne que sur cette machine. Modifiez host pour y accéder depuis un téléphone.",
        "serve_exposed": "Exposé au-delà de cette machine. Fournissez TLS via un proxy inverse.",
    },
    "de": {
        "title": "AI-PMO Einrichtung",
        "intro": "Ein paar Fragen. Mit Enter wird der Standardwert übernommen.",
        "overwrite": "config.yaml existiert bereits. Überschreiben? [y/N]: ",
        "cancelled": "Abgebrochen.",
        "q_mode": "1) Wo soll die KI laufen?",
        "mode_cloud": "[1] Cloud (OpenAI) — für Notebooks empfohlen",
        "mode_local": "[2] Lokal (Ollama) — benötigt GPU oder Docker",
        "choose": "   Auswahl [1]: ",
        "q_key": "2) Geben Sie Ihren OpenAI-API-Schlüssel ein.",
        "key_hidden": "   Die Eingabe bleibt verborgen. Einfügen und Enter drücken.",
        "key_where": "   Erhältlich unter: https://platform.openai.com/api-keys",
        "key_prompt": "   API-Schlüssel: ",
        "q_tenant": "3) Geben Sie einen kurzen Namen für Ihre Organisation ein.",
        "tenant_rule": "   Nur Kleinbuchstaben, Ziffern und Unterstrich.",
        "tenant_prompt": "   Name [my_company]: ",
        "q_data": "4) Datenbankanbindung aktivieren?",        "data_note": "   Benötigt PostgreSQL und Qdrant. Im Zweifel N wählen.",
        "data_prompt": "   Aktivieren? [y/N]: ",
        "done": "Fertig.",
        "next_try": "Probieren Sie es mit:",
        "pull_models": "Die Ollama-Modelle müssen noch geladen werden:",
        "key_stored": "Ihr Schlüssel wurde in .env gespeichert, nur für Ihr Konto lesbar.",
        "err_mode": "mode muss cloud oder local sein",
        "err_tenant": "der Name muss 2-31 Zeichen aus Kleinbuchstaben, Ziffern oder Unterstrich haben",
        "err_key": "die Cloud-Einrichtung benötigt einen API-Schlüssel",
        "err_perms": "Zugriffsrechte für .env konnten nicht eingeschränkt werden; bitte manuell prüfen",
        "role_operator": "Für Personen, die ausführen dürfen:",
        "role_viewer": "Für Personen, die nur zusehen:",
        "web_view_only": "Dieser Schlüssel darf ansehen, nicht ausführen",
        "q_provider": "Wählen Sie einen KI-Anbieter.",
        "no_embeddings": " (keine Embeddings — separater OpenAI-Schlüssel nötig)",
        "web_templates": "Vorlagen",
        "web_runs": "Ausführungen",
        "web_running": "Läuft…",
        "web_run_done": "Fertig",
        "web_run_failed": "Ausführung fehlgeschlagen",
        "web_no_runs": "Noch nichts ausgeführt. Tippen Sie oben auf eine Vorlage.",
        "web_no_templates": "Keine Vorlagen gefunden.",
        "serve_ready": "Öffnen Sie diese Adresse auf Ihrem Telefon:",
        "serve_local_only": "Diese Adresse funktioniert nur auf diesem Rechner. Ändern Sie host für den Zugriff vom Telefon.",
        "serve_exposed": "Über diesen Rechner hinaus erreichbar. TLS über einen Reverse-Proxy bereitstellen.",
    },
    "pt": {
        "title": "Configuração do AI-PMO",
        "intro": "Algumas perguntas. Pressione Enter para aceitar o padrão.",
        "overwrite": "config.yaml já existe. Sobrescrever? [y/N]: ",
        "cancelled": "Cancelado.",
        "q_mode": "1) Onde a IA deve rodar?",
        "mode_cloud": "[1] Nuvem (OpenAI) — indicado para notebooks",
        "mode_local": "[2] Local (Ollama) — exige GPU ou Docker",
        "choose": "   Escolha [1]: ",
        "q_key": "2) Informe sua chave de API da OpenAI.",
        "key_hidden": "   O que você digitar não aparece na tela. Cole e pressione Enter.",
        "key_where": "   Obtenha uma em: https://platform.openai.com/api-keys",
        "key_prompt": "   Chave de API: ",
        "q_tenant": "3) Informe um nome curto que identifique sua organização.",
        "tenant_rule": "   Apenas letras minúsculas, dígitos e sublinhado.",
        "tenant_prompt": "   Nome [my_company]: ",
        "q_data": "4) Ativar a camada de banco de dados?",
        "data_note": "   Exige PostgreSQL e Qdrant. Na dúvida, responda N.",
        "data_prompt": "   Ativar? [y/N]: ",
        "done": "Concluído.",
        "next_try": "Teste com:",
        "pull_models": "Ainda é preciso baixar os modelos do Ollama:",
        "key_stored": "Sua chave foi salva em .env, legível apenas pela sua conta.",
        "err_mode": "mode deve ser cloud ou local",
        "err_tenant": "o nome deve ter de 2 a 31 caracteres em minúsculas, dígitos ou sublinhado",
        "err_key": "a configuração em nuvem exige uma chave de API",
        "err_perms": "não foi possível restringir as permissões do .env; verifique manualmente",
        "role_operator": "Para quem pode executar:",
        "role_viewer": "Para quem apenas observa:",
        "web_view_only": "Esta chave permite ver, não executar",
        "q_provider": "Escolha um provedor de IA.",
        "no_embeddings": " (sem embeddings — exige uma chave OpenAI à parte)",
        "web_templates": "Modelos",
        "web_runs": "Execuções",
        "web_running": "Executando…",
        "web_run_done": "Concluído",
        "web_run_failed": "A execução falhou",
        "web_no_runs": "Nada foi executado ainda. Toque em um modelo acima.",
        "web_no_templates": "Nenhum modelo encontrado.",
        "serve_ready": "Abra este endereço no seu telefone:",
        "serve_local_only": "Este endereço só funciona nesta máquina. Altere host para acessar pelo telefone.",
        "serve_exposed": "Exposto além desta máquina. Forneça TLS com um proxy reverso.",
    },
}


def normalize(tag: str | None) -> str:
    """'ja_JP.UTF-8' や 'zh-Hans-CN' を対応言語コードに落とす。

    Reduce a locale tag such as 'ja_JP.UTF-8' or 'zh-Hans-CN' to a supported code.
    """
    if not tag:
        return DEFAULT_LANG
    base = tag.replace("_", "-").split(".")[0].split("-")[0].lower()
    return base if base in CATALOG else DEFAULT_LANG


def detect() -> str:
    """環境から言語を推定する / infer the language from the environment."""
    explicit = os.environ.get("AIPMO_LANG")
    if explicit:
        return normalize(explicit)

    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var)
        if value and value not in ("C", "POSIX"):
            return normalize(value.split(":")[0])

    if os.name == "nt":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            buffer = ctypes.create_unicode_buffer(85)
            ctypes.windll.kernel32.LCIDToLocaleName(lcid, buffer, 85, 0)
            return normalize(buffer.value)
        except Exception:
            pass

    return DEFAULT_LANG


def translator(lang: str | None = None):
    """メッセージ取得関数を返す / return a lookup function for one language.

    未翻訳のキーは英語にフォールバックする。翻訳の遅れが
    KeyError による異常終了になってはいけない。

    Untranslated keys fall back to English: a lagging translation must never
    turn into a crash.
    """
    code = normalize(lang) if lang else detect()
    primary = CATALOG[code]
    fallback = CATALOG[DEFAULT_LANG]

    def t(key: str) -> str:
        return primary.get(key) or fallback[key]

    return t

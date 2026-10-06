# 実サービス（Jira・Plane・OpenProject）につなぐ確認 / Verifying against real trackers

この環境（開発・CI）には実サービスの接続先も認証情報も無いので、実サービスに対する確認は**手元で行う**。
そのための道具を 3 つ用意した。**どれも、書き込みは明示したときだけ**。

## 1. 接続の診断（読み取りだけ）

```bash
aipmo --config config.yaml integrations            # 設定したアダプタ全部
aipmo --config config.yaml integrations plane      # 1 つだけ
```

次の順に確かめ、止まった所と原因を出す（終了コードは、1 つでも失敗すれば 1）。

| 段階 | 内容 | 失敗のときに疑うところ |
|---|---|---|
| `health` | アダプタの疎通確認 | URL・認証・プロジェクト／ワークスペースの指定 |
| `read` | 課題を 1 件だけ検索する | 読み取りの権限、JQL などの検索の書き方 |
| `people` | 担当候補の一覧（Plane・OpenProject） | メンバー一覧を読む権限（担当者の引き当てに必要） |

原因は `auth`（401/403）・`not_found`（404）・`timeout`・`network`・`rate_limit`・`other` に分類する。
エラー文からは、設定のトークン・API キーを伏せる。**書き込みのアクションは呼ばない。**

同じ診断は、Web 画面の「連携」の **接続を確かめる** ボタン（operator のみ、5 秒に 1 回まで。`POST /api/integrations/check`）でもできる。

## 2. 担当者の引き当ての確認（読み取りだけ）

```bash
aipmo --config config.yaml members --tracker plane
```

メンバーがトラッカーのどのユーザーに当たるかを見る。曖昧・該当なしは、そのまま書き戻すと**書かずに止まる**
（[TICKET-TRACKERS.md](TICKET-TRACKERS.md)）。

## 3. 実サービスに対する自動確認（手元でだけ動く）

[tests/test_live_trackers.py](../tests/test_live_trackers.py) は、環境変数が無ければ飛ばす。

```bash
# 読み取りだけ: 全アダプタの診断と、担当者の引き当て
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# 担当の書き戻しまで（実際の課題の担当を変える。専用のテスト課題で）
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<課題のid>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

書き戻しは「担当を書く → 読み直して付いたことを確かめる → 元の担当に戻す」。Jira は担当者の id、Plane はユーザー id で
元に戻せる。**OpenProject は読み戻した名前しか分からないので元に戻せない**（警告を出す）。未割り当てだった課題も戻せない。
そのため、必ず専用のテスト課題を使うこと。

## 確認の状況（正直に）

- 作り物の HTTP（Jira・Plane・OpenProject の応答の形）では、診断・書き戻し・エラーの分類・秘密の伏せ字・
  「読み取りで書き込みを出さない」を確かめた（`tests/test_probe.py`、`tests/test_plane.py` ほか）。
- **実サービスに対しては、まだ一度も動かしていない。** 実際の応答の細部（権限の範囲、検索の癖、Jira の
  `accountId` の引き当て、OpenProject の `lockVersion` の競合、Plane の API キーの種類）は、上の確認を実環境で
  動かして初めて分かる。動かして出た差は、このファイルの表に足していく。

---

## 中文

由于开发/CI 环境没有真实服务的连接信息和认证信息，针对真实服务的验证需要**在本地进行**。为此准备了 3 个工具，**全部只在明确指定时才会写入**。

### 1. 连接诊断（仅读取）

```bash
aipmo --config config.yaml integrations            # 所有已配置的适配器
aipmo --config config.yaml integrations plane      # 只检查一个
```

按以下顺序检查，并给出卡在哪一步、原因是什么（只要有一项失败，退出码就是 1）。

| 阶段 | 内容 | 失败时应怀疑的地方 |
|---|---|---|
| `health` | 适配器的连通性检查 | URL・认证・项目/工作区的指定 |
| `read` | 只搜索一条问题 | 读取权限、JQL 等搜索语法 |
| `people` | 候选负责人列表（Plane・OpenProject） | 读取成员列表的权限（分配负责人所需） |

原因分为 `auth`（401/403）・`not_found`（404）・`timeout`・`network`・`rate_limit`・`other`。
错误文本中会隐去配置的令牌、API 密钥。**绝不调用写入类操作。**

同样的诊断也可以通过 Web 界面「集成」页的**检查连接**按钮完成（仅限 operator，每 5 秒最多一次，`POST /api/integrations/check`）。

### 2. 负责人映射确认（仅读取）

```bash
aipmo --config config.yaml members --tracker plane
```

查看成员对应到跟踪工具中的哪个用户。歧义或找不到对应用户时，直接写回会**在写入前停止**
（参见 [TICKET-TRACKERS.md](TICKET-TRACKERS.md)）。

### 3. 针对真实服务的自动确认（仅能在本地运行）

[tests/test_live_trackers.py](../tests/test_live_trackers.py) 在没有设置环境变量时会跳过。

```bash
# 只读：所有适配器的诊断 + 负责人映射
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# 包含负责人写回（会实际修改问题的负责人，请使用专用的测试问题）
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<问题id>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

写回流程是「写入负责人 → 重新读取确认已生效 → 恢复为原负责人」。Jira 可用负责人 id、Plane 可用用户 id 恢复。
**OpenProject 只能读回名称，因此无法恢复**（会发出警告）。原本未分配的问题也无法恢复。
因此务必使用专用的测试问题。

### 确认现状（诚实说明）

- 已经用模拟 HTTP（Jira・Plane・OpenProject 响应的形状）验证了诊断・写回・错误分类・密钥隐藏・
  「读取操作不触发写入」（`tests/test_probe.py`、`tests/test_plane.py` 等）。
- **尚未对真实服务运行过一次。** 实际响应的细节（权限范围、搜索的特殊行为、Jira 的
  `accountId` 映射、OpenProject 的 `lockVersion` 冲突、Plane 的 API 密钥种类）只有在真实环境中运行上述确认后才能知道。
  运行后发现的差异会补充到本文件的表格中。

## 한국어

이 환경(개발·CI)에는 실제 서비스 연결 정보도 인증 정보도 없으므로, 실제 서비스에 대한 확인은 **직접 손으로 해야 합니다.** 이를 위한 도구 3가지를 준비했으며, **모두 명시했을 때만 쓰기를 수행합니다.**

### 1. 연결 진단(읽기 전용)

```bash
aipmo --config config.yaml integrations            # 설정된 어댑터 전부
aipmo --config config.yaml integrations plane      # 하나만
```

다음 순서로 확인하여 멈춘 지점과 원인을 알려줍니다(하나라도 실패하면 종료 코드는 1).

| 단계 | 내용 | 실패 시 의심할 곳 |
|---|---|---|
| `health` | 어댑터 연결 확인 | URL·인증·프로젝트/워크스페이스 지정 |
| `read` | 이슈 1건만 검색 | 읽기 권한, JQL 등 검색 문법 |
| `people` | 담당 후보 목록(Plane·OpenProject) | 멤버 목록 읽기 권한(담당자 배정에 필요) |

원인은 `auth`(401/403)·`not_found`(404)·`timeout`·`network`·`rate_limit`·`other`로 분류합니다.
오류 메시지에서는 설정된 토큰·API 키를 가립니다. **쓰기 액션은 절대 호출하지 않습니다.**

같은 진단은 웹 화면 "연동"의 **연결 확인** 버튼(operator 전용, 5초에 1회까지, `POST /api/integrations/check`)으로도 할 수 있습니다.

### 2. 담당자 매핑 확인(읽기 전용)

```bash
aipmo --config config.yaml members --tracker plane
```

멤버가 트래커의 어느 사용자에 해당하는지 확인합니다. 모호하거나 해당자가 없으면 그대로 써 보내도
**쓰지 않고 멈춥니다**([TICKET-TRACKERS.md](TICKET-TRACKERS.md) 참고).

### 3. 실제 서비스에 대한 자동 확인(로컬에서만 동작)

[tests/test_live_trackers.py](../tests/test_live_trackers.py)는 환경 변수가 없으면 건너뜁니다.

```bash
# 읽기만: 전체 어댑터 진단과 담당자 매핑
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# 담당자 쓰기까지(실제 이슈의 담당자를 변경합니다. 전용 테스트 이슈를 사용하세요)
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<이슈id>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

쓰기 흐름은 "담당자 쓰기 → 다시 읽어 적용 확인 → 원래 담당자로 복원"입니다. Jira는 담당자 id, Plane은 사용자 id로
복원할 수 있습니다. **OpenProject는 읽어온 이름만 알 수 있어 복원할 수 없습니다**(경고를 표시합니다). 원래 미배정이었던 이슈도 복원할 수 없습니다.
그러므로 반드시 전용 테스트 이슈를 사용하세요.

### 확인 현황(솔직하게)

- 가짜 HTTP(Jira·Plane·OpenProject 응답 형태)로 진단·쓰기·오류 분류·비밀 가리기·
  "읽기에서 쓰기를 내지 않음"을 확인했습니다(`tests/test_probe.py`, `tests/test_plane.py` 등).
- **실제 서비스에 대해서는 아직 한 번도 실행하지 않았습니다.** 실제 응답의 세부 사항(권한 범위, 검색의 특이점, Jira의
  `accountId` 매핑, OpenProject의 `lockVersion` 충돌, Plane의 API 키 종류)은 위 확인을 실제 환경에서
  돌려봐야 알 수 있습니다. 실행해서 나온 차이는 이 파일의 표에 추가해 나갑니다.

## Español

Como este entorno (desarrollo/CI) no tiene destinos ni credenciales de servicios reales, la verificación contra servicios reales debe hacerse **a mano**. Para ello se preparan 3 herramientas, y **todas solo escriben cuando se indica explícitamente.**

### 1. Diagnóstico de conexión (solo lectura)

```bash
aipmo --config config.yaml integrations            # todos los adaptadores configurados
aipmo --config config.yaml integrations plane      # solo uno
```

Comprueba en este orden y muestra dónde se detuvo y por qué (el código de salida es 1 si falla alguno).

| Fase | Contenido | Qué sospechar si falla |
|---|---|---|
| `health` | Comprobación de conectividad del adaptador | URL, autenticación, proyecto/espacio de trabajo indicado |
| `read` | Buscar una sola incidencia | Permiso de lectura, sintaxis de búsqueda como JQL |
| `people` | Lista de candidatos a responsable (Plane, OpenProject) | Permiso para leer la lista de miembros (necesario para asignar responsable) |

Las causas se clasifican como `auth` (401/403), `not_found` (404), `timeout`, `network`, `rate_limit`, `other`.
Los tokens y claves de API configurados se ocultan en los mensajes de error. **Nunca se llama a ninguna acción de escritura.**

El mismo diagnóstico también puede hacerse con el botón **comprobar conexión** de la pantalla "Integraciones" (solo operator, máximo una vez cada 5 segundos, `POST /api/integrations/check`).

### 2. Verificación de la asignación de responsables (solo lectura)

```bash
aipmo --config config.yaml members --tracker plane
```

Muestra a qué usuario del rastreador corresponde cada miembro. Si es ambiguo o no hay coincidencia, escribir tal cual
**se detiene sin escribir** (ver [TICKET-TRACKERS.md](TICKET-TRACKERS.md)).

### 3. Verificación automática contra servicios reales (solo funciona en local)

[tests/test_live_trackers.py](../tests/test_live_trackers.py) se omite si no hay variables de entorno.

```bash
# Solo lectura: diagnóstico de todos los adaptadores y asignación de responsables
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# Incluye la escritura del responsable (cambia el responsable real de una incidencia; usa una incidencia de prueba dedicada)
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<id de la incidencia>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

El flujo de escritura es "escribir el responsable → releer para confirmar que se aplicó → restaurar el responsable original". Jira puede restaurarse con el id del responsable, Plane con el id de usuario.
**OpenProject solo conoce el nombre leído, así que no se puede restaurar** (se muestra una advertencia). Una incidencia que no tenía responsable tampoco puede restaurarse.
Por eso, usa siempre una incidencia de prueba dedicada.

### Estado de verificación (con honestidad)

- Con HTTP simulado (la forma de las respuestas de Jira, Plane, OpenProject) se verificaron el diagnóstico, la escritura, la clasificación de errores, el ocultado de secretos y que "la lectura no dispara escritura" (`tests/test_probe.py`, `tests/test_plane.py`, entre otros).
- **Todavía no se ha ejecutado ni una vez contra un servicio real.** Los detalles de las respuestas reales (alcance de permisos, peculiaridades de búsqueda, la asignación del `accountId` de Jira, los conflictos de `lockVersion` de OpenProject, el tipo de clave de API de Plane) solo se conocerán al ejecutar estas verificaciones en un entorno real. Las diferencias que aparezcan se irán añadiendo a la tabla de este archivo.

## Français

Comme cet environnement (développement/CI) n'a ni cibles ni identifiants de services réels, la vérification contre des services réels doit se faire **à la main**. Trois outils sont prévus à cet effet, et **tous n'écrivent que lorsque c'est explicitement demandé.**

### 1. Diagnostic de connexion (lecture seule)

```bash
aipmo --config config.yaml integrations            # tous les adaptateurs configurés
aipmo --config config.yaml integrations plane      # un seul
```

Vérifie dans cet ordre et indique où cela s'est arrêté et pourquoi (le code de sortie est 1 si un seul échoue).

| Étape | Contenu | À suspecter en cas d'échec |
|---|---|---|
| `health` | Vérification de connectivité de l'adaptateur | URL, authentification, projet/espace de travail indiqué |
| `read` | Rechercher un seul ticket | Permission de lecture, syntaxe de recherche comme JQL |
| `people` | Liste des responsables candidats (Plane, OpenProject) | Permission de lire la liste des membres (nécessaire pour assigner un responsable) |

Les causes sont classées en `auth` (401/403), `not_found` (404), `timeout`, `network`, `rate_limit`, `other`.
Les jetons et clés API configurés sont masqués dans les messages d'erreur. **Aucune action d'écriture n'est jamais appelée.**

Le même diagnostic est aussi accessible via le bouton **vérifier la connexion** de l'écran "Intégrations" (operator uniquement, une fois toutes les 5 secondes maximum, `POST /api/integrations/check`).

### 2. Vérification de l'affectation des responsables (lecture seule)

```bash
aipmo --config config.yaml members --tracker plane
```

Montre à quel utilisateur du gestionnaire de tickets correspond chaque membre. En cas d'ambiguïté ou d'absence de correspondance, écrire tel quel
**s'arrête sans écrire** (voir [TICKET-TRACKERS.md](TICKET-TRACKERS.md)).

### 3. Vérification automatique contre des services réels (ne fonctionne qu'en local)

[tests/test_live_trackers.py](../tests/test_live_trackers.py) est ignoré si les variables d'environnement sont absentes.

```bash
# Lecture seule : diagnostic de tous les adaptateurs et affectation des responsables
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# Jusqu'à l'écriture du responsable (modifie le responsable réel d'un ticket ; utiliser un ticket de test dédié)
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<id du ticket>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

Le flux d'écriture est « écrire le responsable → relire pour confirmer l'application → restaurer le responsable d'origine ». Jira peut être restauré par l'id du responsable, Plane par l'id utilisateur.
**OpenProject ne connaît que le nom relu, donc impossible de restaurer** (un avertissement est émis). Un ticket qui n'avait pas de responsable ne peut pas non plus être restauré.
Utilisez donc toujours un ticket de test dédié.

### État de la vérification (en toute honnêteté)

- Avec du HTTP simulé (la forme des réponses de Jira, Plane, OpenProject), le diagnostic, l'écriture, la classification des erreurs, le masquage des secrets et le fait que « la lecture ne déclenche pas d'écriture » ont été vérifiés (`tests/test_probe.py`, `tests/test_plane.py`, entre autres).
- **Cela n'a encore jamais été exécuté contre un service réel.** Les détails des réponses réelles (étendue des permissions, particularités de recherche, affectation de l'`accountId` de Jira, conflits de `lockVersion` d'OpenProject, type de clé API de Plane) ne seront connus qu'en exécutant ces vérifications en environnement réel. Les écarts constatés seront ajoutés au tableau de ce fichier.

## Deutsch

Da diese Umgebung (Entwicklung/CI) weder Ziele noch Zugangsdaten echter Dienste hat, muss die Prüfung gegen echte Dienste **manuell** erfolgen. Dafür gibt es 3 Werkzeuge, die **alle nur schreiben, wenn es ausdrücklich angegeben wird.**

### 1. Verbindungsdiagnose (nur Lesen)

```bash
aipmo --config config.yaml integrations            # alle konfigurierten Adapter
aipmo --config config.yaml integrations plane      # nur einen
```

Prüft in dieser Reihenfolge und zeigt, wo es hängen blieb und warum (Exit-Code ist 1, wenn auch nur einer fehlschlägt).

| Phase | Inhalt | Bei Fehlschlag zu prüfen |
|---|---|---|
| `health` | Konnektivitätsprüfung des Adapters | URL, Authentifizierung, angegebenes Projekt/Workspace |
| `read` | Nur ein Ticket suchen | Leseberechtigung, Suchsyntax wie JQL |
| `people` | Liste möglicher Zuständiger (Plane, OpenProject) | Berechtigung, die Mitgliederliste zu lesen (für die Zuweisung nötig) |

Ursachen werden als `auth` (401/403), `not_found` (404), `timeout`, `network`, `rate_limit`, `other` klassifiziert.
Konfigurierte Tokens und API-Schlüssel werden in Fehlermeldungen verdeckt. **Es wird niemals eine Schreibaktion aufgerufen.**

Dieselbe Diagnose ist auch über die Schaltfläche **Verbindung prüfen** im Bildschirm „Integrationen" möglich (nur operator, höchstens einmal alle 5 Sekunden, `POST /api/integrations/check`).

### 2. Prüfung der Zuständigen-Zuordnung (nur Lesen)

```bash
aipmo --config config.yaml members --tracker plane
```

Zeigt, welchem Benutzer im Tracker ein Mitglied entspricht. Bei Mehrdeutigkeit oder keiner Entsprechung
**bricht das Zurückschreiben ohne zu schreiben ab** (siehe [TICKET-TRACKERS.md](TICKET-TRACKERS.md)).

### 3. Automatische Prüfung gegen echte Dienste (läuft nur lokal)

[tests/test_live_trackers.py](../tests/test_live_trackers.py) wird übersprungen, wenn die Umgebungsvariablen fehlen.

```bash
# Nur Lesen: Diagnose aller Adapter und Zuständigen-Zuordnung
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# Inklusive Zurückschreiben des Zuständigen (ändert den echten Zuständigen eines Tickets; dediziertes Testticket verwenden)
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<Ticket-ID>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

Der Schreibablauf ist „Zuständigen schreiben → erneut lesen, um die Anwendung zu bestätigen → ursprünglichen Zuständigen wiederherstellen". Jira lässt sich über die Zuständigen-ID wiederherstellen, Plane über die Benutzer-ID.
**OpenProject kennt nur den zurückgelesenen Namen und kann daher nicht wiederherstellen** (eine Warnung wird ausgegeben). Ein ursprünglich nicht zugewiesenes Ticket kann ebenfalls nicht wiederhergestellt werden.
Verwenden Sie deshalb unbedingt ein dediziertes Testticket.

### Prüfstand (ehrlich gesagt)

- Mit simuliertem HTTP (der Form der Antworten von Jira, Plane, OpenProject) wurden Diagnose, Zurückschreiben, Fehlerklassifizierung, Geheimnisverdeckung und „Lesen löst kein Schreiben aus" verifiziert (`tests/test_probe.py`, `tests/test_plane.py` u. a.).
- **Gegen einen echten Dienst wurde dies noch kein einziges Mal ausgeführt.** Details echter Antworten (Berechtigungsumfang, Besonderheiten der Suche, Zuordnung der `accountId` von Jira, `lockVersion`-Konflikte von OpenProject, Art des API-Schlüssels von Plane) sind erst bekannt, wenn diese Prüfungen in einer echten Umgebung laufen. Beim Ausführen auftretende Abweichungen werden der Tabelle in dieser Datei hinzugefügt.

## Português

Como este ambiente (desenvolvimento/CI) não tem destinos nem credenciais de serviços reais, a verificação contra serviços reais precisa ser feita **manualmente**. Para isso há 3 ferramentas preparadas, e **todas só escrevem quando explicitamente indicado.**

### 1. Diagnóstico de conexão (somente leitura)

```bash
aipmo --config config.yaml integrations            # todos os adaptadores configurados
aipmo --config config.yaml integrations plane      # apenas um
```

Verifica nesta ordem e mostra onde parou e por quê (o código de saída é 1 se qualquer um falhar).

| Etapa | Conteúdo | O que suspeitar em caso de falha |
|---|---|---|
| `health` | Verificação de conectividade do adaptador | URL, autenticação, projeto/workspace indicado |
| `read` | Buscar apenas um item | Permissão de leitura, sintaxe de busca como JQL |
| `people` | Lista de candidatos a responsável (Plane, OpenProject) | Permissão para ler a lista de membros (necessária para atribuir responsável) |

As causas são classificadas como `auth` (401/403), `not_found` (404), `timeout`, `network`, `rate_limit`, `other`.
Tokens e chaves de API configurados são ocultados nas mensagens de erro. **Nenhuma ação de escrita é chamada.**

O mesmo diagnóstico também pode ser feito pelo botão **verificar conexão** na tela "Integrações" (somente operator, no máximo uma vez a cada 5 segundos, `POST /api/integrations/check`).

### 2. Verificação do mapeamento de responsáveis (somente leitura)

```bash
aipmo --config config.yaml members --tracker plane
```

Mostra a qual usuário do rastreador cada membro corresponde. Em caso de ambiguidade ou nenhuma correspondência, escrever
diretamente **para sem escrever** (ver [TICKET-TRACKERS.md](TICKET-TRACKERS.md)).

### 3. Verificação automática contra serviços reais (só funciona localmente)

[tests/test_live_trackers.py](../tests/test_live_trackers.py) é pulado se as variáveis de ambiente não existirem.

```bash
# Somente leitura: diagnóstico de todos os adaptadores e mapeamento de responsáveis
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# Incluindo a escrita do responsável (altera o responsável real de um item; use um item de teste dedicado)
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<id do item>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

O fluxo de escrita é "escrever o responsável → reler para confirmar que foi aplicado → restaurar o responsável original". Jira pode ser restaurado pelo id do responsável, Plane pelo id de usuário.
**OpenProject só conhece o nome relido, portanto não pode ser restaurado** (um aviso é emitido). Um item que não tinha responsável também não pode ser restaurado.
Por isso, use sempre um item de teste dedicado.

### Situação da verificação (com honestidade)

- Com HTTP simulado (a forma das respostas de Jira, Plane, OpenProject) foram verificados o diagnóstico, a escrita, a classificação de erros, a ocultação de segredos e que "a leitura não dispara escrita" (`tests/test_probe.py`, `tests/test_plane.py`, entre outros).
- **Ainda não foi executado nenhuma vez contra um serviço real.** Os detalhes das respostas reais (alcance das permissões, peculiaridades de busca, mapeamento do `accountId` do Jira, conflitos de `lockVersion` do OpenProject, tipo de chave de API do Plane) só serão conhecidos ao executar essas verificações em um ambiente real. As diferenças encontradas serão adicionadas à tabela deste arquivo.

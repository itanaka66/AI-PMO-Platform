# PMO Core の自律的な判断 / Autonomous judgment

PMO Core が、自分で状況を診断し、対処を選び、運用者が許した範囲でだけ実行する仕組みです。
**LLM は使いません**。診断も選択も、数えて比べるだけの決定論的な処理で、同じ入力なら同じ結論になります。

## 何をするか

1. **診断**（毎周）：ブリーフィングと台帳から、次の問題を見つけます。
   `overload`（過負荷）・`project_risk`（プロジェクトの危険）・`agent_failure`（役割AIの失敗）・
   `collection_failing`（進捗収集の不調）・`capacity_shortage`（担い手不足）・`estimate_risk`（見積りの危うさ）。
2. **対処の選択**：診断ごとに候補の「はしご」があり、効用 = 基礎点 − 危険の代償 + 40 × (成功率 − 0.5) で並べます。
   成功率は過去の結果から学習します（Laplace 平滑化。実績が無ければ 50%）。
   対処は `notify`（人へ通知）・`recollect`（進捗を再収集）・`retry_agent`（役割AIの再実行）・
   `launch`（テンプレートの起動）・`followup`（対応タスクの作成）。
3. **自律度**：対処ごとに `off` / `propose` / `auto`。**既定は `notify` と `recollect` だけが `auto`**
   （通知と読み取りだけ）。ほかは `propose`：台帳に「判断」の記録（承認待ち）を作るだけで、人が承認するまで何も起きません。
4. **実行**：常駐の `aipmo schedule` だけが実行します。`aipmo judgment` や Web は表示・承認だけで、実行しません。
5. **効いたかの確認**：実行後、診断が消えれば成功を学習に加え、残れば「効かなかった」として次の段へ進みます。

## 設定

```yaml
pmo_core:
  judgment:                    # 書かなければ、この機能は無効
    autonomy:                  # 既定: notify=auto, recollect=auto, 他=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # 同じ診断で次の対処を試すまでの待ち
      recheck_hours: 24        # 効いたかを見るまでの待ち
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # 失敗した同じ対処を、1 つの診断の中で試す上限
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # 起動してよいテンプレートの許可リスト（これ以外は決して起動しない）
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

## 安全のしくみ

- **遮断器**：自動実行の失敗が `failures` 回（`hours` 時間内）続くと、`auto` は `propose` に落ちます（通知は除く）。
  `aipmo judgment reset` で戻せます。戻すと、それまでの失敗は数えずにやり直せます。時間がたてば自動でも戻ります。
- **失敗の再試行**：失敗した対処は、`max_attempts` 回まで同じ診断の中で再び試します（記録は「n 回目」として別に残ります）。尽きたら次の段へ。
- **一時停止**：`aipmo judgment pause` の間は、診断だけを行い、何も作らず、承認済みのものも実行しません。`resume` で再開。
- **1 日の上限**：`max_auto_per_day`、テンプレートごとの `max_per_day`。
- **判断の記録は仕事ではない**：順位に入らず、完了もできません。却下した判断は同じ診断の間は再提案されません。
- 外の世界への書き込みは、今までどおり人が確定した操作だけです。

## コマンド

```
aipmo judgment            # 診断・自律度・承認待ち・直近の判断（表示専用）
aipmo judgment pause      # 一時停止
aipmo judgment resume     # 再開
aipmo judgment reset      # 遮断器を戻す
aipmo generated           # 判断の記録（承認待ち）を見る・承認する
```

## 限界

- 実サービス（Jira・GitHub など）に対しては未検証です。偽のサーバーを使った実プロセスの通し確認と単体テストのみです。
- 対処は上の 5 種類だけです。診断の追加には `aipmo/judgment.py` の変更が要ります。
- 学習は成功・失敗の回数で、診断の種類ごとの粗いものです。
- 他言語のガイドには、まだ書いていません。

---

PMO Core diagnoses problems deterministically (no LLM), picks a remedy from a per-diagnosis ladder by learned
utility, and acts only inside the operator-granted envelope (default: notify and read-only re-collect are `auto`;
everything else is a proposal needing human approval). Only the resident `aipmo schedule` executes. A circuit
breaker demotes `auto` to `propose` after repeated failures; failed remedies are retried up to `max_attempts`;
`aipmo judgment pause|resume|reset` controls it. Not verified against real services.

---

## 中文

PMO Core 自主诊断状况、选择对策，并只在运营者允许的范围内执行的机制。**不使用 LLM**。诊断和选择都只是计数与比较的决定论处理，相同输入必定得出相同结论。

### 功能

1. **诊断**（每个周期）：从简报和台账中发现以下问题：`overload`（过载）・`project_risk`（项目风险）・`agent_failure`（角色 AI 失败）・`collection_failing`（进度收集不畅）・`capacity_shortage`（人手不足）・`estimate_risk`（估算风险）。
2. **对策选择**：每种诊断都有候选对策的"梯子"，按效用 = 基础分 − 风险代价 + 40 × (成功率 − 0.5) 排序。成功率从过往结果中学习（Laplace 平滑，无记录时为 50%）。对策包括 `notify`（通知人）・`recollect`（重新收集进度）・`retry_agent`（重新执行角色 AI）・`launch`（启动模板）・`followup`（创建应对任务）。
3. **自主程度**：每种对策为 `off` / `propose` / `auto` 之一。**默认只有 `notify` 和 `recollect` 是 `auto`**（仅通知和读取）。其余为 `propose`：只在台账中创建"判断"记录（等待批准），人批准前不会发生任何事。
4. **执行**：只有常驻的 `aipmo schedule` 会执行。`aipmo judgment` 和 Web 界面只显示和批准，不执行。
5. **效果确认**：执行后，若诊断消失则记为成功并加入学习；若仍存在则视为"未生效"，进入下一阶段。

### 配置

```yaml
pmo_core:
  judgment:                    # 不写则此功能禁用
    autonomy:                  # 默认: notify=auto, recollect=auto, 其余=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # 同一诊断尝试下一对策前的等待
      recheck_hours: 24        # 确认是否生效前的等待
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # 同一诊断中重试失败对策的上限
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # 允许启动的模板白名单（此外绝不启动）
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

### 安全机制

- **断路器**：自动执行连续失败 `failures` 次（`hours` 小时内）后，`auto` 会降级为 `propose`（通知除外）。可用 `aipmo judgment reset` 恢复，恢复后之前的失败不计数可重新开始。时间足够后也会自动恢复。
- **失败重试**：失败的对策在同一诊断中最多重试 `max_attempts` 次（记录为"第 n 次"单独保留）。用尽后进入下一阶段。
- **暂停**：`aipmo judgment pause` 期间只进行诊断，不创建任何内容，也不执行已批准的内容。`resume` 恢复。
- **每日上限**：`max_auto_per_day`，以及每个模板的 `max_per_day`。
- **判断记录不是任务**：不计入排名，也无法标记完成。被拒绝的判断在同一诊断持续期间不会再次提出。
- 对外部世界的写入，与以往一样只有经人确认的操作。

### 命令

```
aipmo judgment            # 诊断・自主程度・待批准・最近判断（仅显示）
aipmo judgment pause      # 暂停
aipmo judgment resume     # 恢复
aipmo judgment reset      # 恢复断路器
aipmo generated           # 查看・批准判断记录（待批准）
```

### 局限

- 尚未对真实服务（Jira・GitHub 等）验证。只做过使用模拟服务器的真实流程确认和单元测试。
- 对策仅限上述 5 种。新增诊断需要修改 `aipmo/judgment.py`。
- 学习只是按诊断类型统计成功失败次数，较为粗略。

## 한국어

PMO Core가 스스로 상황을 진단하고, 대책을 고르고, 운영자가 허용한 범위 안에서만 실행하는 체계입니다. **LLM은 사용하지 않습니다.** 진단도 선택도 세고 비교하는 결정론적 처리이며, 같은 입력이면 같은 결론이 나옵니다.

### 하는 일

1. **진단**(매 주기): 브리핑과 원장에서 다음 문제를 찾습니다. `overload`(과부하)·`project_risk`(프로젝트 위험)·`agent_failure`(역할 AI 실패)·`collection_failing`(진행 수집 부진)·`capacity_shortage`(인력 부족)·`estimate_risk`(추정 위험).
2. **대책 선택**: 진단마다 후보 "사다리"가 있고, 효용 = 기본점수 − 위험 대가 + 40 × (성공률 − 0.5)로 정렬합니다. 성공률은 과거 결과에서 학습합니다(라플라스 평활화, 실적이 없으면 50%). 대책은 `notify`(사람에게 알림)·`recollect`(진행 재수집)·`retry_agent`(역할 AI 재실행)·`launch`(템플릿 기동)·`followup`(대응 작업 생성).
3. **자율도**: 대책마다 `off` / `propose` / `auto` 중 하나. **기본값은 `notify`와 `recollect`만 `auto`**(알림과 읽기뿐). 나머지는 `propose`: 원장에 "판단" 기록(승인 대기)만 만들고, 사람이 승인하기 전까지는 아무 일도 일어나지 않습니다.
4. **실행**: 상주하는 `aipmo schedule`만 실행합니다. `aipmo judgment`나 웹은 표시·승인만 하고 실행하지 않습니다.
5. **효과 확인**: 실행 후 진단이 사라지면 성공으로 학습에 더하고, 남아 있으면 "효과 없음"으로 보고 다음 단계로 넘어갑니다.

### 설정

```yaml
pmo_core:
  judgment:                    # 적지 않으면 이 기능은 비활성
    autonomy:                  # 기본: notify=auto, recollect=auto, 나머지=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # 같은 진단에서 다음 대책을 시도하기까지의 대기
      recheck_hours: 24        # 효과를 확인하기까지의 대기
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # 실패한 같은 대책을 한 진단 안에서 시도하는 상한
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # 기동해도 좋은 템플릿 허용 목록(이 외에는 절대 기동하지 않음)
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

### 안전장치

- **차단기**: 자동 실행 실패가 `failures`회(`hours`시간 내) 이어지면 `auto`는 `propose`로 떨어집니다(알림 제외). `aipmo judgment reset`으로 되돌릴 수 있고, 되돌리면 그동안의 실패는 세지 않고 다시 시작합니다. 시간이 지나면 자동으로도 돌아옵니다.
- **실패 재시도**: 실패한 대책은 `max_attempts`회까지 같은 진단 안에서 다시 시도합니다(기록은 "n번째"로 별도 보관). 다 쓰면 다음 단계로.
- **일시정지**: `aipmo judgment pause` 동안은 진단만 하고, 아무것도 만들지 않으며, 승인된 것도 실행하지 않습니다. `resume`으로 재개.
- **일일 상한**: `max_auto_per_day`, 템플릿별 `max_per_day`.
- **판단 기록은 작업이 아닙니다**: 순위에 들어가지 않고 완료도 할 수 없습니다. 거부된 판단은 같은 진단이 지속되는 동안 다시 제안되지 않습니다.
- 외부 세계에 대한 쓰기는 지금까지와 같이 사람이 확정한 작업만입니다.

### 명령

```
aipmo judgment            # 진단·자율도·승인 대기·최근 판단(표시 전용)
aipmo judgment pause      # 일시정지
aipmo judgment resume     # 재개
aipmo judgment reset      # 차단기 되돌리기
aipmo generated           # 판단 기록(승인 대기) 보기·승인
```

### 한계

- 실제 서비스(Jira·GitHub 등)에 대해서는 검증되지 않았습니다. 가짜 서버를 이용한 실제 프로세스 확인과 단위 테스트만 했습니다.
- 대책은 위 5종뿐입니다. 진단 추가에는 `aipmo/judgment.py` 수정이 필요합니다.
- 학습은 성공·실패 횟수로, 진단 종류별로 거칠게 이루어집니다.

## Español

Un mecanismo por el que PMO Core diagnostica la situación por sí mismo, elige un remedio y actúa solo dentro del margen que el operador le ha concedido. **No usa LLM.** Tanto el diagnóstico como la elección son un proceso determinista de contar y comparar: la misma entrada siempre produce la misma conclusión.

### Qué hace

1. **Diagnóstico** (cada ciclo): encuentra los siguientes problemas a partir del briefing y el libro mayor: `overload` (sobrecarga), `project_risk` (riesgo de proyecto), `agent_failure` (fallo de IA de rol), `collection_failing` (recolección de progreso fallida), `capacity_shortage` (falta de capacidad), `estimate_risk` (riesgo de estimación).
2. **Elección del remedio**: cada diagnóstico tiene una "escalera" de candidatos, ordenada por utilidad = puntuación base − coste de riesgo + 40 × (tasa de éxito − 0.5). La tasa de éxito se aprende de resultados pasados (suavizado de Laplace; 50% sin historial). Los remedios son `notify` (avisar a una persona), `recollect` (volver a recolectar progreso), `retry_agent` (reintentar la IA de rol), `launch` (lanzar una plantilla), `followup` (crear una tarea de seguimiento).
3. **Autonomía**: cada remedio es `off` / `propose` / `auto`. **Por defecto solo `notify` y `recollect` son `auto`** (solo notificar y leer). El resto es `propose`: solo crea un registro de "juicio" en el libro mayor (pendiente de aprobación); no pasa nada hasta que una persona lo aprueba.
4. **Ejecución**: solo el `aipmo schedule` residente ejecuta. `aipmo judgment` y la web solo muestran y aprueban, no ejecutan.
5. **Comprobación de efecto**: tras ejecutar, si el diagnóstico desaparece se añade como éxito al aprendizaje; si persiste, se considera "sin efecto" y se avanza al siguiente escalón.

### Configuración

```yaml
pmo_core:
  judgment:                    # Si no se escribe, esta función está desactivada
    autonomy:                  # Por defecto: notify=auto, recollect=auto, el resto=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # Espera antes de probar el siguiente remedio para el mismo diagnóstico
      recheck_hours: 24        # Espera antes de comprobar el efecto
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # Tope de reintentos del mismo remedio fallido dentro de un diagnóstico
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # Lista blanca de plantillas que se pueden lanzar (nunca se lanza ninguna otra)
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

### Mecanismos de seguridad

- **Disyuntor**: si la ejecución automática falla `failures` veces (en `hours` horas), `auto` baja a `propose` (excepto notificar). Se puede restaurar con `aipmo judgment reset`, y al restaurar los fallos anteriores no cuentan. También se restaura solo con el tiempo.
- **Reintento de fallos**: un remedio fallido se reintenta hasta `max_attempts` veces dentro del mismo diagnóstico (se registra por separado como "intento n"). Agotado esto, se pasa al siguiente escalón.
- **Pausa**: durante `aipmo judgment pause` solo se diagnostica, no se crea nada y no se ejecuta ni lo ya aprobado. `resume` reanuda.
- **Tope diario**: `max_auto_per_day`, y `max_per_day` por plantilla.
- **Un registro de juicio no es una tarea**: no entra en el ranking ni puede marcarse como completado. Un juicio rechazado no se vuelve a proponer mientras persista el mismo diagnóstico.
- La escritura al mundo exterior sigue siendo, como siempre, solo la que una persona ha confirmado.

### Comandos

```
aipmo judgment            # Diagnóstico, autonomía, pendientes, juicios recientes (solo lectura)
aipmo judgment pause      # Pausar
aipmo judgment resume     # Reanudar
aipmo judgment reset      # Restaurar el disyuntor
aipmo generated           # Ver y aprobar registros de juicio (pendientes)
```

### Límites

- No verificado contra servicios reales (Jira, GitHub, etc.). Solo verificación de extremo a extremo con un servidor simulado y pruebas unitarias.
- Solo existen los 5 remedios anteriores. Añadir un diagnóstico requiere modificar `aipmo/judgment.py`.
- El aprendizaje es el recuento de éxitos/fallos, bastante tosco por tipo de diagnóstico.

## Français

Un mécanisme par lequel PMO Core diagnostique la situation lui-même, choisit un remède et n'agit que dans la marge accordée par l'opérateur. **N'utilise pas de LLM.** Le diagnostic comme le choix sont un traitement déterministe de comptage et de comparaison : la même entrée produit toujours la même conclusion.

### Ce que ça fait

1. **Diagnostic** (à chaque cycle) : trouve les problèmes suivants à partir du briefing et du registre : `overload` (surcharge), `project_risk` (risque projet), `agent_failure` (échec d'IA de rôle), `collection_failing` (échec de collecte de progression), `capacity_shortage` (manque de capacité), `estimate_risk` (risque d'estimation).
2. **Choix du remède** : chaque diagnostic a une « échelle » de candidats, classée par utilité = score de base − coût du risque + 40 × (taux de succès − 0.5). Le taux de succès est appris à partir des résultats passés (lissage de Laplace ; 50 % sans historique). Les remèdes sont `notify` (avertir une personne), `recollect` (recollecter la progression), `retry_agent` (relancer l'IA de rôle), `launch` (lancer un modèle), `followup` (créer une tâche de suivi).
3. **Autonomie** : chaque remède est `off` / `propose` / `auto`. **Par défaut, seuls `notify` et `recollect` sont `auto`** (notification et lecture seulement). Le reste est `propose` : ne crée qu'un enregistrement de « jugement » dans le registre (en attente d'approbation) ; rien ne se passe avant qu'une personne l'approuve.
4. **Exécution** : seul le `aipmo schedule` résident exécute. `aipmo judgment` et le web n'affichent et n'approuvent que, sans exécuter.
5. **Vérification de l'effet** : après exécution, si le diagnostic disparaît, c'est ajouté comme succès à l'apprentissage ; s'il persiste, c'est considéré « sans effet » et on passe à l'échelon suivant.

### Configuration

```yaml
pmo_core:
  judgment:                    # Si absent, cette fonction est désactivée
    autonomy:                  # Par défaut : notify=auto, recollect=auto, le reste=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # Attente avant d'essayer le remède suivant pour le même diagnostic
      recheck_hours: 24        # Attente avant de vérifier l'effet
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # Plafond de réessais du même remède échoué dans un diagnostic
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # Liste blanche des modèles pouvant être lancés (jamais d'autre)
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

### Mécanismes de sécurité

- **Disjoncteur** : si l'exécution automatique échoue `failures` fois (en `hours` heures), `auto` redescend à `propose` (sauf notification). Restaurable avec `aipmo judgment reset` ; après restauration, les échecs précédents ne comptent plus. Se restaure aussi automatiquement avec le temps.
- **Réessai des échecs** : un remède échoué est retenté jusqu'à `max_attempts` fois dans le même diagnostic (enregistré séparément comme « tentative n »). Une fois épuisé, on passe à l'échelon suivant.
- **Pause** : pendant `aipmo judgment pause`, seul le diagnostic a lieu, rien n'est créé, et même ce qui est approuvé n'est pas exécuté. `resume` relance.
- **Plafond quotidien** : `max_auto_per_day`, et `max_per_day` par modèle.
- **Un enregistrement de jugement n'est pas une tâche** : n'entre pas dans le classement et ne peut pas être marqué terminé. Un jugement rejeté n'est pas re-proposé tant que le même diagnostic persiste.
- L'écriture vers le monde extérieur reste, comme toujours, uniquement ce qu'une personne a confirmé.

### Commandes

```
aipmo judgment            # Diagnostic, autonomie, en attente, jugements récents (affichage seul)
aipmo judgment pause      # Mettre en pause
aipmo judgment resume     # Reprendre
aipmo judgment reset      # Réinitialiser le disjoncteur
aipmo generated           # Voir et approuver les enregistrements de jugement (en attente)
```

### Limites

- Non vérifié contre des services réels (Jira, GitHub, etc.). Seulement une vérification de bout en bout avec un serveur simulé et des tests unitaires.
- Seulement les 5 remèdes ci-dessus. Ajouter un diagnostic nécessite de modifier `aipmo/judgment.py`.
- L'apprentissage est un comptage de succès/échecs, assez grossier par type de diagnostic.

## Deutsch

Ein Mechanismus, mit dem PMO Core die Lage selbst diagnostiziert, eine Abhilfe wählt und nur innerhalb des vom Betreiber gewährten Rahmens handelt. **Verwendet kein LLM.** Sowohl Diagnose als auch Auswahl sind ein deterministischer Zähl- und Vergleichsprozess: dieselbe Eingabe führt immer zum gleichen Ergebnis.

### Was es tut

1. **Diagnose** (jeden Zyklus): findet aus Briefing und Ledger folgende Probleme: `overload` (Überlastung), `project_risk` (Projektrisiko), `agent_failure` (Fehler der Rollen-KI), `collection_failing` (gestörte Fortschrittssammlung), `capacity_shortage` (Kapazitätsmangel), `estimate_risk` (Schätzrisiko).
2. **Wahl der Abhilfe**: jede Diagnose hat eine Kandidaten-„Leiter", sortiert nach Nutzen = Basiswert − Risikokosten + 40 × (Erfolgsquote − 0.5). Die Erfolgsquote wird aus früheren Ergebnissen gelernt (Laplace-Glättung; 50 % ohne Historie). Abhilfen sind `notify` (Person benachrichtigen), `recollect` (Fortschritt neu sammeln), `retry_agent` (Rollen-KI erneut ausführen), `launch` (Vorlage starten), `followup` (Folgeaufgabe anlegen).
3. **Autonomiegrad**: jede Abhilfe ist `off` / `propose` / `auto`. **Standardmäßig sind nur `notify` und `recollect` `auto`** (nur Benachrichtigung und Lesen). Der Rest ist `propose`: erzeugt nur einen „Urteils"-Eintrag im Ledger (wartet auf Genehmigung); es passiert nichts, bis eine Person zustimmt.
4. **Ausführung**: nur das residente `aipmo schedule` führt aus. `aipmo judgment` und das Web zeigen nur an und genehmigen, führen aber nicht aus.
5. **Wirkungsprüfung**: nach der Ausführung wird bei verschwundener Diagnose Erfolg ins Lernen aufgenommen; bleibt sie bestehen, gilt sie als „wirkungslos" und es geht zur nächsten Stufe.

### Konfiguration

```yaml
pmo_core:
  judgment:                    # Ohne Eintrag ist diese Funktion deaktiviert
    autonomy:                  # Standard: notify=auto, recollect=auto, Rest=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # Wartezeit vor dem nächsten Versuch für dieselbe Diagnose
      recheck_hours: 24        # Wartezeit vor der Wirkungsprüfung
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # Obergrenze für Wiederholungen derselben fehlgeschlagenen Abhilfe in einer Diagnose
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # Erlaubnisliste startbarer Vorlagen (sonst wird nie etwas gestartet)
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

### Sicherheitsmechanismen

- **Schutzschalter**: Schlägt die automatische Ausführung `failures`-mal (innerhalb von `hours` Stunden) fehl, fällt `auto` auf `propose` zurück (außer Benachrichtigung). Mit `aipmo judgment reset` zurücksetzbar; danach zählen frühere Fehlschläge nicht mehr. Stellt sich mit der Zeit auch automatisch wieder her.
- **Wiederholung bei Fehlern**: eine fehlgeschlagene Abhilfe wird innerhalb derselben Diagnose bis zu `max_attempts`-mal erneut versucht (separat als „Versuch n" protokolliert). Danach geht es zur nächsten Stufe.
- **Pause**: während `aipmo judgment pause` wird nur diagnostiziert, nichts erzeugt, und selbst Genehmigtes wird nicht ausgeführt. `resume` setzt fort.
- **Tageslimit**: `max_auto_per_day` sowie `max_per_day` pro Vorlage.
- **Ein Urteilseintrag ist keine Aufgabe**: zählt nicht im Ranking und kann nicht als erledigt markiert werden. Ein abgelehntes Urteil wird nicht erneut vorgeschlagen, solange dieselbe Diagnose besteht.
- Das Schreiben in die Außenwelt bleibt wie bisher ausschließlich von Menschen bestätigten Aktionen vorbehalten.

### Befehle

```
aipmo judgment            # Diagnose, Autonomie, Ausstehendes, letzte Urteile (nur Anzeige)
aipmo judgment pause      # Pausieren
aipmo judgment resume     # Fortsetzen
aipmo judgment reset      # Schutzschalter zurücksetzen
aipmo generated           # Urteilseinträge (ausstehend) ansehen und genehmigen
```

### Grenzen

- Nicht gegen echte Dienste (Jira, GitHub usw.) verifiziert. Nur End-to-End-Prüfung mit simuliertem Server und Unittests.
- Nur die obigen 5 Abhilfen existieren. Eine neue Diagnose hinzuzufügen erfordert Änderungen an `aipmo/judgment.py`.
- Das Lernen ist eine Erfolgs-/Fehlschlagszählung, recht grob pro Diagnosetyp.

## Português

Um mecanismo pelo qual o PMO Core diagnostica a situação por conta própria, escolhe um remédio e age apenas dentro do limite concedido pelo operador. **Não usa LLM.** Tanto o diagnóstico quanto a escolha são um processo determinístico de contagem e comparação: a mesma entrada sempre produz a mesma conclusão.

### O que faz

1. **Diagnóstico** (a cada ciclo): encontra os seguintes problemas a partir do briefing e do livro-razão: `overload` (sobrecarga), `project_risk` (risco de projeto), `agent_failure` (falha de IA de função), `collection_failing` (falha na coleta de progresso), `capacity_shortage` (falta de capacidade), `estimate_risk` (risco de estimativa).
2. **Escolha do remédio**: cada diagnóstico tem uma "escada" de candidatos, ordenada por utilidade = pontuação base − custo de risco + 40 × (taxa de sucesso − 0.5). A taxa de sucesso é aprendida a partir de resultados passados (suavização de Laplace; 50% sem histórico). Os remédios são `notify` (avisar uma pessoa), `recollect` (recoletar progresso), `retry_agent` (reexecutar a IA de função), `launch` (lançar um modelo), `followup` (criar uma tarefa de acompanhamento).
3. **Autonomia**: cada remédio é `off` / `propose` / `auto`. **Por padrão, só `notify` e `recollect` são `auto`** (apenas notificar e ler). O resto é `propose`: apenas cria um registro de "julgamento" no livro-razão (aguardando aprovação); nada acontece até que uma pessoa aprove.
4. **Execução**: só o `aipmo schedule` residente executa. `aipmo judgment` e a web só exibem e aprovam, não executam.
5. **Verificação de efeito**: após a execução, se o diagnóstico desaparecer, é adicionado como sucesso ao aprendizado; se persistir, é considerado "sem efeito" e avança para o próximo estágio.

### Configuração

```yaml
pmo_core:
  judgment:                    # Se não for escrito, esta função fica desativada
    autonomy:                  # Padrão: notify=auto, recollect=auto, o resto=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # Espera antes de tentar o próximo remédio para o mesmo diagnóstico
      recheck_hours: 24        # Espera antes de verificar o efeito
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # Limite de novas tentativas do mesmo remédio falho dentro de um diagnóstico
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # Lista de permissão de modelos que podem ser lançados (nunca lança outro)
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

### Mecanismos de segurança

- **Disjuntor**: se a execução automática falhar `failures` vezes (em `hours` horas), `auto` cai para `propose` (exceto notificação). Pode ser restaurado com `aipmo judgment reset`; ao restaurar, as falhas anteriores não contam mais. Também se restaura automaticamente com o tempo.
- **Nova tentativa de falhas**: um remédio falho é tentado novamente até `max_attempts` vezes dentro do mesmo diagnóstico (registrado separadamente como "tentativa n"). Esgotado isso, avança para o próximo estágio.
- **Pausa**: durante `aipmo judgment pause`, apenas o diagnóstico ocorre, nada é criado, e nem mesmo o que já foi aprovado é executado. `resume` retoma.
- **Limite diário**: `max_auto_per_day`, e `max_per_day` por modelo.
- **Um registro de julgamento não é uma tarefa**: não entra no ranking e não pode ser marcado como concluído. Um julgamento rejeitado não é proposto novamente enquanto o mesmo diagnóstico persistir.
- A escrita para o mundo externo continua sendo, como sempre, apenas o que uma pessoa confirmou.

### Comandos

```
aipmo judgment            # Diagnóstico, autonomia, pendentes, julgamentos recentes (somente exibição)
aipmo judgment pause      # Pausar
aipmo judgment resume     # Retomar
aipmo judgment reset      # Restaurar o disjuntor
aipmo generated           # Ver e aprovar registros de julgamento (pendentes)
```

### Limites

- Não verificado contra serviços reais (Jira, GitHub, etc.). Apenas verificação de ponta a ponta com servidor simulado e testes unitários.
- Apenas os 5 remédios acima existem. Adicionar um diagnóstico requer modificar `aipmo/judgment.py`.
- O aprendizado é uma contagem de sucessos/falhas, bastante grosseira por tipo de diagnóstico.

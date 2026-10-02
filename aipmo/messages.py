"""サーバーが組み立てる文章（受信箱・点数の内訳・WBS の注意）の、言語ごとの文言。

画面の見出しやボタンは `aipmo/i18n.py` にある。ここにあるのは、サーバーが状況に応じて文章に
して返すもの：「承認すると何が起きるか」「点数がなぜこうなったか」「WBS のどこが現実とずれているか」。
キー 1 つにつき 8 言語（ja en zh ko es fr de pt）を並べ、`{名前}` の差し込みは全言語で同じにする
（`tests/test_messages.py` が確かめる）。

常駐が書く警告・診断・生成タスクの題名・判断の理由・担当の提案の理由は、日本語の文章に加えて
`{"key", "params"}` の部品（下の `spec` / `render`）を添えて残す。画面はその部品から、選んだ言語で組み立て直す。
部品の無い古い記録や、タスク名・エラー文などのデータは、書かれたまま出る。保存される日本語の文章は、
部品を日本語で組み立てたものと必ず同じ（`tests/test_messages.py` が確かめる）ので、CLI・Slack の表示は変わらない。

Wording for the sentences the server builds (inbox, score parts, WBS notes) and for the structured specs
the resident process stores next to its Japanese text (alerts, diagnoses, generated titles, rationales).
One key, eight languages, identical `{placeholders}`. Screens rebuild a sentence from its spec in the chosen
language; records without a spec, and data such as task names, are shown as written.
"""
from __future__ import annotations

from typing import Any

from .i18n import DEFAULT_LANG, normalize

LANGS = ("ja", "en", "zh", "ko", "es", "fr", "de", "pt")

_M: dict[str, tuple[str, ...]] = {
    # ---- 対処の名前と効果 / remedies ----
    "remedy_notify": ("人へ通知", "Notify a person", "通知相关人员", "담당자에게 알림", "Avisar a una persona", "Notifier une personne", "Person benachrichtigen", "Notificar uma pessoa"),
    "remedy_recollect": ("進捗を再収集", "Re-collect progress", "重新收集进度", "진행 상황 재수집", "Recoger el avance de nuevo", "Recollecter l'avancement", "Fortschritt neu erfassen", "Recoletar o progresso"),
    "remedy_retry_agent": ("役割AIの再実行", "Re-run the role AI", "重新运行角色 AI", "역할 AI 재실행", "Reejecutar la IA de rol", "Relancer l'IA de rôle", "Rollen-KI erneut ausführen", "Reexecutar a IA de função"),
    "remedy_launch": ("テンプレートの起動", "Launch a template", "启动模板", "템플릿 실행", "Lanzar una plantilla", "Lancer un modèle", "Vorlage starten", "Iniciar um modelo"),
    "remedy_followup": ("対応タスクの作成", "Create a follow-up task", "创建跟进任务", "대응 작업 생성", "Crear una tarea de seguimiento", "Créer une tâche de suivi", "Folgeaufgabe anlegen", "Criar uma tarefa de acompanhamento"),
    "effect_followup": ("対応タスクを台帳に作ります（課題管理ツールには作りません）。", "Creates a follow-up task in the ledger (not in the issue tracker).", "在台账中创建跟进任务（不会在工单系统中创建）。", "원장에 대응 작업을 만듭니다(이슈 트래커에는 만들지 않습니다).", "Crea una tarea de seguimiento en el libro (no en el gestor de incidencias).", "Crée une tâche de suivi dans le registre (pas dans le gestionnaire de tickets).", "Legt eine Folgeaufgabe im Hauptbuch an (nicht im Issue-Tracker).", "Cria uma tarefa de acompanhamento no livro-razão (não no gerenciador de itens)."),
    "effect_launch": ("許可リストにあるテンプレートを、診断の文脈つきで起動します。", "Launches an allow-listed template with the diagnosis as context.", "以诊断为上下文启动白名单内的模板。", "허용 목록에 있는 템플릿을 진단 맥락과 함께 실행합니다.", "Lanza una plantilla de la lista permitida con el diagnóstico como contexto.", "Lance un modèle de la liste autorisée avec le diagnostic en contexte.", "Startet eine freigegebene Vorlage mit der Diagnose als Kontext.", "Inicia um modelo da lista permitida com o diagnóstico como contexto."),
    "effect_retry_agent": ("失敗した役割AIの実行を、もう一度任せます。", "Hands the failed role-AI run over once more.", "再次交给角色 AI 执行失败的任务。", "실패한 역할 AI 실행을 다시 맡깁니다.", "Vuelve a encargar la ejecución fallida de la IA de rol.", "Confie à nouveau l'exécution échouée de l'IA de rôle.", "Übergibt den fehlgeschlagenen Lauf der Rollen-KI erneut.", "Entrega novamente a execução que falhou da IA de função."),
    "effect_recollect": ("課題管理ツールの状態を読み直します（読み取りだけ）。", "Re-reads the issue tracker (read-only).", "重新读取工单系统的状态（只读）。", "이슈 트래커 상태를 다시 읽습니다(읽기 전용).", "Vuelve a leer el gestor de incidencias (solo lectura).", "Relit le gestionnaire de tickets (lecture seule).", "Liest den Issue-Tracker neu ein (nur lesend).", "Lê novamente o gerenciador de itens (somente leitura)."),
    "effect_notify": ("人へ通知します。", "Notifies a person.", "通知相关人员。", "담당자에게 알립니다.", "Avisa a una persona.", "Notifie une personne.", "Benachrichtigt eine Person.", "Notifica uma pessoa."),

    # ---- 共通の操作 / shared actions ----
    "act_approve": ("承認する", "Approve", "批准", "승인", "Aprobar", "Approuver", "Freigeben", "Aprovar"),
    "act_reject": ("却下する", "Reject", "驳回", "반려", "Rechazar", "Rejeter", "Ablehnen", "Rejeitar"),

    # ---- 自律的な判断 / judgment ----
    "j_evidence": ("診断の根拠", "Evidence for the diagnosis", "诊断依据", "진단 근거", "Base del diagnóstico", "Éléments du diagnostic", "Grundlage der Diagnose", "Base do diagnóstico"),
    "j_remedy": ("選んだ対処", "Chosen remedy", "选择的对策", "선택한 대처", "Remedio elegido", "Remède choisi", "Gewählte Maßnahme", "Medida escolhida"),
    "j_why": ("なぜこの対処か", "Why this remedy", "为何选此对策", "이 대처를 고른 이유", "Por qué este remedio", "Pourquoi ce remède", "Warum diese Maßnahme", "Por que esta medida"),
    "j_approve_h": ("承認すると", "If you approve", "批准后", "승인하면", "Si aprueba", "Si vous approuvez", "Bei Freigabe", "Se você aprovar"),
    "j_approve_1": ("常駐（aipmo schedule）が次の周で実行し、結果を判断の記録に残します。", "The resident process (aipmo schedule) runs it on its next cycle and records the result.", "常驻进程（aipmo schedule）在下一周期执行，并把结果记入判断记录。", "상주 프로세스(aipmo schedule)가 다음 주기에 실행하고 결과를 판단 기록에 남깁니다.", "El proceso residente (aipmo schedule) la ejecuta en el próximo ciclo y registra el resultado.", "Le processus résident (aipmo schedule) l'exécute au prochain cycle et consigne le résultat.", "Der Dauerprozess (aipmo schedule) führt sie im nächsten Zyklus aus und protokolliert das Ergebnis.", "O processo residente (aipmo schedule) executa no próximo ciclo e registra o resultado."),
    "j_approve_2": ("実行しても診断が続けば「効かなかった」と学習し、次の対処へ進みます。", "If the diagnosis persists afterwards it is learned as \"did not work\" and the next remedy is tried.", "若执行后诊断仍持续，则学习为“无效”，并转向下一个对策。", "실행 후에도 진단이 계속되면 '효과 없음'으로 학습하고 다음 대처로 넘어갑니다.", "Si el diagnóstico persiste, se aprende como «no funcionó» y se pasa al siguiente remedio.", "Si le diagnostic persiste, c'est appris comme « sans effet » et le remède suivant est tenté.", "Besteht die Diagnose danach fort, wird „hat nicht gewirkt“ gelernt und die nächste Maßnahme versucht.", "Se o diagnóstico persistir, é aprendido como «não funcionou» e passa-se à próxima medida."),
    "j_dont_h": ("しないこと", "What it does not do", "不会做的事", "하지 않는 것", "Lo que no hace", "Ce que cela ne fait pas", "Was nicht geschieht", "O que não faz"),
    "j_dont_1": ("承認しただけでは実行されません（実行するのは常駐だけ）。", "Approving alone does not run it (only the resident process does).", "仅批准不会执行（只有常驻进程会执行）。", "승인만으로는 실행되지 않습니다(실행은 상주 프로세스만 합니다).", "Aprobar no la ejecuta por sí solo (solo la ejecuta el proceso residente).", "L'approbation seule ne l'exécute pas (seul le processus résident le fait).", "Die Freigabe allein führt nichts aus (nur der Dauerprozess).", "Aprovar sozinho não executa (só o processo residente executa)."),
    "j_dont_2": ("Jira など外部への書き込みはしません。", "It does not write to Jira or any other external system.", "不会写入 Jira 等外部系统。", "Jira 등 외부에는 쓰지 않습니다.", "No escribe en Jira ni en ningún sistema externo.", "Aucune écriture vers Jira ou un système externe.", "Es wird nicht in Jira oder andere externe Systeme geschrieben.", "Não escreve no Jira nem em sistemas externos."),
    "j_reject_h": ("却下すると", "If you reject", "驳回后", "반려하면", "Si rechaza", "Si vous rejetez", "Bei Ablehnung", "Se você rejeitar"),
    "j_reject_1": ("同じ診断が続く間は、同じ対処を再び提案しません。", "The same remedy is not proposed again while the same diagnosis continues.", "只要同一诊断持续，就不会再次提议相同对策。", "같은 진단이 계속되는 동안 같은 대처를 다시 제안하지 않습니다.", "No se vuelve a proponer el mismo remedio mientras siga el mismo diagnóstico.", "Le même remède n'est pas reproposé tant que le diagnostic persiste.", "Dieselbe Maßnahme wird nicht erneut vorgeschlagen, solange die Diagnose besteht.", "A mesma medida não é proposta de novo enquanto o diagnóstico continuar."),
    "j_summary": ("提案: {remedy}", "Proposal: {remedy}", "提议：{remedy}", "제안: {remedy}", "Propuesta: {remedy}", "Proposition : {remedy}", "Vorschlag: {remedy}", "Proposta: {remedy}"),

    # ---- 提案（対応タスク・WBS）/ proposals ----
    "p_trigger": ("きっかけ", "Trigger", "起因", "계기", "Origen", "Déclencheur", "Auslöser", "Gatilho"),
    "w_trigger": ("WBS のずれ: {code}（作業 {node}）", "WBS drift: {code} (task {node})", "WBS 偏差：{code}（任务 {node}）", "WBS 어긋남: {code}(작업 {node})", "Desvío del WBS: {code} (tarea {node})", "Écart WBS : {code} (tâche {node})", "WBS-Abweichung: {code} (Aufgabe {node})", "Desvio do WBS: {code} (tarefa {node})"),
    "w_approve_1": ("「WBS を確かめる」という普通のタスクになります（順位に入り、担当の提案が出ます）。", "It becomes an ordinary \"check the WBS\" task (it enters the ranking and gets an assignee proposal).", "将成为普通的“核对 WBS”任务（进入排序，并给出负责人提议）。", "'WBS 확인'이라는 일반 작업이 됩니다(순위에 들어가고 담당 제안이 나옵니다).", "Se convierte en una tarea ordinaria «revisar el WBS» (entra en el ranking y recibe propuesta de responsable).", "Devient une tâche ordinaire « vérifier le WBS » (entre dans le classement et reçoit une proposition d'assigné).", "Wird zu einer normalen Aufgabe „WBS prüfen“ (kommt ins Ranking, Zuständigen-Vorschlag folgt).", "Torna-se uma tarefa comum «verificar o WBS» (entra no ranking e recebe proposta de responsável)."),
    "w_dont_1": ("WBS ファイルは変わりません。直すのは人（PR）です。", "The WBS file is not changed; a person fixes it (via a PR).", "WBS 文件不会改变，由人（通过 PR）修正。", "WBS 파일은 바뀌지 않습니다. 고치는 것은 사람(PR)입니다.", "El archivo WBS no cambia; lo corrige una persona (mediante PR).", "Le fichier WBS n'est pas modifié ; une personne le corrige (via une PR).", "Die WBS-Datei bleibt unverändert; ein Mensch korrigiert sie (per PR).", "O arquivo WBS não muda; uma pessoa corrige (via PR)."),
    "w_reject_1": ("同じ問題が続く間は、再び提案しません。直ってから再び起きれば、新しい提案になります。", "It is not proposed again while the problem persists; if it recurs after being fixed, it becomes a new proposal.", "问题持续期间不会再次提议；修复后再次出现则成为新提议。", "같은 문제가 계속되는 동안 다시 제안하지 않습니다. 고친 뒤 다시 생기면 새 제안이 됩니다.", "No se vuelve a proponer mientras persista el problema; si reaparece tras corregirse, será una propuesta nueva.", "Non reproposé tant que le problème persiste ; s'il revient après correction, ce sera une nouvelle proposition.", "Wird nicht erneut vorgeschlagen, solange das Problem besteht; tritt es nach Behebung wieder auf, entsteht ein neuer Vorschlag.", "Não é proposta de novo enquanto o problema persistir; se voltar depois de corrigido, será uma nova proposta."),
    "f_trigger": ("続いている警告: {rule}", "Ongoing alert: {rule}", "持续的警告：{rule}", "계속되는 경고: {rule}", "Alerta persistente: {rule}", "Alerte persistante : {rule}", "Anhaltende Warnung: {rule}", "Alerta persistente: {rule}"),
    "f_prio": ("優先度・期限", "Priority and due date", "优先级与截止日", "우선순위·기한", "Prioridad y vencimiento", "Priorité et échéance", "Priorität und Fälligkeit", "Prioridade e prazo"),
    "f_prio_line": ("{priority} ・ 期限 {due}", "{priority} · due {due}", "{priority} · 截止 {due}", "{priority} · 기한 {due}", "{priority} · vence {due}", "{priority} · échéance {due}", "{priority} · fällig {due}", "{priority} · prazo {due}"),
    "f_approve_1": ("普通のタスクになります（順位に入り、担当の提案が出ます。役割AIにも任せられます）。", "It becomes an ordinary task (it enters the ranking and gets an assignee proposal; a role AI can take it too).", "将成为普通任务（进入排序并给出负责人提议，也可交给角色 AI）。", "일반 작업이 됩니다(순위에 들어가고 담당 제안이 나오며 역할 AI에게도 맡길 수 있습니다).", "Se convierte en una tarea ordinaria (entra en el ranking, recibe propuesta de responsable y puede encargarse a una IA de rol).", "Devient une tâche ordinaire (entre dans le classement, reçoit une proposition d'assigné et peut être confiée à une IA de rôle).", "Wird zur normalen Aufgabe (kommt ins Ranking, Zuständigen-Vorschlag folgt, auch einer Rollen-KI übertragbar).", "Torna-se uma tarefa comum (entra no ranking, recebe proposta de responsável e pode ser dada a uma IA de função)."),
    "f_reject_1": ("記録は残り、同じ警告の間は再び提案しません（完了実績にはしません）。", "The record stays and it is not proposed again while the same alert continues (it is not counted as a completion).", "记录保留；同一警告持续期间不再提议（不计为完成实绩）。", "기록은 남고 같은 경고가 계속되는 동안 다시 제안하지 않습니다(완료 실적으로 치지 않습니다).", "El registro permanece y no se vuelve a proponer mientras siga la alerta (no cuenta como finalizada).", "La trace est conservée et elle n'est pas reproposée tant que l'alerte dure (non comptée comme terminée).", "Der Eintrag bleibt; solange dieselbe Warnung besteht, wird nicht erneut vorgeschlagen (zählt nicht als erledigt).", "O registro permanece e não é proposta de novo enquanto o alerta durar (não conta como concluída)."),
    "p_summary": ("提案（承認待ち） ・ {priority}", "Proposal (awaiting approval) · {priority}", "提议（待批准）· {priority}", "제안(승인 대기) · {priority}", "Propuesta (pendiente de aprobación) · {priority}", "Proposition (en attente d'approbation) · {priority}", "Vorschlag (Freigabe offen) · {priority}", "Proposta (aguardando aprovação) · {priority}"),

    # ---- 担当 / assignment ----
    "a_reason": ("理由", "Reason", "理由", "이유", "Motivo", "Raison", "Begründung", "Motivo"),
    "a_task": ("タスク", "Task", "任务", "작업", "Tarea", "Tâche", "Aufgabe", "Tarefa"),
    "a_task_line": ("{key} ・ {priority} ・ 期限 {due} ・ 点数 {score}", "{key} · {priority} · due {due} · score {score}", "{key} · {priority} · 截止 {due} · 分数 {score}", "{key} · {priority} · 기한 {due} · 점수 {score}", "{key} · {priority} · vence {due} · puntos {score}", "{key} · {priority} · échéance {due} · score {score}", "{key} · {priority} · fällig {due} · Punkte {score}", "{key} · {priority} · prazo {due} · pontos {score}"),
    "a_confirm_h": ("確定すると", "If you confirm", "确认后", "확정하면", "Si confirma", "Si vous confirmez", "Bei Bestätigung", "Se você confirmar"),
    "a_confirm_1": ("台帳の担当を {who} にします。", "The ledger assignee becomes {who}.", "台账中的负责人将设为 {who}。", "원장의 담당을 {who}(으)로 합니다.", "El responsable del libro pasa a ser {who}.", "L'assigné du registre devient {who}.", "Der Zuständige im Hauptbuch wird {who}.", "O responsável no livro-razão passa a ser {who}."),
    "a_confirm_2": ("{tracker} の担当者も更新します（引き当てられなければ、別人に付けずに止まります）。", "The assignee in {tracker} is updated too (if it cannot be matched, it stops rather than assigning someone else).", "同时更新 {tracker} 中的负责人（若无法匹配，则停止而不会分配给他人）。", "{tracker}의 담당자도 갱신합니다(찾지 못하면 다른 사람에게 붙이지 않고 멈춥니다).", "También se actualiza el responsable en {tracker} (si no se encuentra, se detiene sin asignar a otra persona).", "L'assigné dans {tracker} est aussi mis à jour (s'il est introuvable, on s'arrête sans en désigner un autre).", "Der Zuständige in {tracker} wird ebenfalls aktualisiert (ist er nicht auffindbar, wird abgebrochen statt jemand anderen einzutragen).", "O responsável em {tracker} também é atualizado (se não for encontrado, para sem atribuir a outra pessoa)."),
    "a_action": ("担当を確定する", "Confirm assignee", "确认负责人", "담당 확정", "Confirmar responsable", "Confirmer l'assigné", "Zuständigen bestätigen", "Confirmar responsável"),

    # ---- 役割AIの成果 / review ----
    "r_result": ("成果", "Result", "成果", "성과", "Resultado", "Résultat", "Ergebnis", "Resultado"),
    "r_none": ("（要約なし）", "(no summary)", "（无摘要）", "(요약 없음)", "(sin resumen)", "(pas de résumé)", "(keine Zusammenfassung)", "(sem resumo)"),
    "r_run": ("実行", "Run", "执行", "실행", "Ejecución", "Exécution", "Lauf", "Execução"),
    "r_run_line": ("{agent} ・ 実行 {dispatch}", "{agent} · run {dispatch}", "{agent} · 执行 {dispatch}", "{agent} · 실행 {dispatch}", "{agent} · ejecución {dispatch}", "{agent} · exécution {dispatch}", "{agent} · Lauf {dispatch}", "{agent} · execução {dispatch}"),
    "r_accept_h": ("認めると", "If you accept", "认可后", "인정하면", "Si acepta", "Si vous acceptez", "Bei Annahme", "Se você aceitar"),
    "r_accept_1": ("確かめた記録が残ります。タスクは完了になりません（閉じるのは人）。", "A record of the review is kept. The task is not completed (a person closes it).", "留下确认记录。任务不会被完成（由人关闭）。", "확인한 기록이 남습니다. 작업은 완료되지 않습니다(닫는 것은 사람).", "Queda constancia de la revisión. La tarea no se completa (la cierra una persona).", "Une trace de la revue est conservée. La tâche n'est pas terminée (une personne la ferme).", "Die Prüfung wird protokolliert. Die Aufgabe wird nicht abgeschlossen (das tut ein Mensch).", "Fica o registro da revisão. A tarefa não é concluída (uma pessoa a fecha)."),
    "r_reject_h": ("差し戻すと", "If you send it back", "退回后", "반려하면", "Si lo devuelve", "Si vous renvoyez", "Bei Zurückweisung", "Se você devolver"),
    "r_reject_1": ("警告になり、人が引き取るか、もう一度任せます。理由が必要です。役割AI自身は、確かめる人になれません。", "It raises an alert and a person takes over or hands it over again. A reason is required. A role AI cannot be the reviewer.", "将产生警告，由人接手或再次委托。需要填写理由。角色 AI 本身不能作为确认人。", "경고가 되고 사람이 맡거나 다시 맡깁니다. 이유가 필요합니다. 역할 AI 자신은 확인자가 될 수 없습니다.", "Genera una alerta y una persona lo asume o lo vuelve a encargar. Hace falta un motivo. Una IA de rol no puede ser quien revisa.", "Cela crée une alerte et une personne reprend ou le confie à nouveau. Un motif est requis. Une IA de rôle ne peut pas être relectrice.", "Es entsteht eine Warnung; ein Mensch übernimmt oder vergibt es neu. Eine Begründung ist nötig. Eine Rollen-KI kann nicht prüfen.", "Gera um alerta e uma pessoa assume ou entrega de novo. É preciso um motivo. Uma IA de função não pode ser a revisora."),
    "r_summary": ("{agent} の成果（レビュー待ち）", "Result from {agent} (awaiting review)", "{agent} 的成果（待确认）", "{agent}의 성과(확인 대기)", "Resultado de {agent} (pendiente de revisión)", "Résultat de {agent} (à relire)", "Ergebnis von {agent} (Prüfung offen)", "Resultado de {agent} (aguardando revisão)"),
    "r_act_accept": ("認める", "Accept", "认可", "인정", "Aceptar", "Accepter", "Annehmen", "Aceitar"),
    "r_act_reject": ("差し戻す", "Send back", "退回", "반려", "Devolver", "Renvoyer", "Zurückweisen", "Devolver"),

    # ---- 起票 / filing ----
    "g_target": ("起票先", "Target tracker", "创建目标", "등록 대상", "Destino", "Destination", "Ziel", "Destino"),
    "g_origin": ("由来", "Origin", "来源", "유래", "Origen", "Origine", "Herkunft", "Origem"),
    "g_origin_recurring": ("定期タスク（設定に書いたので、承認なしで作成済み）", "Recurring task (created without approval because it is in the config)", "定期任务（已写入配置，无需批准即创建）", "정기 작업(설정에 적혀 있어 승인 없이 생성됨)", "Tarea periódica (creada sin aprobación porque está en la configuración)", "Tâche récurrente (créée sans approbation car écrite dans la configuration)", "Wiederkehrende Aufgabe (ohne Freigabe angelegt, da in der Konfiguration)", "Tarefa recorrente (criada sem aprovação por estar na configuração)"),
    "g_origin_followup": ("承認済みの提案", "Approved proposal", "已批准的提议", "승인된 제안", "Propuesta aprobada", "Proposition approuvée", "Freigegebener Vorschlag", "Proposta aprovada"),
    "g_assignee": ("担当", "Assignee", "负责人", "담당", "Responsable", "Assigné", "Zuständig", "Responsável"),
    "g_unassigned": ("未定（担当なしで起票します）", "Undecided (it will be filed without an assignee)", "未定（将不指定负责人创建）", "미정(담당 없이 등록합니다)", "Sin decidir (se dará de alta sin responsable)", "Non défini (créé sans assigné)", "Offen (wird ohne Zuständigen angelegt)", "Indefinido (será criado sem responsável)"),
    "g_failed": ("前回の失敗", "Previous failure", "上次失败", "이전 실패", "Fallo anterior", "Échec précédent", "Letzter Fehler", "Falha anterior"),
    "g_file_h": ("起票すると", "If you file it", "创建后", "등록하면", "Si lo da de alta", "Si vous le créez", "Beim Anlegen", "Se você criar"),
    "g_file_1": ("課題管理ツールに課題を作ります（外の世界を変える操作です）。", "An issue is created in the issue tracker (this changes the outside world).", "将在工单系统中创建工单（会改变外部系统）。", "이슈 트래커에 이슈를 만듭니다(외부를 바꾸는 조작입니다).", "Se crea una incidencia en el gestor (cambia el mundo exterior).", "Un ticket est créé dans le gestionnaire (cela modifie le monde extérieur).", "Im Issue-Tracker wird ein Ticket angelegt (das verändert die Außenwelt).", "Um item é criado no gerenciador (isso altera o mundo externo)."),
    "g_file_2": ("同じものは二重に作りません。以後の収集が、その課題を同じタスクとして更新します。", "It is never created twice; later collection updates that issue as the same task.", "不会重复创建；之后的收集会把该工单作为同一任务更新。", "같은 것은 두 번 만들지 않습니다. 이후 수집이 그 이슈를 같은 작업으로 갱신합니다.", "No se crea dos veces; la recogida posterior actualiza esa incidencia como la misma tarea.", "Jamais créé en double ; la collecte suivante mettra ce ticket à jour comme la même tâche.", "Wird nie doppelt angelegt; spätere Erfassung aktualisiert das Ticket als dieselbe Aufgabe.", "Nunca é criado duas vezes; a coleta seguinte atualiza esse item como a mesma tarefa."),
    "g_summary": ("{tracker} への起票待ち", "Waiting to be filed in {tracker}", "待在 {tracker} 中创建", "{tracker} 등록 대기", "Pendiente de alta en {tracker}", "En attente de création dans {tracker}", "Wartet auf Anlage in {tracker}", "Aguardando criação em {tracker}"),
    "g_act_file": ("起票する", "File it", "创建", "등록", "Dar de alta", "Créer", "Anlegen", "Criar"),
    "g_act_skip": ("見送る", "Skip", "暂不创建", "보류", "Omitir", "Passer", "Überspringen", "Pular"),

    # ---- WBS 再計画案 / replan ----
    "l_reason": ("根拠", "Rationale", "依据", "근거", "Fundamento", "Justification", "Begründung", "Justificativa"),
    "l_content": ("内容", "Content", "内容", "내용", "Contenido", "Contenu", "Inhalt", "Conteúdo"),
    "l_content_changes": ("WBS ファイルへ反映できる変更 {n} 件", "{n} change(s) that can be applied to the WBS file", "可应用到 WBS 文件的变更 {n} 项", "WBS 파일에 반영할 수 있는 변경 {n}건", "{n} cambio(s) aplicables al archivo WBS", "{n} modification(s) applicable(s) au fichier WBS", "{n} Änderung(en), die auf die WBS-Datei anwendbar sind", "{n} alteração(ões) aplicável(is) ao arquivo WBS"),
    "l_content_free": ("自由な形の提案（承認しても記録だけ）", "Free-form proposal (approving only records it)", "自由格式的提议（批准后仅留记录）", "자유 형식 제안(승인해도 기록만 남음)", "Propuesta de formato libre (aprobarla solo deja constancia)", "Proposition libre (l'approuver ne fait que l'enregistrer)", "Freiform-Vorschlag (die Freigabe protokolliert nur)", "Proposta de formato livre (aprovar apenas registra)"),
    "l_approve_changes": ("反映先が設定されていれば、反映後の内容まで検証してから WBS ファイルへ書きます。", "If a target is configured, the result is validated before it is written to the WBS file.", "若已配置反映目标，会在验证应用后的内容后再写入 WBS 文件。", "반영 대상이 설정되어 있으면 반영 후 내용까지 검증한 뒤 WBS 파일에 씁니다.", "Si hay destino configurado, se valida el resultado antes de escribirlo en el archivo WBS.", "Si une cible est configurée, le résultat est validé avant d'être écrit dans le fichier WBS.", "Ist ein Ziel konfiguriert, wird das Ergebnis vor dem Schreiben in die WBS-Datei geprüft.", "Se houver destino configurado, o resultado é validado antes de ser gravado no arquivo WBS."),
    "l_approve_free": ("承認の記録だけが残ります。", "Only the approval is recorded.", "仅留下批准记录。", "승인 기록만 남습니다.", "Solo queda constancia de la aprobación.", "Seule l'approbation est enregistrée.", "Nur die Freigabe wird protokolliert.", "Apenas a aprovação é registrada."),
    "l_title": ("WBS 再計画案{label}: {wbs}", "WBS replan proposal{label}: {wbs}", "WBS 重新规划方案{label}：{wbs}", "WBS 재계획안{label}: {wbs}", "Propuesta de replanificación del WBS{label}: {wbs}", "Proposition de replanification du WBS{label} : {wbs}", "WBS-Umplanungsvorschlag{label}: {wbs}", "Proposta de replanejamento do WBS{label}: {wbs}"),
    "l_summary": ("tier {tier} ・ 確信度 {confidence}", "tier {tier} · confidence {confidence}", "tier {tier} · 置信度 {confidence}", "tier {tier} · 신뢰도 {confidence}", "tier {tier} · confianza {confidence}", "tier {tier} · confiance {confidence}", "Tier {tier} · Konfidenz {confidence}", "tier {tier} · confiança {confidence}"),

    # ---- 点数の内訳 / score parts ----
    "s_priority": ("優先度 {priority}", "Priority {priority}", "优先级 {priority}", "우선순위 {priority}", "Prioridad {priority}", "Priorité {priority}", "Priorität {priority}", "Prioridade {priority}"),
    "s_priority_shift": ("優先度 {priority}（実績による補正 {shift}）", "Priority {priority} (adjusted by track record {shift})", "优先级 {priority}（按实绩修正 {shift}）", "우선순위 {priority}(실적 보정 {shift})", "Prioridad {priority} (ajuste por historial {shift})", "Priorité {priority} (ajustement selon l'historique {shift})", "Priorität {priority} (Anpassung nach Erfahrung {shift})", "Prioridade {priority} (ajuste pelo histórico {shift})"),
    "s_unset": ("未設定", "not set", "未设置", "미설정", "sin definir", "non défini", "nicht gesetzt", "não definida"),
    "s_due_over": ("期限を {days} 日超過", "{days} day(s) past the due date", "已超期 {days} 天", "기한을 {days}일 초과", "{days} día(s) de retraso", "{days} jour(s) de retard", "{days} Tag(e) überfällig", "{days} dia(s) de atraso"),
    "s_due_left": ("期限まで残り {days} 日", "{days} day(s) until the due date", "距截止还有 {days} 天", "기한까지 {days}일", "{days} día(s) hasta el vencimiento", "{days} jour(s) avant l'échéance", "Noch {days} Tag(e) bis zur Fälligkeit", "{days} dia(s) até o prazo"),
    "s_pace": ("見積り {effort} 点 × 実績ペース {per} 日/点 → あと約 {still} 日、期限まで {left} 日", "Estimate {effort} pt × pace {per} d/pt → about {still} more days, {left} days until due", "估算 {effort} 点 × 实绩节奏 {per} 天/点 → 还需约 {still} 天，距截止 {left} 天", "추정 {effort}점 × 실적 페이스 {per}일/점 → 약 {still}일 더 필요, 기한까지 {left}일", "Estimación {effort} pt × ritmo {per} d/pt → unos {still} días más, {left} hasta el vencimiento", "Estimation {effort} pt × rythme {per} j/pt → environ {still} jours de plus, {left} avant l'échéance", "Schätzung {effort} Pkt × Tempo {per} T/Pkt → noch ca. {still} Tage, {left} bis zur Fälligkeit", "Estimativa {effort} pt × ritmo {per} d/pt → cerca de {still} dias a mais, {left} até o prazo"),
    "s_blocked": ("ブロック中", "Blocked", "被阻塞", "차단됨", "Bloqueada", "Bloquée", "Blockiert", "Bloqueada"),
    "s_corroboration": ("他 {n} 件のテンプレートも指摘", "Also flagged by {n} other template(s)", "另有 {n} 个模板也指出", "다른 템플릿 {n}건도 지적", "También señalada por otras {n} plantilla(s)", "Aussi signalée par {n} autre(s) modèle(s)", "Auch von {n} weiteren Vorlage(n) gemeldet", "Também apontada por outros {n} modelo(s)"),
    "s_unassigned": ("担当者未定", "No assignee", "未指定负责人", "담당자 미정", "Sin responsable", "Sans assigné", "Kein Zuständiger", "Sem responsável"),
    "s_label": ("過去実績で遅れやすいラベル「{label}」", "Label \"{label}\" tends to run late", "按历史实绩易延迟的标签“{label}”", "과거 실적상 지연되기 쉬운 라벨 '{label}'", "Etiqueta «{label}» propensa a retrasos según el historial", "Libellé « {label} » souvent en retard d'après l'historique", "Label „{label}“ verzögert sich erfahrungsgemäß", "Rótulo «{label}» costuma atrasar pelo histórico"),
    "s_other": ("その他の補正 {gap}（学習モデルの更新差など）", "Other adjustment {gap} (e.g. a stale learned model)", "其他修正 {gap}（如学习模型更新差异）", "기타 보정 {gap}(학습 모델 갱신 차이 등)", "Otro ajuste {gap} (p. ej., modelo aprendido desfasado)", "Autre ajustement {gap} (p. ex. modèle appris obsolète)", "Sonstige Anpassung {gap} (z. B. veraltetes Lernmodell)", "Outro ajuste {gap} (p. ex., modelo aprendido desatualizado)"),

    # ---- WBS の注意 / WBS problems ----
    "wp_done_without_evidence": ("完了ですが証拠（evidence）がありません。現実と合っているか確かめられません", "Marked done but has no evidence, so it cannot be checked against reality", "已标记完成但没有证据（evidence），无法核对是否符合实际", "완료로 되어 있지만 증거(evidence)가 없어 현실과 맞는지 확인할 수 없습니다", "Marcada como hecha pero sin evidencia: no se puede comprobar con la realidad", "Marquée terminée mais sans preuve : impossible de vérifier avec la réalité", "Als erledigt markiert, aber ohne Nachweis – Abgleich mit der Realität nicht möglich", "Marcada como concluída mas sem evidência: não dá para conferir com a realidade"),
    "wp_evidence_missing": ("完了と書かれていますが証拠がありません: {why}", "Marked done but the evidence is missing: {why}", "标记为完成，但缺少证据：{why}", "완료로 되어 있지만 증거가 없습니다: {why}", "Marcada como hecha pero falta la evidencia: {why}", "Marquée terminée mais la preuve manque : {why}", "Als erledigt markiert, aber der Nachweis fehlt: {why}", "Marcada como concluída mas falta a evidência: {why}"),
    "wp_bad_evidence_path": ("証拠のパスはリポジトリ内の相対パスだけ: {path}", "Evidence paths must be relative paths inside the repository: {path}", "证据路径只能是仓库内的相对路径：{path}", "증거 경로는 저장소 안의 상대 경로만 가능: {path}", "Las rutas de evidencia deben ser relativas dentro del repositorio: {path}", "Les chemins de preuve doivent être relatifs au dépôt : {path}", "Nachweispfade müssen relative Pfade im Repository sein: {path}", "Os caminhos de evidência devem ser relativos ao repositório: {path}"),
    "wp_done_before_dependency": ("完了ですが依存先が未完了です: {deps}", "Marked done but a dependency is not done: {deps}", "已标记完成，但依赖项尚未完成：{deps}", "완료지만 의존 대상이 미완료입니다: {deps}", "Marcada como hecha pero una dependencia no lo está: {deps}", "Marquée terminée mais une dépendance ne l'est pas : {deps}", "Als erledigt markiert, aber eine Abhängigkeit ist offen: {deps}", "Marcada como concluída mas uma dependência não está: {deps}"),
    "wp_maybe_done": ("証拠がすべて揃っています。完了にできるか確認してください", "All the evidence is in place. Check whether it can be marked done", "证据已齐全。请确认是否可标记为完成", "증거가 모두 갖춰졌습니다. 완료로 할 수 있는지 확인하세요", "Toda la evidencia está presente. Compruebe si puede marcarse como hecha", "Toutes les preuves sont réunies. Vérifiez si elle peut être marquée terminée", "Alle Nachweise liegen vor. Prüfen Sie, ob sie als erledigt gelten kann", "Toda a evidência está presente. Verifique se pode ser marcada como concluída"),
    "wp_unestimated": ("見積り（effort）がありません", "No estimate (effort)", "没有估算（effort）", "추정(effort)이 없습니다", "Sin estimación (effort)", "Pas d'estimation (effort)", "Keine Schätzung (effort)", "Sem estimativa (effort)"),
    "wp_overdue": ("期限 {due} を過ぎています", "Past its due date {due}", "已超过截止日 {due}", "기한 {due}을(를) 넘겼습니다", "Pasó su vencimiento {due}", "Échéance {due} dépassée", "Fälligkeit {due} überschritten", "Passou do prazo {due}"),
    "wp_deadline_passed": ("全体の期限 {due} を過ぎています", "The overall deadline {due} has passed", "整体截止日 {due} 已过", "전체 기한 {due}이(가) 지났습니다", "Pasó la fecha límite global {due}", "L'échéance globale {due} est dépassée", "Die Gesamtfrist {due} ist überschritten", "O prazo geral {due} já passou"),
    "ev_not_found": ("{path} が見つかりません", "{path} was not found", "找不到 {path}", "{path}을(를) 찾을 수 없습니다", "No se encontró {path}", "{path} est introuvable", "{path} wurde nicht gefunden", "{path} não foi encontrado"),
    "ev_phrase_missing": ("{path} に「{phrase}」が見つかりません", "\"{phrase}\" was not found in {path}", "在 {path} 中找不到“{phrase}”", "{path}에서 '{phrase}'을(를) 찾을 수 없습니다", "No se encontró «{phrase}» en {path}", "« {phrase} » est introuvable dans {path}", "„{phrase}“ wurde in {path} nicht gefunden", "«{phrase}» não foi encontrado em {path}"),
    "ev_outside_root": ("ルート外・絶対パスは使えません: {path}", "Absolute paths and paths outside the root are not allowed: {path}", "不能使用根目录之外的路径或绝对路径：{path}", "루트 밖·절대 경로는 사용할 수 없습니다: {path}", "No se permiten rutas absolutas ni fuera de la raíz: {path}", "Les chemins absolus ou hors racine sont interdits : {path}", "Absolute Pfade und Pfade außerhalb der Wurzel sind nicht erlaubt: {path}", "Caminhos absolutos ou fora da raiz não são permitidos: {path}"),
    "ev_bad_path": ("パスを解釈できません: {path}", "The path cannot be interpreted: {path}", "无法解析路径：{path}", "경로를 해석할 수 없습니다: {path}", "No se puede interpretar la ruta: {path}", "Le chemin est illisible : {path}", "Der Pfad ist nicht interpretierbar: {path}", "Não foi possível interpretar o caminho: {path}"),
    # ---- 警告（進捗ルール）/ alerts ----
    "al_overdue": ("期限を {late} 日超過（基準 {base} 日）", "{late} day(s) past the due date (threshold {base})", "已超期 {late} 天（基准 {base} 天）", "기한을 {late}일 초과(기준 {base}일)", "{late} día(s) de retraso (umbral {base})", "{late} jour(s) de retard (seuil {base})", "{late} Tag(e) überfällig (Schwelle {base})", "{late} dia(s) de atraso (limite {base})"),
    "al_stalled": ("「{status}」のまま {held} 日動きなし（基準 {base} 日）", "No movement for {held} day(s) in \"{status}\" (threshold {base})", "停留在“{status}”已 {held} 天无进展（基准 {base} 天）", "'{status}' 상태로 {held}일간 움직임 없음(기준 {base}일)", "Sin avance durante {held} día(s) en «{status}» (umbral {base})", "Aucun mouvement depuis {held} jour(s) en « {status} » (seuil {base})", "{held} Tag(e) ohne Bewegung in „{status}“ (Schwelle {base})", "Sem movimento há {held} dia(s) em «{status}» (limite {base})"),
    "al_blocked_long": ("ブロックが {held} 日続いている（基準 {base} 日）", "Blocked for {held} day(s) (threshold {base})", "已阻塞 {held} 天（基准 {base} 天）", "차단이 {held}일째 계속됨(기준 {base}일)", "Bloqueada desde hace {held} día(s) (umbral {base})", "Bloquée depuis {held} jour(s) (seuil {base})", "Seit {held} Tag(en) blockiert (Schwelle {base})", "Bloqueada há {held} dia(s) (limite {base})"),
    "al_unassigned": ("担当者が {held} 日決まっていない（基準 {base} 日）", "No assignee for {held} day(s) (threshold {base})", "{held} 天未确定负责人（基准 {base} 天）", "담당자가 {held}일째 정해지지 않음(기준 {base}일)", "Sin responsable desde hace {held} día(s) (umbral {base})", "Aucun assigné depuis {held} jour(s) (seuil {base})", "Seit {held} Tag(en) ohne Zuständigen (Schwelle {base})", "Sem responsável há {held} dia(s) (limite {base})"),
    "al_not_started": ("期限まで残り {remaining} 日だが未着手（基準 {base} 日）", "{remaining} day(s) until the due date but not started (threshold {base})", "距截止还有 {remaining} 天但尚未开始（基准 {base} 天）", "기한까지 {remaining}일 남았지만 미착수(기준 {base}일)", "Quedan {remaining} día(s) para el vencimiento y no ha empezado (umbral {base})", "Plus que {remaining} jour(s) avant l'échéance et pas commencée (seuil {base})", "Noch {remaining} Tag(e) bis zur Fälligkeit, aber nicht begonnen (Schwelle {base})", "Faltam {remaining} dia(s) para o prazo e não foi iniciada (limite {base})"),
    "al_agent_failed": ("役割AI {agent} の実行が {status} で終わりました{detail}（人が引き取ってください）", "The role AI {agent}'s run ended as {status}{detail} (a person should take over)", "角色 AI {agent} 的执行以 {status} 结束{detail}（请人工接手）", "역할 AI {agent}의 실행이 {status}(으)로 끝났습니다{detail}(사람이 맡아 주세요)", "La ejecución de la IA de rol {agent} terminó en {status}{detail} (una persona debe asumirla)", "L'exécution de l'IA de rôle {agent} s'est terminée en {status}{detail} (une personne doit reprendre)", "Der Lauf der Rollen-KI {agent} endete mit {status}{detail} (bitte von Hand übernehmen)", "A execução da IA de função {agent} terminou em {status}{detail} (uma pessoa deve assumir)"),
    "al_agent_rejected": ("役割AI {agent} の成果が差し戻されました{detail}（人が引き取るか、aipmo agents run でもう一度任せてください）", "The role AI {agent}'s result was sent back{detail} (take it over, or hand it over again with aipmo agents run)", "角色 AI {agent} 的成果被退回{detail}（请人工接手，或用 aipmo agents run 再次委托）", "역할 AI {agent}의 성과가 반려되었습니다{detail}(사람이 맡거나 aipmo agents run으로 다시 맡기세요)", "El resultado de la IA de rol {agent} fue devuelto{detail} (asúmalo o vuelva a encargarlo con aipmo agents run)", "Le résultat de l'IA de rôle {agent} a été renvoyé{detail} (reprenez-le ou confiez-le à nouveau avec aipmo agents run)", "Das Ergebnis der Rollen-KI {agent} wurde zurückgewiesen{detail} (selbst übernehmen oder mit aipmo agents run neu vergeben)", "O resultado da IA de função {agent} foi devolvido{detail} (assuma ou entregue de novo com aipmo agents run)"),

    # ---- 診断 / diagnoses ----
    "dg_overload": ("{name} の負荷が上限を超えています（{load}/{cap}）", "{name} is over capacity ({load}/{cap})", "{name} 的负荷超过上限（{load}/{cap}）", "{name}의 부하가 상한을 넘었습니다({load}/{cap})", "{name} supera su capacidad ({load}/{cap})", "{name} dépasse sa capacité ({load}/{cap})", "{name} ist überlastet ({load}/{cap})", "{name} excede a capacidade ({load}/{cap})"),
    "dg_overload_ev1": ("未完了 {load} 件に対して上限 {cap} 件", "{load} open task(s) against a limit of {cap}", "未完成 {load} 项，上限为 {cap} 项", "미완료 {load}건, 상한 {cap}건", "{load} tarea(s) abiertas frente a un límite de {cap}", "{load} tâche(s) ouvertes pour une limite de {cap}", "{load} offene Aufgabe(n) bei einem Limit von {cap}", "{load} tarefa(s) abertas para um limite de {cap}"),
    "dg_overload_ev2": ("うち点数 60 以上の高リスクが {n} 件: {titles}", "{n} of them are high-risk (score 60 or more): {titles}", "其中得分 60 以上的高风险任务 {n} 项：{titles}", "그중 점수 60 이상의 고위험이 {n}건: {titles}", "{n} de ellas son de alto riesgo (60 puntos o más): {titles}", "dont {n} à haut risque (score 60 ou plus) : {titles}", "davon {n} mit hohem Risiko (ab 60 Punkten): {titles}", "{n} delas são de alto risco (60 pontos ou mais): {titles}"),
    "dg_project_risk": ("プロジェクト {project} のリスクが {level}（警告 {n} 件）", "Project {project} is at {level} risk ({n} alert(s))", "项目 {project} 的风险为 {level}（警告 {n} 条）", "프로젝트 {project}의 위험이 {level}입니다(경고 {n}건)", "El proyecto {project} está en riesgo {level} ({n} alerta(s))", "Le projet {project} est à risque {level} ({n} alerte(s))", "Projekt {project} hat Risikostufe {level} ({n} Warnung(en))", "O projeto {project} está em risco {level} ({n} alerta(s))"),
    "dg_project_ev": ("{title} — {message}", "{title} — {message}", "{title} — {message}", "{title} — {message}", "{title} — {message}", "{title} — {message}", "{title} — {message}", "{title} — {message}"),
    "dg_agent_failure": ("役割AI {agent} の実行が失敗しています（{n} 件）", "The role AI {agent} has failing runs ({n})", "角色 AI {agent} 的执行失败（{n} 项）", "역할 AI {agent}의 실행이 실패하고 있습니다({n}건)", "La IA de rol {agent} tiene ejecuciones fallidas ({n})", "L'IA de rôle {agent} a des exécutions en échec ({n})", "Die Rollen-KI {agent} hat fehlgeschlagene Läufe ({n})", "A IA de função {agent} tem execuções com falha ({n})"),
    "dg_agent_ev": ("{title}: {error}", "{title}: {error}", "{title}：{error}", "{title}: {error}", "{title}: {error}", "{title} : {error}", "{title}: {error}", "{title}: {error}"),
    "dg_collection": ("進捗の自動収集がうまくいっていません", "Automatic progress collection is not working well", "自动进度收集运行不佳", "진행 상황 자동 수집이 잘 되지 않습니다", "La recogida automática de avance no funciona bien", "La collecte automatique de l'avancement ne se passe pas bien", "Die automatische Fortschrittserfassung läuft nicht richtig", "A coleta automática de progresso não está funcionando bem"),
    "dg_coll_all_failed": ("全ての収集元が失敗: {errors}", "Every source failed: {errors}", "所有收集源均失败：{errors}", "모든 수집원이 실패: {errors}", "Fallaron todas las fuentes: {errors}", "Toutes les sources ont échoué : {errors}", "Alle Quellen sind fehlgeschlagen: {errors}", "Todas as fontes falharam: {errors}"),
    "dg_coll_failed": ("再読み込みの失敗が {n} 件", "{n} re-read failure(s)", "重新读取失败 {n} 项", "재읽기 실패 {n}건", "{n} fallo(s) al releer", "{n} échec(s) de relecture", "{n} Fehler beim erneuten Lesen", "{n} falha(s) ao reler"),
    "dg_coll_stale": ("最後の収集から {hours} 時間", "{hours} hour(s) since the last collection", "距上次收集已 {hours} 小时", "마지막 수집 후 {hours}시간", "{hours} hora(s) desde la última recogida", "{hours} heure(s) depuis la dernière collecte", "{hours} Stunde(n) seit der letzten Erfassung", "{hours} hora(s) desde a última coleta"),
    "dg_capacity": ("割り当て先の空きが無い未完了タスクが {n} 件あります", "{n} open task(s) have nobody with room to take them", "有 {n} 项未完成任务没有可分配的人选", "배정할 여유가 없는 미완료 작업이 {n}건 있습니다", "Hay {n} tarea(s) abiertas sin nadie con hueco para asumirlas", "{n} tâche(s) ouvertes sans personne de disponible", "{n} offene Aufgabe(n) ohne freie Kapazität", "Há {n} tarefa(s) abertas sem ninguém com espaço"),
    "dg_example": ("例: {titles}", "e.g. {titles}", "例如：{titles}", "예: {titles}", "p. ej.: {titles}", "p. ex. : {titles}", "z. B.: {titles}", "p. ex.: {titles}"),
    "dg_estimate": ("見積りとペースでは期限に間に合わないタスクが {n} 件あります", "{n} task(s) cannot make their due date at the estimated pace", "按估算与节奏有 {n} 项任务赶不上截止日", "추정과 페이스로는 기한을 맞출 수 없는 작업이 {n}건 있습니다", "{n} tarea(s) no llegan a su vencimiento con el ritmo estimado", "{n} tâche(s) ne tiendront pas l'échéance au rythme estimé", "{n} Aufgabe(n) schaffen die Frist beim geschätzten Tempo nicht", "{n} tarefa(s) não cumprem o prazo no ritmo estimado"),

    # ---- 生成されるタスクの題名 / generated task titles ----
    "t_followup": ("対応を決める: {title} — {reason}", "Decide how to respond: {title} — {reason}", "决定如何应对：{title} — {reason}", "대응 결정: {title} — {reason}", "Decidir la respuesta: {title} — {reason}", "Décider de la réponse : {title} — {reason}", "Reaktion festlegen: {title} — {reason}", "Decidir a resposta: {title} — {reason}"),
    "t_decide": ("対応を決める: {title}", "Decide how to respond: {title}", "决定如何应对：{title}", "대응 결정: {title}", "Decidir la respuesta: {title}", "Décider de la réponse : {title}", "Reaktion festlegen: {title}", "Decidir a resposta: {title}"),
    "t_wbs": ("WBS を確かめる: {id} {name} — {reason}", "Check the WBS: {id} {name} — {reason}", "核对 WBS：{id} {name} — {reason}", "WBS 확인: {id} {name} — {reason}", "Revisar el WBS: {id} {name} — {reason}", "Vérifier le WBS : {id} {name} — {reason}", "WBS prüfen: {id} {name} — {reason}", "Verificar o WBS: {id} {name} — {reason}"),
    "t_judgment": ("[判断] {title} → {remedy}", "[Judgment] {title} → {remedy}", "[判断] {title} → {remedy}", "[판단] {title} → {remedy}", "[Juicio] {title} → {remedy}", "[Jugement] {title} → {remedy}", "[Beurteilung] {title} → {remedy}", "[Julgamento] {title} → {remedy}"),
    "t_judgment_n": ("[判断] {title} → {remedy}（{attempt} 回目）", "[Judgment] {title} → {remedy} (attempt {attempt})", "[判断] {title} → {remedy}（第 {attempt} 次）", "[판단] {title} → {remedy}({attempt}회째)", "[Juicio] {title} → {remedy} (intento {attempt})", "[Jugement] {title} → {remedy} (tentative {attempt})", "[Beurteilung] {title} → {remedy} (Versuch {attempt})", "[Julgamento] {title} → {remedy} (tentativa {attempt})"),

    # ---- 判断の理由 / judgment rationale ----
    "ra_main": ("{title}。{remedy}を選んだ。根拠: {evidence}。{history}。{how}。", "{title}. Chose: {remedy}. Evidence: {evidence}. {history}. {how}.", "{title}。选择了：{remedy}。依据：{evidence}。{history}。{how}。", "{title}. 선택: {remedy}. 근거: {evidence}. {history}. {how}.", "{title}. Se eligió: {remedy}. Base: {evidence}. {history}. {how}.", "{title}. Choix : {remedy}. Éléments : {evidence}. {history}. {how}.", "{title}. Gewählt: {remedy}. Grundlage: {evidence}. {history}. {how}.", "{title}. Escolhida: {remedy}. Base: {evidence}. {history}. {how}."),
    "ra_history": ("過去の実績: 効いた {ok} 回・効かなかった {bad} 回（効く見込み {rate}）", "Track record: worked {ok} time(s), did not work {bad} time(s) (expected to work: {rate})", "历史实绩：有效 {ok} 次、无效 {bad} 次（预计有效率 {rate}）", "과거 실적: 효과 {ok}회·효과 없음 {bad}회(효과 예상 {rate})", "Historial: funcionó {ok} vez/veces, no funcionó {bad} (probabilidad de funcionar: {rate})", "Historique : a fonctionné {ok} fois, sans effet {bad} fois (chance de réussite : {rate})", "Bisherige Erfahrung: wirkte {ok}-mal, wirkte nicht {bad}-mal (erwartete Wirkung: {rate})", "Histórico: funcionou {ok} vez(es), não funcionou {bad} (chance de funcionar: {rate})"),
    "ra_no_history": ("過去の実績: まだ無い（見込み 50%）", "Track record: none yet (expected 50%)", "历史实绩：暂无（预计 50%）", "과거 실적: 아직 없음(예상 50%)", "Historial: aún ninguno (previsto 50 %)", "Historique : aucun pour l'instant (50 % attendu)", "Bisherige Erfahrung: noch keine (erwartet 50 %)", "Histórico: ainda nenhum (previsto 50%)"),
    "ra_auto": ("自律度 auto: 実行する", "Autonomy auto: it runs", "自主程度 auto：直接执行", "자율도 auto: 실행합니다", "Autonomía auto: se ejecuta", "Autonomie auto : exécuté", "Autonomie auto: wird ausgeführt", "Autonomia auto: é executada"),
    "ra_propose": ("自律度 propose: 提案して人の承認を待つ", "Autonomy propose: proposed and waits for human approval", "自主程度 propose：提议并等待人工批准", "자율도 propose: 제안하고 사람의 승인을 기다립니다", "Autonomía propose: se propone y espera la aprobación humana", "Autonomie propose : proposé, en attente d'approbation humaine", "Autonomie propose: wird vorgeschlagen und wartet auf Freigabe", "Autonomia propose: é proposta e aguarda aprovação humana"),

    # ---- 担当の提案の理由 / assignee suggestion reasons ----
    "sg_skill": ("スキル一致で選定（現在 {load}/{cap} 件）、一致: {labels}", "Chosen for a skill match (currently {load}/{cap}); matched: {labels}", "因技能匹配而选定（当前 {load}/{cap} 项），匹配：{labels}", "스킬 일치로 선정(현재 {load}/{cap}건), 일치: {labels}", "Elegido por coincidencia de habilidades (ahora {load}/{cap}); coincide: {labels}", "Choisi pour la compétence (actuellement {load}/{cap}) ; correspond : {labels}", "Wegen passender Fähigkeiten gewählt (derzeit {load}/{cap}); Treffer: {labels}", "Escolhido por correspondência de habilidades (agora {load}/{cap}); coincide: {labels}"),
    "sg_room": ("空き状況で選定（現在 {load}/{cap} 件）", "Chosen for availability (currently {load}/{cap})", "因有空闲而选定（当前 {load}/{cap} 项）", "여유 상황으로 선정(현재 {load}/{cap}건)", "Elegido por disponibilidad (ahora {load}/{cap})", "Choisi pour sa disponibilité (actuellement {load}/{cap})", "Wegen freier Kapazität gewählt (derzeit {load}/{cap})", "Escolhido por disponibilidade (agora {load}/{cap})"),
}

MESSAGES: dict[str, dict[str, str]] = {key: dict(zip(LANGS, row, strict=True)) for key, row in _M.items()}


def translate(lang: str | None, message: str, /, **params: Any) -> str:
    """文言を引く。無い言語は英語、無いキーはキーそのもの（落とさない）。"""
    code = normalize(lang) if lang else DEFAULT_LANG
    row = MESSAGES.get(message)
    if row is None:
        return message
    text = row.get(code) or row[DEFAULT_LANG]
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def translator(lang: str | None):
    """`t(key, **params)` を返す / a lookup bound to one language."""
    def t(message: str, /, **params: Any) -> str:
        return translate(lang, message, **params)

    return t


# -- 構造化された文章 / structured sentences ---------------------------------------------------
#
# 常駐が書く警告・診断・題名は、日本語の文章に加えて `{"key", "params"}`（または `{"text"}`）を添えて残す。
# 画面は、その構造から選んだ言語で組み立て直す。構造の無い古い記録は、書かれたままの文章を出す。
# The resident process stores alerts, diagnoses and titles as Japanese text plus a `{"key", "params"}`
# spec; screens rebuild the sentence from the spec in the chosen language. Old records without one keep
# their stored text.

LIST_SEPARATOR = {"ja": "、", "zh": "、"}


def spec(key: str, **params: Any) -> dict[str, Any]:
    """言語に依らない文章の部品 / a language-neutral sentence spec."""
    return {"key": key, "params": params}


def raw(text: str) -> dict[str, Any]:
    """訳さない文字列（タスク名・エラー文など、データ）/ untranslated data."""
    return {"text": text}


def render(lang: str | None, node: Any) -> str:
    """`spec` / `raw` / 文字列 / それらのリストを、`lang` の文章にする。入れ子の差し込みも再帰で解く。"""
    code = normalize(lang) if lang else DEFAULT_LANG
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return LIST_SEPARATOR.get(code, ", ").join(render(code, item) for item in node)
    if isinstance(node, dict):
        if "items" in node and "key" not in node:               # 区切りを指定した一覧
            return str(node.get("sep", ", ")).join(render(code, item) for item in node["items"])
        if "text" in node and "key" not in node:
            return str(node["text"])
        params = {k: render(code, v) if isinstance(v, (dict, list)) else v
                  for k, v in (node.get("params") or {}).items()}
        return translate(code, str(node.get("key", "")), **params)
    return str(node)


def localized(lang: str | None, stored: str, node: Any) -> str:
    """構造があればその言語で、無ければ（古い記録は）書かれたままの文章を返す。"""
    if node and normalize(lang or DEFAULT_LANG) != "ja":
        return render(lang, node)
    return stored


def is_japanese(lang: str | None) -> bool:
    return normalize(lang or DEFAULT_LANG) == "ja"


def task_title(lang: str | None, title: str, payload: dict[str, Any] | None) -> str:
    """生成されたタスクの題名。構造（payload.i18n.title）があれば `lang` で、無ければ書かれたまま。"""
    node = ((payload or {}).get("i18n") or {}).get("title")
    return localized(lang, title, node)


def suggestion_reason(lang: str | None, reason: str | None, payload: dict[str, Any] | None) -> str:
    """担当の提案の理由。構造の本文が、いまの理由と同じときだけ使う（古い構造を取り違えない）。"""
    stored = (payload or {}).get("i18n_suggestion") or {}
    if reason and stored.get("text") == reason:
        return localized(lang, reason, stored.get("spec"))
    return reason or ""


def localize_alert(lang: str | None, alert: dict[str, Any]) -> dict[str, Any]:
    if is_japanese(lang) or not alert.get("i18n"):
        return alert
    return {**alert, "message": render(lang, alert["i18n"])}


def localize_diagnosis(lang: str | None, diagnosis: dict[str, Any]) -> dict[str, Any]:
    node = diagnosis.get("i18n")
    if is_japanese(lang) or not node:
        return diagnosis
    return {**diagnosis, "title": render(lang, node["title"]),
            "evidence": [render(lang, e) for e in node.get("evidence") or []]}


def localize_briefing(lang: str | None, briefing: dict[str, Any],
                      reasons: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """ブリーフィングの文章を `lang` にする（元は変えない）。構造の無い古い項目は書かれたまま。

    `reasons` は、タスク id → その言語の点数の内訳（上位のタスクの理由に使う）。
    """
    if is_japanese(lang):
        return briefing
    out = dict(briefing)
    out["alerts"] = [localize_alert(lang, a) for a in briefing.get("alerts") or []]
    if briefing.get("judgment"):
        j = dict(briefing["judgment"])
        j["diagnoses"] = [localize_diagnosis(lang, d) for d in j.get("diagnoses") or []]
        j["recent"] = [{**r, "title": localized(lang, r.get("title", ""), (r.get("i18n") or {}).get("title"))}
                       for r in j.get("recent") or []]
        out["judgment"] = j
    if reasons is not None:
        out["top_priorities"] = [{**t, "reasons": reasons.get(t.get("id"), t.get("reasons"))}
                                 for t in briefing.get("top_priorities") or []]
    out["assignment_proposals"] = [
        {**a, "reason": localized(lang, a.get("reason"), (a.get("i18n") or {}).get("spec"))
         if a.get("i18n") else a.get("reason")} for a in briefing.get("assignment_proposals") or []]
    return out


__all__ = ["LANGS", "MESSAGES", "is_japanese", "localize_alert", "localize_briefing",
           "localize_diagnosis", "localized", "raw", "render", "spec", "suggestion_reason",
           "task_title", "translate", "translator"]

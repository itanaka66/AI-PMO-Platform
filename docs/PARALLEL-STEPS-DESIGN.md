# 並列ステップ実行の実装設計

## 1. 概要

### 現状
- **逐次実行のみ**：ステップを 1 つずつ順序通りに実行
- **ボトルネック**：3 つの独立した通知タスク（Slack #pmo / #dev / #safety）を送るのに、各 wait = 2s なら 6s かかる

### 目指す状態
- 依存関係のないステップ A と B を同時実行
- A の出力が B で必要なら、A 完了後に B 開始
- エラーハンドリング：個別失敗は他を止めない。全滅時のみ失敗扱い

### 期待効果
- テンプレート実行時間 **30~50% 短縮**（待機時間削減）
- 構文シンプル：`depends_on` で依存関係を明示的に

---

## 2. DSL 設計

### 2.1 基本シンタックス

```yaml
name: parallel_example
industry: software

steps:
  # Step 1: Transcript 取得（前提条件）
  - id: fetch_transcript
    adapter: teams
    action: get_transcript
    inputs:
      meeting_id: "{{ trigger.meeting_id }}"

  # Step 2, 3, 4: 並列実行（いずれも fetch_transcript に依存）
  - id: generate_minutes
    group: analysis              # グループ名（表示用）
    llm: { profile: default }
    prompt: minutes_ja
    inputs:
      transcript: "{{ steps.fetch_transcript.output.text }}"
    depends_on: [fetch_transcript]

  - id: extract_todos
    group: analysis
    llm: { profile: default }
    prompt: extract_todos_ja
    inputs:
      transcript: "{{ steps.fetch_transcript.output.text }}"
    depends_on: [fetch_transcript]

  - id: extract_decisions
    group: analysis
    llm: { profile: default }
    prompt: extract_decisions_ja
    inputs:
      transcript: "{{ steps.fetch_transcript.output.text }}"
    depends_on: [fetch_transcript]

  # Step 5: 複数の先行ステップに依存
  - id: register_jira
    adapter: jira
    action: create_issues
    inputs:
      todos: "{{ steps.extract_todos.output.items }}"
    depends_on: [extract_todos]

  # Step 6: 複数通知（相互独立）→ 並列実行
  - id: notify_pmo
    adapter: slack
    action: post_message
    inputs:
      channel: "#pmo"
      text: "{{ steps.generate_minutes.output.text }}"
    depends_on: [generate_minutes]

  - id: notify_decisions
    adapter: slack
    action: post_message
    inputs:
      channel: "#decisions"
      text: "{{ steps.extract_decisions.output.text }}"
    depends_on: [extract_decisions]

  - id: notify_todos
    adapter: slack
    action: post_message
    inputs:
      channel: "#todos"
      text: "{{ steps.register_jira.output.created }}"
    depends_on: [register_jira]
```

### 2.2 省略ルール

```yaml
# depends_on を省略した場合：前のステップに依存と見なす（逆方向互換性）
steps:
  - id: step_a
    adapter: ...
  
  - id: step_b
    adapter: ...
    # depends_on: [step_a] と同等

  - id: step_c
    adapter: ...
    # depends_on: [step_b] と同等（step_a には依存しない）
```

### 2.3 依存関係のルール

- `depends_on: []`（空配列）：並列実行開始時に即座に開始
- `depends_on` 省略：直前のステップに依存（backward compatible）
- 循環依存：YAML ロード時に検出・エラー
- 存在しないステップ参照：YAML ロード時に検出・エラー

---

## 3. 実行エンジン設計

### 3.1 実行フロー

```
┌─────────────────────────────────────────┐
│ YAML をパース、DAG（有向非環グラフ）生成 │
└─────────────────────────────────────────┘
            ↓
┌─────────────────────────────────────────┐
│ トポロジカルソート（実行順序決定）        │
│ → 同一レベル = 並列実行候補               │
└─────────────────────────────────────────┘
            ↓
┌─────────────────────────────────────────┐
│ ステップレベルごとに実行                 │
│ Level 1: fetch_transcript              │
│ Level 2: generate_minutes (↓)          │  ← 並列実行
│         extract_todos (↓)              │
│         extract_decisions (↓)          │
│ Level 3: register_jira                 │
│ Level 4: notify_* (↓)                  │  ← 並列実行
└─────────────────────────────────────────┘
```

### 3.2 並列実行のための選択肢

| 方式 | 長所 | 短所 | 採用 |
|------|------|------|------|
| **ThreadPoolExecutor** | シンプル、I/O 待機効率化 | GIL の影響、CPU バウンドは遅い | ✅ 推奨 |
| **asyncio** | 高速、細粒度制御 | 既存コードの rewrite コスト大 | ❌ |
| **multiprocessing** | CPU バウンド OK | オーバーヘッド大、メッセージング複雑 | ❌ |

→ **ThreadPoolExecutor + future を採用**

### 3.3 実装の骨組み

```python
# aipmo/engine/runner.py（修正版）

import concurrent.futures
from typing import Dict, List, Set
from dataclasses import dataclass

@dataclass
class ExecutionLevel:
    """同時実行できるステップグループ"""
    step_ids: List[str]
    level: int

class DAGAnalyzer:
    def __init__(self, steps: Dict[str, Step]):
        self.steps = steps
        self.graph = self._build_graph()
    
    def _build_graph(self) -> Dict[str, Set[str]]:
        """ステップ → 依存ステップのマッピング"""
        graph = {}
        for step_id, step in self.steps.items():
            depends_on = step.depends_on or self._infer_from_previous(step_id)
            graph[step_id] = set(depends_on)
        return graph
    
    def _detect_cycles(self):
        """循環依存を検出"""
        visited = set()
        rec_stack = set()
        
        def has_cycle(node):
            visited.add(node)
            rec_stack.add(node)
            for neighbor in self.graph.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.remove(node)
            return False
        
        for node in self.graph:
            if node not in visited:
                if has_cycle(node):
                    raise ValueError(f"Cyclic dependency detected involving {node}")
    
    def topological_levels(self) -> List[ExecutionLevel]:
        """トポロジカルソート → 実行レベルを決定"""
        self._detect_cycles()
        
        in_degree = {step_id: len(deps) for step_id, deps in self.graph.items()}
        levels = []
        
        while any(d > 0 for d in in_degree.values()):
            current_level = [sid for sid, d in in_degree.items() if d == 0 and sid not in [s for level in levels for s in level.step_ids]]
            
            if not current_level:
                raise ValueError("Impossible state: no nodes with in_degree 0")
            
            levels.append(ExecutionLevel(step_ids=current_level, level=len(levels)))
            
            # 入次数を更新
            for step_id in current_level:
                for dependent, deps in self.graph.items():
                    if step_id in deps:
                        in_degree[dependent] -= 1
        
        return levels

class ParallelRunner:
    def __init__(self, engine, max_workers=4):
        self.engine = engine
        self.max_workers = max_workers
        self.step_results = {}
    
    def run_template(self, template: Template, trigger: dict) -> RunResult:
        """テンプレート実行（並列対応版）"""
        
        analyzer = DAGAnalyzer(template.steps)
        levels = analyzer.topological_levels()
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for level in levels:
                futures = {}
                
                # Level 内のステップを並列送信
                for step_id in level.step_ids:
                    step = template.steps[step_id]
                    future = executor.submit(
                        self._execute_single_step,
                        step,
                        context={**self.engine.context, 'steps': self.step_results}
                    )
                    futures[step_id] = future
                
                # Level 内のすべてのステップの完了を待機
                done, not_done = concurrent.futures.wait(
                    futures.values(),
                    timeout=300  # タイムアウト 5 分
                )
                
                # 結果を収集
                for step_id, future in futures.items():
                    try:
                        result = future.result()
                        self.step_results[step_id] = result
                    except Exception as e:
                        # 個別ステップの失敗をログ
                        self.engine.log_error(step_id, e)
                        self.step_results[step_id] = StepFailure(step_id=step_id, error=str(e))
        
        return self._build_run_result()
    
    def _execute_single_step(self, step: Step, context: dict) -> dict:
        """単一ステップの実行（スレッドセーフ）"""
        # 既存の実行エンジン呼び出し
        return self.engine._execute_step(step, context)
```

### 3.4 出力参照の一貫性

同一レベル内のステップ C から別ステップ A の出力を参照する場合、**A が完了するまで待機**：

```yaml
steps:
  - id: a
    depends_on: []

  - id: c
    depends_on: []
    inputs:
      data: "{{ steps.a.output.result }}"  # ← A が先に実行済みなら使える
                                             # ← A がまだなら ResolutionError
```

**設計決定：**
- 参照先ステップが同じレベル内にあり、未完了の場合は **ResolutionError** を発行
- テンプレート作者が `depends_on` を明示することで防止可能
- テスト時に検出 → 設計段階で明確化

---

## 4. エラーハンドリング

### 4.1 ステップレベル

```python
class ParallelResult(BaseModel):
    level: int
    step_results: Dict[str, Union[StepSuccess, StepFailure]]
    partial_failure: bool  # 一部ステップが失敗したか
    
    @property
    def all_failed(self) -> bool:
        return all(isinstance(r, StepFailure) for r in self.step_results.values())

class StepFailure(BaseModel):
    step_id: str
    error: str
    error_type: str  # "timeout" / "adapter_error" / "llm_error" / etc.
    timestamp: datetime
```

### 4.2 実行ポリシー

| 状況 | 動作 |
|------|------|
| **1 つ失敗** | スキップ、他は続行 |
| **全部失敗** | テンプレート全体を失敗扱い |
| **タイムアウト** | スレッド強制終了、該当ステップを失敗扱い |
| **後続ステップが失敗依存** | 先行ステップ失敗時も、該当ステップは実行（出力なしで ResolutionError） |

### 4.3 ログ出力

```yaml
# 実行ログ例
[09:45:00] Level 1: fetch_transcript (seq) → OK (1.2s)
[09:45:01] Level 2: 並列実行開始
  ├─ generate_minutes (llm) → OK (2.1s)
  ├─ extract_todos (llm) → OK (2.0s)
  └─ extract_decisions (llm) → FAILED: Timeout (>5s)
[09:45:03] Level 3: register_jira (adapter, deps=[extract_todos]) → OK (1.5s)
[09:45:04] Level 4: 並列実行開始
  ├─ notify_pmo → OK (0.5s)
  ├─ notify_decisions → SKIPPED (upstream failure)
  └─ notify_todos → OK (0.5s)
[09:45:04] Run completed with 1 warning
```

---

## 5. テスト戦略

### 5.1 ユニットテスト

```python
# tests/test_parallel_runner.py

def test_parallel_execution_speeds_up():
    """並列実行が逐次実行より高速な事を確認"""
    
    template = Template.parse("""
    steps:
      - id: wait_a
        kind: expression
        script: time.sleep(1)
        depends_on: []
      
      - id: wait_b
        kind: expression
        script: time.sleep(1)
        depends_on: []
    """)
    
    runner = ParallelRunner(engine, max_workers=2)
    start = time.time()
    runner.run_template(template, {})
    elapsed = time.time() - start
    
    assert elapsed < 1.5, f"Expected <1.5s, got {elapsed}s"

def test_circular_dependency_detected():
    """循環依存が YAML ロード時に検出"""
    
    template_str = """
    steps:
      - id: a
        depends_on: [c]
      - id: b
        depends_on: [a]
      - id: c
        depends_on: [b]
    """
    
    with pytest.raises(ValueError, match="Cyclic dependency"):
        Template.parse(template_str)

def test_partial_failure_continues():
    """1 つ失敗しても他は続行"""
    
    template = Template.parse("""
    steps:
      - id: ok_step
        adapter: mock
        action: succeed
        depends_on: []
      
      - id: fail_step
        adapter: mock
        action: fail
        depends_on: []
      
      - id: dependent
        adapter: mock
        action: succeed
        depends_on: [ok_step]
    """)
    
    result = runner.run_template(template, {})
    
    assert result.steps['ok_step'].status == 'ok'
    assert result.steps['fail_step'].status == 'failed'
    assert result.steps['dependent'].status == 'ok'

def test_resolution_error_for_unfinished_dep():
    """未完了の依存参照が ResolutionError"""
    
    template = Template.parse("""
    steps:
      - id: slow
        adapter: mock
        action: slow_action
        depends_on: []
      
      - id: fast
        adapter: mock
        action: quick_action
        inputs:
          data: "{{ steps.slow.output.data }}"
        depends_on: []  # 明示的に並列指定
    """)
    
    # fast が slow の完了を待つまで参照できない設計
    # → テンプレート作者が depends_on: [slow] を記述すべき
```

### 5.2 統合テスト

```python
def test_meeting_to_tasks_parallel():
    """実テンプレート meeting_to_tasks を並列実行"""
    
    # Teams Transcript 取得（逐次）
    # ↓
    # 3 つの LLM 並列実行（議事録・TODO・決定事項）
    # ↓
    # Jira 起票・Slack 通知 並列実行
    
    result = runner.run_template('meeting_to_tasks.yaml', {
        'meeting_id': 'test-meeting-001'
    })
    
    assert result.overall_status == 'ok'
    assert 'generate_minutes' in result.steps
    assert result.steps['generate_minutes'].output.text is not None
```

### 5.3 パフォーマンステスト

```python
def test_parallel_speedup_ratio():
    """並列実行による高速化を定量化"""
    
    # 同じテンプレートを逐次・並列で実行
    seq_time = measure_sequential_time()  # 期待値: 6s
    par_time = measure_parallel_time()    # 期待値: 2s
    
    speedup = seq_time / par_time
    assert speedup > 2.5, f"Expected >2.5x speedup, got {speedup}x"
```

---

## 6. 移行計画

### Phase 1: 基盤実装（PR #1）
- [ ] DAGAnalyzer（依存グラフ解析）
- [ ] ParallelRunner（ThreadPoolExecutor 統合）
- [ ] ユニットテスト 12 件
- [ ] 後方互換性確認（既存テンプレート 490 件が動作）

### Phase 2: テンプレート活用（PR #2）
- [ ] 既存テンプレートで `depends_on` を活用（4 件修正）
- [ ] `group` メタデータ（表示用）を追加
- [ ] Web UI でグラフを可視化（DAG 図）

### Phase 3: 高度な使用パターン（PR #3）
- [ ] `max_workers` を config で制御可能に
- [ ] per-step timeout override
- [ ] エージェント内での並列ツール呼び出し対応

---

## 7. ドキュメント

### templates/examples/parallel_analysis.yaml
並列実行の実例テンプレート（見出しの分析を 3 つの LLM で同時実行）

### docs/PARALLEL.md
- 設計思想・トレードオフ
- ユースケース（何が高速化するか）
- トラブルシューティング（デッドロック、循環依存、タイムアウト）

---

## 8. 設計上の決定と理由

| 決定 | 理由 |
|------|------|
| **`depends_on` 明示** | 配布テンプレートが意図しない並列化で壊れるのを防ぐ |
| **ThreadPoolExecutor** | I/O 待機が主（API 呼び出し）→ GIL でも十分高速 |
| **部分失敗を続行** | PMO テンプレートは「一部情報欠けても通知」が多い |
| **出力参照エラー** | 一貫性を失うより、テンプレ作者が depends_on を書く方が安全 |
| **DAG ロード時検証** | 循環依存は実行時より開発時に検出する |

---

## 9. 懸念事項と対策

| 懸念 | 対策 |
|------|------|
| **スレッドセーフティ** | engine.context をスレッドローカルストレージに移す、or copy-on-read |
| **デッドロック** | Python スレッド実装が直列なため不可能。ただし future 待機で hang のリスク → タイムアウト設定 |
| **出力順序の非決定性** | JSON 出力は ID でソート → 再現性確保 |
| **既存テンプレート破損** | `depends_on` 省略時は旧動作（直前ステップ依存）を保持 |

---

## 10. 成功指標

- ✅ 既存テスト 543 件 100% パス
- ✅ 新規テスト 20 件追加（全部パス）
- ✅ 並列テンプレート実行時間 30%以上短縮（4s → 2.8s）
- ✅ ドキュメント・図版完備
- ✅ GitHub PR レビュー承認

---

**初版作成日：2026-09-03**  
**実装予定日：Phase 1: 2026-09 / Phase 2: 2026-10 / Phase 3: 2026-11**

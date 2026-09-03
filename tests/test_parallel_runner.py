"""並列ステップ実行エンジンのテスト"""
import pytest
from aipmo.dsl.schema import Step, StepKind, Template
from aipmo.engine.parallel import DAGAnalyzer, ParallelExecutor, ExecutionLevel


class TestDAGAnalyzer:
    """DAG 解析のテスト"""

    def test_simple_linear_dependency(self):
        """単純な直線依存"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1+1"),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2+2", depends_on=["a"]),
            Step(id="c", kind=StepKind.TRANSFORM, expression="3+3", depends_on=["b"]),
        ]

        analyzer = DAGAnalyzer(steps)

        assert analyzer.graph["a"] == set()
        assert analyzer.graph["b"] == {"a"}
        assert analyzer.graph["c"] == {"b"}

    def test_depends_on_omitted_uses_previous(self):
        """depends_on 省略時は直前ステップに依存（互換性）"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1"),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2"),  # depends_on なし
            Step(id="c", kind=StepKind.TRANSFORM, expression="3"),  # depends_on なし
        ]

        analyzer = DAGAnalyzer(steps)

        assert analyzer.graph["a"] == set()
        assert analyzer.graph["b"] == {"a"}
        assert analyzer.graph["c"] == {"b"}

    def test_depends_on_empty_list_means_no_dependency(self):
        """depends_on: [] は依存なし"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=[]),
            Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["a"]),
        ]

        analyzer = DAGAnalyzer(steps)

        assert analyzer.graph["a"] == set()
        assert analyzer.graph["b"] == set()  # 依存なし → 並列実行可能
        assert analyzer.graph["c"] == {"a"}

    def test_multiple_dependencies(self):
        """複数の依存関係"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=[]),
            Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["a", "b"]),
        ]

        analyzer = DAGAnalyzer(steps)

        assert analyzer.graph["a"] == set()
        assert analyzer.graph["b"] == set()
        assert analyzer.graph["c"] == {"a", "b"}

    def test_nonexistent_dependency_raises_error(self):
        """存在しないステップへの依存は ロード時に検出"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1"),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=["x"]),
        ]

        with pytest.raises(ValueError, match="存在しないステップ"):
            DAGAnalyzer(steps)

    def test_cyclic_dependency_detected(self):
        """循環依存を検出"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=["c"]),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=["a"]),
            Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["b"]),
        ]

        analyzer = DAGAnalyzer(steps)
        with pytest.raises(ValueError, match="循環依存"):
            analyzer.detect_cycles()

    def test_topological_sort_linear(self):
        """トポロジカルソート：線形"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=["a"]),
            Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["b"]),
        ]

        analyzer = DAGAnalyzer(steps)
        levels = analyzer.topological_levels()

        # 線形依存なので各レベルに1つずつ
        assert len(levels) == 3
        assert levels[0].step_ids == ["a"]
        assert levels[1].step_ids == ["b"]
        assert levels[2].step_ids == ["c"]

    def test_topological_sort_parallel(self):
        """トポロジカルソート：並列"""
        steps = [
            Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
            Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=[]),
            Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=[]),
            Step(id="d", kind=StepKind.TRANSFORM, expression="4", depends_on=["a", "b", "c"]),
        ]

        analyzer = DAGAnalyzer(steps)
        levels = analyzer.topological_levels()

        # 最初の3つが並列実行
        assert len(levels) == 2
        assert set(levels[0].step_ids) == {"a", "b", "c"}
        assert levels[1].step_ids == ["d"]

    def test_topological_sort_complex_dag(self):
        """トポロジカルソート：複雑な DAG"""
        steps = [
            Step(id="fetch", kind=StepKind.ADAPTER, adapter="teams", action="get_transcript", depends_on=[]),
            Step(id="analyze_a", kind=StepKind.LLM, llm=None, depends_on=["fetch"]),
            Step(id="analyze_b", kind=StepKind.LLM, llm=None, depends_on=["fetch"]),
            Step(id="analyze_c", kind=StepKind.LLM, llm=None, depends_on=["fetch"]),
            Step(id="merge", kind=StepKind.TRANSFORM, expression="merge", depends_on=["analyze_a", "analyze_b", "analyze_c"]),
        ]

        analyzer = DAGAnalyzer(steps)
        levels = analyzer.topological_levels()

        assert len(levels) == 3
        assert levels[0].step_ids == ["fetch"]
        assert set(levels[1].step_ids) == {"analyze_a", "analyze_b", "analyze_c"}
        assert levels[2].step_ids == ["merge"]

    def test_level_structure(self):
        """ExecutionLevel の構造"""
        level = ExecutionLevel(step_ids=["a", "b", "c"], level=1)

        assert level.level == 1
        assert len(level.step_ids) == 3


class TestParallelExecutor:
    """並列実行計画のテスト"""

    def test_plan_simple_template(self):
        """テンプレートから実行計画を生成"""
        template = Template(
            name="test",
            steps=[
                Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
                Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=[]),
                Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["a"]),
            ]
        )

        executor = ParallelExecutor(max_workers=2)
        plan = executor.plan(template)

        assert len(plan) == 2
        assert set(plan[0].step_ids) == {"a", "b"}  # a と b は並列
        assert plan[1].step_ids == ["c"]

    def test_get_dependencies(self):
        """ステップの依存関係を取得"""
        template = Template(
            name="test",
            steps=[
                Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
                Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=["a"]),
                Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["a", "b"]),
            ]
        )

        executor = ParallelExecutor()

        assert executor.get_dependencies("a", template) == set()
        assert executor.get_dependencies("b", template) == {"a"}
        assert executor.get_dependencies("c", template) == {"a", "b"}

    def test_get_dependents(self):
        """ステップに依存する後続ステップを取得"""
        template = Template(
            name="test",
            steps=[
                Step(id="a", kind=StepKind.TRANSFORM, expression="1", depends_on=[]),
                Step(id="b", kind=StepKind.TRANSFORM, expression="2", depends_on=["a"]),
                Step(id="c", kind=StepKind.TRANSFORM, expression="3", depends_on=["a"]),
                Step(id="d", kind=StepKind.TRANSFORM, expression="4", depends_on=["b", "c"]),
            ]
        )

        executor = ParallelExecutor()

        assert executor.get_dependents("a", template) == {"b", "c"}
        assert executor.get_dependents("b", template) == {"d"}
        assert executor.get_dependents("c", template) == {"d"}
        assert executor.get_dependents("d", template) == set()

    def test_max_workers_setting(self):
        """max_workers の設定"""
        executor = ParallelExecutor(max_workers=8)
        assert executor.max_workers == 8

        executor2 = ParallelExecutor()
        assert executor2.max_workers == 4  # デフォルト


class TestBackwardCompatibility:
    """逆方向互換性のテスト"""

    def test_legacy_template_without_depends_on(self):
        """depends_on なしのレガシーテンプレート"""
        # 既存のテンプレートは depends_on を指定していない
        template = Template(
            name="legacy",
            steps=[
                Step(id="step1", kind=StepKind.ADAPTER, adapter="teams", action="get"),
                Step(id="step2", kind=StepKind.LLM, llm=None),  # depends_on なし
                Step(id="step3", kind=StepKind.ADAPTER, adapter="slack", action="post"),  # depends_on なし
            ]
        )

        executor = ParallelExecutor()
        plan = executor.plan(template)

        # 逐次実行（各レベルに 1 つずつ）
        assert len(plan) == 3
        assert plan[0].step_ids == ["step1"]
        assert plan[1].step_ids == ["step2"]
        assert plan[2].step_ids == ["step3"]

    def test_mixed_depends_on_and_implicit(self):
        """depends_on を混在させる"""
        template = Template(
            name="mixed",
            steps=[
                Step(id="a", kind=StepKind.ADAPTER, adapter="teams", action="get"),
                Step(id="b", kind=StepKind.LLM, llm=None),  # depends_on なし → a に依存
                Step(id="c", kind=StepKind.LLM, llm=None, depends_on=[]),  # 明示的に独立
                Step(id="d", kind=StepKind.ADAPTER, adapter="slack", action="post", depends_on=["b", "c"]),
            ]
        )

        executor = ParallelExecutor()
        plan = executor.plan(template)

        assert len(plan) == 3
        assert set(plan[0].step_ids) == {"a", "c"}  # a は依存なし、c は独立 → 並列実行
        assert plan[1].step_ids == ["b"]  # b は a に依存
        assert plan[2].step_ids == ["d"]  # d は b と c に依存


class TestEdgeCases:
    """エッジケースのテスト"""

    def test_single_step_template(self):
        """1つのステップだけ"""
        template = Template(
            name="single",
            steps=[
                Step(id="only", kind=StepKind.ADAPTER, adapter="teams", action="get"),
            ]
        )

        executor = ParallelExecutor()
        plan = executor.plan(template)

        assert len(plan) == 1
        assert plan[0].step_ids == ["only"]

    def test_empty_template(self):
        """ステップなしテンプレート"""
        template = Template(name="empty", steps=[])

        executor = ParallelExecutor()
        plan = executor.plan(template)

        assert len(plan) == 0

    def test_all_parallel_no_deps(self):
        """すべてが独立（全部が同じレベル）"""
        template = Template(
            name="all_parallel",
            steps=[
                Step(id=f"step{i}", kind=StepKind.ADAPTER, adapter="slack", action="post", depends_on=[])
                for i in range(5)
            ]
        )

        executor = ParallelExecutor()
        plan = executor.plan(template)

        assert len(plan) == 1
        assert len(plan[0].step_ids) == 5


# 統合テスト（実行はテンプレート側なので、ここでは計画だけ）
class TestIntegration:
    """統合テスト"""

    def test_meeting_to_tasks_parallel_plan(self):
        """実テンプレート meeting_to_tasks の並列実行計画"""
        # Teams Transcript 取得 → 3 つの LLM 並列 → Jira/Slack 通知
        template = Template(
            name="meeting_to_tasks",
            steps=[
                Step(id="fetch_transcript", kind=StepKind.ADAPTER, adapter="teams", action="get_transcript", depends_on=[]),
                Step(id="generate_minutes", kind=StepKind.LLM, llm=None, depends_on=["fetch_transcript"]),
                Step(id="extract_todos", kind=StepKind.LLM, llm=None, depends_on=["fetch_transcript"]),
                Step(id="extract_decisions", kind=StepKind.LLM, llm=None, depends_on=["fetch_transcript"]),
                Step(id="register_jira", kind=StepKind.ADAPTER, adapter="jira", action="create_issues", depends_on=["extract_todos"]),
                Step(id="notify_pmo", kind=StepKind.ADAPTER, adapter="slack", action="post_message", depends_on=["generate_minutes"]),
                Step(id="notify_decisions", kind=StepKind.ADAPTER, adapter="slack", action="post_message", depends_on=["extract_decisions"]),
                Step(id="notify_todos", kind=StepKind.ADAPTER, adapter="slack", action="post_message", depends_on=["register_jira"]),
            ]
        )

        executor = ParallelExecutor()
        plan = executor.plan(template)

        # Level 0: Transcript 取得
        assert plan[0].step_ids == ["fetch_transcript"]

        # Level 1: 3 つの LLM 並列
        assert set(plan[1].step_ids) == {"generate_minutes", "extract_todos", "extract_decisions"}

        # Level 2: Jira + 2 つの Slack 通知（Jira と notify_pmo / notify_decisions は独立）
        assert set(plan[2].step_ids) == {"register_jira", "notify_pmo", "notify_decisions"}

        # Level 3: 最後の Slack 通知
        assert plan[3].step_ids == ["notify_todos"]

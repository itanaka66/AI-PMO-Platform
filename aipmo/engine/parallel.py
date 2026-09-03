"""並列ステップ実行エンジン。

複数のステップの依存関係を DAG として解析し、
同一レベルのステップを ThreadPoolExecutor で並列実行する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..dsl.schema import Step, Template

logger = logging.getLogger("aipmo.engine.parallel")


@dataclass
class ExecutionLevel:
    """同時実行できるステップグループ"""
    step_ids: list[str]
    level: int


class DAGAnalyzer:
    """ステップの依存関係を解析し、トポロジカルソートを行う"""

    def __init__(self, steps: list[Step]):
        self.steps = {step.id: step for step in steps}
        self.graph: dict[str, set[str]] = {}
        self._build_graph()

    def _build_graph(self) -> None:
        """ステップ → 依存ステップのマッピングを構築"""
        step_list = [s for s in self.steps.values()]
        
        for i, step in enumerate(step_list):
            if step.depends_on is not None:
                # 明示的に depends_on が指定されている場合
                depends = set(step.depends_on)
                
                # 存在しないステップ参照を検出
                for dep in depends:
                    if dep not in self.steps:
                        raise ValueError(
                            f"ステップ '{step.id}' が存在しないステップ '{dep}' に依存しています"
                        )
                
                self.graph[step.id] = depends
            elif step.depends_on == []:
                # 空配列 = 依存なし
                self.graph[step.id] = set()
            else:
                # depends_on 省略 = 直前のステップに依存（互換性維持）
                if i > 0:
                    self.graph[step.id] = {step_list[i - 1].id}
                else:
                    self.graph[step.id] = set()

    def detect_cycles(self) -> None:
        """循環依存を検出"""
        visited = set()
        rec_stack = set()

        def has_cycle(node: str) -> bool:
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
                    raise ValueError(
                        f"循環依存が検出されました。ステップ '{node}' に関わるループを確認してください"
                    )

    def topological_levels(self) -> list[ExecutionLevel]:
        """トポロジカルソート → 実行レベルを決定"""
        self.detect_cycles()
        
        # 入次数を計算
        in_degree = {step_id: len(deps) for step_id, deps in self.graph.items()}
        levels = []
        processed = set()
        
        while len(processed) < len(self.steps):
            # 入次数が 0 のノード（依存が全て完了したノード）を取得
            current_level = [
                sid for sid, d in in_degree.items()
                if d == 0 and sid not in processed
            ]
            
            if not current_level:
                # ここに到達することは理論上ない（循環依存チェック済み）
                raise ValueError("不可能な状態: 入次数が 0 のノードが無い")
            
            levels.append(ExecutionLevel(step_ids=current_level, level=len(levels)))
            processed.update(current_level)
            
            # 次レベルのために入次数を更新
            for step_id in current_level:
                for dependent, deps in self.graph.items():
                    if step_id in deps:
                        in_degree[dependent] -= 1
        
        return levels

    def get_graph(self) -> dict[str, set[str]]:
        """依存グラフを返す（テスト用）"""
        return self.graph


class ParallelExecutor:
    """並列実行計画を実行"""
    
    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
    
    def plan(self, template: Template) -> list[ExecutionLevel]:
        """テンプレートから実行計画（実行レベルのリスト）を生成"""
        analyzer = DAGAnalyzer(template.steps)
        return analyzer.topological_levels()

    def get_dependencies(self, step_id: str, template: Template) -> set[str]:
        """指定したステップの依存ステップを取得"""
        analyzer = DAGAnalyzer(template.steps)
        return analyzer.graph.get(step_id, set())

    def get_dependents(self, step_id: str, template: Template) -> set[str]:
        """指定したステップに依存するステップを取得"""
        analyzer = DAGAnalyzer(template.steps)
        dependents = set()
        for sid, deps in analyzer.graph.items():
            if step_id in deps:
                dependents.add(sid)
        return dependents

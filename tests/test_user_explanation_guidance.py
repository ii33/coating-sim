"""User-facing explanation guidance stays ahead of implementation details."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UserExplanationGuidanceTests(unittest.TestCase):
    def test_assistant_prompt_uses_a_user_first_explanation_order(self):
        prompt = (ROOT / "agent.md").read_text(encoding="utf-8")
        for phrase in ("可观察现象", "物理意义", "解决步骤", "可读标题", "可核验链接", "用户明确询问代码"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_manual_has_direct_settings_and_algorithm_entry_points(self):
        manual = (ROOT / "docs/software_manual.html").read_text(encoding="utf-8")
        for section in ("settings", "algorithm_selection"):
            with self.subTest(section=section):
                self.assertIn(f'id="{section}"', manual)
        self.assertIn('<a href="#settings">设置入门</a>', manual)
        self.assertIn('<a href="#algorithm_selection">算法选型</a>', manual)
        settings = manual.split('<section id="settings">', 1)[1].split("</section>", 1)[0]
        self.assertIn("刷新模型", settings)
        self.assertIn("可能消耗额度或产生费用", settings)
        self.assertIn("系统安全凭据库", settings)

        algorithm = manual.split('<section id="algorithm_selection">', 1)[1].split("</section>", 1)[0]
        details = algorithm.index("<details><summary>算法接口、参数与数值边界（按需展开）</summary>")
        user_guide = algorithm[:details]
        self.assertIn("<table>", user_guide)
        for phrase in ("时间采样", "物理模型", "R/T/A", "候选", "下一步核对办法"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, user_guide)
        self.assertNotIn("algorithm_paths.py", user_guide)

    def test_readmes_link_to_user_tasks_and_keep_the_offline_algorithm_atlas(self):
        for name in ("README.md", "README.en.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(readme=name):
                self.assertIn("software_manual.html#settings", text)
                self.assertIn("software_manual.html#algorithm_selection", text)
                self.assertIn("docs/algorithms.html", text)


if __name__ == "__main__":
    unittest.main()

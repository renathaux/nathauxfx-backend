import ast
from pathlib import Path
import unittest


class EmailCredentialSecurityTests(unittest.TestCase):
    def test_feedback_password_is_loaded_from_environment(self):
        api_path = Path(__file__).resolve().parents[1] / "api.py"
        tree = ast.parse(api_path.read_text(encoding="utf-8"))

        assignments = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "FEEDBACK_APP_PASSWORD"
                for target in node.targets
            )
        ]

        self.assertEqual(len(assignments), 1)
        assignment = assignments[0]
        self.assertNotIsInstance(assignment, ast.Constant)
        self.assertIn("FEEDBACK_APP_PASSWORD", ast.unparse(assignment))
        self.assertIn("os.getenv", ast.unparse(assignment))

    def test_feedback_login_never_uses_a_password_literal(self):
        api_path = Path(__file__).resolve().parents[1] / "api.py"
        tree = ast.parse(api_path.read_text(encoding="utf-8"))
        login_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "login"
        ]
        self.assertTrue(login_calls)
        for call in login_calls:
            self.assertGreaterEqual(len(call.args), 2)
            self.assertFalse(
                isinstance(call.args[1], ast.Constant)
                and isinstance(call.args[1].value, str)
            )


if __name__ == "__main__":
    unittest.main()

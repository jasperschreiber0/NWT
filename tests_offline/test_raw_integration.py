"""Offline behavioral checks; no broker, DB, or application imports."""
import ast
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'nwt_agents'))
import opportunity_outcomes as lanes


class RawIntegrationTests(unittest.TestCase):
    def test_identity_separates_versions_and_polls(self):
        args = ('S', 'SPY', '2026-09-06', 'C')
        self.assertEqual(lanes.opportunity_id_for(*args), lanes.opportunity_id_for(*args))
        self.assertNotEqual(lanes.opportunity_id_for(*args, 1), lanes.opportunity_id_for(*args, 2))
        self.assertNotEqual(lanes.opportunity_id_for(*args, 1, 'a'), lanes.opportunity_id_for(*args, 1, 'b'))

    def test_analytics_failure_recovers_connection_and_returns_canonical_id(self):
        import json
        tree = ast.parse((ROOT / 'nwt_agents/shared_context.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'log_decision_input')
        namespace = {'json': json, 'log_system_event': MagicMock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<isolated function>', 'exec'), namespace)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (42,)
        with patch.object(lanes, 'upsert_outcome', side_effect=RuntimeError('DB unavailable')):
            result = namespace['log_decision_input'](
                conn, '2026-09-06', 'SPY', 'S', 'C', {}, 5, 'A', False, 'CANDIDATE')
        self.assertEqual(result, 42)
        conn.commit.assert_called_once()
        conn.rollback.assert_called_once()
        namespace['log_system_event'].assert_not_called()

    def test_unknown_quantity_is_not_fabricated(self):
        import json
        tree = ast.parse((ROOT / 'nwt_agents/shared_context.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'log_decision_input')
        ns = {'json': json, 'log_system_event': MagicMock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<isolated function>', 'exec'), ns)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (42,)
        with patch.object(lanes, 'upsert_outcome') as write:
            ns['log_decision_input'](conn, '2026-09-06', 'SPY', 'S', 'C', {}, 5, 'A', False, 'CANDIDATE')
        self.assertIsNone(write.call_args.args[3]['proposed_qty'])
        self.assertEqual(write.call_args.args[3]['source_decision_id'], 42)


if __name__ == '__main__':
    unittest.main()


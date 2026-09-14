import ast
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock


def test_learning_statistics_can_be_logged_without_losing_decimal_precision():
    source=Path(__file__).resolve().parents[1]/'nwt_agents/shared_context.py'
    tree=ast.parse(source.read_text())
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='log_system_event')
    namespace={'json':json}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<logging>','exec'),namespace)
    conn=MagicMock()
    namespace['log_system_event'](conn,'INFO','learning_agent','summary',{'expectancy':Decimal('0.1234567890123456789')})
    args=conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert json.loads(args[1][-1])['expectancy']=='0.1234567890123456789'
    conn.commit.assert_called_once()

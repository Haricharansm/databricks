import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ssis_fixes import component_specs, repair_notebook, CELL_BREAK

FIX = Path(__file__).parent / 'fixtures'
DTSX = (FIX / 'SharePointList.dtsx').read_text()
NOTEBOOK = (FIX / 'sharepoint_notebook.py').read_text()


class FakeWidgets:
    def __init__(self, values): self.values = values
    def text(self, name, default, label=''): self.values.setdefault(name, default)
    def dropdown(self, name, default, choices, label=''): self.values.setdefault(name, default)
    def get(self, name): return self.values[name]


class FakeSpark:
    def createDataFrame(self, rows, schema):
        frame = types.SimpleNamespace(rows=rows, columns=[f.name for f in schema.fields])
        frame.createOrReplaceTempView = lambda name: setattr(frame, 'view', name)
        return frame


class Response:
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return self.body


class RepairTests(unittest.TestCase):
    def test_specs_read_rest_source(self):
        spec = component_specs(DTSX)['Package\\Data Flow Task\\REST Source']
        self.assertEqual(spec['type'], 'CozyRoc.RestSource')
        self.assertEqual(spec['props']['ResourceParameter'], 'list_id=ClauseMetadata')
        self.assertEqual(len(spec['columns']), 7)

    def test_original_is_invalid_and_repair_is_valid(self):
        with self.assertRaises(SyntaxError):
            ast.parse(NOTEBOOK)
        fixed, notes = repair_notebook(NOTEBOOK, component_specs(DTSX))
        ast.parse(fixed)
        self.assertNotIn('%DF%', fixed)
        self.assertNotIn('from curses import meta', fixed)
        self.assertEqual(len(notes), 3)

    def test_unknown_component_becomes_clear_error(self):
        nb = NOTEBOOK.replace('REST Source', 'Mystery Thing')
        fixed, notes = repair_notebook(nb, component_specs(DTSX))
        ast.parse(fixed)
        self.assertIn('raise NotImplementedError', fixed)
        self.assertIn('could not be converted', notes[-1])

    def test_generated_cell_reads_all_pages(self):
        fixed, _ = repair_notebook(NOTEBOOK, component_specs(DTSX))
        cell = fixed.split(CELL_BREAK)[-1]
        calls = []
        pages = {
            'first': {'value': [{'id': '1', 'fields': {'ID': 1, 'Title': 'A', 'ClauseName': 'N1'}}],
                      '@odata.nextLink': 'https://graph.microsoft.com/next'},
            'next': {'value': [{'id': '2', 'fields': {'ID': 2, 'Title': 'B', 'RequiresInput': True}}]},
        }
        def get(url, headers=None, timeout=None):
            calls.append(url)
            if url.endswith('/sites/contoso.sharepoint.com:/sites/Legal'):
                return Response({'id': 'site-123'})
            return Response(pages['next' if url.endswith('/next') else 'first'])
        fake_requests = types.SimpleNamespace(
            post=lambda *a, **k: Response({'access_token': 'tok'}), get=get,
            utils=types.SimpleNamespace(quote=lambda s: s))
        widgets = FakeWidgets({'sharepoint_host': 'contoso.sharepoint.com', 'sharepoint_site_path': 'sites/Legal'})
        env = {'dbutils': types.SimpleNamespace(widgets=widgets, secrets=types.SimpleNamespace(get=lambda s, k: 'secret')),
               'spark': FakeSpark()}
        with patch.dict(sys.modules, {'requests': fake_requests}):
            try:
                import pyspark  # noqa: F401
            except ImportError:
                stub = types.ModuleType('pyspark.sql.types')
                stub.StringType = lambda: 'string'
                stub.StructField = lambda n, t, nullable: types.SimpleNamespace(name=n)
                stub.StructType = lambda fields: types.SimpleNamespace(fields=fields)
                sys.modules.update({'pyspark': types.ModuleType('pyspark'), 'pyspark.sql': types.ModuleType('pyspark.sql'), 'pyspark.sql.types': stub})
            exec(compile(cell, 'cell', 'exec'), env)
        frame = env['Package_Data_Flow_Task_REST_Source']
        self.assertEqual(frame.view, 'Package_Data_Flow_Task_REST_Source')
        self.assertEqual(frame.columns[0], 'fields.ClauseName')
        self.assertEqual(len(frame.rows), 2)
        row = dict(zip(frame.columns, frame.rows[1]))
        self.assertEqual((row['fields.ID'], row['fields.Title'], row['fields.RequiresInput'], row['fields.ClauseName']), ('2', 'B', 'True', None))
        self.assertIn('/sites/site-123/lists/ClauseMetadata/items?expand=fields&$top=200', calls[1])


if __name__ == '__main__':
    unittest.main()


class SparkSqlCellTests(unittest.TestCase):
    CELL = ('Package_Create_Staging_Table = f"""\nDROP TABLE IF EXISTS dbo.Stg_Customer\n;\n'
            'CREATE OR REPLACE TABLE dbo.Stg_Customer (\n  CustomerID INT NOT NULL PRIMARY KEY,\n'
            '  City STRING NULL,\n  LoadDate timestamp NOT NULL DEFAULT current_timestamp(););"""\n'
            'spark.sql(Package_Create_Staging_Table)\n')

    def test_split_and_repair(self):
        from ssis_fixes import fix_sql_cells
        out, notes = fix_sql_cells(self.CELL)
        ast.parse(out)
        self.assertIn('Package_Create_Staging_Table_1 = f"""DROP TABLE IF EXISTS dbo.Stg_Customer"""\nspark.sql(Package_Create_Staging_Table_1)', out)
        self.assertIn("DEFAULT current_timestamp())\nTBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')", out)
        self.assertIn('City STRING,', out)
        self.assertNotIn(';', out)
        self.assertEqual(fix_sql_cells(out), (out, []))

    def test_clean_cell_untouched(self):
        from ssis_fixes import fix_sql_cells
        cell = 'X = f"""SELECT {n} AS n"""\nspark.sql(X)\n'
        self.assertEqual(fix_sql_cells(cell), (cell, []))

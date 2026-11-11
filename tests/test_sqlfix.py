import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from sqlfix import before_conversion, after_conversion

FIXTURE = Path(__file__).parent / 'fixtures' / 'missing_commas.sql'


class BeforeConversionTests(unittest.TestCase):
    def test_real_script_is_repaired(self):
        fixed, notes = before_conversion('t.sql', FIXTURE.read_text())
        self.assertIn('[ModifiedBy] [int] NULL,\nPRIMARY KEY CLUSTERED \n(\n\t[RoleTypeID] ASC', fixed)
        self.assertIn('[ModifiedBy] [int] NULL,\n CONSTRAINT [PK_defRole] PRIMARY KEY', fixed)
        self.assertNotIn('Pk_RoleTypeID', fixed)
        self.assertNotIn('[PK_defRole],', fixed)
        self.assertEqual(len(notes), 3)

    def test_correct_script_unchanged(self):
        sql = "CREATE TABLE t ([a] INT NOT NULL, [b] INT NULL CHECK (b > 0), [c] INT UNIQUE,\n CONSTRAINT [pk] PRIMARY KEY CLUSTERED ([a] ASC))"
        self.assertEqual(before_conversion('t.sql', sql), (sql, []))

    def test_comments_and_strings_ignored(self):
        sql = "-- CREATE TABLE x (a INT PRIMARY KEY (a))\nCREATE TABLE t ([a] INT DEFAULT 'x PRIMARY KEY (', [b] INT,\nPRIMARY KEY ([a]))"
        self.assertEqual(before_conversion('t.sql', sql), (sql, []))

    def test_unknown_key_column_without_match_is_dropped(self):
        sql = "CREATE TABLE t ([a] INT, [b] INT,\nPRIMARY KEY ([zzz]))"
        fixed, notes = before_conversion('t.sql', sql)
        self.assertEqual(fixed, "CREATE TABLE t ([a] INT, [b] INT)")
        self.assertIn('removed a key constraint', notes[0])


class AfterConversionTests(unittest.TestCase):
    def test_nulls_removed_across_tables(self):
        sql = "CREATE TABLE `dbo`.`a` (`x` INT NULL, `y` INT NOT NULL);\n\nCREATE TABLE `dbo`.`b` (`z` STRING NULL);\nSELECT NULL;"
        fixed, notes = after_conversion('o.sql', sql)
        self.assertEqual(fixed, "CREATE TABLE `dbo`.`a` (`x` INT, `y` INT NOT NULL);\n\nCREATE TABLE `dbo`.`b` (`z` STRING);\nSELECT NULL;")
        self.assertIn('removed 2', notes[0])


class ConvertPipelineTests(unittest.TestCase):
    def test_fixes_wrap_the_engine(self):
        seen = {}
        def engine(files):
            seen['input'] = files[0]['sql']
            return {'engine': 'mock', 'files': [{'name': 'out.sql', 'sql': 'CREATE TABLE `dbo`.`u` (`a` INT NULL)'}], 'diagnostics': '[]', 'conversion_errors': '[]'}
        with patch.object(core, '_convert_sql_engine', engine):
            result = core.convert_sql([{'name': 'in.sql', 'sql': 'CREATE TABLE [dbo].[u] ([a] INT NULL\nPRIMARY KEY ([a]))'}])
        self.assertIn('NULL,\nPRIMARY KEY', seen['input'])
        self.assertEqual(result['files'][0]['sql'], 'CREATE TABLE `dbo`.`u` (`a` INT)')
        self.assertEqual(len(result['auto_fixes']), 2)
        self.assertIn('Automatic fixes applied', result['diagnostics'])


if __name__ == '__main__':
    unittest.main()

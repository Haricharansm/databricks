import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from email.message import EmailMessage
from conversion_workspace import conversion_workspace
from morpheus import Morpheus
from bladebridge import convert_package

class ScratchTests(unittest.TestCase):
    def scratch(self,errors):
        scratch=Mock();scratch.name='fixture-scratch';scratch.cleanup.side_effect=errors;return scratch
    def test_transient_lock_is_retried_without_warning(self):
        temp=self.scratch([PermissionError('locked'),None]);notes=[]
        with patch('conversion_workspace.tempfile.TemporaryDirectory',return_value=temp),patch('conversion_workspace.time.sleep'):
            with conversion_workspace('test-',notes.append) as path:self.assertEqual(path,'fixture-scratch')
        self.assertEqual(temp.cleanup.call_count,2);self.assertEqual(notes,[])
    def test_persistent_lock_does_not_discard_completed_output(self):
        temp=self.scratch(PermissionError('locked'));notes=[]
        with patch('conversion_workspace.tempfile.TemporaryDirectory',return_value=temp),patch('conversion_workspace.time.sleep'):
            with conversion_workspace('test-',notes.append):output='converted'
        self.assertEqual(output,'converted');self.assertEqual(temp.cleanup.call_count,5);self.assertIn('fixture-scratch',notes[0]);self.assertIn('source code and logs',notes[0])
    def test_original_engine_error_is_preserved(self):
        temp=self.scratch(PermissionError('locked'));error=RuntimeError('original engine conversion failure')
        with patch('conversion_workspace.tempfile.TemporaryDirectory',return_value=temp),patch('conversion_workspace.time.sleep'):
            with self.assertRaises(RuntimeError) as exc:
                with conversion_workspace('test-'):raise error
        self.assertIs(exc.exception,error);self.assertTrue(error.__notes__)
    def test_other_cleanup_fault_is_not_silenced(self):
        temp=self.scratch(OSError('disk fault'))
        with patch('conversion_workspace.tempfile.TemporaryDirectory',return_value=temp):
            with self.assertRaises(OSError):
                with conversion_workspace('test-'):pass
        self.assertEqual(temp.cleanup.call_count,1)
    def test_bladebridge_keeps_successful_artifacts_and_cleanup_diagnostic(self):
        with tempfile.TemporaryDirectory() as root:
            temp=Mock();temp.name=root;temp.cleanup.side_effect=PermissionError('lsp-server.log locked');client=Mock();message=EmailMessage();message.add_attachment('# Databricks notebook source\nprint("converted")',filename='Product.py');client.transpile.return_value=(message.as_string(),[])
            xml=Path(__file__).resolve().parents[1]/'samples/lakeflow/ProductPackage/ProductPackage.dtsx'
            with patch('conversion_workspace.tempfile.TemporaryDirectory',return_value=temp),patch('conversion_workspace.time.sleep'),patch('bladebridge.Morpheus',return_value=client),patch('bladebridge.status',return_value={'available':True}):
                files,diagnostics=convert_package({'name':'ProductPackage.dtsx','sql':xml.read_text()})
            self.assertTrue(any(f['name'].endswith('.py') for f in files));self.assertTrue(any(d['severity']==2 and 'still locked' in d['message'] for d in diagnostics));client.close.assert_called_once()

class ShutdownTests(unittest.TestCase):
    def client(self):
        c=Morpheus.__new__(Morpheus);c.process=Mock();c.process.pid=12345;c.process.poll.return_value=None;c.reader=Mock();c.reader.is_alive.return_value=False;c.log=Mock();return c
    def test_windows_stops_only_owned_pid_and_descendants(self):
        c=self.client()
        def finished(*a,**k):c.process.poll.return_value=0
        with patch('morpheus.os.name','nt'),patch('morpheus.subprocess.run',side_effect=finished) as kill:
            c.close();c.close()
        self.assertEqual(kill.call_count,1);self.assertEqual(kill.call_args.args[0],['taskkill','/PID','12345','/T','/F']);c.process.stdout.close.assert_called_once();c.log.close.assert_called_once()
    def test_failed_spawn_closes_stderr_log(self):
        log=Mock()
        with patch('morpheus.tempfile.TemporaryFile',return_value=log),patch('morpheus.subprocess.Popen',side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError):Morpheus(None,'.',command=['missing-engine'])
        log.close.assert_called_once()
    def test_live_reader_never_blocks_main_thread_on_stdout_close(self):
        c=self.client();c.process.poll.return_value=0;c.reader.is_alive.return_value=True
        with patch('morpheus.os.name','nt'):c.close()
        c.process.stdout.close.assert_not_called();c.process.stdin.close.assert_called_once();c.log.close.assert_called_once()

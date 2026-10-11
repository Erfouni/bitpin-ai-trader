import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from bitpin import personal_whale as pw
from test_personal_whale import FakeClient, NOW, head

ROOT = Path(__file__).resolve().parents[1]


def cli():
    spec = importlib.util.spec_from_file_location('personal_cli', str(ROOT / 'scripts' / 'personal_whale_shadow.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PersonalCLI(unittest.TestCase):
    def test_report_only_has_no_network_and_writes_unknown(self):
        client = Mock()
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(cli().main(['--state-dir', directory, '--report-only'], client=client, clock=lambda: NOW), 1)
            client.call.assert_not_called()
            result = json.loads((Path(directory) / 'personal-whale-shadow.json').read_text())
            self.assertEqual(result['status'], 'missing')
            self.assertTrue((Path(directory) / 'report.html').is_file())

    def test_collection_outputs_and_report_only_stale(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(cli().main(['--state-dir',directory], client=FakeClient(),clock=lambda:NOW,sleeper=lambda _:None), 0)
            self.assertEqual(cli().main(['--state-dir',directory,'--report-only'], clock=lambda:NOW+9001), 1)
            result = json.loads((Path(directory)/'personal-whale-shadow.json').read_text())
            self.assertIsNone(result['balance_wei'])
            self.assertFalse(result['live_enabled'])
            self.assertEqual(result['model_calls'],0)

    def test_nonempty_directory_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stderr',new=io.StringIO()):
            path = Path(directory)/'keep'
            path.write_text('original')
            self.assertEqual(cli().main(['--state-dir',directory,'--report-only']),2)
            self.assertEqual(path.read_text(),'original')

    def test_parent_other_collector_marker_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'.whale-shadow-state').write_text('other collector')
            child=Path(directory)/'child'
            with self.assertRaises(ValueError): pw.prepare_directory(child)
            self.assertFalse(child.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_junction_into_other_collector_child_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = root / 'other'
            target = other / 'child'
            target.mkdir(parents=True)
            (other / '.whale-shadow-state').write_text('other collector')
            script = root / 'junction.ps1'
            script.write_text('param($AliasPath, $TargetPath)\n'
                              'New-Item -ItemType Junction -Path $AliasPath -Target $TargetPath | Out-Null\n')
            alias = root / 'alias'
            subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-File', str(script),
                            str(alias), str(target)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with self.assertRaises(ValueError): pw.prepare_directory(alias)
                self.assertFalse((target / pw.MARKER).exists())
            finally:
                os.rmdir(str(alias))

    @unittest.skipIf(os.name=='nt','Windows symlink privilege varies')
    def test_symlink_state_and_outputs_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); alias=root/'alias'; target=root/'target'; target.mkdir()
            alias.symlink_to(target,target_is_directory=True)
            with self.assertRaises(ValueError): pw.prepare_directory(alias)
            isolated=pw.prepare_directory(root/'isolated')
            (isolated/'personal-whale-shadow.json').symlink_to(root/'protected')
            with self.assertRaises(ValueError): pw.prepare_directory(isolated)
            self.assertFalse((root/'protected').exists())

    def test_request_is_fixed_public_read_without_credentials(self):
        opener=Mock()
        opener.open.return_value=io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')
        self.assertEqual(pw.PersonalClient(opener).call('publicnode','eth_chainId',[]),'0x1')
        request=opener.open.call_args[0][0]
        self.assertEqual(request.full_url,pw.PROVIDERS['publicnode'])
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(json.loads(request.data)['method'],'eth_chainId')
        self.assertNotIn('Authorization',request.headers)

    def test_oversize_http_response_rejected(self):
        opener=Mock(); opener.open.return_value=io.BytesIO(b'0'*(1024*1024+1))
        with self.assertRaises(pw.PublicDataError): pw.PersonalClient(opener).call('publicnode','eth_chainId',[])

    def test_503_retry_after_preserved(self):
        opener=Mock()
        opener.open.side_effect=urllib.error.HTTPError('https://rpc.flashbots.net',503,'unavailable',{'Retry-After':'7200'},io.BytesIO(b''))
        with self.assertRaises(pw.PublicDataError) as caught:
            pw.PersonalClient(opener).call('flashbots','eth_chainId',[])
        self.assertEqual(caught.exception.retry_after,7200)

    def test_wrong_chain_or_stale_head_hides_metrics(self):
        for condition in ('chain','stale','lag','height','moving'):
            with tempfile.TemporaryDirectory() as directory:
                store=pw.PersonalStore(Path(directory)/'personal.sqlite3')
                try:
                    client=FakeClient(); original=client.call
                    def changed(provider,method,params):
                        result=original(provider,method,params)
                        if provider=='flashbots':
                            if condition=='chain' and method=='eth_chainId': return '0x2'
                            if condition=='stale' and method=='eth_getBlockByNumber': return head(timestamp=NOW-7201)
                            if condition=='lag' and params==['finalized',False]: return head(number=1000)
                            if condition=='height' and method=='eth_getBlockByNumber' and params[0]!='finalized': return head(number=1)
                            if condition=='moving' and method=='eth_getBlockByNumber' and len(client.calls)>=10:
                                result=dict(result,hash='0x'+'b'*64)
                        return result
                    client.call=changed
                    result=pw.PersonalCollector(store,client,clock=lambda:NOW,sleeper=lambda _:None).collect()
                    with self.subTest(condition=condition):
                        self.assertEqual(result['status'],'error')
                        self.assertIsNone(result['balance_wei'])
                finally: store.close()

    def test_reorganization_followup_does_not_use_old_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            store=pw.PersonalStore(Path(directory)/'personal.sqlite3'); client=FakeClient()
            try:
                client.now=NOW-pw.DAY
                pw.PersonalCollector(store,client,clock=lambda:NOW-pw.DAY,sleeper=lambda _:None).collect()
                client.now=NOW; client.old_hash='0x'+'b'*64
                result=pw.PersonalCollector(store,client,clock=lambda:NOW,sleeper=lambda _:None).collect()
                self.assertEqual(result['status'],'reorg')
                result=pw.PersonalCollector(store,client,clock=lambda:NOW,sleeper=lambda _:None).collect()
                self.assertEqual(result['status'],'ok')
                self.assertIsNone(result['change_24h_wei'])
            finally: store.close()

    def test_duplicate_keys_and_excessive_nesting_rejected(self):
        for raw in (b'{"jsonrpc":"2.0","id":2,"id":1,"result":"0x1"}',
                    ('{"jsonrpc":"2.0","id":1,"result":'+ '['*45+'0'+']'*45+'}').encode()):
            with self.subTest(raw=raw), self.assertRaises(pw.PublicDataError): pw.rpc_result(raw)


if __name__=='__main__': unittest.main()

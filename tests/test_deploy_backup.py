"""Real age + PostgreSQL dump roundtrip, with Docker calls replaced locally."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from deploy import Deployment, read_recovery
from deploy_runtime import DeployError

AGE=shutil.which('age')
KEYGEN=shutil.which('age-keygen')
PG_DUMP=shutil.which('pg_dump')
PG_RESTORE=shutil.which('pg_restore')
TEST_URL=os.environ.get('GAB_TEST_DATABASE_URL')


class BackupRunner:
    def __init__(self,real_run):self.real_run=real_run
    def run(self,args,data=None,output=None):
        if args[:3]==['docker','image','save']:
            Path(args[args.index('-o')+1]).write_bytes(b'artificial Docker image archive')
            return b''
        if 'pg_dump' in args:
            p=self.real_run([PG_DUMP,'-d',TEST_URL,'-Fc'],stdout=output,stderr=subprocess.PIPE)
            if p.returncode:raise DeployError('Local synthetic dump failed')
            return b''
        if args[:2]==['docker','exec'] and 'cat' in args:
            return b'[{"id":"synthetic-credential","data":"ENCRYPTED_SYNTHETIC_VALUE"}]'
        if args[:2]==['docker','exec'] and 'rm' in args:return b''
        raise AssertionError('Unexpected mocked Docker operation')


@unittest.skipUnless(AGE and KEYGEN and PG_DUMP and PG_RESTORE and TEST_URL,
    'age, age-keygen, pg tools and GAB_TEST_DATABASE_URL are needed for backup integration')
class BackupTests(unittest.TestCase):
    def test_actual_encryption_recovery_and_wrong_key(self):
        real_run=subprocess.run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);identity=root/'identity.agekey';wrong=root/'wrong.agekey'
            real_run([KEYGEN,'-o',str(identity)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            real_run([KEYGEN,'-o',str(wrong)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            recipient=real_run([KEYGEN,'-y',str(identity)],check=True,stdout=subprocess.PIPE).stdout.decode().strip()
            source=root/'synthetic-document.txt';source.write_text('Artificial investor document; no real personal data')
            config={'backup_dir':str(root/'backups'),'postgres_container':'synthetic-postgres',
                    'postgres_user':'synthetic-user','n8n_container':'synthetic-n8n','age_identity':str(identity)}
            deployment=Deployment(config=config,runner=BackupRunner(real_run),engine=object())
            deployment.state.update(release='synthetic-release',commit='synthetic-commit')
            deployment.snapshots={'crm':{'Image':'synthetic-crm-image'},'ocr':{'Image':'synthetic-ocr-image'}}
            deployment.previous={'id':'GAB02InvImp20261','active':False,'nodes':[],'connections':{}}
            deployment.all_workflows=[deployment.previous];deployment.backup_paths=[str(source)]
            deployment.recipient=recipient;deployment.n8n=lambda *args:b'credentials exported'
            def intercepted_run(args,**kwargs):
                if args[:3]==['docker','exec','-i'] and 'pg_restore' in args:
                    return real_run([PG_RESTORE,'--list'],**kwargs)
                return real_run(args,**kwargs)
            with patch('deploy.subprocess.run',side_effect=intercepted_run):deployment.backup()
            archive=Path(deployment.state['backup'])
            self.assertTrue(archive.exists())
            self.assertEqual(archive.stat().st_mode & 0o077,0)
            self.assertTrue(archive.with_suffix('.age.sha256').exists())
            self.assertNotIn(b'Artificial investor',archive.read_bytes())
            recovered=read_recovery(str(archive),str(identity))
            self.assertEqual(recovered['workflow-target.json'],[deployment.previous])
            self.assertEqual(recovered['containers.json'],deployment.snapshots)
            self.assertEqual(list((root/'backups').glob('gab-backup-*')),[])
            with self.assertRaises(DeployError):read_recovery(str(archive),str(wrong))

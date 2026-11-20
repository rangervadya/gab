import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from app.integration_config import api_key,configuration


class IntegrationConfigTests(unittest.TestCase):
    def test_private_file_overrides_environment_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'integrations.private.json'
            path.write_text(json.dumps({'DADATA_API_KEY':'PRIVATE_DADATA'}));path.chmod(0o600)
            with patch.dict(os.environ,{'GAB_INTEGRATIONS_FILE':str(path),'DADATA_API_KEY':'OLD','CHECKO_API_KEY':'PRIVATE_CHECKO'}):
                self.assertEqual(api_key('DADATA_API_KEY'),'PRIVATE_DADATA')
                self.assertEqual(configuration(),{'dadata_configured':True,'checko_configured':True})
                self.assertNotIn('PRIVATE',json.dumps(configuration()))
                path.chmod(0o644)
                with self.assertRaises(HTTPException):configuration()

    def test_config_writer_is_atomic_private_and_refuses_repository(self):
        spec=importlib.util.spec_from_file_location('configure_integrations',Path(__file__).resolve().parents[1]/'scripts/configure-integrations.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'integrations.private.json'
            module.write_private(path,{'DADATA_API_KEY':'SYNTHETIC'},os.getuid(),os.getgid())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(path.read_text())['DADATA_API_KEY'],'SYNTHETIC')
            self.assertEqual(list(Path(tmp).glob('.integrations-*')),[])
        with self.assertRaises(ValueError):module.write_private(Path(__file__).parent/'integrations.private.json',{},os.getuid(),os.getgid())

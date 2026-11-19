"""Office-server defaults. Private overrides live outside Git."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'compose_file': '/root/ai-stack/docker-compose.yml',
    'crm_container': 'gab-review',
    'postgres_container': 'ai-stack-postgres_ai-1',
    'postgres_user': 'n8n_user',
    'database': 'gab_crm',
    'n8n_container': 'ai-stack-n8n_ai-1',
    'n8n_version': '2.42.7',
    'ocr_service': 'gab_ocr',
    'ocr_version': '0.4.6',
    'backup_dir': '/var/backups/gab-invest',
    'state_dir': '/var/lib/gab-invest/deployments',
    'age_identity': '/root/.config/gab-invest/backup.agekey',
    'age_recipient': '',
    'publication_wait_seconds': 35,
    'publication_timeout_seconds': 180,
    'health_attempts': 20,
    'extra_config_paths': [],
}


def load_config():
    config = dict(DEFAULTS)
    path = Path(os.environ.get('GAB_DEPLOY_CONFIG', '/etc/gab-invest/deploy.json'))
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError('Private deploy.json must have permissions 0600')
        overrides = json.loads(path.read_text())
        if not isinstance(overrides, dict) or set(overrides) - set(DEFAULTS):
            raise ValueError('Unknown deployment configuration keys')
        config.update(overrides)
    return config

#!/usr/bin/env bash
# Compatibility notice: server configuration is centralized in deploy_config.py.
# Shell deployment entry points delegate to deploy.py; no environment-variable chain.
set -euo pipefail
umask 077

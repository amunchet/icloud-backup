#!/bin/bash

python3 -m venv .venv
source .venv/bin/activate
pip install icloudpy pyyaml

# clone your repo first
git clone git@gitlab.com:amunchet/obsidian.git /srv/obsidian-vault-backup
cd /srv/obsidian-vault-backup

# run
python3 /home/amunchet/icloud-backup/backup.py --config /home/amunchet/icloud-backup/config.yml


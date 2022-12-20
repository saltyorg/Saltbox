#!/bin/bash
printf '' > /srv/git/saltbox/ansible-update.log
{
    python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade --root-user-action=ignore apprise certbot
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade pip setuptools wheel
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade pyOpenSSL requests netaddr jmespath jinja2 ansible">=7.0.0,<8.0.0"
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade ruamel.yaml tld argon2_cffi ndg-httpsclient
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade dnspython lxml jmespath passlib PyMySQL
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade docker
    cp /srv/ansible/venv/bin/ansible* /usr/local/bin/
} >> /srv/git/saltbox/ansible-update.log 2>&1

returnValue=$?

if [ $returnValue -ne 0 ]; then
    echo "An Error occurred check '/srv/git/saltbox/ansible-update.log' for more information."
fi

exit $returnValue

#!/bin/bash
python -m pip uninstall -y ansible ansible-base
python3 -m pip uninstall -y ansible ansible-base
python3 -m pip install ansible">=3.0.0,<4.0.0"

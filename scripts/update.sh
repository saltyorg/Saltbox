#!/bin/bash
python3 -m pip uninstall -y ansible ansible-base
python3 -m pip install ansible">=4.0.0,<5.0.0"

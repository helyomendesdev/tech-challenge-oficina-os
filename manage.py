#!/usr/bin/env python
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
    sys.path.insert(0, str(BASE_DIR))
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
